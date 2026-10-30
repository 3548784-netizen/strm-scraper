# -*- coding: utf-8 -*-
"""海报 / 背景图的网页缩略图缓存。

任务列表里的海报格子只有 34×51 像素，背景图 60×34。原来直接把原图
（海报 1000×1500、背景图 1920×1080，单张 100KB～1MB）发给浏览器，条目
一多就是几百兆流量加几百次大图解码，列表看着就"转不出来"。

这里按「源文件 + 修改时间 + 尺寸」算指纹，生成一张几十 KB 的小图缓存到
磁盘，同一个文件同一宽度只生成一次；源图重新生成后指纹变化，自动换新。
生成过程用 per-文件 的锁去重（同一张图被并发请求时只解码一次），并按
TTL 与总量清扫，避免把 /config 撑满。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# 缓存有效期与总量上限（一张小图 5～20KB，2000 张约 30MB）
CACHE_TTL = 14 * 24 * 3600
CACHE_MAX_FILES = 2000
# 出图质量：列表里只占几十像素，82 足够且肉眼看不出差别
JPEG_QUALITY = 82
# 允许请求的最大宽度，防止有人用 w=99999 让服务端白干活
MAX_WIDTH = 512


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{path}|{st.st_mtime_ns}|{st.st_size}"


class ThumbCache:
    """把大图缩成指定宽度的小图，按指纹缓存在磁盘上。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._writes = 0

    # ---- 对外 ----

    def get(self, source: Path, width: int) -> Path:
        """返回宽度不超过 ``width`` 的小图路径。

        源图本身不比 ``width`` 宽时直接返回原图，省一次转码；出图失败也
        退回原图——缩略图只是加速手段，不能因为它让列表裂掉。
        """
        if not width:
            return source
        width = max(16, min(int(width), MAX_WIDTH))
        try:
            digest = hashlib.sha1(
                f"{_fingerprint(source)}|{width}".encode("utf-8")
            ).hexdigest()[:20]
        except OSError:
            return source

        out = self.cache_dir / f"{digest}.jpg"
        if self._fresh(out):
            _touch(out)
            return out

        with self._lock_for(out.name):
            if self._fresh(out):  # 等锁期间别的请求已经生成好
                return out
            try:
                made = self._render(source, out, width)
            except Exception as exc:  # noqa: BLE001 - 缩略图失败不该影响页面
                log.debug("缩略图生成失败，退回原图：%s（%s）", source, exc)
                return source
            self._sweep()
            return made or source

    # ---- 内部 ----

    def _render(self, source: Path, out: Path, width: int) -> Path | None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + f".{os.getpid()}.part")
        try:
            with Image.open(source) as img:
                # draft 让 libjpeg 直接按 1/2、1/4、1/8 解码，比全尺寸解码快很多
                img.draft("RGB", (width, width * 2))
                if img.width <= width:
                    return None  # 原图已经够小，交给调用方发原图
                ratio = width / float(img.width)
                height = max(1, round(img.height * ratio))
                small = img.convert("RGB").resize((width, height), Image.LANCZOS)
                small.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=False)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        tmp.replace(out)
        return out

    def _lock_for(self, name: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock

    @staticmethod
    def _fresh(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _sweep(self) -> None:
        """每写 50 张扫一次：清掉过期和超量的。"""
        self._writes += 1
        if self._writes % 50:
            return
        try:
            files = [p for p in self.cache_dir.glob("*.jpg") if p.is_file()]
            files.sort(key=_mtime)
        except OSError:
            return
        now = time.time()
        for index, path in enumerate(files):
            stale = now - _mtime(path) > CACHE_TTL
            overflow = len(files) - index > CACHE_MAX_FILES
            if not (stale or overflow):
                break
            try:
                path.unlink()
            except OSError:
                continue


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _touch(path: Path) -> None:
    """更新访问时间，让常看的缩略图不被清扫掉（失败无所谓）。"""
    try:
        os.utime(path, None)
    except OSError:
        pass
