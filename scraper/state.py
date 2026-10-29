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
    updated   REAL    NOT NULL DEFAULT 0,
    frame_at  REAL    NOT NULL DEFAULT 0
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


def parent_dir(path: str) -> str:
    """取所在目录。不依赖 os.path，Windows 路径在 Linux 容器里也要判对。"""
    cut = max(path.rfind("/"), path.rfind("\\"))
    return path[:cut] if cut > 0 else ""


def dir_label(path: str) -> str:
    """目录的显示名：最后一段，取不到就用全路径。"""
    cut = max(path.rfind("/"), path.rfind("\\"))
    name = path[cut + 1:] if cut >= 0 else path
    return name or path


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
    # 手动选定的帧位置（秒），0 表示交给程序自动挑
    frame_at: float = 0.0

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """老库平滑升级：缺列就补上，不要求用户删 state.db。"""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(items)")}
        if "frame_at" not in columns:
            self._conn.execute(
                "ALTER TABLE items ADD COLUMN frame_at REAL NOT NULL DEFAULT 0"
            )

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

    def delete(self, paths: Iterable[str]) -> int:
        """从任务列表里彻底删掉这些记录（不动磁盘上的文件）。"""
        wanted = [p for p in dict.fromkeys(paths) if p]
        if not wanted:
            return 0
        removed = 0
        with self._lock:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                cur = self._conn.execute(
                    f"DELETE FROM items WHERE path IN ({marks})", chunk
                )
                removed += cur.rowcount
            self._conn.commit()
        return removed

    def paths_in(self, dir_path: str, recursive: bool = True) -> list[str]:
        """取某个目录下的记录路径，任务列表按文件夹删除时用。"""
        base = dir_path.rstrip("/\\")
        if not base:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM items WHERE path LIKE ?", (base + "%",)
            ).fetchall()
        result: list[str] = []
        for (path,) in rows:
            where = parent_dir(path)
            if where == base:
                result.append(path)
            elif recursive and (where.startswith(base + "/")
                                or where.startswith(base + "\\")):
                result.append(path)
        return result

    def set_frame_at(self, path: str, seconds: float) -> None:
        """记住手选的帧位置，之后重刮也用这一帧；传 0 表示恢复自动选帧。"""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET frame_at=?, updated=? WHERE path=?",
                (max(0.0, round(float(seconds), 2)), time.time(), path),
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

    def get_many(self, paths: Iterable[str]) -> dict[str, Item]:
        """一次查一批，文件管理页面列表用，避免逐条查询。"""
        wanted = [p for p in dict.fromkeys(paths) if p]
        if not wanted:
            return {}
        result: dict[str, Item] = {}
        with self._lock:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT * FROM items WHERE path IN ({marks})", chunk
                ).fetchall()
                for row in rows:
                    result[row["path"]] = _row_to_item(row)
        return result

    def next_pending(self, limit: int, only: Iterable[str] | None = None) -> list[Item]:
        """取待处理队列。失败过 3 次的排到最后，避免反复卡同一个。

        ``only`` 给定时只看这些路径：文件管理里只勾了一个文件夹，就只跑
        这个文件夹里的文件，不能顺手把整库的待处理都拉起来。
        """
        scope = [p for p in dict.fromkeys(only or ()) if p] if only is not None else []
        if only is not None and not scope:
            return []
        with self._lock:
            if not scope:
                rows = self._conn.execute(
                    "SELECT * FROM items WHERE status='pending' "
                    "ORDER BY attempts ASC, updated ASC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [_row_to_item(r) for r in rows]
            found = []
            for start in range(0, len(scope), 400):
                chunk = scope[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                found.extend(self._conn.execute(
                    f"SELECT * FROM items WHERE status='pending' AND path IN ({marks})",
                    chunk,
                ).fetchall())
        found.sort(key=lambda r: (r["attempts"], r["updated"]))
        return [_row_to_item(r) for r in found[:limit]]

    def items(self, status: str | None = None, keyword: str = "",
              dir_path: str = "", limit: int = 50, offset: int = 0) -> list[Item]:
        sql = "SELECT * FROM items WHERE 1=1"
        args: list = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if keyword:
            sql += " AND path LIKE ?"
            args.append(f"%{keyword}%")
        if dir_path:
            # 先按前缀粗筛，再由 parent_dir 精确到这一层（避免带进子目录）
            sql += " AND path LIKE ?"
            args.append(f"{dir_path}%")
        sql += " ORDER BY updated DESC"
        if not dir_path:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        picked = [_row_to_item(r) for r in rows]
        if dir_path:
            picked = [it for it in picked if parent_dir(it.path) == dir_path]
            picked = picked[offset:offset + limit]
        return picked

    def count(self, status: str | None = None, keyword: str = "",
              dir_path: str = "") -> int:
        sql = "SELECT path FROM items WHERE 1=1"
        args: list = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if keyword:
            sql += " AND path LIKE ?"
            args.append(f"%{keyword}%")
        if dir_path:
            sql += " AND path LIKE ?"
            args.append(f"{dir_path}%")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        if dir_path:
            return sum(1 for r in rows if parent_dir(r[0]) == dir_path)
        return len(rows)

    def dir_groups(self, status: str | None = None, keyword: str = "",
                   limit: int = 400) -> list[dict]:
        """按所在文件夹聚合，任务列表的「按文件夹」视图用。

        状态筛选只影响参与统计的记录（比如只看失败时，只列出有失败项的文件夹）。
        """
        sql = "SELECT path, status, updated FROM items WHERE 1=1"
        args: list = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if keyword:
            sql += " AND path LIKE ?"
            args.append(f"%{keyword}%")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()

        groups: dict[str, dict] = {}
        for path, st, updated in rows:
            key = parent_dir(path)
            group = groups.get(key)
            if group is None:
                group = groups[key] = {
                    "path": key, "name": dir_label(key),
                    "total": 0, "pending": 0, "running": 0,
                    "ok": 0, "skipped": 0, "failed": 0, "updated": 0.0,
                }
            group["total"] += 1
            if st in group:
                group[st] += 1
            if updated and updated > group["updated"]:
                group["updated"] = float(updated)

        result = list(groups.values())
        for group in result:
            group["done"] = group["ok"] + group["skipped"]
            group["pct"] = round(100 * group["done"] / group["total"]) if group["total"] else 0
        # 正在跑的排最前，其次有失败的，再其次还有待处理的
        result.sort(key=lambda g: (-g["running"], -g["failed"], -g["pending"], g["path"].lower()))
        return result[:limit]

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
        frame_at=row["frame_at"] if "frame_at" in row.keys() else 0.0,
    )
