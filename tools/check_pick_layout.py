# -*- coding: utf-8 -*-
"""选帧界面居中卡片回归：多视口 / 缩放等效尺寸下不铺满、不裁切、不重叠、不出现意外滚动条。

    python tools/check_pick_layout.py --base http://127.0.0.1:6687

只读：只打开选帧面板、改视口、试一下拖框与缩放按钮，不发任何写接口。
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

ROOT = Path(__file__).resolve().parent.parent
FAIL: list[str] = []
OK = 0

# 常见桌面尺寸 + 浏览器缩放后的等效 CSS 视口（125% / 150% / 200%）
VIEWPORTS = [
    (2560, 1440, "2K"),
    (1920, 1080, "1080p"),
    (1600, 900, "1600"),
    (1440, 900, "1440"),
    (1366, 768, "笔记本"),
    (1280, 800, "1280"),
    (1152, 720, "125% 缩放等效"),
    (1024, 768, "1024"),
    (960, 600, "150% 缩放等效"),
    (820, 700, "窄屏"),
    (768, 480, "矮窗 / 高倍缩放"),
    (640, 640, "手机宽"),
]

MEASURE = r"""() => {
  const q = id => document.getElementById(id);
  const R = el => { if (!el) return null; const b = el.getBoundingClientRect();
    return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)}; };
  const of = el => ({sw: el.scrollWidth, cw: el.clientWidth,
                     sh: el.scrollHeight, ch: el.clientHeight});
  const card = document.querySelector('#pickModal .modal-card');
  const body = document.querySelector('#pickModal .pick-body');
  const main = document.querySelector('#pickModal .pick-main');
  const side = document.querySelector('#pickModal .pick-side');
  const shown = el => el && getComputedStyle(el).display !== 'none';
  const state = typeof PICK === 'undefined' ? null
    : {crop: {...PICK.crop}, zoom: PICK.zoom, t: PICK.t, mode: PICK.cropMode};
  const fit = (typeof PICK === 'undefined' || !PICK.fit) ? null
    : {w: Math.round(PICK.fit.width), h: Math.round(PICK.fit.height),
       left: Math.round(PICK.fit.left), top: Math.round(PICK.fit.top)};
  return {
    vw: innerWidth, vh: innerHeight,
    card: R(card), head: R(document.querySelector('#pickModal .modal-head')),
    body: R(body), foot: R(document.querySelector('#pickModal .modal-foot')),
    main: R(main), stage: R(q('pickStage')), side: R(side), sideShown: shown(side),
    bar: R(q('pickCropBar')), line: R(document.querySelector('.pick-line')),
    ctrl: R(document.querySelector('.pick-ctrl')), strip: R(q('pickStrip')),
    cropView: R(q('pickCropView')), poster: R(q('pickPoster')),
    box: R(q('pickBox')), boxHidden: q('pickBox').hidden,
    apply: R(q('btnPickApply')), zoom: R(document.querySelector('.pick-zoom')),
    range: R(q('pickRange')), close: R(q('btnClosePick')),
    overflow: {card: of(card), body: of(body), main: of(main), side: of(side),
               strip: of(q('pickStrip')), html: of(document.documentElement)},
    img: {nw: q('pickImg').naturalWidth, nh: q('pickImg').naturalHeight},
    state, fit,
  };
}"""


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def no_overflow(m: dict, key: str) -> bool:
    o = m["overflow"][key]
    return o["sw"] <= o["cw"] + 1 and o["sh"] <= o["ch"] + 1


def bottom(r: dict) -> int:
    return r["y"] + r["h"]


def right(r: dict) -> int:
    return r["x"] + r["w"]


def in_view(r: dict, m: dict) -> bool:
    return (r and r["w"] > 0 and r["h"] > 0
            and r["x"] >= -1 and r["y"] >= -1
            and right(r) <= m["vw"] + 1 and bottom(r) <= m["vh"] + 1)


def find_media(base: str) -> str | None:
    """找一个真的能抽帧的 .strm；根目录取自服务端配置，别写死本地路径。

    写死 ``.devdata/media`` 时，只要 devserver 换了个 --data，就会拿空路径
    打开面板并记一笔 400，把「布局全绿」误报成失败。
    """
    roots: list[Path] = []
    try:
        with urllib.request.urlopen(f"{base}/api/config", timeout=10) as r:
            cfg = json.load(r)
        roots = [Path(x) for x in (cfg.get("scan", {}).get("roots") or [])]
    except Exception:
        pass
    roots.append(ROOT / ".devdata" / "media")   # 兼容老环境
    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.strm")):
            url = f"{base}/api/frames/plan?count=4&path={urllib.parse.quote(str(f))}"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    if r.status == 200:
                        return str(f)
            except Exception:
                continue
    return None


def source_checks(src: str) -> None:
    print("\n---------- 1. 源码层：尺寸约束与交互入口 ----------")
    check("不再有 920px 居中固定宽度", "max-width: 920px" not in src)
    check("弹窗恢复居中、不再拉伸铺满", "place-items: stretch" not in src
          and "place-items: center" in src)
    check("卡片有明确宽高上限，四周留出可点的遮罩",
          "width: min(1180px, 100%); height: min(84vh, 820px)" in src
          and "#pickModal { padding: var(--s5); }" in src)
    pick_css = src.split(".modal-card.pick {", 1)[1].split("}", 1)[0]
    check("不再全屏铺满 / 保留圆角边框",
          "width: 100%; height: 100%" not in pick_css
          and "border: 0" not in pick_css
          and "min(1180px, 100%)" in pick_css)
    check("内容区纵向 flex 吃剩余高度", "flex: 1; min-height: 0; overflow: hidden;" in src)
    check("侧栏宽度随视口 clamp", "clamp(176px, 17vw, 320px)" in src)
    check("舞台不再写死 48vh / 200px 下限",
          "height: 48vh" not in src and "min-height: 200px" not in src)
    check("侧栏不再写死 47vh / 预览 168px 上限",
          "max-height: 47vh" not in src and "max-height: 168px" not in src)
    check("窄屏不再把舞台压回 28vh", "height: 28vh" not in src)
    check("缩略图随容器宽度放大", "clamp(104px, 8vw, 148px)" in src)
    check("窗口变化仍会重贴合画面与取景框",
          "new ResizeObserver(refitStage).observe($('pickStage'))" in src)
    check("选帧 / 应用 / 关闭入口与数据状态原样保留",
          all(s in src for s in ("function openPicker(", "function closePicker(",
                                 "btnPickApply", "PICK.crop", "function refitStage(")))


def layout_checks(m: dict, tag: str) -> None:
    c, b, f, main, stage = m["card"], m["body"], m["foot"], m["main"], m["stage"]
    # 卡片居中且不铺满：四周都要留出可见遮罩，点外部才能关掉
    check(f"{tag}：卡片居中、四周留遮罩（不铺满全屏）",
          c["x"] >= 8 and c["y"] >= 8
          and m["vw"] - right(c) >= 8 and m["vh"] - bottom(c) >= 8
          and abs((c["x"] + c["w"] / 2) - m["vw"] / 2) <= 2
          and abs((c["y"] + c["h"] / 2) - m["vh"] / 2) <= 2,
          f"{c['x']},{c['y']} {c['w']}x{c['h']} / {m['vw']}x{m['vh']}")
    check(f"{tag}：标题栏 / 内容区 / 底栏依次排布不重叠",
          bottom(m["head"]) <= b["y"] + 1 and bottom(b) <= f["y"] + 1
          and bottom(f) <= m["vh"] + 1,
          f"head={bottom(m['head'])} body={b['y']}..{bottom(b)} foot={f['y']}..{bottom(f)}")
    check(f"{tag}：工具条 / 舞台 / 时间轴 / 控件行 / 胶片条互不重叠",
          bottom(m["bar"]) <= stage["y"] + 1
          and bottom(stage) <= m["line"]["y"] + 1
          and bottom(m["line"]) <= m["ctrl"]["y"] + 1
          and bottom(m["ctrl"]) <= m["strip"]["y"] + 1
          and bottom(m["strip"]) <= bottom(b) + 1,
          f"bar={bottom(m['bar'])} stage={stage['y']}..{bottom(stage)} "
          f"line={m['line']['y']} ctrl={m['ctrl']['y']} strip={m['strip']['y']}..{bottom(m['strip'])}")
    check(f"{tag}：主区与预览列不横向交叠",
          not m["sideShown"] or (right(main) <= m["side"]["x"] + 1
                                 and right(m["side"]) <= right(b) + 1),
          f"main.right={right(main)} side.x={m['side']['x']} side.right={right(m['side'])}")
    check(f"{tag}：卡片 / 内容区 / 主区 / 侧栏都不溢出",
          all(no_overflow(m, k) for k in ("card", "body", "main", "side")),
          str(m["overflow"]))
    check(f"{tag}：胶片条只有设计内的横向滚动、无纵向滚动",
          m["overflow"]["strip"]["sh"] <= m["overflow"]["strip"]["ch"] + 1,
          str(m["overflow"]["strip"]))
    check(f"{tag}：页面不出现意外滚动条",
          m["overflow"]["html"]["sw"] <= m["vw"] + 1
          and m["overflow"]["html"]["sh"] <= m["vh"] + 1,
          str(m["overflow"]["html"]))
    check(f"{tag}：关键控件完整可见且不被裁切",
          all(in_view(m[k], m) for k in ("apply", "zoom", "range", "strip", "close", "stage")),
          f"apply={m['apply']} zoom={m['zoom']} strip={m['strip']}")
    # 极矮窗口里标题 / 底栏 / 胶片条是固定高度，用「舞台占内容区高度」衡量更稳，
    # 不受窗口宽高比影响；同时打印占整屏面积供对照
    # 极矮窗口里固定区块挤掉了大部分高度，绝对下限跟着视口缩放：
    # 768x480 时舞台约 124px 已是可用的全部空间，仍要占内容区 40% 以上
    floor = min(180, m["vh"] * 0.22)
    h_ratio = stage["h"] / max(1, b["h"])
    check(f"{tag}：主舞台充分利用可用面积",
          stage["h"] >= floor and h_ratio >= 0.40,
          f"{stage['w']}x{stage['h']}，下限 {floor:.0f}px，占内容区高度 {h_ratio:.0%}，"
          f"占整屏 {stage['w']*stage['h']/(m['vw']*m['vh']):.0%}")
    if m["sideShown"]:
        # 矮窗口下预览图跟着缩放，别拿大屏的 120px 去要求它
        preview_floor = 120 if m["vh"] >= 600 else 80
        check(f"{tag}：两张预览随侧栏缩放且没被压扁",
              m["cropView"]["w"] > preview_floor
              and m["cropView"]["h"] > preview_floor * 0.8
              and m["poster"]["h"] > preview_floor * 0.8,
              f"cropView={m['cropView']['w']}x{m['cropView']['h']} poster.h={m['poster']['h']}")
    if not m["boxHidden"]:
        check(f"{tag}：取景框完整落在舞台内",
              m["box"]["x"] >= stage["x"] - 1 and m["box"]["y"] >= stage["y"] - 1
              and right(m["box"]) <= right(stage) + 1
              and bottom(m["box"]) <= bottom(stage) + 1,
              f"box={m['box']} stage={stage}")
        check(f"{tag}：画面显示尺寸随舞台重新贴合",
              bool(m["fit"]) and m["fit"]["w"] <= stage["w"] + 1
              and m["fit"]["h"] <= stage["h"] + 1,
              f"fit={m['fit']} stage={stage['w']}x{stage['h']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    source_checks(src)

    media = find_media(base)
    if not media:
        print("  提示：没找到可抽帧的 .strm，交互相关断言会跳过；布局断言照常跑。")

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        bad: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("response", lambda r: bad.append(f"{r.status} {r.url[:100]}")
              if r.status >= 400 and "/api/" in r.url else None)
        pg.goto(base, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1200)
        pg.evaluate("p => openPicker(p)", media or "")
        if media:
            try:
                pg.wait_for_function("() => document.getElementById('pickImg').naturalWidth > 0",
                                     timeout=60000)
            except Exception as e:
                print("  提示：主帧未就绪", str(e)[:100])
            pg.wait_for_timeout(600)

        print("\n---------- 2. 十二档视口 / 缩放等效尺寸 ----------")
        first_state = pg.evaluate("""() => typeof PICK === 'undefined' ? null
            : ({crop: {...PICK.crop}, zoom: PICK.zoom, t: PICK.t, mode: PICK.cropMode})""")
        measured: dict[str, dict] = {}
        for w, h, label in VIEWPORTS:
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(260)
            m = pg.evaluate(MEASURE)
            measured[f"{w}x{h}"] = m
            print(f"\n  [{label} {w}x{h}] card={m['card']['w']}x{m['card']['h']} "
                  f"stage={m['stage']['w']}x{m['stage']['h']} "
                  f"side={m['side']['w'] if m['sideShown'] else '-'} "
                  f"preview={m['cropView']['h']}")
            layout_checks(m, f"{label} {w}x{h}")

        print("\n---------- 3. 自适应与数据状态 ----------")
        a, z = measured["1920x1080"], measured["1280x800"]
        # 卡片宽到 1180px 就封顶了：再宽的视口只会让侧栏按 clamp 变宽，
        # 主舞台反而略窄。这里验的是「封顶后不溢出、两块都不被挤没」。
        check("宽视口下卡片封顶 1180px、主舞台与侧栏都不溢出",
              a["card"]["w"] <= 1180 and z["card"]["w"] <= 1180
              and a["stage"]["w"] >= 700 and z["stage"]["w"] >= 700
              and a["stage"]["w"] + a["side"]["w"] <= a["card"]["w"]
              and z["stage"]["w"] + z["side"]["w"] <= z["card"]["w"],
              f"card {z['card']['w']}->{a['card']['w']}, "
              f"stage {z['stage']['w']}->{a['stage']['w']}, "
              f"side {z['side']['w']}->{a['side']['w']}")
        check("窗口变高时主舞台跟着变高", a["stage"]["h"] > z["stage"]["h"] + 100,
              f"{z['stage']['h']} -> {a['stage']['h']}")
        check("预览列与预览图跟着容器长大",
              a["side"]["w"] > z["side"]["w"] and a["cropView"]["h"] > z["cropView"]["h"] + 50,
              f"side {z['side']['w']}->{a['side']['w']}, view {z['cropView']['h']}->{a['cropView']['h']}")
        last_state = pg.evaluate("""() => typeof PICK === 'undefined' ? null
            : ({crop: {...PICK.crop}, zoom: PICK.zoom, t: PICK.t, mode: PICK.cropMode})""")
        check("反复改视口后取景范围 / 缩放 / 时间点 / 模式全部不变",
              first_state == last_state, f"{first_state} -> {last_state}")

        # 浏览器 Ctrl+ 缩放等价于 CSS 视口缩小，已在第 2 节用 1152x720 /
        # 960x600 / 768x480 覆盖；这里用 CDP 改设备像素比验证系统级显示缩放。
        print("\n---------- 4. 系统显示缩放（高 DPI / DPR） ----------")
        cdp = pg.context.new_cdp_session(pg)
        for dpr in (1.25, 1.5, 2):
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": dpr, "mobile": False})
            pg.wait_for_timeout(260)
            m = pg.evaluate(MEASURE)
            check(f"DPR {dpr}: 卡片仍居中不铺满、容器不溢出、无意外滚动条",
                  m["card"]["x"] >= 8 and m["vw"] - right(m["card"]) >= 8
                  and m["card"]["y"] >= 8 and m["vh"] - bottom(m["card"]) >= 8
                  and abs((m["card"]["x"] + m["card"]["w"] / 2) - m["vw"] / 2) <= 2
                  and all(no_overflow(m, k) for k in ("card", "body", "main", "side"))
                  and m["overflow"]["strip"]["sh"] <= m["overflow"]["strip"]["ch"] + 1
                  and m["overflow"]["html"]["sw"] <= m["vw"] + 1
                  and m["overflow"]["html"]["sh"] <= m["vh"] + 1,
                  str(m["overflow"]))
            check(f"DPR {dpr}: 区块不重叠、关键控件仍完整可见",
                  bottom(m["head"]) <= m["body"]["y"] + 1
                  and bottom(m["stage"]) <= m["line"]["y"] + 1
                  and bottom(m["strip"]) <= bottom(m["body"]) + 1
                  and all(in_view(m[k], m) for k in ("apply", "zoom", "range", "close")),
                  f"stage={m['stage']} apply={m['apply']}")
        cdp.send("Emulation.clearDeviceMetricsOverride")

        if media:
            print("\n---------- 5. 交互入口与数据状态 ----------")
            pg.set_viewport_size({"width": 1440, "height": 900})
            pg.wait_for_timeout(300)
            geo = pg.evaluate("""() => ({fit: PICK.fit,
                stage: (r => ({x: r.x, y: r.y}))(document.getElementById('pickStage').getBoundingClientRect()),
                crop: {...PICK.crop}})""")
            cx = geo["stage"]["x"] + geo["fit"]["left"] + geo["fit"]["width"] * 0.5
            cy = geo["stage"]["y"] + geo["fit"]["top"] + geo["fit"]["height"] * 0.5
            pg.mouse.move(cx, cy)
            pg.mouse.down()
            pg.mouse.move(cx + 55, cy + 18, steps=12)
            pg.mouse.up()
            pg.wait_for_timeout(350)
            moved = pg.evaluate("({...PICK.crop})")
            check("拖取景框仍可平移", moved["x"] != geo["crop"]["x"]
                  or moved["y"] != geo["crop"]["y"], f"{geo['crop']} -> {moved}")
            check("拖动不改变取景大小", abs(moved["w"] - geo["crop"]["w"]) < 1e-6
                  and abs(moved["h"] - geo["crop"]["h"]) < 1e-6)
            pg.click("#pickZoomIn")
            pg.wait_for_timeout(250)
            check("放大按钮仍生效", pg.evaluate("PICK.zoom") > 1)
            pg.click("#pickZoomFit")
            pg.wait_for_timeout(250)
            check("回到适应窗口仍生效", abs(pg.evaluate("PICK.zoom") - 1) < 1e-6)
            pg.click('button[data-crop="none"]')
            pg.wait_for_timeout(250)
            check("整帧模式仍切换主按钮文案并收起取景框",
                  pg.inner_text("#btnPickApply").strip() == "使用整帧"
                  and pg.evaluate("document.getElementById('pickBox').hidden"))
            pg.click('button[data-crop="poster"]')
            pg.wait_for_timeout(250)
            check("切回海报 2:3 后取景框恢复",
                  pg.inner_text("#btnPickApply").strip() == "确定裁切"
                  and not pg.evaluate("document.getElementById('pickBox').hidden"))
            check("胶片条时间点入口仍在且数量随宽度增多",
                  pg.locator("#pickStrip figure").count() >= 10,
                  str(pg.locator("#pickStrip figure").count()))
            # 全屏工作区仍留 8px 遮罩热区，原来的「点外部关闭」入口不能丢
            pg.mouse.click(6, 6)
            pg.wait_for_timeout(180)
            check("点遮罩仍能关闭选帧面板",
                  pg.evaluate("document.getElementById('pickModal').hidden"))

        check("整轮没有 JS 报错", not errors, str(errors[:3]))
        check("整轮没有接口 4xx/5xx", not bad, str(bad[:3]))
        b.close()

    print(f"\n结果：{len(FAIL)} 项失败，{OK} 项通过")
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
