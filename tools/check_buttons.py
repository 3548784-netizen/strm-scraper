# -*- coding: utf-8 -*-
"""按钮一致性回归：统一规范、状态机、重复提交、显示条件与反馈。

    python tools/check_buttons.py --base http://127.0.0.1:6687

只读：不发任何写接口，网络请求在页面里用假的 fetch 拦住，测完即恢复。
退出码非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
OK = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    src = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
        encoding="utf-8")
    login_src = (Path(__file__).resolve().parent.parent / "static" / "login.html").read_text(
        encoding="utf-8")

    print("\n---------- 1. 源码层：统一的按钮契约 ----------")
    check("统一状态机 runBtn 已定义", "async function runBtn(" in src)
    check("按钮说明契约表已定义", "const BTN_HINTS = {" in src)
    check("统一确认函数已定义", "function askConfirm(" in src)
    check("所有异步按钮复用 runBtn", src.count("runBtn(") >= 25,
          f"实际 {src.count('runBtn(')} 处")
    check("业务代码不再直接调原生 confirm", src.count("confirm(") == 1,
          f"剩余 {src.count('confirm(')} 处")
    check("不再手写 btn.disabled = true 置灰",
          "btn.disabled = true" not in src and "$('btnFolderArt').disabled = true" not in src)
    check("不再用 className 整体覆盖按钮类", ".className = paused" not in src)
    check("禁用态只定义一处且为 0.38",
          src.count("button:disabled, button[aria-disabled=\"true\"]") == 1
          and "opacity: .42" not in src)
    check("加载态同时支持 is-loading 与 data-loading",
          "button.is-loading, button[data-loading=\"1\"]" in src)
    check("登录页与主站同一套状态语义",
          "button[data-loading=\"1\"]" in login_src and 'aria-busy' in login_src
          and "button:disabled, button[aria-disabled=\"true\"]" in login_src)

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(args.base, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1500)

        print("\n---------- 2. 视觉：三档尺寸 / 四级语义 / 五态 ----------")
        shape = pg.evaluate("""() => {
          const b = id => document.getElementById(id);
          const h = id => Math.round(b(id).getBoundingClientRect().height);
          const cssH = id => Math.round(parseFloat(getComputedStyle(b(id)).height));
          const opacity = id => {
            const el = b(id), was = el.disabled;
            el.disabled = true;
            const v = getComputedStyle(el).opacity;
            el.disabled = was;
            return v;
          };
          return { normal: h('btnStart'), tiny: h('btnTaskRefresh'),
                   large: cssH('btnPickApply'),
                   op: ['btnStart','btnTaskRefresh','btnPickApply','btnFileScrape'].map(opacity),
                   spin: (() => {
                     const el = b('btnStart');
                     el.classList.add('is-loading');
                     const s = getComputedStyle(el, '::before');
                     const r = {a: s.animationName, w: s.width,
                                pe: getComputedStyle(el).pointerEvents};
                     el.classList.remove('is-loading');
                     return r;
                   })() };
        }""")
        check("三档尺寸：32 / 28 / 36",
              shape["normal"] == 32 and shape["tiny"] == 28 and shape["large"] == 36,
              str(shape))
        check("所有按钮禁用态统一 0.38", set(shape["op"]) == {"0.38"}, str(shape["op"]))
        check("加载态圆环 12px 且不再接收点击",
              shape["spin"]["a"] == "amane-spin" and shape["spin"]["w"] == "12px"
              and shape["spin"]["pe"] == "none", str(shape["spin"]))

        hover = pg.evaluate("""async () => {
          const out = {};
          for (const [name, sel] of Object.entries({
            primary: '#btnStart', outline: '#btnFileScrape',
            ghost: '#btnTaskRefresh', danger: '#btnLogout'})) {
            const el = document.querySelector(sel);
            const before = getComputedStyle(el).backgroundColor;
            el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true}));
            out[name] = [before, getComputedStyle(el).backgroundColor];
          }
          return out;
        }""")
        # hover 用真实鼠标更准，下面单独跑；这里只确认语义类都还在
        classes = pg.evaluate("""() => ({
          primary: document.getElementById('btnStart').className,
          outline: document.getElementById('btnFileScrape').className,
          ghost: document.getElementById('btnTaskRefresh').className,
          subtle: [...document.querySelectorAll('.rowbtns button.danger, button[data-srvdel].danger')].length,
          notSubtle: document.querySelectorAll(
            '.rowbtns button.danger:not(.subtle), button[data-srvdel].danger:not(.subtle)').length,
        })""")
        check("层级 class 齐全（primary / accent-outline / ghost）",
              "primary" in classes["primary"] and "accent-outline" in classes["outline"]
              and "ghost" in classes["ghost"], str(classes))
        # 真 hover：主按钮与描边按钮都要有可见变化
        pg.hover("#btnStart")
        pg.wait_for_timeout(240)
        solid_hover = pg.evaluate(
            "getComputedStyle(document.getElementById('btnStart')).backgroundColor")
        pg.click('[data-tab="settings"]')
        pg.wait_for_timeout(300)
        outline_before = pg.evaluate(
            "getComputedStyle(document.getElementById('btnStart')).backgroundColor")
        pg.hover("#btnStart")
        pg.wait_for_timeout(240)
        outline_hover = pg.evaluate(
            "getComputedStyle(document.getElementById('btnStart')).backgroundColor")
        check("主按钮与描边按钮悬停都有反馈",
              solid_hover != "rgb(0, 85, 204)" and outline_before != outline_hover,
              f"{solid_hover} / {outline_before} → {outline_hover}")
        pg.click('[data-tab="tasks"]')
        pg.wait_for_timeout(250)

        print("\n---------- 3. 契约表与显示条件 ----------")
        hints = pg.evaluate("""() => {
          const ids = ['btnStart','btnFull','btnPause','btnCancel','btnRetry',
                       'btnFileScrape','btnFileRescrape','btnFileDel','btnFolderArt',
                       'btnSrvTest','btnSrvScan','btnSrvRefresh','btnAutoSave',
                       'btnSaveSettings','btnPickApply','btnLogDownload'];
          return ids.map(id => [id, document.getElementById(id).getAttribute('title')]);
        }""")
        check("主要按钮都有统一悬停说明",
              all(t for _, t in hints), str([i for i, t in hints if not t]))

        auth = pg.evaluate("""() => ({
          top: document.getElementById('btnLogoutTop').hidden,
          settings: document.getElementById('btnLogout').hidden,
          save: document.getElementById('btnSaveAccount').disabled,
        })""")
        check("未启用登录时，顶栏与设置页退出按钮都隐藏、保存账号禁用",
              auth["top"] and auth["settings"] and auth["save"], str(auth))

        pg.click('[data-tab="files"]')
        pg.wait_for_timeout(900)
        subtle = pg.evaluate("""() => ({
          n: document.querySelectorAll('.rowbtns button.danger, button[data-srvdel].danger').length,
          bad: document.querySelectorAll(
            '.rowbtns button.danger:not(.subtle), button[data-srvdel].danger:not(.subtle)').length,
        })""")
        check("行内 / 列表破坏性按钮统一使用 danger subtle",
              src.count('class="tiny danger subtle"') >= 3 and subtle["bad"] == 0,
              f"模板 {src.count('class=\"tiny danger subtle\"')} 处 / DOM {subtle}")
        cond0 = pg.evaluate("""() => ['btnFileScrape','btnFileRescrape','btnFileDel','btnFolderArt']
          .map(id => document.getElementById(id).disabled)""")
        check("未勾选文件时，四个批量按钮都禁用", all(cond0), str(cond0))
        if pg.locator('#fileBody input[data-pick]').count():
            pg.locator('#fileBody input[data-pick]').first.click()
            pg.wait_for_timeout(350)
            cond1 = pg.evaluate("""() => ['btnFileScrape','btnFileRescrape','btnFileDel','btnFolderArt']
              .map(id => document.getElementById(id).disabled)""")
            check("勾选文件后，四个批量按钮一起可用", not any(cond1), str(cond1))
        else:
            check("勾选文件后，四个批量按钮一起可用（测试库无文件时跳过）", True)

        # 菜单显隐只测 aria 同步，不依赖「有没有勾文件」这个业务状态
        pg.evaluate("setBtnDisabled('btnFileDel', false)")
        pg.click("#btnFileDel")
        pg.wait_for_timeout(150)
        menu = pg.evaluate("""() => {
          const btn = document.getElementById('btnFileDel');
          const open = {expanded: btn.getAttribute('aria-expanded'),
                        hidden: document.getElementById('fileDelMenu').hidden,
                        disabled: btn.disabled,
                        haspopup: btn.getAttribute('aria-haspopup')};
          document.body.click();
          return [open, btn.getAttribute('aria-expanded')];
        }""")
        check("下拉按钮 aria-expanded 与显隐同步",
              menu[0]["expanded"] == "true" and not menu[0]["hidden"]
              and menu[1] == "false" and menu[0]["haspopup"] == "menu", str(menu))
        pg.evaluate("updateFileSelInfo()")

        watch = pg.evaluate("""() => {
          renderAutoSummary({count: 0, watch_available: false, watch_reason: '验收模拟'});
          const box = document.getElementById('autoWatch');
          const r = {disabled: box.disabled, checked: box.checked, title: box.title,
                     optsHidden: document.getElementById('autoWatchOpts').hidden};
          renderAutoSummary({count: 0, watch_available: true});
          r.recovered = !box.disabled;
          return r;
        }""")
        check("实时监听不可用时统一禁用开关并写明原因，恢复后解锁",
              watch["disabled"] and not watch["checked"] and "验收模拟" in watch["title"]
              and watch["optsHidden"] and watch["recovered"], str(watch))

        print("\n---------- 4. 状态机：重复提交 / 加载 / 恢复 / 失败反馈 ----------")
        pg.click('[data-tab="servers"]')
        pg.wait_for_timeout(350)
        # 用页面内的假 fetch 卡住一个请求，完整观察按钮的一生
        pg.evaluate("""() => {
          window.__btnOrigFetch = window.fetch;
          window.__btnCalls = 0;
          window.__btnFail = false;
          window.__btnResolve = null;
          window.fetch = (url, opt) => {
            if (String(url).includes('/api/servers/test')) {
              window.__btnCalls++;
              return new Promise(resolve => {
                window.__btnResolve = () => resolve(new Response(
                  JSON.stringify(window.__btnFail
                    ? {detail: '模拟失败'} : {ok: true, message: '测试完成', servers: []}),
                  {status: window.__btnFail ? 500 : 200,
                   headers: {'Content-Type': 'application/json'}}));
              });
            }
            return window.__btnOrigFetch(url, opt);
          };
        }""")
        pg.click("#btnSrvTest")
        pg.wait_for_timeout(180)
        busy = pg.evaluate("""() => {
          const el = document.getElementById('btnSrvTest');
          return {disabled: el.disabled, loading: el.dataset.loading,
                  aria: el.getAttribute('aria-busy'), text: el.textContent.trim(),
                  spin: getComputedStyle(el, '::before').animationName};
        }""")
        check("请求中：禁用 + 圆环 + aria-busy + 进行中文案",
              busy["disabled"] and busy["loading"] == "1" and busy["aria"] == "true"
              and "中" in busy["text"] and busy["spin"] == "amane-spin", str(busy))
        # 连点第二次（含事件委托与直接 click 两种路径）
        pg.evaluate("""() => {
          document.getElementById('btnSrvTest').click();
          document.getElementById('btnSrvTest').dispatchEvent(
            new MouseEvent('click', {bubbles: true}));
        }""")
        pg.wait_for_timeout(150)
        calls = pg.evaluate("window.__btnCalls")
        check("请求中会挡住重复提交，只发一次请求", calls == 1, f"实际 {calls} 次")
        pg.evaluate("window.__btnResolve()")
        pg.wait_for_timeout(350)
        done = pg.evaluate("""() => {
          const el = document.getElementById('btnSrvTest');
          return {disabled: el.disabled, loading: el.dataset.loading || '',
                  aria: el.getAttribute('aria-busy'), text: el.textContent.trim(),
                  toast: document.getElementById('toast').textContent};
        }""")
        check("成功后按钮与文案完全恢复、无残留加载态",
              not done["disabled"] and not done["loading"] and not done["aria"]
              and done["text"] == "测试连接", str(done))
        check("成功有明确反馈", "测试完成" in done["toast"], done["toast"])

        pg.evaluate("window.__btnFail = true")
        pg.click("#btnSrvTest")
        pg.wait_for_timeout(150)
        pg.evaluate("window.__btnResolve()")
        pg.wait_for_timeout(350)
        failed = pg.evaluate("""() => {
          const el = document.getElementById('btnSrvTest');
          const t = document.getElementById('toast');
          return {disabled: el.disabled, loading: el.dataset.loading || '',
                  text: el.textContent.trim(), err: t.className.includes('err'),
                  toast: t.textContent};
        }""")
        check("失败后按钮恢复且统一给出错误反馈",
              not failed["disabled"] and not failed["loading"]
              and failed["text"] == "测试连接" and failed["err"]
              and "模拟失败" in failed["toast"], str(failed))
        pg.evaluate("window.fetch = window.__btnOrigFetch")

        print("\n---------- 5. 点击反馈：下载 / 新增服务器 ----------")
        dl = pg.evaluate("""() => {
          const real = window.open;
          window.open = () => null;
          document.getElementById('btnLogDownload').click();
          const blocked = document.getElementById('toast').textContent;
          const blockedErr = document.getElementById('toast').className.includes('err');
          window.open = () => ({});
          document.getElementById('btnLogDownload').click();
          const ok = document.getElementById('toast').textContent;
          const okErr = document.getElementById('toast').className.includes('err');
          window.open = real;
          return {blocked, blockedErr, ok, okErr};
        }""")
        check("下载：被拦截报失败、正常打开报成功",
              dl["blockedErr"] and "拦截" in dl["blocked"]
              and not dl["okErr"] and "已在新窗口打开" in dl["ok"], str(dl))
        pg.click('[data-tab="settings"]')
        pg.wait_for_timeout(600)
        pg.click("#btnSrvAdd")
        pg.wait_for_timeout(200)
        added = pg.evaluate("document.getElementById('toast').textContent")
        check("新增服务器行有明确反馈", "已新增一行" in added, added)

        print("\n---------- 6. 勾选后工具条不跳位 ----------")
        # 选中数曾经既写在按钮上、又带「（含 N 个文件夹）」，勾选后整行变宽：
        # 宽屏会把右侧筛选组挤到第二行，所有宽度下批量按钮都会整体右移。
        snap = """() => {
          const r = el => { const b = el.getBoundingClientRect();
            return {x: Math.round(b.left), y: Math.round(b.top),
                    h: Math.round(b.height), w: Math.round(b.width)}; };
          const q = s => document.querySelector(s);
          return {bar: r(q('.pane[data-pane="files"] .toolbar')),
                  ops: r(q('.pane[data-pane="files"] .tb-ops')),
                  strip: r(document.getElementById('posterStrip')),
                  sel: r(document.getElementById('fileSelInfo')),
                  btns: ['btnFileScrape','btnFileRescrape','btnFolderArt','btnFileDel']
                    .map(id => r(document.getElementById(id))),
                  scrape: document.getElementById('btnFileScrape').textContent.trim(),
                  title: document.getElementById('fileSelInfo').title,
                  clipped: (() => { const el = document.getElementById('fileSelInfo');
                    return el.scrollWidth > el.clientWidth + 1; })()};
        }"""
        for w in (1440, 1280, 1100, 1024):
            pg.set_viewport_size({"width": w, "height": 900})
            pg.click('[data-tab="files"]')
            pg.wait_for_timeout(700)
            before = pg.evaluate(snap)
            box = pg.query_selector("#fileBody input[data-dirpick]")
            if not box:
                check(f"{w}px：夹具里有可勾选的文件夹", False)
                continue
            box.click()
            pg.wait_for_timeout(350)
            after = pg.evaluate(snap)
            check(f"{w}px：勾选后四个批量按钮原地不动",
                  all(a["x"] == b["x"] for a, b in zip(before["btns"], after["btns"])),
                  f"{[b['x'] for b in before['btns']]} -> {[a['x'] for a in after['btns']]}")
            check(f"{w}px：工具条高度 / 右侧控件组 / 海报条都不跳",
                  before["bar"]["h"] == after["bar"]["h"]
                  and before["ops"]["y"] == after["ops"]["y"]
                  and before["strip"]["y"] == after["strip"]["y"],
                  f"bar {before['bar']['h']}->{after['bar']['h']}, "
                  f"ops {before['ops']['y']}->{after['ops']['y']}, "
                  f"strip {before['strip']['y']}->{after['strip']['y']}")
            check(f"{w}px：选中数槽宽度固定且文案没被裁",
                  before["sel"]["w"] == after["sel"]["w"] and after["sel"]["w"] > 0
                  and not after["clipped"],
                  f"{before['sel']['w']} -> {after['sel']['w']}, clipped={after['clipped']}")
            check(f"{w}px：按钮文案恒定、文件夹数仍在悬停说明里",
                  after["scrape"] == "开始刮削" and "个文件夹" in after["title"],
                  f"{after['scrape']} / {after['title']}")
            box.click()                       # 取消勾选，复原
            pg.wait_for_timeout(200)

        pg.set_viewport_size({"width": 1440, "height": 900})
        pg.click('[data-tab="tasks"]')
        pg.wait_for_timeout(700)
        tsnap = """() => {
          const r = el => { const b = el.getBoundingClientRect();
            return {x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width)}; };
          return {ops: r(document.querySelector('.pane[data-pane="tasks"] .tb-ops')),
                  sel: r(document.getElementById('taskSelInfo')),
                  btns: ['btnTaskDel','btnRetry','btnTaskRefresh']
                    .map(id => r(document.getElementById(id))),
                  title: document.getElementById('taskSelInfo').title,
                  clipped: (() => { const el = document.getElementById('taskSelInfo');
                    return el.scrollWidth > el.clientWidth + 1; })()};
        }"""
        t_before = pg.evaluate(tsnap)
        pg.click("#taskAll")
        pg.wait_for_timeout(400)
        t_after = pg.evaluate(tsnap)
        check("任务列表：全选后右侧操作组与三个按钮原地不动",
              t_before["ops"]["x"] == t_after["ops"]["x"]
              and all(a["x"] == b["x"] for a, b in zip(t_before["btns"], t_after["btns"])),
              f"ops {t_before['ops']['x']}->{t_after['ops']['x']}, "
              f"btns {[b['x'] for b in t_before['btns']]} -> {[a['x'] for a in t_after['btns']]}")
        check("任务列表：选中数槽宽度固定且完整信息在悬停说明里",
              t_before["sel"]["w"] == t_after["sel"]["w"] and bool(t_after["title"])
              and not t_after["clipped"],
              f"{t_before['sel']['w']} -> {t_after['sel']['w']} / {t_after['title']}")
        pg.click("#taskAll")
        pg.wait_for_timeout(200)

        check("整轮没有 JS 报错", not errors, str(errors[:3]))
        b.close()

    print(f"\n结果：{len(FAIL)} 项失败，{OK} 项通过")
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
