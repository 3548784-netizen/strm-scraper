# -*- coding: utf-8 -*-
"""115 网盘直链的连接台账 + 全局请求节奏控制。

strm 文件里写的就是 115 资源的一条**连接地址**（通常是带签名的直链或转直链
服务的播放 URL）。刮削一个 strm 至少要连它 3 次（ffprobe 一次、抓 2 帧），
批量任务一涌进来，瞬时连接数和请求密度都会直接顶到网盘风控上。

这里把「连接」当成一份需要维护的资源，而不是每次现读现用：

* :class:`LinkRegistry` —— 持久化台账（``strm_links`` 表）。一个 strm 只在
  首次 / 文件内容变化时解析一次；解析出的直链**粘住不放**，过期只是标记
  ``stale`` 而不是删掉，更不会在批量任务开始时集体重新校验（那才是最容易
  把账号撞进风控的动作）。真正需要换链时按预算走可选的刷新钩子。
* :class:`RequestGate` —— 全进程共用的请求闸门。所有到网盘的 ffprobe /
  ffmpeg，不论来自批量队列、网页选帧还是单条「立即刮削」，都从这里领一张
  「票」：FIFO 排队 → 令牌桶限速 → 全局并发上限 → 同域并发上限 → 最小间隔。
  撞到 403 / 429 / 5xx 后自动降到单路并进入冷却，恢复也要连续成功若干次。
* 重试策略 —— 失败分类（限流 / 永久 / 临时）、指数退避 + 抖动、最大次数。
  失败项写 ``next_retry`` 到持久队列，**到期才重新入队**，不会在下一轮任务
  开始时把成百上千条失败项一起放回队首。

设计上刻意不引入新的并行队列：``items`` 表本来就是持久队列（pending /
running / failed），突发任务先全部落库（不丢），再由闸门按节奏放出去（不撞）。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import throttle

log = logging.getLogger(__name__)

# 只有这些协议要过闸门；本地文件路径 / Windows 盘符一律直连
_NET_SCHEMES = ("http://", "https://", "rtsp://", "ftp://")

# 判定「链接本身废了」的信号：这类错误重试多少次都不会好，别浪费请求额度
_PERMANENT = (
    "404", "not found", "401", "unauthorized", "地址要求鉴权",
    "没有可用的播放地址", "编码无法识别", "没有视频轨道", "不是有效视频",
    "下载不完整", "不支持的协议", "域名解析失败", "找不到 ffprobe",
    "未安装或不可执行", "无法解析", "读取 strm 失败",
)


class LinkDeferred(RuntimeError):
    """请求**没有发出去**（排队超时 / 队列已满 / 任务被取消），应该退回队列。

    和「失败」严格区分：不计 attempts、不进退避、不算一次网盘请求。批量高峰
    时靠这个把任务原样留在持久队列里，既不丢也不放大流量。
    """


class LinkAborted(LinkDeferred):
    """等待槽位期间收到暂停 / 取消 / 客户端断开。"""


class LinkError(RuntimeError):
    """直链本身不可用（strm 里没写地址、刷新钩子返回非法值等）。"""


# ----------------------------------------------------------------------
# 参数快照：不 import config，避免 probe/frames/config 之间形成环
# ----------------------------------------------------------------------

@dataclass
class Settings:
    enabled: bool = True
    # 节奏
    max_inflight: int = 2
    per_host_inflight: int = 2
    rate_per_minute: float = 60.0
    burst: int = 2
    min_gap_ms: int = 300
    queue_max: int = 2000
    acquire_timeout: float = 180.0
    bypass_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    # 撞墙后的自适应降级
    cooldown_seconds: float = 60.0
    degrade_gap_ms: int = 800
    recover_every: int = 8
    # 直链台账
    link_ttl_minutes: int = 360
    sticky: bool = True
    refresh_url: str = ""
    refresh_token: str = ""
    refresh_on_error: bool = True
    verify_budget_per_minute: int = 6
    # 批量任务
    batch_size: int = 20
    batch_interval_ms: int = 0
    # 失败恢复
    retry_base_seconds: float = 30.0
    retry_max_seconds: float = 1800.0
    retry_jitter: float = 0.30
    retry_max_attempts: int = 5

    @classmethod
    def from_config(cls, cfg) -> "Settings":
        raw = getattr(cfg, "links", None)
        data: dict[str, Any] = {}
        if raw is not None:
            for f in cls.__dataclass_fields__:
                if hasattr(raw, f):
                    data[f] = getattr(raw, f)
        if "bypass_hosts" in data:
            data["bypass_hosts"] = tuple(str(h).strip().lower()
                                         for h in (data["bypass_hosts"] or ()) if h)
        s = cls(**data)
        s.clamp()
        return s

    def clamp(self) -> None:
        """把明显不合法的值收回来，配置写错也不能把闸门变成无限制。"""
        self.max_inflight = max(1, min(64, int(self.max_inflight or 1)))
        self.per_host_inflight = max(1, min(64, int(self.per_host_inflight or 1)))
        self.rate_per_minute = max(0.1, min(100000.0, float(self.rate_per_minute or 1)))
        self.burst = max(1, min(64, int(self.burst or 1)))
        self.min_gap_ms = max(0, min(60000, int(self.min_gap_ms or 0)))
        self.queue_max = max(0, int(self.queue_max or 0))
        self.acquire_timeout = max(1.0, float(self.acquire_timeout or 1))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds or 0))
        self.degrade_gap_ms = max(0, min(60000, int(self.degrade_gap_ms or 0)))
        self.recover_every = max(1, int(self.recover_every or 1))
        self.link_ttl_minutes = max(1, int(self.link_ttl_minutes or 1))
        self.verify_budget_per_minute = max(0, int(self.verify_budget_per_minute or 0))
        self.batch_size = max(1, min(10000, int(self.batch_size or 1)))
        self.batch_interval_ms = max(0, min(60000, int(self.batch_interval_ms or 0)))
        self.retry_base_seconds = max(1.0, float(self.retry_base_seconds or 1))
        self.retry_max_seconds = max(self.retry_base_seconds,
                                     float(self.retry_max_seconds or 1))
        self.retry_jitter = max(0.0, min(0.9, float(self.retry_jitter or 0)))
        self.retry_max_attempts = max(0, min(100, int(self.retry_max_attempts or 0)))

    def public(self) -> dict:
        """给网页 /api/links 看的参数（不含刷新钩子的令牌）。"""
        return {
            "enabled": self.enabled,
            "max_inflight": self.max_inflight,
            "per_host_inflight": self.per_host_inflight,
            "rate_per_minute": self.rate_per_minute,
            "burst": self.burst,
            "min_gap_ms": self.min_gap_ms,
            "queue_max": self.queue_max,
            "acquire_timeout": self.acquire_timeout,
            "cooldown_seconds": self.cooldown_seconds,
            "degrade_gap_ms": self.degrade_gap_ms,
            "recover_every": self.recover_every,
            "link_ttl_minutes": self.link_ttl_minutes,
            "sticky": self.sticky,
            "refresh_url": self.refresh_url,
            "refresh_on_error": self.refresh_on_error,
            "verify_budget_per_minute": self.verify_budget_per_minute,
            "batch_size": self.batch_size,
            "batch_interval_ms": self.batch_interval_ms,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "retry_jitter": self.retry_jitter,
            "retry_max_attempts": self.retry_max_attempts,
            "bypass_hosts": list(self.bypass_hosts),
        }


# ----------------------------------------------------------------------
# 地址识别
# ----------------------------------------------------------------------

def is_network(source: str | Path) -> bool:
    text = str(source)
    return text.lower().startswith(_NET_SCHEMES)


def host_of(source: str | Path) -> str:
    """取 host:port；本地文件返回 ``local``。"""
    text = str(source)
    if not is_network(text):
        return "local"
    match = re.match(r"^[a-z]+://([^/?#]+)", text, re.I)
    return (match.group(1) if match else "remote").lower()


def _bare_host(host: str) -> str:
    """从 ``host[:port]`` 里取主机名，兼容 ``[::1]:8080`` 这种 IPv6 写法。"""
    try:
        return urllib.parse.urlsplit("//" + host).hostname or ""
    except ValueError:
        return host.split("@")[-1].split(":")[0]


def _bypassed(host: str, s: Settings) -> bool:
    """本机 / 测试用的媒体服务不占网盘额度，也不该被限速拖慢。"""
    bare = _bare_host(host)
    # 带方括号的 IPv6（[::1]）也要能和白名单里的 ::1 对上
    return bare in s.bypass_hosts or host in s.bypass_hosts


# ----------------------------------------------------------------------
# 全局请求闸门
# ----------------------------------------------------------------------

@dataclass(eq=False)
class _Waiter:
    """按**对象身份**排队；不能用值相等，否则同时等待的票会互相误删。"""

    cancelled: bool = False
    granted: bool = False


@dataclass
class _Lease:
    host: str
    ok: bool | None = None
    error: str = ""

    def note(self, ok: bool, error: str = "") -> None:
        self.ok = bool(ok)
        self.error = error or ""


class _Passthrough:
    """本地文件 / 白名单主机：不占额度、不排队。"""

    def note(self, ok: bool, error: str = "") -> None:
        pass


class RequestGate:
    """FIFO + 令牌桶 + 并发上限 + 自适应降级，全进程共用一把锁。

    为什么用显式 FIFO 而不是 semaphore：短时间涌入几百个任务时，信号量只保证
    「不超过 N 个在跑」，唤醒顺序由操作系统决定，后来的请求可能一直抢到槽位、
    先来的饿死；而且失败重试会和首次请求混在一起乱序冲出去。这里给每个等待
    者发票，只有队首能尝试放行，节奏可预测、可观测。
    """

    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings()
        self._cond = threading.Condition()
        self._queue: deque[_Waiter] = deque()
        self._inflight = 0
        self._per_host: dict[str, int] = {}
        self._tokens = float(self.s.burst)
        self._refill_at = time.monotonic()
        self._next_allowed = 0.0
        self._degrade_until = 0.0
        self._adaptive = 0
        self._ok_run = 0
        self._peak = 0
        self._granted = 0
        self._deferred = 0
        self._throttle_hits = 0
        self._stop = False

    # ---------- 配置热更新 ----------

    def reconfigure(self, settings: Settings) -> None:
        with self._cond:
            self.s = settings
            self._tokens = min(self._tokens, float(settings.burst))
            self._adaptive = min(self._adaptive or 0, settings.max_inflight) or 0
            self._cond.notify_all()

    # ---------- 核心 ----------

    def _refill(self, now: float) -> None:
        rate = self.s.rate_per_minute / 60.0
        elapsed = max(0.0, now - self._refill_at)
        self._refill_at = now
        self._tokens = min(float(self.s.burst), self._tokens + elapsed * rate)

    def _gap(self) -> float:
        gap = self.s.min_gap_ms / 1000.0
        if time.monotonic() < self._degrade_until:
            gap = max(gap, self.s.degrade_gap_ms / 1000.0)
        return gap

    def _limit(self) -> int:
        # 降级后的并发要**逐级**回升：只按冷却期判断的话，冷却一结束就会
        # 直接弹回 max_inflight，`recover_every` 形同虚设，账号刚被 403 掐过
        # 又会被满速请求撞一次。这里只要 _adaptive 还在（1..max_inflight），
        # 就按它限流，直到连续成功够次数才 +1，回到 max 后归零。
        if self._adaptive:
            return max(1, min(self.s.max_inflight, self._adaptive))
        return self.s.max_inflight

    def _can_start(self, host: str, now: float) -> bool:
        if throttle.GUARD.status()["remaining"] > 0:
            return False            # 退避期内一个都不放
        if now < self._next_allowed:
            return False
        if self._inflight >= self._limit():
            return False
        per_host = min(self.s.per_host_inflight, self._limit())
        if self._per_host.get(host, 0) >= per_host:
            return False
        return self._tokens >= 1.0

    def _consume(self, host: str, now: float) -> None:
        self._tokens -= 1.0
        self._next_allowed = now + self._gap()
        self._inflight += 1
        self._per_host[host] = self._per_host.get(host, 0) + 1
        self._peak = max(self._peak, self._inflight)
        self._granted += 1

    def _drop_waiter(self, waiter: _Waiter) -> None:
        try:
            self._queue.remove(waiter)
        except ValueError:
            pass
        self._cond.notify_all()

    def acquire(self, host: str, abort: threading.Event | None = None) -> None:
        """领一张票；拿不到就**阻塞排队**，异常时请求根本没发出去。"""
        s = self.s
        if not s.enabled:
            return
        deadline = time.monotonic() + s.acquire_timeout
        with self._cond:
            if s.queue_max and len(self._queue) >= s.queue_max:
                self._deferred += 1
                raise LinkDeferred("请求队列已满，任务稍后自动重排")
            waiter = _Waiter()
            self._queue.append(waiter)
            try:
                while not waiter.granted:
                    now = time.monotonic()
                    if self._stop or (abort is not None and abort.is_set()):
                        self._deferred += 1
                        raise LinkAborted("等待网盘连接时任务已暂停")
                    if now >= deadline:
                        self._deferred += 1
                        raise LinkDeferred(
                            f"排队 {s.acquire_timeout:.0f} 秒仍未拿到网盘连接槽位")
                    self._refill(now)
                    head = bool(self._queue) and self._queue[0] is waiter
                    if head and self._can_start(host, now):
                        self._consume(host, now)
                        waiter.granted = True
                        self._queue.popleft()
                        self._cond.notify_all()
                        break
                    self._cond.wait(timeout=min(0.1, max(0.01, deadline - now)))
            except BaseException:
                waiter.cancelled = True
                self._drop_waiter(waiter)
                raise

    def release(self, host: str, lease: _Lease | None) -> None:
        with self._cond:
            self._inflight = max(0, self._inflight - 1)
            self._per_host[host] = max(0, self._per_host.get(host, 0) - 1)
            if lease is not None and lease.ok is not None:
                self._observe(lease.ok, lease.error)
            self._cond.notify_all()

    def _observe(self, ok: bool, error: str) -> None:
        now = time.monotonic()
        if not ok and throttle.detect(error):
            self._throttle_hits += 1
            self._ok_run = 0
            was = self._adaptive
            self._adaptive = 1
            self._degrade_until = now + self.s.cooldown_seconds
            self._next_allowed = max(self._next_allowed, now + self._gap())
            if was != 1:
                log.warning("网盘返回限流信号（%s），全局并发降到 1 路，冷却 %.0f 秒",
                            throttle.detect(error), self.s.cooldown_seconds)
            return
        if not ok:
            return
        self._ok_run += 1
        if self._adaptive and now >= self._degrade_until:
            if self._ok_run >= self.s.recover_every:
                self._ok_run = 0
                self._adaptive = min(self.s.max_inflight, self._adaptive + 1)
                if self._adaptive >= self.s.max_inflight:
                    self._adaptive = 0
                    log.info("网盘连接恢复正常，并发回到 %d 路", self.s.max_inflight)
                else:
                    log.info("网盘连接稳定，并发回升到 %d 路", self._adaptive)

    def reset(self) -> None:
        """任务开始时清掉降级状态（不动累计计数，方便排查）。"""
        with self._cond:
            self._degrade_until = 0.0
            self._adaptive = 0
            self._ok_run = 0
            self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def status(self) -> dict:
        with self._cond:
            now = time.monotonic()
            return {
                "enabled": self.s.enabled,
                "inflight": self._inflight,
                "waiting": len(self._queue),
                "peak": self._peak,
                "max_inflight": self.s.max_inflight,
                "active_limit": self._limit(),
                "per_host_inflight": self.s.per_host_inflight,
                "per_host": dict(self._per_host),
                "rate_per_minute": self.s.rate_per_minute,
                "tokens": round(self._tokens, 2),
                "degraded": now < self._degrade_until and bool(self._adaptive),
                "cooldown_remaining": round(max(0.0, self._degrade_until - now), 1),
                "guard": throttle.GUARD.status(),
                "granted": self._granted,
                "deferred": self._deferred,
                "throttle_hits": self._throttle_hits,
            }

    @contextmanager
    def lease(self, source: str | Path, abort: threading.Event | None = None):
        """一次网盘请求的上下文。退出时一定归还槽位。"""
        host = host_of(source)
        if not self.s.enabled or not is_network(source) or _bypassed(host, self.s):
            yield _Passthrough()
            return
        self.acquire(host, abort)
        ticket = _Lease(host)
        try:
            yield ticket
        finally:
            self.release(host, ticket)


# ----------------------------------------------------------------------
# 直链台账
# ----------------------------------------------------------------------

class LinkRegistry:
    """strm → 115 直链的持久化映射，只做本地 IO，不主动打网盘。

    「保持已有连接有效」的具体含义：
    * 直链一旦登记就**粘住**，批量任务涌入、TTL 到期、重启进程都不会让它失效；
    * 只有 strm 文件内容变了（size+mtime 签名）才重新解析；
    * 请求失败只改台账状态，不删连接；标记 ``stale`` 后仍按原链接复用；
    * 只有明确配了刷新钩子、且刷新预算还有余量时，才会为失效链接发一次请求。
    """

    def __init__(self, store, settings: Settings):
        self.store = store
        self.s = settings
        self._refresh_tokens = float(settings.verify_budget_per_minute)
        self._refresh_at = time.monotonic()
        self._budget_lock = threading.Lock()
        self._refreshes = 0
        self._budget_denied = 0

    def reconfigure(self, settings: Settings) -> None:
        self.s = settings

    def resolve(self, path: str | Path) -> str:
        p = Path(path)
        if p.suffix.lower() != ".strm":
            return str(path)          # 本地视频没有「连接」要维护
        key = str(path)
        signature = _signature(p)
        row = self.store.link_get(key)
        if row and row.get("url") and row.get("signature") == signature:
            if not self.s.sticky or row.get("state") in ("fresh", "stale"):
                return row["url"]
            if row.get("state") == "invalid":
                fresh = self._maybe_refresh(key, p, row, signature)
                if fresh:
                    return fresh
                return row["url"]      # 刷新没额度也不能丢掉已有连接
        url = _read_strm(p)
        self.store.link_upsert(key, url, host_of(url), signature,
                               self.s.link_ttl_minutes * 60)
        return url

    def note(self, path: str | Path, ok: bool, error: str = "") -> None:
        """一次真实请求的结果回写台账。``ok`` 只看请求本身成没成功。"""
        p = Path(path)
        if p.suffix.lower() != ".strm":
            return
        key = str(path)
        if ok:
            state = "fresh"
        elif throttle.detect(error):
            state = "stale"           # 403 也可能是并发掐的，不是链接过期
        elif _is_permanent(error):
            state = "invalid"
        else:
            state = "stale"
        try:
            self.store.link_mark(key, state, error,
                                 self.s.link_ttl_minutes * 60, ok)
        except Exception:  # noqa: BLE001 - 台账写失败不能拖垮刮削
            log.debug("直链台账回写失败：%s", key, exc_info=True)

    # ---------- 预算内刷新 ----------

    def _take_budget(self) -> bool:
        if not self.s.refresh_on_error or self.s.verify_budget_per_minute <= 0:
            return False
        with self._budget_lock:
            now = time.monotonic()
            rate = self.s.verify_budget_per_minute / 60.0
            self._refresh_tokens = min(float(self.s.verify_budget_per_minute),
                                       self._refresh_tokens
                                       + max(0.0, now - self._refresh_at) * rate)
            self._refresh_at = now
            if self._refresh_tokens < 1.0:
                self._budget_denied += 1
                return False
            self._refresh_tokens -= 1.0
            self._refreshes += 1
            return True

    def _maybe_refresh(self, key: str, path: Path, row: dict,
                       signature: str) -> str:
        """按预算换一次链；拿不到就返回空串，由调用方继续复用旧链接。

        这里**不再**重新读 strm 文件去和缓存的 URL 比对：文件变了会体现在
        ``signature`` 上，``resolve()`` 在进到这里之前就已经重新解析过了。
        若在这里再比一次，刷新钩子刚换来的新签名 URL 会在下一次失败时被
        误判成「文件里写的不是这个」，悄悄退回 strm 里那条已经过期的旧链。
        """
        if not self.s.refresh_url or not self._take_budget():
            return ""
        url = self.s.refresh_url.replace("{path}", urllib.parse.quote(key))
        url = url.replace("{url}", urllib.parse.quote(row["url"] or "", safe=""))
        headers = {"Accept": "application/json, text/plain"}
        if self.s.refresh_token:
            headers["Authorization"] = f"Bearer {self.s.refresh_token}"
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read(65536).decode("utf-8", "replace").strip()
            fresh = _parse_refresh(body)
            if not fresh:
                raise LinkError("刷新钩子没有返回可用的 URL")
        except (OSError, urllib.error.URLError, ValueError, LinkError) as exc:
            log.info("直链刷新失败（保留原连接）：%s（%s）", key, exc)
            return ""
        self.store.link_upsert(key, fresh, host_of(fresh), signature,
                               self.s.link_ttl_minutes * 60)
        log.info("直链已刷新：%s", key)
        return fresh

    def stats(self) -> dict:
        out = self.store.link_stats()
        out.update({
            "sticky": self.s.sticky,
            "refresh_hook": bool(self.s.refresh_url),
            "refresh_budget": self.s.verify_budget_per_minute,
            "refreshes": self._refreshes,
            "budget_denied": self._budget_denied,
        })
        return out


def _parse_refresh(body: str) -> str:
    """刷新钩子支持 ``{"url": "..."}`` 或直接一行 URL。"""
    if not body:
        return ""
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return ""
        candidate = str(data.get("url") or data.get("play_url") or "").strip()
    else:
        candidate = body.splitlines()[0].strip()
    return candidate if is_network(candidate) else ""


def _signature(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_size}:{round(st.st_mtime, 3)}"
    except OSError:
        return "0:0"


def _read_strm(path: Path) -> str:
    from . import probe  # 延迟导入：resolve_source_file 的实现留在 probe 里
    return probe.resolve_source_file(path)


# ----------------------------------------------------------------------
# 失败分类与退避
# ----------------------------------------------------------------------

def _is_permanent(text: str) -> bool:
    low = (text or "").lower()
    return any(token in low for token in _PERMANENT)


def classify(reason: str) -> str:
    """``throttle`` / ``permanent`` / ``transient``。

    限流类一定要退得更久：整批 403 之后如果按普通间隔重试，等于在冷却期
    结束时再造一次小高峰，正好是「放大请求量」的典型形态。
    """
    if not reason:
        return "transient"
    if throttle.detect(reason):
        return "throttle"
    if _is_permanent(reason):
        return "permanent"
    return "transient"


def retry_delay(attempt: int, kind: str, settings: Settings | None = None) -> float:
    """指数退避 + 抖动，返回秒；0 表示不再自动重试。

    抖动是**必须**的：一批任务在同一秒失败、退避时长又相同，就会在同一秒
    一起醒来重试，形成完全同步的重试风暴。
    """
    s = settings or SETTINGS
    if kind == "permanent":
        return 0.0
    base = s.retry_base_seconds * (3.0 if kind == "throttle" else 1.0)
    delay = min(s.retry_max_seconds, base * (2 ** max(0, int(attempt) - 1)))
    jitter = 1.0 + random.uniform(-s.retry_jitter, s.retry_jitter)
    value = delay * jitter
    # retry_max_seconds 是硬上限，抖动不能把它顶穿（先封顶再乘 1+jitter，
    # 配置 200 秒时可能返回 250 秒）。接近 / 到达上限时改成只向下抖：
    # 既守住上限，也不让一批老失败项都卡在同一个整数秒上一起醒来。
    if value > s.retry_max_seconds:
        value = s.retry_max_seconds * (1.0 - random.random() * s.retry_jitter)
    return round(max(1.0, value), 2)


# ----------------------------------------------------------------------
# 全局单例与装配入口
# ----------------------------------------------------------------------

SETTINGS = Settings()
GATE = RequestGate(SETTINGS)
_REGISTRY: LinkRegistry | None = None
_registry_store: Any = None
_config_lock = threading.Lock()


def configure(cfg, store=None) -> None:
    """在进程启动时（以及网页保存设置后）调用，热更新参数并挂上台账。"""
    global SETTINGS, _REGISTRY, _registry_store
    s = Settings.from_config(cfg)
    with _config_lock:
        SETTINGS = s
        GATE.reconfigure(s)
        if store is not None:
            # 同一个 state.db 就复用台账实例，别把刷新预算的计数清零
            # （否则反复保存设置就能绕过预算，又变成批量刷新）
            if _REGISTRY is None or _registry_store is not store:
                _REGISTRY = LinkRegistry(store, s)
                _registry_store = store
            else:
                _REGISTRY.reconfigure(s)
        elif _REGISTRY is not None:
            _REGISTRY.reconfigure(s)


def configured() -> bool:
    return _REGISTRY is not None


def lease(source: str | Path, abort: threading.Event | None = None):
    return GATE.lease(source, abort)


def resolve_source(path: str | Path) -> str:
    reg = _REGISTRY
    return reg.resolve(path) if reg is not None else _read_strm(path)


def note_result(path: str | Path, ok: bool, error: str = "") -> None:
    reg = _REGISTRY
    if reg is not None:
        reg.note(path, ok, error)


def retry_max_attempts() -> int:
    return SETTINGS.retry_max_attempts


def status() -> dict:
    return {
        "gate": GATE.status(),
        "registry": _REGISTRY.stats() if _REGISTRY else {"total": 0},
        "config": SETTINGS.public(),
    }
