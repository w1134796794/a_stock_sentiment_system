# 历史分钟缓存无损压缩

适用于系统盘 40GB、`cache/stock/tick` 占用较大的部署。解决磁盘占用，不增加物理内存。生产环境当前目录需以实际检查结果为准；本次用户确认项目在 `/opt/a_stock_sentiment_system`。

## 部署顺序

1. 先部署本次完整代码，重启使用该项目的 Web、采集、策略、回测与训练进程，使它们能够读取 `.csv.gz`。不要单独复制维护脚本，也不要先用 `gzip` 批量处理旧文件。
2. 在非交易维护窗口暂停采集、历史补数、训练、回放及盘前证据冻结任务。具体 systemd 服务名、timer 和 cron 需先核查。新代码的分钟写入和压缩共享目录锁；外部旧进程和手工写入不受该锁保护。
3. 在项目目录执行预演。默认仅处理上海日期今天之前的历史分钟缓存；不创建压缩文件，不删历史行，不修改数据库。

```bash
cd /opt/a_stock_sentiment_system
.venv/bin/python scripts/optimize_storage.py --target minutes --limit 100
```

结果中 `saved_mib` 是本批文件的压缩估算，不能直接当作整个 7.4GB 目录的节省值。`errors` 应为 0。存在错误时先处理对应文件，不要直接删除。

## 执行及验收

先处理 100 个文件，每个文件均完成 gzip 解压、SHA256 和长度校验，再替换原文件；单文件失败保留原始数据。逐文件生成临时文件，默认额外保留至少 512MiB 空闲空间。

```bash
.venv/bin/python scripts/optimize_storage.py --target minutes --limit 100 --apply
```

确认应用能够读取这批已压缩的历史分钟行情，且回测/训练正常后，再完成剩余文件：

```bash
.venv/bin/python scripts/optimize_storage.py --target minutes --apply

df -h /
du -sh cache/stock/tick
```

已压缩的文件会跳过。可用 `--before YYYYMMDD` 缩小范围，截止日期本身不处理，且不允许指定未来日期。非标准文件名跳过；损坏、冲突或权限不足会报错并保留源文件。`--report /path/to/new-report.jsonl` 可记录逐文件结果，文件必须尚不存在；不记录时只保留汇总和前 20 条样本，避免大报告占用空间。

维护完成后恢复先前暂停的服务，检查其状态并打开历史分钟行情、回放页面进行验证。压缩后逻辑文件名仍为 `.csv`，历史训练源哈希按解压后的原始字节校验。

## 防止新数据继续膨胀

默认新写入仍为 CSV，刷新已压缩文件时保持 gzip。若要让新文件也压缩，在应用及独立 worker 的环境配置中加入：

```dotenv
MINUTE_CACHE_COMPRESSION=true
```

重新启动相关进程后生效。该开关不替代历史维护步骤。未来新增直接读取分钟缓存的代码必须使用 `core.data.minute_cache_files`，不能只检查 `.csv` 是否存在。

## 还原

所有仍在使用中的进程保持支持 gzip 的版本，先预留足够磁盘空间，再还原。仅关闭配置开关不会自动解压历史文件。

```bash
.venv/bin/python scripts/optimize_storage.py --target minutes --restore-minutes --apply --limit 100
.venv/bin/python scripts/optimize_storage.py --target minutes --restore-minutes --apply
```

还原逐字节校验，恢复原始 CSV 后才移除 gzip。全部还原后才可以回滚到不支持 gzip 的旧代码。空间不足时暂停还原，保留压缩文件；不要强行删除。

## 可选：组合 JSON 去除排版空白

不默认处理 `webdata`，先解决占用最大的分钟缓存。需要进一步节省时，可单独预演：

```bash
.venv/bin/python scripts/optimize_storage.py --target combinations --limit 100
```

此功能保留所有字段、数组、数字原始精度和字符串内容，只去除字符串外的空白。重复键、NaN/Infinity、符号链接会拒绝处理；超过 16MiB 的 JSON 默认跳过以控制内存，可用 `--max-json-mb` 调整。

JSON 排版变更会改变原文件字节哈希；需要保留原始文件哈希审计的组合文件不应执行该功能。其排版无法从紧凑版原样还原，需要字节级回滚时应先备份到另一块磁盘。正式执行需显式添加 `--apply`，且不能与盘前输入冻结任务并行。

本工具不清理 snapshots、intraday_replays、模型、交易记录或数据库文件，也不设置历史数据保留期限。压缩减少的具体空间以服务器执行后结果为准。
