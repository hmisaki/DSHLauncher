"""端到端：造一个小样本树 → 打包 → 校验 → 演练恢复 → 比对 → 淘汰。

不碰真实数据，全部落在 tmp 下的临时目录，跑完自删。
跑：cd D:\\DSHLuncher && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from dshbackup import archive, collector, config as cfgmod, log as logmod, manifest, restore, retention, retention_state, runner


def build_sample(base: Path) -> dict:
    """造一棵像 dsh_home 的小树 + 一个像 checkout 的小树。"""
    dsh = base / "dsh_home"
    ck = base / "checkout"
    ws = base / "workspace"
    root = base / "backups"
    for p in (dsh, ck, ws, root):
        p.mkdir(parents=True, exist_ok=True)

    (dsh / "sessions" / "slug-a").mkdir(parents=True)
    (dsh / "sessions" / "slug-a" / "session.v4.jsonl").write_text('{"m":1}\n' * 200, encoding="utf-8")
    (dsh / "sessions" / "slug-b").mkdir(parents=True)
    (dsh / "sessions" / "slug-b" / "session.v4.jsonl").write_text('{"m":2}\n' * 50, encoding="utf-8")
    (dsh / "attachments" / "v1" / "objects" / "ab").mkdir(parents=True)
    (dsh / "attachments" / "v1" / "objects" / "ab" / "abc.bin").write_bytes(b"\x00\x01\x02" * 100)
    (dsh / "config.yaml").write_text("version: 1\n", encoding="utf-8")

    (ck / "packages" / "boot").mkdir(parents=True)
    (ck / "packages" / "boot" / "index.js").write_text("export const a = 1;\n", encoding="utf-8")
    (ck / "node_modules" / ".pnpm").mkdir(parents=True)
    (ck / "node_modules" / ".pnpm" / "dep.js").write_text("module.exports=1\n", encoding="utf-8")
    (ck / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    (ck / "node_modules" / ".modules.yaml").write_text("hoistPattern: '*'\n", encoding="utf-8")

    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "a.md").write_text("# hi\n", encoding="utf-8")

    cfg = cfgmod.default_config()
    cfg["paths"].update(
        {
            "backup_root": str(root),
            "checkout": str(ck),
            "dsh_home": str(dsh),
            "workspace": str(ws),
        }
    )
    cfg["layers"]["dialog"]["include"] = ["{dsh_home}/sessions", "{dsh_home}/attachments"]
    cfg["layers"]["baseline"]["include"] = ["{dsh_home}", "{checkout}"]
    cfg["layers"]["workspace"]["include"] = ["{workspace}/docs"]
    cfg["compress"]["level"] = 1
    cfgmod.ensure_dirs(root)
    return cfg


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dshbk-", dir=Path(__file__).parent.parent / "tmp"))
        self.cfg = build_sample(self.tmp)
        self.root = Path(self.cfg["paths"]["backup_root"])
        self.logger = logmod.Logger(self.root / cfgmod.DIR_LOGS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 打包 ──────────────────────────────────────────
    def test_collect_respects_exclude(self):
        res = collector.collect(self.cfg, "baseline")
        arcs = [a for a, _, _ in res.files]
        self.assertTrue(any(a.endswith("session.v4.jsonl") for a in arcs) is False, "基线不该含 sessions")
        self.assertTrue(any(".pnpm/dep.js" in a for a in arcs), "node_modules 实体应进包")
        self.assertTrue(any(a.endswith("pnpm-lock.yaml") for a in arcs))

    def test_dialog_pack_and_manifest(self):
        res = runner.run_layer_once(self.cfg, "dialog", logger=self.logger)
        self.assertTrue(res["ok"], res)
        folder = Path(res["dir"])
        self.assertTrue((folder / "dialog.zip").exists())
        m = manifest.read_manifest(folder)
        self.assertEqual(m["layer"], "dialog")
        self.assertEqual(m["files"], 3, "两份 session + 一个 attachment")
        self.assertTrue(m["verifiedOk"])
        self.assertEqual(len(m["sha256"]), 64)
        ok, detail = archive.verify_zip(folder / "dialog.zip")
        self.assertTrue(ok, detail)

    def test_zip_opens_without_our_code(self):
        import zipfile

        res = runner.run_layer_once(self.cfg, "dialog", logger=self.logger)
        with zipfile.ZipFile(res["zipPath"]) as zf:
            names = zf.namelist()
            self.assertIsNone(zf.testzip())
            self.assertTrue(any(n.endswith("session.v4.jsonl") for n in names))
            blob = zf.read([n for n in names if n.endswith("abc.bin")][0])
            self.assertEqual(len(blob), 300)

    # ── 恢复演练 ──────────────────────────────────────
    def test_drill_restore_matches_file_count(self):
        res = runner.run_layer_once(self.cfg, "dialog", logger=self.logger)
        drill = restore.restore_slot(
            self.cfg, res["slot"], logger=self.logger, to=str(self.tmp / "drill")
        )
        self.assertTrue(drill["ok"], drill)
        self.assertTrue(drill["filesMatch"], f"{drill['files']} vs {drill['expectFiles']}")
        self.assertEqual(drill["files"], 3)
        # 内容真的回来了（dialog 层只含 sessions + attachments，不含 config.yaml）
        got = self.tmp / "drill" / ".dsh" / "sessions" / "slug-a" / "session.v4.jsonl"
        self.assertTrue(got.exists(), f"缺 {got}")
        self.assertEqual(len(got.read_text(encoding="utf-8").splitlines()), 200)
        att = self.tmp / "drill" / ".dsh" / "attachments" / "v1" / "objects" / "ab" / "abc.bin"
        self.assertEqual(len(att.read_bytes()), 300)

    def test_restore_refuses_bad_sha(self):
        res = runner.run_layer_once(self.cfg, "dialog", logger=self.logger)
        m = manifest.read_manifest(Path(res["dir"]))
        m["sha256"] = "0" * 64
        manifest.write_manifest(Path(res["dir"]), m)
        out = restore.restore_slot(self.cfg, res["slot"], logger=self.logger, to=str(self.tmp / "x"))
        self.assertFalse(out["ok"])
        self.assertIn("sha256", out["reason"])

    def test_junction_recorded_and_restored(self):
        """junction 只记清单不进包；落位恢复时用 mklink /J 重建。"""
        import os
        import subprocess
        import zipfile

        real = self.tmp / "real"
        real.mkdir()
        (real / "payload.txt").write_text("payload\n", encoding="utf-8")
        ck = Path(self.cfg["paths"]["checkout"])
        link = ck / "linked"

        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real)],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
        )
        if r.returncode != 0:
            self.skipTest(f"本机不支持 mklink /J：{r.stdout}{r.stderr}")

        res = runner.run_layer_once(self.cfg, "baseline", logger=self.logger)
        self.assertTrue(res["ok"], res)
        m = manifest.read_manifest(Path(res["dir"]))
        self.assertGreaterEqual(m["junctions"], 1, "junction 应被记进清单")
        with zipfile.ZipFile(res["zipPath"]) as zf:
            names = zf.namelist()
        self.assertFalse(any("linked/payload" in n for n in names), "junction 内容不该进包")
        self.assertTrue((Path(res["dir"]) / "junctions-harness.txt").exists())

        # 毁掉 checkout（junction 先 rmdir，避免 rmtree 顺着链接删到 real）
        try:
            os.rmdir(link)
        except OSError:
            pass
        shutil.rmtree(ck, ignore_errors=True)
        self.assertFalse(ck.exists())

        # 落位恢复（把 dsh 端口指到没人监听的端口，绕开「网关在跑」拦截）
        self.cfg["ports"]["dsh"] = 59599
        out = restore.restore_slot(self.cfg, res["slot"], logger=self.logger, assume_yes=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(ck.exists(), "checkout 应被重建")
        self.assertGreaterEqual(out["junctions"]["restored"], 1, out)
        self.assertEqual(out["junctions"]["failed"], [], out)
        self.assertTrue(link.exists(), out)
        self.assertEqual(
            os.path.realpath(link).lower(),
            os.path.realpath(real).lower(),
            "重建的 junction 应指回原目标",
        )
        self.addCleanup(lambda: os.path.isdir(str(link)) and os.rmdir(str(link)))

    # ── 淘汰 ──────────────────────────────────────────
    def test_prune_needs_streak(self):
        res = runner.run_layer_once(self.cfg, "dialog", logger=self.logger)
        slot_dir = Path(res["dir"])
        # 造两个超期包：hold 只留其中最新一份，另一份进 drop，才会触发 streak 门控
        olds = [
            ("2020-01-01T000000-dialog", 1577836800000),
            ("2020-01-02T000000-dialog", 1577923200000),
        ]
        made = []
        for slot, ms in olds:
            fake = self.root / cfgmod.DIR_DAILY / slot.replace("-dialog", "")
            fake.mkdir(parents=True, exist_ok=True)
            m = manifest.read_manifest(slot_dir).copy()
            m.update({"slot": slot, "createdAt": ms})
            manifest.write_manifest(fake, m)
            made.append(fake)

        params = cfgmod.layer_params(self.cfg, "dialog")
        slots = manifest.scan(self.root)
        now_ms = int(time.time() * 1000)
        keep, hold, drop = retention.select_keep(now_ms, slots, params)
        self.assertEqual(len(drop), 1, f"应恰有 1 份候选删除，实际 hold={len(hold)} drop={len(drop)}")

        st = retention_state.load(self.root)
        r1 = retention_state.apply_prune(self.root, "dialog", keep, hold, drop, st, params, self.logger)
        self.assertTrue(r1["deferred"], "streak=0 时必须 deferred")
        self.assertEqual(r1["deleted"], 0)
        self.assertTrue(all(f.exists() for f in made), "deferred 时一份都不能删")

        # 默认 hold_delete_streak=2：连两份新包校验通过才放行
        need = int(params.get("hold_delete_streak", 2))
        for _ in range(need):
            retention_state.note_verified(self.root, "dialog", True)
        st = retention_state.load(self.root)
        r2 = retention_state.apply_prune(self.root, "dialog", keep, hold, drop, st, params, self.logger)
        self.assertEqual(r2["deleted"], 1)
        self.assertFalse(made[0].exists(), "streak 达标后应真删最旧那份")
        self.assertEqual(retention_state.load(self.root)["perLayer"]["dialog"]["streak"], 0, "删完 streak 归零")

    def test_dedup_by_lock_reuses_package(self):
        r1 = runner.run_layer_once(self.cfg, "baseline", logger=self.logger)
        self.assertTrue(r1["ok"], r1)
        cfg = self.cfg
        cfg["layers"]["baseline"]["dedup_by_lock"] = True
        # 第一次跑把指纹写进 state；第二次 lock 未变 ⇒ 复用
        runner.reuse_previous_zip(cfg, "baseline", self.logger)
        reuse = runner.reuse_previous_zip(cfg, "baseline", self.logger)
        self.assertIsNotNone(reuse, "lock 未变时应复用上一份 zip")
        self.assertIn("复用", reuse["note"])


class ConfigValidateTest(unittest.TestCase):
    """配置校验不能被绕过：端口范围、端口撞车、只允许回环。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dshcfg-", dir=Path(__file__).parent.parent / "tmp"))
        for n in ("root", "ck", "dsh", "ws"):
            (self.tmp / n).mkdir()
        self.cfg = cfgmod.default_config()
        self.cfg["paths"].update(
            {
                "backup_root": str(self.tmp / "root"),
                "checkout": str(self.tmp / "ck"),
                "dsh_home": str(self.tmp / "dsh"),
                "workspace": str(self.tmp / "ws"),
            }
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_valid(self):
        self.assertEqual(cfgmod.validate(self.cfg), [])

    def test_server_port_out_of_range(self):
        self.cfg["server"]["port"] = 70000
        errs = cfgmod.validate(self.cfg)
        self.assertTrue(any("server.port" in e for e in errs), errs)

    def test_server_port_collides_with_dsh(self):
        self.cfg["server"]["port"] = 3080
        errs = cfgmod.validate(self.cfg)
        self.assertTrue(any("ports.dsh" in e for e in errs), errs)

    def test_server_port_may_equal_api(self):
        # server.port 与 ports.api 本就是同一个东西，允许相等
        self.cfg["server"]["port"] = 8099
        self.assertEqual(cfgmod.validate(self.cfg), [])

    def test_host_loopback_only(self):
        self.cfg["server"]["host"] = "0.0.0.0"
        errs = [e for e in cfgmod.validate(self.cfg) if "server.host" in e]
        self.assertEqual(len(errs), 1, errs)  # 只报一条，别重复
        self.cfg["server"]["host"] = "::1"
        self.assertEqual([e for e in cfgmod.validate(self.cfg) if "server.host" in e], [])


class SegBoundaryTest(unittest.TestCase):
    """段表 + 份数的硬断言（设计 §7.1/§7.2）。"""

    def test_dialog_declared_counts(self):
        b = retention.seg_bounds(15, 360, 23040)
        self.assertEqual(len(b) + 24, 34)

    def test_baseline_declared_counts(self):
        b = retention.seg_bounds(1440, 1440, 44640)
        self.assertEqual(len(b) + 1, 5)


if __name__ == "__main__":
    unittest.main()
