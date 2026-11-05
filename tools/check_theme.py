# -*- coding: utf-8 -*-
"""视觉回归：把 Amane 主题的设计令牌、交互状态和响应式断点钉死。

check_ui_sweep.py 管「点下去会不会炸」，这个管「看起来对不对」：
配色是不是中性灰阶 + 品牌蓝、有没有残留渐变或毛玻璃、按钮和输入框的
默认 / 悬停 / 焦点 / 禁用 / 加载五态、宽屏到手机有没有横向溢出。

    python tools/check_theme.py --base http://127.0.0.1:6672

主界面走 --base 指定的服务；登录页直接用仓库里的 static/login.html
（file:// 打开即可，不需要真的开认证，也不提交表单）。
退出码非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
VIEWPORTS = [(1500, 950), (1280, 800), (1024, 700), (480, 820)]

BG = "rgb(36, 36, 36)"
CARD = "rgb(31, 31, 31)"
LINE = "rgb(66, 66, 66)"
BLUE = "rgb(26, 113, 255)"
SOLID = "rgb(0, 85, 204)"
SOLID_HOVER = "rgb(0, 73, 180)"


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAIL.append(name)


def css(pg, selector: str, *props: str, pseudo: str | None = None) -> dict:
    return pg.evaluate(
        """([sel, props, pseudo]) => {
          const e = document.querySelector(sel);
          if (!e) return null;
          const s = getComputedStyle(e, pseudo);
          return Object.fromEntries(props.map(k => [k, s[k]]));
        }""",
        [selector, list(props), pseudo],
    )


def main_theme(pg) -> None:
    print("\n---------- 1. 设计令牌 ----------")
    body = css(pg, "body", "backgroundColor", "fontFamily")
    check("页面底色是 Amane 的 dark[7] #242424", body["backgroundColor"] == BG,
          body["backgroundColor"])
    check("字体栈与 Amane 一致（IBM Plex Sans 打头）",
          body["fontFamily"].startswith('"IBM Plex Sans"'), body["fontFamily"][:48])
    card = css(pg, ".set-card", "borderRadius", "borderTopColor", "backgroundColor")
    check("卡片 8px 圆角 / 1px 中性描边",
          card["borderRadius"] == "8px" and card["borderTopColor"] == LINE,
          f'{card["borderRadius"]} / {card["borderTopColor"]}')
    btn = css(pg, "#btnStart", "backgroundColor", "backgroundImage", "fontWeight", "boxShadow")
    check("主按钮是实心品牌蓝，无渐变无辉光",
          btn["backgroundColor"] == SOLID and btn["backgroundImage"] == "none"
          and btn["boxShadow"] == "none" and btn["fontWeight"] == "600",
          f'{btn["backgroundColor"]} / {btn["backgroundImage"]}')
    top = css(pg, ".topbar", "backgroundImage")
    check("顶栏不用渐变", top["backgroundImage"] == "none", top["backgroundImage"])
    modal = css(pg, ".modal", "backdropFilter")
    check("弹窗遮罩不用毛玻璃（原生弹层定位隐患）",
          modal["backdropFilter"] == "none", modal["backdropFilter"])

    print("\n---------- 2. 交互五态 ----------")
    # 顶栏主按钮只在任务类页面是 primary；设置页会被 JS 降权成 accent-outline
    pg.hover("#btnStart")
    pg.wait_for_timeout(280)
    hover = css(pg, "#btnStart", "backgroundColor")
    check("主按钮悬停压深一档", hover["backgroundColor"] == SOLID_HOVER,
          hover["backgroundColor"])
    # 切到设置页后 JS 会把顶栏主按钮降权成 accent-outline，正好验描边态
    pg.click('[data-tab="settings"]')
    pg.wait_for_timeout(350)
    outline = css(pg, "#btnStart", "backgroundColor")
    pg.hover("#btnStart")
    pg.wait_for_timeout(280)
    outline_hover = css(pg, "#btnStart", "backgroundColor")
    check("描边 / 选中按钮悬停比默认更深",
          outline["backgroundColor"] != outline_hover["backgroundColor"]
          and "26, 113, 255" in outline_hover["backgroundColor"],
          f'{outline["backgroundColor"]} → {outline_hover["backgroundColor"]}')

    inp = 'section.pane.active input[type="text"]'
    pg.focus(inp)
    pg.wait_for_timeout(220)
    focus = css(pg, inp, "borderColor", "boxShadow")
    check("输入框聚焦是 2px 蓝环，不是发光",
          focus["borderColor"] == BLUE and "26, 113, 255" in focus["boxShadow"],
          focus["borderColor"])
    disabled = pg.evaluate("""() => {
      const b = document.querySelector('#btnStart');
      b.disabled = true;
      const s = getComputedStyle(b);
      const r = {opacity: s.opacity, cursor: s.cursor};
      b.disabled = false;
      return r;
    }""")
    check("禁用态降透明度且不可点", disabled["opacity"] == "0.38", str(disabled))
    loading = pg.evaluate("""() => {
      const b = document.querySelector('#btnStart');
      b.classList.add('is-loading');
      const s = getComputedStyle(b, '::before');
      const r = {animation: s.animationName, size: s.width, pointer: getComputedStyle(b).pointerEvents};
      b.classList.remove('is-loading');
      return r;
    }""")
    check("加载态有旋转圆环且不再接收点击",
          loading["animation"] == "amane-spin" and loading["size"] == "12px"
          and loading["pointer"] == "none", str(loading))
    pg.emulate_media(reduced_motion="reduce")
    pg.wait_for_timeout(120)
    reduced = pg.evaluate("""() => {
      const b = document.querySelector('#btnStart');
      b.classList.add('is-loading');
      const a = getComputedStyle(b, '::before').animationName;
      b.classList.remove('is-loading');
      return a;
    }""")
    check("减少动效偏好下圆环停转", reduced == "none", reduced)
    pg.emulate_media(reduced_motion="no-preference")


def responsive(pg) -> None:
    print("\n---------- 3. 响应式（含 flat 视图行尾按钮） ----------")
    pg.evaluate("""() => localStorage.removeItem('strm-scraper.prefs.v1')""")
    pg.click('[data-tab="tasks"]')
    pg.wait_for_timeout(250)
    # 界面上不再有视图切换控件；直接设定内部渲染形态来覆盖文件行样式
    pg.evaluate("v => setTaskView(v)", 'flat')
    pg.wait_for_timeout(400)
    for w, h in VIEWPORTS:
        pg.set_viewport_size({"width": w, "height": h})
        pg.wait_for_timeout(350)
        info = pg.evaluate("""() => {
          const d = document.documentElement;
          const b = document.querySelector('section.pane.active button[data-act="clean"]');
          let before = -1, after = -1, scrollable = false;
          if (b) {
            before = Math.round(b.getBoundingClientRect().right);
            const wrap = b.closest('.tablewrap');
            if (wrap) {
              scrollable = wrap.scrollWidth > wrap.clientWidth;
              wrap.scrollLeft = wrap.scrollWidth;
            }
            after = Math.round(b.getBoundingClientRect().right);
          }
          return {sw: d.scrollWidth, vw: innerWidth, before, after, scrollable};
        }""")
        check(f"{w}px 没有横向滚动", info["sw"] <= info["vw"] + 2,
              f'scrollWidth {info["sw"]} / 视口 {info["vw"]}')
        if w >= 1024:
            check(f"{w}px 行尾操作按钮没有被挤出屏幕",
                  info["before"] <= info["vw"] + 1,
                  f'right {info["before"]} / 视口 {info["vw"]}')
        else:
            # 手机上表格保留全部数据列，靠容器内部横向滚动够到行尾按钮
            check(f"{w}px 表格内部可滚动且行尾按钮可达",
                  info["scrollable"] and info["after"] <= info["vw"] + 1,
                  f'滚动后 right {info["after"]} / 视口 {info["vw"]}')


def login_theme(b, login_url: str) -> None:
    print("\n---------- 4. 登录页 ----------")
    pg = b.new_page(viewport={"width": 1200, "height": 800})
    errs: list[str] = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:140]))
    pg.goto(login_url, wait_until="load", timeout=20000)
    pg.bring_to_front()
    pg.evaluate("""() => { document.querySelector('#msg').className = 'msg'; }""")
    card = css(pg, ".card", "backgroundColor", "borderRadius", "borderTopColor")
    check("登录卡片与主界面同一套表面 / 描边 / 圆角",
          card["backgroundColor"] == CARD and card["borderRadius"] == "8px"
          and card["borderTopColor"] == LINE, str(card))
    btn = css(pg, "#btn", "backgroundColor", "fontWeight")
    check("登录按钮用同一个实心蓝", btn["backgroundColor"] == SOLID
          and btn["fontWeight"] == "600", str(btn))
    pg.focus("#username")
    pg.wait_for_timeout(220)
    focus = css(pg, "#username", "borderColor", "boxShadow")
    check("登录输入框聚焦蓝环", focus["borderColor"] == BLUE
          and "26, 113, 255" in focus["boxShadow"], focus["borderColor"])
    loading = pg.evaluate("""() => {
      const b = document.querySelector('#btn');
      b.classList.add('is-loading'); b.disabled = true;
      const s = getComputedStyle(b, '::before');
      const r = {animation: s.animationName, size: s.width, opacity: getComputedStyle(b).opacity};
      b.classList.remove('is-loading'); b.disabled = false;
      return r;
    }""")
    check("登录加载态圆环 + 禁用透明度",
          loading["animation"] == "amane-spin" and loading["size"] == "12px"
          and loading["opacity"] == "0.38", str(loading))
    pg.set_viewport_size({"width": 390, "height": 760})
    pg.wait_for_timeout(250)
    narrow = pg.evaluate("""() => ({sw: document.documentElement.scrollWidth, vw: innerWidth,
      w: Math.round(document.querySelector('.card').getBoundingClientRect().width)})""")
    check("390px 登录卡片完整可见", narrow["sw"] <= narrow["vw"] + 2
          and 0 < narrow["w"] < narrow["vw"], str(narrow))
    check("登录页没有 JS 报错", not errs, "; ".join(errs[:2]))
    pg.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6672")
    args = ap.parse_args()
    login_url = (Path(__file__).resolve().parents[1] / "static/login.html").as_uri()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 950})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:140]))
        pg.goto(args.base, wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(500)
        main_theme(pg)
        responsive(pg)
        check("主界面没有 JS 报错", not errs, "; ".join(errs[:2]))
        pg.close()
        login_theme(b, login_url)
        b.close()
    print(f"\n结果：{len(FAIL)} 项失败")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
