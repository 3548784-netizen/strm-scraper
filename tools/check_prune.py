# -*- coding: utf-8 -*-
"""检查镜像标签清理的保留策略（tools/prune_tags.py）。

不需要网络也不需要 docker；纯逻辑层断言，随时可跑：
    python tools/check_prune.py

重点盯两件事：
1. 只删 sha- 开头的标签，latest 和 v1.1.0 这类手工标签永远不碰
   （曾经有一份硬编码黑名单把 v1.0.0 当废弃标签删，打版本号等于白打）
2. GHCR 删的是版本不是标签，同版本还挂着别的标签时必须整条跳过
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import prune_tags  # noqa: E402

checks = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks, failed
    checks += 1
    if not ok:
        failed += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"    {detail}" if detail else ""))


def keep_ids() -> None:
    print("---------- 1. 保留名单 ----------")
    # 时间戳递增表示从旧到新；pick_keep 按时间倒序取最近的 KEEP 个
    tags = [
        ("latest", "2026-11-09T08:29:47Z"),
        ("v1.1.0", "2026-11-09T09:00:00Z"),
        ("sha-aaaaaaa", "2026-11-01T00:00:00Z"),
        ("sha-bbbbbbb", "2026-11-02T00:00:00Z"),
        ("sha-ccccccc", "2026-11-03T00:00:00Z"),
        ("sha-ddddddd", "2026-11-04T00:00:00Z"),
    ]
    keep, drop = prune_tags.pick_keep(tags, {"latest", "sha-ddddddd"})

    check("latest 不被删", "latest" not in drop)
    check("版本号标签不被删（曾因硬编码黑名单被误删）", "v1.1.0" not in drop, "v1.1.0")
    check("版本号标签也不进保留名单", "v1.1.0" not in keep, "它本来就不该被这套逻辑管辖")
    check("当前提交的 sha 受保护", "sha-ddddddd" not in drop)
    check("超出 KEEP 的旧 sha 被删", "sha-aaaaaaa" in drop, "KEEP=3，最旧的该走")
    check("最近 KEEP 个 sha 保留", {"sha-bbbbbbb", "sha-ccccccc"} <= keep, " ".join(sorted(keep)))


def quota_ids() -> None:
    print("---------- 2. 受保护的旧 sha 多留一个 ----------")
    tags = [
        ("sha-aaaaaaa", "2026-11-04T00:00:00Z"),
        ("sha-bbbbbbb", "2026-11-03T00:00:00Z"),
        ("sha-ccccccc", "2026-11-02T00:00:00Z"),
        ("sha-ddddddd", "2026-11-01T00:00:00Z"),
    ]
    # 给旧提交打版本标签时会保护一个不在最近 KEEP 里的 sha：宁可多留，
    # 也不能为了凑够 KEEP 个去删掉更新的构建
    keep, drop = prune_tags.pick_keep(tags, {"latest", "sha-ddddddd"})
    sha_kept = {n for n in keep if n.startswith("sha-")}
    check("受保护的旧 sha 保留", "sha-ddddddd" in keep)
    check("多留一个而不是删新的", len(sha_kept) == 4, " ".join(sorted(sha_kept)))
    check("没有误删其他构建", not drop, str(drop) if drop else "全部保留")


def no_legacy_ids() -> None:
    print("---------- 3. 版本号标签不会被当垃圾清掉 ----------")
    check("模块里不再有 LEGACY_TAGS", not hasattr(prune_tags, "LEGACY_TAGS"))
    # 曾经有一份硬编码黑名单 {"1.0","1.0.0","v1.0.0"}，打这些标签等于白打。
    # 这里用行为断言兜住：任何非 sha- 标签都不许出现在删除名单里。
    tags = [(f"sha-{'a' * 7}", "2026-11-01T00:00:00Z"),
            (f"sha-{'b' * 7}", "2026-11-02T00:00:00Z"),
            (f"sha-{'c' * 7}", "2026-11-03T00:00:00Z"),
            (f"sha-{'d' * 7}", "2026-11-04T00:00:00Z"),
            ("v1.0.0", "2026-10-29T00:00:00Z"),
            ("v1.1.0", "2026-11-09T00:00:00Z")]
    _, drop = prune_tags.pick_keep(tags, {"latest"})
    check("删除名单里只有 sha- 标签",
          all(n.startswith("sha-") for n in drop), " ".join(drop))
    check("历史版本号 v1.0.0 也不删", "v1.0.0" not in drop)


def version_plan_ids() -> None:
    print("---------- 4. GHCR 版本级删除 ----------")
    versions = [
        {"id": 1, "created_at": "2026-11-04T00:00:00Z",
         "metadata": {"container": {"tags": ["sha-ddddddd", "latest"]}}},
        {"id": 2, "created_at": "2026-11-03T00:00:00Z",
         "metadata": {"container": {"tags": ["sha-ccccccc", "v1.1.0"]}}},
        {"id": 3, "created_at": "2026-11-01T00:00:00Z",
         "metadata": {"container": {"tags": ["sha-aaaaaaa"]}}},
    ]
    victims = prune_tags.plan_versions(versions, ["sha-aaaaaaa", "sha-ccccccc", "sha-ddddddd"])
    check("挂 latest 的版本整条跳过", 1 not in victims)
    check("挂版本号的版本整条跳过", 2 not in victims)
    check("只挂待删标签的版本才删", list(victims) == [3], str(victims))


def main() -> int:
    keep_ids()
    quota_ids()
    no_legacy_ids()
    version_plan_ids()
    print()
    print("=" * 62)
    print(f"结果：{checks} 项通过，{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
