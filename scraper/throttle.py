# -*- coding: utf-8 -*-
"""源站限流保护：撞到 429 / 503 / 504 就歇一会儿再继续。

网盘（115 之类的）是按**瞬时并发连接**掐人的，一旦被掐，之前那种
「失败了立刻换下一个文件继续撞」的做法会把账号越推越深。这里维护一个
全局退避点：抓帧 / 探测发现限流信号就记一笔，后面的任务先等到期再发。

退避时长按连续命中次数递增（5s → 15s → 45s → 135s → 300s 封顶），
中间成功了就逐步衰减回零，所以偶发一次只歇几秒，真被风控才会明显降速。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

# 第一次命中歇 5 秒，之后每次乘 3，最长 5 分钟
BASE_DELAY = 5.0
FACTOR = 3.0
MAX_DELAY = 300.0

# 连续命中多少次后开始打警告日志（前几次只记 info，避免刷屏）
LOUD_AFTER = 2

# 连续成功多少次才把「连续命中数」降一档
DECAY_EVERY = 5

# ffmpeg / ffprobe 报错里出现这些就当作限流信号。只认明确的限流状态码，
# 超时和 404 都不算——那些是链接本身的问题，退避也救不回来。
_PATTERNS = (
    "429",
    "too many requests",
    "503",
    "service unavailable",
    "504",
    "gateway timeout",
    "gateway time-out",
    # ffmpeg 对 5xx 只输出一句笼统的 "Server returned 5XX Server Error reply"，
    # 拿不到具体是 500 还是 503，所以整段 5xx 都按限流处理（多歇几秒，不亏）
    "5xx",
    "server returned 5",
)


def detect(text: str) -> str:
    """从报错文本里认出限流信号，认不出返回空串。"""
    low = (text or "").lower()
    for token in _PATTERNS:
        if token in low:
            return token
    return ""


class Throttle:
    """全局退避点。线程安全，抓帧线程和探测线程共用同一个实例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streak = 0
        self._pause_until = 0.0
        self._hits = 0
        self._success_run = 0
        self._total_paused = 0.0

    # ---------- 供抓帧 / 探测调用 ----------

    def report(self, text: str) -> bool:
        """报错文本里有限流信号就记一笔并开始退避，返回是否命中。"""
        token = detect(text)
        if not token:
            return False
        with self._lock:
            self._streak += 1
            delay = min(MAX_DELAY, BASE_DELAY * (FACTOR ** (self._streak - 1)))
            self._pause_until = max(self._pause_until, time.monotonic() + delay)
            self._hits += 1
            self._success_run = 0
            self._total_paused += delay
            streak = self._streak
        if streak >= LOUD_AFTER:
            log.warning("源站限流（命中 %s），暂停抓帧 %d 秒（连续第 %d 次）",
                        token, delay, streak)
        else:
            log.info("源站限流（命中 %s），暂停抓帧 %d 秒", token, delay)
        return True

    def ok(self) -> None:
        """一次成功。连续成功若干次才衰减一档。

        不逐次衰减：被限流时失败和成功会交替出现，逐次衰减会让连续命中数
        在 1~2 之间来回跳，退避永远升不上去。
        """
        with self._lock:
            self._success_run += 1
            if self._success_run < DECAY_EVERY or not self._streak:
                return
            self._success_run = 0
            self._streak -= 1

    # ---------- 供调度线程调用 ----------

    def wait(self, abort: threading.Event | None = None) -> bool:
        """退避中就在这里睡，返回 False 表示等待期间被暂停 / 取消了。"""
        while True:
            with self._lock:
                remain = self._pause_until - time.monotonic()
            if remain <= 0:
                return True
            if abort is not None and abort.is_set():
                return False
            # 分段睡，能让「暂停 / 取消」在 0.2 秒内生效
            time.sleep(min(remain, 0.2))

    def reset(self) -> None:
        with self._lock:
            self._streak = 0
            self._success_run = 0
            self._pause_until = 0.0

    def status(self) -> dict:
        with self._lock:
            remain = max(0.0, self._pause_until - time.monotonic())
            return {
                "active": remain > 0,
                "remaining": round(remain, 1),
                "streak": self._streak,
                "hits": self._hits,
            }


# 全进程共用一个
GUARD = Throttle()
