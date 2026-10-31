# -*- coding: utf-8 -*-
"""「文件夹海报修复」回归：多层目录的提示与手动修复。

背景：Emby 与 Jellyfin 用同一套取图规则 —— 目录里直接有视频，它是「影片」
条目，认 ``<视频名>-poster.jpg``；目录里只有子目录，它是「文件夹」条目，
只认 ``folder.jpg`` / ``poster.jpg`` / ``cover.jpg``。所以多层嵌套时中间那几层
一定没有封面，按视频名生成的海报对它们完全无效。

这个脚本验证四件事：
1. 多层目录在文件管理、任务列表的备注里能被标出来（单层目录不能被误标）；
2. 修复只在这个动作被点时发生，正常刮削不会写 folder.jpg / poster.jpg；
3. 修复覆盖「选中目录 + 它的各层祖先 + 它下面每一层」，每层写 folder.jpg 与
   poster.jpg 两个名字（两个客户端都能认），已有封面默认跳过；
4. 扫描根本身永远不写。

先 `python tools/devserver.py` 起环境（6670），再跑本脚本（需要本机 Edge）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAIL: list[str] = []
HERE = Path(__file__).resolve().parent.parent
MEDIA = HERE / ".devdata" / "media"
DB = HERE / ".devdata" / "state.db"
NEST = MEDIA / "嵌套测试"
LAYER2 = NEST / "第一层"
LAYER3 = LAYER2 / "第二层"
LAYERS = [NEST, LAYER2, LAYER3]
VIDEO = LAYER3 / "影片A.strm"
POSTER = LAYER3 / "影片A-poster.jpg"
# 第二张海报：用来验证「父层指定用哪张」确实按指定的走，而不是按名字挑第一张
ALT = LAYER3 / "影片Z-指定海报-poster.jpg"
WRITE_NAMES = ("folder.jpg", "poster.jpg")
MISS = "缺海报"
DONE = "海报已就绪"


def arts_of(folder: Path) -> list[Path]:
    return [folder / name for name in WRITE_NAMES]


ALL_ARTS = [a for folder in LAYERS for a in arts_of(folder)]


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
    POSTER.write_bytes(b"\xff\xd8\xff" + b"P" * 400)
    (LAYER3 / "影片A-fanart.jpg").write_bytes(b"\xff\xd8\xff" + b"F" * 600)
    (LAYER3 / "影片A.nfo").write_text("<movie><title>影片A</title></movie>", encoding="utf-8")
    ALT.unlink(missing_ok=True)
    clear_pins()
    if clean:
        for art in ALL_ARTS:
            if art.exists():
                art.unlink()
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

        print("\n---------- 1. 文件管理：多层目录在备注里被标出来 ----------")
        rows = dir_rows(page)
        check("根目录的「嵌套测试」标了缺海报", rows.get("嵌套测试") == MISS,
              str(rows.get("嵌套测试")))
        singles = [k for k, v in rows.items() if v and k != "嵌套测试"]
        check("单层目录（直接放视频）不被误标", not singles, str(singles))
        check("按钮默认禁用（还没勾）", page.is_disabled("#btnFolderArt"))

        open_dir(page, "嵌套测试")
        rows = dir_rows(page)
        check("进去后「第一层」（第 2 层、无直放视频）也标了", rows.get("第一层") == MISS,
              str(rows.get("第一层")))
        open_dir(page, "第一层")
        rows = dir_rows(page)
        check("「第二层」（第 3 层、有直放视频）也标了", rows.get("第二层") == MISS,
              str(rows.get("第二层")))

        open_dir(page, "第二层")
        file_tags = page.evaluate("""() => [...document.querySelectorAll('#fileBody tr[data-path] .foldertag')]
            .map(e => e.textContent.trim())""")
        check("文件行的备注也标了", file_tags == [MISS], str(file_tags))

        print("\n---------- 2. 任务列表：备注同样标出来 ----------")
        page.click('[data-tab="tasks"]')
        page.wait_for_timeout(1400)
        task_tags = page.evaluate("""() => [...document.querySelectorAll('#taskBody .foldertag')]
            .map(e => e.textContent.trim())""")
        check("文件视图里那条深层记录带标记", MISS in task_tags, str(task_tags))
        page.click('#taskView button[data-view="dir"]')
        page.wait_for_timeout(1200)
        grp = page.evaluate("""() => {
          const row = [...document.querySelectorAll('#taskBody tr.grouprow')]
            .find(tr => tr.textContent.includes('第二层'));
          const tag = row && row.querySelector('.foldertag');
          return tag ? tag.textContent.trim() : '';
        }""")
        check("文件夹视图里该文件夹行也带标记", grp == MISS, grp)

        print("\n---------- 3. 正常刮削不会写 folder.jpg / poster.jpg ----------")
        r = api_post(base, "/api/files/scrape", {"paths": [str(VIDEO)]})
        check("产物齐全的刮削直接跳过", not r.get("started"), r.get("message", ""))
        check("刮削没有生成任何文件夹封面",
              not any(a.exists() for a in ALL_ARTS),
              str([a.name for a in ALL_ARTS if a.exists()]))

        print("\n---------- 4. 勾选文件夹 → 点修复 → 每层都补两个名字 ----------")
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
        check("提示写明补了 3 层 / 6 个文件", "已补 3 个文件夹封面" in toast and "6 个文件" in toast, toast)
        check("三层的 folder.jpg + poster.jpg 都落地",
              all(a.is_file() for a in ALL_ARTS),
              str([(a.parent.name, a.name, a.is_file()) for a in ALL_ARTS]))
        src = POSTER.read_bytes()
        check("内容取自里面那张海报", all(a.read_bytes() == src for a in ALL_ARTS))

        print("\n---------- 5. 再点一次：默认跳过已有的 ----------")
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)]})
        check("第二次全部跳过", "3 个已就绪跳过" in r["message"], r["message"])
        check("没有重复写", r["created"] == [], str(r["created"]))

        print("\n---------- 6. 补完之后备注变成「海报已就绪」 ----------")
        page.wait_for_timeout(600)
        rows = dir_rows(page)
        check("根目录标签翻转", rows.get("嵌套测试") == DONE, str(rows.get("嵌套测试")))

        print("\n---------- 7. 每一层都补：单层目录同样有封面，扫描根不写 ----------")
        root_path = Path(api_get(base, "/api/files")["path"])
        single = next((d for d in api_get(base, "/api/files")["dirs"]
                       if d["name"] != "嵌套测试" and d["own"]), None)
        if single:
            sp = Path(single["path"])
            # 先清干净，避免上一轮跑残的封面让「已就绪跳过」提前命中
            for n in WRITE_NAMES:
                (sp / n).unlink(missing_ok=True)
            # 拿这个目录里任意一个 strm 造一张海报当源，模拟「已经刮过」的目录；
            # 不这么做会撞上「没找到可用海报（先刮削再补）」，测的就不是本意了
            fake = sp / "_seed-poster.jpg"
            if not fake.exists():
                fake.write_bytes(b"\xff\xd8\xff" + b"S" * 300)
            r = api_post(base, "/api/files/folder-poster", {"dirs": [single["path"]]})
            check("单层目录也会补上封面", "已补 1 个文件夹封面" in r["message"], r["message"])
            check("单层目录两个名字都有了",
                  all((sp / n).is_file() for n in WRITE_NAMES),
                  str([n for n in WRITE_NAMES if not (sp / n).is_file()]))
            r2 = api_post(base, "/api/files/folder-poster", {"dirs": [single["path"]], "force": True})
            check("强制重建不报错", r2.get("ok") is True and not r2["failed"], r2["message"])
            # 收尾：别把测试封面留在别的用例目录里
            for n in (*WRITE_NAMES, fake.name):
                (sp / n).unlink(missing_ok=True)

        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(root_path)]})
        check("扫描根本身不写封面",
              not any((root_path / n).exists() for n in WRITE_NAMES),
              str([n for n in WRITE_NAMES if (root_path / n).exists()]))

        print("\n---------- 8. 指定用哪张：父层不再被「按名字挑第一张」覆盖 ----------")
        ALT.write_bytes(b"\xff\xd8\xff" + b"A" * 520)
        cand = api_get(base, "/api/files/art-candidates?path="
                       + urllib.parse.quote(str(LAYER2)))
        names = [i["name"] for i in cand["items"]]
        check("候选里能看到里面两张海报",
              ALT.name in names and POSTER.name in names, str(names))
        check("本层已有的 folder.jpg 不算候选",
              not any(i["path"] == str(LAYER2 / "folder.jpg") for i in cand["items"]),
              str([i["name"] for i in cand["items"]]))
        check("候选带相对路径，前端能区分同名图",
              all(i.get("rel") for i in cand["items"]), str(cand["items"][:1]))
        check("正片海报排在 folder 副本前面", cand["items"][0]["kind"] == "poster",
              str(cand["items"][0]["kind"]))

        r = api_post(base, "/api/files/art-pin", {"dir": str(LAYER2), "source": str(ALT)})
        check("指定成功并立刻写两个名字", r.get("ok") and "2 个文件" in r["message"], r["message"])
        check("两个文件的内容都是指定的那张",
              all(a.read_bytes() == ALT.read_bytes() for a in arts_of(LAYER2)),
              str([(a.name, a.stat().st_size) for a in arts_of(LAYER2)]))

        d = api_get(base, "/api/files?path=" + urllib.parse.quote(str(NEST)))
        row = next(x for x in d["dirs"] if x["name"] == LAYER2.name)
        check("列表带回指定标记与图名", row["pinned"] == 1 and row["pin_name"] == ALT.name,
              str((row["pinned"], row["pin_name"])))

        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)], "force": True})
        check("覆盖重建时按指定的图走", r.get("pinned") == 1, r["message"])
        check("重建后第一层仍是指定那张", all(a.read_bytes() == ALT.read_bytes()
                                            for a in arts_of(LAYER2)))
        check("没指定的层照旧自动挑", arts_of(LAYER3)[0].read_bytes() == POSTER.read_bytes())

        (LAYER2 / "poster.jpg").unlink(missing_ok=True)
        r = api_post(base, "/api/files/folder-poster", {"dirs": [str(NEST)]})
        check("缺一个名字时补回来且仍用指定的图",
              (LAYER2 / "poster.jpg").is_file()
              and (LAYER2 / "poster.jpg").read_bytes() == ALT.read_bytes(), r["message"])

        print("\n---------- 9. 越界与非图片被拒 ----------")
        try:
            api_post(base, "/api/files/art-pin",
                     {"dir": str(LAYER2), "source": str(POSTER.with_name("影片A.nfo"))})
            check("非图片文件被拒", False, "居然成功了")
        except urllib.error.HTTPError as exc:
            check("非图片文件被拒", exc.code == 400, str(exc.code))
        other = MEDIA / "_outside.jpg"
        other.write_bytes(b"\xff\xd8\xff" + b"O" * 200)
        if other:
            try:
                api_post(base, "/api/files/art-pin",
                         {"dir": str(LAYER2), "source": str(other)})
                check("文件夹外的图被拒", False, "居然成功了")
            except urllib.error.HTTPError as exc:
                check("文件夹外的图被拒", exc.code == 403, str(exc.code))
            finally:
                other.unlink(missing_ok=True)

        print("\n---------- 10. 界面：目录行的「封面」按钮 → 恢复自动 ----------")
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
        # 当前屏在「嵌套测试」这一层，它的子目录是「第一层」
        check("恢复自动后标签去掉「已指定」", rows.get(LAYER2.name) == DONE,
              str(rows.get(LAYER2.name)))

        page.click("#fileBody tr.dirrow button[data-artdir]")
        page.wait_for_selector("#artGrid .art-card", timeout=8000)
        page.wait_for_timeout(500)
        page.locator("#artGrid .art-card").nth(0).click()
        page.click("#btnArtApply")
        page.wait_for_timeout(1600)
        rows = dir_rows(page)
        check("点「用这张」后标签变成已指定", "已指定" in (rows.get(LAYER2.name) or ""),
              str(rows.get(LAYER2.name)))
        check("页面没有新增报错", not errors, str(errors[:2]))

        print("\n---------- 11. 收尾 ----------")
        clear_pins()
        ALT.unlink(missing_ok=True)
        check("指定的图已清理", not ALT.exists())
        check("页面全程没有抛错", not errors, str(errors[:2]))

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
