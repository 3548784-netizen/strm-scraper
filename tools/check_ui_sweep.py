# -*- coding: utf-8 -*-
"""全量界面巡检：把每个标签页、每个按钮、每个弹窗都点一遍。

别的脚本验的是「某个功能对不对」，这个验的是「点下去会不会炸」：
点完有没有 JS 报错、接口有没有 4xx/5xx、弹窗能不能用四种方式关掉
（关闭按钮 / 取消 / Esc / 点遮罩）、窄屏下有没有横向滚动条、浮层会不会跑出屏幕。

    # 只读巡检（默认）：跳过会写盘、会开刮削的按钮
    python tools/check_ui_sweep.py --base http://127.0.0.1:6672
    # 连破坏性按钮一起点（只在夹具环境用）
    python tools/check_ui_sweep.py --base http://127.0.0.1:6672 --destructive

退出码非 0 表示有失败项。截图写到 .uisweep/（--shots 可改）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
NOTE: list[str] = []

# 点了会写盘 / 会跑任务 / 会删东西的按钮：只读巡检时跳过
DESTRUCTIVE = {
    "btnStart", "btnPause", "btnRetry", "btnFull", "btnPosterOnly", "btnCancel",
    "btnFileScrape", "btnFileRescrape", "btnFileDel", "btnSaveSettings",
    "btnSaveAccount", "btnLogDownload", "btnSrvTest", "btnSrvScan", "btnSrvRefresh",
    "btnAutoSave", "btnArtApply", "btnArtAuto", "btnPickApply", "btnPickAuto",
}
# 任务卡 / 文件行里的写操作
DESTRUCTIVE_ACT = {"redo", "clean", "del", "run", "scrape", "add"}

TABS = ["tasks", "files", "servers", "automation", "logs", "settings"]
VIEWPORTS = [(1500, 950), (1280, 800), (1024, 700)]


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAIL.append(name)


def note(text: str) -> None:
    print("  ..   " + text)
    NOTE.append(text)


def api(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as resp:
        return json.loads(resp.read())


class Page:
    """包一层：每次操作后收集报错，方便定位是哪一下点炸的。"""

    def __init__(self, page):
        self.p = page
        self.errors: list[str] = []
        self.http_bad: list[str] = []
        self.frame_fail = False
        page.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)[:200]))
        # 浏览器给「资源 4xx」打的 console 没有 URL，重复又定位不到；
        # HTTP 错误下面用 response 事件单独统计（带 URL）
        page.on("console", lambda m: self.errors.append("console: " + m.text[:200])
                if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("response", self._on_response)
        page.on("dialog", lambda d: d.accept())

    def _on_response(self, resp):
        if resp.status >= 400 and "/api/" in resp.url:
            # 夹具里的 strm 指向 example.invalid，抽帧本来就该失败；
            # 这里放过一马，改成在选帧弹窗里断言「失败要有话说」
            if "frames/plan" in resp.url:
                self.frame_fail = True
                return
            self.http_bad.append(f"{resp.status} {resp.url.split('/api/')[-1][:60]}")

    def mark(self) -> tuple[int, int]:
        return len(self.errors), len(self.http_bad)

    def since(self, m: tuple[int, int]) -> str:
        errs = self.errors[m[0]:]
        bad = self.http_bad[m[1]:]
        return "; ".join((errs + bad)[:2])


def settle(pg: Page, ms: int = 450) -> None:
    pg.p.wait_for_timeout(ms)


def close_overlays(pg: Page) -> None:
    """把可能被上一次点击打开的浮层/弹窗都收掉。"""
    page = pg.p
    if page.is_visible("#libMenu"):
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
    for mid in ("#autoModal", "#previewModal", "#artModal", "#pickModal"):
        if page.is_visible(mid):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                if page.is_visible(mid):
                    page.evaluate("""(id) => { document.querySelector(id).hidden = true; }""", mid)
            except Exception:  # noqa: BLE001
                pass


def login_if_needed(pg: Page) -> None:
    page = pg.p
    if page.query_selector("#username") and page.is_visible("#username"):
        page.fill("#username", "admin")
        page.fill("#password", "admin")
        page.click("#btn")
        page.wait_for_timeout(1500)
        note("这个实例开了登录，用 admin/admin 登进去了")


def goto_tab(pg: Page, tab: str) -> bool:
    page = pg.p
    try:
        page.click(f'[data-tab="{tab}"]')
    except Exception as exc:  # noqa: BLE001
        check(f"能切到「{tab}」标签页", False, str(exc)[:60])
        return False
    page.wait_for_timeout(1200)
    return True


class Sweep:
    def __init__(self, base: str, shots: Path, destructive: bool):
        self.base = base.rstrip("/")
        self.shots = shots
        self.destructive = destructive
        self.preview_skipped = False
        self.shots.mkdir(parents=True, exist_ok=True)

    # ---------------- 1. 标签页 ----------------
    def tabs(self, pg: Page) -> None:
        print("\n---------- 1. 六个标签页来回切 ----------")
        page = pg.p
        for tab in TABS:
            m = pg.mark()
            if not goto_tab(pg, tab):
                continue
            info = page.evaluate("""(tab) => {
                const panes = [...document.querySelectorAll('section.pane')];
                const on = panes.filter(s => s.classList.contains('active')).map(s => s.dataset.pane);
                const btn = document.querySelector(`[data-tab="${tab}"]`);
                return {on, tabOn: btn && btn.classList.contains('active'),
                        visible: panes.some(s => s.classList.contains('active') && s.offsetHeight > 0)};
            }""", tab)
            check(f"「{tab}」只有一个面板亮着", info["on"] == [tab], str(info["on"]))
            check(f"「{tab}」标签按钮是选中态", bool(info["tabOn"]))
            check(f"「{tab}」面板真的显示出来了", bool(info["visible"]))
            check(f"「{tab}」切换没有报错", not pg.since(m), pg.since(m))
            page.screenshot(path=str(self.shots / f"tab-{tab}.png"))

    # ---------------- 2. 弹窗四种关法 ----------------
    def _open_auto_modal(self, page) -> bool:
        try:
            page.click("#btnAutoAdd")
            page.wait_for_selector("#autoModal:not([hidden])", timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _open_preview(self, page) -> bool:
        # 图片预览得点一张已生成的海报 / 背景图。测试库被别的脚本清过产物后
        # 一张都没有，这属于夹具前提问题，不该算成界面回归失败（同 check_dirview）。
        self.preview_skipped = False
        try:
            page.evaluate("v => setTaskView(v)", "flat")
            page.wait_for_timeout(400)
            thumbs = page.locator("#taskBody img.thumb")
            if thumbs.count() == 0:
                self.preview_skipped = True
                return False
            thumbs.first.click()
            page.wait_for_selector("#previewModal:not([hidden])", timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _open_art(self, page) -> bool:
        # 「封面」按钮在文件管理的文件夹行上（任务列表的目录树里没有）
        try:
            page.click('[data-tab="files"]')
            page.wait_for_timeout(1500)
            page.wait_for_selector("#fileBody [data-artdir]", timeout=6000)
            page.locator("#fileBody [data-artdir]").first.click()
            page.wait_for_selector("#artModal:not([hidden])", timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _open_pick(self, page) -> bool:
        try:
            page.click('[data-tab="tasks"]')
            page.wait_for_timeout(1200)
            page.evaluate("v => setTaskView(v)", "flat")
            page.wait_for_selector('#taskBody [data-act="pick"]', timeout=6000)
            page.locator('#taskBody [data-act="pick"]').first.click()
            page.wait_for_selector("#pickModal:not([hidden])", timeout=6000)
            page.wait_for_timeout(800)
            return True
        except Exception:  # noqa: BLE001
            return False

    def modals(self, pg: Page) -> None:
        print("\n---------- 2. 四个弹窗 × 四种关法 ----------")
        page = pg.p
        plan = [
            ("新建任务", "#autoModal", self._open_auto_modal, "automation",
             [("#btnCloseAuto", "关闭按钮"), ("#btnAutoCancel", "取消按钮"),
              ("Esc", "Esc 键"), ("backdrop", "点遮罩")]),
            ("图片预览", "#previewModal", self._open_preview, "tasks",
             [("#btnClosePreview", "关闭按钮"), ("Esc", "Esc 键"), ("backdrop", "点遮罩")]),
            ("封面挑选", "#artModal", self._open_art, "files",
             [("#btnCloseArt", "关闭按钮"), ("Esc", "Esc 键"), ("backdrop", "点遮罩")]),
            ("选帧", "#pickModal", self._open_pick, "tasks",
             [("#btnClosePick", "关闭按钮"), ("Esc", "Esc 键"), ("backdrop", "点遮罩")]),
        ]
        for label, mid, opener, tab, closers in plan:
            goto_tab(pg, tab)      # 按钮在别的标签页里，先切过去
            page.wait_for_timeout(600)
            if not opener(page):
                if label == "图片预览" and self.preview_skipped:
                    note("图片预览：夹具里没有海报 / 背景图产物，跳过（不计失败）")
                else:
                    check(f"「{label}」弹窗能打开", False, "点了没反应")
                continue
            check(f"「{label}」弹窗能打开", page.is_visible(mid),
                  page.inner_text(f"{mid} .modal-head h2") if page.is_visible(mid) else "")
            if mid == "#pickModal":
                # 抽帧失败（夹具里的链接是假的）时，标题不能一直挂着「正在抽帧…」
                hint = page.inner_text("#pickStageHint")
                title = page.inner_text("#pickTitle")
                if "正在读取视频信息" not in hint:
                    check("选帧出结果后标题不再写「正在抽帧…」",
                          "正在抽帧" not in title, title)
                    check("抽帧失败时界面上说清了原因",
                          bool(hint.strip()) and hint.strip() != "—", hint[:60])
            # 弹窗开着的时候不该还能滚背后的页面
            locked = page.evaluate("() => getComputedStyle(document.body).overflow")
            note(f"{label} 打开时 body overflow = {locked}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)

            for sel, how in closers:
                if not opener(page):
                    goto_tab(pg, tab)
                    page.wait_for_timeout(400)
                    opener(page)
                if not page.is_visible(mid):
                    check(f"「{label}」重新打开（验 {how}）", False)
                    continue
                m = pg.mark()
                if sel == "Esc":
                    page.keyboard.press("Escape")
                elif sel == "backdrop":
                    page.mouse.click(6, 6)
                else:
                    try:
                        page.click(sel, timeout=3000)
                    except Exception:  # noqa: BLE001
                        check(f"「{label}」{how}能点", False, "点不到")
                        close_overlays(pg)
                        continue
                page.wait_for_timeout(400)
                gone = not page.is_visible(mid)
                check(f"「{label}」{how}能关掉", gone)
                check(f"「{label}」{how}关掉时没报错", not pg.since(m), pg.since(m))
                if not gone:
                    close_overlays(pg)

    # ---------------- 3. 按钮 ------------
    def buttons(self, pg: Page) -> None:
        print("\n---------- 3. 每个标签页里的按钮都点一遍 ----------")
        page = pg.p
        for tab in TABS:
            if not goto_tab(pg, tab):
                continue
            page.wait_for_timeout(300)
            ids = page.eval_on_selector_all(
                "section.pane.active button[id]", "els => els.map(e => e.id)")
            acts = page.eval_on_selector_all(
                "section.pane.active button[data-act], section.pane.active button[data-artdir]",
                "els => els.map(e => e.dataset.act || 'artdir')")
            buttons = [f"#{i}" for i in ids if i not in DESTRUCTIVE or self.destructive]
            # 选帧在弹窗那一节专门验过（会去打视频源，夹具里必然失败）
            buttons += [f'{a}' for a in dict.fromkeys(acts)
                        if (a not in DESTRUCTIVE_ACT or self.destructive) and a != "pick"]
            skipped = [i for i in ids if i in DESTRUCTIVE and not self.destructive]
            if skipped:
                note(f"「{tab}」跳过写操作按钮：{' '.join(skipped)}")

            for label in buttons:
                m = pg.mark()
                try:
                    if label == "artdir":
                        page.locator('[data-artdir]').first.click(timeout=3000)
                    elif label in ("edit", "toggle", "pick", "redo", "clean", "del", "run", "scrape", "add"):
                        page.locator(f'[data-act="{label}"]').first.click(timeout=3000)
                    else:
                        page.click(label, timeout=3000)
                except Exception as exc:  # noqa: BLE001
                    # 按钮不可见 / 被禁用（比如没选中文件时的「删除」）算正常
                    msg = str(exc)
                    if "not visible" in msg or "disabled" in msg or "outside of the viewport" in msg:
                        note(f"「{tab}」{label} 当前不可点（没选中东西 / 禁用），跳过")
                    else:
                        check(f"「{tab}」{label} 能点", False, msg.split("\n")[0][:70])
                    close_overlays(pg)
                    continue
                settle(pg)
                check(f"「{tab}」{label} 点完没报错", not pg.since(m), pg.since(m))
                close_overlays(pg)

    # ---------------- 4. 浮层定位 ----------------
    def libmenu(self, pg: Page) -> None:
        print("\n---------- 4. 三处库名浮层都贴着按钮 ----------")
        page = pg.p
        goto_tab(pg, "tasks")
        page.wait_for_timeout(400)
        # (名字, 点哪里, 浮层该贴谁, 额外步骤)
        # 面包屑是「点小箭头、贴整块库名」，所以点击和锚点不是同一个元素
        spots = [("任务列表横条", "#taskLibBar [data-libmenu]", "#taskLibBar [data-libmenu]", {}),
                 ("文件管理面包屑", "#crumbs .caret[data-libmenu]", "#crumbs .crumb.libcrumb",
                  {"tab": "files"}),
                 ("自动化监控范围", "#autoLib", "#autoLib", {"modal": True})]
        for label, sel, anchor, how in spots:
            if how.get("tab"):
                goto_tab(pg, how["tab"])
                page.wait_for_timeout(1400)
            if how.get("modal"):
                goto_tab(pg, "automation")
                page.wait_for_timeout(900)
                if not self._open_auto_modal(page):
                    check(f"「{label}」浮层", False, "弹窗打不开")
                    continue
            try:
                page.click(sel, timeout=4000)
                page.wait_for_selector("#libMenu:not([hidden])", timeout=4000)
            except Exception:  # noqa: BLE001
                check(f"「{label}」点开有浮层", False, "没弹出来")
                close_overlays(pg)
                continue
            box = page.evaluate("""(sel) => {
                const m = document.getElementById('libMenu').getBoundingClientRect();
                const a = document.querySelector(sel).getBoundingClientRect();
                return {ml: m.left, mt: m.top, mr: m.right, mb: m.bottom,
                        al: a.left, ab: a.bottom, ar: a.right,
                        vw: innerWidth, vh: innerHeight,
                        z: +getComputedStyle(document.getElementById('libMenu')).zIndex,
                        mz: +getComputedStyle(document.querySelector('.modal:not([hidden])') || document.body).zIndex || 0};
            }""", anchor)
            check(f"「{label}」浮层贴着按钮", abs(box["ml"] - box["al"]) < 26
                  and box["ab"] - 20 <= box["mt"] <= box["ab"] + 20,
                  f'菜单({box["ml"]:.0f},{box["mt"]:.0f}) 按钮左 {box["al"]:.0f} 底 {box["ab"]:.0f}')
            check(f"「{label}」浮层没出屏", box["ml"] >= 0 and box["mr"] <= box["vw"] + 1
                  and box["mt"] >= 0 and box["mb"] <= box["vh"] + 1,
                  f'右下 {box["mr"]:.0f},{box["mb"]:.0f} / 视口 {box["vw"]}x{box["vh"]}')
            if how.get("modal"):
                check(f"「{label}」浮层压得住弹窗", box["z"] > box["mz"],
                      f'浮层 z={box["z"]} 弹窗 z={box["mz"]}')
            # Esc 和点外面都该收起来
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            check(f"「{label}」Esc 收起浮层", not page.is_visible("#libMenu"))
            close_overlays(pg)

    # ---------------- 4b. 刷新后记得上次的样子 ----------------
    def remember(self, pg: Page) -> None:
        print("\n---------- 4b. 刷新后还记得上次的标签页 / 目录 / 排序 ----------")
        page = pg.p
        goto_tab(pg, "files")
        page.wait_for_timeout(1500)
        names = page.evaluate(
            "() => [...document.querySelectorAll('#fileBody span.dir[data-dir]')].map(e => e.dataset.dir)")
        if not names:
            note("这个库根目录下没有子目录，跳过「记住目录」这条")
            return
        page.evaluate("""(p) => {
            const el = [...document.querySelectorAll('#fileBody span.dir[data-dir]')]
                .find(e => e.dataset.dir === p);
            if (el) el.click();
        }""", names[0])
        page.wait_for_timeout(1600)
        page.click('#fileSort button[data-sort="name"]')
        page.wait_for_timeout(1000)
        before = page.evaluate(
            "() => ({tab: S.tab, path: S.files.path, sort: S.files.sort})")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2600)
        after = page.evaluate(
            "() => ({tab: S.tab, path: S.files.path, sort: S.files.sort, "
            "pane: document.querySelector('.pane.active')?.dataset.pane})")
        check("刷新后还停在文件管理", after["tab"] == "files" and after["pane"] == "files",
              str(after["tab"]))
        check("刷新后还停在刚才那个目录", after["path"] == before["path"],
              (after["path"] or "").replace("\\", "/").split("/")[-1])
        check("刷新后排序还是按名称", after["sort"] == before["sort"],
              f'{before["sort"]["key"]} → {after["sort"]["key"]}')

    # ---------------- 5. 三种屏幕宽度 ----------------
    def layout(self, pg: Page) -> None:
        print("\n---------- 5. 三种宽度下不许横向滚动 / 按钮被裁 ----------")
        page = pg.p
        for w, h in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(700)
            for tab in TABS:
                if not goto_tab(pg, tab):
                    continue
                page.wait_for_timeout(500)
                info = page.evaluate("""() => {
                    const doc = document.documentElement;
                    const off = [...document.querySelectorAll(
                        'section.pane.active button, section.pane.active input, .tabs button')]
                      .filter(e => e.offsetParent !== null)
                      .map(e => ({t: (e.id || e.textContent || '').trim().slice(0, 14),
                                  r: e.getBoundingClientRect().right}))
                      .filter(x => x.r > window.innerWidth + 1);
                    return {sw: doc.scrollWidth, vw: window.innerWidth,
                            over: off.slice(0, 4), n: off.length};
                }""")
                check(f"{w}px「{tab}」没有横向滚动条", info["sw"] <= info["vw"] + 2,
                      f'scrollWidth {info["sw"]} / 视口 {info["vw"]}')
                check(f"{w}px「{tab}」没有控件被挤出屏幕", info["n"] == 0,
                      str(info["over"]))
                page.screenshot(path=str(self.shots / f"{w}-{tab}.png"))
        page.set_viewport_size({"width": 1500, "height": 950})

    def run(self) -> int:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            ctx = browser.new_context(viewport={"width": 1500, "height": 950})
            page = ctx.new_page()
            pg = Page(page)
            page.goto(self.base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            login_if_needed(pg)

            self.tabs(pg)
            self.modals(pg)
            self.buttons(pg)
            self.libmenu(pg)
            self.remember(pg)
            self.layout(pg)

            print("\n---------- 6. 收尾 ----------")
            check("整轮下来没有 JS 报错", not pg.errors, "; ".join(pg.errors[:3]))
            check("整轮下来没有接口 4xx/5xx", not pg.http_bad, "; ".join(pg.http_bad[:3]))
            browser.close()
        return 0 if not FAIL else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6672")
    ap.add_argument("--shots", default=".uisweep", help="截图目录")
    ap.add_argument("--destructive", action="store_true",
                    help="连「开始刮削 / 删除 / 保存」这些写操作按钮一起点（只在夹具上用）")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    try:
        st = api(base, "/api/status")
        note(f"目标 {base} · 共 {st.get('stats', {}).get('total', '?')} 条 · "
             f"根目录 {st.get('roots')}")
    except Exception as exc:  # noqa: BLE001
        print(f"服务没起来？{exc}")
        return 1

    return Sweep(base, Path(args.shots), args.destructive).run()


if __name__ == "__main__":
    sys.exit(main())
