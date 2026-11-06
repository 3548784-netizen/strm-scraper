# -*- coding: utf-8 -*-
"""「勾选大量文件 → 开始任务」的性能与语义验收（纯本地，不用起服务）。

复现的是 NAS 上的真实卡顿：勾 700 个文件点开始，接口里逐条查库、逐条
探测产物、逐条 commit，后台扫描又重复一遍，加起来上千次刷盘。

覆盖：
  1. 新扫描逻辑的 commit 次数是常数级，并与「改动前的逐条版本」对照
  2. 模拟慢盘（每次 commit 睡 1ms）量出准备阶段耗时差距
  3. 两条路径最终的状态库内容逐字段一致（行为没被改坏）
  4. 重复触发、整库增量扫描同样是常数级提交，已有产物始终不覆盖
  5. 批量 API 与逐条 API 语义一致（reset / skipped / running / ok+签名）
  6. 媒体服务器索引短时复用：零散刷新不再反复分页拉全库，回查仍取新鲜数据

用法：
    python tools/check_scan_perf.py
    python tools/check_scan_perf.py --count 733 --delay 0.002
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.media import build_target, split_by_products  # noqa: E402
from scraper.mediaserver import (INDEX_REUSE_MAX, MediaServerClient,  # noqa: E402
                                 PAGE_SIZE)
from scraper.state import StateStore  # noqa: E402
from scraper.worker import Runner  # noqa: E402

FAIL: list[str] = []
PASS = 0
FLAGS = (True, True, True, True)  # nfo / poster / fanart / thumb 全开


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


class CountingConn:
    """包一层连接：数 commit 次数，并可模拟 NAS 上每次刷盘的等待。"""

    def __init__(self, inner, delay: float = 0.0):
        self._inner = inner
        self.commits = 0
        self.delay = delay

    def commit(self):
        self.commits += 1
        if self.delay:
            time.sleep(self.delay)
        return self._inner.commit()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def make_store(db: Path, delay: float = 0.0):
    store = StateStore(db)
    counter = CountingConn(store._conn, delay)
    store._conn = counter
    return store, counter


def make_cfg(root: Path):
    return SimpleNamespace(
        scrape=SimpleNamespace(
            write_nfo=True, write_poster=True, write_fanart=True,
            thumb=SimpleNamespace(enabled=True),
        ),
        scan=SimpleNamespace(
            roots=[str(root)], extensions=[".strm"], recursive=True,
            concurrency=2, timeout=60,
        ),
    )


def build_fixture(root: Path, count: int):
    """造 N 个 strm，每 3 个带齐全产物（模拟「记录删了但产物还在」）。"""
    root.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    ready: list[str] = []
    for i in range(count):
        source = root / f"测试影片{i:04d}.strm"
        source.write_text("http://127.0.0.1:1/not-used", encoding="utf-8")
        path = str(source)
        paths.append(path)
        if i % 3 == 0:
            target = build_target(source)
            for art in (target.nfo, target.poster, target.fanart, target.thumb):
                art.write_bytes(b"art-" + str(i).encode())
            ready.append(path)
    return paths, ready


def fingerprint(paths: list[str]) -> dict[str, bytes]:
    out = {}
    for path in paths:
        target = build_target(Path(path))
        for art in (target.nfo, target.poster, target.fanart, target.thumb):
            out[str(art)] = art.read_bytes() if art.is_file() else b""
    return out


def rows_of(paths: list[str]):
    rows = []
    for path in paths:
        info = Path(path).stat()
        rows.append((path, info.st_size, round(info.st_mtime, 3)))
    return rows


def dump(store: StateStore, paths: list[str]) -> list[tuple]:
    items = store.get_many(paths)
    return [(p, items[p].status, items[p].reason, items[p].size,
             items[p].mtime, items[p].attempts) for p in paths if p in items]


def legacy_scan(store: StateStore, paths: list[str]) -> int:
    """改动前 _scan(only=...) 的逐条版本，用来做提交次数/耗时对照。"""
    rows = rows_of(paths)
    before = store.get_many(paths)
    store.ensure(rows)
    fresh = [p for p in paths if p not in before]
    ready, _missing = split_by_products(fresh, *FLAGS)
    for path in ready:
        store.mark_skipped(path, "产物齐全，跳过")
    reused = set(ready)
    for path in paths:
        if path not in reused:
            store.reset_one(path)
    return len(rows)


def e2e_bulk_submit(base: str, count: int) -> None:
    """真实接口验收：一次提交 N 个产物齐全的文件，应全部跳过且很快返回。

    这条走的是 ``/api/files/scrape``，正好覆盖单元验收碰不到的 HTTP 路径
    （并发探测 + 批量登记 + 批量写状态库）。产物齐全所以不会启动刮削。
    """
    import json
    import shutil
    import urllib.request

    base = base.rstrip("/")
    # 本地验收不走系统代理，免得 127.0.0.1 被代理层挡掉
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path: str) -> dict:
        with opener.open(base + path, timeout=60) as r:
            return json.load(r)

    def post(path: str, data: dict) -> dict:
        req = urllib.request.Request(
            base + path, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with opener.open(req, timeout=120) as r:
            return json.load(r)

    root = Path(get("/api/config")["scan"]["roots"][0])
    directory = root / "__perf__"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    try:
        paths = []
        for i in range(count):
            source = directory / f"批量{i:04d}.strm"
            source.write_text("http://127.0.0.1:1/never.mp4\n", encoding="utf-8")
            (directory / f"{source.stem}.nfo").write_text("<movie/>", encoding="utf-8")
            for kind in ("poster", "fanart", "thumb"):
                (directory / f"{source.stem}-{kind}.jpg").write_bytes(b"art")
            paths.append(str(source))

        t0 = time.perf_counter()
        reply = post("/api/files/scrape", {"paths": paths, "now": False})
        elapsed = time.perf_counter() - t0
        print(f"     真实接口提交 {count} 个文件：{elapsed:.2f}s，{reply}")
        check("大批量提交接口正常返回", reply.get("ok") is True, str(reply))
        check("产物齐全的全部跳过、不启动刮削",
              reply.get("skipped") == count and reply.get("started") is False,
              str(reply))
        check("大批量提交在数秒内完成", elapsed < 15.0, f"{elapsed:.2f}s")

        # 清掉这轮写进任务列表的记录，别把测试文件留给用户
        items = get("/api/items?limit=5000").get("items", [])
        wanted = [it["path"] for it in items
                  if Path(it["path"]).parent == directory]
        if wanted:
            post("/api/items/delete", {"paths": wanted, "purge": False})
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300, help="模拟勾选的文件数")
    ap.add_argument("--delay", type=float, default=0.001,
                    help="模拟每次 commit 的刷盘耗时（秒）")
    ap.add_argument("--base", default="",
                    help="可选：额外跑一次真实接口的大批量提交（如 http://127.0.0.1:6693）")
    args = ap.parse_args()
    count = max(30, args.count)

    if args.base:
        e2e_bulk_submit(args.base, count)

    with tempfile.TemporaryDirectory(prefix="ws-perf-") as tmp:
        base = Path(tmp)
        paths, ready = build_fixture(base / "media", count)
        cfg = make_cfg(base / "media")
        before_art = fingerprint(paths)

        print(f"\n== 扫描准备：{count} 个勾选文件（{len(ready)} 个已有产物） ==")
        # --- 新逻辑 ---
        new_store, new_conn = make_store(base / "new.db", args.delay)
        runner = Runner(cfg, new_store)
        new_conn.commits = 0
        t0 = time.perf_counter()
        added = runner._scan(False, only=set(paths))
        new_commits, new_elapsed = new_conn.commits, time.perf_counter() - t0
        check("勾选文件全部登记", added == count, f"added={added}")

        # --- 改动前的逐条逻辑（同样条件、同样模拟刷盘） ---
        old_store, old_conn = make_store(base / "old.db", args.delay)
        old_conn.commits = 0
        t0 = time.perf_counter()
        legacy_scan(old_store, paths)
        old_commits, old_elapsed = old_conn.commits, time.perf_counter() - t0

        print(f"     新逻辑 {new_commits} 次提交 / {new_elapsed:.3f}s，"
              f"逐条版本 {old_commits} 次提交 / {old_elapsed:.3f}s")
        check("新逻辑提交次数为常数级", new_commits <= 6, f"{new_commits} 次")
        check("逐条版本确实一文件一提交", old_commits >= count // 2,
              f"{old_commits} 次")
        check("提交次数下降一个数量级以上",
              new_commits * 10 <= old_commits, f"{new_commits} vs {old_commits}")
        check("模拟慢盘下准备阶段明显更快",
              new_elapsed < old_elapsed / 2,
              f"{new_elapsed:.3f}s vs {old_elapsed:.3f}s")
        check("新旧逻辑状态库逐字段一致",
              dump(new_store, paths) == dump(old_store, paths))

        states = {p: new_store.get(p) for p in paths}
        check("产物齐全且记录缺失的标为可复用",
              all(states[p].status == "skipped" for p in ready),
              f"ready={len(ready)}")
        check("缺产物的仍进入待处理队列",
              all(states[p].status == "pending" for p in paths if p not in set(ready)))
        check("已有产物一个字节都没动", fingerprint(paths) == before_art)

        print("\n== 重复触发与整库增量 ==")
        new_conn.commits = 0
        runner._scan(False, only=set(paths))
        check("重复触发仍是常数级提交", new_conn.commits <= 6,
              f"{new_conn.commits} 次")
        check("重复触发不覆盖已有产物", fingerprint(paths) == before_art)
        check("重复触发状态不漂移",
              all(new_store.get(p).status == states[p].status for p in paths))

        new_conn.commits = 0
        runner._scan(False, None)
        check("整库增量扫描也是常数级提交", new_conn.commits <= 8,
              f"{new_conn.commits} 次")
        check("整库增量没有把可复用文件重新入队",
              all(new_store.get(p).status == "skipped" for p in ready))
        check("整库增量不覆盖已有产物", fingerprint(paths) == before_art)

        print("\n== 批量 API 与逐条 API 语义一致 ==")
        a, ca = make_store(base / "api_a.db")
        b, cb = make_store(base / "api_b.db")
        sample = paths[:40]
        a.ensure(rows_of(sample))
        b.ensure(rows_of(sample))
        a.reset_many(sample[:10])
        for p in sample[10:20]:
            b.reset_one(p)
        a.mark_skipped_many(sample[20:30], "产物齐全，跳过")
        for p in sample[30:40]:
            b.mark_skipped(p, "产物齐全，跳过")
        # 两边分段做同一件事，最后逐类比对
        check("reset_many 与 reset_one 结果一致",
              [a.get(p).status for p in sample[10:20]]
              == [b.get(p).status for p in sample[10:20]] == ["pending"] * 10
              and all(a.get(p).status == "pending" for p in sample[:10]))
        check("mark_skipped_many 与 mark_skipped 结果一致",
              all(a.get(p).status == "skipped" and a.get(p).reason == "产物齐全，跳过"
                  for p in sample[20:30])
              and all(b.get(p).status == "skipped" for p in sample[30:40]))

        a.mark_running_many(sample[:5])
        for p in sample[5:10]:
            b.mark_running(p)
        check("mark_running_many 与逐条标记一致",
              all(a.get(p).status == "running" for p in sample[:5])
              and all(b.get(p).status == "running" for p in sample[5:10]))

        pa, pb = sample[0], sample[5]
        a.mark_ok(pa, "标题", 12.5, 1234, 99.5)
        b.mark_ok(pb, "标题", 12.5)
        b.touch_signature(pb, 1234, 99.5)
        ia, ib = a.get(pa), b.get(pb)
        check("mark_ok 合并签名与两次写入结果一致",
              (ia.status, ia.title, ia.duration, ia.size, ia.mtime, ia.attempts)
              == (ib.status, ib.title, ib.duration, ib.size, ib.mtime, ib.attempts))
        ca.delay = cb.delay = 0
        a.close()
        b.close()

        print("\n== 媒体服务器索引短时复用 ==")
        client = MediaServerClient("http://127.0.0.1:1", "dummy-key")
        calls = {"n": 0}

        def fake_request(path: str, method: str = "GET", body=None):
            calls["n"] += 1
            if method == "POST":
                return {}
            # 第一页满、第二页不满，模拟分页结束
            start = int(path.split("StartIndex=")[-1].split("&")[0]) if "StartIndex=" in path else 0
            total = PAGE_SIZE + 3 if start == 0 else 3
            return {"Items": [{"Id": f"id{start + i}", "Path": f"/srv/f{i}.strm"}
                              for i in range(total)]}

        client._request = fake_request
        first = client.path_index(max_age=30.0)
        after_first = calls["n"]
        second = client.path_index(max_age=30.0)
        check("零散刷新第二次不再重复分页拉全库",
              calls["n"] == after_first, f"多打了 {calls['n'] - after_first} 次")
        check("复用返回的路径表一致", first == second)
        client.path_index(with_images=True)
        check("回查图片状态始终拉新鲜索引", calls["n"] > after_first)
        before_scan = calls["n"]
        client.scan_library()
        client.path_index(max_age=30.0)
        check("触发全库扫描后缓存失效", calls["n"] > before_scan + 1)

        # refresh_paths 的小批量复用开关（用能匹配到索引的路径）
        calls["n"] = 0
        client._index_cache = None
        client.refresh_paths(["/srv/f0.strm"])
        one_round = calls["n"]
        client.refresh_paths(["/srv/f0.strm"])
        check("连续单文件刷新只建一次索引",
              calls["n"] - one_round < one_round,
              f"{one_round} → 增量 {calls['n'] - one_round}")
        calls["n"] = 0
        client.refresh_paths(["/srv/not-there.strm"] * (INDEX_REUSE_MAX + 1))
        check("大批量刷新仍每次取新鲜索引", calls["n"] >= 2,
              f"{calls['n']} 次请求")

        new_store.close()
        old_store.close()

    print(f"\n结果：{PASS} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        for name in FAIL:
            print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
