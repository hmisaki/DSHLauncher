"""待删区状态与「连续 N 份校验通过」门控。

设计 §7.5：超期包先进 hold，不立刻删；每写完一份新包且校验通过，streak += 1；
达到阈值才真正删 drop，然后 streak 归 0。校验失败则 streak 归 0。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

STATE_NAME = "retention.json"


def _path(root: Path) -> Path:
    return Path(root) / "state" / STATE_NAME


def load(root: Path) -> dict:
    p = _path(root)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("perLayer", {})
    return data


def save(root: Path, state: dict) -> None:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def layer_state(state: dict, layer: str) -> dict:
    return state["perLayer"].setdefault(layer, {"streak": 0, "since": None, "holdSlots": []})


def note_verified(root: Path, layer: str, ok: bool, slot: str | None = None) -> dict:
    """新包写完后调用：校验通过则 streak+1，失败清零。返回该层状态副本。"""
    state = load(root)
    ls = layer_state(state, layer)
    if ok:
        ls["streak"] = int(ls.get("streak", 0)) + 1
        ls["since"] = ls.get("since") or None
        if slot:
            ls["lastOkSlot"] = slot
    else:
        ls["streak"] = 0
    save(root, state)
    return dict(ls)


def apply_prune(
    root: Path,
    layer: str,
    keep: list[dict],
    hold: list[dict],
    drop: list[dict],
    state: dict,
    params: dict,
    logger=None,
) -> dict:
    """在 streak 门控下执行删除。返回 {deleted, deferred, streak}。"""
    root = Path(root)
    ls = layer_state(state, layer)
    streak = int(ls.get("streak", 0))
    need = int(params.get("hold_delete_streak", 2))
    ls["holdSlots"] = [s["slot"] for s in hold]

    if not drop:
        save(root, state)
        if logger:
            logger.info("prune_done", layer=layer, deleted=0, kept=len(keep) + len(hold))
        return {"deleted": 0, "deferred": False, "streak": streak, "kept": len(keep) + len(hold)}

    if streak < need:
        save(root, state)
        if logger:
            logger.warn(
                "hold_deferred", layer=layer, streak=streak, need=need, candidates=len(drop)
            )
        return {"deleted": 0, "deferred": True, "streak": streak, "kept": len(keep) + len(hold)}

    deleted = 0
    failed: list[str] = []
    purged: list[dict] = []
    for s in drop:
        d = s.get("dir")
        if not d:
            continue
        # 体检 C：自动淘汰是真删（没有 5 秒撤销、没有回收区），删前必须留痕。
        # 策略参数改错一次数据就真没了，事后至少要能回答「删了什么、多大、当时是哪一份」。
        trace = _purge_trace(Path(d), s, layer)
        try:
            shutil.rmtree(d)
            deleted += 1
            purged.append(trace)
        except OSError as exc:
            failed.append(f"{s.get('slot')}: {exc}")
        if logger:
            logger.warn(
                "prune_purged",
                layer=layer,
                slot=trace.get("slot"),
                bytes=trace.get("bytes"),
                files=trace.get("files"),
                createdAt=trace.get("createdAt"),
                sha256=trace.get("sha256"),
                manifestMissing=trace.get("manifestMissing", False),
                ok=trace.get("slot") is not None and "error" not in trace,
                error=trace.get("error"),
            )
    ls["streak"] = 0
    ls["lastPruneAt"] = None
    save(root, state)
    if logger:
        logger.info(
            "prune_done",
            layer=layer,
            deleted=deleted,
            kept=len(keep) + len(hold),
            failed=len(failed),
            failedSamples=failed[:5],
        )
    return {"deleted": deleted, "deferred": False, "streak": 0, "kept": len(keep) + len(hold),
            "failed": failed, "purged": purged}


def _purge_trace(slot_dir: Path, slot: dict, layer: str) -> dict:
    """删之前从该槽的 manifest.json 抄一份身份信息，供 `prune_purged` 事件使用。

    读不到 manifest（旧格式/已被手删）不算错——记 manifestMissing，其余字段退回扫描结果。
    """
    trace: dict = {
        "slot": slot.get("slot") or slot_dir.name,
        "layer": layer,
        "dir": str(slot_dir),
        "bytes": int(slot.get("bytes") or 0),
        "files": int(slot.get("files") or 0),
        "createdAt": slot.get("createdAt"),
        "manifestMissing": True,
        "sha256": None,
    }
    mf = slot_dir / "manifest.json"
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return trace
    except json.JSONDecodeError:
        return trace
    if isinstance(data, dict):
        trace["manifestMissing"] = False
        trace["slot"] = data.get("slot", trace["slot"])
        trace["bytes"] = int(data.get("bytes") or trace["bytes"] or 0)
        trace["files"] = int(data.get("files") or trace["files"] or 0)
        trace["createdAt"] = data.get("createdAt") or trace["createdAt"]
        trace["sha256"] = data.get("sha256")
        if trace["sha256"] is None:
            for part in data.get("parts") or []:
                if isinstance(part, dict) and part.get("sha256"):
                    trace["sha256"] = part["sha256"]
                    break
    return trace
