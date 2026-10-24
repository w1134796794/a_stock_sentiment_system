# eltdx 第五至七阶段：研究数据、证据页面与离线对照

本功能承接 [第一至四阶段](20261024-eltdx-evidence.md)。所有新增数据仍为观察证据，不覆盖 Tushare 正式行情、不替换同花顺正式板块确认，不改变买点、仓位、通知或模拟账户。

## 第五阶段：低频研究资料

支持接口：

| kind | 资料 | 时间及口径 |
| --- | --- | --- |
| `bars_raw` | 不复权 K 线 | 与其他来源核对时只比较相同周期、不复权价格 |
| `bars_qfq` / `bars_hfq` | 服务端前/后复权 K 线 | 单独版本保存，不与原始价格混算 |
| `corporate_actions` | 权息/股本事件 | 记录事件日及抓取时间，不能据事件日推断原公告可见时间 |
| `adjustment_factors` | 本地仿射复权系数 | 显式设置起点和锚点；不宣称与服务端价格完全一致 |
| `money_flow` | 主站保留的日资金流 | 元；保留通达信分类，不能与 Tushare 净流入字段直接拼接 |
| `company_profile` | 公司资料 | 保存各表业务字段，未验证发布时间 |
| `announcements` | 公告资料 | 原字段保留；不猜测历史可见性 |
| `finance_report` | 财报资料 | 报告期不等于发布日期 |
| `topics` | 题材资料 | 当前抓取版本，不能回填为历史题材归属 |
| `security_status` | 当前证券/ST/停牌核验 | 无记录为未知，不推断退市 |

### 手工预演、执行与续跑

```bash
.venv/bin/python scripts/prefetch_eltdx_research.py \
  --start 20260901 --end 20261023 --codes 000001,600000 \
  --kinds bars_raw,corporate_actions,money_flow,company_profile,announcements,finance_report,topics
```

默认预演不联网、不创建数据库。增加 `--apply` 才抓取。可用 `--collection-date 20261023` 替代 `--codes`，严格读取该日已有的冻结名单，不临时扩大股票池。单次最多 100 只股票，顺序执行。

`--period` 支持 `day/week/month/1m/5m/15m/30m/60m`，`--max-pages` 默认 20，上限 100。范围请求不能保证数据源保留的历史覆盖完整，覆盖状态单独记录；分页预算耗尽不会被当作成功。

数据库为 `WEB_DATA_DIR/market_archive/research/research.sqlite3`。按代码、接口、请求范围、周期和复权模式保存不可变版本，版本 ID 为内容哈希。重复执行跳过成功请求；空结果和失败结果会重试，`--refresh` 可显式保留新版本。每个任务的状态与各项结果单独保存。

所有版本记录 `captured_at`、SDK 版本、口径、质量与覆盖状态。`as_of` 读取按真正抓取时间截断；资料不具备已验证发布时间时保持 `historical_eligible=false`。当前数据不能伪装成过去已经可见的数据。

### 可选盘后自动抓取

默认关闭。在现有盘后自动流水线运行环境中设置：

```dotenv
ELTDX_RESEARCH_ENABLED=true
```

仅当前日期 16:00 之后，在原盘后流水线结束后启动独立研究子进程；使用当日已冻结名单，抓取最近 40 个自然日的日线/权息/资金流及当前 F10 资料。30 分钟超时，任务异常或部分失败单独报告，不改变原生产流水线成功口径。

不会为历史日期重放自动任务去抓“当时”的 F10。未启用原自动流水线或没有当日冻结名单时，不会凭页面访问启动抓取。管理员可在证据页查看研究任务状态。

### 不复权价格核验

```bash
.venv/bin/python scripts/audit_eltdx_research.py \
  --code 000001 --reference /path/to/tushare_daily.csv --reference-adjust none
```

CSV 需要 `code` 或 `ts_code`、`date` 或 `trade_date`、`open/high/low/close`。只比较相同股票、相同日线时点的不复权 OHLC，不比较口径可能不同的成交额。不改任何源文件。

差异超过 0.011 元会列出字段，并注明该日是否存在已保存的权息事件；无重合日期或存在差异时返回非零退出码。`--as-of` 限定可用研究抓取版本。此核验不自动选择“正确来源”。

### 板块版本

继续使用现有盘前命令的 `--tdx-boards` / `--board-mappings`，新建映射版本增加 THS/TDX 完整成员及成员哈希。旧版本保持原样可读，不用今日成员覆盖历史成员。

页面展示固定名单分母、实际观测覆盖率、上涨比例、映射状态及独立的通达信报价。通达信影子指数与 THS 成分股宽度可能不是同步观测，不能据此替代正式板块共振条件。

## 第六阶段：证据页面和权限

在“盘中转强”页顶部、个股卡片和诊断回放页进入 **行情证据**（`/market-evidence`）。

普通用户沿用现有 `intraday` 权限，查看：

- 当日名单归属、证据质量及新鲜度。
- 竞价过程曲线、正式撮合摘要、本批新增成交摘要。
- 盘口价差、深度、失衡和五档价量。
- 公司资料摘录、研究版本及板块影子对照。

本批新增成交不等于全天成交或固定窗口成交。缺数据不显示为有效的零值，过期数据明确标记。页面只读取已有数据，不实例化网络行情客户端。

管理员额外查看采集状态、归档积压、研究任务、原始分页和离线对照报告。原始接口仅管理员可访问，分页上限 200。接口包括：

| 路径 | 权限 |
| --- | --- |
| `GET /api/market-evidence/stock/{code}?market_date=YYYYMMDD` | 普通证据摘要 |
| `GET /api/market-evidence/boards?market_date=YYYYMMDD` | 板块影子摘要 |
| `GET /api/admin/market-evidence/diagnostics?market_date=YYYYMMDD` | 管理员 |
| `GET /api/admin/market-evidence/raw/{code}/{kind}` | 管理员，实时原始分页 |
| `GET /api/admin/market-evidence/research/{code}/{kind}` | 管理员，研究原始分页，支持 `as_of` |
| `GET /api/admin/market-evidence/experiments` | 管理员，已保存对照报告摘要 |

现有归档快照回放页面增加“新增证据对照”，使用原后台回放任务的已保存输入，不引入页面在线补数。所有来自资料及接口的文本均以转义/文本方式展示。

## 第七阶段：离线 A/B/C 对照

- **A**：原规则回放的确认。
- **B**：只观察新增证据，与 A 的确认保持一致。
- **C**：仅在原确认转换时点评估额外过滤，不运行新的生产策略。

策略文件必须显式提供过滤条件。下面的示例只检查正式撮合额为正，供检查工具链使用，不是生产门槛建议：

```json
{
  "schema_version": 1,
  "filters": {"min_opening_amount_yuan": 1},
  "max_age_seconds": 15,
  "depth_visibility": "published_only"
}
```

支持 `max_spread_bps`、`min_bid_depth_hand`、`min_buy_amount_share`、`min_opening_amount_yuan`。买方向占比使用已验证成交记录的 300 秒来源分钟窗口，窗口边界精度为分钟，不代表真实资金身份。

必需证据缺失、过期、不完整或未验证发布时为 `unknown`，不会当作通过。`depth_visibility` 默认 `published_only`；显式使用 `snapshot_receipt` 才允许主快照内盘口按接收时间作研究对照，报告会保留 `snapshot_receipt_only`，仍不证明当时 Redis 已完成发布。

可选 `calibration_end` 必须早于全部评估日，但仅凭声明不证明真正样本外验证，报告保持 `out_of_sample_verified=false`。

### 生成与复跑

```bash
.venv/bin/python scripts/evaluate_eltdx_evidence.py \
  --dates 20261022,20261023 --policy /path/to/policy.json \
  --profile decision_pool --until 10:00:00
```

默认写入 `WEB_DATA_DIR/experiments/eltdx_evidence/`，可通过 `--output` 指定路径。每天单独冻结为旁边 `.inputs/<hash>.json`，主报告保存清单和哈希；逐日处理，避免同时持有多日原始行情。记录策略哈希、实现内容哈希、Git 提交及工作区状态。

```bash
.venv/bin/python scripts/evaluate_eltdx_evidence.py \
  --saved-inputs /path/to/report.json --policy /path/to/policy.json
```

复跑读取原清单引用的冻结数据并检查哈希，不加载新行情。移动报告时必须一并移动其 `.inputs` 目录。

报告包含确认数、C 保留/拒绝/未知数量、确认延迟及逐条门禁。10 日目标只统计基线完整、存在确认且 C 可判定的样本日；仍不构成全市场覆盖、样本外优势或收益有效证明。

### 可选导入执行回测结果

只有导入 `--outcomes PATH` 才计算逐笔回测统计，输入格式：

```json
{
  "schema_version": 1,
  "frozen_inputs_hash": "与报告完全相同",
  "policy_hash": "与报告完全相同",
  "source_run_id": "已有执行回测的运行标识",
  "round_trip_cost_bps": 10,
  "rows": [
    {
      "market_date": "20261023",
      "code": "000001",
      "group": "A",
      "confirmed_at": "与报告该组确认时点完全相同",
      "filled": false
    }
  ]
}
```

`group` 为 A 或 C；B 复用 A。已成交记录还需要 `entry_at/exit_at/entry_price/exit_price`，成交时间必须晚于确认。延迟后的 C 确认不能复用 A 的成交结果。统一成本下计算成交率、已成交单平均净收益和胜率；缺失匹配结果会明确计数，不当作零收益。

不会从逐笔均值伪造组合收益、最大回撤或假突破率；没有组合净值及明确定义的标签时这些指标保持未测量。导入结果也不证明真实排队成交或实盘执行。

## 上线顺序与当前限制

1. 在功能分支完成测试和审阅；本次未提交、未推送。
2. 原三个实时证据开关及 `ELTDX_RESEARCH_ENABLED` 默认关闭。
3. 先在有 Redis 和通达信网络访问的节点验证覆盖、耗时、磁盘与归档积压，再逐项开启观察采集。
4. 盘前映射仍需真实交易日历及冻结名单。真实接口历史保留范围必须实测。
5. 生成真实观察报告并检查缺口，再单独讨论是否修改正式门禁；本实现没有开启正式门禁的入口。

当前云环境未配置通达信 TCP 目标，真实行情与 F10 远端响应未验证。F10 默认使用 SDK 的 HTTP 7615 入口，需按实际目标配置平台访问策略；保持代理及 TLS 验证，不以页面可打开作为采集已部署的证据。
