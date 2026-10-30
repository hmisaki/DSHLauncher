"""外部工具封装：7-Zip 压缩/解包、robocopy 镜像、目录扫描（链接与空目录）。

设计要点
  · 7z 一律以「源目录为工作目录」调用，包内路径就是相对路径，排除项写法稳定
  · junction / symlink 不跟随、不入包，只记进清单，恢复时 mklink /J 重建
  · 空目录 7z 会丢，单独扫描出清单，恢复时补建
  · 进度靠解析 stdout 百分比（-bsp1 / robocopy 自带），失败按返回码判定
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[float, str], None]

_PCT_7Z = re.compile(r"^\s*(\d{1,3})%")
_PCT_RB = re.compile(r"^\s*([\d.]+)%")


@dataclass
class RunResult:
    ok: bool
    code: int
    seconds: float
    cmd: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)
    error: str = ""
    # 体检 E：磁盘满不该只显示「失败了」——用户能自己解决，得让他看见原因
    disk_full: bool = False

    @property
    def tail_text(self) -> str:
        return "\n".join(self.tail[-12:])


# 7z 在磁盘满时的几种说法（英文/中文版都见），外加 Windows 的 ERROR_DISK_FULL
_DISK_FULL_HINTS = (
    "not enough space",
    "there is not enough space",
    "error_disk_full",
    "磁盘空间不足",
    "空间不足",
    "no space left",
)


def is_disk_full(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _DISK_FULL_HINTS)


def _tag(res: RunResult) -> RunResult:
    """补上 disk_full 标记（7z 把原因写在输出尾部）。"""
    if not res.ok:
        res.disk_full = is_disk_full(res.tail_text) or is_disk_full(res.error)
    return res


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _run(cmd: list[str], cwd: str | None, pct_re, on_progress: ProgressFn | None,
         ok_codes: set[int], timeout_s: int | None = None) -> RunResult:
    t0 = time.time()
    tail: list[str] = []
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    try:
        assert p.stdout is not None
        for raw in iter(p.stdout.readline, b""):
            line = _decode(raw).rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 200:
                tail = tail[-120:]
            if on_progress is not None:
                m = pct_re.match(line)
                if m:
                    try:
                        on_progress(float(m.group(1)), line.strip())
                    except (TypeError, ValueError):
                        pass
        p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        return _tag(RunResult(False, -1, time.time() - t0, cmd, tail, f"超时 {timeout_s}s"))
    except OSError as exc:
        return _tag(RunResult(False, -1, time.time() - t0, cmd, tail, f"启动失败：{exc}"))
    return _tag(RunResult(p.returncode in ok_codes, p.returncode, time.time() - t0, cmd, tail))


# ---------------- 7z ----------------

def seven_zip_path(cfg: dict) -> str:
    return str(cfg.get("tools", {}).get("seven_zip", ""))


def pack_7z(
    cfg: dict,
    out_archive: str | Path,
    source_root: str | Path,
    exclude_dirs: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    """把 source_root 下的内容压成一个 7z。包内路径相对 source_root。"""
    out_archive = str(Path(out_archive).resolve())
    source_root = str(Path(source_root).resolve())
    Path(out_archive).parent.mkdir(parents=True, exist_ok=True)
    tmp = out_archive + ".tmp"
    if Path(tmp).exists():
        Path(tmp).unlink()

    lvl = int(cfg.get("compress", {}).get("level", 5))
    mt = str(cfg.get("compress", {}).get("threads", "on"))
    cmd = [seven_zip_path(cfg), "a", "-t7z", f"-mx={lvl}", f"-mmt={mt}", "-bsp1", "-y", tmp, "*"]
    for d in exclude_dirs or []:
        cmd.append(f"-xr!{d}")

    # 排除项多的时候（例如几千个 pnpm junction）走 listfile，
    # 否则命令行会超过 Windows 的 32k 上限，CreateProcess 直接报 WinError 206。
    paths = list(exclude_paths or [])
    if paths:
        if len(paths) > 40:
            lst = Path(out_archive).with_name(Path(out_archive).stem + ".exclude.txt")
            lst.write_text("\n".join(paths) + "\n", encoding="utf-8")
            cmd.extend(["-scsUTF-8", f"-x@{lst}"])
        else:
            cmd.extend(f"-x!{p}" for p in paths)

    cmd.extend(str(a) for a in cfg.get("compress", {}).get("extra_args", []))

    # 7-Zip 返回 1 表示“有警告”，通常是某些文件没能读取。归档本身仍可能通过
    # `7z t`，但这不代表源目录的所有文件都进了包；备份必须把警告当失败处理。
    res = _run(cmd, cwd=source_root, pct_re=_PCT_7Z, on_progress=on_progress, ok_codes={0})
    if res.ok and Path(tmp).exists():
        os.replace(tmp, out_archive)
    elif Path(tmp).exists():
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return res


def pack_7z_multi(
    cfg: dict,
    out_archive: str | Path,
    sources: list[dict],
    exclude_dirs: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    """把多个目录打进同一个 7z。包内每个源以自身目录名作顶层（默认 -spf 行为）。

    sources: [{"path": "...", "label": "..."}]，label 只用于报错。
    排除项按「目录名」全局生效（-xr!name），精确路径用 -x!<绝对路径>。
    """
    out_archive = str(Path(out_archive).resolve())
    Path(out_archive).parent.mkdir(parents=True, exist_ok=True)
    tmp = out_archive + ".tmp"
    if Path(tmp).exists():
        Path(tmp).unlink()

    lvl = int(cfg.get("compress", {}).get("level", 5))
    mt = str(cfg.get("compress", {}).get("threads", "on"))
    cmd = [seven_zip_path(cfg), "a", "-t7z", f"-mx={lvl}", f"-mmt={mt}", "-bsp1", "-y", tmp]
    for d in exclude_dirs or []:
        cmd.append(f"-xr!{d}")

    paths = list(exclude_paths or [])
    if paths:
        if len(paths) > 40:
            lst = Path(out_archive).with_name(Path(out_archive).stem + ".exclude.txt")
            lst.write_text("\n".join(paths) + "\n", encoding="utf-8")
            cmd.extend(["-scsUTF-8", f"-x@{lst}"])
        else:
            cmd.extend(f"-x!{p}" for p in paths)

    cmd.extend(str(a) for a in cfg.get("compress", {}).get("extra_args", []))
    cmd.extend(str(Path(s["path"]).resolve()) for s in sources)

    # 多源必须给 cwd=None：给 cwd 会让 7z 只收当前目录下的相对路径，绝对路径反而进不去
    # 多源压缩同样不能把“部分文件跳过”的警告认作成功。
    res = _run(cmd, cwd=None, pct_re=_PCT_7Z, on_progress=on_progress, ok_codes={0})
    if res.ok and Path(tmp).exists():
        os.replace(tmp, out_archive)
    elif Path(tmp).exists():
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return res


def extract_7z(
    cfg: dict,
    archive: str | Path,
    dest: str | Path,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    dest = str(Path(dest).resolve())
    Path(dest).mkdir(parents=True, exist_ok=True)
    cmd = [seven_zip_path(cfg), "x", "-bsp1", "-y", f"-o{dest}", str(Path(archive).resolve())]
    return _run(cmd, cwd=dest, pct_re=_PCT_7Z, on_progress=on_progress, ok_codes={0})


def test_7z(cfg: dict, archive: str | Path, on_progress: ProgressFn | None = None) -> RunResult:
    cmd = [seven_zip_path(cfg), "t", "-bsp1", str(Path(archive).resolve())]
    return _run(cmd, cwd=None, pct_re=_PCT_7Z, on_progress=on_progress, ok_codes={0})


def list_7z(cfg: dict, archive: str | Path) -> list[str]:
    """返回包内条目路径（目录以 \\ 结尾）。

    注意：不能走 _run —— 它的 tail 只保留最后 120 行，数大包会误判成「只有几条」。
    这里单独用 subprocess 拿完整输出，按 -slt 的 `Path = ` 行解析。
    """
    cmd = [seven_zip_path(cfg), "l", "-slt", "-ba", str(Path(archive).resolve())]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    text = _decode(r.stdout or b"")
    arch_name = Path(archive).name
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("Path = "):
            p = line[len("Path = "):].strip()
            if p and p != str(Path(archive).resolve()) and p != arch_name:
                out.append(p)
    return out


def archive_info(cfg: dict, archive: str | Path) -> dict:
    """从 7z l 尾部读出实际文件数，供备份与清单交叉校验。"""
    cmd = [seven_zip_path(cfg), "l", str(Path(archive).resolve())]
    res = _run(cmd, cwd=None, pct_re=_PCT_7Z, on_progress=None, ok_codes={0})
    files: int | None = None
    folders: int | None = None
    # 7-Zip 26 prints a final summary such as "41372 files, 4106 folders".
    # It is within _run's retained tail even when the archive has tens of thousands
    # of entries. Older code counted dated listing rows, which mixed files and dirs.
    for line in reversed(res.tail):
        match = re.search(r"(\d[\d,]*)\s+files?,\s*(\d[\d,]*)\s+folders?", line, re.I)
        if match:
            files = int(match.group(1).replace(",", ""))
            folders = int(match.group(2).replace(",", ""))
            break
    return {
        "files": files,
        "folders": folders,
        "ok": res.ok and files is not None,
        "code": res.code,
        "error": res.error or ("\n".join(res.tail[-8:]) if files is None else ""),
    }


# ---------------- robocopy ----------------

def mirror_dir(
    cfg: dict,
    src: str | Path,
    dst: str | Path,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    """robocopy /E /MT:32 /XJ /R:1 /W:1 —— 只增不删，天然增量。"""
    src = str(Path(src).resolve())
    dst = str(Path(dst).resolve())
    Path(dst).mkdir(parents=True, exist_ok=True)
    cmd = [
        str(cfg.get("tools", {}).get("robocopy", "robocopy")),
        src,
        dst,
        "/E",
        "/MT:32",
        "/XJ",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
    ]
    # robocopy 的 0–7 都算成功（1=有复制，2=有额外项，3=1+2 …）
    return _run(cmd, cwd=None, pct_re=_PCT_RB, on_progress=on_progress, ok_codes=set(range(8)))


# ---------------- 目录扫描 ----------------

def is_link(path: Path) -> bool:
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return False


def scan_links(
    roots: list[dict],
    exclude_links: list[str] | None = None,
) -> list[dict]:
    """扫出 roots 下的 junction/symlink。不跟随。返回 [{link, target, root_label}]。"""
    skip = {os.path.normcase(os.path.abspath(p)) for p in (exclude_links or [])}
    out: list[dict] = []
    for root in roots:
        base = Path(root["path"])
        if not base.exists():
            continue
        excl = {os.path.normcase(os.path.abspath(base / d)) for d in root.get("exclude_dirs", [])}
        stack = [base]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        p = Path(e.path)
                        key = os.path.normcase(os.path.abspath(p))
                        if key in excl:
                            continue
                        try:
                            if e.is_symlink() or e.is_junction():
                                target = ""
                                try:
                                    target = os.readlink(p)
                                except OSError:
                                    target = str(p.resolve())
                                out.append(
                                    {"link": str(p), "target": target, "root": root["label"]}
                                )
                                continue
                            if key in skip:
                                # 显式排除的普通目录不递归；若它是链接，上面仍保留目标，
                                # 恢复到新环境时才能把 junction 指向新根。
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(p)
                        except OSError:
                            continue
            except OSError:
                continue
    return out


def scan_empty_dirs(
    roots: list[dict],
    exclude_links: list[str] | None = None,
) -> list[dict]:
    """扫出 roots 下「不含任何文件、也不含子目录」的目录，返回 [{dir, root}]。"""
    skip = {os.path.normcase(os.path.abspath(p)) for p in (exclude_links or [])}
    out: list[dict] = []
    for root in roots:
        base = Path(root["path"])
        if not base.exists():
            continue
        excl = {os.path.normcase(os.path.abspath(base / d)) for d in root.get("exclude_dirs", [])}
        for cur, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            curp = Path(cur)
            kept = []
            for d in dirnames:
                p = curp / d
                key = os.path.normcase(os.path.abspath(p))
                if key in excl or key in skip or is_link(p):
                    continue
                kept.append(d)
            dirnames[:] = kept
            if not dirnames and not filenames and curp != base:
                out.append({"dir": str(curp), "root": root["label"]})
    return out


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path: str | Path) -> tuple[int, int]:
    """返回 (文件数, 字节数)，不跟随链接。"""
    files = 0
    size = 0
    for cur, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if not is_link(Path(cur) / d)]
        for f in filenames:
            fp = Path(cur) / f
            if is_link(fp):
                continue
            try:
                files += 1
                size += fp.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return files, size
