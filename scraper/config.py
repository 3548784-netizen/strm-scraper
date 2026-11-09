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
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_origin, get_type_hints

from . import specs
from .media import SUPPORTED_EXTENSIONS

try:
    import yaml
except ImportError:  # pragma: no cover - yaml 是硬依赖
    yaml = None

log = logging.getLogger(__name__)

# 没有显式指定、也找不到已有配置文件时，网页「设置」保存到这里
DEFAULT_CONFIG_PATH = Path("/config/config.yaml")


DEFAULTS: dict[str, Any] = {
    "scan": {
        "roots": ["/media"],
        "recursive": True,
        # 支持的格式全开：strm 和本地 mp4/mkv/avi 等放进媒体库就直接刮，
        # 不用再回来改这里。要加冷门容器（.iso / .vob）往列表里补一条即可。
        "extensions": list(SUPPORTED_EXTENSIONS),
        # auto: 首次跑全量，之后只补缺失；full: 每次都重刮；incremental: 只补缺失
        # manual: 不自动跑，只在网页上手动触发
        "mode": "manual",
        # 启动容器后是否自动开跑（默认关，打开网页点「开始任务」才跑）
        "autostart": False,
        # 定时扫描间隔（分钟），0 表示只在网页上手动触发
        "interval_minutes": 0,
        # 定时到点跑哪种：incremental 只补缺失 / full 全量重刮覆盖
        "schedule_mode": "incremental",
        "concurrency": 3,
        # 单个时间点抓一帧的超时（秒）。网盘源上超过 30 秒基本就是被限流或
        # 链接废了，再等下去只会拖住整个队列
        "timeout": 30,
        # 探测阶段的网络超时（秒），strm 指向的地址可能已经失效
        "probe_timeout": 8,
    },
    "scrape": {
        "frames": 2,
        "frame_positions": [0.15, 0.30, 0.45, 0.60, 0.75],
        "min_width": 320,
        # 2:3 竖版。Emby / Jellyfin 推荐 1000×1500，最小 500×750
        "poster": {"width": 1000, "height": 1500, "mode": "crop"},
        # 16:9 横版背景图，两家都推荐 1920×1080
        "fanart": {"width": 1920, "height": 1080},
        # 16:9 缩略图，写 <名称>-thumb.jpg，Emby 的列表视图直接用；默认开启
        "thumb": {"enabled": True, "width": 1000, "height": 562},
        "write_nfo": True,
        "write_poster": True,
        "write_fanart": True,
        # missing: 只在缺失时生成；always: 覆盖已有文件
        "overwrite": "missing",
        "jpeg_quality": 92,
    },
    # 115 网盘直链的连接台账与请求节奏。网盘按「瞬时并发连接数」掐人，
    # 短时间涌入几千个 strm 时不能让批量队列、网页选帧、单条刮削各跑各的。
    # 详见 docs/115-links.md。
    "links": {
        "enabled": True,
        # 全进程同时发往网盘的请求上限（ffprobe / ffmpeg 都算）
        "max_inflight": 2,
        # 同一个域名/中转服务同时几路
        "per_host_inflight": 2,
        # 令牌桶：每分钟最多发起多少次新请求，突发额度 burst
        "rate_per_minute": 60,
        "burst": 2,
        # 两次发起请求之间的最小间隔（毫秒）
        "min_gap_ms": 300,
        # 排队等待槽位的上限（0 = 不限）；超出的任务退回持久队列，不算失败
        "queue_max": 2000,
        "acquire_timeout": 180,
        # 本机 / 测试媒体服务不占网盘额度
        "bypass_hosts": ["127.0.0.1", "localhost", "::1"],
        # 撞到 403 / 429 / 5xx 后降到单路并冷却这么久，连续成功才回升
        "cooldown_seconds": 60,
        "degrade_gap_ms": 800,
        "recover_every": 8,
        # 直链台账：过期只标记 stale，绝不批量复验；刷新钩子按预算调用
        "link_ttl_minutes": 360,
        "sticky": True,
        "refresh_url": "",
        "refresh_token": "",
        "refresh_on_error": True,
        "verify_budget_per_minute": 6,
        # 批量任务：单轮在途上限与两次派发之间的间隔
        "batch_size": 20,
        "batch_interval_ms": 0,
        # 失败恢复：指数退避 + 抖动，到期才重新入队
        "retry_base_seconds": 30,
        "retry_max_seconds": 1800,
        "retry_jitter": 0.30,
        "retry_max_attempts": 5,
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
            # 部署在 nginx / 宝塔等反向代理后面时才改成 true，
            # 否则客户端可以自己写 X-Forwarded-For 绕过登录限流
            "trust_proxy": False,
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
    extensions: list[str] = field(default_factory=lambda: list(SUPPORTED_EXTENSIONS))
    mode: str = "manual"
    autostart: bool = False
    interval_minutes: int = 0
    schedule_mode: str = "incremental"
    concurrency: int = 3
    timeout: int = 30
    probe_timeout: int = 8


@dataclass
class PosterConfig:
    width: int = 1000
    height: int = 1500
    mode: str = "crop"
    # 用户在网页里主动选过海报模式。老版本默认是 blur，升级时要把这类
    # 「没人动过的模糊补边」迁成 crop；动过的保持原样。
    mode_explicit: bool = False


@dataclass
class FanartConfig:
    width: int = 1920
    height: int = 1080


@dataclass
class ThumbConfig:
    enabled: bool = True
    width: int = 1000
    height: int = 562
    # 用户在网页里动过缩略图开关（或配置已被迁移过）。旧默认是关，升级时
    # 只把「没人动过的 false」迁成 true；动过的（包括特意关掉的）保持原样。
    # 默认 True：新代码构造出来的配置都算「已表态」，save() 不会写出假 false。
    enabled_explicit: bool = True


@dataclass
class ScrapeConfig:
    frames: int = 2
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
class LinksConfig:
    """115 网盘直链的节奏与维护参数。完整说明见 docs/115-links.md。"""

    enabled: bool = True
    max_inflight: int = 2
    per_host_inflight: int = 2
    rate_per_minute: float = 60.0
    burst: int = 2
    min_gap_ms: int = 300
    queue_max: int = 2000
    acquire_timeout: float = 180.0
    bypass_hosts: list[str] = field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1"])
    cooldown_seconds: float = 60.0
    degrade_gap_ms: int = 800
    recover_every: int = 8
    link_ttl_minutes: int = 360
    sticky: bool = True
    refresh_url: str = ""
    refresh_token: str = ""
    refresh_on_error: bool = True
    verify_budget_per_minute: int = 6
    batch_size: int = 20
    batch_interval_ms: int = 0
    retry_base_seconds: float = 30.0
    retry_max_seconds: float = 1800.0
    retry_jitter: float = 0.30
    retry_max_attempts: int = 5


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
    # 部署在反向代理后面时置 true：才相信 X-Forwarded-For / X-Real-IP。
    # 直接暴露（没有代理）时必须是 false，否则登录限流的 IP 由客户端自己写，
    # 换一个头就是一次全新尝试，锁定形同虚设。
    trust_proxy: bool = False


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
    links: LinksConfig = field(default_factory=LinksConfig)
    servers: list[ServerConfig] = field(default_factory=list)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log: LogConfig = field(default_factory=LogConfig)
    # 实际加载的配置文件路径，网页「设置」保存时写回这里；不参与 yaml 持久化
    source: Path | None = field(default=None, compare=False)
    # 加载时把旧默认值迁成新默认值产生的说明，validate() 会一并返回
    migration_notes: list[str] = field(default_factory=list, compare=False, repr=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        raw = copy.deepcopy(DEFAULTS)
        notes: list[str] = []

        candidate = Path(path) if path else _find_config_file()
        if candidate and candidate.is_file():
            if yaml is None:
                raise RuntimeError("读取配置文件需要 PyYAML：pip install pyyaml")
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件格式不对，顶层应该是字典：{candidate}")
            # 老版本用的是单个 jellyfin 段，自动转成 servers 列表
            legacy = loaded.pop("jellyfin", None)
            notes, changed = _migrate_stale_defaults(loaded)
            # 旧版默认只扫 .strm，本地视频得自己加后缀才有反应。现在改成
            # 全开：任何支持的格式丢进媒体库就能刮，不用再碰配置。
            if _upgrade_extensions(loaded):
                notes.append(
                    "扫描格式从「只扫 .strm」改成支持的全部格式："
                    "mp4 / mkv / avi / ts / m2ts / mov / wmv / flv / rmvb / webm / mpg "
                    "放进媒体库就会直接刮，不用再改配置"
                )
                _rewrite_extensions(candidate, list(SUPPORTED_EXTENSIONS))
            raw = _deep_merge(raw, loaded)
            if legacy and not raw.get("servers"):
                raw["servers"] = [_legacy_server(legacy)]
            if changed:
                # 改完顺手写回文件：迁移只在内存生效的话，每次启动都会重复
                # 提示同一件事，用户还会以为设置没保存
                flat = [c for c in changed if c[1] != "thumb.enabled"]
                if flat:
                    _persist_migrations(candidate, flat)
                if any(c[1] == "thumb.enabled" for c in changed):
                    _rewrite_thumb_enabled(candidate)

        raw = _apply_env(raw)
        cfg = _from_dict(cls, raw)
        cfg.source = candidate if (candidate and candidate.is_file()) else None
        cfg.migration_notes = notes
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        """把当前配置写回 yaml（网页「设置」页保存时用）。

        写的是完整生效配置，所以 ``config.yaml`` 里的注释会丢；改的是同名的
        临时文件再替换，写一半断电也不会留下半个配置文件。
        """
        if yaml is None:
            raise RuntimeError("保存配置需要 PyYAML：pip install pyyaml")
        target = Path(path) if path else (self.source or DEFAULT_CONFIG_PATH)
        payload = self.to_yaml_dict()
        text = (
            "# 由网页「设置」页保存，注释不会保留；想写注释请直接编辑本文件\n"
            + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                             default_flow_style=False)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        self.source = target
        return target

    def to_yaml_dict(self) -> dict[str, Any]:
        """可序列化的完整配置（不含 Path 之类 yaml 表达不了的东西）。"""
        return {
            "scan": _to_dict(self.scan),
            "scrape": _to_dict(self.scrape),
            "links": _to_dict(self.links),
            "servers": _to_dict(self.servers),
            "web": _to_dict(self.web),
            "storage": _to_dict(self.storage),
            "log": _to_dict(self.log),
        }

    def validate(self) -> list[str]:
        if self.scan.schedule_mode not in ("incremental", "full"):
            self.scan.schedule_mode = "incremental"
        """校正明显不对的配置，返回需要提示的条目。"""
        notes: list[str] = list(self.migration_notes)

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
                f"poster.mode={self.scrape.poster.mode} 不认识（可选 crop/blur/gradient），"
                "已回退为 crop"
            )
            self.scrape.poster.mode = "crop"
        elif self.scrape.poster.mode in ("blur", "gradient") and not self.scrape.poster.mode_explicit:
            # 老版本的隐式默认是 blur，横版视频会缩在中间、上下两条深色背景。
            # 只迁移「没在网页里选过」的配置，选过的保持原样。
            notes.append(
                f"海报模式从旧的 {self.scrape.poster.mode}（补边）改成 crop（满幅裁切）："
                "横版视频也铺满 2:3；想保留留白可在设置里改回 blur"
            )
            self.scrape.poster.mode = "crop"
        elif self.scrape.poster.mode in ("blur", "gradient"):
            notes.append(
                f"海报是 {self.scrape.poster.mode} 补边模式，四周会有背景条；"
                "想要满幅裁切、和 PC 版一致，把 scrape.poster.mode 改成 crop"
            )

        if self.scrape.overwrite not in ("missing", "always"):
            notes.append(f"overwrite={self.scrape.overwrite} 不认识，已回退为 missing")
            self.scrape.overwrite = "missing"

        if not self.scan.roots:
            notes.append("scan.roots 为空，已改成 /media")
            self.scan.roots = ["/media"]
        if not self.scan.extensions:
            notes.append("scan.extensions 为空，已改成支持的全部格式")
            self.scan.extensions = list(SUPPORTED_EXTENSIONS)
        if not self.scrape.frame_positions:
            self.scrape.frame_positions = [0.15, 0.30, 0.45, 0.60, 0.75]

        mode = str(self.scan.mode).lower()
        if mode not in ("auto", "full", "incremental", "manual"):
            notes.append(f"scan.mode={self.scan.mode} 不认识，已回退为 auto")
            self.scan.mode = "auto"

        if self.scan.concurrency < 1:
            notes.append("scan.concurrency 小于 1，已改为 1")
            self.scan.concurrency = 1

        # 115 节奏参数：写错也不能把闸门变成「无限并发 + 无限速率」，
        # 否则一涌进来的 strm 会直接把账号顶到风控上
        for key, low, high in (
            ("max_inflight", 1, 64),
            ("per_host_inflight", 1, 64),
            ("burst", 1, 64),
            ("min_gap_ms", 0, 60000),
            ("queue_max", 0, 1000000),
            ("degrade_gap_ms", 0, 60000),
            ("recover_every", 1, 1000),
            ("link_ttl_minutes", 1, 100000),
            ("verify_budget_per_minute", 0, 10000),
            ("batch_size", 1, 10000),
            ("batch_interval_ms", 0, 60000),
            ("retry_max_attempts", 0, 100),
        ):
            value = getattr(self.links, key, None)
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = low
            if not low <= value <= high:
                notes.append(f"links.{key}={value} 超出 {low}–{high}，已收回范围内")
                value = max(low, min(high, value))
            setattr(self.links, key, value)
        for key, low, high in (
            ("rate_per_minute", 0.1, 100000.0),
            ("acquire_timeout", 1.0, 86400.0),
            ("cooldown_seconds", 0.0, 86400.0),
            ("retry_base_seconds", 1.0, 86400.0),
            ("retry_max_seconds", 1.0, 604800.0),
            ("retry_jitter", 0.0, 0.9),
        ):
            try:
                value = float(getattr(self.links, key))
            except (TypeError, ValueError):
                value = low
            if not low <= value <= high:
                notes.append(f"links.{key} 超出范围，已收回")
                value = max(low, min(high, value))
            setattr(self.links, key, value)
        if self.links.retry_max_seconds < self.links.retry_base_seconds:
            self.links.retry_max_seconds = self.links.retry_base_seconds
        if self.links.burst > self.links.max_inflight:
            self.links.burst = self.links.max_inflight
        if self.links.per_host_inflight > self.links.max_inflight:
            self.links.per_host_inflight = self.links.max_inflight
        url = str(self.links.refresh_url or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            notes.append("links.refresh_url 要以 http:// 或 https:// 开头，已清空")
            self.links.refresh_url = ""
        self.links.bypass_hosts = [
            str(h).strip().lower() for h in (self.links.bypass_hosts or [])
            if str(h).strip()
        ]

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
            f"links={'on' if self.links.enabled else 'off'}"
            f"({self.links.max_inflight}路/{self.links.rate_per_minute:g}次每分) "
            f"interval={self.scan.interval_minutes}min"
            f"({self.scan.schedule_mode}) web={self.web.host}:{self.web.port} "
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


def _migrate_stale_defaults(raw: dict) -> tuple[list[str], list[tuple[str, str, Any, Any]]]:
    """把配置文件里**仍然等于旧默认值**的项迁成新默认值。

    只认旧默认值本身：这些基本都是照旧示例抄下来的，不是有人特意调过；
    写成别的值一律不动。返回（给用户看的说明, 需要写回文件的改动）。
    """
    notes: list[str] = []
    changed: list[tuple[str, str, Any, Any]] = []
    scan = raw.get("scan")
    if isinstance(scan, dict):
        if scan.get("timeout") == 240:
            scan["timeout"] = 30
            changed.append(("scan", "timeout", 240, 30))
            notes.append(
                "抓帧超时从 240 秒收到 30 秒（这是单个时间点抓一帧的超时）："
                "网盘源超过 30 秒基本就是被限流或链接废了，再等只会拖住整个队列"
            )
        if scan.get("probe_timeout") == 20:
            scan["probe_timeout"] = 8
            changed.append(("scan", "probe_timeout", 20, 8))
            notes.append("探测超时从 20 秒收到 8 秒，失效链接能更快放掉")

    scrape = raw.get("scrape")
    if isinstance(scrape, dict) and scrape.get("frames") == 5:
        scrape["frames"] = 2
        changed.append(("scrape", "frames", 5, 2))
        notes.append(
            "采样帧数从 5 改成 2（取位置列表两端 0.15 / 0.75）：单文件抓帧次数少 60%，"
            "整库速度快一倍以上，海报质量基本没差别；想更挑剔改回 5 即可"
        )
    if isinstance(scrape, dict):
        thumb = scrape.get("thumb")
        # 只迁「没人动过的 false」：带 explicit 标记的（网页上动过开关）保持原样
        if (isinstance(thumb, dict) and thumb.get("enabled") is False
                and not thumb.get("enabled_explicit")):
            thumb["enabled"] = True
            thumb["enabled_explicit"] = True
            changed.append(("scrape", "thumb.enabled", False, True))
            notes.append(
                "缩略图默认改为开启（写 <名称>-thumb.jpg，Emby 的列表视图直接用）："
                "已有文件会在下次扫描时补抓一帧生成；不想要的话在设置里关掉"
            )
    return notes, changed


def _persist_migrations(path: Path, changed: list[tuple[str, str, Any, Any]]) -> None:
    """把迁移后的值写回 yaml，只动这几行，注释和其它内容原样保留。

    不用 ``save()`` 重写整份文件，是因为那样会把用户写的注释全冲掉；这里
    按「顶层段 + 键名」定位到具体一行，只替换冒号后面的值。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("配置迁移结果没能写回 %s：%s", path, exc)
        return

    wants = {(section, key): (old, new) for section, key, old, new in changed}
    hit: set[tuple[str, str]] = set()
    section = ""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not line[0].isspace():
            head = stripped.split(":", 1)[0].strip()
            section = head
        else:
            for (want_section, key), (old, new) in wants.items():
                if section != want_section or (want_section, key) in hit:
                    continue
                if not stripped.startswith(key + ":"):
                    continue
                prefix, sep, tail = line.partition(":")
                value_part = tail
                comment = ""
                if "#" in value_part:
                    value_part, comment = value_part.split("#", 1)
                    comment = "#" + comment
                try:
                    current = yaml.safe_load(value_part) if yaml else value_part.strip()
                except Exception:
                    continue
                if current != old:
                    continue
                ending = "\n" if line.endswith("\n") else ""
                line = f"{prefix}: {new}{' ' + comment.strip() if comment else ''}{ending}"
                hit.add((want_section, key))
        out.append(line)

    if not hit:
        log.info("配置里没找到需要写回的旧默认值（%s），只按内存值运行", path)
        return
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("配置迁移结果没能写回 %s：%s", path, exc)
        return
    log.info("已把 %s 项迁移后的默认值写回 %s（注释与其它设置未改动）",
             len(hit), path)



# extensions 在 yaml 里是列表，可能是块写法（每行一个 ``- .strm``）或行内写法
# （``extensions: [.strm]``），两种都要能整体换掉。块写法允许列表项和键同缩进
# （yaml 合法，devserver 生成的配置就是这么写的）。
_EXT_BLOCK = re.compile(
    r"^(?P<indent>[ \t]*)extensions:[ \t]*(?P<comment>#[^\n]*)?\n"
    r"(?:(?P=indent)[ \t]*-[^\n]*\n)*",
    re.M,
)
_EXT_INLINE = re.compile(
    r"^(?P<indent>[ \t]*)extensions:[ \t]*\[[^\]\n]*\][ \t]*(?P<comment>#[^\n]*)?$",
    re.M,
)


def _upgrade_extensions(raw: dict) -> bool:
    """旧默认「只扫 .strm」→ 支持的全部格式。

    只认 ``['.strm']`` 这个旧默认值本身：写成别的组合（比如用户特意加了
    ``.iso``）说明是手动调过的，一律不动。
    """
    scan = raw.get("scan")
    if not isinstance(scan, dict):
        return False
    if scan.get("extensions") != [".strm"]:
        return False
    scan["extensions"] = list(SUPPORTED_EXTENSIONS)
    return True


def _rewrite_extensions(path: Path, items: list[str]) -> bool:
    """只换 yaml 里 extensions 那段列表，注释和其它内容原样保留。

    ``_persist_migrations`` 是按「一行一个标量」写的，列表接不住，所以这里
    单独处理：块写法和行内写法都认，认不出来就放弃（内存里已经是新值了，
    只是文件没跟着变，下次启动会再迁一次）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("扩展名列表没能写回 %s：%s", path, exc)
        return False

    def build(indent: str) -> str:
        # 不带上原来那行的注释：写「只刮 strm」的注释配一份全格式列表是自相矛盾，
        # 迁移前后的差异由启动时的提示说明
        body = "".join(f"{indent}  - {item}\n" for item in items)
        return f"{indent}extensions:\n" + body

    match = _EXT_BLOCK.search(text)
    if match:
        new_text = text[:match.start()] + build(match.group("indent")) + text[match.end():]
    else:
        match = _EXT_INLINE.search(text)
        if not match:
            log.info("没认出 %s 里 extensions 的写法，只在内存里生效", path)
            return False
        # 行内写法的换行不在匹配里，替换时手动吃掉一个，免得留下空行
        rest = text[match.end():]
        new_text = (text[:match.start()] + build(match.group("indent"))
                    + (rest[1:] if rest.startswith("\n") else rest))

    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("扩展名列表没能写回 %s：%s", path, exc)
        return False
    log.info("已把 %s 的 scan.extensions 扩成支持的全部格式", path)
    return True


# thumb.enabled 是嵌套键，块写法长这样（行内写法是 ``thumb: {enabled: false, ...}``）：
#   scrape:
#     thumb:
#       enabled: false
_THUMB_BLOCK = re.compile(
    r"^(?P<ind>[ \t]*)thumb:(?P<head>[^\n]*)\r?\n"
    r"(?P=ind)(?P<deep>[ \t]+)enabled:[ \t]*false(?P<tail>[^\n]*)",
    re.M,
)
_THUMB_INLINE = re.compile(
    r"(?P<pre>[ \t]*thumb:[ \t]*\{[^}\n]*?enabled:[ \t]*)false(?P<post>[^}\n]*\})",
)


def _thumb_block_repl(match: "re.Match[str]") -> str:
    """块写法只换 enabled 那一行的值并补上 explicit 标记，缩进和注释原样保留。"""
    return (f"{match.group('ind')}thumb:{match.group('head')}\n"
            f"{match.group('ind')}{match.group('deep')}enabled: true"
            f"{match.group('tail')}\n"
            f"{match.group('ind')}{match.group('deep')}enabled_explicit: true")


def _rewrite_thumb_enabled(path: Path) -> bool:
    """缩略图默认值迁移的写回：把 thumb 的 ``enabled: false`` 换成 ``true``。

    ``_persist_migrations`` 按「一行一个标量」定位，接不住 ``thumb:`` 下面
    嵌套的 ``enabled:``，所以这里单独处理。块写法和行内写法都认，认不出
    来就放弃（内存里已经是新值了，用户在网页上保存设置时会自然写回）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("缩略图默认值没能写回 %s：%s", path, exc)
        return False

    new_text, hits = _THUMB_BLOCK.subn(_thumb_block_repl, text, count=1)
    if not hits:
        new_text, hits = _THUMB_INLINE.subn(
            r"\g<pre>true, enabled_explicit: true\g<post>", text, count=1)
    if not hits:
        log.info("没认出 %s 里 thumb.enabled 的写法，只在内存里生效", path)
        return False

    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("缩略图默认值没能写回 %s：%s", path, exc)
        return False
    log.info("已把 %s 的缩略图默认开启写回（注释与其它设置未改动）", path)
    return True


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
    for candidate in (Path("config.yaml"), DEFAULT_CONFIG_PATH):
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
