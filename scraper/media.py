# -*- coding: utf-8 -*-
"""媒体文件扫描与产物命名规则。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 视频目录里常见但不是视频的文件，扫描时直接跳过
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass(frozen=True)
class Target:
    """一个 strm 文件及其产物路径。"""

    source: Path
    nfo: Path
    poster: Path
    fanart: Path
    thumb: Path

    @property
    def stem(self) -> str:
        return self.source.stem

    @property
    def parent(self) -> Path:
        return self.source.parent


def build_target(source: Path) -> Target:
    """按 ``同名`` 规则推出产物路径。

    ``片名.AVI.strm`` → ``片名.AVI.nfo`` / ``片名.AVI-poster.jpg`` /
    ``片名.AVI-fanart.jpg`` / ``片名.AVI-thumb.jpg``

    这套命名是 Emby 和 Jellyfin 共用的（两家都按 ``<视频文件名>-类型.jpg``
    查找本地图片），所以同一份产物两边都能直接识别，不需要为谁单独复制一份。
    """
    stem = source.stem
    parent = source.parent
    return Target(
        source=source,
        nfo=parent / f"{stem}.nfo",
        poster=parent / f"{stem}-poster.jpg",
        fanart=parent / f"{stem}-fanart.jpg",
        thumb=parent / f"{stem}-thumb.jpg",
    )


def iter_sources(roots: list[str], extensions: list[str], recursive: bool = True):
    """遍历所有待处理文件，产出 Path。

    会跳过隐藏目录、``@eaDir``（群晖缩略图目录）和 ``__pycache__``。
    """
    wanted = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            log.warning("扫描根目录不存在，已跳过：%s", base)
            continue

        if not recursive:
            for entry in sorted(base.iterdir()):
                if entry.is_file() and entry.suffix.lower() in wanted:
                    yield entry
            continue

        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(
                d for d in dirs
                if not d.startswith(".")
                and d not in ("@eaDir", "__pycache__", "node_modules", "$RECYCLE.BIN")
            )
            for name in sorted(files):
                if name.lower() in IGNORED_NAMES:
                    continue
                path = Path(current) / name
                if path.suffix.lower() in wanted:
                    yield path


def product_state(target: Target, want_nfo: bool, want_poster: bool,
                  want_fanart: bool, want_thumb: bool = False) -> tuple[bool, list[str]]:
    """检查产物是否齐全，返回（是否完整, 缺失项名称列表）。"""
    missing: list[str] = []
    if want_nfo and not _non_empty(target.nfo):
        missing.append("nfo")
    if want_poster and not _non_empty(target.poster):
        missing.append("poster")
    if want_fanart and not _non_empty(target.fanart):
        missing.append("fanart")
    if want_thumb and not _non_empty(target.thumb):
        missing.append("thumb")
    return (not missing), missing


def artifacts(target: Target) -> dict[str, int]:
    """各产物的字节数，0 表示不存在。网页上用来判断缺哪一项。"""
    result = {}
    for name, path in (("nfo", target.nfo), ("poster", target.poster),
                       ("fanart", target.fanart), ("thumb", target.thumb)):
        try:
            result[name] = path.stat().st_size if path.is_file() else 0
        except OSError:
            result[name] = 0
    return result


def _non_empty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def signature(path: Path) -> tuple[int, float]:
    """用大小 + 修改时间判断文件是否变化过。"""
    try:
        stat = path.stat()
        return stat.st_size, round(stat.st_mtime, 3)
    except OSError:
        return 0, 0.0
