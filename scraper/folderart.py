"""给文件夹的封面（竖版海报）和背景图（横版）压上文件夹名。

「媒体库/合集/第 1 部、第 2 部…」这种结构里，每一层的封面都是从里层抄上来的
同一张图，几个兄弟目录看着一模一样，光看海报分不出是哪一层。这里在底部压一条
渐变名字条，写上层目录名 —— 网格里一眼能区分。

背景图另算：Emby / Jellyfin 的背景图是横版展示，直接拿竖版海报会被拉成
上下留黑或者糊成大色块，所以先居中裁成 16:9 再压字。

不写 NFO：文件夹条目本来就不吃 NFO（Emby / Jellyfin 的本地图片只按固定文件名
找，NFO 里的信息对「只有子目录、没有视频」的层不生效），写进去只是多一个没人读
的文件。

图片编码统一走 JPEG（Emby / Jellyfin 只认 .jpg / .png，JPEG 最省事），
字体找不到或者源图有怪癖时，调用方退到 ``encode_jpeg`` 只转码不压字 ——
**任何情况下落盘的都是真 JPEG**，绝不把 png / webp 的原字节改成 .jpg 名字，
那样 Emby 会按扩展名当 JPEG 解码，客户端里就是一个裂图图标。
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 优先粗体：海报底下是照片，细体压不住
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",          # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",          # 黑体
    "C:/Windows/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",   # 文泉驿微米黑（Docker 里装的）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

BAND_RATIO = 0.16          # 名字条占封面高度
MAX_WIDTH = 1200           # 再大的图也缩到这个宽度，省空间也省解码
BACKDROP_RATIO = 16 / 9    # 背景图按这个比例裁，和 Emby / Jellyfin 的展示框一致
_ILLEGAL = '<>:"/\\|?*'


def safe_stem(name: str, limit: int = 80) -> str:
    """把目录名变成能当文件名用的主干。

    Windows 上 ``\\ / : * ? " < > |`` 都不能出现在文件名里，替换成下划线；
    结尾的点和空格会被系统悄悄吃掉，一并去掉。整段为空时退回 ``folder``。
    """
    cleaned = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip().rstrip(". ")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(". ")
    return cleaned or "folder"


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def has_font() -> bool:
    """能不能压字 —— 容器里没装中文字体时用来给出明确提示。"""
    return _font(24) is not None


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: float,
         max_h: float, start: int) -> tuple[ImageFont.FreeTypeFont | None, str]:
    """字号从大到小试到装得下；最小字号还装不下就截断加省略号。"""
    size = max(start, 12)
    while size >= 12:
        font = _font(size)
        if font is None:
            return None, text
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return font, text
        size -= 2
    font = _font(12)
    if font is None:
        return None, text
    cut = text
    while cut and draw.textbbox((0, 0), cut + "…", font=font)[2] > max_w:
        cut = cut[:-1]
    return font, (cut + "…") if cut else text


def _load(src: Path) -> Image.Image | None:
    try:
        with Image.open(src) as raw:
            image = raw.convert("RGB")
    except (OSError, ValueError):
        return None
    return image


def _shrink(image: Image.Image) -> Image.Image:
    """超宽的图缩到 MAX_WIDTH，省空间也省客户端的解码开销。"""
    if image.width > MAX_WIDTH:
        height = round(image.height * MAX_WIDTH / image.width)
        return image.resize((MAX_WIDTH, max(height, 1)), Image.LANCZOS)
    return image


def _trim_band(image: Image.Image) -> Image.Image:
    """裁掉底部那条旧名字条。

    「拿本层已有的封面再压一遍」时必须先裁，否则新旧两行字会叠在一起；
    上一版没裁干净时，旧字的白色笔画还会从半透明渐变里透出来。
    """
    keep = int(image.height * (1 - BAND_RATIO))
    if keep > image.height * 0.5:
        return image.crop((0, 0, image.width, keep))
    return image


def _band_and_text(image: Image.Image, name: str) -> bytes | None:
    """在底部压名字条，返回 JPEG 字节；没有可用字体时返回 ``None``。"""
    width, height = image.size
    band = max(int(height * BAND_RATIO), 44)
    band = min(band, int(height * 0.32))
    top = height - band

    # 从透明渐变到半透明黑：不硬切一刀，照片下缘还能透出来
    mask = Image.linear_gradient("L").resize((width, band))
    mask = mask.point(lambda value: int(value * 205 / 255))
    region = image.crop((0, top, width, height))
    region = Image.composite(Image.new("RGB", region.size, (0, 0, 0)), region, mask)
    image.paste(region, (0, top))

    draw = ImageDraw.Draw(image)
    font, text = _fit(draw, name, width * 0.9, band * 0.62, int(band * 0.55))
    if font is None:
        return None
    box = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    x = (width - text_w) / 2 - box[0]
    y = top + (band - text_h) / 2 - box[1]

    # 描边当阴影：浅色海报上也能看清
    draw.text((x, y), text, font=font, fill=(255, 255, 255),
              stroke_width=max(1, int(band * 0.03)), stroke_fill=(0, 0, 0))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


def _crop_backdrop(image: Image.Image) -> Image.Image:
    """居中裁成 16:9 横版，裁剪位置略偏上——人脸通常在画面偏上三分之一。"""
    width, height = image.size
    target_h = round(width / BACKDROP_RATIO)
    if height > target_h:
        top = int((height - target_h) * 0.42)
        return image.crop((0, top, width, top + target_h))
    return image


def _to_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


def encode_jpeg(src: Path, crop_band: bool = False,
                as_backdrop: bool = False) -> bytes | None:
    """只转码、不压名字，返回 JPEG 字节；源图读不了返回 ``None``。

    这是「压不上名字」时的保底路径（容器里没装中文字体、源图是 png / webp
    或者带怪异的色彩配置文件）。重点是**一定要重新编码**：直接把 png / webp
    的字节写成 ``folder.jpg``，Emby 会按扩展名当 JPEG 解码，也就是裂图。
    """
    image = _load(src)
    if image is None:
        return None
    if crop_band:
        image = _trim_band(image)
    if as_backdrop:
        image = _crop_backdrop(image)
    return _to_jpeg(_shrink(image))


def render_cover(src: Path, name: str, crop_band: bool = False) -> bytes | None:
    """读 ``src``，在底部压上 ``name``，返回 JPEG 字节（竖版海报，保持原比例）。

    ``crop_band`` 用于「拿本层已有的封面再压一遍」的情况：先把上一条名字条
    裁掉，否则两行字会叠在一起。
    """
    image = _load(src)
    if image is None:
        return None
    image = _shrink(image)
    if crop_band:
        image = _trim_band(image)
    return _band_and_text(image, name)


def render_backdrop(src: Path, name: str, crop_band: bool = False) -> bytes | None:
    """和 ``render_cover`` 一样压名字，但先居中裁成 16:9 的横版背景图。

    源图是竖版海报时直接拿去当背景，客户端要么上下留黑、要么糊成大色块，
    所以先裁再压。
    """
    image = _load(src)
    if image is None:
        return None
    if crop_band:
        image = _trim_band(image)
    image = _shrink(_crop_backdrop(image))
    return _band_and_text(image, name)
