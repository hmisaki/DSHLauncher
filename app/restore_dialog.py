"""恢复对话框：选一份包 → 填不填目标目录 → 恢复。

一句话规则：**目标目录留空 = 恢复到原来的位置；填了 = 恢复到那个目录**。
没有别的模式开关。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.humanize import human_ts
from dshbackup import engine, manifest
from dshbackup.config import human_slot, kind_label
from dshbackup.sevenzip import human_bytes

OK_GREEN = "#0f7a3d"
DANGER_RED = "#c0392b"


class RestoreDialog(QDialog):
    def __init__(self, cfg: dict, root: Path, kind: str, slot: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"取回备份 · {kind_label(kind)} · {human_slot(slot)}")
        self.setMinimumWidth(780)
        self.cfg = cfg
        self.root = root
        self.kind = kind
        self.slot = slot
        # 交给调用方的东西只有一样：into（None = 回原位置）
        self.result_payload: dict | None = None

        self.preview = engine.restore_slot(cfg, root, kind, slot, dry_run=True)

        full_ts = datetime.fromtimestamp(manifest.slot_ms(slot) / 1000)
        title = QLabel(
            f"<b>取回 {kind_label(kind)}</b> · {human_ts(manifest.slot_ms(slot))}"
            f" · {human_bytes(int(self.preview.get('sourceBytes', 0) or 0))}"
            f" · {int(self.preview.get('files', 0)):,} 文件"
        )
        title.setStyleSheet("font-size:15px")
        sub = QLabel(
            f"完整时间 {full_ts:%Y-%m-%d %H:%M:%S}　·　"
            f"目录联接 {self.preview.get('links', 0)} 条 · "
            f"空目录 {self.preview.get('emptyDirs', 0)} 个"
        )
        sub.setStyleSheet("color:#555")

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["块", "包文件", "现在的目录", "文件数", "状态"])
        self.table.verticalHeader().setVisible(False)
        for p in self.preview.get("parts", []):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(p.get("label"))))
            self.table.setItem(r, 1, QTableWidgetItem(str(p.get("zipName"))))
            it_t = QTableWidgetItem(str(p.get("target")))
            it_t.setToolTip(str(p.get("target")))
            self.table.setItem(r, 2, it_t)
            it_n = QTableWidgetItem(f"{p.get('sourceFiles', 0):,}")
            it_n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 3, it_n)
            self.table.setItem(r, 4, self._status_item(p))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        # 唯一的选项：目标目录。留空＝回原位置。
        self.dest_edit = QLineEdit("")
        self.dest_edit.setPlaceholderText("留空 = 恢复到原来的位置；填一个空目录 = 恢复到那里")
        self.dest_edit.textChanged.connect(self._refresh_warn)
        self.dest_btn = QPushButton("选择…")
        self.dest_btn.setFixedWidth(80)
        self.dest_btn.clicked.connect(self._pick_dest)
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("恢复到"))
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(self.dest_btn)

        self.warn = QLabel("")
        self.warn.setWordWrap(True)

        self.chk_finish = QCheckBox("恢复到别处时，顺手把依赖装好、链接建好（开包即用）")
        self.chk_finish.setChecked(True)
        self.chk_finish.setToolTip(
            "勾上：解包后自动跑 pnpm install --offline 建 node_modules、跑 restore-links.cmd 建目录联接，"
            "再写一份 start-dsh.cmd 放进 harness 目录（全部相对定位，直接双击就能起网关）。"
            "收尾脚本跑完即删。\n"
            "不留空目标（写回原位置）时本项无意义，会自动忽略。"
        )

        self.btn_check = QPushButton("检查包")
        self.btn_check.setToolTip("只读：核实每个包文件是否在磁盘上、大小是否与清单一致，不动任何真实路径")
        self.btn_check.clicked.connect(self._check_packs)
        self.lbl_check = QLabel("")
        self.lbl_check.setWordWrap(True)
        check_row = QHBoxLayout()
        check_row.addWidget(self.btn_check)
        check_row.addWidget(self.lbl_check, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_go = QPushButton("恢复")
        self.btn_go.setDefault(True)
        self.btn_go.clicked.connect(self._confirm)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_go)

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(self.table, 1)
        lay.addLayout(check_row)
        lay.addLayout(dest_row)
        lay.addWidget(self.chk_finish)
        lay.addWidget(self.warn)
        lay.addLayout(btn_row)

        self._refresh_warn()

    def focus_primary(self) -> None:
        self.btn_go.setFocus()

    # ---------- 界面 ----------

    def _pick_dest(self) -> None:
        got = QFileDialog.getExistingDirectory(self, "选一个空目录（包会铺到它下面）", "")
        if got:
            self.dest_edit.setText(str(Path(got)))

    @staticmethod
    def _status_item(p: dict) -> QTableWidgetItem:
        if not p.get("onDisk"):
            it = QTableWidgetItem("文件已丢失")
            it.setForeground(QColor(DANGER_RED))
            it.setToolTip("这个包文件在磁盘上找不到，这份备份不完整，无法完整恢复。")
        elif not p.get("verifiedOk"):
            it = QTableWidgetItem("校验失败")
            it.setForeground(QColor(DANGER_RED))
            it.setToolTip("包文件在，但大小或校验值与清单不符，可能不完整。")
        else:
            it = QTableWidgetItem("可恢复")
            it.setForeground(QColor(OK_GREEN))
            it.setToolTip("包文件在，大小与清单一致")
        return it

    def _plan(self) -> list[tuple[str, str]]:
        """填了目标时，每块会落到哪（照 engine 的 into 规则算一遍，仅用于提示）。"""
        dest = self.dest_edit.text().strip()
        if not dest:
            return []
        base = Path(dest)
        out = []
        for p in self.preview.get("parts", []):
            nm = Path(str(p.get("target", ""))).name or str(p.get("label"))
            if self.kind == "sessions":
                where = base / nm
            elif self.kind == "workspace":
                where = base / nm
            else:
                where = base / nm
            out.append((str(p.get("label")), str(where)))
        return out

    def _refresh_warn(self) -> None:
        dest = self.dest_edit.text().strip()
        if hasattr(self, "chk_finish"):
            self.chk_finish.setEnabled(bool(dest))
        if dest:
            plan = self._plan()
            lines = "\n".join(f"　· {lb} → {w}" for lb, w in plan)
            tail = ("解完自动装依赖、建链接，并写一份 start-dsh.cmd 到 harness 目录，"
                    "收尾脚本跑完即删。"
                    if self.chk_finish.isChecked() else
                    "装依赖和建链接要自己来（restore-links.cmd 会留着）。")
            self.warn.setText(
                f"铺到指定目录（原盘一个字节都不动）：\n{lines}\n"
                f"目标目录必须是空的。{tail}"
            )
            self.warn.setStyleSheet("color:#3d2c00; font-size:13px;")
            self.btn_go.setStyleSheet("font-weight:600;padding:4px 14px;border:1px solid #0f7a3d")
        else:
            targets = "、".join(str(p.get("target")) for p in self.preview.get("parts", []))
            self.warn.setText(
                f"⚠ 会覆盖原来的位置：{targets}\n"
                "旧目录先改名成「<名字>.pre-restore-<时间>」留底（不删东西），"
                "失败会自动回滚；要撤就把新目录删掉、把留底那份改回原名。"
            )
            self.warn.setStyleSheet(
                "color:#ffffff; background:#b3261e; font-weight:600;"
                " font-size:13px; padding:6px; border-radius:4px;"
            )
            self.btn_go.setStyleSheet(
                "color:#ffffff; background:#c0392b; font-weight:600;"
                " padding:4px 14px; border-radius:4px"
            )

    # ---------- 检查包 ----------

    def _check_packs(self) -> None:
        prev = engine.restore_slot(self.cfg, self.root, self.kind, self.slot, dry_run=True)
        parts = prev.get("parts", [])
        self.preview = prev
        for r, p in enumerate(parts):
            if r < self.table.rowCount():
                self.table.setItem(r, 4, self._status_item(p))

        missing = [p for p in parts if not p.get("onDisk")]
        bad = [p for p in parts if p.get("onDisk") and not p.get("verifiedOk")]
        detail = "\n\n".join(
            f"{p.get('label')} · {p.get('zipName')}\n"
            f"    要解到：{p.get('target')}\n"
            f"    文件数：{int(p.get('sourceFiles', 0)):,}　"
            f"包大小：{human_bytes(int(p.get('bytes', 0)))}"
            for p in parts
        )
        if missing:
            head = f"✗ {len(missing)}/{len(parts)} 个包文件已丢失，这份恢复做不完整。"
        elif bad:
            head = f"✗ {len(bad)}/{len(parts)} 个包校验不通过，建议重新备份再用。"
        else:
            head = (f"✓ {len(parts)} 个包都在，共 {int(prev.get('files', 0)):,} 个文件，"
                    f"{human_bytes(int(prev.get('sourceBytes', 0)))}，可以恢复。")
        self.lbl_check.setText(head)
        self.lbl_check.setStyleSheet(
            f"color:{DANGER_RED};font-weight:600" if (missing or bad) else f"color:{OK_GREEN};font-weight:600"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if (missing or bad) else QMessageBox.Information)
        box.setWindowTitle("检查包")
        box.setText(head)
        box.setInformativeText("这次检查只读包文件，没有改动任何真实路径。")
        box.setDetailedText(detail[:8000])
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()

    # ---------- 确认 ----------

    def _confirm(self) -> None:
        dest = self.dest_edit.text().strip()

        if dest:
            base = Path(dest)
            if not base.is_absolute():
                QMessageBox.warning(self, "路径不对", "目标目录请填一个绝对路径。")
                return
            if base.exists() and any(base.iterdir()):
                QMessageBox.warning(
                    self, "目标目录不是空的",
                    f"{base}\n\n恢复到别处要求目标为空目录，换一个位置。",
                )
                return
            plan = "\n".join(f"  · {lb} → {w}" for lb, w in self._plan())
            tail = ("\n\n解完会自动装依赖、建链接，再在 harness 目录里写一份 start-dsh.cmd"
                    "（全部相对定位，双击就能起网关）。"
                    if self.chk_finish.isChecked()
                    else "\n\n装依赖和建链接要自己来（restore-links.cmd 会留着）。")
            ask = QMessageBox(self)
            ask.setIcon(QMessageBox.Information)
            ask.setWindowTitle("恢复到指定目录")
            ask.setText("会把包铺到下面这些位置（原盘不动）：")
            ask.setInformativeText(f"{plan}{tail}")
            ask.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
            ask.button(QMessageBox.Ok).setText("开始")
            ask.button(QMessageBox.Cancel).setText("算了")
            if ask.exec() != QMessageBox.Ok:
                return
            self.result_payload = {"into": str(base),
                                   "auto_finish": bool(self.chk_finish.isChecked())}
            self.accept()
            return

        # 留空 → 覆盖原来位置，要打字确认
        targets = "\n".join(f"  · {p.get('target')}" for p in self.preview.get("parts", []))
        ask = QInputDialog(self)
        ask.setWindowTitle("确认恢复回原位置")
        ask.setLabelText(
            "这会把包解到下面这些原来的位置，覆盖当前内容（旧目录改名留底）：\n"
            f"{targets}\n\n"
            "要撤：删掉新目录，把 <名字>.pre-restore-<时间> 改回原名。\n\n"
            "确认请在下面输入「恢复」两个字："
        )
        ask.setTextValue("")
        ask.setOkButtonText("恢复")
        ask.setCancelButtonText("不恢复")
        if ask.exec() != QInputDialog.Accepted:
            return
        if ask.textValue().strip() != "恢复":
            return
        self.result_payload = {"into": None}
        self.accept()
