"""拼装恢复单测：真 7z 打包 → plan 合并 → run 覆盖顺序。

只造 tmp 下的小样本库，不碰真实备份。造包用 engine.backup_kind（与线上同一条路径），
因为"能不能拼装"取决于真实包内路径结构，手搓假 manifest 测不出这件事。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import restore_batch  # noqa: E402
from dshbackup import config as cfgmod  # noqa: E402
from dshbackup import engine, manifest  # noqa: E402


class RestoreBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.root = self.base / "lib"
        self.src_a = self.base / "packA"
        self.src_b = self.base / "packB"
        for src in (self.src_a, self.src_b):
            (src / "sub").mkdir(parents=True, exist_ok=True)
            (src / "same.txt").write_text("x" * 100, encoding="utf-8")
            (src / "sub" / "only.txt").write_text("y" * 50, encoding="utf-8")

        cfg = cfgmod.default_config()
        cfg["paths"]["backup_root"] = str(self.root)
        cfg["sets"] = {
            "baseline": {"roots": [{"label": "pakA", "path": self.src_a,
                                    "exclude_dirs": []}], "exclude_links": []},
            "sessions": {"roots": [{"label": "pakB", "path": self.src_b,
                                    "exclude_dirs": []}], "exclude_links": []},
        }
        self.cfg = cfg
        cfgmod.ensure_dirs(self.root, cfg)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _backup(self, kind: str) -> dict:
        res = engine.backup_kind(self.cfg, self.root, kind, note="单测",
                                 progress=None, logger=None)
        self.assertTrue(res.get("ok"), res)
        time.sleep(1.05)   # slot 名精确到秒，两份同秒会互相覆盖
        return res

    # ---------- 用例 1：跨类合并 ----------

    def test_plan_merges_cross_kind_by_created_at(self) -> None:
        s_base = self._backup("baseline")["slot"]
        s_sess = self._backup("sessions")["slot"]

        p = restore_batch.plan(self.cfg, self.root,
                              [("sessions", s_sess), ("baseline", s_base)])
        self.assertTrue(p["ok"], p)
        self.assertEqual(len(p["items"]), 2)
        # 顺序：createdAt 升序（先 baseline 后 sessions），与勾选顺序无关
        self.assertEqual(p["order"], [("baseline", s_base), ("sessions", s_sess)])
        base_item = next(i for i in p["items"] if i["kind"] == "baseline")
        sess_item = next(i for i in p["items"] if i["kind"] == "sessions")
        self.assertEqual(p["totalFiles"], base_item["files"] + sess_item["files"])
        self.assertEqual(p["totalBytes"], base_item["bytes"] + sess_item["bytes"])
        # 两个源目录名不同 ⇒ 解出来是两个顶层目录，没有覆盖
        self.assertEqual(p["conflictCount"], 0, p["conflicts"][:5])

        dest = self.base / "out"
        r = restore_batch.run(self.cfg, self.root, p, dest, logger=None)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["restored"]), 2)
        self.assertTrue((dest / "packA" / "same.txt").exists())
        self.assertTrue((dest / "packB" / "same.txt").exists())
        self.assertEqual(r["totalFiles"], 4, "两个包各 2 个文件，拼起来应是 4")

    # ---------- 用例 2：覆盖顺序（新包赢） ----------

    def test_run_lets_newer_package_win(self) -> None:
        first = self._backup("sessions")["slot"]
        # 同一个源目录，改内容后再备一份 → 两份包内路径完全相同
        (self.src_b / "same.txt").write_text("NEW" * 40, encoding="utf-8")
        second = self._backup("sessions")["slot"]
        self.assertNotEqual(first, second)

        p = restore_batch.plan(self.cfg, self.root,
                               [("sessions", second), ("sessions", first)])
        self.assertTrue(p["ok"], p)
        self.assertGreater(p["conflictCount"], 0, "同源两份包必须报告覆盖关系")
        conflicts = {c["path"]: c["overlappedBy"] for c in p["conflicts"]}
        self.assertIn(("packb\\same.txt").lower(), {k.lower() for k in conflicts})
        # 从旧到新：first 先、second 后
        self.assertEqual(p["order"], [("sessions", first), ("sessions", second)])

        dest = self.base / "out2"
        r = restore_batch.run(self.cfg, self.root, p, dest, logger=None)
        self.assertTrue(r["ok"], r)
        got = (dest / "packB" / "same.txt").read_text(encoding="utf-8")
        self.assertEqual(got, "NEW" * 40, "后解的那份必须赢")

    # ---------- 用例 3：空间检查（不碰 7z） ----------

    def test_check_space_reports_shortfall(self) -> None:
        ok = restore_batch.check_space(self.base, 1024)
        self.assertTrue(ok["ok"], ok)
        self.assertGreater(ok["freeBytes"], 0)
        self.assertEqual(ok["needWithMargin"], int(1024 * restore_batch.SPACE_MARGIN))

        huge = restore_batch.check_space(self.base, 10 ** 15)
        self.assertFalse(huge["ok"])
        self.assertIn("空间不够", huge["reason"])

    # ---------- 用例 4：包丢了要说清，不许"当成没冲突" ----------

    def test_plan_refuses_missing_parts(self) -> None:
        slot = self._backup("sessions")["slot"]
        folder = Path(manifest.scan(self.root, "sessions")[0]["dir"])
        for zp in folder.glob("*.7z"):
            zp.unlink()
        p = restore_batch.plan(self.cfg, self.root, [("sessions", slot)])
        self.assertFalse(p["ok"])
        self.assertIn("丢失", p["reason"])
        # 关键：缺包时 conflictCount == 0（读不到条目自然算不出冲突），
        # 所以"没冲突"绝不能当成"可以放行"——ok/reason 才是闸，冲突数只是附带信息。
        self.assertEqual(p["conflictCount"], 0)
        self.assertNotIn("无冲突", p["reason"])

    # ---------- 用例 5：真实目录形态下顶层被摊平成兄弟目录 ----------

    def test_flattened_tops_keep_dotdsh_and_sessions_side_by_side(self) -> None:
        """照线上形态造：baseline = 检出目录 + .dsh（排除 sessions），sessions = .dsh\\sessions。

        三个源如果按真实嵌套还原，基线里的 .dsh 会盖掉会话那份；
        所以顶层一律取"该 part 目标的最后一段"，.dsh 与 sessions 变成兄弟目录。
        """
        host = self.base / "host"
        dsh = host / ".dsh"
        harness = host / "deepseek-harness"
        (dsh / "sessions").mkdir(parents=True, exist_ok=True)
        harness.mkdir(parents=True, exist_ok=True)
        (dsh / "config.json").write_text("{}", encoding="utf-8")
        (dsh / "sessions" / "s1.json").write_text("s" * 30, encoding="utf-8")
        (harness / "package.json").write_text("{}", encoding="utf-8")

        cfg = cfgmod.default_config()
        cfg["paths"]["backup_root"] = str(self.root)
        cfg["sets"] = {
            "baseline": {"roots": [
                {"label": "harness", "path": harness, "exclude_dirs": []},
                {"label": "dsh", "path": dsh, "exclude_dirs": ["sessions"]},
            ], "exclude_links": []},
            "sessions": {"roots": [
                {"label": "sessions", "path": dsh / "sessions", "exclude_dirs": []},
            ], "exclude_links": []},
        }
        cfgmod.ensure_dirs(self.root, cfg)
        s_base = engine.backup_kind(cfg, self.root, "baseline", note="单测",
                                    progress=None, logger=None)["slot"]
        time.sleep(1.05)
        s_sess = engine.backup_kind(cfg, self.root, "sessions", note="单测",
                                    progress=None, logger=None)["slot"]

        p = restore_batch.plan(cfg, self.root,
                               [("baseline", s_base), ("sessions", s_sess)])
        self.assertTrue(p["ok"], p)
        tops = {restore_batch.top_of(part)
                for it in p["items"] for part in it["parts"]}
        self.assertEqual(tops, {"deepseek-harness", ".dsh", "sessions"}, tops)
        self.assertEqual(p["conflictCount"], 0, p["conflicts"][:5])

        dest = self.base / "flat"
        r = restore_batch.run(cfg, self.root, p, dest, logger=None)
        self.assertTrue(r["ok"], r)
        self.assertTrue((dest / ".dsh" / "config.json").exists())
        self.assertTrue((dest / "sessions" / "s1.json").exists())
        self.assertTrue((dest / "deepseek-harness" / "package.json").exists())
        # 摊平 = 兄弟目录，不是嵌套：.dsh 里面不该再出现一个 sessions
        self.assertFalse((dest / ".dsh" / "sessions").exists())
        self.assertEqual(r["totalFiles"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
