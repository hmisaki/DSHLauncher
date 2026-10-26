"""把文件清单写成 zip。

设计 §10.4：一律写 `<zip>.tmp`，写完 + sha256 + manifest 落盘之后才原子改名。
半份包在启动时被清掉，用户永远看不到「打了个一半的 dialog.zip」。
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from .manifest import file_sha256


class ArchiveError(Exception):
    pass


def build_zip(
    zip_path: Path,
    files: list[tuple[str, str, int]],
    level: int = 6,
    on_progress=None,
    progress_every: int = 200,
) -> dict:
    """files: [(arcname, abspath, size)]。返回打包统计，失败抛 ArchiveError。"""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    done = 0
    packed = 0
    failed: list[tuple[str, str]] = []

    try:
        with zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=int(level), allowZip64=True
        ) as zf:
            for arcname, abspath, size in files:
                try:
                    zf.write(abspath, arcname)
                    packed += size
                except OSError as exc:
                    # 单文件读失败不中断整包（设计 §13 风险 2）
                    failed.append((abspath, f"{exc.__class__.__name__}: {exc}"))
                done += 1
                if on_progress and (done % progress_every == 0 or done == len(files)):
                    on_progress(done, len(files), packed)
        if done == 0:
            raise ArchiveError("没有任何文件被打进包")
        sha = file_sha256(tmp)
        size = tmp.stat().st_size
        os.replace(tmp, zip_path)
        return {
            "ok": True,
            "zipPath": str(zip_path),
            "zipBytes": size,
            "sha256": sha,
            "filesPacked": done - len(failed),
            "filesFailed": len(failed),
            "failedSamples": failed[:5],
            "sourceBytes": packed,
        }
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def verify_zip(zip_path: Path) -> tuple[bool, str]:
    """能不能打开、条目数是否 > 0。真正的完整性靠恢复前的 sha256 比对。"""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        return False, "文件不存在"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"条目损坏: {bad}"
            n = len(zf.namelist())
            if n == 0:
                return False, "包内没有条目"
            return True, f"{n} 个条目"
    except zipfile.BadZipFile as exc:
        return False, f"不是合法 zip: {exc}"
    except OSError as exc:
        return False, f"无法读取: {exc}"


def cleanup_orphan_tmp(root: Path) -> list[str]:
    """启动时清理残留 `.tmp`，返回被删掉的路径。"""
    removed: list[str] = []
    for p in Path(root).rglob("*.zip.tmp"):
        try:
            p.unlink()
            removed.append(str(p))
        except OSError:
            continue
    return removed
