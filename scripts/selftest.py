# -*- coding: utf-8 -*-
"""环境自检：不依赖网页，直接在命令行跑通一条 strm，用于排查环境问题。

用法::

    python scripts/selftest.py /media/某影片.strm
    python scripts/selftest.py /media/某影片.strm --out /tmp/check

会依次检查 ffmpeg/ffprobe、解析 strm、探测媒体信息、抓帧、生成海报与 NFO，
每一步失败都会停止并打印原因。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

# 允许直接从仓库根目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import frames, nfo, probe  # noqa: E402
from scraper.media import build_target  # noqa: E402


def step(index: int, title: str) -> None:
    print(f"\n[{index}] {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description="strm 刮削环境自检")
    parser.add_argument("strm", help="待测试的 strm 文件路径")
    parser.add_argument("--out", help="产物输出目录（默认在 strm 同目录）")
    parser.add_argument("--timeout", type=int, default=30, help="网络超时秒数")
    parser.add_argument("--thumb", action="store_true", help="额外生成 16:9 缩略图")
    args = parser.parse_args()

    source = Path(args.strm).resolve()
    if not source.is_file():
        print(f"文件不存在：{source}")
        return 1

    step(1, "检查 ffmpeg / ffprobe")
    try:
        ffmpeg = probe.find_binary("ffmpeg")
        ffprobe = probe.find_binary("ffprobe")
        print(f"    ffmpeg  : {ffmpeg}")
        print(f"    ffprobe : {ffprobe}")
    except probe.ProbeError as exc:
        print(f"    失败：{exc}")
        return 1

    step(2, "解析 strm")
    try:
        url = probe.resolve_source(source)
        print(f"    地址：{url[:110]}")
    except probe.ProbeError as exc:
        print(f"    失败：{exc}")
        return 1

    step(3, "探测媒体信息")
    started = time.monotonic()
    try:
        info = probe.probe(url, timeout=args.timeout)
    except probe.ProbeError as exc:
        print(f"    失败：{exc}")
        return 1
    print(f"    耗时   : {time.monotonic() - started:.2f}s")
    print(f"    分辨率 : {info.width}x{info.height}  {info.video.codec if info.video else ''}")
    print(f"    时长   : {info.duration:.1f}s  ({int(info.duration // 60)} 分)")
    print(f"    码率   : {info.bitrate / 1000:.0f} kbps")
    print(f"    大小   : {info.size / 1024 / 1024:.1f} MB")
    print(f"    音轨   : {len(info.audio)} 条")

    step(4, "抓帧")
    import tempfile

    tmp = tempfile.TemporaryDirectory(prefix="ws-selftest-")
    workdir = Path(tmp.name)
    started = time.monotonic()
    try:
        best, count = frames.capture_best(
            url, info.duration, [0.15, 0.30, 0.45, 0.60, 0.75], workdir, timeout=args.timeout
        )
    except frames.FrameError as exc:
        print(f"    失败：{exc}")
        tmp.cleanup()
        return 1
    print(f"    成功抓取 {count} 帧，耗时 {time.monotonic() - started:.2f}s")
    print(f"    选用：{best.name}  {best.stat().st_size / 1024:.0f} KB")

    out_dir = Path(args.out).resolve() if args.out else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    target = build_target(source)
    if out_dir != source.parent:
        target = replace(
            target,
            nfo=out_dir / target.nfo.name,
            poster=out_dir / target.poster.name,
            fanart=out_dir / target.fanart.name,
            thumb=out_dir / target.thumb.name,
        )

    step(5, "生成海报（2:3）与背景图（16:9）")
    pw, ph = frames.make_poster(best, target.poster, 1000, 1500, "blur", 92)
    fw, fh = frames.make_fanart(best, target.fanart, 1920, 1080, 92)
    print(f"    {target.poster.name}  {pw}×{ph}  {target.poster.stat().st_size / 1024:.0f} KB")
    print(f"    {target.fanart.name}  {fw}×{fh}  {target.fanart.stat().st_size / 1024:.0f} KB")
    if args.thumb:
        tw, th = frames.make_thumb(best, target.thumb, 1000, 562, 92)
        print(f"    {target.thumb.name}  {tw}×{th}  {target.thumb.stat().st_size / 1024:.0f} KB")

    step(6, "生成 NFO")
    title = nfo.display_title(source.stem)
    nfo.write_nfo(target.nfo, info, title)
    print(f"    标题：{title}")
    print(f"    {target.nfo.name}  {target.nfo.stat().st_size} 字节")

    tmp.cleanup()
    print("\n全部通过。产物目录：", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
