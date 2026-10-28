# -*- coding: utf-8 -*-
"""单条刮削流程与并发调度。"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import frames, nfo, probe
from .config import Config
from .media import (Target, artifacts, build_target, iter_sources, product_state,
                    signature)
from .state import Item, StateStore

log = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    ok: bool
    reason: str = ""
    title: str = ""
    duration: float = 0.0
    poster: bool = False
    fanart: bool = False
    thumb: bool = False
    nfo: bool = False
    sizes: dict = field(default_factory=dict)
    elapsed: float = 0.0


def process(cfg: Config, target: Target) -> ScrapeResult:
    """处理一个 strm：探测 → 抓帧 → 出图 → 写 NFO。"""
    started = time.monotonic()
    overwrite = cfg.scrape.overwrite == "always"
    want_thumb = cfg.scrape.thumb.enabled

    complete, missing = product_state(
        target, cfg.scrape.write_nfo, cfg.scrape.write_poster,
        cfg.scrape.write_fanart, want_thumb,
    )
    if complete and not overwrite:
        return ScrapeResult(ok=True, reason="产物已存在，跳过", title=target.stem)

    log.debug("开始处理 %s（缺 %s）", target.stem[:50], ",".join(missing) or "无")

    url = probe.resolve_source(target.source)
    info = probe.probe(url, timeout=cfg.scan.probe_timeout)
    title = nfo.display_title(target.stem)

    need_image = (
        (cfg.scrape.write_poster and (overwrite or "poster" in missing))
        or (cfg.scrape.write_fanart and (overwrite or "fanart" in missing))
        or (want_thumb and (overwrite or "thumb" in missing))
    )

    made_poster = made_fanart = made_thumb = False
    if need_image:
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            workdir = Path(tmp)
            best, count = frames.capture_best(
                url,
                info.duration,
                cfg.scrape.frame_positions[: max(1, cfg.scrape.frames)],
                workdir,
                timeout=cfg.scan.timeout,
            )
            log.debug("%s 抓到 %s 帧，选用 %s", target.stem[:40], count, best.name)

            if cfg.scrape.write_poster and (overwrite or "poster" in missing):
                frames.make_poster(
                    best, target.poster,
                    width=cfg.scrape.poster.width,
                    height=cfg.scrape.poster.height,
                    mode=cfg.scrape.poster.mode,
                    quality=cfg.scrape.jpeg_quality,
                )
                made_poster = True

            if cfg.scrape.write_fanart and (overwrite or "fanart" in missing):
                frames.make_fanart(
                    best, target.fanart,
                    width=cfg.scrape.fanart.width,
                    height=cfg.scrape.fanart.height,
                    quality=cfg.scrape.jpeg_quality,
                )
                made_fanart = True

            if want_thumb and (overwrite or "thumb" in missing):
                frames.make_thumb(
                    best, target.thumb,
                    width=cfg.scrape.thumb.width,
                    height=cfg.scrape.thumb.height,
                    quality=cfg.scrape.jpeg_quality,
                )
                made_thumb = True

    made_nfo = False
    if cfg.scrape.write_nfo and (overwrite or "nfo" in missing):
        nfo.write_nfo(target.nfo, info, title)
        made_nfo = True

    return ScrapeResult(
        ok=True,
        title=title,
        duration=info.duration,
        poster=made_poster,
        fanart=made_fanart,
        thumb=made_thumb,
        nfo=made_nfo,
        sizes=artifacts(target),
        elapsed=time.monotonic() - started,
    )


ProgressHook = Callable[[str, Item, ScrapeResult | None], None]


class Runner:
    """管理扫描与刮削队列，状态供网页读取。"""

    def __init__(self, cfg: Config, store: StateStore,
                 on_done: ProgressHook | None = None,
                 on_refresh: Callable[[list[str]], None] | None = None):
        self.cfg = cfg
        self.store = store
        self._on_done = on_done
        self._on_refresh = on_refresh

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.state = "idle"  # idle / scanning / running / stopping
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.current: dict[str, float] = {}
        self.started_at = 0.0
        self.finished_at = 0.0
        self.last_error = ""
        self.mode = ""

    # ---------- 对外接口 ----------

    def busy(self) -> bool:
        with self._lock:
            return self.state in ("scanning", "running", "stopping")

    def start(self, full: bool = False) -> bool:
        """启动一轮扫描+刮削。已在跑就返回 False。"""
        with self._lock:
            if self.busy():
                return False
            self._stop.clear()
            self.processed = self.succeeded = self.failed = 0
            self.current.clear()
            self.started_at = time.time()
            self.finished_at = 0.0
            self.last_error = ""
            self.state = "scanning"
            self.mode = "full" if full else "incremental"

        self._thread = threading.Thread(target=self._run, args=(full,), daemon=True,
                                        name="ws-runner")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.state in ("scanning", "running"):
                self.state = "stopping"

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self.started_at) if self.started_at else 0
            return {
                "state": self.state,
                "mode": self.mode,
                "processed": self.processed,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "active": sorted(self.current),
                "concurrency": self.cfg.scan.concurrency,
                "elapsed": round(elapsed, 1),
                "last_error": self.last_error,
                "stats": self.store.stats(),
            }

    # ---------- 内部流程 ----------

    def _run(self, full: bool) -> None:
        started = time.time()
        try:
            added = self._scan(full)
            log.info("扫描完成，新登记 %s 个文件，待处理 %s 个",
                     added, self.store.count("pending"))
            with self._lock:
                self.state = "running"

            scraped_paths = self._drain()
            self._refresh_servers(scraped_paths)
        except Exception as exc:  # noqa: BLE001 - 主控线程不能静默死掉
            log.exception("调度线程异常")
            with self._lock:
                self.last_error = str(exc)
        finally:
            with self._lock:
                self.state = "idle"
                self.finished_at = time.time()
                self.current.clear()
            self.store.set_meta("last_run", str(int(time.time())))
            log.info("本轮结束：处理 %s，成功 %s，失败 %s，用时 %.1f 分钟",
                     self.processed, self.succeeded, self.failed,
                     (time.time() - started) / 60)

    def _scan(self, full: bool) -> int:
        cfg = self.cfg
        rows = []
        for source in iter_sources(cfg.scan.roots, cfg.scan.extensions, cfg.scan.recursive):
            size, mtime = signature(source)
            rows.append((str(source), size, mtime))
        added = self.store.ensure(rows)

        if full:
            # 全量：把所有条目重置，连历史失败项一起重来
            reset = self.store.reset(("ok", "skipped", "failed"))
            log.info("全量模式：重置 %s 个已有记录", reset)
        else:
            # 增量：失败的自动重试；成功的只要产物缺失也会重新入队
            self.store.reset(("failed",))
            for path, size, mtime in rows:
                item = self.store.get(path)
                if item and item.status in ("ok", "skipped"):
                    target = build_target(Path(path))
                    complete, _ = product_state(
                        target, cfg.scrape.write_nfo, cfg.scrape.write_poster,
                        cfg.scrape.write_fanart, cfg.scrape.thumb.enabled,
                    )
                    if not complete:
                        self.store.reset_one(path)
        return added

    def _drain(self) -> list[str]:
        """取空待处理队列，返回刮削成功的路径。"""
        cfg = self.cfg
        workers = max(1, cfg.scan.concurrency)
        ok_paths: list[str] = []
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ws-worker")
        pending: set = set()

        try:
            while not self._stop.is_set():
                while len(pending) < workers:
                    batch = self.store.next_pending(1)
                    if not batch:
                        break
                    item = batch[0]
                    self.store.mark_running(item.path)
                    with self._lock:
                        self.current[item.path] = time.time()
                    pending.add(executor.submit(self._work, item))

                if not pending:
                    break

                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    path = future.result()
                    if path:
                        ok_paths.append(path)

            if self._stop.is_set() and pending:
                log.info("收到停止请求，等待 %s 个进行中的任务收尾", len(pending))
                wait(pending, timeout=cfg.scan.timeout + 10)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return ok_paths

    def _work(self, item: Item) -> str:
        """线程池里执行单个任务，返回成功时的路径。"""
        target = build_target(Path(item.path))
        result: ScrapeResult | None = None
        try:
            result = process(self.cfg, target)
        except (probe.ProbeError, frames.FrameError) as exc:
            result = ScrapeResult(ok=False, reason=str(exc))
        except OSError as exc:
            result = ScrapeResult(ok=False, reason=f"文件读写失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单条失败不能影响整轮
            log.exception("处理失败：%s", item.path, extra={"item": item.path})
            result = ScrapeResult(ok=False, reason=f"未预期的错误：{exc}")

        # 写盘后刷新时间戳，避免下一轮重复处理
        size, mtime = signature(Path(item.path))

        if result.ok:
            if result.reason == "产物已存在，跳过":
                self.store.mark_skipped(item.path, result.reason)
                log.debug("[跳过] %s（产物齐全）", target.stem[:60], extra={"item": item.path})
            else:
                self.store.mark_ok(item.path, result.title, result.duration)
                self.store.touch_signature(item.path, size, mtime)
                log.info(
                    "[完成] %s | %.1fs | 海报 %s 背景 %s%s NFO %s",
                    result.title[:60], result.elapsed,
                    _size(result.sizes.get("poster")), _size(result.sizes.get("fanart")),
                    f" 缩略图 {_size(result.sizes.get('thumb'))}" if result.thumb else "",
                    _size(result.sizes.get("nfo")),
                    extra={"item": item.path},
                )
            with self._lock:
                self.succeeded += 1
        else:
            self.store.mark_failed(item.path, result.reason)
            with self._lock:
                self.failed += 1
            log.warning("[失败] %s | %s", target.stem[:60], result.reason,
                        extra={"item": item.path})

        with self._lock:
            self.processed += 1
            self.current.pop(item.path, None)

        if self._on_done:
            try:
                self._on_done(item.path, item, result)
            except Exception:  # noqa: BLE001
                log.debug("进度回调异常", exc_info=True)

        return item.path if result.ok and result.reason != "产物已存在，跳过" else ""

    def _refresh_servers(self, paths: list[str]) -> None:
        """刮削完成后，让配置里的每台 Emby / Jellyfin 重建图片。"""
        if not paths or not self._on_refresh:
            return
        try:
            self._on_refresh(paths)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不影响刮削结果
            log.warning("媒体服务器刷新失败：%s", exc)


def _size(value: int | None) -> str:
    """字节数转成好读的短写法，日志用。"""
    if not value:
        return "未生成"
    if value >= 1024 * 1024:
        return f"{value / 1048576:.1f}MB"
    return f"{value / 1024:.0f}KB"
