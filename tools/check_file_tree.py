# -*- coding: utf-8 -*-
"""文件管理的目录树回归：就地展开、缩进、箭头、和任务列表口径一致。

文件管理以前是「进目录 + 面包屑」单层浏览，层级只能靠点进去才看得见；
任务列表那边早就改成目录树了，同一个库两个页面对层级的表达不一致。
现在文件夹行加了三角箭头，点箭头就地展开下一层，缩进 / 连接线 / 计数文案
都跟任务列表一套，点文件夹名字仍然是进入那一层。

本脚本覆盖：
1. 接口给的 ``subs``（直接子目录数）和实际目录一致；
2. 只有真有子目录的层才显示箭头，没子目录的留空位（箭头列不会参差）；
3. 点箭头 → 缩进 20px、行数增加、箭头转成朝下；再点一次原样收起；
4. 收起父层时，它下面展开过的孙层一起收掉，不留孤儿行；
5. 点文件夹名字仍然是进入目录，面包屑跟着变，展开状态清零；
6. 展开后勾选子层，勾的是它自己的 strm（和顶层勾选同一套逻辑）；
7. 状态筛选同时作用在子层上；
8. 全程没有 JS 报错。

自带素材：在 ``.devdata/media/目录树测试`` 下造三层结构，跑完删掉。

    python tools/devserver.py --port 6672 &
    python tools/check_file_tree.py --base http://127.0.0.1:6672
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
HERE = Path(__file__).resolve().parent.parent
FAKE_STRM = "http://127.0.0.1:5244/d/115/%E7%94%B5%E5%BD%B1/%E5%BD%B1%E7%89%87.mkv"

# 三层：根下 2 个 strm + 3 个文件夹，其中一个文件夹再往下两层
PLAN = {
    "": 2,
    "第一层": 2,
    "第一层/第二层": 3,
    "第一层/第二层/第三层": 1,
    "只有文件": 1,
    "没子目录": 2,
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def build(base_dir: Path) -> None:
    shutil.rmtree(base_dir, ignore_errors=True)
    for rel, count in PLAN.items():
        folder = base_dir / rel if rel else base_dir
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (folder / f"{rel.split('/')[-1] or '根'}{i + 1:02d}.strm").write_text(
                FAKE_STRM, encoding="utf-8")


def cleanup(base_dir: Path) -> None:
    shutil.rmtree(base_dir, ignore_errors=True)


def get(base: str, path: str = "") -> dict:
    url = base + "/api/files"
    if path:
        url += "?path=" + urllib.parse.quote(path)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def rows_state(page) -> list[dict]:
    """把当前 tbody 里的文件夹行摊成 [{name, indent, open, nest, subs}]。"""
    return page.evaluate("""() => [...document.querySelectorAll('#fileBody tr.dirrow')].map(tr => {
        const wrap = tr.querySelector('.fwrap');
        const caret = tr.querySelector('.fcaret');
        return {
            name: (tr.querySelector('.dir') || {}).textContent.trim(),
            indent: parseFloat(getComputedStyle(wrap).paddingLeft) || 0,
            nest: wrap.classList.contains('nest'),
            open: tr.classList.contains('fopen'),
            hasCaret: !!(caret && caret.tagName === 'BUTTON'),
            caretBlank: !!(caret && caret.classList.contains('blank')),
            cnt: (tr.querySelector('.cnt') || {}).textContent.trim(),
        };
    })""")


def click_caret(page, tail: str) -> bool:
    btns = page.locator('#fileBody button[data-subtoggle]')
    for i in range(btns.count()):
        if (btns.nth(i).get_attribute("data-subtoggle") or "").endswith(tail):
            btns.nth(i).click()
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # 素材建在服务端认为的媒体根下面，不猜 --data 目录叫什么
    root = Path(get(base).get("path") or "")
    if not root.is_dir():
        print(f"拿不到媒体根目录（服务端返回 {root}）")
        return 2
    base_dir = root / "目录树测试"

    build(base_dir)
    try:
        data = get(base, str(base_dir))
        assert data.get("ok"), data.get("message")
        by_name = {d["name"]: d for d in data["dirs"]}

        print("== 1. 接口给出直接子目录数 ==")
        check("第一层 subs=1", by_name.get("第一层", {}).get("subs") == 1,
              str(by_name.get("第一层")))
        check("没子目录的层 subs=0", by_name.get("没子目录", {}).get("subs") == 0)
        check("只有文件的层 subs=0", by_name.get("只有文件", {}).get("subs") == 0)
        check("根层 2 个 strm", len(data["files"]) == 2, str(len(data["files"])))

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base, wait_until="networkidle")
            page.wait_for_timeout(700)
            page.click('[data-tab="files"]')
            page.wait_for_timeout(600)
            page.evaluate(
                """(path) => { S.files.path = path; S.files.sig = ''; return loadFiles(true); }""",
                str(base_dir))
            page.wait_for_timeout(1200)

            print("== 2. 只有真有子目录的层才显示箭头 ==")
            rows = rows_state(page)
            d = {r["name"]: r for r in rows}
            check("第一层有箭头", d.get("第一层", {}).get("hasCaret") is True, str(d.get("第一层")))
            check("没子目录的层没有箭头但有占位",
                  d.get("没子目录", {}).get("hasCaret") is False
                  and d.get("没子目录", {}).get("caretBlank") is True)
            check("初始全部收起", all(not r["open"] for r in rows))
            check("初始缩进都是 0", all(r["indent"] == 0 for r in rows),
                  str([r["indent"] for r in rows]))
            check("顶层的计数带「含 N 个子目录」", "含 1 个子目录" in d.get("第一层", {}).get("cnt", ""),
                  d.get("第一层", {}).get("cnt"))
            check("没有子目录的层不写子目录数",
                  "子目录" not in d.get("没子目录", {}).get("cnt", ""), d.get("没子目录", {}).get("cnt"))

            print("== 3. 点箭头就地展开一层 ==")
            before = len(rows)
            check("找到第一层的箭头", click_caret(page, "第一层"))
            page.wait_for_timeout(1500)
            rows = rows_state(page)
            check("行数变多", len(rows) == before + 1, f"{before} → {len(rows)}")
            d = {r["name"]: r for r in rows}
            check("第一层变成展开态", d.get("第一层", {}).get("open") is True)
            check("子层缩进 20px", d.get("第二层", {}).get("indent") == 20,
                  str(d.get("第二层", {}).get("indent")))
            check("子层带连接线", d.get("第二层", {}).get("nest") is True)
            check("父层还在原位（缩进 0）", d.get("第一层", {}).get("indent") == 0)

            print("== 4. 再展开一层，缩进继续递进 ==")
            check("找到第二层的箭头", click_caret(page, "第二层"))
            page.wait_for_timeout(1500)
            rows = rows_state(page)
            d = {r["name"]: r for r in rows}
            check("第三层缩进 40px", d.get("第三层", {}).get("indent") == 40,
                  str(d.get("第三层", {}).get("indent")))
            check("第三层没有子目录，不显示箭头", d.get("第三层", {}).get("hasCaret") is False)

            print("== 5. 收起父层，孙层一起收掉 ==")
            click_caret(page, "第一层")
            page.wait_for_timeout(1000)
            rows = rows_state(page)
            names = [r["name"] for r in rows]
            check("回到收起状态", len(rows) == before, f"{len(rows)} 行")
            check("孙层没有残留", "第三层" not in names and "第二层" not in names, str(names))

            print("== 6. 展开后勾选子层 ==")
            click_caret(page, "第一层")
            page.wait_for_timeout(1400)
            boxes = page.locator('#fileBody input[data-dirpick]')
            target = None
            for i in range(boxes.count()):
                path = boxes.nth(i).get_attribute("data-dirpick") or ""
                if path.endswith("第二层"):
                    boxes.nth(i).check()
                    target = boxes.nth(i)
                    break
            page.wait_for_timeout(1500)
            sel = page.evaluate("() => S.files.sel.size")
            # 第二层自己 3 个 + 第三层 1 个（勾文件夹是递归的）
            check("勾子层选中的是它那一支", sel == 4, f"已选 {sel}")
            check("勾上后行变成选中态",
                  target is not None
                  and "sel" in (target.evaluate("e => e.closest('tr').className") or ""))

            print("== 7. 点文件夹名字仍然是进入那一层 ==")
            # 点明确的「第一层」，别靠「第一行」—— 同级目录按时间排序，
            # 时间粒度相同的话先后是不稳定的
            page.evaluate("""() => {
                const tr = [...document.querySelectorAll('#fileBody tr.dirrow')]
                  .find(r => ((r.querySelector('.dir') || {}).textContent || '').trim() === '第一层');
                if (tr) tr.querySelector('.dir').click();
            }""")
            page.wait_for_timeout(1600)
            check("面包屑进到了第一层", "第一层" in page.inner_text("#crumbs"),
                  page.inner_text("#crumbs").replace("\n", ""))
            check("展开状态清空", page.evaluate("() => S.files.subOpen.size") == 0)
            check("子层缓存清空", page.evaluate("() => S.files.sub.size") == 0)
            rows = rows_state(page)
            check("新的一层从头收起", all(not r["open"] for r in rows))

            print("== 8. 筛选作用在子层上 ==")
            page.evaluate(
                """(p) => { S.files.path = p; S.files.sig = ''; return loadFiles(true); }""",
                str(base_dir))
            page.wait_for_timeout(1200)
            click_caret(page, "第一层")
            page.wait_for_timeout(1400)
            page.click('#fileState button[data-fstate="new"]')
            page.wait_for_timeout(800)
            rows = rows_state(page)
            names = [r["name"] for r in rows]
            # 素材没有任何产物，全部都是「未刮削」，所以筛完还是全部——这里只验证
            # 筛选不会把展开的层级拍平、也不会崩
            check("筛选后仍然是树状（父层还在）", "第一层" in names, str(names))
            check("筛选后缩进没丢",
                  all(r["indent"] in (0, 20, 40) for r in rows), str([r["indent"] for r in rows]))
            page.click('#fileState button[data-fstate="all"]')
            page.wait_for_timeout(600)

            check("全程没有 JS 报错", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        cleanup(base_dir)

    print()
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for name in FAIL:
            print("  -", name)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
