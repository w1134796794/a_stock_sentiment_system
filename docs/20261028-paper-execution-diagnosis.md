# 微信已提醒但模拟盘未买入：20261028

只读分析 Downloads/20261028.sqlite3，未更改源数据库。表仅有 debug_events，不包含账户成交、通知 outbox、systemd 状态，不能据此证明哪条微信送达，也不能证明执行服务实际启动过。

## 日志事实

共有 evaluation 10093、health_gate 10156、cycle 242 条采样记录。正式确认样本共 103 条，涉及五只股票：

| 代码 | 确认样本 | 未带模拟成交参考价 | 带模拟成交参考价 |
|---|---:|---:|---:|
| 605488 福莱新材 | 39 | 32 | 7 |
| 688836 | 22 | 21 | 1 |
| 301313 凡拓数创 | 19 | 15 | 4 |
| 002929 润建股份 | 15 | 11 | 4 |
| 003021 兆威机电 | 8 | 7 | 1 |

86/103 条确认尚无模拟成交价。福莱新材09:35:55记录已确认，确认时间09:35:54，但entry_time空、entry_price为空。09:41:42记录则已有09:41:41、47.10元参考成交。后者仍不证明账户已买入。

## 已核实的代码缺口及修复

1. 通知在正式确认时入队，但 PaperTradingService._skip_reason 原来直接拒绝没有 entry_price 的信号，理由“下一分钟成交价缺失”。现在允许正式确认进入执行报价检查，必须等确认之后的新报价；保持行情日期、新鲜度、涨跌停、滑点、仓位、资金、T+1等原有检查，并检查执行截止时间。不按确认时点价格倒填成交。
2. 默认 systemd 行情组原来只有采集、归档、策略，没有模拟执行服务。安装器增加显式 --paper-execution，可安装 a-stock-execution.service 并加入目标组，命令为 run_realtime_execution.py --paper --monitor。不会仅凭部署Web就自动启用账户变更。
3. 执行消费者发布 realtime:execution:health，确认信号的前置拒绝原因写入 paper_order_rejected，便于后续逐股对账。账户或成交门禁仍可能正常拒单，修复不保证五只都应买入。

## 盘后调度

默认上海时间21:30采集；策略任务21:45开始检查，但只有当日 automation_daily 状态 done/pipeline_ok 且采集分区ready时才计算因子、选股。失败等待重试，不使用旧分区冒充新一轮采集完成。

已有服务器环境变量优先于默认值，需将 .env 中 AUTOMATION_DAILY_TIME 设为21:30、AUTOMATION_STRATEGY_TIME 设为21:45，再重启Web调度进程。采集和计算任务仍分别运行。

## 服务器一次性启用

由root执行，自动读取现有Web工作目录和运行账号，不新增sudo权限：

```bash
APP=$(systemctl show a-stock -p WorkingDirectory --value)
RUN_USER=$(systemctl show a-stock -p User --value)
cd "$APP"
.venv/bin/python -X utf8 scripts/install_market_systemd.py --project-root "$APP" --user "$RUN_USER" --notifications --paper-execution --install
systemctl restart a-stock-market.target
systemctl status a-stock-execution --no-pager -l
```

这会启用模拟账户消费及原有持仓监控，可能在之后的有效信号到达时修改模拟账户；不得为补买今天已过期信号而关闭门禁。

要确认当天最终未买入的唯一原因，还需要执行服务状态/日志，以及账户数据库中的 paper_order_rejected、订单和成交记录。本次修复及本地测试不等于服务器已经安装新服务。
