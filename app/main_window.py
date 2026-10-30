from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dshbackup import config as cfgmod
from dshbackup import engine, manifest, sevenzip
from dshbackup.log import Logger

from .theme import STYLE, card, stat_card
from .timeline import TimelineWidget
from .workers import JobThread


PAGE_META = {
    "overview": ("运行概览", "DSH 状态、备份新鲜度与磁盘空间一屏查看"),
    "backup": ("备份与恢复", "按类型查看备份时间泳道；store 镜像用于新盘部署"),
    "deploy": ("部署新环境", "选择快照和目标位置，由程序完成路径重定位与启动准备"),
    "manage": ("DSH 管理", "独立管理网关启停、重启与运行期间的守护"),
}


def human_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def format_time(value: int | None) -> str:
    if not value:
        return "暂无"
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, data_dir: Path) -> None:
        super().__init__()
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.backup_root = Path(cfg["paths"]["backup_root"])
        self.logger = Logger(self.backup_root / "logs")
        self.logger.info(
            "dshlauncher2_started",
            port=int(self.cfg.get("ports", {}).get("dsh", 3025) or 3025),
            backupRoot=str(self.backup_root),
        )
        self.worker: JobThread | None = None
        self.job_label = ""
        self.job_started = 0.0
        self._auto_attempted: dict[str, float] = {}
        self._deploy_manual_choice: set[str] = set()
        self._managed_start_script = self._make_managed_start_script()
        self._guard_enabled = self._read_guard()
        self._guard_starting_until = 0.0
        self.setWindowTitle("DSHLuncher 2")
        self.setMinimumSize(1120, 760)
        self.resize(1370, 900)
        self.setStyleSheet(STYLE)
        self._build_shell()
        self._build_pages()
        self._build_status_bar()
        self._activate_page(0)
        self.refresh_all()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_job)
        self.poll_timer.start(300)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.refresh_all)
        self.health_timer.start(8000)
        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.timeout.connect(self._auto_backup_tick)
        self.scheduler_timer.start(60_000)
        self.guard_timer = QTimer(self)
        self.guard_timer.timeout.connect(self._guard_tick)
        self.guard_timer.start(30_000)

    # ---------- shell ----------

    def _build_shell(self) -> None:
        root = QWidget()
        root.setObjectName("mainRoot")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(226)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(17, 22, 17, 16)
        side.setSpacing(7)
        brand = QLabel("DSH LUNCHER")
        brand.setObjectName("brandMark")
        brand_note = QLabel("MAINTAINER  ·  2.0")
        brand_note.setObjectName("brandSub")
        side.addWidget(brand)
        side.addWidget(brand_note)
        side.addSpacing(20)

        self.nav_buttons: list[QPushButton] = []
        self.stack = QStackedWidget()
        for index, (key, label, glyph) in enumerate([
            ("overview", "运行概览", "◉"),
            ("backup", "备份与恢复", "▣"),
            ("deploy", "部署新环境", "↗"),
            ("manage", "DSH 管理", "⚙"),
        ]):
            button = QPushButton(f"{glyph}     {label}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._activate_page(i))
            side.addWidget(button)
            self.nav_buttons.append(button)

        side.addStretch(1)
        port = int(self.cfg.get("ports", {}).get("dsh", 3025) or 3025)
        footer = QLabel(f"本地网关  127.0.0.1:{port}\n数据目录由新版配置独立管理")
        footer.setObjectName("brandSub")
        footer.setWordWrap(True)
        side.addWidget(footer)

        content_wrap = QWidget()
        content_layout = QVBoxLayout(content_wrap)
        content_layout.setContentsMargins(28, 22, 28, 0)
        content_layout.setSpacing(18)
        header = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(4)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("pageSubtitle")
        header_text.addWidget(self.page_title)
        header_text.addWidget(self.page_subtitle)
        header.addLayout(header_text)
        header.addStretch(1)
        self.gateway_badge = QLabel("●  检查中")
        self.gateway_badge.setStyleSheet("color:#758196;background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:8px 12px;font-size:12px")
        header.addWidget(self.gateway_badge)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_all)
        header.addWidget(refresh_button)
        content_layout.addLayout(header)
        content_layout.addWidget(self.stack, 1)

        outer.addWidget(self.sidebar)
        outer.addWidget(content_wrap, 1)
        self.setCentralWidget(root)

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)
        self.job_text = QLabel("就绪")
        self.job_text.setMinimumWidth(280)
        self.job_progress = QProgressBar()
        self.job_progress.setFixedWidth(190)
        self.job_progress.setRange(0, 100)
        self.job_progress.hide()
        self.job_detail = QLabel("")
        self.job_detail.setMinimumWidth(260)
        status.addWidget(self.job_text, 1)
        status.addWidget(self.job_detail, 1)
        status.addPermanentWidget(self.job_progress)

    def _activate_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)
        key = ["overview", "backup", "deploy", "manage"][index]
        self.page_title.setText(PAGE_META[key][0])
        self.page_subtitle.setText(PAGE_META[key][1])
        if key == "backup":
            self._refresh_backup_table()
        elif key == "deploy":
            self._refresh_deploy_slots()
        elif key == "manage":
            self._refresh_manage_state()

    # ---------- overview ----------

    def _build_pages(self) -> None:
        self.overview_page = self._build_overview_page()
        self.backup_page = self._build_backup_page()
        self.deploy_page = self._build_deploy_page()
        self.manage_page = self._build_manage_page()
        for page in (self.overview_page, self.backup_page, self.deploy_page, self.manage_page):
            self.stack.addWidget(page)

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(15)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 21, 24, 21)
        hero_left = QVBoxLayout()
        hero_left.setSpacing(7)
        eyebrow = QLabel("DEEPSEEK HARNESS  /  GATEWAY")
        eyebrow.setObjectName("heroMuted")
        self.hero_status = QLabel("正在读取 DSH 状态…")
        self.hero_status.setObjectName("heroValue")
        self.hero_detail = QLabel("")
        self.hero_detail.setObjectName("heroMuted")
        hero_left.addWidget(eyebrow)
        hero_left.addWidget(self.hero_status)
        hero_left.addWidget(self.hero_detail)
        hero_layout.addLayout(hero_left, 1)
        hero_buttons = QVBoxLayout()
        self.quick_dsh_button = QPushButton("打开 DSH")
        self.quick_dsh_button.clicked.connect(self.open_dsh)
        self.quick_dsh_button.setStyleSheet("color:#143126;background:#bcebd9;border:0;font-weight:700;padding:11px 18px")
        quick_manage = QPushButton("进入管理")
        quick_manage.clicked.connect(lambda: self._activate_page(3))
        quick_manage.setStyleSheet("color:white;background:#25394f;border:1px solid #3c5268")
        hero_buttons.addWidget(self.quick_dsh_button)
        hero_buttons.addWidget(quick_manage)
        hero_layout.addLayout(hero_buttons)
        layout.addWidget(hero)

        stats = QGridLayout()
        stats.setSpacing(12)
        self.stat_baseline = stat_card("软件基线", "—", "最近的完整程序快照")
        self.stat_sessions = stat_card("会话备份", "—", "15 分钟节奏，单独恢复")
        self.stat_storage = stat_card("备份库", "—", "占用与目标盘剩余空间")
        self.stat_workspace = stat_card("工作区", "手动备份", "不参与自动轮转")
        stats.addWidget(self.stat_baseline, 0, 0)
        stats.addWidget(self.stat_sessions, 0, 1)
        stats.addWidget(self.stat_storage, 0, 2)
        stats.addWidget(self.stat_workspace, 0, 3)
        layout.addLayout(stats)

        bottom = QHBoxLayout()
        backup_frame, backup_box = card("快速备份", "常用备份动作在概览可直接开始")
        quick_row = QHBoxLayout()
        self.quick_baseline = QPushButton("立即备份基线")
        self.quick_baseline.setObjectName("primaryButton")
        self.quick_baseline.clicked.connect(lambda: self.start_backup("baseline"))
        self.quick_sessions = QPushButton("备份会话")
        self.quick_sessions.clicked.connect(lambda: self.start_backup("sessions"))
        self.quick_workspace = QPushButton("备份工作区")
        self.quick_workspace.clicked.connect(lambda: self.start_backup("workspace"))
        self.quick_store = QPushButton("同步 pnpm store")
        self.quick_store.clicked.connect(lambda: self.start_backup("store"))
        quick_row.addWidget(self.quick_baseline)
        quick_row.addWidget(self.quick_sessions)
        quick_row.addWidget(self.quick_workspace)
        quick_row.addWidget(self.quick_store)
        backup_box.addLayout(quick_row)
        backup_frame.setMinimumHeight(112)
        bottom.addWidget(backup_frame, 3)

        health_frame, health_box = card("最近活动", "新旧状态一眼可见")
        self.recent_activity = QLabel("正在读取备份记录…")
        self.recent_activity.setObjectName("muted")
        self.recent_activity.setWordWrap(True)
        health_box.addWidget(self.recent_activity)
        self.auto_status = QLabel("自动备份状态读取中")
        self.auto_status.setObjectName("muted")
        health_box.addWidget(self.auto_status)
        bottom.addWidget(health_frame, 2)
        layout.addLayout(bottom)
        layout.addStretch(1)
        return page

    # ---------- backups and restore ----------

    def _build_backup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(14)

        actions, actions_box = card("备份类型", "store 以 robocopy 增量镜像保存；新环境部署时会复制到目标盘的本地 pnpm store。")
        row = QHBoxLayout()
        self.backup_buttons: dict[str, QPushButton] = {}
        for kind, title in [("baseline", "备份基线"), ("sessions", "备份会话"), ("workspace", "备份工作区"), ("store", "同步 pnpm store")]:
            button = QPushButton(title)
            if kind in {"baseline", "store"}:
                button.setObjectName("primaryButton")
            button.clicked.connect(lambda checked=False, k=kind: self.start_backup(k))
            self.backup_buttons[kind] = button
            row.addWidget(button)
        row.addStretch(1)
        self.pause_auto = QCheckBox("暂停自动备份")
        self.pause_auto.toggled.connect(self.toggle_pause)
        row.addWidget(self.pause_auto)
        actions_box.addLayout(row)
        layout.addWidget(actions)

        history, history_box = card("备份历史", "选中一份再恢复。恢复到原位置会替换对应目录，并保留旧目录兜底。")
        controls = QHBoxLayout()
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("全部类型", "all")
        self.kind_filter.addItem("基线", "baseline")
        self.kind_filter.addItem("会话", "sessions")
        self.kind_filter.addItem("工作区", "workspace")
        self.kind_filter.addItem("pnpm store", "store")
        self.kind_filter.currentIndexChanged.connect(self._refresh_backup_table)
        controls.addWidget(QLabel("类型"))
        controls.addWidget(self.kind_filter)
        controls.addStretch(1)
        self.restore_button = QPushButton("恢复所选单项…")
        self.restore_button.setObjectName("primaryButton")
        self.restore_button.clicked.connect(self.restore_selected)
        controls.addWidget(self.restore_button)
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self._refresh_backup_table)
        controls.addWidget(refresh)
        history_box.addLayout(controls)

        self.backup_table = QTableWidget(0, 5)
        self.backup_table.setHorizontalHeaderLabels(["类型", "备份时间", "备份大小", "文件数", "状态"])
        self.backup_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.backup_table.setSelectionMode(QTableWidget.SingleSelection)
        self.backup_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.backup_table.setAlternatingRowColors(True)
        self.backup_table.verticalHeader().setVisible(False)
        self.backup_table.horizontalHeader().setStretchLastSection(True)
        self.backup_table.setColumnWidth(0, 100)
        self.backup_table.setColumnWidth(1, 180)
        self.backup_table.setColumnWidth(2, 140)
        self.backup_table.setColumnWidth(3, 100)
        self.backup_table.itemSelectionChanged.connect(self._update_restore_enabled)
        history_box.addWidget(self.backup_table, 1)
        self.timeline = TimelineWidget()
        self.timeline.slotClicked.connect(self._on_timeline_click)
        history_box.addWidget(self.timeline)
        self.backup_hint = QLabel("")
        self.backup_hint.setObjectName("muted")
        history_box.addWidget(self.backup_hint)
        layout.addWidget(history, 1)
        return page

    def _refresh_backup_table(self, *_args) -> None:
        if not hasattr(self, "backup_table"):
            return
        kind_filter = self.kind_filter.currentData()
        previous = None
        selected_rows = self.backup_table.selectionModel().selectedRows()
        if selected_rows:
            item = self.backup_table.item(selected_rows[0].row(), 0)
            previous = item.data(Qt.UserRole) if item else None
        all_rows: list[dict] = []
        for kind in ("baseline", "sessions", "workspace"):
            all_rows.extend(manifest.scan(self.backup_root, kind))
        store_slot = manifest.mirror_slot(self.backup_root)
        if store_slot:
            all_rows.append(store_slot)
        all_rows.sort(key=lambda item: item.get("createdAt", 0), reverse=True)
        rows = all_rows if kind_filter == "all" else [r for r in all_rows if r.get("kind") == kind_filter]
        self.timeline.setData(all_rows)
        self.backup_table.setRowCount(len(rows))
        labels = {"baseline": "基线", "sessions": "会话", "workspace": "工作区", "store": "pnpm store"}
        for row, slot in enumerate(rows):
            kind = slot.get("kind")
            if kind == "store":
                state = "固定增量镜像 · 可部署" if slot.get("verifiedOk") and slot.get("onDisk") else "镜像需检查"
            else:
                state = "可恢复" if slot.get("verifiedOk") and slot.get("onDisk") else "需检查"
            values = [
                labels.get(kind, kind or ""),
                format_time(slot.get("createdAt")),
                human_bytes(slot.get("bytes")),
                f"{int(slot.get('files') or 0):,}",
                state,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, (slot.get("kind"), slot.get("slot")))
                self.backup_table.setItem(row, col, item)
        self.backup_table.resizeRowsToContents()
        if previous:
            for row in range(self.backup_table.rowCount()):
                item = self.backup_table.item(row, 0)
                if item and item.data(Qt.UserRole) == previous:
                    self.backup_table.setCurrentCell(row, 0)
                    self.backup_table.selectRow(row)
                    break
        self.backup_hint.setText(
            f"共 {len(rows)} 项。store 是固定增量镜像，不按时间淘汰，也不单项恢复；"
            "部署新环境时会把它复制到目标盘。"
        )
        self._update_restore_enabled()

    def _update_restore_enabled(self) -> None:
        if not hasattr(self, "restore_button"):
            return
        selected_rows = self.backup_table.selectionModel().selectedRows()
        row = selected_rows[0].row() if selected_rows else -1
        selection = None
        if row >= 0:
            item = self.backup_table.item(row, 0)
            selection = item.data(Qt.UserRole) if item else None
        kind, slot = selection or ("", "")
        if hasattr(self, "timeline"):
            self.timeline.setSelected(kind, slot)
        self.restore_button.setEnabled(bool(row >= 0 and kind != "store" and self.worker is None))

    def _on_timeline_click(self, kind: str, slot: str) -> None:
        """在全量时间泳道上选中对应历史行；store 镜像不开放单项恢复。"""
        if self.kind_filter.currentData() != "all":
            index = self.kind_filter.findData("all")
            if index >= 0:
                self.kind_filter.setCurrentIndex(index)
        for row in range(self.backup_table.rowCount()):
            item = self.backup_table.item(row, 0)
            if item and item.data(Qt.UserRole) == (kind, slot):
                self.backup_table.setCurrentCell(row, 0)
                self.backup_table.selectRow(row)
                self.backup_table.scrollToItem(item, QTableWidget.PositionAtCenter)
                return

    def restore_selected(self) -> None:
        selected_rows = self.backup_table.selectionModel().selectedRows()
        row = selected_rows[0].row() if selected_rows else -1
        if row < 0:
            return
        selected = self.backup_table.item(row, 0).data(Qt.UserRole)
        kind, slot_id = selected
        if kind == "store":
            QMessageBox.information(self, "store 是固定镜像", "pnpm store 不作为独立时间点恢复；部署新环境时会从该镜像复制到目标盘。")
            return
        hit = next((s for s in manifest.scan(self.backup_root, kind) if s.get("slot") == slot_id), None)
        if not hit:
            QMessageBox.warning(self, "备份不存在", "刷新列表后重新选择。")
            return
        if not hit.get("verifiedOk") or not hit.get("onDisk"):
            QMessageBox.warning(self, "此备份暂不可恢复", "压缩包缺失或未通过完整性校验。")
            return
        choice, ok = QInputDialog.getItem(
            self, "单项恢复", "选择恢复位置：", ["恢复回原位置", "恢复到新目录"], 0, False
        )
        if not ok:
            return
        if choice == "恢复回原位置":
            confirm = QMessageBox.question(
                self, "确认覆盖原位置",
                f"将把 {format_time(hit.get('createdAt'))} 的{kind_label(kind)}恢复到它原来的目录。\n"
                "恢复前会把当前目录打兜底包；确认继续？",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                return
            operation = lambda progress: self._verified_restore(
                progress, kind, slot_id,
                lambda: engine.restore_slot(
                    self.cfg, self.backup_root, kind, slot_id,
                    dry_run=False, progress=progress, logger=self.logger,
                ),
            )
            self._start_job(f"恢复{kind_label(kind)}到原位置", operation, "restore")
            return

        target = QFileDialog.getExistingDirectory(self, "选择目标父目录", str(Path.home()))
        if not target:
            return
        confirm = QMessageBox.question(
            self, "确认恢复到新目录",
            f"备份会写入新位置：\n{target}\n\n原目录不会被覆盖。继续？",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        operation = lambda progress: self._verified_restore(
            progress, kind, slot_id,
            lambda: engine.restore_slot(
                self.cfg, self.backup_root, kind, slot_id,
                into=target, dry_run=False, auto_finish=(kind == "baseline"),
                progress=progress, logger=self.logger,
            ),
        )
        self._start_job(f"恢复{kind_label(kind)}到新目录", operation, "restore")

    def _verified_restore(self, progress, kind: str, slot_id: str, restore_call) -> dict:
        failure = self._verify_snapshot(progress, kind, slot_id)
        if failure:
            return failure
        return restore_call()

    def _verify_snapshot(self, progress, kind: str, slot_id: str) -> dict | None:
        hit = next((slot for slot in manifest.scan(self.backup_root, kind)
                    if slot.get("slot") == slot_id), None)
        if not hit:
            return {"ok": False, "reason": f"找不到{kind_label(kind)}快照：{slot_id}"}
        parts = hit.get("parts") or []
        if not hit.get("onDisk"):
            return {"ok": False, "reason": "快照压缩包文件缺失；没有写入目标目录。"}

        # 旧版本只验证 7z 包自身能读，没核对包内文件数是否等于源目录扫描数。
        # 这会让“3 个占用文件没进包”的快照直到写入目标后才失败。先在原地列目录，
        # 按唯一归档分组；单包工作区的多个源要把各自 sourceFiles 相加再比较。
        groups: dict[str, list[dict]] = {}
        for part in parts:
            archive = Path(part.get("zipPath") or "")
            if not archive.is_file():
                return {"ok": False, "reason": f"压缩包文件不存在：{archive}"}
            groups.setdefault(os.path.normcase(str(archive.resolve())), []).append(part)
        for archive_key, group in groups.items():
            archive = Path(archive_key)
            info = sevenzip.archive_info(self.cfg, archive)
            label = "/".join(str(p.get("label") or "包") for p in group)
            if not info.get("ok"):
                return {
                    "ok": False,
                    "reason": f"无法读取快照文件数，未写入目标目录：{archive.name}\n{info.get('error') or ''}",
                }
            expected = sum(int(p.get("sourceFiles", 0) or 0) for p in group)
            actual = int(info["files"])
            if actual != expected:
                return {
                    "ok": False,
                    "reason": (
                        f"{label} 快照不完整，未写入目标目录：源目录清单记录 {expected:,} 个文件，"
                        f"归档实际只有 {actual:,} 个（差 {expected - actual:+,}）。"
                        "这通常表示备份时有文件被占用或发生变化；请使用其他完整快照，"
                        "或关闭相关程序后重新备份。"
                    ),
                }
        if not hit.get("verifiedOk"):
            detail = next((str(p.get("error")) for p in parts if p.get("error")), "")
            return {
                "ok": False,
                "reason": "快照未通过完整性校验；没有写入目标目录。"
                          + (f"\n{detail}" if detail else ""),
            }
        for index, part in enumerate(parts):
            archive = Path(part.get("zipPath") or "")
            phase = f"恢复前校验 {index + 1}/{len(parts)}"
            progress.update(pct=index / max(len(parts), 1) * 20,
                            phase=phase, detail=f"正在检查 {archive.name}")
            check = sevenzip.test_7z(
                self.cfg, archive,
                on_progress=lambda pct, detail: progress.update(
                    pct=(index + pct / 100) / max(len(parts), 1) * 20,
                    phase=phase, detail=f"{archive.name} · {detail}",
                ),
            )
            if not check.ok:
                return {"ok": False, "reason": f"压缩包校验失败，未触碰目标目录：{archive.name}\n{check.error or check.tail_text}"}
        progress.update(pct=20, phase="恢复前校验完成", detail="所有压缩包均可完整读取")
        return None

    # ---------- deploy new environment ----------

    def _build_deploy_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(14)

        selection, selection_box = card("1 · 选择备份时间点", "基线和会话分别选择；工作区可选。pnpm 依赖将使用已配置的本机 store 离线安装。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.deploy_baseline = QComboBox()
        self.deploy_sessions = QComboBox()
        self.deploy_workspace = QComboBox()
        for combo, kind in ((self.deploy_baseline, "baseline"),
                            (self.deploy_sessions, "sessions"),
                            (self.deploy_workspace, "workspace")):
            combo.activated.connect(lambda _index, key=kind: self._deploy_manual_choice.add(key))
        self.include_workspace = QCheckBox("同时恢复工作区")
        grid.addWidget(QLabel("软件基线（必选）"), 0, 0)
        grid.addWidget(self.deploy_baseline, 0, 1)
        grid.addWidget(QLabel("会话（必选）"), 1, 0)
        grid.addWidget(self.deploy_sessions, 1, 1)
        grid.addWidget(self.include_workspace, 2, 0)
        grid.addWidget(self.deploy_workspace, 2, 1)
        selection_box.addLayout(grid)
        layout.addWidget(selection)

        destination, dest_box = card("2 · 选择部署位置", "程序会在此目录下创建新 Harness、.dsh 和可选的 WorkSpace，并自动把目录联接改到新位置。")
        dest_row = QHBoxLayout()
        self.deploy_root = QLineEdit()
        self.deploy_root.setPlaceholderText(r"例如 D:\DSH-Environments\main")
        self.deploy_root.textChanged.connect(self._update_target_preview)
        browse = QPushButton("选择文件夹…")
        browse.clicked.connect(self._browse_deploy_root)
        dest_row.addWidget(self.deploy_root, 1)
        dest_row.addWidget(browse)
        dest_box.addLayout(dest_row)
        self.target_preview = QLabel("目标路径预览：选择一个目标目录")
        self.target_preview.setObjectName("muted")
        self.target_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dest_box.addWidget(self.target_preview)
        layout.addWidget(destination)

        finish, finish_box = card("3 · 自动准备可启动环境", "数据恢复 → pnpm install --offline --frozen-lockfile → 重建 junction → 生成 start-dsh.cmd。")
        self.deploy_finish_note = QLabel("")
        self.deploy_finish_note.setObjectName("muted")
        self.deploy_finish_note.setWordWrap(True)
        finish_box.addWidget(self.deploy_finish_note)
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.deploy_button = QPushButton("开始部署新环境")
        self.deploy_button.setObjectName("primaryButton")
        self.deploy_button.clicked.connect(self.deploy_environment)
        action_row.addWidget(self.deploy_button)
        finish_box.addLayout(action_row)
        layout.addWidget(finish)
        layout.addStretch(1)
        self.include_workspace.toggled.connect(self.deploy_workspace.setEnabled)
        self.deploy_workspace.setEnabled(False)
        self._refresh_deploy_slots()
        self._update_target_preview()
        return page

    def _refresh_deploy_slots(self) -> None:
        if not hasattr(self, "deploy_baseline"):
            return
        for combo, kind in [
            (self.deploy_baseline, "baseline"),
            (self.deploy_sessions, "sessions"),
            (self.deploy_workspace, "workspace"),
        ]:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            slots = manifest.scan(self.backup_root, kind)
            usable = [s for s in slots if s.get("verifiedOk") and s.get("onDisk")]
            for slot in usable:
                combo.addItem(
                    f"{format_time(slot.get('createdAt'))}  ·  {human_bytes(slot.get('bytes'))}",
                    slot.get("slot"),
                )
            old_index = combo.findData(current)
            if kind in self._deploy_manual_choice and old_index >= 0:
                combo.setCurrentIndex(old_index)
            combo.blockSignals(False)
            if not usable:
                combo.addItem("没有可用快照", "")
        self._update_target_preview()

    def _browse_deploy_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择新环境的父目录", str(Path.home()))
        if chosen:
            self.deploy_root.setText(chosen)

    def _target_paths(self) -> dict[str, Path]:
        root_text = self.deploy_root.text().strip()
        if not root_text:
            return {}
        root = Path(root_text).expanduser()
        return {
            "root": root,
            "harness": root / "deepseek-harness",
            "dsh": root / ".dsh",
            "workspace": root / "WorkSpace",
        }

    def _target_pnpm_store(self, checkout: Path) -> Path:
        configured = str(self.cfg.get("paths", {}).get("pnpm_store") or r"D:\.pnpm-store\v11")
        return engine.store_path_for_checkout(self.cfg, checkout) or Path(configured)

    def _update_target_preview(self, *_args) -> None:
        if not hasattr(self, "target_preview"):
            return
        paths = self._target_paths()
        store_slot = manifest.mirror_slot(self.backup_root) or {}
        state = (
            f"{human_bytes(store_slot.get('bytes'))} · {format_time(store_slot.get('createdAt'))}"
            if store_slot.get("verifiedOk") and store_slot.get("onDisk") else "镜像缺失或不可用"
        )
        target_store = self._target_pnpm_store(paths["harness"]) if paths else Path(
            self.cfg.get("paths", {}).get("pnpm_store") or r"D:\.pnpm-store\v11"
        )
        self.deploy_finish_note.setText(
            f"pnpm store 镜像：{state}\n部署后使用目标盘本地路径：{target_store}\n"
            f"新环境启动脚本使用 DSH 端口 {self._dsh_port()}。"
        )
        if not paths:
            self.target_preview.setText("目标路径预览：选择一个目标目录")
            return
        text = (
            f"Harness     {paths['harness']}\n"
            f".dsh             {paths['dsh']}\n"
            f"工作区       {paths['workspace']}"
        )
        self.target_preview.setText(text)

    def deploy_environment(self) -> None:
        baseline_id = self.deploy_baseline.currentData()
        sessions_id = self.deploy_sessions.currentData()
        root_text = self.deploy_root.text().strip()
        if not baseline_id or not sessions_id:
            QMessageBox.warning(self, "快照未选全", "部署新环境需要一份软件基线和一份会话快照。")
            return
        if not root_text or not Path(root_text).is_absolute():
            QMessageBox.warning(self, "目标路径无效", "请输入或选择一个绝对路径。")
            return
        paths = self._target_paths()
        include_workspace = self.include_workspace.isChecked()
        workspace_id = self.deploy_workspace.currentData() if include_workspace else ""
        if include_workspace and not workspace_id:
            QMessageBox.warning(self, "工作区快照未选", "请先选择一份可用的工作区快照。")
            return
        store_slot = manifest.mirror_slot(self.backup_root)
        if not store_slot or not store_slot.get("verifiedOk") or not store_slot.get("onDisk"):
            QMessageBox.warning(self, "pnpm store 镜像不可用", "先到「备份与恢复」更新 pnpm store，再部署新环境。")
            return
        target_store = self._target_pnpm_store(paths["harness"])
        source_store = Path(str(self.cfg.get("paths", {}).get("pnpm_store") or ""))
        same_store = os.path.normcase(os.path.abspath(target_store)) == os.path.normcase(os.path.abspath(source_store))
        if not same_store:
            try:
                free_bytes = shutil.disk_usage(target_store.anchor).free
            except OSError as exc:
                QMessageBox.warning(self, "目标盘不可用", f"无法读取 pnpm store 目标盘空间：\n{target_store.anchor}\n{exc}")
                return
            needed = int(store_slot.get("bytes") or 0)
            if free_bytes < needed:
                QMessageBox.warning(self, "目标盘空间不足", f"复制 pnpm store 至少需要 {human_bytes(needed)}，目标盘仅剩 {human_bytes(free_bytes)}。\n{target_store}")
                return
        selected = [paths["harness"], paths["dsh"]]
        if include_workspace:
            selected.append(paths["workspace"])
        reason = self._validate_deploy_targets(selected)
        if reason:
            QMessageBox.warning(self, "目标路径检查未通过", reason)
            return
        details = (
            f"基线：{baseline_id}\n会话：{sessions_id}\n"
            + (f"工作区：{workspace_id}\n" if include_workspace else "工作区：不恢复\n")
            + f"\nHarness：{paths['harness']}\n.dsh：{paths['dsh']}"
            + (f"\nWorkSpace：{paths['workspace']}" if include_workspace else "")
            + f"\n\npnpm store 镜像：{human_bytes(store_slot.get('bytes'))} / {int(store_slot.get('files') or 0):,} 个文件"
            + f"\n目标盘 store：{target_store}"
            + f"\n启动端口：{self._dsh_port()}\n\n确认开始创建新环境？"
        )
        if QMessageBox.question(self, "确认部署新环境", details, QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        operation = lambda progress: self._deploy_operation(
            progress, baseline_id, sessions_id, workspace_id if include_workspace else "", paths
        )
        self._start_job("部署新环境", operation, "deploy")

    def _validate_deploy_targets(self, targets: list[Path]) -> str:
        resolved = [p.resolve(strict=False) for p in targets]
        for idx, path in enumerate(resolved):
            for other in resolved[idx + 1:]:
                if self._paths_overlap(path, other):
                    return f"部署目录互相嵌套或重叠：\n{path}\n{other}"
            if path.exists() and any(path.iterdir()):
                return f"目标目录非空，为保护现有文件，无法部署：\n{path}"
            protected = [
                Path(value) for value in (
                    self.cfg.get("paths", {}).get("checkout"),
                    self.cfg.get("paths", {}).get("dsh_home"),
                    str(self.backup_root),
                ) if str(value or "").strip()
            ]
            for source in protected:
                if not str(source).strip():
                    continue
                source = source.resolve(strict=False)
                if self._paths_overlap(path, source):
                    return f"目标目录与当前数据或备份库重叠：\n{path}\n{source}"
        return ""

    @staticmethod
    def _paths_overlap(a: Path, b: Path) -> bool:
        try:
            return a == b or a.is_relative_to(b) or b.is_relative_to(a)
        except (AttributeError, OSError):
            try:
                common = Path(os.path.commonpath([str(a), str(b)]))
                return common == a or common == b
            except ValueError:
                return False

    def _deploy_operation(self, progress, baseline_id: str, sessions_id: str,
                          workspace_id: str, paths: dict[str, Path]) -> dict:
        selected = [("baseline", baseline_id), ("sessions", sessions_id)]
        if workspace_id:
            selected.append(("workspace", workspace_id))
        for kind, slot_id in selected:
            failure = self._verify_snapshot(progress, kind, slot_id)
            if failure:
                return {"ok": False, "dataReady": False, "steps": [], "reason": failure["reason"]}
        steps: list[dict] = []
        restore_roots = {"harness": str(paths["harness"]), "dsh": str(paths["dsh"])}
        if workspace_id:
            restore_roots["workspace"] = str(paths["workspace"])
        baseline = engine.restore_slot(
            self.cfg, self.backup_root, "baseline", baseline_id,
            dry_run=False,
            roots=restore_roots,
            auto_finish=False, progress=progress, logger=self.logger,
        )
        steps.append({"label": "软件基线", "ok": bool(baseline.get("ok")), "result": baseline})
        if not baseline.get("ok"):
            return {"ok": False, "dataReady": False, "steps": steps, "reason": baseline.get("reason", "基线恢复失败")}

        sessions = engine.restore_slot(
            self.cfg, self.backup_root, "sessions", sessions_id,
            dry_run=False, roots=restore_roots,
            auto_finish=False, progress=progress, logger=self.logger,
        )
        steps.append({"label": "会话", "ok": bool(sessions.get("ok")), "result": sessions})
        if not sessions.get("ok"):
            return {"ok": False, "dataReady": False, "steps": steps, "reason": sessions.get("reason", "会话恢复失败")}

        if workspace_id:
            workspace = engine.restore_slot(
                self.cfg, self.backup_root, "workspace", workspace_id,
                dry_run=False, roots=restore_roots,
                auto_finish=False, progress=progress, logger=self.logger,
            )
            steps.append({"label": "工作区", "ok": bool(workspace.get("ok")), "result": workspace})
            if not workspace.get("ok"):
                return {"ok": False, "dataReady": False, "steps": steps, "reason": workspace.get("reason", "工作区恢复失败")}

        progress.update(phase="安装依赖与生成启动入口", detail="正在使用本机 pnpm store 安装依赖并准备启动脚本")
        finish = engine.finish_restore(
            self.cfg, baseline, do_install=True, do_links=True,
            cleanup=True, backup_root=self.backup_root, progress=progress, logger=self.logger,
        )
        install = finish.get("install")
        links = finish.get("links")
        store_sync = finish.get("storeSync")
        store_ok = store_sync is None or bool(store_sync.get("ok"))
        if store_sync is not None:
            steps.append({"label": "pnpm store", "ok": store_ok, "result": store_sync})
        launcher = Path(finish.get("launcher") or "")
        install_ok = bool(install and install.get("ok"))
        links_ok = links is None or bool(links.get("ok"))
        launcher_ok = launcher.is_file()
        environment_ready = store_ok and install_ok and links_ok and launcher_ok
        progress.finish(
            "新环境已部署，可直接启动" if environment_ready else "数据已恢复，环境收尾未全部通过",
            environment_ready,
        )
        return {
            "ok": environment_ready,
            "dataReady": True,
            "environmentReady": environment_ready,
            "steps": steps,
            "finish": finish,
            "paths": {key: str(value) for key, value in paths.items()},
        }

    # ---------- DSH management ----------

    def _build_manage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(14)

        status_frame, status_box = card("Gateway 状态", "服务端口与启动入口采用新版配置；自动守护只在 DSHLuncher2 正在运行时生效。")
        status_row = QHBoxLayout()
        self.manage_status = QLabel("正在检查…")
        self.manage_status.setObjectName("statValue")
        self.manage_port = QLabel("")
        self.manage_port.setObjectName("muted")
        status_row.addWidget(self.manage_status)
        status_row.addStretch(1)
        status_row.addWidget(self.manage_port)
        status_box.addLayout(status_row)
        self.manage_script = QLabel("")
        self.manage_script.setObjectName("muted")
        self.manage_script.setWordWrap(True)
        status_box.addWidget(self.manage_script)
        layout.addWidget(status_frame)

        controls, controls_box = card("网关控制", "停止会结束监听目标端口的 DSH 进程树；重启会先停止，再用配置的 Harness 和端口启动。")
        control_row = QHBoxLayout()
        self.start_dsh_button = QPushButton("启动 DSH")
        self.start_dsh_button.setObjectName("primaryButton")
        self.start_dsh_button.clicked.connect(self.start_dsh)
        self.restart_dsh_button = QPushButton("重启 DSH")
        self.restart_dsh_button.clicked.connect(self.restart_dsh)
        self.stop_dsh_button = QPushButton("关闭 DSH")
        self.stop_dsh_button.setObjectName("dangerButton")
        self.stop_dsh_button.clicked.connect(self.stop_dsh)
        self.open_dsh_button = QPushButton("打开本地界面")
        self.open_dsh_button.clicked.connect(self.open_dsh)
        for button in (self.start_dsh_button, self.restart_dsh_button, self.stop_dsh_button, self.open_dsh_button):
            control_row.addWidget(button)
        controls_box.addLayout(control_row)
        self.guard_check = QCheckBox("启用运行期间守护（每 30 秒检查一次端口）")
        self.guard_check.setChecked(self._guard_enabled)
        self.guard_check.toggled.connect(self.toggle_guard)
        controls_box.addWidget(self.guard_check)
        layout.addWidget(controls)

        info, info_box = card("当前连接", "新版会保留旧 Harness 和旧 .dsh 作为来源；部署新环境会把路径映射到你选择的新目录。")
        self.manage_paths = QLabel("")
        self.manage_paths.setObjectName("muted")
        self.manage_paths.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.manage_paths.setWordWrap(True)
        info_box.addWidget(self.manage_paths)
        layout.addWidget(info)
        layout.addStretch(1)
        self._refresh_manage_state()
        return page

    def _dsh_port(self) -> int:
        return int(self.cfg.get("ports", {}).get("dsh", 3025) or 3025)

    def _refresh_manage_state(self) -> None:
        if not hasattr(self, "manage_status"):
            return
        port = self._dsh_port()
        live = port_is_open(port)
        self.manage_status.setText("运行中" if live else "已停止")
        self.manage_status.setStyleSheet("color:#10805e" if live else "color:#748196")
        self.manage_port.setText(f"127.0.0.1:{port}")
        self.manage_script.setText(f"DSH 启动脚本：{self._managed_start_script}")
        self.manage_paths.setText(
            f"Harness：{self.cfg.get('paths', {}).get('checkout', '')}\n"
            f".dsh：{self.cfg.get('paths', {}).get('dsh_home', '')}\n"
            f"pnpm store：{self.cfg.get('paths', {}).get('pnpm_store', '')}"
        )
        self.start_dsh_button.setEnabled(not live)
        self.stop_dsh_button.setEnabled(live)
        self.restart_dsh_button.setEnabled(True)

    def _make_managed_start_script(self) -> Path:
        checkout = Path(str(self.cfg.get("paths", {}).get("checkout") or ""))
        source = checkout / "start.cmd"
        target = self.data_dir / "start-dsh-3025.cmd"
        if not source.is_file():
            return Path(str(self.cfg.get("paths", {}).get("dsh_cmd") or ""))
        text = source.read_text(encoding="utf-8", errors="replace")
        needle = 'set "DSH_CHECKOUT=%~dp0"'
        replacement = f'set "DSH_CHECKOUT={checkout}"'
        if needle not in text:
            return source
        text = text.replace(needle, replacement)
        port = self._dsh_port()
        text = text.replace("3080", str(port))
        text = re.sub(
            r'(?m)^(call "%PNPM%" dsh web)(?![^\r\n]*--port)([^\r\n]*)$',
            lambda match: f"{match.group(1)} --port {port}{match.group(2)}",
            text,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        return target

    def start_dsh(self) -> None:
        port = self._dsh_port()
        if port_is_open(port):
            self._refresh_manage_state()
            return
        script = self._managed_start_script
        if not script.is_file():
            QMessageBox.warning(self, "找不到启动脚本", f"无法找到 DSH 启动入口：\n{script}")
            return
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", str(script)],
                cwd=str(script.parent), close_fds=True,
            )
            self._guard_starting_until = time.monotonic() + 90
            self.job_text.setText(f"已启动 DSH，等待 {port} 端口就绪…")
        except OSError as exc:
            QMessageBox.critical(self, "启动失败", str(exc))
        self._refresh_manage_state()

    def stop_dsh(self, *, confirm: bool = True) -> None:
        port = self._dsh_port()
        pids = self._port_pids(port)
        if not pids:
            self._refresh_manage_state()
            return
        if confirm and QMessageBox.question(
            self, "关闭 DSH", f"将结束监听 {port} 端口的进程树：{pids}\n当前会话会断开，继续？",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        failures = []
        for pid in pids:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
            if result.returncode != 0 and "not found" not in (result.stderr or "").lower():
                failures.append((result.stderr or result.stdout or "").strip())
        self._guard_starting_until = 0
        if failures:
            QMessageBox.warning(self, "关闭未完成", "\n".join(failures))
        self._refresh_manage_state()

    def restart_dsh(self) -> None:
        if QMessageBox.question(
            self, "重启 DSH", f"将重启网关并在端口 {self._dsh_port()} 重新启动。当前会话会断开，继续？",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self.stop_dsh(confirm=False)
        QTimer.singleShot(900, self.start_dsh)

    def open_dsh(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{self._dsh_port()}")

    def toggle_guard(self, enabled: bool) -> None:
        self._guard_enabled = bool(enabled)
        self._write_guard(enabled)
        if enabled:
            self._guard_tick()

    def _guard_tick(self) -> None:
        if not self._guard_enabled or self.worker is not None:
            return
        if time.monotonic() < self._guard_starting_until:
            return
        if not port_is_open(self._dsh_port()):
            self.start_dsh()

    def _read_guard(self) -> bool:
        path = self.data_dir / "guardian.json"
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("enabled"))
        except (OSError, json.JSONDecodeError, AttributeError):
            return False

    def _write_guard(self, enabled: bool) -> None:
        path = self.data_dir / "guardian.json"
        path.write_text(json.dumps({"enabled": bool(enabled)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _port_pids(self, port: int) -> list[int]:
        script = f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue).OwningProcess"
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted({int(value) for value in result.stdout.split() if value.isdigit()})

    # ---------- backup actions and automatic schedule ----------

    def start_backup(self, kind: str) -> None:
        labels = {"baseline": "软件基线", "sessions": "会话", "workspace": "工作区", "store": "pnpm store"}
        if kind == "store":
            operation = lambda progress: engine.mirror_store(
                self.cfg, self.backup_root, progress=progress, logger=self.logger,
            )
        else:
            operation = lambda progress: engine.backup_kind(
                self.cfg, self.backup_root, kind, progress=progress, logger=self.logger,
            )
        self._start_job(f"备份{labels.get(kind, kind)}", operation, "backup")

    def toggle_pause(self, paused: bool) -> None:
        engine.write_pause(self.backup_root, bool(paused), logger=self.logger)
        self.auto_status.setText("自动备份已暂停" if paused else "自动备份已恢复")

    def _auto_backup_tick(self) -> None:
        try:
            pause = engine.read_pause(self.backup_root)
            paused = bool((pause or {}).get("paused"))
            if self.pause_auto.isChecked() != paused:
                self.pause_auto.blockSignals(True)
                self.pause_auto.setChecked(paused)
                self.pause_auto.blockSignals(False)
            if paused or self.worker is not None:
                return
            now = time.monotonic()
            due = engine.due_kinds(self.cfg, self.backup_root)
            for kind in due:
                if now - self._auto_attempted.get(kind, 0) < 900:
                    continue
                self._auto_attempted[kind] = now
                self.start_backup(kind)
                break
        except Exception as exc:  # noqa: BLE001
            self.job_text.setText(f"自动备份调度读取失败：{exc}")

    # ---------- worker lifecycle ----------

    def _start_job(self, label: str, operation, job_type: str) -> None:
        if self.worker is not None:
            QMessageBox.information(self, "任务正在进行", "当前任务完成后再开始下一项。")
            return
        self.job_label = label
        self.job_started = time.monotonic()
        self.job_text.setText(f"{label}…")
        self.job_detail.clear()
        self.job_progress.setValue(0)
        self.job_progress.show()
        self.worker = JobThread(operation, self)
        self.worker.completed.connect(lambda result: self._job_completed(result, job_type))
        self.worker.failed.connect(self._job_failed)
        self.worker.start()
        self._update_controls()

    def _poll_job(self) -> None:
        if self.worker is None:
            return
        state = self.worker.progress.snapshot()
        value = max(0, min(100, int(state.get("pct") or 0)))
        self.job_progress.setValue(value)
        phase = state.get("phase") or self.job_label
        detail = state.get("detail") or ""
        self.job_text.setText(f"{self.job_label} · {phase}")
        elapsed = int(time.monotonic() - self.job_started)
        self.job_detail.setText(f"{detail}   ·   {elapsed // 60:02d}:{elapsed % 60:02d}" if detail else f"运行 {elapsed // 60:02d}:{elapsed % 60:02d}")

    def _job_completed(self, result: dict, job_type: str) -> None:
        label = self.job_label
        self.worker = None
        self.job_progress.hide()
        self.job_detail.clear()
        ok = bool(result.get("ok"))
        message = result.get("reason") or result.get("message") or ("完成" if ok else "未完成")
        self.job_text.setText(f"{label}：{message}")
        self._update_controls()
        self.refresh_all()
        if job_type in {"restore", "deploy"}:
            QMessageBox.information(self, label, self._format_job_result(result))

    def _job_failed(self, message: str) -> None:
        label = self.job_label
        self.worker = None
        self.job_progress.hide()
        self.job_text.setText(f"{label}失败")
        self.job_detail.clear()
        self._update_controls()
        self.refresh_all()
        QMessageBox.critical(self, f"{label}失败", message)

    def _format_job_result(self, result: dict) -> str:
        if result.get("dataReady") and "finish" in result:
            finish = result["finish"] or {}
            lines = ["数据已恢复。", ""]
            store_sync = finish.get("storeSync")
            if store_sync:
                lines.append(
                    f"pnpm store：{'已准备' if store_sync.get('ok') else '准备失败'} · "
                    f"{store_sync.get('dest') or ''}"
                )
            install = finish.get("install")
            if install:
                lines.append(f"依赖安装：{'成功' if install.get('ok') else '失败'}")
                if install.get("tail"):
                    lines.append(str(install["tail"]))
            else:
                lines.append("依赖安装：未运行（pnpm 未找到或 Harness 未包含 package.json）")
            links = finish.get("links")
            lines.append(f"目录联接：{'成功' if links is None or links.get('ok') else '失败'}")
            lines.append(f"启动脚本：{finish.get('launcher') or '未生成'}")
            lines.append("\n环境状态：可直接启动" if result.get("environmentReady") else "\n环境状态：数据已恢复，但收尾步骤未全部通过")
            return "\n".join(lines)
        if result.get("mode"):
            lines = ["恢复成功。" if result.get("ok") else "恢复未完整成功。"]
            lines.append(f"类型：{kind_label(result.get('kind', ''))}")
            lines.append(f"文件：{result.get('files', 0):,} / {result.get('expectFiles', 0):,}")
            if result.get("finish"):
                store_sync = result["finish"].get("storeSync") or {}
                if store_sync:
                    lines.append(f"pnpm store：{'已准备' if store_sync.get('ok') else '准备失败'} · {store_sync.get('dest') or ''}")
                lines.append(f"启动脚本：{result['finish'].get('launcher') or '未生成'}")
            if result.get("reason"):
                lines.append(str(result["reason"]))
            return "\n".join(lines)
        if result.get("steps"):
            lines = ["部署结果：" + ("可启动" if result.get("ok") else "尚未完全就绪")]
            for step in result["steps"]:
                lines.append(f"{step.get('label')}: {'成功' if step.get('ok') else '失败'}")
                detail = step.get("result") or {}
                if detail.get("reason"):
                    lines.append(f"  原因：{detail['reason']}")
                for part in detail.get("parts") or []:
                    if part.get("ok") and part.get("filesMatch"):
                        continue
                    if part.get("files") is not None and part.get("expectFiles") is not None:
                        lines.append(
                            f"  {part.get('label', '文件块')}：实际 {int(part['files']):,} / "
                            f"预期 {int(part['expectFiles']):,} 个文件"
                        )
                    if part.get("error"):
                        lines.append(f"  7z：{part['error']}")
            if result.get("reason"):
                reason = str(result["reason"])
                if not any(reason in line for line in lines):
                    lines.append(reason)
            return "\n".join(lines)
        return str(result.get("message") or result.get("reason") or result)

    def _update_controls(self) -> None:
        busy = self.worker is not None
        for button in getattr(self, "backup_buttons", {}).values():
            button.setEnabled(not busy)
        for name in ("quick_baseline", "quick_sessions", "quick_workspace", "quick_store"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(not busy)
        if hasattr(self, "deploy_button"):
            self.deploy_button.setEnabled(not busy)
        if hasattr(self, "restore_button"):
            self._update_restore_enabled()

    # ---------- state refresh ----------

    def refresh_all(self) -> None:
        self._refresh_gateway_badge()
        self._refresh_overview()
        self._refresh_backup_table()
        self._refresh_deploy_slots()
        self._refresh_manage_state()

    def _refresh_gateway_badge(self) -> None:
        port = self._dsh_port()
        if port_is_open(port):
            self.gateway_badge.setText(f"●  DSH 运行中 · {port}")
            self.gateway_badge.setStyleSheet("color:#117a5b;background:#e4f4ed;border:1px solid #c6e8d9;border-radius:14px;padding:8px 12px;font-size:12px;font-weight:600")
        else:
            self.gateway_badge.setText(f"●  DSH 已停止 · {port}")
            self.gateway_badge.setStyleSheet("color:#768397;background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:8px 12px;font-size:12px")

    def _refresh_overview(self) -> None:
        try:
            status = engine.status_summary(self.cfg, self.backup_root)
            kinds = status.get("kinds", {})
            baseline = kinds.get("baseline", {}).get("latest") or {}
            sessions = kinds.get("sessions", {}).get("latest") or {}
            self.stat_baseline.value_label.setText(format_time(baseline.get("createdAt")) if baseline else "暂无")
            self.stat_baseline.note_label.setText(f"{human_bytes(baseline.get('bytes'))} · {'完整' if baseline.get('verifiedOk') else '需要检查'}" if baseline else "尽快创建第一份基线")
            self.stat_sessions.value_label.setText(format_time(sessions.get("createdAt")) if sessions else "暂无")
            self.stat_sessions.note_label.setText(f"{int(kinds.get('sessions', {}).get('count') or 0)} 份 · 每 15 分钟自动备份" if sessions else "还没有会话快照")
            free = float(status.get("diskFreeGb") or 0)
            used = int(status.get("libraryBytes") or 0)
            self.stat_storage.value_label.setText(human_bytes(used))
            self.stat_storage.note_label.setText(f"备份盘剩余 {free:.1f} GB · 保留上限 15 天")
            workspace_count = int(kinds.get("workspace", {}).get("count") or 0)
            store_latest = kinds.get("store", {}).get("latest") or {}
            self.stat_workspace.value_label.setText(f"{workspace_count} 份快照")
            self.stat_workspace.note_label.setText("按需手动创建，不参与自动轮转")
            running = port_is_open(self._dsh_port())
            self.hero_status.setText("DSH 网关正在运行" if running else "DSH 网关当前已停止")
            self.hero_detail.setText(f"本机地址 127.0.0.1:{self._dsh_port()} · 启动入口 {self._managed_start_script.name}")
            self.recent_activity.setText(
                f"最近基线：{format_time(baseline.get('createdAt')) if baseline else '暂无'}\n"
                f"最近会话：{format_time(sessions.get('createdAt')) if sessions else '暂无'}\n"
                f"pnpm store：{format_time(store_latest.get('createdAt')) if store_latest else '未同步'}\n"
                f"工作区历史：{workspace_count} 份"
            )
            pause = engine.read_pause(self.backup_root)
            paused = bool((pause or {}).get("paused"))
            if self.pause_auto.isChecked() != paused:
                self.pause_auto.blockSignals(True)
                self.pause_auto.setChecked(paused)
                self.pause_auto.blockSignals(False)
            self.auto_status.setText("自动备份已暂停" if paused else "自动备份运行中 · 会话 15 分钟 / 基线每日")
        except Exception as exc:  # noqa: BLE001
            self.recent_activity.setText(f"读取备份状态失败：{exc}")

    # ---------- helpers ----------

    @staticmethod
    def _kind_label(kind: str) -> str:
        return kind_label(kind)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None:
            choice = QMessageBox.question(
                self, "任务仍在运行", "关闭窗口会让当前操作失去监控。仍然退出？",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if choice != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def kind_label(kind: str) -> str:
    return {"baseline": "软件基线", "sessions": "会话", "workspace": "工作区"}.get(kind, kind)
