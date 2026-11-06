# -*- coding: utf-8 -*-
"""批量刷新并发化 + 「用时」排除暂停 的专项验收（纯本地，秒级）。

背景：NAS 上批量刮完 733 个文件后，媒体服务器刷新是逐条串行 HTTP；
另一个问题是暂停一小时再继续，收尾日志会显示「用时 81.7 分钟」。

覆盖：
  1. 刷新请求确实并发，且并发数不超过上限（本地假 Emby 量耗时）
  2. 同一条目被多条路径命中只刷一次；统计口径与逐条版本一致
  3. 单条失败不影响其它条目；空列表 / missing / pause 限流语义保持
  4. 暂停时「用时」不涨；继续、取消后界面与收尾日志都用净耗时

用法：
    python tools/check_refresh_concurrency.py
"""
from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.mediaserver import (AUTH_STYLES, PAGE_SIZE, REFRESH_WORKERS,  # noqa: E402
                                 MediaServerClient, MediaServerError)
from scraper.state import StateStore  # noqa: E402
from scraper.worker import Runner  # noqa: E402

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


# ---------------------------------------------------------------- 桩客户端

def stub_client(index: dict, bad_ids: set[str] | None = None,
                delay: float = 0.0):
    """造一个不发网络的客户端，refresh_item 记录并发与调用顺序。"""
    client = MediaServerClient("http://127.0.0.1:1", "test-key",
                               kind="emby", timeout=5)
    client.path_index = lambda max_age=0.0, with_images=False: dict(index)
    state = {"running": 0, "max": 0, "calls": []}
    gate = threading.Lock()

    def refresh_item(item_id: str) -> None:
        with gate:
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
            state["calls"].append(item_id)
        try:
            if delay:
                time.sleep(delay)
            if bad_ids and item_id in bad_ids:
                raise MediaServerError("模拟服务端拒绝")
        finally:
            with gate:
                state["running"] -= 1

    client.refresh_item = refresh_item
    return client, state


# ---------------------------------------------------------------- 1. 并发与统计

def test_concurrency() -> None:
    print("\n== 1. 并发上限、去重与统计口径 ==")
    n = 40
    index = {f"/srv/f{i:03d}.strm": {"id": f"id{i}", "has_image": False}
             for i in range(n)}
    paths = list(index)
    client, state = stub_client(index, delay=0.01)
    started = time.monotonic()
    result = client.refresh_paths(paths)
    elapsed = time.monotonic() - started

    check("全部匹配并刷新", result["matched"] == n and result["refreshed"] == n,
          str(result))
    check("每条路径都有一次刷新调用", len(state["calls"]) == n,
          f"{len(state['calls'])} 次")
    check("确实并发执行", state["max"] > 1, f"最大并发 {state['max']}")
    check("并发不超过上限", state["max"] <= REFRESH_WORKERS,
          f"最大并发 {state['max']} > {REFRESH_WORKERS}")
    serial = n * 0.01
    check("批量耗时显著低于串行", elapsed < serial * 0.6,
          f"{elapsed:.3f}s vs 串行约 {serial:.3f}s")
    print(f"       实测：并发 {state['max']} 路，{elapsed:.3f}s "
          f"（逐条约 {serial:.3f}s）")

    # 同一条目被三条路径命中：只发一次 HTTP，但统计仍按路径算
    dup = {
        "/srv/同名.strm": {"id": "same", "has_image": False},
        "/srv/别名.strm": {"id": "same", "has_image": False},
        "/srv/另一部.strm": {"id": "other", "has_image": False},
    }
    client2, state2 = stub_client(dup)
    r2 = client2.refresh_paths(list(dup) + ["/srv/找不到.strm"])
    check("重复条目只发一次刷新请求", len(state2["calls"]) == 2,
          f"{state2['calls']}")
    check("去重后统计仍按输入路径计数",
          r2["matched"] == 3 and r2["refreshed"] == 3, str(r2))
    check("找不到的条目仍进 missing", len(r2["missing"]) == 1, str(r2))

    # 一条失败不拖累其它条目
    client3, state3 = stub_client(index, bad_ids={"id7"})
    r3 = client3.refresh_paths(paths)
    check("单条失败不影响其余刷新", r3["refreshed"] == n - 1, str(r3))
    check("失败被单独记录", len(r3["failed"]) == 1 and "id7" not in r3["failed"][0],
          str(r3["failed"]))
    check("失败条目也尝试过刷新", state3["calls"].count("id7") == 1)

    # 空列表：连索引都不该拉
    client4, state4 = stub_client(index)
    client4.path_index = lambda **kw: (_ for _ in ()).throw(
        AssertionError("空列表不该建索引"))
    r4 = client4.refresh_paths([])
    check("空列表直接返回，不建索引",
          r4 == {"matched": 0, "refreshed": 0, "missing": [], "failed": []}
          and not state4["calls"])

    # pause 仍是全局最小间隔，不会因为并发被绕过
    small = {f"/srv/p{i}.strm": {"id": f"p{i}", "has_image": False}
             for i in range(8)}
    client5, _s5 = stub_client(small)
    t0 = time.monotonic()
    client5.refresh_paths(list(small), pause=0.02)
    paced = time.monotonic() - t0
    check("pause 限流在并发下仍生效", paced >= 0.1,
          f"{paced:.3f}s < 7×0.02s")


# ---------------------------------------------------------------- 2. 真实 HTTP

class FakeEmby(BaseHTTPRequestHandler):
    """一页返回完所有条目；每条刷新故意慢 30ms，用来量并发收益。"""

    total = 24
    delay = 0.03
    stats = {"running": 0, "max": 0, "posts": 0}
    gate = threading.Lock()

    def log_message(self, *args) -> None:  # 静音
        return

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/Items":
            self._json({})
            return
        start = int(urllib.parse.parse_qs(parsed.query).get("StartIndex", ["0"])[0])
        rows = [{"Id": f"id{i}", "Path": f"/srv/f{i:03d}.strm"}
                for i in range(start, min(self.total, start + PAGE_SIZE))]
        self._json({"Items": rows, "TotalRecordCount": self.total})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        with self.gate:
            self.stats["running"] += 1
            self.stats["max"] = max(self.stats["max"], self.stats["running"])
            self.stats["posts"] += 1
        try:
            time.sleep(self.delay)
        finally:
            with self.gate:
                self.stats["running"] -= 1
        self.send_response(204)
        self.end_headers()


def test_real_http() -> None:
    print("\n== 2. 真实 HTTP 链路（本地假 Emby） ==")
    FakeEmby.stats = {"running": 0, "max": 0, "posts": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        client = MediaServerClient(base, "test-key", kind="emby", timeout=10)
        # 预设鉴权方式，避免并发首包各试一种（与线上启动后已鉴权一致）
        client.auth_style = AUTH_STYLES[0]
        paths = [f"/srv/f{i:03d}.strm" for i in range(FakeEmby.total)]
        t0 = time.monotonic()
        result = client.refresh_paths(paths)
        elapsed = time.monotonic() - t0

        check("每条都发出了真实刷新请求",
              FakeEmby.stats["posts"] == FakeEmby.total,
              str(FakeEmby.stats))
        check("真实请求并发且受限",
              1 < FakeEmby.stats["max"] <= REFRESH_WORKERS,
              str(FakeEmby.stats))
        check("统计与真实请求数一致",
              result["matched"] == result["refreshed"] == FakeEmby.total,
              str(result))
        serial = FakeEmby.total * FakeEmby.delay
        check("真实链路耗时低于串行一半", elapsed < serial * 0.5,
              f"{elapsed:.3f}s vs 串行约 {serial:.3f}s")
        print(f"       实测：并发 {FakeEmby.stats['max']} 路，{elapsed:.3f}s "
              f"（逐条约 {serial:.3f}s）")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 3. 暂停耗时

def make_runner(db: Path, log_buf: io.StringIO, gate: threading.Event | None = None):
    cfg = SimpleNamespace(
        scrape=SimpleNamespace(write_nfo=True, write_poster=True,
                               write_fanart=True,
                               thumb=SimpleNamespace(enabled=True)),
        scan=SimpleNamespace(roots=[], extensions=[".strm"], recursive=True,
                             concurrency=2, timeout=60),
    )
    store = StateStore(db)
    runner = Runner(cfg, store)
    # 不真的扫描 / 刮削，只走 _run 的收尾与状态统计；gate 用来把这一轮
    # 停在「扫描中」，好让我们真的暂停一段时间再放它结束
    if gate is None:
        runner._scan = lambda full, only: 0
    else:
        runner._scan = lambda full, only: (gate.wait(5), 0)[1]
    runner._drain = lambda: []
    runner._refresh_servers = lambda paths: None
    logger = logging.getLogger("scraper.worker")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(log_buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return runner, store, logger, handler


def test_elapsed(tmp: Path) -> None:
    print("\n== 3. 「用时」排除暂停时长 ==")
    buf = io.StringIO()
    gate = threading.Event()
    runner, store, logger, handler = make_runner(tmp / "s1.db", buf, gate)
    try:
        check("启动前用时为 0", runner.snapshot()["elapsed"] == 0)
        runner.start()
        time.sleep(0.05)
        paused_ok = runner.pause()
        check("能正常暂停", paused_ok)
        time.sleep(0.35)
        during = runner.snapshot()["elapsed"]
        check("暂停期间用时不再增长", during < 0.2, f"elapsed={during}")
        time.sleep(0.2)
        check("继续等待后用时仍冻结", runner.snapshot()["elapsed"] <= during + 0.02,
              f"{during} → {runner.snapshot()['elapsed']}")
        runner.resume()
        gate.set()
        runner._thread.join(timeout=5)
        after = runner.snapshot()["elapsed"]
        check("继续后用时仍是净耗时", after < 0.2, f"elapsed={after}")
        time.sleep(0.1)
        check("任务结束后用时停在结束那一刻",
              runner.snapshot()["elapsed"] == after)
        check("收尾日志确实打出了用时", "本轮结束" in buf.getvalue()
              and "用时" in buf.getvalue(), buf.getvalue()[-120:])

        # 取消路径也要把暂停时长扣掉
        gate2 = threading.Event()
        runner2, store2, _l2, h2 = make_runner(tmp / "s2.db", buf, gate2)
        runner2.start()
        time.sleep(0.05)
        runner2.pause()
        time.sleep(0.25)
        runner2.cancel()
        gate2.set()
        runner2._thread.join(timeout=5)
        check("取消时也扣掉暂停时长", runner2.snapshot()["elapsed"] < 0.2,
              f"elapsed={runner2.snapshot()['elapsed']}")
        store2.close()
        logger.removeHandler(h2)

        # 净耗时函数被日志与界面共用：把它固定成 1 分钟，看日志是否同步
        original = Runner._net_elapsed
        Runner._net_elapsed = lambda self: 60.0
        try:
            runner3, store3, _l3, h3 = make_runner(tmp / "s3.db", buf)
            runner3.start()
            runner3._thread.join(timeout=5)
            check("收尾日志用的是净耗时口径",
                  "用时 1.0 分钟" in buf.getvalue(),
                  buf.getvalue()[-160:])
            store3.close()
            _l3.removeHandler(h3)
        finally:
            Runner._net_elapsed = original
    finally:
        store.close()
        logger.removeHandler(handler)


def main() -> int:
    test_concurrency()
    test_real_http()
    with tempfile.TemporaryDirectory(prefix="ws-elapsed-") as td:
        test_elapsed(Path(td))
    print(f"\n结果：{PASS} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        for name in FAIL:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
