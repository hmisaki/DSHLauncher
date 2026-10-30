"""保留算法：纯函数，不碰磁盘、不写日志。

照抄设计 §7.3 伪码（那份伪码已用脚本实跑验证过），只多两样东西：
`stats` 出参（带回空桶/窄桶索引，供上层写日志）与 `bucket_label`（给人看的档位名）。
"""
from __future__ import annotations

from datetime import datetime


def seg_bounds(c: float, D: float, M: float, anchors: list | None = None) -> list[tuple[float, float]]:
    """返回 [(lo, hi), ...]，单位分钟，代表 age 落在 [lo, hi) 的段。"""
    out: list[tuple[float, float]] = []
    if anchors:
        lo = D
        for a in anchors:
            hi = min(float(a), M)
            if hi > lo:
                out.append((lo, hi))
            lo = max(lo, hi)
        if M > lo:
            out.append((lo, M))
        return out
    k = 1
    while k <= 64:
        lo = D + 2 * c * (2 ** (k - 1) - 1)
        if lo >= M:
            break
        hi = min(D + 2 * c * (2 ** k - 1), M)
        out.append((lo, hi))
        if hi >= M:
            break
        k += 1
    return out


def select_keep(now_ms: int, slots: list[dict], p: dict, stats: dict | None = None):
    """slots 必须是同一 layer，元素含 slot/createdAt（毫秒）。

    返回 (keep, hold, drop)：
      keep — 保留（密集期 + 每段代表）
      hold — 超期区最多留 1 份（留最新）
      drop — 删除候选（**不等于已删**，还要过 streak 门控）
    """
    keep: list[dict] = []
    hold: list[dict] = []
    drop: list[dict] = []
    future: list[dict] = []
    live: list[tuple[float, dict]] = []

    for s in sorted(slots, key=lambda x: x["createdAt"]):
        age = (now_ms - s["createdAt"]) / 60000.0
        if age < 0:
            future.append(s)                   # 未来时间戳 = 脏数据，不占代表位
        elif age >= p["M"]:
            hold.append(s)                     # 超期区
        else:
            live.append((age, s))

    # 时钟回拨护栏：未来包占多数时不是脏数据，是系统时钟错了，一份都不删
    clock_skew = len(future) > max(1, len(live) + len(hold))
    if clock_skew:
        keep.extend(future)
        future = []

    bounds = seg_bounds(p["c"], p["D"], p["M"], p.get("anchors"))
    buckets: list[list[dict]] = [[] for _ in bounds]

    for age, s in live:
        if age < p["D"]:
            keep.append(s)                     # 密集期全留
            continue
        for i, (lo, hi) in enumerate(bounds):
            if lo <= age < hi:
                buckets[i].append(s)
                break
        else:
            drop.append(s)

    empty: list[int] = []
    narrow: list[int] = []
    for i, b in enumerate(buckets):
        if not b:
            empty.append(i)                    # 空桶只记日志，不报错
            continue
        b.sort(key=lambda x: x["createdAt"])   # ts 升序 ⇒ 首个 age 最大
        keep.append(b[0])                      # ★ 每段留最老
        drop.extend(b[1:])                     # 段内落选者进删除候选（否则永不回收）

    for i, (lo, hi) in enumerate(bounds):
        if (hi - lo) < p["c"]:
            narrow.append(i)

    hold.sort(key=lambda x: x["createdAt"])
    keep_hold = hold[-1:] if hold else []
    drop.extend(hold[:-1])

    if stats is not None:
        stats.update(
            {
                "emptyBuckets": empty,
                "narrowBuckets": narrow,
                "bounds": bounds,
                "dense": sum(1 for s in keep if (now_ms - s["createdAt"]) / 60000.0 < p["D"]),
                "total": len(slots),
                "futureDropped": len(future),
                "clockSkew": clock_skew,
            }
        )

    drop = future + drop

    # 一份代表都没有时，从待删里救回最新一份；其余照删。
    # （旧版这里是「keep+hold<=1 就清空 drop」，会把段内落选者永远钉在盘上。）
    if not keep and not keep_hold:
        pool = [s for s in drop if s["slot"] not in {f["slot"] for f in future}]
        if pool:
            pool.sort(key=lambda x: x["createdAt"])
            keep_hold = pool[-1:]
            rescue = keep_hold[0]["slot"]
            drop = [s for s in drop if s["slot"] != rescue]

    return keep, keep_hold, drop


def bucket_label(created_at_ms: int, now_ms: int, p: dict) -> str:
    """给人看的档位名：「密集期」/「段3 7.5–9.5h」。"""
    age = (now_ms - created_at_ms) / 60000.0
    if age < 0:
        return "未来（脏）"
    if age >= p["M"]:
        return "超期区"
    if age < p["D"]:
        return "密集期"
    for i, (lo, hi) in enumerate(seg_bounds(p["c"], p["D"], p["M"], p.get("anchors"))):
        if lo <= age < hi:
            return f"段{i + 1} {_dur(lo)}–{_dur(hi)}"
    return "无归属"


def _dur(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def expected_counts(
    c: float, D: float, M: float, anchors: list | None = None, interval: float | None = None
) -> dict:
    """设计声明的份数：密集期份数 + 段数。用于自测断言。

    密集期份数 = D / 写入间隔（不是 D / c）：
      sessions  D=180  interval=15  → 12 份
      baseline  D=1440 interval=1440 → 1 份（c=720 只决定段宽，不决定密集期份数）
    """
    step = float(interval or c)
    dense = int(D // step) if step > 0 else 0
    segs = len(seg_bounds(c, D, M, anchors))
    return {"dense": dense, "segs": segs, "total": dense + segs}


def simulate(days: int, interval_min: float, c: float, D: float, M: float, anchors=None, step_min: int = 5) -> dict:
    """在模拟时间上跑一遍，返回最终存活份数与覆盖情况（自测用）。

    不落盘：直接在内存里造 slot 列表并按写入节奏触发 select_keep。
    """
    p = {"c": c, "D": D, "M": M, "anchors": anchors}
    total_min = days * 1440
    slots: list[dict] = []
    t = 0
    next_write = 0
    while t <= total_min:
        if t >= next_write:
            slots.append({"slot": f"sim-{t}", "createdAt": int(t * 60000), "verifiedOk": True})
            next_write = t + interval_min
            now_ms = int(t * 60000)
            keep, hold, drop = select_keep(now_ms, slots, p)
            drop_ids = {s["slot"] for s in drop}
            slots = [s for s in slots if s["slot"] not in drop_ids]
        t += step_min
    now_ms = int(total_min * 60000)
    keep, hold, drop = select_keep(now_ms, slots, p)
    keep_ages = sorted((now_ms - s["createdAt"]) / 60000.0 for s in keep)
    hold_ages = sorted((now_ms - s["createdAt"]) / 60000.0 for s in hold)
    return {
        "survivors": len(keep) + len(hold),
        "representatives": len(keep),
        "holdback": len(hold),
        "maxAgeMinutes": (keep_ages + hold_ages)[-1] if (keep_ages or hold_ages) else 0,
        "maxKeepAgeMinutes": keep_ages[-1] if keep_ages else 0,
        "maxHoldAgeMinutes": hold_ages[-1] if hold_ages else 0,
        "coverageDays": seg_bounds(c, D, M, anchors)[-1][1] / 1440.0,
        "coverage": f"{len(keep) + len(hold)}/{expected_counts(c, D, M, anchors, interval_min)['total']}",
        "slotAgesHours": [round(a / 60.0, 2) for a in keep_ages + hold_ages],
    }
