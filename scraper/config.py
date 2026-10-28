# -*- coding: utf-8 -*-
"""配置加载：config.yaml + 环境变量覆盖。

环境变量统一加 ``WS_`` 前缀，嵌套字段用双下划线连接，例如::

    WS_SCAN__CONCURRENCY=4
    WS_WEB__PORT=9000
    WS_SERVERS__0__URL=http://192.168.1.10:8096

值的类型按 YAML 规则推断（true/false/数字/列表用逗号分隔）。

``servers`` 是列表，环境变量不好表达多台服务器，建议直接写在 yaml 里。
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_origin, get_type_hints

from . import specs

try:
    import yaml
except ImportError:  # pragma: no cover - yaml 是硬依赖
    yaml = None

log = logging.getLogger(__name__)


DEFAULTS: dict[str, Any] = {
    "scan": {
        "roots": ["/media"],
        "recursive": True,
        "extensions": [".strm"],
        # auto: 首次跑全量，之后只补缺失；full: 每次都重刮；incremental: 只补缺失
        # manual: 不自动跑，只在网页上手动触发
        "mode": "manual",
        # 启动容器后是否自动开跑（默认关，打开网页点「开始任务」才跑）
        "autostart": False,
        # 定时扫描间隔（分钟），0 表示只在网页上手动触发
        "interval_minutes": 0,
        "concurrency": 3,
        # 单个视频从探测到出图的整体超时（秒）
        "timeout": 240,
        # 探测阶段的网络超时（秒），strm 指向的地址可能已经失效
        "probe_timeout": 20,
    },
    "scrape": {
        "frames": 5,
        "frame_positions": [0.15, 0.30, 0.45, 0.60, 0.75],
        "min_width": 320,
        # 2:3 竖版。Emby / Jellyfin 推荐 1000×1500，最小 500×750
        "poster": {"width": 1000, "height": 1500, "mode": "blur"},
        # 16:9 横版背景图，两家都推荐 1920×1080
        "fanart": {"width": 1920, "height": 1080},
        # 16:9 缩略图，写 <名称>-thumb.jpg，Emby 的列表视图会用
        "thumb": {"enabled": False, "width": 1000, "height": 562},
        "write_nfo": True,
        "write_poster": True,
        "write_fanart": True,
        # missing: 只在缺失时生成；always: 覆盖已有文件
        "overwrite": "missing",
        "jpeg_quality": 92,
    },
    # Emby / Jellyfin 可以配多台，刮完自动刷新各自的图片缓存
    "servers": [],
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 6666,
        "auth": {
            # 关掉的话谁都能直接打开控制台，只建议在内网或反代后面这么用
            "enabled": True,
            # 登录状态保持多少小时，默认 7 天
            "session_hours": 168,
            # 账号文件，密码是加盐哈希，删掉这个文件会重置成 admin / admin
            "file": "/config/auth.json",
        },
    },
    "storage": {"state_db": "/config/state.db"},
    "log": {
        "level": "INFO",
        # 日志目录，留空则只输出到控制台
        "dir": "/config/logs",
        "file": "strm-scraper.log",
        # 单个文件上限（MB）与保留份数
        "max_mb": 5,
        "backups": 5,
        "console": True,
    },
}


def _coerce(raw: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    low = raw.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("", "null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 覆盖 base。"""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(cfg: dict, prefix: str = "WS_") -> dict:
    """把 WS_A__B=v 形式的环境变量写进配置。"""
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        cursor = cfg
        for part in path[:-1]:
            if isinstance(cursor, dict):
                if not isinstance(cursor.get(part), dict):
                    cursor[part] = {}
                cursor = cursor[part]
            elif isinstance(cursor, list) and part.isdigit():
                index = int(part)
                while len(cursor) <= index:
                    cursor.append({})
                if not isinstance(cursor[index], dict):
                    cursor[index] = {}
                cursor = cursor[index]
            else:
                break
        else:
            cursor[path[-1]] = _coerce(value)
    return cfg


@dataclass
class ScanConfig:
    roots: list[str] = field(default_factory=lambda: ["/media"])
    recursive: bool = True
    extensions: list[str] = field(default_factory=lambda: [".strm"])
    mode: str = "manual"
    autostart: bool = False
    interval_minutes: int = 0
    concurrency: int = 3
    timeout: int = 240
    probe_timeout: int = 20


@dataclass
class PosterConfig:
    width: int = 1000
    height: int = 1500
    mode: str = "blur"


@dataclass
class FanartConfig:
    width: int = 1920
    height: int = 1080


@dataclass
class ThumbConfig:
    enabled: bool = False
    width: int = 1000
    height: int = 562


@dataclass
class ScrapeConfig:
    frames: int = 5
    frame_positions: list[float] = field(default_factory=lambda: [0.15, 0.30, 0.45, 0.60, 0.75])
    min_width: int = 320
    poster: PosterConfig = field(default_factory=PosterConfig)
    fanart: FanartConfig = field(default_factory=FanartConfig)
    thumb: ThumbConfig = field(default_factory=ThumbConfig)
    write_nfo: bool = True
    write_poster: bool = True
    write_fanart: bool = True
    overwrite: str = "missing"
    jpeg_quality: int = 92


@dataclass
class ServerConfig:
    """一台 Emby 或 Jellyfin。"""

    name: str = ""
    # jellyfin / emby / auto（自动识别）
    type: str = "auto"
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    timeout: int = 30
    # 容器内路径 → 媒体服务器看到的路径
    path_map: dict[str, str] = field(default_factory=dict)
    # 刮削完成后自动重建图片缓存
    refresh_after: bool = True
    # 刷新后回查一遍，确认图真的被识别（问题早发现）
    verify_after: bool = True
    # 刮削完成后让服务器扫库（新文件才会被登记）
    scan_after: bool = False
    # 自签名 https 证书
    insecure: bool = False

    def label(self) -> str:
        return self.name or f"{self.type}-{self.url or '未配置'}"


@dataclass
class AuthConfig:
    """网页控制台的登录鉴权。"""

    enabled: bool = True
    # 登录状态保持时长（小时）
    session_hours: int = 168
    # 账号文件（密码加盐哈希，不含明文）
    file: str = "/config/auth.json"


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 6666
    auth: AuthConfig = field(default_factory=AuthConfig)


@dataclass
class StorageConfig:
    state_db: str = "/config/state.db"


@dataclass
class LogConfig:
    level: str = "INFO"
    dir: str = "/config/logs"
    file: str = "strm-scraper.log"
    max_mb: float = 5
    backups: int = 5
    console: bool = True


@dataclass
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    servers: list[ServerConfig] = field(default_factory=list)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        raw = copy.deepcopy(DEFAULTS)

        candidate = Path(path) if path else _find_config_file()
        if candidate and candidate.is_file():
            if yaml is None:
                raise RuntimeError("读取配置文件需要 PyYAML：pip install pyyaml")
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件格式不对，顶层应该是字典：{candidate}")
            # 老版本用的是单个 jellyfin 段，自动转成 servers 列表
            legacy = loaded.pop("jellyfin", None)
            raw = _deep_merge(raw, loaded)
            if legacy and not raw.get("servers"):
                raw["servers"] = [_legacy_server(legacy)]

        raw = _apply_env(raw)
        cfg = _from_dict(cls, raw)
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """校正明显不对的配置，返回需要提示的条目。"""
        notes: list[str] = []

        for kind, holder in (
            ("poster", self.scrape.poster),
            ("fanart", self.scrape.fanart),
            ("thumb", self.scrape.thumb),
        ):
            width, height, note = specs.conform(holder.width, holder.height, kind)
            holder.width, holder.height = width, height
            if note:
                notes.append(note)

        if self.scrape.poster.mode not in ("blur", "gradient", "crop"):
            notes.append(
                f"poster.mode={self.scrape.poster.mode} 不认识（可选 blur/gradient/crop），"
                "已回退为 blur"
            )
            self.scrape.poster.mode = "blur"

        if self.scrape.overwrite not in ("missing", "always"):
            notes.append(f"overwrite={self.scrape.overwrite} 不认识，已回退为 missing")
            self.scrape.overwrite = "missing"

        if not self.scan.roots:
            notes.append("scan.roots 为空，已改成 /media")
            self.scan.roots = ["/media"]
        if not self.scan.extensions:
            notes.append("scan.extensions 为空，已改成 .strm")
            self.scan.extensions = [".strm"]
        if not self.scrape.frame_positions:
            self.scrape.frame_positions = [0.15, 0.30, 0.45, 0.60, 0.75]

        mode = str(self.scan.mode).lower()
        if mode not in ("auto", "full", "incremental", "manual"):
            notes.append(f"scan.mode={self.scan.mode} 不认识，已回退为 auto")
            self.scan.mode = "auto"

        if self.scan.concurrency < 1:
            notes.append("scan.concurrency 小于 1，已改为 1")
            self.scan.concurrency = 1

        if not 1 <= self.web.port <= 65535:
            notes.append(f"web.port={self.web.port} 不合法，已改成 6666")
            self.web.port = 6666

        hours = self.web.auth.session_hours
        if not isinstance(hours, int) or not 1 <= hours <= 24 * 365:
            notes.append(f"web.auth.session_hours={hours} 超出 1–8760，已改成 168")
            self.web.auth.session_hours = 168
        if not str(self.web.auth.file or "").strip():
            notes.append("web.auth.file 为空，已改成 /config/auth.json")
            self.web.auth.file = "/config/auth.json"

        # 服务器：补齐名字、去重、校验必填项
        seen: set[str] = set()
        cleaned: list[ServerConfig] = []
        for index, server in enumerate(self.servers or []):
            if not isinstance(server, ServerConfig):  # 防御：手改坏了的条目直接跳过
                log.warning("servers[%s] 不是有效配置，已忽略", index)
                continue
            if not server.name:
                server.name = f"{server.type or 'server'}-{index + 1}"
            if server.name in seen:
                server.name = f"{server.name}-{index + 1}"
            seen.add(server.name)

            if server.type not in ("jellyfin", "emby", "auto"):
                notes.append(f"servers[{server.name}].type={server.type} 不认识，已改为 auto")
                server.type = "auto"

            if server.enabled and (not server.url or not server.api_key):
                notes.append(f"servers[{server.name}] 已启用但缺少 url 或 api_key，本轮跳过")
                server.enabled = False
            cleaned.append(server)
        self.servers = cleaned

        for note in notes:
            log.warning("配置校正：%s", note)
        return notes

    def enabled_servers(self) -> list[ServerConfig]:
        return [server for server in (self.servers or []) if server.enabled]

    def as_dict(self) -> dict[str, Any]:
        return _to_dict(self)

    def describe(self) -> str:
        """给日志用的一行概要。"""
        roots = ", ".join(self.scan.roots)
        servers = ", ".join(
            f"{s.name}({s.type})" for s in self.enabled_servers()
        ) or "无"
        return (
            f"roots=[{roots}] mode={self.scan.mode} "
            f"autostart={'on' if self.scan.autostart else 'off'} "
            f"concurrency={self.scan.concurrency} "
            f"interval={self.scan.interval_minutes}min web={self.web.host}:{self.web.port} "
            f"auth={'on' if self.web.auth.enabled else 'off'}"
            f"({self.web.auth.session_hours}h) "
            f"servers=[{servers}] log={self.log.dir or '仅控制台'}"
        )

    def image_summary(self) -> str:
        """海报与背景图的最终规格，启动时打一行，方便核对。"""
        poster = self.scrape.poster
        fanart = self.scrape.fanart
        thumb = self.scrape.thumb
        parts = [f"poster {poster.width}×{poster.height}（2:3）",
                 f"fanart {fanart.width}×{fanart.height}（16:9）"]
        if thumb.enabled:
            parts.append(f"thumb {thumb.width}×{thumb.height}（16:9）")
        return "，".join(parts)


def _legacy_server(legacy: dict) -> dict:
    """把旧的 ``jellyfin:`` 段转成 servers 列表里的一项。"""
    if not isinstance(legacy, dict):
        return {"name": "jellyfin", "type": "jellyfin", "enabled": False}
    server = {
        "name": "jellyfin",
        "type": "jellyfin",
        "enabled": bool(legacy.get("enabled")),
        "url": legacy.get("url", ""),
        "api_key": legacy.get("api_key", ""),
        "timeout": legacy.get("timeout", 30),
        "path_map": legacy.get("path_map") or {},
        "refresh_after": legacy.get("refresh_after", True),
    }
    log.info("检测到旧版 jellyfin 配置，已自动转成 servers 列表（建议改用 servers:）")
    return server


def _find_config_file() -> Path | None:
    """按优先级找配置文件：$WS_CONFIG → ./config.yaml → /config/config.yaml。"""
    env = os.environ.get("WS_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    for candidate in (Path("config.yaml"), Path("/config/config.yaml")):
        if candidate.is_file():
            return candidate
    return None


def _from_dict(cls, data: dict) -> Any:
    """把嵌套字典递归塞进 dataclass。

    注意：模块启用了 ``from __future__ import annotations``，dataclass 的
    ``f.type`` 会是字符串而不是类型对象，必须用 ``get_type_hints`` 还原，
    否则嵌套配置会被当成普通 dict。
    """
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name, f.type)

        # YAML 里写 ``servers:`` 或 ``roots:`` 后面只跟注释时会被解析成 None。
        # 这种情况当作“没写”，直接用 dataclass 的默认值，不能让 None 流进去。
        if value is None:
            continue

        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _from_dict(ftype, value)
        elif f.name == "servers":
            if not isinstance(value, list):
                value = [value]
            items: list[ServerConfig] = []
            for item in value:
                if isinstance(item, dict):
                    items.append(_from_dict(ServerConfig, item))
                elif isinstance(item, ServerConfig):
                    items.append(item)
                else:
                    log.warning("servers 里有一项不是字典，已忽略：%r", item)
            kwargs[f.name] = items
        elif get_origin(ftype) is list and not isinstance(value, list):
            # roots: /media 这种单值写法也接受
            kwargs[f.name] = [value]
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def _to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    return obj
