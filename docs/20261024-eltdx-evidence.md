# eltdx 补充证据：第一至四阶段

第五至七阶段见 [研究数据、证据页面与离线对照](20261024-eltdx-research-ui-experiments.md)。

本功能只采集、存储和回放观察证据。它不修改生产买点、仓位、通知、账户和正式板块口径。SDK 保持 `eltdx==3.3.0`。

## 启用与运行

三个新增开关默认全部关闭。按需在采集器运行环境中设置，随后重启采集器：

```dotenv
ELTDX_AUCTION_EVIDENCE_ENABLED=true
ELTDX_TRADE_EVIDENCE_ENABLED=true
ELTDX_DEPTH_EVIDENCE_ENABLED=true
ELTDX_EVIDENCE_POLL_SECONDS=5
ELTDX_EVIDENCE_WORKERS=2
ELTDX_TRADE_MAX_PAGES=3
ELTDX_EVIDENCE_MAX_PENDING=10000
ELTDX_EVIDENCE_MIN_FREE_MB=512
```

沿用现有独立进程，不需要新增服务：

```bash
.venv/bin/python scripts/check_eltdx_runtime.py
.venv/bin/python scripts/run_eltdx_quote_collector.py
.venv/bin/python scripts/run_market_archiver.py
```

需要可用的共享 Redis、真实交易日历、09:25 前已冻结的当日名单，以及数据源的网络权限。独立行情/归档进程需共享同一归档目录。离线 SDK 检查成功只证明接口存在，不证明实时连接或历史覆盖。

云环境还需通过平台允许实际使用的通达信 TCP 目标和端口（行情通常为 7709；具体以 SDK 选用节点为准）。HTTP 域名允许不等于 TCP 访问授权；不绕过平台代理。当前云端 TCP 允许列表为空，未验证远端行情，也未开启真实采集。

采集范围严格采用冻结名单 `rows`，不扩入所有板块成分股或名单外持仓。新通道使用独立连接、最多两个工作线程，与原报价请求分开。一个批次未结束时不堆叠任务；租约丢失或跨日后停止发布。日志积压或空闲磁盘低于阈值时暂停新通道。并发写盘仍共享硬件，需在真实负载下观察原报价 P95 周期与新鲜度，不能用线程隔离代替性能验收。

`realtime:evidence:health` 以及采集器健康中的 `evidence` 包含分股票、接口状态、分页覆盖、错误和耗时。回退时关闭三个新增开关并重启采集器，已有归档保留。

## 数据契约

证据封装包含 `schema_version/source/sdk_version/code/market_date/event_time/time_precision/received_at/published_at/collection_plan_id/capture_mode/quality_status/quality_reasons/coverage/rows/summary`。

- 金额为元，价格为元；`volume_hand` 为手，`volume_shares` 为股。
- 缺失数值保持 `null`；成交量为 0 与数据缺失不混同。
- 成交明细保留分钟精度，不把 SDK 的 `:00` 当作真实秒级时间。逐笔指来源成交明细，不承诺交易所逐单或 L2 覆盖。
- 买卖方向是数据商分类，未知方向独立保存，不能当作真实主力身份。
- 成交额由价格与手数估算，明确标记；竞价撮合过程量额不计入实际成交统计。
- 状态为 `ok/empty/partial/unsupported/network_error/parse_error/request_error`。来源没有明确说明时，不推断空数据意味着停牌、退市或超出历史覆盖范围。
- `formal_eligible=false`，所有新证据默认不影响策略。

### 竞价

`auctions.series(code, date)` 显式传入交易日；旧接口无法查历史时不调用当日接口冒充历史。

新增 `auction_series` 与 `opening_match` 独立事件。正式开盘撮合使用 `trades.opening_match_today/history()`，仅接受 `event_kind=opening_match` 且时间为 09:25 的记录。竞价快照与报价中的开盘参考额不替代此记录。

实时采集在 09:15—09:30 留存竞价曲线，09:25 后尝试正式撮合。正式撮合与旧缓存参考冲突时记录 `opening_reference_conflict`，不覆盖旧策略输入。输出最近一分钟竞价价格变化、撮合量变化，但不推导真实撤单量。

### 成交明细

服务端 `start=0` 是最新页，增大偏移读取旧页；页内顺序保留，反转页顺序得到按时间排列的记录。

初次建立序列必须读到空页。后续采集用上批末尾最多 16 条指纹做唯一连续重叠核验，只给新增记录分配序列和 ID。不会把分页索引当作交易所 ID，也不会按“同一分钟、同价格”合并真实重复成交。

多页实时查询还会复查首页；查询期间首页变化、锚点不唯一、缺失或分页预算耗尽时均标记 `partial`，不推进游标，不纳入可信汇总。原始观察保留用于诊断。游标与事件在同一 SQLite 事务提交，Redis 清空或进程重启不会丢失序列位置。

默认 3 页预算可能不足以在盘中首次追溯高活跃股票全天记录；应尽量盘前启动。诊断持续 `pagination_limit/overlap_missing` 时可在独立压测后提高预算；不能用历史补数偷偷推进实时游标。

每批输出实际成交数量、量额、方向金额占比和前五笔金额占比。回放另提供按来源分钟时间计算的最近 300 秒汇总，窗口边界精度明确为分钟。

### 五档盘口

优先保留主报价已返回的五档，缺失、不完整或过期时才使用独立 `quotes.get_depth()`。快照内盘口随原快照进入热窗口和归档，不另存重复盘口事件。

包含价差、买卖五档深度、失衡比例以及单边/双边/空盘口状态。档位不足、非有限数值、逆序或交叉盘口标记质量问题；单边盘口不等于停牌。盘口不证明排队位置或真实成交。

## 可靠存储与可见时间

新增事件复用 `market_archive/ingestion.sqlite3` 及每日 `market_YYYYMMDD.sqlite3`，归档器无需改动。

顺序为：提交接收日志及游标 → 发布 Redis → 留存 `evidence_publication`。日志失败不得发布；Redis 失败仍保留日志与游标。发布标记记录的是发布完成后的观测，不能表示精确 Redis 提交时刻。标记缺失的事件不进入补充证据回放。

已发布结果按日期、股票和种类保存在 `realtime:evidence:v1:*`，TTL 为一天。缓存封装中的 `published_at` 可为空，因为不能在发布完成前声称知道完成时刻；归档中的发布标记提供可验证的可见时间。只读入口 `ReadOnlyMarketData.get_market_evidence()` 返回新鲜度，不修复、不联网。

## 历史补数

默认只预演，不联网、不创建数据目录：

```bash
.venv/bin/python scripts/prefetch_eltdx_evidence.py --start 20261022 --end 20261023 --codes 000001,600000
```

显式执行：

```bash
.venv/bin/python scripts/prefetch_eltdx_evidence.py --start 20261022 --end 20261023 --codes 000001,600000 --apply
```

可用 `--kinds auction_series,opening_match,trade_batch`、`--max-pages 100`、`--data-root PATH`。

所有结果写入 `WEB_DATA_DIR/market_archive/backfill/`，不写 Redis、不覆盖冻结名单或实时归档。完成任务有持久检查点；重复运行跳过完成项，失败或分页不完整项继续重试。`--refresh` 显式重新抓取并保留新版本。日期范围按显式日期逐日执行，节假日可能为空；空结果不证明该日完整覆盖。

历史五档无此能力时明确不支持，不用当前盘口冒充。

## 只读回放

现有 `scripts/replay_snapshot_archive.py` 结果新增 `supplemental_evidence`，冻结回放输入同时包含 `evidence_events`。新证据与既有买点计算隔离；不会改变原来的确认结果。旧归档无新增事件时该部分为空。

独立查看：

```bash
.venv/bin/python scripts/replay_eltdx_evidence.py --market-date 20261023 --until 10:00:00
.venv/bin/python scripts/replay_eltdx_evidence.py --market-date 20261023 --until 10:00:00 --research-backfill
```

正常回放要求匹配的冻结计划，仅使用截止前具有发布观测的实时记录；文件以 SQLite `mode=ro` 读取，缺文件不创建。研究模式只读取 backfill 目录，明确 `research_only=true`，按来源事件时间截断，不宣称数据当时已被系统接收。

回放保留数据缺失、冲突、质量和新鲜度；不会请求数据、发通知或修改账户。未启用新门禁，不代表策略已被增强或收益已改善。
