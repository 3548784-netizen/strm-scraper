# -*- coding: utf-8 -*-
"""检查刷新媒体服务器时发出去的参数：绝不能带「替换所有图像」。

起因：以前请求里写了 ``ReplaceAllImages: true`` + ``ImageRefreshMode: FullRefresh``，
等于在 Emby / Jellyfin 界面上勾了「替换所有图像」——服务器会把条目的图片先删掉
（删的是媒体目录里 ``<名字>-poster.jpg`` 这些文件本身）再去图像源重抓，
结果刮削器刚写好的图当场清零。

这个脚本起一个假的 Emby，把真实请求抓下来逐项断言，不需要真服务器。

    python tools/check_refresh_payload.py
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.mediaserver import MediaServerClient  # noqa: E402

ITEM_ID = "abc123"
REMOTE_PATH = "/media/电影/样片.strm"

seen: list[dict] = []
checked = failed = 0


def check(label: str, ok: bool, note: str = "") -> None:
    global checked, failed
    checked += 1
    if ok:
        print(f"  OK   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" —— {note}" if note else ""))


class FakeEmby(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # 别把每条请求都打到终端
        return

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/System/Info/Public":
            self._json({"ProductName": "Emby Server", "Version": "4.8.0"})
            return
        if parsed.path == "/System/Info":
            self._json({"ServerName": "假Emby", "Version": "4.8.0"})
            return
        if parsed.path == "/Items":
            self._json({"Items": [{"Id": ITEM_ID, "Path": REMOTE_PATH}],
                        "TotalRecordCount": 1})
            return
        self._json({})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"__raw": raw}
        seen.append({
            "path": parsed.path,
            "query": dict(urllib.parse.parse_qsl(parsed.query)),
            "body": body,
        })
        self.send_response(204)
        self.end_headers()


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        print("== 1. 发一次刷新请求 ==")
        client = MediaServerClient(base, "test-key", kind="emby", timeout=10)
        local = "/media/电影/样片.strm"
        result = client.refresh_paths([local])
        check("匹配到 1 条", result["matched"] == 1, str(result))
        check("提交了 1 次刷新", result["refreshed"] == 1, str(result))

        refreshes = [s for s in seen if s["path"].startswith("/Items/") and
                     s["path"].endswith("/Refresh")]
        check("请求打到 /Items/<id>/Refresh", len(refreshes) == 1, str(seen))
        if not refreshes:
            return 1
        req = refreshes[0]
        body, query = req["body"], req["query"]

        print("== 2. 参数里不能有「替换所有图像」 ==")
        for label, src in (("body", body), ("query", query)):
            raw = json.dumps(src, ensure_ascii=False)
            v = src.get("ReplaceAllImages")
            check(f"{label} 的 ReplaceAllImages 不是 true",
                  v is None or str(v).lower() in ("false", "0"), f"{label}={v!r}")
            mode = src.get("ImageRefreshMode")
            check(f"{label} 的 ImageRefreshMode 不是 FullRefresh",
                  str(mode).lower() != "fullrefresh", f"{label}={mode!r}")
            check(f"{label} 的 ImageRefreshMode 是 Default",
                  str(mode).lower() == "default", f"{label}={mode!r}")
            check(f"{label} 不要求替换全部元数据",
                  str(src.get("ReplaceAllMetadata")).lower() not in ("true", '"true"', "1"),
                  f"{label}={src.get('ReplaceAllMetadata')!r}")

        print("== 3. 仍然会真的刷新，不是啥都不发 ==")
        check("MetadataRefreshMode 有值", bool(body.get("MetadataRefreshMode")),
              json.dumps(body))
        check("路径匹配不上时会报 missing 而不是静默成功",
              client.refresh_paths(["/media/别的/名字.strm"])["missing"] != [])

        print("== 4. 抓到的原始请求内容（便于核对） ==")
        print(f"  query: {json.dumps(query, ensure_ascii=False)}")
        print(f"  body : {json.dumps(body, ensure_ascii=False)}")
    finally:
        server.shutdown()
        server.server_close()

    print(f"\n共 {checked} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
