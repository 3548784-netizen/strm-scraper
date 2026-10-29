# -*- coding: utf-8 -*-
"""登录与账号回归测试：登录 → 设置页改用户名/密码 → 旧密码失效。

自己起一套独立数据（``.devdata_auth``）和端口，不碰主测试环境：

    python tools/selftest_auth.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".devdata_auth"
PORT = 6671
BASE = f"http://127.0.0.1:{PORT}"
FAIL = []

NEW_USER = "看片达人"
NEW_PWD = "admin12345"


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


def api(path, data=None):
    if data is None:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return json.load(r)
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return {"__status__": exc.code, "detail": exc.read().decode()[:200]}


def wait_ready(seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/login", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.6)
    return False


def main() -> int:
    shutil.rmtree(DATA, ignore_errors=True)
    env = dict(os.environ, WS_DEVDATA=str(DATA))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools/devserver.py"), "--auth",
         "--port", str(PORT), "--media-port", "6681"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready():
            print("测试环境没起来，检查端口 6671 是否被占用")
            return 2

        print("========== 1. 登录 ==========")
        try:
            urllib.request.urlopen(BASE + "/api/settings", timeout=5)
            code = 200
        except urllib.error.HTTPError as exc:
            code = exc.code
        check("未登录访问接口返回 401", code == 401, str(code))

        with sync_playwright() as p:
            b = p.chromium.launch(channel="msedge", headless=True)
            pg = b.new_page(viewport={"width": 1500, "height": 940})
            errs, bad = [], []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            # 故意用错密码会有一条 401 的资源加载报错，那是预期行为，别算成 JS 报错
            pg.on("console", lambda m: errs.append("console: " + m.text)
                  if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.on("response", lambda r: bad.append(f"{r.status} {r.url}")
                  if r.status >= 500 else None)
            pg.on("dialog", lambda d: d.accept())

            pg.goto(BASE, wait_until="networkidle")
            check("未登录打开首页跳到登录页", "/login" in pg.url, pg.url)
            pg.fill("#username", "admin")
            pg.fill("#password", "admin")
            pg.click("#btn")
            pg.wait_for_timeout(2000)
            check("admin / admin 能登录", "/login" not in pg.url, pg.url)

            print("========== 2. 设置页账号区 ==========")
            pg.click('[data-tab="settings"]'); pg.wait_for_timeout(1200)
            check("设置页显示当前账号", "admin" in pg.inner_text("#setAccHint"),
                  pg.inner_text("#setAccHint"))
            check("提示还在用默认密码", "默认密码" in pg.inner_text("#setAccHint"),
                  pg.inner_text("#setAccHint"))
            check("用户名已回填", pg.input_value("#accUser") == "admin", pg.input_value("#accUser"))

            print("========== 3. 改用户名 + 密码 ==========")
            pg.fill("#accUser", NEW_USER)
            pg.fill("#accCurrent", "admin")
            pg.fill("#accNew", NEW_PWD)
            pg.fill("#accConfirm", NEW_PWD)
            pg.click("#btnSaveAccount"); pg.wait_for_timeout(1800)
            msg = pg.inner_text("#accMsg")
            check("保存成功提示", "用户名改为" in msg and "密码已更新" in msg, msg)
            # 改完账号旧 cookie 立刻失效，用浏览器里的新会话去查
            acc = pg.evaluate("() => fetch('/api/account').then(r => r.json())")
            check("接口里的用户名已变", acc["username"] == NEW_USER, str(acc["username"]))
            check("不再是默认密码", acc["default_password"] is False)

            print("========== 4. 旧密码失效 / 新密码可用 ==========")
            pg.click("#btnLogoutTop"); pg.wait_for_timeout(1800)
            check("退出后回到登录页", "/login" in pg.url, pg.url)
            pg.fill("#username", "admin")
            pg.fill("#password", "admin")
            pg.click("#btn")
            pg.wait_for_timeout(1500)
            check("旧密码登不进去", "/login" in pg.url, pg.url)

            pg.fill("#username", NEW_USER)
            pg.fill("#password", NEW_PWD)
            pg.click("#btn")
            pg.wait_for_timeout(2200)
            check("新账号能登录", "/login" not in pg.url, pg.url)

            pg.click('[data-tab="settings"]'); pg.wait_for_timeout(1200)
            check("设置页显示新用户名", NEW_USER in pg.inner_text("#setAccHint"),
                  pg.inner_text("#setAccHint"))
            check("无 JS 报错", not errs, " | ".join(errs[:3]))
            check("无 5xx 请求", not bad, " | ".join(bad[:3]))
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 下次用 --auth 启动时还是 admin / admin
        shutil.rmtree(DATA, ignore_errors=True)

    print()
    print("========== 结果 ==========")
    print("失败项:", FAIL if FAIL else "无")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
