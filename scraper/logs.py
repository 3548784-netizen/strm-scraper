# -*- coding: utf-8 -*-
"""日志：控制台 + 滚动文件 + 内存环形缓冲。

三路输出各自解决一个问题：

* 控制台 —— ``docker logs`` 里看，容器前台随时有输出；
* 滚动文件 —— 事后排查。按大小切分，主日志记全量，错误日志只记 WARNING
  及以上，翻的时候不用在一堆进度里找异常；
* 内存缓冲 —— 网页实时看，支持按级别、关键字过滤，不用登服务器。

格式统一成 ``时间 级别 [来源] 消息``，来源用短名（``worker`` / ``probe``），
一眼能看出是哪一层出的问题。
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 网页上按级别上色
LEVEL_STYLE = {
    "DEBUG": "dim",
    "INFO": "info",
    "WARNING": "warn",
    "ERROR": "err",
    "CRITICAL": "err",
}

# 日志级别排序，用于「只看某级别以上」
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class ShortNameFormatter(logging.Formatter):
    """把 ``strm_scraper.scraper.worker`` 这类长名字压成 ``worker``。"""

    def format(self, record: logging.LogRecord) -> str:
        if record.name.startswith("scraper."):
            record.shortname = record.name.split(".")[-1]
        else:
            record.shortname = record.name
        return super().format(record)


class RingLogHandler(logging.Handler):
    """把最近的日志留在内存里，网页直接读，不用去翻 docker logs。"""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.records: collections.deque = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志本身不能抛异常
            return
        with self._lock:
            self._seq += 1
            self.records.append({
                "id": self._seq,
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "level_style": LEVEL_STYLE.get(record.levelname, "info"),
                "name": getattr(record, "shortname", record.name),
                # 单条任务的失败/成功会带上文件路径，网页可以按文件过滤
                "item": getattr(record, "item", ""),
                "message": message,
            })

    def tail(self, limit: int = 200, min_level: str = "DEBUG",
             keyword: str = "", since: int = 0) -> list[dict]:
        """按条件取最近的日志。``since`` 是上次看到的最大 id，用于增量拉取。"""
        floor = LEVEL_ORDER.get(min_level.upper(), 0)
        needle = keyword.strip().lower()
        with self._lock:
            snapshot = list(self.records)

        picked = []
        for entry in snapshot:
            if entry["id"] <= since:
                continue
            if LEVEL_ORDER.get(entry["level"], 0) < floor:
                continue
            if needle and needle not in entry["message"].lower() \
                    and needle not in entry["item"].lower():
                continue
            picked.append(entry)
        return picked[-limit:]

    def last_id(self) -> int:
        with self._lock:
            return self._seq


@dataclass
class LogFiles:
    """日志文件句柄，供网页下载与页面提示用。"""

    directory: Path | None = None
    main: Path | None = None
    errors: Path | None = None
    handlers: list[logging.Handler] = field(default_factory=list)

    def describe(self) -> str:
        if not self.main:
            return "未写文件（log.dir 为空或不可写）"
        return str(self.main)


def setup_logging(level: str = "INFO", log_dir: str | Path | None = None,
                  filename: str = "strm-scraper.log", max_mb: float = 5.0,
                  backups: int = 5, console: bool = True) -> tuple[RingLogHandler, LogFiles]:
    """初始化根 logger，返回（内存缓冲, 文件信息）。"""
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    verbose = ShortNameFormatter(
        "%(asctime)s %(levelname)-7s [%(shortname)-12s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ShortNameFormatter(
            "%(asctime)s %(levelname)-7s [%(shortname)-12s] %(message)s", "%H:%M:%S"
        ))
        root.addHandler(stream)

    buffer = RingLogHandler()
    buffer.setLevel(logging.DEBUG)
    buffer.setFormatter(verbose)
    root.addHandler(buffer)

    files = LogFiles()
    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            files.directory = directory
            files.main = directory / filename
            files.errors = directory / f"errors-{filename}"

            main_handler = logging.handlers.RotatingFileHandler(
                files.main, maxBytes=int(max_mb * 1024 * 1024),
                backupCount=backups, encoding="utf-8",
            )
            main_handler.setFormatter(verbose)
            main_handler.setLevel(numeric)
            root.addHandler(main_handler)
            files.handlers.append(main_handler)

            error_handler = logging.handlers.RotatingFileHandler(
                files.errors, maxBytes=int(max_mb * 1024 * 1024),
                backupCount=backups, encoding="utf-8",
            )
            error_handler.setFormatter(verbose)
            error_handler.setLevel(logging.WARNING)
            root.addHandler(error_handler)
            files.handlers.append(error_handler)
        except OSError as exc:
            files = LogFiles()
            logging.getLogger("logs").warning("日志目录不可写，只输出到控制台：%s", exc)

    # 第三方库的日志太吵
    for noisy in ("urllib3", "uvicorn", "uvicorn.access", "uvicorn.error", "watchfiles",
                  "PIL", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return buffer, files


def tail_file(path: Path | None, limit: int = 400) -> list[str]:
    """读日志文件末尾若干行，网页「原始日志」用。"""
    if not path or not path.is_file():
        return []
    try:
        # 从尾部倒着读，避免把整个文件读进内存
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, 256 * 1024)
            handle.seek(size - block)
            data = handle.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-limit:]
    except OSError:
        return []
