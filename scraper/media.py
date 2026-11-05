# -*- coding: utf-8 -*-
"""媒体文件扫描与产物命名规则。"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# 视频目录里常见但不是视频的文件，扫描时直接跳过
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

# 默认扫描的容器格式。**全开**——放进媒体库就能刮，不用改任何配置。
# strm 排在最前（它只是个写着播放地址的文本，其余都是真实视频文件）。
# ffmpeg 能解、Emby / Jellyfin 能认的都在这里；想加冷门容器（.iso / .vob 等）
# 在 scan.extensions 里补一条即可，这里不用动。
SUPPORTED_EXTENSIONS = (
    ".strm",
    ".mp4",
    ".m4v",
    ".mkv",
    ".avi",
    ".ts",
    ".m2ts",
    ".mov",
    ".wmv",
    ".flv",
    ".rmvb",
    ".webm",
    ".mpg",
)

# Emby / Jellyfin 的「附加内容」命名后缀（正片之外的预告、花絮、删减片段…）。
# 这些文件不该生成海报和 NFO —— 媒体服务器会拿正片的图铺满它们，
# 我们跟着生成只会多出一堆 xxx-trailer-poster.jpg 的垃圾。
_EXTRA_SUFFIXES = (
    "-trailer", ".trailer",
    "-sample", ".sample",
    "-featurette", ".featurette",
    "-behindthescenes", ".behindthescenes",
    "-deleted", ".deleted",
    "-interview", ".interview",
    "-scene", ".scene",
    "-short", ".short",
    "-other", ".other",
    "-makingof", ".makingof",
    "-clip", ".clip",
    "-theme", ".theme",
)


def is_extra(name: str) -> bool:
    """是不是媒体服务器眼里的「附加内容」（预告 / 花絮 / 样片…）。"""
    stem = name.rsplit(".", 1)[0].lower() if "." in name else name.lower()
    return stem.endswith(_EXTRA_SUFFIXES)


def wanted_extensions(extensions: list[str]) -> set[str]:
    """配置里的扩展名统一成带点的小写集合（``strm`` 和 ``.STRM`` 都认）。"""
    return {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}


def is_media_file(path: Path | str, wanted: set[str]) -> bool:
    """在扫描范围内、而且不是预告花絮这类附加内容。"""
    path = Path(path)
    if path.suffix.lower() not in wanted:
        return False
    return not is_extra(path.name)


@dataclass(frozen=True)
class Target:
    """一个媒体文件（strm 或本地视频）及其产物路径。"""

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

    本地视频走同一条规则，只是去的是自己的后缀：
    ``片名.mkv`` → ``片名.nfo`` / ``片名-poster.jpg`` …

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

    会跳过隐藏目录、``@eaDir``（群晖缩略图目录）、``__pycache__``，
    以及预告 / 花絮这类附加内容（媒体服务器自己会给它们配图，用不着我们）。
    """
    wanted = wanted_extensions(extensions)
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            log.warning("扫描根目录不存在，已跳过：%s", base)
            continue

        if not recursive:
            for entry in sorted(base.iterdir()):
                if entry.is_file() and is_media_file(entry, wanted):
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
                if is_media_file(path, wanted):
                    yield path


def scan_rows(roots: list[str], extensions: list[str], recursive: bool = True,
              progress=None, workers: int = 4) -> list[tuple[str, int, float]]:
    """一次遍历拿到（路径, 大小, 修改时间），尽量少打系统调用。

    比 ``iter_sources`` + 逐条 ``stat`` 快很多：``os.scandir`` 在 Windows /
    SMB 上会顺带把大小和修改时间带回来（``DirEntry.stat`` 直接读目录项，
    不再单独发一次 stat 请求），文件越多省得越多——几千个文件的媒体库
    启动那一下要等几十秒，主要就是耗在逐个 stat 上。

    多个扫描根目录之间用线程并发（网络盘上 IO 等待可以重叠）。
    """
    wanted = wanted_extensions(extensions)
    counter = {"n": 0}
    lock = threading.Lock()

    def bump(delta: int) -> None:
        """累加已发现数量；每 200 个才回调一次，别把回调本身变成负担。"""
        with lock:
            counter["n"] += delta
            total = counter["n"]
            report = progress is not None and total % 200 < delta
        if report:
            progress(total)

    def walk_one(root: str) -> list[tuple[str, int, float]]:
        found: list[tuple[str, int, float]] = []
        base = Path(root)
        if not base.is_dir():
            log.warning("扫描根目录不存在，已跳过：%s", base)
            return found
        if not recursive:
            try:
                entries = list(os.scandir(base))
            except OSError as exc:
                log.warning("读取目录失败，已跳过：%s（%s）", base, exc)
                return found
            for entry in entries:
                row = _row_of(entry, wanted, want_dir=False)
                if row:
                    found.append(row)
                    bump(1)
            return found

        stack = [str(base)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not _skip_scan_dir(entry.name):
                                    stack.append(entry.path)
                                continue
                            row = _row_of(entry, wanted, want_dir=True)
                        except OSError:
                            continue
                        if row:
                            found.append(row)
                            bump(1)
            except OSError as exc:
                log.warning("读取目录失败，已跳过：%s（%s）", current, exc)
        return found

    result: list[tuple[str, int, float]] = []
    if len(roots) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(roots))) as pool:
            for chunk in pool.map(walk_one, roots):
                result.extend(chunk)
    else:
        for root in roots:
            result.extend(walk_one(root))
    if progress:
        progress(len(result))
    return result


# 遍历时跳过的目录（群晖缩略图、回收站之类）
_SKIP_DIRS = {"@eaDir", "__pycache__", "node_modules", "$RECYCLE.BIN", "#recycle"}


def _skip_scan_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS


def _row_of(entry: os.DirEntry, wanted: set[str], want_dir: bool):
    """目录项转成（路径, 大小, 修改时间）；不感兴趣的返回 None。"""
    name = entry.name
    if want_dir and name.lower() in IGNORED_NAMES:
        return None
    if not is_media_file(name, wanted):
        return None
    try:
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    # 精度和 signature() 保持一致：两处存进库的时间戳必须同量级，
    # 否则同一个文件在「扫描登记」和「处理完回写」时会看起来像变过
    return (entry.path, stat.st_size, round(stat.st_mtime, 3))


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


def split_by_products(sources: Iterable[str | Path], want_nfo: bool,
                      want_poster: bool, want_fanart: bool,
                      want_thumb: bool = False) -> tuple[list[str], list[str]]:
    """按**磁盘上的产物**把媒体文件分成（齐全, 缺失）两组。

    这里刻意不查任务状态库：任务记录可能被用户从列表里删掉、换台机器、
    重建容器，但海报 / NFO 还在磁盘上，就仍然是可以直接复用的结果。
    并发 stat 只是为了大库时别把串行 I/O 拖成瓶颈，判断口径和
    :func:`product_state` 完全一致。
    """
    paths = list(dict.fromkeys(str(p) for p in sources))
    if not paths:
        return [], []

    def complete(path: str) -> bool:
        ready, _missing = product_state(
            build_target(Path(path)), want_nfo, want_poster, want_fanart, want_thumb
        )
        return ready

    with ThreadPoolExecutor(max_workers=8) as pool:
        ready = [p for p, ok in zip(paths, pool.map(complete, paths)) if ok]
    done = set(ready)
    return ready, [p for p in paths if p not in done]


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
