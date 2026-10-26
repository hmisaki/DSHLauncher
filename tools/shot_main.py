"""生成 README 用的主界面截图（offscreen，不需要真开窗口）。

    python tools/shot_main.py [输出路径]

默认输出 docs/screenshots/main.png。用的是真实备份库（data/）里的索引，
所以截出来的列表、时间轴刻度都是当前实际状态；没有 data/ 时退化成空列表。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 平台默认不带字体目录，不设这个变量截出来全是方框
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main_window import MainWindow, app_icon, global_qss  # noqa: E402
from dshbackup import config as cfgmod, log as logmod  # noqa: E402


def main() -> int:
    root = ROOT / "data"
    if (root / "config.json").exists():
        cfg = cfgmod.load(root)
    else:
        cfg = cfgmod.default_config()
    logger = logmod.Logger(root / "logs")

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(global_qss())
    app.setWindowIcon(app_icon())

    win = MainWindow(cfg, root, logger)
    win.resize(1600, 1000)
    win.show()
    app.processEvents()
    win.reload_table()
    app.processEvents()

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots" / "main.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    ok = win.grab().save(str(out))
    print(f"{'已存' if ok else '保存失败'} {out}" + (f"  {out.stat().st_size} B" if ok else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
