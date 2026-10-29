# -*- coding: utf-8 -*-
"""取帧提速 + 限流退避的验收脚本（不需要启服务也能跑前 4 节）。

覆盖：
1. 限流信号识别：429 / 503 / 504 认得出，超时和 404 不算
2. 退避时长按 5 → 15 → 45 → 135 → 300 递增，封顶 5 分钟，成功后逐步衰减
3. wait() 会被暂停 / 取消立刻打断（不能睡死）
4. 采样点均匀取（5 个位置取 2 个 = 0.30 / 0.60，而不是 0.15 / 0.30）
5. 旧配置的默认值自动迁移，改过的值不动
6. 起服务时 /api/status 里能读到 throttle 状态

用法：
    .venv/Scripts/python.exe tools/check_speed.py            # 只跑离线部分
    .venv/Scripts/python.exe tools/check_speed.py --live     # 顺带查本地实例
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import frames, throttle          # noqa: E402
from scraper.config import Config             # noqa: E402

BASE = "http://127.0.0.1:6670"
passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def section(title: str) -> None:
    print(f"\n========== {title} ==========")


# ---------------------------------------------------------------- 1. 信号识别
section("1. 限流信号识别")

samples = {
    "HTTP error 429 Too Many Requests": True,
    "Server returned 503 Service Unavailable": True,
    "HTTP error 504 Gateway Timeout": True,
    "Gateway time-out": True,
    # ffmpeg 对 5xx 只给这一句，拿不到具体状态码，必须也认出来
    "Server returned 5XX Server Error reply": True,
    "Connection timed out": False,
    "Server returned 404 Not Found": False,
    "Invalid data found when processing input": False,
    "": False,
}
for text, expect in samples.items():
    got = bool(throttle.detect(text))
    check(f"识别 {text!r} → {expect}", got == expect, f"实际 {got}")

check("正常报错不会触发退避", throttle.detect("moov atom not found") == "")


# ------------------------------------------------------------ 2. 退避时长递增
section("2. 退避时长与衰减")

g = throttle.Throttle()
delays = []
for _ in range(6):
    g.report("HTTP error 503")
    delays.append(g.status()["remaining"])
check("连续命中时剩余时长递增", delays[0] < delays[1] < delays[2], str(delays[:3]))
check("首次约 5 秒", 4.0 <= delays[0] <= 6.0, str(delays[0]))
check("封顶不超过 300 秒 + 1", delays[-1] <= 301, str(delays[-1]))
check("status 报告 active", g.status()["active"] is True)

# 成功要连续 5 次才降一档
streak_before = g.status()["streak"]
for _ in range(4):
    g.ok()
check("连续 4 次成功还不降档", g.status()["streak"] == streak_before,
      str(g.status()["streak"]))
g.ok()
check("第 5 次成功降一档", g.status()["streak"] == streak_before - 1,
      str(g.status()["streak"]))

g2 = throttle.Throttle()
g2.report("429")
g2.reset()
check("reset 清空退避", g2.status()["active"] is False and g2.status()["streak"] == 0)


# -------------------------------------------------- 3. wait() 能被 abort 打断
section("3. 暂停能立刻打断退避等待")

g3 = throttle.Throttle()
g3.report("504")
abort = threading.Event()
threading.Timer(0.3, abort.set).start()
started = time.monotonic()
ok = g3.wait(abort)
spent = time.monotonic() - started
check("abort 后 wait 返回 False", ok is False, str(ok))
check("打断延迟 < 1 秒", spent < 1.0, f"{spent:.2f}s")

g4 = throttle.Throttle()
check("没有退避时 wait 立即返回 True", g4.wait(None) is True)


# -------------------------------------------------------- 4. 采样点均匀取
section("4. 采样点均匀取")

five = [0.15, 0.30, 0.45, 0.60, 0.75]
check("5 个位置取 2 个 = 两端 0.15 / 0.75（拉得最开）",
      frames.pick_positions(five, 2) == [0.15, 0.75],
      str(frames.pick_positions(five, 2)))
check("5 个位置取 3 个 = 0.15 / 0.45 / 0.75",
      frames.pick_positions(five, 3) == [0.15, 0.45, 0.75],
      str(frames.pick_positions(five, 3)))
check("取 1 个落在中间", frames.pick_positions(five, 1) == [0.45],
      str(frames.pick_positions(five, 1)))
check("要的比给的多就全给", frames.pick_positions(five, 8) == five)
check("不会再取到前 N 个（那样全挤在片头）", frames.pick_positions(five, 2) != five[:2])


# ------------------------------------------------------------ 5. 配置迁移
section("5. 旧配置自动迁移")

old_yaml = """scan:
  roots: ['.']
  concurrency: 3
  timeout: 240
  probe_timeout: 20
scrape:
  frames: 5
  frame_positions: [0.15, 0.30, 0.45, 0.60, 0.75]
web:
  host: 127.0.0.1
  port: 6670
"""
tmp = Path(tempfile.mkdtemp())
(tmp / "config.yaml").write_text(old_yaml, encoding="utf-8")
cfg = Config.load(tmp / "config.yaml")
check("frames 5 → 2", cfg.scrape.frames == 2, str(cfg.scrape.frames))
check("timeout 240 → 30", cfg.scan.timeout == 30, str(cfg.scan.timeout))
check("probe_timeout 20 → 8", cfg.scan.probe_timeout == 8,
      str(cfg.scan.probe_timeout))
check("并发保持 3（不提高）", cfg.scan.concurrency == 3, str(cfg.scan.concurrency))
check("迁移有说明日志", len(cfg.migration_notes) == 3, str(cfg.migration_notes))

keep_yaml = old_yaml.replace("timeout: 240", "timeout: 90") \
                     .replace("probe_timeout: 20", "probe_timeout: 15") \
                     .replace("frames: 5", "frames: 4")
(tmp / "config.yaml").write_text(keep_yaml, encoding="utf-8")
cfg2 = Config.load(tmp / "config.yaml")
check("自己调过的值不动", (cfg2.scan.timeout, cfg2.scan.probe_timeout,
                          cfg2.scrape.frames) == (90, 15, 4),
      f"{cfg2.scan.timeout}/{cfg2.scan.probe_timeout}/{cfg2.scrape.frames}")
check("没迁移就没有说明", not cfg2.migration_notes, str(cfg2.migration_notes))

empty = Config.load(tmp / "nope.yaml")
check("全新配置直接用新默认值",
      (empty.scrape.frames, empty.scan.timeout, empty.scan.probe_timeout) == (2, 30, 8),
      f"{empty.scrape.frames}/{empty.scan.timeout}/{empty.scan.probe_timeout}")


# ------------------------------------------------------------ 6. 在线检查
if "--live" in sys.argv:
    section("6. 运行中的实例")
    try:
        data = json.loads(urllib.request.urlopen(BASE + "/api/status", timeout=10).read())
    except Exception as exc:  # noqa: BLE001
        check("能连上本地实例", False, str(exc))
    else:
        check("能连上本地实例", True)
        snap = data.get("runner") or data
        check("/api/status 带 throttle 字段", "throttle" in snap, str(list(snap))[:120])
        th = snap.get("throttle") or {}
        check("throttle 结构完整",
              {"active", "remaining", "streak", "hits"} <= set(th), str(th))
        cfg_live = json.loads(urllib.request.urlopen(BASE + "/api/settings", timeout=10).read())
        want = cfg_live["scan"]["concurrency"]
        check("并发没被改（跟配置一致）", snap.get("concurrency") == want,
              f"status {snap.get('concurrency')} vs 配置 {want}")
        check("配置里的采样帧数已生效", cfg_live["scrape"].get("frames") in (1, 2, 3, 4, 5),
              str(cfg_live["scrape"].get("frames")))
        check("限流命中数为整数", isinstance(th.get("hits"), int), str(th))


print(f"\n===== {passed} 项通过，{failed} 项失败 =====")
sys.exit(1 if failed else 0)
