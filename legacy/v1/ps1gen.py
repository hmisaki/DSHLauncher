"""里程碑文件夹：说明.txt + 恢复.ps1 + 配置包。

设计 §8.2 要求「工具不在也能手工恢复」，所以这里生成的两份文件
必须**只依赖 Windows 自带命令**，不 import 本工具任何东西。
"""
from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

INFO_NAME = "说明.txt"
PS1_NAME = "恢复.ps1"
CONFIG_ZIP = "config.zip"

PS1_TEMPLATE = r"""# 恢复.ps1 — 从一个里程碑文件夹把整台机器还原回去
# 用法（管理员 PowerShell 更稳；普通权限也能跑，只是 mklink 可能被拒）：
#   powershell -ExecutionPolicy Bypass -File .\恢复.ps1
#   powershell -ExecutionPolicy Bypass -File .\恢复.ps1 -NoJunctions      # 不重建 junction
#   powershell -ExecutionPolicy Bypass -File .\恢复.ps1 -DrillOnly        # 只解到临时目录看，不动真机
param(
  [switch]$NoJunctions,
  [switch]$DrillOnly,
  [string]$MilestoneDir = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$DshHome  = '__DSH_HOME__'
$Checkout = '__CHECKOUT__'
$WorkSpace = '__WORKSPACE__'
$BackupRoot = '__BACKUP_ROOT__'

function Say($m) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $m" }

Say "里程碑目录: $MilestoneDir"

# 0) 兜底：把现在的东西先复制一份（不动原件）
$fallback = Join-Path $BackupRoot "兜底\手工-$ts"
New-Item -ItemType Directory -Path $fallback -Force | Out-Null
Say "兜底区: $fallback"

# 1) 解基线包到临时区
$tmp = Join-Path $BackupRoot "tmp\restore-$ts"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$baseZip = Join-Path $MilestoneDir 'baseline.zip'
if (Test-Path $baseZip) {
  Say "解压基线包（这可能要几分钟）…"
  Expand-Archive -Path $baseZip -DestinationPath $tmp -Force
  $n = (Get-ChildItem $tmp -Recurse -File | Measure-Object).Count
  Say "解开 $n 个文件到 $tmp"
} else {
  Say "！没有 baseline.zip，跳过基线还原"
}

if ($DrillOnly) {
  Say "DrillOnly：只解到这里，不动真机。目录：$tmp"
  Say "顺手把对话包也解开："
  $dlgZip = Join-Path $MilestoneDir 'dialog.zip'
  if (Test-Path $dlgZip) { Expand-Archive -Path $dlgZip -DestinationPath (Join-Path $tmp 'dialog') -Force }
  exit 0
}

# 2) 落位：改名旧的（不删），再移动新的
function Place($src, $dst, $label) {
  if (-not (Test-Path $src)) { Say "跳过 $label（包内没有）"; return }
  if (Test-Path $dst) {
    Copy-Item $dst (Join-Path $fallback (Split-Path $dst -Leaf)) -Recurse -Force -ErrorAction SilentlyContinue
    $bak = "$dst.pre-restore-$ts"
    Say "改名 $dst → $bak"
    Rename-Item -Path $dst -NewName (Split-Path $bak -Leaf)
  }
  Say "落位 $label → $dst"
  Move-Item -Path $src -Destination $dst
}

Place (Join-Path $tmp 'deepseek-harness') $Checkout '同步检出目录'
Place (Join-Path $tmp '.dsh') $DshHome 'DSH 主目录'

# 3) 对话包：只把 sessions / attachments 挪回去
$dlg = Join-Path $MilestoneDir 'dialog.zip'
if (Test-Path $dlg) {
  $dlgTmp = Join-Path $tmp 'dialog'
  New-Item -ItemType Directory -Path $dlgTmp -Force | Out-Null
  Expand-Archive -Path $dlg -DestinationPath $dlgTmp -Force
  foreach ($sub in @('sessions', 'attachments')) {
    $s = Join-Path $dlgTmp ".dsh\$sub"
    $d = Join-Path $DshHome $sub
    if (Test-Path $s) {
      if (Test-Path $d) {
        Copy-Item $d (Join-Path $fallback $sub) -Recurse -Force -ErrorAction SilentlyContinue
        Rename-Item -Path $d -NewName "$sub.pre-restore-$ts"
      }
      Move-Item -Path $s -Destination $d
      Say "对话块落位: $sub"
    }
  }
}

# 4) 重建 junction（每行：链接路径<TAB>目标）
if (-not $NoJunctions) {
  $h = Join-Path $MilestoneDir 'junctions-harness.txt'
  $d = Join-Path $MilestoneDir 'junctions-dsh.txt'
  foreach ($f in @($h, $d)) {
    if (-not (Test-Path $f)) { continue }
    Say "重建链接: $(Split-Path $f -Leaf)"
    Get-Content $f -Encoding UTF8 | Where-Object { $_ -match "`t" } | ForEach-Object {
      $parts = $_ -split "`t", 2
      $link = $parts[0]; $target = $parts[1]
      if (-not (Test-Path $target)) { Say "  跳过（目标不存在）$link"; return }
      if (Test-Path $link) { cmd /c rmdir "$link" 2>$null | Out-Null }
      cmd /c mklink /J "$link" "$target" | Out-Null
      if ($LASTEXITCODE -eq 0) { Say "  ok $link" } else { Say "  失败（需要管理员权限）$link" }
    }
  }
}

Say "完成。旧状态在: $fallback 以及各 *.pre-restore-$ts"
Say "起网关验证：双击桌面 DSH.lnk，或 Start-ScheduledTask -TaskName 'DSH Gateway'"
"""


def _info_text(name: str, made: dict, cfg: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"里程碑：{name}",
        f"打包时间：{ts}",
        f"备份根目录：{cfg['paths']['backup_root']}",
        "",
        "这个文件夹是什么",
        "──────────────",
        "它是一个「整台机器此刻可用的快照」。文件夹里的每一份 zip 都是独立的整包，",
        "不依赖任何其它包、不依赖本工具、也不需要网络和 pnpm。",
        "",
        "里面有什么",
        "──────────",
    ]
    for layer, label in (("baseline", "基线包 baseline.zip（DSH 本体 + 检出目录）"),
                         ("dialog", "对话包 dialog.zip（sessions + attachments）"),
                         ("workspace", "工作区块 workspace.zip（文档 / 画布 / 素材）")):
        r = made.get(layer)
        if r:
            lines.append(f"  {label}")
            lines.append(f"    {r.get('files')} 个文件，{_gb(r.get('bytes', 0))}")
        else:
            lines.append(f"  {label} —— 这次没打成")
    lines += [
        f"  配置包 {CONFIG_ZIP}（这一刻的配置 + 环境快照，给人看，不参与恢复）",
        f"  {PS1_NAME}（一键恢复脚本）",
        "",
        "怎么恢复",
        "────────",
        "最省事的办法：右键 恢复.ps1 → 用 PowerShell 运行。它自己会先兜底、再改名、再落位。",
        "只想看看包里有什么：",
        "    powershell -ExecutionPolicy Bypass -File .\\恢复.ps1 -DrillOnly",
        "",
        "不想用脚本？手工就四步（PowerShell）：",
        '    # 1) 先给自己拍一份兜底，把现在的 sessions 复制到别处',
        '    Copy-Item "C:\\Users\\<你>\\.dsh\\sessions" "D:\\备用\\sessions-$(Get-Date -f yyyyMMddHHmm)" -Recurse',
        "    # 2) 解压基线包到临时目录",
        '    Expand-Archive -Path ".\\baseline.zip" -DestinationPath "D:\\tmp\\restore" -Force',
        "    # 3) 目视核对：临时目录里应该有 deepseek-harness\\ 与 .dsh\\",
        '    Get-ChildItem "D:\\tmp\\restore"',
        "    # 4) 落位（先改名旧的，绝不删）",
        '    Rename-Item "D:\\AIBot\\deepseek-harness" "deepseek-harness.bak-$(Get-Date -f yyyyMMddHHmm)"',
        '    Move-Item "D:\\tmp\\restore\\deepseek-harness" "D:\\AIBot\\deepseek-harness"',
        '    Rename-Item "C:\\Users\\<你>\\.dsh" ".dsh.bak-$(Get-Date -f yyyyMMddHHmm)"',
        '    Move-Item "D:\\tmp\\restore\\.dsh" "C:\\Users\\<你>\\.dsh"',
        "    # 5) 重建链接（每行是 链接路径<TAB>目标），然后双击 DSH.lnk 起网关",
        "",
        "恢复不需要 pnpm、不需要联网。这条包里已经含 node_modules 实体。",
        "",
        "三条硬规矩（脚本和你自己都按这个来）",
        "──────────────────",
        "1. 永不原地删：旧的一律改名成 *.pre-restore-<时间> 或 *.bak-<时间>，留着退路。",
        "2. 兜底优先：兜底没成功就别落位。",
        "3. 记得旧状态在哪：本文件夹同级的 兜底\\ 目录 + 各 .pre-restore-* 就是退路。",
        "",
    ]
    return "\n".join(lines)


def _gb(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def write_milestone_folder(folder: Path, cfg: dict, made: dict, name: str, logger=None) -> dict:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    ps1 = folder / PS1_NAME
    ps1.write_text(
        PS1_TEMPLATE.replace("__DSH_HOME__", cfg["paths"]["dsh_home"])
        .replace("__CHECKOUT__", cfg["paths"]["checkout"])
        .replace("__WORKSPACE__", cfg["paths"]["workspace"])
        .replace("__BACKUP_ROOT__", cfg["paths"]["backup_root"]),
        encoding="utf-8-sig",  # 带 BOM，Windows PowerShell 5.1 才不会把中文读成乱码
    )

    (folder / INFO_NAME).write_text(_info_text(name, made, cfg), encoding="utf-8-sig")

    # 配置包：这一刻的配置 + 环境快照
    snap = {
        "milestone": name,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "slots": {k: {kk: v.get(kk) for kk in ("slot", "files", "bytes", "sha256", "createdAtIso")} for k, v in made.items()},
        "environment": _environment(),
    }
    cfg_zip = folder / CONFIG_ZIP
    with zipfile.ZipFile(cfg_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        zf.writestr("snapshot.json", json.dumps(snap, ensure_ascii=False, indent=2))

    if logger:
        logger.info("milestone_done", name=name, layers=list(made.keys()), folder=str(folder))

    # 合装 manifest：一夹四包，scan 靠它把每层拆回独立 slot 显示
    from . import manifest as manmod

    now_ms = int(datetime.now().timestamp() * 1000)
    bundles = []
    for layer, res in made.items():
        if not res.get("ok"):
            continue
        bundles.append(
            {
                k: res.get(k)
                for k in (
                    "slot",
                    "layer",
                    "zipName",
                    "files",
                    "bytes",
                    "sourceBytes",
                    "sha256",
                    "createdAt",
                    "createdAtIso",
                    "verifiedOk",
                    "covers",
                    "note",
                )
                if res.get(k) is not None
            }
        )
    if bundles:
        manmod.write_manifest(
            folder,
            {
                "schemaVersion": manmod.SCHEMA_VERSION,
                "kind": "milestone",
                "name": name,
                "slot": f"milestone-{name}",
                "layer": "milestone",
                "createdAt": now_ms,
                "createdAtIso": datetime.fromtimestamp(now_ms / 1000.0).isoformat(timespec="seconds"),
                "verifiedOk": all(b.get("verifiedOk") for b in bundles),
                "bundles": bundles,
            },
        )

    return {
        "folder": str(folder),
        "info": str(folder / INFO_NAME),
        "ps1": str(ps1),
        "configZip": str(cfg_zip),
    }


def _environment() -> dict:
    import os
    import shutil
    import sys

    return {
        "python": sys.version.split()[0],
        "pythonExe": sys.executable,
        "cwd": os.getcwd(),
        "freeDiskGb": round(shutil.disk_usage("D:\\").free / 1024 ** 3, 1) if Path("D:\\").exists() else None,
        "platform": sys.platform,
    }


def copy_config_example(dest: Path) -> Path:
    """把当前配置（或默认值）写成一份示例文件；全是路径与端口，不含任何机密。"""
    from . import config as cfgmod

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = cfgmod.config_file(Path(cfgmod.DEFAULTS["paths"]["backup_root"]))
    if src.exists():
        text = src.read_text(encoding="utf-8")
    else:
        text = json.dumps(cfgmod.DEFAULTS, ensure_ascii=False, indent=2) + "\n"
    dest.write_text(text, encoding="utf-8")
    return dest
