# -*- coding: utf-8 -*-
"""检查排序、老库迁移、配置写回、兜底抓帧这几处改动。

不需要服务在跑；纯逻辑层断言，随时可跑：
    python tools/check_sort.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import frames  # noqa: E402
from scraper.config import Config  # noqa: E402
from scraper.state import StateStore  # noqa: E402

checks = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks, failed
    checks += 1
    if not ok:
        failed += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"    {detail}" if detail else ""))


def names(rows) -> list[str]:
    return [row.name for row in rows]


def test_sort(store: StateStore) -> None:
    print("\n---------- 1. 任务列表排序 ----------")
    add = [
        (r"C:\m\电影\Z 最后.strm", 10, 1.0),
        (r"C:\m\电影\A 最早.strm", 10, 1.0),
        (r"C:\m\电影\m 中间.strm", 10, 1.0),
        (r"C:\m\剧集\B 第二个.strm", 10, 1.0),
    ]
    store.ensure(add)
    # 加入时间用不同值，方便判断顺序
    for path, stamp in ((add[0][0], 100.0), (add[1][0], 300.0),
                        (add[2][0], 200.0), (add[3][0], 400.0)):
        store._conn.execute("UPDATE items SET added=? WHERE path=?", (stamp, path))
    store._conn.commit()

    asc = names(store.items(limit=4, sort="name", order="asc"))
    desc = names(store.items(limit=4, sort="name", order="desc"))
    check("按名称正序", asc == sorted(asc, key=str.lower), str(asc))
    check("按名称倒序是正序的反向", desc == list(reversed(asc)), str(desc))
    check("名称排序只看文件名、不分目录",
          [n[0].lower() for n in asc] == sorted([n[0].lower() for n in asc]))

    newest = names(store.items(limit=4, sort="added", order="desc"))
    check("按添加时间倒序（最新在前）", newest[0].startswith("B"), str(newest))
    oldest = names(store.items(limit=4, sort="added", order="asc"))
    check("按添加时间正序（最早在前）", oldest[0].startswith("Z"), str(oldest))

    bad = names(store.items(limit=4, sort="__乱写__", order="__乱写__"))
    check("非法排序参数不报错、退回默认", len(bad) == 4, str(bad))

    paged = store.items(limit=2, offset=2, sort="name", order="asc")
    check("排序 + 分页一起用", names(paged) == asc[2:4], str(names(paged)))

    print("\n---------- 2. 按文件夹视图排序 ----------")
    by_name = store.dir_groups(sort="name", order="asc")
    check("文件夹按名称正序",
          [g["name"] for g in by_name] == sorted([g["name"] for g in by_name]),
          str([g["name"] for g in by_name]))
    by_added = store.dir_groups(sort="added", order="desc")
    stamps = [g["added"] for g in by_added]
    check("文件夹按最新加入时间倒序", stamps == sorted(stamps, reverse=True),
          str([round(s) for s in stamps]))
    priority = store.dir_groups()
    check("不传排序时仍是行动优先级（接口兼容）", len(priority) == len(by_name))


def test_migrate_old_db(tmp: Path) -> None:
    print("\n---------- 3. 老库升级 ----------")
    db = tmp / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE items (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
            duration REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            updated REAL NOT NULL DEFAULT 0, frame_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO items(path, status, updated) VALUES('C:\\m\\a.strm', 'ok', 12345.0);
    """)
    conn.commit()
    conn.close()

    store = StateStore(db)
    item = store.get("C:\\m\\a.strm")
    check("老库能打开", item is not None)
    check("补上了 added 列且用 updated 兜底", item and abs(item.added - 12345.0) < 1,
          f"added={item.added if item else '—'}")
    rows = store.items(limit=5, sort="added", order="desc")
    check("补列后按添加时间排序可用", len(rows) == 1)
    indexes = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("建了 added 索引", "idx_items_added" in indexes, str(sorted(indexes)))
    store.close()

    again = StateStore(db)
    check("再打开一次不会重复报错", again.get("C:\\m\\a.strm") is not None)
    again.close()


def test_config_writeback(tmp: Path) -> None:
    print("\n---------- 4. 配置迁移写回 ----------")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "# 我自己写的注释，不能被冲掉\n"
        "scan:\n"
        "  roots:\n"
        "    - /media\n"
        "  timeout: 240   # 单点抓帧超时\n"
        "  probe_timeout: 20\n"
        "scrape:\n"
        "  frames: 5\n"
        "servers:\n"
        "  - name: emby\n"
        "    type: emby\n"
        "    url: http://10.0.0.2:8096\n"
        "    timeout: 30\n",
        encoding="utf-8")

    cfg = Config.load(cfg_path)
    check("旧默认值被迁移", (cfg.scan.timeout, cfg.scan.probe_timeout, cfg.scrape.frames)
          == (30, 8, 2), f"{cfg.scan.timeout}/{cfg.scan.probe_timeout}/{cfg.scrape.frames}")
    text = cfg_path.read_text(encoding="utf-8")
    check("写回了 timeout: 30", "timeout: 30" in text)
    check("写回了 probe_timeout: 8", "probe_timeout: 8" in text)
    check("写回了 frames: 2", "frames: 2" in text)
    check("原注释保留", "我自己写的注释，不能被冲掉" in text and "单点抓帧超时" in text)
    check("别处的同名键没被误改（servers.timeout=30 仍是 30）",
          text.count("timeout: 30") == 2, f"出现 {text.count('timeout: 30')} 次")
    check("其它配置原样保留", "url: http://10.0.0.2:8096" in text)

    again = Config.load(cfg_path)
    check("第二次加载不再有迁移提示", again.migration_notes == [],
          str(again.migration_notes))
    check("值仍是迁移后的", again.scan.timeout == 30 and again.scrape.frames == 2)


def test_fallback_frames(tmp: Path) -> None:
    print("\n---------- 5. 兜底抓帧 ----------")
    tried: list[float] = []

    def fake_capture(source, seconds, output, timeout=60, fast_seek=True,
                     width=0, min_bytes=None, abort=None):
        tried.append(round(float(seconds), 2))
        Path(output).write_bytes(b"x" * 2048)
        return True

    def fake_sharpness(path):
        # 模拟「片头是黑场」：只有 3 秒那一帧有画面，采样点（90s / 450s）
        # 和其它兜底位置都不行
        second = tried[-1] if tried else 0
        return 42.0 if abs(second - 3.0) < 0.01 else None

    origin_capture, origin_sharp = frames.capture, frames.sharpness
    frames.capture, frames.sharpness = fake_capture, fake_sharpness
    try:
        work = tmp / "frames"
        work.mkdir()
        tried.clear()
        best, count = frames.capture_best("x.stream", 600.0, [0.15, 0.75],
                                          work, timeout=5)
        check("采样全失败后兜底拿到了帧", count == 1, f"count={count}")
        check("兜底按顺序往后试（0 → 1 → 3）",
              tried[-3:] == [0.0, 1.0, 3.0], str(tried))
        check("选中的是 3 秒那一帧", best.name == "f_fallback.jpg", best.name)

        # 全都黑：要给一条说清楚试了哪些位置的错误
        tried.clear()
        frames.sharpness = lambda path: None
        try:
            frames.capture_best("x.stream", 600.0, [0.15], work, timeout=5)
            check("全黑时应该失败", False, "没有抛错")
        except frames.FrameError as exc:
            msg = str(exc)
            check("全黑时报错", True)
            check("报错里说明了兜底试过的位置",
                  "0s" in msg and "8s" in msg, msg[:80])
        check("兜底把 5 个位置都试过了", len(tried) >= 6, str(tried))
    finally:
        frames.capture, frames.sharpness = origin_capture, origin_sharp


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        store = StateStore(tmp / "state.db")
        test_sort(store)
        store.close()
        test_migrate_old_db(tmp)
        test_config_writeback(tmp)
        test_fallback_frames(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== 结果：{checks - failed}/{checks} 通过 =====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
