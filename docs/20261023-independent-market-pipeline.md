# 独立行情与策略运行

本版本不引入Kafka。盘前准备、行情采集、增量归档、策略判定是独立任务；Web默认只展示已发布结果，不启动采集或策略线程。

## 盘前

在交易日09:25前运行：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/prepare_realtime_collection.py
```

命令先校验/冻结前一交易日的候选及风险配置，再发布当日采集名单。已冻结的名单不可被盘中输入覆盖；缺少对应股票的行业、概念名称、同花顺代码或成分股绑定时准备失败，不在交易时段临时解析补齐。

启用原有`AUTOMATION_ENABLED`调度后，新增盘前任务默认从09:00开始、每分钟检查一次，独立启动上述准备命令。成功发布后不重复准备；失败保留日志并在09:25前重试，09:25起停止补建。可用`AUTOMATION_PREOPEN_TIME=08:45`调整开始时间。Web启动本身不再同步构建盘前输入；真实交易日历缺失时拒绝按工作日近似调度。

- 个股名单为近10日龙头池前30只与生产决策池全部候选的去重并集。决策池实际5只时不会凑数；两个池无重叠时为35只，重叠则更少。不按页面的20条显示上限截断生产采集名单。
- 保存股票代码、名称、池来源、板块概念绑定、成分股及冻结输入哈希。持仓不自动扩入此名单；名单外持仓会明确显示，不能承诺这些持仓的实时监控有效。
- 个股快照名单与板块证据名单分离。后者采集成分股最新报价并留存分钟证据，不记录为个股3秒快照序列，也不扩入买点池。
- 保存为`WEB_DATA_DIR/collection_plans/YYYYMMDD.json`及配套inputs文件，并发布到共享Redis。只有本机文件而没有共享Redis，不能完成跨进程/跨主机运行。
- 盘中页面顶部“当日行情采集”可展开名单，接口为`GET /api/realtime/collection-plan?market_date=YYYYMMDD`。
- 老盘前包没有历史分钟量能模型时，页面提前提示；相应买点按原有缺模型规则观察。不要盘中覆盖旧包来伪造开盘前已知信息。

## 运行进程

在采集机配置可用的`REDIS_URL`，所有进程使用相同Redis命名空间。以下命令分别作为受监督的长期进程运行：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/run_eltdx_quote_collector.py
.venv/Scripts/python.exe -X utf8 scripts/run_market_archiver.py
.venv/Scripts/python.exe -X utf8 scripts/run_realtime_strategy.py
```

默认策略只判定、记录信号并发布结果，不发送真实通知。部署确认后，策略命令添加`--notifications`才启动真实通知消费；必须先停用旧策略刷新进程，避免新旧系统同时运行。

原有自动模拟买入及持仓执行不再塞在策略循环中。需要保留时另启动`.venv/Scripts/python.exe -X utf8 scripts/run_realtime_execution.py --paper --monitor`；它只消费已发布结果和已保存行情，保留原执行检查与账户去重，不能把历史曾确认当成当前买点。`--monitor`单独使用时不自动成交，但会启用已有退出通知。没有启动此消费者，就不会自动运行原来与Web刷新捆绑的模拟执行；策略通知仍不等待模拟成交。

归档进程必须能访问采集器写入的同一`market_archive`目录（推荐在采集机运行），不能仅连接同一Redis就读取另一台机器的本地日志。

默认`REALTIME_STRATEGY_EXTERNAL=true`、`ELTDX_EMBEDDED_COLLECTOR_ENABLED=false`；外部策略模式下，即使旧环境变量要求内嵌采集，Web也不会启动采集线程。关闭外部模式是明确回退至旧Web调度，不属于本方案。

策略进程要求共享Redis，持有独占任务租约；Redis不可用或租约丢失则停止执行，不降级为私有内存继续确认。Web未取得独立进程结果时只显示等待，不临时运行策略补结果。

## 存储边界

1. 个股报价标准化后，先以SQLite WAL、FULL同步提交方式写入`market_archive/ingestion.sqlite3`。这是可靠接收日志，不是等待三分钟的内存队列。
2. 日志成功后才更新Redis热行情。快照按真实接收时间保留最近180秒，使用新版本有序集合键；不以60条固定记录数代替时间窗口。断流后读取也会排除过期数据。
3. 独立归档进程立即、连续、批量复制到`market_archive/market_YYYYMMDD.sqlite3`，确认事务提交后才删除接收日志。复制成功但确认前崩溃时，通过唯一事件ID幂等重放。
4. 超过三分钟只影响热窗口。归档失败不删除待处理日志，积压数和最早待归档时间可查询；运维必须监控磁盘容量，磁盘写失败会使该批行情无法作为成功采集发布。SQLite日志/WAL不等于异机备份，磁盘损坏仍需备份策略。
5. 原始3秒快照、分钟序列修订和有效竞价参考分别归档。分钟结构和真实开盘价不随180秒快照淘汰；板块配对分钟仍按原有独立证据仓库保存。

策略默认使用`ReadOnlyMarketData`，无eltdx/Tushare/Ashare连接入口。实时分钟读取在所有节点均为只读；竞价及昨收仅从Redis、冻结输入或已有本地文件读取。研究/历史补数接口保留给独立的数据准备任务，不从实时策略缺数分支调用。

## 数据驱动

每批热数据完整写入后发布Redis行情修订号和Pub/Sub唤醒提示。策略读取存储中的最新修订，不把通知消息本身作为行情事实。分钟同步也发布修订，板块更新同样可以唤醒判断。

- 一个策略进程串行运行一轮，不堆叠重入。多个更新合并为最新修订，但该轮读取180秒序列，不能用单一末价替代连续性检查。
- 每秒超时醒来仅检查修订；没有新数据不重新计算全池。漏掉唤醒消息后仍能通过修订号发现更新。
- 同一轮内逐股确认后立即执行原有健康门禁、留档并在启用通知时入队，不等待其余股票或模拟成交。
- Web展示时重新检查结果及原行情年龄，停机后不会保留过期的绿色可执行确认。历史“曾确认”留档不等于当前仍可买入。
- 午休/非交易时段不运行买点判断。策略输入只使用盘前冻结版本，缺数时等待，不在线修复成员关系或重新筛选股票。

本地诊断Web使用`--lifespan off`，不代表这些长期进程已部署或真实通知已启用。必须分别确认采集、归档、策略心跳及Redis连通性；不要只重启Web就认为新架构已运行。

## Linux开机自启

仓库原有GitHub Actions部署只重启`a-stock`。首次需要在服务器安装独立进程的systemd配置；以后系统重启自动拉起，部署流程也会重启已启用的`a-stock-market.target`。不需要每天手工启动，也不需要定时重启进程。

先检查现有Web运行账号和配置，以下命令中的`astock`替换为实际账号，项目路径使用服务器真实路径：

```bash
systemctl show a-stock -p User -p WorkingDirectory -p EnvironmentFiles
cd /opt/a_stock_sentiment_system
sudo .venv/bin/python scripts/install_market_systemd.py --user astock --install --notifications
sudo systemctl restart a-stock-market.target
sudo systemctl status a-stock-collector a-stock-archiver a-stock-strategy --no-pager
```

安装命令仅写入服务配置并设置开机自启，不立即启动或重启；启动是下一条显式命令。不加`--install`仅在`outputs/systemd`生成预览文件；不加`--notifications`不发送真实通知，不启用模拟交易或持仓执行。

三个服务使用同一项目目录、Python环境、运行账号和`.env`；可用`--python /absolute/venv/bin/python`及`--env-file /absolute/shared.env`指定已有环境。该环境文件须符合systemd的`KEY=value`格式，不使用`export`、shell命令或变量展开。Web若另有Environment配置，必须将Redis地址、前缀、数据目录、通知配置等同步至共享文件。`.env`应设置`REALTIME_STRATEGY_EXTERNAL=true`、`ELTDX_EMBEDDED_COLLECTOR_ENABLED=false`并重启Web，停止原有独立旧策略进程，避免新旧逻辑同时运行。运行账号须能读取代码/环境文件，写入数据、缓存和归档目录。脚本不修改目录所有者，也不自动安装Redis。

```bash
sudo systemctl restart a-stock-market.target
sudo systemctl stop a-stock-market.target
sudo journalctl -u a-stock-collector -u a-stock-archiver -u a-stock-strategy -f
```

`restart`统一重启三个进程；`stop`暂时停止，服务器重启仍自动恢复。永久停用使用`sudo systemctl disable --now a-stock-market.target`。单独重启某个进程可使用对应service名称。服务进程异常退出后5秒重启；进程仍存活但采集停滞时，仍需监控原有行情新鲜度、策略心跳和归档积压，不能把systemd的active当作行情健康。

盘前任务仍由启用`AUTOMATION_ENABLED`的Web调度器在真实交易日09:00至09:25运行；这些systemd服务不会绕过冻结输入要求。若服务器在09:25之后才启动且没有当天名单，会等待并显示缺失原因，不在盘中补建。首次安装后还需核对`/api/realtime/collection-plan`中的名单、采集计划ID及各进程健康状态。
