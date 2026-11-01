# -*- coding: utf-8 -*-
"""「文件夹封面 + 背景图」回归：多层目录的提示与手动修复。

背景：Emby 与 Jellyfin 用同一套取图规则 —— 目录里直接有视频，它是「影片」
条目，认 ``<视频名>-poster.jpg``；目录里只有子目录，它是「文件夹」条目，
只认 ``folder.jpg`` / ``poster.jpg`` / ``cover.jpg``（背景图认
``fanart.jpg`` / ``backdrop.jpg``）。所以多层嵌套时中间那几层一定没有封面，
按视频名生成的海报对它们完全无效。

这个脚本验证五件事：
1. 只有「这一层没有直接放视频」的中转层才标「缺海报」；直接放视频的层不标；
2. 修复只在这个动作被点时发生，正常刮削不会写这些文件；
3. 修复给每个中转层只写三个文件：folder.jpg、poster.jpg（竖版封面）和
   fanart.jpg（横版背景）；**不写 NFO、不写 backdrop.jpg、不写带目录名的副本**；
   直接放视频的层一个都不写；
4. 真图会被压上文件夹名，背景图是裁成 16:9 的横版；老版本留下的同名 NFO 和
   多写的副本会被顺手清掉，但用户自己的同名图一律不动；
5. 扫描根本身永远不写。

先 `python tools/devserver.py` 起环境（6670），再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

FAIL: list[str] = []
HERE = Path(__file__).resolve().parent.parent
MEDIA = HERE / ".devdata" / "media"
DB = HERE / ".devdata" / "state.db"
NEST = MEDIA / "嵌套测试"
LAYER2 = NEST / "第一层"
LAYER3 = LAYER2 / "第二层"
# 需要文件夹封面的层：自己不放视频，只有子目录
ART_LAYERS = [NEST, LAYER2]
# 直接放视频的层：Emby 当影片条目，封面走 xxx-poster.jpg，不该再写文件夹封面
LEAF = LAYER3
VIDEO = LAYER3 / "影片A.strm"
POSTER = LAYER3 / "影片A-poster.jpg"
# 第二张海报：用来验证「父层指定用哪张」确实按指定的走，而不是按名字挑第一张
ALT = LAYER3 / "影片Z-指定海报-poster.jpg"
MISS = "缺海报"
DONE = "海报已就绪"


def write_names(folder: Path) -> tuple[str, ...]:
    """这一层应该落地的三个文件名（和后端 ``_art_targets`` 同一口径）。"""
    return ("folder.jpg", "poster.jpg", "fanart.jpg")


def legacy_names(folder: Path) -> tuple[str, ...]:
    """旧版本多写、现在会被清理掉的那三个文件名。"""
    return ("backdrop.jpg", f"{folder.name}-poster.jpg", f"{folder.name}-fanart.jpg")


def arts_of(folder: Path) -> list[Path]:
    return [folder / name for name in write_names(folder)[:2]]


ALL_ARTS = [a for folder in (*ART_LAYERS, LEAF) for a in arts_of(folder)]
ALL_TARGETS = [a for folder in ART_LAYERS for a in map(folder.__truediv__, write_names(folder))]


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api_get(base: str, path: str) -> dict:
    return json.load(urllib.request.urlopen(base + path))


def api_post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def clear_pins() -> None:
    """清掉库里所有「指定封面」关系，避免上一轮跑残的记录影响断言。"""
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS folder_art"
                 " (folder TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT '', updated REAL)")
    conn.execute("DELETE FROM folder_art")
    conn.commit()
    conn.close()


def seed(clean: bool = True) -> None:
    """造一组三层目录：媒体库/嵌套测试/第一层/第二层/影片A.strm"""
    LAYER3.mkdir(parents=True, exist_ok=True)
    VIDEO.write_text("http://example.invalid/a.mkv\n", encoding="utf-8")
    POSTER.write_bytes(real_jpeg(POSTER, (110, 130, 150)))
    (LAYER3 / "影片A-fanart.jpg").write_bytes(real_jpeg(POSTER, (150, 130, 110)))
    (LAYER3 / "影片A.nfo").write_text("<movie><title>影片A</title></movie>", encoding="utf-8")
    ALT.unlink(missing_ok=True)
    clear_pins()
    if clean:
        # 上一轮留下的任何名字都清掉，否则「只补缺的」会直接判定已就绪，
        # 测不到写入；旧版的副本也一并清掉
        for folder in (*ART_LAYERS, LEAF):
            for path in folder.glob("*"):
                if path.is_file() and path.name in (*write_names(folder),
                                                    *legacy_names(folder)):
                    path.unlink()
            # 老版本写的同名 NFO 也清掉：留着会被当成「待清理的旧产物」，
            # 让每一层都判定成需要重写，干扰「已就绪跳过」那条断言
            (folder / f"{folder.name}.nfo").unlink(missing_ok=True)
    now = time.time()
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR REPLACE INTO items"
        " (path, size, mtime, status, reason, title, duration, attempts, updated, frame_at, added)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(VIDEO), VIDEO.stat().st_size, round(VIDEO.stat().st_mtime, 3), "ok",
         "", "影片A", 0.0, 1, int(now), 0.0, now),
    )
    conn.commit()
    conn.close()


def dir_rows(page) -> dict[str, str]:
    """当前屏目录行 → {名字: 备注里的文件夹标签}"""
    return page.evaluate("""() => {
      const out = {};
      document.querySelectorAll('#fileBody tr.dirrow').forEach(tr => {
        const name = tr.querySelector('span.dir').textContent.trim();
        const tag = tr.querySelector('.foldertag');
        out[name] = tag ? tag.textContent.trim() : '';
      });
      return out;
    }""")


def open_dir(page, name: str) -> None:
    page.evaluate("""(name) => {
      const row = [...document.querySelectorAll('#fileBody tr.dirrow')]
        .find(tr => tr.querySelector('span.dir').textContent.trim() === name);
      if (row) row.querySelector('span.dir').click();
    }""", name)
    page.wait_for_timeout(900)


def go_root(page) -> None:
    """点面包屑里的第一个（扫描根）回到媒体库根目录。"""
    page.evaluate("""() => {
      const crumb = document.querySelector('#crumbs .crumb[data-path]');
      if (crumb) crumb.click();
    }""")
    page.wait_for_timeout(1000)


def tick_dir(page, name: str) -> None:
    page.evaluate("""(name) => {
      const row = [...document.querySelectorAll('#fileBody tr.dirrow')]
        .find(tr => tr.querySelector('span.dir').textContent.trim() === name);
      if (row) row.querySelector('input[data-dirpick]').click();
    }""", name)
    page.wait_for_timeout(1300)


def bottom_brightness(path: Path) -> float:
    """封面底部 12% 的平均亮度：压过名字条会明显变暗。"""
    with Image.open(path) as im:
        im = im.convert("L")
        band = im.crop((0, int(im.height * 0.88), im.width, im.height))
        pixels = list(band.getdata())
    return sum(pixels) / len(pixels)


def top_similar(path: Path, src: Path) -> bool:
    """``path`` 的上半部分是不是 ``src`` 那张图（压字只压底部，上面不动）。

    写入时图会被重新编码成 JPEG 并在底部压一条名字条，字节不可能和源图一样；
    裁掉底部 30% 再比像素，能证明「封面确实来自这张源图」。
    """
    from PIL import ImageChops, ImageStat
    try:
        with Image.open(path) as a, Image.open(src) as b:
            a = a.convert("RGB")
            b = b.convert("RGB")
            if a.size != b.size:            # 超宽图会被缩到 1200
                return False
            cut = int(a.height * 0.7)
            stat = ImageStat.Stat(ImageChops.difference(
                a.crop((0, 0, a.width, cut)), b.crop((0, 0, b.width, cut))))
    except (OSError, ValueError):
        return False
    return max(stat.mean) < 8


def real_jpeg(path: Path, color: tuple[int, int, int], size=(400, 600)) -> bytes:
    """造一张真 JPEG 当夹具海报：Pillow 打不开的假图会被服务拒掉。"""
    image = Image.new("RGB", size, color)
    for y in range(0, size[1], 50):
        for x in range(0, size[0], 50):
            image.paste((255, 255, 255), (x, y, x + 25, y + 25))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    seed()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1300)
        page.click('[data-tab="files"]')
        page.wait_for_timeout(1200)

        print("\n---------- 1. 只有中转层标「缺海报」 ----------")
        rows = dir_rows(page)
        check("根目录的「嵌套测试」（只放子目录）标了缺海报", rows.get("嵌套测试") == MISS,
              str(rows.get("嵌套测试")))
        # 别的脚本（check_emby_art）会在同一个媒体库里留目录，只认自己的夹具
        leaf_tags = [k for k, v in rows.items() if v and k not in ("嵌套测试", "取图测试")]
        check("直接放视频的目录一个都不标", not leaf_tags, str(leaf_tags))
        check("按钮默认禁用（还没勾）", page.is_disabled("#btnFolderArt"))

        open_dir(page, "嵌套测试")
        rows = dir_rows(page)
        check("进去后「第一层」（只有子目录）也标了", rows.get("第一层") == MISS,
              str(rows.get("第一层")))
        open_dir(page, "第一层")
        rows = dir_rows(page)
        check("「第二层」直接放视频，不再标", rows.get("第二层") == "", str(rows.get("第二层")))

        open_dir(page, "第二层")
        file_tags = page.evaluate("""() => [...document.querySelectorAll('#fileBody tr[data-path] .foldertag')]
            .map(e => e.textContent.trim())""")
        check("文件行不再挂文件夹封面标签", file_tags == [], str(file_tags))

        print("\n---------- 2. 任务列表：同一套判据 ----------")
        page.click('[data-tab="tasks"]')
        page.wait_for_timeout(1400)
        # 默认是「按文件夹」，先切到「按文件」再断言：文件行不该再挂文件夹标签
        page.click('#taskView button[data-view="flat"]')
        page.wait_for_timeout(1200)
        task_tags = page.evaluate("""() => [...document.querySelectorAll('#taskBody .foldertag')]
            .map(e => e.textContent.trim())""")
        check("平铺视图里文件行不带文件夹标签", not task_tags, str(task_tags))
        page.click('#taskView button[data-view="dir"]')
        page.wait_for_timeout(1200)
        first_tag = page.evaluate("""() => {
          const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
            .find(tr => tr.textContent.includes('嵌套测试'));
          const tag = row && row.querySelector('.foldertag');
          return tag ? tag.textContent.trim() : '';
        }""")
        check("第一屏的中转目录标着缺海报", first_tag == MISS, first_tag)
        page.click("#btnExpandAll")
        page.wait_for_timeout(1600)
        grp = page.evaluate("""() => {
          const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
            .find(tr => tr.textContent.includes('第二层'));
          const tag = row && row.querySelector('.foldertag');
          return tag ? tag.textContent.trim() : '';
        }""")
        check("展开后直接放视频的层不标", grp == "", grp)

        print("\n---------- 3. 正常刮削不会写文件夹封面 ----------")
        r = api_post(base, "/api/files/scrape", {"paths": [str(VIDEO)]})
        check("产物齐全的刮削直接跳过", not r.get("started"), r.get("message", ""))
        check("刮削没有生成任何文件夹封面",
              not any(a.exists() for a in ALL_ARTS),
              str([str(a) for a in ALL_ARTS if a.exists()]))

        print("\n---------- 4. 勾选 → 修复：中转层写三个文件，叶子层不写 ----------")
        page.click('[data-tab="files"]')
        page.wait_for_timeout(900)
        go_root(page)
        tick_dir(page, "嵌套测试")
        check("勾选后按钮可用", page.is_enabled("#btnFolderArt"))
        page.click("#btnFolderArt")
        page.wait_for_timeout(400)
        check("菜单弹出来了", page.is_visible("#folderArtMenu"))
        page.click('#folderArtMenu button[data-fart="miss"]')
        page.wait_for_timeout(1500)
        toast = page.inner_text("#toast")
        check("提示写明补了 2 层 / 6 个文件",
              "已补 2 个文件夹的封面和背景图" in toast and "6 个文件" in toast, toast)
        check("两层各三个文件都落地",
              all(a.is_file() for a in ALL_TARGETS),
              str([(a.parent.name, a.name, a.is_file()) for a in ALL_TARGETS]))
        leaf_left = [a.name for a in map(LEAF.__truediv__, write_names(LEAF)) if a.exists()]
        check("直接放视频的层一个文件都没写", not leaf_left, str(leaf_left))
        check("不再生成 NFO",
              not (NEST / f"{NEST.name}.nfo").exists()
              and not (LAYER2 / f"{LAYER2.name}.nfo").exists())
        extra = [n for f in ART_LAYERS for n in legacy_names(f) if (f / n).exists()]
        check("不再写 backdrop.jpg 和带目录名的副本", not extra, str(extra))
        check("每层只有三个产物",
              sorted(p.name for p in NEST.iterdir()
                     if p.is_file() and p.suffix.lower() in (".jpg", ".png")) ==
              sorted(write_names(NEST)),
              str(sorted(p.name for p in NEST.iterdir() if p.is_file())))

        print("\n---------- 5. 再点一次：默认跳过已有的 ----------")
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)]})
        check("第二次全部跳过", "2 个已就绪跳过" in r["message"], r["message"])
        check("没有重复写", r["created"] == [], str(r["created"]))

        print("\n---------- 6. 补完之后备注变成「海报已就绪」 ----------")
        page.wait_for_timeout(600)
        rows = dir_rows(page)
        check("根目录标签翻转", rows.get("嵌套测试") == DONE, str(rows.get("嵌套测试")))

        print("\n---------- 7. 直接放视频的目录不补、扫描根不写 ----------")
        info = api_get(base, "/api/files")
        leaf_dir = next((d for d in info["dirs"]
                         if d["name"] != "嵌套测试" and (d.get("own") or 0) > 0), None)
        if leaf_dir:
            lp = Path(leaf_dir["path"])
            for name in write_names(lp):
                (lp / name).unlink(missing_ok=True)
            r = api_post(base, "/api/files/folder-poster", {"dirs": [leaf_dir["path"]]})
            check("说明这里没有需要封面的层",
                  "没有需要文件夹封面的层" in r["message"], r["message"])
            left = [n for n in write_names(lp) if (lp / n).exists()]
            check("这一层没有被写入任何文件", not left, str(left))
            for name in write_names(lp):
                (lp / name).unlink(missing_ok=True)

        root_path = Path(info["path"])
        api_post(base, "/api/files/folder-poster", {"dirs": [str(root_path)]})
        check("扫描根本身不写封面",
              not any((root_path / n).exists() for n in write_names(root_path)),
              str([n for n in write_names(root_path) if (root_path / n).exists()]))

        print("\n---------- 8. 指定用哪张：父层不再被「按名字挑第一张」覆盖 ----------")
        ALT.write_bytes(real_jpeg(ALT, (90, 160, 90)))
        cand = api_get(base, "/api/files/art-candidates?path="
                       + urllib.parse.quote(str(LAYER2)))
        names = [i["name"] for i in cand["items"]]
        check("候选里能看到里面两张海报",
              ALT.name in names and POSTER.name in names, str(names))
        check("本层已有的 folder.jpg 不算候选",
              not any(i["path"] == str(LAYER2 / "folder.jpg") for i in cand["items"]),
              str([i["name"] for i in cand["items"]]))
        check("本层自己生成的带名字海报也不算候选",
              f"{LAYER2.name}-poster.jpg" not in names, str(names))
        check("候选带相对路径，前端能区分同名图",
              all(i.get("rel") for i in cand["items"]), str(cand["items"][:1]))
        check("正片海报排在 folder 副本前面", cand["items"][0]["kind"] == "poster",
              str(cand["items"][0]["kind"]))

        r = api_post(base, "/api/files/art-pin", {"dir": str(LAYER2), "source": str(ALT)})
        check("指定成功并立刻写三个文件", r.get("ok") and "3 个文件" in r["message"], r["message"])
        check("图片都来自指定的那张",
              all(top_similar(a, ALT) for a in arts_of(LAYER2)),
              str([(a.name, a.stat().st_size) for a in arts_of(LAYER2)]))
        cand = api_get(base, "/api/files/art-candidates?path="
                       + urllib.parse.quote(str(LAYER2)))
        check("接口回传当前实际在用的那张（按内容反查）",
              cand.get("current") == str(ALT), str(cand.get("current")))

        d = api_get(base, "/api/files?path=" + urllib.parse.quote(str(NEST)))
        row = next(x for x in d["dirs"] if x["name"] == LAYER2.name)
        check("列表带回指定标记与图名", row["pinned"] == 1 and row["pin_name"] == ALT.name,
              str((row["pinned"], row["pin_name"])))

        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)], "force": True})
        check("覆盖重建时按指定的图走", r.get("pinned") == 1, r["message"])
        check("重建后第一层仍是指定那张", all(top_similar(a, ALT)
                                            for a in arts_of(LAYER2)))
        check("没指定的层照旧自动挑", top_similar(arts_of(NEST)[0], POSTER))

        (LAYER2 / "poster.jpg").unlink(missing_ok=True)
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)]})
        check("缺一个名字时补回来且仍用指定的图",
              (LAYER2 / "poster.jpg").is_file()
              and top_similar(LAYER2 / "poster.jpg", ALT), r["message"])

        print("\n---------- 9. 真图会被压上文件夹名，背景图裁成横版 ----------")
        real = Image.new("RGB", (400, 600), (120, 140, 160))
        for y in range(600):
            for x in range(0, 400, 8):
                real.putpixel((x, y), (200, 190, 180))
        # 必须放在这一层里面：指定封面只接受本层目录内的图
        tmp_real = NEST / "_real-poster.jpg"
        real.save(tmp_real, "JPEG", quality=95)
        before = bottom_brightness(tmp_real)
        r = api_post(base, "/api/files/art-pin", {"dir": str(NEST), "source": str(tmp_real)})
        check("指定真图后写入成功", r.get("ok") is True, r["message"])
        out = NEST / "folder.jpg"
        after = bottom_brightness(out) if out.is_file() else 255
        check("底部压了名字条（比源图暗）", after < before - 8,
              f"源底部 {before:.0f} → 产出底部 {after:.0f}")
        with Image.open(out) as im:
            check("封面保留原比例（宽不超过 1200）", im.width <= 1200 and im.height > 0,
                  f"{im.width}x{im.height}")
        bg = NEST / "fanart.jpg"
        with Image.open(bg) as im:
            ratio = im.width / im.height
            check("背景图是 16:9 横版", abs(ratio - 16 / 9) < 0.03, f"{im.width}x{im.height}")
            check("背景图也压了名字条", bottom_brightness(bg) < 150,
                  f"底部亮度 {bottom_brightness(bg):.0f}")
            check("背景图和封面确实不是同一张图",
                  bg.read_bytes() != out.read_bytes())
        tmp_real.unlink(missing_ok=True)
        api_post(base, "/api/files/art-pin", {"dir": str(NEST), "source": ""})

        print("\n---------- 9b. 老版本留下的同名 NFO 会被清掉 ----------")
        legacy = NEST / f"{NEST.name}.nfo"
        legacy.write_text('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
                          f"<movie>\n  <title>{NEST.name}</title>\n"
                          f"  <sorttitle>{NEST.name}</sorttitle>\n</movie>\n",
                          encoding="utf-8")
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)], "force": True})
        check("旧 NFO 被删掉了", not legacy.exists(), r["message"])
        check("提示里写了清掉几个", "清掉 1 个旧版 NFO" in r["message"], r["message"])

        # 用户自己的 movie.nfo（有 plot / tmdbid）绝不能被当成我们的产物删掉
        mine = NEST / f"{NEST.name}.nfo"
        mine.write_text('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
                        f"<movie>\n  <title>{NEST.name}</title>\n"
                        "  <plot>用户自己的简介</plot>\n  <tmdbid>123</tmdbid>\n</movie>\n",
                        encoding="utf-8")
        api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)], "force": True})
        check("有 plot / tmdbid 的 NFO 不会被误删", mine.exists())
        mine.unlink(missing_ok=True)

        print("\n---------- 9c. 旧版多写的副本会被清掉，用户自己的同名图不动 ----------")
        # 模拟旧版留下的样子：backdrop.jpg 和带目录名的两份都是正本的逐字节副本
        for name, twin in (("backdrop.jpg", "fanart.jpg"),
                           (f"{NEST.name}-poster.jpg", "folder.jpg"),
                           (f"{NEST.name}-fanart.jpg", "fanart.jpg")):
            (NEST / name).write_bytes((NEST / twin).read_bytes())
        # 用户自己放的一张 backdrop.jpg：内容和 fanart.jpg 不同
        (NEST / "backdrop.jpg").write_bytes(b"\xff\xd8\xff" + b"U" * 3000)
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)], "force": True})
        check("带目录名的两份被清掉",
              not (NEST / f"{NEST.name}-poster.jpg").exists()
              and not (NEST / f"{NEST.name}-fanart.jpg").exists(), r["message"])
        check("用户自己的 backdrop.jpg 留着", (NEST / "backdrop.jpg").is_file())
        check("提示里写了清掉几个多余图片", "多余的图片副本" in r["message"], r["message"])
        (NEST / "backdrop.jpg").unlink(missing_ok=True)
        # 只清理不重画：正本已经齐了，再点一次只删多余文件、不写图
        (NEST / "backdrop.jpg").write_bytes((NEST / "fanart.jpg").read_bytes())
        before = (NEST / "folder.jpg").stat().st_mtime
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)]})
        check("已就绪的层只清理不重画",
              not (NEST / "backdrop.jpg").exists()
              and (NEST / "folder.jpg").stat().st_mtime == before
              and "多余的图片副本" in r["message"], r["message"])

        print("\n---------- 10. 越界与非图片被拒 ----------")
        try:
            api_post(base, "/api/files/art-pin",
                     {"dir": str(LAYER2), "source": str(POSTER.with_name("影片A.nfo"))})
            check("非图片文件被拒", False, "居然成功了")
        except urllib.error.HTTPError as exc:
            check("非图片文件被拒", exc.code == 400, str(exc.code))
        other = MEDIA / "_outside.jpg"
        other.write_bytes(b"\xff\xd8\xff" + b"O" * 200)
        try:
            api_post(base, "/api/files/art-pin",
                     {"dir": str(LAYER2), "source": str(other)})
            check("文件夹外的图被拒", False, "居然成功了")
        except urllib.error.HTTPError as exc:
            check("文件夹外的图被拒", exc.code == 403, str(exc.code))
        finally:
            other.unlink(missing_ok=True)

        print("\n---------- 11. 界面：目录行的「封面」按钮 → 恢复自动 ----------")
        page.click('[data-tab="files"]')
        page.wait_for_timeout(900)
        go_root(page)
        open_dir(page, NEST.name)
        check("目录行有「封面」按钮",
              page.locator("#fileBody tr.dirrow button[data-artdir]").count() == 1)
        page.click("#fileBody tr.dirrow button[data-artdir]")
        page.wait_for_selector("#artGrid .art-card", timeout=8000)
        page.wait_for_timeout(600)
        check("弹窗里预选了当前指定的那张",
              page.locator("#artGrid .art-card.on").count() == 1)
        check("底部写明「已指定」", "已指定" in page.inner_text("#artCur"),
              page.inner_text("#artCur"))
        page.click("#btnArtAuto")
        page.wait_for_timeout(1500)
        rows = dir_rows(page)
        check("恢复自动后标签去掉「已指定」", rows.get(LAYER2.name) == DONE,
              str(rows.get(LAYER2.name)))

        page.click("#fileBody tr.dirrow button[data-artdir]")
        page.wait_for_selector("#artGrid .art-card", timeout=8000)
        page.wait_for_timeout(600)
        n_inuse = page.locator("#artGrid .art-card.inuse").count()
        cur_txt = page.inner_text("#artCur")
        check("自动挑选时标出正在使用的那张", n_inuse == 1, f"{n_inuse} 张 / {cur_txt}")
        check("底部写清自动挑了哪张",
              "自动挑选 · " in cur_txt and cur_txt.endswith(".jpg"), cur_txt)
        check("没有指定时角标不写成「当前」",
              page.locator("#artGrid .art-card .now:not(.auto)").count() == 0)
        page.click("#btnCloseArt")
        page.wait_for_timeout(400)
        check("关闭后弹窗收起", page.locator("#artModal").is_hidden())

        page.click("#fileBody tr.dirrow button[data-artdir]")
        page.wait_for_selector("#artGrid .art-card", timeout=8000)
        page.wait_for_timeout(500)
        page.locator("#artGrid .art-card").nth(0).click()
        page.click("#btnArtApply")
        page.wait_for_timeout(1600)
        rows = dir_rows(page)
        check("点「用这张」后标签变成已指定", "已指定" in (rows.get(LAYER2.name) or ""),
              str(rows.get(LAYER2.name)))
        check("页面全程没有抛错", not errors, str(errors[:2]))

        print("\n---------- 12. 收尾 ----------")
        clear_pins()
        ALT.unlink(missing_ok=True)
        check("指定的图已清理", not ALT.exists())

        browser.close()

    print("\n========== 结果 ==========")
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for name in FAIL:
            print("  -", name)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
