# -*- coding: utf-8 -*-
"""排序竞态回归：页面刚加载时点排序，不能被先发出的旧请求盖回去。

真实库里 /api/items 要扫几千行，页面初始加载的请求往往比点击后发出的请求慢，
旧响应后到就会把新排序覆盖掉——表现是「按钮变了、列表没变，再点一次才好」。

先 `python tools/devserver.py` 起环境，再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:6670"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as resp:
        return json.load(resp)


def base_name(path: str) -> str:
    return path.replace("/", "\\").rsplit("\\", 1)[-1]


async def main_async() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        calls = {"n": 0}

        async def slow_first(route):
            # 只拖慢第一个请求：模拟「初始加载还没回来，用户已经点了排序」
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(0.9)
            await route.continue_()

        await page.route("**/api/items?*", slow_first)
        await page.goto(BASE + "/", wait_until="domcontentloaded")
        await page.wait_for_timeout(200)
        size = int(await page.evaluate("() => S.size"))
        by_name = [i["path"] for i in api(
            f"/api/items?size={size}&sort=name&order=asc")["items"]]
        if len(by_name) < 3:
            print("测试数据太少（至少 3 条），先跑一轮任务")
            await browser.close()
            return 1

        print("\n---------- 1. 初始请求未返回时点「名称」 ----------")
        await page.click('#taskSort button[data-sort="name"]')
        await page.wait_for_timeout(2600)
        rows = await page.eval_on_selector_all(
            "#taskBody tr[data-path]", "els => els.map(e => e.dataset.path)")
        check("按钮切到名称正序",
              await page.eval_on_selector("#taskSort button.on", "e => e.dataset.sort + e.textContent")
              == "name名称 ↑")
        check("列表没被旧响应盖回去", rows == by_name, str([base_name(r) for r in rows[:2]]))

        print("\n---------- 2. 连点三次不同排序 ----------")
        for key in ("added", "name", "updated"):
            await page.click(f'#taskSort button[data-sort="{key}"]')
        await page.wait_for_timeout(2000)
        key = await page.eval_on_selector("#taskSort button.on", "e => e.dataset.sort")
        expect = [i["path"] for i in api(
            f"/api/items?size={size}&sort={key}&order=desc")["items"]]
        rows = await page.eval_on_selector_all(
            "#taskBody tr[data-path]", "els => els.map(e => e.dataset.path)")
        check("连点后停在最后一次点击的排序上", key == "updated", key)
        check("连点后列表与接口一致", rows == expect, str([base_name(r) for r in rows[:2]]))

        check("没有 JS 报错", not errors, "; ".join(errors[:2]))
        await browser.close()

    print("\n" + ("===== 全部通过 =====" if not FAIL else f"===== 失败 {len(FAIL)} 项 ====="))
    for name in FAIL:
        print("  -", name)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
