# -*- coding: utf-8 -*-
"""界面回归测试：文件夹勾选 / 全选 / 单文件执行 / 自动刷新 / 删除产物。

先 `python tools/devserver.py` 起环境，再 `python tools/selftest_ui.py`。
需要本机装了 Edge（playwright 用 channel="msedge"）。
会反复清空刮削产物，不动源视频。
"""
import json
import os
import time
from pathlib import Path
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:6670"
FAIL = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api(path, data=None):
    if data is None:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return json.load(r)
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return {"__status__": exc.code, "detail": exc.read().decode()[:200]}


def wait_idle(pg, seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        st = api("/api/status")
        if st["state"] == "idle" and not (st.get("single") or {}).get("count"):
            return True
        pg.wait_for_timeout(1000)
    return False


def reset_all():
    """只清一个目录的产物。

    本机沙箱对单轮批量删除有上限（超过就直接杀进程），全清容易把 dev server
    一起带走；测试用例只依赖第一个目录处于待处理即可。
    """
    api("/api/task/cancel", {})
    time.sleep(1)
    files = api("/api/files")
    if not files.get("dirs"):
        return
    d = files["dirs"][0]
    sub = api("/api/files?path=" + urllib.request.quote(d["path"]))
    paths = [f["path"] for f in sub.get("files", [])]
    if paths:
        api("/api/files/delete", {"paths": paths, "what": ["poster", "fanart", "thumb", "nfo"],
                                  "reset": True})


print("========== 准备：清空产物 ==========")
reset_all()
print("  当前 stats:", api("/api/status")["stats"])

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1600, "height": 950})
    errs, bad = [], []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
    pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)

    pg.goto(BASE, wait_until="networkidle")
    pg.click('[data-tab="files"]')
    pg.wait_for_timeout(1200)

    print("========== 1. 文件夹勾选 & 全选的状态机 ==========")
    # 每个文件夹有几个 strm 由测试媒体库决定，别写死数字：勾文件夹是递归选中的，
    # 期望数量直接取文件夹行上的计数（那本来就是含子目录的总数）
    def dir_n() -> list[int]:
        return pg.evaluate("""() => [...document.querySelectorAll('#fileBody tr.dirrow')]
            .map(tr => Number((tr.querySelector('.cnt')?.textContent || '0')
                              .replace(/[^0-9]/g, '')))""")

    ns = dir_n()
    n1 = ns[0]
    n12 = sum(ns[:2])
    n_all = sum(ns) + pg.evaluate("() => (S.files.data.files || []).length")
    dboxes = pg.query_selector_all("#fileBody input[data-dirpick]")
    dboxes[0].click(); pg.wait_for_timeout(900)
    check(f"勾 1 个文件夹 → {n1} 项", f"{n1} 项" in pg.inner_text("#fileSelInfo"),
          pg.inner_text("#fileSelInfo").replace("\n", " "))
    check("只勾了一部分时全选框不勾", pg.is_checked("#fileAll") is False)

    dboxes[1].click(); pg.wait_for_timeout(1200)
    check(f"勾 2 个文件夹 → {n12} 项", f"{n12} 项" in pg.inner_text("#fileSelInfo"),
          pg.inner_text("#fileSelInfo").replace("\n", " "))
    check("还有没勾的文件夹时全选框不勾", pg.is_checked("#fileAll") is False)

    pg.click("#fileAll"); pg.wait_for_timeout(2500)
    check(f"全选 → {n_all} 项", f"{n_all} 项" in pg.inner_text("#fileSelInfo")
          and pg.is_checked("#fileAll"), pg.inner_text("#fileSelInfo").replace("\n", " "))

    pg.click("#fileAll"); pg.wait_for_timeout(1200)
    check("取消全选 → 0 项", "0 项" in pg.inner_text("#fileSelInfo"),
          pg.inner_text("#fileSelInfo").replace("\n", " "))
    check("取消全选后文件夹也清空",
          not any(x.is_checked() for x in pg.query_selector_all("#fileBody input[data-dirpick]")))

    pg.click("#fileAll"); pg.wait_for_timeout(2500)
    check(f"再全选 → {n_all} 项",
          f"{n_all} 项" in pg.inner_text("#fileSelInfo") and pg.is_checked("#fileAll"),
          pg.inner_text("#fileSelInfo").replace("\n", " "))

    print("========== 2. 勾文件夹后批量刮削（自动刷新） ==========")
    pg.click("#btnFileScrape")
    pg.wait_for_timeout(1500)
    check("批量刮削已提交", wait_idle(pg, 120))
    st = api("/api/status")
    check("批量跑完有结果", st["stats"]["ok"] + st["stats"]["skipped"] >= 5, str(st["stats"]))
    pg.wait_for_timeout(2500)   # 等 watchFiles 最后一次刷新
    pg.click("#fileBody .dir"); pg.wait_for_timeout(1500)
    body = pg.inner_text("#fileBody")
    check("页面自动显示新状态（无需手动刷新）", "成功" in body or "失败" in body,
          body[:120].replace("\n", " "))
    pg.click("[data-up]"); pg.wait_for_timeout(1200)

    print("========== 3. 单文件刮削自动刷新 ==========")
    pg.evaluate("() => { window.confirm = () => true; }")
    pg.click("#fileBody .dir"); pg.wait_for_timeout(1500)
    # 先删掉产物，再点行内刮削
    pg.query_selector_all('#fileBody button[data-act="clean"]')[0].click()
    pg.wait_for_timeout(1500)
    pg.query_selector_all('#fileBody button[data-act="scrape"]')[0].click()
    pg.wait_for_timeout(3000)
    ok = wait_idle(pg, 90)
    pg.wait_for_timeout(2500)
    row = pg.inner_text("#fileBody").replace("\n", " ")
    check("单文件刮削完成并自动刷新",
          ok and ("成功" in row or "已跳过" in row or "失败" in row), row[:140])

    print("========== 4. 删除产物（下拉） ==========")
    before = api("/api/files?path=" + urllib.request.quote(
        pg.evaluate("S.files.path")))
    had = sum(1 for f in before["files"] if f["artifacts"].get("poster"))
    pg.query_selector_all("#fileBody input[data-pick]")[0].click()
    pg.wait_for_timeout(600)
    pg.click("#btnFileDel"); pg.wait_for_timeout(400)
    pg.click('#fileDelMenu button[data-del="all"]')
    pg.wait_for_timeout(1800)
    after = api("/api/files?path=" + urllib.request.quote(pg.evaluate("S.files.path")))
    gone = sum(1 for f in after["files"] if not f["artifacts"].get("poster"))
    check("删除全部产物生效", had > 0 and gone > 0, f"删除前有海报 {had}，现缺 {gone}")

    print("========== 5. 只删 NFO ==========")
    pg.query_selector_all('#fileBody button[data-act="scrape"]')[0].click()
    pg.wait_for_timeout(3000)
    wait_idle(pg, 90)
    pg.wait_for_timeout(2000)
    pg.query_selector_all("#fileBody input[data-pick]")[0].click()
    pg.wait_for_timeout(500)
    pg.click("#btnFileDel"); pg.wait_for_timeout(400)
    pg.click('#fileDelMenu button[data-del="nfo"]')
    pg.wait_for_timeout(1500)
    cur = api("/api/files?path=" + urllib.request.quote(pg.evaluate("S.files.path")))
    no_nfo = all(not f["artifacts"].get("nfo") for f in cur["files"])
    has_poster = any(f["artifacts"].get("poster") for f in cur["files"])
    check("只删 NFO：NFO 没了、海报还在", no_nfo and has_poster,
          f"nfo全无={no_nfo} 海报还有={has_poster}")

    print("========== 6. 选文件夹开始刮削 → 任务列表按文件夹显示 ==========")
    pg.click('[data-tab="files"]'); pg.wait_for_timeout(700)
    if pg.query_selector("[data-up]"):
        pg.click("[data-up]"); pg.wait_for_timeout(1000)
    # 先清掉之前的勾选，只留这一步要选的文件夹（模拟用户真实操作）
    if pg.is_checked("#fileAll"):
        pg.click("#fileAll"); pg.wait_for_timeout(900)
    for row in pg.query_selector_all("#fileBody tr.dirrow"):
        if "剧集" in row.inner_text():
            row.query_selector("input[data-dirpick]").click()
            break
    pg.wait_for_timeout(900)
    before_attempts = {i["path"]: i["attempts"] for i in api("/api/items?size=200")["items"]}
    pg.click("#btnFileScrape"); pg.wait_for_timeout(1500)
    check("只跑勾选范围：模式标记为 selection", api("/api/status")["mode"] == "selection",
          str(api("/api/status")["mode"]))
    wait_idle(pg, 180)
    after_attempts = {i["path"]: i["attempts"] for i in api("/api/items?size=200")["items"]}
    touched = [p for p, n in after_attempts.items()
               if "剧集" not in p and n != before_attempts.get(p)]
    check("勾一个文件夹不会把别的文件夹一起跑", not touched,
          f"{len(touched)} 个别的目录被重跑：{touched[:2]}")
    pg.click('[data-tab="tasks"]'); pg.wait_for_timeout(2200)

    check("自动切到文件夹视图",
          pg.eval_on_selector("#taskView button.on", "e => e.dataset.view") == "dir")
    groups = pg.query_selector_all("#taskBody tr.grouprow")
    check("有分组行", len(groups) >= 2, f"{len(groups)} 组")
    check("刚选的文件夹排第一", groups and "剧集" in groups[0].inner_text(),
          groups[0].inner_text().replace("\n", " ") if groups else "—")
    check("带「刚选择」标记", bool(groups) and "刚选择" in groups[0].inner_text())
    check("先不展开（点了才展开）", len(pg.query_selector_all("#taskBody tr.gitem")) == 0)

    pg.click("#taskBody .gbtn"); pg.wait_for_timeout(1600)
    items = pg.query_selector_all("#taskBody tr.gitem")
    check("点开才显示里面的文件", len(items) > 0, f"{len(items)} 行")
    check("组内行带缩进标记", bool(items) and "gitem" in (items[0].get_attribute("class") or ""))
    check("展开后「刚选择」标记收掉",
          "刚选择" not in pg.query_selector_all("#taskBody tr.grouprow")[0].inner_text())

    nums = [e.inner_text() for e in pg.query_selector_all("#taskBody .gnum")]
    check("每组有进度数字", len(nums) == len(groups), str(nums))

    stats = api("/api/status")["stats"]
    pg.click('#statusChips .chip-btn[data-status="failed"]'); pg.wait_for_timeout(1500)
    names = [r.inner_text().replace("\n", " ") for r in pg.query_selector_all("#taskBody tr.grouprow")]
    if stats["failed"]:
        check("只看失败只留有失败的组", len(names) >= 1, str(names))
    else:
        check("没有失败项时失败视图为空", not names, str(names))
    pg.click('#statusChips .chip-btn[data-status="all"]'); pg.wait_for_timeout(1200)
    pg.click('#taskView button[data-view="flat"]'); pg.wait_for_timeout(1200)
    check("能切回按文件视图", len(pg.query_selector_all("#taskBody tr.grouprow")) == 0)

    print("========== 7. 齐全跳过 / 开始重刮 ==========")
    ju = [d for d in api("/api/files")["dirs"] if d["name"] == "剧集"][0]["path"]
    ju_paths = api("/api/files/expand", {"dirs": [ju]})["per"][ju]
    r = api("/api/files/scrape", {"paths": ju_paths})
    check("产物齐全时不再重刮", r.get("started") is False and r.get("skipped") == len(ju_paths),
          str(r))

    pg.click('[data-tab="files"]'); pg.wait_for_timeout(700)
    if pg.query_selector("[data-up]"):
        pg.click("[data-up]"); pg.wait_for_timeout(900)
    for row in pg.query_selector_all("#fileBody tr.dirrow"):
        if "剧集" in row.inner_text():
            box = row.query_selector("input[data-dirpick]")
            if not box.is_checked():      # 上一节可能已经勾着了，别又点掉
                box.click()
            break
    pg.wait_for_timeout(800)
    check("文件管理有「开始重刮」按钮", pg.is_visible("#btnFileRescrape"))
    check("按钮文案是「开始重刮」", "开始重刮" in pg.inner_text("#btnFileRescrape"),
          pg.inner_text("#btnFileRescrape"))
    check("勾选后「开始重刮」可用", not pg.is_disabled("#btnFileRescrape"))

    poster = Path(ju_paths[0]).with_suffix("").as_posix() + "-poster.jpg"
    mtime_before = os.path.getmtime(poster) if os.path.exists(poster) else 0
    pg.click("#btnFileRescrape")
    # 请求刚发出时状态还是 idle，等它真的跑起来再等结束
    started = False
    for _ in range(40):
        st = api("/api/status")
        if st["state"] != "idle" or (st.get("single") or {}).get("count"):
            started = True
            break
        pg.wait_for_timeout(300)
    check("开始重刮已启动", started)
    check("开始重刮已跑完", wait_idle(pg, 240))
    mtime_after = os.path.getmtime(poster) if os.path.exists(poster) else 0
    check("开始重刮覆盖了旧海报", mtime_after != mtime_before,
          f"{mtime_before} → {mtime_after}")

    print("========== 7b. 选帧：海报可选 + 自定义取景 ==========")
    target = ju_paths[0]
    pg.evaluate("p => openPicker(p)", target)
    pg.wait_for_timeout(4000)
    check("选帧面板有取景框", pg.is_visible("#pickBox"))
    check("海报开关可见且默认勾选", pg.is_checked("#pickPosterOn"))
    ratio = pg.evaluate("PICK.crop.w * PICK.fit.iw / (PICK.crop.h * PICK.fit.ih)")
    check("取景框默认按海报 2:3", abs(ratio - 2 / 3) < 0.02, f"{ratio:.3f}")
    check("取景预览已出图", bool(pg.get_attribute("#pickCropView", "src")))
    check("主按钮写着确定裁切", pg.inner_text("#btnPickApply").strip() == "确定裁切",
          pg.inner_text("#btnPickApply"))
    check("标题说明可拖动", "拖动" in pg.inner_text("#pickTitle"), pg.inner_text("#pickTitle"))

    # 框内按住拖动 = 平移，大小不变
    crop0 = pg.evaluate("PICK.crop")
    shell = pg.query_selector("#pickBox").bounding_box()
    pg.mouse.move(shell["x"] + shell["width"] / 2, shell["y"] + shell["height"] / 2)
    pg.mouse.down()
    pg.mouse.move(shell["x"] + shell["width"] / 2 + 45,
                  shell["y"] + shell["height"] / 2 + 15, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(500)
    crop1 = pg.evaluate("PICK.crop")
    check("框内拖动可平移", crop1["x"] != crop0["x"] or crop1["y"] != crop0["y"],
          f'{crop0["x"]:.2f},{crop0["y"]:.2f} → {crop1["x"]:.2f},{crop1["y"]:.2f}')
    check("平移不改大小", abs(crop1["w"] - crop0["w"]) < 1e-6)

    box = pg.query_selector('#pickBox i[data-h="se"]').bounding_box()
    crop_before = pg.evaluate("PICK.crop")
    pg.mouse.move(box["x"] + 5, box["y"] + 5)
    pg.mouse.down()
    pg.mouse.move(box["x"] - 70, box["y"] - 90, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(700)
    crop_after = pg.evaluate("PICK.crop")
    check("拖角能缩小取景框", crop_after["w"] < crop_before["w"] - 0.02,
          f"{crop_before['w']:.3f} → {crop_after['w']:.3f}")
    ratio2 = pg.evaluate("PICK.crop.w * PICK.fit.iw / (PICK.crop.h * PICK.fit.ih)")
    check("缩放后仍锁 2:3", abs(ratio2 - 2 / 3) < 0.02, f"{ratio2:.3f}")

    # 缩放：滚轮放大后画面变大，取景框同步放大但比例不变
    small_box = pg.query_selector("#pickBox").bounding_box()
    stage_geo = pg.query_selector("#pickStage").bounding_box()
    zoom0 = pg.evaluate("PICK.zoom")
    pg.mouse.move(stage_geo["x"] + stage_geo["width"] / 2,
                  stage_geo["y"] + stage_geo["height"] / 2)
    for _ in range(3):
        pg.mouse.wheel(0, -120)
        pg.wait_for_timeout(180)
    pg.wait_for_timeout(300)
    zoom1 = pg.evaluate("PICK.zoom")
    big_box = pg.query_selector("#pickBox").bounding_box()
    check("滚轮可放大画面", zoom1 > zoom0 + 0.3, f"{zoom0:.2f} → {zoom1:.2f}")
    check("缩放后取景框同步放大", big_box["width"] > small_box["width"] + 5,
          f'{small_box["width"]:.0f} → {big_box["width"]:.0f}')
    ratio3 = pg.evaluate("PICK.crop.w * PICK.fit.iw / (PICK.crop.h * PICK.fit.ih)")
    check("缩放不改取景比例", abs(ratio3 - 2 / 3) < 0.02, f"{ratio3:.3f}")
    check("缩放百分比显示", "%" in pg.inner_text("#pickZoomFit"), pg.inner_text("#pickZoomFit"))

    # 空格 + 拖画面 = 平移视角，裁切范围不动
    crop_z = pg.evaluate("PICK.crop")
    img0 = pg.evaluate("document.getElementById('pickImg').style.top")
    pg.keyboard.down(" ")
    pg.mouse.move(stage_geo["x"] + stage_geo["width"] / 2,
                  stage_geo["y"] + stage_geo["height"] / 2)
    pg.mouse.down()
    pg.mouse.move(stage_geo["x"] + stage_geo["width"] / 2,
                  stage_geo["y"] + stage_geo["height"] / 2 - 70, steps=10)
    pg.mouse.up()
    pg.keyboard.up(" ")
    pg.wait_for_timeout(400)
    check("空格拖能平移画面",
          pg.evaluate("document.getElementById('pickImg').style.top") != img0,
          img0)
    check("平移不改裁切范围", pg.evaluate("PICK.crop") == crop_z)

    # 方向键微调取景框
    nx = pg.evaluate("PICK.crop.x")
    pg.keyboard.press("ArrowRight")
    pg.wait_for_timeout(250)
    check("方向键能微调取景框", pg.evaluate("PICK.crop.x") > nx,
          f'{nx:.3f} → {pg.evaluate("PICK.crop.x"):.3f}')

    # 点百分比回到适应
    pg.click("#pickZoomFit")
    pg.wait_for_timeout(300)
    check("点百分比回到适应窗口", abs(pg.evaluate("PICK.zoom") - 1) < 1e-6,
          str(pg.evaluate("PICK.zoom")))

    pg.fill("#pickRange", "0")
    pg.evaluate("PICK.t = 5; selectFrame(5, true)")
    pg.wait_for_timeout(1800)
    before_pick = os.path.getmtime(poster) if os.path.exists(poster) else 0
    pg.evaluate("() => { window.confirm = () => true; }")
    pg.click("#btnPickApply")
    pg.wait_for_timeout(1500)
    ok_pick = wait_idle(pg, 120)
    after_pick = os.path.getmtime(poster) if os.path.exists(poster) else 0
    check("选帧按取景框重出了海报", ok_pick and after_pick != before_pick,
          f"{before_pick} → {after_pick}")
    from PIL import Image

    with Image.open(poster) as im:
        size = im.size
    check("海报仍是 2:3", abs(size[0] / size[1] - 2 / 3) < 0.02, str(size))

    print("========== 8. 任务列表勾选与删除 ==========")
    pg.click('[data-tab="tasks"]'); pg.wait_for_timeout(1200)
    pg.click('#taskView button[data-view="flat"]'); pg.wait_for_timeout(1400)
    pg.click('#statusChips .chip-btn[data-status="all"]'); pg.wait_for_timeout(1400)
    total_before = api("/api/items?size=1")["total"]
    boxes = pg.query_selector_all("#taskBody input[data-tpick]")
    check("任务行有勾选框", len(boxes) > 0, f"{len(boxes)} 个")

    boxes[0].click(); pg.wait_for_timeout(400)
    check("勾 1 条 → 已选 1 个文件", "1" in pg.inner_text("#taskSelInfo"),
          pg.inner_text("#taskSelInfo"))
    check("删除按钮变为可用", not pg.is_disabled("#btnTaskDel"))
    check("表头全选显示半选", pg.eval_on_selector("#taskAll", "e => e.indeterminate"))

    pg.click("#taskAll"); pg.wait_for_timeout(500)
    checked = len(pg.query_selector_all("#taskBody input[data-tpick]:checked"))
    check("表头全选勾上本页所有行", checked == len(boxes), f"{checked}/{len(boxes)}")

    pg.click("#btnTaskDel"); pg.wait_for_timeout(350)
    pg.click('#taskDelMenu button[data-tdel="record"]'); pg.wait_for_timeout(2000)
    total_after = api("/api/items?size=1")["total"]
    check("仅移除记录后列表变短", total_after < total_before,
          f"{total_before} → {total_after}")
    check("删除后勾选自动清空", "未选择" in pg.inner_text("#taskSelInfo"),
          pg.inner_text("#taskSelInfo"))

    # 记录删光了就补一轮，后面测文件夹删除还要有数据
    api("/api/task/start", {})
    wait_idle(pg, 120)
    pg.wait_for_timeout(1200)
    pg.click("#btnTaskRefresh"); pg.wait_for_timeout(1400)

    pg.click('#taskView button[data-view="dir"]'); pg.wait_for_timeout(1800)
    groups = pg.query_selector_all("#taskBody tr.grouprow")
    check("按文件夹视图有分组", len(groups) >= 2, f"{len(groups)} 组")
    group_before = len(groups)
    groups[0].query_selector("input[data-gpick]").click()
    pg.wait_for_timeout(500)
    check("勾文件夹 → 显示 1 个文件夹", "1" in pg.inner_text("#taskSelInfo"),
          pg.inner_text("#taskSelInfo"))

    pg.click("#btnTaskDel"); pg.wait_for_timeout(350)
    pg.click('#taskDelMenu button[data-tdel="purge"]'); pg.wait_for_timeout(2200)
    group_after = len(pg.query_selector_all("#taskBody tr.grouprow"))
    check("删文件夹（连产物）后分组变少", group_after < group_before,
          f"{group_before} → {group_after}")

    print("========== 9. 设置页：账号 / 媒体库 / 服务器 ==========")
    pg.click('[data-tab="settings"]'); pg.wait_for_timeout(1400)
    check("设置页有 4 个区块", len(pg.query_selector_all(".settings .set-card")) >= 4,
          str(len(pg.query_selector_all(".settings .set-card"))))
    check("媒体库目录已回填", ".devdata" in pg.input_value("#setRoots"),
          pg.input_value("#setRoots")[:60])
    check("没有启用登录时账号区有提示", "没有启用登录" in pg.inner_text("#setAccHint"),
          pg.inner_text("#setAccHint"))

    pg.fill("#setConcurrency", "4")
    pg.click("#btnSaveSettings"); pg.wait_for_timeout(1800)
    cfg = api("/api/settings")
    check("并发数保存并生效", cfg["scan"]["concurrency"] == 4, str(cfg["scan"]["concurrency"]))
    check("保存后提示立即生效", "立即生效" in pg.inner_text("#setSaveMsg"),
          pg.inner_text("#setSaveMsg"))
    check("底部显示写入的配置文件", "config.yaml" in pg.inner_text("#setFileHint"),
          pg.inner_text("#setFileHint"))
    cfg_file = Path(__file__).resolve().parent.parent / ".devdata" / "config.yaml"
    text = cfg_file.read_text(encoding="utf-8") if cfg_file.is_file() else ""
    check("已写回 config.yaml", "concurrency: 4" in text,
          next((l for l in text.splitlines() if "concurrency" in l), "配置文件里没有 concurrency"))

    pg.click("#btnSrvAdd"); pg.wait_for_timeout(500)
    rows = pg.query_selector_all("#srvEditor .srv-edit")
    check("能新增服务器表单", len(rows) == 1, f"{len(rows)} 行")
    row = rows[0]
    row.query_selector('[data-f="name"]').fill("测试Jellyfin")
    row.query_selector('[data-f="url"]').fill("http://127.0.0.1:9999")
    row.query_selector('[data-f="key"]').fill("abcdef123456")
    row.query_selector('[data-f="map"]').fill("/media=/vol1/strm")
    pg.click("#btnSaveSettings"); pg.wait_for_timeout(1800)
    cfg = api("/api/settings")
    saved = cfg["servers"][0] if cfg["servers"] else {}
    check("服务器保存成功", len(cfg["servers"]) == 1 and saved.get("name") == "测试Jellyfin",
          str(cfg["servers"])[:120])
    check("密钥不回传明文", saved.get("api_key") == "" and saved.get("has_key") is True)
    check("路径映射保存正确", (saved.get("path_map") or {}).get("/media") == "/vol1/strm",
          str(saved.get("path_map")))

    pg.reload(wait_until="networkidle"); pg.wait_for_timeout(800)
    # reload 会把之前覆盖的 confirm 冲掉，删除服务器那步要重新关掉确认框
    pg.evaluate("() => { window.confirm = () => true; }")
    pg.click('[data-tab="settings"]'); pg.wait_for_timeout(1400)
    check("刷新后设置还在", "测试Jellyfin" in pg.inner_text("#srvEditor"),
          pg.inner_text("#srvEditor")[:80])
    check("密钥框提示沿用", "已配置" in (pg.get_attribute('#srvEditor [data-f="key"]', "placeholder") or ""),
          pg.get_attribute('#srvEditor [data-f="key"]', "placeholder") or "")

    pg.click("#srvEditor [data-srvdel]"); pg.wait_for_timeout(600)
    pg.click("#btnSaveSettings"); pg.wait_for_timeout(1600)
    check("删除服务器生效", len(api("/api/settings")["servers"]) == 0,
          str(api("/api/settings")["servers"]))
    pg.fill("#setConcurrency", "2")
    pg.click("#btnSaveSettings"); pg.wait_for_timeout(1200)

    print("========== 10. 收尾 ==========")
    pg.click('[data-tab="files"]'); pg.wait_for_timeout(800)
    if pg.query_selector("[data-up]"):
        pg.click("[data-up]"); pg.wait_for_timeout(1000)

    check("无 JS 报错", not errs, " | ".join(errs[:3]))
    check("无 4xx/5xx 请求", not bad, " | ".join(bad[:3]))
    b.close()

print()
print("========== 结果 ==========")
print("失败项:", FAIL if FAIL else "无")
