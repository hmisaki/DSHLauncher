"""备份清单（manifest）：一份包一个文件夹，文件夹里一个 manifest.json。

目录约定
  <backup_root>/baseline/2026-10-26T033000/{manifest.json, harness.7z, dsh.7z, links.txt, emptydirs.txt}
  <backup_root>/sessions/2026-10-26T003000/{manifest.json, sessions.7z, ...}
  <backup_root>/store/{.mirror.json, ...镜像内容}
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2
MANIFEST = "manifest.json"
MIRROR_STATE = ".mirror.json"
TS_FMT = "%Y-%m-%dT%H%M%S"


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stamp(ts_ms: int | None = None) -> str:
    ts = (ts_ms if ts_ms is not None else int(time.time() * 1000)) / 1000.0
    return datetime.fromtimestamp(ts).strftime(TS_FMT)


def slot_ms(name: str) -> int:
    try:
        return int(datetime.strptime(name, TS_FMT).timestamp() * 1000)
    except ValueError:
        p = Path(name)
        return int(p.stat().st_mtime * 1000) if p.exists() else 0


def slot_dir(root: str | Path, kind: str, ts_ms: int) -> Path:
    return Path(root) / kind / stamp(ts_ms)


def make_slot(
    kind: str,
    ts_ms: int,
    parts: list[dict],
    *,
    links: int = 0,
    empty_dirs: int = 0,
    note: str = "",
    method: str = "7z",
) -> dict:
    # 单包模式：多个源共用一个归档，bytes 不能按源累加（否则包大小被算 N 遍）
    _seen_arch: set[str] = set()
    total_bytes = 0
    for p in parts:
        _key = str(p.get("zipPath") or p.get("label") or "")
        if _key in _seen_arch:
            continue
        _seen_arch.add(_key)
        total_bytes += int(p.get("bytes", 0))
    total_files = sum(int(p.get("files", 0)) for p in parts)
    source_bytes = sum(int(p.get("sourceBytes", 0)) for p in parts)
    source_files = sum(int(p.get("sourceFiles", 0)) for p in parts)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "slot": stamp(ts_ms),
        "kind": kind,
        "createdAt": ts_ms,
        "createdAtIso": datetime.fromtimestamp(ts_ms / 1000.0).isoformat(timespec="seconds"),
        "method": method,
        "parts": parts,
        "files": total_files,
        "bytes": total_bytes,
        "sourceFiles": source_files,
        "sourceBytes": source_bytes,
        "links": links,
        "emptyDirs": empty_dirs,
        "verifiedOk": all(bool(p.get("verifiedOk", True)) for p in parts) if parts else False,
        "note": note,
    }


def write_manifest(folder: str | Path, slot: dict) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / MANIFEST
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(slot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_manifest(folder: str | Path) -> dict | None:
    p = Path(folder) / MANIFEST
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _enrich(slot: dict, folder: Path, kind: str) -> dict:
    s = dict(slot)
    s["kind"] = kind
    s["dir"] = str(folder)
    s.setdefault("parts", [])
    for part in s["parts"]:
        zp = folder / part.get("zipName", "")
        part["zipPath"] = str(zp)
        part["onDisk"] = zp.exists()
    s["onDisk"] = all(p.get("onDisk") for p in s["parts"]) if s["parts"] else False
    return s


def scan(root: str | Path, kind: str) -> list[dict]:
    """列出某类型的全部包，按 createdAt 降序（最新在前）。"""
    base = Path(root) / kind
    if not base.exists():
        return []
    out: list[dict] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        m = read_manifest(d)
        if not m:
            continue
        out.append(_enrich(m, d, kind))
    out.sort(key=lambda s: s.get("createdAt", 0), reverse=True)
    return out


def scan_all(root: str | Path) -> dict[str, list[dict]]:
    from . import config as cfgmod

    return {k: scan(root, k) for k in cfgmod.KINDS if k != cfgmod.KIND_STORE}


def newest(root: str | Path, kind: str) -> dict | None:
    got = scan(root, kind)
    return got[0] if got else None


def compact(slot: dict, now_ms: int | None = None) -> dict:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    age_min = max(0.0, (now - slot.get("createdAt", now)) / 60000.0)
    return {
        "slot": slot.get("slot"),
        "kind": slot.get("kind"),
        "dir": slot.get("dir"),
        "createdAt": slot.get("createdAt"),
        "createdAtIso": slot.get("createdAtIso"),
        "ageMinutes": round(age_min, 1),
        "files": slot.get("files", 0),
        "bytes": slot.get("bytes", 0),
        "sourceFiles": slot.get("sourceFiles", 0),
        "sourceBytes": slot.get("sourceBytes", 0),
        "verifiedOk": bool(slot.get("verifiedOk")),
        "onDisk": bool(slot.get("onDisk")),
        "links": slot.get("links", 0),
        "emptyDirs": slot.get("emptyDirs", 0),
        "note": slot.get("note", ""),
        "parts": [
            {
                "label": p.get("label"),
                "zipName": p.get("zipName"),
                "files": p.get("files", 0),
                "bytes": p.get("bytes", 0),
            }
            for p in slot.get("parts", [])
        ],
    }


# ---------------- store 镜像状态 ----------------

def read_mirror_state(root: str | Path) -> dict:
    p = Path(root) / "store" / MIRROR_STATE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_mirror_state(root: str | Path, state: dict) -> Path:
    folder = Path(root) / "store"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / MIRROR_STATE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def mirror_slot(root: str | Path) -> dict | None:
    """把 store 镜像包装成一个可列表的 slot（没有槽目录，指向 store 根）。"""
    st = read_mirror_state(root)
    if not st:
        return None
    folder = Path(root) / "store"
    return {
        "slot": "store",
        "kind": "store",
        "dir": str(folder),
        "createdAt": int(st.get("syncedAt", 0)),
        "createdAtIso": st.get("syncedAtIso", ""),
        "files": int(st.get("files", 0)),
        "bytes": int(st.get("bytes", 0)),
        "sourceFiles": int(st.get("sourceFiles", 0)),
        "sourceBytes": int(st.get("sourceBytes", 0)),
        "verifiedOk": bool(st.get("ok", True)),
        "onDisk": folder.exists(),
        "links": 0,
        "emptyDirs": 0,
        "note": st.get("note", ""),
        "parts": [],
        "method": "mirror",
    }


# ---------------- 链接 / 空目录清单 ----------------

LINKS_FILE = "links.txt"
EMPTY_FILE = "emptydirs.txt"


def write_link_lists(folder: str | Path, links: list[dict], empties: list[dict]) -> tuple[Path, Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lp = folder / LINKS_FILE
    ep = folder / EMPTY_FILE
    lp.write_text(
        "".join(f"{l['link']}\t{l['target']}\n" for l in links), encoding="utf-8"
    )
    ep.write_text("".join(f"{e['dir']}\n" for e in empties), encoding="utf-8")
    return lp, ep


def read_link_lists(folder: str | Path) -> tuple[list[dict], list[dict]]:
    folder = Path(folder)
    links: list[dict] = []
    empties: list[dict] = []
    lp = folder / LINKS_FILE
    if lp.exists():
        for line in lp.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                a, _, b = line.partition("\t")
                links.append({"link": a, "target": b})
    ep = folder / EMPTY_FILE
    if ep.exists():
        for line in ep.read_text(encoding="utf-8").splitlines():
            if line.strip():
                empties.append({"dir": line.strip()})
    return links, empties
