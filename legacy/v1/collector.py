"""收集要打包的文件清单（不压缩、不写盘）。

职责单一：把「include/exclude 规则 + 文件系统」变成一份确定的文件列表。
它不认识 zip、不认识 HTTP，方便单测（测试缝之一）。
"""
from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfgmod

# 顶层别名：备份包内的第一层目录名（恢复时靠它决定落到哪）
ALIASES = {
    "dsh_home": ".dsh",
    "checkout": "deepseek-harness",
    "workspace": "WorkSpace",
    "backup_root": "backups",
}


@dataclass
class CollectResult:
    files: list[tuple[str, str, int]] = field(default_factory=list)  # (arcname, abspath, size)
    junctions: list[tuple[str, str]] = field(default_factory=list)   # (link, target)
    total_bytes: int = 0
    missing_roots: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)     # (path, reason)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _alias_roots(cfg: dict) -> list[tuple[str, str]]:
    """[(绝对根, 别名)]，按路径长度降序 —— 最长前缀优先匹配。"""
    paths = cfg["paths"]
    items = [(paths[k], ALIASES[k]) for k in ALIASES if paths.get(k)]
    items.sort(key=lambda x: len(x[0]), reverse=True)
    return items


def arc_prefix(path: str, cfg: dict) -> str:
    """把绝对路径映射到包内相对路径（用 / 分隔）。落不到任何别名下则用盘符目录名。"""
    norm = _norm(path)
    for root, alias in _alias_roots(cfg):
        nroot = _norm(root)
        if norm == nroot:
            return alias
        if norm.startswith(nroot + os.sep):
            rel = os.path.relpath(norm, nroot)
            return alias + "/" + rel.replace(os.sep, "/")
    drive, tail = os.path.splitdrive(norm)
    return tail.strip(os.sep).replace(os.sep, "/")


def _split_excludes(patterns: list[str]) -> tuple[list[str], list[str]]:
    """拆成（目录前缀, 文件通配）。目录前缀用于遍历时整棵剪枝，快且准。"""
    prefixes: list[str] = []
    globs: list[str] = []
    for raw in patterns:
        if not raw:
            continue
        p = _norm(raw)
        tail = "\\**" if "\\" in p else "/**"
        if p.endswith(tail):
            prefixes.append(p[: -len(tail)])
        elif p.endswith("**"):
            prefixes.append(p[:-2].rstrip("\\/"))
        else:
            # 开头的 ** 在 fnmatch 里等价于「任意前缀」
            if p.startswith("**"):
                p = "*" + p[2:]
            globs.append(p)
    return prefixes, globs


class _Filter:
    def __init__(self, patterns: list[str]):
        self.prefixes, self.globs = _split_excludes(patterns)

    def excluded_dir(self, path: str) -> bool:
        n = _norm(path)
        for pref in self.prefixes:
            if n == pref or n.startswith(pref + os.sep):
                return True
        return False

    def excluded_file(self, path: str) -> bool:
        n = _norm(path)
        for pref in self.prefixes:
            if n == pref or n.startswith(pref + os.sep):
                return True
        for g in self.globs:
            if fnmatch.fnmatchcase(n, g):
                return True
        return False


def collect(cfg: dict, layer: str) -> CollectResult:
    """按层配置收集文件。纯函数式：只读文件系统，不写任何东西。"""
    layer_cfg = cfg["layers"][layer]
    out = CollectResult()

    includes = cfgmod.expand(layer_cfg.get("include", []), cfg)
    excludes = cfgmod.expand(layer_cfg.get("exclude", []), cfg)

    if layer == "baseline" and not layer_cfg.get("include_electron", True):
        excludes = excludes + [
            os.path.join(cfg["paths"]["dsh_home"], "electron", "**"),
        ]

    flt = _Filter(excludes)

    for root in includes:
        root = os.path.normpath(root)
        if not os.path.exists(root):
            out.missing_roots.append(root)
            continue
        if os.path.isfile(root):
            if not flt.excluded_file(root):
                _add_file(out, root, arc_prefix(root, cfg))
            continue
        _walk(out, root, flt, cfg)

    out.total_bytes = sum(f[2] for f in out.files)
    return out


def _add_file(out: CollectResult, path: str, arcname: str) -> None:
    try:
        st = os.stat(path)
    except OSError as exc:
        out.skipped.append((path, f"stat 失败: {exc.__class__.__name__}"))
        return
    if not stat.S_ISREG(st.st_mode):
        return
    out.files.append((arcname, path, st.st_size))


def _walk(out: CollectResult, root: str, flt: _Filter, cfg: dict) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        kept: list[str] = []
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if _is_link(p):
                # junction / 符号链接：绝不进 zip（会打进别人的目录），只记录
                out.junctions.append((p, _link_target(p)))
                continue
            if flt.excluded_dir(p):
                continue
            kept.append(d)
        dirnames[:] = kept

        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if _is_link(p):
                out.junctions.append((p, _link_target(p)))
                continue
            if flt.excluded_file(p):
                continue
            _add_file(out, p, arc_prefix(p, cfg))


def _is_link(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        return os.path.isjunction(path)  # Python 3.12+
    except (OSError, AttributeError):
        return False


def _link_target(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def junction_manifest_lines(result: CollectResult) -> list[str]:
    """`路径<TAB>目标` 行，供 junctions-*.txt 用。"""
    return [f"{link}\t{target}" for link, target in result.junctions]


def split_junctions(result: CollectResult, cfg: dict) -> tuple[list[str], list[str]]:
    """把 junction 拆成 harness 树与 dsh 树两份清单。"""
    harness_root = _norm(cfg["paths"]["checkout"])
    dsh_root = _norm(cfg["paths"]["dsh_home"])
    harness: list[str] = []
    dsh: list[str] = []
    other: list[str] = []
    for line in junction_manifest_lines(result):
        link = _norm(line.split("\t", 1)[0])
        if link.startswith(harness_root + os.sep) or link == harness_root:
            harness.append(line)
        elif link.startswith(dsh_root + os.sep) or link == dsh_root:
            dsh.append(line)
        else:
            other.append(line)
    return harness + other, dsh


def summarize(result: CollectResult) -> dict:
    return {
        "files": len(result.files),
        "bytes": result.total_bytes,
        "junctions": len(result.junctions),
        "missingRoots": result.missing_roots,
        "skipped": len(result.skipped),
    }


def preview_paths(cfg: dict, layer: str, limit: int = 20) -> list[str]:
    """UI 用：看一眼这次会打哪些顶层路径（不遍历）。"""
    return [
        str(p)
        for p in cfgmod.expand(cfg["layers"][layer].get("include", []), cfg)[:limit]
    ]
