# -*- coding: utf-8 -*-
"""发版：改版本号 → 提交 → 打 vX.Y.Z 标签 → 推送。

用法
    python tools/release.py 1.0.0        # 直接指定版本
    python tools/release.py patch        # 1.0.0 → 1.0.1
    python tools/release.py minor        # 1.0.0 → 1.1.0
    python tools/release.py major        # 1.0.0 → 2.0.0
    python tools/release.py 1.1.0 --no-push   # 只改版本号和打标签，不推送（自己检查后再推）

推上去之后 CI 会做两件事：
    1. 构建并推送 1.0.0 / 1.0 / latest 三个镜像标签（Docker Hub + GHCR）
    2. 用这两个标签之间的提交记录自动生成 GitHub Release 说明

常用参数
    --message/-m   版本号提交的说明文字
    --dry-run      只打印要做什么，不实际改动
    --no-push      本地提交并打标签，但不推到远端
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "scraper" / "version.py"
BUMP = {"patch": 2, "minor": 1, "major": 0}


def sh(cmd: list[str], dry: bool = False) -> str:
    """跑一条命令并返回输出；dry 模式下只打印。"""
    text = " ".join(cmd)
    if dry:
        print(f"    [dry] {text}")
        return ""
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        print(f"命令失败：{text}", file=sys.stderr)
        print(out.stdout or "", out.stderr or "", file=sys.stderr)
        sys.exit(1)
    return (out.stdout or "").strip()


def current_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        print("在 scraper/version.py 里没找到 __version__", file=sys.stderr)
        sys.exit(1)
    return m.group(1)


def normalize(current: str, ask: str) -> str:
    """把「版本号」或「patch/minor/major」变成一个合法的 X.Y.Z。"""
    ask = ask.strip().lstrip("vV")
    parts = (current.lstrip("vV") + ".0.0").split(".")[:3]
    nums = [int(p) if p.isdigit() else 0 for p in parts]

    if ask in BUMP:
        i = BUMP[ask]
        nums[i] += 1
        for j in range(i + 1, 3):
            nums[j] = 0
        return ".".join(str(n) for n in nums)

    got = ask.split(".")
    if len(got) == 1 and got[0].isdigit():          # 只写 1 就是 1.0.0
        got += ["0", "0"]
    elif len(got) == 2 and all(p.isdigit() for p in got):   # 只写 1.0 就是 1.0.0
        got += ["0"]
    if len(got) != 3 or not all(p.isdigit() for p in got):
        print(f"版本号要写成 X.Y.Z（或 patch/minor/major），收到的是：{ask}", file=sys.stderr)
        sys.exit(1)
    return ".".join(str(int(p)) for p in got)


def write_version(version: str, dry: bool) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    new = re.sub(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', text)
    if dry:
        print(f"    [dry] 写入 {VERSION_FILE.relative_to(ROOT)}: {version}")
        return
    VERSION_FILE.write_text(new, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发版并打 vX.Y.Z 标签")
    ap.add_argument("version", help="版本号（1.0.0）或增量（patch / minor / major）")
    ap.add_argument("-m", "--message", default="", help="这次发版的说明")
    ap.add_argument("--no-push", action="store_true", help="只提交和打标签，不推远端")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作")
    args = ap.parse_args(argv)

    old = current_version()
    new = normalize(old, args.version)
    tag = f"v{new}"

    if not args.dry_run and new == old:
        print(f"现在就是 v{old}，要发新版本换一个号，或者用 --dry-run 看看动作", file=sys.stderr)
        return 1

    dirty = sh(["git", "status", "--porcelain"]).strip()
    # 发版本身会改 version.py，所以只要没有别的文件没提交就行
    others = [l for l in dirty.splitlines() if "scraper/version.py" not in l]
    if others:
        print("工作区还有没提交的改动，先处理掉再发版：", file=sys.stderr)
        for line in others[:10]:
            print("  " + line, file=sys.stderr)
        return 1

    if sh(["git", "tag", "-l", tag]).strip():
        print(f"标签 {tag} 已经存在了，换个版本号", file=sys.stderr)
        return 1

    print(f"发版：v{old} → {tag}")
    write_version(new, args.dry_run)
    sh(["git", "add", str(VERSION_FILE.relative_to(ROOT))], args.dry_run)
    msg = args.message.strip() or f"chore: 发版 {tag}"
    sh(["git", "commit", "-m", msg], args.dry_run)
    sh(["git", "tag", "-a", tag, "-m", msg], args.dry_run)

    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if args.no_push:
        print(f"\n已提交并打好标签 {tag}，没有推送。确认后自己推：")
        print(f"    git push origin {branch} && git push origin {tag}")
        return 0

    print("\n推送到远端（CI 会自动构建镜像并创建 Release）：")
    sh(["git", "push", "origin", branch], args.dry_run)
    sh(["git", "push", "origin", tag], args.dry_run)
    print(f"\n完成：{tag}")
    print("    · 镜像：3548784/strm-scraper:%s（另附 %s 与 latest）"
          % (new, ".".join(new.split(".")[:2])))
    print(f"    · Release：https://github.com/3548784-netizen/strm-scraper/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
