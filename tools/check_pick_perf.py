# -*- coding: utf-8 -*-
"""选帧界面「加载慢 / 偶尔失败」修复的验收。

分两段：

  * 离线单元：探测缓存、缩略图快速通道、同秒原图复用、并发配额、取消事件。
    直接 patch 掉 ffmpeg 调用，不依赖服务，随时可跑。
  * 浏览器集成：给 /api/frames/frame 注入延迟、一次偶发 504 和一次持续 504，
    量候选帧是不是 3 路并发、有没有等主帧、偶发失败能否自动补回、持续失败
    的格子能否点重试、拖进度条会不会取消过期请求。

用法：
    python tools/check_pick_perf.py                        # 只跑离线段
    python tools/check_pick_perf.py --base http://127.0.0.1:6687
    python tools/check_pick_perf.py --base http://127.0.0.1:6687 --require-ui

退出码非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from scraper import frames, probe, picker  # noqa: E402
from scraper.config import Config  # noqa: E402

FAIL: list[str] = []
PASS = 0
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


def jpeg_bytes(w: int = 260, h: int = 146) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (92, 138, 205)).save(buf, "JPEG")
    return buf.getvalue()


def write_jpg(path: Path, w: int = 320, h: int = 180) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(w, h))


# ---------------------------------------------------------------- 离线单元

def offline_checks() -> None:
    print("\n---------- 1. 探测结果缓存（不再每次开面板都 ffprobe） ----------")
    tmp = Path(tempfile.mkdtemp(prefix="pickperf_"))
    src = tmp / "clip.mp4"
    src.write_bytes(b"fake video")
    pk = picker.FramePicker(Config(), tmp / "cache")
    calls = {"probe": 0}
    original_probe = probe.probe

    def fake_probe(source, timeout=20, abort=None):
        calls["probe"] += 1
        return probe.MediaInfo(source=source, duration=120.0, size=12345,
                               video=probe.StreamInfo(width=1920, height=1080))

    probe.probe = fake_probe
    try:
        target = SimpleNamespace(source=src)
        a = pk.plan(target, 12)
        b = pk.plan(target, 12)
        duration = pk.duration(src)
        check("连续两次 plan + duration 只探测一次", calls["probe"] == 1,
              f"probe={calls['probe']}")
        check("缓存命中仍返回完整时长 / 尺寸",
              a["duration"] == 120 and a["width"] == 1920
              and b["candidates"] == a["candidates"] and duration == 120)
        check("12 个候选时间点照常给出", len(a["candidates"]) == 12)
        os.utime(src, (time.time() + 5, time.time() + 5))
        pk.plan(target, 12)
        check("源文件改过之后会重新探测", calls["probe"] == 2,
              f"probe={calls['probe']}")
    finally:
        probe.probe = original_probe

    print("\n---------- 2. 缩略图快速通道 vs 主帧精确定位 ----------")
    log: dict[str, list] = {"capture": [], "capture_at": []}
    orig_capture, orig_capture_at = frames.capture, frames.capture_at

    def fake_capture(source, seconds, output, timeout=60, fast_seek=True,
                     width=0, min_bytes=None, abort=None):
        log["capture"].append((seconds, timeout, fast_seek, width, abort))
        write_jpg(output, width or 320, 146 if width else 180)
        return True

    def fake_capture_at(source, seconds, output, timeout=60, width=0,
                        abort=None):
        log["capture_at"].append((seconds, timeout, width, abort))
        write_jpg(output, width or 320, 146 if width else 180)
        return True

    frames.capture, frames.capture_at = fake_capture, fake_capture_at
    try:
        small = pk.frame(src, 10.0, 260, fast=True)
        check("快速通道只跑一次关键帧跳转（不退化慢速解码定位）",
              len(log["capture"]) == 1 and not log["capture_at"],
              f"capture={len(log['capture'])} capture_at={len(log['capture_at'])}")
        check("快速通道用短超时且 fast_seek=True",
              log["capture"][0][1] == picker.FAST_TIMEOUT
              and log["capture"][0][2] is True, str(log["capture"][0][:3]))
        check("快速通道产物是有效 JPEG", small.is_file()
              and Image.open(small).width == 260)

        pk.frame(src, 11.0, 0, fast=False)
        check("主帧仍走精确定位通道 capture_at", len(log["capture_at"]) == 1)
        check("主帧超时沿用扫描配置（30s）", log["capture_at"][0][1] == 30,
              str(log["capture_at"][0][1]))

        ev = threading.Event()
        pk.frame(src, 12.0, 260, fast=True, abort=ev)
        check("取消事件一路传到 ffmpeg", log["capture"][-1][4] is ev)

        print("\n---------- 3. 同秒原图缓存复用 ----------")
        pk.frame(src, 20.0, 0)          # 先备好主帧原图
        before = len(log["capture"]) + len(log["capture_at"])
        def boom(*_a, **_kw):
            raise AssertionError("不该再抓一次 ffmpeg")
        frames.capture, frames.capture_at = boom, boom
        reused = pk.frame(src, 20.0, 260, fast=True)
        check("缩略图直接由同秒原图缩放生成，零 ffmpeg",
              len(log["capture"]) + len(log["capture_at"]) == before,
              f"{len(log['capture']) + len(log['capture_at'])} vs {before}")
        check("复用出来的小图宽度正确且有效", reused.is_file()
              and Image.open(reused).width == 260)
    finally:
        frames.capture, frames.capture_at = orig_capture, orig_capture_at

    print("\n---------- 4. 并发配额：缩略图限 3 路、主帧不被饿死 ----------")
    state = {"cur": 0, "max": 0}
    guard = threading.Lock()

    def slow_capture(source, seconds, output, timeout=60, fast_seek=True,
                     width=0, min_bytes=None, abort=None):
        with guard:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.22)
        write_jpg(output)
        with guard:
            state["cur"] -= 1
        return True

    frames.capture = slow_capture
    try:
        threads = [threading.Thread(
            target=lambda s=s: pk.frame(src, 30.0 + s, 260, fast=True))
            for s in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("8 张缩略图并发峰值不超过 3", 2 <= state["max"] <= picker.THUMB_PARALLEL,
              f"max={state['max']}")
    finally:
        frames.capture = orig_capture

    # 4 张缩略图占满 3 路配额后，第 4 个全局槽必须还能让主帧立刻进来
    main_cost = {"elapsed": 0.0}
    def slow_full(source, seconds, output, timeout=60, width=0, abort=None):
        time.sleep(0.1)
        write_jpg(output)
        return True
    frames.capture, frames.capture_at = slow_capture, slow_full
    try:
        busy = [threading.Thread(
            target=lambda s=s: pk.frame(src, 50.0 + s, 260, fast=True))
            for s in range(4)]
        for t in busy:
            t.start()
        time.sleep(0.08)
        t0 = time.monotonic()
        pk.frame(src, 60.0, 0, fast=False)
        main_cost["elapsed"] = time.monotonic() - t0
        for t in busy:
            t.join()
        check("缩略图忙时主帧仍能立刻拿到槽位", main_cost["elapsed"] < 0.35,
              f"{main_cost['elapsed']:.3f}s")
    finally:
        frames.capture, frames.capture_at = orig_capture, orig_capture_at

    print("\n---------- 5. 主帧请求接管（拖进度条不靠 ASGI 断开通知） ----------")
    from scraper.web import FrameAbortRegistry
    reg = FrameAbortRegistry()
    a = reg.take("p|tab1")
    b = reg.take("p|tab1")
    check("同一标签的新主帧请求会取消旧抓帧", a.is_set() and not b.is_set())
    c = reg.take("p|tab2")
    check("另一个标签页的请求不会被误取消", not c.is_set())
    reg.release("p|tab1", a)
    d = reg.take("p|tab1")
    check("旧请求收尾不会把新请求从注册表抹掉", b.is_set() and not d.is_set())
    reg.release("p|tab1", d)
    e = reg.take("p|tab1")
    check("正常收尾后新请求是全新的", not e.is_set())


# ---------------------------------------------------------------- 浏览器集成

def api_get(base: str, path: str, timeout: float = 10):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        import json
        return json.load(r)


def find_media(base: str) -> str | None:
    try:
        cfg = api_get(base, "/api/config")
        roots = cfg.get("scan", {}).get("roots") or []
    except Exception as exc:
        print(f"  提示：读不到 /api/config（{exc}）")
        return None
    for root in roots:
        p = Path(root)
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*.strm")):
            url = f"{base}/api/frames/plan?count=4&path={urllib.parse.quote(str(f))}"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    if r.status == 200:
                        return str(f)
            except Exception:
                continue
    return None


async def ui_checks(base: str) -> None:
    from playwright.async_api import async_playwright

    media = find_media(base)
    if not media:
        raise RuntimeError("找不到能抽帧的 .strm，先确认 devserver 的媒体库正常")
    plan = api_get(base, "/api/frames/plan?count=12&path="
                   + urllib.parse.quote(media))
    cands = plan.get("candidates") or []
    if len(cands) < 4:
        raise RuntimeError("候选时间点不足，无法验证缩略图条")
    # 两个注入点：heal_t 只挂一次，验证「偶发失败自动补回」；
    # hard_t 连挂两次（首波 + 自动重试），验证「真坏了才标失败、点重试能救回来」
    heal_t = f"{cands[1]['t']:.2f}"
    hard_t = f"{cands[2]['t']:.2f}"
    pic = jpeg_bytes()

    stats = {
        "thumb_inflight": 0, "thumb_peak": 0,
        "thumb_urls": [], "main_urls": [],
        "thumb_first_at": 0.0, "main_first_at": 0.0,
        "heal_first": 0, "heal_retry": 0, "hard_fail": 0, "retry": 0,
        "aborted": 0, "after_close": 0,
    }

    async def handler(route, request):
        url = request.url
        if "/api/frames/frame" not in url:
            await route.continue_()
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        fast = q.get("fast", [""])[0] == "1"
        retry = "r" in q
        crop = "crop" in q
        main = not fast and not crop and not retry
        now = time.monotonic()
        if main:
            stats["main_urls"].append(url)
            if not stats["main_first_at"]:
                stats["main_first_at"] = now
        if fast:
            if not stats["thumb_first_at"]:
                stats["thumb_first_at"] = now
            stats["thumb_urls"].append(url)
            if retry:
                stats["retry"] += 1
            t = q.get("t", [""])[0]
            if t == heal_t and not retry:
                stats["heal_first"] += 1
                await route.fulfill(status=504, body="")
                return
            if t == heal_t:
                stats["heal_retry"] += 1
            if t == hard_t and stats["hard_fail"] < 2:
                stats["hard_fail"] += 1
                await route.fulfill(status=504, body="")
                return
            stats["thumb_inflight"] += 1
            stats["thumb_peak"] = max(stats["thumb_peak"], stats["thumb_inflight"])
        try:
            await asyncio.sleep(0.55 if main else 0.22)
            await route.fulfill(status=200, content_type="image/jpeg", body=pic)
        except Exception:
            pass                       # 请求已被客户端取消
        finally:
            if fast:
                stats["thumb_inflight"] -= 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("requestfailed", lambda r: stats.__setitem__(
            "aborted", stats["aborted"] + 1)
            if r.failure and "ERR_ABORTED" in str(r.failure) else None)
        await page.route("**/api/frames/frame*", handler)
        await page.goto(base, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(900)

        print("\n---------- 5. 候选帧并发加载 / 不再等主帧 ----------")
        t0 = time.monotonic()
        await page.evaluate("p => openPicker(p)", media)
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('#pickStrip figure.pending').length === 0",
                timeout=20000)
        except Exception as exc:
            print(f"  提示：等候选帧超时 {str(exc)[:80]}")
        elapsed = time.monotonic() - t0
        total_thumbs = len(stats["thumb_urls"])
        check("12 个候选帧都带 fast=1",
              total_thumbs >= 12 - 2 and all("fast=1" in u for u in stats["thumb_urls"]),
              f"count={total_thumbs}")
        check("请求带标签页 sid，多窗口不会互相取消",
              bool(stats["main_urls"])
              and all("sid=" in u for u in stats["main_urls"] + stats["thumb_urls"]),
              f"main={len(stats['main_urls'])}")
        check("缩略图确实是并发补的（峰值 ≥2）", stats["thumb_peak"] >= 2,
              f"peak={stats['thumb_peak']}")
        check("整条胶片条加载明显快于 12 次串行往返", elapsed < 2.4,
              f"{elapsed:.2f}s（串行基线约 2.6s+）")
        check("缩略图不用等主帧回来才开工",
              0 <= stats["thumb_first_at"] - stats["main_first_at"] < 0.35,
              f"Δ={stats['thumb_first_at'] - stats['main_first_at']:.3f}s")
        check("加载过程没有 JS 异常", not errors, "; ".join(errors[:2]))

        print("\n---------- 6. 偶发失败自动补回 / 持续失败可点重试 ----------")
        check("偶发 504 触发了自动补一次请求",
              stats["heal_first"] == 1 and stats["heal_retry"] >= 1,
              f"first={stats['heal_first']} retry={stats['heal_retry']}")
        heal_ok = await page.evaluate("""t => {
          const fig = [...document.querySelectorAll('#pickStrip figure')]
            .find(f => Math.abs(parseFloat(f.dataset.t) - parseFloat(t)) < 0.01);
          const im = fig && fig.querySelector('img');
          return !!im && !fig.classList.contains('failed')
                 && !!im.src && !im.dataset.src;
        }""", heal_t)
        check("偶发失败的那格已自动补回、不用手动点", bool(heal_ok))
        failed = await page.locator("#pickStrip figure.failed").count()
        check("只有持续失败的那格被标失败（不是留空黑框）", failed == 1, f"count={failed}")
        if failed:
            content = await page.evaluate(
                """() => getComputedStyle(
                    document.querySelector('#pickStrip figure.failed'), '::after').content""")
            check("失败格有「点击重试」提示", "重试" in str(content), str(content))
            before = stats["retry"]
            await page.locator("#pickStrip figure.failed").click()
            await page.wait_for_timeout(700)
            check("点击后发出重试请求", stats["retry"] > before,
                  f"retry {before} -> {stats['retry']}")
            check("重试成功后失败标记消失",
                  await page.locator("#pickStrip figure.failed").count() == 0)

        print("\n---------- 7. 过期主帧请求会被取消 ----------")
        await page.evaluate("""async () => {
          for (const t of [2, 3, 4, 5]) {
            selectFrame(t, true);
            await new Promise(r => setTimeout(r, 70));
          }
        }""")
        await page.wait_for_timeout(200)
        check("连续换时间点时旧主帧 fetch 被 AbortController 取消",
              stats["aborted"] >= 1, f"aborted={stats['aborted']}")

        print("\n---------- 8. 关面板后不再继续补图 ----------")
        await page.evaluate("closePicker()")
        before = len(stats["thumb_urls"])
        await page.wait_for_timeout(700)
        stats["after_close"] = len(stats["thumb_urls"]) - before
        check("关闭后面板不再发新的缩略图请求", stats["after_close"] == 0,
              f"新增={stats['after_close']}")
        check("全流程没有 JS 异常", not errors, "; ".join(errors[:2]))

        print("\n---------- 9. 真实抓帧接口（前面几段被拦截，这里直连服务端） ----------")
        q = urllib.parse.urlencode({"path": media, "t": f"{cands[2]['t']:.2f}"})

        def real_get(url: str):
            with urllib.request.urlopen(url, timeout=40) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()

        for label, extra in (("主帧原图", ""),
                             ("快速缩略图", "&width=260&fast=1"),
                             ("裁切预览", "&width=520&crop=0.1,0.1,0.5,0.5")):
            try:
                status, ctype, body = await asyncio.to_thread(
                    real_get, f"{base}/api/frames/frame?{q}{extra}")
                check(f"{label}：200 且返回 JPEG",
                      status == 200 and "image/jpeg" in ctype
                      and body[:2] == b"\xff\xd8",
                      f"{status} {ctype} {len(body)}B")
            except Exception as exc:
                check(f"{label}：200 且返回 JPEG", False, str(exc)[:120])

        await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    ap.add_argument("--require-ui", action="store_true",
                    help="UI 段跑不起来时也按失败处理（发布前用）")
    args = ap.parse_args()

    offline_checks()
    if args.base:
        try:
            asyncio.run(ui_checks(args.base.rstrip("/")))
        except Exception as exc:
            msg = f"UI 段不可用：{exc}"
            if args.require_ui:
                FAIL.append(msg)
                print(f"  FAIL {msg}")
            else:
                SKIPPED.append(msg)
                print(f"  SKIP {msg}")
    else:
        SKIPPED.append("未传 --base，跳过浏览器集成段")

    print(f"\n结果：{PASS} 项通过，{len(FAIL)} 项失败，{len(SKIPPED)} 项跳过")
    for s in SKIPPED:
        print("  跳过：" + s)
    for f in FAIL:
        print("  失败：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
