"""拼装恢复：把多份不同类别的包按时间顺序解到同一个目录（纯逻辑，不 import Qt）。

与 `engine.restore_slot` 的区别（不要混用）：

| | `restore_slot` | 本模块 `run` |
|---|---|---|
| 目的 | 把**一份**包写回它原始的绝对路径 | 把**多份**包解到一个**指定目录** |
| 动作 | 改名留底 `.pre-restore-*`、重建 junction 指向 | 只解压，不碰真实路径、不重建链接 |
| 失败 | 自动回滚到恢复前 | 留着已解出的文件，报告停在哪一份 |

设计取舍（写在代码里，免得下次又被"复用一下"诱惑）：

* **不复用 `restore_slot`**：它一半的动作是"落位到真实路径"，拼装恢复一个都不要。
* **后解覆盖前解**：按 `createdAt` 升序解压，所以同一路径上后一份包的内容胜出。
  这就是"拼装"的语义——用户勾了一份 3 天前的基线 + 今天早上的会话，
  得到的是"3 天前的底座 + 今天早上覆盖过的会话文件"。
* **每份包解到 `dest\\<顶层名>\\`**，不是直接把包里的 `a.json` 摊在 dest 根上。
  顶层名取该 part 的真实目标路径的最后一段（`{dsh_home}` → `.dsh`、
  `{checkout}` → `deepseek-harness`、`{dsh_home}/sessions` → `sessions`、`{workspace}` → `WorkSpace`）。
  这和 `restore_slot` 试运行时解到 `dest\\<label>\\` 的做法同构——包内路径本来就是
  "相对源根"，少了这一层四个源的文件会全部糊在一起。

  ⚠ 已知限制：真实路径上的**嵌套**（`.dsh\\sessions` 在 `.dsh` 里面）在这里被摊平成一层的
  兄弟目录（`.dsh`、`sessions`）。因为"拼装"要的是"这几份包叠起来是什么样"，
  而不是复刻磁盘布局；若按真实嵌套拼，基线与同库的会话会互相盖，
  用户就永远拿不到"旧底座 + 新会话"的组合。
* **冲突只报告不阻止**：覆盖是用户要的行为，但得先告诉他有 N 个文件会被盖掉，
  不然他解完发现"某几个文件不是我勾的那一刻"会以为是 bug。
* **取消是粗粒度的**：7z 子进程一旦起来就等它这一份解完（中途 kill 会留半个文件）。
  `cancel` 只在**份与份之间**生效，所以 UI 上会说"正在解第 3 份，这份解完就停"。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from dshbackup import manifest, sevenzip
from dshbackup import engine as _engine

SPACE_MARGIN = 1.1
"""空间余量：7z 解压过程会有临时峰值，卡着 1.0 倍放行等于注定失败一次。"""


def _slots_of(root, kind: str) -> list[dict]:
    """该类备份的全部包（manifest.scan 已按 createdAt 降序）。"""
    return manifest.scan(root, kind)


def _find(root, kind: str, slot: str) -> dict | None:
    for s in _slots_of(root, kind):
        if s.get("slot") == slot:
            return s
    return None


def top_of(part: dict) -> str:
    """包在拼装目录里的顶层目录名：取该 part 真实目标的最后一段。

    `C:\\Users\\x\\.dsh` → `.dsh`；`D:\\AIBot\\deepseek-harness` → `deepseek-harness`；
    `...\\.dsh\\sessions` → `sessions`。取不到就退回 label，再退回 `"pkg"`。
    """
    target = str(part.get("target") or "").rstrip("\\/")
    name = Path(target).name if target else ""
    return name or str(part.get("label") or "pkg")


def _part_paths(cfg: dict, part: dict) -> list[str] | None:
    """列一个 part 解出来后**在 dest 里的相对路径**（已加上顶层名，去掉目录项）。

    返回 None 表示这个 part 读不了（包丢了/坏了）——上层据此报 reason，
    不能让一个读不了的包静默地当成"没有冲突"。

    返回的是**原样大小写**：路径要显示给用户看，Windows 比较时才 lower()。
    """
    archive = part.get("zipPath")
    if not archive or not Path(archive).exists():
        return None
    try:
        entries = sevenzip.list_7z(cfg, archive)
    except Exception:  # noqa: BLE001 - 7z 的失败形态不统一，一律当作读不到
        return None
    top = top_of(part)
    out = []
    for raw in entries:
        p = str(raw).strip()
        if not p or p.endswith("\\") or p.endswith("/"):
            continue
        out.append(top + "\\" + p.replace("/", "\\"))
    return out


def plan(cfg: dict, root, selections: list[tuple[str, str]]) -> dict:
    """算这一次拼装要做哪些事：解哪几份、按什么顺序、共多少、哪些路径会被覆盖。

    `selections` 是 [(kind, slot), ...]，顺序无所谓——顺序由 createdAt 决定，
    所以用户先勾新的后勾旧的不会解错。

    返回：
        {ok, items:[{kind, slot, createdAt, parts, files, bytes}],
         order:[(kind, slot), ...],          # createdAt 升序 = 从旧到新
         totalFiles, totalBytes,
         conflicts:[{path, overlappedBy:[slot, ...]}], conflictCount,
         reason?}
    """
    root = Path(root)
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, slot in selections or []:
        key = (str(kind), str(slot))
        if key in seen:
            continue
        seen.add(key)
        s = _find(root, kind, slot)
        if not s:
            return {"ok": False, "items": [], "order": [], "totalFiles": 0,
                    "totalBytes": 0, "conflicts": [], "conflictCount": 0,
                    "reason": f"找不到这一份包：{kind} / {slot}"}
        if not s.get("onDisk"):
            return {"ok": False, "items": [], "order": [], "totalFiles": 0,
                    "totalBytes": 0, "conflicts": [], "conflictCount": 0,
                    "reason": f"包文件已丢失，没法解：{kind} / {slot}"}
        items.append({
            "kind": kind,
            "slot": slot,
            "createdAt": int(s.get("createdAt") or 0),
            "files": int(s.get("files") or 0),
            "bytes": int(s.get("bytes") or 0),
            "parts": [
                {
                    "label": p.get("label") or "",
                    "target": p.get("target") or "",
                    "zipPath": p.get("zipPath") or "",
                    "files": int(p.get("files") or 0),
                    "bytes": int(p.get("bytes") or 0),
                }
                for p in s.get("parts") or []
            ],
        })
    if not items:
        return {"ok": False, "items": [], "order": [], "totalFiles": 0,
                "totalBytes": 0, "conflicts": [], "conflictCount": 0,
                "reason": "一份都没勾"}

    # 从旧到新：后解的覆盖先解的
    items.sort(key=lambda it: (it["createdAt"], it["kind"], it["slot"]))
    order = [(it["kind"], it["slot"]) for it in items]

    # 冲突：同一相对路径出现在 >=2 份包里。只在"已经见过别的包"时记一次，
    # 免得 6 万个条目全部进 dict 再筛（基线一份就 6 万条，内存与耗时都翻倍）。
    # 键用 lower()（Windows 不区分大小写），展示用原样大小写。
    first_slot: dict[str, str] = {}
    conflict_map: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    unreadable: list[str] = []
    for it in items:
        for part in it["parts"]:
            paths = _part_paths(cfg, part)
            if paths is None:
                unreadable.append(f"{it['kind']}/{it['slot']} · {part.get('label') or ''}")
                continue
            for p in paths:
                key = p.lower()
                owner = first_slot.get(key)
                if owner is None:
                    first_slot[key] = it["slot"]
                    display.setdefault(key, p)
                elif owner != it["slot"]:
                    bucket = conflict_map.setdefault(key, [owner])
                    if it["slot"] not in bucket:
                        bucket.append(it["slot"])
    if unreadable:
        return {"ok": False, "items": items, "order": order, "totalFiles": 0,
                "totalBytes": 0, "conflicts": [], "conflictCount": 0,
                "reason": "这些包读不了，没法判定覆盖关系：" + "；".join(unreadable)}

    conflicts = [{"path": display.get(k, k), "overlappedBy": slots}
                 for k, slots in sorted(conflict_map.items())]
    return {
        "ok": True,
        "items": items,
        "order": order,
        "totalFiles": sum(it["files"] for it in items),
        "totalBytes": sum(it["bytes"] for it in items),
        "conflicts": conflicts,
        "conflictCount": len(conflicts),
    }


def check_space(dest, need_bytes: int, *, margin: float = SPACE_MARGIN) -> dict:
    """目标盘放不放得下（含 1.1 倍余量）。dest 不存在时向上找第一个存在的祖先。"""
    p = Path(dest)
    probe = p
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(str(probe)).free
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "freeBytes": 0, "needBytes": int(need_bytes),
                "needWithMargin": int(need_bytes * margin),
                "reason": f"读不到磁盘剩余空间：{exc}"}
    need = int(need_bytes * margin)
    if free < need:
        return {
            "ok": False,
            "freeBytes": free,
            "needBytes": int(need_bytes),
            "needWithMargin": need,
            "reason": "空间不够：要 {:.1f} GB（含 10% 余量），这个盘只剩 {:.1f} GB".format(
                need / 1024 ** 3, free / 1024 ** 3),
        }
    return {"ok": True, "freeBytes": free, "needBytes": int(need_bytes),
            "needWithMargin": need}


def run(cfg: dict, root, plan_result: dict, dest, *, progress=None, cancel=None,
        logger=None) -> dict:
    """按 plan 的 order 逐个解到 dest（后包覆盖前包）。

    `progress(dict)` 会被频繁调用（每一份的每一个百分比），形如
    `{"i": 2, "n": 3, "kind":..., "slot":..., "pct": 58}`。
    `cancel` 是 threading.Event；只在份与份之间检查（见模块 docstring）。

    返回：
        {ok, restored:[{kind, slot, files, bytes, seconds}],
         totalFiles, totalBytes, cancelled, stoppedAt, errors:[...]}
    """
    started = time.monotonic()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    order = list(plan_result.get("order") or [])
    n = len(order)
    restored: list[dict] = []
    errors: list[str] = []
    cancelled = False
    stopped_at = None

    for idx, (kind, slot) in enumerate(order, start=1):
        if cancel is not None and cancel.is_set():
            cancelled = True
            stopped_at = idx - 1
            break
        s = _find(root, kind, slot)
        if not s:
            errors.append(f"第 {idx} 份找不到：{kind} / {slot}")
            stopped_at = idx
            break
        t0 = time.monotonic()
        for part in s.get("parts") or []:
            archive = part.get("zipPath")
            label = part.get("label") or ""
            # 每一 part 解到自己的顶层目录（见模块 docstring 的布局说明）
            sub_dest = dest / top_of(part)

            def _on_pct(m, _idx=idx, _kind=kind, _slot=slot, _label=label):
                if progress is not None:
                    progress({"i": _idx, "n": n, "kind": _kind, "slot": _slot,
                              "label": _label, "pct": m})

            res = sevenzip.extract_7z(cfg, archive, sub_dest, on_progress=_on_pct)
            if not res.ok:
                errors.append(
                    f"第 {idx} 份（{kind} · {slot} · {label}）解压失败：{str(res.tail)[-300:]}"
                )
                stopped_at = idx
                break
        if errors:
            break
        restored.append({
            "kind": kind,
            "slot": slot,
            "files": int(s.get("files") or 0),
            "bytes": int(s.get("bytes") or 0),
            "seconds": round(time.monotonic() - t0, 2),
        })

    # 解完以**磁盘上真实存在**的东西为准，不拿清单里的数字充数
    real_files, real_bytes = _engine.count_tree(dest, [])
    ok = not errors and not cancelled and len(restored) == n
    out = {
        "ok": ok,
        "dest": str(dest),
        "restored": restored,
        "wantCount": n,
        "totalFiles": real_files,
        "totalBytes": real_bytes,
        "cancelled": cancelled,
        "stoppedAt": stopped_at,
        "errors": errors,
        "seconds": round(time.monotonic() - started, 2),
    }
    if logger is not None:
        try:
            logger.event("restore_batch", ok=ok, dest=str(dest),
                         count=len(restored), want=n, files=real_files,
                         bytes=real_bytes, cancelled=cancelled, errors=errors[:3])
        except Exception:  # noqa: BLE001 - 日志失败不能影响恢复结果
            pass
    return out


__all__ = ["SPACE_MARGIN", "plan", "check_space", "run"]
