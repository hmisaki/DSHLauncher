"""桌面入口：python launcher.py [--root DIR] [--check]

- 首次运行把 config.example.json 复制成 <backup_root>\\config.json
- 校验失败就地打印，不弹窗（方便命令行排查）
- 校验通过拉起 PySide6 主窗口，不占任何端口
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dshbackup import config as cfgmod, log as logmod  # noqa: E402


def _dsh_alive(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _dsh_process_running() -> bool:
    """端口之外再看一眼进程。

    DSH 重启的那十几秒里 3080 是空的，只看端口会趁机再拉一个实例。
    """
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
                "Where-Object { $_.CommandLine -like '*DSH.cmd*' } | "
                "Measure-Object).Count",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        return out.isdigit() and int(out) > 0
    except Exception:
        return False


def _maybe_start_dsh(cfg: dict, logger) -> str:
    """界面起来时顺手把网关拉起来，返回一句给日志/用户看的结果。"""
    if not cfg.get("autoStartDsh", True):
        return "自动拉起已关闭（config.json 的 autoStartDsh=false）"
    port = int(cfg.get("ports", {}).get("dsh", 3080))
    if _dsh_alive(port):
        return f"DSH 已在 {port} 上跑着，没动它"
    if _dsh_process_running():
        return "DSH 正在启动或重启中，等它自己开端口"
    cmd = Path(str(cfg.get("paths", {}).get("dsh_cmd") or ""))
    if not cmd.is_file():
        logger.error("dsh_cmd_missing", path=str(cmd))
        return f"找不到启动脚本 {cmd}，网关没拉起来"
    try:
        # 不能用 os.startfile：它走 ShellExecute，对 .cmd 会弹
        # 「打开文件 - 安全警告：无法验证发布者」（2026-10-26 用户实测）。
        # cmd /c start 只是普通进程创建，不经过那个检查，控制台窗口照常可见。
        subprocess.Popen(  # noqa: S603
            ["cmd.exe", "/c", "start", "", str(cmd)],
            cwd=str(cmd.parent), close_fds=True,
        )
    except OSError as exc:
        logger.error("dsh_start_failed", error=str(exc), cmd=str(cmd))
        return f"拉起 DSH 失败：{exc}"
    logger.info("dsh_autostart", cmd=str(cmd), port=port)
    return f"已拉起 DSH（{cmd.name}），端口 {port} 要等它加载完"


def _prepare(root: Path, cfg: dict, logger) -> bool:
    """返回 True 表示配置可用。"""
    root.mkdir(parents=True, exist_ok=True)
    cfgmod.ensure_dirs(root, cfg)
    errs = cfgmod.validate(cfg)
    if errs:
        print("配置有几处要改：", file=sys.stderr)
        for e in errs:
            print(f"  ✗ {e}", file=sys.stderr)
        print(f"\n配置文件：{cfgmod.config_file(root)}", file=sys.stderr)
        logger.error("config_invalid", count=len(errs), errors=errs[:12])
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dshbackup", description="DSH 备份（桌面版）")
    ap.add_argument("--root", default=None, help="备份根目录（默认取配置里的值）")
    ap.add_argument("--check", action="store_true", help="只校验配置然后退出")
    ap.add_argument("--reset-config", action="store_true", help="用默认值覆盖 config.json")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else Path(
        cfgmod.DEFAULTS["paths"]["backup_root"]
    )
    root.mkdir(parents=True, exist_ok=True)
    logger = logmod.Logger(root / "logs")

    example = HERE / "config.example.json"
    cfg_file = cfgmod.config_file(root)
    if args.reset_config or not cfg_file.exists():
        if example.exists():
            shutil.copyfile(example, cfg_file)
            print(f"已生成配置：{cfg_file}")

    cfg = cfgmod.load(root)
    cfg["paths"]["backup_root"] = str(root)

    if not _prepare(root, cfg, logger):
        return 2
    print(f"配置校验通过：{cfg_file}")
    if args.check:
        return 0

    from PySide6.QtWidgets import QApplication  # 延迟导入，--check 时不拉起 Qt

    from app.main_window import MainWindow, app_icon, global_qss

    app = QApplication(sys.argv[:1])
    app.setApplicationName("DSH 备份")
    # UX-13：Tab 走到哪个控件原来完全看不出来（走查里因此误触了一次 524 MB 备份）。
    # 焦点框必须全局设：按钮上 outline 无效，Qt 里只认 border / 背景色变化。
    # UX-18：表格 hover 与选中色一起写在这里，两处颜色只维护一份。
    app.setStyleSheet(global_qss())
    # UX-17：任务栏里没有图标、两个窗口分不出来 → 给一个自绘图标（不依赖外部图片文件）
    app.setWindowIcon(app_icon())
    win = MainWindow(cfg, root, logger)
    win.show()

    # 网关拉起放进事件循环：查进程要几百毫秒，别挡窗口首次绘制。
    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, lambda: print(f"[dshbackup] {_maybe_start_dsh(cfg, logger)}"))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
