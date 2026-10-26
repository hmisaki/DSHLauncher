"""设置对话框：备份根路径、三个端口、三个命令槽、7z/robocopy 路径、压缩级别。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.humanize import human_gb, human_span_long, human_ts
from dshbackup import config as cfgmod
from dshbackup import engine, manifest


class _PathRow(QWidget):
    def __init__(self, value: str, pick_dir: bool = True) -> None:
        super().__init__()
        self.edit = QLineEdit(value)
        self.btn = QPushButton("浏览…")
        self.btn.setFixedWidth(72)
        self.btn.clicked.connect(lambda: self._pick(pick_dir))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        lay.addWidget(self.btn)

    def _pick(self, pick_dir: bool) -> None:
        if pick_dir:
            got = QFileDialog.getExistingDirectory(self, "选择目录", self.edit.text())
        else:
            got, _ = QFileDialog.getOpenFileName(self, "选择文件", self.edit.text())
        if got:
            self.edit.setText(got)

    def value(self) -> str:
        return self.edit.text().strip()


class _WsRow(QWidget):
    """一行工作区：名字 + 目录 + 浏览 + 移除。"""

    def __init__(self, label: str, path: str, on_remove=None) -> None:
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.ed_label = QLineEdit(label)
        self.ed_label.setPlaceholderText("名字（包里的顶层目录名）")
        self.ed_label.setFixedWidth(190)
        self.ed_label.setToolTip("这个名字就是 7z 包里的顶层目录名，改它不影响备份内容。")
        self.ed_path = QLineEdit(path)
        self.ed_path.setPlaceholderText("绝对路径，例如 D:\\AIBot\\WorkSpace")
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setFixedWidth(72)
        self.btn_browse.clicked.connect(self._pick)
        self.btn_del = QPushButton("移除")
        self.btn_del.setFixedWidth(52)
        self.btn_del.setToolTip("从列表里去掉这一项（不动磁盘上的目录）")
        if on_remove is not None:
            self.btn_del.clicked.connect(lambda: on_remove(self))
        h.addWidget(self.ed_label)
        h.addWidget(self.ed_path, 1)
        h.addWidget(self.btn_browse)
        h.addWidget(self.btn_del)

    def _pick(self) -> None:
        got = QFileDialog.getExistingDirectory(self, "选工作区目录", self.ed_path.text() or "")
        if got:
            self.ed_path.setText(str(Path(got)))
            if not self.ed_label.text().strip():
                self.ed_label.setText(Path(got).name)

    def data(self) -> dict:
        return {
            "label": self.ed_label.text().strip(),
            "path": self.ed_path.text().strip(),
        }


class _WsList(QWidget):
    """工作区列表：想加几个加几个，全部打进同一个 7z 包。"""

    def __init__(self, roots: list[dict], on_change=None) -> None:
        super().__init__()
        self._on_change = on_change
        self.rows: list[_WsRow] = []
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(4)
        self.btn_add = QPushButton("+ 添加工作区")
        self.btn_add.clicked.connect(lambda: self.add("", ""))
        self.v.addWidget(self.btn_add)
        for r in roots or []:
            p = str(r.get("path") or "")
            lb = str(r.get("label") or "")
            if p:
                self.add(lb or Path(p).name, p)
        if not self.rows:
            self.add("", "")

    def add(self, label: str, path: str) -> None:
        row = _WsRow(label, path, on_remove=self.remove)
        self.v.insertWidget(self.v.count() - 1, row)
        self.rows.append(row)
        if self._on_change is not None:
            row.ed_path.textChanged.connect(lambda _t: self._on_change())
            row.ed_label.textChanged.connect(lambda _t: self._on_change())

    def remove(self, row: "_WsRow") -> None:
        if len(self.rows) <= 1:
            row.ed_label.clear()
            row.ed_path.clear()
            return
        self.rows.remove(row)
        self.v.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        if self._on_change is not None:
            self._on_change()

    def items(self) -> list[dict]:
        out: list[dict] = []
        for r in self.rows:
            d = r.data()
            if not d["path"]:
                continue
            out.append({
                "label": d["label"] or Path(d["path"]).name,
                "path": d["path"],
                "exclude_dirs": [],
            })
        return out


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(720)
        self.cfg = cfg
        self.root = root
        self._notes: dict[str, tuple[QLabel, str, QWidget]] = {}

        paths = cfg.get("paths", {})
        tools = cfg.get("tools", {})
        ports = cfg.get("ports", {})
        cmds = cfg.get("commands", {})
        comp = cfg.get("compress", {})

        # U-07：四组标题把 13 个字段切开；每一项底下给一行「这是什么」，
        # 只写字段名（DSH_HOME / pnpm store）等于让用户猜——设置页原来就是这个毛病。
        def make_group(title: str, collapsed: bool = False) -> tuple[QGroupBox, QFormLayout]:
            box = QGroupBox(title)
            f = QFormLayout(box)
            f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
            if collapsed:
                box.setCheckable(True)
                box.setChecked(False)
                # 折叠：勾上才显示内容（默认收起，免得设置页一开就是三屏）
                box.toggled.connect(lambda on, inner=box: (_show_group(inner, on)))
            return box, f

        def _show_group(box: QGroupBox, on: bool) -> None:
            """折叠组：只隐藏内容控件，标题行（勾选框）始终留一条。"""
            lay = box.layout()
            for i in range(lay.count()):
                item = lay.itemAt(i)
                w = item.widget() if item else None
                if w is not None:
                    w.setVisible(on)
            box.updateGeometry()

        def note(text: str) -> QLabel:
            lb = QLabel(text)
            lb.setWordWrap(True)
            lb.setStyleSheet("color:#5a5a5a;font-size:12px")
            return lb

        def add_row(f: QFormLayout, label: str, widget: QWidget, text: str) -> None:
            holder = QWidget()
            v = QVBoxLayout(holder)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)
            v.addWidget(widget)
            lb = note(text)
            v.addWidget(lb)
            f.addRow(label, holder)
            # U-07：把说明标签留下来，填错时原地改成红字说明（不攒到点保存再弹一整页）
            self._notes[label] = (lb, text, widget)

        self.root_row = _PathRow(str(paths.get("backup_root", root)))
        self.dsh_row = _PathRow(str(paths.get("dsh_home", "")))
        self.ck_row = _PathRow(str(paths.get("checkout", "")))
        self.store_row = _PathRow(str(paths.get("pnpm_store", "")))
        self.ws_list = _WsList(
            list(cfg.get("sets", {}).get(cfgmod.KIND_WORKSPACE, {}).get("roots", [])),
            on_change=lambda: self._live_check(deep=False),
        )
        self.sz_row = _PathRow(str(tools.get("seven_zip", "")), pick_dir=False)
        # U-16：包进 _PathRow，与其他 11 行同宽同对齐（原来裸 QLineEdit，
        # 右边缘少一个 72px 的按钮位，看着像漏了控件）
        self.rb_row = _PathRow(str(tools.get("robocopy", "robocopy")), pick_dir=False)

        g_path, f_path = make_group("备份哪些东西 · 放在哪")
        add_row(f_path, "备份根目录", self.root_row,
                "所有包、清单、日志都放这里。换根不会自动搬旧包，旧包留在原处。")
        add_row(f_path, "DSH_HOME", self.dsh_row,
                "DSH 的运行时目录（含 profiles / sessions / skills），改坏 DSH 就从这里退。")
        add_row(f_path, "源码 checkout", self.ck_row,
                "DSH 源码目录；只备份其中的配置，不打包 node_modules。")
        add_row(f_path, "pnpm store", self.store_row,
                "全局 pnpm 缓存，体积大、内容可从网上重下，所以单独一档、只复制不打包。")
        add_row(f_path, "工作区", self.ws_list,
                "要备份的目录，可自由增删。全部打进同一个 7z 包，包里按名字分层；"
                "子目录（含嵌套的工作区）递归包含，不用单独加。名字重复会被拒绝。不自动跑，只能手动备份。")

        self.p_dsh = QSpinBox()
        self.p_mi = QSpinBox()
        self.p_vb = QSpinBox()
        for sb, key in ((self.p_dsh, "dsh"), (self.p_mi, "mediainbox"), (self.p_vb, "voicebox")):
            sb.setRange(1, 65535)
            sb.setValue(int(ports.get(key, 0)))
        port_row = QWidget()
        pl = QHBoxLayout(port_row)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(QLabel("DSH")); pl.addWidget(self.p_dsh)
        pl.addSpacing(12)
        pl.addWidget(QLabel("MediaInbox")); pl.addWidget(self.p_mi)
        pl.addSpacing(12)
        pl.addWidget(QLabel("VoiceBox")); pl.addWidget(self.p_vb)
        pl.addStretch(1)

        g_port, f_port = make_group("端口（只用于健康检查）")
        add_row(f_port, "端口", port_row,
                "这三个数字只用来探测「服务在不在」，改它不会改服务本身的监听端口。")

        self.c_start = QLineEdit(str(cmds.get("start_cmd", "")))
        self.c_start.setPlaceholderText("留空 = 面板不提供「启动环境」")
        self.c_stop = QLineEdit(str(cmds.get("stop_cmd", "")))
        self.c_stop.setPlaceholderText("留空 = 面板不提供「关闭环境」")
        self.c_restart = QLineEdit(str(cmds.get("restart_cmd", "")))
        self.c_restart.setPlaceholderText("例：Start-ScheduledTask -TaskName 'DSH Restart Now'")

        g_cmd, f_cmd = make_group("环境命令（面板上的按钮原样执行它们）")
        add_row(f_cmd, "启动命令", self.c_start,
                "点「启动环境」时执行；留空则该按钮不出现。")
        add_row(f_cmd, "关闭命令", self.c_stop,
                "点「关闭环境」时执行；留空则该按钮不出现。")
        add_row(f_cmd, "重启命令", self.c_restart,
                "本机固定用计划任务重启：Start-ScheduledTask -TaskName 'DSH Restart Now'。"
                "别改成直接 start，那样重启后网关活不下来。")

        self.level = QSpinBox()
        self.level.setRange(0, 9)
        self.level.setValue(int(comp.get("level", 5)))

        g_tool, f_tool = make_group("外部工具与压缩")
        add_row(f_tool, "7-Zip 路径", self.sz_row,
                "必须指向 7z.exe：打包、写清单、解包、校验都靠它。")
        # U-16：这一行原来只写「robocopy」，不说哪里用、填错什么时候报
        add_row(f_tool, "robocopy", self.rb_row,
                "可填命令名（走系统 PATH）或绝对路径。只在「更新store」时用；"
                "命令名写错要等下次更新 store 才报错。仅在填绝对路径时会当场校验存在性。")
        add_row(f_tool, "7z 压缩级别", self.level,
                "0 = 只存储（最快、最大），9 = 最小（最慢）；默认 5，日常够用。")

        hint = QLabel("工具不自己造启动逻辑，只调上面三条命令。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666")

        # UX-15：界面一直在承诺「可回退 会话 15 天 / 基线 16 天」，设置里却一项都没有。
        # 这里补只读展示——不是不给改，而是改它属于改配置契约，不该在弹层里手滑改掉。
        g_keep, f_keep = make_group("保留策略（只读）")
        keep_lines = []
        total_gb = 0.0
        for kind in (cfgmod.KIND_SESSIONS, cfgmod.KIND_BASELINE):
            k = cfg.get("kinds", {}).get(kind, {})
            days = int(k.get("M", 0) or 0) / 1440
            slots = manifest.scan(root, kind)
            used = sum(int(s.get("bytes") or 0) for s in slots) / (1024 ** 3)
            total_gb += used
            keep_lines.append(
                f"{cfgmod.KIND_LABEL.get(kind, kind)}：保留 {days:.0f} 天 · "
                f"现 {len(slots)} 份 · 占 {used:.2f} GB"
            )
        keep_lines.append(f"合计占用 {total_gb:.2f} GB（不含工作区与store）")
        lbl_keep = QLabel("\n".join(keep_lines))
        lbl_keep.setWordWrap(True)
        add_row(f_keep, "当前策略", lbl_keep,
                "保留策略由配置文件决定，改它请编辑 config.json 里的 kinds.<类型>.c/D/M。")

        # 裁决 3：回收区必须在这里看得见——占用多少、最老一份多久以前、
        # 什么时候会被清掉。用户删了东西找不到，第一个会来这里翻。
        g_rec, f_rec = make_group("回收区（_deleted）", collapsed=True)
        rec = engine.recycle_stats(root)
        keep_days = engine.recycle_keep_days(cfg)
        if rec.get("count"):
            oldest = human_ts(rec.get("oldestAt")) if rec.get("oldestAt") else "不明"
            # R3-4 定稿：`占用 0.2 GB · 最老一份 10-21 14:02 · 保留 30 天`
            rec_line = (
                f"占用 {human_gb(int(rec['bytes']))} · 最老一份 {oldest} · "
                f"保留 {keep_days} 天\n"
                f"最老那份已放 {human_span_long(int(rec.get('oldestAgeMs') or 0))}；"
                f"超过 {keep_days} 天的在下次启动时自动清除"
            )
        else:
            rec_line = f"空的 · 保留 {keep_days} 天（删掉/清掉的备份先放这里，超期自动清除）"
        lbl_rec = QLabel(rec_line)
        lbl_rec.setWordWrap(True)
        add_row(f_rec, "当前占用", lbl_rec,
                f"位置：{Path(root) / cfgmod.DELETED_DIR}（想立刻腾地方，可以直接删里面的目录）")
        self._recycle_stats = rec

        # U-08：保存/取消用中文且说清动作
        box = QDialogButtonBox()
        self.btn_save = box.addButton("保存设置", QDialogButtonBox.AcceptRole)
        box.addButton("不改了", QDialogButtonBox.RejectRole)
        self.btn_save.setStyleSheet("font-weight:600")
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(g_path)
        lay.addWidget(g_port)
        lay.addWidget(g_cmd)
        lay.addWidget(g_tool)
        lay.addWidget(g_keep)
        lay.addWidget(g_rec)
        lay.addWidget(hint)
        lay.addStretch(1)
        lay.addWidget(box)

        # U-07：边填边校验——空值/非绝对路径即时标红，存在性在离开输入框时核一次
        for row in (self.root_row, self.dsh_row, self.ck_row, self.store_row, self.sz_row):
            row.edit.textChanged.connect(lambda _t: self._live_check(deep=False))
            row.edit.editingFinished.connect(lambda: self._live_check(deep=True))
        self.rb_row.edit.textChanged.connect(lambda _t: self._live_check(deep=False))
        self.rb_row.edit.editingFinished.connect(lambda: self._live_check(deep=True))
        self._live_check(deep=True)

    def _live_check(self, deep: bool) -> None:
        """把「填错了」标在那一行正下方，并据此决定保存按钮能不能点。"""
        import os

        if not hasattr(self, "btn_save"):
            return  # 构造期：列表控件刚建好就触发了 change，保存按钮还没出来

        checks: list[tuple[str, bool, str]] = []

        def abs_only(label: str, value: str, must_exist: bool, kind: str) -> None:
            if not value:
                checks.append((label, False, f"{label} 不能为空，点右边「浏览…」选一个{kind}。"))
                return
            if not os.path.isabs(value):
                checks.append((label, False,
                               f"要填绝对路径（例如 D:\\...），当前是 {value!r}。"))
                return
            if must_exist and deep and not Path(value).exists():
                checks.append((label, False, f"这个{kind}不存在：{value}"))
                return
            checks.append((label, True, ""))

        abs_only("备份根目录", self.root_row.value(), False, "目录")
        abs_only("DSH_HOME", self.dsh_row.value(), True, "目录")
        abs_only("源码 checkout", self.ck_row.value(), True, "目录")
        abs_only("pnpm store", self.store_row.value(), True, "目录")
        # 工作区是多行列表，逐行标红会把同一行说明覆盖掉，这里合并成一句
        ws_items = self.ws_list.items()
        if not ws_items:
            checks.append(("工作区", False, "至少加一个工作区目录。"))
        else:
            bad_ws = [it["path"] for it in ws_items
                      if not os.path.isabs(it["path"])
                      or (deep and not Path(it["path"]).exists())]
            if bad_ws:
                checks.append(("工作区", False, "这些目录有问题：" + "、".join(bad_ws)))
            else:
                checks.append(("工作区", True, ""))
        abs_only("7-Zip 路径", self.sz_row.value(), True, "文件")

        rb = self.rb_row.value()
        if not rb:
            checks.append(("robocopy", False,
                           "不能为空。留默认的 robocopy（走系统 PATH）即可。"))
        elif os.path.isabs(rb) and deep and not Path(rb).exists():
            checks.append(("robocopy", False, f"这个文件不存在：{rb}"))
        else:
            checks.append(("robocopy", True, ""))

        bad = 0
        for label, ok, why in checks:
            got = self._notes.get(label)
            if not got:
                continue
            lb, default, _w = got
            if ok:
                lb.setText(default)
                lb.setStyleSheet("color:#5a5a5a;font-size:12px")
            else:
                bad += 1
                lb.setText("✗ " + why)
                lb.setStyleSheet("color:#c0392b;font-size:12px;font-weight:600")
        self.btn_save.setEnabled(bad == 0)
        self.btn_save.setToolTip("" if bad == 0 else f"还有 {bad} 项要改（见红色说明）")

    def _save(self) -> None:
        cfg = self.cfg
        old_root = cfg["paths"].get("backup_root")

        cfg["paths"]["backup_root"] = self.root_row.value()
        cfg["paths"]["dsh_home"] = self.dsh_row.value()
        cfg["paths"]["checkout"] = self.ck_row.value()
        cfg["paths"]["pnpm_store"] = self.store_row.value()
        ws_items = self.ws_list.items()
        if ws_items:
            # paths.workspace 是老字段（占位符、别处引用），跟着第一个根走
            cfg["paths"]["workspace"] = ws_items[0]["path"]
        cfg.setdefault("sets", {}).setdefault(cfgmod.KIND_WORKSPACE, {})["roots"] = ws_items
        cfg["tools"]["seven_zip"] = self.sz_row.value()
        cfg["tools"]["robocopy"] = self.rb_row.value() or "robocopy"
        cfg["ports"] = {
            "dsh": self.p_dsh.value(),
            "mediainbox": self.p_mi.value(),
            "voicebox": self.p_vb.value(),
        }
        cfg["commands"]["start_cmd"] = self.c_start.text().strip()
        cfg["commands"]["stop_cmd"] = self.c_stop.text().strip()
        cfg["commands"]["restart_cmd"] = self.c_restart.text().strip()
        cfg["compress"]["level"] = self.level.value()

        errs = cfgmod.validate(cfg)
        if errs:
            QMessageBox.warning(self, "配置有问题", "\n".join(f"· {e}" for e in errs))
            return

        new_root = Path(cfg["paths"]["backup_root"])
        cfgmod.save(new_root, cfg)
        cfgmod.ensure_dirs(new_root, cfg)
        if old_root and str(old_root) != str(new_root):
            QMessageBox.information(
                self, "备份根已改",
                f"配置已写入新根：\n{new_root}\n\n主窗口的列表会在下次刷新时切过去。",
            )
        self.accept()


def commands_available(cfg: dict) -> dict:
    c = cfg.get("commands", {})
    return {
        "start": bool(str(c.get("start_cmd", "")).strip()),
        "stop": bool(str(c.get("stop_cmd", "")).strip()),
        "restart": bool(str(c.get("restart_cmd", "")).strip()),
    }
