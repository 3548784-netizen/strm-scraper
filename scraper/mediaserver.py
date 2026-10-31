# -*- coding: utf-8 -*-
"""媒体服务器联动：Emby 与 Jellyfin 共用一套实现。

两者接口同源（``/Items``、``/Items/{id}/Refresh``、``/Library/Refresh``），
差别只在鉴权和刷新参数的传法。这里把这几个坑都封在客户端里：

1. **鉴权**：不同版本/打包方式对认证头的支持不一样。
   fnOS 打包的 Jellyfin 只认 ``Authorization: MediaBrowser Token="<key>"``，
   老版本 Emby 习惯 ``X-Emby-Token``，还有一部分只吃 ``?api_key=``。
   这里依次尝试，成功后记住哪种能用，后续请求直接用。

2. **刷新参数**：``POST /Items/{id}/Refresh`` 的参数在 Jellyfin 12.x 上
   **只认 JSON body**，写成 URL query 会返回 204 但什么都不做；Emby 反过来
   对 query 更稳。所以两边同时写：body 一份、query 一份，谁认哪个都能生效。

3. **路径**：容器内路径和媒体服务器看到的路径经常不一样，必须用
   ``path_map`` 做前缀替换，否则按路径永远匹配不上条目。
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PAGE_SIZE = 500
# 三种鉴权写法，按成功率排序
AUTH_STYLES = ("media_browser", "x_emby", "query")


class MediaServerError(RuntimeError):
    """接口调用失败，消息里带可读原因。"""


def _norm_path(path: str) -> str:
    """统一成正斜杠，Windows 反斜杠也能和服务端路径对上。"""
    return str(path).replace("\\", "/")


def _strip_strm(path: str) -> str:
    """去掉 .strm 后缀：有的版本记录的条目路径不带它。"""
    text = _norm_path(path)
    return text[:-5] if text.lower().endswith(".strm") else text


class _AuthFailed(Exception):
    """鉴权没通过，换下一种写法重试。"""


@dataclass
class ServerInfo:
    ok: bool
    kind: str = ""
    name: str = ""
    version: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "kind": self.kind, "name": self.name,
            "version": self.version, "detail": self.detail,
        }


class MediaServerClient:
    """一个 Emby / Jellyfin 实例。"""

    def __init__(self, url: str, api_key: str, kind: str = "auto",
                 timeout: int = 30, path_map: dict[str, str] | None = None,
                 insecure: bool = False):
        if not url:
            raise MediaServerError("未配置服务器地址")
        if not api_key:
            raise MediaServerError("未配置 API 密钥")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.path_map = path_map or {}
        self.insecure = insecure
        self.kind = kind if kind in ("jellyfin", "emby") else "auto"
        self.auth_style: str | None = None
        self._ssl = ssl._create_unverified_context() if insecure else None

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------

    def _do_request(self, style: str, path: str, method: str,
                    body: dict | None) -> tuple[int, bytes]:
        url = self.url + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if style == "media_browser":
            headers["Authorization"] = f'MediaBrowser Token="{self.api_key}"'
        elif style == "x_emby":
            headers["X-Emby-Token"] = self.api_key
        else:  # query
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{urllib.parse.urlencode({'api_key': self.api_key})}"

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")[:200]
            if exc.code in (401, 403):
                raise _AuthFailed(payload or str(exc.code)) from exc
            raise MediaServerError(f"{method} {path} 返回 {exc.code}：{payload}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if "certificate" in str(reason).lower():
                raise MediaServerError(
                    f"SSL 证书校验失败：{reason}（自签名证书可把 servers[].insecure 设为 true）"
                ) from exc
            raise MediaServerError(f"连不上 {self.url}：{reason}") from exc
        except TimeoutError as exc:
            raise MediaServerError(f"请求 {path} 超时（{self.timeout}s）") from exc

    def _request(self, path: str, method: str = "GET", body: dict | None = None):
        """按可用鉴权方式发请求，返回解析后的 JSON（可能为 None）。"""
        styles = [self.auth_style] if self.auth_style else list(AUTH_STYLES)
        last_error = ""
        for style in styles:
            try:
                _status, raw = self._do_request(style, path, method, body)
            except _AuthFailed as exc:
                last_error = str(exc)
                log.debug("鉴权方式 %s 被拒绝（%s），换下一种", style, last_error[:60])
                if self.auth_style == style:
                    self.auth_style = None
                continue
            if self.auth_style != style:
                self.auth_style = style
                log.info("媒体服务器鉴权通过，使用方式：%s", style)
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return None
        raise MediaServerError(
            f"鉴权失败：{self.url} 拒绝了 API 密钥（三种认证头都试过）。"
            f"检查密钥是否有效、是否有该库的访问权限。服务端返回：{last_error[:120]}"
        )

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------

    def detect_kind(self) -> str:
        """自动识别是 Emby 还是 Jellyfin（公开接口不需要密钥）。"""
        if self.kind in ("jellyfin", "emby"):
            return self.kind
        try:
            request = urllib.request.Request(
                self.url + "/System/Info/Public", headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(
                request, timeout=min(self.timeout, 10), context=self._ssl
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            product = str(payload.get("ProductName") or "")
            detected = "jellyfin" if "jellyfin" in product.lower() else "emby"
        except Exception:  # noqa: BLE001 - 识别不了就当 Jellyfin，接口是一样的
            detected = "jellyfin"
        self.kind = detected
        return detected

    def info(self) -> ServerInfo:
        """取服务器名和版本，并做一次连通性验证。"""
        try:
            data = self._request("/System/Info") or {}
        except MediaServerError as exc:
            return ServerInfo(ok=False, kind=self.detect_kind(), detail=str(exc))
        name = data.get("ServerName") or data.get("Name") or "未命名"
        version = data.get("Version") or "未知"
        kind = self.detect_kind()
        return ServerInfo(ok=True, kind=kind, name=name, version=version,
                          detail=f"{name} / {version}")

    def test(self) -> str:
        info = self.info()
        if not info.ok:
            raise MediaServerError(info.detail)
        return f"{info.detail}（{info.kind}）"

    # ------------------------------------------------------------------
    # 路径与条目
    # ------------------------------------------------------------------

    def map_path(self, local_path: str) -> str:
        """容器内路径 → 媒体服务器视角的路径。

        统一成 ``/`` 分隔再比较：Windows 上跑测试时路径是反斜杠，容器和
        Jellyfin 里都是正斜杠，不归一化就永远匹配不上。
        """
        local = _norm_path(local_path)
        for local_prefix, remote_prefix in self.path_map.items():
            prefix = _norm_path(str(local_prefix)).rstrip("/")
            if local == prefix or local.startswith(prefix + "/"):
                return _norm_path(str(remote_prefix)).rstrip("/") + local[len(prefix):]
        return local

    def path_index(self, with_images: bool = False) -> dict[str, dict]:
        """拉取全部视频条目，返回 path → {id, has_image}。

        键做了两种处理：原样 + 去掉 ``.strm`` 后缀，因为不同版本对 strm 条目
        记录的 Path 可能带后缀也可能不带，两种都能查到更稳。
        """
        fields = "Path,ImageTags" if with_images else "Path"
        index: dict[str, dict] = {}
        start = 0
        while True:
            query = urllib.parse.urlencode({
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode,Video,MusicVideo",
                "Fields": fields,
                "EnableImages": "false",
                "Limit": PAGE_SIZE,
                "StartIndex": start,
            })
            data = self._request(f"/Items?{query}") or {}
            items = data.get("Items") or []
            for item in items:
                path = item.get("Path")
                if not path:
                    continue
                entry = {
                    "id": item.get("Id"),
                    "has_image": bool((item.get("ImageTags") or {}).get("Primary")),
                }
                index[_norm_path(path)] = entry
                index.setdefault(_strip_strm(path), entry)
            if len(items) < PAGE_SIZE:
                break
            start += PAGE_SIZE
        log.info("媒体服务器索引完成，共 %s 个视频条目", len(index))
        return index

    def refresh_item(self, item_id: str) -> None:
        """让条目重新检查一遍图片，有就更新、没有就补上。

        **不能带 ``ReplaceAllImages``，也不能用 ``ImageRefreshMode=FullRefresh``**
        —— 那等于在界面上勾「替换所有图像」：服务器会先把条目的图片删掉
        （删的就是媒体目录里 ``<名字>-poster.jpg`` / ``-fanart.jpg`` 这些
        文件本身），再去图像源重新抓一份。刮削器的图就是这么被清空的，
        尤其是刚刮完、图片刚写进去的时候。

        这里只要求「重新校验图片」：本地已经有图就直接用上，没有才去补。
        body 和 query 各写一份：Jellyfin 12.x 只认 body，Emby 对 query 更稳，
        两边都给才不会有一边静默失败。
        """
        query = urllib.parse.urlencode({
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        })
        body = {
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": False,
            "ReplaceAllImages": False,
        }
        self._request(f"/Items/{item_id}/Refresh?{query}", "POST", body)

    def scan_library(self) -> None:
        """触发一次全库扫描（新文件、新图片会被重新识别）。"""
        self._request("/Library/Refresh", "POST")

    # ------------------------------------------------------------------
    # 批量操作
    # ------------------------------------------------------------------

    def refresh_paths(self, local_paths: list[str], pause: float = 0.0) -> dict:
        """按本地路径批量刷新图片。"""
        if not local_paths:
            return {"matched": 0, "refreshed": 0, "missing": [], "failed": []}

        index = self.path_index()
        matched = refreshed = 0
        missing: list[str] = []
        failed: list[str] = []

        for local_path in local_paths:
            remote = self.map_path(str(local_path))
            entry = index.get(remote) or index.get(_strip_strm(remote))
            if not entry:
                missing.append(remote)
                continue
            matched += 1
            try:
                self.refresh_item(entry["id"])
                refreshed += 1
            except MediaServerError as exc:
                failed.append(f"{Path(remote).name}: {exc}")
                log.warning("刷新失败：%s", exc)
            if pause:
                time.sleep(pause)

        if missing:
            log.warning(
                "%s 个文件在媒体服务器里找不到对应条目。通常是 path_map 没配："
                "容器里看到的是 %s，而服务端记录的是另一种前缀", len(missing), missing[0],
            )
            sample = next(iter(index), "")
            if sample:
                log.info("对照：映射后应为 %s，而服务端实际记录形如 %s", missing[0], sample)
        return {
            "matched": matched, "refreshed": refreshed,
            "missing": missing[:50], "failed": failed[:20],
        }

    def audit_images(self, local_paths: list[str]) -> dict:
        """检查这些文件在媒体服务器里到底有没有图，用于刷新后自检。"""
        if not local_paths:
            return {"checked": 0, "without_image": [], "unknown": []}
        index = self.path_index(with_images=True)
        checked = 0
        without: list[str] = []
        unknown: list[str] = []
        for local_path in local_paths:
            remote = self.map_path(str(local_path))
            entry = index.get(remote) or index.get(_strip_strm(remote))
            if not entry:
                unknown.append(remote)
                continue
            checked += 1
            if not entry["has_image"]:
                without.append(remote)
        return {"checked": checked, "without_image": without[:50], "unknown": unknown[:50]}


def build_client(server_cfg) -> MediaServerClient:
    """从配置对象建客户端。"""
    return MediaServerClient(
        server_cfg.url, server_cfg.api_key, server_cfg.type,
        server_cfg.timeout, server_cfg.path_map, getattr(server_cfg, "insecure", False),
    )


class ServerPool:
    """管理配置里的多台 Emby / Jellyfin。

    网页上要频繁读连接状态，所以测试结果缓存 60 秒，避免每次轮询都去打接口。
    """

    def __init__(self, cfg, cache_seconds: float = 60.0):
        self.cfg = cfg
        self.cache_seconds = cache_seconds
        self._clients: dict[str, MediaServerClient] = {}
        self._status: dict[str, tuple[float, ServerInfo]] = {}

    # ---------- 基础 ----------

    def servers(self) -> list:
        return [s for s in self.cfg.servers if s.enabled]

    def reset_cache(self) -> None:
        """改过设置（地址、密钥、路径映射）后把客户端和连通状态都丢掉重来。"""
        self._clients.clear()
        self._status.clear()

    def client(self, server) -> MediaServerClient:
        key = f"{server.name}|{server.url}|{server.api_key[:6]}"
        if key not in self._clients:
            self._clients[key] = build_client(server)
        return self._clients[key]

    def test(self, server, force: bool = False) -> ServerInfo:
        now = time.time()
        cached = self._status.get(server.name)
        if not force and cached and now - cached[0] < self.cache_seconds:
            return cached[1]
        try:
            info = self.client(server).info()
        except MediaServerError as exc:
            info = ServerInfo(ok=False, kind=server.type, detail=str(exc))
        self._status[server.name] = (now, info)
        return info

    def test_all(self, force: bool = False) -> list[dict]:
        results = []
        for server in self.servers():
            info = self.test(server, force=force)
            if info.ok:
                log.info("媒体服务器 %s 连接正常：%s（%s）", server.name, info.detail, info.kind)
            else:
                log.error("媒体服务器 %s 连接失败：%s", server.name, info.detail)
            results.append({
                "name": server.name,
                "server_name": info.name,
                "type": info.kind or server.type,
                "url": server.url,
                "ok": info.ok,
                "kind": info.kind,
                "version": info.version,
                "detail": info.detail,
            })
        return results

    def snapshot(self) -> list[dict]:
        """给状态接口用的精简信息（带缓存的连通状态）。"""
        rows = []
        for server in self.servers():
            info = self.test(server)
            rows.append({
                "name": server.name,
                "type": info.kind or server.type,
                "url": server.url,
                "ok": info.ok,
                "version": info.version,
                "detail": info.detail,
                "refresh_after": server.refresh_after,
                "path_map": server.path_map,
            })
        return rows

    # ---------- 动作 ----------

    def refresh(self, local_paths: list[str]) -> list[dict]:
        """所有服务器刷新一批路径的图片，并按需回查结果。"""
        results = []
        for server in self.servers():
            if not server.refresh_after:
                continue
            client = self.client(server)
            try:
                result = client.refresh_paths(local_paths)
            except MediaServerError as exc:
                log.error("媒体服务器 %s 刷新失败：%s", server.name, exc)
                results.append({"name": server.name, "ok": False, "message": str(exc)})
                continue

            message = (f"匹配 {result['matched']} 条，已刷新 {result['refreshed']} 条"
                       f"（提交 {len(local_paths)} 条）")
            log.info("媒体服务器 %s：%s", server.name, message)

            verified = None
            if server.verify_after and result["refreshed"]:
                # 刷新是异步的，给服务端几百毫秒到几秒的处理时间再回查
                time.sleep(min(5.0, 1.0 + result["refreshed"] * 0.05))
                try:
                    verified = client.audit_images(local_paths)
                except MediaServerError as exc:
                    log.warning("%s 回查失败：%s", server.name, exc)
                if verified:
                    missing = verified["without_image"]
                    if missing:
                        log.warning(
                            "%s 回查：%s 条仍无海报。若是刚刮完，稍等几分钟让它建索引；"
                            "持续如此请看日志里的文件名：%s",
                            server.name, len(missing),
                            ", ".join(Path(p).name for p in missing[:3]),
                        )
                    else:
                        log.info("%s 回查：%s 条全部已有海报",
                                 server.name, verified["checked"])

            if server.scan_after:
                try:
                    client.scan_library()
                    log.info("媒体服务器 %s：已触发媒体库扫描", server.name)
                except MediaServerError as exc:
                    log.warning("%s 触发扫描失败：%s", server.name, exc)

            results.append({
                "name": server.name, "ok": True, "message": message,
                "matched": result["matched"], "refreshed": result["refreshed"],
                "missing": result["missing"], "failed": result["failed"],
                "verify": verified,
            })
        return results

    def scan_all(self) -> list[dict]:
        results = []
        for server in self.servers():
            try:
                self.client(server).scan_library()
                log.info("媒体服务器 %s：已触发媒体库扫描", server.name)
                results.append({"name": server.name, "ok": True,
                                "message": "已触发媒体库扫描"})
            except MediaServerError as exc:
                log.error("媒体服务器 %s 扫描失败：%s", server.name, exc)
                results.append({"name": server.name, "ok": False, "message": str(exc)})
        return results
