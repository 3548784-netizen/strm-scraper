# strm-scraper

[![构建镜像](https://github.com/3548784-netizen/strm-scraper/actions/workflows/docker.yml/badge.svg)](https://github.com/3548784-netizen/strm-scraper/actions/workflows/docker.yml)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-3548784%2Fstrm--scraper-2496ED)](https://hub.docker.com/r/3548784/strm-scraper)
[![镜像大小](https://img.shields.io/docker/image-size/3548784/strm-scraper?label=%E4%BD%93%E7%A7%AF)](https://hub.docker.com/r/3548784/strm-scraper)
[![GHCR](https://img.shields.io/badge/ghcr.io-3548784--netizen%2Fstrm--scraper-2496ED)](https://github.com/3548784-netizen/strm-scraper/pkgs/container/strm-scraper)

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
- 不满意可以在网页上手动选帧，指定哪一秒出图，选择会被记住
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

**登录**

- 控制台默认要登录，默认账号 `admin` / `admin`，登录后在网页上就能改用户名和密码
- 密码加盐哈希存在 `config/auth.json`，文件里看不到明文；改完密码旧会话立即失效
- 同一 IP 连续 5 次密码错误锁 5 分钟；确实不需要可以 `web.auth.enabled: false` 关掉

**日志**

- 控制台 + 滚动文件 + 网页实时三路输出
- 主日志记全量，`errors-*.log` 只记 WARNING 及以上，排查不用在进度里翻
- 网页上按级别、关键字筛选，一键下载，也能看原始文件末尾
- `--doctor` 自检一次查完 ffmpeg、目录、状态库、日志、服务器连通性

## 快速开始

镜像同时发布在两个仓库，内容完全一致，挑能拉快的那个用：

```
3548784/strm-scraper:latest                              # Docker Hub，国内一般更快
ghcr.io/3548784-netizen/strm-scraper:latest              # GitHub 容器仓库
```

两个都不需要登录，直接匿名拉取。`latest` 跟 `main` 分支同步，每次推送自动重建。

### 方式一：拉现成镜像（推荐）

建个目录放配置，把下面内容存成 `docker-compose.yml`：

```bash
mkdir -p /vol1/1000/docker/strm-scraper && cd /vol1/1000/docker/strm-scraper
```

```yaml
services:
  strm-scraper:
    image: 3548784/strm-scraper:latest   # 拉不动就换成 ghcr.io/3548784-netizen/strm-scraper:latest
    container_name: strm-scraper
    restart: unless-stopped
    ports:
      - "6666:6666"
    volumes:
      - ./config:/config                    # 配置目录，必须（配置、账号、状态库、日志都在这）
      - /vol1/1000/LitePan/strm:/media      # 媒体库，可映射多个
      # - /vol1/1000/另一个媒体库:/media2   # 再挂一个库，同时把 /media2 加进 scan.roots
    environment:
      TZ: Asia/Shanghai
```

```bash
docker compose up -d
```

飞牛 / 群晖这类有图形界面的 NAS，直接在 Docker 应用的「项目」里粘贴同样的内容启动即可。完整步骤见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

### 方式二：从源码构建

改代码或者想自己打镜像时用这条。镜像在 `main` 每次推送后自动重建，源码构建和拉取的效果一致。

```bash
git clone https://github.com/3548784-netizen/strm-scraper.git
cd strm-scraper
```

改 `docker-compose.yml` 里的挂载路径，把左边换成你自己的 strm 目录：

```yaml
volumes:
  - ./config:/config                    # 配置目录，必须
  - /vol1/1000/LitePan/strm:/media      # 媒体库，可映射多个
```

然后：

```bash
docker compose up -d --build
```

### 打开控制台

浏览器打开 `http://你的NAS地址:6666`，第一次会跳到登录页：

```
用户名 admin    密码 admin
```

登录后建议立刻在右上角「**账号**」里改掉，改完其他设备上的登录全部失效。

首次运行会全量扫一遍所有 strm，之后自动转成增量模式，只补新文件和缺图。跑一轮的耗时主要取决于视频数量和网盘响应速度，300 个视频大约 20~40 分钟。

### 更新到新版本

`main` 有新提交时镜像会自动重建，NAS 上重新拉一次即可：

```bash
docker compose pull && docker compose up -d
```

配置、状态库、日志都在 `./config` 里，更新不会丢，已生成的图片也不会重刮。

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
| `web.auth.enabled` | 控制台是否要登录 | `true` |
| `web.auth.session_hours` | 登录保持多少小时 | `168` |
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

**任务列表**

- 登录后才能操作，默认账号 `admin` / `admin`
- 顶部按钮：开始任务（增量）/ 全量重刮 / 暂停（暂停后变「继续任务」）/ 取消任务
- 状态条：状态灯、进度条、成功 / 失败 / 待处理 / 总数 / 用时
- 表格：状态、视频名称、时长、大小、海报缩略图、背景图缩略图、NFO、备注、操作
- 按状态筛选、只看失败、搜索片名或路径、分页、单条重刮 / 选帧 / 删产物

**暂停和取消的区别**

| 操作 | 已经在跑的任务 | 队列 | 结果 |
|---|---|---|---|
| 暂停 | 跑完当前这批再停住 | 停止派发 | 保留，继续后接着来 |
| 取消 | 无法中断，但结果作废 | 立即停止 | 状态退回待处理，下次开始重新做 |

**文件管理**

- 直接浏览扫描目录，面包屑 + 上一级，目录后面标着里面有几个 strm
- 每个文件的海报 / 背景图 / NFO 有没有、多大，一眼可见
- 勾选后可批量：刮削选中、删除产物、仅删 NFO；删完状态自动重置，下次开始重新生成
- 只能操作 `scan.roots` 里的路径，`../` 和系统目录会被 403 挡掉

**其它面板**

- 媒体服务器：连接状态、测试连接、扫描媒体库、补齐缺图
- 运行日志：级别筛选、关键字搜索、跟随最新、看原始文件、下载主日志和错误日志

## 手动选帧

自动选帧是按比例采样再挑最清晰的一帧，碰到片头黑场、字幕压屏、画面切镜的时候不一定合心意。文件管理或任务列表里点某一行的「选帧」，可以自己挑。

面板里能做的事：

- 打开时按片长 3%–92% 抽 12 张候选缩略图，避开片头厂标和片尾字幕
- 点缩略图直接定位；不精确就拖时间轴，或用「−1 秒 / +1 秒」微调
- 大图实时预览，确认是想要的画面再动手
- 勾选要更新的图片（海报 / 背景图 / 缩略图），一键生成，可选顺手通知媒体服务器刷新
- 选定的时间点会记在状态库里，文件名旁边显示「手选 12:34」；**之后重刮、全量重刮都沿用这一帧**，不会被自动采样覆盖
- 想回到自动挑帧就点「恢复自动」，现有图片不变，下次重刮重新采样

实现上的两点：

- 预览帧按「文件 + 修改时间 + 时间点 + 宽度」缓存在 `config/framecache/`，来回拖动不重复打网络；缓存 6 小时过期，最多 400 张
- 抓帧同时最多 4 路，避免十几张预览一起抢上行带宽，结果谁先回来谁先显示

> 选帧依赖 strm 里的地址当时可用。链接失效时面板会明确提示「这一帧抓不下来」，换时间点或先修好链接再试。

## 登录与账号

控制台默认要求登录，账号密码存在 `config/auth.json` 里（加盐哈希，没有明文）。

| 项 | 说明 |
|---|---|
| 默认账号 | `admin` / `admin`，首次启动自动创建 |
| 改账号 | 右上角「账号」→ 填当前密码 → 改用户名和/或新密码 → 保存 |
| 会话 | 签名 cookie，默认保持 7 天，超时后自动回登录页 |
| 改完的效果 | 所有旧登录立刻失效，当前这个浏览器自动续上 |
| 防爆破 | 同一 IP 连续 5 次失败锁 5 分钟，日志里有记录 |

相关配置：

```yaml
web:
  auth:
    enabled: true          # 关掉就免登录，只建议在内网或反代鉴权后面这么用
    session_hours: 168     # 登录保持时长
    file: /config/auth.json
```

也可以用环境变量临时关掉：`WS_WEB__AUTH__ENABLED=false`。

> 忘记密码：删掉 `config/auth.json` 后重启容器，会重新生成 `admin` / `admin`。这个文件在 `./config` 卷里，删宿主机的 `config/auth.json` 即可。

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

**忘记密码了**

删掉宿主机的 `config/auth.json`，重启容器，账号恢复成 `admin` / `admin`。刮削进度和已生成的图片都不受影响，只有登录信息重置。

**不想每次登录**

两种做法：`web.auth.enabled: false` 或环境变量 `WS_WEB__AUTH__ENABLED=false`。只建议在内网、或前面有带鉴权的反向代理时这么用——控制台没有登录就等于把刮削按钮开放给所有能访问端口的人。

**登录一直提示密码错误，改过用户名**

用户名不区分大小写（`Admin` 和 `admin` 等价），密码区分。如果连自己改成什么了都忘了，按上一条删 `auth.json` 重置。

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

本地跑要注意默认路径都是容器里的 `/config`，改成相对路径：

```yaml
storage:
  state_db: ./data/state.db
web:
  auth:
    file: ./data/auth.json
log:
  dir: ./data/logs
```

先跑一条视频确认环境正常：

```bash
python scripts/selftest.py /path/to/影片.strm
```

它会依次检查 ffmpeg、解析 strm、探测、抓帧、出图、写 NFO，哪一步失败会直接告诉你原因。

## 许可

MIT。项目只处理你自己合法拥有的媒体文件，不提供任何内容源。
