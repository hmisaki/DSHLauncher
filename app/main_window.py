"""主窗口：状态区 + 动作按钮 + 四视图备份历史 + 进度条。"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import restore_batch
from app.restore_dialog import RestoreDialog
from app.settings_dialog import SettingsDialog
from app.timeline import TimelineWidget
from app.workers import Job
from dshbackup import config as cfgmod, engine, manifest
from dshbackup import sevenzip
from dshbackup.sevenzip import human_bytes

# U-06：原始 kind 只出现在目录名里；给人看的一律走这张表（定义在 config，engine 也用它）
KIND_LABEL = cfgmod.KIND_LABEL
SUCCESS_GREEN = "#0f7a3d"
DANGER_RED = "#c0392b"

# 四列并排的固定顺序（用户 2026-10-26 定的概念链）：
#   store（依赖目录，手动更新）→ 基线（harness + .dsh，不含 session/voice）
#   → 会话（每 15 分钟自动）→ 工作区（手动备份，所以放最后）
# 顺序改了要同步 tmp\ui_selfcheck.py 的断言。
# 「里程碑」不是 kind（v2 时代那套文件夹合装结构早就没有了），所以没有第五列。
KIND_PAGES = (
    (cfgmod.KIND_STORE, "store"),
    (cfgmod.KIND_BASELINE, "基线"),
    (cfgmod.KIND_SESSIONS, "会话"),
    (cfgmod.KIND_WORKSPACE, "工作区"),
)

# 单列最窄宽度：再窄三列放不下「10-26 05:35」+「525.7 MB」，会挤成两行
COL_MIN_WIDTH = 200
COL_CHECK_WIDTH = 24
COL_SIZE_WIDTH = 90


def compact_ts(created_ms) -> str:
    """列内紧凑时间：今天只 `05:35`、昨天 `昨天 05:35`、同年 `10-26 05:35`。

    完整到秒的时间进 tooltip。列宽只有 200px，`今天 05:35:31` 那种写法会把
    大小列挤走，而"是不是今天的"在四列并排的视图里不需要每天都重复说。
    """
    if not created_ms:
        return ""
    dt = datetime.fromtimestamp(int(created_ms) / 1000)
    today = datetime.now().date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    if (today - dt.date()).days == 1:
        return "昨天 " + dt.strftime("%H:%M")
    if dt.year == today.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%y-%m-%d %H:%M")


def port_alive(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


from app.humanize import (  # noqa: F401（restore_dialog 也走这里拿）
    day_word, human_age, human_gb, human_span, human_span_long, human_ts,
)


def cover_of(root: Path, kind: str) -> str:
    """UX-01：能力段只认实际数据——该种类最老一份包的年龄；一份都没有就直说没有。

    原来这里读的是配置里的 M（保留窗口上限），那是策略，不是库里真有的东西：
    0 份基线时也写「可回退 16 天」，比不说更误导（走查 §1 / ux-07）。
    """
    slots = manifest.scan(root, kind)
    if not slots:
        return "还没有备份"
    oldest = min(int(s.get("createdAt") or 0) for s in slots)
    if not oldest:
        return "还没有备份"
    return human_span(datetime.now().timestamp() * 1000 - oldest)


def disk_tier(free_gb: float, lib_gb: float, snap_gb: float) -> int:
    """R3-5 三档判据（抽成纯函数，自检可以直接喂数字验阈值）。

    ③ 危急：剩余 < 一份基线 × 2 —— 存不下下一份，红字 + ⚠
    ② 偏紧：剩余 ≤ max(库占用 × 2, 20 GB) —— 橙字 + 「够再存 N 份」
    ① 正常：其余
    """
    if free_gb < snap_gb * 2:
        return 3
    if free_gb <= max(lib_gb * 2, 20.0):
        return 2
    return 1


def global_qss() -> str:
    """UX-13 / UX-18：全局样式——焦点框、表格 hover 与选中色。

    焦点框只能靠 border：Qt 的 QPushButton 不支持 outline，靠样式表给不出系统那种焦点虚框，
    于是在主色上做 2px 实线边框（放在应用级而不是单个 widget，Tab 到哪个控件都看得见）。
    """
    return f"""
    QPushButton:focus, QLineEdit:focus, QComboBox:focus, QCheckBox:focus,
    QSpinBox:focus, QTableWidget:focus, QGroupBox:focus {{
        border: 2px solid {SUCCESS_GREEN};
    }}
    QPushButton {{
        padding: 4px 10px;
    }}
    QTableWidget::item:hover {{
        background: #eef6ff;
    }}
    QTableWidget::item:selected {{
        background: #d6e9ff;
        color: #111111;
    }}
    """


def app_icon() -> QIcon:
    """UX-17：自绘一个 64×64 图标（绿底白字），不依赖外部图片文件。

    走查时两个窗口在任务栏里都是空白默认图标，完全分不出是哪个库。
    """
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor(SUCCESS_GREEN))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(2, 2, 60, 60, 12, 12)
        p.setPen(QColor("#ffffff"))
        f = QFont()
        f.setPointSize(22)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "备")
    finally:
        p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, root: Path, logger) -> None:
        super().__init__()
        self.cfg = cfg
        self.root = Path(cfg["paths"]["backup_root"])
        self.logger = logger
        self.progress = engine.Progress()
        self.job: Job | None = None
        self._last_auto = 0.0
        self._busy_btn: QPushButton | None = None
        self._busy_btn_text = ""
        self._busy_text = "备份中…"
        self._last_call: tuple | None = None
        self._tick_n = 0
        self._finish_until = 0.0
        # UX-03：进度行/结论区只描述「最近一次动作」，动作一变就整体重置
        self._action = ""
        # UX-04：软删除的撤销窗口
        self._undo: dict | None = None
        self._undo_left = 0
        self._is_restore = False

        # UX-17：两个实例同标题同位置完全重叠，任务栏也分不出来 → 标题带库路径尾段
        self.setWindowTitle(f"DSH 备份 — {self.root.name or self.root}")
        # 四列并排要放得下：默认窗口按令文 1400×820
        self.resize(
            int(cfg.get("ui", {}).get("window_width", 1400)),
            int(cfg.get("ui", {}).get("window_height", 820)),
        )
        # 令文：最小宽下调到 900（原 820）。再窄就靠表格区横向滚动，不压列。
        self.setMinimumSize(900, 560)

        # UX-17：记住上次位置尺寸；若已有同位置窗口（两个库同时开），错开 40px 起见得到
        self._qset = QSettings("DSH", "DSHBackup")
        self._restore_geometry()

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(9)

        # ---- 状态区 ----
        # 首屏要回答的第一个问题是「我现在能退到哪天」，端口只是环境噪音，降权到最右。
        # UX-01：能力（实际能退多久）与策略（保留窗口上限）拆成两段，字号与颜色都不同，
        # 免得再出现「0 份包却写可回退 16 天」这种拿上限冒充实际值的事。
        self.lbl_cover = QLabel("…")
        self.lbl_policy = QLabel("…")
        self.lbl_last = QLabel("…")
        self.lbl_store = QLabel("…")
        self.lbl_ports = QLabel("…")
        self._cover_full = ""
        # 首行只放「能力」（能退到哪天）+ 最右的环境噪音。
        # 策略（上限）、上次基线时间、store 时间挪到第二行小字：
        # 首行原来 5 个标签挤在一起，1180px 下「可回退 基线 5 小时 · 会话记…」被截断，
        # 而首屏第一个该回答的问题就是「我能退到哪天」——它不该输给策略上限。
        status_row = QHBoxLayout()
        status_row.addWidget(self.lbl_cover)
        status_row.addStretch(1)
        status_row.addWidget(self.lbl_ports)
        self.lbl_cover.setStyleSheet("font-size:16px;font-weight:600")
        self.lbl_cover.setTextFormat(Qt.PlainText)
        self.lbl_cover.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.lbl_ports.setStyleSheet("color:#666666;font-size:12px")
        self.lbl_ports.setToolTip("端口 3080 / 8080 / 17493 只做连通性探测，不代表备份是否完好")
        lay.addLayout(status_row)

        # 第二行：细信息靠左，策略上限靠右（只当上限说，不抢能力的位置）
        info_row = QHBoxLayout()
        for w in (self.lbl_last, self.lbl_store):
            w.setTextFormat(Qt.RichText)
            info_row.addWidget(w)
            info_row.addSpacing(14)
        info_row.addStretch(1)
        self.lbl_policy.setTextFormat(Qt.RichText)
        info_row.addWidget(self.lbl_policy)
        self.lbl_policy.setStyleSheet("color:#666666;font-size:12px")
        self.lbl_policy.setToolTip("策略上限，不是你现在拥有的：由配置文件的保留参数决定")
        self.lbl_last.setStyleSheet("font-size:13px;color:#444444")
        lay.addLayout(info_row)

        # ---- 动作按钮（U-05：主 / 次 / 工具 三段，不再 7 个同权重并排）----
        btn_row = QHBoxLayout()
        self.btn_base = QPushButton("立即备份基线")
        self.btn_sess = QPushButton("备份会话")
        self.btn_ws = QPushButton("备份工作区")
        self.btn_store = QPushButton("更新 store")
        self.btn_open = QPushButton("打开备份目录")
        # UX-14：原来叫「查看日志」，点开其实是资源管理器 → 名字照实说
        self.btn_log = QPushButton("打开日志文件夹")
        self.btn_log.setToolTip(
            "日志是纯文本，一天一个文件：logs\\backup-YYYYMMDD.log\n"
            "点一下会在资源管理器里打开这个目录，并告诉你今天那个文件的全路径。"
        )
        self.btn_set = QPushButton("设置")

        # 主操作：半粗 + 更大内边距，视线一眼能抓到
        self.btn_base.setStyleSheet("font-weight:600;padding:6px 16px")
        self.btn_set.setFixedWidth(72)          # 设置最右且窄，不跟备份抢注意力
        for b in (self.btn_base,):              # 第一段：主
            btn_row.addWidget(b)
        btn_row.addSpacing(18)
        for b in (self.btn_sess, self.btn_ws, self.btn_store):   # 第二段：次
            btn_row.addWidget(b)
        btn_row.addStretch(1)                   # 第三段：工具靠右
        # DSH 网关控制（用户 2026-10-26）：从这里也能开关网关，但**底层仍是脚本与计划任务**
        # —— 桌面快捷方式、DSH.cmd、DSH-restart.cmd 全部原样保留，这条路只是多一个入口，
        # 不是替代品。所以哪天这个界面打不开，双击 DSH.lnk 照样能起。
        self.btn_dsh_start = QPushButton("启动 DSH")
        self.btn_dsh_start.setToolTip(
            "等同双击桌面的 DSH.lnk —— 调 D:\\AIBot\\WorkSpace\\DSH.cmd，可见控制台。\n"
            "已经跑着的话它自己会拒绝重复启动。")
        self.btn_dsh_restart = QPushButton("重启 DSH")
        self.btn_dsh_restart.setToolTip(
            "等同 Start-ScheduledTask -TaskName 'DSH Restart Now'。\n"
            "这条必须走计划任务：直接 start 起的进程会被 job object 一起杀掉。")
        self.btn_dsh_stop = QPushButton("关闭 DSH")
        self.btn_dsh_stop.setStyleSheet(f"color:{DANGER_RED}")
        self.btn_dsh_stop.setToolTip(
            "按端口找到网关进程，taskkill /T /F 连子进程一起关。\n"
            "会先弹确认框——关掉之后当前这个会话也就断了。")
        for _b in (self.btn_dsh_start, self.btn_dsh_restart, self.btn_dsh_stop):
            btn_row.addWidget(_b)
        btn_row.addSpacing(18)
        for b in (self.btn_open, self.btn_log):
            btn_row.addWidget(b)
        btn_row.addSpacing(18)
        btn_row.addWidget(self.btn_set)
        lay.addLayout(btn_row)

        self.btn_base.clicked.connect(lambda: self.run_kind(cfgmod.KIND_BASELINE))
        self.btn_sess.clicked.connect(lambda: self.run_kind(cfgmod.KIND_SESSIONS))
        self.btn_ws.clicked.connect(lambda: self.run_kind(cfgmod.KIND_WORKSPACE))
        self.btn_store.clicked.connect(self.run_store)
        self.btn_open.clicked.connect(self.open_backup_dir)
        self.btn_log.clicked.connect(self.open_log_dir)
        self.btn_set.clicked.connect(self.open_settings)
        self.btn_dsh_start.clicked.connect(self.dsh_start)
        self.btn_dsh_restart.clicked.connect(self.dsh_restart)
        self.btn_dsh_stop.clicked.connect(self.dsh_stop)

        # ---- 进度 ----
        prog_row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        self.lbl_phase = QLabel("空闲")
        self.lbl_phase.setMinimumWidth(360)
        self.lbl_phase.setToolTip("本次不可中断：中断会留半成品包，结束后自动清理")
        # 裁决 1：暂停是首屏总开关（不是藏在设置页），且「开/已暂停 + 暂停了多久」常显。
        # 走查原文：「关掉程序再打开就悄悄恢复自动备份」——用户以为自己还在暂停里。
        self.chk_pause = QCheckBox("暂停自动备份")
        self.chk_pause.setStyleSheet("font-weight:600;font-size:14px")
        self.chk_pause.setToolTip(
            "管什么：只暂停「基线」和「会话」的自动备份——手动按钮照常可用，"
            "「工作区」和「store」本来就不自动跑。\n"
            "正在跑的那一次不会被打断：勾上只停本工具的定时器，已经开始的会跑完。\n"
            "什么时候复位：勾选状态会写进配置文件（paused），关掉程序再打开仍然是暂停，"
            "要恢复就取消勾选；想跳过它跑一次命令行加 --force（配置 paused 见 README §5）。\n"
            "和 Windows 计划任务无关：如果你另外挂了计划任务去跑命令行，它照旧会跑，"
            "要一起停就在那边也加 paused=true（或 --paused）。"
        )
        self.lbl_pause_state = QLabel("")
        self.lbl_pause_state.setTextFormat(Qt.RichText)
        self.lbl_pause_state.setStyleSheet("font-weight:600;font-size:13px")
        self.chk_pause.stateChanged.connect(self._on_pause_toggled)
        prog_row.addWidget(self.bar, 1)
        prog_row.addWidget(self.lbl_phase)
        prog_row.addWidget(self.lbl_pause_state)
        prog_row.addWidget(self.chk_pause)
        lay.addLayout(prog_row)

        # ---- 历史表 ----
        filter_row = QHBoxLayout()
        # 类型不再做下拉：改成四页签之后，两个"选类型"的地方同时存在会让人怀疑到底哪个生效。
        # 页签是类型，时间窗对所有页生效。
        filter_row.addWidget(QLabel("时间"))
        self.cmb_range = QComboBox()
        for _label, _days in (
            ("全部时间", 0), ("今天", 1), ("昨天", -3),
            ("最近 7 天", 7), ("最近 30 天", 30), ("自定义…", -2),
        ):
            self.cmb_range.addItem(_label, _days)
        self._custom_range: tuple[datetime, datetime] | None = None
        self.cmb_range.currentIndexChanged.connect(self._on_range_changed)
        filter_row.addWidget(self.cmb_range)
        filter_row.addStretch(1)
        # 类型下拉：四视图之后类型由页签决定，这个控件不再出现在界面上。
        # 留一个恒为「全部」的占位，是因为 _clear_filters / 自检按「类型+时间两处筛选一起复位」
        # 的旧口径写的——多留一个不可见的占位，比把那两处调用点全改一遍风险小。
        self.cmb_kind = QComboBox()
        self.cmb_kind.addItem("全部", "")
        self.cmb_kind.setVisible(False)
        self.lbl_count = QLabel("")
        filter_row.addWidget(self.lbl_count)
        lay.addLayout(filter_row)

        act_row = QHBoxLayout()
        act_row.setContentsMargins(0, 0, 0, 0)
        self.btn_restore = QPushButton("取回备份…")
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self._on_restore_clicked)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._on_delete_clicked)
        act_row.addWidget(self.btn_restore)
        act_row.addWidget(self.btn_delete)
        # UX-04：软删除的撤销窗口——删完 5 秒内可原样移回，超时按钮自己消失
        self.btn_undo = QPushButton("撤销删除（5）")
        self.btn_undo.setStyleSheet(f"color:{SUCCESS_GREEN};font-weight:600")
        self.btn_undo.setVisible(False)
        self.btn_undo.clicked.connect(self._undo_delete)
        act_row.addWidget(self.btn_undo)
        # UX-06：提示句按「有没有选中」切换，0 行时不再说「选中一行再操作」
        self.lbl_hint = QLabel("")
        self.lbl_hint.setStyleSheet("color:#666666;padding-left:8px")
        act_row.addWidget(self.lbl_hint)
        act_row.addStretch(1)
        # 裁决 2：批量清空只在「已暂停」时可用——自动备份跑着的时候清空，
        # 下一分钟又冒出新包，用户会以为清空失败（而且真会删掉刚备份的那份）。
        self.btn_clear = QPushButton("清空历史…")
        self.btn_clear.clicked.connect(self._on_clear_history)
        act_row.addWidget(self.btn_clear)
        lay.addLayout(act_row)

        # 四列并排（令文 task-bcfca4bdf4ac）：四类备份同时可见，不再切页签。
        # 每列只留 勾选/时间/大小；文件数与状态进 tooltip——200px 放不下五列，
        # 而"这份能不能用"在并排视图里靠颜色点不出来，所以改成悬停可查 + 丢失的行标红。
        self.tables: dict[str, QTableWidget] = {}
        self._empty_lbl: dict[str, QLabel] = {}
        self._empty_btn: dict[str, QPushButton] = {}
        self._col_head: dict[str, QLabel] = {}
        self._col_del: dict[str, QPushButton] = {}
        # 最后获得焦点的那张表：单槽操作（取回/删除/双击）按它定位，
        # 不再有"当前页"这个概念。
        self._active_tbl: QTableWidget | None = None
        # kind → 已勾选的 slot 集合。跨列各自保留——用户勾的是"这几份包"，
        # 不是"这一列里这几行"；清空的条件只有两个：解压成功、或他手动取消勾选。
        self._checked: dict[str, set[str]] = {k: set() for k, _ in KIND_PAGES}
        self._page_rows: dict[str, list[dict]] = {k: [] for k, _ in KIND_PAGES}
        self._kind_total: dict[str, int] = {}
        self._total_now = 0
        self._slot_index: dict[tuple[str, str], dict] = {}
        self._loading = False
        # 拼装恢复的运行期状态（进度字典由解压线程写、_batch_pump 读）
        self._batch_prog: dict = {}
        self._batch_cancel: threading.Event | None = None
        self._batch_dlg: QProgressDialog | None = None
        self._batch_pump_timer: QTimer | None = None

        # 四列的容器：宽度不够时整片横向滚动，绝不把单列压到 200 以下
        self.cols_host = QWidget()
        self.cols_host.setMinimumWidth(
            COL_MIN_WIDTH * len(KIND_PAGES) + 8 * (len(KIND_PAGES) - 1)
        )
        cols = QHBoxLayout(self.cols_host)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(8)
        for _kind, _label in KIND_PAGES:
            box = QWidget()
            box.setMinimumWidth(COL_MIN_WIDTH)
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(3)
            head_row = QHBoxLayout()
            head_row.setContentsMargins(0, 0, 0, 0)
            head_row.setSpacing(4)
            head = QLabel(_label)
            head.setStyleSheet("font-weight:600")
            head.setToolTip(f"{_label}：双击一行取回，勾选可跨列拼装")
            self._col_head[_kind] = head
            head_row.addWidget(head)
            head_row.addStretch(1)
            # 用户 2026-10-26：每一列都要自带删除入口——勾了就删本列勾的那几份（多选），
            # 没勾就删当前焦点那一行。不必先"切到某列"再跑去顶部找按钮。
            del_btn = QPushButton("删除")
            del_btn.setFixedHeight(22)
            del_btn.setStyleSheet("font-size:12px;padding:0 8px")
            del_btn.setToolTip("删除本列勾选的几份；没勾就删当前选中的那一行（进回收区，可找回）")
            del_btn.clicked.connect(lambda _c=False, k=_kind: self.delete_in_kind(k))
            self._col_del[_kind] = del_btn
            if _kind == cfgmod.KIND_STORE:
                # store 只能更新不能删：按钮留着占位但置灰——直接隐藏会让这一列的
                # 表头矮一截，四列高度就对不齐了（自检里有这条几何断言）。
                del_btn.setEnabled(False)
                del_btn.setToolTip("store 只能「更新」，不参与按时间淘汰")
                del_btn.setText("—")
            head_row.addWidget(del_btn)
            v.addLayout(head_row)
            tbl = self._make_table(_kind)
            v.addWidget(tbl, 1)
            cols.addWidget(box, 1)
            self.tables[_kind] = tbl
        self.scroll_cols = QScrollArea()
        self.scroll_cols.setWidgetResizable(True)
        self.scroll_cols.setWidget(self.cols_host)
        self.scroll_cols.setFrameShape(QFrame.NoFrame)
        self.scroll_cols.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_cols.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        lay.addWidget(self.scroll_cols, 1)

        # 四条泳道的时间轴（用户 2026-10-26 要求）：
        # 每一类备份在各自的轴上一个点，拖动平移、滚轮缩放，点一下选中那次备份。
        # 放在表格下方而不是单独一页：表格回答「有哪些」，时间轴回答「哪几天备过」。
        self.timeline = TimelineWidget()
        self.timeline.slotClicked.connect(self._on_timeline_click)
        lay.addWidget(self.timeline)

        # 底部跨类操作条：常驻，不属于任何一页
        sel_row = QHBoxLayout()
        self.lbl_sel = QLabel("已选 0 项")
        self.lbl_sel.setStyleSheet("font-weight:600")
        self.lbl_sel.setToolTip("勾选框跨页保留：在四页里挑，最后一起来解")
        # 同类勾多份时：结果虽然确定（后解盖前解 = 只剩最新那份），但用户会以为"两个版本合并"
        # → 不拦、不弹框，只在操作条上红字说一句（§ 裁决：允许 + 必须告知）
        self.lbl_sel_warn = QLabel("")
        self.lbl_sel_warn.setStyleSheet("color:#c0392b;font-weight:600")
        self.lbl_sel_warn.setVisible(False)
        self.lbl_sel_warn.setToolTip("同一类里勾了多份时，只有 createdAt 最新的那份能留下")
        self.btn_batch = QPushButton("恢复所选…")
        self.btn_batch.setEnabled(False)
        self.btn_batch.setToolTip("把勾选的几份按时间顺序解到同一个目录（后解覆盖前解）")
        self.btn_batch.clicked.connect(self._on_batch_restore)
        sel_row.addWidget(self.lbl_sel)
        sel_row.addWidget(self.lbl_sel_warn)
        sel_row.addStretch(1)
        sel_row.addWidget(self.btn_batch)
        lay.addLayout(sel_row)

        # 空态每列各一套（跟着自己的 viewport 走，不用在滚动时搬来搬去）
        for _kind, _ in KIND_PAGES:
            tbl = self.tables[_kind]
            lbl = QLabel("", tbl.viewport())
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setTextFormat(Qt.RichText)
            # 列只有 ~200px：不换行会把「点上面「备份会话/备份工作区」」整句切掉
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#666666;font-size:15px")
            lbl.setVisible(False)
            btn = QPushButton("立即备份基线", tbl.viewport())
            btn.setStyleSheet("font-weight:600;padding:6px 16px")
            btn.clicked.connect(lambda _c=False, k=_kind: self._on_empty_action(k))
            btn.setVisible(False)
            self._empty_lbl[_kind] = lbl
            self._empty_btn[_kind] = btn

        self.setStatusBar(QStatusBar())
        # UX-20：底栏只有一个 6pt 灰字，成功/失败/进行中长得一样 → 左端放状态徽标
        self.lbl_badge = QLabel("")
        self.lbl_badge.setTextFormat(Qt.RichText)
        self.lbl_badge.setStyleSheet("font-weight:600;padding-left:4px")
        self.statusBar().addWidget(self.lbl_badge)
        # 硬伤 4：备份库会越长越大，底栏必须常显「库多大 / 磁盘还剩多少 / 回收区多大」。
        # 只在失败时才说磁盘不足，等于让用户在无感知的情况下把盘塞满。
        self.lbl_disk = QLabel("")
        self.lbl_disk.setTextFormat(Qt.RichText)
        self.lbl_disk.setStyleSheet("padding-right:6px")
        self.statusBar().addPermanentWidget(self.lbl_disk)
        self._set_badge("idle")
        self._set_status("就绪")

        # UX-13：Tab 顺序写死，别让键盘用户在 7 个按钮里靠猜。
        # 「删除选中」故意排在最后（第二圈才够到），它旁边就是「恢复」，
        # 走查里正是因为焦点不可见 + 顺序随机，误触了一次 524 MB 的基线备份。
        order = [self.btn_base, self.btn_sess, self.btn_ws, self.btn_store,
                 self.btn_restore, self.btn_open, self.btn_log, self.btn_set,
                 self.chk_pause, self.cmb_range, self.tables[KIND_PAGES[0][0]],
                 self.btn_batch, self.btn_delete, self.btn_clear]
        for a, b in zip(order, order[1:]):
            QWidget.setTabOrder(a, b)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1200)

        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_due)
        self.auto_timer.start(60000)

        self.refresh_status()
        self.reload_table()
        # 裁决 1：暂停状态从配置里读回来——上次暂停着关掉，这次打开必须仍然是暂停。
        self._apply_pause_state(engine.read_pause(self.root))
        self._purge_recycle()
        QTimer.singleShot(1500, self._auto_due)

    # ---------- 四视图 ----------

    def _make_table(self, kind: str) -> QTableWidget:
        """一列一张表：只有 勾选 / 时间 / 大小 三列（令文：文件数与状态进 tooltip）。

        200px 一列放五列会挤成两行；"能不能用"不靠一列写着，靠 tooltip + 丢失行标红。
        """
        tbl = QTableWidget(0, 3)
        tbl.setHorizontalHeaderLabels(["", "时间", "大小"])
        tbl.verticalHeader().setVisible(False)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 单选：单槽恢复（双击/取回备份…）一次只针对一行；跨列批量走勾选框，互不干扰
        tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setProperty("kind", kind)
        tbl.setWordWrap(False)
        # 四列并排 → 滚动条窄一点，省下来的宽度给时间列
        tbl.setStyleSheet(
            "QScrollBar:vertical{width:9px;background:transparent}"
            "QScrollBar:horizontal{height:9px;background:transparent}"
        )
        tbl.itemSelectionChanged.connect(lambda t=tbl: self._on_selection_changed(t))
        tbl.doubleClicked.connect(self._on_row_double_clicked)
        tbl.itemChanged.connect(self._on_item_changed)
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setStretchLastSection(False)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(lambda sec, t=tbl: self._on_header_clicked(t, sec))
        # 列宽只在这里定一次（U-12 的老规矩：每次 reload 都自适应会让用户正看的那行横移）
        tbl.setColumnWidth(0, COL_CHECK_WIDTH)
        tbl.setColumnWidth(2, COL_SIZE_WIDTH)
        hdr = tbl.horizontalHeaderItem(0)
        if hdr is not None:
            # 不写 ✓：offscreen/精简字体下会渲染成方框，CJK 字反而稳
            hdr.setText("选")
            hdr.setToolTip("点这一格＝全选/全不选本列（不影响其它列已勾的）")
        return tbl

    def _kind_of(self, tbl: QTableWidget | None) -> str:
        kind = tbl.property("kind") if tbl is not None else None
        return kind if kind in self.tables else KIND_PAGES[0][0]

    def _page_kind(self) -> str:
        """兼容层：没有"当前页"了，返回最后获得焦点的那张表所属 kind。"""
        return self._kind_of(getattr(self, "_active_tbl", None))

    @property
    def table(self) -> QTableWidget:
        """兼容层（老代码/自检仍按 self.table 取"正在看的表"）。

        先给最后点过的那张；一次都没点过时给第一张有行的列，再退回第一列。
        """
        t = getattr(self, "_active_tbl", None)
        if t is not None and t in self.tables.values():
            return t
        for kind, _ in KIND_PAGES:
            if self.tables[kind].rowCount():
                return self.tables[kind]
        return self.tables[KIND_PAGES[0][0]]

    @property
    def lbl_empty(self) -> QLabel:
        return self._empty_lbl[self._page_kind()]

    @property
    def btn_empty(self) -> QPushButton:
        return self._empty_btn[self._page_kind()]

    def _on_row_double_clicked(self, idx) -> None:
        """双击直接取回那一行——不管双击落在哪一列（勾选列除外）。"""
        tbl = self.sender()
        if not isinstance(tbl, QTableWidget):
            tbl = self.table
        self._active_tbl = tbl
        if idx.column() == 0:
            return
        it = tbl.item(idx.row(), 0)
        data = it.data(Qt.UserRole) if it is not None else None
        if not data:
            return
        kind, slot = data
        if kind == cfgmod.KIND_STORE:
            QMessageBox.information(
                self, "store不用恢复",
                "它是依赖目录的副本，用「更新 store」覆盖，不走恢复。",
            )
            return
        self.open_restore(kind, slot)

    def _on_item_changed(self, item) -> None:
        """勾选框变了：把状态记进 self._checked（跨页保留），再刷新底部合计。"""
        if self._loading or item is None or item.column() != 0:
            return
        data = item.data(Qt.UserRole)
        if not data:
            return
        kind, slot = data
        bucket = self._checked.setdefault(kind, set())
        if item.checkState() == Qt.Checked:
            bucket.add(slot)
        else:
            bucket.discard(slot)
        self._update_batch_bar()

    def _on_header_clicked(self, tbl: QTableWidget, section: int) -> None:
        if section != 0:
            return
        kind = tbl.property("kind")
        rows = self._page_rows.get(kind) or []
        bucket = self._checked.setdefault(kind, set())
        all_on = bool(rows) and all(s.get("slot") in bucket for s in rows)
        bucket.clear() if all_on else bucket.update(
            s.get("slot") for s in rows if s.get("slot")
        )
        self._loading = True
        try:
            target = Qt.Unchecked if all_on else Qt.Checked
            for i in range(tbl.rowCount()):
                it = tbl.item(i, 0)
                if it is not None:
                    it.setCheckState(target)
        finally:
            self._loading = False
        self._update_batch_bar()

    def _update_batch_bar(self) -> None:
        n = sum(len(s) for s in self._checked.values())
        total = 0
        for kind, slots in self._checked.items():
            for slot in slots:
                row = self._slot_index.get((kind, slot))
                if row:
                    total += int(row.get("bytes") or 0)
        if n:
            self.lbl_sel.setText(f"已选 {n} 项 · 合计 {human_bytes(total)}")
        else:
            self.lbl_sel.setText("已选 0 项")
        warn = self._same_kind_warning()
        self.lbl_sel_warn.setText(warn)
        self.lbl_sel_warn.setVisible(bool(warn))
        busy = self.job is not None and self.job.isRunning()
        self.btn_batch.setEnabled(n > 0 and not busy)

    def _same_kind_warning(self) -> str:
        """同类勾了 ≥2 份时的红字提示；只勾 1 份或全跨类返回空串。

        为什么是"告知"而不是"禁止"：同类多份的结果是确定的（后解压的覆盖先解压的
        = 只剩 createdAt 最新的那份），不是数据损坏；硬禁只会让"勾了第二份没反应"变静默。
        """
        groups: list[tuple[str, int, int, str]] = []
        for kind, label in KIND_PAGES:
            slots = self._checked.get(kind) or ()
            if len(slots) < 2:
                continue
            newest_ms = -1
            newest_txt = ""
            for slot in slots:
                row = self._slot_index.get((kind, slot)) or {}
                ms = int(row.get("createdAt") or 0)
                if ms >= newest_ms:
                    newest_ms = ms
                    newest_txt = human_ts(ms) if ms else str(slot)
            groups.append((label, len(slots), newest_ms, newest_txt))
        if not groups:
            return ""
        if len(groups) == 1:
            _, cnt, _, txt = groups[0]
            return f"· 同类勾了 {cnt} 份，只会保留最新那份（{txt}）的内容"
        names = "、".join(g[0] for g in groups)
        _, _, ms, txt = max(groups, key=lambda g: g[2])
        return f"· {names} 各类都勾了多份，每类只会保留最新那份（最新的是 {txt}）的内容"

    def _batch_selections(self) -> list[tuple[str, str]]:
        """勾选的 (kind, slot)，按页面顺序排（真正的解压顺序由 createdAt 决定）。"""
        out: list[tuple[str, str]] = []
        for kind, _ in KIND_PAGES:
            for slot in sorted(self._checked.get(kind) or ()):
                out.append((kind, slot))
        return out

    # ---------- 拼装恢复 ----------

    def _on_batch_restore(self) -> None:
        sel = self._batch_selections()
        if not sel:
            QMessageBox.information(self, "还没勾", "在任意一页里勾选几行，再来拼装。")
            return
        if self._busy_guard():
            return

        tmp_root = self.root / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        parent = QFileDialog.getExistingDirectory(
            self, "选一个父目录（会在里面新建「拼装-<时间戳>」）", str(tmp_root)
        )
        if not parent:
            return
        dest = Path(parent) / f"拼装-{datetime.now():%Y%m%d-%H%M%S}"

        with_plan = restore_batch.plan(self.cfg, self.root, sel)
        if not with_plan.get("ok"):
            QMessageBox.warning(self, "这份拼不出来", str(with_plan.get("reason")))
            return

        # 空间：一次算总账，不够就当场停手（别解到一半才发现盘满）
        space = restore_batch.check_space(dest, with_plan["totalBytes"])
        if not space.get("ok"):
            QMessageBox.warning(
                self, "空间不够",
                f"{space.get('reason')}\n\n需要 {human_bytes(with_plan['totalBytes'])}，"
                f"那个盘只剩 {human_bytes(space.get('freeBytes', 0))}。\n"
                "换个盘，或者少勾几份。",
            )
            return

        lines = [
            f"要解 {len(with_plan['order'])} 份，合计 {human_bytes(with_plan['totalBytes'])}"
            f" / {with_plan['totalFiles']:,} 个文件。",
            f"目标：{dest}",
            "顺序：按时间从旧到新，后解的包会覆盖先解的。",
        ]
        if with_plan["conflictCount"]:
            head = with_plan["conflicts"][:10]
            lines.append("")
            lines.append(
                f"其中 {with_plan['conflictCount']:,} 个文件会被后面的包覆盖："
            )
            for c in head:
                lines.append("  · " + c["path"])
            if with_plan["conflictCount"] > len(head):
                lines.append(f"  …共 {with_plan['conflictCount']:,} 个")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("确认拼装")
        box.setText("要把勾选的这些解到同一个目录吗？")
        box.setInformativeText("\n".join(lines))
        btn_go = box.addButton("开始解压", QMessageBox.AcceptRole)
        btn_no = box.addButton("先不解", QMessageBox.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        if box.clickedButton() is not btn_go:
            return
        self._start_batch(with_plan, dest)

    def _start_batch(self, plan_result: dict, dest: Path) -> None:
        n = len(plan_result["order"])
        self._batch_prog = {"i": 0, "n": n, "pct": 0, "kind": "", "slot": ""}
        self._batch_cancel = threading.Event()
        self._batch_dlg = QProgressDialog(
            f"准备解 {n} 份…", "取消", 0, n * 100, self
        )
        self._batch_dlg.setWindowTitle("拼装恢复")
        self._batch_dlg.setWindowModality(Qt.WindowModal)
        self._batch_dlg.setMinimumDuration(0)
        self._batch_dlg.setAutoClose(False)
        self._batch_dlg.setAutoReset(False)
        self._batch_dlg.canceled.connect(self._batch_cancel.set)
        self._batch_pump_timer = QTimer(self)
        self._batch_pump_timer.timeout.connect(self._batch_pump)
        self._batch_pump_timer.start(300)
        self._batch_dlg.show()

        self._begin_action(f"拼装 {n} 份")
        self._set_status(f"拼装恢复 → {dest}")
        self._set_busy_ui(True)
        self.job = Job(
            restore_batch.run, self.cfg, self.root, plan_result, dest,
            progress=self._batch_tick_prog,
            cancel=self._batch_cancel,
            logger=self.logger,
        )
        self.job.finished_ok.connect(self._on_batch_done)
        self.job.failed.connect(self._on_job_fail)
        self.job.start()

    def _batch_tick_prog(self, d: dict) -> None:
        """从解压线程回调进来：只写字典，UI 由 _batch_pump 在主线程读。

        跨线程碰控件是 Qt 的禁忌，但这里连信号都不需要——进度是"最新值覆盖"型数据，
        丢几帧没人看得出来，省一个 Signal 定义。
        """
        self._batch_prog.update(d)

    def _batch_pump(self) -> None:
        dlg = getattr(self, "_batch_dlg", None)
        if dlg is None:
            return
        p = self._batch_prog
        i, n = int(p.get("i") or 0), int(p.get("n") or 0)
        pct = int(p.get("pct") or 0)
        if i and n:
            dlg.setValue(min(n * 100, (i - 1) * 100 + pct))
            label = KIND_LABEL.get(p.get("kind") or "", p.get("kind") or "")
            dlg.setLabelText(f"第 {i}/{n} 份 · {label} · 解压中 {pct}%")
        if self._batch_cancel.is_set():
            dlg.setLabelText(f"第 {i}/{n} 份解完就停…")

    def _on_batch_done(self, res: dict) -> None:
        if getattr(self, "_batch_pump_timer", None) is not None:
            self._batch_pump_timer.stop()
            self._batch_pump_timer = None
        if getattr(self, "_batch_dlg", None) is not None:
            self._batch_dlg.close()
            self._batch_dlg = None
        self._set_busy_ui(False)
        ok = bool(res.get("ok"))
        done = len(res.get("restored") or [])
        want = int(res.get("wantCount") or 0)
        self._begin_action("拼装恢复")
        if ok:
            self._checked = {k: set() for k, _ in KIND_PAGES}
            self.reload_table()
            msg = (f"成功 {done} 份 · 结果 {res.get('totalFiles', 0):,} 个文件 / "
                   f"{human_bytes(res.get('totalBytes', 0))} · 耗时 {res.get('seconds', 0)} s")
            self.lbl_phase.setText(msg)
            self.lbl_phase.setStyleSheet(f"color:{SUCCESS_GREEN};font-weight:600")
            self._set_badge("ok", "✔ 已拼装")
            self._set_status(msg)
        else:
            msg = f"成品了 {done}/{want} 份就停了"
            if res.get("cancelled"):
                msg = f"你取消了：已解 {done}/{want} 份，前面的文件留着"
            self.lbl_phase.setText(msg)
            self.lbl_phase.setStyleSheet(f"color:{DANGER_RED};font-weight:600")
            self._set_badge("fail", "✖ 拼装中断")
            self._set_status(msg)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setWindowTitle("拼装完成" if ok else "拼装没做全")
        box.setText(msg)
        detail = [f"目标目录：{res.get('dest', '')}"]
        if res.get("errors"):
            detail.append("")
            detail.extend(str(e) for e in res["errors"][:5])
        box.setInformativeText("\n".join(detail))
        btn_open = box.addButton("打开目标目录", QMessageBox.ActionRole)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is btn_open:
            try:
                os.startfile(str(res.get("dest") or ""))
            except OSError as exc:
                QMessageBox.warning(self, "打不开", str(exc))

    # ---------- 状态 ----------

    def _begin_action(self, label: str) -> None:
        """UX-03：动作一变就把进度区整体重置——不留上一次的百分比与结论。

        走查里删完一份 524 MB 基线，进度行还挂着「50% baseline 完成: 2 个包 / 524.3 MB」，
        用户以为没删掉，又点了一次删除。
        """
        self._action = label
        self._finish_until = 0.0
        self.bar.setValue(0)
        self.bar.setStyleSheet("")
        self.bar.setTextVisible(True)
        self.lbl_phase.setStyleSheet("")
        self.lbl_phase.setText(f"{label}…")
        self.lbl_phase.setToolTip("")

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def _apply_cover_elide(self) -> None:
        """首行只在不真的放不下时才省略（窗口 ≥1000px 必须完整）。

        QLabel 不会自己省略：原来靠布局挤压，结果是「会话记…」——一句话说到一半没了，
        用户看不出到底是几分钟还是几天。

        坑：不能用「标签当前宽度」判断放不放得下——省略会让 sizeHint 变小、标签再变窄，
        于是越省略越窄、越窄越省略（实测 1000→1180px 反而从 384 缩到 368）。
        这里用**父行预算**（行宽 − 端口宽度 − 边距）判定，并用 minimumWidth 把整句宽度钉住，
        宽度就不会反过来影响判定。放得下就整句显示并清 tooltip；放不下才 ElideRight + 整句进 tooltip。
        """
        full = getattr(self, "_cover_full", "")
        if not full:
            return
        fm = self.lbl_cover.fontMetrics()
        need = fm.horizontalAdvance(full)
        parent = self.lbl_cover.parentWidget()
        row_w = parent.width() if parent is not None else 0
        if row_w <= 0:
            row_w = self.lbl_cover.width() + self.lbl_ports.width() + 120
        budget = row_w - self.lbl_ports.sizeHint().width() - 40
        if budget <= 0:
            budget = self.lbl_cover.width()
        if budget >= need + 8:
            self.lbl_cover.setMinimumWidth(need + 8)  # 钉住整句，别被布局再压
            if self.lbl_cover.text() != full:
                self.lbl_cover.setText(full)
            self.lbl_cover.setToolTip("")
            return
        self.lbl_cover.setMinimumWidth(0)
        avail = max(budget - 8, 40)
        self.lbl_cover.setText(fm.elidedText(full, Qt.ElideRight, avail))
        self.lbl_cover.setToolTip(full)

    def resizeEvent(self, event) -> None:  # noqa: N802（Qt 命名）
        super().resizeEvent(event)
        self._apply_cover_elide()

    def refresh_status(self) -> None:
        # UX-01：能力段 = 实际数据（最老一份包能退到多久，没有就直说）；策略段单独小字
        parts = []
        for kind in (cfgmod.KIND_BASELINE, cfgmod.KIND_SESSIONS):
            parts.append(f"{KIND_LABEL.get(kind, kind)} {cover_of(self.root, kind)}")
        self._cover_full = "可回退　" + "　·　".join(parts)
        self._apply_cover_elide()

        pol = []
        for kind in (cfgmod.KIND_SESSIONS, cfgmod.KIND_BASELINE):
            m = int(self.cfg.get("kinds", {}).get(kind, {}).get("M", 0) or 0)
            if m:
                pol.append(f"{KIND_LABEL.get(kind, kind)} {m / 1440:.0f} 天")
        self.lbl_policy.setText("保留策略：" + " / ".join(pol) if pol else "")

        # 用户 2026-10-26：这三个「在跑 / 未开」指示灯不要了——它们只回答
        # "端口有没有人 listen"，却霸占首行最显眼的位置，还容易让人把「未开」读成故障。
        # DSH 的启停改成顶部三个显式按钮（启动 / 重启 / 关闭），状态用按钮可用性表达。
        self.lbl_ports.setText("")
        self.lbl_ports.setVisible(False)

        st = manifest.mirror_slot(self.root)
        if st:
            self.lbl_store.setText(
                f"store 已更新 · {human_bytes(st['bytes'])} · {st['files']:,} 文件"
                f" · {human_age(st['createdAt'])}"
            )
            self.lbl_store.setToolTip("")
        else:
            self.lbl_store.setText(
                f'<span style="color:{DANGER_RED}">store 还没更新过</span>'
            )
            # UX-10：只说「还没更新过」等于没说后果——store缺了，恢复之后要重装依赖
            self.lbl_store.setToolTip(
                "store是一份固定的依赖目录副本，用来在恢复后省掉重新下载。\n"
                "没更新过不影响备份和恢复本身；但恢复之后可能要重新装依赖"
                "（pnpm install 之类），离线或网络差时会很慢。"
            )

        base = manifest.newest(self.root, cfgmod.KIND_BASELINE)
        sess = manifest.newest(self.root, cfgmod.KIND_SESSIONS)
        self.lbl_last.setText(
            f"上次基线 {human_age(base['createdAt']) if base else '还没有'}"
            f"　上次会话 {human_age(sess['createdAt']) if sess else '还没有'}"
        )
        self._update_disk_row()

    def _tick(self) -> None:
        s = self.progress.snapshot()
        busy = bool(s["busy"])
        # U-04：每拍按进度黑板重算一次，跑完/失败/异常退出都一定回到可点状态
        self._set_busy_ui(busy)
        self._tick_n += 1
        self._tick_undo()
        if busy:
            self.lbl_phase.setStyleSheet("")
            self.bar.setValue(int(s["pct"]))
            self.bar.setTextVisible(True)
            used = ""
            started = int(s.get("startedAt") or 0)
            if started:
                sec = max(0, int(datetime.now().timestamp() - started / 1000))
                used = f" · 已用 {sec // 60:02d}:{sec % 60:02d}"
            # UX-11：原来把「harness: 0% 0% 547 + 半截路径」整串塞进一行——三个百分比、
            # 一个孤儿数字、一条被截断的路径。现在只留「一句人话阶段 + 已用」，
            # 原始细节（哪个子任务、多少文件、当前文件）整体挪进 tooltip，需要时悬停看。
            text = f"{s['phase']}{used}"
            self.lbl_phase.setText(text)
            self.lbl_phase.setToolTip(
                f"{text}\n{s['detail']}" if s["detail"] else text
            )
            self.statusBar().setToolTip(str(s["detail"]))
            self._set_badge("busy")
        else:
            if s["message"]:
                self.lbl_phase.setText(s["message"])
                self.lbl_phase.setToolTip(s["message"])
            # U-11：跑完的那一句只说「完成」，不说是成功还是失败——用户看不出结果
            self._update_finish_tone(s)
            if self.bar.value() >= 100:
                # 成功后不立刻回 0%（看着像失败了）：保持满格 8 秒再归空闲
                self.bar.setTextVisible(False)   # UX-07：完成态不再显示百分数
                if not self._finish_until:
                    self._finish_until = time.monotonic() + 8.0
                if time.monotonic() >= self._finish_until:
                    self.bar.setValue(0)
                    self.bar.setStyleSheet("")
                    self._finish_until = 0.0
                    self.lbl_phase.setText("空闲")
                    self.lbl_phase.setToolTip("")
                    self.lbl_phase.setStyleSheet("")
        # 1.2 秒一拍会跨过整 15 秒那一拍，用计数节拍才不会漏（原写法最长 30 秒不刷新）
        if not busy and self._tick_n % 12 == 0:
            self.refresh_status()

    def _tick_undo(self) -> None:
        """UX-04：撤销按钮上的秒数每秒减一，到 0 就撤掉入口（回收区里的包不动）。"""
        if not self._undo or self._undo_left <= 0:
            return
        self._undo_left -= 1
        if self._undo_left <= 0:
            self.btn_undo.setVisible(False)
            self._set_status(
                f"已删除，可以在「{cfgmod.DELETED_DIR}」里找回"
                f"（{engine.recycle_keep_days(self.cfg)} 天后自动清理）")
            return
        self.btn_undo.setText(f"撤销删除（{self._undo_left}）")

    def _set_badge(self, state: str, text: str = "") -> None:
        """UX-20：底栏左端的状态徽标——成功/失败/进行中靠符号 + 颜色区分。"""
        cfg = {
            "idle": ("", ""),
            "busy": ("… 进行中", "#1668c1"),
            "ok": ("✔ 完成", SUCCESS_GREEN),
            "fail": ("✖ 失败", DANGER_RED),
        }
        sym, color = cfg.get(state, ("", ""))
        if not sym:
            self.lbl_badge.setText("")
            return
        label = text or sym
        self.lbl_badge.setText(f'<span style="color:{color}">{label}</span>')
        if state == "fail":
            self.lbl_badge.setToolTip("点「打开日志文件夹」看原因，或按提示重试")

    def _update_finish_tone(self, s: dict) -> None:
        """U-11：结束时只写「完成」看不出成败 → 符号 + 文字 + 颜色三重表达。"""
        phase = s.get("phase") or ""
        if phase not in ("完成", "失败"):
            self.lbl_phase.setStyleSheet("")
            return
        ok = phase == "完成"
        text = (s.get("message") or phase).replace(" 完成：", " 成功：")
        self.lbl_phase.setText(("✓ " if ok else "✗ ") + text)
        self.lbl_phase.setToolTip(text)
        self.lbl_phase.setStyleSheet(
            f"color:{SUCCESS_GREEN};font-weight:600" if ok
            else f"color:{DANGER_RED};font-weight:600"
        )
        self._set_badge("ok" if ok else "fail")
        if ok:
            # 成功：条子钉在 100 并转绿，8 秒后才归零（见 _tick）
            self.bar.setValue(100)
            self.bar.setStyleSheet(
                "QProgressBar::chunk{background:%s;border-radius:3px}"
                % SUCCESS_GREEN
            )

    # ---------- 列表 ----------

    # 表格顺序：同类里按时间倒序，种类之间按「地基 → 日常 → 附属」排
    # UX-08：17 行里 15 行是 15 分钟的会话包，2 份基线被淹在中间，用户找不到地基
    KIND_ORDER = {
        cfgmod.KIND_BASELINE: 0,
        cfgmod.KIND_SESSIONS: 1,
        cfgmod.KIND_WORKSPACE: 2,
        cfgmod.KIND_STORE: 3,
    }

    def collect_rows(self) -> list[dict]:
        """四类全取（类型不再是筛选条件——页签就是类型）。"""
        rows: list[dict] = []
        for kind in (cfgmod.KIND_BASELINE, cfgmod.KIND_SESSIONS,
                     cfgmod.KIND_WORKSPACE, cfgmod.KIND_STORE):
            if kind == cfgmod.KIND_STORE:
                s = manifest.mirror_slot(self.root)
                if s:
                    rows.append(s)
                continue
            rows.extend(manifest.scan(self.root, kind))
        rows.sort(
            key=lambda r: (self.KIND_ORDER.get(r.get("kind"), 9),
                           -int(r.get("createdAt") or 0))
        )
        return rows

    def _on_range_changed(self, _idx: int) -> None:
        """时间下拉变了：选「自定义…」就先弹日期区间，取消则退回「全部时间」。"""
        if int(self.cmb_range.currentData() or 0) == -2:
            if not self._pick_custom_range():
                self.cmb_range.blockSignals(True)
                self.cmb_range.setCurrentIndex(0)
                self.cmb_range.blockSignals(False)
                self._custom_range = None
        self.reload_table()

    def _pick_custom_range(self) -> bool:
        """弹一个起止日期。返回 True 表示用户确认。"""
        today = datetime.now().date()
        dlg = QDialog(self)
        dlg.setWindowTitle("自定义时间范围")
        form = QFormLayout(dlg)
        d_from = QDateEdit(QDate(today.year, today.month, today.day).addDays(-7))
        d_to = QDateEdit(QDate(today.year, today.month, today.day))
        for d in (d_from, d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        form.addRow("从", d_from)
        form.addRow("到", d_to)
        note = QLabel("包含两端当天整天。备份时间落在区间内才显示。")
        note.setStyleSheet("color:#666666;font-size:12px")
        form.addRow(note)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("就这个范围")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if dlg.exec() != QDialog.Accepted:
            return False
        a, b = d_from.date().toPython(), d_to.date().toPython()
        if a > b:
            a, b = b, a
        self._custom_range = (
            datetime(a.year, a.month, a.day),
            datetime(b.year, b.month, b.day, 23, 59, 59),
        )
        return True

    def _in_range(self, created_ms: int | None) -> bool:
        """硬伤 2 + R3-2：时间窗筛选。

        0=全部、1=今天、-3=昨天、N=最近 N 天（含今天）、-2=自定义区间。
        「今天/昨天/最近 N 天」按**日历日**算，不按「过去 24 小时」——
        走查要的是「昨天那份基线」，滚动窗口会把前天 23:50 的包算成昨天。
        """
        days = int(self.cmb_range.currentData() or 0)
        if days == 0:
            return True
        if not created_ms:
            return False
        ts = int(created_ms) / 1000.0
        if days == -2:
            if not self._custom_range:
                return True
            lo, hi = self._custom_range
            return lo.timestamp() <= ts <= hi.timestamp()
        t_date = datetime.fromtimestamp(ts).date()
        today = datetime.now().date()
        if days == 1:
            return t_date == today
        if days == -3:
            return (today - t_date).days == 1
        return 0 <= (today - t_date).days <= days - 1

    def reload_table(self) -> None:
        """四页各填各的：类型＝页签，时间窗仍是全页共用的筛选。"""
        all_rows = self.collect_rows()
        self._page_rows = {k: [] for k, _ in KIND_PAGES}
        self._slot_index = {}
        self._kind_total = {k: 0 for k, _ in KIND_PAGES}
        for r in all_rows:
            self._kind_total[r["kind"]] = self._kind_total.get(r["kind"], 0) + 1
            if self._in_range(r.get("createdAt")):
                self._page_rows.setdefault(r["kind"], []).append(r)
        self._total_now = len(all_rows)

        self._loading = True
        try:
            for kind, _ in KIND_PAGES:
                self._fill_page(kind, self._page_rows.get(kind) or [])
        finally:
            self._loading = False
        self._refresh_count_label()
        self._update_batch_bar()
        self._on_selection_changed()
        # 时间轴吃全量（不受时间窗筛选影响）——它的作用就是让你看见窗外还有东西
        self.timeline.setData(all_rows)

    def _fill_page(self, kind: str, rows: list[dict]) -> None:
        """填一列：3 列（勾选/时间/大小），文件数与状态进 tooltip。"""
        tbl = self.tables[kind]
        keep_sel = self._selected_slot()
        checked = self._checked.get(kind) or set()
        tbl.setUpdatesEnabled(False)
        try:
            tbl.setRowCount(0)
            for r in rows:
                i = tbl.rowCount()
                tbl.insertRow(i)
                it_chk = QTableWidgetItem("")
                it_chk.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
                )
                it_chk.setCheckState(
                    Qt.Checked if r["slot"] in checked else Qt.Unchecked
                )
                it_chk.setData(Qt.UserRole, (r["kind"], r["slot"]))
                tbl.setItem(i, 0, it_chk)
                self._slot_index[(r["kind"], r["slot"])] = r

                # 令文：列里只写紧凑时间（今天 `05:35`），完整到秒的时间进 tooltip
                ts = r.get("createdAtIso") or datetime.fromtimestamp(
                    r.get("createdAt", 0) / 1000
                ).strftime("%Y-%m-%d %H:%M:%S")
                ts_full = str(ts)[:19].replace("T", " ")
                it_time = QTableWidgetItem(compact_ts(r.get("createdAt")))
                tbl.setItem(i, 1, it_time)

                it_size = QTableWidgetItem(human_bytes(r.get("bytes", 0)))
                it_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                tbl.setItem(i, 2, it_size)

                # U-06 / 状态：给人看的词，「包缺失」是内部说法。
                # 令文要求文件数与状态不进表体，但丢失/校验失败必须还看得见——
                # 所以坏行整行标红 + tooltip 第一行就说清，别让它藏在悬停里。
                files = f"{r.get('files', 0):,} 个文件"
                if not r.get("onDisk"):
                    status = "文件已丢失"
                    tip = (
                        f"{files} · {status} · 完整时间 {ts_full}\n"
                        "清单里有这份包，但磁盘上找不到包文件。\n"
                        "可能被手动删掉、被清理软件带走，或备份时写入失败。\n"
                        "它无法恢复，可以删掉这条记录。"
                    )
                    bad = True
                elif not r.get("verifiedOk"):
                    status = "校验失败"
                    tip = (
                        f"{files} · {status} · 完整时间 {ts_full}\n"
                        "包文件在，但大小/校验与清单不符，可能不完整。\n"
                        "建议重新备份该类型，不要用它恢复。"
                    )
                    bad = True
                else:
                    status = "可用"
                    tip = f"{files} · {status} · 完整时间 {ts_full}"
                    bad = False
                for col in (1, 2):
                    cell = tbl.item(i, col)
                    if cell is not None:
                        cell.setToolTip(tip)
                        if bad:
                            cell.setForeground(QColor(DANGER_RED))

                # UX-08：基线是地基，整行加粗，一眼能从一串会话包里挑出来
                if r.get("kind") == cfgmod.KIND_BASELINE:
                    for col in range(3):
                        cell = tbl.item(i, col)
                        if cell is not None:
                            f = cell.font()
                            f.setBold(True)
                            cell.setFont(f)
        finally:
            tbl.setUpdatesEnabled(True)
        # U-12：列宽在 __init__ 里定死，这里不再 resizeColumnsToContents
        if keep_sel and keep_sel[0] == kind:
            for i in range(tbl.rowCount()):
                cell = tbl.item(i, 0)
                if cell is not None and cell.data(Qt.UserRole) == keep_sel:
                    tbl.selectRow(i)
                    break
        self._sync_empty_state(kind, len(rows))

    def _refresh_count_label(self) -> None:
        """列标题带计数（`store 3` / `会话 筛出 2 / 共 15`）。

        四列并排之后没有"当前页"了，原来那一个 lbl_count 只留一句总计，
        每列的份数写到各自的标题行上。
        """
        for kind, label in KIND_PAGES:
            shown = len(self._page_rows.get(kind) or [])
            total = (self._kind_total or {}).get(kind, shown)
            head = self._col_head[kind]
            if shown == total:
                head.setText(f"{label} {total}")
                head.setToolTip(f"{label}：共 {total} 份")
            else:
                head.setText(f"{label} {shown}/{total}")
                head.setToolTip(
                    f"{label}：筛出 {shown} 份，共 {total} 份"
                    "（当前有时间筛选：改上面的「时间」可放宽）"
                )
        shown_all = sum(len(v or []) for v in self._page_rows.values())
        total_all = sum((self._kind_total or {}).values())
        if shown_all == total_all:
            self.lbl_count.setText(f"共 {total_all} 份")
            self.lbl_count.setToolTip("四类合计")
        else:
            self.lbl_count.setText(f"筛出 {shown_all} / 共 {total_all} 份")
            self.lbl_count.setToolTip("当前有时间筛选：改上面的「时间」可放宽")

    def _sync_empty_state(self, kind: str, count: int) -> None:
        """UX-06 + R3-2：0 份时表格一片白 → 空区居中一句引导。

        空态要分清两件事：这一类真的一份没有 / 被时间窗筛掉了。
        后者必须说清「是筛选造成的」并给出口——走查里她筛到空白就以为备份丢了。
        """
        lbl, btn = self._empty_lbl[kind], self._empty_btn[kind]
        empty = count == 0
        has_filter = bool(self.cmb_range.currentData())
        lbl.setVisible(empty)
        btn.setVisible(empty)
        if not empty:
            return
        if has_filter:
            btn.setText("显示全部")
            lbl.setText(self._empty_hint(kind))
        else:
            label = KIND_LABEL.get(kind, kind)
            if kind == cfgmod.KIND_STORE:
                btn.setText("更新 store")
                lbl.setText(
                    "还没有store<br>"
                    '<span style="color:#888888;font-size:13px">'
                    "点「更新store」做一份，恢复之后就不用重新装依赖"
                    "</span>"
                )
            else:
                btn.setText(f"立即备份{label}")
                tip = ("先做一份基线当垫底，之后每 15 分钟会自动记录会话"
                       if kind == cfgmod.KIND_BASELINE
                       else "这一类还没有备份，点上面「备份会话/备份工作区」就会有一份")
                lbl.setText(
                    f"还没有{label}<br>"
                    f'<span style="color:#888888;font-size:13px">{tip}</span>'
                )
        self._place_empty()

    def _empty_hint(self, kind: str) -> str:
        """R3-2 空结果必须说话：谁没有、最近一份是什么时候、比它早多少。

        spec 原句：`昨天没有基线。最近一份是 10-25 20:14（前天）。[显示全部]`
        """
        what = KIND_LABEL.get(kind, "备份")
        span = self._range_label()
        rows = [r for r in self.collect_rows() if r.get("kind") == kind]
        if not rows:
            return (
                f"{span}没有{what}，这一类整个库里也没有——"
                '<span style="color:#888888;font-size:13px">先备份一次再看</span>'
            )
        newest = max(rows, key=lambda r: int(r.get("createdAt") or 0))
        ts = human_ts(newest.get("createdAt"))
        word = day_word(newest.get("createdAt"))
        # spec 原句是「（前天）」这种相对日词；只有今天那份才退回「N 小时前」
        rel = (human_span(max(0.0, time.time() * 1000
                              - int(newest.get("createdAt") or 0))) + "前"
               if word == "今天" else word)
        return (
            f"{span}没有{what}。最近一份是 "
            f'<b>{ts}</b>（{rel}）<br>'
            '<span style="color:#888888;font-size:13px">'
            "下面这个按钮会把时间筛选清空</span>"
        )

    def _range_label(self) -> str:
        label = self.cmb_range.currentText() if self.cmb_range.currentData() else "全部时间"
        return "" if label in ("全部时间", "自定义…") else label

    def _on_empty_action(self, kind: str | None = None) -> None:
        """空态那个按钮：有时间筛选时复位，没筛选时才去备份那一种。"""
        if bool(self.cmb_range.currentData()) or bool(self.cmb_kind.currentData()):
            self._clear_filters()
            return
        kind = kind or self._page_kind()
        if kind == cfgmod.KIND_STORE:
            self.run_store()
        else:
            self.run_kind(kind)

    def _clear_filters(self) -> None:
        """空态里的「显示全部」：两处筛选一起复位，别只清一半。"""
        self.cmb_kind.blockSignals(True)
        self.cmb_range.blockSignals(True)
        self.cmb_kind.setCurrentIndex(0)
        self.cmb_range.setCurrentIndex(0)
        self.cmb_kind.blockSignals(False)
        self.cmb_range.blockSignals(False)
        self._custom_range = None
        self.reload_table()

    def _place_empty(self) -> None:
        for kind, _ in KIND_PAGES:
            vp = self.tables[kind].viewport()
            w, h = vp.width(), vp.height()
            lbl, btn = self._empty_lbl[kind], self._empty_btn[kind]
            # 四周留 12px：窄列里换行后的长句要贴着边不会顶到边框
            pad = 12
            lbl.setGeometry(
                pad, max(0, h // 2 - 56), max(40, w - pad * 2), 96
            )
            bw, bh = 160, 34
            btn.setGeometry(max(0, (w - bw) // 2), max(0, h // 2 + 40), bw, bh)

    def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(ev)
        if getattr(self, "lbl_empty", None) is not None:
            self._place_empty()

    def closeEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        """UX-17：关窗时记住位置尺寸，下次开在同一处；两个库同时开时错位。"""
        try:
            self._qset.setValue("win/geometry", self.saveGeometry())
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(ev)

    def _restore_geometry(self) -> None:
        geo = self._qset.value("win/geometry")
        if geo:
            self.restoreGeometry(geo)
        # 与已存在的同族窗口（另一个库）位置比对，重叠就右移 40px
        mine = self.frameGeometry()
        for w in QApplication.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or not w.isVisible():
                continue
            if w.frameGeometry().topLeft() == mine.topLeft():
                self.move(mine.topLeft().x() + 40, mine.topLeft().y() + 40)
                break

    # ---------- 选中行 ----------

    def _selected_slot(self) -> tuple[str, str] | None:
        """哪一列有选中行就是它——四列并排后没有"当前页"，按焦点列优先找。"""
        order: list[QTableWidget] = []
        t = getattr(self, "_active_tbl", None)
        if t is not None:
            order.append(t)
        order += [self.tables[k] for k, _ in KIND_PAGES if self.tables[k] is not t]
        for tbl in order:
            sm = tbl.selectionModel()
            if sm is None:
                continue
            rows = sm.selectedRows()
            if not rows:
                continue
            it = tbl.item(rows[0].row(), 0)
            if it is None:
                continue
            data = it.data(Qt.UserRole)
            if data:
                return tuple(data)
        return None

    def _on_selection_changed(self, tbl=None) -> None:
        if self._loading:
            # reload / 表头全选过程中产生的选中变化不算"用户换了焦点列"
            return
        if isinstance(tbl, QTableWidget):
            self._active_tbl = tbl
            # 四列各留一个高亮会让「取回备份…」不知道指谁 → 清掉其它列的选择。
            # 用 _loading 挡递归：clearSelection 也会发 itemSelectionChanged。
            self._loading = True
            try:
                for kind, _ in KIND_PAGES:
                    other = self.tables[kind]
                    if other is not tbl and other.selectionModel() is not None:
                        other.clearSelection()
            finally:
                self._loading = False
        sel = self._selected_slot()
        editable = sel is not None and sel[0] != cfgmod.KIND_STORE
        busy = self.job is not None and self.job.isRunning()
        self.btn_restore.setEnabled(editable and not busy)
        # UX-12：备份跑着的时候「删除选中」原来是可点的（U-04 的锁漏了一个）
        self.btn_delete.setEnabled(editable and not busy)
        # UX-06：提示句跟着状态走——0 行、选中、选中不可操作，三种说法
        if self.table.rowCount() == 0:
            self.lbl_hint.setText("表里还没有备份")
        elif sel is None:
            self.lbl_hint.setText("选中一行再操作，或双击那一行直接取回")
        elif sel[0] == cfgmod.KIND_STORE:
            self.lbl_hint.setText("store只能更新，不能恢复/删除")
        else:
            self.lbl_hint.setText(
                f"已选中「{KIND_LABEL.get(sel[0], sel[0])} · {cfgmod.human_slot(sel[1])}」"
            )
        # 表格选中 → 时间轴那个点加一圈（反过来点时间轴也会走到这里）
        if sel is None:
            self.timeline.setSelected("", "")
        else:
            self.timeline.setSelected(sel[0], sel[1])

    def _on_timeline_click(self, kind: str, slot: str) -> None:
        """时间轴上点了一个圆点：在该类表格里选中那一行，四列只留这一个高亮。"""
        tbl = self.tables.get(kind)
        if tbl is None:
            return
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is None:
                continue
            if tuple(it.data(Qt.UserRole) or ()) != (kind, slot):
                continue
            # 四列并排：先清掉别人，再选自己（用 _loading 挡递归，同 _on_selection_changed）
            self._loading = True
            try:
                for k, _ in KIND_PAGES:
                    other = self.tables[k]
                    if other is not tbl and other.selectionModel() is not None:
                        other.clearSelection()
            finally:
                self._loading = False
            self._active_tbl = tbl
            tbl.selectRow(i)
            target = tbl.item(i, 1) or it
            tbl.scrollToItem(target)
            self._on_selection_changed(tbl)
            return
        # 行不在当前时间窗里（时间轴吃全量、表格只显示窗内）→ 清掉筛选让它现身
        self._set_status("那份备份在当前时间窗之外，已把筛选切回「全部时间」")
        try:
            # 「全部时间」这一项的 itemData 是 0（见 cmb_range.addItem 的取值表），不是字符串 "all"
            idx = self.cmb_range.findData(0)
            if idx >= 0:
                self.cmb_range.setCurrentIndex(idx)
        except Exception:
            pass
        self.reload_table()
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None and tuple(it.data(Qt.UserRole) or ()) == (kind, slot):
                self._active_tbl = tbl
                tbl.selectRow(i)
                tbl.scrollToItem(tbl.item(i, 1) or it)
                self._on_selection_changed(tbl)
                return

    def _on_restore_clicked(self) -> None:
        sel = self._selected_slot()
        if not sel:
            QMessageBox.information(self, "先选一份", "在表格里选一行，再点「取回备份…」。")
            return
        if sel[0] == cfgmod.KIND_STORE:
            QMessageBox.information(
                self, "store不用恢复",
                "它是依赖目录的副本，用「更新 store」覆盖，不走恢复。",
            )
            return
        self.open_restore(*sel)

    def _on_delete_clicked(self) -> None:
        sel = self._selected_slot()
        if not sel:
            QMessageBox.information(self, "先选一份", "在表格里选一行，再点删除。")
            return
        if sel[0] == cfgmod.KIND_STORE:
            # U-13：删按钮已置灰，这里本该点不到；万一走键盘/脚本触发，要说清为什么不行
            QMessageBox.information(
                self, "store不用删",
                "store 是一份固定副本，不参与按时间淘汰。\n"
                "要更新它，用「更新store」。",
            )
            return
        self.delete_slot(*sel)

    # ---------- 动作 ----------

    def _busy_guard(self) -> bool:
        if self.job is not None and self.job.isRunning():
            QMessageBox.information(self, "等一下", "已经有一个任务在跑，等它结束。")
            return True
        return False

    def _set_busy_ui(self, busy: bool) -> None:
        """U-04 忙碌态联动：禁动作按钮，只把「开这一枪」的按钮改成进行中文案。

        结束分支无条件复原（先还原文字，再按当前选中状态重算可用性），
        所以成功、失败、异常三条出口都回到同一个状态——不依赖哪一个信号先到。
        """
        for b in (self.btn_base, self.btn_sess, self.btn_ws, self.btn_store,
                  self.btn_restore, self.btn_delete, self.btn_batch,
                  *self._col_del.values()):
            b.setEnabled(not busy)
        # 清空历史：忙时不点，闲时仍要服从「只有暂停才可清空」
        self.btn_clear.setEnabled(not busy and self.chk_pause.isChecked())
        # UX-12：忙碌时「删除选中」也禁；主按钮文案保持「备份中…」不变形
        self.btn_empty.setEnabled(not busy)
        # 表也不许在解压/备份途中被误勾——勾了会算进合计，但已经开跑的那次不读它
        for _k, _ in KIND_PAGES:
            self.tables[_k].setEnabled(not busy)
        if not busy:
            self._update_batch_bar()
        if self._busy_btn is not None:
            self._busy_btn.setText(self._busy_text if busy else self._busy_btn_text)
        if not busy:
            self._busy_btn = None
            self._on_selection_changed()

    def _run_job(self, label: str, fn, *args, btn=None, busy_text: str = "备份中…",
                 **kwargs) -> None:
        if self._busy_guard():
            return
        self._begin_action(label)
        self._set_status(f"{label}…")
        self._busy_btn = btn
        self._busy_btn_text = btn.text() if btn is not None else ""
        self._busy_text = busy_text
        self._last_call = (label, fn, args, kwargs, btn, busy_text)  # U-03：给「重试」用
        self._set_busy_ui(True)
        self.job = Job(fn, *args, **kwargs)
        self.job.finished_ok.connect(self._on_job_ok)
        self.job.failed.connect(self._on_job_fail)
        self.job.start()

    def _on_job_ok(self, res: dict) -> None:
        self._set_busy_ui(False)
        self._set_status("完成")
        self._set_badge("ok")
        self.refresh_status()
        self.reload_table()
        # UX-02：恢复完成必须说清「写回真实目录了没有、解到哪个路径」。
        # 原来演练也写「恢复完成」，走查里用户以为已经写回去了。
        if res.get("ok", True) and res.get("dest") and self._is_restore:
            drill = res.get("mode") == "drill"
            dest = str(res.get("dest"))
            n = int(res.get("files", 0))
            need = int(res.get("expectFiles", 0))
            head = (
                f"演练完成，已解到 {dest}（未写回真实目录）" if drill
                else f"已写回 {dest}"
            )
            self.lbl_phase.setText(head)
            self.lbl_phase.setToolTip(f"{head}\n{n:,} / {need:,} 个文件")
            self.lbl_phase.setStyleSheet(
                f"color:{SUCCESS_GREEN};font-weight:600"
            )
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("演练完成" if drill else "已恢复到真实目录")
            box.setText(head)
            detail = f"{n:,} / {need:,} 个文件"
            if drill:
                detail += "对得上。这是演练，真实路径没有被改动；看完把临时目录删掉就干净了。"
            else:
                detail += "已写回。回滚用的旧文件留在同目录的 .pre-restore-* 里。"
            box.setInformativeText(detail)
            box.setDetailedText(dest)
            btn_open = box.addButton("打开结果目录…", QMessageBox.ActionRole)
            box.addButton("知道了", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is btn_open:
                try:
                    os.startfile(dest)  # noqa: S606
                except OSError as exc:
                    QMessageBox.warning(self, "打不开", str(exc))
            self._is_restore = False
            return
        self._is_restore = False
        if not res.get("ok", True) and res.get("diskFull"):
            # 体检 E：磁盘满不是「出错了」，是一个用户能自己解决的状态
            self._update_disk_row()
            self._set_status("磁盘不够，这次没存成")
            self.lbl_phase.setText("磁盘空间不够，这次没存成")
            self.lbl_phase.setStyleSheet(f"color:{DANGER_RED};font-weight:600")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("磁盘不够了")
            box.setText("磁盘不够了，这次没存成。")
            box.setInformativeText(
                "下一步：清一下这个盘（回收区、旧备份、别的临时文件），"
                "或者到设置页把「备份根目录」改到空间更大的盘再重试。\n"
                "已经存在的旧备份没有被破坏。"
            )
            box.setDetailedText(str(res.get("reason") or res)[:4000])
            btn_log = box.addButton("打开日志…", QMessageBox.ActionRole)
            btn_retry = box.addButton("重试", QMessageBox.ActionRole)
            box.addButton("知道了", QMessageBox.AcceptRole)
            box.exec()
            hit = box.clickedButton()
            if hit is btn_log:
                self.open_log_dir()
            elif hit is btn_retry:
                self._retry_last()
            return
        if not res.get("ok", True):
            # 部分成功：一句话结论 + 下一步 + 可展开的原文（U-03③）
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("写进去了，但没通过自检")
            box.setText("包写进去了，但自检没过 —— 现在这份不建议用来恢复。")
            box.setInformativeText("先看一眼日志找原因；下一次备份如果通过自检，就可以正常用了。")
            box.setDetailedText(str(res.get("reason") or res)[:4000])
            btn_log = box.addButton("打开日志…", QMessageBox.ActionRole)
            box.addButton("知道了", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is btn_log:
                self.open_log_dir()

    def _retry_last(self) -> None:
        if not self._last_call:
            return
        label, fn, args, kwargs, btn, busy_text = self._last_call
        self._run_job(label, fn, *args, btn=btn, busy_text=busy_text, **kwargs)

    def _on_job_fail(self, msg: str) -> None:
        # U-03：失败不再是「出错了 + 异常原文」，改成结论 + 下一步 + 可展开详情 + 日志/重试入口
        # 拼装恢复走的是同一个 Job，失败时先把那个模态进度条收掉，否则错误框会被压在下面
        if getattr(self, "_batch_pump_timer", None) is not None:
            self._batch_pump_timer.stop()
            self._batch_pump_timer = None
        if getattr(self, "_batch_dlg", None) is not None:
            self._batch_dlg.close()
            self._batch_dlg = None
        self._set_busy_ui(False)
        self._set_badge("fail")
        self._is_restore = False
        if sevenzip.is_disk_full(msg):
            # 体检 E：异常路径也可能是磁盘满（例如校验阶段写临时文件失败）
            self._update_disk_row()
            self._set_status("磁盘不够，这次没存成")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("磁盘不够了")
            box.setText("磁盘不够了，这次没存成。")
            box.setInformativeText(
                "下一步：清一下这个盘，或到设置页把「备份根目录」改到空间更大的盘再重试。"
            )
            box.setDetailedText(msg[:4000])
            box.addButton("知道了", QMessageBox.AcceptRole)
            box.exec()
            return
        self._set_status("失败 · 详情见日志")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("这次没成功")
        box.setText("备份/恢复中途停了，真实目录没有被改动。")
        box.setInformativeText("先看一眼日志找原因，修好后可以直接重试这一次。")
        box.setDetailedText(msg[:4000])
        btn_log = box.addButton("打开日志…", QMessageBox.ActionRole)
        btn_retry = box.addButton("重试", QMessageBox.ActionRole)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()
        hit = box.clickedButton()
        if hit is btn_log:
            self.open_log_dir()
        elif hit is btn_retry:
            self._retry_last()

    def run_kind(self, kind: str) -> None:
        btn = {
            cfgmod.KIND_BASELINE: self.btn_base,
            cfgmod.KIND_SESSIONS: self.btn_sess,
            cfgmod.KIND_WORKSPACE: self.btn_ws,
        }.get(kind)
        self._run_job(
            f"备份 {KIND_LABEL.get(kind, kind)}",
            engine.backup_kind,
            self.cfg, self.root, kind,
            btn=btn,
            note="手动",
            progress=self.progress,
            logger=self.logger,
        )

    def run_store(self) -> None:
        self._run_job(
            "更新 store",
            engine.mirror_store,
            self.cfg, self.root,
            btn=self.btn_store,
            busy_text="更新中…",
            progress=self.progress,
            logger=self.logger,
        )

    def open_restore(self, kind: str, slot: str) -> None:
        if self._busy_guard():
            return
        dlg = RestoreDialog(self.cfg, self.root, kind, slot, self)
        dlg.focus_primary()
        if dlg.exec() != RestoreDialog.Accepted or not dlg.result_payload:
            return
        p = dlg.result_payload
        self._is_restore = True
        # 目标目录填了 → into（铺到那里）；留空 → into=None（回原位置覆盖）。
        # dry_run 必须显式关掉：它是「只预览不落盘」的开关，默认是 True。
        self._run_job(
            "恢复",
            engine.restore_slot,
            self.cfg, self.root, kind, slot,
            into=p.get("into"),
            auto_finish=p.get("auto_finish", True),
            dry_run=False,
            btn=self.btn_restore,
            busy_text="恢复中…",
            progress=self.progress,
            logger=self.logger,
        )

    # ---------- DSH 网关控制（用户 2026-10-26）----------
    #
    # 三条路都指向**已有的脚本 / 计划任务**，界面只是调用方，不是替代品：
    #   启动 → paths.dsh_cmd（默认 ~\.dsh\bin\DSH.cmd，等同双击桌面快捷方式）
    #   重启 → commands.restart_cmd（默认 Start-ScheduledTask -TaskName 'DSH Restart Now'）
    #   关闭 → 按 ports.dsh 找 pid，taskkill /T /F
    # 这些脚本与计划任务全部保留——这个界面打不开也能手动启。

    def _dsh_port(self) -> int:
        return int(self.cfg.get("ports", {}).get("dsh", 3080) or 3080)

    def _port_pids(self, port: int) -> list[int]:
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

    def dsh_start(self) -> None:
        port = self._dsh_port()
        if port_alive(port):
            QMessageBox.information(
                self, "DSH 已经在跑",
                f"{port} 端口有人听着，不用再起一次。\n要重启请用「重启 DSH」。")
            return
        script = Path(str((self.cfg.get("paths") or {}).get("dsh_cmd") or ""))
        if not script.is_file():
            QMessageBox.warning(
                self, "脚本不在",
                f"找不到 {script}\n可以手动双击桌面的 DSH.lnk，"
                f"或者把配置里 paths.dsh_cmd 指到真正的 DSH.cmd。")
            return
        try:
            # 不走 os.startfile：ShellExecute 对 .cmd 会弹「打开文件 - 安全警告」。
            # cmd /c start 是普通进程创建，控制台窗口照样可见。
            subprocess.Popen(  # noqa: S603
                ["cmd.exe", "/c", "start", "", str(script)],
                cwd=str(script.parent), close_fds=True,
            )
        except OSError as exc:
            QMessageBox.warning(self, "启动失败", f"{exc}\n可以直接双击 {script}")
            return
        self._set_status(f"已拉起 {script.name}，控制台窗口会出现，等它自己开端口")
        self._set_badge("run", "启动中…")

    def _confirm(self, title: str, text: str, yes_text: str) -> bool:
        """UX-16 的统一确认框：图标警告、按钮写中文动作名，不用 Yes/No 混排。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        btn_yes = box.addButton(yes_text, QMessageBox.DestructiveRole)
        btn_no = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        return box.clickedButton() is btn_yes

    def dsh_restart(self) -> None:
        if not self._confirm(
            "重启 DSH",
            "会执行计划任务「DSH Restart Now」：先停旧实例（15 秒宽限期），再起新的。\n"
            "当前正在这个网关里跑的会话会断开。",
            "重启",
        ):
            return
        try:
            r = subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", "Start-ScheduledTask -TaskName 'DSH Restart Now'"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, "重启失败", str(exc))
            return
        if r.returncode != 0:
            QMessageBox.warning(self, "重启失败", (r.stderr or r.stdout or "").strip()[:400])
            return
        self._set_status("已触发重启计划任务，等网关自己回来")
        self._set_badge("run", "重启中…")

    def dsh_stop(self) -> None:
        port = self._dsh_port()
        pids = self._port_pids(port)
        if not pids:
            QMessageBox.information(self, "没在跑", f"{port} 端口没有监听，本来就没起来。")
            return
        if not self._confirm(
            "关闭 DSH",
            f"会 taskkill /T /F 掉这些进程：{pids}\n"
            "整个进程树一起关；当前会话随即断开。",
            "关闭",
        ):
            return
        killed = []
        for pid in pids:
            try:
                subprocess.run(  # noqa: S603
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=60,
                )
                killed.append(pid)
            except (OSError, subprocess.SubprocessError):
                pass
        self._set_status(f"已关闭 DSH（pid {killed}）。要再起来，点「启动 DSH」或双击 DSH.lnk")
        self._set_badge("idle", "已关闭")

    # ---------- 每列自带的删除入口 ----------

    def delete_in_kind(self, kind: str) -> None:
        """某一列的删除按钮：勾了就批量删勾选的，没勾就删当前焦点那一行。"""
        if kind == cfgmod.KIND_STORE:
            QMessageBox.information(
                self, "store不用删",
                "store 是一份固定副本，不参与按时间淘汰。\n要更新它，用「更新 store」。")
            return
        slots = sorted(self._checked.get(kind) or ())
        if not slots:
            sel = self._selected_slot()
            if sel and sel[0] == kind:
                slots = [sel[1]]
        if not slots:
            QMessageBox.information(
                self, "先选一份",
                f"「{KIND_LABEL.get(kind, kind)}」列里勾一行（可多选），或点中一行，再点删除。")
            return
        if len(slots) == 1:
            self.delete_slot(kind, slots[0])
            return
        self.delete_many(kind, slots)

    def delete_many(self, kind: str, slots: list[str]) -> None:
        """批量删同一列的多份。走软删除（进回收区），不提供 5 秒撤销。"""
        if self._busy_guard():
            return
        label = KIND_LABEL.get(kind, kind)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("确认删除")
        box.setText(f"删除「{label}」里的 {len(slots)} 份？")
        box.setInformativeText(
            f"它们都会移到回收区，{engine.recycle_keep_days(self.cfg)} 天后自动清除。\n"
            "多份一起删不提供 5 秒撤销——要反悔请从回收区拿回来。")
        btn_yes = box.addButton(f"删除 {len(slots)} 份", QMessageBox.DestructiveRole)
        btn_no = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        if box.clickedButton() is not btn_yes:
            return
        ok = 0
        failed: list[str] = []
        for s in slots:
            res = engine.delete_slot(self.root, kind, s, self.logger)
            if res.get("ok"):
                ok += 1
            else:
                failed.append(s)
        self._begin_action(f"删除 {label}")
        self._checked[kind] = set()
        self.reload_table()
        self.refresh_status()
        now_n = len(manifest.scan(self.root, kind))
        msg = f"已删除 {ok} 份 · 现存 {now_n} 份"
        if failed:
            msg += f" · {len(failed)} 份失败"
        self.lbl_phase.setText(msg)
        self.lbl_phase.setToolTip("失败的那些仍在列表里，可以单份重试")
        self.lbl_phase.setStyleSheet(f"color:{SUCCESS_GREEN};font-weight:600")
        self.bar.setValue(0)
        self.bar.setStyleSheet("")
        self._set_badge("ok", "✔ 已删除")
        self._set_status(msg)

    def delete_slot(self, kind: str, slot: str) -> None:
        if self._busy_guard():
            return
        total = len(manifest.scan(self.root, kind))
        label = KIND_LABEL.get(kind, kind)
        # UX-16：图标换成警告、按钮写「删除 / 取消」，不再 Yes/No 混排
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("确认删除")
        box.setText(f"删除「{label} · {cfgmod.human_slot(slot)}」？")
        # R3-4：这句定稿，全仓只有这一个说法（旧版写「永远留在回收区」，
        # 与 30 天自动清除直接矛盾）
        lines = [
            "删掉后 5 秒内可以撤销；超时它会留在回收区，"
            f"{engine.recycle_keep_days(self.cfg)} 天后自动清除。"]
        if total <= 1:
            lines.insert(0, f"这是你唯一的一份{label}——删掉这段时间点就只剩回收区里那一份了。")
        box.setInformativeText("\n".join(lines))
        btn_yes = box.addButton("删除", QMessageBox.DestructiveRole)
        btn_no = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        if box.clickedButton() is not btn_yes:
            return
        res = engine.delete_slot(self.root, kind, slot, self.logger)
        if not res.get("ok"):
            QMessageBox.warning(self, "删除失败", str(res.get("reason")))
            return
        # UX-03：动作一切换就重置进度区，不再留着上一次的「50% 完成 2 个包」
        self._begin_action(f"删除 {label}")
        self.reload_table()
        self.refresh_status()
        now_n = len(manifest.scan(self.root, kind))
        self.lbl_phase.setText(f"已删除 1 份 · 现存 {now_n} 份")
        self.lbl_phase.setToolTip(f"已删除「{label} · {cfgmod.human_slot(slot)}」")
        self.lbl_phase.setStyleSheet(f"color:{SUCCESS_GREEN};font-weight:600")
        self.bar.setValue(0)
        self.bar.setStyleSheet("")
        self._set_badge("ok", "✔ 已删除")
        self._set_status(f"已删除 {label} · 现存 {now_n} 份")
        # UX-04：5 秒撤销窗口
        self._undo = dict(res)
        self._undo["deleted_kind"] = kind
        self._undo_left = 5
        self.btn_undo.setText(f"撤销删除（{self._undo_left}）")
        self.btn_undo.setVisible(True)

    def _undo_delete(self) -> None:
        if not self._undo:
            return
        res = engine.restore_deleted(
            self.root, self._undo.get("deleted_kind") or "", self._undo.get("deleted"),
            moved_to=self._undo.get("movedTo"), logger=self.logger,
        )
        if not res.get("ok"):
            QMessageBox.warning(self, "撤销失败", str(res.get("reason")))
            return
        self._undo = None
        self._undo_left = 0
        self.btn_undo.setVisible(False)
        self.reload_table()
        self.refresh_status()
        self.lbl_phase.setText("已撤销删除，那一份回到列表里了")
        self.lbl_phase.setStyleSheet(f"color:{SUCCESS_GREEN}")
        self._set_status("已撤销删除")

    def open_log_dir(self) -> None:
        """UX-14：按钮说实话，并把「今天那个日志文件的完整路径 + 命名规则」直接告诉用户。

        原来点「查看日志」只是打开资源管理器，用户要自己在里面找文件、找程序打开、
        猜格式。名字改对 + 全路径给到位，至少省掉一次摸索。
        """
        d = Path(self.cfg["paths"]["backup_root"]) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        today = d / f"backup-{datetime.now():%Y%m%d}.log"
        target = today if today.exists() else d
        self._set_status(f"日志目录 {d}（一天一个文件：backup-YYYYMMDD.log）")
        try:
            os.startfile(str(target))  # noqa: S606
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def open_backup_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.root))  # noqa: S606
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self.root, self)
        if dlg.exec() == SettingsDialog.Accepted:
            self.cfg = cfgmod.load(Path(self.cfg["paths"]["backup_root"]))
            self.root = Path(self.cfg["paths"]["backup_root"])
            cfgmod.ensure_dirs(self.root, self.cfg)
            self.refresh_status()
            self.reload_table()
            self._set_status("设置已保存")

    # ---------- 自动调度 ----------

    def _on_pause_toggled(self, state: int) -> None:
        """裁决 1：开关落盘（跨重启有效）+ 常显状态文字 + 恢复时立刻补跑一次。"""
        paused = bool(state)
        engine.write_pause(self.root, paused)
        self._apply_pause_state(engine.read_pause(self.root), from_click=True)
        if not paused:
            self.refresh_status()
            self._auto_due()

    def _apply_pause_state(self, rec: dict | None, from_click: bool = False) -> None:
        """把「暂停中/开着 + 暂停了多久」写进进度行右侧的常显标签。"""
        paused = bool(rec and rec.get("paused"))
        self._pause_rec = rec or {}
        if paused and not from_click:
            self.chk_pause.blockSignals(True)
            self.chk_pause.setChecked(True)
            self.chk_pause.blockSignals(False)
        if paused:
            at = int(self._pause_rec.get("pausedAt") or 0)
            span = human_span_long((datetime.now().timestamp() - at / 1000.0) * 1000.0) if at else "刚刚"
            # 状态只说一次：勾选框写**动作名**（点它会发生什么），状态交给 lbl_pause_state。
            # 原来两处都写「自动备份已暂停」，同一行出现两遍。
            self.chk_pause.setText("恢复自动备份")
            self.chk_pause.setStyleSheet(
                "font-weight:600;font-size:14px;color:#444444"
            )
            self.chk_pause.setToolTip(
                "取消勾选＝恢复自动备份。\n"
                "暂停期间：基线与会话都不再自动生成；手动按钮一直可用。\n"
                "正在跑的那一次不会被中断（跑完为止）。\n"
                "状态存在 state\\pause.json（paused=true），关掉程序再打开仍是暂停。"
            )
            # spec §4：灰点 + 「已暂停（N）」+ 手动备份仍可用，一行说完
            self.lbl_pause_state.setText(
                f'<span style="color:#666666">● 自动备份已暂停（{span}）</span>'
                f'<span style="color:{DANGER_RED}"> · 手动备份仍可用</span>'
            )
            self.lbl_pause_state.setToolTip(
                "暂停状态会保存，关掉程序再打开仍然是暂停的。\n"
                "只停本工具的定时器；已经开始的备份会跑完。\n"
                "手动按钮（立即备份基线 / 备份会话 / 备份工作区）一直可用。"
            )
        else:
            self.chk_pause.setText("暂停自动备份")
            self.chk_pause.setStyleSheet("font-weight:600;font-size:14px")
            self.chk_pause.setToolTip(
                "勾上＝暂停自动备份：基线与会话都停掉定时器，手动按钮一直可用。\n"
                "正在跑的那一次不会被中断（会跑完）。\n"
                "状态会存进 state\\pause.json（paused=true），关掉程序再打开仍是暂停的。"
            )
            self.lbl_pause_state.setText(
                f'<span style="color:{SUCCESS_GREEN}">自动备份：开</span>'
                '<span style="color:#666666"> · 每 15 分钟一次</span>'
            )
            self.lbl_pause_state.setToolTip(
                "到点会自动备份基线与会话（会话每 15 分钟，基线每天一次）")
        # 清空历史三种置灰原因分开写（spec §4）：未暂停 / 忙 / 无包
        if hasattr(self, "btn_clear"):
            self.btn_clear.setEnabled(paused and self.job is None)
            if not paused:
                tip = "先勾「暂停自动备份」才能清空——跑着的时候清空，下一分钟又会有新包"
            elif self.job is not None:
                tip = "正在跑一次备份/恢复，等它结束再清空"
            elif not [r for r in self.collect_rows() if r.get("kind") != cfgmod.KIND_STORE]:
                tip = "现在没有任何备份记录，没有可清空的东西"
            else:
                tip = "清空所有备份记录（移到 _deleted\\，30 天后自动清理，期间可手工取回）"
            self.btn_clear.setToolTip(tip)

    def _on_clear_history(self) -> None:
        """裁决 2：暂停后才可清空；打字「清空」确认；逐份走软删除。"""
        if not self.chk_pause.isChecked():
            QMessageBox.information(
                self, "先暂停自动备份",
                "清空历史只在「自动备份：已暂停」时可用。\n"
                "跑着的时候清空，下一分钟就会有新包冒出来，看不出到底清干净了没有。",
            )
            return
        rows = [r for r in self.collect_rows() if r.get("kind") != cfgmod.KIND_STORE]
        if not rows:
            QMessageBox.information(self, "没有可清的", "现在历史表里只有store，没有可清的备份。")
            return
        total_bytes = sum(int(r.get("bytes") or 0) for r in rows)
        proceed, wipe = self._ask_clear(rows, total_bytes)
        if not proceed:
            self._set_status("已取消清空")
            return
        wiped = 0
        if wipe and self._ask_type_clear():
            res = engine.purge_recycle(self.root, 0, logger=self.logger)
            wiped = len(res.get("purged") or [])
        done, failed = 0, []
        for r in rows:
            try:
                engine.delete_slot(self.root, r.get("kind"), r.get("slot"), self.logger)
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{r.get('slot')}: {exc}")
        self.reload_table()
        self.refresh_status()
        rec = engine.recycle_stats(self.root)
        keep_days = engine.recycle_keep_days(self.cfg)
        tail = f"，回收区 {human_bytes(int(rec.get('bytes') or 0))}（{keep_days} 天后自动释放）"
        if wiped:
            tail += f"（其中 {wiped} 份已彻底删除）"
        self._set_status(f"已清空 {done} 份{tail}")
        msg = (
            f"已清空 {done} 份。它们只是移到了 {cfgmod.DELETED_DIR}\\ 回收区，"
            f"**磁盘空间要等 {keep_days} 天后自动清理时才真正释放**。\n"
            f"所以现在回收区会变得很大（{human_bytes(int(rec.get('bytes') or 0))}），"
            "底栏可能立刻提示回收区偏大——这是正常的，不是没清掉。\n"
            "想马上腾出空间：在上面勾「同时清空回收区」再清一次。"
        )
        if wiped:
            msg += f"\n回收区另有 {wiped} 份被彻底删除（不可还原）。"
        if failed:
            msg += f"\n\n{len(failed)} 份没清掉：\n" + "\n".join(failed[:6])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if failed else QMessageBox.Information)
        box.setWindowTitle("清空历史")
        box.setText(f"已清空 {done} 份备份{tail}。")
        box.setInformativeText(msg)
        box.setDetailedText(f"回收区位置：{Path(self.root) / cfgmod.DELETED_DIR}")
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()

    def _ask_clear(self, rows: list[dict], total_bytes: int) -> tuple[bool, bool]:
        """R3-4 第一层确认：三句封顶 + 「同时清空回收区」勾选。

        返回 (是否继续, 是否连回收区一起清)。抽成独立方法是为了自检能打桩。
        """
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Warning)
        dlg.setWindowTitle("清空历史")
        dlg.setText(f"清空 {len(rows)} 份备份（{human_bytes(total_bytes)}）？")
        dlg.setInformativeText(
            f"它们会移到 {cfgmod.DELETED_DIR}\\ 回收区，"
            f"{engine.recycle_keep_days(self.cfg)} 天内可以手工搬回来。\n"
            f"**空间不会立刻释放**：要等 {engine.recycle_keep_days(self.cfg)} 天后回收区自动清理"
            "才真正腾出来，所以清完回收区会变得很大。\n"
            "不影响正在进行的备份；store不动。"
        )
        chk = QCheckBox("同时清空回收区（不可还原）")
        dlg.setCheckBox(chk)
        btn_ok = dlg.addButton(f"清空 {len(rows)} 份", QMessageBox.DestructiveRole)
        btn_no = dlg.addButton("不清了", QMessageBox.RejectRole)
        dlg.setDefaultButton(btn_no)
        dlg.exec()
        return dlg.clickedButton() is btn_ok, chk.isChecked()

    def _ask_type_clear(self) -> bool:
        """R3-4 第二层：连回收区一起清才配得上打字确认（不可逆）。"""
        ask = QInputDialog(self)
        ask.setWindowTitle("连回收区一起清")
        ask.setInputMode(QInputDialog.TextInput)
        ask.setLabelText(
            "这一下连回收区里那些旧备份也删掉，不能还原。\n\n"
            "确认请打字：清空"
        )
        ask.setOkButtonText("连回收区一起清")
        ask.setCancelButtonText("只清列表")
        return (ask.exec() == QInputDialog.Accepted
                and ask.textValue().strip() == "清空")

    def _purge_recycle(self) -> None:
        """裁决 3：回收区超过保留天数自动清（启动时跑一次，失败不打扰用户）。"""
        try:
            res = engine.purge_recycle(self.root, engine.recycle_keep_days(self.cfg),
                                       logger=self.logger)
        except Exception:  # noqa: BLE001
            return
        gone = len(res.get("purged") or [])
        if gone:
            self._set_status(
                f"回收区已清理 {gone} 份超期备份（{human_bytes(int(res.get('bytes', 0)))}）"
            )

    def _update_disk_row(self) -> None:
        """硬伤 4 + R3-5：底栏一行磁盘态势，三档按「还能不能存下下一份」判。

        定稿文案：`磁盘剩余 42.7 GB（18%） · 库占用 3.4 GB · 回收区 0.2 GB`
        档位①正常（灰）②偏紧（橙 #8a4b00 + 「够再存 N 份基线」）
        ③危急（红 #a12a1e + ⚠ + 「不够下一次基线」）。
        危急档必须常显——它挂在 statusBar 的 permanent 区，进度行让位也压不掉。
        """
        try:
            st = engine.status_summary(self.cfg, self.root)
        except Exception:  # noqa: BLE001
            self.lbl_disk.setText("")
            return
        kinds = st.get("kinds") or {}
        free_gb = float(st.get("diskFreeGb") or 0.0)
        total_gb = float(st.get("diskTotalGb") or 0.0)
        lib_bytes = int(st.get("libraryBytes") or 0)
        lib_gb = lib_bytes / (1024 ** 3)
        recycle = st.get("recycle") or {}
        rec_bytes = int(recycle.get("bytes") or 0)
        pct = (free_gb / total_gb * 100.0) if total_gb else 0.0
        # 「一份基线」多大：优先拿现存基线的平均体积，没有就拿库里最大的那份
        base = kinds.get(cfgmod.KIND_BASELINE) or {}
        base_bytes = int(base.get("bytes") or 0)
        base_n = int(base.get("count") or 0)
        snap_gb = (base_bytes / base_n / (1024 ** 3)) if base_n else 0.6
        snap_gb = max(snap_gb, 0.05)

        bits = [f"磁盘剩余 {free_gb:.1f} GB（{pct:.0f}%）", f"库占用 {human_gb(lib_bytes)}"]
        if rec_bytes > 0:
            bits.append(f"回收区 {human_gb(rec_bytes)}")
        tier = disk_tier(free_gb, lib_gb, snap_gb)
        if tier == 3:
            color = "#a12a1e"
            bits[0] = f"<b>⚠ {bits[0]} —— 不够下一次基线</b>"
        elif tier == 2:
            color = "#8a4b00"
            room = int(free_gb / snap_gb)
            bits[0] = f"<b>{bits[0]}</b>"
            bits.append(f"够再存 {room} 份基线")
        else:
            color = "#666666"
        self.lbl_disk.setText(" · ".join(bits))
        self.lbl_disk.setStyleSheet(f"color:{color};padding-right:6px")
        self._disk_tier = tier
        self.lbl_disk.setToolTip(
            f"回收区：{int(recycle.get('count') or 0)} 份 "
            f"{human_gb(rec_bytes)}，{engine.recycle_keep_days(self.cfg)} 天后自动清理。\n"
            f"一份基线约 {snap_gb:.1f} GB；剩余低于它的两倍就是「危急」。\n"
            f"剩余低于 {engine.disk_hard_min_gb(self.cfg):.0f} GB 才会拒绝新备份。"
        )

    def _auto_due(self) -> None:
        if self.chk_pause.isChecked():
            return
        if self.job is not None and self.job.isRunning():
            return
        try:
            due = engine.due_kinds(self.cfg, self.root)
        except Exception:  # noqa: BLE001
            return
        if not due:
            return
        kind = due[0]
        self._run_job(
            f"自动备份 {KIND_LABEL.get(kind, kind)}",
            engine.backup_kind,
            self.cfg, self.root, kind,
            note="自动",
            progress=self.progress,
            logger=self.logger,
        )
