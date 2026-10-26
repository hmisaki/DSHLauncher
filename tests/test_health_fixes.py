"""体检十条（A–J）里可脱离界面验证的部分：A 兜底索引原子写 / B 日志跨天与轮转 /
C 淘汰审计 / E 磁盘满识别 / F 落位前可改名探测 / G 回收区缺标记退回 mtime。

界面相关的 D（轮换失败留痕）、J（清空文案）由 tmp/ui_selfcheck.py 断言，
这里只放能纯函数验证的部分，保证这些行为不依赖 PySide6 也能回归。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dshbackup import config as cfgmod  # noqa: E402
from dshbackup import engine, log as logmod, retention_state, sevenzip  # noqa: E402


class _Rec:
    """最简 logger 桩：只记事件名与关键字。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warn(self, event: str, **kw) -> dict:
        self.events.append((event, kw))
        return {}

    def info(self, event: str, **kw) -> dict:
        self.events.append((event, kw))
        return {}

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def first(self, event: str) -> dict:
        for e, kw in self.events:
            if e == event:
                return kw
        return {}


class FallbackIndexAtomicTest(unittest.TestCase):
    """A：索引原子写；坏索引改名留档 + 记事件，不静默当空列表。"""

    def test_atomic_write_leaves_no_tmp(self) -> None:
        with TemporaryDirectory() as td:
            target = Path(td) / "bucket" / "index.json"
            target.parent.mkdir(parents=True)
            engine.atomic_write_text(target, json.dumps([{"src": "X"}], ensure_ascii=False))
            self.assertTrue(target.exists())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), [{"src": "X"}])
            self.assertFalse((target.parent / "index.json.tmp").exists())
            # 覆盖写也不留残渣
            engine.atomic_write_text(target, "[]")
            self.assertEqual(target.read_text(encoding="utf-8"), "[]")
            self.assertEqual(
                sorted(p.name for p in target.parent.iterdir()), ["index.json"]
            )

    def test_corrupt_index_is_kept_aside_and_logged(self) -> None:
        with TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            idx.write_text('{"half": ', encoding="utf-8")
            rec = _Rec()
            rows = engine.read_fallback_index(idx, logger=rec)
            self.assertEqual(rows, [])
            self.assertIn("fallback_index_corrupt", rec.names())
            self.assertFalse(idx.exists(), "坏索引应被改名留档，不该留在原位")
            aside = [p.name for p in Path(td).iterdir() if p.name.startswith("index.json.corrupt-")]
            self.assertEqual(len(aside), 1, f"实际目录内容：{[p.name for p in Path(td).iterdir()]}")

    def test_good_index_roundtrip(self) -> None:
        with TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            engine.atomic_write_text(idx, json.dumps([{"src": "A", "zip": "a.7z"}]))
            rec = _Rec()
            self.assertEqual(engine.read_fallback_index(idx, logger=rec),
                             [{"src": "A", "zip": "a.7z"}])
            self.assertEqual(rec.names(), [])


class LogRotationTest(unittest.TestCase):
    """B：今天文件还空着时也读得到昨天；超 30 天的轮转掉。"""

    def test_recent_lines_reads_previous_day_when_today_empty(self) -> None:
        with TemporaryDirectory() as td:
            d = Path(td)
            today = datetime.now().strftime("%Y%m%d")
            yday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            (d / f"backup-{today}.log").write_text("", encoding="utf-8")
            (d / f"backup-{yday}.log").write_text(
                json.dumps({"event": "run_done", "n": 1}) + "\n", encoding="utf-8"
            )
            got = logmod.recent_lines(d, limit=10)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["event"], "run_done")

    def test_rotate_removes_only_older_than_keep_days(self) -> None:
        with TemporaryDirectory() as td:
            d = Path(td)
            yday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            ancient = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
            (d / f"backup-{yday}.log").write_text("{}\n", encoding="utf-8")
            (d / f"backup-{ancient}.log").write_text("{}\n", encoding="utf-8")
            removed = logmod.rotate_logs(d, keep_days=30)
            self.assertIn(f"backup-{ancient}.log", removed)
            self.assertTrue((d / f"backup-{yday}.log").exists())
            self.assertFalse((d / f"backup-{ancient}.log").exists())


def _slot_row(path: Path, created_at: int = 1767225600000) -> dict:
    return {"slot": path.name, "dir": str(path), "kind": "sessions",
            "createdAt": created_at, "files": 1, "bytes": 100}


class PruneAuditTest(unittest.TestCase):
    """C：自动淘汰前先写审计（含 sha256），返回体也带凭据；manifest 缺了不炸。"""

    def _make_slot(self, root: Path, name: str, with_manifest: bool = True) -> Path:
        slot = root / "sessions" / name
        slot.mkdir(parents=True)
        (slot / "a.txt").write_text("x" * 100, encoding="utf-8")
        if with_manifest:
            (slot / "manifest.json").write_text(json.dumps({
                "slot": name, "kind": "sessions", "createdAt": 1767225600000,
                "files": 1, "bytes": 100,
                "parts": [{"label": "sessions", "sha256": "deadbeef" * 8}],
            }), encoding="utf-8")
        return slot

    def _state(self) -> dict:
        return {"perLayer": {"sessions": {"streak": 9, "since": None, "holdSlots": []}}}

    def test_purge_writes_audit_with_sha256(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir(parents=True)
            slot = self._make_slot(root, "sessions-20260101-000000")
            rec = _Rec()
            out = retention_state.apply_prune(
                root, "sessions", [], [], [_slot_row(slot)], self._state(),
                {"hold_delete_streak": 2}, logger=rec,
            )
            self.assertIn("prune_purged", rec.names())
            kw = rec.first("prune_purged")
            self.assertEqual(kw.get("slot"), "sessions-20260101-000000")
            self.assertEqual(kw.get("bytes"), 100)
            self.assertEqual(kw.get("files"), 1)
            self.assertEqual(kw.get("createdAt"), 1767225600000)
            self.assertEqual(kw.get("sha256"), "deadbeef" * 8)
            self.assertTrue(out.get("purged"))
            self.assertEqual(out["purged"][0]["slot"], "sessions-20260101-000000")
            self.assertEqual(out["purged"][0]["sha256"], "deadbeef" * 8)
            self.assertFalse(slot.exists(), "硬删语义不变：目录应已消失")

    def test_missing_manifest_marks_manifest_missing(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir(parents=True)
            slot = self._make_slot(root, "sessions-20260102-000000", with_manifest=False)
            rec = _Rec()
            retention_state.apply_prune(
                root, "sessions", [], [], [_slot_row(slot)], self._state(),
                {"hold_delete_streak": 2}, logger=rec,
            )
            kw = rec.first("prune_purged")
            self.assertTrue(kw.get("manifestMissing"))
            self.assertIsNone(kw.get("sha256"))


class TargetProbeTest(unittest.TestCase):
    """F：落位前先试改名，改不动就提前拒绝，一个字节都不动。"""

    def test_probe_accepts_normal_dir(self) -> None:
        with TemporaryDirectory() as td:
            target = Path(td) / "workspace"
            target.mkdir()
            self.assertIsNone(engine._probe_rename(target))
            self.assertTrue(target.exists(), "探测必须是可逆的：名字要能改回来")

    def test_probe_skips_missing_path(self) -> None:
        with TemporaryDirectory() as td:
            self.assertIsNone(engine._probe_rename(Path(td) / "nope"))

    def test_restore_refuses_before_touching_target(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            slot_dir = root / "workspace" / "workspace-x"
            slot_dir.mkdir(parents=True)
            fake_zip = slot_dir / "workspace.7z"
            fake_zip.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 64)
            tgt = root / "tgt"
            tgt.mkdir()
            (tgt / "keep.txt").write_text("untouched", encoding="utf-8")
            (slot_dir / "manifest.json").write_text(json.dumps({
                "slot": "workspace-x", "kind": "workspace",
                "createdAt": 1767225600000, "files": 1, "bytes": 1,
                "parts": [{"label": "workspace", "zipName": "workspace.7z",
                           "zipPath": str(fake_zip), "target": str(tgt),
                           "sourceFiles": 1, "bytes": 1, "verifiedOk": True}],
            }), encoding="utf-8")

            orig = engine._probe_rename
            engine._probe_rename = lambda _t: "另一个进程打开了它（PermissionError）"
            try:
                res = engine.restore_slot({}, root, "workspace", "workspace-x",
                                          dry_run=False, logger=None)
            finally:
                engine._probe_rename = orig

            self.assertEqual(res.get("blocked"), "target_locked")
            self.assertIn("正被其他程序占用", res.get("reason", ""))
            self.assertIn("先关掉占用它的程序", res.get("reason", ""))
            self.assertEqual((tgt / "keep.txt").read_text(encoding="utf-8"), "untouched")
            self.assertFalse(any(p.name.startswith("tgt.pre-restore-")
                                 for p in root.iterdir()))


class RecycleStampFallbackTest(unittest.TestCase):
    """G：回收区没有 .deleted-at 时按目录 mtime 算年龄，并留 warn。"""

    def test_missing_stamp_detected(self) -> None:
        with TemporaryDirectory() as td:
            slot = Path(td) / "sessions-old"
            slot.mkdir()
            self.assertFalse(engine.has_deleted_stamp(slot))

    def test_age_falls_back_to_mtime(self) -> None:
        with TemporaryDirectory() as td:
            slot = Path(td) / "sessions-old"
            slot.mkdir()
            age_ms = int(time.time() * 1000) - engine.deleted_at_ms(slot)
            self.assertLess(age_ms, 10 * 60 * 1000, "刚建的目录不该被算成很老")

    def test_old_dir_without_stamp_gets_purged_with_warn(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            slot = root / cfgmod.DELETED_DIR / "sessions" / "sessions-old"
            slot.mkdir(parents=True)
            (slot / "x.txt").write_text("y", encoding="utf-8")
            old = time.time() - 40 * 86400
            os.utime(slot, (old, old))
            rec = _Rec()
            res = engine.purge_recycle(root, 30, logger=rec)
            self.assertTrue(res["purged"])
            self.assertFalse(slot.exists())
            self.assertIn("recycle_missing_stamp", rec.names())


class DiskFullDetectTest(unittest.TestCase):
    """E：磁盘满的几种说法都要认出，正常输出不能误判。"""

    def test_recognizes_common_phrasings(self) -> None:
        self.assertTrue(sevenzip.is_disk_full("ERROR: There is not enough space on the disk."))
        self.assertTrue(sevenzip.is_disk_full("7z: 磁盘空间不足"))
        self.assertFalse(sevenzip.is_disk_full("Everything is Ok"))

    def test_run_result_has_disk_full_flag(self) -> None:
        self.assertIn("disk_full", sevenzip.RunResult.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main()
