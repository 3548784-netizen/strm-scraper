# -*- coding: utf-8 -*-
"""检查「实时监听」：目录里一出现新的 strm 就自动入队，不用等下一轮定时。

分两段：

1. 纯单元：不起服务，直接驱动 ``Watcher``——自检能不能收到事件、批量事件会不会
   被合并成一批、非 strm（刮削自己写的海报 / NFO）会不会误触发、重复触碰已刮好
   的文件会不会重复入队。
2. 端到端：给正在跑的控制台（默认 http://127.0.0.1:6672）建一条开实时监听的
   任务，往监控目录里丢一个 strm，看它是不是自己开始刮了。

跑法：

    python tools/check_watcher.py                # 只跑单元
    python tools/check_watcher.py --base http://127.0.0.1:6672
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.media import signature  # noqa: E402
from scraper.state import StateStore  # noqa: E402
from scraper.watcher import Watcher  # noqa: E402

checked = failed = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global checked, failed
    checked += 1
    if ok:
        print(f"  OK   {label}")
        return
    failed += 1
    print(f"  FAIL {label}{'  → ' + detail if detail else ''}")


def fake_cfg(roots: list[str] | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(scan=types.SimpleNamespace(
        roots=roots or [], extensions=[".strm"]))


def part_unit() -> None:
    print("---------- 1. 监听与防抖 ----------")
    root = Path(".devdata/watchtest")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    deep = root / "子目录" / "再深一层"
    deep.mkdir(parents=True, exist_ok=True)

    batches: list[list[str]] = []
    watcher = Watcher(fake_cfg(), lambda tid, paths: batches.append(list(paths)) or True)
    check(watcher.available, "watchdog 已安装", watcher.unavailable_reason)

    watcher.sync([{"id": "t1", "enabled": True, "watch": True,
                   "scope": str(root), "debounce_sec": 10}])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and watcher.state("t1").get("reason"):
        time.sleep(0.3)
    state = watcher.state("t1")
    check(state.get("active") is True, "自检通过，监听生效", str(state))
    check(not any(p.name.startswith(".strm-watch-probe") for p in root.iterdir()),
          "自检的探针文件没留在目录里", str([p.name for p in root.iterdir()]))

    (root / "a.strm").write_text("http://example.invalid/a.mkv\n", encoding="utf-8")
    (deep / "b.strm").write_text("http://example.invalid/b.mkv\n", encoding="utf-8")
    (root / "别管我.txt").write_text("刮削产物不该触发", encoding="utf-8")
    (root / "poster.jpg").write_bytes(b"\xff\xd8\xff" + b"P" * 40)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not batches:
        time.sleep(0.3)
    check(len(batches) == 1, "收到新文件并触发入队", str(batches))
    if batches:
        names = sorted(Path(p).name for p in batches[0])
        check(names == ["a.strm", "b.strm"], "两个 strm 合成了一批（防抖生效）", str(names))
        check(all(n.endswith(".strm") for n in names),
              "海报和非 strm 文件没有混进来", str(names))
    check(any(Path(p).parent.name == "再深一层" for p in (batches[0] if batches else [])),
          "子目录里的文件也能收到（递归监听）")

    print("\n---------- 2. 开关与范围 ----------")
    batches.clear()
    watcher.sync([{"id": "t1", "enabled": True, "watch": True, "scope": str(root),
                   "debounce_sec": 10}])
    watcher.sync([])   # 任务删掉 → 监听停掉
    (root / "c.strm").write_text("http://example.invalid/c.mkv\n", encoding="utf-8")
    time.sleep(14)
    check(not batches, "任务删掉后不再触发", str(batches))

    # 只监听子目录：上级目录里的变化不该算到它头上
    sub = root / "子目录"
    watcher.sync([{"id": "t2", "enabled": True, "watch": True,
                   "scope": str(sub), "debounce_sec": 10}])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and watcher.state("t2").get("reason"):
        time.sleep(0.3)
    batches.clear()
    (root / "d.strm").write_text("http://example.invalid/d.mkv\n", encoding="utf-8")
    time.sleep(14)
    check(not batches, "范围外的文件不触发", str(batches))

    watcher.stop()
    shutil.rmtree(root, ignore_errors=True)


def api(base: str, path: str, payload: dict | None = None) -> dict:
    if payload is None:
        with urllib.request.urlopen(base + path, timeout=20) as resp:
            return json.load(resp)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def part_dedupe() -> None:
    """目录被随便碰一下就来事件，已刮好的文件不能反复重刮。"""
    print("\n---------- 2b. 已刮好的文件不再重复入队 ----------")
    root = Path(".devdata/watchdedupe")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    done = root / "已刮好.strm"
    broken = root / "上次失败.strm"
    fresh = root / "新来的.strm"
    for path in (done, broken, fresh):
        path.write_text("http://example.invalid/x.mkv\n", encoding="utf-8")

    db = root / "state.db"
    try:
        store = StateStore(db)
        store.ensure([(str(p), *signature(p)) for p in (done, broken)])
        with store._lock:
            store._conn.execute(
                "UPDATE items SET status='ok' WHERE path=?", (str(done),))
            store._conn.execute(
                "UPDATE items SET status='failed' WHERE path=?", (str(broken),))
            store._conn.commit()

        from scraper.scheduler import Scheduler
        sched = Scheduler.__new__(Scheduler)
        sched._lock = threading.RLock()
        sched.store = store
        todo = sched._new_or_changed([str(done), str(broken), str(fresh)])
        names = sorted(Path(p).name for p in todo)
        check(names == ["上次失败.strm", "新来的.strm"],
              "只入队「没刮过的」和「上次失败的」", str(names))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def part_live(base: str) -> None:
    print("\n---------- 3. 端到端：丢一个 strm 进去，看它自己跑起来 ----------")
    try:
        data = api(base, "/api/automation")
    except OSError as exc:
        check(False, "连不上控制台", str(exc))
        return
    roots = data.get("roots") or []
    check(bool(roots), "控制台里有媒体库目录", str(roots))
    if not roots:
        return
    check(data["status"].get("watch_available") is True, "实时监听可用",
          data["status"].get("watch_reason", ""))

    scope = Path(roots[0]) / "实时监听测试"
    shutil.rmtree(scope, ignore_errors=True)
    scope.mkdir(parents=True, exist_ok=True)

    saved = None
    try:
        saved = api(base, "/api/automation/save", {
            "name": "实时监听回归", "scope": str(scope), "mode": "incremental",
            "interval_minutes": 600, "enabled": True,
            "watch": True, "debounce_sec": 10,
        })
        task = next((t for t in saved["tasks"] if t["name"] == "实时监听回归"), None)
        check(task is not None, "任务建好了")
        if task is None:
            return
        tid = task["id"]

        # 等自检跑完（最多 10 秒），确认没有降级
        state = {}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            rows = api(base, "/api/automation")["tasks"]
            row = next((t for t in rows if t["id"] == tid), None)
            state = (row or {}).get("watchState") or {}
            if state.get("active") or (state.get("reason")
                                       and "正在确认" not in state["reason"]):
                break
            time.sleep(0.5)
        check(state.get("active") is True, "界面上显示实时监听已生效", str(state))

        (scope / "新片.strm").write_text(
            "http://example.invalid/new.mkv\n", encoding="utf-8")
        # 不能只看 last_result：刮完会被「完成：成功 x，失败 y」覆盖掉，
        # 而 example.invalid 这种假地址几秒就失败了，轮询很容易错过中间那句
        started = False
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            rows = api(base, "/api/automation")["tasks"]
            row = next((t for t in rows if t["id"] == tid), None)
            if row and int(row.get("runs") or 0) > 0 and row.get("last_run"):
                started = True
                print(f"        已跑 {row['runs']} 次：{row.get('last_result')}")
                break
            time.sleep(0.6)
        check(started, "新 strm 落地后自动开始刮削（没等定时）",
              str((row or {}).get("last_result")))
    finally:
        if saved:
            task = next((t for t in saved["tasks"] if t["name"] == "实时监听回归"), None)
            if task:
                try:
                    api(base, "/api/automation/delete", {"id": task["id"]})
                except OSError:
                    pass
        shutil.rmtree(scope, ignore_errors=True)


def part_downgrade() -> None:
    """收不到事件的目录（挂载盘、容器跨系统挂载）必须明确降级，不能假装实时。"""
    print("\n---------- 2c. 收不到事件时要降级 ----------")
    root = Path(".devdata/watchdown")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    batches: list[list[str]] = []
    watcher = Watcher(fake_cfg(), lambda tid, paths: batches.append(list(paths)) or True)
    watcher._probe_once = lambda directory: False      # 假装这个目录收不到事件
    try:
        watcher.sync([{"id": "t9", "enabled": True, "watch": True,
                       "scope": str(root), "debounce_sec": 10}])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not watcher._watches:
            time.sleep(0.2)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and watcher._watches:
            time.sleep(0.3)
        state = watcher.state("t9")
        check(not watcher._watches, "自检失败后监听真的停掉了", str(watcher._watches))
        check(state.get("active") is False, "状态标记为未生效", str(state))
        check("定时轮询" in (state.get("reason") or ""),
              "界面文案说明已退回定时轮询", str(state))
    finally:
        watcher.stop()
        shutil.rmtree(root, ignore_errors=True)


def part_ui(base: str) -> None:
    """界面别被改坏：能开出带实时开关的弹窗，任务卡上有监听标签。"""
    print("\n---------- 4. 界面 ----------")
    from playwright.sync_api import sync_playwright
    # 先清场：上一次跑残留的任务会让「删掉它」这条断言永远失败
    for row in api(base, "/api/automation").get("tasks") or []:
        try:
            api(base, "/api/automation/delete", {"id": row["id"]})
        except OSError:
            pass
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.click('[data-tab="automation"]')
        page.wait_for_timeout(800)
        check(not errors, "页面没有 JS 报错", str(errors[:2]))
        page.click("#btnAutoAdd")
        page.wait_for_timeout(400)
        check(page.is_visible("#autoWatch"), "弹窗里有实时监听开关")
        page.check("#autoWatch")
        page.wait_for_timeout(200)
        check(page.is_visible("#autoDebounce"), "勾上后出现静默时间输入框")
        page.fill("#autoName", "界面检查用")
        page.fill("#autoInterval", "600")
        page.click("#btnAutoSave")
        page.wait_for_timeout(1500)
        # 自检要几秒，先等标签从「检测中…」变成结论，再判断
        text = ""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            el = page.query_selector(".auto-card")
            text = el.inner_text() if el else ""
            if "实时监听中" in text or "实时未生效" in text:
                break
            page.wait_for_timeout(500)
        check("实时监听中" in text, "任务卡上标出了实时监听状态", text[:160])
        # 删掉，别把夹具库弄脏
        page.on("dialog", lambda d: d.accept())
        page.click('.auto-card [data-act="del"]')
        page.wait_for_timeout(1200)
        check(page.query_selector(".auto-card") is None
              or "界面检查用" not in page.inner_text("#autoList"),
              "任务已删除", page.inner_text("#autoList")[:120])
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="控制台地址，给了才跑端到端")
    args = parser.parse_args()

    part_unit()
    part_dedupe()
    part_downgrade()
    if args.base:
        part_live(args.base.rstrip("/"))
        part_ui(args.base.rstrip("/"))
    print(f"\n结果：{checked} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
