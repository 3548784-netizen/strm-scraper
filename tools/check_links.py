# -*- coding: utf-8 -*-
"""115 连接台账 / 全局闸门 / 批量节奏的离线验收。

不连网盘、不启动服务、不动真实素材。模拟的是「短时间涌入几百个 strm」时
最要紧的几件事：任务不丢、并发与速率不越界、已有连接不失效、403 之后
不放大请求量、失败能恢复且不会重放失败高峰。

    python tools/check_links.py

退出码非 0 表示有失败项。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 刷新钩子用假的 urlopen，但保险起见关掉本机代理拦截
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from scraper import frames, links, probe, throttle, worker  # noqa: E402
from scraper.config import Config  # noqa: E402
from scraper.state import StateStore  # noqa: E402

FAIL: list[str] = []
PASS = 0
SKIP = 0
URL = "http://115.example.com/play/abc?token=SECRET"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


def tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="check_links_"))


def new_store() -> StateStore:
    return StateStore(tmp_dir() / "state.db")


def settings(**kw) -> links.Settings:
    base = dict(max_inflight=2, per_host_inflight=2, rate_per_minute=6000,
                burst=2, min_gap_ms=0, queue_max=0, acquire_timeout=3.0,
                cooldown_seconds=0.5, degrade_gap_ms=0, recover_every=4,
                bypass_hosts=("127.0.0.1", "localhost", "::1"),
                link_ttl_minutes=1, sticky=True, refresh_on_error=True,
                verify_budget_per_minute=6, batch_size=20,
                retry_base_seconds=1.0, retry_max_seconds=4.0,
                retry_jitter=0.3, retry_max_attempts=5)
    base.update(kw)
    s = links.Settings(**base)
    s.clamp()
    return s


# ----------------------------------------------------------------------

def config_checks() -> None:
    print("\n---------- 1. 配置与热更新 ----------")
    cfg = Config()
    check("新配置默认开启闸门", cfg.links.enabled is True)
    check("默认并发 / 速率是保守值",
          cfg.links.max_inflight == 2 and cfg.links.rate_per_minute == 60,
          f"{cfg.links.max_inflight}/{cfg.links.rate_per_minute}")
    check("完整配置能写回 yaml 且带 links 段",
          "links" in cfg.to_yaml_dict()
          and cfg.to_yaml_dict()["links"]["max_inflight"] == 2)
    check("示例配置有 links 段",
          "links:" in (ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    check("文档存在", (ROOT / "docs" / "115-links.md").is_file())

    store = new_store()
    cfg.links.max_inflight = 4
    cfg.links.rate_per_minute = 12
    links.configure(cfg, store)
    check("configure 后闸门参数立即生效",
          links.GATE.status()["max_inflight"] == 4
          and links.GATE.status()["rate_per_minute"] == 12,
          str(links.GATE.status()))
    cfg.links.max_inflight = 1
    links.configure(cfg, store)
    check("网页保存设置式的二次热更新不改 store",
          links.GATE.status()["max_inflight"] == 1
          and links.configured())
    check("热更新不会重置台账实例",
          links.status()["registry"]["total"] == 0)
    store.close()

    # 环境变量覆盖
    os.environ["WS_LINKS__MAX_INFLIGHT"] = "7"
    try:
        cfg2 = Config.load(tmp_dir() / "missing.yaml")
        check("WS_LINKS__MAX_INFLIGHT 能覆盖", cfg2.links.max_inflight == 7,
              str(cfg2.links.max_inflight))
    finally:
        os.environ.pop("WS_LINKS__MAX_INFLIGHT", None)

    # 写错不能被放大成无限并发
    bad = Config()
    bad.links.max_inflight = 9999
    bad.links.rate_per_minute = -5
    bad.links.burst = 999
    notes = bad.validate()
    check("非法节奏参数被收回范围",
          bad.links.max_inflight == 64 and bad.links.rate_per_minute == 0.1
          and bad.links.burst <= bad.links.max_inflight and bool(notes),
          f"{bad.links.max_inflight}/{bad.links.rate_per_minute}/{bad.links.burst}")


def address_checks() -> None:
    print("\n---------- 2. 地址归类与白名单 ----------")
    check("网络地址识别", links.is_network(URL) and links.is_network("rtsp://x/y"))
    check("本地路径不算网络",
          not links.is_network("D:/media/a.mkv")
          and links.host_of("D:/media/a.mkv") == "local")
    check("host 保留端口",
          links.host_of(URL) == "115.example.com"
          and links.host_of("http://47.95.254.240:5211/x") == "47.95.254.240:5211")
    s = settings()
    check("回环地址默认绕过", links._bypassed("127.0.0.1:6688", s)
          and links._bypassed("localhost", s))
    check("IPv6 回环地址也绕过",
          links._bypassed(links.host_of("http://[::1]:8080/x"), s),
          links.host_of("http://[::1]:8080/x"))
    check("网盘地址不绕过", not links._bypassed("115.example.com", s))
    g = links.RequestGate(s)
    with g.lease("http://127.0.0.1:6688/a.mp4") as ticket:
        check("绕过时是直通票、不占额度",
              isinstance(ticket, links._Passthrough)
              and g.status()["inflight"] == 0)
    check("本地文件也直通", g.status()["granted"] == 0)


def ledger_checks() -> None:
    print("\n---------- 3. 直链台账：粘性、重启、失效与刷新预算 ----------")
    store = new_store()
    s = settings()
    reg = links.LinkRegistry(store, s)
    d = tmp_dir()
    path = d / "电影.A.strm"
    path.write_text(URL + "\n", encoding="utf-8")

    url1 = reg.resolve(path)
    row = store.link_get(str(path))
    check("首次解析登记进台账", url1 == URL and row and row["state"] == "fresh",
          str(row))
    check("重复解析不新增记录、不产生网络请求",
          reg.resolve(path) == URL and store.link_stats()["total"] == 1)

    # 模拟重启：同一个库，全新台账实例
    reg2 = links.LinkRegistry(store, s)
    check("重启后直链保持有效", reg2.resolve(path) == URL
          and store.link_stats()["total"] == 1)

    # 删掉任务记录，台账不能跟着没
    store.ensure([(str(path), 1, 1.0)])
    store.delete([str(path)])
    check("删除任务记录不删连接", reg2.resolve(path) == URL
          and store.link_get(str(path)) is not None)

    # 过期只标记，不删、不换
    throttle.GUARD.reset()
    reg.note(path, False, "Server returned 403 Forbidden (access denied)")
    row = store.link_get(str(path))
    check("403 只标 stale，链接保留",
          row["state"] == "stale" and row["url"] == URL and row["fail_count"] == 1,
          str(row))
    check("stale 链接继续复用（不批量复验）", reg.resolve(path) == URL)

    reg.note(path, False, "地址返回 404，文件可能已被删除")
    check("404 标 invalid", store.link_get(str(path))["state"] == "invalid")
    check("invalid 且没钩子时仍保留原链接", reg.resolve(path) == URL)

    reg.note(path, True, "")
    check("成功后回到 fresh 并清零失败次数",
          store.link_get(str(path))["state"] == "fresh"
          and store.link_get(str(path))["fail_count"] == 0)

    # strm 内容变了才重新解析（纯本地，不发请求）
    new_url = URL.replace("SECRET", "NEWTOKEN")
    path.write_text(new_url + "\n", encoding="utf-8")
    os.utime(path, (time.time() + 2, time.time() + 2))
    check("strm 内容变化后重新解析", reg.resolve(path) == new_url,
          reg.resolve(path))
    check("重新解析不新增台账行", store.link_stats()["total"] == 1)

    # 刷新钩子：预算内成功换链
    calls = {"n": 0}
    real_urlopen = links.urllib.request.urlopen

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n):
            return b'{"url": "https://cdn.115.example/play?token=FRESH"}'

    def fake_urlopen(req, timeout=8):
        calls["n"] += 1
        return FakeResp()

    s_hook = settings(refresh_url="http://127.0.0.1:9999/refresh?path={path}",
                      verify_budget_per_minute=2)
    reg_hook = links.LinkRegistry(store, s_hook)
    store.link_mark(str(path), "invalid", "过期", 0, False)
    links.urllib.request.urlopen = fake_urlopen
    try:
        fresh = reg_hook.resolve(path)
        check("刷新钩子返回新直链", fresh.endswith("token=FRESH"), fresh)
        # 立刻再复验两次：预算只有 2，第 3 次不该再发请求
        store.link_mark(str(path), "invalid", "过期", 0, False)
        reg_hook.resolve(path)
        store.link_mark(str(path), "invalid", "过期", 0, False)
        reg_hook.resolve(path)
        check("刷新预算挡住批量复验", calls["n"] <= 2, str(calls))
        check("预算耗尽时旧连接仍可用",
              store.link_get(str(path))["url"] != "")
        check("被预算拒绝有计数", reg_hook.stats()["budget_denied"] >= 1,
              str(reg_hook.stats()))
        check("预算耗尽不会把钩子换来的新链退回 strm 旧链",
              store.link_get(str(path))["url"].endswith("token=FRESH"),
              store.link_get(str(path))["url"])
    finally:
        links.urllib.request.urlopen = real_urlopen
    store.close()
    throttle.GUARD.reset()


def gate_checks() -> None:
    print("\n---------- 4. 全局闸门：并发、FIFO、速率、取消、降级 ----------")
    throttle.GUARD.reset()
    s = settings(max_inflight=3, per_host_inflight=2, rate_per_minute=6000,
                 burst=3, cooldown_seconds=0.6, recover_every=4)
    g = links.RequestGate(s)
    lock = threading.Lock()
    peak = {"v": 0}
    order: list[int] = []

    def run(i: int, hold: float = 0.02):
        with g.lease(URL) as ticket:
            with lock:
                peak["v"] = max(peak["v"], g.status()["inflight"])
                order.append(i)
            time.sleep(hold)
            ticket.note(True)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(run, range(12)))
    check("全局并发不越界", peak["v"] <= 3, f"peak={peak['v']}")
    check("同域并发不越界", g.status()["peak"] <= 3 and
          max(g.status()["per_host"].values() or [0]) <= 2,
          str(g.status()["per_host"]))
    check("12 个请求全部放行、无遗漏", len(order) == 12, str(len(order)))
    check("槽位全部归还", g.status()["inflight"] == 0
          and g.status()["waiting"] == 0)

    # FIFO：先占住唯一槽位，再按顺序排队，放掉后应按排队顺序进入
    fifo_settings = settings(max_inflight=1, per_host_inflight=1,
                             rate_per_minute=6000, burst=1)
    fg = links.RequestGate(fifo_settings)
    fg.acquire("115.example.com", None)
    seq: list[int] = []
    aborts = [threading.Event() for _ in range(4)]

    def waiter(i: int):
        try:
            with fg.lease(URL, aborts[i]):
                seq.append(i)
        except links.LinkDeferred:
            pass

    threads = []
    for i in range(4):
        t = threading.Thread(target=waiter, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.04)
    time.sleep(0.1)
    check("排队期间状态可见", fg.status()["waiting"] == 4,
          str(fg.status()["waiting"]))
    fg.release("115.example.com", None)
    for t in threads:
        t.join(timeout=3)
    check("FIFO 按到达顺序放行", seq == [0, 1, 2, 3], str(seq))

    # 排队中被取消：不计失败、队列清空
    cg = links.RequestGate(fifo_settings)
    cg.acquire("115.example.com", None)
    abort = threading.Event()
    result = {"err": ""}

    def cancelled():
        try:
            with cg.lease(URL, abort):
                pass
        except links.LinkAborted as exc:
            result["err"] = type(exc).__name__

    t = threading.Thread(target=cancelled, daemon=True)
    t.start()
    time.sleep(0.08)
    abort.set()
    t.join(timeout=2)
    check("排队中取消抛 LinkAborted", result["err"] == "LinkAborted", str(result))
    check("取消后不残留排队者", cg.status()["waiting"] == 0)
    cg.release("115.example.com", None)

    # 队列上限：超出的任务退回，而不是硬闯
    qg = links.RequestGate(settings(max_inflight=1, per_host_inflight=1,
                                    queue_max=1, rate_per_minute=6000))
    qg.acquire("115.example.com", None)
    a1, a2 = threading.Event(), threading.Event()
    outcomes: list[str] = []

    def qwait(ev: threading.Event):
        try:
            with qg.lease(URL, ev):
                outcomes.append("ok")
        except links.LinkDeferred as exc:
            outcomes.append(type(exc).__name__)

    t1 = threading.Thread(target=qwait, args=(a1,), daemon=True)
    t1.start()
    time.sleep(0.08)
    t2 = threading.Thread(target=qwait, args=(a2,), daemon=True)
    t2.start()
    t2.join(timeout=2)
    check("队列满时新任务退回且不发请求",
          "LinkDeferred" in outcomes and qg.status()["deferred"] >= 1,
          str(outcomes))
    a1.set()
    t1.join(timeout=2)
    qg.release("115.example.com", None)

    # 速率：burst=1、每分钟 600 次（10/s），6 次至少要约 0.5 秒
    rg = links.RequestGate(settings(max_inflight=4, per_host_inflight=4,
                                    rate_per_minute=600, burst=1))
    stamps: list[float] = []

    def timed():
        with rg.lease(URL) as ticket:
            stamps.append(time.monotonic())
            ticket.note(True)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: timed(), range(6)))
    elapsed = time.monotonic() - start
    check("令牌桶把请求摊平（不瞬间全放）", elapsed >= 0.35,
          f"elapsed={elapsed:.2f}s")
    stamps.sort()
    check("相邻请求间隔受速率约束",
          all(stamps[i + 1] - stamps[i] >= 0.07 for i in range(len(stamps) - 1)),
          str([round(stamps[i + 1] - stamps[i], 3) for i in range(len(stamps) - 1)]))

    # 403 降级 + 退避期内一个都不放 + 连续成功后回升
    dg = links.RequestGate(settings(max_inflight=3, per_host_inflight=3,
                                    cooldown_seconds=0.8, recover_every=2,
                                    degrade_gap_ms=60))
    throttle.GUARD.reset()
    with dg.lease(URL) as ticket:
        ticket.note(False, "Server returned 403 Forbidden (access denied)")
    check("403 后自动降到单路",
          dg.status()["degraded"] and dg.status()["active_limit"] == 1,
          str(dg.status()))
    throttle.GUARD.report("Server returned 403 Forbidden")
    released = {"n": 0}

    def blocked():
        with dg.lease(URL) as ticket:
            released["n"] += 1
            ticket.note(True)

    bt = threading.Thread(target=blocked, daemon=True)
    bt.start()
    time.sleep(0.15)
    check("退避期内不放行任何请求", released["n"] == 0, str(released))
    throttle.GUARD.reset()
    bt.join(timeout=3)
    for _ in range(3):
        with dg.lease(URL) as ticket:
            ticket.note(True)
    check("冷却结束后连续成功才回升",
          dg.status()["active_limit"] >= 1, str(dg.status()))
    throttle.GUARD.reset()

    # 冷却结束 ≠ 立刻满速：必须按 recover_every 逐级回升。用独立闸门排除
    # 前面 blocked 线程的成功计数干扰。
    dg2 = links.RequestGate(settings(max_inflight=4, per_host_inflight=4,
                                     cooldown_seconds=0.2, recover_every=2,
                                     degrade_gap_ms=0, min_gap_ms=0))
    with dg2.lease(URL) as ticket:
        ticket.note(False, "403 Forbidden")
    time.sleep(0.3)
    check("冷却结束后仍停留在单路",
          dg2.status()["active_limit"] == 1, str(dg2.status()))
    for _ in range(2):
        with dg2.lease(URL) as ticket:
            ticket.note(True)
    check("连续成功 recover_every 次回升一档",
          dg2.status()["active_limit"] == 2, str(dg2.status()))
    throttle.GUARD.reset()


def retry_checks() -> None:
    print("\n---------- 5. 失败分类与退避（不放大请求量） ----------")
    s = settings(retry_base_seconds=10, retry_max_seconds=200, retry_jitter=0.25)
    check("403 归为限流", links.classify("地址返回 403，可能需要鉴权") == "throttle")
    check("429 / 503 归为限流",
          links.classify("Too Many Requests 429") == "throttle"
          and links.classify("Server returned 503") == "throttle")
    check("404 / 401 归为永久", links.classify("地址返回 404，文件可能已被删除") == "permanent"
          and links.classify("地址要求鉴权，检查登录状态") == "permanent")
    check("超时归为临时", links.classify("连接超时：xx 响应太慢") == "transient")
    check("永久错误不再自动重试", links.retry_delay(1, "permanent", s) == 0.0)
    d1 = links.retry_delay(1, "transient", s)
    d2 = links.retry_delay(2, "transient", s)
    d3 = links.retry_delay(3, "transient", s)
    check("退避按次数指数增长", d3 > d2 > d1 >= 7.5, f"{d1}/{d2}/{d3}")
    # 单次抽样比大小会被抖动区间重叠影响（限流下限 22.5s vs 普通上限
    # 12.5s×2=25s），改成比中位数，避免偶发误报。
    delays = [links.retry_delay(1, "transient", s) for _ in range(40)]
    throttles = [links.retry_delay(1, "throttle", s) for _ in range(40)]
    median = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
    check("限流类退得更久", median(throttles) > median(delays) * 2,
          f"{median(throttles)} vs {median(delays)}")
    check("抖动把同一批失败错开", len(set(delays)) >= 20,
          f"unique={len(set(delays))}")
    # 上限必须是硬约束：抽样多次，别让「这次没抽到上偏的抖动」蒙混过去
    capped = [links.retry_delay(20, "throttle", s) for _ in range(40)]
    check("退避有上限", max(capped) <= 200, f"max={max(capped)}")
    check("接近上限时仍有抖动", len(set(capped)) >= 20,
          f"unique={len(set(capped))}")


def state_checks() -> None:
    print("\n---------- 6. 持久队列：到期重试、次数上限、退回不算失败 ----------")
    store = new_store()
    store.ensure([(f"/m/{i}.strm", 1, 1.0) for i in range(6)])
    store.mark_running_many([f"/m/{i}.strm" for i in range(6)])
    store.mark_failed("/m/0.strm", "403", retry_after=0.0)     # 永久，不再自动重试
    store.mark_failed("/m/1.strm", "403", retry_after=0.05)    # 马上到期
    store.mark_failed("/m/2.strm", "timeout", retry_after=999)  # 还没到期
    check("失败项写入 next_retry",
          store.get("/m/1.strm").next_retry > 0
          and store.get("/m/0.strm").next_retry == 0)
    time.sleep(0.08)
    due = store.requeue_due(5)
    check("只有到期的失败项重新入队", due == 1, f"due={due}")
    check("未到期的仍在 failed",
          store.get("/m/2.strm").status == "failed")
    check("永久失败不会被自动重试",
          store.get("/m/0.strm").status == "failed")

    # 次数上限
    for _ in range(6):
        store.mark_failed("/m/3.strm", "timeout", retry_after=0.01)
    time.sleep(0.03)
    check("超过最大次数不再自动入队", store.requeue_due(5) == 0,
          f"attempts={store.get('/m/3.strm').attempts}")

    # 范围任务：退避恢复和等待计数都不能越过自己的文件范围
    store.mark_running("/m/5.strm")
    store.mark_failed("/m/5.strm", "timeout", retry_after=0.01)
    time.sleep(0.03)
    cnt = store.requeue_due(5, {"/m/5.strm"})
    check("范围内退避到期能被恢复", cnt == 1
          and store.get("/m/5.strm").status == "pending", f"cnt={cnt}")
    store.mark_running("/m/5.strm")
    store.mark_failed("/m/5.strm", "timeout", retry_after=0.01)
    time.sleep(0.03)
    check("范围外失败项不会被范围任务改动",
          store.requeue_due(5, {"/m/0.strm"}) == 0
          and store.get("/m/5.strm").status == "failed",
          f"status={store.get('/m/5.strm').status}")
    check("范围化统计只数范围内的等待项",
          store.retry_counts({"/m/5.strm"})["due"] == 1
          and store.retry_counts({"/m/0.strm"})["due"] == 0)
    store.requeue_due(5)   # 收尾清掉，别影响后面的断言

    # 退回队列不计失败次数
    before = store.get("/m/4.strm").attempts
    store.mark_running("/m/4.strm")
    store.defer_one("/m/4.strm", 0.05)
    check("退回队列 attempts 不变",
          store.get("/m/4.strm").attempts == before
          and store.get("/m/4.strm").status == "pending")
    check("未到期的 pending 不会被派发",
          all(i.path != "/m/4.strm" for i in store.next_pending(10)))
    time.sleep(1.05)   # defer_one 有 1 秒下限，避免任务原地打转
    check("到期后能被派发",
          any(i.path == "/m/4.strm" for i in store.next_pending(10)))
    check("stats 暴露等退避的数量",
          "retry_waiting" in store.stats() and "retry_due" in store.stats())
    check("批量放回只统计真实存在的路径",
          store.reset_many(["/m/不存在.strm"]) == 0
          and store.reset_many(["/m/1.strm"]) == 1)
    store.close()


def burst_checks() -> None:
    print("\n---------- 7. 突发 200 个 strm：不丢、不越界、连接保持 ----------")
    throttle.GUARD.reset()
    N = 200
    store = new_store()
    s = settings(max_inflight=3, per_host_inflight=2, rate_per_minute=6000,
                 burst=3, batch_size=6, cooldown_seconds=0.4,
                 retry_base_seconds=1.0, retry_max_attempts=3)
    # 用独立闸门，不碰全局
    gate = links.RequestGate(s)
    reg = links.LinkRegistry(store, s)
    d = tmp_dir()
    rows = []
    for i in range(N):
        p = d / f"burst_{i:04d}.strm"
        p.write_text(f"http://115.example.com/play/{i}?token=T{i}\n",
                     encoding="utf-8")
        rows.append((str(p), p.stat().st_size, round(p.stat().st_mtime, 3)))
    store.ensure(rows)
    check("200 个任务一次性入队（零网络请求）",
          store.count("pending") == N, str(store.stats()))

    req_count = {"n": 0}
    peak = {"v": 0}
    per_host_peak = {"v": 0}
    lock = threading.Lock()
    done: list[str] = []

    def handle(path: str) -> None:
        url = reg.resolve(Path(path))
        with gate.lease(url) as ticket:
            with lock:
                req_count["n"] += 1
                peak["v"] = max(peak["v"], gate.status()["inflight"])
                per_host_peak["v"] = max(
                    per_host_peak["v"],
                    max(gate.status()["per_host"].values() or [0]),
                )
            time.sleep(0.002)
            ticket.note(True)
        reg.note(Path(path), True, "")
        with lock:
            done.append(path)
        store.mark_ok(path, "ok", 1.0)

    workers = min(8, s.batch_size)
    dispatched = 0
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight = set()
        while time.monotonic() - start < 30:
            need = workers - len(inflight)
            if need:
                batch = store.next_pending(need)
                if batch:
                    store.mark_running_many(i.path for i in batch)
                    for item in batch:
                        inflight.add(pool.submit(handle, item.path))
                    dispatched += len(batch)
            if not inflight:
                if store.next_pending(1):
                    continue
                break
            finished = {f for f in inflight if f.done()}
            for f in finished:
                f.result()
            inflight -= finished
            time.sleep(0.01)
    check("200 个任务全部落到终态",
          store.count("pending") == 0 and store.count("running") == 0
          and store.count("ok") == N, str(store.stats()))
    check("没有重复派发", dispatched == N and len(set(done)) == N,
          f"dispatched={dispatched} done={len(done)}")
    check("请求数严格等于任务数（不放大）", req_count["n"] == N,
          str(req_count["n"]))
    check("全局并发峰值不越界", peak["v"] <= 3, f"peak={peak['v']}")
    check("同域并发峰值不越界", per_host_peak["v"] <= 2,
          f"peak={per_host_peak['v']}")
    check("台账登记了全部连接", store.link_stats()["total"] == N,
          str(store.link_stats()))
    sample = rows[0][0]
    check("突发之后已有连接仍有效",
          store.link_get(sample)["url"].endswith("token=T0")
          and reg.resolve(Path(sample)) == store.link_get(sample)["url"])
    # 重启模拟
    reg2 = links.LinkRegistry(store, s)
    check("重启后台账条数不变、直链不变",
          store.link_stats()["total"] == N
          and all(reg2.resolve(Path(p)) == store.link_get(p)["url"]
                  for p, _s, _m in rows[:10]))
    store.close()


def storm_checks() -> None:
    print("\n---------- 8. 整批 403：冷却、不重放失败高峰、可恢复 ----------")
    throttle.GUARD.reset()
    M = 60
    store = new_store()
    s = settings(retry_base_seconds=2.0, retry_max_seconds=30.0,
                 retry_jitter=0.3, retry_max_attempts=5,
                 cooldown_seconds=1.2, recover_every=4)
    links.SETTINGS = s          # retry_delay / retry_max_attempts 走全局
    gate = links.RequestGate(s)
    reg = links.LinkRegistry(store, s)
    d = tmp_dir()
    paths = []
    for i in range(M):
        p = d / f"bad_{i}.strm"
        p.write_text(f"http://115.example.com/play/bad{i}\n", encoding="utf-8")
        paths.append(str(p))
        store.ensure([(str(p), p.stat().st_size, 1.0)])
        store.mark_running(str(p))

    # 模拟整批撞 403
    t0 = time.time()
    for path in paths:
        url = reg.resolve(Path(path))
        with gate.lease(url) as ticket:
            ticket.note(False, "Server returned 403 Forbidden (access denied)")
        reg.note(Path(path), False, "Server returned 403 Forbidden")
        kind = links.classify("地址返回 403，可能需要鉴权或链接已过期")
        delay = links.retry_delay(1, kind, s)
        store.mark_failed(path, "地址返回 403，可能需要鉴权或链接已过期",
                          retry_after=delay)
    check("整批失败后闸门降级", gate.status()["degraded"]
          and gate.status()["active_limit"] == 1, str(gate.status()))
    check("失败任务一条不少地留在库里",
          store.count("failed") == M and store.count("pending") == 0,
          str(store.stats()))
    due_times = [round(store.get(p).next_retry, 2) for p in paths]
    check("重试时间被抖动打散，不会同时醒来",
          len(set(due_times)) >= M * 0.6, f"unique={len(set(due_times))}")
    check("限流类退避明显长于普通失败",
          min(due_times) - t0 > 3.0,
          f"写入时的最短退避={min(due_times) - t0:.1f}s")

    # 冷却期内再发起一批：全部排队，不硬闯
    throttle.GUARD.report("Server returned 403 Forbidden")
    sent = {"n": 0}

    def probe_one():
        with gate.lease(URL) as ticket:
            sent["n"] += 1
            ticket.note(True)

    t = threading.Thread(target=probe_one, daemon=True)
    t.start()
    time.sleep(0.2)
    check("退避期内新请求零放行", sent["n"] == 0, str(sent))
    throttle.GUARD.reset()
    t.join(timeout=3)

    # 到期才恢复：先不到期不许动，改到过去就能被 requeue_due 捡起
    check("未到期时没有失败项被重放", store.requeue_due(5) == 0)
    one = paths[0]
    with store._lock:
        store._conn.execute("UPDATE items SET next_retry=? WHERE path=?",
                            (time.time() - 1, one))
        store._conn.commit()
    check("到期项能被捡回队列", store.requeue_due(5) == 1
          and store.get(one).status == "pending")
    check("其余失败项仍在退避中", store.count("failed") == M - 1)
    check("403 没把台账里的链接删掉",
          all(store.link_get(p)["url"] for p in paths[:10]))
    store.close()
    throttle.GUARD.reset()


def wiring_checks() -> None:
    print("\n---------- 9. 集成接线（静态） ----------")
    probe_src = (ROOT / "scraper" / "probe.py").read_text(encoding="utf-8")
    frames_src = (ROOT / "scraper" / "frames.py").read_text(encoding="utf-8")
    worker_src = (ROOT / "scraper" / "worker.py").read_text(encoding="utf-8")
    web_src = (ROOT / "scraper" / "web.py").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check("ffprobe 过全局闸门", "with links.lease(source, abort) as ticket" in probe_src)
    check("ffmpeg 过全局闸门", "with links.lease(source, abort) as ticket" in frames_src)
    check("strm 解析走台账",
          "if links.configured():" in probe_src
          and "return links.resolve_source(path)" in probe_src)
    check("结果回写台账", "links.note_result(" in worker_src)
    check("批量调度用 batch_size 限在途", "links.SETTINGS.batch_size" in worker_src)
    check("失败项按退避到期才重放", "requeue_due(" in worker_src)
    check("排队退回与暂停分开处理",
          "except links.LinkDeferred" in worker_src
          and "except links.LinkAborted" in worker_src)
    for path in ("/api/links", "/api/links/list", "/api/links/retry",
                 "/api/links/refresh"):
        check(f"接口 {path} 已注册", path in web_src)
    check("设置页可读写 links", '"links": {' in web_src
          and 'raw_links = data.get("links")' in web_src)
    check("设置页有节奏参数卡", 'id="setLinksCard"' in html
          and 'id="setLinkInflight"' in html)
    check("设置提交带上 links", "links: {" in html
          and "max_inflight: Number($('setLinkInflight').value)" in html)
    check("状态接口带 links", 'snapshot["links"] = links.status()' in web_src
          or '"links": links.status()' in worker_src)
    check("main 启动时装配台账", "configure_links(cfg, store)" in
          (ROOT / "scraper" / "main.py").read_text(encoding="utf-8"))


def frame_abort_checks() -> None:
    print("\n---------- 10. 排队中断不误伤直链状态 ----------")
    throttle.GUARD.reset()
    store = new_store()
    s = settings(max_inflight=1, per_host_inflight=1, acquire_timeout=0.3,
                 rate_per_minute=6000)
    links.configure(Config(), store)
    links.GATE.reconfigure(s)
    d = tmp_dir()
    p = d / "queued.strm"
    p.write_text(URL + "\n", encoding="utf-8")
    url = probe.resolve_source(p)
    check("台账已登记", store.link_get(str(p)) is not None)
    # 占住唯一槽位
    links.GATE.acquire("115.example.com", None)
    aborted = {"v": False}
    abort = threading.Event()

    def waiter():
        try:
            with links.lease(url, abort):
                pass
        except links.LinkAborted:
            aborted["v"] = True

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.06)
    abort.set()
    t.join(timeout=2)
    links.GATE.release("115.example.com", None)
    check("排队中被取消抛 LinkAborted", aborted["v"])
    check("取消不改台账状态（连接保持有效）",
          store.link_get(str(p))["state"] == "fresh",
          str(store.link_get(str(p))["state"]))
    links.GATE.reconfigure(settings())
    store.close()
    throttle.GUARD.reset()


def main() -> int:
    config_checks()
    address_checks()
    ledger_checks()
    gate_checks()
    retry_checks()
    state_checks()
    burst_checks()
    storm_checks()
    wiring_checks()
    frame_abort_checks()
    print("\n" + "=" * 62)
    if FAIL:
        print(f"结果：{PASS} 项通过，{len(FAIL)} 项失败")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"结果：{PASS} 项通过，0 项失败" + (f"，{SKIP} 项跳过" if SKIP else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
