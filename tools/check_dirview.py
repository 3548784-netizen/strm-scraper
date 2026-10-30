# -*- coding: utf-8 -*-
"""「按文件夹」视图回归：展开/收起时滚动位置是否稳定、大文件夹展开是否卡顿。

用法：
    python tools/devserver.py --media-root <带大量 strm 的目录> --data .devdata_perf --port 6671
    python tools/check_dirview.py --base http://127.0.0.1:6671

判据：
- 展开/收起中间的分组后，视野里那一行相对窗口的位置偏移 ≤ 2px；
- 反复展开/收起 3 轮后偏移仍然 ≤ 2px，滚动位置不归零；
- 大文件夹展开：第一屏行出现 ≤ 400ms，全部行出现 ≤ 3s，期间无 >150ms 长任务。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return json.load(r)


def click_group(page, path: str) -> None:
    page.evaluate("""(path) => {
      const btn = [...document.querySelectorAll('#taskBody .gbtn')]
        .find(b => b.dataset.toggle === path);
      if (btn) btn.click();
    }""", path)


def group_rows(page) -> int:
    return page.evaluate("() => document.querySelectorAll('#taskBody tr.gitem').length")


def marker_offset(page, group_path: str):
    """某个分组第一行相对滚动容器顶边的偏移，用来判断视野有没有动。"""
    return page.evaluate("""(path) => {
      const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
        .find(r => r.dataset.group === path);
      const wrap = document.querySelector('.tablewrap');
      if (!row || !wrap) return null;
      return {
        delta: Math.round(row.getBoundingClientRect().top - wrap.getBoundingClientRect().top),
        scroll: Math.round(wrap.scrollTop),
      };
    }""", group_path)


def expand_group(page, path: str) -> dict:
    """点开一个分组，返回浏览器内测到的「首屏出现 / 全部铺完 / 长任务」数据。"""
    page.evaluate("""() => {
      window.__long = [];
      if (window.__po) window.__po.disconnect();
      window.__po = new PerformanceObserver(list => {
        for (const e of list.getEntries()) window.__long.push(Math.round(e.duration));
      });
      try { window.__po.observe({ entryTypes: ['longtask'] }); } catch (_) {}
    }""")
    return page.evaluate("""async (path) => {
      const tb = document.querySelector('#taskBody');
      const btn = [...document.querySelectorAll('#taskBody .gbtn')]
        .find(b => b.dataset.toggle === path);
      const before = tb.querySelectorAll('tr.gitem').length;
      const t0 = performance.now();
      let first = null;
      btn.click();
      await new Promise(res => {
        const obs = new MutationObserver(() => {
          const n = tb.querySelectorAll('tr.gitem').length;
          if (first === null && n > before) first = performance.now() - t0;
          const row = [...tb.querySelectorAll('tr.grouprow')].find(r => r.dataset.group === path);
          const hint = row && row.nextElementSibling && row.nextElementSibling.textContent;
          if (n > before + 150 || (hint || '').includes('只显示')) {
            setTimeout(() => { obs.disconnect(); res(); }, 60);
          }
        });
        obs.observe(tb, { childList: true, subtree: true });
        setTimeout(() => { obs.disconnect(); res(); }, 8000);
      });
      return { first: first === null ? -1 : Math.round(first),
               all: Math.round(performance.now() - t0),
               rows: tb.querySelectorAll('tr.gitem').length - before };
    }""", path)



def is_open(page, path: str) -> bool:
    return page.evaluate("""(path) => {
      const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
        .find(r => r.dataset.group === path);
      return !!row && row.classList.contains('gopen');
    }""", path)


def set_open(page, path: str, want: bool, settle: int = 700) -> None:
    """只在状态不符时才点，避免把"本来就展开"的组点成收起。"""
    if is_open(page, path) == want:
        return
    click_group(page, path)
    page.wait_for_timeout(settle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6671")
    ap.add_argument("--big", default="", help="大文件夹路径，默认自动选条数最多的")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    groups = api(base, "/api/items/groups?limit=400")["groups"]
    groups.sort(key=lambda g: -g["total"])
    big = next((g for g in groups if g["path"] == args.big), groups[0])
    mid = [g for g in groups if 10 <= g["total"] <= 60]
    print(f"数据：{len(groups)} 个文件夹；大组「{big['name']}」{big['total']} 条；"
          f"中等组 {len(mid)} 个")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1500, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(700)
        if page.query_selector("#username"):
            page.fill("#username", "admin")
            page.fill("#password", "admin")
            page.click("#btn")
            page.wait_for_timeout(1200)
        page.click('[data-tab="tasks"]')
        page.wait_for_timeout(1000)
        page.click('#taskView button[data-view="dir"]')
        page.wait_for_timeout(1500)

        print("\n---------- 1. 大文件夹展开耗时 ----------")
        set_open(page, big["path"], False)          # 先确保是收起状态
        first_open = expand_group(page, big["path"])
        print(f"  首屏 {first_open['first']} ms，全部 {first_open['all']} ms，"
              f"{first_open['rows']} 行")
        check("首屏行出现够快（≤400ms）", 0 <= first_open["first"] <= 400, f"{first_open['first']} ms")
        check("全部行铺完（≤3s）", 0 < first_open["all"] <= 3000, f"{first_open['all']} ms")
        check("大文件夹行数到位", first_open["rows"] >= 150, f"{first_open['rows']} 行")
        page.wait_for_timeout(500)

        longs = page.evaluate("() => window.__long || []")
        worst = max(longs) if longs else 0
        check("展开期间没有明显长任务（<150ms）", worst < 150, f"最长 {worst} ms，共 {len(longs)} 个")

        print("\n---------- 2. 收起中间的组：视野是否钉住 ----------")
        # 展开几个中等组，把页面撑长，然后滚到中间
        opened = []
        for g in mid[:6]:
            click_group(page, g["path"])
            page.wait_for_timeout(350)
            opened.append(g["path"])
        page.wait_for_timeout(600)
        anchor = mid[4]["path"]          # 拿第 5 个组当参照物
        page.evaluate("""(path) => {
          const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
            .find(r => r.dataset.group === path);
          row.scrollIntoView({ block: 'start' });
        }""", anchor)
        page.wait_for_timeout(300)

        before = marker_offset(page, anchor)
        for cycle in range(3):
            target = mid[1]["path"] if cycle % 2 == 0 else mid[2]["path"]
            click_group(page, target)                 # 收起（在上方）
            page.wait_for_timeout(450)
            mid_state = marker_offset(page, anchor)
            click_group(page, target)                 # 再展开回来
            page.wait_for_timeout(700)
            after = marker_offset(page, anchor)
            drift = abs(after["delta"] - before["delta"])
            print(f"  第 {cycle+1} 轮：收起后 {mid_state['delta']}px / 展开后 {after['delta']}px"
                  f"（基准 {before['delta']}px），scroll {before['scroll']} → {after['scroll']}")
            check(f"第 {cycle+1} 轮收起后视野没跳", abs(mid_state["delta"] - before["delta"]) <= 2,
                  f"偏移 {mid_state['delta'] - before['delta']}px")
            check(f"第 {cycle+1} 轮展开后视野没跳", drift <= 2, f"偏移 {drift}px")
            check(f"第 {cycle+1} 轮滚动位置没被清零", after["scroll"] > 100, str(after["scroll"]))

        print("\n---------- 3. 反复点同一组 6 次 ----------")
        for i in range(6):
            click_group(page, mid[3]["path"])
            page.wait_for_timeout(320)
            state = marker_offset(page, anchor)
            check(f"第 {i+1} 次切换后参照行没动", abs(state["delta"] - before["delta"]) <= 2,
                  f"{state['delta']}px")
            check(f"第 {i+1} 次切换后滚动位置保持", state["scroll"] > 100, str(state["scroll"]))

        print("\n---------- 4. 展开大组不影响其它组 ----------")
        set_open(page, big["path"], True, settle=1200)
        state = marker_offset(page, anchor)
        check("大组展开后参照行仍在原位", abs(state["delta"] - before["delta"]) <= 2,
              f"{state['delta']}px（基准 {before['delta']}px）")
        # 大组在下方展开，视野不该被拽走
        check("大组展开后滚动位置保持", state["scroll"] > 100, str(state["scroll"]))

        print("\n---------- 5. 大组开着时点小分组：不该整表重建 ----------")
        set_open(page, big["path"], True, settle=1200)
        marked = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#taskBody tr.gitem')];
          const row = rows[Math.floor(rows.length / 2)];
          window.__keep = row;
          return rows.length;
        }""")
        click_group(page, mid[6]["path"])
        page.wait_for_timeout(900)
        alive = page.evaluate("() => !!(window.__keep && window.__keep.isConnected)")
        check("别的分组里的行没有被重建", alive, f"大组 {marked} 行，引用{'仍在' if alive else '已失效'}")
        check("小分组展开成功", is_open(page, mid[6]["path"]))
        set_open(page, mid[6]["path"], False)

        print("\n---------- 6. 收起全部后仍在可用状态 ----------")
        for path in opened + [big["path"]]:
            set_open(page, path, False, settle=250)
        page.wait_for_timeout(400)
        check("全部收起后没有残留明细行", group_rows(page) == 0, f"{group_rows(page)} 行")
        check("无 JS 报错", not errors, "; ".join(errors[:2]))
        browser.close()

    print(f"\n========== 失败 {len(FAIL)} 项 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
