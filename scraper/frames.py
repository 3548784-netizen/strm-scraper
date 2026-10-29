# -*- coding: utf-8 -*-
"""抓帧与图片加工：采样多帧、挑最清晰的一帧、生成海报和背景图。"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageStat

from . import specs
from .probe import find_binary

log = logging.getLogger(__name__)

# 单帧低于这个字节数基本是空文件；黑屏另由亮度判断筛掉
MIN_FRAME_BYTES = 512
# 平均亮度低于此值视为黑屏（0~255）
MIN_BRIGHTNESS = 12.0

# 轮询 ffmpeg 是否退出的间隔。取 0.2 秒，暂停时最多这么久就能收到中断
_POLL_SECONDS = 0.2


class FrameError(RuntimeError):
    """抓帧失败。"""


class CaptureAborted(FrameError):
    """抓帧被暂停 / 取消中断，不是这个文件本身的问题。"""


def _aborted(abort: threading.Event | None) -> bool:
    return abort is not None and abort.is_set()


def _run_ffmpeg(cmd: list[str], timeout: int,
                abort: threading.Event | None) -> tuple[int, bytes]:
    """跑 ffmpeg 并盯着退出状态，支持随时中断。

    不能用 ``subprocess.run``：它在超时前一直阻塞，网页上点「暂停」要等到
    整个抓帧结束才生效，用户看到的就是“暂停了还在跑”。改成 Popen + 轮询，
    暂停信号一到就 kill 掉子进程。
    """
    creation = 0x08000000 if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=creation,
        )
    except FileNotFoundError as exc:
        raise FrameError("ffmpeg 未安装或不可执行") from exc

    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        try:
            proc.wait(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if _aborted(abort):
                proc.kill()
                proc.wait(timeout=5)
                raise CaptureAborted("任务已暂停") from None
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                proc.wait(timeout=5)
                break

    try:
        _, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        err = b""
    if timed_out:
        return -1, b"timeout"
    return proc.returncode, err or b""


def capture(source: str, seconds: float, output: Path, timeout: int = 60,
            fast_seek: bool = True, width: int = 0,
            min_bytes: int | None = None,
            abort: threading.Event | None = None) -> bool:
    """按秒抓一帧。

    Args:
        fast_seek: True 时把 ``-ss`` 放在 ``-i`` 前（关键帧跳转，网络源上快 10 倍以上）；
            网络源不支持 Range 时跳到错误位置，可置 False 改为解码定位。
        width: 大于 0 时缩放输出宽度（高度按比例），网页缩略图用，能省不少流量。
        min_bytes: 判定“成功”的最小字节数，缩放后的小图门槛要放低。
        abort: 一旦被 set 就立刻杀掉 ffmpeg 并抛 :class:`CaptureAborted`，
            网页上的「暂停 / 取消」靠它才能真正生效。
    """
    ffmpeg = find_binary("ffmpeg")
    seek = ["-ss", f"{max(0.0, seconds):.3f}"]
    tail = ["-frames:v", "1"]
    if width > 0:
        tail += ["-vf", f"scale={int(width)}:-2"]
    tail += ["-q:v", "2", "-y", str(output)]
    prefix = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    # -ss 在 -i 前是快速跳转，在 -i 后是解码定位
    cmd = prefix + seek + ["-i", source] + tail if fast_seek \
        else prefix + ["-i", source] + seek + tail

    # 缩略图本来就小（纯色画面只有几百字节），门槛得跟着宽度放低
    floor = min_bytes if min_bytes is not None else (
        MIN_FRAME_BYTES if width <= 0 else 128)

    code, err = _run_ffmpeg(cmd, timeout, abort)
    if code == -1:
        log.debug("抓帧超时 %ss @%.1fs", timeout, seconds)
        _drop(output)
        return False
    if code != 0:
        log.debug("ffmpeg 退出码 %s @%.1fs：%s", code, seconds,
                  (err or b"").decode("utf-8", "replace").strip()[:300])
    if code != 0 or not _valid(output, floor):
        _drop(output)
        return False
    return True


def capture_at(source: str, seconds: float, output: Path, timeout: int = 60,
               width: int = 0, abort: threading.Event | None = None) -> bool:
    """抓指定时间点的一帧：先试快速跳转，不行再退化解码定位。

    手动选帧走这条路，用户点了哪一秒就要哪一秒，不能像自动选帧那样悄悄
    换到别的位置。
    """
    if capture(source, seconds, output, timeout=timeout, fast_seek=True,
               width=width, abort=abort):
        return True
    if _aborted(abort) or seconds <= 1.0:
        return False
    return capture(source, seconds, output, timeout=timeout, fast_seek=False,
                   width=width, abort=abort)


def capture_best(source: str, duration: float, positions: list[float],
                 workdir: Path, timeout: int = 60,
                 abort: threading.Event | None = None) -> tuple[Path, int]:
    """按片长比例采样若干帧，返回（最清晰的一帧, 成功抓到的帧数）。

    策略：每个位置先试快速跳转，失败再退回解码定位；全部失败时兜底抓开头。
    """
    frames: list[tuple[Path, float]] = []
    for index, ratio in enumerate(positions):
        target = workdir / f"f{index:02d}.jpg"
        seconds = max(0.0, duration * ratio) if duration > 0 else 0.0

        ok = capture(source, seconds, target, timeout=timeout, fast_seek=True,
                     abort=abort)
        if not ok and seconds > 1.0 and not _aborted(abort):
            ok = capture(source, seconds, target, timeout=timeout, fast_seek=False,
                         abort=abort)
        if not ok:
            if _aborted(abort):
                raise CaptureAborted("任务已暂停")
            continue

        score = sharpness(target)
        if score is None:
            _drop(target)
            continue
        frames.append((target, score))

    if not frames:
        # 兜底：有的源只能在开头正常出帧
        fallback = workdir / "f_fallback.jpg"
        if capture(source, 0.0, fallback, timeout=timeout, fast_seek=True, abort=abort):
            score = sharpness(fallback)
            if score is not None:
                frames.append((fallback, score))

    if not frames:
        raise FrameError(
            f"{len(positions)} 个时间点全部抓帧失败：链接可能已失效、源站拒绝 Range 请求，"
            "或视频编码无法解码（看 DEBUG 日志里的 ffmpeg 输出）"
        )

    log.debug("采样 %s 帧，成功 %s 帧", len(positions), len(frames))
    frames.sort(key=lambda item: item[1], reverse=True)
    return frames[0][0], len(frames)


def sharpness(path: Path) -> float | None:
    """用边缘检测后的方差衡量清晰度，越锐利值越大；黑屏返回 None。"""
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            if gray.width < 2 or gray.height < 2:
                return None
            brightness = ImageStat.Stat(gray).mean[0]
            if brightness < MIN_BRIGHTNESS:
                return None
            edges = gray.filter(ImageFilter.FIND_EDGES)
            variance = ImageStat.Stat(edges).var[0]
            # 纯色画面方差接近 0，加亮度和尺寸权重避免选到色块图
            return variance * (1.0 + min(brightness, 128) / 256.0)
    except (OSError, ValueError):
        return None


def make_poster(frame: Path, output: Path, width: int = 1000, height: int = 1500,
                mode: str = "crop", quality: int = 92) -> tuple[int, int]:
    """生成 2:3 竖版海报，返回实际像素尺寸。

    Emby 和 Jellyfin 都要求海报是 2:3，比例不对会被拉伸或裁掉一条，所以这里
    先按 spec 校正一次再画。

    ``mode`` 默认 ``crop``：等比放大后中心裁切，画面铺满、四周不留任何边——
    和 PC 版刮削器出图一致。横版视频会把左右裁掉一部分，这是铺满 2:3 的必然
    代价；不想丢画面可改成 ``blur``（模糊背景补边）或 ``gradient``（渐变补边）。
    """
    width, height, note = specs.conform(width, height, "poster")
    if note:
        log.warning("%s：%s", output.name, note)
    with Image.open(frame) as img:
        img = img.convert("RGB")
        if mode == "blur":
            canvas = _contain_with_blur(img, width, height)
        elif mode == "gradient":
            canvas = _contain_with_gradient(img, width, height)
        else:
            canvas = _cover(img, width, height)
        canvas.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
    return width, height


def make_fanart(frame: Path, output: Path, width: int = 1920, height: int = 1080,
                quality: int = 92) -> tuple[int, int]:
    """生成 16:9 横版背景图（fanart / backdrop），返回实际像素尺寸。"""
    width, height, note = specs.conform(width, height, "fanart")
    if note:
        log.warning("%s：%s", output.name, note)
    with Image.open(frame) as img:
        canvas = _cover(img.convert("RGB"), width, height)
        canvas.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
    return width, height


def make_thumb(frame: Path, output: Path, width: int = 1000, height: int = 562,
               quality: int = 92) -> tuple[int, int]:
    """生成 16:9 缩略图（``<名称>-thumb.jpg``），Emby 列表视图会用。"""
    width, height, note = specs.conform(width, height, "thumb")
    if note:
        log.warning("%s：%s", output.name, note)
    with Image.open(frame) as img:
        canvas = _cover(img.convert("RGB"), width, height)
        canvas.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
    return width, height


def _cover(img: Image.Image, width: int, height: int, top_bias: float = 0.28) -> Image.Image:
    """等比缩放后裁切，铺满目标尺寸。

    横向居中裁切（横版视频切 2:3 时左右各让一点，主体通常还在中间）；纵向裁切
    时往上偏一点（``top_bias`` = 0.28，即把多余的 28% 留在上方切掉），这样竖版
    视频转 2:3 不会把脑袋切掉——死居中经常正好切在额头上。
    """
    scale = max(width / img.width, height / img.height)
    resized = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = round(max(0, resized.height - height) * min(1.0, max(0.0, top_bias)))
    return resized.crop((left, top, left + width, top + height))


def _contain_with_blur(img: Image.Image, width: int, height: int) -> Image.Image:
    """背景取原图虚化铺满，前景等比缩放居中放置。

    16:9 的帧放进 2:3 的框里，上下各有一条留白。这张背景必须「沉下去」才不
    会让人以为是画面重复了一遍——所以模糊半径给得很大（宽度 1/10），再压暗到
    四成亮度、加暗角。轻虚化会留下能认出轮廓的残影，看起来像画切断了。
    """
    radius = max(10, round(width / 10))
    background = _cover(img, width, height).filter(ImageFilter.GaussianBlur(radius=radius))
    background = ImageEnhance.Brightness(background).enhance(0.42)
    background = _vignette(background, 0.45)

    foreground, offset = _fit_inside(img, width, height)
    canvas = background.copy()
    canvas.paste(foreground, offset)
    return canvas


def _contain_with_gradient(img: Image.Image, width: int, height: int) -> Image.Image:
    """背景用画面上下边缘的平均色做竖直渐变，最干净，没有任何残影。"""
    strip = max(1, img.height // 8)
    top = ImageStat.Stat(img.crop((0, 0, img.width, strip))).mean
    bottom = ImageStat.Stat(img.crop((0, img.height - strip, img.width, img.height))).mean

    canvas = Image.new("RGB", (width, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        canvas.paste(color, (0, y, width, y + 1))
    canvas = ImageEnhance.Brightness(canvas).enhance(0.5)

    foreground, offset = _fit_inside(img, width, height)
    canvas.paste(foreground, offset)
    return canvas


def _fit_inside(img: Image.Image, width: int, height: int) -> tuple[Image.Image, tuple[int, int]]:
    """等比缩放到能完整放进目标框，返回（图, 居中偏移）。"""
    scale = min(width / img.width, height / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS), ((width - size[0]) // 2, (height - size[1]) // 2)


def _vignette(canvas: Image.Image, strength: float = 0.3) -> Image.Image:
    """四周压暗，中心不动。strength 是边缘最多压暗的比例。"""
    mask = Image.radial_gradient("L").resize(canvas.size, Image.BILINEAR)
    levels = mask.point(lambda value: int(value * strength))
    dark = Image.new("RGB", canvas.size, (0, 0, 0))
    return Image.composite(dark, canvas, levels)


def _valid(path: Path, min_bytes: int = MIN_FRAME_BYTES) -> bool:
    """判断这张图是不是真的写完了。

    光看大小不可靠：纯色画面或小尺寸缩略图本来就只有一两 KB，会被误判成
    失败。所以再看一眼 JPEG 的头尾标记——ffmpeg 正常收尾才会写 EOI。
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size < min_bytes:
            return False
        with path.open("rb") as handle:
            head = handle.read(2)
            handle.seek(-2, os.SEEK_END)
            tail = handle.read(2)
        return head == b"\xff\xd8" and tail == b"\xff\xd9"
    except OSError:
        return False


def _drop(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
