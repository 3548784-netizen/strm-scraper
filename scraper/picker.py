# -*- coding: utf-8 -*-
"""手动选帧：规划候选帧、按需抓帧并缓存、用指定的一帧重出图。

网页上的做法是把整条时间轴切成十几个候选位置，浏览器用少量并发直接
请求，谁先抓到谁先显示，不用等服务端批量跑完。候选缩略图走快速通道
（只做关键帧跳转、短超时、失败留空可重试），主帧仍走精确定位；抓过的帧按
「文件 + 修改时间 + 时间点 + 宽度」缓存，缩放预览和原图分开存，来回拖动
不会重复打网络。客户端关掉请求时 ffmpeg 会被立刻停掉，释放并发槽位。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from . import frames, links, probe
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
# 候选缩略图最多同时占几路：永远留一个槽给主帧，拖进度条时不会被排队挤死
THUMB_PARALLEL = 3
# 候选缩略图只做关键帧快速跳转，拿不到就留空让用户点重试，别再退化成
# 从头解码定位（网络源上这一步动辄几十秒，是「选帧界面卡住」的主因）
FAST_TIMEOUT = 12
# 探测结果缓存 10 分钟：来回开关选帧面板不该每次都重新 ffprobe 网络源
PROBE_TTL = 600
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


class SourceGate:
    """同一来源同时最多几路 ffmpeg；连续失败就自动降到单路串行。

    本地文件 / 宽松的源保持 3 路小并发，12 张候选帧齐头并进；私有直链这类
    按「瞬时连接数」掐人的源，第一批 403 之后立刻降到 1 路并留一点间隔，
    后面排队和重试的请求才不会继续把整批都撞掉。冷却过后连续成功再回升。
    """

    MAX = 3
    FAIL_WINDOW = 12.0     # 这个时间窗里失败几次算「被掐了」
    FAIL_TRIGGER = 2
    COOLDOWN = 60.0        # 降级后至少保持多久
    RECOVER_EVERY = 8      # 连续成功多少次回升一档
    RETRY_GAP = 0.35       # 降级期间同一来源两次抓帧的最小间隔

    def __init__(self, max_parallel: int = MAX):
        self._max = max(1, int(max_parallel))
        self._cond = threading.Condition()
        self._inflight: dict[str, int] = {}
        self._limit: dict[str, int] = {}
        self._until: dict[str, float] = {}
        self._next: dict[str, float] = {}
        self._fails: dict[str, list[float]] = {}
        self._ok_run: dict[str, int] = {}

    @staticmethod
    def host(url: str) -> str:
        try:
            parts = urlsplit(url)
            return parts.netloc or parts.scheme or "local"
        except ValueError:
            return "local"

    def acquire(self, key: str, abort: threading.Event | None) -> bool:
        with self._cond:
            while True:
                if abort is not None and abort.is_set():
                    return False
                now = time.monotonic()
                limit = self._limit.get(key, self._max)
                gap = self._next.get(key, 0.0) - now
                if gap <= 0 and self._inflight.get(key, 0) < limit:
                    self._inflight[key] = self._inflight.get(key, 0) + 1
                    if limit <= 1:
                        self._next[key] = now + self.RETRY_GAP
                    return True
                self._cond.wait(timeout=max(0.02, min(gap, 0.2) if gap > 0 else 0.2))

    def release(self, key: str, ok: bool | None) -> None:
        """``ok=None`` 表示客户端取消，只是归还槽位，不计入成败统计。"""
        with self._cond:
            self._inflight[key] = max(0, self._inflight.get(key, 0) - 1)
            now = time.monotonic()
            if ok is True:
                self._fails[key] = []
                if now < self._until.get(key, 0.0):
                    self._ok_run[key] = 0
                else:
                    run = self._ok_run.get(key, 0) + 1
                    if run >= self.RECOVER_EVERY:
                        run = 0
                        current = self._limit.get(key, self._max)
                        if current < self._max:
                            self._limit[key] = current + 1
                            log.info("选帧：%s 恢复稳定，并发回升到 %d 路",
                                     key, current + 1)
                    self._ok_run[key] = run
            elif ok is False:
                self._ok_run[key] = 0
                recent = [t for t in self._fails.get(key, [])
                          if now - t < self.FAIL_WINDOW]
                recent.append(now)
                self._fails[key] = recent
                if (len(recent) >= self.FAIL_TRIGGER
                        and self._limit.get(key, self._max) > 1):
                    self._limit[key] = 1
                    self._until[key] = now + self.COOLDOWN
                    self._next[key] = now + self.RETRY_GAP
                    log.info("选帧：%s 连续抓帧失败，改单路串行避开源站风控", key)
            self._cond.notify_all()

    def limit(self, key: str) -> int:
        with self._cond:
            return self._limit.get(key, self._max)


class FramePicker:
    """候选帧 + 抓帧缓存 + 指定帧出图。线程安全，多个请求可以同时进来。"""

    def __init__(self, cfg: Config, cache_dir: str | Path):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._slots = threading.Semaphore(MAX_PARALLEL)
        self._thumb_slots = threading.Semaphore(THUMB_PARALLEL)
        self._gate = SourceGate()
        self._probes: dict[str, tuple[float, float, probe.MediaInfo]] = {}
        self._writes = 0

    # ---------- 对外接口 ----------

    def plan(self, target: Target, count: int = 12) -> dict:
        """读时长并给出候选时间点，网页打开选帧面板时调一次。

        探测结果缓存 10 分钟：同一个文件反复开关面板不该每次都让 ffprobe
        重新连一次网盘（这一步超时 8 秒，是「面板半天出不来」的常见来源）。
        """
        source = target.source
        info = self._probe_info(source)
        duration = max(0.0, info.duration)
        log.debug("选帧面板：%s 时长 %.1fs", source.name, duration)
        return {
            "path": str(source),
            "url": probe.resolve_source(source),
            "duration": round(duration, 2),
            "size": info.size,
            "width": info.width,
            "height": info.height,
            "candidates": self._candidates(duration, count),
        }

    def frame(self, source: Path, seconds: float, width: int = 0,
              fast: bool = False,
              abort: threading.Event | None = None) -> Path:
        """抓指定时间点的一帧，返回缓存文件路径。

        ``fast`` 是候选缩略图专用通道：只做关键帧跳转、超时更短、失败不
        退化成从头解码定位；用户真正选中的主帧仍走精确定位。
        ``abort`` 被 set（客户端关掉请求）时立刻杀掉 ffmpeg。
        """
        seconds = max(0.0, float(seconds or 0))
        out = self._cache_path(source, seconds, width)
        if self._fresh(out):
            _touch(out)
            return out

        with self._lock_for(out.name):
            if self._fresh(out):  # 等锁期间别的请求已经抓好
                return out
            # 同一秒的原图已经在缓存里（往往是刚打开面板抓的主帧）：
            # 直接缩放一份，别为了 260px 的小图再打一次网络
            if width > 0 and self._reuse_full(source, seconds, out, width):
                self._sweep()
                return out

            url = probe.resolve_source(source)
            # 全局槽位之外再按来源限一道：被源站掐过之后自动降到单路串行，
            # 不然 12 张候选帧会一齐撞上去，整批 403 全变空白格
            key = SourceGate.host(url)
            if not self._gate.acquire(key, abort):
                raise frames.CaptureAborted("请求已取消")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 后缀必须是 .jpg，ffmpeg 靠它选编码器；.part 只是防止读到半截文件
            tmp = out.with_suffix(".part.jpg")
            timeout = FAST_TIMEOUT if fast else max(15, min(self.cfg.scan.timeout, 60))
            ok = False
            cancelled = False
            try:
                if fast:
                    ok = self._run_fast(url, seconds, tmp, width, timeout, abort)
                else:
                    ok = self._run_full(url, seconds, tmp, width, timeout, abort)
            except frames.CaptureAborted:
                cancelled = True
                tmp.unlink(missing_ok=True)
                raise
            except links.LinkDeferred:
                # 全局闸门排队满 / 超时：ffmpeg 根本没启动。别把它算成
                # 「这个源又抓失败了」，否则选帧的按来源并发闸会白白降级。
                cancelled = True
                tmp.unlink(missing_ok=True)
                raise
            finally:
                self._gate.release(key, None if cancelled else ok)
            if not ok:
                raise frames.FrameError(
                    f"{clock(seconds)} 这一帧抓不下来：链接可能已失效，"
                    "或这个位置无法解码（换一个时间点再试）"
                )
            tmp.replace(out)
            self._sweep()
        return out

    def _run_fast(self, url: str, seconds: float, tmp: Path, width: int,
                  timeout: int, abort: threading.Event | None) -> bool:
        # 先拿缩略图配额、再拿全局配额：这样最多 3 张缩略图能进 ffmpeg，
        # 第 4 个槽永远留给主帧。顺序反过来会让 4 个槽全被缩略图占满。
        with self._thumb_slots:
            with self._slots:
                return frames.capture(url, seconds, tmp, timeout=timeout,
                                      fast_seek=True, width=width, abort=abort)

    def _run_full(self, url: str, seconds: float, tmp: Path, width: int,
                  timeout: int, abort: threading.Event | None) -> bool:
        with self._slots:
            return frames.capture_at(url, seconds, tmp, timeout=timeout,
                                     width=width, abort=abort)

    def _reuse_full(self, source: Path, seconds: float, out: Path,
                    width: int) -> bool:
        """把同秒已缓存的原图缩成小图，省掉一次 ffmpeg。"""
        full = self._cache_path(source, seconds, 0)
        if not self._fresh(full):
            return False
        try:
            with Image.open(full) as raw:
                img = raw.convert("RGB")
                iw, ih = img.size
                if iw <= 0 or ih <= 0:
                    return False
                height = max(2, round(ih * width / iw))
                img.resize((width, height), Image.LANCZOS).save(
                    out, "JPEG", quality=85, optimize=True)
            _touch(full)
            return out.is_file() and out.stat().st_size >= MIN_PREVIEW_BYTES
        except (OSError, ValueError):
            out.unlink(missing_ok=True)
            return False

    def apply(self, target: Target, seconds: float, kinds: list[str],
              crop: tuple[float, float, float, float] | None = None,
              crop_for: str = "all") -> dict:
        """用选中的那一帧重新生成海报 / 背景图 / 缩略图。

        ``crop`` 是用户在网页上拖出来的取景框，按「相对整帧的比例」给
        ``(x, y, w, h)``（0–1）。``crop_for`` 说明这个框作用于谁：
        ``poster`` 只裁海报、``fanart`` 只裁背景图/缩略图、``all`` 一起裁。
        """
        cfg = self.cfg.scrape
        frame = self.frame(target.source, seconds, 0)  # 原图，缓存供后续重出
        cropped = self._cropped(frame, crop) if crop else None

        def source_for(kind: str) -> Path:
            if cropped is None:
                return frame
            if crop_for == "all":
                return cropped
            if crop_for == "poster":
                return cropped if kind == "poster" else frame
            # fanart：背景图和缩略图都是横版，跟着一起裁
            return frame if kind == "poster" else cropped

        if "poster" in kinds:
            frames.make_poster(
                source_for("poster"), target.poster,
                width=cfg.poster.width, height=cfg.poster.height,
                mode=cfg.poster.mode, quality=cfg.jpeg_quality,
            )
        if "fanart" in kinds:
            frames.make_fanart(
                source_for("fanart"), target.fanart,
                width=cfg.fanart.width, height=cfg.fanart.height,
                quality=cfg.jpeg_quality,
            )
        if "thumb" in kinds:
            frames.make_thumb(
                source_for("thumb"), target.thumb,
                width=cfg.thumb.width, height=cfg.thumb.height,
                quality=cfg.jpeg_quality,
            )

        log.info("手动选帧：%s 使用 %s（%s%s）", target.source.name,
                 clock(seconds), "、".join(kinds),
                 "，取景 " + _crop_label(crop) if crop else "")
        return {
            "path": str(target.source),
            "seconds": round(float(seconds), 2),
            "label": clock(seconds),
            "kinds": kinds,
            "artifacts": artifacts(target),
            "duration": round(self._duration(target.source), 2),
        }

    def cropped(self, source: Path, seconds: float, crop, width: int = 0,
                abort: threading.Event | None = None) -> Path:
        """按取景框裁一版预览图（网页上拖框时实时看效果）。"""
        frame = self.frame(source, seconds, width, abort=abort)
        return self._cropped(frame, crop)

    def _cropped(self, frame: Path, crop) -> Path:
        """把缓存的整帧按取景框裁下来并缓存，同一框只算一次。"""
        x, y, w, h = _norm_crop(crop)
        digest = hashlib.sha1(
            f"{frame.name}|{x:.4f}|{y:.4f}|{w:.4f}|{h:.4f}".encode("utf-8")
        ).hexdigest()[:20]
        out = frame.parent / f"c_{digest}.jpg"
        if self._fresh(out):
            _touch(out)
            return out
        with self._lock_for(out.name):
            if self._fresh(out):
                return out
            with Image.open(frame) as img:
                img = img.convert("RGB")
                width, height = img.size
                left = min(max(0, round(x * width)), max(0, width - 2))
                top = min(max(0, round(y * height)), max(0, height - 2))
                right = min(width, left + max(2, round(w * width)))
                bottom = min(height, top + max(2, round(h * height)))
                img.crop((left, top, right, bottom)).save(
                    out, "JPEG", quality=95, optimize=True)
            self._sweep()
        return out

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

    def _probe_info(self, source: Path) -> probe.MediaInfo:
        """ffprobe 结果缓存 10 分钟（key 含文件修改时间，改过 strm 会失效）。"""
        key = self._key(source)
        with self._guard:
            cached = self._probes.get(key)
        if cached and time.time() - cached[1] < PROBE_TTL:
            return cached[2]
        url = probe.resolve_source(source)
        # 网盘处在退避期时，探测会先在全局闸门里排队。选帧面板不能一直挂着，
        # 给这次探测一个排队上限；超时就报错，前端会提示稍后再试。
        wait_abort = threading.Event()
        timer = threading.Timer(max(8.0, self.cfg.scan.probe_timeout + 8.0),
                                wait_abort.set)
        timer.daemon = True
        timer.start()
        try:
            info = probe.probe(url, timeout=self.cfg.scan.probe_timeout,
                               abort=wait_abort)
        finally:
            timer.cancel()
        with self._guard:
            self._probes[key] = (max(0.0, info.duration), time.time(), info)
        return info

    def _duration(self, source: Path) -> float:
        return max(0.0, self._probe_info(source).duration)

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


def _norm_crop(crop) -> tuple[float, float, float, float]:
    """把取景框规整成 0–1 的 (x, y, w, h)，越界或不合法就退回整帧。"""
    try:
        x, y, w, h = (float(v) for v in crop)
    except (TypeError, ValueError):
        return (0.0, 0.0, 1.0, 1.0)
    w = min(1.0, max(0.02, w))
    h = min(1.0, max(0.02, h))
    x = min(max(0.0, x), 1.0 - w)
    y = min(max(0.0, y), 1.0 - h)
    return (x, y, w, h)


def _crop_label(crop) -> str:
    x, y, w, h = _norm_crop(crop)
    return f"{w * 100:.0f}%×{h * 100:.0f}% @ {x * 100:.0f}%/{y * 100:.0f}%"


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
