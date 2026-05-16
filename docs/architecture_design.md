# A股短线情绪量化系统 — 架构设计文档

> 版本：v2.0  
> 日期：2026-05-16  
> 定位：面向短线选手的复盘→选股→计划→执行全流程量化系统

---

## 一、当前架构总览

### 1.1 目录结构

```
a_stock_sentiment_system/
├── main.py                          # 主入口：SentimentSystem 类，编排全流程
├── scheduler.py                     # 定时调度：每日 15:40 自动执行
├── run_backtest.py                  # 回测入口
│
├── config/                          # 【配置层】
│   ├── settings.py                  # 全局参数（阈值、权重、API配置）
│   ├── config_loader.py             # YAML 配置加载器
│   ├── emotion_cycle_config.yaml    # 情绪周期专用配置
│   └── sector_tracker_config.yaml   # 板块追踪专用配置
│
├── core/                            # 【核心层】
│   ├── data/                        # 数据子层
│   │   ├── data_manager.py          # 数据中枢（Tushare + AkShare 双源，多级缓存）
│   │   ├── data_manager_extensions.py # 扩展接口（北向资金、龙虎榜）
│   │   └── industry_mapper.py       # 三级行业映射（L1/L2/L3）
│   │
│   ├── analysis/                    # 分析子层
│   │   ├── emotion_cycle_engine.py          # 情绪周期引擎（规则引擎）
│   │   ├── emotion_cycle_integrated.py      # 综合情绪引擎（规则 + ML 双轨）
│   │   ├── emotion_cycle_ml.py              # ML 情绪模型
│   │   ├── pattern_recognition.py           # 模式识别统一入口（调度器）
│   │   ├── sector_analysis_orchestrator.py  # 板块分析统筹入口（带缓存）
│   │   ├── ths_sector_tracker.py            # 同花顺板块追踪（热点识别+持续性+共振）
│   │   ├── sector_hotspot_detector.py       # 板块热点检测
│   │   ├── sector_persistence_analyzer.py   # 板块持续性分析（10天出现次数）
│   │   ├── concept_board_hierarchy.py       # 概念连板梯队分析
│   │   ├── concept_industry_validator.py    # 概念-行业交叉验证
│   │   ├── moneyflow_analyzer.py            # 资金流向分析（主力/散户/北向）
│   │   ├── chip_structure_analyzer.py       # 筹码结构分析（获利盘/集中度）
│   │   └── market_indicators.py             # 通用市场指标计算
│   │
│   ├── pattern/                     # 模式策略子层
│   │   ├── weak_to_strong.py               # 弱转强（龙头池→走弱池→转强信号）
│   │   ├── second_board_dragon.py          # 二板定龙（首板质量+次日确认）
│   │   ├── first_board_breakout.py         # 热点首板突破
│   │   ├── dragon_second_wave.py           # 龙二波（调整后第二波）
│   │   ├── dragon_dynamic_manager.py       # 龙头动态管理（池子数据结构）
│   │   ├── blast_reseal.py                 # 炸板回封（未启用）
│   │   ├── position_battle.py              # 卡位板（未启用）
│   │   └── divergence_to_consensus.py      # 分歧转一致（未启用）
│   │
│   ├── execution/                   # 执行子层
│   │   ├── execution_engine.py             # 交易执行引擎（计划生成+信号推送）
│   │   └── retail_trader_support_v2.py     # 散户决策支持（隔夜预判+剧本推演）
│   │
│   ├── report/                      # 报告子层
│   │   ├── report_generator.py             # 基础报告生成
│   │   └── report_generator_v2.py          # V2 报告生成（Excel 多 Sheet）
│   │
│   ├── publish/                     # 发布子层
│   │   ├── wechat_publisher.py             # 微信公众号发布
│   │   ├── llm_report_generator.py         # LLM 自然语言报告
│   │   └── report_formatter.py             # 报告格式化
│   │
│   └── utils/                       # 工具子层
│       ├── date_utils.py                   # 交易日历（基于 trade_calendar.csv）
│       ├── time_utils.py                   # 时间处理（交易时段判断）
│       ├── stock_code_utils.py             # 股票代码标准化
│       ├── calculation_utils.py            # 通用计算函数
│       ├── validation_utils.py             # 数据校验
│       └── data_updater.py                 # 数据更新器
│
├── backtest/                        # 【回测层】
│   ├── backtest_engine.py           # 回测引擎
│   ├── performance_analyzer.py      # 绩效分析（夏普/最大回撤/胜率）
│   └── trade_simulator.py           # 交易模拟器（滑点/手续费）
│
├── risk/                            # 【风控层】
│   ├── risk_manager.py              # 风险管理器（仓位/止损/集中度）
│   ├── risk_analyzer.py             # 风险分析器
│   └── position_sizer.py            # 仓位计算器
│
├── scripts/                         # 【脚本层】
│   ├── advanced_stock_screener.py   # 高级选股器
│   ├── analyze_market_mainline.py   # 市场主线分析
│   ├── generate_final_report.py     # 最终报告生成
│   ├── publish_to_wechat.py         # 微信发布脚本
│   └── update_trade_calendar.py     # 交易日历更新
│
└── examples/                        # 【示例】
    ├── emotion_cycle_example.py
    ├── demo_usage.py
    └── execution_usage.py
```

### 1.2 当前执行流程（main.py → run_daily_analysis）

```
                        ┌──────────────────────┐
                        │   scheduler.py        │
                        │   每日 15:40 触发      │
                        └──────────┬───────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      SentimentSystem.run_daily_analysis(date)         │
│                                                                      │
│  Step 1  数据获取                                                     │
│          ├── dm.get_limit_up_pool(date)           → zt_pool          │
│          ├── dm.get_limit_down_pool(date)         → limit_down_df    │
│          └── dm.get_limit_up_pool(T-2)            → prev_limit_up_df │
│                                                                      │
│  Step 2  行业层级映射                                                 │
│          └── mapper.build_hierarchy_dataframe()   → hierarchy_df     │
│                                                                      │
│  Step 3  情绪周期分析（规则 + ML 双引擎）                              │
│          ├── emotion_engine.analyze_market_data() → emotion_result   │
│          └── integrated_engine.detect_cycle()     → integrated       │
│                                                                      │
│  Step 4-8  板块分析（统筹入口，带缓存）                                │
│          ├── 热点概念识别       → hot_concepts_df                    │
│          ├── 热点行业识别       → hot_industries_df                  │
│          ├── 概念持续性分析     → concept_persistence_df             │
│          ├── 行业持续性分析     → industry_persistence_df            │
│          ├── 概念-行业共振分析  → resonance_df (市场主线)             │
│          └── 概念连板梯队分析   → concept_hierarchy                  │
│                                                                      │
│  Step 9   资金流向 + 筹码结构（前10只涨停股）                          │
│          ├── moneyflow_analyzer.analyze_stock_moneyflow()            │
│          └── chip_analyzer.analyze_chip_structure()                  │
│                                                                      │
│  Step 10  北向资金 + 龙虎榜                                           │
│          ├── analyze_hsgt_flow()                                     │
│          └── analyze_top_list_summary()                              │
│                                                                      │
│  Step 11  模式识别                                                     │
│          ├── 弱转强（龙头池→走弱池→转强检测）                          │
│          ├── 二板定龙（首板质量 + 次日确认）                           │
│          ├── 首板突破（热点板块首板）                                  │
│          └── 龙二波（历史连板 + 调整 + 突破）                          │
│                                                                      │
│  Step 12  生成 Excel 分析报告                                         │
│                                                                      │
│  Step 13  生成次日交易计划（共振过滤 → 执行引擎）                      │
│                                                                      │
│  Step 14  生成散户决策支持报告（隔夜预判 + 剧本推演）                  │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.3 核心数据流

```
┌──────────┐    ┌──────────────┐    ┌─────────────────────┐
│ Tushare  │───▶│ DataManager  │───▶│ 涨停池 / 跌停池      │
│ AkShare  │    │ (多级缓存)    │    │ 日线 / 分时 / 竞价   │
└──────────┘    └──────────────┘    │ 板块 / 概念 / 资金   │
                                    └──────────┬──────────┘
                                               │
              ┌────────────────────────────────┼────────────────────────────────┐
              │                                │                                │
              ▼                                ▼                                ▼
   ┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
   │ 情绪周期引擎      │           │ 板块分析统筹      │           │ 模式识别引擎      │
   │                  │           │                  │           │                  │
   │ 规则引擎:        │           │ 热点识别(当日)    │           │ 弱转强           │
   │  涨停家数        │           │ 持续性(10天频次)  │           │ 二板定龙         │
   │  炸板率          │           │ 共振(概念×行业)   │           │ 首板突破         │
   │  溢价率(T+1)     │           │ 连板梯队          │           │ 龙二波           │
   │  胜率/赢面       │           │                  │           │                  │
   │                  │           │                  │           │                  │
   │ ML模型:          │           │ 输出:            │           │ 输出:            │
   │  统计规律判断    │           │  热点板块列表     │           │  模式信号列表     │
   │                  │           │  市场主线         │           │  龙头池/走弱池    │
   └────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
            │                              │                              │
            └──────────────────────────────┼──────────────────────────────┘
                                           │
                                           ▼
                              ┌───────────────────────┐
                              │ 交易计划生成           │
                              │ ├── 共振过滤           │
                              │ ├── 情绪周期调仓       │
                              │ └── 介入时机分配       │
                              ├───────────────────────┤
                              │ 散户决策支持           │
                              │ ├── 隔夜预判           │
                              │ ├── 三阶过滤           │
                              │ └── 剧本推演           │
                              ├───────────────────────┤
                              │ 报告输出               │
                              │ ├── Excel 分析报告     │
                              │ ├── 交易计划 JSON      │
                              │ └── 散户决策 TXT       │
                              └───────────────────────┘
```

---

## 二、对照短线选手复盘逻辑的匹配度分析

### 2.1 标准短线复盘流程

一个成熟的短线选手，每日复盘遵循以下五步：

```
第一步：看大盘（定仓位）
   ├── 指数走势（上证/深证/创业板）
   ├── 量能变化（放量/缩量）
   ├── 涨跌家数比
   └── 结论：今天适合几成仓？

第二步：看板块（定方向）
   ├── 当日最强板块是哪些？
   ├── 板块持续性如何？（过去N天反复活跃）
   ├── 板块梯队是否完整？（龙头→跟风→补涨）
   ├── 板块之间共振还是轮动？
   └── 结论：明天主攻哪个方向？

第三步：看个股（定标的）
   ├── 龙头是谁？（最高板/最强板/最早板）
   ├── 有哪些模式内买点？
   │   ├── 弱转强：昨日烂板/炸板 → 今日高开转强
   │   ├── 二板定龙：首板硬 → 二板确认
   │   ├── 首板突破：热点板块首板
   │   └── 龙二波：龙头调整后第二波
   ├── 个股在板块中的地位？（龙头/跟风/补涨/独狼）
   └── 结论：明天盯哪几只？

第四步：定计划（定执行）
   ├── 竞价条件（高开多少、竞价量、板块确认）
   ├── 买入时机（竞价末段/开盘/回封）
   ├── 仓位分配（核心标的 vs 套利标的）
   ├── 止损止盈（硬止损/移动止盈）
   └── 结论：明天具体怎么操作？

第五步：盘后总结
   ├── 今日操作回顾
   ├── 模式信号表现回顾
   ├── 情绪周期变化趋势
   └── 结论：系统是否需要调参？
```

### 2.2 当前系统匹配度矩阵

| 复盘步骤 | 系统对应模块 | 匹配度 | 说明 |
|----------|-------------|--------|------|
| **第一步：看大盘** | ❌ 缺失 | 0% | 无指数分析、无量能判断、无涨跌家数比 |
| **第二步：看板块** | sector_analysis_orchestrator | 85% | 热点识别✅ 持续性✅ 共振✅ 梯队✅，缺少板块轮动节奏判断 |
| **第三步：看个股** | pattern_recognition | 70% | 四种模式✅，缺少个股板块地位量化、缺少信号优先级 |
| **第四步：定计划** | execution_engine | 60% | 有计划生成✅，但缺少竞价条件、缺少多维度评分排序 |
| **第五步：盘后总结** | ❌ 缺失 | 0% | 无操作回顾、无信号表现统计、无周期变化趋势 |

**综合匹配度：约 55%**

### 2.3 架构层面的具体问题

#### 问题1：main.py 上帝类（God Class）

`SentimentSystem.run_daily_analysis()` 超过 400 行，承担了：
- 数据获取调度
- 分析流程编排
- 报告数据组装
- 交易计划生成
- 散户报告生成
- DEBUG 日志输出

**违反单一职责原则**，任何改动都需要修改 main.py。

#### 问题2：缺少"大盘环境"判断层

短线选手第一步是看大盘——指数涨跌、量能、市场宽度。当前系统直接从涨停池开始分析，缺少对整体市场环境的量化：
- 无上证/深证/创业板指数量化
- 无市场量能判断（放量/缩量）
- 无涨跌家数比（市场宽度）
- 无炸板率与情绪周期的交叉验证

#### 问题3：模式识别与板块分析耦合不清晰

`PatternRecognition` 类同时承担：
- 模式策略调度（弱转强、二板定龙等）
- 龙头池/走弱池管理（通过 weak_to_strong 子模块）
- 历史数据获取

`dragon_dynamic_manager.py` 定义了龙头池数据结构，但未被独立使用，而是嵌入在 weak_to_strong.py 中。

#### 问题4：缺少"个股板块地位"量化

系统识别了热点板块和模式信号，但没有量化每只股票在其板块中的"地位"：
- 空间龙头（最高连板）
- 强度龙头（最早封板/最强封单）
- 中军（大市值涨停）
- 跟风（板块内后排涨停）
- 补涨（板块内首板）

#### 问题5：交易计划生成过于简单

当前逻辑：模式信号 → 共振过滤 → 生成计划。缺少：
- 多维度评分排序（模式质量 × 板块强度 × 个股地位 × 情绪周期）
- 信号优先级与互斥规则（同一只股票触发多个模式时）
- 竞价条件量化（高开幅度、竞价量、竞价图形）

#### 问题6：缺少"盘后总结/复盘回顾"模块

- 无今日操作回顾（如果按系统信号操作会怎样）
- 无模式信号表现统计（各模式胜率、盈亏比）
- 无情绪周期变化趋势图

#### 问题7：回测与主流程割裂

`run_backtest.py` 独立运行，依赖 `trade_plans` 目录下的 JSON 文件，与主分析流程不共享数据，无法做"如果按系统信号操作会怎样"的实时复盘。

#### 问题8：缺少"集合竞价"分析

短线选手最重要的决策窗口是 9:15-9:25，当前系统缺少：
- 竞价量分析
- 竞价图形识别（抢筹/砸盘/平稳）
- 竞价封单变化

#### 问题9：配置分散

三处配置：`settings.py`（Python）、`emotion_cycle_config.yaml`、`sector_tracker_config.yaml`，参数分散，调参不便。

#### 问题10：缺少信号优先级与互斥规则

当同一只股票同时触发"弱转强"和"二板定龙"时，系统没有优先级判断和互斥处理。

---

## 三、架构优化方案

### 3.1 目标架构：五层复盘流水线

```
┌─────────────────────────────────────────────────────────────────┐
│                    短线复盘流水线 (Review Pipeline)                │
│                                                                  │
│  Layer 1          Layer 2         Layer 3        Layer 4         │
│  看大盘            看板块           看个股          定计划          │
│  (定仓位)          (定方向)         (定标的)        (定执行)        │
│                                                                  │
│  MarketEnv     SectorAnalysis   StockSelection  TradePlan       │
│  Analyzer      Orchestrator     Engine          Generator       │
│      │              │               │               │           │
│      ▼              ▼               ▼               ▼           │
│  指数+量能      热点+持续       龙头+模式        竞价+仓位        │
│  涨跌比+         +共振+          +地位+          +止损+          │
│  情绪周期        梯队+轮动       优先级          止盈            │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 5: 盘后总结 (Review & Feedback)                            │
│  ├── 信号表现统计（各模式胜率/盈亏比）                              │
│  ├── 情绪周期变化趋势                                              │
│  └── 参数自适应调整建议                                            │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 优化后的目录结构

```
a_stock_sentiment_system/
├── main.py                          # 精简入口，仅做 CLI 解析和调度
├── scheduler.py                     # 定时调度
│
├── config/                          # 【统一配置层】
│   ├── settings.py                  # 全局参数（精简，仅基础路径/API）
│   ├── strategy_params.yaml         # 所有策略参数（统一 YAML）
│   └── config_loader.py             # 统一配置加载器
│
├── core/                            # 【核心层】
│   ├── data/                        # 数据子层（不变）
│   │   ├── data_manager.py
│   │   ├── data_manager_extensions.py
│   │   └── industry_mapper.py
│   │
│   ├── pipeline/                    # 【新增】复盘流水线子层
│   │   ├── __init__.py
│   │   ├── review_pipeline.py       # 流水线编排器（替代 main.py 中的大方法）
│   │   ├── layer1_market_env.py     # Layer 1：大盘环境判断
│   │   ├── layer2_sector.py         # Layer 2：板块分析（封装 orchestrator）
│   │   ├── layer3_stock_selection.py # Layer 3：个股筛选（模式+地位+优先级）
│   │   ├── layer4_trade_plan.py     # Layer 4：交易计划生成
│   │   └── layer5_review.py         # Layer 5：盘后总结
│   │
│   ├── analysis/                    # 分析子层（精简）
│   │   ├── emotion/                 # 情绪周期（独立子包）
│   │   │   ├── __init__.py
│   │   │   ├── rule_engine.py       # 规则引擎（原 emotion_cycle_engine.py）
│   │   │   ├── ml_engine.py         # ML 引擎（原 emotion_cycle_ml.py）
│   │   │   └── integrated.py        # 综合引擎（原 emotion_cycle_integrated.py）
│   │   │
│   │   ├── sector/                  # 板块分析（独立子包）
│   │   │   ├── __init__.py
│   │   │   ├── orchestrator.py      # 统筹入口
│   │   │   ├── hotspot.py           # 热点识别
│   │   │   ├── persistence.py       # 持续性分析
│   │   │   ├── resonance.py         # 共振分析
│   │   │   └── hierarchy.py         # 连板梯队
│   │   │
│   │   ├── moneyflow/               # 资金流向（独立子包）
│   │   │   ├── __init__.py
│   │   │   ├── stock_moneyflow.py
│   │   │   ├── hsgt_flow.py         # 北向资金
│   │   │   └── top_list.py          # 龙虎榜
│   │   │
│   │   └── chip/                    # 筹码结构
│   │       └── chip_analyzer.py
│   │
│   ├── pattern/                     # 模式策略子层（不变）
│   │   ├── weak_to_strong.py
│   │   ├── second_board_dragon.py
│   │   ├── first_board_breakout.py
│   │   ├── dragon_second_wave.py
│   │   ├── dragon_dynamic_manager.py
│   │   └── signal_priority.py       # 【新增】信号优先级与互斥规则
│   │
│   ├── stock_ranking/               # 【新增】个股地位量化子层
│   │   ├── __init__.py
│   │   ├── sector_position.py       # 个股在板块中的地位（龙头/跟风/补涨）
│   │   └── multi_factor_scorer.py   # 多因子综合评分
│   │
│   ├── auction/                     # 【新增】集合竞价分析子层
│   │   ├── __init__.py
│   │   ├── auction_analyzer.py      # 竞价量/竞价图形分析
│   │   └── auction_condition.py     # 竞价条件判断
│   │
│   ├── execution/                   # 执行子层
│   │   ├── execution_engine.py
│   │   └── retail_trader_support_v2.py
│   │
│   ├── report/                      # 报告子层
│   ├── publish/                     # 发布子层
│   └── utils/                       # 工具子层
│
├── backtest/                        # 【回测层】与主流程打通
│   ├── backtest_engine.py
│   ├── performance_analyzer.py
│   ├── trade_simulator.py
│   └── signal_review.py             # 【新增】信号复盘（按系统信号模拟操作）
│
├── risk/                            # 风控层
└── scripts/                         # 脚本层
```

### 3.3 新增/重构模块详解

#### 3.3.1 Layer 1：大盘环境判断 (`layer1_market_env.py`)

```
职责：替代当前缺失的"看大盘"步骤

输入数据：
  - 上证指数/深证成指/创业板指 日线数据
  - 全市场涨跌家数
  - 全市场成交额

输出指标：
  - 指数趋势（多头/空头/震荡）
  - 量能状态（放量/缩量/平量）
  - 市场宽度（上涨家数/下跌家数）
  - 综合环境评分（0-100）

与情绪周期的关系：
  - 大盘环境 + 涨停情绪 = 综合仓位建议
  - 大盘空头 + 涨停高潮 = 警惕诱多
  - 大盘多头 + 涨停冰点 = 可能是低吸机会
```

#### 3.3.2 Layer 3：个股筛选增强 (`layer3_stock_selection.py`)

```
职责：替代当前 pattern_recognition 中的分散逻辑

新增功能：
  1. 个股板块地位量化
     - 空间龙头：板块内最高连板
     - 强度龙头：板块内最早封板
     - 中军：板块内最大市值涨停
     - 跟风：板块内后排涨停
     - 补涨：板块内首板

  2. 信号优先级与互斥规则
     - 同一股票触发多个模式 → 按优先级取最高
       优先级：弱转强 > 二板定龙 > 龙二波 > 首板突破
     - 互斥规则：同一板块最多推荐 3 只标的

  3. 多因子综合评分
     Score = 模式质量(0.35) × 板块强度(0.30) × 个股地位(0.20) × 情绪适配(0.15)
```

#### 3.3.3 Layer 4：交易计划增强 (`layer4_trade_plan.py`)

```
职责：替代当前 execution_engine 中的简单过滤

新增功能：
  1. 竞价条件量化
     - 高开幅度阈值（按模式差异化）
     - 竞价量阈值（相对昨日成交量）
     - 竞价图形识别（抢筹/砸盘/平稳）

  2. 仓位分配矩阵
     - 核心标的（龙头+共振）：40%仓位
     - 次核心（模式+共振）：25%仓位
     - 套利标的（仅模式）：15%仓位
     - 观察标的（仅共振）：不入

  3. 止损止盈差异化
     - 弱转强：止损-5%，止盈+15%
     - 二板定龙：止损-7%，止盈+20%
     - 首板突破：止损-3%，止盈+8%
     - 龙二波：止损-5%，止盈+12%
```

#### 3.3.4 Layer 5：盘后总结 (`layer5_review.py`)

```
职责：新增模块，填补当前空白

功能：
  1. 今日信号回顾
     - 各模式发出了多少信号
     - 信号标的表现如何（涨停/大涨/冲高回落/低开）
     - 按模式统计胜率

  2. 情绪周期变化趋势
     - 近5日情绪周期变化
     - 关键指标趋势图（涨停家数/炸板率/溢价率）

  3. 参数敏感度分析
     - 如果阈值调高/调低，信号数量变化
     - 建议调参方向
```

#### 3.3.5 集合竞价分析 (`auction/`)

```
职责：新增模块，填补竞价分析空白

功能：
  1. 竞价量分析
     - 竞价量/昨日成交量 比值
     - 竞价量变化趋势（9:15→9:25）

  2. 竞价图形识别
     - 抢筹型：价格逐步推高
     - 砸盘型：价格逐步走低
     - 平稳型：价格窄幅波动

  3. 竞价封单分析
     - 涨停封单量变化
     - 板块竞价封单对比
```

#### 3.3.6 信号优先级与互斥 (`signal_priority.py`)

```
职责：新增模块，处理多信号冲突

规则：
  1. 优先级排序
     弱转强(100) > 二板定龙(85) > 龙二波(70) > 首板突破(50)

  2. 互斥规则
     - 同一板块最多推荐 3 只标的
     - 同一模式最多推荐 5 只标的
     - 总推荐标的 ≤ 10 只

  3. 去重规则
     - 同一股票触发多个模式 → 保留最高优先级
     - 保留次高优先级作为"备选逻辑"
```

### 3.4 重构后的 main.py（精简版）

```python
class SentimentSystem:
    def __init__(self):
        self.dm = DataManager(...)
        self.pipeline = ReviewPipeline(self.dm)  # 流水线编排器

    def run_daily_analysis(self, date: str = None):
        """精简入口：仅做 CLI 解析和流水线调度"""
        date = self._resolve_date(date)

        # 执行五层复盘流水线
        result = self.pipeline.execute(date)

        # 生成报告
        self._generate_reports(result)

        # 输出摘要
        self._print_summary(result)
```

### 3.5 优化优先级排序

| 优先级 | 优化项 | 影响范围 | 工作量 |
|--------|--------|----------|--------|
| **P0** | main.py 重构（抽取 ReviewPipeline） | 全局架构 | 中 |
| **P0** | 新增 Layer 1：大盘环境判断 | 新增模块 | 中 |
| **P1** | 新增 Layer 5：盘后总结 | 新增模块 | 中 |
| **P1** | 新增信号优先级与互斥规则 | 新增模块 | 小 |
| **P1** | 新增个股板块地位量化 | 新增模块 | 中 |
| **P2** | Layer 4 交易计划增强（竞价条件+仓位矩阵） | 增强现有 | 中 |
| **P2** | 新增集合竞价分析 | 新增模块 | 大 |
| **P2** | 回测与主流程打通 | 重构现有 | 大 |
| **P3** | 配置统一化（YAML 合并） | 重构配置 | 小 |
| **P3** | 分析子层拆分子包（emotion/sector/moneyflow） | 重构目录 | 小 |

---

## 四、数据流优化

### 4.1 当前数据流问题

1. **重复获取**：模式识别和板块分析各自获取历史涨停池，存在重复调用
2. **缓存粒度粗**：`SectorAnalysisOrchestrator` 缓存整个分析结果，但部分子结果可能被单独需要
3. **数据传递链路长**：`main.py → orchestrator → tracker → analyzer`，中间经过多层

### 4.2 优化方案

```
                    ┌─────────────────────────┐
                    │    DataManager (单例)     │
                    │    ├── 内存缓存 (LRU)     │
                    │    ├── 本地缓存 (Parquet) │
                    │    └── API 调用           │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
     ┌────────────┐    ┌────────────┐    ┌────────────┐
     │ Layer 1    │    │ Layer 2    │    │ Layer 3    │
     │ 大盘环境    │    │ 板块分析    │    │ 个股筛选    │
     │            │    │            │    │            │
     │ 需要:      │    │ 需要:      │    │ 需要:      │
     │ 指数日线   │    │ 涨停池      │    │ 涨停池      │
     │ 涨跌家数   │    │ 板块成分股  │    │ 历史涨停池  │
     │ 成交额     │    │ 历史热点    │    │ 日线数据    │
     └────────────┘    └────────────┘    └────────────┘
              │                  │                  │
              └──────────────────┼──────────────────┘
                                 │
                                 ▼
                      ┌─────────────────────┐
                      │ SharedContext        │
                      │ (流水线共享上下文)    │
                      │                     │
                      │ ├── zt_pool         │
                      │ ├── limit_down_df   │
                      │ ├── history_pools   │
                      │ ├── hot_sectors     │
                      │ └── emotion_result  │
                      └─────────────────────┘
```

**SharedContext** 设计：
- 每个 Layer 从 SharedContext 读取上游结果，写入本层结果
- 避免重复数据获取
- 便于单步调试（可以单独执行某一层）

---

## 五、总结

### 5.1 当前架构优势

1. **模块化程度较高**：data/analysis/pattern/execution/report 分层清晰
2. **板块分析体系完整**：热点识别→持续性→共振→梯队，四维一体
3. **情绪周期双引擎**：规则+ML，互相验证
4. **模式策略丰富**：弱转强、二板定龙、首板突破、龙二波四大核心模式
5. **输出多样化**：Excel报告、交易计划JSON、散户决策TXT、微信公众号

### 5.2 核心差距

| 维度 | 当前状态 | 目标状态 |
|------|----------|----------|
| 大盘环境 | ❌ 缺失 | 指数+量能+宽度综合评分 |
| 板块分析 | ✅ 85% | 增加板块轮动节奏 |
| 个股筛选 | ⚠️ 70% | 增加地位量化+优先级+多因子评分 |
| 交易计划 | ⚠️ 60% | 增加竞价条件+仓位矩阵+差异化止损 |
| 盘后总结 | ❌ 缺失 | 信号回顾+趋势图+参数建议 |
| 代码架构 | ⚠️ main.py 臃肿 | ReviewPipeline 流水线模式 |

### 5.3 建议实施路径

```
Phase 1（1-2周）：架构重构
  ├── main.py 抽取 ReviewPipeline
  ├── 新增 SharedContext
  └── 分析子层拆分子包

Phase 2（1-2周）：补齐缺失层
  ├── Layer 1：大盘环境判断
  ├── Layer 5：盘后总结
  └── 信号优先级与互斥规则

Phase 3（2-3周）：增强现有层
  ├── Layer 3：个股地位量化 + 多因子评分
  ├── Layer 4：竞价条件 + 仓位矩阵
  └── 集合竞价分析

Phase 4（1-2周）：打通与优化
  ├── 回测与主流程打通
  ├── 配置统一化
  └── 全流程集成测试
```