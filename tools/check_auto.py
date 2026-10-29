# -*- coding: utf-8 -*-
"""自动化任务验收：接口 + 「自动化」页（新建/编辑/停用/立即执行/删除）。

用法：
    python tools/check_auto.py                 # 默认打 6670
    python tools/check_auto.py --base http://127.0.0.1:6670
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def api(base: str, path: str, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    verb = method or ("POST" if payload is not None else "GET")
    req = urllib.request.Request(base + path, data=data, method=verb,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def expect_400(base: str, path: str, payload: dict) -> int:
    try:
        api(base, path, payload)
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


def find_task(data: dict, task_id: str) -> dict | None:
    for task in data.get("tasks") or []:
        if task["id"] == task_id:
            return task
    return None


def wait_idle(base: str, timeout: int = 240) -> dict:
    last = {}
    for _ in range(timeout):
        last = api(base, "/api/automation")
        if not last["status"].get("busy") and not last["status"].get("running_task"):
            return last
        time.sleep(1)
    return last


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:6670")
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")

    print("========== 1. 接口：任务列表结构 ==========")
    data = api(base, "/api/automation")
    check("有 tasks 列表", isinstance(data.get("tasks"), list), str(data)[:120])
    check("带可监控的媒体库根目录", len(data.get("roots") or []) > 0, str(data.get("roots")))
    status = data["status"]
    check("状态里有条数/下次时间", "count" in status and "next_run" in status,
          str(status)[:140])
    check("状态里有「启动后先跑一轮」", "autostart" in status)

    # 清掉上一轮遗留的测试任务，保证从干净状态开始
    for task in data["tasks"]:
        if task["name"].startswith("验收-"):
            api(base, "/api/automation/delete", {"id": task["id"]})

    root = (data["roots"] or [""])[0]
    files = api(base, "/api/files?path=" + urllib.parse.quote(root))
    subdir = ""
    for row in files.get("dirs") or []:
        # 目录里可能还有几层，展开一次才知道里面到底有没有 strm
        expanded = api(base, "/api/files/expand",
                       {"dirs": [row["path"]], "recursive": True, "limit": 20})
        if expanded.get("count"):
            subdir = row["path"]
            break
    check("媒体库里有可监控的子文件夹", bool(subdir), str(files.get("dirs"))[:120])

    print("\n========== 2. 接口：新建任务（监控单个文件夹） ==========")
    created = api(base, "/api/automation/save", {
        "name": "验收-电影库", "scope": subdir,
        "mode": "incremental", "interval_minutes": 30, "enabled": True,
    })
    task = created["tasks"][-1] if created["tasks"] else {}
    task_id = task.get("id")
    check("新建成功并出现在列表里", bool(task_id), str(created)[:140])
    check("监控范围保存为所选文件夹", task.get("scope") == subdir,
          f"{task.get('scope')} != {subdir}")
    check("间隔 30 分钟", task.get("interval_minutes") == 30, str(task.get("interval_minutes")))
    check("next_run 约等于 30 分钟后",
          abs((task.get("next_run") or 0) - (time.time() + 1800)) < 120,
          str(task.get("next_run")))
    check("创建后默认启用", task.get("enabled") is True)
    check("状态里的任务数同步", created["status"]["count"] >= 1)

    print("\n========== 3. 接口：参数校验 ==========")
    check("间隔小于 5 分钟被拒",
          expect_400(base, "/api/automation/save",
                     {"name": "x", "interval_minutes": 1, "scope": ""}) == 400)
    check("间隔超过 7 天被拒",
          expect_400(base, "/api/automation/save",
                     {"name": "x", "interval_minutes": 90000, "scope": ""}) == 400)
    check("执行方式非法被拒",
          expect_400(base, "/api/automation/save",
                     {"name": "x", "interval_minutes": 60, "scope": "",
                      "mode": "whatever"}) == 400)
    check("监控目录越界被拒",
          expect_400(base, "/api/automation/save",
                     {"name": "x", "interval_minutes": 60, "scope": "/etc"}) == 400)
    check("改不存在的任务被拒",
          expect_400(base, "/api/automation/save",
                     {"id": "nope", "name": "x", "interval_minutes": 60}) == 404)

    print("\n========== 4. 接口：修改 / 停用 / 启用 ==========")
    changed = api(base, "/api/automation/save", {
        "id": task_id, "name": "验收-电影库", "scope": subdir,
        "mode": "incremental", "interval_minutes": 60, "enabled": True,
    })
    row = find_task(changed, task_id) or {}
    check("改间隔后立即重算下次时间",
          abs((row.get("next_run") or 0) - (time.time() + 3600)) < 120,
          str(row.get("next_run")))

    off = api(base, "/api/automation/toggle", {"id": task_id, "enabled": False})
    row = find_task(off, task_id) or {}
    check("停用后不再有时间点", row.get("enabled") is False and row.get("next_run") is None,
          str(row.get("next_run")))
    check("停用后调度器下次时间为空",
          off["status"]["next_run"] is None or off["status"]["enabled_count"] >= 1)
    on = api(base, "/api/automation/toggle", {"id": task_id, "enabled": True})
    row = find_task(on, task_id) or {}
    check("重新启用后有时间点", row.get("next_run") is not None)

    print("\n========== 5. 接口：立即执行这条任务 ==========")
    started = api(base, "/api/automation/run", {"id": task_id})
    check("立即执行被受理", started.get("started") is True, str(started)[:140])
    check("提示里带任务名", "验收-电影库" in (started.get("message") or ""),
          started.get("message", ""))
    row = find_task(started, task_id) or {}
    check("算作一次运行", (row.get("runs") or 0) >= 1, str(row.get("runs")))

    final = wait_idle(base)
    row = find_task(final, task_id) or {}
    check("跑完写回结果", "完成" in (row.get("last_result") or ""), row.get("last_result", ""))
    check("上次运行时间已记录", row.get("last_run") is not None)
    check("执行后顺延到下一个间隔",
          (row.get("next_run") or 0) > time.time(), str(row.get("next_run")))

    print("\n========== 6. 接口：只跑监控范围（别的目录不动） ==========")
    # 任务范围是这个子文件夹：任务页里该目录之外的文件不应该被重新处理
    items = api(base, "/api/items?size=200")
    outside = [row for row in items["items"]
               if not str(row["path"]).startswith(subdir)
               and row["status"] not in ("ok", "skipped")]
    check("范围外的文件没被重扫", not outside,
          f"{len(outside)} 条状态异常：{[r['path'] for r in outside][:2]}")

    print("\n========== 7. 持久化 / 删除 ==========")
    again = api(base, "/api/automation")
    check("任务可从库里重新读出", find_task(again, task_id) is not None)
    removed = api(base, "/api/automation/delete", {"id": task_id})
    check("删除后列表里没有了", find_task(removed, task_id) is None)
    check("重复删除返回 404",
          expect_400(base, "/api/automation/delete", {"id": task_id}) == 404)

    print("\n========== 8. 界面：独立「自动化」标签页 ==========")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 940})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text[:120])
                if m.type == "error" else None)
        page.goto(base, wait_until="networkidle")

        tabs = page.eval_on_selector_all(
            "#tabs .tab", "els => els.map(e => e.dataset.tab)")
        order = ["servers", "automation", "logs"]
        check("自动化在「媒体服务器」和「运行日志」之间",
              all(t in tabs for t in order)
              and tabs.index(order[0]) + 1 == tabs.index(order[1])
              and tabs.index(order[1]) + 1 == tabs.index(order[2]), str(tabs))

        page.click('button[data-tab="settings"]')
        page.wait_for_timeout(600)
        check("设置页里不再有自动化卡片", page.locator("#setAuto").count() == 0)

        page.click('button[data-tab="automation"]')
        page.wait_for_selector("#autoList", timeout=15000)
        page.wait_for_timeout(800)
        check("自动化页有新建按钮", page.is_visible("#btnAutoAdd"))
        check("页头摘要显示任务数", "条任务" in page.inner_text("#autoSummary")
              or "暂无任务" in page.inner_text("#autoSummary"),
              page.inner_text("#autoSummary"))

        # 新建：选一个文件夹 + 快捷间隔
        page.click("#btnAutoAdd")
        page.wait_for_selector("#autoModal:not([hidden])", timeout=8000)
        check("弹窗里有监控范围目录列表",
              page.locator("#autoDirs button").count() > 0)
        page.fill("#autoName", "验收-界面任务")
        # 进第一个子目录，再设为监控范围
        page.locator('#autoDirs button').first.click()
        page.wait_for_timeout(900)
        page.click("#autoScopeHere")
        check("已选监控目录", "已选当前目录" in page.inner_text("#autoScopePick"),
              page.inner_text("#autoScopePick"))
        page.click('#autoChips button[data-min="360"]')
        check("快捷间隔写入 360", page.input_value("#autoInterval") == "360",
              page.input_value("#autoInterval"))
        page.click("#btnAutoSave")
        page.wait_for_timeout(1600)
        check("保存后弹窗关闭", not page.is_visible("#autoModal"))
        card = page.locator(".auto-card").filter(has_text="验收-界面任务")
        check("列表里出现新卡片", card.count() == 1, str(card.count()))
        text = card.inner_text() if card.count() else ""
        check("卡片显示监控路径", "监控" in text and ("\\" in text or "/" in text), text[:120])
        check("卡片显示每 6 小时", "每 6 小时" in text, text[:120])
        check("卡片显示下次时间", "下次" in text, text[:120])

        # 编辑：改成全量 + 写个新名字
        card.locator('[data-act="edit"]').click()
        page.wait_for_selector("#autoModal:not([hidden])", timeout=8000)
        check("编辑弹窗带出原值", page.input_value("#autoInterval") == "360",
              page.input_value("#autoInterval"))
        page.select_option("#autoMode", "full")
        page.click("#btnAutoSave")
        page.wait_for_timeout(1500)
        check("改成全量后卡片有标记",
              "全量" in page.locator(".auto-card").filter(has_text="验收-界面任务").inner_text())

        # 停用 / 立即执行
        page.locator(".auto-card").filter(has_text="验收-界面任务") \
            .locator('[data-act="toggle"]').click()
        page.wait_for_timeout(1200)
        card = page.locator(".auto-card").filter(has_text="验收-界面任务")
        check("停用后显示「已停用」", "已停用" in card.inner_text(), card.inner_text()[:120])
        card.locator('[data-act="toggle"]').click()
        page.wait_for_timeout(1200)
        check("再次启用成功", "启用中" in page.locator(".auto-card")
              .filter(has_text="验收-界面任务").inner_text())

        page.locator(".auto-card").filter(has_text="验收-界面任务") \
            .locator('[data-act="run"]').click()
        page.wait_for_timeout(3500)
        check("立即执行有提示", "扫描" in page.inner_text("#toast"),
              page.inner_text("#toast"))

        # 删除（confirm 一律确认）
        page.on("dialog", lambda d: d.accept())
        wait_idle(base, timeout=180)
        page.reload(wait_until="networkidle")
        page.click('button[data-tab="automation"]')
        page.wait_for_selector("#autoList", timeout=15000)
        page.wait_for_timeout(800)
        card = page.locator(".auto-card").filter(has_text="验收-界面任务")
        if card.count():
            card.locator('[data-act="del"]').click()
            page.wait_for_timeout(1500)
        check("删除后卡片消失",
              page.locator(".auto-card").filter(has_text="验收-界面任务").count() == 0)
        check("无 JS 报错", not errors, str(errors[:3]))
        browser.close()

    # 收尾：别给用户留测试任务
    left = api(base, "/api/automation")
    for task in left["tasks"]:
        if task["name"].startswith("验收-"):
            api(base, "/api/automation/delete", {"id": task["id"]})
    final = api(base, "/api/automation")
    check("收尾清掉验收任务",
          not [t for t in final["tasks"] if t["name"].startswith("验收-")])

    print(f"\n===== 自动化验收：{PASS} 项通过，{FAIL} 项失败 =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
