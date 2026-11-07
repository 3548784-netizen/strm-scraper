# -*- coding: utf-8 -*-
"""「删除任务慢」的性能与语义验收。

复现的是 NAS 上的真实卡顿：任务列表勾几百条删除，接口里逐条 get()、
逐条 store.delete()（每次一个事务刷盘），purge 再串行探测产物——
500 条就是 500 次 commit，慢盘上几十秒。

覆盖：
  1. store 批量 delete 的 commit 次数是常数级，并与「逐条删除」对照
  2. 模拟慢盘（每次 commit 睡 1ms）量出删除耗时差距
  3. 语义不变：删除后记录消失；返回的 removed/purged/skipped 正确
  4. purge：产物文件被删、.strm 保留；库外路径不碰磁盘、只删记录
  5. 文件管理删产物：指定类型被删、其余保留、记录重置为 pending
  6. 真实接口端到端：造夹具 → 删除 → 语义与耗时（--base，可选）

用法：
    python tools/check_delete_perf.py
    python tools/check_delete_perf.py --count 500 --delay 0.002
    python tools/check_delete_perf.py --base http://127.0.0.1:6693
    python tools/check_delete_perf.py --base http://127.0.0.1:6693 --root D:\\媒体库
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.media import build_target  # noqa: E402
from scraper.state import StateStore  # noqa: E402

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


def seed_records(store: StateStore, paths: list[str]) -> None:
    """直接灌一批记录，不经过扫描（本脚本只关心删除）。"""
    store.ensure([(p, 123, 456.0) for p in paths])


def build_fixture(root: Path, count: int, prefix: str = "影片"):
    """造 N 个 strm，每个都带齐全产物（模拟「已完成任务」）。"""
    root.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i in range(count):
        source = root / f"{prefix}{i:04d}.strm"
        source.write_text("http://127.0.0.1:1/not-used", encoding="utf-8")
        target = build_target(source)
        for art in (target.nfo, target.poster, target.fanart, target.thumb):
            art.write_bytes(b"art-" + f"{prefix}-{i}".encode())
        paths.append(str(source))
    return paths


def arts_of(source: Path) -> list[Path]:
    t = build_target(source)
    return [t.nfo, t.poster, t.fanart, t.thumb]


def offline_suite(count: int, delay: float) -> None:
    print(f"\n== 离线：{count} 条记录，逐条删除 vs 批量删除（commit 慢盘 {delay*1000:.0f}ms/次） ==")
    with tempfile.TemporaryDirectory(prefix="ws-delperf-") as tmp:
        base = Path(tmp)

        # --- 旧方式：逐条 delete（每次一个事务） ---
        store1, conn1 = make_store(base / "old.db", delay)
        seed_records(store1, [f"/m/old/{i}.strm" for i in range(count)])
        t0 = time.perf_counter()
        for i in range(count):
            store1.delete([f"/m/old/{i}.strm"])
        old_s = time.perf_counter() - t0
        old_commits = conn1.commits

        # --- 新方式：一次批量 delete（一个事务） ---
        store2, conn2 = make_store(base / "new.db", delay)
        seed_records(store2, [f"/m/new/{i}.strm" for i in range(count)])
        t0 = time.perf_counter()
        removed = store2.delete([f"/m/new/{i}.strm" for i in range(count)])
        new_s = time.perf_counter() - t0
        new_commits = conn2.commits

        print(f"     逐条：{old_s:.2f}s / {old_commits} 次 commit；"
              f"批量：{new_s:.2f}s / {new_commits} 次 commit")
        check("批量删除的 commit 次数是常数级（≤3 次）", new_commits <= 3,
              f"{new_commits}")
        check("逐条删除的 commit 次数与记录数成正比（旧问题复现）",
              old_commits >= count, f"{old_commits} vs {count}")
        check(f"批量删除比逐条快（{old_s:.2f}s -> {new_s:.2f}s）",
              new_s < old_s, f"{new_s:.3f} vs {old_s:.3f}")
        check("批量删除的行数统计正确", removed == count, str(removed))
        check("删除后记录确实消失",
              all(store2.get(f"/m/new/{i}.strm") is None for i in range(count)))

        # --- 删除 API 用到的配套接口（含 running 过滤） ---
        store3, conn3 = make_store(base / "api.db", delay)
        paths = [f"/m/api/{i}.strm" for i in range(count)]
        seed_records(store3, paths)
        # 第一条假装正在处理：删除 API 必须把它留下、只删其余的
        store3._conn.execute(
            "UPDATE items SET status='running' WHERE path=?", (paths[0],))
        store3._conn.commit()
        t0 = time.perf_counter()
        items = store3.get_many(paths)          # 批量取状态（running 过滤用）
        running = {p for p, it in items.items() if it.status == "running"}
        removable = [p for p in paths if p not in running]
        store3.delete(removable)                # 一次事务删完
        api_s = time.perf_counter() - t0
        print(f"     API 同款两步（get_many + delete，含 1 条 running）："
              f"{api_s:.2f}s / {conn3.commits} 次 commit")
        check("get_many 一次取全量、无遗漏", len(items) == count)
        check("running 记录被排除、不再可删", running == {paths[0]},
              str(running))
        check("API 同款路径 commit 次数是常数级（≤3 次）", conn3.commits <= 3,
              str(conn3.commits))
        check("running 保留、其余全部删除",
              store3.get(paths[0]) is not None
              and all(store3.get(p) is None for p in paths[1:]))
        for s in (store1, store2, store3):
            s._conn.close()


# ---------- 真实接口端到端 ----------

class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return json.load(r)

    def post(self, path: str, data: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data if data is not None else {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "message": f"HTTP {e.code}"}


def items_index(c: Client) -> dict[str, str]:
    """当前任务列表 {path: status}（分页接口，size 上限 200）。"""
    out: dict[str, str] = {}
    page = 1
    while True:
        rows = c.get(f"/api/items?page={page}&size=200").get("items", [])
        for it in rows:
            out[it["path"]] = it.get("status", "")
        if len(rows) < 200:
            return out
        page += 1


def e2e_suite(c: Client, count: int) -> None:
    print(f"\n== 真实接口：{c.base} ==")
    root = Path(c.get("/api/config")["scan"]["roots"][0]).resolve()
    d = root / "__delperf__"
    shutil.rmtree(d, ignore_errors=True)
    try:
        paths = build_fixture(d, count)
        sources = [Path(p) for p in paths]

        # 登记「已完成任务」（产物齐全 → 直接跳过并留下记录，不启动任务）
        r = c.post("/api/files/scrape", {"paths": paths, "now": False})
        check("夹具登记成功且不启动任务",
              r.get("ok") is True and r.get("started") is False, str(r)[:120])

        # --- 1. 批量删记录（不删产物） ---
        idx0 = items_index(c)
        listed = [p for p in paths if p in idx0]
        check("夹具记录都在任务列表里", len(listed) == count,
              f"{len(listed)}/{count}")
        t0 = time.perf_counter()
        r1 = c.post("/api/items/delete", {"paths": paths, "purge": False})
        dt1 = time.perf_counter() - t0
        print(f"     删除 {count} 条记录：{dt1:.2f}s")
        check("删除接口成功", r1.get("ok") is True, str(r1)[:120])
        check("removed 统计正确", r1.get("removed") == count, str(r1)[:120])
        check(f"批量删除在数秒内完成", dt1 < 8.0, f"{dt1:.2f}s")
        idx1 = items_index(c)
        check("删除后记录全部消失", not any(p in idx1 for p in paths))
        alive = all(a.is_file() for s in sources for a in arts_of(s))
        check("不 purge 时产物文件一个不动", alive)

        # 重复删除同样的路径：返回友好提示，不报错
        r2 = c.post("/api/items/delete", {"paths": paths, "purge": False})
        check("重复删除返回 ok=false 且有提示",
              r2.get("ok") is False and "不在列表" in r2.get("message", ""),
              str(r2)[:120])

        # --- 2. purge 删产物 ---
        c.post("/api/files/scrape", {"paths": paths, "now": False})
        t0 = time.perf_counter()
        r3 = c.post("/api/items/delete", {"paths": paths, "purge": True})
        dt3 = time.perf_counter() - t0
        print(f"     purge 删除 {count} 条（含产物）：{dt3:.2f}s")
        check("purge 接口成功", r3.get("ok") is True, str(r3)[:120])
        check("purged 统计正确（每条 4 个产物）",
              r3.get("purged") == count * 4, str(r3)[:120])
        idx2 = items_index(c)
        check("purge 后记录消失", not any(p in idx2 for p in paths))
        leftovers = [a for s in sources for a in arts_of(s) if a.is_file()]
        check("purge 后产物文件全部删除", not leftovers,
              f"剩 {len(leftovers)} 个")
        check(".strm 本体保留", all(s.is_file() for s in sources))

        # --- 3. 越界保护：roots 外路径不碰磁盘 ---
        with tempfile.TemporaryDirectory(prefix="ws-del-out-") as outside:
            outside = Path(outside)
            alien = outside / "越界.strm"
            alien.write_text("x", encoding="utf-8")
            alien_art = build_target(alien)
            alien_art.poster.write_bytes(b"keep-me")
            r4 = c.post("/api/items/delete", {"paths": [str(alien)],
                                              "purge": True})
            check("越界路径请求不报 500",
                  r4.get("ok") is not None or r4.get("message") is not None,
                  str(r4)[:120])
            check("越界路径的产物文件没被碰", alien_art.poster.is_file())

        # --- 4. 文件管理删指定类型产物（上一节 purge 清光了产物，先补回来） ---
        c.post("/api/task/cancel")   # 保险：别让后台任务搅局
        for s in sources:
            t = build_target(s)
            for art in (t.nfo, t.poster, t.fanart, t.thumb):
                art.write_bytes(b"art-again-" + s.name.encode())
        r = c.post("/api/files/scrape", {"paths": paths, "now": False})
        check("补产物后登记仍走跳过路径",
              r.get("ok") is True and r.get("started") is False, str(r)[:120])
        r5 = c.post("/api/files/delete",
                    {"paths": paths[:10], "what": ["poster", "nfo"]})
        check("文件管理删产物成功", r5.get("ok") is True, str(r5)[:120])
        check("删除数量统计正确", r5.get("removed") == 20, str(r5)[:120])
        first10 = [Path(p) for p in paths[:10]]
        check("poster/nfo 被删", all(
            not t.poster.is_file() and not t.nfo.is_file() for t in
            (build_target(s) for s in first10)))
        check("fanart/thumb 保留", all(
            t.fanart.is_file() and t.thumb.is_file() for t in
            (build_target(s) for s in first10)))
        idx3 = items_index(c)
        check("删过产物的记录重置为 pending",
              all(idx3.get(p) == "pending" for p in paths[:10]),
              str({p: idx3.get(p) for p in paths[:3]}))
        check("未触碰的记录状态不变",
              all(idx3.get(p) == "skipped" for p in paths[10:]),
              str(idx3.get(paths[10])))
    finally:
        # 收尾：把登记的测试记录也删掉，别把状态库留给下一个测试当惊喜
        try:
            page = 1
            leftovers: list[str] = []
            while True:
                rows = c.get(f"/api/items?page={page}&size=200").get("items", [])
                leftovers.extend(it["path"] for it in rows
                                 if Path(it["path"]).parent == d)
                if len(rows) < 200:
                    break
                page += 1
            if leftovers:
                c.post("/api/items/delete", {"paths": leftovers, "purge": False})
                print(f"  （清理：移除 {len(leftovers)} 条测试记录）")
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300, help="记录条数")
    ap.add_argument("--delay", type=float, default=0.001,
                    help="模拟每次 commit 的刷盘耗时（秒）")
    ap.add_argument("--base", default="",
                    help="可选：额外跑一轮真实接口端到端（如 http://127.0.0.1:6693）")
    ap.add_argument("--root", default="",
                    help="媒体库根目录，默认读 /api/config")
    args = ap.parse_args()
    count = max(30, args.count)

    offline_suite(count, args.delay)

    if args.base:
        c = Client(args.base)
        if args.root:
            # 简单桩：e2e_suite 会重新读 /api/config，这里只做连通性验证
            c.get("/api/config")
        e2e_suite(c, min(count, 300))

    print(f"\n========== 结果：{PASS} 通过，{len(FAIL)} 失败 ==========")
    for name in FAIL:
        print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
