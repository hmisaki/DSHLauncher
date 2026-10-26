"""四条备份线 + 时间轴。

四类备份各占一条泳道（store / 基线 / 会话 / 工作区），每一次备份在泳道上是一个点。
- 横向拖动 = 沿时间平移
- 滚轮 = 以光标位置为中心缩放（要看清楚某一天，或者拉远看整体）
- 点一下某个点 = 选中它（主窗口会同步表格里的那一行）
- 悬停 = 显示时间 / 大小 / 备注

只画点不画线：同一类里相邻两次备份之间没有"过程"，连成折线会让人误以为
中间也有东西。泳道本身就是"这条线"，点就是"在这个时刻备过一份"。
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

# 泳道顺序与主窗口四列保持一致（store → 基线 → 会话 → 工作区）
LANE_ORDER = ("store", "baseline", "sessions", "workspace")
LANE_LABEL = {
    "store": "store",
    "baseline": "基线",
    "sessions": "会话",
    "workspace": "工作区",
}
LANE_COLOR = {
    "store": "#7c7c80",
    "baseline": "#1a7f37",
    "sessions": "#1f6feb",
    "workspace": "#9a6700",
}

LANE_H = 30
PAD_L = 84
PAD_R = 14
PAD_T = 22
PAD_B = 10

MIN_SPAN_MS = 60_000           # 最近能缩到 1 分钟
FALLBACK_SPAN_MS = 86_400_000  # 只有一个点时铺一天

# 刻度间隔候选：从 1 分钟到 30 天
STEPS = [
    60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000,
    3_600_000, 3 * 3_600_000, 6 * 3_600_000, 12 * 3_600_000,
    86_400_000, 3 * 86_400_000, 7 * 86_400_000, 14 * 86_400_000, 30 * 86_400_000,
]

HIT_RADIUS = 9  # 点击容差（像素），比圆的半径大，方便点中


def _fmt(ms: int, span: int) -> str:
    """刻度文字：时间跨度小于 2 天写时分，再长就带日期。"""
    dt = datetime.fromtimestamp(ms / 1000)
    if span <= 2 * 86_400_000:
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def _fmt_full(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


class TimelineWidget(QWidget):
    """四条泳道的时间轴。数据由主窗口喂进来，自己不管备份逻辑。"""

    slotClicked = Signal(str, str)  # kind, slot

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        h = PAD_T + LANE_H * len(LANE_ORDER) + PAD_B
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)
        self.setToolTip("拖动平移时间轴，滚轮缩放，点一下圆点选中那次备份")

        self._rows: list[dict] = []
        self._points: list[dict] = []  # {kind, slot, ms, x, y, bytes, note, files}
        self._center: float | None = None
        self._span: float | None = None
        self._pressed_x: float | None = None
        self._press_center: float | None = None
        self._moved = False
        self._hover: str | None = None  # slot key
        self._selected: tuple[str, str] | None = None

    # ------------------------------------------------------------------ 数据
    def setData(self, rows: list[dict]) -> None:
        """喂数据。视图范围只在第一次、或数据整体跑出视野时才重算。"""
        self._rows = list(rows or [])
        had = bool(self._points)
        self._recompute_range()
        self._rebuild_points()
        if not had:
            self._fit_all()
        self.update()

    def setSelected(self, kind: str, slot: str) -> None:
        key = (kind, slot)
        if self._selected == key:
            return
        self._selected = key if kind and slot else None
        self.update()

    # ------------------------------------------------------------- 范围与换算
    def _times(self) -> list[int]:
        return [int(r.get("createdAt") or 0) for r in self._rows if r.get("createdAt")]

    def _recompute_range(self) -> None:
        ts = self._times()
        if ts:
            self._data_min = min(ts)
            self._data_max = max(ts)
        else:
            # 没有任何备份：把上下界压成同一时刻，_full_span 会退回「铺一天」。
            # 不能写成 now-1天/now —— 那样 raw>0，会被按「实际跨度×1.25」算成 30 小时。
            now = int(time.time() * 1000)
            self._data_min = self._data_max = now

    def _full_span(self) -> float:
        """「看全部」时的跨度。

        有多个点时按实际跨度铺（只留 25% 余量），最少 4 分钟；
        只有一个点时没法推出跨度，才铺一整天。
        之前一率铺一天，导致「刚做完几次备份」的时候几个点全挤在正中一小段里。
        """
        raw = self._data_max - self._data_min
        if raw <= 0:
            return FALLBACK_SPAN_MS
        return max(raw * 1.25, MIN_SPAN_MS * 4)

    def _fit_all(self) -> None:
        self._center = (self._data_min + self._data_max) / 2
        self._span = self._full_span()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            PAD_L,
            PAD_T,
            max(1.0, self.width() - PAD_L - PAD_R),
            LANE_H * len(LANE_ORDER),
        )

    def _x_of(self, ms: float) -> float:
        pr = self._plot_rect()
        c = self._center if self._center is not None else self._data_max
        s = self._span if self._span else FALLBACK_SPAN_MS
        return pr.left() + (ms - (c - s / 2)) / s * pr.width()

    def _ms_of(self, x: float) -> float:
        pr = self._plot_rect()
        c = self._center if self._center is not None else self._data_max
        s = self._span if self._span else FALLBACK_SPAN_MS
        return (c - s / 2) + (x - pr.left()) / pr.width() * s

    def _lane_y(self, kind: str) -> float:
        pr = self._plot_rect()
        idx = LANE_ORDER.index(kind) if kind in LANE_ORDER else 0
        return pr.top() + idx * LANE_H + LANE_H / 2

    def _rebuild_points(self) -> None:
        self._points = []
        for r in self._rows:
            ms = r.get("createdAt")
            if not ms:
                continue
            kind = r.get("kind")
            if kind not in LANE_ORDER:
                continue
            self._points.append({
                "kind": kind,
                "slot": r.get("slot"),
                "ms": int(ms),
                "bytes": r.get("bytes", 0),
                "files": r.get("files", 0),
                "note": r.get("note", "") or "",
            })

    # ------------------------------------------------------------------- 绘制
    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        pr = self._plot_rect()
        c = self._center if self._center is not None else self._data_max
        s = self._span if self._span else FALLBACK_SPAN_MS
        left_ms, right_ms = c - s / 2, c + s / 2

        # 泳道底色与分隔线
        for i, kind in enumerate(LANE_ORDER):
            y = pr.top() + i * LANE_H
            bg = QColor("#f6f7f9") if i % 2 == 0 else QColor("#ffffff")
            p.fillRect(QRectF(pr.left(), y, pr.width(), LANE_H), bg)
            p.setPen(QPen(QColor("#e6e8eb"), 1))
            p.drawLine(pr.left(), y + LANE_H, pr.right(), y + LANE_H)

        # 左侧标签
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF()))
        p.setFont(f)
        for i, kind in enumerate(LANE_ORDER):
            y = pr.top() + i * LANE_H + LANE_H / 2
            p.setPen(QPen(QColor(LANE_COLOR[kind]), 1))
            dot = QRectF(10, y - 4, 8, 8)
            p.setBrush(QColor(LANE_COLOR[kind]))
            p.setPen(Qt.NoPen)
            p.drawEllipse(dot)
            p.setPen(QPen(QColor("#3a3a3c"), 1))
            p.drawText(
                QRectF(24, y - LANE_H / 2, PAD_L - 30, LANE_H),
                Qt.AlignVCenter | Qt.AlignLeft,
                LANE_LABEL[kind],
            )

        # 时间刻度
        step = STEPS[-1]
        for cand in STEPS:
            if s / cand <= 7:
                step = cand
                break
        first = int(left_ms // step) * step
        t = first
        p.setPen(QPen(QColor("#c8ccd2"), 1, Qt.DashLine))
        while t <= right_ms:
            x = self._x_of(t)
            if pr.left() <= x <= pr.right():
                p.drawLine(int(x), pr.top(), int(x), pr.bottom())
                p.setPen(QPen(QColor("#6b6f76"), 1))
                p.drawText(
                    QRectF(x - 48, 2, 96, PAD_T - 4),
                    Qt.AlignHCenter | Qt.AlignBottom,
                    _fmt(int(t), int(s)),
                )
                p.setPen(QPen(QColor("#c8ccd2"), 1, Qt.DashLine))
            t += step

        # 泳道内的横向轴线
        for i, kind in enumerate(LANE_ORDER):
            y = pr.top() + i * LANE_H + LANE_H / 2
            p.setPen(QPen(QColor("#dfe2e6"), 1))
            p.drawLine(pr.left(), int(y), pr.right(), int(y))

        # 数据点
        for pt in self._points:
            if pt["ms"] < left_ms - s or pt["ms"] > right_ms + s:
                continue
            x = self._x_of(pt["ms"])
            if x < pr.left() - 4 or x > pr.right() + 4:
                continue
            y = self._lane_y(pt["kind"])
            key = f"{pt['kind']}|{pt['slot']}"
            sel = self._selected == (pt["kind"], pt["slot"])
            hover = self._hover == key
            color = QColor(LANE_COLOR[pt["kind"]])

            if sel or hover:
                p.setPen(Qt.NoPen)
                halo = QColor(color)
                halo.setAlpha(60 if hover and not sel else 90)
                p.setBrush(halo)
                p.drawEllipse(QRectF(x - 10, y - 10, 20, 20))

            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.setBrush(color)
            r = 6.0 if sel else 5.0
            p.drawEllipse(QRectF(x - r, y - r, r * 2, r * 2))

            if sel:
                p.setPen(QPen(color.darker(120), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QRectF(x - 8, y - 8, 16, 16))

        # 边框
        p.setPen(QPen(QColor("#dcdfe4"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        p.end()

    # ------------------------------------------------------------------- 交互
    def _hit(self, x: float, y: float) -> dict | None:
        best = None
        best_d = HIT_RADIUS
        for pt in self._points:
            px = self._x_of(pt["ms"])
            py = self._lane_y(pt["kind"])
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = pt, d
        return best

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._pressed_x = ev.position().x()
            self._press_center = self._center
            self._moved = False
            hit = self._hit(ev.position().x(), ev.position().y())
            self.setCursor(Qt.ClosedHandCursor if hit is None else Qt.PointingHandCursor)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        x, y = ev.position().x(), ev.position().y()

        if self._pressed_x is not None and self._press_center is not None:
            dx = x - self._pressed_x
            if abs(dx) > 3:
                self._moved = True
            if self._moved:
                pr = self._plot_rect()
                s = self._span or FALLBACK_SPAN_MS
                self.setCursor(Qt.ClosedHandCursor)
                self._center = self._press_center - dx / pr.width() * s
                self.update()
                return

        hit = self._hit(x, y)
        key = f"{hit['kind']}|{hit['slot']}" if hit else None
        if key != self._hover:
            self._hover = key
            self.setCursor(Qt.PointingHandCursor if hit else Qt.OpenHandCursor)
            if hit:
                note = f"   {hit['note']}" if hit["note"] else ""
                self.setToolTip(
                    f"{LANE_LABEL[hit['kind']]} · {_fmt_full(hit['ms'])}\n"
                    f"{hit['files']:,} 个文件 / "
                    f"{hit['bytes'] / 1024 ** 2:,.1f} MB{note}"
                )
            else:
                self.setToolTip("拖动平移时间轴，滚轮缩放，点一下圆点选中那次备份")
            self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.LeftButton:
            return
        if not self._moved and self._pressed_x is not None:
            hit = self._hit(ev.position().x(), ev.position().y())
            if hit:
                self._selected = (hit["kind"], hit["slot"])
                self.slotClicked.emit(hit["kind"], hit["slot"])
                self.update()
        self._pressed_x = None
        self._press_center = None
        self._moved = False
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if self._center is None or not self._span:
            self._fit_all()
        pr = self._plot_rect()
        x = ev.position().x()
        # 以光标所在时刻为中心缩放：光标下的那个备份不会跑掉
        anchor_ms = self._ms_of(x) if pr.left() <= x <= pr.right() else self._center
        delta = ev.angleDelta().y()
        factor = 0.82 if delta > 0 else 1.0 / 0.82
        new_span = max(MIN_SPAN_MS, min(self._span * factor, self._full_span() * 4))
        ratio = (x - pr.left()) / pr.width()
        ratio = max(0.0, min(1.0, ratio))
        self._span = new_span
        self._center = anchor_ms + (0.5 - ratio) * new_span
        self.update()
        ev.accept()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        """双击空白 = 回到全部范围内（缩小跑丢了容易找不到）。"""
        if self._hit(ev.position().x(), ev.position().y()) is None:
            self._fit_all()
            self.update()

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        """键盘也能平移：方向键一次挪 10%，Home 回到全部。"""
        if self._center is None or not self._span:
            self._fit_all()
        step = self._span * 0.12
        if ev.key() == Qt.Key_Left:
            self._center -= step
        elif ev.key() == Qt.Key_Right:
            self._center += step
        elif ev.key() == Qt.Key_Home:
            self._fit_all()
        else:
            super().keyPressEvent(ev)
            return
        self.update()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self.update()
