# -*- coding: utf-8 -*-
"""网页控制台：登录鉴权 + 进度看板 + 失败排查 + 媒体服务器联动 + 日志查看。"""
from __future__ import annotations

import logging
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import (COOKIE_NAME, AuthStore, LoginThrottle, validate_password,
                   validate_username)
from .config import Config
from .logs import LogFiles, RingLogHandler, tail_file
from .media import build_target, artifacts
from .mediaserver import ServerPool
from .state import StateStore
from .worker import Runner

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 不需要登录就能访问的路径
PUBLIC_PATHS = {"/login", "/api/login", "/api/auth-info", "/api/health", "/favicon.ico"}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(cfg: Config, store: StateStore, runner: Runner,
               log_buffer: RingLogHandler, log_files: LogFiles | None = None,
               pool: ServerPool | None = None,
               auth: AuthStore | None = None) -> FastAPI:
    app = FastAPI(title="strm-scraper", docs_url="/api/docs", redoc_url=None)
    pool = pool or ServerPool(cfg)
    log_files = log_files or LogFiles()
    auth = auth or AuthStore(cfg.web.auth.file, cfg.web.auth.session_hours)
    throttle = LoginThrottle()
    manual_refresh: dict = {"running": False, "done": 0, "total": 0, "message": ""}

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
        return FileResponse(page, media_type="text/html; charset=utf-8")

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
        return FileResponse(page, media_type="text/html; charset=utf-8")

    # ---------- 状态 ----------

    @app.get("/api/status")
    def status():
        snapshot = runner.snapshot()
        snapshot["servers"] = pool.snapshot()
        snapshot["roots"] = cfg.scan.roots
        snapshot["overwrite"] = cfg.scrape.overwrite
        snapshot["timestamp"] = int(time.time())
        snapshot["images"] = {
            "poster": [cfg.scrape.poster.width, cfg.scrape.poster.height],
            "fanart": [cfg.scrape.fanart.width, cfg.scrape.fanart.height],
            "thumb": [cfg.scrape.thumb.width, cfg.scrape.thumb.height]
            if cfg.scrape.thumb.enabled else None,
        }
        snapshot["log_file"] = str(log_files.main) if log_files.main else ""
        snapshot["refresh"] = manual_refresh
        return snapshot

    @app.get("/api/items")
    def items(
        status_filter: str = Query("all", alias="status"),
        keyword: str = Query("", alias="q"),
        page: int = Query(1, ge=1),
        size: int = Query(30, ge=1, le=200),
    ):
        rows = store.items(
            status=status_filter, keyword=keyword, limit=size, offset=(page - 1) * size
        )
        return {
            "page": page,
            "size": size,
            "total": store.count(status_filter) if not keyword else len(rows),
            "items": [
                {
                    "path": row.path,
                    "name": row.name,
                    "status": row.status,
                    "reason": row.reason,
                    "title": row.title,
                    "duration": round(row.duration, 1),
                    "attempts": row.attempts,
                    "updated": int(row.updated),
                    "artifacts": artifacts(build_target(Path(row.path))),
                }
                for row in rows
            ],
        }

    # ---------- 任务控制 ----------

    @app.post("/api/scan")
    def scan(full: bool = Query(False)):
        if runner.busy():
            return JSONResponse({"ok": False, "message": "任务正在运行中"}, status_code=409)
        started = runner.start(full=full)
        message = "已开始全量刮削" if full else "已开始增量刮削"
        return {"ok": started, "message": message if started else "任务已在运行"}

    @app.post("/api/stop")
    def stop():
        runner.stop()
        return {"ok": True, "message": "已发送停止请求，等待进行中的任务收尾"}

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

        import threading
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

    @app.get("/api/health")
    def health():
        return {"ok": True, "servers": len(pool.servers())}

    return app
