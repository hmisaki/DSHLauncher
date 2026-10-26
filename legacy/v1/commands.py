"""环境的启停：只调配置里那三个命令，不自己造启动逻辑。

设计 §5.7：start_cmd / stop_cmd / restart_cmd 三个字符串槽，
支持 {workspace} {port} {dsh_home} {checkout} {pnpm} {dsh_pid} 占位符。
重启在本机必须落到 `Start-ScheduledTask -TaskName 'DSH Restart Now'`（AGENTS.md 硬约束）。
"""
from __future__ import annotations

import socket
import subprocess

from . import config as cfgmod

SLOTS = ("start_cmd", "stop_cmd", "restart_cmd")


def _dsh_pid(cfg: dict, host: str = "127.0.0.1") -> int | None:
    """从端口反查 PID（只用 netstat，不引 psutil）。"""
    port = cfg["ports"]["dsh"]
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def health(cfg: dict) -> dict:
    host = cfg["server"]["host"]
    ports = cfg["ports"]
    state = {name: port_open(p, host) for name, p in ports.items()}
    pid = _dsh_pid(cfg, host)
    return {
        "ports": state,
        "dshRunning": state.get("dsh", False),
        "dshPid": pid,
        "allUp": all(state.values()),
    }


def run_slot(cfg: dict, which: str, logger=None) -> dict:
    if which not in SLOTS:
        return {"ok": False, "reason": f"未知命令槽 {which}（只有 {', '.join(SLOTS)}）"}
    raw = cfg["commands"].get(which, "")
    if not raw.strip():
        return {"ok": False, "reason": f"commands.{which} 是空的"}

    extra = {"dsh_pid": _dsh_pid(cfg) or ""}
    cmd = cfgmod.expand(raw, cfg, extra)
    timeout = int(cfg["commands"].get("timeout_seconds", 120))
    if logger:
        logger.info("cmd_start", slot=which, cmd=cmd[:400], timeout=timeout)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if logger:
            logger.error("cmd_timeout", slot=which, timeout=timeout)
        return {"ok": False, "reason": f"命令超时（{timeout} 秒）", "cmd": cmd}
    except OSError as exc:
        if logger:
            logger.error("cmd_fail", slot=which, error=str(exc))
        return {"ok": False, "reason": f"命令无法执行：{exc}", "cmd": cmd}

    ok = r.returncode == 0
    if logger:
        (logger.info if ok else logger.error)(
            "cmd_ok" if ok else "cmd_fail",
            slot=which,
            rc=r.returncode,
            stdout=(r.stdout or "")[-500:],
            stderr=(r.stderr or "")[-500:],
        )
    return {
        "ok": ok,
        "slot": which,
        "cmd": cmd,
        "rc": r.returncode,
        "stdout": (r.stdout or "")[-2000:],
        "stderr": (r.stderr or "")[-2000:],
    }


def wait_up(cfg: dict, seconds: int = 60) -> dict:
    """起完之后轮询端口，告诉 UI「起没起来」。"""
    import time

    host = cfg["server"]["host"]
    port = cfg["ports"]["dsh"]
    t0 = time.time()
    while time.time() - t0 < seconds:
        if port_open(port, host):
            return {"ok": True, "waitedSeconds": round(time.time() - t0, 1)}
        time.sleep(1.0)
    return {"ok": False, "waitedSeconds": seconds, "reason": f"{seconds} 秒内端口 {port} 没通"}
