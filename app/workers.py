from __future__ import annotations

from typing import Callable, Any

from PySide6.QtCore import QThread, Signal

from dshbackup.engine import Progress


class JobThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[Progress], Any], parent=None) -> None:
        super().__init__(parent)
        self.operation = operation
        self.progress = Progress()

    def run(self) -> None:
        try:
            self.completed.emit(self.operation(self.progress))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
