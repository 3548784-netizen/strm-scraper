# -*- coding: utf-8 -*-
"""长文件名布局回归：名字再长也只能在名称列内单行截断、以省略号结尾。

背景：以前长名靠折行显示全名，行高会随名字变两三行，右侧列虽然不被挤出
视口，但行与行的基线对不齐；后来改成固定列宽 + 单行省略，这个脚本钉死：

  * 名称 / 文件夹名 / 分组名一律 white-space: nowrap + text-overflow: ellipsis；
  * 超出列宽就截断，但只允许发生在 overflow hidden 的省略容器内；
  * DOM 里的文本一字不丢（与 data-full 一致），完整名悬停 title 可见；
  * 「手选」标签、刮削标题、备注、行尾按钮不被长名挤走或遮挡；
  * 短名不截断；整表不横向滚动（手机端保留表内滚动，行尾按钮可达）。

    python tools/check_longname.py --base http://127.0.0.1:6687

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
           "最近开始做频道，更新作品整合版，合集 第01集 1080p 无码 完整版 加长版 "
           "导演剪辑版 蓝光原盘转码 高码率收藏版.mp4")
LONG_NOSPACE = "SuperLongReleaseGroup-Name-Without-Any-Space-" + "A" * 150 + ".mkv"
SHORT_CN = "预告片.mp4"

# 追加：长名文件行（带手选标签 + 刮削标题）、短名文件行、
# 一行文件夹（文件页）或一行分组（任务页），覆盖三种名称容器
INJECT = """([long, short, tab]) => {
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
  file.cells[ni].innerHTML =
    `<div class="name" title="${long}&#10;/fake/媒体库/国产剧集" data-full="${long}">` +
    `<span class="nm">${long}</span>` +
    `<span class="tag">手选 1:23</span>` +
    `<span class="path">/fake/媒体库/国产剧集/第一季</span></div>`;
  // 备注 + 行尾三按钮：顺带验这两列不会被截断的名字挤坏
  file.cells[n - 2].innerHTML =
    '<span class="reason" title="测试">ffmpeg 抽帧失败：找不到视频流</span>';
  file.cells[n - 1].innerHTML = '<div class="rowbtns">' +
    '<button class="tiny" data-act="pick">选帧</button>' +
    '<button class="tiny" data-act="redo">重刮</button>' +
    '<button class="tiny danger" data-act="clean">删产物</button></div>';
  const sho = mk();
  sho.cells[ni].innerHTML =
    `<div class="name" title="${short}" data-full="${short}"><span class="nm">${short}</span></div>`;
  if (tab === 'files') {
    const dir = mk();
    dir.className = 'dirrow';
    dir.cells[1].innerHTML =
      `<div class="fwrap"><span class="fcaret blank"></span>` +
      `<span class="dir" title="${long}&#10;进入这个文件夹" data-full="${long}">` +
      `<svg width="14" height="14" style="flex:none" viewBox="0 0 24 24"></svg>` +
      `<span class="nm">${long}</span></span>` +
      `<span class="cnt">1 个视频 · 含 2 个子目录</span></div>`;
  } else {
    const grp = document.createElement('tr');
    grp.dataset.fake = '1';
    grp.dataset.path = '/fake/' + long;
    grp.className = 'grouprow';
    grp.innerHTML = `<td>—</td><td>—</td>` +
      `<td><div class="gwrap"><button class="gbtn">` +
      `<span class="gname" title="${long}&#10;/fake/${long}" data-full="${long}">${long}</span>` +
      `</button></div></td>` +
      `<td colspan="${n - 3}"><div class="gwrap grow-row">` +
      `<span class="grow"></span>` +
      `<div class="gbar"><i style="width:40%"></i></div>` +
      `<span class="gnum">1/4</span>` +
      `<span class="gbadge fail">失败 1</span></div></td>`;
    body.appendChild(grp);
  }
}"""

PROBE = """(tab) => {
  const pane = document.querySelector(`.pane[data-pane="${tab}"]`);
  const wrap = pane.querySelector('.tablewrap');
  const table = wrap.querySelector('table');
  // 只在当前面板的表里取基准长名，别被另一个面板残留的假行干扰
  const long0 = () => table.querySelector('tr[data-fake] .name')
    .dataset.full.slice(0, 30);
  const rows = [...table.querySelectorAll('tr[data-fake]')];
  const nameCell = (r) => [...r.cells].find(td => td.querySelector('.name'));
  // 省略载体：.name / .dir 的 nowrap+ellipsis 都落在内层 .nm 上，容器只管行高
  const shape = (e) => {
    const carrier = e.querySelector(':scope > .nm') || e;
    const cs = getComputedStyle(carrier);
    const ccs = getComputedStyle(e);
    return {
      w: Math.round(e.getBoundingClientRect().width),
      h: Math.round(e.getBoundingClientRect().height),
      lh: parseFloat(ccs.lineHeight),
      ws: cs.whiteSpace, to: cs.textOverflow, ox: cs.overflowX,
      clipped: carrier.scrollWidth > carrier.clientWidth + 1,
      full: e.dataset.full || '',
      text: carrier.textContent.trim()
    };
  };
  const fileRow = rows.find(r => r.querySelector('.name .nm') &&
                                r.querySelector('.name').textContent.includes(long0()));
  const names = rows.filter(r => r.querySelector('.name'))
                    .map(r => nameCell(r).querySelector('.name'));
  const tag = fileRow ? fileRow.querySelector('.name .tag') : null;
  const path = fileRow ? fileRow.querySelector('.name .path') : null;
  const tagInfo = () => {
    if (!tag) return null;
    const tb = tag.getBoundingClientRect();
    const cb = nameCell(fileRow).getBoundingClientRect();
    return {visible: tag.offsetWidth > 0, right: Math.round(tb.right),
            cellRight: Math.round(cb.right)};
  };
  const pathInfo = () => {
    if (!path) return null;
    return {visible: path.offsetParent !== null &&
                     path.getBoundingClientRect().width > 1};
  };
  const lastTd = rows[0].cells[rows[0].cells.length - 1];
  const btn = lastTd.querySelector('.rowbtns');
  const br = btn && btn.getBoundingClientRect();
  const tr = lastTd.getBoundingClientRect();
  const others = [...table.querySelectorAll('tr[data-fake] .dir, tr[data-fake] .gname')]
    .map(shape);
  return {
    vw: innerWidth,
    layout: getComputedStyle(table).tableLayout,
    wrapCW: wrap.clientWidth, wrapSW: wrap.scrollWidth,
    nameColW: Math.round(table.querySelector('thead th.c-name').getBoundingClientRect().width),
    names: names.map(shape),
    tag: tagInfo(), pathShown: pathInfo(),
    others,
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


def verify_long(pg, tab: str, width: int, long: str, mobile: bool) -> None:
    tag = f"{width}px {tab} {'无空格' if long == LONG_NOSPACE else '中文长名'}"
    pg.evaluate(INJECT, [long, SHORT_CN, tab])
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
    check(f"{tag} · 名称单行（nowrap + 行高恒定）",
          bool(nm) and nm["ws"] == "nowrap" and nm["h"] <= nm["lh"] * 1.3,
          str(nm)[:120] if nm else "没找到 .name")
    check(f"{tag} · 超出列宽截断且以省略号收尾",
          bool(nm) and nm["clipped"] and nm["to"] == "ellipsis" and nm["ox"] == "hidden",
          f'clipped {nm and nm["clipped"]} / to {nm and nm["to"]}' if nm else "-")
    check(f"{tag} · DOM 文本一字不丢", bool(nm) and nm["text"] == long,
          f'len {nm and len(nm["text"])} / {len(long)}' if nm else "-")
    check(f"{tag} · title/data-full 保留完整名",
          bool(nm) and long in nm["full"], "")
    check(f"{tag} · 「手选」标签不被挤走", bool(r["tag"]) and r["tag"]["visible"]
          and r["tag"]["right"] <= r["tag"]["cellRight"] + 1, str(r["tag"]))
    check(f"{tag} · 刮削标题仍在行内可见", bool(r["pathShown"])
          and r["pathShown"]["visible"] is True, str(r["pathShown"]))

    others = r["others"]
    label = "文件夹名" if tab == "files" else "分组名"
    o = others[0] if others else None
    check(f"{tag} · {label}单行省略且文本不丢",
          len(others) == 1 and bool(o) and o["ws"] == "nowrap"
          and o["to"] == "ellipsis" and o["ox"] == "hidden" and o["text"] == long,
          str(o)[:110] if o else "没找到")
    if tab == "tasks":
        # 分组名已经回到名称列里：列宽就是边界，长名必须被截断，
        # 且不宽于名称列（不再铺满整行）
        check(f"{tag} · {label}被名称列截断（不铺满整行）",
              bool(o) and o["clipped"] and o["w"] <= r["nameColW"] + 1,
              f'w {o and o["w"]} / 列宽 {r["nameColW"]}' if o else "-")
    check(f"{tag} · {label}悬停可看全名", bool(o) and long in o["full"], "")
    check(f"{tag} · 没有单元格内容溢出到邻列", not r["badCells"] and not r["actsOver"],
          str(r["badCells"]))


def verify_short(pg, tab: str, width: int) -> None:
    tag = f"{width}px {tab} 短名"
    r = pg.evaluate(PROBE, tab)
    names = r["names"]
    sho = names[1] if len(names) > 1 else None
    check(f"{tag} · 短名不截断、完整显示",
          bool(sho) and not sho["clipped"] and sho["text"] == SHORT_CN,
          str(sho)[:110] if sho else "没找到短名行")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6687")
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
                    verify_long(pg, tab, width, long, mobile)
                verify_short(pg, tab, width)
            check(f"{width}px 页面没有 JS 报错", not errs, "; ".join(errs[:2]))
            pg.close()
        b.close()
    print(f"\n结果：{len(FAIL)} 项失败")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
