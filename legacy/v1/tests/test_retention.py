"""保留算法的单测：逐条对设计 §7.2/§7.4/§7.6 的边界。

跑：cd D:\\DSHLuncher && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from dshbackup import retention

# 设计 §7.1 的两组参数（分钟）
DIALOG = {"c": 15.0, "D": 360.0, "M": 23040.0, "anchors": None}
BASE = {"c": 1440.0, "D": 1440.0, "M": 44640.0, "anchors": None}

MIN = 60_000  # 1 分钟 = 60000 ms


def mk(ages_min: list[float]) -> tuple[int, list[dict]]:
    """按「多少分钟前」造 slot，now = 0 时刻。"""
    now_ms = 1_000_000_000_000
    slots = [
        {"slot": f"s{i:03d}", "createdAt": now_ms - int(a * MIN), "verifiedOk": True}
        for i, a in enumerate(ages_min)
    ]
    return now_ms, slots


class TestSegBounds(unittest.TestCase):
    def test_dialog_ten_segments(self):
        b = retention.seg_bounds(DIALOG["c"], DIALOG["D"], DIALOG["M"])
        self.assertEqual(len(b), 10)
        expect = [
            (360, 390), (390, 450), (450, 570), (570, 810), (810, 1290),
            (1290, 2250), (2250, 4170), (4170, 8010), (8010, 15690), (15690, 23040),
        ]
        self.assertEqual([(int(lo), int(hi)) for lo, hi in b], expect)

    def test_segments_are_contiguous(self):
        b = retention.seg_bounds(DIALOG["c"], DIALOG["D"], DIALOG["M"])
        for i in range(len(b) - 1):
            self.assertEqual(b[i][1], b[i + 1][0], f"段{i+1} 与段{i+2} 之间有缝")

    def test_baseline_four_segments(self):
        b = retention.seg_bounds(BASE["c"], BASE["D"], BASE["M"])
        self.assertEqual([(int(lo) // 1440, int(hi) // 1440) for lo, hi in b], [(1, 3), (3, 7), (7, 15), (15, 31)])

    def test_anchors_six_segments(self):
        anchors = [720, 1440, 2880, 5760, 11520, 23040]
        b = retention.seg_bounds(DIALOG["c"], DIALOG["D"], DIALOG["M"], anchors)
        self.assertEqual(len(b), 6)
        self.assertEqual(b[0], (360.0, 720.0))
        self.assertEqual(b[-1][1], 23040.0)

    def test_counts_match_design(self):
        self.assertEqual(retention.expected_counts(15, 360, 23040)["total"], 34)
        self.assertEqual(retention.expected_counts(1440, 1440, 44640)["total"], 5)
        self.assertEqual(retention.expected_counts(15, 360, 23040, [720, 1440, 2880, 5760, 11520, 23040])["total"], 30)


class TestSelectKeep(unittest.TestCase):
    def test_dense_period_keeps_all(self):
        now, slots = mk([0, 15, 30, 45, 60, 300])  # 全在 6h 密集期内
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        self.assertEqual(len(keep), 6)
        self.assertEqual(hold, [])
        self.assertEqual(drop, [])

    def test_keeps_oldest_per_segment(self):
        # 段1 [360,390)：放 365 与 385，应留 385（更老）；另加一份密集期包避开「唯一幸存者」
        now, slots = mk([10, 365, 385])
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        self.assertEqual(sorted(s["slot"] for s in keep), ["s000", "s002"])
        self.assertEqual([s["slot"] for s in drop], ["s001"])

    def test_one_representative_per_segment(self):
        ages = [370, 400, 420, 440,  500, 550,  600, 790,  900, 1200,  1400, 2100,
                2400, 4000,  5000, 7900,  9000, 15000, 20000]
        now, slots = mk(ages)
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        segs = retention.seg_bounds(DIALOG["c"], DIALOG["D"], DIALOG["M"])
        buckets = set()
        for s in keep:
            age = (now - s["createdAt"]) / MIN
            for i, (lo, hi) in enumerate(segs):
                if lo <= age < hi:
                    buckets.add(i)
        self.assertEqual(buckets, set(range(10)), "十段应有十位代表")
        self.assertEqual(len(keep), 10)

    def test_m_truncation_counterexample(self):
        """设计 §7.4 反例：X 超期、Y 合格，同落末段时不能误杀 Y。"""
        x_age = 16.05 * 1440   # 超期
        y_age = 15.95 * 1440   # 合格
        now, slots = mk([x_age, y_age])
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        self.assertEqual([s["slot"] for s in keep], ["s001"], "合格的 Y 必须保留")
        self.assertEqual([s["slot"] for s in hold], ["s000"], "超期的 X 进待删区")
        self.assertEqual(drop, [], "唯一幸存者不删")

        # 再来一份新包，Y 仍是代表
        slots.append({"slot": "NEW", "createdAt": now, "verifiedOk": True})
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        ids = sorted(s["slot"] for s in keep)
        self.assertIn("s001", ids, "新包不该把合格代表挤掉")
        self.assertIn("NEW", ids)

    def test_future_timestamp_dropped(self):
        now, slots = mk([10])
        slots.append({"slot": "FUT", "createdAt": now + 3_600_000, "verifiedOk": True})
        stats: dict = {}
        keep, hold, drop = retention.select_keep(now, slots, DIALOG, stats)
        self.assertIn("FUT", [s["slot"] for s in drop])
        self.assertNotIn("FUT", [s["slot"] for s in keep])
        self.assertFalse(stats["clockSkew"])

    def test_clock_skew_protects_everything(self):
        """时钟回拨：未来包占多数时一份都不删。"""
        now, slots = mk([10, 20])
        for i in range(5):
            slots.append({"slot": f"F{i}", "createdAt": now + (i + 1) * 3_600_000, "verifiedOk": True})
        stats: dict = {}
        keep, hold, drop = retention.select_keep(now, slots, DIALOG, stats)
        self.assertTrue(stats["clockSkew"])
        self.assertEqual([s for s in drop if s["slot"].startswith("F")], [])
        self.assertEqual(len(keep), 7)

    def test_unique_survivor_never_deleted(self):
        now, slots = mk([30 * 1440])  # 唯一一份且已超期
        keep, hold, drop = retention.select_keep(now, slots, DIALOG)
        self.assertEqual(len(keep) + len(hold), 1)
        self.assertEqual(drop, [])

    def test_idempotent(self):
        ages = [5, 20, 35, 380, 420, 600, 1400, 3000, 9000, 20000, 30000]
        now, slots = mk(ages)
        a = retention.select_keep(now, slots, DIALOG)
        b = retention.select_keep(now, slots, DIALOG)
        self.assertEqual([[s["slot"] for s in x] for x in a], [[s["slot"] for s in x] for x in b])

    def test_empty_buckets_warn_but_not_raise(self):
        now, slots = mk([10, 380])  # 只有段1 有代表
        stats: dict = {}
        keep, hold, drop = retention.select_keep(now, slots, DIALOG, stats)
        self.assertEqual(len(stats["emptyBuckets"]), 9)
        self.assertEqual(len(keep), 2)

    def test_bucket_label(self):
        now, _ = mk([0])
        self.assertEqual(retention.bucket_label(now - 60 * MIN, now, DIALOG), "密集期")
        self.assertIn("段1", retention.bucket_label(now - 370 * MIN, now, DIALOG))
        self.assertEqual(retention.bucket_label(now - 30_000 * MIN, now, DIALOG), "超期区")


class TestSimulate(unittest.TestCase):
    def test_dialog_20days_reaches_declared_counts(self):
        r = retention.simulate(days=20, interval_min=15, c=15, D=360, M=23040)
        # 24 密集 + 10 段 = 34；段10 代表毕业后进待删区，总数不变
        self.assertIn(r["survivors"], (34, 35), f"实际 {r['survivors']}")
        self.assertGreater(r["maxAgeMinutes"], 15000, "最老一份应接近 16 天")

    def test_baseline_31days(self):
        r = retention.simulate(days=40, interval_min=1440, c=1440, D=1440, M=44640)
        self.assertIn(r["survivors"], (5, 6), f"实际 {r['survivors']}")


if __name__ == "__main__":
    unittest.main()
