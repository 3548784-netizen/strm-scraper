# -*- coding: utf-8 -*-
"""真实素材界面验收：只跑 D:\\Desktop\\test strm 这一套。

前置：``python tools/devserver.py --media-root "D:/Desktop/test strm" --data .devdata_real``
覆盖：文件夹勾选 → 开始刮削（齐全跳过）→ 开始重刮 → 任务列表删除 →
      选帧面板拖取景框并应用 → 设置页读写。

注意：会覆盖该目录下已生成的 poster / fanart / nfo（不动 .strm 本身）。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:6670"
ROOT = Path(r"D:\Desktop\test strm")
FAIL = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def js(pg, expr):
    return pg.evaluate(expr)


def wait_idle(pg, seconds=240):
    end = time.time() + seconds
    while time.time() < end:
        st = json.loads(js(pg, "fetch('/api/status').then(r=>r.text())"))
        single = (st.get("single") or {}).get("count")
        if st["state"] == "idle" and not st["active"] and not single:
            return True
        pg.wait_for_timeout(1500)
    return False


def toast(pg):
    return js(pg, "document.getElementById('toast').textContent")


def api_total(pg):
    """任务列表总条数（读接口，避免读到表格重渲染的中间态）。"""
    return json.loads(js(pg, "fetch('/api/items?limit=1').then(r=>r.text())"))["total"]


def gone(pg):
    """等 toast 淡出，避免上一条干扰下一条断言。"""
    pg.wait_for_timeout(3600)


def main() -> int:
    files = sorted((ROOT / "1" / "2").glob("*.strm"))
    poster = files[0].with_suffix("").with_name(files[0].stem + "-poster.jpg")
    print(f"素材：{len(files)} 个 strm，样例 {files[0].name[:24]}…")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error"
              and "Failed to load resource" not in m.text else None)
        pg.goto(BASE, wait_until="networkidle")
        js(pg, "window.confirm = () => true")

        print("\n[1] 文件管理：勾一个文件夹")
        pg.click('button[data-tab="files"]')
        pg.wait_for_selector("#fileBody tr.dirrow")
        pg.click('#fileBody span.dir:text-is("1")')
        pg.wait_for_selector("#fileBody tr.dirrow, #fileBody tr[data-path]")
        pg.wait_for_timeout(500)
        box = pg.locator('#fileBody input[data-dirpick]').first
        box.check()
        pg.wait_for_timeout(900)
        info = pg.inner_text("#fileSelInfo")
        check("工具栏显示已选 5 项 + 文件夹", "5" in info and "文件夹" in info, info)
        check("开始刮削按钮可用且带计数",
              pg.is_enabled("#btnFileScrape") and "(5)" in pg.inner_text("#btnFileScrape"),
              pg.inner_text("#btnFileScrape"))
        check("开始重刮按钮可用", pg.is_enabled("#btnFileRescrape"))

        print("\n[2] 点「开始刮削」：产物齐全应跳过")
        mtime = poster.stat().st_mtime if poster.exists() else 0
        pg.click("#btnFileScrape")
        pg.wait_for_timeout(2500)
        msg = toast(pg)
        check("提示产物已齐、不重刮", "都齐了" in msg or "跳过" in msg, msg)
        check("海报未被覆盖", poster.exists() and poster.stat().st_mtime == mtime)
        gone(pg)

        print("\n[3] 点「开始重刮」：覆盖旧图")
        pg.click("#btnFileRescrape")
        pg.wait_for_timeout(2000)
        check("提示已开始重刮", "重刮" in toast(pg), toast(pg))
        check("任务跑完", wait_idle(pg))
        check("海报被覆盖（mtime 变化）", poster.exists() and poster.stat().st_mtime != mtime)
        st = json.loads(js(pg, "fetch('/api/status').then(r=>r.text())"))
        check("本轮无失败", st["stats"]["failed"] == 0, str(st["stats"]))

        print("\n[4] 任务列表：勾选并删除一条记录")
        gone(pg)
        pg.click('button[data-tab="tasks"]')
        pg.wait_for_selector("#taskBody tr")
        before = api_total(pg)
        first = pg.locator("#taskBody input[data-tpick]").first
        path = first.get_attribute("data-tpick")
        first.check()
        pg.wait_for_timeout(400)
        check("任务工具栏显示已选", "1" in pg.inner_text("#taskSelInfo"),
              pg.inner_text("#taskSelInfo"))
        pg.click("#btnTaskDel")
        pg.wait_for_timeout(500)
        js(pg, "document.querySelector('#taskDelMenu [data-tdel=\"record\"]').click()")
        pg.wait_for_timeout(2000)
        check("提示已移除", "移除" in toast(pg), toast(pg))
        after = api_total(pg)
        check("列表少了一条", after == before - 1, f"{before} → {after}")
        check("磁盘上的 strm 没动", Path(path).exists())
        check("产物还在", poster.exists())
        gone(pg)

        print("\n[5] 选帧：拖取景框并应用")
        pg.click('button[data-tab="files"]')
        if not pg.locator("#fileBody tr[data-path]").count():
            pg.click('#fileBody span.dir:text-is("2")')
        pg.wait_for_selector("#fileBody tr[data-path]")
        pg.locator('#fileBody tr[data-path] button[data-act="pick"]').first.click()
        pg.wait_for_function("() => !document.getElementById('pickBox').hidden", timeout=120000)
        pg.wait_for_timeout(1200)
        check("取景框可见", pg.is_visible("#pickBox"))
        check("取景比例提示有百分比", "%" in pg.inner_text("#pickCropInfo"),
              pg.inner_text("#pickCropInfo"))
        title = pg.inner_text("#pickTitle")
        check("标题写明裁切对象与可拖动", "海报 2:3" in title and "拖动" in title, title)
        check("主按钮是「确定裁切」", pg.inner_text("#btnPickApply").strip() == "确定裁切",
              pg.inner_text("#btnPickApply"))
        # 框的像素比例要等于 2:3，且不能超出画面
        box_geo = js(pg, """(() => {const b=document.getElementById('pickBox').getBoundingClientRect();
            const s=document.getElementById('pickStage').getBoundingClientRect();
            return {w:b.width, h:b.height, inside:b.left>=s.left-1 && b.top>=s.top-1
                    && b.right<=s.right+1 && b.bottom<=s.bottom+1};})()""")
        check("裁切框是 2:3", abs(box_geo["w"] / box_geo["h"] - 2 / 3) < 0.02,
              f'{box_geo["w"]:.0f}×{box_geo["h"]:.0f}')
        check("裁切框在画面内", box_geo["inside"])
        stage = pg.locator("#pickStage").bounding_box()
        fit = js(pg, "PICK.fit")
        before = js(pg, "PICK.crop")

        # 框内按住拖动 = 平移取景范围
        cx = stage["x"] + fit["left"] + fit["width"] * 0.5
        cy = stage["y"] + fit["top"] + fit["height"] * 0.5
        pg.mouse.move(cx, cy)
        pg.mouse.down()
        pg.mouse.move(cx + 55, cy + 18, steps=14)
        pg.mouse.up()
        pg.wait_for_timeout(700)
        moved = js(pg, "PICK.crop")
        check("取景范围可平移", moved["x"] != before["x"] or moved["y"] != before["y"],
              f'{before["x"]:.2f},{before["y"]:.2f} → {moved["x"]:.2f},{moved["y"]:.2f}')
        check("平移后大小不变", abs(moved["w"] - before["w"]) < 1e-6)

        # 拖右下角 = 缩放，且锁定 2:3
        hx = stage["x"] + fit["left"] + (moved["x"] + moved["w"]) * fit["width"]
        hy = stage["y"] + fit["top"] + (moved["y"] + moved["h"]) * fit["height"]
        pg.mouse.move(hx - 2, hy - 2)
        pg.mouse.down()
        pg.mouse.move(hx - 85, hy - 40, steps=14)
        pg.mouse.up()
        pg.wait_for_timeout(700)
        small = js(pg, "PICK.crop")
        check("取景范围可缩放", small["w"] < moved["w"] - 0.01,
              f'{moved["w"]:.2f} → {small["w"]:.2f}')
        # 归一化坐标里的宽高比要换算成像素比才是海报的 2:3
        ratio = (small["w"] * fit["iw"]) / (small["h"] * fit["ih"])
        check("缩放后仍是海报 2:3", abs(ratio - 2 / 3) < 0.02, f"像素比 {ratio:.3f}")
        # 预览图应当有内容
        prev = js(pg, """(() => {const im = document.getElementById('pickCropView');
            return im && im.naturalWidth ? im.naturalWidth + 'x' + im.naturalHeight : '';})()""")
        check("右侧取景预览已出图", bool(prev), prev)
        pg.click("#btnPickApply")
        pg.wait_for_timeout(4000)
        msg = toast(pg)
        check("应用提示生成海报", "海报" in msg, msg)
        saved = json.loads(js(pg, "fetch('/api/items?limit=50').then(r=>r.text())"))
        picked = [i for i in saved["items"] if i.get("frame_at")]
        check("手选帧已记住", bool(picked), str([i.get("frame_at") for i in picked][:3]))
        if pg.is_visible("#btnClosePick"):
            pg.click("#btnClosePick")
        gone(pg)

        print("\n[6] 设置页：读出来、改并发、保存")
        pg.click('button[data-tab="settings"]')
        pg.wait_for_selector("#setConcurrency", state="visible", timeout=15000)
        wait = 0
        while not pg.input_value("#setConcurrency") and wait < 20:
            pg.wait_for_timeout(300)
            wait += 1
        conc = pg.input_value("#setConcurrency")
        roots = pg.input_value("#setRoots")
        check("并发数已回填", conc.isdigit(), conc)
        check("扫描目录已回填", "test strm" in roots, roots)
        pg.fill("#setConcurrency", "3")
        pg.click("#btnSaveSettings")
        pg.wait_for_timeout(2500)
        check("保存成功提示", "已保存" in toast(pg) or "保存" in toast(pg), toast(pg))
        pg.reload(wait_until="networkidle")
        pg.click('button[data-tab="settings"]')
        pg.wait_for_selector("#setConcurrency", state="visible")
        pg.wait_for_timeout(1200)
        check("刷新后并发仍是 3（已落盘）", pg.input_value("#setConcurrency") == "3",
              pg.input_value("#setConcurrency"))
        js(pg, "window.confirm = () => true")
        pg.fill("#setConcurrency", conc)
        pg.click("#btnSaveSettings")
        pg.wait_for_timeout(2000)

        check("页面无 JS 报错", not errors, "; ".join(errors[:2]))
        browser.close()

    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：{FAIL}"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
