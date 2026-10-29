"""自动化任务：到点自动扫库 + 刮新文件。

一个任务 = 一条「监控范围 + 间隔 + 执行方式」的规则。范围可以是某个媒体库
根目录，也可以是库里的任意子文件夹；到点只跑这个范围里的 strm，别的目录
不会被顺带拉起来。

任务存在 state.db 的 meta 里（不写回 yaml），网页上增删改立即生效，重启后
接着按上次的时间跑。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from .config import Config
from .media import iter_sources
from .state import StateStore
from .worker import Runner

log = logging.getLogger(__name__)

META_TASKS = "auto_tasks"

MIN_INTERVAL = 5          # 分钟；太短会把网盘打爆
DEFAULT_INTERVAL = 360


def _task_id() -> str:
    return "t" + uuid.uuid4().hex[:8]


class TaskNotFound(ValueError):
    """任务 id 对不上（多半是另一个标签页刚删掉了）。"""


class Scheduler:
    """管理多条自动化任务，到点依次拉起扫描。

    - 任务落库到 state.db，容器重启后自动恢复
    - 任意时刻只有一轮任务在跑；到点时若还在跑，记一笔跳过并顺延
    - 增删改任务后立即重排下一次时间，不用重启
    """

    def __init__(self, cfg: Config, runner: Runner, store: StateStore | None = None):
        self.cfg = cfg
        self.runner = runner
        self.store = store
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 正在由哪条任务触发（跑完把成功/失败数写回这条任务）
        self._running_task: str | None = None

        self._tasks: list[dict] = []
        self._load()

    # ---------- 任务存取 ----------

    def tasks(self) -> list[dict]:
        """给前端看的任务列表（按创建顺序）。"""
        with self._lock:
            return [dict(task) for task in self._tasks]

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            for task in self._tasks:
                if task["id"] == task_id:
                    return dict(task)
        return None

    def save(self, payload: dict, task_id: str | None = None) -> dict:
        """新建或修改一条任务，返回保存后的任务。

        参数不合法抛 ``ValueError``，由网页那边转成 400 明确告诉用户。
        """
        data = payload or {}
        task_id = task_id or str(data.get("id") or "").strip()

        existing = None
        with self._lock:
            for task in self._tasks:
                if task["id"] == task_id:
                    existing = task
                    break
            if task_id and existing is None:
                raise TaskNotFound("这条任务不存在，可能已经被删掉了")

            base = existing or {
                "id": _task_id(), "runs": 0,
                "last_run": None, "last_result": "", "next_run": None,
            }
            name = str(data.get("name") or base.get("name") or "").strip()
            if not name:
                name = self._default_name(data.get("scope", base.get("scope", "")))
            scope = self._clean_scope(data.get("scope", base.get("scope", "")))
            mode = str(data.get("mode", base.get("mode", "incremental"))).lower()
            if mode not in ("incremental", "full"):
                raise ValueError("执行方式只能是增量或全量")
            interval = data.get("interval_minutes", base.get("interval_minutes", DEFAULT_INTERVAL))
            try:
                interval = int(interval)
            except (TypeError, ValueError):
                raise ValueError("间隔要填整数分钟") from None
            if interval < MIN_INTERVAL:
                raise ValueError(f"间隔不能小于 {MIN_INTERVAL} 分钟")
            if interval > 10080:
                raise ValueError("间隔最多 7 天（10080 分钟）")
            enabled = bool(data.get("enabled", base.get("enabled", True)))

            now = time.time()
            previous_interval = base.get("interval_minutes")
            base.update({
                "name": name[:60], "scope": scope, "mode": mode,
                "interval_minutes": interval, "enabled": enabled,
            })
            if enabled:
                if (base.get("next_run") is None or previous_interval != interval
                        or data.get("reset_timer")):
                    base["next_run"] = now + interval * 60
            else:
                base["next_run"] = None

            if existing is None:
                self._tasks.append(base)
            saved = dict(base)
        self._persist()
        log.info("自动化任务已保存：%s（%s，每 %d 分钟，%s）",
                 saved["name"], saved["scope"] or "全部媒体库", saved["interval_minutes"],
                 "启用" if saved["enabled"] else "停用")
        return saved

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t["id"] != task_id]
            removed = len(self._tasks) != before
            if self._running_task == task_id:
                self._running_task = None
        if removed:
            self._persist()
            log.info("自动化任务已删除：%s", task_id)
        return removed

    def set_enabled(self, task_id: str, enabled: bool) -> dict:
        with self._lock:
            task = next((t for t in self._tasks if t["id"] == task_id), None)
            if task is None:
                raise TaskNotFound("这条任务不存在")
            task["enabled"] = bool(enabled)
            if enabled:
                task["next_run"] = time.time() + task["interval_minutes"] * 60
            else:
                task["next_run"] = None
            saved = dict(task)
        self._persist()
        log.info("自动化任务「%s」已%s", saved["name"], "启用" if enabled else "停用")
        return saved

    def run_now(self, task_id: str, reason: str = "手动") -> dict:
        with self._lock:
            task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task is None:
            raise TaskNotFound("这条任务不存在")
        return self._fire(task, reason)

    # ---------- 状态 ----------

    def status(self) -> dict:
        with self._lock:
            enabled = [t for t in self._tasks if t.get("enabled")]
            next_runs = [t["next_run"] for t in enabled if t.get("next_run")]
            payload = {
                "count": len(self._tasks),
                "enabled_count": len(enabled),
                "enabled": bool(enabled),
                "autostart": bool(self.cfg.scan.autostart),
                "next_run": min(next_runs) if next_runs else None,
                "running_task": self._running_task,
            }
        payload["busy"] = self.runner.busy()
        payload["now"] = time.time()
        return payload

    # ---------- 线程 ----------

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="ws-scheduler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reconfigure(self, announce: bool = True) -> None:
        """媒体库目录改过之后调用：把不在库里的范围标出来，时间不动。"""
        with self._lock:
            for task in self._tasks:
                scope = task.get("scope") or ""
                if scope and not self._scope_exists(scope):
                    task["last_result"] = "监控目录已不在媒体库内，等改设置"
        if announce:
            count = len(self._tasks)
            log.info("自动化任务已按新设置重新排期（共 %d 条）", count)

    # ---------- 内部 ----------

    def _default_name(self, scope: str) -> str:
        text = str(scope or "").strip()
        if not text:
            return "全部媒体库"
        name = Path(text).name
        return name or "自动化任务"

    def _roots(self) -> list[Path]:
        found: list[Path] = []
        for root in self.cfg.scan.roots:
            try:
                found.append(Path(root).resolve())
            except OSError:
                continue
        return found

    def _scope_exists(self, scope: str) -> bool:
        try:
            path = Path(scope).resolve()
        except OSError:
            return False
        return path.is_dir()

    def _clean_scope(self, raw) -> str:
        """空 = 全部媒体库；否则必须是媒体库根目录或它下面的子目录。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute():
            raise ValueError("监控目录要写容器内的绝对路径")
        try:
            target = path.resolve()
        except OSError as exc:
            raise ValueError(f"路径无法解析：{exc}") from None
        for root in self._roots():
            if target == root or root in target.parents:
                return str(target)
        raise ValueError("这个目录不在媒体库范围内，先在设置里把上级目录加进去")

    def _collect(self, scope: str) -> list[str]:
        """把监控目录展开成 strm 列表（递归子目录）。"""
        if not self._scope_exists(scope):
            return []
        return [str(p) for p in iter_sources(
            [scope], self.cfg.scan.extensions, recursive=True)]

    def _load(self) -> None:
        if self.store is None:
            return
        raw = ""
        try:
            raw = self.store.get_meta(META_TASKS, "")
        except OSError:
            raw = ""
        loaded: list[dict] = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    loaded = [t for t in data if isinstance(t, dict) and t.get("id")]
            except (ValueError, TypeError):
                log.warning("自动化任务记录解析失败，已忽略")
        if not loaded:
            # 老版本只有一个全局间隔：升级后自动变成一条「全部媒体库」任务
            interval = max(0, int(self.cfg.scan.interval_minutes or 0))
            if interval >= MIN_INTERVAL:
                loaded = [{
                    "id": _task_id(), "name": "全部媒体库",
                    "scope": "", "mode": getattr(self.cfg.scan, "schedule_mode", "incremental"),
                    "interval_minutes": interval, "enabled": True,
                    "next_run": time.time() + interval * 60,
                    "last_run": None, "last_result": "", "runs": 0,
                }]
                log.info("已把原来的「每 %d 分钟」定时设置转成一条自动化任务", interval)

        for task in loaded:
            task.setdefault("scope", "")
            task.setdefault("mode", "incremental")
            task.setdefault("interval_minutes", DEFAULT_INTERVAL)
            task.setdefault("enabled", True)
            task.setdefault("last_run", None)
            task.setdefault("last_result", "")
            task.setdefault("runs", 0)
            if not task.get("enabled"):
                task["next_run"] = None
            elif not task.get("next_run"):
                task["next_run"] = time.time() + task["interval_minutes"] * 60
        self._tasks = loaded
        if loaded:
            log.info("已载入 %d 条自动化任务", len(loaded))

    def _persist(self) -> None:
        if self.store is None:
            return
        with self._lock:
            payload = json.dumps(self._tasks, ensure_ascii=False)
        try:
            self.store.set_meta(META_TASKS, payload)
        except OSError:
            log.warning("自动化任务写库失败", exc_info=True)

    @staticmethod
    def _fmt(ts: float | None) -> str:
        if not ts:
            return "—"
        return time.strftime("%m-%d %H:%M", time.localtime(ts))

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(1.0):
                return
            try:
                self._tick()
            except Exception:  # noqa: BLE001 定时线程不能死
                log.exception("定时任务执行出错")

    def _tick(self) -> None:
        self._settle()
        now = time.time()
        with self._lock:
            due = [dict(t) for t in self._tasks
                   if t.get("enabled") and t.get("next_run") and now >= t["next_run"]]
        for task in due:
            if self.runner.busy():
                # 前面的任务还没跑完，这条顺延，别堆着
                self._reschedule(task["id"], skip_note=True)
                continue
            self._fire(task, "定时")

    def _settle(self) -> None:
        """上一轮跑完了，把结果写回触发它的那条任务。"""
        with self._lock:
            task_id = self._running_task
            if not task_id or self.runner.busy():
                return
            self._running_task = None
            result = f"完成：成功 {self.runner.succeeded}，失败 {self.runner.failed}"
            for task in self._tasks:
                if task["id"] == task_id:
                    task["last_result"] = result
                    break
        self._persist()
        log.info("自动化本轮%s", result)

    def _reschedule(self, task_id: str, skip_note: bool = False) -> None:
        with self._lock:
            for task in self._tasks:
                if task["id"] != task_id:
                    continue
                if task.get("enabled"):
                    task["next_run"] = time.time() + task["interval_minutes"] * 60
                if skip_note:
                    task["last_result"] = "上一轮还在跑，本次跳过"
                break
        self._persist()

    def _fire(self, task: dict, reason: str) -> dict:
        task_id = task["id"]
        now = time.time()
        if self.runner.busy():
            self._reschedule(task_id, skip_note=True)
            log.info("%s触发跳过：「%s」排队中，上一轮还在跑", reason, task["name"])
            return {"ok": False, "started": False,
                    "message": "上一轮还没跑完，这次先跳过（已顺延到下一轮）"}

        full = task.get("mode") == "full"
        scope = task.get("scope") or ""
        if scope:
            paths = self._collect(scope)
            if not paths:
                self._reschedule(task_id)
                with self._lock:
                    for item in self._tasks:
                        if item["id"] == task_id:
                            item["last_result"] = "监控目录里没有 strm 文件"
                self._persist()
                return {"ok": False, "started": False,
                        "message": "监控目录里没有 strm 文件，检查一下路径"}
            # 范围模式：只碰这个目录里的文件，force 决定要不要覆盖已有产物
            started = self.runner.start(only=paths, force=full)
        else:
            started = self.runner.start(full=full, force=full)

        with self._lock:
            for item in self._tasks:
                if item["id"] != task_id:
                    continue
                if item.get("enabled"):
                    item["next_run"] = now + item["interval_minutes"] * 60
                if started:
                    item["runs"] = int(item.get("runs") or 0) + 1
                    item["last_run"] = now
                    item["last_result"] = "正在跑（全量重刮）" if full else "正在跑（增量）"
                break
            if started:
                self._running_task = task_id
        if not started:
            return {"ok": False, "started": False, "message": "任务已经在跑了"}

        self._persist()
        log.info("%s触发「%s」：%s扫描%s%s", reason, task["name"],
                 "全量" if full else "增量",
                 f"（只跑 {scope}）" if scope else "（全部媒体库）",
                 f"，下次 {self._fmt(task.get('next_run'))}" if task.get("enabled") else "")
        return {
            "ok": True, "started": True,
            "message": f"「{task['name']}」已开始{'全量' if full else '增量'}扫描"
                       + (f"，只处理 {scope}" if scope else ""),
        }
