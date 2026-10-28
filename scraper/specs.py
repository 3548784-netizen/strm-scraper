# -*- coding: utf-8 -*-
"""图片规格：Emby 与 Jellyfin 对海报/背景图的硬性要求。

两家文档给的尺寸不同，但比例是一致的（这也是它们能通用同一套文件的原因）：

============  ========  =====================  ==============================
产物           比例      推荐尺寸                说明
============  ========  =====================  ==============================
poster        2:3       1000×1500              竖版封面，媒体库网格里显示
fanart        16:9      1920×1080              横版背景图，详情页大图/背景
thumb         16:9      1000×562               列表/剧集缩略图（可选）
============  ========  =====================  ==============================

比例不对的结果是「图能被读进去，但显示被拉变形或者被裁成一条」，比不显示更难
发现，所以配置里的尺寸一律按比例校正，偏了就在日志里明确告警。
"""
from __future__ import annotations

from dataclasses import dataclass

# 宽 / 高
POSTER_RATIO = 2 / 3
WIDE_RATIO = 16 / 9

# 低于这个宽度媒体服务器会嫌弃，或者上采样后糊
MIN_WIDTH = {"poster": 500, "fanart": 1280, "thumb": 640}


@dataclass
class Spec:
    kind: str
    width: int
    height: int
    ratio: float
    label: str

    @property
    def ok(self) -> bool:
        if not self.width or not self.height:
            return False
        actual = self.width / self.height
        return abs(actual - self.ratio) / self.ratio < 0.01


def spec_for(kind: str) -> Spec:
    if kind == "poster":
        return Spec("poster", 1000, 1500, POSTER_RATIO, "2:3")
    if kind == "fanart":
        return Spec("fanart", 1920, 1080, WIDE_RATIO, "16:9")
    return Spec("thumb", 1000, 562, WIDE_RATIO, "16:9")


def conform(width: int, height: int, kind: str) -> tuple[int, int, str]:
    """把尺寸校正到合法比例，返回（宽, 高, 提示）。

    以宽度为基准，按比例算高度；宽度太小则抬到最低要求。比例本来就对的时候
    原样返回，不动用户的值。
    """
    spec = spec_for(kind)
    width = max(16, int(width or spec.width))
    height = max(16, int(height or spec.height))

    actual = width / height
    if abs(actual - spec.ratio) / spec.ratio < 0.01:
        if width < MIN_WIDTH[kind]:
            new_width = MIN_WIDTH[kind]
            return new_width, round(new_width / spec.ratio), (
                f"{kind} 宽度 {width} 偏小，已抬到 {new_width}"
            )
        return width, height, ""

    new_height = round(width / spec.ratio)
    return width, new_height, (
        f"{kind} 配置的是 {width}×{height}（比例 {width / height:.2f}:1），"
        f"不符合 {spec.label}，已自动校正为 {width}×{new_height}"
    )


def is_conforming(width: int, height: int, kind: str) -> bool:
    """给定尺寸是否符合该类产物的比例要求。"""
    if not width or not height:
        return False
    target = spec_for(kind).ratio
    actual = width / height
    return abs(actual - target) / target < 0.01


def describe_all(width: int, height: int) -> str:
    """给自检命令用的一行说明。"""
    return f"{width}×{height}，比例 {width / height:.3f}:1"
