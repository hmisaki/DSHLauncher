"""恢复：先兜底、再校验、解到临时区、改名不删、最后重建 junction。

设计 §8 的三条硬规矩全部硬编码在这里，不给调用方关掉的机会：
  1. 永不原地删 —— 落位前一律改名 `<path>.pre-restore-<ts>`
  2. 兜底优先 —— 兜底包失败 ⇒ 中止
  3. 可逆性写进返回值 —— `rollback` 字段告诉主人旧状态在哪
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from . import config as cfgmod, log as logmod, manifest

# zip 内一级目录 → 落位目标（配置里的哪个 key）
TOP_MAP = {
    ".dsh": "dsh_home",
    "deepseek-harness": "checkout",
    "WorkSpace": "workspace",
    "backups": "backup_root",
}


def _sha256_ok(zip_path: Path, expected: str | None) -> tuple[bool, str]:
    if not expected:
        return True, "manifest 未记录 sha256，跳过比对"
    actual = manifest.file_sha256(zip_path)
    if actual == expected:
        return True, "sha256 一致"
    return False, f"sha256 不匹配（记录 {expected[:12]}… 实际 {actual[:12]}…）"


def unzip_to(zip_path: Path, dest: Path, progress=None) -> dict:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        total = len(names)
        for i, n in enumerate(names, 1):
            zf.extract(n, dest)
            if progress and (i % 50 == 0 or i == total):
                progress.update(phase="extract", done=i, total=total, message=f"正在解包 {i}/{total}")
    files = [p for p in dest.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    return {"files": len(files), "bytes": size, "dest": str(dest)}


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _top_blocks(dest: Path, only: list[str] | None) -> list[str]:
    blocks = [p.name for p in Path(dest).iterdir() if p.is_dir() and p.name in TOP_MAP]
    if only:
        blocks = [b for b in blocks if b in only]
    return blocks


def restore_junctions(folder: Path, ts: str, logger) -> dict:
    """按随包带的 junctions-*.txt 重建 mklink /J。"""
    done, failed = [], []
    for name in ("junctions-harness.txt", "junctions-dsh.txt"):
        p = Path(folder) / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip() or "\t" not in line:
                continue
            link, target = line.split("\t", 1)
            try:
                Path(link).parent.mkdir(parents=True, exist_ok=True)
                if Path(link).exists():
                    os.rmdir(link) if Path(link).is_dir() and not Path(link).is_symlink() else None
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", link, target],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if r.returncode == 0:
                    done.append(link)
                else:
                    failed.append(f"{link}: {(r.stderr or r.stdout).strip()}")
                    logger.warn("junction_restore_failed", link=link, target=target)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{link}: {exc}")
                logger.warn("junction_restore_failed", link=link, target=target, error=str(exc))
    return {"restored": len(done), "failed": failed}


def restore_slot(
    cfg: dict,
    slot_id: str,
    *,
    logger: logmod.Logger,
    targets: list[str] | None = None,
    assume_yes: bool = False,
    skip_fallback: bool = False,
    progress=None,
    to: str | None = None,
) -> dict:
    """slot_id 形如 `2026-10-25T041500-dialog`。

    to 非空 = 演练模式：只解到指定目录并比对，不碰任何真实路径、不打兜底包。
    """
    root = Path(cfg["paths"]["backup_root"])
    slots = manifest.scan(root)
    slot = next((s for s in slots if s["slot"] == slot_id), None)
    if not slot:
        return {"ok": False, "reason": f"找不到 slot {slot_id}（共 {len(slots)} 份）"}
    zip_path = Path(slot["zipPath"])
    if not zip_path.exists():
        return {"ok": False, "reason": f"包文件不存在：{zip_path}"}

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.info("restore_start", slot=slot_id, mode="drill" if to else "apply", targets=targets or [])

    ok, detail = _sha256_ok(zip_path, slot.get("sha256"))
    if not ok:
        logger.error("restore_fail", slot=slot_id, stage="sha256", detail=detail)
        return {"ok": False, "reason": detail}

    # ── 演练模式 ──────────────────────────────────────────────
    if to:
        dest = Path(to)
        info = unzip_to(zip_path, dest, progress)
        expect = int(slot.get("files", 0))
        match = info["files"] == expect
        blocks = _top_blocks(dest, targets)
        logger.info(
            "restore_done", slot=slot_id, mode="drill", files=info["files"], expect=expect, match=match
        )
        return {
            "ok": True,
            "mode": "drill",
            "slot": slot_id,
            "dest": info["dest"],
            "files": info["files"],
            "expectFiles": expect,
            "filesMatch": match,
            "bytes": info["bytes"],
            "blocks": blocks,
            "note": "演练模式，未触碰任何真实路径",
        }

    # ── 落位模式 ──────────────────────────────────────────────
    running = _port_open(cfg["ports"]["dsh"], cfg["server"]["host"])
    if running and any(b in (targets or [".dsh", "deepseek-harness"]) for b in (".dsh", "deepseek-harness")):
        return {
            "ok": False,
            "reason": (
                f"网关正在运行（端口 {cfg['ports']['dsh']} 有人监听）。"
                "覆盖 .dsh 或 checkout 会被占用锁挡住，先点「关闭环境」再来恢复；"
                "只想演练请用 to=<临时目录>。"
            ),
        }

    fallback_dir = None
    if not skip_fallback:
        from . import runner

        fb = runner.run_layer_once(
            cfg,
            slot["layer"] if slot["layer"] in ("baseline", "dialog", "workspace") else "dialog",
            category=cfgmod.DIR_FALLBACK,
            note=f"恢复 {slot_id} 前的兜底",
            logger=logger,
            progress=progress,
            target_dir=None,
            force=True,
        )
        if not fb.get("ok"):
            logger.error("restore_fail", slot=slot_id, stage="fallback", detail=fb.get("reason"))
            return {"ok": False, "reason": f"兜底包失败，已中止恢复：{fb.get('reason')}"}
        fallback_dir = fb["dir"]
        _rotate_fallback(root, keep=3, logger=logger)

    tmp_dir = root / cfgmod.DIR_TMP / f"restore-{ts}"
    info = unzip_to(zip_path, tmp_dir, progress)

    blocks = _top_blocks(tmp_dir, targets)
    moved: list[dict] = []
    for block in blocks:
        key = TOP_MAP[block]
        target = Path(cfg["paths"][key])
        src = tmp_dir / block
        pre = target.with_name(f"{target.name}.pre-restore-{ts}")
        entry = {"block": block, "target": str(target), "renamedTo": None, "ok": False}
        try:
            if target.exists():
                os.rename(target, pre)
                entry["renamedTo"] = str(pre)
            shutil.move(str(src), str(target))
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            # 回滚这一块
            if entry["renamedTo"] and not target.exists():
                try:
                    os.rename(pre, target)
                    entry["rolledBack"] = True
                except OSError:
                    pass
        moved.append(entry)

    junctions = {"restored": 0, "failed": []}
    if slot["layer"] == "baseline" and all(m["ok"] for m in moved):
        junctions = restore_junctions(Path(slot["dir"]), ts, logger)

    all_ok = all(m["ok"] for m in moved) and not junctions["failed"]
    logger.info(
        "restore_done",
        slot=slot_id,
        mode="apply",
        blocks=len(moved),
        ok=all_ok,
        fallback=fallback_dir,
        junctionsRestored=junctions["restored"],
        junctionsFailed=len(junctions["failed"]),
    )
    return {
        "ok": all_ok,
        "mode": "apply",
        "slot": slot_id,
        "files": info["files"],
        "blocks": moved,
        "fallback": fallback_dir,
        "junctions": junctions,
        "rollback": [m["renamedTo"] for m in moved if m.get("renamedTo")],
        "next": "双击桌面 DSH.lnk 或 Start-ScheduledTask -TaskName 'DSH Gateway' 起网关验证",
    }


def _rotate_fallback(root: Path, keep: int = 3, logger=None) -> list[str]:
    """兜底目录只留最近 keep 份（ADR §14 裁决 5）。"""
    base = Path(root) / cfgmod.DIR_FALLBACK
    if not base.is_dir():
        return []
    subs = sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    removed = []
    for p in subs[keep:]:
        try:
            shutil.rmtree(p)
            removed.append(str(p))
        except OSError:
            continue
    if removed and logger:
        logger.info("fallback_rotated", removed=len(removed), keep=keep)
    return removed
