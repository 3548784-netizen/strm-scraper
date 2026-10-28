# strm-scraper

给 115 / LitePan 生成的 `.strm` 文件补上海报、背景图和 NFO 的刮削器，跑在 Docker 里，手机和电脑都能通过网页控制台操作。

**Emby 和 Jellyfin 同时支持**：产物命名和图片比例按两家的规范来，同一份文件两边都能识别，不需要为谁单独复制一套。

```
影片目录/
├── 某影片.AVI.strm          ← 你原来的 strm，一个字都不动
├── 某影片.AVI.nfo           ← 生成
├── 某影片.AVI-poster.jpg    ← 生成（2:3 竖版海报）
├── 某影片.AVI-fanart.jpg    ← 生成（16:9 横版背景图）
└── 某影片.AVI-thumb.jpg     ← 生成（16:9 缩略图，可选）
```

## 为什么需要它

网盘挂载工具生成的 strm 只是一个文本文件，里面写着播放地址。媒体服务器读到它，能播，但拿不到任何画面信息——没有海报，没有时长，没有分辨率，列表里全是一样的灰色占位图。

这个项目做的事就是：读出 strm 里的地址 → 用 ffprobe 探测媒体信息 → 从视频里抓几帧挑最清晰的一张 → 生成海报、背景图和 NFO 放到视频旁边。

整个过程不需要下载完整视频，只读取几个关键位置的数据，所以对网盘流量几乎没有消耗。

## 特性

**抓帧**

- 按片长比例采样 5 个时间点，用边缘检测方差挑出最清晰的一帧，不是固定截中间那张
- 快速跳转失败时自动退回解码定位，网络源不支持 Range 请求也能出图
- 自动过滤纯黑帧和色块帧

**图片规格（符合 Emby / Jellyfin 要求）**

| 产物 | 比例 | 默认尺寸 | 文件名 | 用途 |
|---|---|---|---|---|
| 海报 | 2:3 | 1000×1500 | `<名称>-poster.jpg` | 媒体库网格封面 |
| 背景图 | 16:9 | 1920×1080 | `<名称>-fanart.jpg` | 详情页背景 / backdrop |
| 缩略图 | 16:9 | 1000×562 | `<名称>-thumb.jpg` | Emby 列表视图（可选） |

比例不对会被媒体服务器拉伸或裁切，比不显示更难发现，所以配置里的尺寸一律按比例校正，偏了会在日志里明确告警。

海报有三种生成方式：`blur`（模糊背景 + 完整画面居中，横版视频不切主体，默认）、`gradient`（用画面边缘平均色做渐变背景，最干净）、`crop`（中心裁切，画面最饱满但两侧会丢）。

**处理**

- 多任务并发，单条超时隔离，一条视频卡住不影响整轮
- 增量模式只处理缺失项，已有产物不重复抓帧
- 失败原因写进状态库，网页上直接看到「连接被拒绝」「地址返回 404」这类具体原因

**媒体服务器联动**

- 同时配置多台 Emby / Jellyfin，刮完自动让它们重建图片缓存
- 三种认证头依次尝试（`Authorization: MediaBrowser`、`X-Emby-Token`、`?api_key=`），适配不同版本和打包方式
- 刷新参数同时写进 JSON body 和 URL query：Jellyfin 12.x 只认 body，Emby 对 query 更稳
- 刷新后回查一遍，确认媒体服务器真的识别到了图，没识别上会直接把文件名打出来

**日志**

- 控制台 + 滚动文件 + 网页实时三路输出
- 主日志记全量，`errors-*.log` 只记 WARNING 及以上，排查不用在进度里翻
- 网页上按级别、关键字筛选，一键下载，也能看原始文件末尾
- `--doctor` 自检一次查完 ffmpeg、目录、状态库、日志、服务器连通性

## 快速开始

需要 Docker 和 docker compose。

```bash
git clone https://github.com/3548784-netizen/strm-scraper.git
cd strm-scraper
```

改 `docker-compose.yml` 里的挂载路径，把左边换成你自己的 strm 目录：

```yaml
volumes:
  - /vol1/1000/LitePan/strm:/media
  - ./config:/config
```

然后：

```bash
docker compose up -d --build
```

浏览器打开 `http://你的NAS地址:6666`，点「增量刮削」。

首次运行会全量扫一遍所有 strm，之后自动转成增量模式，只补新文件和缺图。跑一轮的耗时主要取决于视频数量和网盘响应速度，300 个视频大约 20~40 分钟。

## 配置

首次启动会自动生成 `config/config.yaml`，改完重启容器生效。也可以全部用环境变量，用 `WS_` 前缀，嵌套字段用双下划线：

```yaml
WS_SCAN__CONCURRENCY=4
WS_SCAN__INTERVAL_MINUTES=360
WS_LOG__LEVEL=DEBUG
```

常用几项：

| 配置 | 说明 | 默认 |
|---|---|---|
| `scan.roots` | 要扫描的目录（容器内路径） | `/media` |
| `scan.mode` | `auto` 首次全量之后增量 / `full` 每次全量 / `incremental` 只补缺失 / `manual` 只手动 | `auto` |
| `scan.interval_minutes` | 定时扫描间隔，0 表示只在网页手动触发 | `0` |
| `scan.concurrency` | 并发数，网盘有限速，3~5 比较合适 | `3` |
| `scan.probe_timeout` | 探测超时，strm 地址失效时靠它快速失败 | `20` |
| `scrape.poster.mode` | `blur` / `gradient` / `crop` | `blur` |
| `scrape.thumb.enabled` | 是否额外生成 16:9 缩略图 | `false` |
| `scrape.overwrite` | `missing` 只补缺失 / `always` 每次覆盖 | `missing` |
| `servers[]` | 媒体服务器列表，见下一节 | 空 |
| `log.level` / `log.dir` | 日志级别与目录 | `INFO` / `/config/logs` |

完整项见 [`config.example.yaml`](config.example.yaml)，每项都有注释。

## Emby / Jellyfin 联动

不配也能用，只是图片生成后要自己在服务器上点一次刷新——**Jellyfin 不会自动重读图片缓存**，Emby 一般会自动发现。

想让刮完自动生效，在 `servers` 里加一台或多台：

```yaml
servers:
  - name: jellyfin
    type: jellyfin            # jellyfin / emby / auto（自动识别）
    enabled: true
    url: "http://192.168.1.10:8096"
    api_key: "控制台 → API 密钥 → 新建"
    refresh_after: true       # 刮完自动重建图片缓存
    verify_after: true        # 刷新后回查，确认图被识别
    path_map:
      /media: /vol1/1000/LitePan/strm   # 容器路径 → 服务端看到的路径

  - name: emby
    type: emby
    enabled: true
    url: "http://192.168.1.11:8096"
    api_key: "Emby 控制台 → 高级 → API 密钥"
    path_map:
      /media: /vol1/1000/LitePan/strm
```

`path_map` 不能省。容器里是 `/media`，服务端可能是 `/vol1/1000/...`，不做前缀替换就永远匹配不上条目。日志里如果出现「N 个文件找不到对应条目」，会同时打印两边路径的对照，照着改就行。

网页上的「媒体服务器」面板可以：

- 看每台的连接状态和版本
- **测试连接**：立即重连一次，不用等缓存
- **扫描媒体库**：让服务器重新扫一遍目录
- **补齐缺图**：把已刮削过的文件全部提交一次图片重建，等于以前手动点「刷新元数据 → 替换现有图片」那套操作

## 产物说明

**文件名**

跟 strm 同名，加后缀。`某影片.AVI.strm` 对应 `某影片.AVI.nfo` / `某影片.AVI-poster.jpg` / `某影片.AVI-fanart.jpg`。这套命名 Emby 和 Jellyfin 都认，而且不改动目录结构，服务器里已有的条目不会重建。

**NFO 标题**

会去掉文件名尾巴上的伪扩展名再写进 `<title>`。`流浪地球2.2023.2160p.WEB-DL.AVI` → `流浪地球2.2023.2160p.WEB-DL`。Jellyfin 里显示的标题会跟着变干净。

**NFO 内容**

Kodi / Emby 标准 `<movie>` 格式，UTF-8 无 BOM，包含时长、码率、分辨率、宽高比、帧率、音轨信息。`runtime` 和 `durationinseconds` 的单位分别是分钟和秒，两者一致。

## 网页控制台

`http://NAS:6666`

- 实时进度：总数、已完成、失败、待处理、当前在跑哪几个文件
- 失败列表带着具体原因，可以单条重刮
- 按钮：增量刮削 / 全量重刮 / 停止 / 重试全部失败项
- 媒体服务器面板：连接状态、测试、扫描媒体库、补齐缺图
- 运行日志面板：级别筛选、关键字搜索、跟随最新、看原始文件、下载主日志和错误日志

## 日志与排查

日志三路同时输出，各管一件事：

| 位置 | 内容 | 什么时候看 |
|---|---|---|
| `docker logs strm-scraper` | 实时控制台输出 | 容器刚起来、想看有没有立刻报错 |
| `config/logs/strm-scraper.log` | 全量，5MB 滚动，保留 5 份 | 事后复盘，网页缓冲被刷掉之后 |
| `config/logs/errors-*.log` | 只有 WARNING 及以上 | 只想看异常，不想翻进度 |
| 网页「运行日志」 | 最近 1000 条，可筛选 | 日常用 |

每条日志带时间、级别、来源模块（`worker` / `probe` / `frames` / `mediaserver`）和消息。单条结果长这样：

```
19:45:46 INFO  [worker      ] [完成] 流浪地球2.2023 | 17.1s | 海报 106KB 背景 169KB 缩略图 77KB NFO 1KB
19:45:46 WARNING [mediaserver] 假Emby 回查：1 条仍无海报 …
```

`log.level` 设成 `DEBUG` 能看到每次抓帧、每次请求的细节，排查完记得改回来。

**先跑自检**

```bash
docker exec strm-scraper python -m scraper.main --doctor
```

会依次检查 Python、ffmpeg/ffprobe、扫描目录和 strm 数量、状态库可写性、日志落盘、每台媒体服务器的连通性和鉴权方式，最后汇总问题数量。发现问题会直接说缺什么，比盯着日志猜快得多。

## 常见问题

**扫描目录为空 / 提示目录不存在**

`scan.roots` 写的是**容器内路径**，不是宿主机路径。宿主机 `/vol1/1000/LitePan/strm` 挂到容器 `/media`，配置里就写 `/media`。

**海报写不进媒体目录**

权限问题。NAS 上的目录通常属于某个用户，容器默认以 root 跑一般没问题；如果挂了 `user:` 字段，要保证那个 uid 对媒体目录有写权限。

**大量「连接超时」或「连接被拒绝」**

strm 里的地址失效了。检查网盘挂载工具是否在跑、内网穿透端口是否正常。这类失败不会拖慢整轮，`probe_timeout` 一到就跳过。

**都显示成功但媒体库里没海报**

先看网页日志里「回查」那条：

- 「N 条全部已有海报」→ 服务端已经识别，客户端刷新一下就好
- 「N 条仍无海报」→ 点面板上的「补齐缺图」，或确认 `path_map` 是否正确
- 图片文件本身不存在 → 看条目产物大小，重刮这一条

Emby 一般会自动发现新图片，Jellyfin 必须显式刷新，这是两家行为差异，不是 bug。

**媒体服务器连不上**

日志会写清楚是哪种情况：`鉴权失败` 说明密钥不对或权限不足；`连不上` 说明地址/端口不通；`SSL 证书校验失败` 时把 `insecure: true` 加上（自签名证书）。网页上点「测试连接」可以立刻复测。

**封面还是同一张**

说明这个视频可选帧之间差别不大，或者采样区间都落在相似场景里。可以调 `scrape.frame_positions` 换采样位置。

**海报里主体被切了**

把 `scrape.poster.mode` 改成 `blur`（完整画面居中）或 `gradient`（纯渐变背景）。`crop` 会裁掉两侧内容。

**能只刮某个子目录吗**

把 `scan.roots` 指向那个子目录即可，支持多个目录。

**会不会覆盖我手动改过的 NFO**

默认 `overwrite: missing`，文件存在就跳过。只有设成 `always` 或点「全量重刮」才会覆盖。

## 工作原理

```
扫描目录 ──> 读 strm 首行 URL ──> ffprobe 探测（时长/分辨率/码率）
                                        │
                                        ├─> ffmpeg 按比例抓 5 帧 ──> 挑最清晰的一帧
                                        │                          │
                                        │                          ├─> 2:3 海报
                                        │                          ├─> 16:9 背景图
                                        │                          └─> 16:9 缩略图
                                        └─> 写 NFO

状态写进 SQLite，网页读状态库，实现增量与失败重试

刮削完成 ──> 各台 Emby / Jellyfin 刷新图片缓存 ──> 回查确认
```

抓帧只请求视频中间几个时间点的数据，不落盘完整文件。115 直链是 302 跳转到 CDN，播放和抓帧的流量都不经过你自己的网络。

## 本地运行

不用 Docker 也可以，需要 Python 3.10+ 和 ffmpeg：

```bash
pip install -r requirements.txt
python -m scraper.main --config config.yaml
```

先跑一条视频确认环境正常：

```bash
python scripts/selftest.py /path/to/影片.strm
```

它会依次检查 ffmpeg、解析 strm、探测、抓帧、出图、写 NFO，哪一步失败会直接告诉你原因。

## 许可

MIT。项目只处理你自己合法拥有的媒体文件，不提供任何内容源。
