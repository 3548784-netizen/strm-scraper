# -*- coding: utf-8 -*-
"""视频探测：把 strm 解析成真实地址，再用 ffprobe 拿媒体信息。"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import throttle

log = logging.getLogger(__name__)

# ffprobe 对网络流默认会读很多数据才出结果，收紧一点能快不少
PROBE_SIZE = "5M"
ANALYZE_DURATION = "5M"


class ProbeError(RuntimeError):
    """探测失败，消息里带可读原因。"""


@dataclass
class StreamInfo:
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    channels: int = 0
    sampling_rate: int = 0
    language: str = "und"


@dataclass
class MediaInfo:
    source: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    container: str = ""
    video: StreamInfo | None = None
    audio: list[StreamInfo] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.video.width if self.video else 0

    @property
    def height(self) -> int:
        return self.video.height if self.video else 0


def find_binary(name: str) -> str:
    """找 ffmpeg/ffprobe：环境变量 → PATH → 常见目录。"""
    env_key = f"WS_{name.upper()}_PATH"
    explicit = os.environ.get(env_key)
    if explicit and Path(explicit).is_file():
        return explicit
    exe = f"{name}.exe" if os.name == "nt" else name
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / exe
        if candidate.is_file():
            return str(candidate)
    for base in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"):
        candidate = Path(base) / name
        if candidate.is_file():
            return str(candidate)
    raise ProbeError(
        f"找不到 {name}，请确认容器内已安装 ffmpeg，或用 WS_{name.upper()}_PATH 指定路径"
    )


def resolve_source(path: str | Path) -> str:
    """strm 文件返回其中记录的 URL，其它类型原样返回。

    strm 是纯文本，第一行有效内容就是播放地址。有的工具会写成多行或带 BOM，
    这里取第一条以 http/https/rtsp 开头的行。
    """
    path = Path(path)
    if path.suffix.lower() != ".strm":
        return str(path)

    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError as exc:
            raise ProbeError(f"读取 strm 失败：{exc}") from exc
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^(https?|rtsp|ftp)://", line, re.I):
                return line
        # 编码对了但没找到链接，不用再试别的编码
        raise ProbeError("strm 文件里没有可用的播放地址")

    raise ProbeError("strm 文件编码无法识别")


def _classify_error(stderr: str, url: str) -> str:
    """把 ffprobe 的报错翻译成人能看懂的原因。"""
    text = stderr.lower()
    host = ""
    match = re.match(r"^[a-z]+://([^/:]+)", url, re.I)
    if match:
        host = match.group(1)

    if "connection refused" in text:
        return f"连接被拒绝：{host} 上没有服务在监听，检查地址或内网穿透"
    if "429" in text or "too many requests" in text:
        return f"地址返回 429：{host} 认为请求过于频繁，已自动退避"
    if "503" in text or "service unavailable" in text:
        return f"地址返回 503：{host} 可能正在限流，已自动退避"
    if "504" in text or "gateway time" in text:
        return f"地址返回 504：{host} 网关超时，通常是并发太高被限流，已自动退避"
    if "timed out" in text or "timeout" in text:
        return f"连接超时：{host} 响应太慢或不可达"
    if "name or service not known" in text or "getaddrinfo" in text:
        return f"域名解析失败：{host}"
    if "404" in text or "not found" in text:
        return "地址返回 404，文件可能已被删除或移动"
    if "403" in text or "forbidden" in text:
        return "地址返回 403，可能需要鉴权或链接已过期"
    if "401" in text or "unauthorized" in text:
        return "地址要求鉴权，检查网盘或中转服务的登录状态"
    if "invalid data" in text or "moov atom" in text:
        return "文件结构异常，可能下载不完整或不是有效视频"
    if "protocol not found" in text:
        return "不支持的协议，检查 strm 里的地址格式"
    if "server returned 5" in text or "5xx" in text:
        return f"服务端 5xx：{host} 多半是并发被限流，已自动退避后重试"
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else "未知错误"
    return f"探测失败：{tail[:160]}"


def probe(source: str, timeout: int = 20) -> MediaInfo:
    """对本地文件或网络地址做一次探测。"""
    ffprobe = find_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-probesize", PROBE_SIZE,
        "-analyzeduration", ANALYZE_DURATION,
        # ffprobe 的网络读写超时单位是微秒
        "-rw_timeout", str(int(timeout * 1_000_000)),
        source,
    ]
    creation = 0
    if os.name == "nt":  # 不弹黑框
        creation = 0x08000000
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout + 10,
            creationflags=creation,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"探测超时（{timeout} 秒），网络地址可能已失效") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe 未安装或不可执行") from exc

    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        # 被限流就先记一笔，让调度线程歇一会儿（这里不阻塞，网页请求照常返回）
        throttle.GUARD.report(stderr)
        raise ProbeError(_classify_error(stderr, source))

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe 输出无法解析") from exc

    fmt = data.get("format") or {}
    info = MediaInfo(source=source)
    info.container = (fmt.get("format_name") or "").split(",")[0]
    info.duration = _to_float(fmt.get("duration"))
    info.size = _to_int(fmt.get("size"))
    info.bitrate = _to_int(fmt.get("bit_rate"))

    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and _is_real_video(stream):
            if info.video is None:
                info.video = StreamInfo(
                    codec=stream.get("codec_name") or "",
                    width=_to_int(stream.get("width")),
                    height=_to_int(stream.get("height")),
                    fps=_parse_fps(stream),
                    bitrate=_to_int(stream.get("bit_rate")),
                    language=(stream.get("tags") or {}).get("language") or "und",
                )
        elif kind == "audio":
            info.audio.append(
                StreamInfo(
                    codec=stream.get("codec_name") or "",
                    bitrate=_to_int(stream.get("bit_rate")),
                    channels=_to_int(stream.get("channels")),
                    sampling_rate=_to_int(stream.get("sample_rate")),
                    language=(stream.get("tags") or {}).get("language") or "und",
                )
            )

    if info.video is None:
        raise ProbeError("这个地址里没有视频轨道，可能不是视频文件")
    if info.size <= 0:
        # 部分直链不给 format.size，用码率乘时长估算
        if info.bitrate > 0 and info.duration > 0:
            info.size = int(info.bitrate / 8 * info.duration)
    return info


def _is_real_video(stream: dict) -> bool:
    """过滤掉 mp3 封面、mjpeg 缩略图这类伪视频轨道。"""
    if (stream.get("disposition") or {}).get("attached_pic"):
        return False
    return _to_int(stream.get("width")) > 0 and _to_int(stream.get("height")) > 0


def _parse_fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                num_f, den_f = float(num), float(den)
                if den_f:
                    return round(num_f / den_f, 3)
            except ValueError:
                continue
        elif raw:
            try:
                return round(float(raw), 3)
            except ValueError:
                continue
    return 0.0


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_strm_url(path: str | Path) -> str:
    """只取 strm 里的地址，读不到就抛错。"""
    return resolve_source(path)
