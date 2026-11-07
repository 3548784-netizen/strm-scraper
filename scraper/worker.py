# -*- coding: utf-8 -*-
"""单条刮削流程与并发调度。"""
from __future__ import annotations

import logging
import stat as stat_mod
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import frames, links, nfo, picker, probe, throttle
from .config import Config
from .media import (Target, artifacts, build_target, product_state, scan_rows,
                    signature, split_by_products)
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


def process(cfg: Config, target: Target, frame_at: float = 0.0,
            abort: threading.Event | None = None, force: bool = False) -> ScrapeResult:
    """处理一个 strm：探测 → 抓帧 → 出图 → 写 NFO。

    包一层是为了把「这次到底连没连上网盘」回写到直链台账。暂停 / 取消
    （CaptureAborted）和请求没发出去（LinkDeferred）都不算连接失效，不能
    因此把一条本来好好的直链标成过期——否则下一批任务会集体去刷新它。

    ``frame_at`` 大于 0 时表示用户在网页上手动选过帧，直接抓那一秒，
    不再按比例采样自动挑选。``abort`` 被 set 时立刻中断正在跑的 ffmpeg
    并抛 :class:`frames.CaptureAborted`，让「暂停 / 取消」即时生效。
    ``force`` 为真时忽略「产物已齐全」，全部重做并覆盖旧文件（单次有效，
    不改全局的 overwrite 设置）。
    """
    try:
        result = _process(cfg, target, frame_at, abort, force)
    except (frames.CaptureAborted, links.LinkDeferred):
        raise
    except Exception as exc:  # noqa: BLE001
        links.note_result(target.source, False, str(exc))
        raise
    # 产物齐全直接跳过时根本没连网盘，别把一条可能早已过期的直链
    # 误标成「刚验证过」
    if result.reason != "产物已存在，跳过":
        links.note_result(target.source, True, "")
    return result


def _process(cfg: Config, target: Target, frame_at: float = 0.0,
             abort: threading.Event | None = None,
             force: bool = False) -> ScrapeResult:
    started = time.monotonic()
    overwrite = force or cfg.scrape.overwrite == "always"
    want_thumb = cfg.scrape.thumb.enabled

    complete, missing = product_state(
        target, cfg.scrape.write_nfo, cfg.scrape.write_poster,
        cfg.scrape.write_fanart, want_thumb,
    )
    if complete and not overwrite:
        return ScrapeResult(ok=True, reason="产物已存在，跳过", title=target.stem)

    log.debug("开始处理 %s（缺 %s）", target.stem[:50], ",".join(missing) or "无")

    url = probe.resolve_source(target.source)
    # 上一次撞到限流的话，这里先等它到期；等待期间点暂停能立刻退出
    if not throttle.GUARD.wait(abort):
        raise frames.CaptureAborted("任务已暂停")
    info = probe.probe(url, timeout=cfg.scan.probe_timeout, abort=abort)
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
            if frame_at and frame_at > 0:
                best = workdir / "f_manual.jpg"
                seconds = frame_at
                if info.duration > 1:
                    # 别落在最后一帧之外，否则 ffmpeg 直接什么都不输出
                    seconds = min(seconds, info.duration - 1.0)
                if not frames.capture_at(url, seconds, best, timeout=cfg.scan.timeout,
                                         abort=abort):
                    raise frames.FrameError(
                        f"手动选的 {picker.clock(seconds)} 这一帧抓不下来，"
                        "链接可能已失效，可以在网页上重新选一个时间点"
                    )
                count = 1
                log.debug("%s 使用手动选定的 %s", target.stem[:40], picker.clock(seconds))
            else:
                best, count = frames.capture_best(
                    url,
                    info.duration,
                    frames.pick_positions(cfg.scrape.frame_positions,
                                          cfg.scrape.frames),
                    workdir,
                    timeout=cfg.scan.timeout,
                    abort=abort,
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
        self._paused = threading.Event()
        # 传给正在跑的 ffmpeg：一旦置位就立刻杀掉子进程。暂停和取消都会置位，
        # 否则用户点了暂停要等整轮抓帧跑完才停下，看起来就像没生效
        self._abort = threading.Event()
        self._cancelled = False
        self._thread: threading.Thread | None = None

        # idle / scanning / running / paused / stopping
        self.state = "idle"
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.current: dict[str, float] = {}
        self.started_at = 0.0
        self.finished_at = 0.0
        # 暂停时长要从「用时」里扣掉：暂停一小时再继续，界面和日志
        # 都不该显示这轮跑了一小时
        self._paused_at = 0.0
        self._paused_total = 0.0
        self.last_error = ""
        self.last_message = ""
        self.mode = ""
        # 扫描阶段已发现的文件数：文件多时启动那一下要好几秒，
        # 透到界面上至少能看出它在动，而不是像卡住了
        self.scanned = 0
        # 只跑这些路径（文件管理里勾选的范围）；None 表示整库待处理都跑
        self._only: set[str] | None = None
        # 本轮是否强制覆盖已有产物（文件管理里的「开始重刮」）
        self._force = False
        # 扫描时「记录已被删掉 + 产物齐全」的文件要不要重新登记成已跳过：
        # 勾选提交是用户主动动作要登记（让用户看到结果），自动化后台扫描
        # 不登记（用户删了就是不想再看到，不能每轮任务都给加回来）
        self._register_ready = True

    # ---------- 对外接口 ----------

    def busy(self) -> bool:
        with self._lock:
            return self.state in ("scanning", "running", "paused", "stopping")

    def start(self, full: bool = False, only: Iterable[str] | None = None,
              force: bool = False, register_ready: bool = True) -> bool:
        """启动一轮扫描+刮削。已在跑就返回 False。

        ``only`` 给定时只处理这些文件（文件管理里勾了哪个文件夹就只跑哪个），
        不做全库扫描，也不会顺带处理别的目录里的待处理项。
        ``register_ready=False`` 用于自动化后台扫描：记录已被用户删掉、而磁盘
        产物齐全的文件不再登记回任务列表（「删除任务」就该从列表里消失）。
        """
        with self._lock:
            if self.busy():
                return False
            self._stop.clear()
            self._paused.clear()
            self._abort.clear()
            self._cancelled = False
            self.processed = self.succeeded = self.failed = 0
            self.current.clear()
            self.started_at = time.time()
            self.finished_at = 0.0
            self._paused_at = 0.0
            self._paused_total = 0.0
            self.scanned = 0
            self.last_error = ""
            self.last_message = ""
            self.state = "scanning"
            throttle.GUARD.reset()
            self._only = {p for p in (only or ()) if p} if only is not None else None
            self._force = bool(force)
            self._register_ready = bool(register_ready)
            if self._only is not None:
                self.mode = "selection"
            else:
                self.mode = "full" if full else "incremental"

        self._thread = threading.Thread(target=self._run, args=(full,), daemon=True,
                                        name="ws-runner")
        self._thread.start()
        if self._only is not None:
            log.info("任务开始（只处理勾选的 %s 个文件%s）", len(self._only),
                     "，覆盖原有产物" if self._force else "")
        else:
            log.info("任务开始（%s）", "全量" if full else "增量")
        return True

    def add_scope(self, paths: Iterable[str]) -> bool:
        """任务在跑时又勾了新文件：并进当前这轮的范围，别让它们干等。"""
        wanted = {p for p in (paths or ()) if p}
        with self._lock:
            if self._only is None or self.state not in ("scanning", "running", "paused"):
                return False
            self._only |= wanted
        return True

    def _scope(self) -> set[str] | None:
        with self._lock:
            return set(self._only) if self._only is not None else None

    def _settle_pause_locked(self) -> None:
        """把一段已结束的暂停计入总时长；调用方需持有 _lock。"""
        if self._paused_at:
            self._paused_total += max(0.0, time.time() - self._paused_at)
            self._paused_at = 0.0

    def _net_elapsed(self) -> float:
        """本轮净耗时（秒）：不含暂停，任务结束后停在结束那一刻。"""
        end = self.finished_at or time.time()
        raw = max(0.0, end - self.started_at) if self.started_at else 0.0
        paused = self._paused_total
        if self._paused_at:
            # 仍在暂停中，这一段也不计入
            paused += max(0.0, end - self._paused_at)
        return max(0.0, raw - paused)

    def pause(self) -> bool:
        """暂停：不再派发新任务，并立刻中断正在跑的抓帧。

        被中断的条目退回待处理，不算失败；点「继续任务」会重新跑它们。
        """
        with self._lock:
            if self.state not in ("scanning", "running"):
                return False
            self._paused.set()
            if not self._paused_at:
                self._paused_at = time.time()
            self._abort.set()
            self.state = "paused"
            active = len(self.current)
        log.info("任务已暂停，%s 个进行中的任务已中断并退回队列", active)
        return True

    def resume(self) -> bool:
        with self._lock:
            if not self._paused.is_set():
                return False
            self._settle_pause_locked()
            self._paused.clear()
            self._abort.clear()
            alive = self._thread is not None and self._thread.is_alive()
            self.state = "running" if alive else "idle"
            if not alive:
                self._stop.clear()
                self.finished_at = time.time()
        log.info("任务已继续")
        return True

    def cancel(self) -> bool:
        """取消：立刻停止派发，进行中的结果作废、退回待处理。"""
        with self._lock:
            if not self.busy():
                return False
            self._cancelled = True
            self._settle_pause_locked()
            self._paused.clear()
            self._abort.set()
            self._stop.set()
            self.state = "stopping"
        log.warning("任务已取消，进行中的任务结果作废并退回待处理")
        return True

    def stop(self) -> None:
        """进程退出时调用，语义等同取消。"""
        with self._lock:
            if self.state in ("idle",):
                self._stop.set()
                self._abort.set()
                return
            self._cancelled = True
            self._settle_pause_locked()
            self._paused.clear()
            self._abort.set()
            self._stop.set()
            self.state = "stopping"

    def snapshot(self) -> dict:
        with self._lock:
            # 「用时」停在结束那一刻，并扣掉暂停时长：否则暂停一小时后
            # 继续，界面会一直显示这轮跑了一小时
            elapsed = self._net_elapsed()
            paused = self._paused.is_set()
            return {
                "state": self.state,
                "mode": self.mode,
                "paused": paused,
                "processed": self.processed,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "scanned": self.scanned,
                "active": sorted(self.current),
                "concurrency": self.cfg.scan.concurrency,
                "effective_concurrency": min(self.cfg.scan.concurrency,
                                             links.SETTINGS.batch_size),
                "elapsed": round(elapsed, 1),
                "last_error": self.last_error,
                "last_message": self.last_message,
                "throttle": throttle.GUARD.status(),
                "links": links.status(),
                "stats": self.store.stats(),
            }

    # ---------- 内部流程 ----------

    def _run(self, full: bool) -> None:
        try:
            only = self._scope()
            added = self._scan(full, only)
            if only is None:
                log.info("扫描完成，新登记 %s 个文件，待处理 %s 个",
                         added, self.store.count("pending"))
            else:
                log.info("本次只处理勾选的 %s 个文件，不扫描其它目录", len(only))
            with self._lock:
                if self.state == "scanning":
                    self.state = "running"

            scraped_paths = self._drain()
            if not self._cancelled:
                self._refresh_servers(scraped_paths)
        except Exception as exc:  # noqa: BLE001 - 主控线程不能静默死掉
            log.exception("调度线程异常")
            with self._lock:
                self.last_error = str(exc)
        finally:
            with self._lock:
                cancelled = self._cancelled
                self.state = "idle"
                self.finished_at = time.time()
                self.current.clear()
                if cancelled:
                    self.last_message = "任务已取消"
            self.store.set_meta("last_run", str(int(time.time())))
            if cancelled:
                log.warning("本轮已取消：已处理 %s（成功 %s / 失败 %s），剩余 %s 个待处理",
                            self.processed, self.succeeded, self.failed,
                            self.store.count("pending"))
            else:
                log.info("本轮结束：处理 %s，成功 %s，失败 %s，用时 %.1f 分钟",
                         self.processed, self.succeeded, self.failed,
                         self._net_elapsed() / 60)

    def _scan(self, full: bool, only: set[str] | None = None) -> int:
        cfg = self.cfg
        flags = (cfg.scrape.write_nfo, cfg.scrape.write_poster,
                 cfg.scrape.write_fanart, cfg.scrape.thumb.enabled)
        if only is not None:
            # 只处理勾选的文件：不遍历媒体库，别的文件夹里的待处理项也不会被拉起来
            started = time.monotonic()

            def row_of(path: str):
                # 一次 stat 拿到「是不是文件 + 签名」，别再串行发两遍
                try:
                    info = Path(path).stat()
                except OSError:
                    return None
                if not stat_mod.S_ISREG(info.st_mode):
                    return None
                return (path, info.st_size, round(info.st_mtime, 3))

            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = [r for r in pool.map(row_of, sorted(only)) if r]
            paths = [path for path, _s, _m in rows]
            # ensure() 之后所有文件都有记录了，得先记下「原本没记录」的那些：
            # 用户可能刚把已完成的任务从列表里删掉，但磁盘上的产物还在。
            before = self.store.get_many(paths)
            if self._force:
                # 「开始重刮」是明确的覆盖意图，全部重新入队
                self.store.ensure(rows)
                self.store.reset_many(paths)
            else:
                # 只要磁盘产物齐全就一律复用，不看任务记录在不在。登记与否
                # 按来源区分：勾选提交登记成「已跳过」让用户看到结果；自动
                # 化范围任务不登记——用户删过记录的文件，不能每跑一轮任务
                # 就给加回来一次（删完点「立即执行」，列表原样复活一遍）。
                ready, missing = split_by_products(paths, *flags)
                if not self._register_ready:
                    fresh_ready = {p for p in ready if p not in before}
                    if fresh_ready:
                        rows = [r for r in rows if r[0] not in fresh_ready]
                        log.info("复用本地产物：%s 个文件产物齐全（记录已删，不再登记）",
                                 len(fresh_ready))
                self.store.ensure(rows)
                self.store.mark_skipped_many(ready, "产物齐全，跳过")
                self.store.reset_many(missing)
                if ready:
                    log.info("复用本地产物：%s 个文件产物齐全直接跳过", len(ready))
            # 大批量勾选时用户会盯着「点了没反应」，把准备耗时写进日志好定位
            log.info("勾选文件准备完成：%s 个，用时 %.1f 秒",
                     len(rows), time.monotonic() - started)
            return len(rows)

        started = time.monotonic()
        rows = scan_rows(cfg.scan.roots, cfg.scan.extensions, cfg.scan.recursive,
                         progress=self._note_scan)
        if not rows:
            return 0
        all_paths = [path for path, _s, _m in rows]
        before = self.store.get_many(all_paths)

        if full:
            # 全量：把所有条目重置，连历史失败项一起重来
            added = self.store.ensure(rows)
            reset = self.store.reset(("ok", "skipped", "failed"))
            log.info("全量模式：重置 %s 个已有记录", reset)
        else:
            # 增量：只有「退避到期且没超次数」的失败项才重新入队。
            # 原来是 reset(("failed",)) 把整批失败项一次性放回队首，等于在
            # 下一轮开始时重放一次失败高峰，正好会把网盘再撞一次。
            due = self.store.requeue_due(links.retry_max_attempts(), self._scope())
            if due:
                log.info("失败重试：%s 个任务退避到期，已放回队列", due)
            # 记录被删过 / 换了数据目录、而产物齐全的文件：不登记——
            # 「删除任务」就是从列表里移除，别让下一轮自动化扫描又把它们
            # 加回来（那样用户删完记录点一次「立即执行」，列表就复活一遍）。
            # 缺产物的照常登记入队，purge 删过产物的那批还得补刮。
            fresh = [p for p in all_paths if p not in before]
            if fresh:
                ready, _missing = split_by_products(fresh, *flags)
                if ready:
                    drop = set(ready)
                    rows = [r for r in rows if r[0] not in drop]
                    log.info("复用本地产物：%s 个文件产物齐全，记录已删除，不再登记",
                             len(ready))
            added = self.store.ensure(rows)
            # 一次批量取状态：逐个 get() 几千个文件就是几千次查询
            known = self.store.get_many(all_paths)
            stale: list[str] = []
            for path in all_paths:
                item = known.get(path)
                if not item or item.status not in ("ok", "skipped"):
                    continue
                stale.append(path)
            self.store.reset_many(self._missing_products(stale))

        log.info("扫描完成：%s 个文件，新增 %s 个，用时 %.1f 秒",
                 len(rows), added, time.monotonic() - started)
        self.scanned = len(rows)
        return added

    def _note_scan(self, count: int) -> None:
        """扫描进度回调，只存个数字，不写日志（文件多时会刷屏）。"""
        self.scanned = count

    def _missing_products(self, paths: list[str]) -> list[str]:
        """挑出「状态是已完成、但产物已经不在了」的条目。

        每个文件要看好几个产物文件在不在，网络盘上逐个串行 stat 很慢，
        这里并发查（只是 stat，不占 CPU）。
        """
        cfg = self.cfg
        if not paths:
            return []

        def check(path: str) -> str:
            target = build_target(Path(path))
            complete, _ = product_state(
                target, cfg.scrape.write_nfo, cfg.scrape.write_poster,
                cfg.scrape.write_fanart, cfg.scrape.thumb.enabled,
            )
            return "" if complete else path

        with ThreadPoolExecutor(max_workers=8) as pool:
            return [path for path in pool.map(check, paths) if path]

    def _drain(self) -> list[str]:
        """取空待处理队列，返回刮削成功的路径。

        暂停时会中断正在跑的抓帧（``_abort``），被中断的条目退回 pending，
        所以「继续」之后能原样重跑；取消则连同进行中的一起作废。
        """
        cfg = self.cfg
        # 范围任务里派发、退避恢复、等待计数要用同一个范围；范围在每轮循环
        # 开头重新取，因为文件管理可能在跑的过程中 add_scope() 追加文件。
        # 批量在途上限：scan.concurrency 是线程数，links.batch_size 是本轮
        # 允许同时「在途」的任务数，两者取小。突发几千个 strm 时先全部落在
        # 持久队列里，再按这个窗口慢慢放行，不会因为线程多就一起冲出去。
        workers = max(1, min(cfg.scan.concurrency, links.SETTINGS.batch_size))
        ok_paths: list[str] = []
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ws-worker")
        pending: set = set()
        last_requeue = 0.0
        idle_until = 0.0

        try:
            while not self._stop.is_set():
                scope = self._scope()
                if self._paused.is_set():
                    with self._lock:
                        if self.state != "paused":
                            self.state = "paused"
                    while self._paused.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self.state = "running"
                    log.info("任务继续，剩余 %s 个待处理", self.store.count("pending"))
                    continue

                # 退避到期的失败项在一轮任务里也能被接住，但每秒最多查一次，
                # 不会变成每轮循环都刷一遍库
                now_mono = time.monotonic()
                if now_mono - last_requeue >= 1.0:
                    last_requeue = now_mono
                    self.store.requeue_due(links.retry_max_attempts(), scope)

                need = workers - len(pending)
                if need:
                    # 一次取一批、一次改状态：逐条 next_pending(1) +
                    # mark_running 在大批量时就是几千次查询和事务提交
                    batch = self.store.next_pending(need, only=scope)
                    if batch:
                        self.store.mark_running_many(i.path for i in batch)
                        for item in batch:
                            with self._lock:
                                self.current[item.path] = time.time()
                            pending.add(executor.submit(self._work, item))
                        gap = links.SETTINGS.batch_interval_ms
                        if gap:
                            time.sleep(gap / 1000.0)

                if not pending:
                    # 队列看起来空了，但文件管理那边可能刚塞进新的待处理（
                    # 「已加入队列」），所以再问一次，别让它们干等下一次开始
                    if self.store.next_pending(1, only=scope):
                        continue
                    # 还有「等退避 / 等连接槽位」的任务：它们没丢，只是暂时
                    # 不派发。等一小会儿接到期的；等不到就结束本轮，下一轮
                    # 照样会从库里把它们捡起来。
                    retry = self.store.retry_counts(scope)
                    if retry["due"] and \
                            self.store.requeue_due(links.retry_max_attempts(), scope):
                        continue
                    waiting = retry["waiting"]
                    if waiting:
                        if not idle_until:
                            idle_until = time.monotonic()
                        if time.monotonic() - idle_until < 60.0:
                            time.sleep(0.5)
                            continue
                    break

                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    path = future.result()
                    if path:
                        ok_paths.append(path)

            if self._stop.is_set():
                if self._cancelled:
                    # 进行中的任务无法强杀，但结果作废，状态退回待处理
                    running = self.store.reset(("running",))
                    if running:
                        log.info("已把 %s 个进行中的任务退回待处理", running)
                elif pending:
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
            result = process(self.cfg, target, frame_at=item.frame_at,
                             abort=self._abort, force=self._force)
        except frames.CaptureAborted:
            # 暂停 / 取消把 ffmpeg 打断了：这个文件本身没毛病，退回待处理
            with self._lock:
                self.current.pop(item.path, None)
            self.store.reset_one(item.path)
            log.info("[中断] %s | %s，已退回待处理", target.stem[:60],
                     "任务已取消" if self._cancelled else "任务已暂停",
                     extra={"item": item.path})
            return ""
        except links.LinkAborted:
            # 还在排队等网盘连接槽位就被暂停：请求没发出去，直接退回
            with self._lock:
                self.current.pop(item.path, None)
            self.store.reset_one(item.path)
            log.info("[排队中断] %s | 未发出请求，已退回待处理", target.stem[:60],
                     extra={"item": item.path})
            return ""
        except links.LinkDeferred as exc:
            # 队列满 / 排队超时：任务原样留在持久队列，不计失败、不占额度。
            # 给一个短退避，避免几百个任务在同一秒又一起回来排队。
            delay = links.retry_delay(1, "transient")
            self.store.defer_one(item.path, delay)
            with self._lock:
                self.current.pop(item.path, None)
            log.info("[排队] %s | %s，%.0f 秒后重新派发", target.stem[:60],
                     exc, delay, extra={"item": item.path})
            return ""
        except (probe.ProbeError, frames.FrameError) as exc:
            result = ScrapeResult(ok=False, reason=str(exc))
        except OSError as exc:
            result = ScrapeResult(ok=False, reason=f"文件读写失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单条失败不能影响整轮
            log.exception("处理失败：%s", item.path, extra={"item": item.path})
            result = ScrapeResult(ok=False, reason=f"未预期的错误：{exc}")

        # 取消后仍在跑的收尾：结果不算数，状态退回待处理，下次开始接着做
        if self._cancelled:
            self.store.reset_one(item.path)
            with self._lock:
                self.current.pop(item.path, None)
            log.info("[取消] %s | 结果作废，已退回待处理", target.stem[:60],
                     extra={"item": item.path})
            return ""

        # 写盘后刷新时间戳，避免下一轮重复处理
        size, mtime = signature(Path(item.path))

        if result.ok:
            if result.reason == "产物已存在，跳过":
                self.store.mark_skipped(item.path, result.reason)
                log.debug("[跳过] %s（产物齐全）", target.stem[:60], extra={"item": item.path})
            else:
                # 状态和签名一次写完，少一次事务提交
                self.store.mark_ok(item.path, result.title, result.duration,
                                   size, mtime)
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
            # 失败不等于「立刻再来一次」：限流类要退得更久，永久错误和
            # 已到次数上限的不再自动重试，都留在列表里等人工处理。
            attempts = item.attempts + 1
            kind = links.classify(result.reason)
            max_try = links.retry_max_attempts()
            if kind == "permanent" or max_try <= 0 or attempts >= max_try:
                delay = 0.0
                result.reason += "（不再自动重试，可在网页上手动重试）"
            else:
                delay = links.retry_delay(attempts, kind)
            self.store.mark_failed(item.path, result.reason, retry_after=delay)
            with self._lock:
                self.failed += 1
            log.warning("[失败] %s | %s%s", target.stem[:60], result.reason,
                        f"，{delay:.0f} 秒后重试" if delay else "",
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
