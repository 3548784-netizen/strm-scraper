# -*- coding: utf-8 -*-
"""NFO 生成：输出 Emby / Jellyfin 都能识别的 Kodi 风格 movie NFO。"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .probe import MediaInfo

log = logging.getLogger(__name__)

# 文件名里常见的“伪扩展名”，strm 命名常写成 xxx.AVI.strm
VIDEO_EXTENSIONS = {
    "avi", "mkv", "mp4", "m4v", "wmv", "rmvb", "rm", "ts", "m2ts", "mts", "tp",
    "mpg", "mpeg", "mov", "flv", "webm", "iso", "vob", "3gp", "f4v", "asf", "divx",
    "xvid", "ogv", "m2v", "dat", "vro", "wtv", "dvr-ms",
}

# 清晰度后缀，出现在方括号或圆括号里时一并去掉
QUALITY_TAGS = re.compile(
    r"[\[\(（【]?\s*(2160p|1080p|1080i|720p|480p|4k|8k|uhd|hdr|bluray|blu-ray|web-?dl|webrip|hdrip|bdrip)\s*[\]\)）】]?",
    re.I,
)


# 资源站的广告前缀：``【mtcang.net】`` / ``[www.98T.la]`` / ``www.98T.la@``
# 只认带回点域名的那一段，避免把正常的【】片名削掉。
_TLDS = r"com|net|org|cc|la|tv|me|xyz|top|vip|cn|info|club|app|io|co|link|site|pro"
SITE_BRACKET = re.compile(
    rf"^\s*[\[【]\s*[^】\]]{{0,60}}?(?:\.(?:{_TLDS})\b)[^】\]]{{0,20}}\s*[\]】]\s*",
    re.I,
)
SITE_AT = re.compile(
    rf"^\s*(?:https?://)?(?:www\.)?[\w.\-]+\.(?:{_TLDS})@\s*",
    re.I,
)
SITE_URL = re.compile(r"^\s*https?://\S+\s+", re.I)
# 紧跟站名后面的营销词，一并去掉：``【精品】``、``[推荐]``
PROMO_TAG = re.compile(
    r"^\s*[\[【]\s*(?:精品|高清|超清|原画|推荐|首发|独家|原创|自拍|合集|搬运|完整版?)\s*[\]】]\s*"
)


def display_title(stem: str) -> str:
    """从去掉了 .strm 的文件名推出干净的标题。

    例：``片名.AVI`` → ``片名``；``片名.2024.1080p`` → ``片名.2024``；
    ``www.98T.la@片名 (1)`` → ``片名 (1)``；``【mtcang.net】【精品】片名`` → ``片名``。

    只剥开头明显是资源站广告的那几段（域名、URL、紧邻的营销词），
    片名里的括号、年份、序号一律保留。
    """
    name = stem.strip()
    if not name:
        return stem

    # 去掉末尾的伪视频扩展名，可能有多层（xxx.AVI）
    while True:
        base, dot, ext = name.rpartition(".")
        if dot and ext.lower() in VIDEO_EXTENSIONS and base.strip():
            name = base
            continue
        break

    while True:
        before = name
        for pattern in (SITE_URL, SITE_BRACKET, SITE_AT, PROMO_TAG):
            name = pattern.sub("", name, count=1)
        if name == before:
            break

    cleaned = QUALITY_TAGS.sub("", name)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ._-")
    if not cleaned:
        return stem    # 整段都是广告词（少见），宁可保留原名
    return cleaned



def build_nfo(info: MediaInfo, title: str, added: datetime | None = None) -> str:
    """组装 NFO 文本。"""
    root = ET.Element("movie")

    ET.SubElement(root, "plot")
    ET.SubElement(root, "outline")
    ET.SubElement(root, "lockdata").text = "false"
    ET.SubElement(root, "dateadded").text = (added or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "sorttitle").text = title
    # Emby / Jellyfin 的 runtime 单位是分钟
    ET.SubElement(root, "runtime").text = str(max(1, round(info.duration / 60))) if info.duration else "0"

    fileinfo = ET.SubElement(root, "fileinfo")
    details = ET.SubElement(fileinfo, "streamdetails")

    if info.video:
        video = ET.SubElement(details, "video")
        ET.SubElement(video, "codec").text = info.video.codec or "unknown"
        ET.SubElement(video, "micodec").text = info.video.codec or "unknown"
        ET.SubElement(video, "bitrate").text = str(info.video.bitrate or 0)
        ET.SubElement(video, "width").text = str(info.video.width)
        ET.SubElement(video, "height").text = str(info.video.height)
        aspect = _aspect_label(info.video.width, info.video.height)
        ET.SubElement(video, "aspect").text = aspect
        ET.SubElement(video, "aspectratio").text = aspect
        if info.video.fps:
            ET.SubElement(video, "framerate").text = f"{info.video.fps:.5f}".rstrip("0")
        ET.SubElement(video, "language").text = info.video.language or "und"
        ET.SubElement(video, "scantype").text = "progressive"
        ET.SubElement(video, "default").text = "True"
        ET.SubElement(video, "forced").text = "False"
        if info.duration:
            ET.SubElement(video, "duration").text = str(max(1, round(info.duration / 60)))
            ET.SubElement(video, "durationinseconds").text = str(int(info.duration))

    for index, audio in enumerate(info.audio):
        node = ET.SubElement(details, "audio")
        ET.SubElement(node, "codec").text = audio.codec or "unknown"
        ET.SubElement(node, "micodec").text = audio.codec or "unknown"
        ET.SubElement(node, "bitrate").text = str(audio.bitrate or 0)
        ET.SubElement(node, "language").text = audio.language or "und"
        ET.SubElement(node, "scantype").text = "progressive"
        ET.SubElement(node, "channels").text = str(audio.channels or 2)
        ET.SubElement(node, "samplingrate").text = str(audio.sampling_rate or 48000)
        ET.SubElement(node, "default").text = "True" if index == 0 else "False"
        ET.SubElement(node, "forced").text = "False"

    _indent(root)
    body = ET.tostring(root, encoding="unicode")
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        + body
        + "\n"
    )


def write_nfo(path: Path, info: MediaInfo, title: str,
              added: datetime | None = None) -> None:
    """写 NFO 文件（UTF-8 无 BOM，Emby / Jellyfin 都认）。"""
    text = build_nfo(info, title, added)
    path.write_text(text, encoding="utf-8", newline="\n")


def _aspect_label(width: int, height: int) -> str:
    if not width or not height:
        return "16:9"
    from math import gcd

    divisor = gcd(width, height) or 1
    w, h = width // divisor, height // divisor
    # 1280:720 这类能约成 16:9，1920:1080 也是；约完太大就用近似值
    if w <= 32 and h <= 32:
        return f"{w}:{h}"
    ratio = width / height
    for label, value in (("16:9", 16 / 9), ("4:3", 4 / 3), ("21:9", 21 / 9), ("1:1", 1.0)):
        if abs(ratio - value) < 0.03:
            return label
    return f"{ratio:.2f}:1"


def _indent(element: ET.Element, level: int = 0) -> None:
    """给 XML 加缩进，方便人直接看和 diff。"""
    pad = "\n" + "  " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = pad + "  "
        for child in element:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    if level and (not element.tail or not element.tail.strip()):
        element.tail = pad
