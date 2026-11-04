# -*- coding: utf-8 -*-
"""媒体库切换：任务列表、文件管理、自动化三处都能挑库。

三处以前各说各话：任务列表顶上一条「media 媒体库」写着库名，但只有一个库时
那行字是死的；文件管理的面包屑里同样一行字，只能在库里面上下走，出不去；自动化
更绕——想只刮某个库，得先进库、再点「设为当前目录」。

现在统一成一个浮层：点任务列表横条上的库名（文件管理里点库名后面的小箭头），
弹出配置里的全部媒体库，写清楚名字、条数和完整路径；自动化那边的「监控范围」是
一个长得像下拉的按钮，打开的是同一个浮层 —— 第一项「全部媒体库」，之后每个库一项，
选完就写进任务的 scope（原生 <select> 在带毛玻璃的弹窗里会弹到窗口左上角，不能用）。

覆盖：
1. 任务列表：横条可点 → 弹出全部库（名字 + 条数 + 路径）；点另一个库，列表跟着换；
2. 文件管理：面包屑第一个库名带箭头 → 切库后路径和列表都换，勾选清空；
   点库名本身仍然是回到库根（不弹菜单）；
3. 自动化：选择器列出「全部媒体库 + 每个库」，浮层贴着按钮弹（不跑偏）；
   选库 → 按钮/提示/保存的 scope 都是库根，卡片上写库名；选「全部媒体库」→ scope 为空；
4. 只有一个库的环境（``--single``）：菜单照样能开，但不给切换项，写明只有一个；
5. 全程没有 JS 报错。

    # 双库环境
    python tools/check_lib_switch.py --base http://127.0.0.1:6678
    # 顺便验一下单库环境
    python tools/check_lib_switch.py --base http://127.0.0.1:6678 --single http://127.0.0.1:6672
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAIL.append(name)


def call(base: str, path: str, payload: dict | None = None) -> dict:
    if payload is None:
        with urllib.request.urlopen(base + path, timeout=20) as resp:
            return json.loads(resp.read())
    req = urllib.request.Request(
        base + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def leaf(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").split("/")[-1]


def open_menu(page) -> None:
    """点任务列表横条上的库名。"""
    page.click("#taskLibBar [data-libmenu]")
    page.wait_for_selector("#libMenu:not([hidden])", timeout=5000)


def menu_items(page) -> list[dict]:
    return page.eval_on_selector_all(
        "#libMenu button[data-lib]",
        "els => els.map(e => ({path: e.dataset.lib, text: e.innerText, on: e.classList.contains('on')}))")


def pick_in_menu(page, path: str) -> None:
    # 路径里有反斜杠，写不进 CSS 属性选择器，用 JS 直接点
    page.evaluate("""(p) => {
        const b = [...document.querySelectorAll('#libMenu button[data-lib]')]
            .find(x => x.dataset.lib === p);
        if (b) b.click();
    }""", path)


def part_tasks(page, base: str, libs: list[dict]) -> None:
    print("\n---------- 1. 任务列表：点库名切换 ----------")
    page.click('[data-tab="tasks"]')
    page.wait_for_timeout(1200)

    bar = page.inner_text("#taskLibBar")
    check("横条写着当前库名", leaf(libs[0]["path"]) in bar or "媒体库" in bar, bar.replace("\n", " "))
    check("横条上带这个库的条数", str(libs[0]["total"]) in bar, bar.replace("\n", " "))
    check("横条可点（有切换入口）", page.locator("#taskLibBar [data-libmenu]").count() == 1)

    before = page.eval_on_selector_all(
        "#taskBody tr.grouprow", "els => els.map(e => e.dataset.group)")
    open_menu(page)
    items = menu_items(page)
    check("菜单列出全部媒体库", len(items) == len(libs),
          f'{len(items)} 项 / {len(libs)} 个库')
    check("每一项都写完整路径", all(len(i["text"]) > len(leaf(i["path"])) for i in items),
          str([i["text"].replace("\n", " ")[:40] for i in items]))
    check("当前库标成选中", sum(1 for i in items if i["on"]) == 1,
          str([leaf(i["path"]) for i in items if i["on"]]))
    check("菜单写明一共几个库", f'{len(libs)} 个媒体库' in page.inner_text("#libMenu"),
          page.inner_text("#libMenu").replace("\n", " ")[-40:])

    other = next((i for i in items if not i["on"]), None)
    if other is None:
        check(False, "有第二个库可切", "只有 1 个库，切不了")
        return
    pick_in_menu(page, other["path"])
    page.wait_for_timeout(2200)
    check("选完菜单关掉", not page.is_visible("#libMenu"))
    after = page.eval_on_selector_all(
        "#taskBody tr.grouprow", "els => els.map(e => e.dataset.group)")
    check("列表换成了另一个库", before != after, f"{len(before)} → {len(after)} 行")
    check("行都在新库下面", all(a.startswith(other["path"]) for a in after), str(after[:2]))
    bar = page.inner_text("#taskLibBar")
    check("横条跟着换成新库名", leaf(other["path"]) in bar, bar.replace("\n", " "))


def part_files(page, libs: list[dict]) -> None:
    print("\n---------- 2. 文件管理：面包屑上的库名 ----------")
    page.click('[data-tab="files"]')
    page.wait_for_timeout(1800)

    check("第一个面包屑是库名", page.locator("#crumbs .crumb.libcrumb").count() == 1)
    check("库名后面有切换箭头", page.locator("#crumbs .caret[data-libmenu]").count() == 1)

    # 点库名本身：还是回到库根，不该弹菜单
    page.click("#crumbs .crumb.libcrumb", position={"x": 6, "y": 6})
    page.wait_for_timeout(1200)
    check("点库名本身不弹菜单", not page.is_visible("#libMenu"))
    at_root = page.evaluate("() => S.files.path")
    check("点库名回到库根", Path(at_root).name == leaf(at_root), at_root)

    before_rows = page.eval_on_selector_all("#fileBody tr[data-dir]", "els => els.length")
    page.click("#crumbs .caret[data-libmenu]")
    page.wait_for_selector("#libMenu:not([hidden])", timeout=5000)
    items = menu_items(page)
    check("文件管理的菜单也列出全部库", len(items) == len(libs), f"{len(items)} 项")
    other = next((i for i in items if not i["on"]), None)
    check("有另一个库可切", other is not None, str(other))
    if other is None:
        return
    pick_in_menu(page, other["path"])
    page.wait_for_timeout(2200)
    now = page.evaluate("() => S.files.path")
    check("切库后路径就是那个库的根", now == other["path"], now)
    crumbs = page.inner_text("#crumbs").replace("\n", " ")
    check("面包屑换成新库名", leaf(other["path"]) in crumbs, crumbs)
    dirs = page.eval_on_selector_all(
        "#fileBody tr[data-dir]", "els => els.map(e => e.dataset.dir)")
    # 列表里要么是空目录，要么全是新库下面的目录
    check("列表里全是新库的目录",
          all(d.startswith(other["path"]) for d in dirs),
          str([leaf(d) for d in dirs[:3]]))
    check("切库后勾选清空", page.evaluate("() => S.files.sel.size") == 0)
    check("有切库提示", "已切到媒体库" in page.inner_text("#toast"),
          page.inner_text("#toast"))


def part_auto(page, base: str, libs: list[dict]) -> None:
    print("\n---------- 3. 自动化：监控范围可选库 ----------")
    for task in call(base, "/api/automation").get("tasks") or []:
        call(base, "/api/automation/delete", {"id": task["id"]})

    page.click('[data-tab="automation"]')
    page.wait_for_timeout(1000)
    page.click("#btnAutoAdd")
    page.wait_for_selector("#autoModal:not([hidden])", timeout=8000)
    page.wait_for_timeout(800)

    # 监控范围不是原生 <select>，是按钮 + 共用浮层
    page.click("#autoLib")
    page.wait_for_selector("#libMenu:not([hidden])", timeout=5000)
    items = menu_items(page)
    check("第一项是全部媒体库", bool(items) and items[0]["path"] == ""
          and "全部媒体库" in items[0]["text"], str(items[:1]))
    lib_items = [i for i in items if i["path"]]
    check("每个库各占一项", len(lib_items) == len(libs),
          f"{len(lib_items)} 项 / {len(libs)} 个库")
    check("选项写的是库名 + 完整路径",
          all(leaf(i["path"]) in i["text"] and len(i["text"]) > len(leaf(i["path"]))
              for i in lib_items),
          str([i["text"].replace("\n", " ")[:40] for i in lib_items[:2]]))

    # 这条是这次修的病根：原生弹层在带毛玻璃的弹窗里会跳到窗口左上角
    box = page.evaluate("""() => {
        const m = document.getElementById('libMenu').getBoundingClientRect();
        const a = document.getElementById('autoLib').getBoundingClientRect();
        return {mleft: m.left, mtop: m.top, aleft: a.left, abottom: a.bottom,
                aright: a.right, vw: innerWidth, vh: innerHeight};
    }""")
    check("浮层贴着选择器弹出，没有跑到窗口角落",
          abs(box["mleft"] - box["aleft"]) < 24
          and box["abottom"] - 20 <= box["mtop"] <= box["abottom"] + 20
          and 0 < box["mleft"] < box["vw"] - 100 and 0 < box["mtop"] < box["vh"] - 40,
          str({k: round(v) for k, v in box.items()}))

    # 按钮在弹窗里会跟着滚：浮层是钉死在原地的，滚完不收起就会悬在别处
    page.evaluate("""() => {
        const b = document.querySelector('.auto-body');
        b.style.maxHeight = '220px';
    }""")
    page.wait_for_timeout(150)
    page.evaluate("""() => {
        const b = document.querySelector('.auto-body');
        b.scrollTop += 90;
    }""")
    page.wait_for_timeout(300)
    check("弹窗里一滚动浮层就收起", not page.is_visible("#libMenu"))
    page.evaluate("""() => {
        const b = document.querySelector('.auto-body');
        b.scrollTop = 0; b.style.maxHeight = '';
    }""")
    page.wait_for_timeout(200)

    page.click("#autoLib")
    page.wait_for_selector("#libMenu:not([hidden])", timeout=5000)
    target = lib_items[0]
    pick_in_menu(page, target["path"])
    page.wait_for_timeout(900)
    pick = page.inner_text("#autoLibLabel")
    check("按钮上写着选中的库", "媒体库" in pick and leaf(target["path"]) in pick, pick)
    hint = page.inner_text("#autoScopeHint")
    check("提示说明只刮整个库", "整个库" in hint, hint)
    check("浏览区跟着切到那个库", page.evaluate("() => S.auto.browse") == target["path"],
          page.evaluate("() => S.auto.browse"))

    page.fill("#autoName", "切库验收")
    page.click("#btnAutoSave")
    page.wait_for_timeout(1600)
    tasks = call(base, "/api/automation").get("tasks") or []
    row = next((t for t in tasks if t["name"] == "切库验收"), None)
    check("任务存下来了", row is not None)
    check("存下来的范围就是这个库的根", bool(row) and row["scope"] == target["path"],
          (row or {}).get("scope", ""))
    card = page.locator(".auto-card").first.inner_text().replace("\n", " ")
    check("卡片上写库名而不是长路径",
          f'媒体库 · {leaf(target["path"])}' in card, card[:120])

    # 改成「全部媒体库」：scope 应该清空
    page.locator(".auto-card").first.locator('[data-act="edit"]').click()
    page.wait_for_selector("#autoModal:not([hidden])", timeout=8000)
    page.wait_for_timeout(900)
    check("编辑时把原来的库选回来", leaf(target["path"]) in page.inner_text("#autoLibLabel"),
          page.inner_text("#autoLibLabel"))
    page.click("#autoLib")
    page.wait_for_selector("#libMenu:not([hidden])", timeout=5000)
    pick_in_menu(page, "")
    page.wait_for_timeout(600)
    check("选「全部媒体库」后按钮回到全部",
          "全部媒体库" in page.inner_text("#autoLibLabel"),
          page.inner_text("#autoLibLabel"))
    page.click("#btnAutoSave")
    page.wait_for_timeout(1500)
    tasks = call(base, "/api/automation").get("tasks") or []
    row = next((t for t in tasks if t["name"] == "切库验收"), None)
    check("保存后范围被清空成全部媒体库", bool(row) and row["scope"] == "",
          (row or {}).get("scope", ""))
    card = page.locator(".auto-card").first.inner_text().replace("\n", " ")
    check("卡片改回「全部媒体库」", "全部媒体库" in card, card[:120])

    for task in call(base, "/api/automation").get("tasks") or []:
        call(base, "/api/automation/delete", {"id": task["id"]})


def part_single(base: str) -> None:
    print("\n---------- 4. 只有一个库：菜单照样能开 ----------")
    libs = call(base, "/api/items/tree?limit=400").get("libs") or []
    check("这个环境确实只有一个库", len(libs) == 1, f"{len(libs)} 个")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:120]))
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        open_menu(page)
        items = menu_items(page)
        check("单库也能弹出列表", len(items) == 1, f"{len(items)} 项")
        check("没有别的库可点",
              sum(1 for i in items if not i["on"]) == 0, str(items))
        note = page.inner_text("#libMenu")
        check("写明只有一个媒体库、去哪加", "只有一个媒体库" in note, note.replace("\n", " ")[-40:])
        check("单库页面没有报错", not errors, "; ".join(errors[:2]))
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6678", help="多库环境")
    ap.add_argument("--single", default="", help="可选的单库环境，用来验「只有一个库」")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    libs = call(base, "/api/items/tree?limit=400").get("libs") or []
    check("这个环境配了多个媒体库", len(libs) >= 2, f"{len(libs)} 个")
    if len(libs) < 2:
        print("\n需要多库环境（设置里配两个以上扫描根）才能验切换")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text[:120])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)[:120]))
        page.on("dialog", lambda d: d.accept())
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)

        part_tasks(page, base, libs)
        part_files(page, libs)
        part_auto(page, base, libs)

        print("\n---------- 5. 无 JS 报错 ----------")
        check("没有报错", not errors, "; ".join(errors[:2]))
        browser.close()

    if args.single:
        part_single(args.single.rstrip("/"))

    print(f"\n========== 失败 {len(FAIL)} 项 ==========")
    for name in FAIL:
        print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
