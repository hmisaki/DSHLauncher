from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication


APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT / "vendor"))

from dshbackup import config as cfgmod  # noqa: E402
from app.main_window import MainWindow  # noqa: E402


def main() -> int:
    data_dir = APP_ROOT / "data"
    cfg = cfgmod.load(data_dir)
    app = QApplication(sys.argv)
    app.setApplicationName("DSHLuncher 2")
    app.setOrganizationName("DSH")
    window = MainWindow(cfg, data_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
