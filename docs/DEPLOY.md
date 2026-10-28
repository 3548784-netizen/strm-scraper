# 在飞牛 fnOS 上部署

本文以飞牛 fnOS + Docker 为例。群晖、威联通、Unraid 思路一样，改挂载路径即可。

## 1. 确认 strm 目录

先确认你的 strm 在宿主机上的实际路径。飞牛的用户数据一般在 `/vol1`：

```bash
ls /vol1/1000/LitePan/strm
# 115影片AV  115影片国产  123影视  FC2测试 ...
```

如果是通过网盘挂载工具（LitePan、CloudDrive2、alist 等）挂载出来的，注意挂载点必须在容器启动**之前**就存在，否则容器会看到一个空目录。

## 2. 拉取现成镜像（方式 A，推荐）

镜像同时发布在 Docker Hub 和 GitHub 容器仓库，内容完全一致，挑拉得快的一个：

```
3548784/strm-scraper:latest                       # Docker Hub，国内一般更快
ghcr.io/3548784-netizen/strm-scraper:latest       # GHCR，GitHub 官方
```

两个都能匿名拉，不需要登录。压缩后约 217 MB，飞牛上拉取视网络情况 1~5 分钟。

> Docker Hub 匿名拉取有频次限制（同一 IP 每 6 小时 100 次）。只装一次不用管；如果经常重建容器，可以在飞牛 Docker 设置里登录 Docker Hub 账号，额度就按账号算。

### 飞牛 Docker 界面操作

1. 打开「Docker」应用 → 左侧「镜像」→ 右上角「添加镜像」
2. 仓库地址选 `docker.io`（有的版本叫 Docker Hub），镜像名填 `3548784/strm-scraper`，标签填 `latest`
   （也可以在搜索框里直接粘整串 `3548784/strm-scraper:latest`）
3. 点确定，等进度条走完，镜像列表里出现 `strm-scraper` 就拉好了

用 GHCR 的话把第 2 步换成：仓库地址 `ghcr.io`、镜像名 `3548784-netizen/strm-scraper`。

拉到之后建容器。建议用「项目」而不是「容器」，以后改配置方便：

「Docker」→「项目」→「新建」→ 把下面内容整段粘进去 → 保存并启动：

```yaml
services:
  strm-scraper:
    image: 3548784/strm-scraper:latest   # 拉不动就换 ghcr.io/3548784-netizen/strm-scraper:latest
    container_name: strm-scraper
    restart: unless-stopped
    ports:
      - "6666:6666"
    volumes:
      - /vol1/1000/LitePan/strm:/media   # ← 改成你的 strm 目录
      - ./config:/config
    environment:
      TZ: Asia/Shanghai
```

这段和仓库根目录的 `docker-compose.pull.yml` 完全一致，改完挂载路径直接粘即可。

### 命令行等价操作

```bash
docker pull 3548784/strm-scraper:latest
```

挂载和端口的注意事项见下面「方式 B」的说明，两者一样：挂载路径要换成本机的、`./config` 必须留着、6666 被占就改映射左边。

镜像在 `main` 分支每次推送后自动重建并推到两个仓库，所以 `latest` 始终是最新代码。想固定版本可以用提交哈希标签，比如 `sha-660eac5`。

## 3. 从源码构建（方式 B）

不想用现成镜像、想自己改代码的话走这条。首次构建要装 ffmpeg 和 Python 依赖，2~5 分钟。

### 放置项目

SSH 登录飞牛，挑一个数据盘目录，比如 `/vol1/1000/docker`：

```bash
cd /vol1/1000/docker
git clone https://github.com/3548784-netizen/strm-scraper.git
cd strm-scraper
```

没有 git 的话，也可以在自己电脑上下载 zip，通过飞牛的文件管理器上传解压。

### 改挂载路径

编辑 `docker-compose.yml`：

```yaml
services:
  strm-scraper:
    build: .
    container_name: strm-scraper
    restart: unless-stopped
    ports:
      - "6666:6666"
    volumes:
      - /vol1/1000/LitePan/strm:/media    # ← 改成你的 strm 目录
      - ./config:/config
    environment:
      TZ: Asia/Shanghai
```

要点：

- 左边是宿主机真实路径，右边保持 `/media`（或改的话，`config` 里的 `scan.roots` 要跟着改）
- `./config:/config` 必须留着。状态库存在这里，删了会丢失进度记录，虽然不影响已生成的图片，但下一轮会重新扫一遍全部文件
- 端口 6666 被占用就换，比如 `"16666:6666"`；只改左边，右边和 `config.yaml` 里的 `web.port` 保持一致就行
- 从旧版本升级上来的，`config/config.yaml` 里 `web.port` 可能还是 8080。要么把它改成 6666，要么删掉这个文件让容器重新生成一份，否则端口对不上，浏览器打不开

### 启动

```bash
docker compose up -d --build
```

首次构建要装 ffmpeg 和 Python 依赖，大概 2~5 分钟，取决于网络。看到 `Started` 就起来了。

查看日志：

```bash
docker compose logs -f
```

正常输出长这样：

```
[init] 未找到配置，生成默认配置：/config/config.yaml
19:28:23 INFO    [main        ] 配置：roots=[/media] mode=auto concurrency=3 ...
19:28:23 INFO    [main        ] 图片规格：poster 1000×1500（2:3），fanart 1920×1080（16:9）
19:28:23 INFO    [main        ] 日志文件：/config/logs/strm-scraper.log
19:28:23 INFO    [main        ] 网页控制台：http://0.0.0.0:6666
```

## 4. 打开控制台

浏览器访问 `http://飞牛局域网IP:6666`。

首次进入是「空闲」状态。如果 `scan.mode` 是 `auto`，启动时会自动开跑全量，控制台能看到进度往上走。

点「增量刮削」可以手动触发。

## 5. 配置媒体服务器（Emby / Jellyfin）

编辑 `config/config.yaml`，在 `servers` 段加机器（可以多台，Emby 和 Jellyfin 混合也行）：

```yaml
servers:
  - name: jellyfin
    type: jellyfin
    enabled: true
    url: "http://192.168.31.202:8097"
    api_key: "控制台 → API 密钥 → 新建"
    path_map:
      /media: /vol1/1000/LitePan/strm
```

`path_map` 是关键：容器里是 `/media`，Jellyfin 里是 `/vol1/1000/...`，不做前缀替换就匹配不到条目。

改完 `docker compose restart`，然后到控制台的「媒体服务器」面板点**测试连接**，能看到版本号就说明通了。

后续刮削会自动触发刷新；如果界面上海报还是没出来，点**补齐缺图**，它会把已刮削过的文件全部提交一次图片重建。

## 6. 验证产物

随便挑一部片子看目录：

```bash
ls -la "/vol1/1000/LitePan/strm/115影片AV/某个目录"
```

应该能看到和 `.strm` 同名的 `.nfo`、`-poster.jpg`、`-fanart.jpg`。海报是 2:3 竖版，背景图是 16:9，这两个比例 Emby 和 Jellyfin 都要求，写错了会被拉伸显示。

然后在媒体服务器里对那个库做一次「扫描媒体库文件」。Jellyfin 还必须刷新图片缓存，用上面的「补齐缺图」或开启 `servers[].refresh_after` 自动做。

## 7. 看日志

日志在 `config/logs/` 下（容器内 `/config/logs`）：

```bash
ls -la ./config/logs
# strm-scraper.log  errors-strm-scraper.log  ...

tail -f ./config/logs/strm-scraper.log        # 全量
tail -f ./config/logs/errors-strm-scraper.log # 只看异常
```

网页上的「运行日志」面板也能筛选、搜索、下载，不用每次都 SSH。

排查前先跑一次自检，它会一次查完 ffmpeg、目录、状态库、日志、服务器连通性：

```bash
docker exec strm-scraper python -m scraper.main --doctor
```

## 8. 用飞牛 Docker 界面管理（可选）

如果不想用命令行，飞牛的 Docker 应用里可以：

1. 打开「Docker」→「Compose」
2. 新建项目，把 `docker-compose.yml` 内容整段粘进去
3. 注意把 `build: .` 换成 `image: strm-scraper:latest`，或者先在 SSH 里 build 好

不过 compose 涉及构建，命令行更省事。

## 常见问题

**容器起来了但扫描不到文件**

`scan.roots` 是容器内路径。挂载 `/vol1/1000/LitePan/strm:/media` 时，配置里必须写 `/media`，不是宿主机路径。

**海报写不进去，日志报 Permission denied**

飞牛的用户目录属于 uid 1000 之类的普通用户，容器里默认的 root 通常能写。如果 `docker-compose.yml` 里解开了 `user: "0:0"` 之外的用户，改成 root：

```yaml
user: "0:0"
```

**想限制 CPU / 内存占用**

```yaml
deploy:
  resources:
    limits:
      cpus: "1.5"
      memory: 1G
```

抓帧主要吃网络和 CPU 的 H.265 解码，1 核够跑 2 个并发。

**拉镜像很慢或者报 `toomanyrequests`**

Docker Hub 匿名拉取按 IP 限流，每 6 小时 100 次，NAS 上多台设备共用一个出口 IP 时容易撞上。三个办法，任选：

```bash
# 换 GHCR
docker pull ghcr.io/3548784-netizen/strm-scraper:latest
```

```bash
# 或者给 Docker 配国内加速器（飞牛的「Docker → 设置 → 镜像加速」也能填）
# /etc/docker/daemon.json
{ "registry-mirrors": ["https://dockerproxy.com", "https://docker.m.daocloud.io"] }
```

或者登录 Docker Hub 账号再拉，额度改按账号算：

```bash
docker login
```

**改了配置不生效**

配置只在启动时读一次：

```bash
docker compose restart
```

**媒体库里有条目但没海报**

先看日志里的「回查」那条结论：显示「全部已有海报」说明服务端已经识别，客户端刷新即可；显示「N 条仍无海报」就点控制台的**补齐缺图**。Jellyfin 不会自动重读图片缓存，必须显式刷新；Emby 一般能自动发现。

**想清空进度重新来**

```bash
docker compose down
rm -rf ./config/state.db
docker compose up -d
```

已生成的图片不会删，想重刮就点「全量重刮」。
