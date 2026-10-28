# -*- coding: utf-8 -*-
"""手动选帧：规划候选帧、按需抓帧并缓存、用指定的一帧重出图。

网页上的做法是把整条时间轴切成十几个候选位置，浏览器用 ``<img>`` 直接
请求，谁先抓到谁先显示，不用等服务端批量跑完。抓过的帧按「文件 + 修改
时间 + 时间点 + 宽度」缓存，缩放预览和原图分开存，来回拖动不会重复打网络。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from . import frames, probe
from .config import Config
from .media import Target, artifacts

log = logging.getLogger(__name__)

# 候选帧落在片长 3%–92% 之间，避开片头厂标和片尾字幕
START_RATIO = 0.03
END_RATIO = 0.92
# 预览缓存：超过这个时长没被访问就清掉，同时限制总量（约 80MB 上限）
CACHE_TTL = 6 * 3600
CACHE_MAX_FILES = 400
# 同时跑几个 ffmpeg；太多会把上行带宽打满，反而全都变慢
MAX_PARALLEL = 4
# 缓存文件被认为有效的最小字节数（纯色画面的缩略图可能很小）
MIN_PREVIEW_BYTES = 256


def clock(seconds: float) -> str:
    """秒 → ``12:34`` / ``1:23:45``。"""
    total = max(0, int(round(float(seconds or 0))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class FramePicker:
    """候选帧 + 抓帧缓存 + 指定帧出图。线程安全，多个请求可以同时进来。"""

    def __init__(self, cfg: Config, cache_dir: str | Path):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._slots = threading.Semaphore(MAX_PARALLEL)
        self._probes: dict[str, tuple[float, float]] = {}
        self._writes = 0

    # ---------- 对外接口 ----------

    def plan(self, target: Target, count: int = 12) -> dict:
        """读时长并给出候选时间点，网页打开选帧面板时调一次。"""
        source = target.source
        url = probe.resolve_source(source)
        info = probe.probe(url, timeout=self.cfg.scan.probe_timeout)
        duration = max(0.0, info.duration)
        with self._guard:
            self._probes[self._key(source)] = (duration, time.time())
        log.debug("选帧面板：%s 时长 %.1fs", source.name, duration)
        return {
            "path": str(source),
            "url": url,
            "duration": round(duration, 2),
            "size": info.size,
            "width": info.width,
            "height": info.height,
            "candidates": self._candidates(duration, count),
        }

    def frame(self, source: Path, seconds: float, width: int = 0) -> Path:
        """抓指定时间点的一帧，返回缓存文件路径。"""
        seconds = max(0.0, float(seconds or 0))
        out = self._cache_path(source, seconds, width)
        if self._fresh(out):
            _touch(out)
            return out

        with self._lock_for(out.name):
            if self._fresh(out):  # 等锁期间别的请求已经抓好
                return out
            url = probe.resolve_source(source)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 后缀必须是 .jpg，ffmpeg 靠它选编码器；.part 只是防止读到半截文件
            tmp = out.with_suffix(".part.jpg")
            timeout = max(15, min(self.cfg.scan.timeout, 60))
            with self._slots:
                ok = frames.capture_at(url, seconds, tmp, timeout=timeout, width=width)
            if not ok:
                raise frames.FrameError(
                    f"{clock(seconds)} 这一帧抓不下来：链接可能已失效，"
                    "或这个位置无法解码（换一个时间点再试）"
                )
            tmp.replace(out)
            self._sweep()
        return out

    def apply(self, target: Target, seconds: float, kinds: list[str]) -> dict:
        """用选中的那一帧重新生成海报 / 背景图 / 缩略图。"""
        cfg = self.cfg.scrape
        frame = self.frame(target.source, seconds, 0)  # 原图，缓存供后续重出

        if "poster" in kinds:
            frames.make_poster(
                frame, target.poster,
                width=cfg.poster.width, height=cfg.poster.height,
                mode=cfg.poster.mode, quality=cfg.jpeg_quality,
            )
        if "fanart" in kinds:
            frames.make_fanart(
                frame, target.fanart,
                width=cfg.fanart.width, height=cfg.fanart.height,
                quality=cfg.jpeg_quality,
            )
        if "thumb" in kinds:
            frames.make_thumb(
                frame, target.thumb,
                width=cfg.thumb.width, height=cfg.thumb.height,
                quality=cfg.jpeg_quality,
            )

        log.info("手动选帧：%s 使用 %s（%s）", target.source.name,
                 clock(seconds), "、".join(kinds))
        return {
            "path": str(target.source),
            "seconds": round(float(seconds), 2),
            "label": clock(seconds),
            "kinds": kinds,
            "artifacts": artifacts(target),
            "duration": round(self._duration(target.source), 2),
        }

    def duration(self, source: Path) -> float:
        return self._duration(source)

    # ---------- 内部 ----------

    def _candidates(self, duration: float, count: int) -> list[dict]:
        count = max(1, min(int(count), 24))
        if duration <= 0:
            # 读不到时长（少见的直播流 / 特殊封装）：退回固定时间点
            fixed = [5, 30, 60, 120, 300, 600, 900, 1200, 1800, 2400, 3000, 3600]
            return [{"t": float(t), "label": clock(t)} for t in fixed[:count]]

        if count == 1:
            ratios = [(START_RATIO + END_RATIO) / 2]
        else:
            span = END_RATIO - START_RATIO
            ratios = [START_RATIO + span * index / (count - 1) for index in range(count)]

        # 很短的片子（预告片、短片）铺不满 12 个点：把落点压回片内再去重，
        # 免得出现超过片长的位置和一堆重复的 0:00
        limit = max(0.0, duration - 0.5)
        picks: list[dict] = []
        for ratio in ratios:
            seconds = round(min(duration * ratio, limit), 2)
            if picks and abs(picks[-1]["t"] - seconds) < 0.05:
                continue
            picks.append({"t": seconds, "label": clock(seconds)})
        return picks

    def _duration(self, source: Path) -> float:
        """时长缓存 10 分钟，来回开面板不用反复探测。"""
        key = self._key(source)
        with self._guard:
            cached = self._probes.get(key)
        if cached and time.time() - cached[1] < 600:
            return cached[0]
        url = probe.resolve_source(source)
        info = probe.probe(url, timeout=self.cfg.scan.probe_timeout)
        duration = max(0.0, info.duration)
        with self._guard:
            self._probes[key] = (duration, time.time())
        return duration

    def _key(self, source: Path) -> str:
        return f"{source}|{_mtime(source)}"

    def _cache_path(self, source: Path, seconds: float, width: int) -> Path:
        digest = hashlib.sha1(
            f"{source}|{_mtime(source)}|{seconds:.2f}|{width}".encode("utf-8")
        ).hexdigest()[:20]
        return self.cache_dir / f"{digest}.jpg"

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
            return path.is_file() and path.stat().st_size >= MIN_PREVIEW_BYTES
        except OSError:
            return False

    def _sweep(self) -> None:
        """每写 25 张扫一次：清掉过期和超量的，别把 /config 塞满。"""
        self._writes += 1
        if self._writes % 25:
            return
        try:
            files = [p for p in self.cache_dir.glob("*.jpg") if p.is_file()]
            files.sort(key=lambda p: _mtime(p))
        except OSError:
            return
        now = time.time()
        for index, path in enumerate(files):
            stale = now - _mtime(path) > CACHE_TTL
            overflow = len(files) - index > CACHE_MAX_FILES
            if stale or overflow:
                try:
                    path.unlink()
                except OSError:
                    continue


def _mtime(path: Path) -> float:
    try:
        return round(path.stat().st_mtime, 3)
    except OSError:
        return 0.0


def _touch(path: Path) -> None:
    """访问过的帧把时间戳刷新，避免一晚上被反复重抓。"""
    try:
        path.touch()
    except OSError:
        pass
