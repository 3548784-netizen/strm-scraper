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


def _partial(text: str) -> bool:
    """文件夹的中间态文案是「已刮 3/5」。"""
    return text.startswith("已刮 ") and "/" in text


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
        # 角标数量要和真实分布一致（含文件夹），文件行数只算文件
        counts = page.evaluate("""() => {
          const all = (S.files.data && S.files.data.files) || [];
          const files = { all: all.length, ok: 0, new: 0, failed: 0 };
          all.forEach(f => { const k = fileStateOf(f); if (k in files) files[k] += 1; });
          const badge = fileStateCounts();
          const shown = {};
          [...document.querySelectorAll('#fileState button[data-fstate]')].forEach(b => {
            shown[b.dataset.fstate] = Number((b.querySelector('.n') || {}).textContent);
          });
          return { files, badge, shown };
        }""")
        check("按钮上的数量与真实分布一致", counts["badge"] == counts["shown"],
              f"{counts['badge']} vs {counts['shown']}")
        check("角标数量含文件夹", counts["badge"]["all"] >= counts["files"]["all"],
              f"全部 {counts['badge']['all']} / 文件 {counts['files']['all']}")

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
                  all(g == text for g in got) and len(got) == counts["files"][key],
                  f"{len(got)} 行：{sorted(set(got))}")
            active = page.eval_on_selector_all("#fileState button.on", "e => e.map(x => x.dataset.fstate)")
            check(f"「{text}」是当前选中项", active == [key], str(active))

        # 空结果要给出可读的说明，而不是一片空白
        page.click('#fileState button[data-fstate="failed"]')
        page.wait_for_timeout(400)
        empty_tip = page.text_content("#fileBody") if not visible() else ""
        if empty_tip:
            check("没有匹配时给出提示", "当前筛选下没有内容" in empty_tip, empty_tip.strip()[:30])

        # 筛选下全选：只该勾中看得见的那些。挑一个有内容的状态来测，
        # 别赌「未刮削」一定存在——库里全刮完之后这一档就是 0 个，
        # 列表空着，全选框也没得点，断言会变成在测空页面。
        fstate = "new" if counts["files"]["new"] else "ok"
        page.click(f'#fileState button[data-fstate="{fstate}"]')
        page.wait_for_timeout(400)
        page.check("#fileAll")
        page.wait_for_timeout(600)
        picked = page.evaluate("() => S.files.sel.size")
        check("筛选后全选只勾中筛出来的", picked == counts["files"][fstate],
              f"已选 {picked} / {fstate} {counts['files'][fstate]}")
        page.uncheck("#fileAll")
        page.wait_for_timeout(500)
        check("取消全选能清干净", page.evaluate("() => S.files.sel.size") == 0,
              str(page.evaluate("() => S.files.sel.size")))

        page.click('#fileState button[data-fstate="all"]')
        page.wait_for_timeout(450)
        check("切回全部恢复完整列表", len(visible()) == counts["files"]["all"], f"{len(visible())} 行")

        print("\n---------- 5. 文件夹状态 ----------")
        # 回到根目录：文件夹行只在这一层才有
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        page.click('[data-tab="files"]')
        page.wait_for_timeout(1200)

        dirs = page.evaluate("""() => {
          const data = (S.files.data && S.files.data.dirs) || [];
          const rows = [...document.querySelectorAll('#fileBody tr.dirrow')];
          return {
            data: data.map(d => ({ path: d.path, count: d.count, scraped: d.scraped,
                                   st: dirStateOf(d), text: dirStateText(d) })),
            dom: rows.map(tr => {
              const st = tr.querySelector('span.st');
              return {
                name: tr.querySelector('span.dir')?.textContent.trim() || '',
                cnt: tr.querySelector('.cnt')?.textContent.trim() || '',
                cls: st?.className || '', text: st?.textContent.trim() || '',
                title: st?.title || '',
              };
            }),
          };
        }""")
        check("根目录有文件夹行", len(dirs["dom"]) > 0, f"{len(dirs['dom'])} 行")
        check("每个文件夹都有状态", all(d["text"] for d in dirs["dom"]),
              str([d["name"] for d in dirs["dom"] if not d["text"]]))
        check("文案只有四种写法",
              all(d["text"] == "刮削成功" or d["text"] == "未刮削" or d["text"] == "无 strm"
                  or (d["text"].startswith("已刮 ") and "/" in d["text"]) for d in dirs["dom"]),
              str(sorted({d["text"] for d in dirs["dom"]})))
        want_cls = {"刮削成功": "ok", "未刮削": "new", "无 strm": "empty"}
        check("类名与文案对得上", all(
            d["cls"] == "st dirst " + (want_cls.get(d["text"]) or "part") for d in dirs["dom"]),
            str([(d["text"], d["cls"]) for d in dirs["dom"]][:3]))
        check("进度文案里的总数与文件夹计数一致", all(
            d["cnt"] == f"{x['count']} 个 strm"
            for d, x in zip(dirs["dom"], dirs["data"])), "逐行比对")
        check("已刮数不超过总数", all(0 <= x["scraped"] <= x["count"] for x in dirs["data"]),
              str([(x["count"], x["scraped"]) for x in dirs["data"]][:3]))
        check("悬停提示给出总数与已刮数",
              all("含子目录" in d["title"] and "已刮" in d["title"] for d in dirs["dom"]
                  if d["text"] != "无 strm"),
              dirs["dom"][0]["title"] if dirs["dom"] else "")

        def dirs_shown():
            return page.eval_on_selector_all("#fileBody tr.dirrow", """els => els.map(e => ({
              text: e.querySelector('span.st').textContent.trim(),
              scraped: e.querySelector('span.cnt') ? 1 : 0 }))""")

        # 根目录下只有文件夹：角标不能是 0，否则看起来像没东西可筛
        root_badge = page.evaluate("""() => ({
          counts: fileStateCounts(),
          dirs: (S.files.data.dirs || []).length,
          newDirs: (S.files.data.dirs || []).filter(d => ['new', 'part'].includes(dirStateOf(d))).length,
        })""")
        check("根目录角标把文件夹算进去", root_badge["counts"]["all"] == root_badge["dirs"],
              f"全部 {root_badge['counts']['all']} / 文件夹 {root_badge['dirs']}")
        check("未刮削角标 = 还有活的文件夹数",
              root_badge["counts"]["new"] == root_badge["newDirs"],
              f"{root_badge['counts']['new']} / {root_badge['newDirs']}")

        # 筛选要连文件夹一起过滤：筛「未刮削」时留下的文件夹里必须还有没刮的
        page.click('#fileState button[data-fstate="new"]')
        page.wait_for_timeout(450)
        got = dirs_shown()
        check("筛「未刮削」只留还有没刮的文件夹",
              all(g["text"] == "未刮削" or _partial(g["text"]) for g in got),
              str(sorted({g["text"] for g in got})))
        page.click('#fileState button[data-fstate="ok"]')
        page.wait_for_timeout(450)
        got = dirs_shown()
        check("筛「刮削成功」只留全刮完的文件夹",
              all(g["text"] == "刮削成功" for g in got),
              str(sorted({g["text"] for g in got})))
        page.click('#fileState button[data-fstate="failed"]')
        page.wait_for_timeout(450)
        check("筛「失败」不显示文件夹（失败只看文件）", len(dirs_shown()) == 0,
              f"{len(dirs_shown())} 行")
        page.click('#fileState button[data-fstate="all"]')
        page.wait_for_timeout(450)
        check("切回全部恢复所有文件夹", len(dirs_shown()) == len(dirs["dom"]),
              f"{len(dirs_shown())} / {len(dirs['dom'])}")

        print("\n---------- 6. 无 JS 报错 ----------")
        check("没有 JS 报错", not errors, "；".join(errors[:2]))
        browser.close()

    print(f"\n===== 失败 {len(FAIL)} 项 =====" if FAIL else "\n===== 全部通过 =====")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
