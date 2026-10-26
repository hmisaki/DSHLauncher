"""manifest.json 的读写、slot 命名、目录扫描。

一个时间戳文件夹 = 一次备份事件 = 一份包（ADR §14 裁决 1），
所以 manifest 与 zip 同目录、一一对应。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCAN_DIRS = ("日常备份", "里程碑", "手动备份", "工作区", "兜底")

MANIFEST_NAME = "manifest.json"


def file_sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stamp(ts: datetime) -> str:
    """目录与 slot 用的时间戳：2026-10-25T041500"""
    return ts.strftime("%Y-%m-%dT%H%M%S")


def slot_id(ts: datetime, layer: str) -> str:
    return f"{stamp(ts)}-{layer}"


def make_slot(
    layer: str,
    created_ms: int,
    *,
    slot: str,
    files: int,
    bytes_: int,
    sha256: str,
    zip_name: str,
    source_path: str,
    covers: list[str],
    note: str = "",
    verified_ok: bool = True,
    expire_at=None,
) -> dict:
    ts = datetime.fromtimestamp(created_ms / 1000.0).astimezone()
    return {
        "slot": slot,
        "layer": layer,
        "createdAt": int(created_ms),
        "createdAtIso": ts.isoformat(timespec="seconds"),
        "files": int(files),
        "bytes": int(bytes_),
        "sha256": sha256,
        "zipName": zip_name,
        "sourcePath": source_path,
        "covers": list(covers),
        "expireAt": expire_at,
        "note": note,
        "verifiedAt": int(datetime.now().timestamp() * 1000) if verified_ok else None,
        "verifiedOk": bool(verified_ok),
        "schemaVersion": SCHEMA_VERSION,
    }


def write_manifest(folder: Path, data: dict) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / MANIFEST_NAME
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    return p


def read_manifest(folder: Path) -> dict | None:
    p = Path(folder) / MANIFEST_NAME if Path(folder).is_dir() else Path(folder)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "slot" not in data:
        return None
    return data


def scan(root: Path) -> list[dict]:
    """扫全部目录，返回 slot 列表（已去重、已按 createdAt 降序）。

    去重规则（设计 §7.6）：相同 slot 保留 verifiedOk=true 且较新的一份。
    """
    root = Path(root)
    best: dict[str, dict] = {}
    for dirname in SCAN_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            m = read_manifest(child)
            if not m:
                continue
            entries: list[dict]
            bundles = m.get("bundles")
            if isinstance(bundles, list) and bundles:
                # 里程碑合装：一夹多包，manifest 里 bundles 列每层一份
                entries = []
                for b in bundles:
                    e = dict(b)
                    e["category"] = dirname
                    e["dir"] = str(child)
                    e["bundledIn"] = child.name
                    e["zipPath"] = str(child / e.get("zipName", ""))
                    e["onDisk"] = (child / e.get("zipName", "")).exists()
                    e.setdefault("verifiedOk", bool(m.get("verifiedOk")))
                    if not e.get("createdAt"):
                        e["createdAt"] = m.get("createdAt", 0)
                    if not e.get("createdAtIso"):
                        e["createdAtIso"] = m.get("createdAtIso")
                    entries.append(e)
            else:
                m["dir"] = str(child)
                m["category"] = dirname
                m["zipPath"] = str(child / m.get("zipName", ""))
                m["onDisk"] = (child / m.get("zipName", "")).exists()
                entries = [m]
            for e in entries:
                prev = best.get(e["slot"])
                if prev is None or _better(e, prev):
                    best[e["slot"]] = e
    out = list(best.values())
    out.sort(key=lambda s: s.get("createdAt", 0), reverse=True)
    return out


def _better(a: dict, b: dict) -> bool:
    if bool(a.get("verifiedOk")) != bool(b.get("verifiedOk")):
        return bool(a.get("verifiedOk"))
    return a.get("createdAt", 0) > b.get("createdAt", 0)


def by_layer(slots: list[dict], layer: str) -> list[dict]:
    return [s for s in slots if s.get("layer") == layer]


def compact_for_list(slot: dict, now_ms: int) -> dict:
    """给 UI 的精简视图：层 / 年龄 / 大小 / 校验状态 / 档位标签。"""
    age_ms = now_ms - slot.get("createdAt", now_ms)
    return {
        "slot": slot["slot"],
        "layer": slot.get("layer"),
        "createdAt": slot.get("createdAt"),
        "createdAtIso": slot.get("createdAtIso"),
        "ageMinutes": round(age_ms / 60000.0, 1),
        "bytes": slot.get("bytes", 0),
        "files": slot.get("files", 0),
        "verifiedOk": slot.get("verifiedOk", False),
        "zipName": slot.get("zipName"),
        "dir": slot.get("dir"),
        "category": slot.get("category"),
        "note": slot.get("note", ""),
        "onDisk": slot.get("onDisk", False),
    }


def newest(root: Path, layer: str | None = None) -> dict | None:
    slots = scan(root)
    if layer:
        slots = by_layer(slots, layer)
    return slots[0] if slots else None
