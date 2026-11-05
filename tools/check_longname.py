# -*- coding: utf-8 -*-
"""长文件名布局回归：名字再长也不许把右侧列挤出视口，也不许砍掉全名。

背景：以前表格是 auto layout，文件夹行的长目录名会把「名称」列撑到 900+px，
整表宽度跟着涨到 1400+px，右侧「大小 / 海报 / 背景图 / NFO / 状态 / 操作」
必须横向拖滚动条才看得到；而名称本身又被 max-width + ellipsis 截掉一截。
现在改成固定列宽 + 名称折行，这个脚本把两条都钉死。

    python tools/check_longname.py --base http://127.0.0.1:6672

只读：只在浏览器里往 tbody 追加几行假数据，不请求写接口、不动磁盘。
退出码非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
DESKTOP = [(1500, 950), (1280, 800), (1100, 760), (1024, 700)]
MOBILE = [(760, 820), (480, 820)]

LONG_CN = ("国产极品TS 华华 国产人气TS，靓滑粉B时装美身材高挑，原本是作为线下的Coser，"
           "最近开始做频道，更新作品整合版，合集 第01集 1080p 无码 完整版.mp4")
LONG_NOSPACE = "SuperLongReleaseGroup-Name-Without-Any-Space-" + "A" * 90 + ".mkv"

# 追加一行文件 / 一行文件夹（任务页换成分组行），覆盖三种名称容器
INJECT = """([long, tab]) => {
  const body = document.querySelector(tab === 'tasks' ? '#taskBody' : '#fileBody');
  const table = body.closest('table');
  const n = table.querySelectorAll('thead th').length;
  body.querySelectorAll('tr[data-fake]').forEach(r => r.remove());
  const ni = tab === 'tasks' ? 2 : 1;
  const mk = (markup) => {
    const tr = document.createElement('tr');
    tr.dataset.fake = '1';
    tr.dataset.path = '/fake/' + long;
    tr.innerHTML = '<td>—</td>'.repeat(n);
    body.appendChild(tr);
    return tr;
  };
  const file = mk();
  file.cells[ni].innerHTML = `<div class="name" title="${long}">${long}</div>`;
  // 备注 + 行尾三按钮：顺带验这两列不会被折行的名字挤坏
  file.cells[n - 2].innerHTML =
    '<span class="reason" title="测试">ffmpeg 抽帧失败：找不到视频流</span>';
  file.cells[n - 1].innerHTML = '<div class="rowbtns">' +
    '<button class="tiny" data-act="pick">选帧</button>' +
    '<button class="tiny" data-act="redo">重刮</button>' +
    '<button class="tiny danger" data-act="clean">删产物</button></div>';
  if (tab === 'files') {
    const dir = mk();
    dir.className = 'dirrow';
    dir.cells[1].innerHTML = `<div class="fwrap"><span class="fcaret blank"></span>` +
      `<span class="dir" title="进入 ${long}">${long}</span>` +
      `<span class="cnt">1 个视频 · 含 2 个子目录</span></div>`;
  } else {
    const grp = document.createElement('tr');
    grp.dataset.fake = '1';
    grp.dataset.path = '/fake/' + long;
    grp.className = 'grouprow';
    grp.innerHTML = `<td>—</td><td>—</td><td colspan="${n - 2}">` +
      `<div class="gwrap"><button class="gbtn">` +
      `<span class="gname" title="${long}">${long}</span></button></div></td>`;
    body.appendChild(grp);
  }
}"""

PROBE = """(tab) => {
  const pane = document.querySelector(`.pane[data-pane="${tab}"]`);
  const wrap = pane.querySelector('.tablewrap');
  const table = wrap.querySelector('table');
  const rows = [...table.querySelectorAll('tr[data-fake]')];
  const el = (sel) => rows.map(r => r.querySelector(sel)).filter(Boolean);
  const shape = (e) => {
    const cs = getComputedStyle(e);
    return {
      w: Math.round(e.getBoundingClientRect().width),
      h: Math.round(e.getBoundingClientRect().height),
      sw: e.scrollWidth, cw: e.clientWidth,
      lh: parseFloat(cs.lineHeight),
      ws: cs.whiteSpace,
      clipped: e.scrollWidth > e.clientWidth + 1,
      title: e.getAttribute('title') || '',
      text: e.textContent.trim()
    };
  };
  const lastTd = rows[0].cells[rows[0].cells.length - 1];
  const btn = lastTd.querySelector('.rowbtns');
  const br = btn && btn.getBoundingClientRect();
  const tr = lastTd.getBoundingClientRect();
  return {
    vw: innerWidth,
    layout: getComputedStyle(table).tableLayout,
    wrapCW: wrap.clientWidth, wrapSW: wrap.scrollWidth,
    nameColW: Math.round(table.querySelector('thead th.c-name').getBoundingClientRect().width),
    names: el('.name').map(shape),
    dirs: el('.dir').map(shape),
    gnames: el('.gname').map(shape),
    badCells: rows.flatMap(r => [...r.cells].map((td, i) => ({i,
      over: td.scrollWidth > td.clientWidth + 1}))).filter(c => c.over),
    actsRight: br ? Math.round(br.right) : null,
    actsTdRight: Math.round(tr.right),
    actsOver: br ? br.right > tr.right + 1 : null,
    cols: table.querySelectorAll('thead th').length
  };
}"""


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAIL.append(name)


def verify_one(pg, tab: str, width: int, long: str, mobile: bool) -> None:
    tag = f"{width}px {tab} {'无空格' if long == LONG_NOSPACE else '中文长名'}"
    pg.evaluate(INJECT, [long, tab])
    r = pg.evaluate(PROBE, tab)

    check(f"{tag} · 表格是固定列宽", r["layout"] == "fixed", r["layout"])
    if not mobile:
        check(f"{tag} · 整表不横向滚动", r["wrapSW"] <= r["wrapCW"] + 1,
              f'{r["wrapSW"]} / {r["wrapCW"]}')
        check(f"{tag} · 行尾操作按钮在视口内", r["actsRight"] is not None
              and r["actsRight"] <= width + 1, f'right {r["actsRight"]}')
    else:
        # 手机端保留表格容器内部滚动，滚到底要够得到行尾按钮
        after = pg.evaluate("""(tab) => {
          const w = document.querySelector(`.pane[data-pane="${tab}"] .tablewrap`);
          w.scrollLeft = w.scrollWidth;
          const b = w.querySelector('tr[data-fake] .rowbtns');
          return {scrollable: w.scrollWidth > w.clientWidth + 1,
                  right: b ? Math.round(b.getBoundingClientRect().right) : null,
                  vw: innerWidth};
        }""", tab)
        check(f"{tag} · 表内可滚动且行尾按钮可达",
              after["scrollable"] and after["right"] is not None
              and after["right"] <= after["vw"] + 1, str(after))
        pg.evaluate("""(tab) => {
          document.querySelector(`.pane[data-pane="${tab}"] .tablewrap`).scrollLeft = 0;
        }""", tab)

    nm = r["names"][0] if r["names"] else None
    check(f"{tag} · 名称全名完整可读（无横向裁切）",
          bool(nm) and not nm["clipped"] and nm["ws"] == "normal" and nm["text"] == long,
          str(nm)[:120] if nm else "没找到 .name")
    check(f"{tag} · 名称确实折行而非被压成一行",
          bool(nm) and nm["h"] > nm["lh"] * 1.5,
          f'h {nm["h"]} / lh {nm["lh"]}' if nm else "-")
    check(f"{tag} · 名称 title 保留完整名", bool(nm) and long in nm["title"], "")
    # 文件夹行 / 分组行同样不许截断
    others = r["dirs"] if tab == "files" else r["gnames"]
    label = "文件夹名" if tab == "files" else "分组名"
    check(f"{tag} · {label}完整可读（无横向裁切）",
          len(others) == 1 and not others[0]["clipped"]
          and others[0]["text"].startswith(long[:10]),
          str(others[0])[:110] if others else "没找到")
    check(f"{tag} · 没有单元格内容溢出到邻列", not r["badCells"] and not r["actsOver"],
          str(r["badCells"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6672")
    args = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        for width, height in DESKTOP + MOBILE:
            mobile = (width, height) in MOBILE
            pg = b.new_page(viewport={"width": width, "height": height})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)[:140]))
            pg.goto(args.base, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(450)
            print(f"\n---------- {width}px ----------")
            for tab in ("tasks", "files"):
                pg.click(f'[data-tab="{tab}"]')
                pg.wait_for_timeout(300)
                for long in (LONG_CN, LONG_NOSPACE):
                    verify_one(pg, tab, width, long, mobile)
            check(f"{width}px 页面没有 JS 报错", not errs, "; ".join(errs[:2]))
            pg.close()
        b.close()
    print(f"\n结果：{len(FAIL)} 项失败")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
