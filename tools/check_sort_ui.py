# -*- coding: utf-8 -*-
"""排序功能回归：任务列表 / 文件管理按名称、按添加时间排序。

先 `python tools/devserver.py` 起环境，再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import json
import urllib.parse
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:6670"


def _use_argv_base() -> None:
    """支持 --base 换个服务地址（同时起多套环境时用来指定测哪一套）。"""
    global BASE
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--base" and i + 1 < len(argv):
            BASE = argv[i + 1].rstrip("/")
        elif arg.startswith("--base="):
            BASE = arg.split("=", 1)[1].rstrip("/")


_use_argv_base()
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as resp:
        return json.load(resp)


def task_names(page) -> list[str]:
    return page.eval_on_selector_all(
        "#taskBody tr[data-path]", "els => els.map(e => e.dataset.path)")


def seg_state(page, seg: str) -> tuple[str, str]:
    """返回（当前排序键, 正倒序）。"""
    return page.evaluate("""(seg) => {
      const box = document.querySelector(seg);
      const on = box.querySelector('button.on');
      return [on?.dataset.sort || '', on?.textContent.includes('↑') ? 'asc' : 'desc'];
    }""", seg)


def main() -> int:
    items = api("/api/items?size=60&sort=added&order=desc")["items"]
    if len(items) < 3:
        print("测试数据太少（至少 3 条），先跑一轮任务")
        return 1
    by_name = [i["path"] for i in api("/api/items?size=60&sort=name&order=asc")["items"]]
    by_name_desc = [i["path"] for i in api("/api/items?size=60&sort=name&order=desc")["items"]]
    by_added = [i["path"] for i in api("/api/items?size=60&sort=added&order=desc")["items"]]
    by_updated = [i["path"] for i in api("/api/items?size=60&sort=updated&order=desc")["items"]]

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#taskBody tr[data-path]", timeout=15000)
        page.wait_for_timeout(600)

        print("\n---------- 1. 任务列表排序控件 ----------")
        key, order = seg_state(page, "#taskSort")
        check("默认按添加时间排序", key == "added", key)
        check("默认倒序（最新入库的在最前）", order == "desc", order)
        check("默认顺序与接口一致", task_names(page) == by_added,
              f"{len(task_names(page))} 行")

        print("\n---------- 1.5 切到按最近处理 ----------")
        page.click('#taskSort button[data-sort="updated"]')
        page.wait_for_timeout(900)
        key, order = seg_state(page, "#taskSort")
        check("能切到最近处理", [key, order] == ["updated", "desc"], f"{key}/{order}")
        check("按最近处理倒序排列", task_names(page) == by_updated, str(task_names(page)[:2]))

        print("\n---------- 2. 切到按名称 ----------")
        page.click('#taskSort button[data-sort="name"]')
        page.wait_for_timeout(900)
        key, order = seg_state(page, "#taskSort")
        check("排序键切到名称", key == "name", key)
        check("名称默认正序", order == "asc", order)
        check("按名称正序排列", task_names(page) == by_name, str(task_names(page)[:3]))
        check("按钮上有方向箭头",
              "↑" in page.inner_text('#taskSort button[data-sort="name"]'))

        print("\n---------- 3. 再点一次翻转方向 ----------")
        page.click('#taskSort button[data-sort="name"]')
        page.wait_for_timeout(900)
        key, order = seg_state(page, "#taskSort")
        check("方向翻成倒序", order == "desc", order)
        check("列表确实反过来了", task_names(page) == by_name_desc,
              str(task_names(page)[:3]))

        print("\n---------- 4. 按文件夹视图也跟着排 ----------")
        page.click('#taskView button[data-view="dir"]')
        page.wait_for_timeout(1500)
        # 这一屏是目录树，顺序要按「父在前、同层按名字倒序」来比：
        # 直接把整屏扁平排序会比错（根节点总在最前面）
        tree = api("/api/items/tree?status=all&limit=400&sort=name&order=desc")["nodes"]
        expected: list[str] = []

        def walk(nodes):
            for node in nodes:
                expected.append(node["path"])
                walk(node["children"])

        walk(tree)
        groups = page.eval_on_selector_all(
            "#taskBody tr.grouprow", "els => els.map(e => e.dataset.group)")
        check("文件夹视图有分组", len(groups) >= 1, f"{len(groups)} 组")
        if len(groups) >= 2:
            check("文件夹按名称倒序（与接口返回的树一致）", groups == expected,
                  str([g.rsplit("\\", 1)[-1] for g in groups][:6]))
        else:
            print("  ..   只有 1 个文件夹，跳过排序比对（跑全量回归后数据会被删）")
        page.click('#taskSort button[data-sort="added"]')
        page.wait_for_timeout(1400)
        check("文件夹能切回按添加时间", seg_state(page, "#taskSort")[0] == "added")
        page.click('#taskView button[data-view="flat"]')
        page.wait_for_timeout(1200)

        print("\n---------- 5. 文件管理排序 ----------")
        page.click('[data-tab="files"]')
        page.wait_for_selector("#fileBody tr", timeout=15000)
        page.wait_for_timeout(800)
        # 根目录下只有文件夹，先进一个有 strm 的目录
        page.click("#fileBody span.dir[data-dir]")
        page.wait_for_selector("#fileBody tr[data-path]", timeout=15000)
        page.wait_for_timeout(800)
        key, order = seg_state(page, "#fileSort")
        check("文件管理默认按添加时间", [key, order] == ["added", "desc"], f"{key}/{order}")

        def file_names():
            return page.eval_on_selector_all(
                "#fileBody tr[data-path]", "els => els.map(e => e.dataset.path)")

        listing = api("/api/files?sort=added&order=desc&path="
                      + urllib.parse.quote(page.evaluate("() => S.files.path")))
        order_from_api = [f["path"] for f in listing["files"]]
        check("页面顺序与接口返回一致", file_names() == order_from_api,
              f"{len(order_from_api)} 个文件")
        mtimes = [f["mtime"] for f in listing["files"]]
        check("接口返回确实是新→旧", mtimes == sorted(mtimes, reverse=True), str(mtimes[:4]))

        page.click('#fileSort button[data-sort="name"]')
        page.wait_for_timeout(1000)
        state = seg_state(page, "#fileSort")
        check("点名称切到正序", list(state) == ["name", "asc"], str(state))
        names = file_names()
        check("名称正序", names == sorted(names, key=lambda s: s.rsplit("\\", 1)[-1].lower()),
              str([n.rsplit("\\", 1)[-1] for n in names[:4]]))

        print("\n---------- 6. 扫描进度字段 ----------")
        st = api("/api/status")
        check("状态接口带 scanned 字段", "scanned" in st, str(list(st)[:8]))
        check("空闲时 scanned 是数字", isinstance(st.get("scanned"), (int, float)))

        check("没有 JS 报错", not errors, "；".join(errors[:2]))
        browser.close()

    print(f"\n===== 失败 {len(FAIL)} 项 =====" if FAIL else "\n===== 全部通过 =====")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
