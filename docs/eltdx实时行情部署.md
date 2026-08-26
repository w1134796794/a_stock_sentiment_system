# eltdx 实时行情部署

## 架构

系统只保留一条实时行情链路：

```text
Web 内置或独立 eltdx 批量采集器
  -> Redis 标准快照与分钟缓存
  -> Linux Web / 盘中确认 / 持仓监控只读
```

不需要 QMT、券商客户端或资金门槛，也不接入原始 `pytdx`。板块代码、名称和成分股
继续使用同花顺口径，盘中板块强度由成分股的 eltdx 批量快照聚合。

## 默认：Web 内置采集

Web 启动时会自动启动 eltdx 采集线程，无需再手动执行脚本。多 Worker 使用 Redis
任务锁竞争，只有一个实例实际连接行情节点。配置：

安装项目依赖后配置：

```env
MARKET_DATA_NODE_ROLE=collector
REDIS_URL=redis://:password@redis-host:6379/0
ELTDX_POLL_INTERVAL_SECONDS=3
ELTDX_TIMEOUT_SECONDS=5
ELTDX_RETRY_COUNT=3
ELTDX_RETRY_BACKOFF_SECONDS=0.8
ELTDX_MINUTE_SYNC_SECONDS=60
ELTDX_COLLECTOR_ID=eltdx-auto
ELTDX_EMBEDDED_COLLECTOR_ENABLED=true
REALTIME_QUOTE_STALE_SECONDS=12
REALTIME_QUOTE_TTL_SECONDS=86400
```

若要固定使用某个 eltdx 节点，可额外设置 `ELTDX_HOST`；默认留空，让 eltdx 自行选择。

将 `ELTDX_COLLECTOR_ID` 改为服务器名称有助于排查来源。`time_raw` 是 eltdx 的协议
原始值，不能当作 HHMMSS 解析；系统以成功接收时间判断新鲜度，并保留原始值用于诊断。

## 可选：独立采集进程

只有需要把采集与 Web 进程完全隔离时才关闭内置采集并运行以下脚本：

```env
ELTDX_EMBEDDED_COLLECTOR_ENABLED=false
```

先做一次连通性检查：

```powershell
python scripts/run_eltdx_quote_collector.py --once --codes 000001,600000
```

常驻运行：

```powershell
python scripts/run_eltdx_quote_collector.py
```

采集器只在交易日 `09:15-15:05` 轮询。代码列表由昨日候选、近期龙头、当前持仓
及 `--codes` 指定代码合并去重后，一次传给 eltdx 批量接口；eltdx 内部按每批最多
80只拆分，但整轮只保持一条客户端连接。

## Linux 服务端

```env
MARKET_DATA_NODE_ROLE=server
REDIS_URL=redis://:password@redis-host:6379/0
REALTIME_QUOTE_STALE_SECONDS=12
```

服务端不需要安装 QMT、`xtquant` 或原始 `pytdx`。使用默认内置模式时，Web
进程中的唯一采集线程连接 eltdx；使用独立模式时，Web 只读取 Redis。

## 稳定性规则

- 每次批量请求最多重试 `ELTDX_RETRY_COUNT` 次，间隔采用指数退避。
- 请求失败不清空、不覆盖 Redis 中上一批有效行情。
- 页面根据 eltdx 源端时间计算新鲜度，超过阈值明确标记为过期，不能用于买点确认。
- 采集器健康状态写入 `realtime:collector:health`，有效行情元数据写入
  `realtime:quotes:meta`。
- 分钟序列在独立后台线程更新，不阻塞每轮快照请求。
- 高频快照只进 Redis 的有界序列，不写入日度因子表或 DuckDB。

## 排查

1. 查看采集器日志是否出现连续失败，以及是否显示“保留Redis中上一批行情”。
2. 检查 Windows 和 Linux 使用的是同一个 `REDIS_URL` 与 `REDIS_KEY_PREFIX`。
3. 检查 `realtime:collector:health` 的 `updated_at`、`count` 和
   `consecutive_failures`。
4. 检查 `realtime:quotes:meta` 的 `source` 是否为 `eltdx_batch`。
5. 如果固定节点不稳定，清空 `ELTDX_HOST` 后重启采集器，让 eltdx 重新选点。
