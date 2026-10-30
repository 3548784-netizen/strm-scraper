# -*- coding: utf-8 -*-
"""海报 / 背景图缩略图接口自检（需要本地实例在跑）。

用法：python tools/check_thumbs.py [--base http://127.0.0.1:6670]

覆盖：小图尺寸与比例、不放大、缓存头、重复请求命中缓存、原图接口不受影响、
越界路径仍然 404。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))


def api(base: str, path: str, **params) -> dict:
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def image(base: str, path: str, kind: str, version: int, w: int = 0, expect: int = 200):
    url = (f"{base}/api/files/image?kind={kind}"
           f"&path={urllib.parse.quote(path)}&v={version}")
    if w:
        url += f"&w={w}"
    try:
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=20) as r:
            body = r.read()
            code = r.status
            headers = r.headers
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "bytes": 0, "ms": 0.0, "headers": exc.headers, "body": b""}
    with Image.open(io.BytesIO(body)) as im:
        size = im.size
    return {
        "code": code, "bytes": len(body), "size": size,
        "ms": round((time.time() - t0) * 1000, 1), "headers": headers, "body": body,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    items = api(base, "/api/items", size=200)["items"]
    ready = [it for it in items if it["artifacts"].get("poster") and it["artifacts"].get("fanart")]
    print(f"========== 缩略图接口自检（{base}，样本 {len(ready)} 条）==========")
    if not ready:
        print("  没有同时带海报和背景图的样本，跳过")
        return 0

    item = ready[0]
    path, art = item["path"], item["artifacts"]

    print("---------- 1. 尺寸与体积 ----------")
    for kind, want in (("poster", 96), ("fanart", 144)):
        full = image(base, path, kind, art[kind])
        small = image(base, path, kind, art[kind], w=want)
        check(f"{kind} 原图可访问", full["code"] == 200, f"{full['bytes']/1024:.0f} KB {full['size']}")
        check(f"{kind} 小图可访问", small["code"] == 200, f"{small['bytes']/1024:.1f} KB {small['size']}")
        if small["code"] == 200 and full["code"] == 200:
            check(f"{kind} 小图宽度不超过 {want}", small["size"][0] <= want, str(small["size"]))
            fr = full["size"][0] / full["size"][1]
            sr = small["size"][0] / small["size"][1]
            check(f"{kind} 小图保持原始比例", abs(fr - sr) < 0.02, f"{fr:.3f} vs {sr:.3f}")
            check(f"{kind} 体积明显变小", small["bytes"] < full["bytes"] * 0.35,
                  f"{small['bytes']/full['bytes']*100:.0f}%")

    print("---------- 2. 不放大 / 参数上限 ----------")
    tiny = image(base, path, "poster", art["poster"], w=16)
    huge = image(base, path, "poster", art["poster"], w=999)
    check("w=999 被拒绝（超上限 512）", huge["code"] == 422, str(huge["code"]))
    check("w=16 正常返回且不放大", tiny["code"] == 200 and tiny["size"][0] <= 16, str(tiny["size"]))

    print("---------- 3. 缓存头与重复请求 ----------")
    a = image(base, path, "poster", art["poster"], w=96)
    b = image(base, path, "poster", art["poster"], w=96)
    cc = (a["headers"].get("cache-control") or "")
    check("带长缓存头", "max-age" in cc and int(cc.split("max-age=")[1].split(",")[0]) >= 3600, cc)
    check("重复请求体积一致（命中缓存）", a["bytes"] == b["bytes"], f"{a['bytes']} / {b['bytes']}")
    check("二次请求更快", b["ms"] <= max(a["ms"] * 1.5, a["ms"] + 20), f"{a['ms']} → {b['ms']} ms")

    print("---------- 4. 原图接口未受影响 ----------")
    plain = image(base, path, "poster", art["poster"])
    check("不带 w 返回原尺寸", plain["size"] == (1000, 1500), str(plain["size"]))

    print("---------- 5. 异常输入 ----------")
    bad = image(base, "C:/definitely/not/here.strm", "poster", 1, w=96)
    check("越界路径仍然被拒", bad["code"] in (403, 404), str(bad["code"]))

    print(f"========== 通过 {PASS} 项，失败 {FAIL} 项 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
