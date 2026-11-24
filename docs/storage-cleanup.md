# 磁盘压力触发清理

`scripts/maintain_storage.py` 默认只预览，`--apply` 才执行。
默认根分区使用率达到70%，或可用空间低于4GiB时触发；每30分钟检查一次。
健康时不清理、不停服务。Docker、行情归档、因子库、模型、账户成交不在删除范围内。
70%是触发阈值，不是必须达到的清理目标。允许清理的数据不足时，记录剩余压力并等待后续检查，
不扩大范围、不削减保留天数，也不为了达到70%反复停服务。

在线阶段只清理已知命名的清理备份及debug文件（保留7天、使用lsof保护文件组），
apt下载缓存和旧journal（轮转后限制归档到300M）。活动journal可能使总量高于300M。
未安装lsof、路径异常或无权限时停止，不猜测文件是否安全。

仍有压力时，数据库维护仅在上海时间23:00至06:00开始，每个自然日最多尝试一次。
会暂停a-stock Web及行情进程组，网站可能短暂502，然后自动恢复原先运行的服务。
root仅负责编排；SQLite操作通过runuser降权到deploy，避免生成root所属WAL。
删除7天前runtime_observations及trading_events中kind=runtime_cycle的记录；
不删除其他信号、通知、持仓或成交。分批永久提交，无自动备份；不支持恢复已删诊断。
外键、触发器、文件占用及空间检查失败时停止。压缩需额外空闲空间大于当前库两倍加512MiB；
空间不足时不停止服务、不硬做压缩，日志要求人工处理。空闲页不足64MiB时不反复VACUUM。
子进程最长运行15分钟，正常异常/超时均尝试恢复服务；主机掉电或维护父进程被SIGKILL不保证恢复，
需检查a-stock服务。不要在维护窗口安排训练、补数或历史重算。

安装（先部署代码，服务器root执行）：

```bash
cd /opt/a_stock_sentiment_system
.venv/bin/python -X utf8 scripts/install_storage_cleanup_systemd.py --install
systemctl start a-stock-storage-cleanup.timer
systemctl list-timers a-stock-storage-cleanup.timer --no-pager
```

安装启用开机启动，但不立即运行清理。首次start timer如存在错过的日历触发点，可能立即补跑一次检查。
修改阈值、保留天数可编辑该service的ExecStart参数并daemon-reload；不需要修改业务.env。

```bash
.venv/bin/python -X utf8 scripts/maintain_storage.py
journalctl -u a-stock-storage-cleanup -n 80 --no-pager
systemctl disable --now a-stock-storage-cleanup.timer
```

预览不写磁盘锁、状态或业务库。数据库维护尝试日期保存在
WEB_DATA_DIR/storage-cleanup-last-attempt.txt，任务互斥锁为storage-cleanup.lock。
不自动清理训练标签、分钟缓存、历史选股、回放任务，也不自动补数据或发送交易通知。
