"""JSON Lines 日志。一行一事件，按天切文件。

设计 §10.3：`backup_root\\logs\\backup-YYYYMMDD.log`
固定事件名便于 grep；traceback 只留末 8 层。

本模块**不提供任何格式化函数**（体检 I）：时间/大小的人话化只有一份实现——
时间用 `app.humanize`（`human_ts / human_age / human_span / day_word`），
字节用 `dshbackup.sevenzip.human_bytes`。这里曾经有过一份 `human_age` 与
`human_bytes`，与上面两处同功能不同舍入，两处显示对不上就是这么来的，已删除；
不要再往这里加。
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

FATAL = "fatal"
ERROR = "error"
WARN = "warn"
INFO = "info"

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class Logger:
    """线程安全的追加写日志。写失败绝不影响主流程（备份不能因为日志没写成就失败）。"""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self._rotated = False

    def _rotate_once(self) -> None:
        """体检 B：轮转每次进程只跑一次（首次写日志时），不拖慢每一条日志。"""
        if self._rotated:
            return
        self._rotated = True
        removed = rotate_logs(self.log_dir, LOG_KEEP_DAYS, logger=None)
        if removed:
            self.log(INFO, "log_rotated", removed=len(removed),
                     keepDays=LOG_KEEP_DAYS, names=removed[:10])

    def _open(self, now: datetime):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return open(
            self.log_dir / f"backup-{now:%Y%m%d}.log",
            "a",
            encoding="utf-8",
            newline="\n",
        )

    def log(self, level: str, event: str, **fields) -> dict:
        now = datetime.now().astimezone()
        rec = {"ts": _now_iso(), "level": level, "event": event}
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        line = json.dumps(rec, ensure_ascii=False)
        try:
            self._rotate_once()  # 必须在拿锁之前：它自己要写一条 log_rotated
            with _LOCK:
                with self._open(now) as f:
                    f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
        return rec

    def exception(self, level: str, event: str, exc: BaseException, **fields) -> dict:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tail = "\n".join(tb.strip().splitlines()[-8:])
        return self.log(level, event, detail=tail, **fields)

    # 便捷方法
    def info(self, event: str, **f) -> dict:
        return self.log(INFO, event, **f)

    def warn(self, event: str, **f) -> dict:
        return self.log(WARN, event, **f)

    def error(self, event: str, **f) -> dict:
        return self.log(ERROR, event, **f)

    def fatal(self, event: str, **f) -> dict:
        return self.log(FATAL, event, **f)


def recent_lines(log_dir: Path, limit: int = 200) -> list[dict]:
    """读最近几天的日志尾部，供 UI 折叠区显示。

    体检 B：原来只读 `files[-1]`（最新那一天的文件）。跨天后新文件还没写过东西，
    于是 0:00 到当天首次写日志这段时间，日志区一片空白——看起来像「一天没干活」。
    改成从新到旧逆序读，读够 limit 条就停（空文件自动跳过）。
    """
    d = Path(log_dir)
    if not d.exists():
        return []
    files = sorted(d.glob("backup-*.log"), reverse=True)
    if not files:
        return []
    out: list[dict] = []
    for path in files:
        batch: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        batch.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        if not batch:
            continue  # 当天刚建的空文件
        out = batch[-limit:] + out if len(batch) > limit else batch + out
        if len(out) >= limit:
            return out[-limit:]
    return out[-limit:]


LOG_KEEP_DAYS = 30


def rotate_logs(log_dir: Path, keep_days: int = LOG_KEEP_DAYS, logger=None) -> list[str]:
    """只保留最近 `keep_days` 天的 `backup-*.log`，更老的删掉（删失败静默）。

    体检 B：日志原本永不轮转——一天一个文件，一年 365 个、体积无上限。
    文件名就是日期（backup-YYYYMMDD.log），所以按名字判年龄，不需要读 mtime。
    """
    d = Path(log_dir)
    if not d.exists():
        return []
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=int(keep_days))).strftime("%Y%m%d")
    removed: list[str] = []
    for path in sorted(d.glob("backup-*.log")):
        stamp = path.stem.split("-", 1)[-1] if "-" in path.stem else ""
        if len(stamp) != 8 or not stamp.isdigit():
            continue
        if stamp >= cutoff:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    if removed and logger is not None:
        try:
            logger.info("log_rotated", removed=len(removed), keepDays=int(keep_days),
                        names=removed[:10])
        except Exception:  # noqa: BLE001
            pass
    return removed


def disk_free_gb(path: Path) -> float:
    import shutil

    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return -1.0
