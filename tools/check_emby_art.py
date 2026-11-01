# -*- coding: utf-8 -*-
"""「指定封面之后，Emby / Jellyfin 会不会显示裂图」回归。

为什么单独测这个：客户端显示裂图（一个破图标）几乎都不是网络问题，而是
**文件名字叫 .jpg、内容其实不是 JPEG**——Emby 按扩展名选解码器，解不出来就
画个裂图。能踩到的路径有三条：

1. 候选海报本身是 png / webp（网页图、从图源站直接存的），写的时候又没重新
   编码，只是 copy 过去改了个名字；
2. 容器里没装中文字体，压名字失败后走「复制原图」的老路（同上）；
3. 选中的图根本没下完 / 下载回来是 html，复制过去就是一个 0 字节的假 jpg。

所以这个脚本用**真实的 png / webp / 坏图**当源，逐条断言落盘文件的
``Image.open(...).format`` 真的是 JPEG，并且能完整解码（``load()`` 不抛）。
最后再按 Emby 自己的取图优先级模拟一遍「它会拿哪个文件当封面」，这个才是
客户端实际看到的那张。

先 `python tools/devserver.py --port 6672` 起夹具环境，再跑本脚本。
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

FAIL: list[str] = []
HERE = Path(__file__).resolve().parent.parent
MEDIA = HERE / ".devdata" / "media"
LIB = MEDIA / "取图测试"
L1 = LIB / "第一层"
L2 = L1 / "第二层"
VIDEO = L2 / "影片A.strm"

# 源图：同一张图分别存成三种格式 + 一个坏文件
SRC_JPG = L2 / "影片A-poster.jpg"
SRC_PNG = L2 / "影片B-poster.png"
SRC_WEBP = L2 / "影片C-poster.webp"
SRC_BAD = L2 / "影片D-poster.jpg"

# Emby / Jellyfin 找「文件夹封面」的文件名顺序（LocalImageProvider）。
# 排在前面的一旦存在就用它，所以只验证「第一个存在的那个必须能解码」。
PRIMARY_ORDER = ("folder", "poster", "cover")
BACKDROP_ORDER = ("backdrop", "fanart")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api_post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def emby_pick(folder: Path, order: tuple[str, ...]) -> Path | None:
    """按 Emby 的优先级从目录里挑出它会用的那张图。"""
    for stem in order:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            path = folder / f"{stem}{ext}"
            if path.is_file():
                return path
    # 老规矩：<目录名>.jpg 也算
    for ext in (".jpg", ".jpeg", ".png"):
        path = folder / f"{folder.name}{ext}"
        if path.is_file():
            return path
    return None


def jpeg_ok(path: Path) -> tuple[bool, str]:
    """能不能当 JPEG 解码 —— 这就是「会不会裂图」的判据。"""
    try:
        with Image.open(path) as im:
            fmt = im.format
            im.load()
            size = f"{im.width}x{im.height}"
        return fmt == "JPEG", f"{path.name}: format={fmt} {size}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{path.name}: 打不开（{exc}）"


def seed() -> None:
    """造三层目录，源图覆盖 jpg / png / webp / 坏文件。"""
    L2.mkdir(parents=True, exist_ok=True)
    VIDEO.write_text("http://example.invalid/a.mkv\n", encoding="utf-8")

    image = Image.new("RGB", (400, 600), (90, 120, 150))
    for y in range(0, 600, 40):
        for x in range(0, 400, 40):
            image.paste((220, 200, 180), (x, y, x + 20, y + 20))

    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=95)
    SRC_JPG.write_bytes(buf.getvalue())
    buf = io.BytesIO()
    image.save(buf, "PNG")
    SRC_PNG.write_bytes(buf.getvalue())
    buf = io.BytesIO()
    image.save(buf, "WEBP", quality=95)
    SRC_WEBP.write_bytes(buf.getvalue())
    # 下载到一半的那种：头是 jpg 的魔数，后面全是垃圾
    SRC_BAD.write_bytes(b"\xff\xd8\xff" + b"x" * 200)

    for folder in (LIB, L1, L2):
        for path in folder.glob("*"):
            if path.is_file() and path.name in (
                    "folder.jpg", "poster.jpg", "fanart.jpg",
                    "backdrop.jpg", f"{folder.name}-poster.jpg",
                    f"{folder.name}-fanart.jpg"):
                path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6672")
    ap.add_argument("--keep", action="store_true",
                    help="跑完保留夹具目录，方便自己翻盘上的文件")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    seed()
    try:
        return run(base)
    finally:
        if not args.keep:
            # 别的回归脚本（check_folder_poster 之类）会在同一个媒体库里断言
            #「根目录除了自己的夹具没有别的带标签的目录」，留着会互相干扰
            shutil.rmtree(LIB, ignore_errors=True)


def run(base: str) -> int:
    info = json.load(urllib.request.urlopen(base + "/api/files"))
    if not any(d["name"] == LIB.name for d in info["dirs"]):
        print("夹具没被扫描到，先让服务扫一次媒体库")
        return 2

    print("\n---------- 1. 源图确实是 png / webp / 坏图 ----------")
    with Image.open(SRC_PNG) as im:
        check("夹具里有一张真 PNG", im.format == "PNG", str(im.format))
    with Image.open(SRC_WEBP) as im:
        check("夹具里有一张真 WebP", im.format == "WEBP", str(im.format))
    check("夹具里有一张坏图", not jpeg_ok(SRC_BAD)[0])

    print("\n---------- 2. 先补一次，让两层都就绪 ----------")
    r = api_post(base, "/api/files/folder-poster", {"dirs": [str(LIB)], "force": True})
    check("两层都补上了", len(r.get("created", [])) == 2, r["message"])
    check("每层只写三个文件",
          all(sorted(p.name for p in f.iterdir() if p.suffix == ".jpg")
              == ["fanart.jpg", "folder.jpg", "poster.jpg"]
              for f in (LIB, L1)),
          str([(f.name, sorted(p.name for p in f.iterdir() if p.suffix == ".jpg"))
               for f in (LIB, L1)]))

    print("\n---------- 3. 指定 PNG 当封面：落盘的必须还是 JPEG ----------")
    r = api_post(base, "/api/files/art-pin", {"dir": str(L1), "source": str(SRC_PNG)})
    check("指定成功", r.get("ok") is True, r["message"])
    ok, detail = jpeg_ok(L1 / "folder.jpg")
    check("folder.jpg 是真 JPEG（不是改了名的 png）", ok, detail)
    ok, detail = jpeg_ok(L1 / "poster.jpg")
    check("poster.jpg 是真 JPEG", ok, detail)
    ok, detail = jpeg_ok(L1 / "fanart.jpg")
    check("fanart.jpg 是真 JPEG", ok, detail)
    check("PNG 的透明/调色板没被抄进扩展名",
          (L1 / "folder.jpg").read_bytes()[:3] == b"\xff\xd8\xff",
          str((L1 / "folder.jpg").read_bytes()[:4]))
    png_cover = (L1 / "folder.jpg").read_bytes()

    primary = emby_pick(L1, PRIMARY_ORDER)
    check("Emby 会挑 folder.jpg 当封面", primary == L1 / "folder.jpg", str(primary))
    ok, detail = jpeg_ok(primary)
    check("Emby 拿到的这张能解码（不会裂图）", ok, detail)

    backdrop = emby_pick(L1, BACKDROP_ORDER)
    ok, detail = jpeg_ok(backdrop) if backdrop else (False, "没有背景图")
    check("Emby 拿到的背景图能解码", ok, detail)
    if backdrop:
        with Image.open(backdrop) as im:
            ratio = im.width / im.height
        check("背景图是 16:9", abs(ratio - 16 / 9) < 0.03, f"{ratio:.2f}")

    print("\n---------- 4. 指定 WebP 当封面：同样必须是 JPEG ----------")
    r = api_post(base, "/api/files/art-pin", {"dir": str(L1), "source": str(SRC_WEBP)})
    check("指定成功", r.get("ok") is True, r["message"])
    for name in ("folder.jpg", "poster.jpg", "fanart.jpg"):
        ok, detail = jpeg_ok(L1 / name)
        check(f"{name} 能解码且是 JPEG", ok, detail)
    from PIL import ImageChops, ImageStat
    with Image.open(io.BytesIO(png_cover)) as a, Image.open(L1 / "folder.jpg") as b:
        stat = ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB")))
    check("换成 WebP 源后画的还是同一张图（像素几乎一致）",
          max(stat.mean) < 6, f"平均差 {[round(v, 2) for v in stat.mean]}")

    print("\n---------- 5. 指定坏图：一个字节都不写，也不能返回成功 ----------")
    before = (L1 / "folder.jpg").read_bytes()
    try:
        api_post(base, "/api/files/art-pin", {"dir": str(L1), "source": str(SRC_BAD)})
        check("坏图被拒", False, "居然返回成功")
    except urllib.error.HTTPError as exc:
        check("坏图被拒（4xx）", 400 <= exc.code < 500, str(exc.code))
    check("原有封面没被破坏", (L1 / "folder.jpg").read_bytes() == before)
    check("目录里没多出假 jpg",
          not (L1 / "folder.jpg").read_bytes().endswith(b"x" * 200))

    print("\n---------- 6. 恢复自动 → 又换回自动挑的那张，仍是 JPEG ----------")
    r = api_post(base, "/api/files/art-pin", {"dir": str(L1), "source": ""})
    check("恢复自动成功", r.get("ok") is True, r["message"])
    ok, detail = jpeg_ok(L1 / "folder.jpg")
    check("重画后仍是真 JPEG", ok, detail)

    print("\n---------- 7. 老库里有 backdrop.jpg 时删掉，背景图不能断 ----------")
    (L1 / "backdrop.jpg").write_bytes((L1 / "fanart.jpg").read_bytes())
    api_post(base, "/api/files/folder-poster", {"dirs": [str(L1)], "force": True})
    check("重复的 backdrop.jpg 被清掉", not (L1 / "backdrop.jpg").exists())
    backdrop = emby_pick(L1, BACKDROP_ORDER)
    ok, detail = jpeg_ok(backdrop) if backdrop else (False, "没有背景图")
    check("删掉 backdrop.jpg 后 Emby 仍有可解码的背景图", ok, detail)

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
