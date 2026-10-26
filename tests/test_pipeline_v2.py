"""管道单测：真 7z 打包 → manifest → 演练恢复逐文件比对 → store 镜像。

用真实 7z.exe / robocopy（本机已确认：7-Zip 26.05、C:\\WINDOWS\\system32\\Robocopy.exe）。
样本树造在 tmp\\ 下，跑完即删；不接触任何真实备份与 DSH 本体。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dshbackup import config as cfgmod  # noqa: E402
from dshbackup import engine, manifest, sevenzip  # noqa: E402


def make_tree(base: Path) -> Path:
    """造一棵小样本树：2 个文件 + 1 个子目录 + 1 条 junction。"""
    src = base / "src"
    (src / "sub").mkdir(parents=True, exist_ok=True)
    (src / "a.json").write_text('{"hello":"世界"}', encoding="utf-8")
    (src / "sub" / "b.txt").write_text("x" * 500, encoding="utf-8")
    return src


def make_junction(link: Path, target: Path) -> bool:
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )
    return r.returncode == 0 and link.exists()


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.root = self.base / "backups"
        self.src = make_tree(self.base)
        self.junction = self.src / "linked"
        self.has_junction = make_junction(self.junction, self.src / "sub")

        cfg = cfgmod.default_config()
        cfg["paths"]["backup_root"] = str(self.root)
        cfg["sets"]["sessions"] = {
            "roots": [
                {
                    "label": "sessions",
                    "path": str(self.src),
                    "exclude_dirs": [],
                }
            ],
            "exclude_links": [],
        }
        cfg["kinds"]["sessions"].update({"c": 15, "D": 180, "M": 21600, "interval": 15})
        self.cfg = cfg
        cfgmod.ensure_dirs(self.root, cfg)

    def tearDown(self) -> None:
        if self.has_junction and self.junction.exists():
            try:
                os.rmdir(self.junction)
            except OSError:
                pass
        self._td.cleanup()

    # ---------- 打包 ----------

    def test_pack_creates_verifiable_archive(self) -> None:
        res = engine.backup_kind(self.cfg, self.root, "sessions", note="单测", progress=None, logger=None)
        self.assertTrue(res.get("ok"), res)
        slot = res["slot"]
        folder = self.root / "sessions" / slot
        self.assertTrue((folder / "sessions.7z").exists())
        self.assertTrue((folder / manifest.MANIFEST).exists())

        man = manifest.read_manifest(folder)
        self.assertIsNotNone(man)
        self.assertEqual(man["schemaVersion"], 2)
        self.assertEqual(man["kind"], "sessions")
        self.assertEqual(man["files"], 2, "junction 不展开，只应计入 2 个真实文件")
        self.assertTrue(man["verifiedOk"])
        self.assertGreater(man["bytes"], 0)
        self.assertEqual(len(man["parts"][0]["sha256"]), 64)

        # 独立校验：7z t
        chk = sevenzip.test_7z(self.cfg, folder / "sessions.7z")
        self.assertTrue(chk.ok, chk.tail)

    def test_junction_recorded_not_packed(self) -> None:
        if not self.has_junction:
            self.skipTest("本机无法创建 junction")
        res = engine.backup_kind(self.cfg, self.root, "sessions", note="", progress=None, logger=None)
        folder = self.root / "sessions" / res["slot"]
        man = manifest.read_manifest(folder)
        self.assertEqual(man["links"], 1, "junction 要记进 links.txt")
        self.assertTrue((folder / "links.txt").exists())

        listing = sevenzip.list_7z(self.cfg, folder / "sessions.7z")
        joined = "\n".join(listing).replace("/", "\\").lower()
        self.assertNotIn("linked\\sub", joined, "junction 下的内容不能被展开进包")
        self.assertNotIn("linked\\a.json", joined, "junction 下的内容不能被展开进包")

    # ---------- 恢复演练 ----------

    def test_drill_restore_matches_file_count(self) -> None:
        res = engine.backup_kind(self.cfg, self.root, "sessions", note="", progress=None, logger=None)
        slot = res["slot"]
        drill = self.base / "drill"
        out = engine.restore_slot(
            self.cfg, self.root, "sessions", slot, dest=str(drill), dry_run=True, logger=None
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("filesMatch"), f"逐文件比对失败：{out}")
        self.assertEqual(out["files"], out["expectFiles"])
        # restore_slot(dest=...) 不做重定位：归档里那一块按**源根目录名**落到 dest 下。
        # 本测试的 sessions 源根是 <base>\src，所以铺成 <drill>\src\...
        drill_root = drill / self.src.name
        self.assertTrue((drill_root / "a.json").exists())
        self.assertGreater((drill_root / "sub" / "b.txt").stat().st_size, 400)
        # 中文内容没被编码搞坏
        text = (drill_root / "a.json").read_text(encoding="utf-8")
        self.assertIn("世界", json.loads(text)["hello"])

    def test_dry_run_preview_lists_parts(self) -> None:
        res = engine.backup_kind(self.cfg, self.root, "sessions", note="", progress=None, logger=None)
        out = engine.restore_slot(
            self.cfg, self.root, "sessions", res["slot"], dry_run=True, logger=None
        )
        self.assertTrue(out.get("dryRun"))
        self.assertEqual(len(out["parts"]), 1)
        self.assertEqual(out["parts"][0]["target"], str(self.src))

    # ---------- 淘汰 ----------

    def test_prune_keeps_dense_period(self) -> None:
        for _ in range(3):
            engine.backup_kind(self.cfg, self.root, "sessions", note="", progress=None, logger=None)
            time.sleep(1.05)  # slot 名精确到秒，间隔不足会互相覆盖
        slots = manifest.scan(self.root, "sessions")
        self.assertEqual(len(slots), 3, "3 份都在密集期，一份都不该删")

    # ---------- store 镜像 ----------

    def test_mirror_dir_syncs_tree(self) -> None:
        dst = self.base / "mirror"
        r = sevenzip.mirror_dir(self.cfg, self.src, dst)
        self.assertTrue(r.ok, r.tail)
        self.assertTrue((dst / "a.json").exists())
        self.assertTrue((dst / "sub" / "b.txt").exists())

        # 再镜像一次（增量）：删掉一个源文件，镜像后目标也应消失（/E 不删；这里只验证不报错）
        (self.src / "sub" / "b.txt").unlink()
        r2 = sevenzip.mirror_dir(self.cfg, self.src, dst)
        self.assertTrue(r2.ok, r2.tail)

    # ---------- 配置 ----------

    def test_config_roundtrip(self) -> None:
        p = cfgmod.save(self.root, self.cfg)
        self.assertTrue(p.exists())
        back = cfgmod.load(self.root)
        self.assertEqual(back["kinds"]["sessions"]["c"], 15)
        self.assertEqual(cfgmod.validate(back), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
