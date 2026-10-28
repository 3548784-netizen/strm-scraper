# -*- coding: utf-8 -*-
"""登录鉴权。

只支持一个账号（够用，也省得做用户管理），存在 ``auth.json`` 里：

- 密码用 PBKDF2-SHA256 加盐哈希，文件里没有明文；
- 登录状态是 HMAC 签名的 cookie，服务端不存 session，重启不用重建表；
- 改用户名或密码时把 ``token_version`` 加一，所有旧 cookie 立刻失效；
- 登录失败按来源 IP 计数，5 次锁 5 分钟，避免被在线爆破。

首次启动会自动创建 ``admin / admin`` 并在日志里提醒改掉。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

COOKIE_NAME = "ws_session"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128

_ITERATIONS = 200_000
_TOKEN_PREFIX = "v1"


# ----------------------------------------------------------------------
# 哈希
# ----------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None,
                  iterations: int = _ITERATIONS) -> dict[str, Any]:
    """生成密码记录（盐随密码一起存，不存明文）。"""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algo": "pbkdf2_sha256",
        "iterations": iterations,
        "salt": _b64(salt),
        "hash": _b64(derived),
    }


def verify_password(password: str, record: dict[str, Any]) -> bool:
    try:
        salt = _unb64(str(record["salt"]))
        iterations = int(record.get("iterations") or _ITERATIONS)
        expected = _unb64(str(record["hash"]))
    except (KeyError, TypeError, ValueError):
        return False
    if not password:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def validate_username(value: str) -> str:
    """用户名放宽到中文，只挡空白和控制字符。"""
    name = (value or "").strip()
    if not 2 <= len(name) <= 32:
        raise ValueError("用户名长度需在 2–32 之间")
    if any(ch.isspace() for ch in name):
        raise ValueError("用户名不能包含空格")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("用户名包含不可见字符")
    return name


def validate_password(value: str, username: str = "") -> str:
    pwd = value or ""
    if len(pwd) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(pwd) > MAX_PASSWORD_LEN:
        raise ValueError(f"密码最多 {MAX_PASSWORD_LEN} 位")
    if username and pwd.strip().lower() == username.strip().lower():
        raise ValueError("密码不能和用户名相同")
    return pwd


# ----------------------------------------------------------------------
# 账号存储
# ----------------------------------------------------------------------

class AuthStore:
    """读写 auth.json，兼管会话签发与校验。"""

    def __init__(self, path: str | Path, session_hours: int = 168) -> None:
        self.path = Path(path)
        self.session_hours = max(1, min(int(session_hours or 168), 24 * 365))
        self.username = DEFAULT_USERNAME
        self.default_password = True
        self.created = 0.0
        self.changed = 0.0
        self._password: dict[str, Any] = {}
        self._secret = ""
        self._token_version = 0
        self.load()

    # ---------- 读写 ----------

    def load(self) -> None:
        data: Any = None
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                broken = self.path.with_name(self.path.name + ".broken")
                log.error("账号文件读取失败（%s），已备份为 %s 并重建默认账号：%s",
                          self.path, broken.name, exc)
                try:
                    self.path.replace(broken)
                except OSError:
                    pass
                data = None

        if not isinstance(data, dict) or not isinstance(data.get("password"), dict):
            self._init_default()
            self.save()
            # 落盘成功后再提示，免得路径不可写时误导
            log.warning(
                "已创建默认账号 %s / %s（文件：%s）。登录后请在右上角「账号」里改掉",
                DEFAULT_USERNAME, DEFAULT_PASSWORD, self.path,
            )
            return

        self.username = str(data.get("username") or DEFAULT_USERNAME)
        self._password = data["password"]
        self._secret = str(data.get("secret") or _b64(secrets.token_bytes(32)))
        self._token_version = int(data.get("token_version") or 1)
        self.default_password = bool(data.get("default_password", False))
        self.created = float(data.get("created") or 0.0)
        self.changed = float(data.get("changed") or 0.0)

    def _init_default(self) -> None:
        self.username = DEFAULT_USERNAME
        self._password = hash_password(DEFAULT_PASSWORD)
        self._secret = _b64(secrets.token_bytes(32))
        self._token_version = 1
        self.default_password = True
        self.created = self.changed = time.time()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "file_version": 1,
            "username": self.username,
            "password": self._password,
            "secret": self._secret,
            "token_version": self._token_version,
            "default_password": self.default_password,
            "created": self.created,
            "changed": self.changed,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows / 某些挂载卷不支持，忽略
        tmp.replace(self.path)

    # ---------- 校验与修改 ----------

    def verify(self, username: str, password: str) -> bool:
        if not username or not password:
            return False
        if username.strip().lower() != self.username.strip().lower():
            # 用户名不对也跑一遍哈希，避免靠响应时间探测账号是否存在
            verify_password(password, self._password)
            return False
        return verify_password(password, self._password)

    def update(self, username: str | None = None, password: str | None = None) -> None:
        if username:
            self.username = username.strip()
        if password:
            self._password = hash_password(password)
            self.default_password = False
        self._token_version += 1  # 旧 cookie 全部作废
        self.changed = time.time()
        self.save()
        log.info("账号已更新：用户名 %s，密码%s", self.username, "已修改" if password else "未变")

    # ---------- 会话 ----------

    def issue(self, username: str | None = None) -> str:
        payload = {
            "u": username or self.username,
            "v": self._token_version,
            "exp": int(time.time()) + self.session_hours * 3600,
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
        return f"{_TOKEN_PREFIX}.{body}.{self._sign(body)}"

    def _sign(self, body: str) -> str:
        key = _unb64(self._secret)
        return _b64(hmac.new(key, body.encode("utf-8"), hashlib.sha256).digest())

    def read(self, token: str | None) -> str | None:
        """校验 cookie，通过返回用户名，否则 None。"""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
            return None
        _, body, signature = parts
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if int(payload.get("v", -1)) != self._token_version:
            return None  # 改过账号，旧会话作废
        if int(payload.get("exp", 0)) < time.time():
            return None
        return str(payload.get("u") or "") or None

    def describe(self) -> str:
        state = "默认密码" if self.default_password else "已改密码"
        return f"用户 {self.username}（{state}），会话 {self.session_hours} 小时"


# ----------------------------------------------------------------------
# 登录限流
# ----------------------------------------------------------------------

class LoginThrottle:
    """同一个 IP 连续失败多次就短暂锁定。"""

    def __init__(self, max_fails: int = 5, window: int = 300) -> None:
        self.max_fails = max_fails
        self.window = window
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        stamps = [t for t in self._fails.get(key, []) if now - t < self.window]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps

    def blocked(self, key: str) -> int:
        """还要等多少秒才能再试，0 表示没锁。"""
        stamps = self._recent(key)
        if len(stamps) >= self.max_fails:
            return max(1, int(self.window - (time.time() - stamps[0])))
        return 0

    def fail(self, key: str) -> int:
        self._fails.setdefault(key, []).append(time.time())
        return len(self._recent(key))

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
