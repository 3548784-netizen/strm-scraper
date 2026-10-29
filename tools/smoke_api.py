# -*- coding: utf-8 -*-
"""API 冒烟测试：单文件执行 / 暂停中断 / 继续跑完 / 取消不残留。

先 `python tools/devserver.py` 起环境，再 `python tools/smoke_api.py`。
会反复清空刮削产物，不影响源视频。
"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:6670"
import pathlib
MEDIA = str(pathlib.Path(__file__).resolve().parent.parent / ".devdata" / "media")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.load(r)


def post(path, data=None):
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return {"__error__": exc.code, "message": exc.read().decode()[:200]}


def files(folder):
    q = urllib.parse.quote(MEDIA + "\\" + folder)
    return get("/api/files?path=" + q)["files"]


def clear():
    """只删刮削产物（源视频是 .mp4，必须留着）。"""
    import pathlib
    keep = {".mp4", ".strm"}
    for p in pathlib.Path(MEDIA).rglob("*"):
        if p.is_file() and p.suffix.lower() not in keep:
            p.unlink()
    post("/api/task/cancel")
    time.sleep(1.0)


print("=== 1. 单文件立即执行 ===")
movie = [f for f in files("电影") if f["name"].startswith("横版")][0]
print("  文件:", movie["name"], "| 状态:", movie["status"])
r = post("/api/files/scrape", {"paths": [movie["path"]], "now": True})
print("  调用:", r["message"])
for i in range(60):
    time.sleep(0.5)
    st = get("/api/status")
    if st["single"]["count"] == 0:
        break
now = [f for f in files("电影") if f["path"] == movie["path"]][0]
print("  结果状态:", now["status"], "| 产物:", now["artifacts"])

print()
print("=== 2. 暂停能否中断进行中的 ffmpeg ===")
clear()
post("/api/files/scrape", {"paths": [f["path"] for f in files("剧集")]})
time.sleep(0.6)
st = get("/api/status")
print("  启动后 state:", st["state"], "| active:", len(st["active"]))
print("  暂停:", post("/api/task/pause")["message"])
t0 = time.time()
for _ in range(40):
    time.sleep(0.2)
    st = get("/api/status")
    if not st["active"]:
        break
elapsed = time.time() - t0
print("  暂停后 %.1fs：state=%s active=%d pending=%d"
      % (elapsed, st["state"], len(st["active"]), st["stats"]["pending"]))
print("  中断够快（<3s）:", elapsed < 3.0)
before = st["stats"]
time.sleep(3)
after = get("/api/status")["stats"]
print("  暂停 3 秒后统计:", "没涨（对）" if before == after else f"还在涨（错）{before} -> {after}")
print("  暂停期间有没有卡在 running:", after["running"], "（应为 0）")

print()
print("=== 3. 继续 → 队列能接着跑完 ===")
print("  继续:", post("/api/task/resume")["message"])
for _ in range(60):
    time.sleep(0.5)
    st = get("/api/status")
    if st["state"] == "idle":
        break
print("  最终 state:", st["state"], "| stats:", st["stats"])
print("  残留 running:", st["stats"]["running"], "（应为 0）")

print()
print("=== 4. 取消：不残留 running，队列退回 pending ===")
clear()
post("/api/task/start", {"full": True})
time.sleep(0.6)
st = get("/api/status")
print("  启动后 state:", st["state"], "| active:", len(st["active"]), "| pending:", st["stats"]["pending"])
r = post("/api/task/cancel")
print("  取消:", r["message"])
for _ in range(60):
    time.sleep(0.3)
    st = get("/api/status")
    if st["state"] == "idle":
        break
print("  取消后 state:", st["state"], "| stats:", st["stats"])
print("  残留 running:", st["stats"]["running"], "（应为 0）")
print("  队列是否保留（pending>0）:", st["stats"]["pending"])
