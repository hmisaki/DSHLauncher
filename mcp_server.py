"""dshbackup MCP server —— 让 agent 用 stdio JSON-RPC 操作备份库与 DSH 网关。

接入方式（DSH 的 mcp 配置里一条 stdio 服务）：

    command:  <python 解释器路径>
    args:     ["<本工具目录>/mcp_server.py"]

设计原则：
  * 只用标准库，不依赖 PySide6 —— 备份器界面没开着也能用。
  * 复用 dshbackup 库本身（engine / manifest），不另写一套备份逻辑。
  * 危险动作（删、关网关）要显式 confirm，默认拒绝。
  * 恢复只做"演练到指定目录"，**不提供落回原路径**——那条路留给界面里的人。

stdout 只走 JSON-RPC；任何诊断都往 stderr 写，否则会污染协议流。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dshbackup import config as cfgmod, engine, log as logmod, manifest  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "dshbackup", "version": "0.2.0"}

ROOT = Path(cfgmod.DEFAULTS["paths"]["backup_root"])
try:
    CFG = cfgmod.load(ROOT)
except Exception as exc:  # noqa: BLE001 —— 配置坏了也要能握手，工具调用再报错
    print(f"[dshbackup-mcp] 读配置失败：{exc}", file=sys.stderr)
    CFG = dict(cfgmod.DEFAULTS)
CFG.setdefault("paths", {})["backup_root"] = str(ROOT)
LOGGER = logmod.Logger(ROOT / "logs")


# ---------------------------------------------------------------- 小工具

def _port_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _dsh_port() -> int:
    return int(CFG.get("ports", {}).get("dsh", 3080) or 3080)


def _dsh_cmd_path() -> Path:
    """网关启动脚本由配置文件说了算，代码里不再存第二份路径。"""
    raw = str((CFG.get("paths") or {}).get("dsh_cmd") or "").strip()
    return Path(raw) if raw else Path.home() / ".dsh" / "bin" / "DSH.cmd"


def _port_pids(port: int) -> list[int]:
    """按端口找监听进程 pid（Windows 走 Get-NetTCPConnection）。"""
    try:
        r = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({int(x) for x in (r.stdout or "").split() if x.strip().isdigit()})


def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _compact(slots: list[dict], limit: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    out = []
    for s in slots[:limit]:
        c = manifest.compact(s, now_ms)
        c.pop("dir", None)
        out.append(c)
    return out


# ---------------------------------------------------------------- 工具实现

def tool_backup_status(_args: dict) -> dict:
    st = engine.status_summary(CFG, ROOT)
    ports = CFG.get("ports", {})
    st["ports"] = {k: {"port": int(v), "alive": _port_alive(int(v))} for k, v in ports.items()}
    st["root"] = str(ROOT)
    return _ok(st)


def tool_backup_list(args: dict) -> dict:
    kind = (args.get("kind") or "").strip()
    limit = int(args.get("limit") or 20)
    if kind and kind not in cfgmod.KINDS:
        return _err(f"kind 只能是 {list(cfgmod.KINDS)}，收到 {kind!r}")
    if kind == cfgmod.KIND_STORE:
        s = manifest.mirror_slot(ROOT)
        return _ok({"kind": kind, "count": 1 if s else 0,
                    "slots": _compact([s], 1) if s else []})
    if kind:
        slots = manifest.scan(ROOT, kind)
        return _ok({"kind": kind, "count": len(slots), "slots": _compact(slots, limit)})
    everything = manifest.scan_all(ROOT)
    return _ok({k: {"count": len(v), "slots": _compact(v, limit)}
                for k, v in everything.items()})


def tool_backup_run(args: dict) -> dict:
    kind = (args.get("kind") or "").strip()
    note = (args.get("note") or "由 agent 触发").strip()
    if kind not in cfgmod.KINDS:
        return _err(f"kind 只能是 {list(cfgmod.KINDS)}，收到 {kind!r}")
    if kind == cfgmod.KIND_STORE:
        res = engine.mirror_store(CFG, ROOT, logger=LOGGER)
    else:
        res = engine.backup_kind(CFG, ROOT, kind, note=note, logger=LOGGER)
    res.pop("dir", None) if isinstance(res, dict) else None
    return _ok(res if isinstance(res, dict) else {"ok": True, "raw": str(res)})


def tool_backup_delete(args: dict) -> dict:
    kind = (args.get("kind") or "").strip()
    slots = args.get("slots") or []
    if kind not in cfgmod.KINDS:
        return _err(f"kind 只能是 {list(cfgmod.KINDS)}，收到 {kind!r}")
    if kind == cfgmod.KIND_STORE:
        return _err("store 是固定副本，只能更新（backup_run kind=store），不能删")
    if not slots:
        return _err("slots 不能为空：给一个或多份 slot id（先用 backup_list 查）")
    if not args.get("confirm"):
        return _err("删除要显式 confirm=true —— 它会把这些份移到回收区")
    done, failed = [], []
    for s in slots:
        r = engine.delete_slot(ROOT, kind, str(s), LOGGER)
        (done if r.get("ok") else failed).append({"slot": s, "result": r})
    return _ok({"ok": not failed, "deleted": len(done), "failed": len(failed),
                "detail": done + failed,
                "recycleKeepDays": engine.recycle_keep_days(CFG)})


def tool_backup_restore(args: dict) -> dict:
    kind = (args.get("kind") or "").strip()
    slot = (args.get("slot") or "").strip()
    dest = (args.get("dest") or "").strip()
    if kind not in cfgmod.KINDS or kind == cfgmod.KIND_STORE:
        return _err(f"kind 只能是 {[k for k in cfgmod.KINDS if k != cfgmod.KIND_STORE]}")
    if not slot or not dest:
        return _err("slot 和 dest 都要给；dest 是解出来的目录（演练用）")
    res = engine.restore_slot(CFG, ROOT, kind, slot, dest=dest,
                              dry_run=bool(args.get("dry_run", True)),
                              logger=LOGGER)
    return _ok(res)


def tool_dsh_status(_args: dict) -> dict:
    port = _dsh_port()
    pids = _port_pids(port)
    cmd = _dsh_cmd_path()
    return _ok({
        "port": port,
        "alive": _port_alive(port),
        "pids": pids,
        "launcher": str(cmd),
        "launcherExists": cmd.is_file(),
        "autoStartOnPanel": bool(CFG.get("autoStartDsh", True)),
        "restartTask": "DSH Restart Now",
        "note": (
            "启动=拉起 DSH.cmd（与双击桌面 DSH.lnk 同一条路）；"
            "重启=Start-ScheduledTask 'DSH Restart Now'；关闭=taskkill /T /F。"
            "备份器的界面一打开也会自动拉这一条（autoStartOnPanel=true 时）。"
        ),
    })


def tool_dsh_control(args: dict) -> dict:
    action = (args.get("action") or "").strip().lower()
    port = _dsh_port()
    if action == "start":
        if _port_alive(port):
            return _ok({"ok": True, "skipped": True, "reason": f"{port} 已在监听"})
        script = _dsh_cmd_path()
        if not script.is_file():
            return _err(f"找不到 {script}（改配置里的 paths.dsh_cmd）")
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", "start", "", str(script)],
            cwd=str(script.parent), close_fds=True,
        )
        return _ok({"ok": True, "started": True, "script": str(script),
                    "hint": "控制台窗口会出现，端口开起来要几十秒"})
    if action == "restart":
        if not args.get("confirm"):
            return _err("重启要显式 confirm=true（当前网关里的会话会断开）")
        r = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "Start-ScheduledTask -TaskName 'DSH Restart Now'"],
            capture_output=True, text=True, timeout=60)
        return _ok({"ok": r.returncode == 0, "rc": r.returncode,
                    "stderr": (r.stderr or "").strip()[:400]})
    if action == "stop":
        if not args.get("confirm"):
            return _err("关闭要显式 confirm=true（进程树会被 taskkill /T /F）")
        pids = _port_pids(port)
        if not pids:
            return _ok({"ok": True, "skipped": True, "reason": f"{port} 没有监听"})
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S603
                           capture_output=True, text=True, timeout=60)
        return _ok({"ok": True, "killed": pids})
    return _err("action 只能是 start / restart / stop")


# ---------------------------------------------------------------- 工具清单

_KIND_ENUM = list(cfgmod.KINDS)

TOOLS = [
    {
        "name": "backup_status",
        "description": ("备份库总览：每类各有多少份、最新一份的时间与大小、库多大、"
                        "磁盘剩余、回收区占用、下次基线时刻、各端口是否在跑。"),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "backup_list",
        "description": "列备份槽。kind 省略则四类全列。返回 slot id / 时间 / 大小 / 文件数 / 是否完好。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _KIND_ENUM,
                         "description": "store / baseline / sessions / workspace"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "backup_run",
        "description": ("立刻做一次备份。baseline=地基（harness+.dsh，不含 session/voice）；"
                        "sessions=会话；workspace=工作区；store=依赖目录副本（更新而非新增）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _KIND_ENUM},
                "note": {"type": "string", "description": "写进 manifest 的备注"},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "backup_delete",
        "description": ("软删除一份或多份：移到回收区（默认 30 天后自动清除），可用界面撤销。"
                        "必须 confirm=true。store 不能删，只能更新。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _KIND_ENUM},
                "slots": {"type": "array", "items": {"type": "string"},
                          "description": "slot id 列表（backup_list 可查）"},
                "confirm": {"type": "boolean", "description": "必须为 true"},
            },
            "required": ["kind", "slots", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "backup_restore",
        "description": ("把某一份演练恢复到指定目录（dest 必填）。解完会逐文件比对，"
                        "用于验证备份是否真能还原；**不落回原路径**——那条路留给界面。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [k for k in _KIND_ENUM if k != "store"]},
                "slot": {"type": "string"},
                "dest": {"type": "string", "description": "解到哪个目录（要空目录/不存在的）"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["kind", "slot", "dest"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dsh_status",
        "description": "DSH 网关状态：3080 端口是否在监听、监听进程 pid、用哪个脚本重启。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dsh_control",
        "description": ("控制 DSH 网关：start=拉起 DSH.cmd（等同双击 DSH.lnk）；"
                        "restart=走计划任务 DSH Restart Now；stop=taskkill /T /F。"
                        "restart 与 stop 必须 confirm=true。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "restart", "stop"]},
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]

_DISPATCH = {
    "backup_status": tool_backup_status,
    "backup_list": tool_backup_list,
    "backup_run": tool_backup_run,
    "backup_delete": tool_backup_delete,
    "backup_restore": tool_backup_restore,
    "dsh_status": tool_dsh_status,
    "dsh_control": tool_dsh_control,
}


# ---------------------------------------------------------------- 协议

def _handle(req: dict) -> dict | None:
    method = req.get("method") or ""
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        fn = _DISPATCH.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"没有这个工具：{name}"}}
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": fn(args)}
        except Exception as exc:  # noqa: BLE001 —— 工具内部错误按 isError 回，不炸服务
            import traceback
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": rid, "result": _err(f"{type(exc).__name__}: {exc}")}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"未实现的方法：{method}"}}


def main() -> int:
    print(f"[dshbackup-mcp] 起来了，备份根 {ROOT}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[dshbackup-mcp] 收到坏 JSON：{exc}", file=sys.stderr)
            continue
        if isinstance(req, list):
            out = [r for r in (_handle(x) for x in req) if r is not None]
        else:
            out = _handle(req)
        if out is None:
            continue
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
