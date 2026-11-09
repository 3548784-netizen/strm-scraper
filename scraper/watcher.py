"""实时监听：目录里一出现新的 strm 就入队刮削，不用等下一轮定时。

定时轮询有个绕不过去的矛盾：间隔短了，每轮都要整库走一遍目录树，大库和网盘
扛不住；间隔长了，新文件要等半天才被发现。实时监听走另一条路——文件落地时让
操作系统通知我们，只处理那几个新文件，连目录树都不用遍历，所以可以很勤快。

几个必须处理的现实问题：

- **事件风暴**：批量生成一次就是几百上千个文件，一个事件触发一次刮削会把队列
  冲垮。收到事件先攒着，静默一段时间（默认 60 秒）再一次性入队。
- **自己触发自己**：刮削会写出海报、NFO、缩略图，这些同样产生事件。只认扫描
  扩展名（strm 和 mp4 / mkv 这样的本地视频），别的一律忽略，否则就是死循环。
- **监听可能根本不生效**：网络盘、rclone / alist 挂载目录，以及 Docker 把
  Windows 目录挂进 Linux 容器这类跨系统挂载，宿主端的文件变化不会产生容器内
  的文件系统事件。所以启用监听后先自检——往目录里写一个探针文件，看能不能收到
  它的事件；收不到就明确降级为定时轮询，并在界面上说清原因，绝不假装在监听。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path

from .media import is_media_file, wanted_extensions

log = logging.getLogger(__name__)

try:  # 依赖没装也不能让整个服务起不来，只是没有实时监听
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG = True
    IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]
    WATCHDOG = False
    IMPORT_ERROR = str(exc)

MIN_DEBOUNCE = 10       # 秒；再短就攒不住批量生成了
MAX_DEBOUNCE = 900
DEFAULT_DEBOUNCE = 60   # 115 批量落盘通常持续几十秒
PROBE_WAIT = 6.0        # 自检最多等这么久，等不到就判定收不到事件
PROBE_PREFIX = ".strm-watch-probe"
BUSY_RETRY = 30.0       # 上一轮还在跑时，过多久再试


def _norm(path: str) -> str:
    """统一成可比较的绝对路径（Windows 上大小写不敏感）。"""
    try:
        return os.path.normcase(os.path.abspath(path)).rstrip(os.sep)
    except OSError:
        return os.path.normcase(str(path)).rstrip(os.sep)


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    """把所有事件交给 Watcher 自己分拣，它知道每条任务管哪几个目录。"""

    def __init__(self, owner: "Watcher"):
        self.owner = owner

    def on_any_event(self, event) -> None:  # noqa: D102
        try:
            self.owner._on_event(event)
        except Exception:  # noqa: BLE001 事件回调不能把监听线程打死
            log.exception("处理文件事件出错")


class Watcher:
    """按自动化任务维护目录监听，攒够静默时间后回调入队。

    ``on_batch(task_id, paths)`` 由调度器实现，返回 True 表示真的入队了；
    返回 False（比如上一轮还在跑）会把这批文件留着稍后再试。
    """

    def __init__(self, cfg, on_batch):
        self.cfg = cfg
        self._on_batch = on_batch
        self._lock = threading.RLock()
        self._observer = None
        self._watches: dict[str, list] = {}       # task_id -> [ObservedWatch]
        self._dirs: dict[str, list[str]] = {}     # task_id -> 监听的目录
        self._pending: dict[str, set[str]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._state: dict[str, dict] = {}         # task_id -> 界面要显示的状态
        # 自检探针：目录 -> 正在等这个目录事件的 Event 集合。
        # 原来是一份实例级共享状态（_probe_hit / _probe_dir / _probe_waiting），
        # 两个任务同时自检时会互相覆盖：A 的探针事件可能被 B 收到并判成功，
        # 或者 B 改了 _probe_dir 让 A 永远等不到自己的事件。按目录分桶后，
        # 同目录的多个任务共享一次自检，不同目录互不干扰。
        self._probes: dict[str, set[threading.Event]] = {}

    # ---------- 对外 ----------

    @property
    def available(self) -> bool:
        """watchdog 在不在；不在的话所有任务都只能走定时轮询。"""
        return WATCHDOG

    @property
    def unavailable_reason(self) -> str:
        if WATCHDOG:
            return ""
        return f"没装 watchdog，实时监听用不了（{IMPORT_ERROR}）"

    def dirs_for(self, scope: str) -> list[str]:
        """任务要监听的目录：留空就是全部媒体库根目录。"""
        text = str(scope or "").strip()
        if text:
            return [text] if Path(text).is_dir() else []
        found = []
        for root in self.cfg.scan.roots:
            try:
                if Path(root).is_dir():
                    found.append(str(root))
            except OSError:
                continue
        return found

    def sync(self, tasks: list[dict]) -> None:
        """按当前任务列表启停监听；改动任务后调用一次即可。"""
        wanted: dict[str, tuple[list[str], int]] = {}
        for task in tasks:
            tid = str(task.get("id") or "")
            if not tid or not task.get("enabled") or not task.get("watch"):
                continue
            dirs = self.dirs_for(task.get("scope") or "")
            if not dirs:
                continue
            debounce = task.get("debounce_sec") or DEFAULT_DEBOUNCE
            try:
                debounce = int(debounce)
            except (TypeError, ValueError):
                debounce = DEFAULT_DEBOUNCE
            debounce = max(MIN_DEBOUNCE, min(MAX_DEBOUNCE, debounce))
            wanted[tid] = (dirs, debounce)

        with self._lock:
            for tid in list(self._watches):
                if tid not in wanted:
                    self._stop_watch(tid)
            for tid in list(self._state):
                if tid not in wanted and tid not in self._watches:
                    self._state.pop(tid, None)
            for tid, (dirs, debounce) in wanted.items():
                same = (self._dirs.get(tid) == dirs
                        and self._state.get(tid, {}).get("debounce") == debounce)
                if same and self._watches.get(tid):
                    continue
                self._stop_watch(tid)
                self._start_watch(tid, dirs, debounce)

    def state(self, task_id: str) -> dict:
        with self._lock:
            data = dict(self._state.get(task_id) or {})
            data["pending"] = len(self._pending.get(task_id) or ())
        return data

    def states(self) -> dict[str, dict]:
        with self._lock:
            return {tid: dict(self.state(tid)) for tid in self._state}

    def stop(self) -> None:
        with self._lock:
            for tid in list(self._watches):
                self._stop_watch(tid)
            observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=3)
            except RuntimeError:
                pass

    # ---------- 内部 ----------

    def _ensure_observer(self):
        if self._observer is None:
            self._observer = Observer()
            self._observer.start()
            log.info("实时监听已启动")
        return self._observer

    def _start_watch(self, tid: str, dirs: list[str], debounce: int) -> None:
        if not WATCHDOG:
            self._state[tid] = {"active": False, "debounce": debounce,
                                "reason": self.unavailable_reason}
            return
        try:
            observer = self._ensure_observer()
            handler = _Handler(self)
            handles = [observer.schedule(handler, path, recursive=True) for path in dirs]
        except OSError as exc:
            self._state[tid] = {"active": False, "debounce": debounce,
                                "reason": f"监听目录失败：{exc}"}
            log.warning("实时监听启动失败 %s：%s", dirs, exc)
            return
        self._watches[tid] = handles
        self._dirs[tid] = list(dirs)
        self._pending[tid] = set()
        # 自检结果出来之前不能说自己「监听中」——探针还没回来，谁知道这个目录
        # 到底给不给事件。界面上先显示「检测中」，几秒内会有结论。
        self._state[tid] = {"active": False, "checking": True, "debounce": debounce,
                            "reason": "正在确认能不能收到文件事件…"}
        threading.Thread(target=self._probe, args=(tid, dirs[0]),
                         daemon=True, name=f"watch-probe-{tid}").start()
        log.info("实时监听：任务 %s 监听 %s（静默 %d 秒后入队）",
                 tid, "、".join(dirs), debounce)

    def _stop_watch(self, tid: str) -> None:
        handles = self._watches.pop(tid, [])
        if self._observer is not None:
            for handle in handles:
                try:
                    self._observer.unschedule(handle)
                except Exception:  # noqa: BLE001
                    pass
        self._dirs.pop(tid, None)
        timer = self._timers.pop(tid, None)
        if timer is not None:
            timer.cancel()
        self._pending.pop(tid, None)
        state = self._state.get(tid)
        if state:
            state["active"] = False

    def _wanted(self, name: str) -> bool:
        """只认扫描扩展名：刮削写出的海报 / NFO 不算，否则自己触发自己。"""
        return is_media_file(name, wanted_extensions(self.cfg.scan.extensions))

    def _on_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        path = getattr(event, "dest_path", "") or getattr(event, "src_path", "")
        if not path:
            return
        name = os.path.basename(path)
        if name.startswith(PROBE_PREFIX):
            key = _norm(os.path.dirname(path))
            with self._lock:
                waiting = list(self._probes.get(key) or ())
            for event in waiting:
                event.set()
            return
        if not self._wanted(name):
            return

        target = _norm(path)
        with self._lock:
            for tid, dirs in self._dirs.items():
                hit = False
                for directory in dirs:
                    base = _norm(directory)
                    if target == base or target.startswith(base + os.sep):
                        hit = True
                        break
                if not hit:
                    continue
                self._pending.setdefault(tid, set()).add(path)
                self._arm(tid)

    def _arm(self, tid: str, delay: float | None = None) -> None:
        """重新计时：静默 delay 秒没有新事件，才把攒下的文件入队。"""
        old = self._timers.pop(tid, None)
        if old is not None:
            old.cancel()
        seconds = delay
        if seconds is None:
            seconds = float(self._state.get(tid, {}).get("debounce") or DEFAULT_DEBOUNCE)
        timer = threading.Timer(seconds, self._flush, args=(tid,))
        timer.daemon = True
        self._timers[tid] = timer
        timer.start()

    def _flush(self, tid: str) -> None:
        with self._lock:
            paths = sorted(self._pending.pop(tid, set()))
            self._timers.pop(tid, None)
        if not paths:
            return
        try:
            started = bool(self._on_batch(tid, paths))
        except Exception:  # noqa: BLE001
            log.exception("实时监听入队失败")
            started = False
        if started:
            log.info("实时监听：任务 %s 入队 %d 个新文件", tid, len(paths))
            return
        # 上一轮还在跑：留着，过一会儿再试，别丢
        with self._lock:
            self._pending[tid] = set(paths)
            self._arm(tid, BUSY_RETRY)
        log.info("实时监听：上一轮还没跑完，%d 个文件 %.0f 秒后再试",
                 len(paths), BUSY_RETRY)

    # ---------- 自检 ----------

    def _probe(self, tid: str, directory: str) -> None:
        """写个探针文件，看这个目录到底能不能收到事件。"""
        ok = self._probe_once(directory)
        with self._lock:
            if tid not in self._watches:
                return  # 自检期间任务被改掉了，不用管
            state = self._state.setdefault(tid, {})
            state["checking"] = False
            if ok:
                state["active"] = True
                state["reason"] = ""
                log.info("实时监听自检通过：%s", directory)
            else:
                state["active"] = False
                state["reason"] = (
                    "这个目录收不到文件事件，实时监听已停用，改回定时轮询"
                    "（网络盘 / 挂载盘 / 容器跨系统挂载都这样）")
                log.warning("实时监听自检失败，任务 %s 降级为定时轮询：%s", tid, directory)
                self._stop_watch(tid)

    def _probe_once(self, directory: str) -> bool:
        probe = Path(directory) / f"{PROBE_PREFIX}-{uuid.uuid4().hex[:8]}.tmp"
        key = _norm(directory)
        event = threading.Event()
        with self._lock:
            self._probes.setdefault(key, set()).add(event)
        try:
            try:
                probe.write_bytes(b"probe")
            except OSError as exc:
                log.info("实时监听自检写不了探针文件 %s：%s", probe, exc)
                return False
            deadline = time.monotonic() + PROBE_WAIT
            while time.monotonic() < deadline:
                if event.wait(0.25):
                    return True
            return False
        finally:
            with self._lock:
                bucket = self._probes.get(key)
                if bucket is not None:
                    bucket.discard(event)
                    if not bucket:
                        self._probes.pop(key, None)
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
