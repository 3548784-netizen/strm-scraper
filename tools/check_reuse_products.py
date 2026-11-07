# -*- coding: utf-8 -*-
"""「已有本地产物始终可复用」验收。

复现的是这个场景：用户在任务列表里删掉了已完成的任务（只删记录，产物还在），
之后在文件管理页再点「开始刮削」，不该因为 state.db 里少了这一行就把海报 /
NFO 重新刮一遍。

覆盖：单文件、批量混合、重复触发、整库增量、实时监听的入队判断，
以及「只补真正缺的那一项、不覆盖已有结果」。

用法（需要一套隔离的 devserver，脚本会在媒体库里造一个 __reuse__ 测试目录，
跑完默认清掉）：
    python tools/check_reuse_products.py --base http://127.0.0.1:6693
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

# 脚本在 tools/ 里，直接 import scraper 会找不到包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "message": f"HTTP {e.code}"}


def snapshot(directory: Path) -> dict[str, str]:
    """所有产物的内容指纹；只要有一个字节被重写就能发现。"""
    out = {}
    for f in sorted(directory.iterdir()):
        if f.suffix in (".jpg", ".nfo"):
            out[f.name] = hashlib.md5(f.read_bytes()).hexdigest()
    return out


def states(c: Client, directory: Path) -> dict[str, str]:
    """翻页拿全任务列表再过滤（size 上限 200，limit 参数并不存在）。"""
    out: dict[str, str] = {}
    page = 1
    while True:
        j = c.get(f"/api/items?page={page}&size=200")
        rows = j.get("items", [])
        for it in rows:
            try:
                p = Path(it["path"])
            except Exception:
                continue
            if p.parent == directory:
                out[p.name] = it.get("status")
        if len(rows) < 200:
            return out
        page += 1


def drop_records(c: Client, directory: Path) -> None:
    """只删测试目录里的任务记录，模拟用户「从任务列表移除已完成项」。"""
    page = 1
    paths: list[str] = []
    while True:
        j = c.get(f"/api/items?page={page}&size=200")
        rows = j.get("items", [])
        paths.extend(it["path"] for it in rows
                     if Path(it["path"]).parent == directory)
        if len(rows) < 200:
            break
        page += 1
    if paths:
        c.post("/api/items/delete", {"paths": paths, "purge": False})


def make_fixture(root: Path, video: str) -> dict[str, str]:
    """A / C 产物齐全，B 缺 NFO；A / C 指向不可用地址，被误重跑就会失败。"""
    d = root / "__reuse__"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for name in "AC":
        (d / f"{name}.strm").write_text(
            "http://127.0.0.1:1/never-exists.mp4\n", encoding="utf-8")
        (d / f"{name}.nfo").write_text(
            f"<movie><title>{name}</title></movie>\n", encoding="utf-8")
        for kind in ("poster", "fanart"):
            (d / f"{name}-{kind}.jpg").write_bytes(
                f"ORIGINAL-{name}-{kind}".encode())
    (d / "B.strm").write_text(video.strip() + "\n", encoding="utf-8")
    for kind in ("poster", "fanart"):
        (d / f"B-{kind}.jpg").write_bytes(f"ORIGINAL-B-{kind}".encode())
    return {n: str(d / f"{n}.strm") for n in "ABC"}


def wait_idle(c: Client, seconds: float = 40) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        s = c.get("/api/status")
        if s.get("state") in ("idle", "done", ""):
            return True
        time.sleep(0.6)
    return False


def unit_checks(directory: Path) -> None:
    """不依赖服务的单元断言：共用判断函数 + 实时监听的入队守卫。"""
    from scraper.media import split_by_products
    from scraper.scheduler import Scheduler

    flags = (True, True, True, False)
    paths = [str(directory / f"{n}.strm") for n in "ABC"]
    ready, missing = split_by_products(paths, *flags)
    check("共用判断：A/C 齐全、B 缺 NFO",
          set(Path(p).name for p in ready) == {"A.strm", "C.strm"}
          and set(Path(p).name for p in missing) == {"B.strm"},
          f"ready={ready} missing={missing}")
    check("共用判断：重复路径去重、空列表不报错",
          split_by_products(paths + paths[:1], *flags)[0] == ready
          and split_by_products([], *flags) == ([], []))
    check("共用判断：开启 thumb 时缺 thumb 也算不齐",
          split_by_products([paths[0]], True, True, True, True)[0] == [])

    cfg = SimpleNamespace(scrape=SimpleNamespace(
        write_nfo=True, write_poster=True, write_fanart=True,
        thumb=SimpleNamespace(enabled=False)))

    class FakeStore:
        def __init__(self, known: dict):
            self.known = known
            self.skipped: list[str] = []
            self.ensured: list[str] = []

        def get_many(self, _paths):
            return self.known

        def ensure(self, rows):
            self.ensured.extend(p for p, _s, _m in rows)
            return len(rows)

        def mark_skipped(self, path, _reason=""):
            self.skipped.append(path)

    # 记录被全部删掉：产物齐全的 A/C 不该进 todo，B 缺 NFO 才要处理；
    # 而且不再登记 skipped 记录——不然自动化一跑，删掉的列表就复活了
    store = FakeStore({})
    sched = Scheduler.__new__(Scheduler)
    sched.cfg, sched.store = cfg, store
    todo = sched._new_or_changed(paths)
    check("实时监听：记录全缺时只把缺产物的 B 入队",
          [Path(p).name for p in todo] == ["B.strm"], str(todo))
    check("实时监听：齐全文件不再登记（删除后不复活）",
          store.skipped == [] and store.ensured == [],
          f"skipped={store.skipped} ensured={store.ensured}")

    # 没有状态库时也不能把齐全文件当成新片
    sched.store = None
    check("实时监听：无状态库时同样按磁盘产物判断",
          [Path(p).name for p in sched._new_or_changed(paths)] == ["B.strm"])

    # 不能把「源文件确实被改动」的既有语义一起改坏
    from scraper.media import signature
    a = paths[0]
    size, mtime = signature(Path(a))
    stale = {a: SimpleNamespace(status="ok", size=size + 1, mtime=mtime)}
    sched.store = FakeStore(stale)
    check("实时监听：源文件变了仍按原逻辑重跑（不误伤既有语义）",
          sched._new_or_changed([a]) == [a])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    ap.add_argument("--root", default="", help="媒体库根目录，默认读 /api/config")
    ap.add_argument("--video", default="http://127.0.0.1:6688/wide_720.mp4",
                    help="缺产物那条用的可探测视频直链")
    ap.add_argument("--keep", action="store_true", help="跑完保留测试目录")
    args = ap.parse_args()
    c = Client(args.base)

    # 缩略图现在是默认开启，而「产物齐全」判定包含 thumb；本脚本测的复用
    # 语义只关心海报 / 背景图 / NFO，先把它关掉，免得齐全夹具被误判成缺产物。
    st = c.get("/api/settings")
    st.setdefault("scrape", {})["thumb"] = False
    c.post("/api/settings", st)

    root = Path(args.root).resolve() if args.root else Path(
        c.get("/api/config")["scan"]["roots"][0]).resolve()
    print(f"服务 {args.base} | 媒体库 {root}")
    d = root / "__reuse__"
    p = make_fixture(root, args.video)
    base_snap = snapshot(d)

    print("\n========== 1. 单元：产物判断与实时监听入队 ==========")
    unit_checks(d)

    print("\n========== 2. 单个文件 + 重复触发（任务记录已删） ==========")
    drop_records(c, d)
    check("删除后测试目录里没有任务记录", states(c, d) == {}, str(states(c, d)))
    r1 = c.post("/api/files/scrape", {"paths": [p["A"]], "now": True})
    check("单文件：不启动刮削、明确返回跳过",
          r1.get("ok") is True and r1.get("started") is False
          and r1.get("skipped") == 1, str(r1))
    time.sleep(1.5)
    check("单文件：已有产物一个字节都没动", snapshot(d) == base_snap)
    check("单文件：状态记为可复用（skipped）",
          states(c, d).get("A.strm") == "skipped", str(states(c, d)))
    r2 = c.post("/api/files/scrape", {"paths": [p["A"]], "now": True})
    check("重复触发：行为完全一致",
          r2.get("started") is False and r2.get("skipped") == 1, str(r2))
    time.sleep(1.5)
    check("重复触发：产物仍未被覆盖", snapshot(d) == base_snap)

    print("\n========== 3. 批量混合：两个齐全 + 一个缺 NFO ==========")
    drop_records(c, d)
    r3 = c.post("/api/files/scrape",
                {"paths": [p["A"], p["B"], p["C"]], "now": False})
    check("批量：只跳过齐全的两条、启动缺产物那条",
          r3.get("ok") is True and r3.get("skipped") == 2
          and r3.get("started") is True, str(r3))
    time.sleep(8)
    after = snapshot(d)
    check("批量：A/C 的 NFO / 海报 / 背景图全部原样保留",
          all(after.get(k) == v for k, v in base_snap.items() if k.startswith(("A", "C"))))
    check("批量：B 已有图片也没被覆盖",
          all(after.get(k) == v for k, v in base_snap.items() if k.startswith("B")))
    b_nfo = "B.nfo" in after
    print(f"  INFO B.nfo 生成：{b_nfo}（视频源不可用时只会登记失败，不影响跳过结论）")
    st = states(c, d)
    check("批量：缺产物那条确实被执行过（不再是「无记录」）",
          "B.strm" in st, str(st))
    check("批量：齐全两条没有被重新标成处理中/失败",
          all(st.get(f"{n}.strm") in ("ok", "skipped") for n in "AC"), str(st))

    print("\n========== 4. 再重复触发整批 ==========")
    wait_idle(c, 60)   # B 的刮削可能还在收尾，busy 时 scrape 会拒绝重复触发
    r4 = c.post("/api/files/scrape",
                {"paths": [p["A"], p["B"], p["C"]], "now": False})
    check("再次批量：没有需要重跑的条目",
          r4.get("started") is False and r4.get("skipped") >= 2, str(r4))
    time.sleep(2)
    check("再次批量：产物指纹不变", snapshot(d) in (base_snap, after)
          or all(snapshot(d).get(k) == v for k, v in after.items()))

    print("\n========== 5. 整库增量：删除的记录不再被登记回来 ==========")
    drop_records(c, d)
    wait_idle(c, 20)
    st_before5 = states(c, d)
    r5 = c.post("/api/task/start?full=false")
    check("整库增量启动成功", r5.get("ok") is True, str(r5))
    wait_idle(c, 40)
    # processed 是「本轮任务」的计数，库里还有别的真实素材时本来就会大于 0，
    # 拿它断言「齐全文件没入队」会误报；只比对夹具目录里的记录有没有变化。
    p5 = c.get("/api/status").get("processed", 0)
    st5 = states(c, d)
    check("整库增量：齐全文件没有进入刮削队列",
          st5 == st_before5,
          f"before={st_before5} after={st5} 本轮 processed={p5}")
    check("整库增量：产物仍未被覆盖",
          all(snapshot(d).get(k) == v for k, v in base_snap.items()))
    check("整库增量：删过的记录保持缺失（列表不再复活）",
          all(st5.get(f"{n}.strm") is None for n in "ABC"), str(st5))

    print("\n========== 6. 自动化范围任务：删除的记录同样不复活 ==========")
    # 复现用户报告的链路：建一个 scope 指向测试目录的自动化任务，
    # 「立即执行」之后 A/C 不能因为产物齐全就重新出现在任务列表里。
    drop_records(c, d)
    wait_idle(c, 20)
    saved = c.post("/api/automation/save", {
        "name": "__reuse_scope__", "scope": str(d), "mode": "incremental",
        "interval_minutes": 60, "enabled": True, "watch": False,
    })
    tid = ""
    for t in saved.get("tasks", []):
        if t.get("name") == "__reuse_scope__":
            tid = t.get("id") or ""
            break
    check("范围任务保存成功", bool(tid), str(saved)[:120])
    if tid:
        run = c.post("/api/automation/run", {"id": tid})
        check("范围任务立即执行成功",
              run.get("ok") is True and run.get("started") is True, str(run)[:120])
        wait_idle(c, 40)
        st6 = states(c, d)
        check("范围任务：删过的记录保持缺失",
              all(st6.get(f"{n}.strm") is None for n in "ABC"), str(st6))
        check("范围任务：产物仍未被覆盖",
              all(snapshot(d).get(k) == v for k, v in base_snap.items()))
        c.post("/api/automation/delete", {"id": tid})
        check("范围任务已清理",
              all(t.get("id") != tid for t in c.get("/api/automation").get("tasks", [])))

    print("\n========== 7. purge 删掉产物：缺产物的照常登记补刮 ==========")
    # 删除记录 + purge 删产物之后，下一轮扫描要能把缺产物的文件重新
    # 登记入队补刮——「不再登记」只针对产物齐全的，不能误伤这条路径。
    c.post("/api/items/delete", {"paths": [p["B"]], "purge": True})
    wait_idle(c, 10)
    before_p = c.get("/api/status").get("processed", 0)
    r7 = c.post("/api/task/start?full=false")
    check("purge 后增量启动成功", r7.get("ok") is True, str(r7))
    wait_idle(c, 40)
    after_p = c.get("/api/status").get("processed", 0)
    check("purge 后：缺产物的 B 被重新登记并补刮",
          after_p - before_p >= 1, f"processed {before_p} -> {after_p}")
    st7 = states(c, d)
    check("purge 后：B 记录恢复（补刮路径未断）",
          st7.get("B.strm") is not None, str(st7))
    check("purge 后：A/C 仍不复活",
          st7.get("A.strm") is None and st7.get("C.strm") is None, str(st7))
    check("purge 后：A/C 产物仍原样",
          all(snapshot(d).get(k) == v for k, v in base_snap.items()
              if k.startswith(("A", "C"))))

    print("\n========== 8. 源码契约 ==========")
    from pathlib import Path as _P
    src = {f: _P("scraper") / f for f in
           ("media.py", "worker.py", "scheduler.py", "web.py")}
    text = {k: v.read_text(encoding="utf-8") for k, v in src.items()}
    check("共用产物判断函数已定义", "def split_by_products(" in text["media.py"])
    check("增量扫描与勾选范围都按产物复用",
          text["worker.py"].count("split_by_products(") >= 2)
    check("实时监听按产物复用", "split_by_products(" in text["scheduler.py"])
    check("单条立即执行有最后一道保险",
          "item is None and not force and _products_ready" in text["web.py"])
    check("明确重刮仍保留覆盖语义", "if self._force:" in text["worker.py"])
    check("后台扫描带不登记开关", "register_ready" in text["worker.py"]
          and "register_ready=False" in text["scheduler.py"])

    print("\n========== 清理 ==========")
    drop_records(c, d)
    if not args.keep:
        shutil.rmtree(d, ignore_errors=True)
        check("测试目录已清理", not d.exists())
    else:
        print(f"  INFO 按 --keep 保留 {d}")

    print(f"\n结果：{len(FAIL)} 项失败，{PASS} 项通过")
    for name in FAIL:
        print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
