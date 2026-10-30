"""配置：默认值、读写、占位符展开、校验、按类型取保留参数。

结构约定
  paths.*      各处绝对路径
  tools.*      外部工具（7z / robocopy）
  sets.*       每类备份「压什么、排什么」
  kinds.*      每类的调度与保留参数（c / D / M / interval 见 retention.py）
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

CONFIG_NAME = "config.json"
SCHEMA_VERSION = 2

KIND_STORE = "store"
KIND_BASELINE = "baseline"
KIND_SESSIONS = "sessions"
KIND_WORKSPACE = "workspace"
KINDS = (KIND_STORE, KIND_BASELINE, KIND_SESSIONS, KIND_WORKSPACE)

# UX-04：软删除回收区。删一份包 = 移到 <root>\_deleted\<kind>\<槽名>\，不 shutil.rmtree
DELETED_DIR = "_deleted"

# 界面/日志里给人看的名字（U-06）：原始 kind 是内部目录名，不许直接出现在中文文案里
KIND_LABEL = {
    KIND_STORE: "store",
    KIND_BASELINE: "基线",
    KIND_SESSIONS: "会话",
    KIND_WORKSPACE: "工作区",
}


def kind_label(kind: str) -> str:
    return KIND_LABEL.get(kind, kind)


def human_slot(slot: str) -> str:
    """把 slot id（`baseline-20261026-033000`）变回人能读的时刻。

    U-06：slot 里带着裸 kind（baseline/sessions/store），直接显示等于把内部标识
    摆到用户面前，还白占宽度。类型已经单独一列，这里只留时刻。
    """
    parts = str(slot).rsplit("-", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6 and parts[1].isdigit():
        d, t = parts[1], parts[2]
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
    # 目录名形态：`2026-10-26T043325`
    if "T" in str(slot):
        d, _, t = str(slot).partition("T")
        if len(d) == 10 and len(t) == 6 and t.isdigit():
            return f"{d} {t[:2]}:{t[2:4]}:{t[4:]}"
    return str(slot)

_PKG_ROOT = Path(__file__).resolve().parent.parent
_USER_HOME = Path.home()

DEFAULTS: dict = {
    "schemaVersion": SCHEMA_VERSION,
    "paths": {
        # 默认把备份库放在工具自己目录下的 data\，跟着工具走。
        "backup_root": str(_PKG_ROOT / "data"),
        # DSH 的用户级目录。默认 ~\.dsh，换机器不用改。
        "dsh_home": str(_USER_HOME / ".dsh"),
        # 剩下三项没有通用默认值：填你自己的 checkout / pnpm store / 工作区。
        # 留空时对应那一类备份会跳过并提示，不会乱压别的东西。
        "checkout": "",
        "pnpm_store": "",
        "workspace": "",
        # 网关启动入口。默认用 ~\.dsh\bin\DSH.cmd（桌面快捷方式与开机任务通常都指向它）；
        # 没有就留空，界面上「启动 DSH」会提示去设置页填。
        "dsh_cmd": str(_USER_HOME / ".dsh" / "bin" / "DSH.cmd"),
    },
    "tools": {
        "seven_zip": r"C:\Program Files\7-Zip\7z.exe",
        "robocopy": "robocopy",
    },
    "ports": {"dsh": 3080, "mediainbox": 8080, "voicebox": 17493},
    # 打开界面时若网关没在跑，顺手把它拉起来（用户 2026-10-26 的要求：
    # 「以后最好从这个里面启动，同时能保证备份功能能启动」）。
    # 关掉它 = 界面只是个看备份的窗口，网关得自己双击 DSH.lnk。
    "autoStartDsh": True,
    "commands": {
        "start_cmd": "",
        "stop_cmd": "",
        "restart_cmd": "Start-ScheduledTask -TaskName 'DSH Restart Now'",
        "timeout_seconds": 300,
    },
    "sets": {
        "baseline": {
            "roots": [
                {
                    "label": "harness",
                    "path": "{checkout}",
                    "exclude_dirs": [
                        "node_modules",
                        ".git",
                        ".vite",
                        ".vite-temp",
                        ".pnpm-task-run-state-v1",
                    ],
                },
                {
                    "label": "dsh",
                    "path": "{dsh_home}",
                    # service-manager 里只有服务定义与运行期日志；日志被运行中的服务
                    # 独占，7z 打不开会返回 1（warning），整块基线因此被判失败。
                    "exclude_dirs": ["sessions", "voicebox-cache", "service-manager"],
                },
            ],
            # junction 必须按精确路径排；用通配会把同名真目录一起排掉（踩过）
            "exclude_links": [
                r"dsh\profiles\web\node_modules\dsh-peer",
                r"dsh\profiles\web\node_modules\dsh-voicebox",
                r"dsh\profiles\web\node_modules\@deepseek-ai\dsh-subagent-codex",
            ],
        },
        "sessions": {
            "roots": [
                {"label": "sessions", "path": "{dsh_home}/sessions", "exclude_dirs": []},
            ],
            "exclude_links": [],
        },
        "workspace": {
            # 工作区整目录照原样备（不替用户挑内容）。
            # 想同时备多个工作区目录，就在这里往下加行——label 只影响日志与包内顶层名。
            "roots": [
                {"label": "WorkSpace", "path": "{workspace}", "exclude_dirs": []},
            ],
            "exclude_links": [],
        },
    },
    "kinds": {
        KIND_STORE: {
            "enabled": True,
            "method": "mirror",
            "follow_baseline": True,
        },
        KIND_BASELINE: {
            "enabled": True,
            "method": "7z",
            # 先复制到 slot 暂存目录，再压缩；文件被短暂占用时能明确报告跳过项，
            # 避免 7z 在源目录变化时生成只有部分文件的基线包。
            "stage_copy": True,
            "at": "03:30",
            "c": 720,
            "D": 1440,
            "M": 23040,
            "interval": 1440,
            "hold_delete_streak": 2,
        },
        KIND_SESSIONS: {
            "enabled": True,
            "method": "7z",
            "every_minutes": 15,
            "c": 15,
            "D": 180,
            "M": 21600,
            "interval": 15,
            "hold_delete_streak": 2,
        },
        KIND_WORKSPACE: {
            "enabled": True,
            "method": "7z",
            "manual_only": True,
            # 多个工作区目录打进同一个 7z，包里每个目录以自身名字作顶层
            "single_archive": True,
        },
    },
    "compress": {"level": 5, "threads": "on", "extra_args": []},
    "retention": {"enabled": True, "hold_delete_streak": 2},
    "ui": {
        "window_width": 1080,
        "window_height": 760,
        "restore_dry_run_default": True,
        # 硬伤 4：底栏磁盘态势。50 GB 以下是「该清盘了」的提示线，不是拒绝备份的封锁线
        #（拒绝只发生在 ui.disk_hard_min_gb，默认 5 GB，永远不高于提示线）。
        "disk_low_warn_gb": 50.0,
        "disk_hard_min_gb": 5.0,
        # 回收区：移到 _deleted\ 多少天后自动清掉；超过多少 GB 在底栏提示（绝不自动删）
        "recycle_keep_days": 30,
        "recycle_warn_gb": 5.0,
    },
}


def config_file(root: Path) -> Path:
    return Path(root) / CONFIG_NAME


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config() -> dict:
    return json.loads(json.dumps(DEFAULTS))


def load(root: Path) -> dict:
    p = config_file(root)
    if not p.exists():
        return default_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_config()
    return deep_merge(default_config(), data)


def save(root: Path, cfg: dict) -> Path:
    p = config_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def placeholder_map(cfg: dict) -> dict:
    p = cfg.get("paths", {})
    return {
        "backup_root": str(p.get("backup_root", "")),
        "dsh_home": str(p.get("dsh_home", "")),
        "checkout": str(p.get("checkout", "")),
        "pnpm_store": str(p.get("pnpm_store", "")),
        "workspace": str(p.get("workspace", "")),
    }


def expand(text: str, cfg: dict) -> str:
    table = placeholder_map(cfg)

    def sub(m):
        return table.get(m.group(1), m.group(0))

    return re.sub(r"\{(\w+)\}", sub, str(text)).replace("/", os.sep)


def validate(cfg: dict) -> list[str]:
    errs: list[str] = []

    def need_abs(key: str, value: str, must_exist: bool, optional: bool = False) -> None:
        if not value:
            # 留空 = 这一类备份跳过（engine.backup_kind 会把缺失的 root 列进 missing 并提示），
            # 不当成配置错误——新装用户什么都没填也能先把界面打开。
            if not optional:
                errs.append(f"paths.{key}: 不能为空")
            return
        if not os.path.isabs(value):
            errs.append(f"paths.{key}: 必须是绝对路径，当前 {value!r}")
            return
        if must_exist and not os.path.exists(value):
            errs.append(f"paths.{key}: 路径不存在 {value}")

    paths = cfg.get("paths", {})
    need_abs("backup_root", str(paths.get("backup_root", "")), False)
    need_abs("dsh_home", str(paths.get("dsh_home", "")), True)
    need_abs("checkout", str(paths.get("checkout", "")), True, optional=True)
    need_abs("pnpm_store", str(paths.get("pnpm_store", "")), True, optional=True)
    need_abs("workspace", str(paths.get("workspace", "")), True, optional=True)

    sz = str(cfg.get("tools", {}).get("seven_zip", ""))
    if not sz or not Path(sz).exists():
        errs.append(f"tools.seven_zip: 找不到 7z：{sz}")

    rb = str(cfg.get("tools", {}).get("robocopy", ""))
    if not rb:
        errs.append("tools.robocopy: 不能为空")
    elif os.path.isabs(rb) and not Path(rb).exists():
        errs.append(f"tools.robocopy: 找不到：{rb}")

    seen: dict[int, str] = {}
    for name in ("dsh", "mediainbox", "voicebox"):
        v = cfg.get("ports", {}).get(name)
        if not isinstance(v, int) or not (1 <= v <= 65535):
            errs.append(f"ports.{name}: 必须是 1–65535 的整数，当前 {v!r}")
        elif v in seen:
            errs.append(f"ports.{name}: 与 ports.{seen[v]} 重复（{v}）")
        else:
            seen[v] = name

    for kind in KINDS:
        k = cfg.get("kinds", {}).get(kind, {})
        if not isinstance(k, dict):
            errs.append(f"kinds.{kind}: 必须是对象")
            continue
        if k.get("method") == "7z" and not k.get("manual_only"):
            for f in ("c", "D", "M", "interval"):
                if not isinstance(k.get(f), int) or k.get(f, 0) <= 0:
                    errs.append(f"kinds.{kind}.{f}: 必须是正整数，当前 {k.get(f)!r}")
            if all(isinstance(k.get(f), int) and k.get(f) > 0 for f in ("c", "D", "M")):
                if k["M"] <= k["D"]:
                    errs.append(f"kinds.{kind}.M: 上限必须大于密集期 D")
        if kind == KIND_BASELINE and k.get("method") == "7z":
            at = str(k.get("at", ""))
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                errs.append(f"kinds.{kind}.at: 必须是 HH:MM，当前 {at!r}")

    for kind in (KIND_BASELINE, KIND_SESSIONS, KIND_WORKSPACE):
        roots = cfg.get("sets", {}).get(kind, {}).get("roots", [])
        if not roots:
            errs.append(f"sets.{kind}.roots: 至少要有一个源")
        for r in roots:
            if not r.get("label"):
                errs.append(f"sets.{kind}: 有源缺少 label")
            if not r.get("path"):
                errs.append(f"sets.{kind}.{r.get('label')}: 缺少 path")

    lvl = cfg.get("compress", {}).get("level")
    if not isinstance(lvl, int) or not (0 <= lvl <= 9):
        errs.append(f"compress.level: 必须是 0–9，当前 {lvl!r}")

    return errs


def ensure_dirs(root: Path, cfg: dict) -> None:
    root = Path(root)
    # UX-04：删除改成软删除，回收区必须有家；启动就建好，免得第一次删除时才现建
    for d in (root, root / "logs", root / "state", root / DELETED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        k = cfg.get("kinds", {}).get(kind, {})
        if k.get("enabled", True):
            (root / kind).mkdir(parents=True, exist_ok=True)


def kind_params(cfg: dict, kind: str) -> dict | None:
    """该类型的保留参数（分钟）；不参与淘汰的返回 None。"""
    k = cfg.get("kinds", {}).get(kind, {})
    if k.get("method") != "7z" or k.get("manual_only"):
        return None
    p = {
        "c": int(k.get("c", 0) or 0),
        "D": int(k.get("D", 0) or 0),
        "M": int(k.get("M", 0) or 0),
        "interval": int(k.get("interval", k.get("every_minutes", 0)) or 0),
        "hold_delete_streak": int(
            k.get("hold_delete_streak", cfg.get("retention", {}).get("hold_delete_streak", 2))
        ),
    }
    if not all(p[f] > 0 for f in ("c", "D", "M")):
        return None
    return p


def resolve_sets(cfg: dict, kind: str) -> list[dict]:
    out = []
    for r in cfg.get("sets", {}).get(kind, {}).get("roots", []):
        out.append(
            {
                "label": r.get("label", "root"),
                "path": expand(r.get("path", ""), cfg),
                "exclude_dirs": list(r.get("exclude_dirs", [])),
            }
        )
    return out


def resolve_exclude_links(cfg: dict, kind: str) -> list[str]:
    """把「label\\相对路径」展开成绝对路径。"""
    raw = cfg.get("sets", {}).get(kind, {}).get("exclude_links", [])
    if not raw:
        return []
    roots = {r["label"]: r["path"] for r in resolve_sets(cfg, kind)}
    out = []
    for item in raw:
        item = str(item).replace("/", os.sep)
        label, _, rel = item.partition(os.sep)
        base = roots.get(label)
        if base and rel:
            out.append(os.path.join(base, rel))
    return out


def disk_free_gb(path: str | Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return 0.0


def disk_total_gb(path: str | Path) -> float:
    """R3-5：底栏要写「剩余 42.7 GB（18%）」，分母得是这块盘的总量。"""
    try:
        return shutil.disk_usage(str(path)).total / (1024 ** 3)
    except OSError:
        return 0.0
