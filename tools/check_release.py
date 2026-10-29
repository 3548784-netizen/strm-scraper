# -*- coding: utf-8 -*-
"""发版链路自检：版本号、发版脚本、工作流、运行中的实例。

    python tools/check_release.py            # 只查本地
    python tools/check_release.py --live     # 顺便连本地实例（默认 6670）
    python tools/check_release.py --base http://127.0.0.1:6672
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "scraper" / "version.py"
WORKFLOW = ROOT / ".github" / "workflows" / "docker.yml"

passed = failed = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}" + (f"  [{extra}]" if extra else ""))
    else:
        failed += 1
        print(f"  FAIL {name}" + (f"  [{extra}]" if extra else ""))


def load_release():
    spec = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="连一下本地实例")
    ap.add_argument("--base", default="http://127.0.0.1:6670")
    args = ap.parse_args(argv)

    print("===== 1. 版本号 =====")
    check("scraper/version.py 存在", VERSION_FILE.is_file())
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    check("能读到 __version__", bool(m), m.group(1) if m else "")
    version = m.group(1) if m else ""
    check("是 X.Y.Z 三段数字", bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), version)

    print("===== 2. 发版脚本 =====")
    rel = load_release()
    check("读到的版本号一致", rel.current_version() == version, rel.current_version())
    cases = [("patch", "1.0.0", "1.0.1"), ("minor", "1.0.0", "1.1.0"),
             ("major", "1.0.0", "2.0.0"), ("major", "1.9.3", "2.0.0"),
             ("patch", "1.2.9", "1.2.10"), ("1.0", "0.0.0", "1.0.0"),
             ("v2.3.4", "1.0.0", "2.3.4")]
    for ask, cur, want in cases:
        got = rel.normalize(cur, ask)
        check(f"{cur} + {ask} → {want}", got == want, got)

    print("===== 3. 工作流 =====")
    wf = WORKFLOW.read_text(encoding="utf-8")
    check("打 v* 标签会触发", 'tags: ["v*"]' in wf.replace("'", '"'))
    check("镜像带三段版本号", "type=semver,pattern={{version}}" in wf)
    check("镜像带主次版本号", "type=semver,pattern={{major}}.{{minor}}" in wf)
    check("有 release 任务", "\n  release:" in wf)
    check("release 只在打标签时跑", "startsWith(github.ref, 'refs/tags/v')" in wf)
    check("release 有写权限", "contents: write" in wf)
    check("用 gh 创建 Release", "gh release create" in wf)

    print("===== 4. 运行中的实例 =====")
    if not args.live:
        print("  --  跳过（加 --live 才连实例）")
    else:
        try:
            st = json.load(urllib.request.urlopen(f"{args.base}/api/status", timeout=5))
            check("/api/status 带 version", "version" in st, str(st.get("version", "")))
            check("版本与代码一致", st.get("version") == version, str(st.get("version", "")))
            page = urllib.request.urlopen(f"{args.base}/", timeout=5).read().decode("utf-8")
            check("页面里有版本号容器", 'id="appVer"' in page)
        except Exception as exc:  # noqa: BLE001
            check("连得上本地实例", False, str(exc))

    print("===== 5. 标签状态 =====")
    tags = subprocess.run(["git", "tag", "-l", "v*"], cwd=ROOT,
                          capture_output=True, text=True).stdout.split()
    print(f"  --  已有 v* 标签 {len(tags)} 个：{tags[-3:] if tags else '（还没有，首次发版就是 v1.0.0）'}")

    print(f"\n===== {passed} 项通过，{failed} 项失败 =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
