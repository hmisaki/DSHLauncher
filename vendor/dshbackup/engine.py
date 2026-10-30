"""引擎：跑备份、执行淘汰、恢复。

一次备份 = 扫描 → 7z 压缩 → 校验 → 写 manifest → 淘汰
store 例外：robocopy 镜像，永不打包、永不轮转
"""
from __future__ import annotations

import json
import os
import shutil
import stat as _stat
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import manifest, retention, retention_state, sevenzip
from .sevenzip import RunResult, human_bytes


def _force_rmtree(path) -> None:
    """删目录，遇只读文件先去掉只读位再删。

    7z 解出来的 `.git/objects/pack/*.idx` 之类常是只读的，
    `shutil.rmtree(..., ignore_errors=True)` 会静默留下一整个 staging 目录。
    """
    def _onexc(func, p, exc):
        try:
            os.chmod(p, _stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onexc=_onexc)
    except Exception:
        pass

DISK_LOW_GB = 5.0   # 兼容旧引用；实际取值走 ui.disk_low_warn_gb / ui.disk_hard_min_gb

# 落位恢复前的兜底包（v1 有、v2 一直没实现的那个「兜底\ 只留 3 份」）
FALLBACK_DIR = "兜底"
FALLBACK_KEEP = 3
# 回收区：记录「移到回收区的时间」用这个文件名，30 天清理按它算，不看原文件时间
DELETED_AT = ".deleted-at"


def disk_low_warn_gb(cfg: dict) -> float:
    """底栏「余量偏低」的提示阈值（默认 50 GB，配置项 ui.disk_low_warn_gb）。"""
    try:
        return float(cfg.get("ui", {}).get("disk_low_warn_gb", 50.0))
    except (TypeError, ValueError):
        return 50.0


def disk_hard_min_gb(cfg: dict) -> float:
    """真正拒绝开备份的硬底线。

    刻意与提示阈值分开：50 GB 是「该清盘了」的提醒，不是封锁线——若拿它当硬拦，
    盘上还剩 49 GB 就拒绝备份，等于把警告变成事故。硬底线默认 5 GB，且永远不高于提示阈值。
    """
    warn = disk_low_warn_gb(cfg)
    try:
        hard = float(cfg.get("ui", {}).get("disk_hard_min_gb", DISK_LOW_GB))
    except (TypeError, ValueError):
        hard = DISK_LOW_GB
    return min(hard, warn)


def recycle_keep_days(cfg: dict) -> int:
    try:
        return int(cfg.get("ui", {}).get("recycle_keep_days", 30))
    except (TypeError, ValueError):
        return 30


def recycle_warn_gb(cfg: dict) -> float:
    try:
        return float(cfg.get("ui", {}).get("recycle_warn_gb", 5.0))
    except (TypeError, ValueError):
        return 5.0


class Progress:
    """线程安全进度黑板（UI 轮询用）。"""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._s = {
            "busy": False,
            "kind": "",
            "phase": "",
            "pct": 0.0,
            "detail": "",
            "startedAt": 0,
            "message": "",
        }

    def start(self, kind: str, phase: str) -> None:
        with self._lock:
            self._s = {
                "busy": True,
                "kind": kind,
                "phase": phase,
                "pct": 0.0,
                "detail": "",
                "startedAt": int(time.time() * 1000),
                "message": "",
            }

    def update(self, pct: float | None = None, detail: str | None = None,
               phase: str | None = None) -> None:
        with self._lock:
            if pct is not None:
                self._s["pct"] = round(float(pct), 1)
            if detail is not None:
                self._s["detail"] = detail
            if phase is not None:
                self._s["phase"] = phase

    def stage(self, message: str) -> None:
        """切换到一个不按百分比计量的收尾步骤。"""
        self.update(detail=str(message), phase=str(message))

    def finish(self, message: str = "", ok: bool = True) -> None:
        with self._lock:
            self._s["busy"] = False
            self._s["pct"] = 100.0 if ok else self._s["pct"]
            self._s["message"] = message
            self._s["phase"] = "完成" if ok else "失败"

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._s)


# ---------------- 扫描统计 ----------------

def count_tree(root: str | Path, exclude_dirs: list[str],
               exclude_paths: list[str] | None = None,
               on_progress=None, every: int = 2000) -> tuple[int, int]:
    """统计 root 下文件数与字节。

    排除语义必须与 7z 的 `-xr!<名字>` 一致：**任意层级**出现该名字的目录就整棵剪掉，
    否则这里算出来的「预期文件数」会包含 7z 实际排掉的内容，演练比对永远对不上。
    链接（含 junction）不跟随、也不计数——它们不进包，恢复时按 links.txt 重建。

    `on_progress(files, size)`：每 `every` 个文件回调一次。扫描 6 万文件期间界面
    原本一动不动像卡死，靠它报「扫描中 12,340 个」。
    """
    root = Path(root)
    files = 0
    size = 0
    excl_names = {str(d).lower() for d in (exclude_dirs or [])}
    excl_paths = {os.path.normcase(os.path.abspath(str(p))) for p in (exclude_paths or [])}
    for cur, dirnames, filenames in os.walk(root, followlinks=False):
        curp = Path(cur)
        kept = []
        for d in dirnames:
            child = curp / d
            key = os.path.normcase(os.path.abspath(str(child)))
            if d.lower() in excl_names or key in excl_paths:
                continue
            if sevenzip.is_link(child):
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            fp = curp / f
            key = os.path.normcase(os.path.abspath(str(fp)))
            if key in excl_paths or sevenzip.is_link(fp):
                continue
            try:
                st = fp.stat(follow_symlinks=False)
            except OSError:
                continue
            files += 1
            size += st.st_size
            if on_progress is not None and files % every == 0:
                on_progress(files, size)
    if on_progress is not None:
        on_progress(files, size)
    return files, size


# ---------------- store 镜像 ----------------

def mirror_store(cfg: dict, root: Path, progress: Progress | None = None,
                 logger=None) -> dict:
    src = cfgmod.expand(cfg["paths"]["pnpm_store"], cfg)
    dst = Path(root) / cfgmod.KIND_STORE
    if progress:
        progress.start(cfgmod.KIND_STORE, "更新store")
    f0, s0 = count_tree(src, [])
    res = sevenzip.mirror_dir(
        cfg, src, dst,
        on_progress=lambda p, line: progress and progress.update(p, line),
    )
    f1, s1 = count_tree(dst, [])
    ok = res.ok and f1 >= f0 * 0.99
    state = {
        "syncedAt": int(time.time() * 1000),
        "syncedAtIso": datetime.now().isoformat(timespec="seconds"),
        "files": f1,
        "bytes": s1,
        "sourceFiles": f0,
        "sourceBytes": s0,
        "seconds": round(res.seconds, 1),
        "ok": ok,
        "note": "" if ok else f"robocopy 返回 {res.code}",
    }
    manifest.write_mirror_state(root, state)
    if logger:
        (logger.info if ok else logger.warn)("store_sync", **state)
    if progress:
        progress.finish(f"store 已同步：{f1:,} 文件 / {human_bytes(s1)}，{res.seconds:.1f}s", ok)
    return {"ok": ok, "files": f1, "bytes": s1, "sourceFiles": f0,
            "seconds": round(res.seconds, 1), "dir": str(dst)}


# ---------------- 7z 类备份 ----------------

def _copy_for_pack(src: Path, dst: Path, exclude_dirs: list[str],
                   exclude_paths: list[str] | None = None,
                   on_progress=None) -> tuple[int, int, int]:
    """把源目录整棵复制到 dst，供 7z 压缩。

    为什么要多这一步：7z 直接读源目录时会把正在被其他进程写入的文件锁住。
    `~/.dsh/sessions` 里那份 `session.v4.jsonl.zstd` 是网关自己正在追加的，
    被 7z 锁住之后网关写不进去，那一轮直接报 EBUSY 失败。

    复制阶段用 shutil.copy2（底层是普通 open，共享读写），只短暂读一下源文件，
    复制完 7z 随便压 staging，源目录不再被长时间占用。
    复制途中仍在变的文件最多复制到某一时刻的内容，这对会话日志是可接受的。

    返回 (files, bytes, skipped)；skipped 是被占住实在拷不了的文件数。
    """
    dst.mkdir(parents=True, exist_ok=True)
    excl = {d.lower() for d in exclude_dirs}
    excl_paths = {os.path.normcase(os.path.abspath(str(p))) for p in (exclude_paths or [])}

    def excluded(path: Path) -> bool:
        return os.path.normcase(os.path.abspath(str(path))) in excl_paths

    files = 0
    total = 0
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(src):
        cur = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in excl and not excluded(cur / d)
                       and not sevenzip.is_link(cur / d)]
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else (dst / rel)
        target_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            sp = Path(dirpath) / fn
            if excluded(sp) or sevenzip.is_link(sp):
                continue
            tp = target_dir / fn
            try:
                shutil.copy2(sp, tp)
            except OSError:
                skipped += 1
                continue
            files += 1
            try:
                total += tp.stat().st_size
            except OSError:
                pass
        if on_progress and files and files % 500 == 0:
            on_progress(files, total)
    if on_progress:
        on_progress(files, total)
    return files, total, skipped


def backup_kind(cfg: dict, root: Path, kind: str, *, note: str = "",
                progress: Progress | None = None, logger=None) -> dict:
    kcfg = cfg["kinds"][kind]
    if not kcfg.get("enabled", True):
        return {"ok": False, "reason": "该类型已停用"}
    free = cfgmod.disk_free_gb(root)
    hard = disk_hard_min_gb(cfg)
    if free < hard:
        if progress:
            progress.finish(f"磁盘余量不足（{free:.1f} GB < {hard:.1f} GB）", ok=False)
        if logger:
            logger.error("disk_low", freeGb=round(free, 1), needGb=hard)
        return {"ok": False, "reason": f"磁盘余量不足 {free:.1f} GB"}

    roots = cfgmod.resolve_sets(cfg, kind)
    # 空路径必须单独判：Path("") 在 Windows 上 exists() 返回 True，
    # 会一路走到 7z 把当前盘根当源压进去（2026-10-28 实际发生过，打出一个 2.8 GB 的假 harness 包）。
    blank = [r["label"] for r in roots if not str(r.get("path") or "").strip()]
    if blank:
        return {"ok": False,
                "reason": "配置里的源路径为空：" + ", ".join(blank)
                          + "（检查 config.json 的 paths.checkout / paths.workspace）"}
    missing = [r["path"] for r in roots if not Path(r["path"]).exists()]
    if missing:
        return {"ok": False, "reason": "源不存在：" + ", ".join(missing)}

    ts = int(time.time() * 1000)
    folder = manifest.slot_dir(root, kind, ts)
    folder.mkdir(parents=True, exist_ok=True)
    if progress:
        progress.start(kind, f"备份 {cfgmod.kind_label(kind)}")
    if logger:
        logger.info("run_start", kind=kind, folder=str(folder))

    exclude_links = cfgmod.resolve_exclude_links(cfg, kind)
    parts: list[dict] = []
    links = sevenzip.scan_links(roots, exclude_links)
    empties = sevenzip.scan_empty_dirs(roots, exclude_links)
    n = len(roots)

    def _rel_to(p: str, base: str) -> str | None:
        try:
            rel = os.path.relpath(p, base)
        except ValueError:
            return None
        return None if rel.startswith("..") else rel

    single = bool(kcfg.get("single_archive", False))
    auto_links = [l["link"] for l in links]

    if single:
        # 单包模式：所有源打进同一个归档。
        # 7z 加绝对路径时默认只保留最后一级目录名作顶层，所以包内路径是 <目录名>\<相对路径>。
        stats: list[tuple[dict, int, int]] = []
        for r in roots:
            if progress:
                progress.update(phase="扫描中 0 个", detail=f"{r['label']}: 正在数文件")

            def scan_prog(files: int, size: int, label=r["label"]):
                if progress:
                    progress.update(
                        phase=f"扫描中 {files:,} 个",
                        detail=f"{label}: 已扫到 {files:,} 个文件 / {human_bytes(size)}",
                    )

            src_files, src_bytes = count_tree(
                r["path"], r["exclude_dirs"], exclude_paths=exclude_links + auto_links,
                on_progress=scan_prog,
            )
            stats.append((r, src_files, src_bytes))

        total_files = sum(s[1] for s in stats)
        total_bytes = sum(s[2] for s in stats)

        arcs = [Path(r["path"]).name for r, _, _ in stats]
        dup = sorted({a for a in arcs if arcs.count(a) > 1})
        if dup:
            return {"ok": False,
                    "reason": "多个源的目录名相同，打进同一个包会互相覆盖：" + ", ".join(dup)}

        excl_dirs_union = sorted({d for r in roots for d in r["exclude_dirs"]})
        excl_pat: list[str] = []
        for r in roots:
            arc = Path(r["path"]).name
            for ep in list(exclude_links) + auto_links:
                rel = _rel_to(ep, r["path"])
                if rel is not None:
                    excl_pat.append(f"{arc}\\{rel}")

        archive = folder / f"{kind}.7z"
        if progress:
            progress.update(
                phase=f"压缩中 0%（共 {total_files:,} 个）",
                detail=f"单包：{len(roots)} 个源 / {human_bytes(total_bytes)}",
            )

        def prog_single(pct: float, line: str):
            if progress:
                progress.update(pct, line, phase=f"压缩中 {pct:.0f}%（共 {total_files:,} 个）")

        pack = sevenzip.pack_7z_multi(
            cfg, archive,
            [{"path": r["path"], "label": r["label"]} for r in roots],
            exclude_dirs=excl_dirs_union,
            exclude_paths=excl_pat,
            on_progress=prog_single,
        )
        verified = False
        err = ""
        archive_files = None
        if pack.ok:
            if progress:
                progress.update(phase="校验中（单包）", detail="7z t 逐字节校验")
            t = sevenzip.test_7z(cfg, archive)
            if t.ok:
                info = sevenzip.archive_info(cfg, archive)
                archive_files = info.get("files")
                if not info.get("ok"):
                    err = "无法读取归档文件数：" + (info.get("error") or "7z l 未返回统计")
                elif archive_files != total_files:
                    err = (f"归档完整性不符：源目录扫描到 {total_files:,} 个文件，"
                           f"归档实际只有 {archive_files:,} 个；请关闭占用文件的程序后重试。")
                else:
                    verified = True
            else:
                err = "7z t 校验失败：" + (t.error or t.tail_text[-200:])
        else:
            err = (f"7z 压缩退出码 {pack.code}（警告或失败）："
                   + (pack.error or pack.tail_text[-400:]))

        sha = manifest.file_sha256(archive) if archive.exists() else ""
        for r, src_files, src_bytes in stats:
            part = {
                "label": r["label"],
                "target": r["path"],
                "arcName": Path(r["path"]).name,
                "zipName": archive.name,
                "zipPath": str(archive),
                "singleArchive": True,
                "sourceFiles": src_files,
                "sourceBytes": src_bytes,
                "excludeDirs": r["exclude_dirs"],
                "seconds": round(pack.seconds, 1),
                "ok": pack.ok,
                "bytes": archive.stat().st_size if archive.exists() else 0,
                "files": src_files,
                "verifiedOk": verified,
            }
            if archive_files is not None:
                part["archiveFilesTotal"] = archive_files
            if not pack.ok:
                part["error"] = err
                if getattr(pack, "disk_full", False):
                    part["diskFull"] = True
                    part["reason"] = "磁盘空间不够，没写成"
            elif err:
                part["error"] = err
            if sha:
                part["sha256"] = sha
            parts.append(part)
    else:
        for idx, r in enumerate(roots):
            # 分阶段（硬伤 3）：扫描 → 压缩 → 校验，每段都有会动的数字。
            # 扫描阶段还不知道总数（一次遍历才能数出来），所以那段报「已扫到 N 个」；
            # 数完的瞬间把总数告诉用户，压缩阶段就变成「压缩中 58%（共 61,113）」。
            if progress:
                progress.update(phase=f"扫描中 0 个", detail=f"{r['label']}: 正在数文件")

            def scan_prog(files: int, size: int, label=r["label"]):
                if progress:
                    progress.update(
                        phase=f"扫描中 {files:,} 个",
                        detail=f"{label}: 已扫到 {files:,} 个文件 / {human_bytes(size)}",
                    )

            src_files, src_bytes = count_tree(
                r["path"], r["exclude_dirs"], exclude_paths=exclude_links + auto_links,
                on_progress=scan_prog,
            )
            archive = folder / f"{r['label']}.7z"

            # stage_copy：先把源复制到 slot 里的 _staging，再压这一份。
            # sessions 必须这么做 —— 7z 直接压会锁住网关正在追加的 session.v4.jsonl.zstd，
            # 网关那一轮就会报 EBUSY 失败（2026-10-26 实际发生过）。
            stage = bool(kcfg.get("stage_copy", False)) or kind == "sessions"
            pack_src = r["path"]
            pack_excl_dirs = r["exclude_dirs"]
            staging_dir = None
            copy_skipped = 0
            if stage:
                staging_dir = folder / "_staging" / r["label"]
                if progress:
                    progress.update(
                        phase="复制中 0 个",
                        detail=f"{r['label']}: 先拷一份再压（不占用源文件）",
                    )

                def copy_prog(files: int, size: int, label=r["label"]):
                    if progress:
                        progress.update(
                            phase=f"复制中 {files:,} 个",
                            detail=f"{label}: 已复制 {files:,} 个 / {human_bytes(size)}",
                        )

                cf, cb, copy_skipped = _copy_for_pack(
                    Path(r["path"]), staging_dir, r["exclude_dirs"],
                    exclude_paths=exclude_links + auto_links, on_progress=copy_prog,
                )
                pack_src = str(staging_dir)
                pack_excl_dirs = []
                if logger:
                    logger.info("staged_copy", label=r["label"], files=cf,
                                bytes=cb, skipped=copy_skipped)

            if progress:
                progress.update(
                    phase=f"压缩中 0%（共 {src_files:,} 个）",
                    detail=f"{r['label']}: {src_files:,} 个文件 / {human_bytes(src_bytes)}",
                )

            def prog(pct: float, line: str, idx=idx, label=r["label"], total=src_files):
                if progress:
                    overall = (idx * 100.0 + pct) / n
                    progress.update(
                        overall,
                        f"{label}: {line}",
                        phase=f"压缩中 {pct:.0f}%（共 {total:,} 个）",
                    )

            # 排除项 = 配置里点名的 + 这次扫到的所有 junction（否则 7z 会把链接条目压进包，
            # 解压时又还原成链接，导致演练比对的文件数对不上）。
            auto_links = [l["link"] for l in links]

            def _rel(p: str, base: str) -> str | None:
                try:
                    rel = os.path.relpath(p, base)
                except ValueError:
                    return None
                return None if rel.startswith("..") else rel

            excl_rels = [
                rel for rel in (_rel(p, r["path"]) for p in exclude_links + auto_links)
                if rel is not None
            ]

            pack = sevenzip.pack_7z(
                cfg, archive, pack_src,
                exclude_dirs=pack_excl_dirs,
                exclude_paths=excl_rels,
                on_progress=prog,
            )
            part = {
                "label": r["label"],
                "target": r["path"],
                "zipName": archive.name,
                "zipPath": str(archive),
                "sourceFiles": src_files,
                "sourceBytes": src_bytes,
                "excludeDirs": r["exclude_dirs"],
                "seconds": round(pack.seconds, 1),
                "ok": pack.ok,
                "bytes": archive.stat().st_size if archive.exists() else 0,
                "files": src_files,
                "verifiedOk": False,
            }
            if not pack.ok:
                part["error"] = (f"7z 压缩退出码 {pack.code}（警告或失败）："
                                  + (pack.error or pack.tail_text[-400:]))
                if getattr(pack, "disk_full", False):
                    # 体检 E：磁盘满是一个「你能自己解决」的原因，不能只说「失败了」
                    part["diskFull"] = True
                    part["reason"] = "磁盘空间不够，没写成"
            else:
                if progress:
                    progress.update(phase=f"校验中（{r['label']}）",
                                    detail=f"{r['label']}: 7z t 逐字节校验")
                t = sevenzip.test_7z(cfg, archive)
                if t.ok:
                    info = sevenzip.archive_info(cfg, archive)
                    part["archiveFiles"] = info.get("files")
                    if not info.get("ok"):
                        part["error"] = "无法读取归档文件数：" + (info.get("error") or "7z l 未返回统计")
                    elif info["files"] != src_files:
                        part["error"] = (
                            f"归档完整性不符：源目录扫描到 {src_files:,} 个文件，"
                            f"归档实际只有 {info['files']:,} 个；请关闭占用文件的程序后重试。"
                        )
                    else:
                        part["verifiedOk"] = True
                else:
                    part["error"] = "7z t 校验失败：" + (t.error or t.tail_text[-200:])
                part["sha256"] = manifest.file_sha256(archive)
            if staging_dir is not None and staging_dir.exists():
                _force_rmtree(staging_dir)
                try:
                    staging_dir.parent.rmdir()
                except OSError:
                    pass
            if copy_skipped:
                part["copySkipped"] = copy_skipped
                part["verifiedOk"] = False
                skipped_detail = f"复制阶段有 {copy_skipped} 个文件无法读取，归档不完整。"
                part["error"] = f"{part['error']}\n{skipped_detail}" if part.get("error") else skipped_detail
            parts.append(part)

    manifest.write_link_lists(folder, links, empties)
    slot = manifest.make_slot(
        kind, ts, parts,
        links=len(links), empty_dirs=len(empties), note=note,
    )
    manifest.write_manifest(folder, slot)

    ok = all(p.get("ok") and p.get("verifiedOk") for p in parts)
    retention_state.note_verified(root, kind, ok, slot["slot"])
    if logger:
        logger.info(
            "run_done", kind=kind, slot=slot["slot"], ok=ok,
            files=slot["files"], bytes=slot["bytes"], links=len(links),
            parts=[{key: p.get(key) for key in
                    ("label", "ok", "sourceFiles", "archiveFiles", "archiveFilesTotal",
                     "verifiedOk", "copySkipped", "error")}
                   for p in parts],
        )

    pruned = None
    if ok:
        pruned = prune_kind(cfg, root, kind, apply=True, logger=logger)

    if progress:
        disk_full = any(p.get("diskFull") for p in parts)
        if disk_full and not ok:
            # 体检 E：进度条那句也得说人话，不然用户只看到「失败」
            progress.finish("磁盘空间不够，这次没存成", False)
        else:
            first_error = next((p.get("error") for p in parts if p.get("error")), "")
            progress.finish(
                (f"{cfgmod.kind_label(kind)} 完成：{len(parts)} 个包 / {human_bytes(slot['bytes'])}"
                 + (f"；淘汰 {pruned['deleted']} 份" if pruned and pruned.get("deleted") else "")
                 if ok else f"{cfgmod.kind_label(kind)} 备份未通过完整性校验：{first_error or '请查看日志'}"),
                ok,
            )
    # 单包模式下多个源共享同一个归档，耗时也不能按源累加
    _sec_by_arch: dict[str, float] = {}
    for _p in parts:
        _sec_by_arch.setdefault(str(_p.get("zipPath") or _p["label"]), _p.get("seconds", 0))

    first_error = next((p.get("error") for p in parts if p.get("error")), "")
    return {
        "ok": ok,
        "diskFull": any(p.get("diskFull") for p in parts),
        "reason": ("磁盘空间不够，没写成" if any(p.get("diskFull") for p in parts)
                   else first_error),
        "slot": slot["slot"],
        "dir": str(folder),
        "files": slot["files"],
        "bytes": slot["bytes"],
        "sourceFiles": slot["sourceFiles"],
        "sourceBytes": slot["sourceBytes"],
        "links": len(links),
        "emptyDirs": len(empties),
        "singleArchive": bool(kcfg.get("single_archive", False)),
        "seconds": round(sum(_sec_by_arch.values()), 1),
        "pruned": pruned,
        "parts": [
            {"label": p["label"], "bytes": p["bytes"], "verifiedOk": p.get("verifiedOk"),
             "seconds": p.get("seconds"), "zipName": p.get("zipName"),
             "sourceFiles": p.get("sourceFiles"), "archiveFiles": p.get("archiveFiles"),
             "error": p.get("error"),
             "zipPath": p.get("zipPath"), "arcName": p.get("arcName"),
             "singleArchive": p.get("singleArchive")}
            for p in parts
        ],
    }


# ---------------- 淘汰 ----------------

def prune_kind(cfg: dict, root: Path, kind: str, *, apply: bool = True,
               logger=None) -> dict:
    params = cfgmod.kind_params(cfg, kind)
    if params is None:
        return {"skipped": True, "reason": "该类型不参与自动淘汰"}
    slots = manifest.scan(root, kind)
    now_ms = int(time.time() * 1000)
    stats: dict = {}
    keep, hold, drop = retention.select_keep(now_ms, slots, params, stats)
    result = {
        "kind": kind,
        "total": len(slots),
        "keep": [s["slot"] for s in keep],
        "hold": [s["slot"] for s in hold],
        "drop": [s["slot"] for s in drop],
        "emptyBuckets": stats.get("emptyBuckets", []),
        "bounds": stats.get("bounds", []),
        "deleted": 0,
        "deferred": False,
    }
    if logger and stats.get("emptyBuckets"):
        logger.info("empty_buckets", kind=kind, buckets=stats["emptyBuckets"])
    if apply:
        state = retention_state.load(root)
        applied = retention_state.apply_prune(root, kind, keep, hold, drop, state, params, logger)
        result.update(applied)
    return result


# ---------------- 恢复 ----------------

# 恢复时「落到别处」只认这三个角色：harness / dsh / workspace。
# store 不进包（它是 pnpm 缓存，装依赖时自己会长回来），所以不参与恢复。
ROOT_ROLES = ("harness", "dsh", "workspace")


def _role_new_root(kind: str, label: str, roots: dict) -> Path | None:
    """某个 part（或工作区的某一根）在新环境里应该落到哪。

    会话永远跟着 dsh 走（它就在 .dsh\\sessions 下面），所以不用单独指定。
    """
    h = (roots or {}).get("harness")
    d = (roots or {}).get("dsh")
    w = (roots or {}).get("workspace")
    if kind == cfgmod.KIND_BASELINE:
        if label == "harness" and h:
            return Path(h)
        if label == "dsh" and d:
            return Path(d)
    elif kind == cfgmod.KIND_SESSIONS:
        if d:
            return Path(d) / "sessions"
    elif kind == cfgmod.KIND_WORKSPACE:
        if w:
            return Path(w) / label
    return None


def _relocate_path(path: str, cfg: dict, kind: str, roots: dict, *,
                   only_kind: bool = False) -> Path | None:
    """把「原根下的绝对路径」改写成「新根 + 相对路径」；不在任何根下就返回 None。

    junction 的目标常常带 ``\\\\?\\`` 这种长路径前缀，先剥掉再比，否则永远匹配不上祖根。
    """
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    p = Path(raw)
    kinds = [kind]
    if not only_kind:
        for candidate in (cfgmod.KIND_BASELINE, cfgmod.KIND_SESSIONS, cfgmod.KIND_WORKSPACE):
            if candidate not in kinds:
                kinds.append(candidate)
    for candidate in kinds:
        for r in cfgmod.resolve_sets(cfg, candidate):
            try:
                sub = p.relative_to(Path(r["path"]))
            except ValueError:
                continue
            new_root = _role_new_root(candidate, r["label"], roots)
            if new_root is not None:
                return new_root / sub
    return None


def _strip_long(p: str) -> str:
    """去掉 ``\\\\?\\`` 长路径前缀。

    links.txt 里的目标全是这种形式（那是 os.readlink / 目录枚举拿到的原始串）。
    ``mklink`` 不认这个前缀，会把它当字面目录名，所以写脚本前必须剥掉。
    """
    raw = str(p)
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def write_links_script(folder: Path, links: list[dict], *,
                       dep_skipped: int = 0, install_hint: str = "") -> Path:
    """把目录联接（junction）的重建命令写成一份 .cmd，交给人跑。

    为什么不自动建：junction 的目标是写死的绝对路径，换盘换机之后原目标可能根本不存在，
    自动 mklink 会静默失败或指到旧位置。写成脚本，人能一条条看、一条条改。

    只写「目标在新根之外」的那些——那是我们自己造的映射（例如把 ``D:\\AIBot\\tools\\xxx``
    挂进某个工作区的 ``.dsh\\skills``）。pnpm / workspace 生成的依赖链接
    （node_modules → .pnpm、vendor 互链）由 ``pnpm install`` 自己长回来，写进脚本只会多几千行噪音。
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "restore-links.cmd"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "rem DSH backup - rebuild directory junctions after a restore",
        "rem 每条 mklink 独立执行；失败只打印原因，不影响后面几条。",
        "rem 如果目标路径在本机不存在，先改这一行的第二个参数再跑。",
        "",
    ]
    if dep_skipped:
        lines += [
            f"rem 另有 {dep_skipped} 条链接没列在这里：它们是 pnpm / workspace 装出来的依赖",
            "rem （node_modules -> .pnpm，或 vendor 之间互链），装依赖时自己会重建。先跑：",
            f"rem   {install_hint}" if install_hint else "rem   pnpm install",
            "",
        ]
    if not links:
        lines += [
            "rem 这次没有需要手工重建的链接 —— 上面那条 install 跑完就齐了。",
            "",
        ]
    for l in links:
        link = _strip_long(l["link"])
        target = _strip_long(l["target"])
        # 可重复执行：旧 junction 用 rmdir 删除的是链接本身，不会递归删除目标；
        # 普通非空目录的 rmdir 会失败，因此不会清掉用户文件。
        lines.append(f'if exist "{link}" rmdir "{link}" >nul 2>&1')
        lines.append(f'mklink /J "{link}" "{target}"')
    lines += ["", "echo done.", 'if not "%~1"=="-y" pause', ""]
    script.write_text("\r\n".join(lines), encoding="gbk", errors="replace")
    return script


LAUNCHER_NAME = "start-dsh.cmd"

_LAUNCHER_BODY = r"""@echo off
rem ============================================================================
rem  start-dsh.cmd - written by the DSH backup tool after a restore.
rem
rem  Every path here is relative to this file (%~dp0), so a restored copy runs
rem  from wherever it landed. That is the point: no absolute paths.
rem
rem  If a sibling ".dsh" folder exists next to this harness folder, it becomes
rem  DSH_HOME. That is exactly what "restore into <target>" produces:
rem      <target>\deepseek-harness   <- this file lives here
rem      <target>\.dsh               <- picked up automatically
rem
rem  Delete this file once you no longer need it; it is only a convenience.
rem ============================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PNPM=%APPDATA%\npm\pnpm.cmd"

set "CAND=%~dp0..\.dsh"
for %%I in ("%CAND%") do set "CAND=%%~fI"
if exist "%CAND%" set "DSH_HOME=%CAND%"

rem  Optional: an outbound proxy, if this machine needs one to reach the model.
rem  Uncomment and point at your own proxy.
rem  set "HTTPS_PROXY=http://127.0.0.1:7890"
rem  set "HTTP_PROXY=http://127.0.0.1:7890"
rem  set "NO_PROXY=localhost,127.0.0.1"

rem  Optional: extra authority the /api browser-trust fence accepts when the UI
rem  sits behind a reverse proxy. Set DSH_TRUSTED_HOST before running this file.
if "%DSH_TRUSTED_HOST%"=="" (set "TRUST=") else (set "TRUST=--trusted-host %DSH_TRUSTED_HOST%")

if not exist "%PNPM%" goto nopnpm
if not exist "%ROOT%\apps\cli\src\bin.ts" goto nosrc

netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":3080 " >nul 2>&1
if not errorlevel 1 goto busy

cd /d "%ROOT%"
title DSH Gateway - %ROOT%
echo [DSH] checkout : %ROOT%
if defined DSH_HOME echo [DSH] DSH_HOME : %DSH_HOME%
echo [DSH] Close this window to stop the gateway.
echo.
call "%PNPM%" dsh web --port 3080 %TRUST%
set "RC=%ERRORLEVEL%"
echo.
echo [DSH] gateway exited with code %RC%.
if not "%~1"=="-y" pause
exit /b %RC%

:busy
echo [DSH] port 3080 is already taken - another gateway is running.
echo [DSH] close that one first. To run this copy side by side, use a free port:
echo        call "%PNPM%" dsh web --port 3081 %TRUST%
if not "%~1"=="-y" pause
exit /b 1

:nopnpm
echo [DSH] pnpm not found at "%PNPM%".  install it with: npm i -g pnpm
if not "%~1"=="-y" pause
exit /b 1

:nosrc
echo [DSH] "%ROOT%\apps\cli\src\bin.ts" is missing - not a DSH checkout.
if not "%~1"=="-y" pause
exit /b 1
"""


def make_launcher(harness: str | Path, port: int = 3080) -> Path:
    """在 harness 根写一份 start-dsh.cmd（全部 %~dp0 相对定位）。"""
    harness = Path(harness)
    script = harness / LAUNCHER_NAME
    body = _LAUNCHER_BODY.replace("3080", str(int(port)))
    script.write_text(body.replace("\n", "\r\n"), encoding="gbk", errors="replace")
    return script


def _pnpm_exe() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "npm" / "pnpm.cmd"


def store_path_for_checkout(cfg: dict, checkout: str | Path) -> Path | None:
    """把当前 pnpm store 相同的盘符相对路径映射到目标 Harness 所在盘。"""
    configured = str((cfg.get("paths") or {}).get("pnpm_store") or "").strip()
    if not configured:
        return None
    source = Path(configured).expanduser()
    target = Path(checkout).expanduser()
    if not source.is_absolute() or not source.anchor or not target.anchor:
        return source
    if os.path.normcase(source.anchor) == os.path.normcase(target.anchor):
        return source
    try:
        return Path(target.anchor) / source.relative_to(source.anchor)
    except ValueError:
        return source


def _seed_local_store(cfg: dict, backup_root: Path, target: Path,
                      progress=None, logger=None) -> dict:
    """从固定的 store 镜像增量填充目标盘的 pnpm 本地仓库。"""
    mirror = manifest.mirror_slot(backup_root)
    source = Path(backup_root) / cfgmod.KIND_STORE
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
        return {"ok": True, "copied": False, "source": str(source), "dest": str(target)}
    if not mirror or not mirror.get("onDisk") or not mirror.get("verifiedOk"):
        return {"ok": False, "copied": False, "source": str(source),
                "dest": str(target), "reason": "备份库中没有可用的 pnpm store 镜像"}

    if progress:
        progress.stage(f"复制 pnpm store 到目标盘：{target}")
    copied = sevenzip.mirror_dir(
        cfg, source, target,
        on_progress=lambda count, line: progress and progress.update(
            phase="复制 pnpm store", detail=line or f"已处理 {count:,} 个文件",
        ),
    )
    files, size = count_tree(target, [])
    expected = int(mirror.get("files") or 0)
    ok = copied.ok and files >= expected * 0.99
    result = {"ok": ok, "copied": True, "source": str(source), "dest": str(target),
              "files": files, "bytes": size, "expectedFiles": expected,
              "code": copied.code, "seconds": round(copied.seconds, 1)}
    if logger:
        (logger.info if ok else logger.warn)("restore_store_seed", **result)
    if progress:
        progress.update(phase="pnpm store 已准备好" if ok else "pnpm store 复制未完整",
                        detail=f"{files:,} / {expected:,} 个文件 · {human_bytes(size)}")
    return result


def finish_restore(cfg: dict, out: dict, *, do_install: bool = True,
                   do_links: bool = True, cleanup: bool = True,
                   backup_root: Path | None = None,
                   logger=None, progress=None) -> dict:
    """恢复后的收尾，让它「开包即用」。

    顺序有依赖，不能换：装依赖 → 建链接 → 写启动器 → 删脚本。
    - 装依赖要用 ``pnpm install --offline``，store 里有的包不再下载；
    - 链接必须装完再建，否则 pnpm 会把你刚建好的 node_modules 目录联接覆盖掉；
    - 启动器最后写，装依赖失败时也写（人还能自己修）。

    只有 relocate 才有意义：写回原位置时依赖和链接本来就在。
    """
    res = {"install": None, "links": None, "launcher": "", "removed": []}
    if out.get("mode") != "relocate":
        return res

    parts = out.get("parts") or []
    harness = None
    for p in parts:
        if p.get("label") == "harness" and p.get("ok"):
            harness = Path(p.get("dir") or "")
            break
    if harness is None or not (harness / "package.json").exists():
        harness = None

    store_path = store_path_for_checkout(cfg, harness) if harness is not None else None
    store = str(store_path or "")
    pnpm = _pnpm_exe()

    if do_install and harness is not None and store_path is not None:
        source_store = Path(str((cfg.get("paths") or {}).get("pnpm_store") or ""))
        same_as_source = os.path.normcase(os.path.abspath(source_store)) == os.path.normcase(os.path.abspath(store_path))
        if backup_root is not None and (not store_path.exists() or not same_as_source):
            res["storeSync"] = _seed_local_store(cfg, Path(backup_root), store_path,
                                                progress=progress, logger=logger)
        elif store_path.exists():
            res["storeSync"] = {"ok": True, "copied": False,
                                "source": str(store_path), "dest": str(store_path)}
        else:
            res["storeSync"] = {"ok": False, "copied": False,
                                "source": str(source_store), "dest": str(store_path),
                                "reason": "目标盘没有 store，且没有可用的 store 镜像"}

    if do_install and harness is not None and pnpm.exists():
        store_sync = res.get("storeSync")
        if store_sync and not store_sync.get("ok"):
            res["install"] = {"ok": False, "code": -1, "seconds": 0,
                              "tail": store_sync.get("reason") or "pnpm store 未准备好，跳过离线安装"}
        else:
            cmd = [str(pnpm), "install", "--offline", "--frozen-lockfile"]
            if store:
                cmd += ["--store-dir", store]
            if progress:
                progress.stage(f"正在使用 {store or 'pnpm 本地 store'} 离线安装依赖…")
            t0 = time.time()
            try:
                cp = subprocess.run(cmd, cwd=str(harness), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=7200,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                tail = ((cp.stdout or "") + (cp.stderr or "")).strip().splitlines()
                res["install"] = {
                    "ok": cp.returncode == 0,
                    "code": cp.returncode,
                    "seconds": round(time.time() - t0, 1),
                    "tail": "\n".join(tail[-6:]),
                }
            except Exception as exc:                                  # noqa: BLE001
                res["install"] = {"ok": False, "code": -1,
                                  "seconds": round(time.time() - t0, 1),
                                  "tail": f"{type(exc).__name__}: {exc}"}
        if logger:
            logger.info("restore_install", ok=res["install"]["ok"],
                        code=res["install"]["code"], seconds=res["install"]["seconds"])

    if do_links and harness is not None:
        script = out.get("linksScript") or ""
        script_path = Path(script) if script and Path(script).exists() else None
        if script_path is not None:
            if progress:
                progress.stage("正在重建目录联接…")
            try:
                cp = subprocess.run(["cmd.exe", "/c", str(script_path), "-y"],
                                    capture_output=True, text=True, encoding="gbk",
                                    errors="replace", timeout=600,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                body = (cp.stdout or "") + (cp.stderr or "")
                failed = body.count("失败") + body.lower().count("cannot create")
                res["links"] = {"ok": cp.returncode == 0 and failed == 0,
                                "code": cp.returncode, "failedLines": failed,
                                "tail": "\n".join(body.strip().splitlines()[-6:])}
            except Exception as exc:                              # noqa: BLE001
                res["links"] = {"ok": False, "code": -1, "failedLines": 0,
                                "tail": f"{type(exc).__name__}: {exc}"}
            if logger:
                logger.info("restore_links", ok=res["links"]["ok"],
                            failed=res["links"]["failedLines"])

    if harness is not None:
        # DSHLuncher2 部署的是独立环境：包内 start.cmd 可能仍然写着旧端口，
        # 因此始终生成一个相对定位、显式使用当前配置端口的启动入口。
        try:
            port = int((cfg.get("ports") or {}).get("dsh", 3080) or 3080)
            res["launcher"] = str(make_launcher(harness, port=port))
            res["launcherSource"] = "generated"
        except Exception as exc:                                  # noqa: BLE001
            res["launcher"] = f"（写启动器失败：{exc}）"
            res["launcherSource"] = "failed"

    if cleanup:
        # 只删「跑一遍就没用」的收尾脚本：restore-links.cmd、restore-links 留下的临时件。
        # start-dsh.cmd 必须留着——那是给人的入口，删了等于白写。
        cand = out.get("linksScript")
        if cand:
            p = Path(cand)
            if p.exists():
                try:
                    p.unlink()
                    res["removed"].append(str(p))
                except Exception:                                 # noqa: BLE001
                    pass

    out["finish"] = res
    return res


def _probe_rename(target: Path) -> str | None:
    """体检 F：落位前先确认目标目录「改得动名」。

    改名是「留底」和「回滚」唯一的动作：目标被别的进程占用时，落位改名失败 → 进回滚 →
    回滚也要改名 → 同样失败，用户只看到「改回原名失败」，既看不懂也救不了。
    所以先拿一个探针名试一次往返；改不回去就返回原因（此时目录名可能变了，必须立刻报错）。
    """
    target = Path(target)
    if not target.exists():
        return None
    probe = target.with_name(f"{target.name}.rename-probe-{os.getpid()}")
    try:
        target.rename(probe)
    except OSError as exc:
        return str(exc)
    try:
        probe.rename(target)
    except OSError as exc:  # 极罕见：改得出去、改不回来
        return f"探针改名回不去：{exc}（目录现在叫 {probe.name}）"
    return None


def restore_slot(cfg: dict, root: Path, kind: str, slot_id: str, *,
                 dest: str | Path | None = None,
                 into: str | Path | None = None,
                 dry_run: bool = True,
                 targets: list[str] | None = None,
                 roots: dict[str, str] | None = None,
                 auto_finish: bool = True,
                 auto_install: bool = True,
                 auto_links: bool = True,
                 progress: Progress | None = None,
                 logger=None) -> dict:
    """恢复一份包。四种落法：

    · `into` 非空 —— **恢复到指定目录**（界面就是这个）：包里每块按它原来的目录名
      铺到 `<into>\\<原目录名>`，原盘一个字节都不动；junction 写成 restore-links.cmd。
    · `roots` 非空 —— 精细控制：手工给 harness / dsh / workspace 三个新根。
    · `dest` 非空 —— 试运行：解到临时目录逐文件比对，不动真实文件。
    · 三者都空 —— 写回原路径：先打兜底包、旧目录改名 `.pre-restore-<时间>` 留底，
      失败自动回滚。
    """
    if roots and (dest or into):
        return {"ok": False, "reason": "roots（三个根）和 dest / into 只能选一个"}
    if dest and into:
        return {"ok": False,
                "reason": "dest（试运行临时目录）和 into（恢复到指定目录）只能选一个"}
    slots = manifest.scan(root, kind)
    hit = next((s for s in slots if s["slot"] == slot_id), None)
    if not hit:
        return {"ok": False, "reason": f"找不到 slot {slot_id}"}

    folder = Path(hit["dir"])
    links, empties = manifest.read_link_lists(folder)
    parts = [p for p in hit["parts"] if (not targets or p["label"] in targets)]

    # `into` 只是 `roots` 的糖：把「铺到这个目录下」翻译成每块该落到哪。
    # baseline 的两块原目录名是 deepseek-harness / .dsh；sessions 与 workspace
    # 在原根下面还有一层（sessions、<工作区名>），由 _role_new_root 拼。
    into_base: Path | None = Path(into) if into else None
    if into_base is not None:
        auto: dict[str, str] = {}
        if kind == "sessions":
            # sessions 在原根的下一层是 <DSH_HOME>\sessions，所以 dsh 根要指到 .dsh 本身，
            # 否则会铺成 <into>\sessions 而不是 <into>\.dsh\sessions。
            auto["dsh"] = str(into_base / ".dsh")
        elif kind == "workspace":
            auto["workspace"] = str(into_base)
        else:
            for p in parts:
                nm = Path(p.get("target", "")).name or p["label"]
                role = "harness" if p.get("label") == "harness" else "dsh"
                auto[role] = str(into_base / nm)
        roots = auto

    preview = {
        "slot": slot_id,
        "kind": kind,
        "dryRun": bool(dry_run),
        "dest": str(dest) if dest else "",
        "parts": [
            {
                "label": p["label"],
                "zipName": p["zipName"],
                "target": p.get("target", ""),
                "sourceFiles": p.get("sourceFiles", 0),
                "bytes": p.get("bytes", 0),
                "verifiedOk": p.get("verifiedOk", False),
                "onDisk": Path(p.get("zipPath", "")).exists(),
            }
            for p in parts
        ],
        "files": sum(int(p.get("sourceFiles", 0)) for p in parts),
        "sourceBytes": sum(int(p.get("sourceBytes", 0)) for p in parts),
        "links": len(links),
        "emptyDirs": len(empties),
    }
    # 只预览（没给 dest / into）时到此为止；给了 dest 就是真演练：解一遍再逐文件比对。
    if dry_run and not dest and not into:
        preview["ok"] = all(p["onDisk"] for p in preview["parts"])
        preview["blocks"] = [p["label"] for p in parts]
        return preview

    for p in parts:
        if not Path(p.get("zipPath", "")).exists():
            return {"ok": False, "reason": f"包文件不存在：{p.get('zipName')}"}

    reloc = bool(roots)
    drill = (dest is not None) and not reloc
    base_dest = Path(dest) if drill else None
    relocation_targets: dict[str, dict] = {}
    if reloc:
        # 先检查所有落点，再解第一个包；否则第二个目标不合格时，第一个可能已经写了一半。
        for p in parts:
            got = _role_new_root(kind, p["label"], roots)
            if got is None:
                return {"ok": False,
                        "reason": f"没有给「{p['label']}」指定新位置（要 harness / dsh / workspace）"}
            target = Path(got)
            if sevenzip.is_link(target):
                return {"ok": False, "reason": f"部署目标不能是 junction 或符号链接：{target}"}
            if target.exists() and not target.is_dir():
                return {"ok": False, "reason": f"部署目标已存在但不是目录：{target}"}
            if target.exists() and any(target.iterdir()):
                return {"ok": False,
                        "reason": f"目标目录不是空的：{target}\n落到别处要求目标为空目录，换一个位置。"}
            relocation_targets[str(target)] = {"path": target, "existed": target.exists()}
    if reloc:
        where = "、".join(f"{k}={v}" for k, v in roots.items() if v)
    else:
        where = "临时目录" if drill else "原路径"
    if progress:
        progress.start(
            kind,
            f"恢复 {cfgmod.kind_label(kind)}" + (f"（试运行）" if drill else f"（→ {where}）"),
        )

    rollback: list[dict] = []
    fallbacks: list[dict] = []
    restored: list[dict] = []
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 单包模式：一个归档里装了多个源，包内以各源目录名为顶层。
    # 解一次到暂存目录，再按目录名分发回各自的目标位置。
    single = any(bool(p.get("singleArchive")) for p in parts)
    staging: Path | None = None
    res_pack: RunResult | None = None
    if single:
        staging = ((base_dest / f"_staging_{slot_id}") if drill
                   else (root / "tmp" / f"restore-{slot_id}-{ts_tag}"))
        if staging.exists():
            _force_rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        if progress:
            progress.update(0.0, "解压单包到暂存…")
        res_pack = sevenzip.extract_7z(cfg, parts[0]["zipPath"], staging)
        if not res_pack.ok:
            _force_rmtree(staging)
            return {"ok": False,
                    "reason": "解压失败：" + (res_pack.error or res_pack.tail_text[-300:])}

    for idx, p in enumerate(parts):
        arc = p.get("arcName") or Path(p.get("target", "")).name or p["label"]
        if drill:
            target = base_dest / arc
        elif reloc:
            got = _role_new_root(kind, p["label"], roots)
            if got is None:
                return {"ok": False,
                        "reason": f"没有给「{p['label']}」指定新位置（要 harness / dsh / workspace）"}
            target = got
        else:
            target = Path(p["target"])
        if reloc and target.exists() and any(target.iterdir()):
            # 落到别处是「往干净环境里铺」，不覆盖任何已有内容
            return {"ok": False,
                    "reason": f"目标目录不是空的：{target}\n落到别处要求目标为空目录，换一个位置。"}
        if not drill and not reloc:
            # 体检 F：改不动名就别开始，别等打了一半再回滚
            lock = _probe_rename(target)
            if lock:
                reason = (
                    f"{target} 正被其他程序占用，改不了名。"
                    "先关掉占用它的程序（DSH 网关、资源管理器预览、编辑器）再来。"
                )
                if logger:
                    logger.warn("restore_target_locked", target=str(target), detail=lock,
                                kind=kind, slot=slot_id)
                if progress:
                    progress.finish("目标被占用，没动手", False)
                return {"ok": False, "reason": reason, "blocked": "target_locked",
                        "target": str(target), "detail": lock}
        target.mkdir(parents=True, exist_ok=True)
        if not drill and not reloc and target.exists() and any(target.iterdir()):
            # ① 先打兜底包（失败时靠它；也是「兜底\ 只留 3 份」的唯一来源）
            fb = pack_fallback(cfg, root, kind, p["label"], target,
                               logger=logger, progress=progress)
            fallbacks.append(fb)
            # ② 再把旧目录改名留底——rename 是毫秒级，比解压快得多，回滚首选它
            backup = target.with_name(target.name + f".pre-restore-{ts_tag}")
            try:
                target.rename(backup)
                rollback.append({"target": str(target), "backup": str(backup),
                                 "label": p["label"]})
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "reason": f"旧目录改名失败：{exc}"}

        def prog(pct: float, line: str, idx=idx, label=p["label"]):
            if progress:
                overall = (idx * 100.0 + pct) / len(parts)
                progress.update(overall, f"{label}: {line}")

        if single:
            src_dir = staging / arc
            if not src_dir.exists():
                restored.append(
                    {
                        "label": p["label"],
                        "dir": str(target),
                        "ok": False,
                        "error": f"包里没有目录 {arc}",
                        "files": 0,
                        "expectFiles": int(p.get("sourceFiles", 0)),
                        "filesMatch": False,
                        "returnCode": res_pack.code if res_pack else None,
                        "bytes": 0,
                        "seconds": 0.0,
                    }
                )
                continue
            if progress:
                progress.update((idx + 0.5) / max(len(parts), 1), f"{p['label']}: 从暂存搬入")
            for item in src_dir.iterdir():
                dst = target / item.name
                if dst.exists():
                    if dst.is_dir():
                        shutil.rmtree(dst, ignore_errors=True)
                    else:
                        dst.unlink()
                # 暂存在 backup_root 盘上，落到新位置可能跨盘：跨盘 move 只能
                # copy+删，而删源目录时撞上 .git/objects 里的只读文件会 PermissionError
                # （[WinError 17] → [WinError 5]）。所以这里一律复制，源留给
                # 收尾的 rmtree(staging) 统一清；清了失败也不影响恢复结果。
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            res = res_pack
        else:
            res = sevenzip.extract_7z(cfg, p["zipPath"], target, on_progress=prog)

        got_files, got_bytes = count_tree(target, [])
        expected_files = int(p.get("archiveFiles", p.get("sourceFiles", 0)) or 0)
        extract_error = res.error or (res.tail_text[-400:] if not res.ok else "")
        restored.append(
            {
                "label": p["label"],
                "dir": str(target),
                "ok": res.ok,
                "files": got_files,
                "expectFiles": expected_files,
                "sourceFiles": int(p.get("sourceFiles", 0)),
                "filesMatch": got_files == expected_files,
                "returnCode": res.code,
                "error": extract_error,
                "bytes": got_bytes,
                "seconds": round(res.seconds, 1),
            }
        )

    if single and staging is not None and staging.exists():
        _force_rmtree(staging)

    # 空目录补建
    made_empty = 0
    for e in empties:
        pth = Path(e["dir"])
        if reloc:
            moved = _relocate_path(str(pth), cfg, kind, roots)
            if moved is None:
                continue
            pth = moved
        elif drill:
            try:
                rel = pth.relative_to(Path(pth.anchor))
            except ValueError:
                rel = Path(pth.name)
            for r in cfgmod.resolve_sets(cfg, kind):
                rp = Path(r["path"])
                try:
                    sub = pth.relative_to(rp)
                except ValueError:
                    continue
                pth = base_dest / r["label"] / sub
                break
        try:
            pth.mkdir(parents=True, exist_ok=True)
            made_empty += 1
        except OSError:
            pass

    # junction 重建：drill 只改路径不真建；reloc 一律不建，写成脚本交给人
    rebuilt = 0
    failed_links: list[str] = []
    pending_links: list[dict] = []
    dep_links = 0
    explicit_link_keys = {
        os.path.normcase(os.path.abspath(str(path)))
        for path in cfgmod.resolve_exclude_links(cfg, kind)
    }
    for l in links:
        link = Path(l["link"])
        tgt = l["target"]
        if reloc:
            explicit_link = os.path.normcase(os.path.abspath(str(link))) in explicit_link_keys
            moved = _relocate_path(str(link), cfg, kind, roots)
            tgt_moved = _relocate_path(tgt, cfg, kind, roots)
            workspace_link = _relocate_path(
                str(link), cfg, cfgmod.KIND_WORKSPACE, roots, only_kind=True
            )
            workspace_target = _relocate_path(
                tgt, cfg, cfgmod.KIND_WORKSPACE, roots, only_kind=True
            )
            if explicit_link or workspace_link is not None or workspace_target is not None:
                # 工作区与 Harness/.dsh 之间的手工联接不会由 pnpm install 重建，
                # 配置中点名排除的联接也必须显式写入脚本；两端按部署根重定位。
                pending_links.append({
                    "link": str(moved) if moved is not None else str(link),
                    "target": str(tgt_moved) if tgt_moved is not None else tgt,
                    "note": "工作区联接已重定位" if moved is not None and tgt_moved is not None
                            else "联接有一端在新环境根之外，跑脚本前先确认目标路径",
                })
                continue
            if tgt_moved is not None:
                # 目标落在新根内部 = pnpm / workspace 装出来的依赖链接
                # （node_modules → .pnpm，或 vendor 之间互链）。装依赖时全部会重建，
                # 写进脚本就是几千行噪音，这里只计数。
                dep_links += 1
                continue
            if moved is None:
                pending_links.append({
                    "link": str(link),
                    "target": tgt,
                    "note": "不在任何工作区/根下，按原路径保留，跑脚本前先确认目标还在",
                })
            else:
                pending_links.append({
                    "link": str(moved),
                    "target": str(tgt_moved) if tgt_moved is not None else tgt,
                    "note": "" if tgt_moved is not None else "目标在新根之外，按原路径写",
                })
            continue
        if drill:
            for r in cfgmod.resolve_sets(cfg, kind):
                try:
                    sub = link.relative_to(Path(r["path"]))
                except ValueError:
                    continue
                link = base_dest / r["label"] / sub
                break
        try:
            if sevenzip.is_link(link):
                os.rmdir(link)
            elif link.is_dir():
                # 包里可能留了个同名空目录（旧包/手搓包），先清掉才建得了链接
                try:
                    next(link.iterdir())
                    failed_links.append(f"{link}: 目标已存在且非空")
                    continue
                except StopIteration:
                    os.rmdir(link)
            elif link.exists():
                failed_links.append(f"{link}: 同名文件已存在")
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            rc = os.system(f'cmd /c mklink /J "{link}" "{tgt}" >nul 2>&1')
            if rc == 0:
                rebuilt += 1
            else:
                failed_links.append(str(link))
        except OSError as exc:
            failed_links.append(f"{link}: {exc}")

    ok = all(r["ok"] and r["filesMatch"] for r in restored)
    issues = []
    for part in restored:
        if not part["ok"]:
            detail = f"解压退出码 {part.get('returnCode')}"
            if part.get("error"):
                detail += f"：{part['error']}"
            issues.append(f"{part['label']}：{detail}")
        elif not part["filesMatch"]:
            diff = int(part.get("expectFiles", 0)) - int(part.get("files", 0))
            issues.append(
                f"{part['label']}：实际解出 {part['files']:,} / 清单预期 "
                f"{part['expectFiles']:,} 个文件（差 {diff:+,}）"
            )
    reason = "；".join(issues)
    relocation_cleanup = None
    if reloc and not ok:
        removed: list[str] = []
        cleanup_failures: list[str] = []
        for item in relocation_targets.values():
            target = item["path"]
            if not target.exists():
                continue
            try:
                if item["existed"]:
                    # 部署开始前这个目录已确认为空；失败时清掉本次解出的内容，保留空目录。
                    for child in list(target.iterdir()):
                        if sevenzip.is_link(child):
                            os.rmdir(child)
                        elif child.is_dir():
                            _force_rmtree(child)
                        else:
                            child.unlink()
                else:
                    _force_rmtree(target)
                if item["existed"] and target.exists() and not any(target.iterdir()):
                    removed.append(str(target))
                elif not target.exists():
                    removed.append(str(target))
                else:
                    cleanup_failures.append(str(target))
            except OSError as exc:
                cleanup_failures.append(f"{target}: {exc}")
        relocation_cleanup = {
            "ok": not cleanup_failures,
            "removed": removed,
            "failed": cleanup_failures,
        }
        if cleanup_failures:
            suffix = "部署失败后的清理也未完成：" + "；".join(cleanup_failures)
            reason = f"{reason}；{suffix}" if reason else suffix

    # 落到别处时不自动建 junction：目标全是写死的绝对路径，换机之后大概率不存在。
    # 写成一份 .cmd 放在新根旁边，人看一眼、改一行、跑一遍。
    links_script = ""
    if reloc and ok:
        cands = [_role_new_root(kind, p["label"], roots) for p in parts]
        base = next((c for c in cands if c is not None), None)
        if base is None:
            base = Path(next(v for v in roots.values() if v))
        hint = ""
        try:
            nh = _role_new_root(kind, "harness", roots)
            # 只有 baseline 才有 harness 这一层；workspace 类算出来的
            # `<新工作区根>\harness` 是个不存在的路径，写进提示会误导人。
            if nh is not None and (nh / "package.json").exists():
                # 有 store 就不该联网：--offline 只用本地内容寻址库。
                # --frozen-lockfile 保证按原 lockfile 装、不重新解析（也不改 lockfile）。
                # --store-dir 是跨盘保险：pnpm 的 store 默认「按项目所在盘解析」，
                # checkout 换到别的盘时它会用该盘的 store（可能不全），显式指到
                # 本项目一直用的这份才能离线装成（跨盘会退化成复制，慢一些但能成）。
                store = str(cfg.get("paths", {}).get("pnpm_store") or "").strip()
                base_cmd = "pnpm install --offline --frozen-lockfile"
                tail = f'{base_cmd} --store-dir "{store}"' if store else base_cmd
                hint = f'cd /d "{nh}" ^&^& {tail}'
        except Exception:  # noqa: BLE001
            hint = ""
        try:
            links_script = str(write_links_script(
                Path(base).parent, pending_links,
                dep_skipped=dep_links, install_hint=hint,
            ))
        except OSError as exc:  # noqa: BLE001
            links_script = f"（写脚本失败：{exc}）"
    rollback_res = None
    if not ok and not drill and rollback:
        # 硬伤：原来失败只把 .pre-restore-* 的路径列出来就返回了，真实目录停在半成品状态，
        # 「能回滚」全靠用户自己去资源管理器改文件夹名。现在自动回滚并写日志。
        rollback_res = rollback_restore(rollback, logger=logger)
    out = {
        "ok": ok,
        "mode": "relocate" if reloc else ("drill" if drill else "apply"),
        "slot": slot_id,
        "kind": kind,
        "dest": str(dest) if dest else "",
        "roots": {k: str(v) for k, v in (roots or {}).items() if v},
        "parts": restored,
        "files": sum(r["files"] for r in restored),
        "expectFiles": sum(r["expectFiles"] for r in restored),
        "sourceFiles": sum(int(r.get("sourceFiles", 0)) for r in restored),
        "filesMatch": all(r["filesMatch"] for r in restored),
        "reason": reason,
        "emptyDirsMade": made_empty,
        "links": {"restored": rebuilt, "failed": failed_links},
        "pendingLinks": pending_links,
        "depLinks": dep_links,
        "linksScript": links_script,
        "rollback": rollback,
        "fallbacks": fallbacks,
        "rolledBack": rollback_res,
        "relocationCleanup": relocation_cleanup,
        "seconds": round(sum(r["seconds"] for r in restored), 1),
    }
    # 落位到新目录时才做收尾：装依赖 → 建链接 → 写启动器 → 删脚本。
    # 写回原位置不用做——依赖和链接本来就在，重装只会把现有 junction 覆盖掉。
    if ok and reloc and auto_finish:
        try:
            finish_restore(cfg, out, do_install=auto_install, do_links=auto_links,
                           cleanup=True, backup_root=root, logger=logger, progress=progress)
        except Exception as exc:                                  # noqa: BLE001
            out["finish"] = {"error": f"{type(exc).__name__}: {exc}"}
            if logger:
                logger.warning("restore_finish_failed", err=str(exc))
    if logger:
        logger.info("restore_done", kind=kind, slot=slot_id, ok=ok,
                    mode=out["mode"], files=out["files"],
                    expectFiles=out["expectFiles"], reason=reason,
                    parts=[{key: part.get(key) for key in
                            ("label", "ok", "files", "expectFiles", "filesMatch", "returnCode", "error")}
                           for part in restored],
                    rolledBack=bool(rollback_res and rollback_res.get("ok")),
                    fallbacks=len(fallbacks))
    if progress:
        if rollback_res is not None:
            progress.finish(
                "恢复没成功，已自动回滚到恢复前的状态（原目录从 .pre-restore-* 改回来了）"
                if rollback_res.get("ok")
                else "恢复没成功，自动回滚也失败了：请看日志，或按 .pre-restore-* 手工改回",
                False,
            )
        else:
            fin = out.get("finish") or {}
            extra = ""
            ins = fin.get("install")
            if ins is not None:
                extra += ("，依赖已装好" if ins.get("ok")
                          else f"，装依赖失败（退出码 {ins.get('code')}，可手动再跑）")
            lnk = fin.get("links")
            if lnk is not None:
                extra += ("，链接已建好" if lnk.get("ok")
                          else f"，有 {lnk.get('failedLines')} 条链接没建成")
            progress.finish(
                (f"恢复完成：{out['files']:,}/{out['expectFiles']:,} 文件，"
                f"目录联接（junction）{rebuilt} 条"
                + ("" if not failed_links else f"（失败 {len(failed_links)}）")
                + extra) if ok else f"恢复未通过完整性校验：{reason or '请查看日志'}",
                ok,
            )
    return out


def rollback_restore(rollback: list[dict], *, logger=None) -> dict:
    r"""把 `.pre-restore-<ts>` 改回原名，并把解了一半的新目录删掉。

    顺序很关键：先删半成品，再改名——反过来 `rename` 会因为目标已存在而失败，
    于是「回滚」变成什么都没做。删除失败时**绝不**继续改名（那会把原数据盖掉）。
    """
    done = []
    for r in rollback:
        target = Path(r["target"])
        backup = Path(r["backup"])
        item = {"target": str(target), "backup": str(backup), "label": r.get("label", "")}
        try:
            if target.exists():
                shutil.rmtree(target)
        except OSError as exc:
            item.update({"ok": False, "reason": f"半成品删不掉，未改名：{exc}"})
            done.append(item)
            continue
        try:
            backup.rename(target)
            item["ok"] = True
        except OSError as exc:
            item.update({"ok": False, "reason": f"改回原名失败：{exc}"})
        done.append(item)
    ok = all(i.get("ok") for i in done) if done else False
    if logger:
        (logger.info if ok else logger.error)(
            "restore_rollback", ok=ok, items=[i.get("label") for i in done])
    return {"ok": ok, "items": done}


# ---------------- 兜底包（落位恢复前的安全网） ----------------

def fallback_root(root: Path) -> Path:
    return Path(root) / FALLBACK_DIR


# ---------------- 兜底索引：原子写 + 坏文件留档 ----------------
#
# 体检 A：索引原来直接 write_text，断电/被杀会留半截 JSON；下次读失败就退化成 `[]`，
# 于是「兜底包在、但不知道它属于哪个路径」——包等于废了。改成 tmp + os.replace
# （与 config.save / manifest 同款），并把坏文件改名留档，不静默丢掉。

def atomic_write_text(path: str | Path, text: str) -> None:
    """写文本先落 .tmp 再 os.replace，同盘内原子替换。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_fallback_index(idx: str | Path, logger=None) -> list[dict]:
    """读一批兜底包的 index.json。坏文件不静默变空列表：改名 `.corrupt-<ts>` 并记 warn。"""
    idx = Path(idx)
    if not idx.exists():
        return []
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        keep = idx.with_name(idx.name + f".corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            idx.replace(keep)
        except OSError:
            keep = idx  # 连改名都失败就留在原处，至少不删
        if logger:
            logger.warn("fallback_index_corrupt", index=str(idx), kept=str(keep),
                        reason=str(exc))
        return []
    return data if isinstance(data, list) else []


def pack_fallback(cfg: dict, root: Path, kind: str, label: str, src: str | Path,
                  *, logger=None, progress=None) -> dict:
    r"""把「即将被覆盖的真实目录」打一份 7z 到 `<root>\兜底\<时间戳>\<类型>-<标签>.7z`。

    设计口径（v1 的语义，v2 之前一直没实现）：
    - 只在**真落位恢复**、且目标目录非空时打；试运行不碰真实路径，不打。
    - 每打一次写一行 `index.json`，记清楚「哪个包 → 原本属于哪个路径」，回滚靠它。
    - 打完立刻轮换：只留最新 `FALLBACK_KEEP` 份。
    """
    src = Path(src)
    if not src.exists():
        return {"ok": True, "skipped": True, "reason": "目标不存在，没什么可兜底"}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bucket = fallback_root(root) / ts
    bucket.mkdir(parents=True, exist_ok=True)
    archive = bucket / f"{kind}-{label}.7z"
    if progress:
        progress.update(phase=f"兜底打包中（{label}）",
                        detail=f"先把当前真实目录存一份，失败时靠它回滚：{src}")
    res = sevenzip.pack_7z(cfg, archive, str(src), exclude_dirs=[], exclude_paths=[])
    rec = {
        "ok": bool(res.ok),
        "kind": kind,
        "label": label,
        "src": str(src),
        "archive": str(archive),
        "bytes": archive.stat().st_size if archive.exists() else 0,
        "at": int(time.time() * 1000),
        "bucket": str(bucket),
        "seven_zip": str(cfg.get("tools", {}).get("seven_zip", "")),
    }
    idx = bucket / "index.json"
    rows = read_fallback_index(idx, logger=logger)
    rows.append(rec)
    atomic_write_text(idx, json.dumps(rows, ensure_ascii=False, indent=2))
    if logger:
        (logger.info if rec["ok"] else logger.warn)(
            "fallback_packed", kind=kind, label=label, archive=str(archive),
            bytes=rec["bytes"], src=str(src))
    pruned = prune_fallbacks(root, logger=logger)
    rec["pruned"] = pruned
    return rec


def fallback_list(root: Path) -> list[dict]:
    """由新到旧列出兜底包的批次（一个时间戳目录 = 一批）。"""
    base = fallback_root(root)
    if not base.exists():
        return []
    out = []
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        idx = d / "index.json"
        rows = read_fallback_index(idx, logger=None)
        size = 0
        for f in d.rglob("*.7z"):
            try:
                size += f.stat().st_size
            except OSError:
                pass
        out.append({
            "bucket": str(d),
            "name": d.name,
            "packs": len(rows),
            "bytes": size,
            "items": rows,
        })
    return out


def prune_fallbacks(root: Path, keep: int = FALLBACK_KEEP, logger=None) -> dict:
    """兜底包轮换：只留最新 `keep` 批，其余整批删掉。

    体检 D：原来删不掉就 `except OSError: pass` —— 批次数会越滚越大，
    而用户以为在轮换、以为旧的真实目录副本已经清掉了。失败必须留痕。
    """
    buckets = fallback_list(root)
    doomed = buckets[keep:]
    removed = []
    failed: list[dict] = []
    for b in doomed:
        try:
            shutil.rmtree(b["bucket"])
            removed.append(b["name"])
        except OSError as exc:
            failed.append({"name": b["name"], "reason": str(exc)})
    if removed and logger:
        logger.info("fallback_pruned", removed=removed, keep=keep)
    if failed and logger:
        logger.warn("fallback_prune_failed", failed=failed, keep=keep,
                    kept=len(buckets))
    return {"kept": [b["name"] for b in buckets[:keep]], "removed": removed,
            "failed": failed}


def restore_fallback(bucket: str, logger=None) -> dict:
    """把某一批兜底包解回它原本的路径（回滚用）。

    先删掉现存的残骸再解——不然解出来的东西会和半成品混在一起，看起来「回滚成功了」
    其实混了两代文件。
    """
    rows = read_fallback_index(Path(bucket) / "index.json", logger=logger)
    done = []
    for r in rows:
        src = Path(r.get("archive", ""))
        dst = Path(r.get("src", ""))
        if not src.exists() or not dst:
            done.append({"ok": False, "archive": str(src), "reason": "兜底包或目标路径不见"})
            continue
        try:
            if dst.exists():
                shutil.rmtree(dst)
        except OSError as exc:
            done.append({"ok": False, "target": str(dst), "reason": str(exc)})
            continue
        res = sevenzip.extract_7z(_cfg_for_extract(r), str(src), dst)
        done.append({"ok": bool(res.ok), "target": str(dst), "archive": str(src)})
    ok = all(d.get("ok") for d in done) if done else False
    if logger:
        (logger.info if ok else logger.warn)("fallback_restored", bucket=bucket, ok=ok)
    return {"ok": ok, "items": done}


def _cfg_for_extract(rec: dict) -> dict:
    """restore_fallback 只需要 7z 路径；兜底包的 index 里存了当时的 sevenzip。"""
    cfg = cfgmod.default_config()
    sz = rec.get("seven_zip")
    if sz:
        cfg["tools"]["seven_zip"] = sz
    return cfg


# ---------------- 回收区（_deleted） ----------------

def recycle_stats(root: Path) -> dict:
    """回收区占用 / 份数 / 最老一份的时间（按移到回收区的时刻算）。"""
    base = Path(root) / cfgmod.DELETED_DIR
    out = {"bytes": 0, "count": 0, "oldestAt": 0, "oldestAgeMs": 0, "missingAt": 0}
    if not base.exists():
        return out
    now_ms = int(time.time() * 1000)
    stamps: list[int] = []
    for kind_dir in (p for p in base.iterdir() if p.is_dir()):
        for slot_dir in (p for p in kind_dir.iterdir() if p.is_dir()):
            out["count"] += 1
            size = 0
            for f in slot_dir.rglob("*"):
                try:
                    if f.is_file():
                        size += f.stat().st_size
                except OSError:
                    pass
            out["bytes"] += size
            at = deleted_at_ms(slot_dir)
            if at:
                stamps.append(at)
            else:
                out["missingAt"] += 1
    if stamps:
        out["oldestAt"] = min(stamps)
        out["oldestAgeMs"] = max(0, now_ms - out["oldestAt"])
    return out


def deleted_at_ms(slot_dir: Path) -> int:
    """这一份是什么时候被移进回收区的。没有记录文件时退化为目录 mtime。"""
    p = Path(slot_dir) / DELETED_AT
    if p.exists():
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    try:
        return int(Path(slot_dir).stat().st_mtime * 1000)
    except OSError:
        return 0


def has_deleted_stamp(slot_dir: Path) -> bool:
    """体检 G：旧版本搬进来的目录没有 `.deleted-at`，得靠 mtime 兜底并留一条 warn。"""
    return (Path(slot_dir) / DELETED_AT).exists()


def purge_recycle(root: Path, keep_days: int, *, dry_run: bool = False, logger=None) -> dict:
    """回收区里超过 `keep_days` 的整份删掉。**只动 `_deleted\\`，不动任何备份**。"""
    base = Path(root) / cfgmod.DELETED_DIR
    out = {"ok": True, "limitDays": keep_days, "purged": [], "wouldPurge": [],
           "bytes": 0, "kept": 0}
    if not base.exists():
        return out
    now_ms = int(time.time() * 1000)
    limit_ms = int(keep_days) * 86400 * 1000
    missing_stamp: list[str] = []
    for kind_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for slot_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            at = deleted_at_ms(slot_dir)
            age = now_ms - at
            if limit_ms and age < limit_ms:
                out["kept"] += 1
                continue
            size = 0
            for f in slot_dir.rglob("*"):
                try:
                    if f.is_file():
                        size += f.stat().st_size
                except OSError:
                    pass
            name = f"{kind_dir.name}/{slot_dir.name}"
            if not has_deleted_stamp(slot_dir):
                # 体检 G：旧版本搬进来的目录没有标记，按 mtime 算年龄并留痕
                missing_stamp.append(name)
            if dry_run:
                out["wouldPurge"].append({"name": name, "ageDays": round(age / 86400000, 1),
                                          "bytes": size})
                continue
            try:
                shutil.rmtree(slot_dir)
            except OSError as exc:
                out["ok"] = False
                out["purged"].append({"name": name, "ok": False, "reason": str(exc)})
                continue
            out["purged"].append({"name": name, "ageDays": round(age / 86400000, 1),
                                  "bytes": size})
            out["bytes"] += size
    if missing_stamp and logger:
        logger.warn("recycle_missing_stamp", names=missing_stamp[:20],
                    count=len(missing_stamp), usedMtime=True)
    if logger and (out["purged"] or out["wouldPurge"]):
        (logger.info if not dry_run else logger.info)(
            "recycle_purged", dryRun=dry_run, limitDays=keep_days,
            names=[p["name"] for p in (out["purged"] if not dry_run else out["wouldPurge"])],
            bytes=out["bytes"])
    return out


# ---------------- 暂停状态（跨重启记住「什么时候暂停的」） ----------------

def pause_file(root: Path) -> Path:
    return Path(root) / "state" / "pause.json"


def read_pause(root: Path) -> dict:
    """返回 {"paused": bool, "pausedAt": ms}。文件不存在 = 没暂停。"""
    p = pause_file(root)
    if not p.exists():
        return {"paused": False, "pausedAt": 0}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"paused": False, "pausedAt": 0}
    return {"paused": bool(d.get("paused")), "pausedAt": int(d.get("pausedAt") or 0)}


def write_pause(root: Path, paused: bool, logger=None) -> dict:
    p = pause_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"paused": bool(paused), "pausedAt": int(time.time() * 1000) if paused else 0}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if logger:
        logger.info("pause_" + ("on" if paused else "off"), pausedAt=data["pausedAt"])
    return data


# ---------------- 调度与统计 ----------------

def next_baseline_at(cfg: dict, now: datetime | None = None) -> datetime:
    at = str(cfg["kinds"][cfgmod.KIND_BASELINE].get("at", "03:30"))
    hh, mm = (int(x) for x in at.split(":"))
    now = now or datetime.now()
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand = cand.replace(day=now.day) + __import__("datetime").timedelta(days=1)
    return cand


def status_summary(cfg: dict, root: Path) -> dict:
    now_ms = int(time.time() * 1000)
    out = {"kinds": {}, "diskFreeGb": round(cfgmod.disk_free_gb(root), 1),
           "diskTotalGb": round(cfgmod.disk_total_gb(root), 1),
           "diskLowWarnGb": disk_low_warn_gb(cfg),
           "recycleKeepDays": recycle_keep_days(cfg),
           "recycleWarnGb": recycle_warn_gb(cfg)}
    library_bytes = 0
    for kind in cfgmod.KINDS:
        if kind == cfgmod.KIND_STORE:
            s = manifest.mirror_slot(root)
            out["kinds"][kind] = {
                "count": 1 if s else 0,
                "latest": manifest.compact(s, now_ms) if s else None,
                "params": None,
                "bytes": int(s.get("bytes") or 0) if s else 0,
            }
            library_bytes += out["kinds"][kind]["bytes"]
            continue
        slots = manifest.scan(root, kind)
        params = cfgmod.kind_params(cfg, kind)
        target = None
        if params:
            target = retention.expected_counts(
                params["c"], params["D"], params["M"]
            )["total"]
        out["kinds"][kind] = {
            "count": len(slots),
            "target": target,
            "latest": manifest.compact(slots[0], now_ms) if slots else None,
            "params": params,
            "bytes": sum(int(s.get("bytes") or 0) for s in slots),
        }
        library_bytes += out["kinds"][kind]["bytes"]
    out["libraryBytes"] = library_bytes
    out["recycle"] = recycle_stats(root)
    out["nextBaselineAt"] = next_baseline_at(cfg).isoformat(timespec="minutes")
    return out


def due_kinds(cfg: dict, root: Path, now: datetime | None = None) -> list[str]:
    """会话按间隔、基线按每日时刻；store 在新基线完成后增量跟进。"""
    now = now or datetime.now()
    now_ms = int(now.timestamp() * 1000)
    due: list[str] = []
    k = cfg["kinds"]

    s = k[cfgmod.KIND_SESSIONS]
    if s.get("enabled") and s.get("every_minutes"):
        last = manifest.newest(root, cfgmod.KIND_SESSIONS)
        if not last or (now_ms - last["createdAt"]) >= int(s["every_minutes"]) * 60000:
            due.append(cfgmod.KIND_SESSIONS)

    b = k[cfgmod.KIND_BASELINE]
    if b.get("enabled"):
        at = str(b.get("at", "03:30"))
        hh, mm = (int(x) for x in at.split(":"))
        last = manifest.newest(root, cfgmod.KIND_BASELINE)
        slot_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= slot_today and (not last or last["createdAt"] < slot_today.timestamp() * 1000):
            due.append(cfgmod.KIND_BASELINE)

    store_cfg = k.get(cfgmod.KIND_STORE) or {}
    if store_cfg.get("enabled") and store_cfg.get("follow_baseline"):
        baseline = manifest.newest(root, cfgmod.KIND_BASELINE)
        mirror = manifest.read_mirror_state(root)
        if baseline and (not mirror.get("ok") or
                         int(mirror.get("syncedAt") or 0) < int(baseline.get("createdAt") or 0)):
            due.append(cfgmod.KIND_STORE)
    return due


def deleted_dir(root: Path, kind: str) -> Path:
    return Path(root) / cfgmod.DELETED_DIR / kind


def delete_slot(root: Path, kind: str, slot_id: str, logger=None) -> dict:
    r"""UX-04：软删除——把整份包移到 `_deleted\<kind>\<槽名>\`，不真删。

    语义变化（已获批）：原来是 shutil.rmtree，删错只能重新备份。现在 5 秒内可原样移回，
    超时也不清空回收区，留给人后续处理。
    """
    slots = manifest.scan(root, kind)
    hit = next((s for s in slots if s["slot"] == slot_id), None)
    if not hit:
        return {"ok": False, "reason": "找不到该 slot"}
    src = Path(hit["dir"])
    dst = deleted_dir(root, kind) / slot_id
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            # 同名回收件已存在（同槽名被删过两次）→ 加时间后缀，绝不覆盖
            dst = dst.with_name(f"{slot_id}__{int(time.time())}")
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return {"ok": False, "reason": str(exc)}
    # 记下「移进回收区的时刻」——30 天清理按这个算，不能拿包里的原文件时间
    # （那份包可能是两个月前的，但今天才被删，清它等于刚删就没了）。
    at_ms = int(time.time() * 1000)
    try:
        (dst / DELETED_AT).write_text(str(at_ms), encoding="utf-8")
    except OSError:
        pass
    if logger:
        logger.info("slot_deleted", kind=kind, slot=slot_id, soft=True,
                    movedTo=str(dst), deletedAt=at_ms)
    return {"ok": True, "deleted": slot_id, "soft": True, "movedTo": str(dst),
            "deletedAt": at_ms}


def restore_deleted(root: Path, kind: str, slot_id: str, moved_to: str | None = None,
                    logger=None) -> dict:
    """UX-04 撤销：把回收区里那一份移回 <root>\\<kind>\\<槽名>\\。"""
    src = Path(moved_to) if moved_to else deleted_dir(root, kind) / slot_id
    if not src.exists():
        return {"ok": False, "reason": f"回收区里找不到：{src}"}
    dst = Path(root) / kind / slot_id
    if dst.exists():
        return {"ok": False, "reason": f"原位置已有一份同名包：{dst}"}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return {"ok": False, "reason": str(exc)}
    # 撤销后这份不再是「回收区里的东西」，30 天计时器要失效，否则下次清理会把它当垃圾
    try:
        (dst / DELETED_AT).unlink()
    except OSError:
        pass
    if logger:
        logger.info("slot_undeleted", kind=kind, slot=slot_id)
    return {"ok": True, "restored": slot_id, "dir": str(dst)}


def tail_json_line(path: Path) -> str:
    return json.dumps({"path": str(path)}, ensure_ascii=False)


__all__ = [
    "Progress",
    "RunResult",
    "backup_kind",
    "count_tree",
    "deleted_at_ms",
    "delete_slot",
    "disk_hard_min_gb",
    "disk_low_warn_gb",
    "due_kinds",
    "fallback_list",
    "fallback_root",
    "mirror_store",
    "pack_fallback",
    "prune_fallbacks",
    "prune_kind",
    "purge_recycle",
    "read_pause",
    "recycle_stats",
    "restore_deleted",
    "restore_fallback",
    "restore_slot",
    "rollback_restore",
    "status_summary",
    "write_pause",
    "DISK_LOW_GB",
    "FALLBACK_DIR",
    "FALLBACK_KEEP",
    "DELETED_AT",
]
