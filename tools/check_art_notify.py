# -*- coding: utf-8 -*-
"""检查「补完文件夹封面后有没有通知媒体服务器重扫那个目录」。

起因：手动删掉 folder.jpg 之后重新补，Emby 里还是裂图。原因是那一层在 Emby
里是「文件夹条目」，不对应任何 strm，按视频条目刷新永远刷不到它；补完必须
单独把目录提交给 ``/Library/Media/Updated``，服务端才会重新读 folder.jpg。

只跑第一部分不需要任何外部服务：

    python tools/check_art_notify.py

带上正在跑的控制台地址会额外做一次端到端（会删掉夹具里的 folder.jpg 再补回来）：

    python tools/check_art_notify.py --base http://127.0.0.1:6670 --lib D:/test
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.mediaserver import MediaServerClient  # noqa: E402

checked = failed = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global checked, failed
    checked += 1
    if ok:
        print(f"  OK   {label}")
        return
    failed += 1
    print(f"  FAIL {label}{'  → ' + detail if detail else ''}")


class Fake:
    """假的 Emby：把收到的请求原样记下来。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                outer.seen.append(("POST", self.path, body))
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                outer.seen.append(("GET", self.path, ""))
                payload = b'{"ServerName":"fake","Version":"4.8.0.0"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()

    def updates(self) -> list[dict]:
        for method, path, body in self.seen:
            if method == "POST" and "Library/Media/Updated" in path:
                return (json.loads(body) or {}).get("Updates") or []
        return []


def part_unit() -> None:
    print("== 1. 目录变更请求打到哪儿、长什么样 ==")
    fake = Fake()
    try:
        client = MediaServerClient(fake.url, "fake-key", kind="emby",
                                   path_map={"D:/media": "/mnt/media"})
        count = client.notify_dirs(["D:/media/电影/甲", "D:/media/电影/乙"])
        check(count == 2, "返回提交的目录数", str(count))
        check(bool(fake.updates()), "请求打到了 /Library/Media/Updated",
              str([s[1] for s in fake.seen]))
        updates = fake.updates()
        check(len(updates) == 2, "两个目录一起提交", str(len(updates)))
        check(all(u.get("UpdateType") == "Created" for u in updates),
              "UpdateType 是 Created")
        check(all(str(u.get("Path", "")).startswith("/mnt/media/") for u in updates),
              "path_map 生效（容器路径换成服务端路径）",
              str([u.get("Path") for u in updates]))
        check(client.notify_dirs([]) == 0, "空列表不发请求")
    finally:
        fake.stop()


def post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def part_live(base: str, lib: str) -> None:
    print(f"== 2. 端到端：补封面后会不会通知（{base}）==")
    fake = Fake()
    root = Path(lib)
    try:
        settings = post(base, "/api/settings", {"servers": [{
            "name": "fake-emby", "type": "emby", "enabled": True,
            "url": fake.url, "api_key": "fake", "refresh_after": True,
            "verify_after": False, "scan_after": False,
        }]})
        check(settings.get("ok") is True, "媒体服务器配置成功")

        killed = 0
        for p in list(root.rglob("*")):
            if p.is_file() and p.name.lower() in ("folder.jpg", "poster.jpg"):
                p.unlink()
                killed += 1
        print(f"  （删掉夹具里的文件夹封面 {killed} 个）")

        # 补封面要靠里层已刮好的 <视频名>-poster.jpg 当源图。夹具里没刮过的层
        # 什么图都没有，那种层本来就补不出来，先给它塞一张，测的才是「补完通知」。
        seed = next((p for p in root.rglob("*-poster.jpg") if p.is_file()), None)
        if seed is not None:
            for folder in [root, *(p for p in root.rglob("*") if p.is_dir())]:
                films = list(folder.glob("*.strm"))
                if films and not any(folder.glob("*-poster.jpg")):
                    # 用视频名命名，别用「文件夹名-poster.jpg」—— 那个名字是
                    # 我们自己的衍生物标记，挑源图时会被主动跳过
                    (folder / f"{films[0].stem}-poster.jpg").write_bytes(seed.read_bytes())

        result = post(base, "/api/files/folder-poster", {"dirs": [str(root)]})
        check(bool(result.get("created")), "封面又补回来了",
              result.get("message", ""))
        check(result.get("notified", 0) > 0, "响应里标了已通知数量",
              str(result.get("notified")))

        deadline = time.time() + 8
        while time.time() < deadline and not fake.updates():
            time.sleep(0.2)
        updates = fake.updates()
        check(bool(updates), "服务器真的收到了目录变更请求")
        check(len(updates) >= len(result.get("created") or []),
              "每个补过封面的目录都提交了",
              f"提交 {len(updates)} / 补了 {len(result.get('created') or [])}")
    finally:
        fake.stop()
        try:  # 收尾：把测试服务器配置清掉，别留在夹具里
            post(base, "/api/settings", {"servers": []})
        except Exception:  # noqa: BLE001
            pass


def part_pin(base: str, lib: str) -> None:
    """指定封面 / 恢复自动 也要通知，不能只写文件。

    这条单独测：批量修复走的是后台线程，单点指定走的是同步路径，两条路的
    通知是各自实现的，容易漏掉一条（第一版就漏了指定这条）。
    """
    print(f"== 3. 端到端：指定封面 / 恢复自动 也要通知（{base}）==")
    fake = Fake()
    root = Path(lib)
    temp = root / "artpin测试"
    try:
        source = next((p for p in root.rglob("*-poster.jpg") if p.is_file()), None)
        if source is None:
            check(False, "夹具里找不到一张 -poster.jpg 当源图")
            return
        temp.mkdir(exist_ok=True)
        seed = temp / "候选-poster.jpg"
        seed.write_bytes(source.read_bytes())

        post(base, "/api/settings", {"servers": [{
            "name": "fake-emby", "type": "emby", "enabled": True,
            "url": fake.url, "api_key": "fake", "refresh_after": True,
            "verify_after": False, "scan_after": False,
        }]})

        pinned = post(base, "/api/files/art-pin",
                      {"dir": str(temp), "source": str(seed)})
        check(pinned.get("ok") is True, "指定成功", pinned.get("message", ""))
        check(pinned.get("notified", 0) > 0, "响应里标了已通知数量",
              str(pinned.get("notified")))
        check("已通知媒体服务器" in str(pinned.get("message")),
              "提示语写明已通知", pinned.get("message", ""))

        deadline = time.time() + 8
        while time.time() < deadline and not fake.updates():
            time.sleep(0.2)
        updates = fake.updates()
        check(bool(updates), "指定后服务器收到了目录变更请求")
        check(all(str(u.get("Path", "")).replace("\\", "/").endswith("artpin测试")
                  for u in updates),
              "提交的正是这一层目录", str([u.get("Path") for u in updates]))

        fake.seen.clear()
        cleared = post(base, "/api/files/art-pin", {"dir": str(temp), "source": ""})
        check(cleared.get("cleared") is True, "恢复自动成功",
              cleared.get("message", ""))
        check(int(cleared.get("files") or 0) > 0, "恢复自动后重画了文件",
              str(cleared.get("files")))
        deadline = time.time() + 8
        while time.time() < deadline and not fake.updates():
            time.sleep(0.2)
        check(bool(fake.updates()), "恢复自动也通知了服务器",
              str([s[1] for s in fake.seen]))
    finally:
        fake.stop()
        try:
            post(base, "/api/settings", {"servers": []})
        except Exception:  # noqa: BLE001
            pass
        if temp.is_dir():
            for path in temp.iterdir():
                path.unlink(missing_ok=True)
            temp.rmdir()


def part_cleanup(base: str, lib: str) -> None:
    """只清掉旧副本、没重画的那一层，也得通知。

    背景：删文件本身就会让客户端的缓存指向一个不存在的文件。图已经齐了就不
    重画是性能优化，但**因此不通知**的话，Emby 那边对应格子的图片 URL 还是
    老路径，请求回来 404 → 用户看到的就是裂图图标。
    """
    print("== 4. 端到端：只清掉旧副本的层也要通知 ==")
    fake = Fake()
    root = Path(lib)
    temp = root / "clean测试"
    inner = temp / "里层"
    try:
        source = next((p for p in root.rglob("*-poster.jpg") if p.is_file()), None)
        if source is None:
            check(False, "夹具里找不到一张 -poster.jpg 当源图")
            return
        inner.mkdir(parents=True, exist_ok=True)
        (inner / "影片X.strm").write_text("http://example.invalid/x.mkv\n",
                                          encoding="utf-8")
        (inner / "影片X-poster.jpg").write_bytes(source.read_bytes())
        # 正本三个齐全，外加旧版多写的三个逐字节副本
        for name in ("folder.jpg", "poster.jpg", "fanart.jpg"):
            (temp / name).write_bytes(source.read_bytes())
        (temp / "backdrop.jpg").write_bytes((temp / "fanart.jpg").read_bytes())
        (temp / f"clean测试-poster.jpg").write_bytes((temp / "folder.jpg").read_bytes())
        (temp / f"clean测试-fanart.jpg").write_bytes((temp / "fanart.jpg").read_bytes())

        post(base, "/api/settings", {"servers": [{
            "name": "fake-emby", "type": "emby", "enabled": True,
            "url": fake.url, "api_key": "fake", "refresh_after": True,
            "verify_after": False, "scan_after": False,
        }]})

        res = post(base, "/api/files/folder-poster", {"dirs": [str(temp)]})
        check("多余的图片副本" in res.get("message", ""), "提示写明清掉了多余副本",
              res.get("message", ""))
        check(not res.get("created"), "图本来就齐，这一层没有重画",
              str(res.get("created")))
        check(res.get("notified", 0) > 0, "只清理也标了已通知",
              str(res.get("notified")))

        deadline = time.time() + 8
        while time.time() < deadline and not fake.updates():
            time.sleep(0.2)
        updates = fake.updates()
        check(bool(updates), "服务器收到了这一层的变更请求", str([s[1] for s in fake.seen]))
        check(any(str(u.get("Path", "")).replace("\\", "/").endswith("clean测试")
                  for u in updates),
              "提交的正是这一层", str([u.get("Path") for u in updates]))
        check(all((temp / n).is_file() for n in ("folder.jpg", "poster.jpg", "fanart.jpg")),
              "正本三个文件都还在",
              str([n for n in ("folder.jpg", "poster.jpg", "fanart.jpg")
                   if not (temp / n).is_file()]))
        check(not (temp / "backdrop.jpg").exists(), "旧副本已经被清掉")
    finally:
        fake.stop()
        try:
            post(base, "/api/settings", {"servers": []})
        except Exception:  # noqa: BLE001
            pass
        for path in sorted(temp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file():
                path.unlink()
        shutil.rmtree(temp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="控制台地址，给了才跑端到端")
    parser.add_argument("--lib", default="", help="端到端用的夹具目录")
    args = parser.parse_args()

    part_unit()
    if args.base and args.lib:
        print()
        part_live(args.base.rstrip("/"), args.lib)
        print()
        part_pin(args.base.rstrip("/"), args.lib)
        print()
        part_cleanup(args.base.rstrip("/"), args.lib)

    print(f"\n共 {checked} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
