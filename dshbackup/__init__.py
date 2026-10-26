"""DSH 备份/恢复工具（v2 桌面版）。

四类备份：store 镜像 / baseline 每天 / sessions 每 15 分钟 / workspace 纯手动。
保留算法：几何递增段 + 每段留最老，先按 M 切分再分段，纯 age 不归格。
界面用 PySide6，不监听任何端口；外部只调 7z.exe 与 robocopy。
"""

__version__ = "0.2.0"
