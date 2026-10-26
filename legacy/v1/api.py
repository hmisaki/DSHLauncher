"""本地 Web 服务：一屏 UI + 一组 JSON 接口。

只绑回环（配置里 server.host 只允许 127.0.0.1 / localhost）。
长操作一律起后台线程，HTTP 立刻返回 202，前端每 1 秒轮询 /api/progress。
"""
from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import archive, commands, config as cfgmod, log as logmod, manifest, restore, retention, retention_state, runner

UI_DIR = Path(__file__).parent / "ui"

LAYER_LABELS = {"baseline": "基线", "dialog": "对话", "workspace": "工作区"}


class AppState:
    def __init__(self, cfg: dict, root: Path, logger: logmod.Logger):
        self.cfg = cfg
        self.root = Path(root)
        self.logger = logger
        self.progress = runner.Progress()
        self.runner = runner.Runner(cfg, logger, self.progress, on_prune=self._after_run)
        self._jobs: dict[str, threading.Thread] = {}

    def _after_run(self, layer: str) -> None:
        self.prune(layer, apply=True)

    def busy(self) -> bool:
        return self.progress.snapshot().get("busy", False) or any(
            t.is_alive() for t in self._jobs.values()
        )

    def spawn(self, name: str, fn, *a, **kw) -> bool:
        if self.busy():
            return False
        t = threading.Thread(target=fn, args=a, kwargs=kw, name=f"job-{name}", daemon=True)
        self._jobs[name] = t
        t.start()
        return True

    def backup(self, layer: str, note: str = "", category: str | None = None) -> dict:
        return runner.run_layer_once(
            self.cfg, layer, note=note, logger=self.logger, progress=self.progress, category=category
        )

    def prune(self, layer: str, apply: bool = False) -> dict:
        params = cfgmod.layer_params(self.cfg, layer)
        if params is None:
            return {"ok": False, "reason": "该层不参与自动淘汰"}
        slots = manifest.by_layer(manifest.scan(self.root), layer)
        now_ms = int(time.time() * 1000)
        stats: dict = {}
        keep, hold, drop = retention.select_keep(now_ms, slots, params, stats)
        if stats.get("clockSkew"):
            self.logger.warn("clock_skew", layer=layer, future=stats.get("futureDropped"))
        for i in stats.get("emptyBuckets", []):
            self.logger.warn("empty_bucket", layer=layer, bucket=i + 1)
        for i in stats.get("narrowBuckets", []):
            self.logger.warn("bucket_narrower_than_interval", layer=layer, bucket=i + 1)
        out = {
            "ok": True,
            "layer": layer,
            "total": len(slots),
            "keep": len(keep),
            "hold": len(hold),
            "drop": len(drop),
            "emptyBuckets": len(stats.get("emptyBuckets", [])),
        }
        if apply:
            st = retention_state.load(self.root)
            res = retention_state.apply_prune(
                self.root, layer, keep, hold, drop, st, params, self.logger
            )
            out.update(res)
        return out


def create_app(cfg: dict, root: Path, logger: logmod.Logger) -> tuple[FastAPI, AppState]:
    st = AppState(cfg, root, logger)
    app = FastAPI(title="DSH 备份", docs_url="/api/docs", redoc_url=None)

    if (UI_DIR / "index.html").exists():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")

    @app.get("/")
    def index():
        f = UI_DIR / "index.html"
        if not f.exists():
            return JSONResponse({"error": "ui/index.html 缺失"}, status_code=500)
        return FileResponse(str(f))

    @app.get("/api/status")
    def status():
        slots = manifest.scan(st.root)
        now_ms = int(time.time() * 1000)
        layers = []
        for layer, label in LAYER_LABELS.items():
            sub = [s for s in slots if s.get("layer") == layer]
            newest = sub[0] if sub else None
            oldest = sub[-1] if sub else None
            params = cfgmod.layer_params(st.cfg, layer)
            layers.append(
                {
                    "layer": layer,
                    "label": label,
                    "count": len(sub),
                    "expected": retention.expected_counts(
                        params["c"], params["D"], params["M"], params["anchors"]
                    )["total"] if params else None,
                    "lastAt": newest["createdAtIso"] if newest else None,
                    "lastAgeMinutes": round((now_ms - newest["createdAt"]) / 60000.0, 1) if newest else None,
                    "coveredDays": round((now_ms - oldest["createdAt"]) / 86400000.0, 1) if oldest else None,
                    "bytes": sum(s.get("bytes", 0) for s in sub),
                }
            )
        pr = st.progress.snapshot()
        return {
            "now": datetime.now().isoformat(timespec="seconds"),
            "backupRoot": str(st.root),
            "usedBytes": sum(s.get("bytes", 0) for s in slots),
            "diskFreeGb": logmod.disk_free_gb(st.root),
            "paused": bool(st.cfg.get("paused")),
            "layers": layers,
            "health": commands.health(st.cfg),
            "progress": {
                "busy": pr.get("busy", False),
                "phase": pr.get("phase"),
                "done": pr.get("done", 0),
                "total": pr.get("total", 0),
                "message": pr.get("message", ""),
            },
            "nextRun": st.runner.next_run_at(),
            "totalSlots": len(slots),
        }

    @app.get("/api/slots")
    def slots(layer: str | None = None, limit: int = 50):
        all_slots = manifest.scan(st.root)
        if layer:
            all_slots = [s for s in all_slots if s.get("layer") == layer]
        now_ms = int(time.time() * 1000)
        out = []
        for s in all_slots[: max(1, min(limit, 500))]:
            row = manifest.compact_for_list(s, now_ms)
            params = cfgmod.layer_params(st.cfg, s.get("layer", ""))
            row["bucket"] = (
                retention.bucket_label(s["createdAt"], now_ms, params) if params else "手工"
            )
            out.append(row)
        return {"slots": out, "total": len(all_slots)}

    @app.post("/api/backup")
    def backup(payload: dict | None = None):
        payload = payload or {}
        layer = payload.get("layer", "dialog")
        if layer not in LAYER_LABELS:
            raise HTTPException(400, f"未知层 {layer}")
        note = payload.get("note", "")

        def job():
            try:
                res = st.backup(layer, note=note)
                if res.get("ok"):
                    st.prune(layer, apply=True)
                st.logger.info("api_backup_done", layer=layer, ok=bool(res.get("ok")))
            finally:
                st.progress.update(busy=False, phase="idle", message="")

        if not st.spawn("backup", job):
            raise HTTPException(409, "已有任务在跑，等它结束")
        return {"started": True, "layer": layer}

    @app.post("/api/prune")
    def prune(payload: dict | None = None):
        payload = payload or {}
        layer = payload.get("layer", "dialog")
        apply = bool(payload.get("apply", False))
        return st.prune(layer, apply=apply)

    @app.post("/api/restore")
    def do_restore(payload: dict):
        slot = payload.get("slot")
        if not slot:
            raise HTTPException(400, "需要 slot")
        to = payload.get("to")
        targets = payload.get("targets")
        skip_fb = bool(payload.get("skipFallback", False))
        if payload.get("drill") and not to:
            to = str(
                Path(st.root) / cfgmod.DIR_TMP / f"drill-{slot}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )

        def job():
            try:
                res = restore.restore_slot(
                    st.cfg,
                    slot,
                    logger=st.logger,
                    targets=targets,
                    assume_yes=True,
                    skip_fallback=skip_fb,
                    progress=st.progress,
                    to=to,
                )
                st.progress.update(
                    busy=False,
                    phase="idle",
                    message=("恢复完成" if res.get("ok") else f"恢复失败：{res.get('reason')}"),
                    lastResult=res,
                )
            except Exception as exc:  # noqa: BLE001
                st.logger.exception("error", "restore_fail", exc, slot=slot)
                st.progress.update(busy=False, phase="idle", message=f"恢复异常：{exc}")

        if not st.spawn("restore", job):
            raise HTTPException(409, "已有任务在跑，等它结束")
        return {"started": True, "slot": slot, "mode": "drill" if to else "apply"}

    @app.get("/api/restore/check")
    def restore_check(slot: str):
        """恢复前的预检：包在不在、sha 对不对、环境在不在跑。"""
        found = next((s for s in manifest.scan(st.root) if s["slot"] == slot), None)
        if not found:
            raise HTTPException(404, f"没有 {slot}")
        zp = Path(found["zipPath"])
        ok, detail = restore._sha256_ok(zp, found.get("sha256")) if zp.exists() else (False, "包文件不存在")
        return {
            "ok": ok,
            "detail": detail,
            "slot": slot,
            "layer": found.get("layer"),
            "bytes": found.get("bytes"),
            "files": found.get("files"),
            "createdAtIso": found.get("createdAtIso"),
            "gatewayRunning": commands.port_open(st.cfg["ports"]["dsh"], st.cfg["server"]["host"]),
            "blocks": found.get("covers", []),
        }

    @app.post("/api/milestone")
    def milestone(payload: dict):
        name = (payload or {}).get("name") or datetime.now().strftime("%Y%m%d-%H%M")
        folder = st.root / cfgmod.DIR_MILESTONE / name
        if folder.exists():
            raise HTTPException(409, f"里程碑 {name} 已存在")

        def job():
            from . import ps1gen

            made = {}
            try:
                folder.mkdir(parents=True, exist_ok=True)
                for layer in ("baseline", "dialog", "workspace"):
                    res = runner.run_layer_once(
                        st.cfg, layer, note=f"里程碑 {name}", logger=st.logger,
                        progress=st.progress, target_dir=folder, force=True,
                    )
                    if res.get("ok"):
                        made[layer] = res
                if made:
                    ps1gen.write_milestone_folder(folder, st.cfg, made, name, st.logger)
                st.progress.update(busy=False, phase="idle", message=f"里程碑 {name} 完成")
            except Exception as exc:  # noqa: BLE001
                st.logger.exception("error", "milestone_fail", exc, name=name)
                st.progress.update(busy=False, phase="idle", message=f"里程碑失败：{exc}")

        if not st.spawn("milestone", job):
            raise HTTPException(409, "已有任务在跑，等它结束")
        return {"started": True, "name": name, "folder": str(folder)}

    @app.get("/api/config")
    def get_config():
        return {"config": st.cfg, "path": str(cfgmod.config_file(st.root))}

    @app.post("/api/config")
    def set_config(payload: dict):
        new = cfgmod.deep_merge(st.cfg, payload.get("config", payload))
        errs = cfgmod.validate(new)
        if errs:
            raise HTTPException(400, {"errors": errs})
        cfgmod.save(st.root, new)
        st.cfg.clear()
        st.cfg.update(new)
        st.runner.cfg = st.cfg
        st.logger.info("config_saved", fields=list((payload.get("config") or payload).keys()))
        return {"ok": True}

    @app.post("/api/env/{action}")
    def env(action: str):
        slot = {"start": "start_cmd", "stop": "stop_cmd", "restart": "restart_cmd"}.get(action)
        if not slot:
            raise HTTPException(400, f"未知动作 {action}")
        res = commands.run_slot(st.cfg, slot, st.logger)

        def wait():
            w = commands.wait_up(st.cfg, seconds=90)
            st.progress.update(
                busy=False, phase="idle",
                message=("环境已起来" if w["ok"] else f"环境没起来：{w.get('reason')}"),
            )

        if action in ("start", "restart") and res.get("ok"):
            st.progress.update(busy=True, phase="env", message=f"正在等端口 {st.cfg['ports']['dsh']} 通…")
            threading.Thread(target=wait, daemon=True).start()
        return res

    @app.get("/api/logs")
    def logs(n: int = 60):
        return {"lines": logmod.recent_lines(st.root / cfgmod.DIR_LOGS, max(1, min(n, 500)))}

    @app.get("/api/config/check")
    def config_check():
        return {"errors": cfgmod.validate(st.cfg)}

    return app, st


def serve(cfg: dict, root: Path, logger: logmod.Logger, open_browser: bool = True) -> int:
    import uvicorn

    (Path(root) / cfgmod.DIR_TMP).mkdir(parents=True, exist_ok=True)
    removed = archive.cleanup_orphan_tmp(root)
    if removed:
        logger.info("tmp_cleaned", count=len(removed))

    app, st = create_app(cfg, root, logger)
    host = cfg["server"]["host"]
    port = int(cfg["ports"]["api"])
    url = f"http://{host}:{port}/"
    logger.info("serve_start", url=url, root=str(root))
    print(f"\n  DSH 备份面板： {url}\n  备份根目录：   {root}\n  按 Ctrl+C 停止\n")
    if open_browser and cfg["server"].get("open_browser", True):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    st.runner.start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        st.runner.stop()
        logger.info("serve_stop")
    return 0
