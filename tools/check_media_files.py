#!/usr/bin/env python3
"""体检媒体库目录：每个 strm 的同名海报 / 背景图 / NFO 到底好不好。

排查「Emby / Jellyfin 里显示破图」这类问题用。逐项检查：

- 产物文件在不在、是不是 0 字节
- 是不是真的 JPEG（防止下载失败写进去一坨 HTML）
- 能不能读出宽高（读出尺寸 = 文件结构完整）
- 长宽比对不对（海报 2:3、背景图 16:9，比例离谱多半是裁错了）
- Emby 跑起来的用户能不能读到（权限位 / 属主）
- 文件名有没有会让 Emby 对不上的坑（首尾空格、全角字符、大小写）

用法：
    python tools/check_media_files.py /path/to/media/某文件夹
    python tools/check_media_files.py /path/to/media --flat       # 只看这一层
    python tools/check_media_files.py /path/to/media --only-poster

只读，不改任何文件。
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

# Emby 两张图的合理比例区间，超出就当裁歪了
ASPECT = {"poster": (0.62, 0.72), "fanart": (1.66, 1.84)}

SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_size(path: Path) -> tuple[int, int] | None:
    """读 JPEG 的宽高。读得出来说明 SOI 到 SOF 这段结构是好的。"""
    try:
        with path.open("rb") as f:
            data = f.read(1024 * 256)
    except OSError:
        return None
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    return _scan_sof(data)


def _scan_sof(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            return None
        if marker in SOF:
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seglen
    return None


def _sz(n: int) -> str:
    return f"{n} B" if n < 10 * 1024 else f"{n / 1024:.0f} KB"


def describe_image(path: Path, kind: str) -> tuple[str, list[str]]:
    """返回 (人话描述, 问题列表)。"""
    problems: list[str] = []
    try:
        size = path.stat().st_size
    except OSError as e:
        return f"读不到（{e.strerror}）", ["stat 失败"]

    if size == 0:
        return "0 字节", ["空文件"]
    wh = jpeg_size(path)
    if wh is None:
        head = b""
        try:
            with path.open("rb") as f:
                head = f.read(200)
        except OSError:
            pass
        if head[:5].lower().startswith(b"<!doc") or head[:5].lower().startswith(b"<html"):
            problems.append("内容是 HTML，不是图片（下载时拿到了错误页）")
        elif head[:4] == b"RIFF":
            problems.append("其实是 WebP，扩展名却是 .jpg")
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            problems.append("其实是 PNG，扩展名却是 .jpg")
        else:
            problems.append("不是有效 JPEG（可能写坏了）")
        return f"{_sz(size)}，解不出宽高", problems

    w, h = wh
    lo, hi = ASPECT.get(kind, (0, 99))
    ratio = w / h if h else 0
    note = f"{w}×{h}（{ratio:.2f}:1，{_sz(size)}）"
    # 比例偏了不会破图，只是显示时被拉伸，提醒但不计为硬伤
    if kind in ASPECT and not (lo <= ratio <= hi):
        note += f"  ⚠ 比例偏出 {lo:.2f}–{hi:.2f}，显示会被拉伸"
    return note, problems


def permission_notes(path: Path) -> list[str]:
    notes: list[str] = []
    try:
        st = path.stat()
    except OSError:
        return notes
    mode = stat.S_IMODE(st.st_mode)
    if not mode & stat.S_IROTH:
        notes.append(f"其他用户不可读（权限 {mode:o}）——"
                     f"Emby 容器不是 root 跑的话会看不到图")
    if st.st_size and not os.access(path, os.R_OK):
        notes.append("当前用户读不了")
    return notes


def name_notes(path: Path) -> list[str]:
    notes: list[str] = []
    name = path.name
    if name != name.strip():
        notes.append("文件名首尾有空格，Emby 匹配同名文件会失败")
    if any(ord(c) > 0x2000 and c not in "·—" for c in name):
        if any(0xFF01 <= ord(c) <= 0xFF60 for c in name):
            notes.append("含全角字符，某些版本 Emby 匹配不上")
    return notes


def check_file(strm: Path, only_poster: bool) -> tuple[bool, list[str]]:
    """返回 (海报和 NFO 是否可用, 问题列表)。

    海报、NFO 是硬伤——缺了或坏了，Emby 里就是破图 / 认不出片名。
    背景图、缩略图是可选的（刮削设置里可能就没开），只当提示。
    """
    stem, parent = strm.stem, strm.parent
    lines: list[str] = []
    problems: list[str] = []      # 硬伤
    hints: list[str] = []         # 可选产物缺失，不算错

    poster = parent / f"{stem}-poster.jpg"
    if not poster.exists():
        problems.append("缺海报")
        lines.append("    海报   ✗ 不存在")
    else:
        note, probs = describe_image(poster, "poster")
        probs += permission_notes(poster)
        problems += [f"海报：{p}" for p in probs]
        lines.append(f"    海报   {'✗' if probs else '✓'} {note}")
        for p in probs:
            lines.append(f"           → {p}")

    if not only_poster:
        for label, path, kind in [("背景图", parent / f"{stem}-fanart.jpg", "fanart"),
                                  ("缩略图", parent / f"{stem}-thumb.jpg", "poster")]:
            if not path.exists():
                hints.append(f"无{label}")
                lines.append(f"    {label:<4} · 没有（可选）")
                continue
            note, probs = describe_image(path, kind)
            probs += permission_notes(path)
            problems += [f"{label}：{p}" for p in probs]
            lines.append(f"    {label:<4} {'✗' if probs else '✓'} {note}")
            for p in probs:
                lines.append(f"           → {p}")

        nfo = parent / f"{stem}.nfo"
        if not nfo.exists():
            hints.append("无 NFO")
            lines.append("    NFO    · 没有（可选）")
        elif nfo.stat().st_size <= 40:
            problems.append(f"NFO 只有 {nfo.stat().st_size} 字节，Emby 解析会失败")
            lines.append(f"    NFO    ✗ {nfo.stat().st_size} 字节")
        else:
            lines.append(f"    NFO    ✓ {nfo.stat().st_size / 1024:.1f} KB")

    for note in name_notes(strm):
        problems.append(note)
        lines.append(f"    → {note}")

    print(f"  {'✗' if problems else '✓'} {strm.name}"
          + (f"    （{'、'.join(hints)}）" if hints else ""))
    for line in lines:
        print(line)
    return not problems, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="体检媒体库目录里的海报 / 背景图 / NFO")
    ap.add_argument("dirs", nargs="+", help="要检查的目录")
    ap.add_argument("--flat", action="store_true",
                    help="只看这一层，不进子目录（默认递归）")
    ap.add_argument("--only-poster", action="store_true",
                    help="只看海报（目录文件多时更快）")
    args = ap.parse_args()

    stride = (lambda d: Path(d).glob("*.strm")) if args.flat else (lambda d: Path(d).rglob("*.strm"))
    total = bad = 0
    all_problems: list[str] = []

    for d in args.dirs:
        base = Path(d)
        if not base.is_dir():
            print(f"目录不存在：{base}")
            continue
        files = sorted(stride(d))
        print(f"\n{base}  ——  {len(files)} 个 strm")
        if not files:
            print("  （这一层没有 strm，加 --deep 看看子目录）")
        for strm in files:
            total += 1
            ok, probs = check_file(strm, args.only_poster)
            if not ok:
                bad += 1
                all_problems += [f"{strm.parent.name}/{strm.name}：{p}" for p in probs]

    print(f"\n合计 {total} 个文件，{bad} 个有问题")
    if all_problems:
        print("问题汇总：")
        for p in all_problems[:40]:
            print("  -", p)
        if len(all_problems) > 40:
            print(f"  …… 另有 {len(all_problems) - 40} 条")
    elif total:
        print("产物都是好的。如果 Emby 里还是破图，那不是文件问题，"
              "是 Emby 缓存了旧图 —— 刷新元数据时勾上「替换所有图像」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
