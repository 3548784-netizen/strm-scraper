# -*- coding: utf-8 -*-
"""网页控制台：登录鉴权 + 进度看板 + 失败排查 + 媒体服务器联动 + 日志查看。"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import (COOKIE_NAME, AuthStore, LoginThrottle, validate_password,
                   validate_username)
from . import frames, probe
from .config import Config
from .logs import LogFiles, RingLogHandler, tail_file
from .media import IGNORED_NAMES, artifacts, build_target, signature
from .mediaserver import ServerPool
from .nfo import display_title
from .picker import FramePicker
from .state import StateStore
from .worker import Runner

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 不需要登录就能访问的路径
PUBLIC_PATHS = {"/login", "/api/login", "/api/auth-info", "/api/health", "/favicon.ico"}

_KIND_NAMES = {"poster": "海报", "fanart": "背景图", "thumb": "缩略图"}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _count_strm(directory: Path, wanted: set[str]) -> int:
    """数一下这个目录里直接有几个 strm，文件管理页用来提示。"""
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and Path(entry.name).suffix.lower() in wanted:
                        count += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return count


def create_app(cfg: Config, store: StateStore, runner: Runner,
               log_buffer: RingLogHandler, log_files: LogFiles | None = None,
               pool: ServerPool | None = None,
               auth: AuthStore | None = None) -> FastAPI:
    app = FastAPI(title="strm-scraper", docs_url="/api/docs", redoc_url=None)
    pool = pool or ServerPool(cfg)
    log_files = log_files or LogFiles()
    auth = auth or AuthStore(cfg.web.auth.file, cfg.web.auth.session_hours)
    throttle = LoginThrottle()
    # 选帧缓存放在 state.db 旁边，跟着 /config 一起持久化
    picker = FramePicker(cfg, Path(cfg.storage.state_db).parent / "framecache")
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
            "total": store.count(status_filter, keyword),
            "items": [
                {
                    "path": row.path,
                    "name": row.name,
                    "size": row.size,
                    "status": row.status,
                    "reason": row.reason,
                    "title": row.title,
                    "duration": round(row.duration, 1),
                    "attempts": row.attempts,
                    "updated": int(row.updated),
                    "frame_at": round(row.frame_at, 2),
                    "artifacts": artifacts(build_target(Path(row.path))),
                }
                for row in rows
            ],
        }

    # ---------- 任务控制 ----------

    @app.post("/api/task/start")
    def task_start(full: bool = Query(False)):
        if runner.busy():
            return JSONResponse({"ok": False, "message": "任务正在运行，先暂停或取消"},
                                status_code=409)
        started = runner.start(full=full)
        message = "已开始全量重刮" if full else "已开始刮削（增量）"
        return {"ok": started, "message": message if started else "任务已在运行"}

    @app.post("/api/task/pause")
    def task_pause():
        if runner.pause():
            return {"ok": True, "message": "已暂停，进行中的任务会跑完后停住"}
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

    def _skip_dir(name: str) -> bool:
        return name.startswith(".") or name in (
            "@eaDir", "__pycache__", "node_modules", "$RECYCLE.BIN", "#recycle",
        )

    @app.get("/api/files")
    def list_files(path: str = Query(""), q: str = Query(""),
                   limit: int = Query(500, ge=1, le=2000)):
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
        keyword = q.strip().lower()
        dirs: list[dict] = []
        file_rows: list[dict] = []
        truncated = False

        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            raise HTTPException(500, f"读取目录失败：{exc}") from exc

        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if _skip_dir(name):
                    continue
                if keyword and keyword not in name.lower():
                    continue
                dirs.append({"name": name, "path": str(entry), "count": _count_strm(entry, wanted)})
                continue

            if name.lower() in IGNORED_NAMES or entry.suffix.lower() not in wanted:
                continue
            if keyword and keyword not in name.lower():
                continue
            if len(file_rows) >= limit:
                truncated = True
                break
            try:
                stat = entry.stat()
                size, mtime = stat.st_size, round(stat.st_mtime, 1)
            except OSError:
                size, mtime = 0, 0.0
            file_rows.append({
                "path": str(entry), "name": name, "size": size, "mtime": mtime,
            })

        status_map = store.get_many(row["path"] for row in file_rows)
        for row in file_rows:
            item = status_map.get(row["path"])
            row["status"] = item.status if item else "new"
            row["reason"] = item.reason if item else ""
            row["title"] = item.title if item else ""
            row["duration"] = round(item.duration, 1) if item else 0.0
            row["frame_at"] = round(item.frame_at, 2) if item else 0.0
            row["artifacts"] = artifacts(build_target(Path(row["path"])))

        return {
            "ok": True, "path": str(current), "parent": parent,
            "roots": [str(root) for root in roots], "breadcrumb": breadcrumb,
            "dirs": dirs, "files": file_rows, "truncated": truncated,
        }

    @app.get("/api/files/image")
    def file_image(path: str = Query(...), kind: str = Query("poster")):
        source = _within_roots(path)
        target = build_target(source)
        picked = {"poster": target.poster, "fanart": target.fanart,
                  "thumb": target.thumb}.get(kind)
        if not picked or not picked.is_file():
            raise HTTPException(404, "图片还没生成")
        return FileResponse(picked, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=60"})

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
        data = payload or {}
        paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
        if not paths:
            raise HTTPException(400, "没有选中文件")
        queued: list[str] = []
        for raw in paths:
            try:
                source = _within_roots(raw)
            except HTTPException:
                continue
            if source.is_file():
                store.reset_one(str(source))
                queued.append(str(source))
        if not queued:
            raise HTTPException(400, "没有可刮削的文件")

        if runner.busy():
            paused = runner.snapshot().get("paused")
            tail = "任务已暂停，点「继续任务」后处理" if paused else "当前任务会顺带处理"
            return {"ok": True, "started": False,
                    "message": f"已加入队列 {len(queued)} 个，{tail}"}
        runner.start(full=False)
        return {"ok": True, "started": True,
                "message": f"已开始刮削 {len(queued)} 个文件"}

    # ---------- 手动选帧 ----------

    def _pickable(raw: str) -> Path:
        source = _within_roots(raw)
        if not source.is_file():
            raise HTTPException(404, "文件不存在")
        return source

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
        })
        return data

    @app.get("/api/frames/frame")
    def frames_frame(path: str = Query(...), t: float = Query(0, ge=0),
                     width: int = Query(0, ge=0, le=1920)):
        """按时间点抓一帧返回 JPEG；同参数第二次直接走缓存。"""
        source = _pickable(path)
        try:
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

        target = build_target(source)
        item = store.get(str(source))
        if item is None:
            store.ensure([(str(source), *signature(source))])
        try:
            made = picker.apply(target, seconds, kinds)
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
        return {
            "ok": True,
            "message": f"已用 {made['label']} 这一帧生成"
                       f"{'、'.join(_KIND_NAMES[k] for k in kinds)}"
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

    @app.get("/api/health")
    def health():
        return {"ok": True, "servers": len(pool.servers())}

    return app
