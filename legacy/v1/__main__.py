"""命令行入口：M0 能打包、M1 能淘汰、M2 能恢复与生成里程碑、M3 能起服务。

用法（根目录 = D:\\DSHLuncher）：
  python -m dshbackup config --init
  python -m dshbackup config --check
  python -m dshbackup run --layer dialog
  python -m dshbackup list
  python -m dshbackup prune --layer dialog            # 只看判定
  python -m dshbackup prune --layer dialog --apply    # 真删（需 streak 达标）
  python -m dshbackup milestone --name 装完-blender
  python -m dshbackup restore --slot 2026-10-25T041500-dialog [--yes]
  python -m dshbackup serve
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import archive, config as cfgmod, log as logmod, manifest, runner


def _root(args) -> Path:
    return Path(args.root).expanduser() if getattr(args, "root", None) else Path(
        cfgmod.DEFAULTS["paths"]["backup_root"]
    )


def _load(args) -> tuple[Path, dict, logmod.Logger]:
    root = _root(args)
    if getattr(args, "config", None):
        # 支持 --config 指定一份配置文件位置（只在初始化时用）
        cfg = cfgmod.default_config()
        cfg["paths"]["backup_root"] = str(root)
    else:
        cfg = cfgmod.load(root)
        cfg["paths"]["backup_root"] = str(root)
    root.mkdir(parents=True, exist_ok=True)
    cfgmod.ensure_dirs(root)
    return root, cfg, logmod.Logger(root / cfgmod.DIR_LOGS)


def cmd_config(args, root: Path, cfg: dict, logger) -> int:
    if args.init:
        cfgmod.save(root, cfg)
        print(f"已写配置：{cfgmod.config_file(root)}")
    errs = cfgmod.validate(cfg)
    if errs:
        logger.error("config_invalid", count=len(errs), errors=errs[:10])
        print("配置校验失败：")
        for e in errs:
            print("  ✗", e)
        return 2
    print("配置校验通过。")
    if args.show:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args, root: Path, cfg: dict, logger) -> int:
    layers = [args.layer] if args.layer != "all" else ["baseline", "dialog", "workspace"]
    rc = 0
    for layer in layers:
        t0 = time.time()
        print(f"[{layer}] 开始（{cfg['paths']['backup_root']}）…", flush=True)
        prog = runner.Progress()

        def tick():
            s = prog.snapshot()
            if s.get("total"):
                pct = 100.0 * s.get("done", 0) / s["total"]
                print(
                    f"  {s.get('phase')}: {s.get('done')}/{s.get('total')} ({pct:.0f}%)",
                    flush=True,
                )

        res = runner.run_layer_once(
            cfg,
            layer,
            note=args.note or "",
            logger=logger,
            progress=prog,
            category=args.category,
            force=args.force,
        )
        if res.get("ok") or res.get("slot"):
            print(
                f"[{layer}] ok  slot={res.get('slot')}  "
                f"zip={logmod.human_bytes(res.get('bytes', 0))}  "
                f"files={res.get('files')}  {int(time.time() - t0)}s"
            )
            print(f"         {res.get('dir')}")
        else:
            rc = 1
            print(f"[{layer}] 失败：{res.get('reason')}")
            if res.get("missingRoots"):
                print("         缺失根：", ", ".join(res["missingRoots"]))
    return rc


def cmd_list(args, root: Path, cfg: dict, logger) -> int:
    slots = manifest.scan(root)
    if not slots:
        print("还没有任何备份。")
        return 0
    now_ms = int(time.time() * 1000)
    rows = [manifest.compact_for_list(s, now_ms) for s in slots]
    if args.layer:
        rows = [r for r in rows if r["layer"] == args.layer]
    print(f"{'层':<10}{'年龄':<14}{'大小':<12}{'文件':>8}  {'校验':<6}{'目录':<9}slot")
    for r in rows:
        print(
            f"{r['layer']:<10}{logmod.human_age(r['ageMinutes']):<14}"
            f"{logmod.human_bytes(r['bytes']):<12}{r['files']:>8}  "
            f"{'✓' if r['verifiedOk'] else '✗':<6}{r['category']:<9}{r['slot']}"
        )
    for layer in ("baseline", "dialog", "workspace"):
        sub = [r for r in rows if r["layer"] == layer]
        if sub:
            print(f"— {layer}: {len(sub)} 份，最远 {logmod.human_age(sub[-1]['ageMinutes'])}")
    return 0


def cmd_prune(args, root: Path, cfg: dict, logger) -> int:
    from . import retention, retention_state

    layers = [args.layer] if args.layer != "all" else ["baseline", "dialog"]
    rc = 0
    for layer in layers:
        params = cfgmod.layer_params(cfg, layer)
        if params is None:
            print(f"[{layer}] 该层不参与自动淘汰，跳过。")
            continue
        slots = manifest.by_layer(manifest.scan(root), layer)
        now_ms = int(time.time() * 1000)
        keep, hold, drop = retention.select_keep(now_ms, slots, params)
        print(
            f"[{layer}] 共 {len(slots)} 份 → 保留 {len(keep)} + 待删区留 {len(hold)}，"
            f"候选删除 {len(drop)}"
        )
        for sl in sorted(keep, key=lambda s: s["createdAt"]):
            age = (now_ms - sl["createdAt"]) / 60000.0
            tag = retention.bucket_label(sl["createdAt"], now_ms, params)
            print(f"    keep {logmod.human_age(age):<14}{tag:<22}{sl['slot']}")
        if drop:
            for sl in drop[:20]:
                print(f"    drop {sl['slot']}")
            if len(drop) > 20:
                print(f"    … 另有 {len(drop) - 20} 份")
        if args.apply:
            st = retention_state.load(root)
            res = retention_state.apply_prune(root, layer, keep, hold, drop, st, params, logger)
            print(f"    实际删除 {res['deleted']} 份；streak={res['streak']}")
            if res.get("deferred"):
                print("    （streak 未达阈值，只记 hold_deferred，未删）")
        else:
            print("    （未加 --apply，只做判定）")
    return rc


def cmd_milestone(args, root: Path, cfg: dict, logger) -> int:
    from . import ps1gen

    name = args.name
    folder = root / cfgmod.DIR_MILESTONE / name
    folder.mkdir(parents=True, exist_ok=True)
    print(f"里程碑目录：{folder}")
    made: dict[str, dict] = {}
    for layer in ("baseline", "dialog", "workspace"):
        print(f"[{layer}] 打包中…", flush=True)
        res = runner.run_layer_once(
            cfg, layer, note=f"里程碑 {name}", logger=logger, target_dir=folder, force=True
        )
        if res.get("ok"):
            made[layer] = res
            print(f"  ok {res['slot']} {logmod.human_bytes(res.get('bytes', 0))}")
        else:
            print(f"  失败：{res.get('reason')}")
    if made:
        cfgres = ps1gen.write_milestone_folder(folder, cfg, made, name, logger)
        print(f"  配置包 + 说明.txt + 恢复.ps1：{cfgres}")
    return 0 if made else 1


def cmd_restore(args, root: Path, cfg: dict, logger) -> int:
    from . import restore

    res = restore.restore_slot(
        cfg,
        args.slot,
        logger=logger,
        targets=args.targets.split(",") if args.targets else None,
        assume_yes=args.yes,
        skip_fallback=args.no_fallback,
        progress=None,
        to=args.to,
    )
    print(json.dumps({k: v for k, v in res.items() if k != "log"}, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_serve(args, root: Path, cfg: dict, logger) -> int:
    from . import api

    return api.serve(cfg, root, logger, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dshbackup", description="DSH 备份/恢复工具")
    p.add_argument("--root", help="备份根目录（默认 D:\\AIBot\\backups）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="初始化/校验/查看配置")
    c.add_argument("--init", action="store_true", help="把当前默认值写成 config.json")
    c.add_argument("--check", action="store_true", help="只校验")
    c.add_argument("--show", action="store_true", help="打印完整配置")

    r = sub.add_parser("run", help="立刻打一份包")
    r.add_argument("--layer", default="dialog", choices=["baseline", "dialog", "workspace", "all"])
    r.add_argument("--category", default=None, help="落哪个目录（默认按层决定）")
    r.add_argument("--note", default="")
    r.add_argument("--force", action="store_true", help="忽略 paused 总闸")

    lst = sub.add_parser("list", help="列出所有备份")
    lst.add_argument("--layer", default=None, choices=["baseline", "dialog", "workspace"])

    pr = sub.add_parser("prune", help="按保留算法判定/执行淘汰")
    pr.add_argument("--layer", default="all", choices=["baseline", "dialog", "all"])
    pr.add_argument("--apply", action="store_true", help="真的删（需要 streak 达标）")

    ms = sub.add_parser("milestone", help="打一个里程碑（四包 + 说明.txt + 恢复.ps1）")
    ms.add_argument("--name", required=True)

    rs = sub.add_parser("restore", help="从某份包恢复")
    rs.add_argument("--slot", required=True)
    rs.add_argument("--targets", default=None, help="只还原这些顶层块，逗号分隔（.dsh,deepseek-harness,WorkSpace）")
    rs.add_argument("--to", default=None, help="演练：只解到这个目录并比对文件数，不碰任何真实路径")
    rs.add_argument("--yes", action="store_true", help="跳过确认")
    rs.add_argument("--no-fallback", action="store_true", help="不打兜底包（不推荐）")

    sv = sub.add_parser("serve", help="起本地 Web 服务（M3）")
    sv.add_argument("--no-browser", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root, cfg, logger = _load(args)
    handlers = {
        "config": cmd_config,
        "run": cmd_run,
        "list": cmd_list,
        "prune": cmd_prune,
        "milestone": cmd_milestone,
        "restore": cmd_restore,
        "serve": cmd_serve,
    }
    return handlers[args.cmd](args, root, cfg, logger)


if __name__ == "__main__":
    sys.exit(main())
