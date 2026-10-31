#!/usr/bin/env python3
"""清理 Docker Hub 与 GHCR 上过期的 sha- 标签。

每次 push 都会产生一个 sha-xxxxxxx 标签，日积月累会撑爆仓库。
默认策略：latest 和最近 KEEP 个 sha 保留，其余删除。

用法：
    python tools/prune_tags.py --dry-run     # 只打印，不删
    python tools/prune_tags.py               # 真删

环境变量：
    HUB_USER / HUB_TOKEN   Docker Hub 用户名与 token（缺省则跳过 Hub）
    GH_TOKEN               GitHub token，需 packages:write（缺省则跳过 GHCR）
    KEEP                   保留几个 sha 标签，默认 3
    GITHUB_SHA             当前提交，脚本会自动把它的短 sha 设为保护标签

保护规则：latest 永远不动；当前提交的标签永远不动；不是 sha- 开头的
标签（比如手工打的 v1.0.0）不在清理范围内。GHCR 侧一个版本如果还挂着
latest，说明它就是当前版本，整条跳过——GHCR 只能删版本不能单独删标签，
硬删会把 latest 一起抹掉。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

DRY = "--dry-run" in sys.argv
KEEP = int(os.environ.get("KEEP", "3"))

HUB_USER = os.environ.get("HUB_USER", "")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
HUB_REPO = os.environ.get("HUB_REPO", "strm-scraper")

GH_OWNER = os.environ.get("GH_OWNER", "3548784-netizen")
GH_PKG = os.environ.get("GH_PKG", "strm-scraper")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

SHA_PREFIX = "sha-"
UA = {"User-Agent": "strm-scraper-prune"}


def _req(url: str, headers: dict | None = None, data: bytes | None = None,
         method: str | None = None) -> urllib.request.Request:
    h = dict(UA)
    h.update(headers or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _json(req, timeout: int = 30):
    last = None
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read()), r.headers
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # 网络抖动、TLS 断连，重试
            last = e
            if i < 3:
                time.sleep(2 + i * 2)
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- 保留名单

def pick_keep(tags: list[tuple[str, str]], protected: set[str]) -> tuple[set[str], list[str]]:
    """tags 是 (名称, 时间) 列表。返回 (要保留的, 要删的)。

    保留 = 最近 KEEP 个 sha ∪ 受保护标签（latest、当前提交）。
    受保护的 sha 也占名额，不会因为「保护」而多留一个。
    """
    shas = sorted([(n, t) for n, t in tags if n.startswith(SHA_PREFIX)],
                  key=lambda x: x[1], reverse=True)  # 新的在前
    keep = set(protected) | {n for n, _ in shas[:KEEP]}
    drop = [n for n, _ in shas if n not in keep]
    return keep, drop


# --------------------------------------------------------------- Docker Hub

def hub_list() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    url = (f"https://hub.docker.com/v2/repositories/{HUB_USER}/{HUB_REPO}"
           "/tags?page_size=100&ordering=last_updated")
    while url:
        d, _ = _json(_req(url))
        out += [(t["name"], t["last_updated"]) for t in d.get("results", [])]
        url = d.get("next")
    return out


def hub_login() -> str:
    body = json.dumps({"username": HUB_USER, "password": HUB_TOKEN}).encode()
    d, _ = _json(_req("https://hub.docker.com/v2/users/login", data=body,
                      headers={"Content-Type": "application/json"}))
    return d["token"]


def hub_delete(tag: str, jwt: str) -> bool:
    url = f"https://hub.docker.com/v2/repositories/{HUB_USER}/{HUB_REPO}/tags/{tag}/"
    try:
        with urllib.request.urlopen(
                _req(url, {"Authorization": f"JWT {jwt}"}, method="DELETE"), timeout=60):
            return True
    except urllib.error.HTTPError as e:
        print(f"    失败 {tag}: HTTP {e.code} {e.read()[:120]!r}")
        return False


def prune_hub(protected: set[str]) -> None:
    print("== Docker Hub ==")
    if not HUB_USER:
        print("  没有配置 HUB_USER，跳过")
        return
    tags = hub_list()
    print(f"  当前 {len(tags)} 个标签")
    keep, drop = pick_keep(tags, protected)
    print(f"  保留 {len(keep)} 个：{' '.join(sorted(keep))}")
    if not drop:
        print("  没有要删的")
        return
    print(f"  待删 {len(drop)} 个：{' '.join(drop)}")
    if DRY:
        print("  [dry-run] 未实际删除")
        return
    if not HUB_TOKEN:
        print("  没有 HUB_TOKEN，跳过删除")
        return
    jwt = hub_login()
    ok = sum(hub_delete(t, jwt) for t in drop)
    print(f"  已删 {ok}/{len(drop)}")


# --------------------------------------------------------------------- GHCR

def gh_versions() -> list[dict]:
    out: list[dict] = []
    url = (f"https://api.github.com/users/{GH_OWNER}/packages/container/{GH_PKG}"
           "/versions?per_page=100")
    while url:
        d, hdrs = _json(_req(url, {"Authorization": f"Bearer {GH_TOKEN}",
                                   "Accept": "application/vnd.github+json"}))
        out += d
        link = hdrs.get("Link") or ""
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
    return out


def gh_delete(version_id: int) -> bool:
    url = f"https://api.github.com/users/{GH_OWNER}/packages/container/{GH_PKG}/versions/{version_id}"
    try:
        with urllib.request.urlopen(
                _req(url, {"Authorization": f"Bearer {GH_TOKEN}",
                           "Accept": "application/vnd.github+json"},
                     method="DELETE"), timeout=60):
            return True
    except urllib.error.HTTPError as e:
        print(f"    失败 {version_id}: HTTP {e.code} {e.read()[:120]!r}")
        return False


def prune_ghcr(protected: set[str]) -> None:
    print("== GHCR ==")
    if not GH_TOKEN:
        print("  没有 token，跳过")
        return
    try:
        versions = gh_versions()
    except urllib.error.HTTPError as e:
        print(f"  列版本失败 HTTP {e.code}，跳过（不影响本次发布）")
        return

    # 摊平成 (tag, 时间)，一个版本可能挂多个 tag
    flat: list[tuple[str, str]] = []
    owner: dict[str, dict] = {}
    for v in versions:
        for t in (v.get("metadata", {}).get("container", {}).get("tags") or []):
            flat.append((t, v["created_at"]))
            owner[t] = v
    print(f"  当前 {len(versions)} 个版本 / {len(flat)} 个标签")

    keep, drop = pick_keep(flat, protected)
    print(f"  保留 {len(keep)} 个：{' '.join(sorted(keep))}")
    if not drop:
        print("  没有要删的")
        return

    # 一个版本里只要还有别的 tag 挂着，就不能整条删
    victims: dict[int, list[str]] = {}
    for tag in drop:
        v = owner[tag]
        others = [t for t in (v.get("metadata", {}).get("container", {}).get("tags") or [])
                  if t not in drop]
        if others:
            print(f"  跳过 {tag}：同版本还挂着 {' '.join(others)}")
            continue
        victims.setdefault(v["id"], []).append(tag)

    print(f"  待删 {len(victims)} 个版本："
          f"{' '.join(t for ts in victims.values() for t in ts)}")
    if DRY:
        print("  [dry-run] 未实际删除")
        return
    ok = sum(gh_delete(vid) for vid in victims)
    print(f"  已删 {ok}/{len(victims)}")


def main() -> int:
    protected = {"latest"}
    head = (os.environ.get("GITHUB_SHA") or "")[:7]
    if head:
        protected.add(SHA_PREFIX + head)
    print(f"保留策略：latest + 最近 {KEEP} 个 sha"
          f"{'（dry-run）' if DRY else ''}")
    # 清理是收尾动作，网络或权限出问题都不能影响已经发布成功的镜像
    for fn in (prune_hub, prune_ghcr):
        try:
            fn(protected)
        except Exception as e:
            print(f"  {fn.__name__} 出错，跳过：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
