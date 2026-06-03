# A股情绪周期与板块轮动量�化系统 V2

基于情绪周期识别、板块轮动追踪、概念-行业交叉验证的短线交易辅助系统。

## 🎯 核心架构：五层复盘流水线

```
Layer 1: 看大盘（定仓位）   →  MarketEnvAnalyzer
Layer 2: 看板块（定方向）   →  SectorAnalysisOrchestrator
Layer 3: 看个股（定标的）   →  StockSelectionLayer
Layer 4: 定计划（定执行）   →  TradePlanLayer
Layer 5: 盘后总结          →  ReviewAnalyzer
```

### 1. 数据层 (Data Layer)
- **双源备份**: Tushare(历史数据) + AkShare(实时情绪)
- **本地缓存**: 自动避免重复API调用，支持盘中/盘后增量更新
- **智能交易日判断**: 非交易日自动关联最近交易日数据
- **同花顺板块数据**: 集成同花顺概念/行业/特色指数数据(ths_index/ths_daily/ths_member)
- **模块化数据管理**: 按市场/股票/板块/概念/资金流分模块，便于维护和扩展

### 2. 情绪周期引擎 (Emotion Cycle Engine)
- **五阶段模型**: 冰点期 → 回暖期 → 上升期 → 高潮期 → 退潮期
- **多维度评分**: 涨停家数、跌停家数、炸板率、昨日涨停溢价率
- **动态策略**: 根据情绪周期自动生成仓位建议和禁忌操作
- **周期切换预警**: 识别情绪拐点，提前调整策略
- **ML增强**: 机器学习辅助情绪周期分类

### 3. 板块轮动追踪器 (Sector Rotation Tracker)
- **多因子动态权重模型**:
  - 强度因子(30%): 涨停家数、连板数
  - 资金因子(25%): 成交额变化率、换手率
  - 趋势因子(25%): 排名动量、涨停趋势、持续性评分
  - 市场适配(20%): 根据周期动态调整
- **板块生命周期**: 萌芽期 → 加速期 → 高潮期 → 衰退期
- **轮动图谱**: 构建资金迁移路径，预判接力板块
- **T+1板块预判**: 基于历史板块轮动规律，预判次日可能接力的板块

### 4. 概念-行业交叉验证 (Concept-Industry Validation)
- **双轨制分析**: 概念维度(短线热点) + 行业维度(中线趋势)
- **共振识别**: 概念热 + 行业热 = 强信号，重仓参与
- **背离预警**: 概念热 + 行业冷 = 纯炒作，谨慎参与
- **行业集中度**: 分析概念成分股的行业分布，识别真正龙头
- **反向板块查询**: 通过股票代码直接查询所属所有板块，区分行业(I)与概念(N)

### 5. 模式识别 (Pattern Recognition) — 四层优先级过滤管线

所有策略统一采用 **L0 → L1 → L2 → L3 → L4** 四层管线架构：

| 层级 | 名称 | 职责 | 成本 |
|------|------|------|------|
| L0 | 硬性排除 | 一字板、尾盘板、流通市值、首/连板判定 | 极低 |
| L1 | 前身验证 | 龙头身份、回调质量、调整期形态 | 中 |
| L2 | 技术确认 | 量能、封单、高开gap、资金态度 | 中高 |
| L3 | 质量指标 | 涨停时间、开板次数、市值上限 | 低 |
| L4 | 评分生成 | 板块地位、置信度计算、信号输出 | — |

L2/L3 层采用**弹性评分 + 累计扣分**机制，替代硬过滤，提高容错性。

**信号优先级**: 弱转强(100) > 二板定龙(85) > 龙二波(70) > 首板突破(50)

#### 5.1 首板突破 (First Board Breakout)
按突破→时间→量能→筹码→市值→板块效应的顺序进行四层过滤。
- **买点**: 涨停价（打板买入）
- **止损**: 涨停价下方7%
- **止盈**: 涨停价上方10%
- **仓位**: light

#### 5.2 二板定龙 (Second Board Dragon)
二连板确认龙头地位，按首板质量→资金确认→质量指标→板块地位四层过滤。
严格模式加强首板硬逻辑、动态gap阈值和封板速度检查。
- **买点**: 二板涨停价
- **止损**: 二板涨停价下方7%
- **止盈**: 二板涨停价上方15%
- **仓位**: medium（龙头）/ light（跟风）

#### 5.3 弱转强 (Weak to Strong)
昨日弱势（烂板/炸板/尾盘板）→ 今日超预期强势。含完整龙头池管理。
- **龙头候选池**: 自动识别趋势龙头/连板龙头/空间龙头入池
- **龙头走弱池**: 监控烂板、断板、尾盘板、趋势回调
- **转强监控**: 动态gap阈值、竞价量弹性评分、高开低走回退检测
- **买点**: 今日涨停价（打板确认）
- **止损**: 今日涨停价下方5%
- **止盈**: 今日涨停价上方10-15%
- **仓位**: medium

#### 5.4 龙二波 (Dragon Second Wave)
龙头充分调整后开启第二波，按双轨制第一波判断（连板/涨幅/涨停次数）。
- **动态阈值**: 量能/封单阈值根据连板高度、调整天数、板块热度自适应
- **衰减记忆**: 调整天数越短 → 阈值越宽松
- **买点**: 转强当日涨停价
- **止损**: 涨停价下方7%
- **止盈**: 前高附近或涨幅15-20%
- **仓位**: medium

### 6. 快照系统 (Snapshot) — P0
收盘跑批时，喂给 Excel 的 `data_dict` 同步落结构化产物：
- `webdata/snapshots/{date}.json`：整页 JSON 快照（前端直读）
- `webdata/app.sqlite`：结构化索引（每日快照 / 交易计划 / 信号）
- **零侵入设计**: 在报表生成处旁挂写入，失败不影响 Excel 产出

### 7. Web 看板 (Web Dashboard) — P1
基于 FastAPI + Jinja2 的只读看板，浏览每日快照：
- 市场情绪概览 + 仓位建议
- 18-section 结构化快照浏览
- AI 解读（可选，依赖大模型 API）
- 启动: `python run_web.py`

### 8. 知识库层 (Knowledge Base) — P2-P3
把历史每日快照沉淀为可检索、可问答的记忆：
- `store.py` — SQLite 块存储（文本 + 可选向量 BLOB）
- `chunker.py` — 快照 → 文本块
- `embeddings.py` — 可选云嵌入（缺省降级为零依赖中文词法检索）
- `tools.py` — 定量只读查询（基于 app.sqlite），供 LLM 调用
- `retriever.py` — 元数据过滤 + 向量/词法混合检索
- `brief.py` — 每日 AI 解读（结构化 → 叙事）
- `winrate.py` — 周期×模式胜率矩阵

### 9. 风控回测 (Risk & Backtest)
- **完整回测框架**: 逐日分发→标准化→估值→撮合→模拟→评测
- **风控模块**: 仓位管理、凯利公式、回撤熔断、风险审查
- **Point-in-Time 数据**: 消除前视偏差
- **Walk-Forward**: 滚动窗口训练/测试

### 10. 多因子评分 (Multi-Factor Scoring)
- 板块内个股排名评分
- 龙头/跟风地位判断
- 板块席位分配

## 🛠 安装与配置

### 步骤1: 安装依赖
```bash
cd a_stock_sentiment_system
pip install -r requirements.txt
```

Web 看板额外依赖:
```bash
pip install fastapi uvicorn jinja2 python-multipart
```

### 步骤2: 配置API Token
编辑 `config/settings.py`:
```python
TUSHARE_TOKEN = "你的tushare_token_here"
```

### 步骤3: 配置大模型（可选）
在项目根目录创建 `.env`:
```bash
# 阿里云通义千问（推荐，支持嵌入+对话）
DASHSCOPE_API_KEY = sk-xxxx

# 或者 DeepSeek
DEEPSEEK_API_KEY = sk-xxxx
```

## 🚀 常用命令

### 日常运行

```bash
# 当日收盘分析（默认今天）
python main.py

# 指定日期分析
python main.py --date 20260528

# 定时任务调度（每日15:40自动执行）
python scheduler.py
```

### Web 看板

```bash
# 启动 Web 看板（默认 http://127.0.0.1:8000）
python run_web.py

# 自定义端口
python run_web.py --port 9000

# 开发模式（热重载）
python run_web.py --reload
```

### 回测

```bash
# 运行策略回测
python run_backtest.py
```

### 知识库

```bash
# 构建胜率矩阵
python scripts/build_winrate.py

# 灌入历史快照到知识库
python -c "from kb.ingest import ingest_all; ingest_all()"
```

### 报告与发布

```bash
# 生成最终报告
python scripts/generate_final_report.py

# 发布到微信公众号
python scripts/publish_to_wechat.py --preview
python scripts/publish_to_wechat.py --publish
python scripts/publish_to_wechat.py --date 20260528 --preview

# 重建快照 section
python scripts/rebuild_snapshot_sections.py
```

### 工具与检查

```bash
# 查看回测交易记录
python check_trades.py

# 因子结果回填
python scripts/backfill_factor_results.py

# 更新交易日历
python scripts/update_trade_calendar.py
```

### 模块单独测试

```bash
# 情绪周期引擎
python core/analysis/emotion_cycle_engine.py

# 板块轮动追踪器
python core/analysis/sector_analysis_orchestrator.py

# 概念-行业交叉验证
python core/analysis/concept_industry_validator.py

# LHB（龙虎榜）分析
python core/analysis/lhb_analyzer.py

# 筹码结构分析
python core/analysis/chip_structure_analyzer.py
```

### 代码检查

```bash
# 语法编译检查
python -m py_compile core/pattern/dragon_second_wave.py
python -m py_compile core/pattern/weak_to_strong.py
python -m py_compile core/pattern/second_board_dragon.py
python -m py_compile core/pattern/pattern_recognition.py

# Ruff Lint（需安装 ruff）
ruff check core main.py

# Ruff 格式化
ruff format core main.py
```

## 📊 输出文件

| 文件 | 说明 |
|------|------|
| `output/短线情绪分析报告_YYYYMMDD_HHMMSS.xlsx` | 每日情绪分析报告（多Sheet） |
| `output/交易计划_YYYYMMDD.xlsx` | 次日可执行的交易计划 |
| `output/散户决策报告_YYYYMMDD.txt` | 隔夜预判、三阶过滤、剧本推演 |
| `webdata/snapshots/YYYYMMDD.json` | 结构化快照（Web看板消费） |
| `webdata/app.sqlite` | 结构化索引DB |
| `webdata/kb.sqlite` | 知识库块存储 |
| `dragon_pools.json` | 龙头池状态持久化 |

## 🧠 交易逻辑整合

### 决策流程
1. **看情绪**: 当前处于什么周期？冰点/回暖/上升/高潮/退潮？
2. **看板块**: 哪些概念板块评分高？信号类型是共振还是背离？
3. **看验证**: 概念热度是否有行业支撑？集中度如何？
4. **盯模式**: 在这些板块中，谁是"首板突破"？谁是"弱转强"？
5. **定计划**: 根据情绪周期确定仓位，根据信号优先级确定标的

### 仓位管理
| 情绪周期 | 建议仓位 | 禁忌操作 |
|---------|---------|---------|
| 冰点期 | 0-20% | 追高、重仓、格局、补仓 |
| 回暖期 | 20-40% | 追高、重仓、格局 |
| 上升期 | 40-70% | 无（积极做多） |
| 高潮期 | 30-50% | 追高、重仓、格局 |
| 退潮期 | 0-20% | 追高、打板、格局、补仓 |

## ⚠️ 免责声明

本系统仅供学习研究使用，不构成投资建议。股市有风险，投资需谨慎。

## 📁 项目结构

```
a_stock_sentiment_system/
├── config/                          # 配置层
│   ├── settings.py                  # 主配置（Tushare Token、路径、大模型API）
│   ├── emotion_cycle_config.yaml    # 情绪周期参数
│   ├── sector_tracker_config.yaml   # 板块追踪参数
│   ├── risk_control.yaml            # 风控参数
│   └── factors/                     # 因子配置
├── core/
│   ├── analysis/                    # 分析引擎
│   │   ├── emotion_cycle_engine.py   # 情绪周期识别引擎
│   │   ├── emotion_cycle_ml.py       # ML增强情绪分类
│   │   ├── sector_analysis_orchestrator.py  # 板块轮动编排器
│   │   ├── concept_industry_validator.py    # 概念-行业交叉验证
│   │   ├── lhb_analyzer.py           # 龙虎榜分析
│   │   ├── chip_structure_analyzer.py # 筹码结构分析
│   │   ├── moneyflow_analyzer.py     # 资金流分析
│   │   └── ...
│   ├── data/                        # 数据层
│   │   ├── data_manager_main.py      # DataManager 入口（多继承集成）
│   │   ├── data_manager_stock.py     # 个股数据（日线/分时/竞价）
│   │   ├── data_manager_market.py    # 市场数据（daily_basic/指数）
│   │   ├── data_manager_sector.py    # 板块数据（同花顺ths_index/ths_member）
│   │   ├── data_manager_moneyflow.py # 资金流数据
│   │   └── industry_mapper.py        # 行业映射（东财+同花顺）
│   ├── pipeline/                    # 五层流水线
│   │   ├── review_pipeline.py        # 流水线编排器
│   │   ├── layer1_market_env.py      # 看大盘·定仓位
│   │   ├── layer2_sector_analysis.py # 看板块·定方向
│   │   ├── layer3_stock_selection.py # 看个股·定标的
│   │   ├── layer4_trade_plan.py      # 定计划·定执行
│   │   └── layer5_review.py          # 盘后总结
│   ├── pattern/                     # 模式识别策略
│   │   ├── pattern_recognition.py    # 聚合策略调度
│   │   ├── first_board_breakout.py   # 首板突破
│   │   ├── second_board_dragon.py    # 二板定龙
│   │   ├── weak_to_strong.py         # 弱转强（含龙头池管理）
│   │   ├── dragon_second_wave.py     # 龙二波
│   │   ├── signal_priority.py        # 信号优先级与互斥
│   │   └── base.py                   # 统一契约与注册中心
│   ├── execution/                   # 执行层
│   │   └── retail_trader_support_v2.py  # 散户决策支持V2
│   ├── report/                      # 报告层
│   │   └── report_generator_v2.py   # 报告生成器V2
│   ├── publish/                     # 发布层
│   │   ├── wechat_publisher.py      # 微信公众号发布
│   │   └── report_formatter.py      # 报告格式化（HTML）
│   ├── stock_ranking/               # 多因子排序
│   │   ├── multi_factor_scorer.py   # 多因子评分
│   │   └── sector_position.py       # 板块席位
│   └── utils/                       # 工具类
├── backtest/                        # 回测框架
│   ├── backtest_engine.py            # 回测引擎
│   ├── replay_engine.py              # 行情回放引擎
│   ├── performance_analyzer.py       # 绩效分析
│   └── walk_forward.py               # 滚动窗口优化
├── risk/                            # 风控模块
│   ├── risk_manager.py              # 风控管理器
│   ├── position_sizer.py            # 仓位计算
│   ├── kelly_sizer.py               # 凯利公式
│   └── circuit_breaker.py           # 回撤熔断
├── snapshot/                        # 快照层 (P0)
│   ├── writer.py                     # 快照写入
│   ├── reader.py                     # 快照读取
│   └── serialize.py                  # 序列化工具
├── kb/                              # 知识库层 (P2-P3)
│   ├── store.py                      # SQLite块存储
│   ├── chunker.py                    # 文本分块
│   ├── embeddings.py                 # 向量嵌入
│   ├── retriever.py                  # 混合检索
│   ├── ingest.py                     # 快照灌库
│   ├── llm_client.py                 # LLM客户端
│   ├── brief.py                      # AI每日解读
│   └── tools.py                      # 定量查询工具
├── web/                             # Web看板 (P1)
│   ├── app.py                        # FastAPI应用
│   ├── templates/                    # Jinja2模板
│   └── static/                       # 静态资源
├── scripts/                         # 辅助脚本
│   ├── build_winrate.py             # 构建胜率矩阵
│   ├── generate_final_report.py     # 生成最终报告
│   └── publish_to_wechat.py         # 微信公众号发布
├── webdata/                         # Web/KB数据存储
│   ├── snapshots/                    # 每日JSON快照
│   ├── app.sqlite                    # 结构化索引
│   └── kb.sqlite                     # 知识库
├── main.py                          # 主程序入口
├── run_web.py                       # Web服务入口
├── run_backtest.py                  # 回测入口
├── scheduler.py                     # 定时任务调度
└── requirements.txt                 # 依赖列表
```

## 📈 数据流程

```
1. 数据采集 (Tushare/AkShare)
   ↓
2. 情绪周期分析 (Layer 1)
   ↓
3. 板块轮动分析 (Layer 2)
   ↓
4. 个股筛选 + 模式识别 (Layer 3)
   ↓
5. 交易计划生成 (Layer 4)
   ↓
6. 盘后总结 (Layer 5)
   ↓
7. 报告生成 (Excel + 快照JSON + Web看板)
   ↓
8. [可选] 知识库灌入 + AI解读
```
