"""一次备份的完整流程 + 定时调度。

run_layer_once 是「打了就跑」的核心，M0 的 CLI 与 M3 的 API 都调它；
Runner 只负责按节奏叫醒它（M2）。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import archive, collector, config as cfgmod, manifest, log as logmod

DISK_LOW_GB = 5.0


class Progress:
    """进程内进度黑板：run_layer_once 写，API 读。线程安全的最小实现。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.state: dict = {"busy": False, "phase": "idle", "layer": None, "done": 0, "total": 0, "message": ""}

    def update(self, **kw):
        with self._lock:
            self.state.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)


def _covers(cfg: dict, layer: str) -> list[str]:
    """这份包覆盖了哪些路径（包内相对路径，给人读）。"""
    layer_cfg = cfg["layers"][layer]
    out: list[str] = []
    for inc in layer_cfg.get("include", []):
        rel = collector.arc_prefix(cfgmod.expand(inc, cfg), cfg).replace("\\", "/")
        if rel and rel not in out:
            out.append(rel)
    return out


def _lock_fingerprint(cfg: dict) -> str | None:
    """baseline 的去重指纹：lock + modules.yaml 的内容哈希。任缺则 None（不去重）。"""
    ck = Path(cfg["paths"]["checkout"])
    parts = []
    for rel in ("pnpm-lock.yaml", os.path.join("node_modules", ".modules.yaml")):
        p = ck / rel
        if not p.exists():
            return None
        parts.append(manifest.file_sha256(p))
    return manifest.file_sha256.__name__ + ":" + ":".join(parts)


def run_layer_once(
    cfg: dict,
    layer: str,
    *,
    category: str | None = None,
    note: str = "",
    logger: logmod.Logger | None = None,
    progress: Progress | None = None,
    target_dir: Path | None = None,
    force: bool = False,
) -> dict:
    """打一份包。返回 {ok, slot, dir, zipPath, files, bytes, ms, ...}。"""
    root = Path(cfg["paths"]["backup_root"])
    logger = logger or logmod.Logger(root / cfgmod.DIR_LOGS)
    progress = progress or Progress()
    started = time.time()

    if cfg.get("paused") and not force:
        logger.warn("run_skip", layer=layer, reason="paused")
        return {"ok": False, "skipped": True, "reason": "已暂停（paused=true）"}

    free = logmod.disk_free_gb(root)
    if 0 <= free < DISK_LOW_GB:
        logger.error("disk_low", layer=layer, freeGb=round(free, 2))
        return {"ok": False, "reason": f"磁盘余量不足：只剩 {free:.1f} GB（低于 {DISK_LOW_GB:.0f} GB）"}

    category = category or cfgmod.LAYER_TO_DIR[layer]
    snap_ms = int(time.time() * 1000)
    ts = datetime.fromtimestamp(snap_ms / 1000.0)

    if target_dir is not None:
        folder = Path(target_dir)
    else:
        folder = root / category / manifest.stamp(ts)
    zip_name = f"{layer}.zip"

    progress.update(busy=True, phase="collect", layer=layer, done=0, total=0, message=f"正在收集 {layer} 文件清单…")
    logger.info("run_start", layer=layer, category=category, folder=str(folder))

    try:
        result = collector.collect(cfg, layer)
    except Exception as exc:  # noqa: BLE001
        logger.exception("error", "archive_fail", exc, layer=layer, stage="collect")
        progress.update(busy=False, phase="idle", message="")
        return {"ok": False, "reason": f"收集失败：{exc}"}

    if not result.files:
        msg = "没有收集到任何文件（检查 layers.%s.include 与 exclude）" % layer
        logger.error("archive_fail", layer=layer, stage="collect", reason="empty")
        progress.update(busy=False, phase="idle", message="")
        return {"ok": False, "reason": msg, "missingRoots": result.missing_roots}

    total = len(result.files)
    progress.update(phase="archive", total=total, done=0, message=f"正在打包 {layer}…")

    def on_progress(done: int, tot: int, packed: int):
        progress.update(done=done, total=tot, bytesDone=packed)

    try:
        stats = archive.build_zip(
            folder / zip_name, result.files, level=cfg["compress"]["level"], on_progress=on_progress
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("error", "archive_fail", exc, layer=layer, stage="zip")
        progress.update(busy=False, phase="idle", message="")
        return {"ok": False, "reason": f"打包失败：{exc}"}

    # junction 清单（基线包旁），格式 路径<TAB>目标
    if layer == "baseline":
        harness, dsh = collector.split_junctions(result, cfg)
        (folder / "junctions-harness.txt").write_text("\n".join(harness) + ("\n" if harness else ""), encoding="utf-8")
        (folder / "junctions-dsh.txt").write_text("\n".join(dsh) + ("\n" if dsh else ""), encoding="utf-8")

    progress.update(phase="verify", message=f"正在校验 {zip_name}…")
    ok, detail = archive.verify_zip(folder / zip_name)
    if ok:
        logger.info("verify_ok", layer=layer, slot=manifest.slot_id(ts, layer), detail=detail)
    else:
        logger.error("verify_fail", layer=layer, slot=manifest.slot_id(ts, layer), detail=detail)

    data = manifest.make_slot(
        layer,
        snap_ms,
        slot=manifest.slot_id(ts, layer),
        files=stats["filesPacked"],
        bytes_=stats["zipBytes"],
        sha256=stats["sha256"],
        zip_name=zip_name,
        source_path=cfg["paths"]["dsh_home"] if layer != "workspace" else cfg["paths"]["workspace"],
        covers=_covers(cfg, layer),
        note=note,
        verified_ok=ok,
    )
    data["sourceBytes"] = stats["sourceBytes"]
    data["junctions"] = len(result.junctions)
    data["filesFailed"] = stats["filesFailed"]
    if result.missing_roots:
        data["missingRoots"] = result.missing_roots
    manifest.write_manifest(folder, data)

    ms = int((time.time() - started) * 1000)
    logger.info(
        "archive_ok",
        layer=layer,
        slot=data["slot"],
        files=stats["filesPacked"],
        bytes=stats["zipBytes"],
        ms=ms,
        junctions=len(result.junctions),
        verifiedOk=ok,
    )
    logger.info("run_done", layer=layer, slot=data["slot"], ms=ms)
    progress.update(busy=False, phase="idle", message="")

    data.update({"ok": ok, "dir": str(folder), "zipPath": str(folder / zip_name), "ms": ms})
    return data


# ───────────────────────── 去重（baseline 专用） ─────────────────────────


def load_dedup_state(root: Path) -> dict:
    p = Path(root) / cfgmod.DIR_STATE / "dedup.json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_dedup_state(root: Path, state: dict) -> None:
    p = Path(root) / cfgmod.DIR_STATE / "dedup.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def reuse_previous_zip(cfg: dict, layer: str, logger: logmod.Logger) -> dict | None:
    """lock 未变 ⇒ 复制上一份完整 zip 当新包（仍是独立整包，不引入链）。"""
    if layer != "baseline" or not cfg["layers"]["baseline"].get("dedup_by_lock"):
        return None
    root = Path(cfg["paths"]["backup_root"])
    fp = _lock_fingerprint(cfg)
    if fp is None:
        return None
    state = load_dedup_state(root)
    prev = state.get("baselineFingerprint")
    if prev != fp:
        state["baselineFingerprint"] = fp
        save_dedup_state(root, state)
        return None
    last = manifest.newest(root, "baseline")
    if not last or not Path(last["zipPath"]).exists():
        return None
    snap_ms = int(time.time() * 1000)
    ts = datetime.fromtimestamp(snap_ms / 1000.0)
    folder = root / cfgmod.DIR_DAILY / manifest.stamp(ts)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / last["zipName"]
    src = Path(last["zipPath"])
    # 同一秒内连跑两次会撞到同一目录：dest==src 时 copy2 会自复制并报 WinError 32
    if src.resolve() == dest.resolve():
        dest = folder / f"{layer}-reuse-{snap_ms}.zip"
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        logger.warn("archive_fail", layer=layer, phase="reuse", error=str(exc)[:200])
        return None
    data = dict(last)
    data.update(
        {
            "slot": manifest.slot_id(ts, layer),
            "createdAt": snap_ms,
            "createdAtIso": ts.isoformat(timespec="seconds"),
            "verifiedAt": snap_ms,
            "verifiedOk": True,
            "note": "lock 未变，复用上一份完整包",
            "dir": str(folder),
            "zipPath": str(dest),
            "reusedFrom": last["slot"],
        }
    )
    manifest.write_manifest(folder, data)
    logger.info("archive_ok", layer=layer, slot=data["slot"], reusedFrom=last["slot"])
    return data


# ───────────────────────── 调度（M2） ─────────────────────────


class Runner:
    """按配置节奏叫醒备份。不用 cron：只算「下一次该跑的时刻」。"""

    def __init__(self, cfg: dict, logger: logmod.Logger, progress: Progress, on_prune=None):
        self.cfg = cfg
        self.logger = logger
        self.progress = progress
        self.on_prune = on_prune
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dshbackup-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("error", "run_fail", exc, stage="scheduler")
            self._stop.wait(20.0)

    def tick(self, now: datetime | None = None) -> list[str]:
        """到点就跑，返回本次真正跑了的层。"""
        if self.cfg.get("paused"):
            return []
        now = now or datetime.now()
        ran: list[str] = []
        b = self.cfg["layers"]["baseline"]
        if b.get("enabled"):
            hh, mm = (int(x) for x in str(b["at"]).split(":"))
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today_key = now.strftime("%Y%m%d")
            if now >= target and self.last.get("baseline_day") != today_key:
                self.last["baseline_day"] = today_key
                if self._run("baseline"):
                    ran.append("baseline")

        d = self.cfg["layers"]["dialog"]
        if d.get("enabled"):
            every = max(1, int(d["every_minutes"])) * 60
            last = self.last.get("dialog_ts", 0)
            if now.timestamp() - last >= every:
                self.last["dialog_ts"] = int(now.timestamp())
                if self._run("dialog"):
                    ran.append("dialog")
        return ran

    def _run(self, layer: str) -> bool:
        res = run_layer_once(self.cfg, layer, logger=self.logger, progress=self.progress)
        if res.get("ok") and self.on_prune:
            try:
                self.on_prune(layer)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("warn", "prune_fail", exc, layer=layer)
        return bool(res.get("ok"))

    def next_run_at(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        b = self.cfg["layers"]["baseline"]
        hh, mm = (int(x) for x in str(b["at"]).split(":"))
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t = t + timedelta(days=1)
        every = max(1, int(self.cfg["layers"]["dialog"]["every_minutes"])) * 60
        nxt = self.last.get("dialog_ts", now.timestamp()) + every
        return {
            "baseline": t.isoformat(timespec="seconds"),
            "dialog": datetime.fromtimestamp(nxt).isoformat(timespec="seconds"),
        }
