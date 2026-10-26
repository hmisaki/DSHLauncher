"""保留算法单测：段边界 / 密集期 / 留最老 / M 切分 / 纯 age / 待删保护。

参数取用户定稿的两套：
  sessions  c=15   D=180   M=21600 interval=15   → 22 份 / 15.00 天
  baseline  c=720  D=1440  M=23040 interval=1440 →  5 份 / 16.00 天
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dshbackup import retention, retention_state  # noqa: E402

SESS = {"c": 15, "D": 180, "M": 21600, "interval": 15, "hold_delete_streak": 2}
BASE = {"c": 720, "D": 1440, "M": 23040, "interval": 1440, "hold_delete_streak": 2}

MIN = 60_000  # 一分钟的毫秒数


def slot(name: str, age_min: float, now_ms: int) -> dict:
    return {"slot": name, "createdAt": int(now_ms - age_min * MIN), "verifiedOk": True}


class SegBoundsTest(unittest.TestCase):
    def test_sessions_bounds_match_user_spec(self) -> None:
        got = retention.seg_bounds(15, 180, 21600)
        want = [
            (180, 210), (210, 270), (270, 390), (390, 630), (630, 1110),
            (1110, 2070), (2070, 3990), (3990, 7830), (7830, 15510), (15510, 21600),
        ]
        self.assertEqual(got, want)
        self.assertEqual(len(got), 10)

    def test_baseline_bounds_match_user_spec(self) -> None:
        got = retention.seg_bounds(720, 1440, 23040)
        want = [(1440, 2880), (2880, 5760), (5760, 11520), (11520, 23040)]
        self.assertEqual(got, want)
        self.assertEqual(len(got), 4)

    def test_bounds_are_contiguous_and_end_at_cap(self) -> None:
        for p in (SESS, BASE):
            bounds = retention.seg_bounds(p["c"], p["D"], p["M"])
            self.assertEqual(bounds[0][0], p["D"])
            self.assertEqual(bounds[-1][1], p["M"])
            for a, b in zip(bounds, bounds[1:]):
                self.assertEqual(a[1], b[0], "段与段必须严丝合缝")

    def test_first_segment_wider_than_interval(self) -> None:
        # 段宽 >= 写入间隔，否则上段的代表会被下一份挤掉
        for p in (SESS, BASE):
            bounds = retention.seg_bounds(p["c"], p["D"], p["M"])
            for lo, hi in bounds:
                self.assertGreaterEqual(hi - lo, p["interval"], f"{lo}-{hi} 太窄")


class ExpectedCountsTest(unittest.TestCase):
    def test_sessions_22_and_15_days(self) -> None:
        c = retention.expected_counts(15, 180, 21600, interval=15)
        self.assertEqual(c["dense"], 12)
        self.assertEqual(c["segs"], 10)
        self.assertEqual(c["total"], 22)
        self.assertEqual(retention.seg_bounds(15, 180, 21600)[-1][1] / 1440, 15.0)

    def test_baseline_5_and_16_days(self) -> None:
        c = retention.expected_counts(720, 1440, 23040, interval=1440)
        self.assertEqual(c["dense"], 1, "baseline 密集期是 1 份（D/interval=1440/1440）")
        self.assertEqual(c["segs"], 4)
        self.assertEqual(c["total"], 5)
        self.assertEqual(retention.seg_bounds(720, 1440, 23040)[-1][1] / 1440, 16.0)


class SelectKeepTest(unittest.TestCase):
    def test_dense_period_keeps_everything(self) -> None:
        now = 1_800_000_000_000
        slots = [slot(f"d{i}", i * 15 + 1, now) for i in range(12)]
        keep, hold, drop = retention.select_keep(now, slots, SESS)
        self.assertEqual(len(keep), 12)
        self.assertEqual(drop, [])

    def test_keeps_oldest_in_each_segment(self) -> None:
        now = 1_800_000_000_000
        lo, hi = retention.seg_bounds(15, 180, 21600)[0]  # 180–210
        slots = [
            slot("newest", lo + 1, now),        # age 最小（最新）
            slot("oldest", hi - 1, now),        # age 最大（最老）
            slot("middle", (lo + hi) / 2, now),
        ]
        keep, hold, drop = retention.select_keep(now, slots, SESS)
        kept = {s["slot"] for s in keep}
        dropped = {s["slot"] for s in drop}
        self.assertIn("oldest", kept, "每段留最老")
        self.assertEqual(dropped, {"newest", "middle"}, "段内落选者必须进删除候选")

    def test_cap_is_sliced_before_segmenting(self) -> None:
        """超 M 的包进 hold，不进任何段；段表按 M 截断。"""
        now = 1_800_000_000_000
        slots = [
            slot("over", 23040 + 5, now),      # 超期
            slot("inner", 23000, now),         # 落在最后一段
        ]
        keep, hold, drop = retention.select_keep(now, slots, BASE)
        self.assertEqual([s["slot"] for s in hold], ["over"])
        self.assertIn("inner", {s["slot"] for s in keep})
        self.assertEqual(drop, [])

    def test_over_cap_keeps_only_newest_in_hold(self) -> None:
        now = 1_800_000_000_000
        slots = [
            slot("veryold", 40000, now),
            slot("old", 30000, now),
            slot("lessold", 24000, now),
        ]
        keep, hold, drop = retention.select_keep(now, slots, BASE)
        self.assertEqual([s["slot"] for s in hold], ["lessold"], "超期区留最新一份")
        self.assertEqual({s["slot"] for s in drop}, {"veryold", "old"})

    def test_pure_age_not_grid(self) -> None:
        """同样的 age，ts 落在网格内/外，判定结果必须一样（禁 floor(ts/c) 归格）。"""
        now = 1_800_000_000_000
        a = {"slot": "on-grid", "createdAt": now - 195 * MIN, "verifiedOk": True}
        b = {"slot": "off-grid", "createdAt": now - 195 * MIN + 7 * MIN // 2, "verifiedOk": True}
        ka, _, da = retention.select_keep(now, [a], SESS)
        kb, _, db = retention.select_keep(now, [b], SESS)
        self.assertEqual(len(ka), len(kb))
        self.assertEqual(len(da), len(db))

    def test_single_survivor_never_dropped(self) -> None:
        now = 1_800_000_000_000
        slots = [slot("only", 30 * 1440, now)]  # 远超 M
        keep, hold, drop = retention.select_keep(now, slots, SESS)
        self.assertEqual(drop, [], "只剩一份时一份都不删")
        self.assertEqual(len(keep) + len(hold), 1)

    def test_future_timestamps_dropped(self) -> None:
        now = 1_800_000_000_000
        slots = [slot("future1", -600, now), slot("future2", -300, now)]
        # 再补一批正常包，避免触发「时钟回拨」护栏
        slots += [slot(f"ok{i}", i * 15 + 1, now) for i in range(6)]
        keep, hold, drop = retention.select_keep(now, slots, SESS)
        self.assertEqual({s["slot"] for s in drop}, {"future1", "future2"})
        self.assertNotIn("future1", {s["slot"] for s in keep})

    def test_clock_skew_guard_keeps_future(self) -> None:
        """未来包占多数时视为时钟错，一份都不删。"""
        now = 1_800_000_000_000
        slots = [slot(f"f{i}", -i - 1, now) for i in range(5)]
        keep, hold, drop = retention.select_keep(now, slots, SESS)
        self.assertEqual(drop, [])
        self.assertEqual(len(keep), 5)

    def test_simulate_sessions_22(self) -> None:
        """代表 22 份 + 待删区常驻 1 份 = 盘上 23 份；覆盖 15.00 天。"""
        sim = retention.simulate(days=25, interval_min=15, c=15, D=180, M=21600, step_min=15)
        self.assertEqual(sim["representatives"], 22, f"代表 {sim['representatives']} 份")
        self.assertLessEqual(sim["holdback"], 1, "待删区最多 1 份")
        self.assertEqual(sim["survivors"], 23, f"盘上 {sim['survivors']} 份")
        self.assertAlmostEqual(sim["coverageDays"], 15.0, places=2)
        self.assertLessEqual(sim["maxKeepAgeMinutes"], 21600, "代表不能超过上限")
        self.assertGreaterEqual(sim["maxKeepAgeMinutes"], 15510, "末段代表应落在末段内")

    def test_simulate_baseline_5(self) -> None:
        """代表 5 份 + 待删区常驻 1 份 = 盘上 6 份；覆盖 16.00 天。"""
        sim = retention.simulate(days=30, interval_min=1440, c=720, D=1440, M=23040, step_min=60)
        self.assertEqual(sim["representatives"], 5, f"代表 {sim['representatives']} 份")
        self.assertLessEqual(sim["holdback"], 1)
        self.assertEqual(sim["survivors"], 6, f"盘上 {sim['survivors']} 份")
        self.assertAlmostEqual(sim["coverageDays"], 16.0, places=2)
        self.assertLessEqual(sim["maxKeepAgeMinutes"], 23040)
        self.assertGreaterEqual(sim["maxKeepAgeMinutes"], 11520)

    def test_bucket_label(self) -> None:
        now = 1_800_000_000_000
        self.assertEqual(retention.bucket_label(now - 10 * MIN, now, SESS), "密集期")
        self.assertEqual(retention.bucket_label(now - 200 * MIN, now, SESS), "段1 3.0h–3.5h")
        self.assertEqual(retention.bucket_label(now - 30000 * MIN, now, SESS), "超期区")


class ApplyPruneTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _mk(self, kind: str, name: str, age_min: float, now_ms: int) -> dict:
        d = self.root / kind / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{kind}.7z").write_bytes(b"x" * 32)
        return {
            "slot": name,
            "createdAt": int(now_ms - age_min * MIN),
            "kind": kind,
            "dir": str(d),          # apply_prune 靠 dir 删目录
        }

    def test_hold_delete_needs_streak(self) -> None:
        now = 1_800_000_000_000
        old1 = self._mk("baseline", "2020-01-01T000000", 40000, now)
        old2 = self._mk("baseline", "2020-01-02T000000", 39000, now)
        keep, hold, drop = retention.select_keep(now, [old1, old2], BASE)
        self.assertEqual(len(drop), 1, "两份超期 → 较老那份进删除候选")

        st = retention_state.load(self.root)
        r1 = retention_state.apply_prune(
            self.root, "baseline", keep, hold, drop, st, BASE, logger=None
        )
        self.assertTrue(r1["deferred"], "streak 未达标必须先缓删")
        self.assertEqual(r1["deleted"], 0)
        self.assertTrue((self.root / "baseline" / drop[0]["slot"]).exists())

        retention_state.note_verified(self.root, "baseline", True)
        retention_state.note_verified(self.root, "baseline", True)
        st = retention_state.load(self.root)
        r2 = retention_state.apply_prune(
            self.root, "baseline", keep, hold, drop, st, BASE, logger=None
        )
        self.assertFalse(r2["deferred"])
        self.assertEqual(r2["deleted"], 1)
        self.assertFalse((self.root / "baseline" / drop[0]["slot"]).exists())

    def test_streak_resets_on_failed_verify(self) -> None:
        st = retention_state.load(self.root)
        retention_state.note_verified(self.root, "baseline", True)
        retention_state.note_verified(self.root, "baseline", False)
        st = retention_state.load(self.root)
        self.assertEqual(retention_state.layer_state(st, "baseline").get("streak", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
