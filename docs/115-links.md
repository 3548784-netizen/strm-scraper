# 115 网盘直链：连接维护与批量节奏

> 适用版本：本仓库 `scraper/links.py` 落地之后。
> 相关代码：`scraper/links.py`（台账 + 闸门 + 重试策略）、`scraper/state.py`（持久队列与
> `strm_links` 表）、`scraper/probe.py` / `scraper/frames.py`（所有网络出口）、
> `scraper/worker.py`（批量调度）、`scraper/web.py`（`/api/links*`、`/api/settings`）。

## 1. 要解决的问题

`.strm` 是纯文本，里面第一行 `http(s)://…` 就是 115 资源的**连接地址**（通常是带签名的
直链，或转直链服务给出的播放 URL）。刮削一个 strm 至少要连网盘 3 次：一次 `ffprobe`
拿时长分辨率、再抓 2 帧挑图；开了缩略图、手动选帧、网页预览时更多。

115 这类网盘的风控主要看**瞬时并发连接数**和短时间请求密度，撞到之后普遍返回
`403 Forbidden` 而不是 `429`。原来的实现有三个缺口：

| 缺口 | 后果 |
| --- | --- |
| 只在工作线程入口做一次被动退避，批量任务 / 网页选帧 / 单条刮削各跑各的 | 三路流量叠加，实际并发远大于 `scan.concurrency` |
| 每轮任务开头 `reset(("failed",))` 把全部失败项一次性放回队首 | 下一轮「立即执行」重放一次失败高峰，正好在冷却期结束时再撞一次 |
| strm 每次现读现用，没有连接台账 | 无法区分「链接过期」「并发被掐」「任务被暂停」，也无法保证已有连接不失效 |

本实现把连接当成需要维护的资源：**持久台账 + 全进程共用的一份请求额度 + 到期才重试的
持久队列**。不新建并行队列，`items` 表本身就是队列；突发任务先全部落库（不丢），再由
闸门按节奏放出去（不撞）。

## 2. 四层结构

```
  突发 N 个 strm
        │  一次性 INSERT OR IGNORE（纯本地，零网络请求）
        ▼
  items 表（持久队列：pending / running / failed / next_retry）
        │  Runner 每轮只取 min(scan.concurrency, links.batch_size) 条
        ▼
  LinkRegistry.resolve()  ←→  strm_links 表（sticky 直链台账，本地 IO）
        │  得到 url（不因 TTL / 重启 / 任务删除而失效）
        ▼
  RequestGate.lease(url)  ←  全进程唯一：批量 / 选帧 / 单条共用
        │  FIFO 票 → 令牌桶 → 全局并发 → 同域并发 → 最小间隔 → 退避期
        ▼
  ffprobe / ffmpeg（真正发出去的请求）
        │
        └─ 结果回写：LinkRegistry.note() + 失败分类退避（next_retry）
```

### 2.1 连接台账（`strm_links`）

* 一条 strm 只在**首次**、或文件内容签名（`size:mtime`）变化时解析一次。
* 直链**粘住不放**：`link_ttl_minutes` 到期只是把状态记成 `stale`，仍按原链接复用；
  重启进程、删掉任务记录、批量任务涌入都不会让它失效。**不会**在批量任务开始时集体
  重新校验——那恰恰是最容易把账号撞进风控的动作。
* 请求结果只改状态：`fresh`（成功）、`stale`（403 / 超时 / 临时错误，链接保留）、
  `invalid`（404 / 401 / 无视频轨等永久错误）。`403` 一律按 `stale` 处理，因为它也可能
  只是并发被掐，不能当作签名过期。
* 只有 `state=invalid` 且配置了 `refresh_url` 时，才按 `verify_budget_per_minute`
  的预算发**一次**刷新请求；预算耗尽就直接复用旧链接，绝不排队等刷新。
* 台账与 `items` 表同库，但**删除任务不会清台账**，「已有连接保持有效」优先于列表干净。

### 2.2 全局请求闸门（`RequestGate`）

所有 `ffprobe` / `ffmpeg` 调用（`worker.process`、`FramePicker`、单独刮削、抓帧接口）
都必须经过 `links.lease(url, abort)`。本地文件和 `bypass_hosts` 里的主机直连，不占额度。

放行需要同时满足：队首票 → 退避期已过 → 令牌 ≥ 1 → 全局在途 < 上限 → 同域在途 < 上限
→ 距上次放行 ≥ 最小间隔。队首严格 FIFO，避免重试和首次请求乱序冲出去、先来的饿死。

自适应降级：某次请求返回 403 / 429 / 5xx（`throttle.detect()` 识别），闸门立刻把并发
降到 1 路、加大间隔并冷却 `cooldown_seconds`；冷却结束后连续成功 `recover_every` 次才
回升一档。**降级状态跨任务轮次保留**，用户点「开始任务」不会把它清掉。

### 2.3 持久队列与批次

* 突发任务：`ensure()` 一次事务写入，全部 `pending`，**不丢、不去重之外的丢弃**。
* 派发：每轮在途上限 `min(scan.concurrency, links.batch_size)`，批次之间可加
  `batch_interval_ms`。其余任务留在库里等下一批。
* 排队满 / 排队超时：`LinkDeferred` —— 请求**没发出去**，任务退回 `pending` 并写
  `next_retry`，**不计 attempts、不算失败、不占网盘额度**。
* `next_pending()` 只取 `next_retry` 已到期的条目；`requeue_due()` 每秒至多一次把到期
  的失败项放回队列（且 `attempts < retry_max_attempts`）。

### 2.4 失败恢复

| 分类 | 判定 | 处理 |
| --- | --- | --- |
| `throttle` | 403 / 429 / 5xx / `access denied` | 退避基数 ×3，指数增长 + 抖动；闸门同时降级冷却 |
| `permanent` | 404 / 401 / 无视频轨 / 地址不存在 / 协议不支持 | 不自动重试，留在列表里等人工处理 |
| `transient` | 超时、连接拒绝、其它未知错误 | 普通指数退避 + 抖动 |

`retry_delay = min(retry_max_seconds, base × 2^(attempt-1)) × (1 ± retry_jitter)`，
其中 `retry_max_seconds` 是**硬上限**：抖动后若超过它，就改成在
`[retry_max_seconds × (1 - retry_jitter), retry_max_seconds]` 区间内向下抖，
不允许把配置的上限顶穿（否则 1800 秒配出来可能变成 2250 秒），同时仍然错开唤醒时刻。
**抖动是必须的**：同一批任务在同一秒失败、退避时长又相同，就会在同一秒一起醒来，
形成完全同步的重试风暴。达到 `retry_max_attempts` 后不再自动重试。

## 3. 可配置参数

网页「设置 → 115 网盘连接与节奏」可调常用项，保存后立即生效（不用重启）；
完整参数在 `config.yaml` 的 `links:` 段，也支持环境变量（如
`WS_LINKS__MAX_INFLIGHT=1`、`WS_LINKS__RATE_PER_MINUTE=20`）。

| 参数 | 默认 | 范围 | 含义 |
| --- | --- | --- | --- |
| `enabled` | `true` | bool | 总开关。关掉后所有请求直连（不建议在网盘源上关） |
| `max_inflight` | `2` | 1–64 | 全进程同时发往网盘的请求上限 |
| `per_host_inflight` | `2` | 1–64 | 同一域名 / 中转服务同时几路 |
| `rate_per_minute` | `60` | 0.1–100000 | 令牌桶速率：每分钟最多发起多少次新请求 |
| `burst` | `2` | 1–64 | 允许的瞬时突发额度（自动不超过 `max_inflight`） |
| `min_gap_ms` | `300` | 0–60000 | 两次放行之间的最小间隔 |
| `queue_max` | `2000` | 0–1e6 | 排队等待槽位上限；超出的任务退回持久队列 |
| `acquire_timeout` | `180` | 1–86400 | 单个请求最多排队多少秒 |
| `bypass_hosts` | `127.0.0.1, localhost, ::1` | list | 本机 / 测试媒体服务，不占网盘额度 |
| `cooldown_seconds` | `60` | 0–86400 | 撞限流后降级保持时长 |
| `degrade_gap_ms` | `800` | 0–60000 | 降级期间的最小间隔 |
| `recover_every` | `8` | 1–1000 | 连续成功多少次回升一档 |
| `link_ttl_minutes` | `360` | 1–100000 | 直链「可能过期」的时间；只标记不删除 |
| `sticky` | `true` | bool | 粘住已有连接 |
| `refresh_url` / `refresh_token` | 空 | str | 可选刷新钩子（协议见 §6） |
| `refresh_on_error` | `true` | bool | 失效时是否允许走刷新钩子 |
| `verify_budget_per_minute` | `6` | 0–10000 | 每分钟最多发多少次刷新请求 |
| `batch_size` | `20` | 1–10000 | 单轮在途任务上限（与 `scan.concurrency` 取小） |
| `batch_interval_ms` | `0` | 0–60000 | 两次派发之间的间隔 |
| `retry_base_seconds` | `30` | 1–86400 | 退避基数（限流类 ×3） |
| `retry_max_seconds` | `1800` | 1–604800 | 退避上限 |
| `retry_jitter` | `0.30` | 0–0.9 | 抖动幅度 |
| `retry_max_attempts` | `5` | 0–100 | 最大自动重试次数 |

### 三档预设

```yaml
# 保守（已被掐过 / 大库首次全量，最不容易触发风控）
links: {max_inflight: 1, per_host_inflight: 1, rate_per_minute: 20,
        min_gap_ms: 1000, batch_size: 4, retry_base_seconds: 60}

# 默认（均衡）
links: {max_inflight: 2, per_host_inflight: 2, rate_per_minute: 60,
        min_gap_ms: 300, batch_size: 20, retry_base_seconds: 30}

# 宽松（中转服务在自己内网、确认不会掐连接）
links: {max_inflight: 4, per_host_inflight: 4, rate_per_minute: 240,
        min_gap_ms: 0, batch_size: 40}
```

**吞吐量估算**：每个 strm 约需 `1 + scrape.frames (+1 缩略图)` 次请求。默认
`rate_per_minute=60`、`frames=2`、`thumb=on` 时约 `60 / 4 = 15 个 strm/分钟`。
要提速优先调 `rate_per_minute` 和 `scrape.frames`，不要一上来就加 `max_inflight`——
并发连接数才是 115 风控最敏感的那一项。

## 4. 高并发与异常场景的行为约定

1. **短时间涌入大量 strm**：全部落库为 `pending`（一次事务，零网络请求）；派发窗口
   固定，队列深度可在 `/api/links` 看到。不会「因为线程多就一起冲出去」，也不会丢任务。
2. **多个入口同时要连接**（批量跑着又打开选帧面板 / 点单条刮削）：共用同一份额度，
   按 FIFO 排队；选帧请求带着客户端断开的 `abort`，关面板即释放排队位置。
3. **网盘返回 403 / 429 / 5xx**：本次请求失败，但**不立刻重试**；闸门降到单路并冷却，
   任务写 `next_retry` 后退回。冷却由 `throttle.GUARD` 与闸门共同约束，不会在冷却结束
   瞬间整批复活。
4. **整批失败 / 重试风暴**：退避带抖动、按次数指数增长、到期才入队；`requeue_due()`
   每秒至多一次，且受 `retry_max_attempts` 限制。失败高峰不会被重放。
   退避上限 `retry_max_seconds` 是硬约束，抖动只允许在区间内向下偏，不会顶穿配置值。
5. **队列满 / 排队超时 / 暂停 / 取消**：`LinkDeferred` / `LinkAborted`，请求根本没发出；
   任务退回 `pending`（或写短 `next_retry`），`attempts` 不增加，链接状态不变。
6. **进程被 kill / 容器重启**：`running` 在启动时退回 `pending`；`strm_links` 与
   `next_retry` 都在持久库里，已有直链和待重试时间原样保留。
7. **strm 被外部工具重写**：`size:mtime` 签名变化 → 重新解析文件内容并更新台账，
   不产生任何网盘请求。
8. **直链签名真的过期**（404 / 401）：标 `invalid`；若没配刷新钩子就不再自动重试，
   需要更新 strm 文件或在网页上手动重试。不会反复拿一条死链去撞。
9. **本机 / 测试媒体服务**：`bypass_hosts` 内的主机不受限速影响，本地回归测试的
   耗时和以前一致。
10. **`links.enabled=false`**：闸门完全直通，仅台账与重试策略仍生效（可单独用于
    「只想要连接持久化」的场景）。
11. **只跑某个文件夹 / 勾选部分文件的范围任务**：派发、`requeue_due()` 恢复、
    「还有多少在等退避」的计数都带同一个范围。范围外的失败项不会因为本轮任务
    被翻成 `pending`，用户不会在别的目录看到状态莫名跳动；等下次整库任务才按
    各自的退避时间恢复。文件管理在任务运行中追加勾选（`add_scope()`）时，范围
    每轮循环重新读取，新加的文件当轮就能被派发。

## 5. 批量任务下的验证要点

离线验收（不连网盘、不动真实素材）：

```bash
python tools/check_links.py        # 台账 / 闸门 / 退避 / 突发批量 / 重启保持
python tools/check_pick_throttle.py
python tools/check_pick_perf.py
python tools/check_delete_perf.py
python tools/check_reuse_products.py --base http://127.0.0.1:6687
python tools/check_scan_perf.py
```

联调时逐项确认：

- [ ] **不丢任务**：一次性提交 N 个 strm（建议 N ≥ 200），结束后 `items` 里
      `pending + running = 0`，成功 + 跳过 + 失败 = N。
- [ ] **并发不越界**：`/api/links` 的 `gate.peak ≤ max_inflight`，且任一时刻
      `gate.per_host[*] ≤ per_host_inflight`。
- [ ] **速率平稳**：把请求时间戳按秒分桶，任何 1 秒窗口内放行次数不超过
      `burst + ceil(rate_per_minute/60)`；不出现「一大段静默 + 一瞬几十个」。
- [ ] **排队可观测**：突发时 `gate.waiting > 0`、`queue.pending` 随处理单调下降，
      界面不报失败。
- [ ] **已有连接保持**：跑前记录 `strm_links` 条数与 URL，重启进程 / 删掉任务记录 /
      再跑一轮，台账条数与 URL 不变，`fresh` 状态不被批量重置。
- [ ] **不批量复验**：一轮任务里 `registry.refreshes` 不超过
      `verify_budget_per_minute × 分钟数`，`budget_denied > 0` 时链接仍可复用。
- [ ] **403 不放大**：注入全批 403 后，`gate.degraded=true`、`active_limit=1`；
      之后 10 秒内的请求数明显低于正常速率，失败项的 `next_retry` 互不相同（抖动生效）；
      冷却结束后并发按 `recover_every` 逐档回升。
- [ ] **失败可恢复**：把某条 `next_retry` 手工改成过去时间，下一轮（或同一轮 1 秒内）
      会被 `requeue_due()` 捡起；`attempts ≥ retry_max_attempts` 的不再自动入队。
- [ ] **永久错误不重试**：404 的条目 `next_retry=0`，日志标「不再自动重试」。
- [ ] **暂停 / 取消**：排队中的任务 `attempts` 不变、全部退回 `pending`，
      `gate.inflight` 归零。
- [ ] **配置热更新**：网页改 `max_inflight` 保存后，`/api/links` 的
      `config.max_inflight` 与 `gate.max_inflight` 立即跟着变，无需重启。

## 6. 直链刷新钩子（可选）

带签名的 115 直链会过期时，可以接一个自己的换链服务。留空表示**以 strm 文件为唯一
来源**（绝大多数 `115 + Alist/CloudDrive2` 的用法属于这种：strm 里就是稳定的播放地址）。

```
GET {refresh_url}?...      # 模板支持 {path}（strm 绝对路径）、{url}（旧直链）
Authorization: Bearer {refresh_token}   # 配了才带
响应：{"url": "https://.../play?token=NEW"}   或直接一行纯 URL
```

约定：

* 只接受 `http(s)://` 开头的返回；解析失败保留原链接，不删台账。
* 每次调用消耗 1 个 `verify_budget_per_minute` 令牌，预算耗尽直接跳过，**不排队等刷新**。
* 钩子本身的网络请求不经过 `RequestGate`（它不是网盘连接），超时 8 秒。
* 刷新成功后更新台账为 `fresh` 并重置失败次数，任务按新链接继续。
* 网页 `POST /api/links/refresh {"path": "..."}` 可手动触发一次，同样受预算限制。

## 7. HTTP 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/links` | 闸门实时状态（在途 / 排队 / 峰值 / 降级）、台账分布、生效参数、队列计数 |
| `GET /api/links/list?limit=50&state=stale` | 台账明细，直链已打码 |
| `POST /api/links/retry` | `{"all": true}` 或 `{"paths": [...]}`，把失败任务放回队列（仍走闸门） |
| `POST /api/links/refresh` | `{"path": "..."}`，预算内手动复验一条直链 |
| `GET /api/status` | 增加 `links` 字段（同 `/api/links`）与 `effective_concurrency` |
| `GET/POST /api/settings` | `links` 段的常用节奏参数，保存后立即生效 |
