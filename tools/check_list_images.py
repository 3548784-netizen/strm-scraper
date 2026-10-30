# -*- coding: utf-8 -*-
"""列表缩略图回归：任务列表走小图，预览走原图。

先 `python tools/devserver.py` 起环境，再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import re
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:6670"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        hits: list[tuple[str, int]] = []

        def on_response(resp):
            if "/api/files/image" in resp.url:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.url).query)
                w = int((q.get("w") or ["0"])[0])
                try:
                    length = int(resp.header_value("content-length") or 0)
                except Exception:
                    length = 0
                hits.append((resp.url, w, length))

        page.on("response", on_response)

        print("========== 列表缩略图 ==========")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        if page.query_selector("#username"):
            page.fill("#username", "admin")
            page.fill("#password", "admin")
            page.click("#btn")
            page.wait_for_timeout(1500)

        page.click('[data-tab="tasks"]')
        page.wait_for_timeout(2500)

        rows = page.query_selector_all("#taskBody tr[data-path]")
        check("任务列表有行", len(rows) > 0, f"{len(rows)} 行")

        thumbs = page.query_selector_all("#taskBody img.thumb")
        check("列表里有缩略图", len(thumbs) > 0, f"{len(thumbs)} 张")
        srcs = [t.get_attribute("src") or "" for t in thumbs]
        with_w = [s for s in srcs if re.search(r"[?&]w=\d+", s)]
        check("列表图片都带 w 参数（走小图）", len(with_w) == len(srcs), f"{len(with_w)}/{len(srcs)}")
        widths = {int(re.search(r"[?&]w=(\d+)", s).group(1)) for s in with_w}
        check("宽度是小图尺寸（96/144）", widths <= {96, 144}, str(sorted(widths)))

        # 等图片真的加载出来（naturalWidth > 0）
        page.wait_for_timeout(1200)
        loaded = page.evaluate(
            "() => [...document.querySelectorAll('#taskBody img.thumb')]"
            ".filter(i => i.naturalWidth > 0).length")
        check("缩略图已实际渲染", loaded > 0, f"{loaded} 张已加载")

        small_bytes = [n for _, w, n in hits if w and n]
        full_bytes = [n for _, w, n in hits if not w and n]
        if small_bytes:
            avg = sum(small_bytes) / len(small_bytes)
            check("单张缩略图体积 < 40KB", avg < 40 * 1024, f"平均 {avg/1024:.1f} KB")
        check("列表阶段没有拉原图", not full_bytes,
              f"{len(full_bytes)} 次原图请求" if full_bytes else "0 次")

        # 点开预览应当走原图（不带 w）
        first = thumbs[0]
        first.click()
        page.wait_for_timeout(1200)
        shown = page.eval_on_selector("#previewImg", "i => i.getAttribute('src') || ''")
        check("预览用原图（不带 w）", "w=" not in shown, shown[-60:])
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # 刷新后仍走小图
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        srcs2 = [t.get_attribute("src") or "" for t in page.query_selector_all("#taskBody img.thumb")]
        check("刷新后仍走小图", bool(srcs2) and all("w=" in s for s in srcs2), f"{len(srcs2)} 张")

        check("无 JS 报错", not errors, "; ".join(errors[:2]))
        browser.close()

    print(f"========== 失败 {len(FAIL)} 项 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
