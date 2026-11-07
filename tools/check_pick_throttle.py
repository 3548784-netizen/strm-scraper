# -*- coding: utf-8 -*-
"""选帧「源站限流导致整排抓不下来」修复的离线验收。

私有直链 / 115 转直链服务按瞬时并发连接数掐人：单发一个请求 200，一开
十几路就整批 403。这套验收覆盖修复的三层：

  1. 403 / Forbidden 被识别成限流信号（原来只认 429 / 5xx）；
  2. 按来源的并发闸在连续失败后自动降到单路串行，并能在稳定后回升；
  3. 被限流时主帧不再退化成「从头解码定位」白等一个完整超时；
  4. 前端首波失败会自动补一次，而不是直接留一排「点击重试」。

纯离线，不连服务、不动真实素材：
    python tools/check_pick_throttle.py

退出码非 0 表示有失败项。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import frames, picker, probe, throttle  # noqa: E402
from scraper.config import Config  # noqa: E402

FAIL: list[str] = []
PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


HOST = "47.95.254.240:5211"


def throttle_checks() -> None:
    print("\n---------- 1. 限流信号识别（403 家族） ----------")
    throttle.GUARD.reset()
    check("403 Forbidden 被认成限流",
          throttle.detect("Server returned 403 Forbidden (access denied)") != "")
    check("只有 access denied 也认得出来",
          throttle.detect("Error opening input: access denied") != "")
    check("429 / 503 老规则没被挤掉",
          throttle.detect("Too Many Requests 429") != ""
          and throttle.detect("Server returned 503 Service Unavailable") != "")
    check("404 / 超时不误判成限流",
          throttle.detect("Server returned 404 Not Found") == ""
          and throttle.detect("抓帧超时 30s") == "")
    before = throttle.GUARD.status()["hits"]
    hit = throttle.GUARD.report("Error opening input: Server returned 403 Forbidden")
    check("report 403 会记一笔并开始退避",
          hit and throttle.GUARD.status()["hits"] == before + 1
          and throttle.GUARD.status()["remaining"] > 0,
          str(throttle.GUARD.status()))
    throttle.GUARD.reset()


def gate_checks() -> None:
    print("\n---------- 2. 按来源的并发闸：默认 3 路，撞墙降到 1 路 ----------")
    key = picker.SourceGate.host(f"http://{HOST}/api/strm/play/1/token")
    check("host 解析保留端口", key == HOST, key)
    check("file:// 来源按 scheme 归类，不会误当成同一个远程主机",
          picker.SourceGate.host("file:///C:/media/clip.mp4") == "file",
          picker.SourceGate.host("file:///C:/media/clip.mp4"))

    gate = picker.SourceGate()
    abort = threading.Event()
    check("宽松源默认放 3 路", gate.limit(key) == 3)
    check("前 3 路都能立刻拿到槽位",
          all(gate.acquire(key, abort) for _ in range(3)))

    # 第 4 路必须排队；放掉一路后它才能进来
    entered = threading.Event()

    def fourth() -> None:
        if gate.acquire(key, abort):
            entered.set()
            gate.release(key, True)

    th = threading.Thread(target=fourth, daemon=True)
    th.start()
    time.sleep(0.15)
    check("第 4 路在排队，没有硬闯", not entered.is_set())
    gate.release(key, True)
    th.join(timeout=2)
    check("槽位归还后排队请求立刻进入", entered.is_set())
    gate.release(key, True)
    gate.release(key, True)

    # 模拟第一批并发整批 403
    gate.release(key, False)
    gate.release(key, False)
    check("短时间内连续两次失败 → 降为单路串行", gate.limit(key) == 1,
          str(gate.limit(key)))

    peak = 0
    peak_lock = threading.Lock()

    def worker() -> None:
        nonlocal peak
        if not gate.acquire(key, abort):
            return
        with peak_lock:
            peak = max(peak, gate._inflight[key])
        time.sleep(0.02)
        gate.release(key, True)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)
    check("降级后同一时刻只有 1 路抓帧", peak == 1, f"peak={peak}")
    check("冷却期内不会刚成功就回升", gate.limit(key) == 1,
          str(gate.limit(key)))

    # 直接把冷却点拨到过去，模拟「稳定了一分钟之后」
    gate._until[key] = 0.0
    for _ in range(9):
        gate.release(key, True)
    check("冷却结束且连续成功后回升一档", gate.limit(key) == 2,
          str(gate.limit(key)))

    cancel = threading.Event()
    cancel.set()
    check("请求已取消时 acquire 直接放弃", gate.acquire(key, cancel) is False)
    g2 = picker.SourceGate()
    g2.acquire(key, None)
    g2.release(key, None)
    check("客户端取消只还槽位、不算失败不降级", g2.limit(key) == 3)


def capture_at_checks() -> None:
    print("\n---------- 3. 被限流时不再退化慢定位（不再白等 30s） ----------")
    original = frames.capture
    calls: list[bool] = []
    out = Path(tempfile.gettempdir()) / "_pick_throttle_test.jpg"

    def rejected(source, seconds, output, timeout=60, fast_seek=True,
                 width=0, min_bytes=None, abort=None):
        calls.append(fast_seek)
        throttle.GUARD.report("Server returned 403 Forbidden (access denied)")
        return False

    frames.capture = rejected
    try:
        throttle.GUARD.reset()
        ok = frames.capture_at("http://x/clip.mp4", 300.0, out, timeout=5)
        check("403 失败后只跑了一次 ffmpeg",
              ok is False and calls == [True], str(calls))
    finally:
        frames.capture = original
        throttle.GUARD.reset()

    calls.clear()

    def broken(source, seconds, output, timeout=60, fast_seek=True,
               width=0, min_bytes=None, abort=None):
        calls.append(fast_seek)
        return False   # 普通解码失败，没有限流信号

    frames.capture = broken
    try:
        ok = frames.capture_at("http://x/clip.mp4", 300.0, out, timeout=5)
        check("非限流的定位失败仍会按老逻辑退化重试",
              ok is False and calls == [True, False], str(calls))
    finally:
        frames.capture = original
        throttle.GUARD.reset()


def picker_checks() -> None:
    print("\n---------- 4. FramePicker 集成：首批整批 403 后闸降为 1 ----------")
    tmp = Path(tempfile.mkdtemp(prefix="pickthrottle_"))
    src = tmp / "clip.mp4"
    src.write_bytes(b"fake video")
    pk = picker.FramePicker(Config(), tmp / "cache")

    original_resolve = probe.resolve_source
    original_capture = frames.capture

    def fake_resolve(_p):
        return f"http://{HOST}/api/strm/play/1/token"

    def rejected(source, seconds, output, timeout=60, fast_seek=True,
                 width=0, min_bytes=None, abort=None):
        throttle.GUARD.report("Server returned 403 Forbidden (access denied)")
        return False

    probe.resolve_source = fake_resolve
    frames.capture = rejected
    try:
        throttle.GUARD.reset()
        errors = []
        for t in (1.0, 2.0):
            try:
                pk.frame(src, t, 260, fast=True)
            except frames.FrameError as exc:
                errors.append(str(exc))
        key = picker.SourceGate.host(f"http://{HOST}/x")
        check("picker 连续失败后并发闸降到 1", pk._gate.limit(key) == 1,
              str(pk._gate.limit(key)))
        check("限流失败对外仍是明确的「抓不下来」",
              len(errors) == 2 and all("抓不下来" in e for e in errors),
              str(errors))
    finally:
        probe.resolve_source = original_resolve
        frames.capture = original_capture
        throttle.GUARD.reset()


def frontend_checks() -> None:
    print("\n---------- 5. 前端自愈与面板尺寸 ----------")
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check("单张超时放宽到 45s（服务端排队不误判）",
          "const STRIP_TIMEOUT = 45000" in src)
    check("缩略图首波失败自动补一次",
          "STRIP_RETRY_DELAY" in src and "loadOne(im, seq, attempt + 1)" in src)
    check("主帧首波失败自动补一次",
          "showFrame(attempt + 1)" in src and "源站忙，正在重试" in src)
    check("手动点重试仍然保留", "function retryStripImage(" in src)
    check("面板改回居中卡片、四周留遮罩",
          "width: min(1180px, 100%); height: min(84vh, 820px)" in src
          and "place-items: stretch" not in src)


def main() -> int:
    throttle_checks()
    gate_checks()
    capture_at_checks()
    picker_checks()
    frontend_checks()
    print(f"\n结果：{PASS} 项通过，{len(FAIL)} 项失败")
    for name in FAIL:
        print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
