# -*- coding: utf-8 -*-
"""检查「支持的格式放进去就能刮」：不用改配置、不用手动加后缀。

跑法：python tools/check_formats.py
需要 ffmpeg 才跑最后一段真实视频；没有就跳过，其余检查照跑。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import media, worker  # noqa: E402
from scraper.config import Config  # noqa: E402
from scraper.probe import ProbeError, find_binary  # noqa: E402

checks = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks, failed
    checks += 1
    if not ok:
        failed += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"    {detail}" if detail else ""))


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_yaml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
print("========== 1. 默认配置 ==========")
cfg = Config()
expected = {".strm", ".mp4", ".m4v", ".mkv", ".avi", ".ts", ".m2ts",
            ".mov", ".wmv", ".flv", ".rmvb", ".webm", ".mpg"}
got = set(cfg.scan.extensions)
check("默认就扫描全部承诺格式", expected <= got, f"{len(got)} 种")
check("默认配置里没有多出无关类型", got <= expected,
      f"多出 {sorted(got - expected)}" if got - expected else "")
check("用户不用打开 config.yaml 就能用", Config().scan.extensions == list(media.SUPPORTED_EXTENSIONS))

empty_cfg = write_yaml(Path(tempfile.mkdtemp()) / "c.yaml", "scan:\n  roots: ['/media']\n  extensions: []\n")
notes = Config.load(empty_cfg).validate()
check("扩展名被清空时回落到全格式（不是只留 .strm）",
      set(Config.load(empty_cfg).scan.extensions) == expected)

# ---------------------------------------------------------------------------
print("========== 2. 旧配置自动升级 ==========")
tmp = Path(tempfile.mkdtemp())
old = write_yaml(tmp / "old.yaml", """scan:
  roots: ['/media']
  # 这是老版本抄下来的默认值
  extensions:
  - .strm
scrape:
  frames: 2
""")
upgraded = Config.load(old)
check("老的「只扫 .strm」自动扩成全格式", set(upgraded.scan.extensions) == expected,
      f"{len(upgraded.scan.extensions)} 种")
check("升级时给出了说明", any("格式" in n for n in upgraded.migration_notes))
text = old.read_text(encoding="utf-8")
check("新列表写回了配置文件", "- .mkv" in text and "- .ts" in text)
check("写回时没冲掉注释", "# 这是老版本抄下来的默认值" in text)
check("写回后仍能正常解析", Config.load(old).scan.extensions == list(media.SUPPORTED_EXTENSIONS))

inline = write_yaml(tmp / "inline.yaml", "scan:\n  roots: ['/media']\n  extensions: [.strm]\n")
Config.load(inline)
check("行内写法也能升级", "- .mpg" in inline.read_text(encoding="utf-8"))

custom = write_yaml(tmp / "custom.yaml", """scan:
  roots: ['/media']
  extensions:
  - .strm
  - .iso
""")
before = custom.read_text(encoding="utf-8")
Config.load(custom)
check("用户自己加过东西的组合不被动", custom.read_text(encoding="utf-8") == before)

# ---------------------------------------------------------------------------
print("========== 3. 扫描：各种格式都认 ==========")
lib = tmp / "lib"
for name in ("电影.2024.mp4", "电影2.mkv", "电影3.avi", "电影4.ts", "电影5.m2ts",
             "电影6.mov", "电影7.wmv", "电影8.rmvb", "电影9.webm", "电影10.mpg",
             "电影11.MKV", "电影12.strm"):
    write(lib / "一级" / name)
write(lib / "一级" / "二级" / "三级.mkv")

found = sorted(p.name for p in media.iter_sources([str(lib)], Config().scan.extensions))
check("mp4/mkv/avi/ts/m2ts/mov/wmv/rmvb/webm/mpg 全部扫到", len(found) == 13, f"{len(found)} 个")
check("大写扩展名一视同仁", "电影11.MKV" in found)
check("深层目录也扫到", "三级.mkv" in found)

wanted = media.wanted_extensions(Config().scan.extensions)
rows = media.scan_rows([str(lib)], Config().scan.extensions, recursive=True,
                       workers=1)
check("扫描接口和遍历结果一致", sorted(Path(p).name for p, _, _ in rows) == found)

# ---------------------------------------------------------------------------
print("========== 4. 附加内容不生成垃圾 ==========")
extra = tmp / "extra"
for name in ("片子.2024.mp4", "片子.2024-trailer.mp4", "片子.sample.mkv",
             "片子-featurette.mp4", "片子-behindthescenes.mp4", "片子-interview.mp4",
             "片子-deleted.mp4", "片子-scene.mp4", "片子-short.mp4",
             "片子-other.mp4", "片子-makingof.mp4", "片子-clip.mp4",
             "片子-trailer.mp4"):
    write(extra / name)
kept = [p.name for p in media.iter_sources([str(extra)], Config().scan.extensions)]
check("正片照常刮", kept == ["片子.2024.mp4"], f"留下 {kept}")
check("预告/花絮/样片全部跳过", len(kept) == 1)
check("判断函数本身对得上", media.is_extra("a-trailer.mp4") and not media.is_extra("a.mp4"))
check("片名里带 trailer 但不是后缀的不误伤", not media.is_extra("Trailer.Park.Boys.mkv"))

# ---------------------------------------------------------------------------
print("========== 5. 产物命名（和 strm 同一套规则）==========")
t = media.build_target(Path("/media/电影/片子.2024.1080p.mp4"))
check("本地视频去自己的后缀",
      [t.nfo.name, t.poster.name, t.fanart.name, t.thumb.name] ==
      ["片子.2024.1080p.nfo", "片子.2024.1080p-poster.jpg",
       "片子.2024.1080p-fanart.jpg", "片子.2024.1080p-thumb.jpg"])
s = media.build_target(Path("/media/电影/片子.2024.1080p.strm"))
check("strm 的伪扩展名保留（Emby 按这个名字找）",
      s.poster.name == "片子.2024.1080p-poster.jpg")

# ---------------------------------------------------------------------------
print("========== 6. 真实视频（需要 ffmpeg）==========")
ffmpeg = None
try:
    ffmpeg = find_binary("ffmpeg")
except ProbeError:
    pass

if not ffmpeg:
    print("  --   没装 ffmpeg，跳过真实视频这一段")
else:
    clip = tmp / "real" / "电影"
    clip.mkdir(parents=True, exist_ok=True)
    src = clip / "测试片.2023.1080p.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    shot_cfg = write_yaml(tmp / "real.yaml", f"scan:\n  roots: ['{tmp / 'real'}']\n")
    real_cfg = Config.load(shot_cfg)
    target = media.build_target(src)
    result = worker.process(real_cfg, target)
    check("mp4 能直接刮出结果", result.ok, result.reason or "")
    check("海报 / 背景 / NFO 都生成了",
          target.poster.exists() and target.fanart.exists() and target.nfo.exists())
    title = target.nfo.read_text(encoding="utf-8")
    check("标题去掉了清晰度后缀", "<title>测试片.2023</title>" in title)
    check("时长写进了 NFO", "<runtime>" in title)
    again = worker.process(real_cfg, target)
    check("第二次走「产物已存在」不重复劳动", again.reason == "产物已存在，跳过", again.reason)

# ---------------------------------------------------------------------------
print()
print(f"===== 结果：{checks - failed}/{checks} 通过 =====")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if failed else 0)
