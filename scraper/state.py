# -*- coding: utf-8 -*-
"""刮削状态库（SQLite）：记录每个文件的处理结果，支撑增量与失败重试。"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     REAL    NOT NULL DEFAULT 0,
    status    TEXT    NOT NULL DEFAULT 'pending',
    reason    TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL DEFAULT '',
    duration  REAL    NOT NULL DEFAULT 0,
    attempts  INTEGER NOT NULL DEFAULT 0,
    updated   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# pending: 待处理 / running: 处理中 / ok: 已生成 / skipped: 产物齐全无需处理
# failed: 失败（reason 里有原因）
ACTIVE = ("pending", "running")


@dataclass
class Item:
    path: str
    size: int = 0
    mtime: float = 0.0
    status: str = "pending"
    reason: str = ""
    title: str = ""
    duration: float = 0.0
    attempts: int = 0
    updated: float = 0.0

    @property
    def name(self) -> str:
        return Path(self.path).name


class StateStore:
    """轻量封装，单连接 + 锁，够用且不会踩线程坑。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------

    def ensure(self, sources: Iterable[tuple[str, int, float]]) -> int:
        """登记扫描到的文件，返回新增数量。已有记录不动。"""
        added = 0
        with self._lock:
            for path, size, mtime in sources:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO items(path, size, mtime, status, updated) "
                    "VALUES(?,?,?, 'pending', ?)",
                    (path, size, mtime, time.time()),
                )
                added += cur.rowcount
            self._conn.commit()
        return added

    def mark_running(self, path: str) -> None:
        self._set(path, status="running", reason="")

    def mark_ok(self, path: str, title: str = "", duration: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status='ok', reason='', title=?, duration=?, "
                "attempts=attempts+1, updated=? WHERE path=?",
                (title, duration, time.time(), path),
            )
            self._conn.commit()

    def mark_skipped(self, path: str, reason: str = "产物已存在") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status='skipped', reason=?, updated=? WHERE path=?",
                (reason, time.time(), path),
            )
            self._conn.commit()

    def mark_failed(self, path: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status='failed', reason=?, attempts=attempts+1, "
                "updated=? WHERE path=?",
                (reason[:400], time.time(), path),
            )
            self._conn.commit()

    def reset(self, statuses: tuple[str, ...] = ("failed",)) -> int:
        """把指定状态重置为 pending，用于全量重刮或重试失败项。"""
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE items SET status='pending', reason='' WHERE status IN ({marks})",
                statuses,
            )
            self._conn.commit()
            return cur.rowcount

    def reset_one(self, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status='pending', reason='' WHERE path=?", (path,)
            )
            self._conn.commit()

    def touch_signature(self, path: str, size: int, mtime: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET size=?, mtime=? WHERE path=?", (size, mtime, path)
            )
            self._conn.commit()

    def _set(self, path: str, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [time.time(), path]
        with self._lock:
            self._conn.execute(
                f"UPDATE items SET {assignments}, updated=? WHERE path=?", values
            )
            self._conn.commit()

    # ---------- 查询 ----------

    def get(self, path: str) -> Item | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE path=?", (path,)).fetchone()
        return _row_to_item(row) if row else None

    def next_pending(self, limit: int) -> list[Item]:
        """取待处理队列。失败过 3 次的排到最后，避免反复卡同一个。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE status='pending' "
                "ORDER BY attempts ASC, updated ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def items(self, status: str | None = None, keyword: str = "",
              limit: int = 50, offset: int = 0) -> list[Item]:
        sql = "SELECT * FROM items WHERE 1=1"
        args: list = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if keyword:
            sql += " AND path LIKE ?"
            args.append(f"%{keyword}%")
        sql += " ORDER BY updated DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_item(r) for r in rows]

    def count(self, status: str | None = None) -> int:
        with self._lock:
            if status and status != "all":
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM items WHERE status=?", (status,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM items").fetchone()
        return int(row[0])

    def paths(self, statuses: tuple[str, ...] = ("ok", "skipped"),
              limit: int = 2000) -> list[str]:
        """按状态取路径，用于「让媒体服务器重建图片」这类批量操作。"""
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT path FROM items WHERE status IN ({marks}) "
                "ORDER BY updated DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM items GROUP BY status"
            ).fetchall()
        result = {status: count for status, count in rows}
        for key in ("pending", "running", "ok", "skipped", "failed"):
            result.setdefault(key, 0)
        result["total"] = sum(result.values())
        return result

    # ---------- meta ----------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        path=row["path"],
        size=row["size"],
        mtime=row["mtime"],
        status=row["status"],
        reason=row["reason"],
        title=row["title"],
        duration=row["duration"],
        attempts=row["attempts"],
        updated=row["updated"],
    )
