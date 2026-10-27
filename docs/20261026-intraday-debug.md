# 盘中debug留档与高开加速窗口

独立 `run_realtime_strategy.py` 默认启用 `INTRADAY_DEBUG_ENABLED=true`。
统一目录为 `WEB_DATA_DIR/logs/intraday_debug/YYYYMMDD.sqlite3`，不依赖网页打开。
每条正文为 zlib 压缩的 JSON，使用项目查询工具解码，不能直接按文本打开。

## 记录内容

- `evaluation`：每只股票的规则判定，包含策略执行参数、入场参数、实际条件检查、
  板块覆盖详情、源行情/接收时间、最近三条快照及性能计时。
- `health_gate`：最终健康门禁和名单版本检查后的结果，与原始规则判定分开记录。
- `error`：逐股或策略循环异常。未创建进程前的启动故障仍需看服务日志。
- `cycle`：执行周期、耗时、变化代码、复用数及逐股计时。

原因、状态、回退原因或条件布尔状态变化时立即记录；否则同股同阶段每分钟记录一条。
每分钟样本保留最新实际值与阈值，不为每次价格跳动另存一份完整行情。
没有执行到的条件缺省不表示失败；`null` 表示未知。只记录真实执行，不运行额外的
假设评估。异常或磁盘写入失败不阻断策略，每分钟最多警告一次。
日志不自动删除历史日期，需纳入日常磁盘容量监控。

```bash
cd /opt/a_stock_sentiment_system
.venv/bin/python scripts/read_intraday_debug.py --date 20261027 --code 300369 --limit 50
.venv/bin/python scripts/read_intraday_debug.py --date 20261027 --code 300369 --since 09:30:00 --until 10:00:00 --stage evaluation
.venv/bin/python scripts/read_intraday_debug.py --date 20261027 --stage health_gate --reason 板块
```

查询默认只读最后50条，并按记录顺序输出。支持 `--root` 指定已复制的日志目录。
日志从部署并启动更新后的策略进程开始产生，不补造过去的debug记录。

## 各入场模式的开盘窗口

三种普通入场模式分别配置，均接受整数1至5：

```yaml
acceleration_opening_minutes: 1
continuation_opening_minutes: 3
weak_opening_minutes: 5
```

分别对应高开加速、强势延续、弱转强。专用结构策略不受此参数影响。
实时快照和分钟回退/回测共用这些参数。混合模式按实际开盘分层选择对应窗口。
必须取得从09:31起的完整分钟结构，不以稀疏快照代替K线高低点。
龙头身份、成交、连续快照、板块、价格质量、健康与可成交门禁仍适用。
这不是09:30即刻买入通道；取得完整分钟线前仍等待，已封死涨停也不保证可成交。

冻结风险配置缺少对应字段时按5解释，旧计划不被追溯修改。
新的默认值在后续正常生成的盘前冻结输入中生效；不要删除或重建今天的冻结计划。
生产规则版本更新为 `production_shared_v12_configurable_opening_windows`。
