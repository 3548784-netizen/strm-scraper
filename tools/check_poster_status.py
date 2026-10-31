# -*- coding: utf-8 -*-
"""「海报未生成」状态回归：进度、明细、筛选。

文件管理里除了「刮没刮过」，还要能回答三个具体问题：
1. 这个文件夹里还有多少张海报没生成（总数、百分比、失败几个）；
2. 到底是哪些没生成（文件行明确写「未生成」而不是空一格，文件夹行给出子树进度）；
3. 怎么一次补齐（只看未生成 → 全选 → 开始刮削）。

口径：海报只认 ``<视频名>-poster.jpg`` 在不在，和「刮削状态」是两条独立的轴
——关过海报开关、或海报下载失败但 NFO 拿到了，都会出现「状态成功、海报空着」。

先 `python tools/devserver.py` 起环境，再跑本脚本（需要本机 Edge）::

    python tools/check_poster_status.py --base http://127.0.0.1:6672
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
HERE = Path(__file__).resolve().parent.parent
MEDIA = HERE / ".devdata" / "media"
BASE_DIR = MEDIA / "海报测试"
FAKE_STRM = "http://127.0.0.1:5244/d/115/%E7%94%B5%E5%BD%B1/%E5%BD%B1%E7%89%87.mkv"

# 结构：根目录 2 个（1 有海报）+ 子目录一 2 个（都有）+ 子目录二 3 个（只有 1 个有）
# 合计 7 个 strm / 4 张海报 → 57%
PLAN = {
    "": [("片名甲", True), ("片名乙", False)],
    "子目录一": [("剧集 01", True), ("剧集 02", True)],
    "子目录二": [("剧集 03", True), ("剧集 04", False), ("剧集 05", False)],
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def poster_of(folder: Path, stem: str) -> Path:
    return folder / f"{stem}-poster.jpg"


def make_poster(dest: Path, seed: int = 0) -> None:
    """自己画一张竖版海报，不依赖别的测试留下的图。"""
    from PIL import Image

    img = Image.new("RGB", (200, 300), (24 + seed * 12 % 60, 40, 80))
    for y in range(300):
        for x in range(0, 200, 2):
            img.putpixel((x, y), (30 + (y * 2) % 120, 40 + seed * 7 % 80, 90))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=80)


def build(clean: bool = True) -> None:
    if clean and BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    for sub, entries in PLAN.items():
        folder = BASE_DIR / sub if sub else BASE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        for name, has_poster in entries:
            (folder / f"{name}.strm").write_text(FAKE_STRM, encoding="utf-8")
            if has_poster:
                make_poster(poster_of(folder, name), seed=len(name))


def fill_all_posters() -> None:
    """把缺的都补上，用来验证「全部已生成」的状态。"""
    for sub, entries in PLAN.items():
        folder = BASE_DIR / sub if sub else BASE_DIR
        for name, has in entries:
            if not has:
                make_poster(poster_of(folder, name), seed=len(name) + 3)


def api_get(base: str, path: str) -> dict:
    return json.load(urllib.request.urlopen(base + path))


SNAP = """
() => {
  const txt = (id) => (document.getElementById(id) || {}).textContent || '';
  const strip = document.getElementById('posterStrip');
  const btn = document.getElementById('btnPosterOnly');
  return {
    hidden: strip.hidden,
    num: txt('pstripNum'),
    pct: txt('pstripPct'),
    note: txt('pstripNote'),
    hint: txt('pstripHint'),
    bar: document.querySelector('#pstripBar i').style.width,
    barFull: document.querySelector('#pstripBar i').classList.contains('full'),
    btn: txt('btnPosterOnly'),
    btnHidden: btn.hidden,
    // hidden 只是属性，全局 button 的 display 会把它顶掉；真正「看不见」要量盒子
    btnShown: btn.getBoundingClientRect().width > 0,
    btnOn: btn.classList.contains('on'),
    dirs: [...document.querySelectorAll('#fileBody tr.dirrow')].map((t) => {
      const d = t.querySelector('.dposter');
      return { name: t.querySelector('.dir').textContent.trim().replace(/\\s+/g, ' '),
               poster: d ? d.textContent : '', cls: d ? d.className : '' };
    }),
    files: [...document.querySelectorAll('#fileBody tr[data-path]')].map((t) => ({
      name: t.querySelector('.name').textContent.trim(),
      missing: t.querySelectorAll('td')[3].innerHTML.includes('pmiss'),
      thumb: !!t.querySelectorAll('td')[3].querySelector('img'),
    })),
    meta: txt('fileMeta'),
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    build()
    try:
        print("---------- 1. 接口口径 ----------")
        root = api_get(base, "/api/files")
        row = next((d for d in root["dirs"] if d["name"] == "海报测试"), None)
        check("根目录能看到测试目录", row is not None, str([d["name"] for d in root["dirs"]][:4]))
        check("文件夹汇总带上 poster", row is not None and row.get("poster") == 4,
              str(row and row.get("poster")))
        check("poster 与 scraped 分开统计", row is not None and row.get("scraped") == 4,
              f"poster={row and row.get('poster')} scraped={row and row.get('scraped')}")

        here = api_get(base, "/api/files?path=" + urllib.parse.quote(str(BASE_DIR)))
        by_name = {d["name"]: d for d in here["dirs"]}
        check("子目录一 2 个海报", by_name.get("子目录一", {}).get("poster") == 2)
        check("子目录二 只有 1 个海报", by_name.get("子目录二", {}).get("poster") == 1)
        files = {f["name"]: f for f in here["files"]}
        check("有海报的文件返回海报大小",
              files.get("片名甲.strm", {}).get("artifacts", {}).get("poster", 0) > 0)
        check("没海报的文件海报为 0",
              files.get("片名乙.strm", {}).get("artifacts", {}).get("poster", 0) == 0)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_timeout(1300)
            page.click('[data-tab="files"]')
            page.wait_for_timeout(1200)
            page.click('#fileBody tr.dirrow:has-text("海报测试") .dir')
            page.wait_for_timeout(1400)

            print("\n---------- 2. 进度条 ----------")
            s = page.evaluate(SNAP)
            check("进度条出现", not s["hidden"])
            check("总数与已生成数", s["num"] == "4 / 7", s["num"])
            check("百分比", s["pct"] == "57%", s["pct"])
            check("进度条宽度跟随百分比", s["bar"] == "57%", s["bar"])
            check("未生成数量写在进度条上", "3 个未生成" in s["note"], s["note"])
            check("没全部完成时进度条不是绿的", s["barFull"] is False)
            check("切换按钮带数量", s["btn"] == "只看未生成 (3)", s["btn"])

            print("\n---------- 3. 已生成 / 未生成要一眼分得开 ----------")
            dirs = {d["name"]: d for d in s["dirs"]}
            check("文件夹行给出子树进度 2/2", dirs.get("子目录一", {}).get("poster") == "2/2",
                  str(dirs.get("子目录一")))
            check("全部完成的文件夹用 done 样式",
                  "done" in (dirs.get("子目录一", {}).get("cls") or ""),
                  dirs.get("子目录一", {}).get("cls"))
            check("缺一部分的文件夹给出 1/3", dirs.get("子目录二", {}).get("poster") == "1/3",
                  str(dirs.get("子目录二")))
            check("缺一部分的文件夹用 part 样式",
                  "part" in (dirs.get("子目录二", {}).get("cls") or ""),
                  dirs.get("子目录二", {}).get("cls"))
            file_rows = {f["name"]: f for f in s["files"]}
            check("有海报的文件显示缩略图", file_rows.get("片名甲.strm", {}).get("thumb") is True,
                  str(file_rows.get("片名甲.strm")))
            check("没海报的文件不显示缩略图",
                  file_rows.get("片名乙.strm", {}).get("thumb") is False)
            check("没海报的文件写明「未生成」",
                  file_rows.get("片名乙.strm", {}).get("missing") is True,
                  str(file_rows.get("片名乙.strm")))

            print("\n---------- 4. 只看未生成 ----------")
            page.click("#btnPosterOnly")
            page.wait_for_timeout(700)
            s = page.evaluate(SNAP)
            check("筛完只剩还有缺口的文件夹",
                  [d["name"] for d in s["dirs"]] == ["子目录二"], str([d["name"] for d in s["dirs"]]))
            check("筛完只剩没海报的文件",
                  [f["name"] for f in s["files"]] == ["片名乙.strm"], str([f["name"] for f in s["files"]]))
            check("按钮变成「显示全部」", s["btn"] == "显示全部 (7)", s["btn"])
            check("按钮呈按下状态", s["btnOn"] is True)
            check("标注已筛出", "已筛出" in s["note"], s["note"])
            check("提示怎么补齐", "全选" in s["hint"], s["hint"])
            check("底栏只数筛出来的",
                  s["meta"].startswith("1 / 2 个目录"), s["meta"])

            page.click("#fileAll")
            page.wait_for_timeout(1000)
            picked = page.inner_text("#fileSelInfo")
            check("全选只勾筛出来的（1 个文件 + 子目录二的 3 个）", "已选 4 项" in picked, picked)

            print("\n---------- 5. 恢复全部 ----------")
            page.click("#btnPosterOnly")
            page.wait_for_timeout(700)
            s = page.evaluate(SNAP)
            check("取消筛选后文件回来", len(s["files"]) == 2, str([f["name"] for f in s["files"]]))
            check("取消筛选后文件夹回来", len(s["dirs"]) == 2, str([d["name"] for d in s["dirs"]]))
            check("按钮回到「只看未生成」", s["btn"] == "只看未生成 (3)", s["btn"])
            check("按钮不再是按下状态", s["btnOn"] is False)

            print("\n---------- 6. 全部补齐后的状态 ----------")
            fill_all_posters()
            page.click("#btnFileRefresh")
            page.wait_for_timeout(1500)
            s = page.evaluate(SNAP)
            check("进度到 100%", s["pct"] == "100%", s["pct"])
            check("数字变成 7 / 7", s["num"] == "7 / 7", s["num"])
            check("文案写「全部已生成」", s["note"] == "全部已生成", s["note"])
            check("全好时进度条变绿", s["barFull"] is True)
            check("全好时收起筛选按钮", s["btnHidden"] is True)
            check("全好时按钮真的不占位置（不是留个灰的 0）", s["btnShown"] is False)
            check("文件夹行变成 3/3", any(d["poster"] == "3/3" and "done" in d["cls"]
                                          for d in s["dirs"] if d["name"].startswith("子目录二")),
                  str(s["dirs"]))
            check("文件行全部带缩略图", all(f["thumb"] for f in s["files"]), str(s["files"]))

            print("\n---------- 7. 删掉一张后重新提示 ----------")
            poster_of(BASE_DIR / "子目录二", "剧集 04").unlink()
            page.click("#btnFileRefresh")
            page.wait_for_timeout(1500)
            s = page.evaluate(SNAP)
            check("少一张就回落", s["num"] == "6 / 7", s["num"])
            check("未生成重新出现在进度条上", "1 个未生成" in s["note"], s["note"])
            check("筛选按钮回来", s["btnHidden"] is False, s["btn"])

            print("\n---------- 8. 无 JS 报错 ----------")
            check("页面没有抛错", not errors, str(errors[:2]))
            browser.close()
    finally:
        if BASE_DIR.exists():
            shutil.rmtree(BASE_DIR)

    print()
    if FAIL:
        print(f"失败 {len(FAIL)} 项：" + "、".join(FAIL))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
