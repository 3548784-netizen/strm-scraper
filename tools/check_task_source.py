# -*- coding: utf-8 -*-
"""任务列表按提交来源呈现的回归：提交文件显示文件行，提交文件夹显示文件夹行。

    python tools/check_task_source.py --base http://127.0.0.1:6687

界面上不再有「按文件 / 按文件夹」切换控件；列表形态由最近一次提交的选择决定，
并记在 localStorage 里，刷新后保持。

只读保证：拦截 /api/files/scrape 返回模拟成功，不真的启动刮削、不写任何产物；
其余请求照常走（/api/files/expand 只是展开目录读文件，也是只读）。
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FAIL: list[str] = []
OK = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} :: {detail}")


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return json.load(r)


def state(pg) -> str:
    return pg.evaluate("() => S.tasksView")


def rows(pg) -> dict:
    return pg.evaluate("""() => ({
      groups: document.querySelectorAll('#taskBody tr.grouprow').length,
      files: document.querySelectorAll('#taskBody tr[data-path]').length,
      total: document.querySelectorAll('#taskBody tr').length,
    })""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6687")
    base = ap.parse_args().base.rstrip("/")
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    print("\n---------- 1. 源码层：不再有手动视图切换 ----------")
    check("HTML 里没有 #taskView 切换控件", 'id="taskView"' not in src)
    check("没有 data-view 按钮与它的点击监听",
          "data-view=" not in src and "$('taskView').addEventListener" not in src)
    check("提交时按 dirPaths 判断选的是文件还是文件夹",
          "const byDir = S.files.dirPaths.size > 0" in src)
    check("两种来源分别映射到文件行 / 文件夹行",
          "setTaskView(byDir ? 'dir' : 'flat')" in src)
    check("来源写进本地偏好，刷新后仍保持",
          "savePrefs({ taskSource:" in src and "PREFS.taskSource" in src)

    # 找一个真实文件路径和一个真实目录路径，只用来填选择状态，不改动它们
    j = get(base, "/api/files")
    file_path = ""
    dir_path = j.get("dirs", [{}])[0].get("path", "") if j.get("dirs") else ""
    if j.get("files"):
        file_path = j["files"][0]["path"]
    if not file_path and dir_path:
        k = get(base, "/api/files?path=" + urllib.parse.quote(dir_path))
        if k.get("files"):
            file_path = k["files"][0]["path"]
    if not file_path or not dir_path:
        print("  夹具不足：需要一个同时含文件和子目录的媒体库")
        return 1

    print("\n---------- 2. 页面层：切换控件已移除 ----------")
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        errors: list[str] = []
        writes: list[str] = []
        scraped: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("request", lambda r: writes.append(f"{r.method} {r.url}")
              if r.method not in ("GET", "HEAD", "OPTIONS")
              and "/api/files/expand" not in r.url
              and "/api/files/scrape" not in r.url else None)

        def fake_scrape(route):
            scraped.append(route.request.post_data or "")
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "started": True,
                                           "skipped": 0, "message": "模拟提交成功"}))

        pg.route("**/api/files/scrape", fake_scrape)
        pg.goto(base + "/?tab=files", wait_until="domcontentloaded", timeout=30000)
        pg.evaluate("() => localStorage.removeItem('strm-scraper.prefs.v1')")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1400)

        check("页面上不存在 #taskView", pg.locator("#taskView").count() == 0)
        check("不存在任何 data-view 按钮", pg.locator("button[data-view]").count() == 0)
        toolbar = pg.locator(".taskbar").inner_text()
        check("工具条不再出现「按文件 / 按文件夹」字样",
              "按文件" not in toolbar and "按文件夹" not in toolbar, toolbar[:120])
        check("默认显示文件夹行", state(pg) == "dir", state(pg))
        pg.click('[data-tab="tasks"]')
        pg.wait_for_timeout(1400)
        d0 = rows(pg)
        check("默认首屏确实是目录树分组行", d0["groups"] > 0, str(d0))

        # 用页面内部状态精确模拟「只勾文件」/「勾了文件夹」，再走真实提交流程
        def submit(by_dir: bool) -> None:
            pg.click('[data-tab="files"]')
            pg.wait_for_timeout(500)
            pg.evaluate("""([f, d, byDir]) => {
              S.files.sel = new Set([f]);
              S.files.dirSel = new Set();
              S.files.dirPaths = new Map(byDir ? [[d, [f]]] : []);
              updateFileSelInfo();
            }""", [file_path, dir_path, by_dir])
            pg.evaluate("() => scrapeSelected(false)")
            pg.wait_for_timeout(900)

        print("\n---------- 3. 提交文件 → 显示文件行 ----------")
        submit(False)
        check("只选文件提交后切到文件行", state(pg) == "flat", state(pg))
        check("偏好记成 taskSource=file",
              pg.evaluate("() => JSON.parse(localStorage.getItem('strm-scraper.prefs.v1')).taskSource") == "file")
        pg.click('[data-tab="tasks"]')
        pg.wait_for_timeout(1400)
        f1 = rows(pg)
        check("任务列表只有文件行、没有文件夹分组行",
              f1["groups"] == 0 and f1["files"] > 0, str(f1))
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1400)
        check("刷新后仍是文件行", state(pg) == "flat", state(pg))
        check("刷新后列表仍无分组行", rows(pg)["groups"] == 0, str(rows(pg)))

        print("\n---------- 4. 提交文件夹 → 显示文件夹行 ----------")
        submit(True)
        check("选文件夹提交后切到文件夹行", state(pg) == "dir", state(pg))
        check("偏好记成 taskSource=dir",
              pg.evaluate("() => JSON.parse(localStorage.getItem('strm-scraper.prefs.v1')).taskSource") == "dir")
        check("刚选的文件夹被置顶标出",
              pg.evaluate("() => S.pinnedDirs.size") == 1)
        pg.click('[data-tab="tasks"]')
        pg.wait_for_timeout(1500)
        d1 = rows(pg)
        check("任务列表出现文件夹分组行", d1["groups"] > 0, str(d1))
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1400)
        check("刷新后仍是文件夹行", state(pg) == "dir", state(pg))
        check("刷新后仍有分组行", rows(pg)["groups"] > 0, str(rows(pg)))

        print("\n---------- 5. 零写入与报错 ----------")
        check("两次提交都被拦截、没有真的启动刮削",
              len(scraped) == 2, str(len(scraped)))
        check("没有发出其它写请求", not writes, str(writes[:3]))
        check("整轮无 JS 报错", not errors, str(errors[:3]))
        b.close()

    print(f"\n结果：{len(FAIL)} 项失败，{OK} 项通过")
    for name in FAIL:
        print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
