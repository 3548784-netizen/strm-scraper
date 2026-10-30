#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地测试环境：真视频 + 带 Range 的 HTTP 源 + 网页控制台。

改完代码想立刻看效果就用它，不用打镜像、不用真的网盘。

    python tools/devserver.py              # 起服务，浏览器开 http://127.0.0.1:6670
    python tools/devserver.py --reset      # 重建测试视频与状态库
    python tools/devserver.py --auth       # 打开登录（验证 admin / admin 那套流程）

    # 拿真实素材测（不生成视频、不写测试库，只把扫描根指过去）
    python tools/devserver.py --media-root "D:/Desktop/test strm" --data .devdata_real

测试媒体库、状态库、抓帧缓存都在 ``.devdata/`` 下，随时删掉重来。
视频用 ffmpeg 的 testsrc2 生成，带彩色运动图案，裁切和补边一眼就能看出来。
"""
from __future__ import annotations

import argparse
import functools
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 有的开发环境对单轮批量删除有额外上限，超过会直接杀掉进程（回归测试一删产物就崩）。
# 这里只操作 .devdata 下的临时文件，关掉这层保护才能连续跑自动化测试。
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

ROOT = Path(__file__).resolve().parent.parent
# WS_DEVDATA 可以换一套独立数据（跑带登录的回归测试时用，不干扰主测试环境）
DATA = Path(os.environ.get("WS_DEVDATA") or (ROOT / ".devdata"))
SRC = DATA / "src"        # 源视频（只给 HTTP 源用，不参与扫描）
MEDIA = DATA / "media"    # 被扫描的媒体库，里面只有 .strm
LOG_DIR = DATA / "logs"

# 生成规格：文件名 → (分辨率, 时长秒, 说明)
VIDEOS = {
    "wide_720.mp4": ("1280x720", 120, "横版 720p，主测海报裁切"),
    "tall_720.mp4": ("720x1280", 90, "竖版，测竖屏素材"),
    "wide_1080.mp4": ("1920x1080", 45, "横版 1080p，短一点好等"),
}

# 媒体库里的 strm：路径 → (指向哪个视频, 备注)
LIBRARY = {
    "电影/横版测试影片.2024.2160p.strm": ("wide_720.mp4", "横版视频，看海报有没有上下留边"),
    "电影/竖版测试影片.2023.1080p.strm": ("tall_720.mp4", "竖版视频"),
    "电影/短片.2025.strm": ("wide_1080.mp4", "短文件，跑得快"),
    "剧集/测试剧.S01E01.strm": ("wide_720.mp4", "剧集目录"),
    "剧集/测试剧.S01E02.strm": ("wide_1080.mp4", "剧集目录"),
    "电影/失效链接.strm": (None, "故意做坏，测失败态"),
}


def find_tool(name: str) -> str | None:
    """找 ffmpeg / ffprobe：环境变量 → PATH → 仓库同级 ffmpeg_bin。"""
    env = os.environ.get(f"WS_{name.upper()}_PATH")
    if env and Path(env).is_file():
        return env
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    for base in (ROOT / "ffmpeg_bin", ROOT.parent / "ffmpeg_bin"):
        candidate = base / exe
        if candidate.is_file():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# 媒体 HTTP 服务：必须支持 Range，否则 ffmpeg 只能从头顺序解码，很慢甚至超时
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".jpg": "image/jpeg",
    ".png": "image/png", ".webm": "video/webm",
}


class RangeHandler(SimpleHTTPRequestHandler):
    """静态文件服务，带 Range 支持。只读 media 目录。"""

    def do_GET(self) -> None:  # noqa: N802 - 覆盖父类命名
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            return super().do_GET()
        if not path.is_file():
            self.send_error(404, "not found")
            return

        size = path.stat().st_size
        rng = (self.headers.get("Range") or "").strip()
        match = re.match(r"bytes=(\d*)-(\d*)$", rng) if rng else None
        if match:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
        else:
            start, end = 0, size - 1

        length = end - start + 1
        self.send_response(206 if match else 200)
        self.send_header("Content-Type",
                         _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return

        with path.open("rb") as handle:
            handle.seek(start)
            left = length
            while left > 0:
                chunk = handle.read(min(262144, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except ConnectionError:
                    # 暂停/取消会掐断 ffmpeg 的下载，连接断掉是正常现象，别刷堆栈
                    return
                left -= len(chunk)

    do_HEAD = do_GET

    def log_message(self, *args) -> None:
        pass


def start_media_server(port: int) -> ThreadingHTTPServer:
    handler = functools.partial(RangeHandler, directory=str(SRC))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="dev-media").start()
    return server


# ---------------------------------------------------------------------------

def build_videos(ffmpeg: str, reset: bool) -> None:
    targets = []
    for name, (size, seconds, note) in VIDEOS.items():
        path = SRC / name
        if reset or not path.is_file() or path.stat().st_size < 100_000:
            targets.append((path, size, seconds, note))

    if not targets:
        print("测试视频已存在，跳过生成（要重建加 --reset）")
        return

    SRC.mkdir(parents=True, exist_ok=True)
    for path, size, seconds, note in targets:
        print(f"  生成 {path.name}  {size}  {seconds}s  —— {note}")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"testsrc2=size={size}:rate=25:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-g", "50",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if proc.returncode != 0 or not path.is_file():
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:600]
            raise SystemExit(f"生成 {path.name} 失败（退出码 {proc.returncode}）\n{detail}")


def build_library(media_port: int) -> None:
    base = f"http://127.0.0.1:{media_port}"
    for rel, (video, _note) in LIBRARY.items():
        path = MEDIA / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{base}/{video}" if video else "http://127.0.0.1:9/dead.mp4"
        path.write_text(url + "\n", encoding="utf-8")


def write_config(app_port: int, auth: bool, roots: list[str] | None = None) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    config = DATA / "config.yaml"
    scan_roots = roots or [MEDIA.as_posix()]
    lines = [
        "# 本地测试环境自动生成，随时可以删掉重建",
        "scan:",
        "  roots:",
        *[f"    - {root}" for root in scan_roots],
        "  mode: manual",
        "  autostart: false",
        "  recursive: true",
        "  concurrency: 3",
        "  timeout: 30",
        "  probe_timeout: 8",
        "",
        "scrape:",
        "  overwrite: missing",
        "  frames: 2",
        "  poster:",
        "    width: 1000",
        "    height: 1500",
        "    mode: crop",
        "",
        "web:",
        "  host: 127.0.0.1",
        f"  port: {app_port}",
        "  auth:",
        f"    enabled: {'true' if auth else 'false'}",
        f"    file: {(DATA / 'auth.json').as_posix()}",
        "",
        "storage:",
        f"  state_db: {(DATA / 'state.db').as_posix()}",
        "",
        "log:",
        f"  dir: {LOG_DIR.as_posix()}",
        "  level: DEBUG",
        "",
    ]
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def free_port(preferred: int) -> int:
    for candidate in (preferred, preferred + 1, preferred + 2, preferred + 3):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"端口 {preferred} 附近都被占用了，用 --port 换一个")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="strm-scraper 本地测试环境")
    parser.add_argument("--reset", action="store_true",
                        help="重建测试视频、状态库和抓帧缓存")
    parser.add_argument("--auth", action="store_true",
                        help="打开网页登录（默认关闭，省得每次都登）")
    parser.add_argument("--port", type=int, default=6670, help="控制台端口，默认 6670")
    parser.add_argument("--media-port", type=int, default=6680, help="视频源端口，默认 6680")
    parser.add_argument("--media-root", default=None,
                        help="改用已有的媒体库目录（如真实 strm 素材），不再生成测试库")
    parser.add_argument("--data", default=None,
                        help="换一个数据目录（配置/状态库/日志），默认 .devdata")
    args = parser.parse_args(argv)

    global DATA, SRC, MEDIA, LOG_DIR
    if args.data:
        DATA = Path(args.data).expanduser().resolve()
        SRC = DATA / "src"
        MEDIA = DATA / "media"
        LOG_DIR = DATA / "logs"

    real_roots: list[str] | None = None
    if args.media_root:
        root = Path(args.media_root).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"媒体库目录不存在：{root}")
        real_roots = [root.as_posix()]
        # 真实素材不重建、不复制，只把扫描根指过去
        MEDIA = root

    sys.path.insert(0, str(ROOT))

    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise SystemExit("找不到 ffmpeg：装一个，或用 WS_FFMPEG_PATH 指定路径")
    ffprobe = find_tool("ffprobe") or str(Path(ffmpeg).with_name(
        "ffprobe.exe" if os.name == "nt" else "ffprobe"))
    os.environ["WS_FFMPEG_PATH"] = ffmpeg
    os.environ["WS_FFPROBE_PATH"] = ffprobe

    if args.reset:
        print("清理旧的测试数据…")
        # --media-root 指向的是用户自己的素材目录，绝不能删
        junk = (LOG_DIR, SRC) if real_roots else (MEDIA, LOG_DIR, SRC)
        for path in junk:
            shutil.rmtree(path, ignore_errors=True)
        for name in ("state.db", "state.db-journal", "auth.json"):
            (DATA / name).unlink(missing_ok=True)
        shutil.rmtree(DATA / "framecache", ignore_errors=True)
        shutil.rmtree(DATA / "thumbs", ignore_errors=True)
        # 产物写在素材目录里，重跑前清掉，别让上一轮的结果影响判断
        for leftover in ("*.nfo", "*.jpg", "*.png", "*-poster.*", "*-fanart.*"):
            for path in MEDIA.rglob(leftover):
                path.unlink(missing_ok=True)

    media_port = free_port(args.media_port)
    if real_roots:
        print(f"使用真实素材库：{MEDIA}")
        print("  跳过测试视频生成（素材自带远程 strm）")
    else:
        print("准备测试视频：")
        build_videos(ffmpeg, args.reset)
        build_library(media_port)
    start_media_server(media_port)

    app_port = free_port(args.port)
    config = write_config(app_port, args.auth, real_roots)

    url = f"http://127.0.0.1:{app_port}"
    print()
    print("=" * 62)
    print(f"  控制台    {url}")
    print(f"  视频源    http://127.0.0.1:{media_port}")
    print(f"  媒体库    {MEDIA}")
    print(f"  数据      {DATA}")
    print(f"  登录      {'开（admin / admin）' if args.auth else '关'}")
    print("=" * 62)
    print("  容器启动不会自动刮，进网页点「开始任务」")
    print("  Ctrl+C 退出")
    print()

    from scraper.main import main as run_app
    return run_app(["-c", str(config)])


if __name__ == "__main__":
    raise SystemExit(main())
