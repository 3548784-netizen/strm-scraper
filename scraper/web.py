# -*- coding: utf-8 -*-
"""网页控制台：登录鉴权 + 进度看板 + 失败排查 + 媒体服务器联动 + 日志查看。"""
from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import (COOKIE_NAME, AuthStore, LoginThrottle, validate_password,
                   validate_username)
from . import frames, probe
from .config import DEFAULT_CONFIG_PATH, Config, ServerConfig
from .logs import LogFiles, RingLogHandler, tail_file
from .media import (IGNORED_NAMES, artifacts, build_target, product_state, signature)
from .mediaserver import ServerPool
from .nfo import display_title
from .picker import FramePicker
from .scheduler import Scheduler, TaskNotFound
from .state import StateStore, dir_label
from .thumbs import ThumbCache
from .worker import Runner, ScrapeResult, process

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 页面本体每次都回源校验：升级镜像后浏览器别拿旧前端（新按钮点不到、样式对不上）
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

# 缩略图/原图的缓存策略：URL 里带着图片的字节数（重新出图后基本都会变），
# 所以可以放心长时间缓存——翻页、列表重渲染都不会再打网络。留一天是为了
# 万一重出图后字节数恰好没变，也能自己恢复，不用等用户手动强制刷新。
IMAGE_CACHE = {"Cache-Control": "private, max-age=86400"}

# 不需要登录就能访问的路径
PUBLIC_PATHS = {"/login", "/api/login", "/api/auth-info", "/api/health", "/favicon.ico"}

_KIND_NAMES = {"poster": "海报", "fanart": "背景图", "thumb": "缩略图"}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _size_of(value: int | None) -> str:
    """字节数转成日志里好读的短写法。"""
    if not value:
        return "未生成"
    if value >= 1024 * 1024:
        return f"{value / 1048576:.1f}MB"
    return f"{value / 1024:.0f}KB"


# 不需要往下走的目录：隐藏目录、群晖缩略图、Node 依赖、回收站
SKIP_DIR_NAMES = {"@eaDir", "__pycache__", "node_modules", "$RECYCLE.BIN", "#recycle"}


def _skip_dir_name(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIR_NAMES


# 目录汇总单次最多数这么多 strm：媒体库动辄上万文件，不能让列表请求被拖住
DIR_SUMMARY_LIMIT = 5000

# Emby / Jellyfin 认的「文件夹封面」文件名。
# 按视频名生成的 xxx-poster.jpg 只对「影片条目」有效；一旦这个目录本身变成了
# 文件夹条目（中间层、或这一层没有直接放视频），就只能靠这一组固定名字。
FOLDER_ART_NAMES = ("folder.jpg", "poster.jpg", "cover.jpg")
FOLDER_ART_SET = frozenset(FOLDER_ART_NAMES)

# 补封面时实际写哪几个名字：folder.jpg 是两家客户端的首选，poster.jpg 作为
# 并列的通用名一起写，兼容那些只认 poster 的库配置与皮肤；两份内容完全相同，
# 各自独立，删掉其中一个不影响另一个。
FOLDER_ART_WRITE = ("folder.jpg", "poster.jpg")

# 候选封面只认这几种图片后缀：网页能直接显示，Emby / Jellyfin 也都吃得下
ART_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _dir_summary(directory: Path, wanted: set[str]) -> dict[str, int]:
    """数一遍目录（含子目录）里的 strm，顺便算出刮过几个、有没有文件夹封面。

    为什么要连子目录一起数：文件夹行的勾选框是递归选中的（点一下把这个
    文件夹里所有 strm 都选上），状态如果只数这一层，就会出现「勾得上 40 个、
    状态写着 0 个」的矛盾。

    「刮过」的口径和列表里的状态列一致：海报 / 背景图 / NFO 任一存在即算。
    产物和 strm 同目录同名前缀，所以先 scandir 拿到文件名集合、再用集合判断，
    不用给每个文件单独 stat 四次——几千个文件时差别很明显。

    ``poster`` 单独数「有 ``xxx-poster.jpg`` 的」，和 ``scraped`` 分开：
    有 NFO 或背景图但没海报的文件也算没刮完，文件管理的「海报未生成」就用它。

    额外返回 ``own``（直接放在这一层几个）和 ``art``（这一层有没有 folder.jpg），
    两者都是后面判断「这个文件夹在媒体库里会不会没封面」的依据。
    """
    total = 0
    scraped = 0
    poster = 0
    own = 0
    art = 0
    stack = [directory]
    while stack and total < DIR_SUMMARY_LIMIT:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                names: set[str] = set()
                subs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir():
                            if not _skip_dir_name(entry.name):
                                subs.append(Path(entry.path))
                        else:
                            names.add(entry.name.lower())
                    except OSError:
                        continue
        except OSError:
            continue
        stack.extend(subs)

        here = 0
        for name in names:
            if name in IGNORED_NAMES or Path(name).suffix.lower() not in wanted:
                continue
            here += 1
            total += 1
            stem = name.rsplit(".", 1)[0]
            if f"{stem}-poster.jpg" in names:
                poster += 1
            if (f"{stem}.nfo" in names or f"{stem}-poster.jpg" in names
                    or f"{stem}-fanart.jpg" in names):
                scraped += 1
            if total >= DIR_SUMMARY_LIMIT:
                break
        if current == directory:
            own = here
            art = 1 if names & FOLDER_ART_SET else 0
    return {"count": total, "scraped": scraped, "poster": poster,
            "own": own, "art": art}


def _has_folder_art(directory: Path) -> bool:
    """这一层有没有 Emby 认的文件夹封面（folder.jpg / poster.jpg / cover.jpg）。"""
    try:
        with os.scandir(directory) as entries:
            return any(entry.name.lower() in FOLDER_ART_SET for entry in entries)
    except OSError:
        return False


def _tree_stats(base: Path, wanted: set[str], limit: int = 4000) -> dict[Path, dict]:
    """一次后序遍历，算出整棵子树里每个目录的 strm 分布。

    返回 ``{目录: {"own": 直接几个, "total": 含子目录几个, "art": 有没有封面}}``。
    一次扫描就能拿到全树数据，避免逐个目录各扫一遍变成 O(n²)。
    """
    out: dict[Path, dict] = {}
    budget = [limit]

    def walk(current: Path, depth: int) -> int:
        if budget[0] <= 0 or depth > 12:
            return 0
        try:
            with os.scandir(current) as entries:
                names: set[str] = set()
                subs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir():
                            if not _skip_dir_name(entry.name):
                                subs.append(Path(entry.path))
                        else:
                            names.add(entry.name.lower())
                    except OSError:
                        continue
        except OSError:
            return 0
        budget[0] -= 1
        own = sum(1 for name in names
                  if name not in IGNORED_NAMES
                  and Path(name).suffix.lower() in wanted)
        total = own
        for sub in subs:
            total += walk(sub, depth + 1)
        out[current] = {"own": own, "total": total,
                        "art": 1 if names & FOLDER_ART_SET else 0}
        return total

    walk(base, 0)
    return out


def _first_poster(directory: Path, max_dirs: int = 60) -> Path | None:
    """这个层（含子目录）里第一张能拿来当封面的海报。

    优先用刮削器自己生成的 ``xxx-poster.jpg``；实在没有再退而求其次，
    拿子目录里已有的 folder.jpg / poster.jpg。
    """
    queue = [directory]
    seen = 0
    fallback: Path | None = None
    while queue and seen < max_dirs:
        current = queue.pop(0)
        seen += 1
        try:
            with os.scandir(current) as entries:
                names: list[str] = []
                subs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir():
                            if not _skip_dir_name(entry.name):
                                subs.append(Path(entry.path))
                        else:
                            names.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
        for name in sorted(names):
            low = name.lower()
            if low.endswith("-poster.jpg"):
                return current / name
            if fallback is None and low in FOLDER_ART_SET:
                fallback = current / name
        queue.extend(subs)
    return fallback


def _norm_path(target: Path | str) -> str:
    """目录 key 的规范写法：去尾部斜杠、Windows 下统一大小写。

    「指定封面」这个关系要存库、列表又要拿它做比对，两边必须走同一个函数，
    否则 Windows 上盘符大小写不一样就永远对不上，标签会显示成「没指定」。
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(str(target))))


def _poster_candidates(directory: Path, limit: int = 120,
                       max_dirs: int = 200) -> list[dict]:
    """列出这一层（含子目录）里能当封面的图，给人挑「到底用哪张」。

    只收两类：刮削器生成的 ``xxx-poster.jpg``，和文件夹条目认的
    ``folder.jpg / poster.jpg / cover.jpg``。``-fanart.jpg`` / ``-thumb.jpg``
    是横图，混进来只会让列表更难分辨。

    排序：正片海报优先于 folder.jpg —— 前者才是「这部片子」的封面，后者只是
    上一轮补进去的副本，拿副本当封面等于把上一轮的选择再抄一遍；同一类里
    离这一层越近越靠前。

    这一层自己的 folder.jpg / poster.jpg 不算候选：那是当前的封面本身，
    选它等于什么都没改，列出来只会挤占第一屏。
    """
    found: list[tuple[int, int, str, Path, int]] = []
    queue: list[tuple[Path, int]] = [(directory, 0)]
    seen = 0
    while queue and seen < max_dirs and len(found) < limit * 4:
        current, depth = queue.pop(0)
        seen += 1
        try:
            with os.scandir(current) as entries:
                names: list[str] = []
                for entry in entries:
                    try:
                        if entry.is_dir():
                            if not _skip_dir_name(entry.name):
                                queue.append((Path(entry.path), depth + 1))
                        elif entry.name.lower().endswith(ART_IMAGE_EXTS):
                            names.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
        for name in sorted(names):
            low = name.lower()
            if low.endswith("-poster.jpg"):
                rank = 0
            elif low in FOLDER_ART_SET:
                if depth == 0:
                    continue
                rank = 1
            else:
                continue
            try:
                size = (current / name).stat().st_size
            except OSError:
                size = 0
            found.append((depth, rank, low, current / name, size))
    found.sort(key=lambda row: (row[1], row[0], row[2]))
    items = []
    for depth, rank, _, path, size in found[:limit]:
        try:
            rel = str(path.parent.relative_to(directory))
        except ValueError:
            rel = ""
        items.append({"path": str(path), "name": path.name, "folder": str(path.parent),
                      "rel": str(Path(rel) / path.name) if rel else path.name,
                      "depth": depth, "kind": "poster" if rank == 0 else "folder",
                      "size": size})
    return items


def _current_art_source(folder: Path, items: list[dict]) -> str:
    """这一层现在实际在用的封面，来自哪一张候选。

    自动挑选时人看不到「到底挑了哪张」，弹窗里全是缩略图，选之前无法判断
    现状。写入是原样复制（MD5 与源一致），所以拿本层 folder.jpg 的字节去和
    候选比对就能反推出源文件；按大小先筛一遍，通常一次就命中。
    """
    for name in FOLDER_ART_WRITE:
        target = folder / name
        if not target.is_file():
            continue
        try:
            raw = target.read_bytes()
        except OSError:
            continue
        for it in items:
            if it.get("size") != len(raw):
                continue
            try:
                if Path(it["path"]).read_bytes() == raw:
                    return it["path"]
            except OSError:
                continue
    return ""


def _write_folder_art(folder: Path, force: bool,
                      source: Path | None = None) -> dict:
    """给这一层写 folder.jpg + poster.jpg。

    ``source`` 为空就退回 ``_first_poster`` 自动挑；指定了就用指定的那张。
    指定后每次重补都还用它，这是「父层指定用哪张」能长期生效的原因。

    返回 ``written``（写了几个文件）、``skipped``（已经齐了）、
    ``no_source``（找不到可用源图）、``failed``（写入报错）。
    """
    have = {name for name in FOLDER_ART_WRITE if (folder / name).is_file()}
    todo = [name for name in FOLDER_ART_WRITE if force or name not in have]
    if not todo:
        return {"written": 0, "skipped": True, "no_source": False, "failed": False}
    src = source
    if src is None or not src.is_file():
        src = _first_poster(folder)
    if src is None:
        return {"written": 0, "skipped": False, "no_source": True, "failed": False}
    written = 0
    for name in todo:
        dest = folder / name
        try:
            # force 重建时源可能就是这层已有的 folder.jpg，别复制到自己
            if dest.exists() and dest.resolve() == src.resolve():
                written += 1
                continue
            shutil.copy2(src, dest)
            written += 1
            log.info("补文件夹封面：%s ← %s", dest, src.name)
        except OSError as exc:
            log.warning("补文件夹封面失败 %s：%s", dest, exc)
    return {"written": written, "skipped": False, "no_source": False,
            "failed": written == 0, "source": src}


def _chain_to_root(target: Path, roots: list[Path]) -> list[Path]:
    """target 自己 + 它到扫描根之间的每一层（不含根，从下往上）。

    Emby 在「媒体库根 → 文件夹1 → 文件夹2 → 视频」这种结构里，会把中间那些
    没有直接放视频的层当成文件夹条目。它们全都要有 folder.jpg 才能显示封面，
    所以修复范围必须一路补到根的下面这一层。
    """
    for root in roots:
        try:
            rel = target.relative_to(root)
        except ValueError:
            continue
        chain: list[Path] = []
        cursor = target
        for _ in rel.parts:
            chain.append(cursor)
            cursor = cursor.parent
        return chain
    return [target]


def _as_int(value, low: int, high: int, label: str) -> int:
    """设置页的数字字段：转不了或超范围就明确告诉用户，别默默截断。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{label}要填整数") from None
    if not low <= number <= high:
        raise HTTPException(400, f"{label}需在 {low}–{high} 之间")
    return number


def _server_public(server: ServerConfig) -> dict:
    """给前端的服务器配置：密钥不回传明文，只告诉有没有配过。"""
    return {
        "name": server.name,
        "type": server.type,
        "enabled": server.enabled,
        "url": server.url,
        "api_key": "",
        "has_key": bool(server.api_key),
        "timeout": server.timeout,
        "path_map": dict(server.path_map or {}),
        "refresh_after": server.refresh_after,
        "verify_after": server.verify_after,
        "scan_after": server.scan_after,
        "insecure": server.insecure,
    }


def _when_text(ts) -> str:
    """时间戳 → 「10-29 18:40（3 小时 12 分后）」，给自动化卡片用。"""
    if not ts:
        return "—"
    try:
        moment = time.localtime(float(ts))
    except (TypeError, ValueError):
        return "—"
    stamp = time.strftime("%m-%d %H:%M", moment)
    delta = float(ts) - time.time()
    if delta <= -60:
        return f"{stamp}（{_human_delta(-delta)}前）"
    if delta <= 0:
        return f"{stamp}（马上）"
    return f"{stamp}（{_human_delta(delta)}后）"


def _human_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    return f"{seconds // 86400} 天 {seconds % 86400 // 3600} 小时"


def create_app(cfg: Config, store: StateStore, runner: Runner,
               log_buffer: RingLogHandler, log_files: LogFiles | None = None,
               pool: ServerPool | None = None,
               auth: AuthStore | None = None,
               scheduler: Scheduler | None = None) -> FastAPI:
    app = FastAPI(title="strm-scraper", docs_url="/api/docs", redoc_url=None)
    pool = pool or ServerPool(cfg)
    log_files = log_files or LogFiles()
    auth = auth or AuthStore(cfg.web.auth.file, cfg.web.auth.session_hours)
    throttle = LoginThrottle()
    # 选帧缓存放在 state.db 旁边，跟着 /config 一起持久化
    picker = FramePicker(cfg, Path(cfg.storage.state_db).parent / "framecache")
    # 列表缩略图：原图太大，按需压成几十像素宽的小图缓存起来
    thumbs = ThumbCache(Path(cfg.storage.state_db).parent / "thumbs")
    manual_refresh: dict = {"running": False, "done": 0, "total": 0, "message": ""}
    # 定时自动化：main.py 里已经建好并启动了；单独调 create_app 时兜一个
    sched = scheduler or Scheduler(cfg, runner, store)
    sched.start()
    # 单独执行的条目：文件管理里点某个文件「刮削」，不等批量队列，马上跑
    single_lock = threading.Lock()
    single_active: dict[str, float] = {}

    def _set_session(response, username: str):
        response.set_cookie(
            COOKIE_NAME, auth.issue(username),
            max_age=auth.session_hours * 3600,
            httponly=True, samesite="lax", path="/",
        )
        return response

    # ---------- 登录守卫 ----------

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if not cfg.web.auth.enabled:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        user = auth.read(request.cookies.get(COOKIE_NAME))
        if user:
            request.state.user = user
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "message": "登录已过期，请重新登录", "need_login": True},
                status_code=401,
            )
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={urllib.parse.quote(target)}", status_code=303)

    # ---------- 登录页与账号 ----------

    @app.get("/login", response_class=FileResponse)
    def login_page():
        if not cfg.web.auth.enabled:
            return RedirectResponse("/", status_code=303)
        page = STATIC_DIR / "login.html"
        if not page.is_file():
            raise HTTPException(500, "登录页文件缺失")
        return FileResponse(page, media_type="text/html; charset=utf-8",
                            headers=NO_CACHE)

    @app.get("/api/auth-info")
    def auth_info(request: Request):
        """登录页用：是否需要登录、是不是还没改默认密码。"""
        user = auth.read(request.cookies.get(COOKIE_NAME)) if cfg.web.auth.enabled else None
        return {
            "enabled": cfg.web.auth.enabled,
            "default_password": auth.default_password,
            "logged_in": bool(user),
            "username": user or "",
        }

    @app.post("/api/login")
    def api_login(payload: dict, request: Request):
        if not cfg.web.auth.enabled:
            raise HTTPException(400, "当前没有启用登录")
        ip = _client_ip(request)
        wait = throttle.blocked(ip)
        if wait:
            raise HTTPException(429, f"尝试次数过多，请 {wait} 秒后再试")

        username = str((payload or {}).get("username") or "").strip()
        password = str((payload or {}).get("password") or "")
        if not auth.verify(username, password):
            count = throttle.fail(ip)
            log.warning("登录失败：%s（来源 %s，连续 %s 次）", username or "(空)", ip, count)
            raise HTTPException(401, "用户名或密码不正确")

        throttle.reset(ip)
        log.info("登录成功：%s（来源 %s）", auth.username, ip)
        response = JSONResponse({
            "ok": True,
            "username": auth.username,
            "default_password": auth.default_password,
        })
        return _set_session(response, auth.username)

    @app.post("/api/logout")
    def api_logout():
        response = JSONResponse({"ok": True, "message": "已退出登录"})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/account")
    def account(request: Request):
        user = auth.read(request.cookies.get(COOKIE_NAME)) or auth.username
        return {
            "username": auth.username,
            "logged_in_as": user,
            "auth_enabled": cfg.web.auth.enabled,
            "default_password": auth.default_password,
            "session_hours": auth.session_hours,
            "file": str(auth.path) if cfg.web.auth.enabled else "",
            "client_ip": _client_ip(request),
        }

    @app.post("/api/account")
    def update_account(payload: dict, request: Request):
        """改用户名 / 密码。必须验证当前密码，改完旧会话全部失效。"""
        if not cfg.web.auth.enabled:
            raise HTTPException(400, "当前没有启用登录，无需修改账号")
        data = payload or {}
        current = str(data.get("current_password") or "")
        new_user = str(data.get("username") or "").strip()
        new_pass = str(data.get("new_password") or "")

        ip = _client_ip(request)
        wait = throttle.blocked(ip)
        if wait:
            raise HTTPException(429, f"尝试次数过多，请 {wait} 秒后再试")
        if not auth.verify(auth.username, current):
            throttle.fail(ip)
            log.warning("修改账号失败：当前密码不正确（来源 %s）", ip)
            raise HTTPException(400, "当前密码不正确")
        if not new_user and not new_pass:
            raise HTTPException(400, "没有需要修改的内容")

        try:
            if new_user:
                new_user = validate_username(new_user)
            if new_pass:
                new_pass = validate_password(new_pass, new_user or auth.username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if new_user and new_user == auth.username and not new_pass:
            raise HTTPException(400, "用户名没有变化")
        if new_pass and new_pass == current:
            raise HTTPException(400, "新密码不能和当前密码相同")

        auth.update(username=new_user or None, password=new_pass or None)
        throttle.reset(ip)

        parts = []
        if new_user:
            parts.append(f"用户名改为 {new_user}")
        if new_pass:
            parts.append("密码已更新")
        message = "，".join(parts) + "。其他设备上的登录已失效"
        response = JSONResponse({
            "ok": True,
            "message": message,
            "username": auth.username,
            "default_password": auth.default_password,
        })
        return _set_session(response, auth.username)

    @app.get("/", response_class=FileResponse)
    def index():
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(500, "前端文件缺失")
        return FileResponse(page, media_type="text/html; charset=utf-8",
                            headers=NO_CACHE)

    # ---------- 状态 ----------

    @app.get("/api/status")
    def status():
        snapshot = runner.snapshot()
        snapshot["servers"] = pool.snapshot()
        snapshot["roots"] = cfg.scan.roots
        snapshot["overwrite"] = cfg.scrape.overwrite
        snapshot["autostart"] = cfg.scan.autostart
        snapshot["mode_config"] = cfg.scan.mode
        snapshot["interval"] = cfg.scan.interval_minutes
        snapshot["timestamp"] = int(time.time())
        snapshot["images"] = {
            "poster": [cfg.scrape.poster.width, cfg.scrape.poster.height],
            "fanart": [cfg.scrape.fanart.width, cfg.scrape.fanart.height],
            "thumb": [cfg.scrape.thumb.width, cfg.scrape.thumb.height]
            if cfg.scrape.thumb.enabled else None,
        }
        snapshot["log_file"] = str(log_files.main) if log_files.main else ""
        snapshot["refresh"] = manual_refresh
        snapshot["schedule"] = sched.status()
        with single_lock:
            snapshot["single"] = {"count": len(single_active),
                                  "paths": sorted(single_active)}
        return snapshot

    @app.get("/api/items")
    def items(
        status_filter: str = Query("all", alias="status"),
        keyword: str = Query("", alias="q"),
        dir_path: str = Query("", alias="dir"),
        page: int = Query(1, ge=1),
        size: int = Query(30, ge=1, le=200),
        sort: str = Query("added"),
        order: str = Query("desc"),
    ):
        rows = store.items(
            status=status_filter, keyword=keyword, dir_path=dir_path,
            limit=size, offset=(page - 1) * size, sort=sort, order=order,
        )
        depth_of = _depth_map(_roots())
        art_cache: dict[Path, bool] = {}
        try:
            pins = store.folder_art_map()
        except Exception:  # noqa: BLE001 - 老库没有这张表时按「没指定」处理
            pins = {}
        pin_cache: dict[Path, str] = {}

        def folder_art(folder: Path) -> bool:
            # 同一个文件夹下的几十条记录共用一次 scandir
            if folder not in art_cache:
                art_cache[folder] = _has_folder_art(folder)
            return art_cache[folder]

        def folder_pin(folder: Path) -> str:
            if folder not in pin_cache:
                pin_cache[folder] = pins.get(_norm_path(folder), "")
            return pin_cache[folder]

        items_out = []
        for row in rows:
            folder = Path(row.path).parent
            depth = depth_of(folder)
            pin = folder_pin(folder)
            items_out.append({
                "path": row.path,
                "name": row.name,
                "size": row.size,
                "status": row.status,
                "reason": row.reason,
                "title": row.title,
                "duration": round(row.duration, 1),
                "attempts": row.attempts,
                "updated": int(row.updated),
                "added": int(row.added),
                "frame_at": round(row.frame_at, 2),
                # 只有深层目录才有「文件夹封面」这一说，浅层的不必额外 scandir
                "dir_depth": depth,
                "folder_art": folder_art(folder) if depth >= 2 else True,
                "folder_pin": 1 if pin else 0,
                "pin_name": Path(pin).name if pin else "",
                "artifacts": artifacts(build_target(Path(row.path))),
            })
        return {
            "page": page,
            "size": size,
            "total": store.count(status_filter, keyword, dir_path=dir_path),
            "dir": dir_path,
            "items": items_out,
        }

    def _decorate_groups(groups: list[dict]) -> list[dict]:
        """给分组补上「在媒体库里的相对路径 / 层数 / 文件夹封面 / 指定的那张封面」。"""
        roots = sorted((str(Path(r).resolve()) for r in cfg.scan.roots), key=len, reverse=True)
        depth_of = _depth_map(_roots())
        try:
            pins = store.folder_art_map()
        except Exception:  # noqa: BLE001
            pins = {}
        for group in groups:
            group["rel"] = ""
            for root in roots:
                if group["path"] == root:
                    break
                for sep in ("\\", "/"):
                    if group["path"].startswith(root + sep):
                        group["rel"] = group["path"][len(root) + 1:]
                        break
                if group["rel"]:
                    break
            # 分组里的目录一定直接含 strm，所以只有「深层」才需要文件夹封面
            depth = depth_of(Path(group["path"]))
            group["depth"] = depth
            group["folder_art"] = _has_folder_art(Path(group["path"])) if depth >= 2 else True
            pin = pins.get(_norm_path(group["path"]), "")
            group["pinned"] = 1 if pin else 0
            group["pin_name"] = Path(pin).name if pin else ""
        return groups

    @app.get("/api/items/groups")
    def item_groups(
        status_filter: str = Query("all", alias="status"),
        keyword: str = Query("", alias="q"),
        limit: int = Query(400, ge=1, le=2000),
        sort: str = Query("added"),
        order: str = Query("desc"),
    ):
        """任务列表的「按文件夹」视图（平铺）：每一层含 strm 的文件夹一行。"""
        groups = _decorate_groups(store.dir_groups(status_filter, keyword, limit=limit,
                                                   sort=sort, order=order))
        return {
            "groups": groups,
            "total": len(groups),
            "truncated": len(groups) >= limit,
            "counts": store.stats(),
        }

    _STAT_KEYS = ("total", "ok", "skipped", "failed", "pending", "running")

    def _node_sort_key(node: dict, sort: str):
        if sort == "name":
            return node["name"].lower()
        if sort == "added":
            return (node["added"], node["name"].lower())
        if sort == "updated":
            return (node["updated"], node["name"].lower())
        # 默认：正在跑的排最前，其次有失败的，再其次还有待处理的
        return (-node["running"], -node["failed"], -node["pending"],
                node["path"].lower())

    def _build_tree(groups: list[dict], sort: str, order: str) -> list[dict]:
        """把「含 strm 的文件夹」列表补成完整的目录树。

        中间层（只有子目录、自己不含 strm 的那几层）在平铺视图里根本不会出现，
        但在文件管理里是要一层层点进去的 —— 这里按扫描根把整条链路补齐，
        每一层都带上子树汇总，收起时也能看出这一支有多少任务、跑到哪了。
        """
        depth_of = _depth_map(_roots())
        nodes: dict[str, dict] = {}
        parent_of: dict[str, str] = {}

        def new_node(path: str) -> dict:
            node = {
                "path": path, "name": dir_label(path), "parent": "",
                "kind": "dir", "hasItems": False, "depth": 0,
                "children": [], "dirs": 0,
                "total": 0, "ok": 0, "skipped": 0, "failed": 0,
                "pending": 0, "running": 0, "done": 0, "pct": 0,
                "added": 0.0, "updated": 0.0,
                "folder_art": True, "pinned": 0, "pin_name": "",
            }
            nodes[path] = node
            return node

        for group in groups:
            path = group["path"]
            rel = group.get("rel") or ""
            chain: list[str] = []
            if rel:
                cut = len(path) - len(rel) - 1
                prefix = path[:cut] if cut > 0 else ""
                sep = path[cut] if 0 <= cut < len(path) else os.sep
                if prefix:
                    chain.append(prefix)
                    cur = prefix
                    # 相对路径里混用 / 和 \ 的库也照走，按这一层的实际分隔符拼
                    for part in rel.replace("\\", sep).replace("/", sep).split(sep):
                        if not part:
                            continue
                        cur = cur + sep + part
                        chain.append(cur)
            if not chain:
                chain = [path]

            for index, node_path in enumerate(chain):
                node = nodes.get(node_path) or new_node(node_path)
                if index == 0:
                    continue
                key = chain[index - 1]
                if node_path not in parent_of:
                    parent_of[node_path] = key
                    nodes[key]["children"].append(node_path)

            leaf = nodes[path]
            leaf["kind"] = "root" if len(chain) == 1 else "leaf"
            leaf["hasItems"] = True
            leaf["folder_art"] = group["folder_art"]
            leaf["pinned"] = group["pinned"]
            leaf["pin_name"] = group["pin_name"]
            for name in _STAT_KEYS:
                leaf[name] += group.get(name, 0)
            leaf["added"] = max(leaf["added"], float(group.get("added") or 0))
            leaf["updated"] = max(leaf["updated"], float(group.get("updated") or 0))

        def rollup(node: dict) -> dict:
            for key in node["children"]:
                child = rollup(nodes[key])
                for name in _STAT_KEYS:
                    node[name] += child[name]
                node["added"] = max(node["added"], child["added"])
                node["updated"] = max(node["updated"], child["updated"])
                node["dirs"] += child["dirs"]
            if node["hasItems"]:
                node["dirs"] += 1
            node["done"] = node["ok"] + node["skipped"]
            node["pct"] = round(100 * node["done"] / node["total"]) if node["total"] else 0
            node["depth"] = depth_of(Path(node["path"]))
            # 只有子目录的那几层在 Emby 里同样是文件夹条目，也需要 folder.jpg
            if not node["hasItems"] and node["depth"] >= 2:
                node["folder_art"] = _has_folder_art(Path(node["path"]))
            return node

        # 「行动优先级」本身就是从重要到次要排的，不能再按 order 翻一遍
        desc = order == "desc" and sort in ("name", "added", "updated")

        def order_tree(node: dict) -> dict:
            kids = [order_tree(nodes[key]) for key in node["children"]]
            kids.sort(key=lambda item: _node_sort_key(item, sort), reverse=desc)
            node["children"] = kids
            return node

        tops = [order_tree(rollup(nodes[key]))
                for key in nodes if key not in parent_of]
        for node in nodes.values():
            for child in node["children"]:
                child["parent"] = node["path"]
        tops.sort(key=lambda item: _node_sort_key(item, sort), reverse=desc)
        return tops

    @app.get("/api/items/tree")
    def item_tree(
        status_filter: str = Query("all", alias="status"),
        keyword: str = Query("", alias="q"),
        limit: int = Query(400, ge=1, le=2000),
        sort: str = Query("added"),
        order: str = Query("desc"),
    ):
        """任务列表的「按文件夹」视图（目录树）：结构跟文件管理里点进去的一样。"""
        groups = _decorate_groups(store.dir_groups(status_filter, keyword, limit=limit,
                                                   sort=sort, order=order))
        return {
            "nodes": _build_tree(groups, sort, order),
            "total": len(groups),
            "truncated": len(groups) >= limit,
            "counts": store.stats(),
        }

    @app.post("/api/items/delete")
    def items_delete(payload: dict):
        """任务列表里勾选后删除。

        默认只从列表里移除记录（下次扫描还会重新登记）；``purge=true`` 时顺带
        删掉已经生成的图片和 NFO —— 媒体库里的 .strm 本身永远不动。
        """
        data = payload or {}
        paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
        dirs = [str(p).strip() for p in (data.get("dirs") or []) if str(p).strip()]
        recursive = bool(data.get("recursive", True))
        purge = bool(data.get("purge", False))
        for raw in dirs:
            paths.extend(store.paths_in(raw, recursive=recursive))
        unique = [p for p in dict.fromkeys(paths) if p]
        if not unique:
            raise HTTPException(400, "没有选中任务")

        removed = purged = skipped = 0
        errors: list[str] = []
        for raw in unique:
            item = store.get(raw)
            if item and item.status == "running":
                skipped += 1     # 正在跑的那条删了会被回写状态，等它跑完再删
                continue
            if purge:
                try:
                    inside = _within_roots(raw)
                except HTTPException:
                    inside = None    # 扫描目录改过了，只删记录，不碰磁盘
                if inside is not None:
                    target = build_target(inside)
                    for kind in ("poster", "fanart", "thumb", "nfo"):
                        path = getattr(target, kind)
                        try:
                            if path.is_file():
                                path.unlink()
                                purged += 1
                        except OSError as exc:
                            errors.append(f"{path.name}：{exc}")
            removed += store.delete([raw])

        log.info("任务列表：移除 %s 条记录，删除 %s 个产物，跳过 %s 条处理中",
                 removed, purged, skipped)
        if not removed and not skipped:
            if purged:
                return {"ok": not errors, "removed": 0, "purged": purged, "skipped": 0,
                        "message": f"这些任务不在列表里了，已删掉 {purged} 个产物文件",
                        "errors": errors[:5]}
            return {"ok": False, "removed": 0, "purged": 0, "skipped": 0,
                    "message": "这些任务已经不在列表里了，刷新看看"}
        message = f"已从任务列表移除 {removed} 条"
        if purge:
            message += f"，删除 {purged} 个产物文件"
        if skipped:
            message += f"，{skipped} 条正在处理已跳过"
        return {"ok": not errors, "message": message, "removed": removed,
                "purged": purged, "skipped": skipped, "errors": errors[:5]}

    # ---------- 任务控制 ----------

    @app.post("/api/task/start")
    def task_start(full: bool = Query(False)):
        if runner.busy():
            if runner.snapshot().get("paused"):
                return JSONResponse({"ok": False, "message": "任务已暂停，点「继续任务」接着跑"},
                                    status_code=409)
            return JSONResponse({"ok": False, "message": "任务正在运行，先暂停或取消"},
                                status_code=409)
        started = runner.start(full=full)
        message = "已开始全量重刮" if full else "已开始刮削（增量）"
        return {"ok": started, "message": message if started else "任务已在运行"}

    @app.post("/api/task/pause")
    def task_pause():
        if runner.pause():
            return {"ok": True, "message": "已暂停，进行中的任务已中断并退回待处理"}
        return JSONResponse({"ok": False, "message": "当前没有可暂停的任务"},
                            status_code=409)

    @app.post("/api/task/resume")
    def task_resume():
        if runner.resume():
            return {"ok": True, "message": "已继续执行"}
        return JSONResponse({"ok": False, "message": "任务没有处于暂停状态"},
                            status_code=409)

    @app.post("/api/task/cancel")
    def task_cancel():
        if runner.cancel():
            return {"ok": True, "message": "已取消，进行中的结果作废并退回待处理"}
        return JSONResponse({"ok": False, "message": "当前没有正在运行的任务"},
                            status_code=409)

    @app.post("/api/scan")
    def scan(full: bool = Query(False)):
        return task_start(full=full)

    @app.post("/api/stop")
    def stop():
        return task_cancel()

    @app.post("/api/retry-failed")
    def retry_failed():
        if runner.busy():
            return JSONResponse({"ok": False, "message": "任务正在运行中"}, status_code=409)
        count = store.reset(("failed",))
        log.info("手动重试：重置 %s 个失败项", count)
        return {"ok": True, "message": f"已重置 {count} 个失败项，等待下次扫描"}

    @app.post("/api/redo")
    def redo(payload: dict):
        """单条重刮：重置状态后立即跑一轮增量。"""
        target = (payload or {}).get("path", "").strip()
        if not target:
            raise HTTPException(400, "缺少 path 参数")
        if runner.busy():
            return JSONResponse({"ok": False, "message": "任务正在运行中"}, status_code=409)
        store.reset_one(target)
        log.info("手动重刮：%s", target)
        runner.start(full=False)
        return {"ok": True, "message": "已加入队列"}

    # ---------- 文件管理 ----------

    def _roots() -> list[Path]:
        found: list[Path] = []
        for root in cfg.scan.roots:
            path = Path(root)
            if path.is_dir():
                found.append(path.resolve())
        return found

    def _within_roots(raw: str) -> Path:
        """只允许操作扫描目录里面的路径，挡住 ../ 之类的越界。"""
        text = (raw or "").strip()
        if not text:
            raise HTTPException(400, "缺少路径参数")
        path = Path(text)
        if not path.is_absolute():
            raise HTTPException(400, "路径必须是绝对路径")
        target = path.resolve()
        for root in _roots():
            if target == root or root in target.parents:
                return target
        raise HTTPException(403, "该路径不在扫描目录内，已拒绝")

    def _depth_map(roots: list[Path]):
        """返回一个算「这个目录在扫描根下第几层」的函数（直接放在根里 = 1）。"""

        def depth_of(target: Path) -> int:
            best = 0
            for root in roots:
                try:
                    best = max(best, len(target.relative_to(root).parts))
                except ValueError:
                    continue
            return best

        return depth_of

    def _skip_dir(name: str) -> bool:
        return _skip_dir_name(name)

    # ---------- 单条立即执行 ----------

    def _products_ready(source: Path) -> bool:
        """这个文件的图片 / NFO 是不是已经按当前设置齐了。"""
        complete, _missing = product_state(
            build_target(source), cfg.scrape.write_nfo, cfg.scrape.write_poster,
            cfg.scrape.write_fanart, cfg.scrape.thumb.enabled,
        )
        return complete

    def _spawn_single(paths: list[str], force: bool = False) -> list[str]:
        """让这些文件立刻单独跑，不进批量队列。

        文件管理里点单个「刮削」走这条路：不用等当前批量任务跑完，也不必把
        整个目录重扫一遍。已经在跑的文件会跳过，避免同一个文件被两路同时写。
        """
        started: list[str] = []
        for raw in paths:
            with single_lock:
                if raw in single_active:
                    continue
                single_active[raw] = time.time()
            item = store.get(raw)
            if item and item.status == "running":
                with single_lock:
                    single_active.pop(raw, None)
                continue
            # 没扫描过的文件在库里还没有记录，mark_* 都是 UPDATE，不先登记就写不进去
            if item is None:
                try:
                    size, mtime = signature(Path(raw))
                except OSError:
                    size, mtime = 0, 0.0
                store.ensure([(raw, size, mtime)])
            store.mark_running(raw)
            threading.Thread(
                target=_single_worker,
                args=(raw, item.frame_at if item else 0.0, force),
                daemon=True, name="ws-single",
            ).start()
            started.append(raw)
        return started

    def _single_worker(path: str, frame_at: float, force: bool = False) -> None:
        """线程池之外的独立执行，状态直接写进 state.db，前端轮询就能看到。"""
        stem = Path(path).stem
        result: ScrapeResult | None = None
        try:
            result = process(cfg, build_target(Path(path)), frame_at=frame_at, force=force)
        except (probe.ProbeError, frames.FrameError) as exc:
            result = ScrapeResult(ok=False, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - 单条失败不能影响别的任务
            log.exception("单独刮削异常：%s", path)
            result = ScrapeResult(ok=False, reason=f"未预期的错误：{exc}")
        finally:
            with single_lock:
                single_active.pop(path, None)

        if result is None:
            store.mark_failed(path, "未执行")
            return

        if not result.ok:
            store.mark_failed(path, result.reason)
            log.warning("[单独失败] %s | %s", stem[:60], result.reason)
            return

        if result.reason == "产物已存在，跳过":
            store.mark_skipped(path, result.reason)
            log.info("[单独跳过] %s（产物齐全）", stem[:60])
            return

        store.mark_ok(path, result.title, result.duration)
        try:
            size, mtime = signature(Path(path))
            store.touch_signature(path, size, mtime)
        except OSError:
            pass
        log.info("[单独完成] %s | %.1fs | 海报 %s 背景 %s NFO %s",
                 result.title[:60], result.elapsed,
                 _size_of(result.sizes.get("poster")),
                 _size_of(result.sizes.get("fanart")),
                 _size_of(result.sizes.get("nfo")))
        try:
            if pool.servers():
                pool.refresh([path])
        except Exception as exc:  # noqa: BLE001 - 刷新失败不影响刮削结果
            log.warning("刷新媒体服务器失败：%s", exc)

    @app.get("/api/files")
    def list_files(path: str = Query(""), q: str = Query(""),
                   limit: int = Query(500, ge=1, le=2000),
                   sort: str = Query("added"), order: str = Query("desc")):
        roots = _roots()
        if not roots:
            return {
                "ok": False, "path": "", "parent": None, "roots": [],
                "breadcrumb": [], "dirs": [], "files": [], "truncated": False,
                "message": "扫描目录都不存在，检查 docker volume 挂载",
            }

        if path.strip():
            current = _within_roots(path)
            if not current.is_dir():
                raise HTTPException(404, "目录不存在")
        else:
            current = roots[0]

        # 面包屑：从命中的扫描根往下拼
        breadcrumb: list[dict] = []
        parent = None
        for root in roots:
            try:
                rel = current.relative_to(root)
            except ValueError:
                continue
            breadcrumb.append({"name": root.name or str(root), "path": str(root)})
            cursor = root
            for part in rel.parts:
                cursor = cursor / part
                breadcrumb.append({"name": part, "path": str(cursor)})
            break
        if breadcrumb:
            parent = str(current.parent) if len(breadcrumb) > 1 else None

        wanted = {e.lower() if e.startswith(".") else f".{e.lower()}"
                  for e in cfg.scan.extensions}
        depth_of = _depth_map(roots)
        keyword = q.strip().lower()
        # 指定过封面的目录：一次读全表，列表里逐行判断，不额外查库
        try:
            pins = store.folder_art_map()
        except Exception:  # noqa: BLE001 - 老库没这张表也不该让列表打不开
            pins = {}
        dirs: list[dict] = []
        file_rows: list[dict] = []
        truncated = False

        try:
            entries = list(current.iterdir())
        except OSError as exc:
            raise HTTPException(500, f"读取目录失败：{exc}") from exc

        # 先把每个条目的信息取全再排序：排序要在截断（limit）之前做，
        # 否则「取前 500 个再排序」等于随机挑 500 个
        infos: list[dict] = []
        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir and _skip_dir(name):
                continue
            if not is_dir and (name.lower() in IGNORED_NAMES
                               or entry.suffix.lower() not in wanted):
                continue
            if keyword and keyword not in name.lower():
                continue
            try:
                stat = entry.stat()
                size, mtime = stat.st_size, round(stat.st_mtime, 1)
            except OSError:
                size, mtime = 0, 0.0
            infos.append({
                "entry": entry, "name": name, "is_dir": is_dir,
                "size": size, "mtime": mtime,
            })

        descending = str(order).lower() == "desc"
        if sort == "size":
            key = lambda info: (info["size"], info["name"].lower())
        elif sort == "added":
            # 「添加时间」用文件修改时间：网盘下载 / 转存后基本就是这个时间
            key = lambda info: (info["mtime"], info["name"].lower())
        else:
            key = lambda info: info["name"].lower()
        # 文件夹永远排在文件前面，两组各自按选择的规则排
        for group in (True, False):
            part = [i for i in infos if i["is_dir"] is group]
            part.sort(key=key, reverse=descending)

            if group:
                for info in part:
                    summary = _dir_summary(info["entry"], wanted)
                    pin = pins.get(_norm_path(info["entry"]), "")
                    dirs.append({"name": info["name"], "path": str(info["entry"]),
                                 "count": summary["count"], "scraped": summary["scraped"],
                                 "poster": summary["poster"],
                                 "own": summary["own"], "art": summary["art"],
                                 "depth": depth_of(info["entry"]),
                                 "pinned": 1 if pin else 0,
                                 "pin_name": Path(pin).name if pin else "",
                                 "mtime": info["mtime"]})
                continue
            for info in part:
                if len(file_rows) >= limit:
                    truncated = True
                    break
                file_rows.append({
                    "path": str(info["entry"]), "name": info["name"],
                    "size": info["size"], "mtime": info["mtime"],
                })

        status_map = store.get_many(row["path"] for row in file_rows)
        # 这一屏的文件都在同一个目录里，深度、folder.jpg、指定关系只需要算一次
        file_depth = depth_of(current)
        file_art = _has_folder_art(current)
        file_pin = pins.get(_norm_path(current), "")
        for row in file_rows:
            item = status_map.get(row["path"])
            row["status"] = item.status if item else "new"
            row["reason"] = item.reason if item else ""
            row["title"] = item.title if item else ""
            row["duration"] = round(item.duration, 1) if item else 0.0
            row["frame_at"] = round(item.frame_at, 2) if item else 0.0
            row["dir_depth"] = file_depth
            row["folder_art"] = file_art
            row["folder_pin"] = 1 if file_pin else 0
            row["pin_name"] = Path(file_pin).name if file_pin else ""
            row["artifacts"] = artifacts(build_target(Path(row["path"])))

        return {
            "ok": True, "path": str(current), "parent": parent,
            "roots": [str(root) for root in roots], "breadcrumb": breadcrumb,
            "dirs": dirs, "files": file_rows, "truncated": truncated,
        }

    @app.post("/api/files/expand")
    def files_expand(payload: dict):
        """把一个目录展开成里面的 strm 路径列表。

        文件管理里勾文件夹用的：前端只知道当前这一层，子目录里有什么得问后端。
        """
        data = payload or {}
        raw_dirs = [str(p).strip() for p in (data.get("dirs") or []) if str(p).strip()]
        if not raw_dirs:
            raise HTTPException(400, "没有选中目录")
        recursive = bool(data.get("recursive", True))
        limit = int(data.get("limit") or 5000)

        wanted = {e.lower() if e.startswith(".") else f".{e.lower()}"
                  for e in cfg.scan.extensions}
        per: dict[str, list[str]] = {}
        total = 0
        truncated = False
        errors: list[str] = []

        for raw in raw_dirs:
            try:
                directory = _within_roots(raw)
            except HTTPException as exc:
                errors.append(f"{raw}：{exc.detail}")
                continue
            if not directory.is_dir():
                errors.append(f"{raw}：不是目录")
                continue
            found: list[str] = []
            try:
                if recursive:
                    stack = [directory]
                    while stack and len(found) < limit:
                        current = stack.pop()
                        try:
                            with os.scandir(current) as entries:
                                for entry in entries:
                                    try:
                                        if entry.is_dir():
                                            if not _skip_dir(entry.name):
                                                stack.append(Path(entry.path))
                                            continue
                                        if (entry.name.lower() not in IGNORED_NAMES
                                                and Path(entry.name).suffix.lower() in wanted):
                                            found.append(entry.path)
                                    except OSError:
                                        continue
                        except OSError:
                            continue
                        if len(found) >= limit:
                            truncated = True
                            break
                else:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            try:
                                if (entry.is_file()
                                        and entry.name.lower() not in IGNORED_NAMES
                                        and Path(entry.name).suffix.lower() in wanted):
                                    found.append(entry.path)
                            except OSError:
                                continue
            except OSError as exc:
                errors.append(f"{raw}：读取失败 {exc}")
                continue
            found.sort()
            per[raw] = found
            total += len(found)

        return {
            "ok": not errors or total > 0,
            "per": per,
            "paths": [p for group in per.values() for p in group],
            "count": total,
            "truncated": truncated,
            "message": "；".join(errors) or f"展开 {len(per)} 个目录，共 {total} 个 strm",
        }

    @app.get("/api/files/image")
    def file_image(path: str = Query(...), kind: str = Query("poster"),
                   w: int = Query(0, ge=0, le=512)):
        """海报 / 背景图 / 缩略图。

        列表里只用几十像素，`w` 传像素宽度时改发服务端压好的小图（几十 KB
        而不是上百 KB 的原图，条目多的时候差别是几十兆）；不传 `w` 就是原图，
        预览弹窗和选帧用。
        """
        source = _within_roots(path)
        target = build_target(source)
        picked = {"poster": target.poster, "fanart": target.fanart,
                  "thumb": target.thumb}.get(kind)
        if not picked or not picked.is_file():
            raise HTTPException(404, "图片还没生成")
        served = thumbs.get(picked, w) if w else picked
        return FileResponse(served, media_type="image/jpeg", headers=IMAGE_CACHE)

    @app.post("/api/files/delete")
    def files_delete(payload: dict):
        data = payload or {}
        paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
        kinds = [k for k in (data.get("what") or ["poster", "fanart", "thumb", "nfo"])
                 if k in ("poster", "fanart", "thumb", "nfo")]
        reset = bool(data.get("reset", True))
        if not paths:
            raise HTTPException(400, "没有选中文件")
        if not kinds:
            raise HTTPException(400, "没有选中要删除的产物类型")

        removed = 0
        touched = 0
        errors: list[str] = []
        for raw in paths:
            try:
                source = _within_roots(raw)
            except HTTPException as exc:
                errors.append(f"{raw}：{exc.detail}")
                continue
            target = build_target(source)
            for kind in kinds:
                path = getattr(target, kind)
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError as exc:
                    errors.append(f"{path.name}：{exc}")
            if reset:
                store.reset_one(str(source))
                touched += 1

        log.info("文件管理：删除 %s 个产物，重置 %s 条记录", removed, touched)
        message = f"已删除 {removed} 个产物文件"
        if reset and touched:
            message += f"，{touched} 条记录已重置，下次开始任务会重新生成"
        if not removed and not errors:
            message = "选中的文件本来就没有这些产物"
        return {"ok": not errors, "message": message, "removed": removed,
                "reset": touched, "errors": errors[:5]}

    @app.post("/api/files/scrape")
    def files_scrape(payload: dict):
        """刮削选中的文件。

        只跑勾选的这些，别的目录不会被顺带拉起来。已经刮过且产物齐全的
        直接跳过（不重新探测、不改状态）；要连旧的图片/NFO 一起覆盖，传
        ``force: true``（文件管理里的「开始重刮」）。

        单个文件默认**立即执行**（不受批量队列影响，点完就开始跑）；勾选多个
        时交给后台队列统一处理，避免一口气开几十个线程去抢网盘带宽。
        """
        data = payload or {}
        paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
        immediate = bool(data.get("now"))
        force = bool(data.get("force"))
        if not paths:
            raise HTTPException(400, "没有选中文件")
        queued: list[str] = []
        ready: list[str] = []
        rejected = 0
        for raw in paths:
            try:
                source = _within_roots(raw)
            except HTTPException:
                rejected += 1
                continue
            if not source.is_file():
                rejected += 1
                continue
            path = str(source)
            if not force and _products_ready(source):
                # 产物齐全：不重跑、不覆盖，状态保持「成功」
                item = store.get(path)
                if item is None:
                    try:
                        size, mtime = signature(source)
                    except OSError:
                        size, mtime = 0, 0.0
                    store.ensure([(path, size, mtime)])
                    item = store.get(path)
                if item is not None and item.status not in ("ok", "skipped"):
                    store.mark_skipped(path, "产物齐全，跳过")
                ready.append(path)
                continue
            store.reset_one(path)
            queued.append(path)

        tail = f"，{len(ready)} 个产物已齐全跳过" if ready else ""
        if not queued:
            if not ready and rejected:
                return {"ok": False, "started": False, "skipped": 0,
                        "message": f"选中的 {rejected} 个文件不在媒体库目录里"
                                   "（或已经被移动/删除），刷新列表再看"}
            return {"ok": True, "started": False, "skipped": len(ready),
                    "message": f"选中的 {len(ready)} 个文件产物都齐了，不用重刮"
                               "（要覆盖旧图就点「开始重刮」）"}

        if immediate or len(queued) == 1:
            started = _spawn_single(queued, force=force)
            if not started:
                return {"ok": False, "started": False,
                        "message": "这个文件正在执行中，等它跑完"}
            head = "已开始重刮" if force else "已开始单独刮削"
            return {"ok": True, "started": True, "single": True, "skipped": len(ready),
                    "message": f"{head} {len(started)} 个文件{tail}"}

        if runner.busy():
            snap = runner.snapshot()
            joined = runner.add_scope(queued)
            if snap.get("paused"):
                tip = "任务已暂停，点「继续任务」后处理"
            elif joined or snap.get("mode") in ("full", "incremental"):
                tip = "当前任务会顺带处理"
            else:
                tip = "本轮正在收尾，等它停下再点「开始任务」"
            return {"ok": True, "started": False, "skipped": len(ready),
                    "message": f"已加入队列 {len(queued)} 个，{tip}{tail}"}
        # 只跑勾选的这些：不扫描全库，别的文件夹不会被顺带拉起来
        runner.start(full=False, only=queued, force=force)
        head = "已开始重刮选中的" if force else "已开始刮削选中的"
        return {"ok": True, "started": True, "skipped": len(ready),
                "message": f"{head} {len(queued)} 个文件{tail}"}

    # ---------- 文件夹封面修复（手动触发） ----------

    @app.post("/api/files/folder-poster")
    def files_folder_poster(payload: dict):
        """给多层文件夹结构补齐每一层的封面，只在这个动作被点时才执行。

        Emby 与 Jellyfin 的取图规则是同一套：
        - 目录里直接有视频 → 这个目录是「影片」条目，`<视频名>-poster.jpg` 生效；
        - 目录里没有直接视频、只有子目录 → 它是「文件夹」条目，只认
          ``folder.jpg`` / ``poster.jpg`` / ``cover.jpg`` 这类固定名字，
          ``xxx-poster.jpg`` 对它完全无效。
        多层嵌套时，中间那几层全都是文件夹条目；哪一层没封面，整条链路看起来
        就是「海报全丢」。所以这里对**每一层**都写一份固定名字的封面
        （``folder.jpg`` + ``poster.jpg``），哪一层落到文件夹条目都有图可用。

        为什么不做进正常刮削：单层目录不需要，往每个目录都塞图会给媒体库添乱，
        还会跟「按视频名生成的海报」抢优先级。所以这个动作必须手动触发。

        修复范围：
        - 往下：选中目录整棵子树里每一层含 strm 的目录
        - 往上：从选中目录一直到扫描根下面那一层（中间层同样没有直接视频）
        - 扫描根本身不写，那是库图标的事
        """
        data = payload or {}
        raw_dirs = [str(p).strip() for p in (data.get("dirs") or []) if str(p).strip()]
        raw_files = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
        force = bool(data.get("force"))
        if not raw_dirs and not raw_files:
            raise HTTPException(400, "先勾选文件夹")

        roots = _roots()
        if not roots:
            raise HTTPException(400, "扫描目录都不存在，检查 volume 挂载")
        wanted = {e.lower() if e.startswith(".") else f".{e.lower()}"
                  for e in cfg.scan.extensions}

        bases: list[Path] = []
        rejected = 0
        for raw in raw_dirs:
            try:
                folder = _within_roots(raw)
            except HTTPException:
                rejected += 1
                continue
            if not folder.is_dir():
                rejected += 1
                continue
            bases.append(folder)
        for raw in raw_files:
            try:
                source = _within_roots(raw)
            except HTTPException:
                rejected += 1
                continue
            bases.append(source.parent)
        if not bases:
            raise HTTPException(400, "选中的路径都不在媒体库目录里")

        def needs_art(folder: Path, total: int) -> bool:
            """每一层只要递归含有 strm 就补 —— 不再区分「自己有没有直接放视频」。

            早先只补 own==0 的中间层，实测会漏：像「电影/某片/Season 1/xxx.strm」
            这种结构，Season 1 里直接有视频、它却是被当成季条目渲染的；而
            「媒体库根/分类/影片/视频」里那层「分类」既没有直接视频、也不是影片，
            同样要 folder.jpg。与其猜 Emby 把哪一层认成什么类型，不如让每一层
            都自带一张固定名字的封面，哪一层落到文件夹条目都有图可用。

            媒体库根自己永远不写：那是库图标的事，写进去会污染用户根目录。
            """
            return total > 0 and folder not in roots

        # 每个「最高层祖先」只扫一次整棵树：一次遍历就拿到各层的 direct/total/art
        targets: dict[Path, dict] = {}
        tree_cache: dict[Path, dict] = {}
        for base in bases:
            chain = _chain_to_root(base, roots)
            top = chain[-1] if chain else base
            stats = tree_cache.get(top)
            if stats is None:
                stats = tree_cache[top] = _tree_stats(top, wanted)
            chain_set = set(chain)
            for folder, st in stats.items():
                if not (folder == base or folder in chain_set
                        or base in folder.parents):
                    continue
                if needs_art(folder, st["total"]):
                    targets[folder] = st

        # 有「指定封面」的层优先用指定的那张，其余照旧自动挑
        pins = store.folder_art_map()
        created: list[str] = []
        kept: list[str] = []
        no_src: list[str] = []
        failed: list[str] = []
        written = 0
        pinned_used = 0
        for folder in sorted(targets):
            pinned = pins.get(_norm_path(folder), "")
            src = Path(pinned) if pinned and Path(pinned).is_file() else None
            if src is not None:
                pinned_used += 1
            elif pinned:
                log.info("这一层指定的封面已不存在，退回自动挑：%s", pinned)
            res = _write_folder_art(folder, force, src)
            if res["skipped"]:
                kept.append(str(folder))
            elif res["no_source"]:
                no_src.append(str(folder))
            elif res["failed"]:
                failed.append(str(folder))
            else:
                created.append(str(folder))
                written += res["written"]

        if not targets:
            return {"ok": True, "created": [], "kept": [], "noSource": [],
                    "files": 0,
                    "message": "选中的范围里没有含 strm 的文件夹"}
        parts: list[str] = []
        if created:
            parts.append(f"已补 {len(created)} 个文件夹封面（{written} 个文件）")
            if pinned_used:
                parts.append(f"其中 {pinned_used} 个用的是你指定的图")
        if kept:
            parts.append(f"{len(kept)} 个已就绪跳过")
        if no_src:
            parts.append(f"{len(no_src)} 个没找到可用海报（先刮削再补）")
        if failed:
            parts.append(f"{len(failed)} 个写入失败")
        if rejected:
            parts.append(f"{rejected} 个路径不在媒体库里")
        return {
            "ok": bool(created) or not (no_src or failed),
            "created": created, "kept": kept, "noSource": no_src, "failed": failed,
            "files": written, "pinned": pinned_used,
            "message": "，".join(parts) + "；Emby / Jellyfin 里刷新元数据后生效",
        }

    # ---------- 指定某一层用哪张图当封面 ----------

    @app.get("/api/files/art-candidates")
    def files_art_candidates(path: str = Query(...)):
        """列出这一层能拿来当封面的图，供人挑「用哪张」。

        范围是整棵子树：合集目录下每部片子各有一张海报，父层该用哪张只有人
        知道；程序按名字排第一张，经常不是想要的那张。这里把候选都摆出来，
        配上缩略图让人点一下。
        """
        folder = _within_roots(path)
        if not folder.is_dir():
            raise HTTPException(404, "文件夹不存在")
        items = _poster_candidates(folder)
        return {
            "ok": True, "dir": str(folder),
            "pinned": store.get_folder_art(_norm_path(folder)),
            "current": _current_art_source(folder, items),
            "items": items,
            "message": "" if items else "这个文件夹里还没刮出可用的海报",
        }

    @app.get("/api/files/art-image")
    def files_art_image(path: str = Query(...),
                        w: int = Query(0, ge=0, le=512)):
        """候选封面的缩略图：媒体库内的任意一张图片。

        和 ``/api/files/image`` 不同，那个是按 strm 推产物路径；这里给的是
        目录里实际存在的图片文件（比如某部片子的 ``xxx-poster.jpg``）。
        """
        source = _within_roots(path)
        if source.suffix.lower() not in ART_IMAGE_EXTS or not source.is_file():
            raise HTTPException(404, "图片不存在")
        served = thumbs.get(source, w) if w else source
        media = mimetypes.guess_type(served.name)[0] or "image/jpeg"
        return FileResponse(served, media_type=media, headers=IMAGE_CACHE)

    @app.post("/api/files/art-pin")
    def files_art_pin(payload: dict):
        """指定 / 取消指定某一层文件夹的封面图。

        指定后会立刻写一遍 folder.jpg + poster.jpg，不用再点一次修复；关系存进
        库里，之后每次补封面（包括「只补缺的」）都优先用这张，不会又退回
        「按名字排第一张」。
        """
        data = payload or {}
        folder = _within_roots(str(data.get("dir") or ""))
        if not folder.is_dir():
            raise HTTPException(404, "文件夹不存在")
        raw = str(data.get("source") or "").strip()
        if not raw:
            store.set_folder_art(_norm_path(folder), "")
            return {"ok": True, "cleared": True, "pinned": "", "files": 0,
                    "message": "已恢复自动挑选，下次补封面会重新挑一张"}
        source = _within_roots(raw)
        if source.suffix.lower() not in ART_IMAGE_EXTS or not source.is_file():
            raise HTTPException(400, "这不是一张可用的图片")
        if folder not in source.parents:
            raise HTTPException(403, "只能选这个文件夹里的图片")
        res = _write_folder_art(folder, True, source)
        if res["failed"]:
            raise HTTPException(500, "写入失败：这个目录可能没有写权限")
        store.set_folder_art(_norm_path(folder), str(source))
        return {
            "ok": True, "pinned": str(source), "files": res["written"],
            "message": f"已把「{source.name}」设为这一层的封面（{res['written']} 个文件），"
                       "Emby / Jellyfin 里刷新元数据后生效",
        }

    # ---------- 手动选帧 ----------

    def _pickable(raw: str) -> Path:
        source = _within_roots(raw)
        if not source.is_file():
            raise HTTPException(404, "文件不存在")
        return source

    def _parse_crop(raw) -> tuple[float, float, float, float] | None:
        """``"0.1,0.2,0.5,0.6"`` 或 ``[0.1, 0.2, 0.5, 0.6]`` → 取景框；空表示整帧。"""
        if not raw:
            return None
        parts = raw.split(",") if isinstance(raw, str) else raw
        if len(parts) != 4:
            raise HTTPException(400, "取景框格式不对，应为 x,y,w,h")
        try:
            x, y, w, h = (float(v) for v in parts)
        except (TypeError, ValueError):
            raise HTTPException(400, "取景框数值不合法") from None
        if not (0 <= x < 1 and 0 <= y < 1 and 0.02 <= w <= 1 and 0.02 <= h <= 1):
            raise HTTPException(400, "取景框超出画面范围")
        return (x, y, min(w, 1 - x), min(h, 1 - y))

    @app.get("/api/frames/plan")
    def frames_plan(path: str = Query(""), count: int = Query(12, ge=4, le=24)):
        """读时长、给出候选时间点，顺便把已有海报和手选记录带上。"""
        source = _pickable(path)
        target = build_target(source)
        item = store.get(str(source))
        try:
            data = picker.plan(target, count)
        except (probe.ProbeError, frames.FrameError) as exc:
            raise HTTPException(400, f"读不到视频信息：{exc}") from exc
        data.update({
            "status": item.status if item else "new",
            "title": item.title if item else "",
            "frame_at": round(item.frame_at, 2) if item else 0.0,
            "artifacts": artifacts(target),
            # 前端按这两个比例画取景框，和真正出图的尺寸保持一致
            "ratios": {
                "poster": [cfg.scrape.poster.width, cfg.scrape.poster.height],
                "fanart": [cfg.scrape.fanart.width, cfg.scrape.fanart.height],
                "thumb": [cfg.scrape.thumb.width, cfg.scrape.thumb.height],
            },
        })
        return data

    @app.get("/api/frames/frame")
    def frames_frame(path: str = Query(...), t: float = Query(0, ge=0),
                     width: int = Query(0, ge=0, le=1920),
                     crop: str = Query("")):
        """按时间点抓一帧返回 JPEG；同参数第二次直接走缓存。

        ``crop`` 传 ``x,y,w,h``（0–1 的取景框）时返回裁好的那一块，
        网页端拖框时靠它实时预览。
        """
        source = _pickable(path)
        box = _parse_crop(crop)
        try:
            if box:
                image = picker.cropped(source, t, box, width)
            else:
                image = picker.frame(source, t, width)
        except frames.FrameError as exc:
            raise HTTPException(504, str(exc)) from exc
        except probe.ProbeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(image, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=1800"})

    @app.post("/api/frames/apply")
    def frames_apply(payload: dict):
        """用选中的这一帧重出图，并记住位置（之后重刮也用它）。"""
        data = payload or {}
        source = _pickable(str(data.get("path") or ""))
        try:
            seconds = float(data.get("seconds"))
        except (TypeError, ValueError):
            raise HTTPException(400, "时间点不合法") from None
        if seconds < 0:
            raise HTTPException(400, "时间点不合法")

        raw_kinds = data.get("kinds")
        if raw_kinds is None:
            kinds = ["poster", "fanart"]
        else:
            kinds = [k for k in raw_kinds if k in ("poster", "fanart", "thumb")]
        if not kinds:
            raise HTTPException(400, "至少选一种图片（海报 / 背景图 / 缩略图）")

        crop = _parse_crop(data.get("crop"))
        crop_for = str(data.get("crop_for") or "all")
        if crop_for not in ("poster", "fanart", "all"):
            crop_for = "all"

        target = build_target(source)
        item = store.get(str(source))
        if item is None:
            store.ensure([(str(source), *signature(source))])
        try:
            made = picker.apply(target, seconds, kinds, crop=crop, crop_for=crop_for)
        except frames.FrameError as exc:
            raise HTTPException(504, str(exc)) from exc
        except probe.ProbeError as exc:
            raise HTTPException(400, str(exc)) from exc

        title = (item.title if item and item.title
                 else display_title(source.stem))
        store.mark_ok(str(source), title, made["duration"])
        store.set_frame_at(str(source), seconds)
        store.touch_signature(str(source), *signature(source))

        # 媒体服务器刷新比较慢，丢后台去做，别让按钮一直转圈
        refreshed = False
        if data.get("refresh", True) and pool.servers():
            refreshed = True

            def _refresh():
                try:
                    pool.refresh([str(source)])
                except Exception as exc:  # noqa: BLE001
                    log.warning("选帧后刷新媒体服务器失败：%s", exc)

            threading.Thread(target=_refresh, daemon=True, name="ws-pick-refresh").start()

        log.info("手动选帧完成：%s → %s", source.name, made["label"])
        scope = {"poster": "（海报按取景框裁切）", "fanart": "（背景图按取景框裁切）",
                 "all": "（按取景框裁切）"}.get(crop_for, "")
        return {
            "ok": True,
            "message": f"已用 {made['label']} 这一帧生成"
                       f"{'、'.join(_KIND_NAMES[k] for k in kinds)}"
                       + (scope if crop else "")
                       + ("，正在通知媒体服务器刷新" if refreshed else ""),
            "seconds": made["seconds"],
            "label": made["label"],
            "artifacts": made["artifacts"],
        }

    @app.post("/api/frames/auto")
    def frames_auto(payload: dict):
        """恢复自动选帧：清掉手选记录，重刮时按原来的采样策略挑最清晰的一帧。"""
        source = _pickable(str((payload or {}).get("path") or ""))
        store.set_frame_at(str(source), 0)
        log.info("已恢复自动选帧：%s", source.name)
        return {"ok": True,
                "message": "已取消手选，下次重刮会按采样自动挑帧（现有图片不变）"}

    # ---------- 日志 ----------

    @app.get("/api/logs")
    def logs(
        limit: int = Query(200, ge=1, le=1000),
        level: str = Query("DEBUG"),
        q: str = Query(""),
        since: int = Query(0, ge=0),
    ):
        entries = log_buffer.tail(limit=limit, min_level=level, keyword=q, since=since)
        return {"logs": entries, "last_id": log_buffer.last_id()}

    @app.get("/api/logs/file")
    def log_file_tail(limit: int = Query(300, ge=1, le=2000)):
        """读日志文件末尾，内存缓冲被刷掉之后还能查。"""
        return {
            "path": str(log_files.main) if log_files.main else "",
            "lines": tail_file(log_files.main, limit),
        }

    @app.get("/api/logs/download")
    def log_download(which: str = Query("main")):
        path = log_files.errors if which == "errors" else log_files.main
        if not path or not path.is_file():
            raise HTTPException(404, "日志文件不存在（可能 log.dir 未配置或还没写）")
        return FileResponse(path, media_type="text/plain; charset=utf-8",
                            filename=path.name)

    # ---------- 媒体服务器 ----------

    @app.get("/api/servers")
    def servers():
        return {"servers": pool.snapshot()}

    @app.post("/api/servers/test")
    def servers_test():
        if not pool.servers():
            return {"ok": False, "message": "没有启用中的媒体服务器，检查配置 servers 段"}
        results = pool.test_all(force=True)
        ok_count = sum(1 for item in results if item.get("ok"))
        return {
            "ok": ok_count == len(results),
            "message": f"{ok_count}/{len(results)} 台连接正常",
            "servers": results,
        }

    @app.post("/api/servers/scan")
    def servers_scan():
        if not pool.servers():
            return {"ok": False, "message": "没有启用中的媒体服务器"}
        results = pool.scan_all()
        return {"ok": True, "message": "已触发媒体库扫描", "servers": results}

    @app.post("/api/servers/refresh")
    def servers_refresh(limit: int = Query(1000, ge=1, le=5000)):
        """补齐缺图：把已刮削过的文件交给媒体服务器重建图片缓存。

        Emby 一般能自动发现新图片，Jellyfin 不会，必须显式刷新。刮削完成后
        如果界面上还是没海报，点这个就行，不用去后台逐个点。
        """
        if not pool.servers():
            return {"ok": False, "message": "没有启用中的媒体服务器"}
        if manual_refresh["running"]:
            return JSONResponse({"ok": False, "message": "上一轮补图还在进行"}, status_code=409)

        paths = store.paths(("ok", "skipped"), limit=limit)
        if not paths:
            return {"ok": False, "message": "还没有刮削记录，先跑一轮刮削"}
        manual_refresh.update({"running": True, "done": 0, "total": len(paths),
                               "message": "正在提交刷新请求"})
        log.info("手动补图：共 %s 个文件，开始提交刷新", len(paths))

        def worker():
            try:
                results = pool.refresh(paths)
                total = sum(item.get("refreshed", 0) for item in results)
                manual_refresh.update({"done": manual_refresh["total"],
                                       "message": f"已提交 {total} 条刷新请求"})
            except Exception as exc:  # noqa: BLE001
                log.exception("补图失败")
                manual_refresh.update({"message": f"失败：{exc}"})
            finally:
                manual_refresh["running"] = False

        threading.Thread(target=worker, daemon=True, name="ws-refresh").start()
        return {"ok": True, "message": f"已开始补图，共 {len(paths)} 个文件，进度看日志"}


    @app.get("/api/config")
    def show_config():
        """给前端展示当前生效配置，密钥做脱敏。"""
        data = cfg.as_dict()
        for server in data.get("servers", []):
            key = server.get("api_key") or ""
            server["api_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else (
                "已配置" if key else "")
        return data

    # ---------- 设置 ----------

    def _settings_payload() -> dict:
        poster = cfg.scrape.poster
        return {
            "ok": True,
            "config_file": str(cfg.source or DEFAULT_CONFIG_PATH),
            "config_exists": bool(cfg.source and Path(cfg.source).is_file()),
            "scan": {
                "roots": list(cfg.scan.roots),
                "recursive": cfg.scan.recursive,
                "concurrency": cfg.scan.concurrency,
                "timeout": cfg.scan.timeout,
                "probe_timeout": cfg.scan.probe_timeout,
                "mode": cfg.scan.mode,
                "autostart": cfg.scan.autostart,
                "interval_minutes": cfg.scan.interval_minutes,
                "schedule_mode": cfg.scan.schedule_mode,
            },
            "scrape": {
                "frames": cfg.scrape.frames,
                "poster_mode": poster.mode,
                "overwrite": cfg.scrape.overwrite,
                "jpeg_quality": cfg.scrape.jpeg_quality,
                "write_nfo": cfg.scrape.write_nfo,
                "write_poster": cfg.scrape.write_poster,
                "write_fanart": cfg.scrape.write_fanart,
                "thumb": cfg.scrape.thumb.enabled,
                "images": cfg.image_summary(),
            },
            "servers": [_server_public(server) for server in (cfg.servers or [])],
            "account": {
                "username": auth.username,
                "auth_enabled": cfg.web.auth.enabled,
                "session_hours": auth.session_hours,
                "default_password": auth.default_password,
            },
        }

    @app.get("/api/settings")
    def show_settings():
        return _settings_payload()

    # ---------- 自动化 ----------

    def _automation_payload(message: str = "") -> dict:
        status = sched.status()
        status["next_text"] = _when_text(status.get("next_run"))
        return {
            "ok": True,
            "message": message,
            "tasks": sched.tasks(),
            "roots": list(cfg.scan.roots),
            "status": status,
            "autostart": bool(cfg.scan.autostart),
        }

    @app.get("/api/automation")
    def automation_list():
        """自动化页：任务列表 + 可选的监控根目录 + 调度器状态。"""
        return _automation_payload()

    @app.get("/api/automation/browse")
    def automation_browse(path: str = Query("")):
        """新建任务时挑监控目录：只列目录，不列文件。"""
        roots = _roots()
        if not roots:
            return {"ok": False, "roots": [], "path": "", "parent": None, "dirs": [],
                    "message": "扫描目录都不存在，检查 docker volume 挂载"}
        if path.strip():
            current = _within_roots(path)
            if not current.is_dir():
                raise HTTPException(404, "目录不存在")
        else:
            current = roots[0]

        dirs: list[dict] = []
        try:
            for entry in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if _skip_dir(entry.name):
                    continue
                dirs.append({"name": entry.name, "path": str(entry)})
        except OSError as exc:
            raise HTTPException(500, f"读取目录失败：{exc}") from exc

        parent = None
        for root in roots:
            if current == root:
                break
            if root in current.parents:
                parent = str(current.parent)
                break
        return {
            "ok": True, "roots": [str(root) for root in roots],
            "path": str(current), "parent": parent, "dirs": dirs[:500],
            "inRoot": current in roots,
        }

    @app.post("/api/automation/save")
    def automation_save(payload: dict):
        """新建 / 修改一条自动化任务（id 为空就是新建）。"""
        try:
            task = sched.save(payload or {})
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        verb = "已新建" if not (payload or {}).get("id") else "已保存"
        return _automation_payload(f"{verb}「{task['name']}」，{task['interval_minutes']} 分钟一轮")

    @app.post("/api/automation/delete")
    def automation_delete(payload: dict):
        task_id = str((payload or {}).get("id") or "").strip()
        if not task_id:
            raise HTTPException(400, "缺少任务 id")
        if not sched.remove(task_id):
            raise HTTPException(404, "这条任务已经不在列表里了")
        return _automation_payload("任务已删除")

    @app.post("/api/automation/toggle")
    def automation_toggle(payload: dict):
        data = payload or {}
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise HTTPException(400, "缺少任务 id")
        try:
            task = sched.set_enabled(task_id, bool(data.get("enabled", True)))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _automation_payload(
            f"「{task['name']}」已{'启用' if task['enabled'] else '停用'}")

    @app.post("/api/automation/run")
    def automation_run(payload: dict):
        """不等定时点，马上按这条任务的设置跑一轮。"""
        task_id = str((payload or {}).get("id") or "").strip()
        if not task_id:
            raise HTTPException(400, "缺少任务 id")
        try:
            result = sched.run_now(task_id, "网页手动")
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        payload_out = _automation_payload(result.get("message", ""))
        payload_out.update({"started": bool(result.get("started")),
                            "ok": bool(result.get("ok"))})
        return payload_out

    @app.post("/api/automation/autostart")
    def automation_autostart(payload: dict):
        """容器启动后先跑一轮整库（和具体任务无关的全局开关）。"""
        enabled = bool((payload or {}).get("enabled"))
        cfg.scan.autostart = enabled
        note = "容器启动后会先跑一轮整库"
        try:
            cfg.save()
        except (OSError, RuntimeError, ValueError) as exc:
            note = f"本次运行已生效，但写配置文件失败：{exc}"
        log.info("开关「容器启动后先跑一轮」：%s", "开" if enabled else "关")
        return _automation_payload(note)

    @app.post("/api/settings")
    def save_settings(payload: dict):
        """保存设置：先逐项校验，全部通过才改内存、写回 yaml。

        任何一项不合法都整单拒绝，避免只改了一半的配置留在运行中。
        """
        data = payload or {}
        scan = data.get("scan") or {}
        scrape = data.get("scrape") or {}

        # ---- 扫描 ----
        if "roots" in scan:
            raw_roots = scan.get("roots")
            if isinstance(raw_roots, str):
                raw_roots = raw_roots.splitlines()
            cleaned: list[str] = []
            for item in raw_roots or []:
                text = str(item).strip()
                if not text:
                    continue
                path = Path(text)
                if not path.is_absolute():
                    raise HTTPException(400, f"媒体库目录要写容器内的绝对路径：{text}")
                cleaned.append(str(path))
            if not cleaned:
                raise HTTPException(400, "至少要保留一个媒体库目录")
            cfg.scan.roots = list(dict.fromkeys(cleaned))

        if "recursive" in scan:
            cfg.scan.recursive = bool(scan.get("recursive"))
        if "autostart" in scan:
            cfg.scan.autostart = bool(scan.get("autostart"))
        if "schedule_mode" in scan:
            mode = str(scan.get("schedule_mode") or "").lower()
            if mode not in ("incremental", "full"):
                raise HTTPException(400, "定时执行方式只能是 incremental / full")
            cfg.scan.schedule_mode = mode
        if "mode" in scan:
            mode = str(scan.get("mode") or "").lower()
            if mode not in ("auto", "full", "incremental", "manual"):
                raise HTTPException(400, "扫描模式只能是 auto / full / incremental / manual")
            cfg.scan.mode = mode
        for key, low, high, label in (
            ("concurrency", 1, 16, "并发数"),
            ("timeout", 30, 3600, "单个任务超时"),
            ("probe_timeout", 5, 600, "探测超时"),
            ("interval_minutes", 0, 10080, "定时间隔"),
        ):
            if key in scan:
                setattr(cfg.scan, key, _as_int(scan[key], low, high, label))

        # ---- 刮削 ----
        if "frames" in scrape:
            cfg.scrape.frames = _as_int(scrape["frames"], 1, 10, "采样帧数")
        prev_poster_mode = cfg.scrape.poster.mode
        if "poster_mode" in scrape:
            mode = str(scrape.get("poster_mode") or "").lower()
            if mode not in ("crop", "blur", "gradient"):
                raise HTTPException(400, "海报模式只能是 crop / blur / gradient")
            cfg.scrape.poster.mode = mode
            # 手动选过就不再被「旧默认值迁移」覆盖（见 Config.validate）
            cfg.scrape.poster.mode_explicit = True
        if "overwrite" in scrape:
            overwrite = str(scrape.get("overwrite") or "").lower()
            if overwrite not in ("missing", "always"):
                raise HTTPException(400, "覆盖策略只能是 missing / always")
            cfg.scrape.overwrite = overwrite
        if "jpeg_quality" in scrape:
            cfg.scrape.jpeg_quality = _as_int(scrape["jpeg_quality"], 60, 100, "JPEG 质量")
        for key in ("write_nfo", "write_poster", "write_fanart"):
            if key in scrape:
                setattr(cfg.scrape, key, bool(scrape[key]))
        if "thumb" in scrape:
            cfg.scrape.thumb.enabled = bool(scrape["thumb"])

        # ---- 媒体服务器 ----
        raw_servers = data.get("servers")
        if raw_servers is not None:
            if not isinstance(raw_servers, list):
                raise HTTPException(400, "servers 应该是列表")
            old = {server.name: server for server in (cfg.servers or [])}
            fresh: list[ServerConfig] = []
            seen: set[str] = set()
            for index, item in enumerate(raw_servers):
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("type") or "auto").strip().lower()
                if kind not in ("auto", "jellyfin", "emby"):
                    kind = "auto"
                name = str(item.get("name") or "").strip() or f"{kind}-{index + 1}"
                if name in seen:
                    name = f"{name}-{index + 1}"
                seen.add(name)
                url = str(item.get("url") or "").strip().rstrip("/")
                key = str(item.get("api_key") or "").strip()
                if not key and name in old:
                    key = old[name].api_key     # 留空表示沿用原来的密钥
                enabled = bool(item.get("enabled"))
                if enabled and (not url or not key):
                    raise HTTPException(400, f"「{name}」已启用，地址和 API 密钥都要填")
                if url and not url.lower().startswith(("http://", "https://")):
                    raise HTTPException(400, f"「{name}」的地址要以 http:// 或 https:// 开头")
                path_map = item.get("path_map") or {}
                if not isinstance(path_map, dict):
                    raise HTTPException(400, f"「{name}」的路径映射格式不对")
                fresh.append(ServerConfig(
                    name=name, type=kind, enabled=enabled, url=url, api_key=key,
                    timeout=_as_int(item.get("timeout") or 30, 5, 300, f"「{name}」的超时"),
                    path_map={str(k).strip(): str(v).strip()
                              for k, v in path_map.items() if str(k).strip()},
                    refresh_after=bool(item.get("refresh_after", True)),
                    verify_after=bool(item.get("verify_after", True)),
                    scan_after=bool(item.get("scan_after")),
                    insecure=bool(item.get("insecure")),
                ))
            cfg.servers = fresh

        notes = cfg.validate()
        pool.reset_cache()
        # 定时自动化按新配置重新计时，无需重启容器
        sched.reconfigure()

        try:
            target = cfg.save()
            log.info("设置已保存并生效：%s", target)
            message = "设置已保存并立即生效"
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("设置写回文件失败：%s", exc)
            message = f"设置已在本次运行生效，但写文件失败：{exc}"

        if cfg.scrape.poster.mode != prev_poster_mode:
            message += (
                f"；海报模式已改成 {cfg.scrape.poster.mode}，"
                "已生成的海报不会自动重画——到「文件管理」勾上要换的目录点「开始重刮」"
            )

        result = _settings_payload()
        result["schedule"] = sched.status()
        result["message"] = message
        result["notes"] = notes
        return result

    @app.get("/api/health")
    def health():
        return {"ok": True, "servers": len(pool.servers())}

    return app
