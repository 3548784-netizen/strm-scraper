# -*- coding: utf-8 -*-
"""入口：初始化配置、日志、状态库，按需启动网页与定时任务。

除了正常启动，还提供 ``--doctor`` 自检模式：把环境、路径、ffmpeg、媒体服务器
连通性一次查完，出问题时先跑这个再看日志，比盯着容器日志猜快得多。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import specs
from .auth import AuthStore
from .config import Config
from .logs import LogFiles, setup_logging
from .mediaserver import ServerPool
from .probe import ProbeError, find_binary
from .state import StateStore
from .web import create_app
from .worker import Runner

log = logging.getLogger("main")


def build_refresher(pool: ServerPool):
    """刮削结束后刷新各台媒体服务器图片的钩子。"""
    if not pool.servers():
        return None

    def refresh(paths: list[str]) -> None:
        pool.refresh(paths)

    return refresh


def log_environment(cfg: Config, log_files: LogFiles) -> None:
    """启动时把环境信息打一遍，出问题看日志第一段就有线索。"""
    log.info("Python %s / %s", sys.version.split()[0], sys.platform)
    for name in ("ffmpeg", "ffprobe"):
        try:
            path = find_binary(name)
            output = subprocess.run(
                [path, "-version"], capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0,
            ).stdout.splitlines()
            log.info("%s：%s（%s）", name, path, output[0][:70] if output else "版本未知")
        except (ProbeError, OSError, subprocess.SubprocessError) as exc:
            log.error("%s 不可用：%s", name, exc)
    log.info("图片规格：%s", cfg.image_summary())
    log.info("日志文件：%s", log_files.describe())


def wait_idle(runner: Runner) -> None:
    while runner.busy():
        time.sleep(1.0)


def decide_full(cfg: Config, store: StateStore) -> bool:
    """按配置决定这一轮是全量还是增量。"""
    mode = cfg.scan.mode.lower()
    if mode == "full":
        return True
    if mode == "incremental":
        return False
    if mode == "auto":
        return store.get_meta("first_run_done", "") != "1"
    return False


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------

def doctor(cfg: Config, log_files: LogFiles) -> int:
    """把常见故障点逐项检查一遍，返回退出码。"""
    problems = 0
    log.info("=" * 68)
    log.info("自检开始")
    log.info("=" * 68)

    log.info("Python：%s（%s）", sys.version.split()[0], sys.platform)

    for name in ("ffmpeg", "ffprobe"):
        try:
            path = find_binary(name)
            result = subprocess.run(
                [path, "-version"], capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            version = (result.stdout.splitlines() or ["版本未知"])[0][:70]
            log.info("%s：OK %s（%s）", name, path, version)
        except (ProbeError, OSError, subprocess.SubprocessError) as exc:
            log.error("%s：失败 %s", name, exc)
            problems += 1

    poster, fanart = cfg.scrape.poster, cfg.scrape.fanart
    log.info("海报规格：%s×%s（2:3，%s 模式）", poster.width, poster.height, poster.mode)
    log.info("背景规格：%s×%s（16:9）", fanart.width, fanart.height)
    if not specs.is_conforming(poster.width, poster.height, "poster"):
        log.error("海报比例不符合 2:3，Emby/Jellyfin 会拉伸显示")
        problems += 1
    if not specs.is_conforming(fanart.width, fanart.height, "fanart"):
        log.error("背景图比例不符合 16:9，Emby/Jellyfin 会裁切显示")
        problems += 1

    for root in cfg.scan.roots:
        base = Path(root)
        if not base.is_dir():
            log.error("扫描目录不存在：%s（检查 docker volume 挂载）", base)
            problems += 1
            continue
        count = sum(1 for _ in base.rglob("*.strm")) if cfg.scan.recursive else \
            sum(1 for _ in base.glob("*.strm"))
        log.info("扫描目录：%s 存在，找到 %s 个 strm 文件", base, count)

    db_path = Path(cfg.storage.state_db)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = StateStore(db_path)
        stats = store.stats()
        store.close()
        log.info("状态库：%s 可写，已有记录 %s（成功 %s / 失败 %s）",
                 db_path, stats["total"], stats["ok"] + stats["skipped"], stats["failed"])
    except Exception as exc:  # noqa: BLE001
        log.error("状态库不可用：%s（检查卷权限）", exc)
        problems += 1

    log.info("日志文件：%s", log_files.describe())
    if not log_files.main:
        log.warning("日志没有写文件，出问题只能看 docker logs")

    if not cfg.web.auth.enabled:
        log.warning("网页登录已关闭（web.auth.enabled=false），能访问端口的人都能操作")
    else:
        try:
            account = AuthStore(cfg.web.auth.file, cfg.web.auth.session_hours)
            log.info("登录账号：%s（文件 %s）", account.describe(), account.path)
            if account.default_password:
                log.warning("还在用默认密码 admin / admin，登录后请到右上角「账号」里改掉")
        except Exception as exc:  # noqa: BLE001
            log.error("账号文件不可用：%s（检查 %s 所在目录的写权限）", exc, cfg.web.auth.file)
            problems += 1

    if not cfg.servers:
        log.info("媒体服务器：未配置（只生成文件，不联动 Emby / Jellyfin）")
    for server in cfg.servers:
        if not server.enabled:
            log.info("媒体服务器 %s：未启用", server.name)
            continue
        pool = ServerPool(cfg)
        info = pool.test(server, force=True)
        if info.ok:
            log.info("媒体服务器 %s：OK %s（%s，鉴权 %s）",
                     server.name, info.detail, info.kind, pool.client(server).auth_style)
            if not server.path_map:
                log.warning("%s 没配 path_map。如果容器内路径和服务端不一样，"
                            "刷新会匹配不到条目", server.name)
        else:
            log.error("媒体服务器 %s：失败 %s", server.name, info.detail)
            problems += 1

    log.info("=" * 68)
    if problems:
        log.warning("自检完成，发现 %s 个问题，见上面 ERROR", problems)
    else:
        log.info("自检完成，全部正常")
    return 1 if problems else 0


# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strm-scraper",
        description="115 / LitePan strm 刮削器：生成海报、背景图与 NFO（Emby / Jellyfin 通用）",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("--full", action="store_true", help="本次强制全量重刮")
    parser.add_argument("--once", action="store_true", help="跑完一轮就退出，不常驻")
    parser.add_argument("--no-web", action="store_true", help="不启动网页控制台")
    parser.add_argument("--scan-only", action="store_true", help="只扫描登记文件，不刮削")
    parser.add_argument("--doctor", action="store_true", help="自检环境与配置后退出")
    parser.add_argument("--log-level", help="临时覆盖日志级别（DEBUG/INFO/WARNING）")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    level = args.log_level or cfg.log.level
    log_buffer, log_files = setup_logging(
        level, cfg.log.dir, cfg.log.file, cfg.log.max_mb,
        cfg.log.backups, cfg.log.console,
    )

    log.info("配置：%s", cfg.describe())
    log_environment(cfg, log_files)
    for root in cfg.scan.roots:
        if not Path(root).is_dir():
            log.warning("扫描目录不存在：%s（检查 docker volume 挂载）", root)

    if args.doctor:
        return doctor(cfg, log_files)

    store = StateStore(cfg.storage.state_db)
    pool = ServerPool(cfg)
    runner = Runner(cfg, store, on_refresh=build_refresher(pool))

    if pool.servers():
        pool.test_all()
    else:
        log.info("未配置媒体服务器，只生成文件")

    stopping = {"flag": False}

    def handle_signal(signum, _frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        log.info("收到退出信号 %s，正在收尾…", signum)
        runner.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.scan_only:
        full = args.full or decide_full(cfg, store)
        runner.start(full=full)
        wait_idle(runner)
        store.close()
        return 0

    # 默认不自动开跑：打开网页点「开始任务」才开始。要开机就跑就把
    # scan.autostart 设为 true（定时任务 interval_minutes>0 时也会自动跑）。
    interval = max(0, cfg.scan.interval_minutes)
    auto_start = args.full or args.once or (
        cfg.scan.autostart and cfg.scan.mode.lower() != "manual"
    )
    if auto_start:
        full = args.full or decide_full(cfg, store)
        log.info("按配置自动开始首轮刮削（%s）", "全量" if full else "增量")
        runner.start(full=full)
        if args.once:
            wait_idle(runner)
            store.set_meta("first_run_done", "1")
            store.close()
            return 0
    else:
        log.info("未自动开始刮削，打开网页控制台点「开始任务」即可（scan.autostart=false）")

    if interval <= 0 and not auto_start:
        log.warning("未启用定时任务（scan.interval_minutes=0），只能通过网页手动触发")

    app = None
    if not args.no_web and cfg.web.enabled:
        account = None
        if cfg.web.auth.enabled:
            try:
                account = AuthStore(cfg.web.auth.file, cfg.web.auth.session_hours)
            except OSError as exc:
                log.error("账号文件无法写入：%s（%s）", cfg.web.auth.file, exc)
                log.error("把 web.auth.file 改成可写路径，本地运行可用 ./data/auth.json；"
                          "或者临时设 web.auth.enabled=false 关闭登录")
                return 2
            log.info("网页登录：已启用，%s", account.describe())
            if account.default_password:
                log.warning("默认账号 admin / admin，登录后请到右上角「账号」里改掉")
        else:
            log.warning("网页登录已关闭，任何能访问端口的人都能操作控制台")
        app = create_app(cfg, store, runner, log_buffer, log_files, pool, account)

    # 定时线程
    def scheduler():
        if interval <= 0:
            return
        while not stopping["flag"]:
            for _ in range(interval * 60):
                if stopping["flag"]:
                    return
                time.sleep(1)
            if runner.busy():
                log.info("上一轮还在跑，跳过本次定时")
                continue
            full = cfg.scan.mode.lower() == "full"
            log.info("定时触发（%s）", "全量" if full else "增量")
            runner.start(full=full)

    if interval > 0:
        threading.Thread(target=scheduler, daemon=True, name="ws-scheduler").start()

    # 首轮结束后记录标记，下次就是增量。manual 模式没跑首轮，不能打这个标记
    if auto_start:
        def mark_first_run():
            wait_idle(runner)
            if store.get_meta("first_run_done", "") != "1":
                store.set_meta("first_run_done", "1")
                log.info("首轮完成，之后按增量模式运行")

        threading.Thread(target=mark_first_run, daemon=True, name="ws-firstrun").start()

    if app is None:
        log.info("网页控制台已关闭，进程常驻等待信号")
        while not stopping["flag"]:
            time.sleep(1)
        store.close()
        return 0

    import uvicorn

    log.info("网页控制台：http://%s:%s", cfg.web.host, cfg.web.port)
    config = uvicorn.Config(
        app, host=cfg.web.host, port=cfg.web.port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        runner.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
