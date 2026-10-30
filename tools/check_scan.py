# -*- coding: utf-8 -*-
"""检查扫描提速：新扫描结果要和旧的一致，但要明显更快。

跑法：python tools/check_scan.py [文件数]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.media import iter_sources, scan_rows, signature  # noqa: E402

checks = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks, failed
    checks += 1
    if not ok:
        failed += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"    {detail}" if detail else ""))


def build_tree(root: Path, count: int) -> None:
    """造一个贴近真实媒体库的目录树：多级目录、每层几十个文件。"""
    per_dir = 40
    for index in range(count):
        folder = root / "电影" / f"系列{index // per_dir // 5:02d}" / f"第{index // per_dir:03d}组"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"影片.{index:05d}.2024.1080p.strm").write_text(
            f"http://127.0.0.1:6680/v{index}.mp4\n", encoding="utf-8")
    # 放几个必须被跳过的干扰项
    (root / "@eaDir").mkdir(parents=True, exist_ok=True)
    (root / "@eaDir" / "should_skip.strm").write_text("x", encoding="utf-8")
    (root / ".hidden").mkdir(parents=True, exist_ok=True)
    (root / ".hidden" / "should_skip.strm").write_text("x", encoding="utf-8")
    (root / "电影" / "说明.txt").write_text("不是视频", encoding="utf-8")


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    tmp = Path(tempfile.mkdtemp())
    try:
        root = tmp / "media"
        root.mkdir()
        build_tree(root, count)
        print(f"========== 素材：{count} 个 strm（三级目录 + 干扰项） ==========")

        t0 = time.perf_counter()
        old = [(str(p), *signature(p)) for p in iter_sources(
            [str(root)], [".strm"], recursive=True)]
        old_ms = (time.perf_counter() - t0) * 1000

        progress_calls: list[int] = []
        t0 = time.perf_counter()
        new = scan_rows([str(root)], [".strm"], recursive=True,
                        progress=progress_calls.append)
        new_ms = (time.perf_counter() - t0) * 1000

        print(f"  旧（os.walk + 逐个 stat）：{old_ms:7.0f} ms，{len(old)} 个")
        print(f"  新（os.scandir 一次拿全）：{new_ms:7.0f} ms，{len(new)} 个")
        speedup = old_ms / new_ms if new_ms else 0
        print(f"  提速：{speedup:.2f}×")

        print("\n---------- 一致性 ----------")
        check("文件数量一致", len(old) == len(new), f"{len(old)} vs {len(new)}")
        check("路径集合完全一致",
              {row[0] for row in old} == {row[0] for row in new})
        old_map = {row[0]: (row[1], row[2]) for row in old}
        new_map = {row[0]: (row[1], row[2]) for row in new}
        mismatched = [p for p, v in old_map.items() if new_map.get(p) != v]
        check("大小与修改时间一致", not mismatched, f"{len(mismatched)} 个不一致")
        check("跳过了 @eaDir / 隐藏目录 / 非 strm",
              all("@eaDir" not in p and ".hidden" not in p and p.endswith(".strm")
                  for p in new_map))
        check("扫描过程有进度回调", bool(progress_calls),
              f"回调 {len(progress_calls)} 次，最后 {progress_calls[-1] if progress_calls else 0}")

        print("\n---------- 性能门槛 ----------")
        check("比旧实现快", speedup >= 1.5, f"{speedup:.2f}×")
        per_file = new_ms / max(1, len(new))
        check("单个文件平均 < 0.4ms", per_file < 0.4, f"{per_file:.3f} ms/个")
        check(f"{count} 个文件整体 < 1.5s", new_ms < 1500, f"{new_ms:.0f} ms")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== 结果：{checks - failed}/{checks} 通过 =====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
