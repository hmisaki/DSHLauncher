"""后台任务线程：把 engine 的阻塞调用挪出 UI 线程。"""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal


class Job(QThread):
    """跑一个可调用对象，结束时把结果（或异常文本）发回主线程。"""

    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., dict], *args, **kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # noqa: D102
        try:
            res = self._fn(*self._args, **self._kwargs)
            self.finished_ok.emit(res if isinstance(res, dict) else {"ok": True, "result": res})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n{traceback.format_exc(limit=5)}")
