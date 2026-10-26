"""时间/时长的人话化。

UX-05 的教训：表格与恢复弹层各写一套时间格式，同一个包在两处看起来不一样，
用户因此把「会话」当成「基线」恢复了一次。格式只能有一份实现，两处共用。
（放独立模块是为了避免 main_window ↔ restore_dialog 循环导入。）
"""
from __future__ import annotations

from datetime import datetime


def human_ts(ms: int | None, now: datetime | None = None) -> str:
    """今天→`今天 03:46:25`、昨天→`昨天 20:14:03`、同年→`10-24 18:02:41`、跨年→带年份。

    对账补齐（R3-3）：表格单元格与恢复弹层必须**逐字相同**才能交叉核对。
    所以秒一定要在里面——弹层原来压缩成 `T034625`、表格是 `03:46:25`，
    用户对不上这两串就不知道自己在恢复哪一份。
    """
    if not ms:
        return ""
    t = datetime.fromtimestamp(ms / 1000.0)
    now = now or datetime.now()
    days = (now.date() - t.date()).days
    if days == 0:
        return f"今天 {t:%H:%M:%S}"
    if days == 1:
        return f"昨天 {t:%H:%M:%S}"
    if t.year == now.year:
        return f"{t:%m-%d %H:%M:%S}"
    return f"{t:%Y-%m-%d %H:%M:%S}"


def day_word(ms: int | None, now: datetime | None = None) -> str:
    """相对日词：今天 / 昨天 / 前天 / N 天前（R3-2 空结果提示用）。"""
    if not ms:
        return ""
    t = datetime.fromtimestamp(ms / 1000.0)
    now = now or datetime.now()
    days = (now.date() - t.date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return f"{days} 天前"


def human_age(ms: int | None) -> str:
    if not ms:
        return "还没有"
    delta = max(0.0, datetime.now().timestamp() - ms / 1000.0)
    if delta < 90:
        return f"{delta:.0f} 秒前"
    if delta < 5400:
        return f"{delta / 60:.0f} 分钟前"
    if delta < 172800:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


def human_span(ms: float) -> str:
    """UX-01：把「实际能退回多久」写成一句话。只服务能力段，不带「前」字。"""
    sec = max(0.0, float(ms) / 1000.0)
    if sec < 3600:
        return f"{max(1, int(sec // 60))} 分钟"
    if sec < 172800:
        return f"{sec / 3600:.0f} 小时"
    days = sec / 86400.0
    return f"{days:.0f} 天" if days >= 2 else f"{days:.1f} 天"


def human_gb(num_bytes: float) -> str:
    """R3-5：底栏体积口径定死——≥1 GB 一位小数 + ` GB`，<1 GB 整 ` MB`。"""
    gb = max(0.0, float(num_bytes)) / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{int(round(gb * 1024))} MB"


def human_span_long(ms: float) -> str:
    """裁决 1：「已暂停（2 小时 15 分前）」要的是可读精度，不是四舍五入到小时。

    human_span 给的是粗粒度（`2 小时`），用在「暂停了多久」上会显得比实际短——
    用户看到的和墙上钟差一刻钟，就会怀疑这个开关到底生效没有。
    """
    sec = max(0, int(float(ms) / 1000.0))
    if sec < 60:
        return "刚刚"
    mins = sec // 60
    if mins < 60:
        return f"{mins} 分"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"

