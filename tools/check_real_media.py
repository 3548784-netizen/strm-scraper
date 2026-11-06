# -*- coding: utf-8 -*-
"""真实素材只读验收：接入 --media-root 指向的媒体库，验证全格式开箱即用、
已有产物识别、长名布局、工具条稳定、选帧铺满，并确认整轮零写入。

    python tools/devserver.py --media-root "D:/Desktop/test strm" \
        --data .devdata_real --port 6691 --media-port 6692
    python tools/check_real_media.py --base http://127.0.0.1:6691

只读保证：
  * /api/scan 会启动刮削并往素材目录写 nfo/poster，本脚本绝不调用；
  * 拦截 /api/frames/frame，不下载/渲染任何视频画面，只做 DOM 几何断言；
  * 监听所有非 GET 请求，断言一个都没有；
  * 测试前后比对素材目录快照（文件路径 / 大小 / mtime），断言完全未变。

退出码非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
OK = 0

SUPPORTED = {
    ".strm", ".mp4", ".m4v", ".mkv", ".avi", ".ts", ".m2ts",
    ".mov", ".wmv", ".flv", ".rmvb", ".webm", ".mpg",
}

VIEWPORTS = [
    (1920, 1080, "1080p"),
    (1440, 900, "1440"),
    (1280, 800, "1280"),
    (1024, 768, "1024"),
    (820, 700, "窄屏"),
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} :: {detail}")


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def snapshot(root: str) -> dict[str, tuple[int, int]]:
    p = Path(root)
    return {
        str(f.relative_to(p)): (f.stat().st_size, f.stat().st_mtime_ns)
        for f in p.rglob("*") if f.is_file()
    }


def walk_files(base: str) -> tuple[list[dict], str, int]:
    """递归只读文件树；返回 (媒体文件, 文件最多的目录, 目录总数)。"""
    files: list[dict] = []
    by_dir: dict[str, int] = {}
    dirs = 0

    def rec(path: str) -> None:
        nonlocal dirs
        u = "/api/files?limit=2000"
        if path:
            u += "&path=" + urllib.parse.quote(path)
        j = get(base, u)
        fs = j.get("files", [])
        files.extend(fs)
        if fs:
            by_dir[path] = len(fs)
        for d in j.get("dirs", []):
            dirs += 1
            rec(d["path"])

    rec("")
    deepest = max(by_dir, key=by_dir.get) if by_dir else ""
    return files, deepest, dirs


# 只量几何，不读任何图像像素
MEASURE = r"""() => {
  const q = id => document.getElementById(id);
  const R = el => { if (!el) return null; const b = el.getBoundingClientRect();
    return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)}; };
  const of = el => ({sw: el.scrollWidth, cw: el.clientWidth, sh: el.scrollHeight, ch: el.clientHeight});
  const card = document.querySelector('#pickModal .modal-card');
  const body = document.querySelector('#pickModal .pick-body');
  const main = document.querySelector('#pickModal .pick-main');
  const side = document.querySelector('#pickModal .pick-side');
  const shown = el => el && getComputedStyle(el).display !== 'none';
  return {
    vw: innerWidth, vh: innerHeight,
    card: R(card), head: R(document.querySelector('#pickModal .modal-head')),
    body: R(body), foot: R(document.querySelector('#pickModal .modal-foot')),
    main: R(main), stage: R(q('pickStage')), side: R(side), sideShown: shown(side),
    bar: R(q('pickCropBar')), line: R(document.querySelector('.pick-line')),
    ctrl: R(document.querySelector('.pick-ctrl')), strip: R(q('pickStrip')),
    apply: R(q('btnPickApply')), zoom: R(document.querySelector('.pick-zoom')),
    range: R(q('pickRange')), close: R(q('btnClosePick')),
    overflow: {card: of(card), body: of(body), main: of(main), side: of(side),
               strip: of(q('pickStrip')), html: of(document.documentElement)},
  };
}"""


def bottom(r: dict) -> int:
    return r["y"] + r["h"]


def right(r: dict) -> int:
    return r["x"] + r["w"]


def in_view(r: dict, m: dict) -> bool:
    return bool(r and r["w"] > 0 and r["h"] > 0 and r["x"] >= -1 and r["y"] >= -1
                and right(r) <= m["vw"] + 1 and bottom(r) <= m["vh"] + 1)


def no_overflow(m: dict, key: str) -> bool:
    o = m["overflow"][key]
    return o["sw"] <= o["cw"] + 1 and o["sh"] <= o["ch"] + 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6691")
    base = ap.parse_args().base.rstrip("/")

    print("\n---------- 1. 接入与全格式开箱即用 ----------")
    status = get(base, "/api/status")
    roots = status.get("roots") or []
    check("服务指向真实素材库（roots 非空）", bool(roots), str(roots))
    root = roots[0]
    cfg = get(base, "/api/config")
    exts = set(cfg.get("scan", {}).get("extensions") or [])
    check("默认支持全部 13 种格式，无需手动配置",
          SUPPORTED <= exts, f"缺 {sorted(SUPPORTED - exts)}")

    files, deepest, dirs = walk_files(base)
    on_disk = [f for f in Path(root).rglob("*")
               if f.is_file() and f.suffix.lower() in SUPPORTED]
    check("文件树完整发现所有媒体文件", len(files) == len(on_disk) == 34,
          f"接口 {len(files)} / 磁盘 {len(on_disk)}")
    check("扩展名过滤正确（nfo/jpg 不混进媒体列表）",
          all(Path(f["name"]).suffix.lower() in SUPPORTED for f in files),
          str({Path(f["name"]).suffix.lower() for f in files}))
    check("多层目录树可遍历", dirs >= 2 and bool(deepest), f"目录 {dirs} 个")

    print("\n---------- 2. 已有刮削产物被正确识别 ----------")
    arts = [f.get("artifacts") or {} for f in files]
    check("所有条目都带已有 nfo / poster / fanart",
          all(a.get("nfo", 0) > 0 and a.get("poster", 0) > 0 and a.get("fanart", 0) > 0
              for a in arts),
          str(arts[:2]))
    check("不把附加图 / nfo 当成待刮削媒体", len(files) == 34)

    print("\n---------- 3. 真实素材下的界面布局 ----------")
    before = snapshot(root)
    writes: list[str] = []
    errors: list[str] = []
    bad: list[str] = []
    frame_blocked = [0]

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("request", lambda r: writes.append(f"{r.method} {r.url}")
              if r.method not in ("GET", "HEAD", "OPTIONS") else None)
        pg.on("response", lambda r: bad.append(f"{r.status} {r.url[:100]}")
              if r.status >= 400 and "/api/" in r.url
              and "/api/frames/frame" not in r.url else None)
        # 不下载、不渲染任何视频画面；选帧布局仍可按几何测量
        pg.route("**/api/frames/frame*", lambda route: (
            frame_blocked.__setitem__(0, frame_blocked[0] + 1), route.abort())[1])

        url = base + "/?tab=files&path=" + urllib.parse.quote(deepest)
        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1600)

        tbl = pg.evaluate("""() => {
          const body = document.getElementById('fileBody');
          const table = body.closest('table');
          const rows = [...body.querySelectorAll('tr')];
          const btns = [...body.querySelectorAll('.rowbtns button')];
          const names = [...body.querySelectorAll('.name, .gname, .dir')];
          // nowrap/ellipsis 落在内层 .nm 上，探针取省略载体而不是容器
          const carrier = n => n.querySelector(':scope > .nm') || n;
          const ell = n => {
            const c = carrier(n);
            const cs = getComputedStyle(c);
            const full = n.dataset.full || '';
            const text = c.textContent;
            return {
              ws: cs.whiteSpace, to: cs.textOverflow,
              truncated: c.scrollWidth > c.clientWidth + 1,
              badClip: c.scrollWidth > c.clientWidth + 1 && cs.overflowX !== 'hidden',
              domLost: full ? text !== full : false,
              titleLost: full ? !(n.getAttribute('title') || '').includes(full) : false,
              len: text.length,
            };
          };
          return {
            rows: rows.length, layout: getComputedStyle(table).tableLayout,
            docSW: document.documentElement.scrollWidth, vw: innerWidth,
            maxRight: btns.length ? Math.max(...btns.map(b => b.getBoundingClientRect().right)) : 0,
            btnCount: btns.length,
            total: names.length,
            nowrap: names.filter(n => ell(n).ws === 'nowrap').length,
            ellipsis: names.filter(n => ell(n).to === 'ellipsis').length,
            truncated: names.filter(n => ell(n).truncated).length,
            badClip: names.filter(n => ell(n).badClip).length,
            domLost: names.filter(n => ell(n).domLost).length,
            titleLost: names.filter(n => ell(n).titleLost).length,
            longest: Math.max(0, ...names.map(n => ell(n).len)),
          };
        }""")
        expect_rows = len([f for f in files if str(Path(f["path"]).parent) == deepest])
        check("深链接直达目录并渲染全部真实行", tbl["rows"] == expect_rows > 0,
              f"DOM {tbl['rows']} / 接口 {expect_rows}")
        check("表格固定列宽生效", tbl["layout"] == "fixed", tbl["layout"])
        check("长中文名不把右侧操作列挤出视口",
              tbl["maxRight"] <= tbl["vw"] + 1 and tbl["docSW"] <= tbl["vw"] + 1,
              f"maxRight={tbl['maxRight']} docSW={tbl['docSW']} vw={tbl['vw']}")
        check("名称单行省略口径生效（nowrap + ellipsis）",
              tbl["total"] > 0 and tbl["nowrap"] == tbl["total"]
              and tbl["ellipsis"] == tbl["total"],
              f"nowrap {tbl['nowrap']}/{tbl['total']}，ellipsis {tbl['ellipsis']}/{tbl['total']}")
        check("超长名只在省略号容器内截断、不溢出邻列",
              tbl["badClip"] == 0,
              f"截断 {tbl['truncated']} 个，越界截断 {tbl['badClip']} 个，最长 {tbl['longest']} 字")
        check("DOM 文本未丢字（与 data-full 一致）", tbl["domLost"] == 0,
              f"丢字 {tbl['domLost']} 个")
        check("悬停可看完整名（title 覆盖全名）", tbl["titleLost"] == 0,
              f"缺 title {tbl['titleLost']} 个")
        check("行内操作入口保留", tbl["btnCount"] > 0, str(tbl["btnCount"]))

        print("\n---------- 4. 勾选后工具条不跳位 ----------")
        read_tb = """() => {
          const R = el => { const b = el.getBoundingClientRect();
            return {x: Math.round(b.x), y: Math.round(b.y), h: Math.round(b.height),
                    w: Math.round(b.width)}; };
          return {
            bar: R(document.querySelector('.toolbar')),
            sel: R(document.getElementById('fileSelInfo')),
            btns: ['btnFileScrape','btnFileRescrape','btnFolderArt','btnFileDel']
              .map(id => R(document.getElementById(id))),
            label: document.getElementById('fileSelInfo').textContent.trim(),
          };
        }"""
        t0 = pg.evaluate(read_tb)
        boxes = pg.locator("#fileBody input[data-pick]")
        n = min(4, boxes.count())
        for i in range(n):
            boxes.nth(i).click()
        pg.wait_for_timeout(350)
        t1 = pg.evaluate(read_tb)
        check("选中数量只出现在固定宽度槽里",
              t0["sel"]["w"] == t1["sel"]["w"] and t1["sel"]["w"] > 0,
              f"{t0['sel']['w']} -> {t1['sel']['w']}")
        check("工具条高度与四个批量按钮位置不变",
              t0["bar"]["h"] == t1["bar"]["h"]
              and all(a["x"] == c["x"] and a["y"] == c["y"]
                      for a, c in zip(t0["btns"], t1["btns"])),
              f"{[x['x'] for x in t0['btns']]} -> {[x['x'] for x in t1['btns']]}")
        check("按钮文案恒定、不追加 (N)",
              "(" not in t1["label"], t1["label"])

        print("\n---------- 5. 真实条目上的选帧界面铺满 ----------")
        sample = next(f["path"] for f in files if str(Path(f["path"]).parent) == deepest)
        pg.evaluate("p => openPicker(p)", sample)
        pg.wait_for_timeout(900)
        for w, h, tag in VIEWPORTS:
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(220)
            m = pg.evaluate(MEASURE)
            c, bd, f, st = m["card"], m["body"], m["foot"], m["stage"]
            check(f"{tag}：选帧卡片铺满、四周无多余留白",
                  0 <= c["x"] <= 10 and 0 <= c["y"] <= 10
                  and m["vw"] - c["w"] <= 20 and m["vh"] - c["h"] <= 20,
                  f"{c['x']},{c['y']} {c['w']}x{c['h']} / {m['vw']}x{m['vh']}")
            check(f"{tag}：标题/内容/底栏、舞台/控件/胶片条不重叠",
                  bottom(m["head"]) <= bd["y"] + 1 and bottom(bd) <= f["y"] + 1
                  and bottom(m["bar"]) <= st["y"] + 1
                  and bottom(st) <= m["line"]["y"] + 1
                  and bottom(m["strip"]) <= bottom(bd) + 1,
                  f"stage={st['y']}..{bottom(st)} strip={m['strip']['y']}..{bottom(m['strip'])}")
            check(f"{tag}：容器不溢出、页面无意外滚动条",
                  all(no_overflow(m, k) for k in ("card", "body", "main", "side"))
                  and m["overflow"]["html"]["sw"] <= m["vw"] + 1
                  and m["overflow"]["html"]["sh"] <= m["vh"] + 1,
                  str(m["overflow"]["html"]))
            check(f"{tag}：确定裁切 / 缩放 / 时间轴 / 关闭完整可见",
                  all(in_view(m[k], m) for k in ("apply", "zoom", "range", "close", "stage")),
                  f"apply={m['apply']} zoom={m['zoom']}")

        pg.keyboard.press("Escape")
        pg.wait_for_timeout(250)
        check("Esc 仍能关闭选帧弹窗",
              pg.evaluate("() => document.getElementById('pickModal').classList.contains('show') === false"
                          " || getComputedStyle(document.getElementById('pickModal')).display === 'none'"))
        b.close()

    print("\n---------- 6. 零写入保证 ----------")
    after = snapshot(root)
    check("素材目录文件集合完全一致（无新增产物）",
          set(before) == set(after), f"{len(before)} -> {len(after)}")
    check("所有文件 大小 / 修改时间 均未变化",
          before == after, "有文件被改动")
    check("整轮未发出任何写请求（POST / PUT / DELETE）",
          not writes, str(writes[:3]))
    check("未下载任何视频画面（帧请求已拦截）",
          frame_blocked[0] > 0, f"拦截 {frame_blocked[0]} 次")
    check("无 JS 报错", not errors, str(errors[:3]))
    check("无接口 4xx / 5xx（被拦截的帧请求除外）", not bad, str(bad[:3]))

    print(f"\n结果：{len(FAIL)} 项失败，{OK} 项通过")
    for name in FAIL:
        print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
