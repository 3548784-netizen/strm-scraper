# -*- coding: utf-8 -*-
"""任务列表按库显示：顶上一条库名横条，和文件管理里那条是同一个部件。

以前任务列表把「媒体库」画成表格里的第一行 —— 一个叫 media 的文件夹，带勾选框、
状态、进度条；而文件管理里同一个东西是顶上的一条标题。同一个库两个地方两种说法。

现在两边一致：任务列表最上面也是那条横条（库名 + 「媒体库」标签），下面直接就是
这个库里的文件夹行；有多个媒体库时横条变成一排可点的库标签，点谁看谁。

覆盖：
1. 横条在工具栏上方，写着库名 + 「媒体库」标签，只有一个库时不给切换入口；
2. 列表里不再有库根那一行，第一行是库里的第一层文件夹，缩进从 0 开始；
3. 横条上的库名和文件管理面包屑里的库名是同一个（两处一个说法）；
4. 接口口径：``/api/items/tree`` 返回 root + libs，``?root=`` 换库；
   ``/api/items?root=`` 只统计这个库的记录；
5. 多个库时（``--multi``）标签可点、切换后列表跟着换、按文件视图也一起换；
6. 全程没有 JS 报错。

    python tools/devserver.py --port 6672 &
    python tools/check_lib_bar.py --base http://127.0.0.1:6672

    # 双库环境（设置里配两个扫描根）
    python tools/check_lib_bar.py --base http://127.0.0.1:6678 --multi
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAIL.append(name)


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6672")
    ap.add_argument("--multi", action="store_true", help="这个环境配了两个以上媒体库")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    tree = get_json(base, "/api/items/tree?limit=400")
    libs = tree.get("libs") or []
    root = tree.get("root") or ""

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2600)

        print("\n---------- 1. 顶上的库名横条 ----------")
        bar = page.evaluate("""() => {
            const el = document.querySelector('#taskLibBar');
            const pane = document.querySelector('.pane[data-pane="tasks"]');
            const tools = pane.querySelector('.toolbar');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            const tab = el.querySelector('.libtab.on');
            return {
                hidden: el.hidden,
                height: Math.round(r.height),
                above: r.bottom <= tools.getBoundingClientRect().top + 1,
                name: (el.querySelector('.libname') || {}).textContent
                      || (tab ? tab.firstChild.textContent : '') || '',
                tags: [...el.querySelectorAll('.taglib')].map(t => t.textContent.trim()),
                tabs: el.querySelectorAll('.libtab').length,
                bg: getComputedStyle(el).backgroundColor,
            };
        }""")
        check("横条存在且没被隐藏", bool(bar) and not bar["hidden"], str(bar and bar["height"]))
        check("横条在工具栏上方（和文件管理同一个位置）", bool(bar) and bar["above"],
              f"y 关系 {bool(bar) and bar['above']}")
        check("横条写着库名", bool(bar) and bool(bar["name"].strip()), bar["name"] if bar else "")
        check("库名和接口给的 root 是同一个库",
              bool(bar) and root.split("\\")[-1].split("/")[-1] == bar["name"].strip(),
              f'{bar["name"] if bar else ""} vs {root}')
        if not args.multi:
            check("横条带「媒体库」标签", bool(bar) and bar["tags"] == ["媒体库"],
                  str(bar["tags"] if bar else ""))
            check("只有一个库时不给切换标签", bool(bar) and bar["tabs"] == 0, f'{bar["tabs"]} 个')
        else:
            check("多个库时横条是一排库标签", bool(bar) and bar["tabs"] >= 2, f'{bar["tabs"]} 个')
        check("横条有自己的底色（不是透明）", bool(bar) and bar["bg"] not in ("", "rgba(0, 0, 0, 0)"),
              bar["bg"] if bar else "")

        print("\n---------- 2. 列表里不再有库根那一行 ----------")
        rows = page.evaluate("""() => {
            const rows = [...document.querySelectorAll('#taskBody tr.grouprow')];
            return rows.map(r => ({
                path: r.dataset.group,
                name: (r.querySelector('.gname') || {}).textContent || '',
                ind: parseInt(getComputedStyle(r.querySelector('.gwrap'))
                     .getPropertyValue('--ind')) || 0,
                tag: !!r.querySelector('.taglib'),
                caret: !!r.querySelector('.gcaret'),
            }));
        }""")
        check("列表不是空的", len(rows) >= 1, f"{len(rows)} 行")
        check("第一行不是库根", all(r["path"] != root for r in rows),
              rows[0]["path"] if rows else "—")
        check("库名不在列表里重复出现", all(r["name"] != (bar["name"].strip() if bar else "") for r in rows),
              str([r["name"] for r in rows][:3]))
        check("列表里没有「媒体库」标签（标签只在横条上）",
              all(not r["tag"] for r in rows), str([r["tag"] for r in rows][:3]))
        check("第一层缩进是 0", bool(rows) and rows[0]["ind"] == 0,
              str([r["ind"] for r in rows]))
        tops = tree["nodes"][0]["children"] if tree.get("nodes") else []
        check("行数和这个库的第一层文件夹数对得上", len(rows) == len(tops) and len(tops) >= 1,
              f"{len(rows)} 行 / 第一层 {len(tops)} 个")
        check("文件夹行还能展开（三角在）", all(r["caret"] for r in rows),
              str([r["caret"] for r in rows][:3]))

        print("\n---------- 2.5 默认一层都不展开 ----------")
        opened = page.evaluate("() => document.querySelectorAll('#taskBody tr.grouprow.gopen').length")
        items = page.evaluate("() => document.querySelectorAll('#taskBody tr.gitem').length")
        check("进来时没有展开的文件夹", opened == 0, f"{opened} 个展开")
        check("进来时不列任何文件行", items == 0, f"{items} 行文件")
        check("每个文件夹都收着（箭头是 ▶）", page.evaluate(
            "() => S.openNodes.size") == 0, f"openNodes={page.evaluate('() => S.openNodes.size')}")

        # 点开第一层：下一层露出来，缩进跟着递进，文件要等到点开那一层才出现
        page.click("#taskBody tr.grouprow .gbtn")
        page.wait_for_timeout(1300)
        after = page.evaluate("""() => [...document.querySelectorAll('#taskBody tr.grouprow')].map(r => ({
            name: (r.querySelector('.gname') || {}).textContent.trim(),
            ind: parseInt(getComputedStyle(r.querySelector('.gwrap'))
                 .getPropertyValue('--ind')) || 0,
            open: r.classList.contains('gopen'),
        }))""")
        check("点一下就展开这一层", len(after) >= len(rows), f"{len(rows)} → {len(after)} 行")
        check("展开的那一行标成 open", bool(after) and after[0]["open"], str(after[:1]))
        check("父层缩进仍是 0", bool(after) and after[0]["ind"] == 0, str(after[:1]))

        page.click("#btnCollapseAll")
        page.wait_for_timeout(600)
        check("收起全部回到第一层", page.evaluate(
            "() => document.querySelectorAll('#taskBody tr.grouprow').length") == len(tops),
            f"{len(tops)} 个第一层文件夹")

        print("\n---------- 3. 和文件管理是同一个说法 ----------")
        page.click('[data-tab="files"]')
        page.wait_for_timeout(1600)
        crumb = page.inner_text("#crumbs .crumb.last").strip()
        tags = page.eval_on_selector_all("#crumbs .taglib", "els => els.map(e => e.textContent.trim())")
        check("文件管理面包屑的库名和横条一致",
              crumb == (bar["name"].strip() if bar else ""), f"{crumb} vs {bar['name'] if bar else ''}")
        if not args.multi:
            check("文件管理同一个位置也挂「媒体库」标签", tags == ["媒体库"], str(tags))
        page.click('[data-tab="tasks"]')
        page.wait_for_timeout(1200)

        print("\n---------- 4. 接口口径 ----------")
        check("tree 返回当前库的 root", bool(root), root)
        check("tree 返回媒体库列表", len(libs) >= 1, f"{len(libs)} 个库")
        first = libs[0]
        check("libs[0] 就是默认停在的那个库", first["path"] == root, first["path"])
        check("libs 带条数 / 文件夹数", first.get("total", -1) >= 0 and first.get("dirs", -1) >= 0,
              f'{first.get("total")} 条 / {first.get("dirs")} 个文件夹')
        scoped = get_json(base, "/api/items/tree?limit=400&root=" + urllib.parse.quote(root))
        check("指定 root 后只返回这个库", scoped["root"] == root and scoped["total"] <= tree["total"],
              f'{scoped["total"]} / {tree["total"]}')
        flat = get_json(base, "/api/items?size=1&root=" + urllib.parse.quote(root))
        allflat = get_json(base, "/api/items?size=1")
        check("平铺视图也能按库过滤", flat["total"] <= allflat["total"],
              f'{flat["total"]} / {allflat["total"]}')
        check("按库过滤的总数就是这个库的条数", flat["total"] == first["total"],
              f'{flat["total"]} vs {first["total"]}')

        if args.multi:
            print("\n---------- 5. 多个库：横条就是切换入口 ----------")
            tabs = page.eval_on_selector_all(
                "#taskLibBar .libtab",
                "els => els.map(e => ({path: e.dataset.lib, text: e.textContent.trim(), on: e.classList.contains('on')}))")
            check("每个库一个标签", len(tabs) == len(libs), f"{len(tabs)} 个标签 / {len(libs)} 个库")
            check("只有一个标签是选中态", sum(1 for t in tabs if t["on"]) == 1,
                  str([t["text"] for t in tabs if t["on"]]))
            check("标签上带这个库的条数", all(t["text"].strip() != "" for t in tabs),
                  str([t["text"] for t in tabs]))
            other = next((t for t in tabs if t["path"] != root), None)
            check("有第二个库可切", bool(other), str(other))
            if other:
                before = page.eval_on_selector_all(
                    "#taskBody tr.grouprow", "els => els.map(e => e.dataset.group)")
                page.evaluate("""(path) => {
                    const tab = [...document.querySelectorAll('#taskLibBar .libtab')]
                        .find(b => b.dataset.lib === path);
                    if (tab) tab.click();
                }""", other["path"])
                page.wait_for_timeout(2200)
                after = page.eval_on_selector_all(
                    "#taskBody tr.grouprow", "els => els.map(e => e.dataset.group)")
                check("切换后列表换成了另一个库", before != after, f"{len(before)} → {len(after)} 行")
                check("切换后行都在新库下面",
                      all(a.startswith(other["path"]) for a in after), str(after[:2]))
                on = page.eval_on_selector_all(
                    "#taskLibBar .libtab.on", "els => els.map(e => e.dataset.lib)")
                check("切换后选中态跟着过去", on == [other["path"]], str(on))
                flat_n = page.evaluate("""async () => {
                    const r = await fetch('/api/items?size=1&root=' + encodeURIComponent(
                        document.querySelector('#taskLibBar .libtab.on').dataset.lib));
                    return (await r.json()).total;
                }""")
                page.click('#taskView button[data-view="flat"]')
                page.wait_for_timeout(1800)
                shown = page.evaluate("() => document.querySelectorAll('#taskBody tr[data-path]').length")
                check("按文件视图也只列这个库的", 0 < shown <= max(flat_n, 1),
                      f"{shown} 行 / 共 {flat_n} 条")
                page.click('#taskView button[data-view="dir"]')
                page.wait_for_timeout(1500)

        print("\n---------- 6. 无 JS 报错 ----------")
        check("没有报错", not errors, "; ".join(errors[:2]))
        browser.close()

    print(f"\n========== 失败 {len(FAIL)} 项 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
