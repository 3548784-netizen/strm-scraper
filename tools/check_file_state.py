# -*- coding: utf-8 -*-
"""文件管理「状态」列回归：应该按产物在不在判，而不是照抄任务历史。

任务里同一个文件可能是 ok（这次刮的）或 skipped（产物已存在，跳过），
对使用者来说都等于「刮过了」。这个脚本直接调页面里的 fileStateOf 覆盖各种组合。

先 `python tools/devserver.py` 起环境，再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


ART_NONE = {"nfo": 0, "poster": 0, "fanart": 0, "thumb": 0}


def case(status: str, **arts) -> dict:
    return {"status": status, "artifacts": {**ART_NONE, **arts}}


# (说明, 文件数据, 期望状态键, 期望文案)
CASES = [
    ("没记录也没产物 → 未刮削", case("new"), "new", "未刮削"),
    ("有记录 ok 但产物全没了 → 未刮削", case("ok"), "new", "未刮削"),
    ("skipped 又没产物（异常库）→ 未刮削", case("skipped"), "new", "未刮削"),
    ("这次刮成功且有海报 → 刮削成功", case("ok", poster=20480, fanart=40960, nfo=900),
     "ok", "刮削成功"),
    ("任务里是 skipped，产物齐全 → 刮削成功", case("skipped", poster=20480, fanart=40960, nfo=900),
     "ok", "刮削成功"),
    ("只有 NFO 也算刮过 → 刮削成功", case("skipped", nfo=900), "ok", "刮削成功"),
    ("只有背景图也算刮过 → 刮削成功", case("new", fanart=40960), "ok", "刮削成功"),
    ("这次失败 → 刮削失败（即使有旧产物）",
     case("failed", poster=20480, fanart=40960, nfo=900), "failed", "刮削失败"),
    ("正在跑 → 刮削中（优先于产物判断）", case("running", poster=20480), "running", "刮削中"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(args.base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.click('[data-tab="files"]')
        page.wait_for_selector("#fileBody tr", timeout=15000)
        page.wait_for_timeout(600)

        print("\n---------- 1. 状态判定 ----------")
        for label, data, want_key, want_text in CASES:
            got_key = page.evaluate("(f) => fileStateOf(f)", data)
            got_text = page.evaluate("(k) => FILE_STATE_TEXT[k] || ''", got_key)
            check(label, [got_key, got_text] == [want_key, want_text],
                  f"{got_key}/{got_text}")

        print("\n---------- 2. 悬停提示 ----------")
        hint = page.evaluate("(f) => fileStateHint(f)",
                             case("skipped", poster=1, nfo=2))
        check("提示里列出三类产物", all(k in hint for k in ("海报", "背景图", "NFO")), hint)
        check("有产物时不写「已跳过」", "已跳过" not in hint, hint)

        print("\n---------- 3. 真实列表一致性 ----------")
        # 默认停在根目录（只有子目录），先进一个里面有 strm 的目录
        if not page.query_selector("#fileBody tr[data-path]"):
            page.evaluate("""() => {
              const row = [...document.querySelectorAll('#fileBody tr')]
                .find(tr => /[1-9]\\d* 个 strm/.test(tr.textContent));
              row?.querySelector('span.dir')?.click();
            }""")
            page.wait_for_timeout(1500)
        rows = page.evaluate("""() => {
          return [...document.querySelectorAll('#fileBody tr[data-path]')].map(tr => {
            const st = tr.querySelector('span.st');
            return { id: tr.dataset.path, cls: st?.className || '', text: st?.textContent || '',
                     hasArt: !!tr.querySelector('img.thumb') };
          });
        }""")
        if len(rows) >= 1:
            check("每行都有状态标签", all(r["text"] for r in rows), f"{len(rows)} 行")
            check("文字只出现在约定集合里",
                  all(r["text"] in ("未刮削", "刮削成功", "刮削失败", "刮削中") for r in rows),
                  str(sorted({r["text"] for r in rows})))
            check("类名与文案对得上",
                  all(r["cls"] == "st " + {"未刮削": "new", "刮削成功": "ok",
                                           "刮削失败": "failed", "刮削中": "running"}[r["text"]]
                      for r in rows))
        else:
            print("  ..   当前目录没有 strm 文件，跳过列表比对")

        print("\n---------- 4. 状态筛选 ----------")
        # 统计按钮上的数字要和当前目录真实分布一致
        counts = page.evaluate("""() => {
          const all = (S.files.data && S.files.data.files) || [];
          const real = { all: all.length, ok: 0, new: 0, failed: 0 };
          all.forEach(f => { const k = fileStateOf(f); if (k in real) real[k] += 1; });
          const shown = {};
          [...document.querySelectorAll('#fileState button[data-fstate]')].forEach(b => {
            shown[b.dataset.fstate] = Number((b.querySelector('.n') || {}).textContent);
          });
          return { real, shown };
        }""")
        check("按钮上的数量与真实分布一致", counts["real"] == counts["shown"],
              f"{counts['real']} vs {counts['shown']}")

        def visible():
            return page.eval_on_selector_all(
                "#fileBody tr[data-path]",
                "els => els.map(e => e.querySelector('span.st').textContent)")

        want = {"new": "未刮削", "ok": "刮削成功", "failed": "刮削失败"}
        for key, text in want.items():
            page.click(f'#fileState button[data-fstate="{key}"]')
            page.wait_for_timeout(450)
            got = visible()
            check(f"筛「{text}」只剩这一类",
                  all(g == text for g in got) and len(got) == counts["real"][key],
                  f"{len(got)} 行：{sorted(set(got))}")
            active = page.eval_on_selector_all("#fileState button.on", "e => e.map(x => x.dataset.fstate)")
            check(f"「{text}」是当前选中项", active == [key], str(active))

        # 空结果要给出可读的说明，而不是一片空白
        page.click('#fileState button[data-fstate="failed"]')
        page.wait_for_timeout(400)
        empty_tip = page.text_content("#fileBody") if not visible() else ""
        if empty_tip:
            check("没有匹配时给出提示", "当前筛选下没有文件" in empty_tip, empty_tip.strip()[:30])

        # 筛选下全选：只该勾中看得见的那些
        page.click('#fileState button[data-fstate="new"]')
        page.wait_for_timeout(400)
        page.check("#fileAll")
        page.wait_for_timeout(600)
        picked = page.evaluate("() => S.files.sel.size")
        check("筛选后全选只勾中筛出来的", picked == counts["real"]["new"],
              f"已选 {picked} / 未刮削 {counts['real']['new']}")
        page.uncheck("#fileAll")
        page.wait_for_timeout(500)
        check("取消全选能清干净", page.evaluate("() => S.files.sel.size") == 0,
              str(page.evaluate("() => S.files.sel.size")))

        page.click('#fileState button[data-fstate="all"]')
        page.wait_for_timeout(450)
        check("切回全部恢复完整列表", len(visible()) == counts["real"]["all"], f"{len(visible())} 行")

        print("\n---------- 5. 无 JS 报错 ----------")
        check("没有 JS 报错", not errors, "；".join(errors[:2]))
        browser.close()

    print(f"\n===== 失败 {len(FAIL)} 项 =====" if FAIL else "\n===== 全部通过 =====")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
