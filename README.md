# A股情绪周期与板块轮动量化系统 V2

基于情绪周期识别、板块轮动追踪、概念-行业交叉验证的短线交易辅助系统。

## 🎯 核心功能

### 1. 数据层 (Data Layer)
- **双源备份**: Tushare(历史数据) + AkShare(实时情绪)
- **本地缓存**: 自动避免重复API调用，支持盘中/盘后增量更新
- **智能交易日判断**: 非交易日自动关联最近交易日数据
- **同花顺板块数据**: 集成同花顺概念/行业/特色指数数据(ths_index/ths_daily/ths_member)

### 2. 情绪周期引擎 (Emotion Cycle Engine)
- **五阶段模型**: 冰点期 → 回暖期 → 上升期 → 高潮期 → 退潮期
- **多维度评分**: 涨停家数、跌停家数、炸板率、昨日涨停溢价率
- **动态策略**: 根据情绪周期自动生成仓位建议和禁忌操作
- **周期切换预警**: 识别情绪拐点，提前调整策略

### 3. 板块轮动追踪器 V2 (Sector Rotation Tracker)
- **多因子动态权重模型**:
  - 强度因子(30%): 涨停家数、连板数
  - 资金因子(25%): 成交额变化率、换手率
  - 趋势因子(25%): 排名动量、涨停趋势、持续性评分
  - 市场适配(20%): 根据周期动态调整
- **市场周期适配**: 自动检测市场周期，调整因子权重
- **板块生命周期**: 萌芽期 → 加速期 → 高潮期 → 衰退期
- **轮动图谱**: 构建资金迁移路径，预判接力板块

### 4. 概念-行业交叉验证 (Concept-Industry Validation)
- **双轨制分析**: 概念维度(短线热点) + 行业维度(中线趋势)
- **共振识别**: 概念热 + 行业热 = 强信号，重仓参与
- **背离预警**: 概念热 + 行业冷 = 纯炒作，谨慎参与
- **行业集中度**: 分析概念成分股的行业分布，识别真正龙头
- **共振得分**: 0-100分量化信号强度，动态调整仓位

### 5. 模式识别 (Pattern Recognition)
- **首板突破**: 早盘秒封 + 封单强度>5% + 换手5-20%
- **二板定龙**: 昨日首板 + 今日高开3-7% + 15分钟内涨停
- **弱转强**: 昨日烂板/炸板 + 今日跳空高开>2% + 今日涨停
- **分歧转一致**: 昨日烂板爆量 + 今日高开2-5% + 30分钟内涨停
- **卡位板**: 同板块内低位股抢先涨停5分钟以上
- **龙头首阴**: 龙头股首次阴线后的反包机会

### 6. 散户决策支持 (Retail Trader Support)
- **隔夜预判**: 基于当日数据推演次日市场剧本
- **三阶过滤**: 大盘环境 → 板块强度 → 个股质量
- **散户特供指标**: 昨日涨停溢价率、一字板占比、策略建议
- **决策清单**: 持仓处理、目标买入、风险规避

### 7. 可视化报告 (Reporting)
- **Excel多Sheet报表**:
  - Dashboard: 市场情绪、主线Top5、涨停梯队
  - 板块轮动分析: 概念评分、信号类型、共振得分
  - 涨停梯队: 按连板高度分组
  - 模式信号: 交易机会列表
  - 情绪周期: 五阶段判断和策略建议
- **Markdown分析报告**: 市场主线分析报告

## 🛠️ 安装与配置

### 步骤1: 安装依赖
```bash
cd a_stock_sentiment_system
pip install -r requirements.txt
```

### 步骤2: 配置API Token
编辑 `config/settings.py`:
```python
TUSHARE_TOKEN = "你的tushare_token_here"
```

### 步骤3: 准备行业映射
系统使用 `data/Industry_Mapping.csv` 作为东财行业映射源，格式如下：
```csv
一级行业,二级行业,三级行业
电力设备,光伏设备,光伏组件
电力设备,光伏设备,光伏辅材
...
```

## 🚀 使用方式

### 每日分析（收盘后）
```bash
python main.py
```

### 指定日期分析
```bash
python main.py --date 20260417
```

### 模块测试
```bash
# 测试情绪周期引擎
python core/analysis/emotion_cycle_engine.py

# 测试板块轮动追踪器
python core/analysis/sector_rotation_tracker.py

# 测试概念-行业交叉验证
python core/analysis/concept_industry_validator.py
```

### 定时任务（每日15:40自动运行）
```bash
python scheduler.py
```

## 📊 输出文件

### 主系统输出
- `output/A股情绪分析报告_YYYYMMDD.xlsx` - 每日情绪分析报告
  - Dashboard: 市场情绪概览
  - 板块轮动分析: 概念评分、信号类型、共振得分
  - 涨停梯队: 连板高度分布
  - 模式信号: 交易机会
  - 情绪周期: 五阶段判断

### 散户决策支持报告
- `output/散户决策报告_YYYYMMDD.txt` - 隔夜预判、三阶过滤、剧本推演

### 交易计划
- `output/交易计划_YYYYMMDD.xlsx` - 次日可执行的交易计划

## 🧠 交易逻辑整合

### 数据流程 (15:40)
1. 采集涨停池、跌停池、连板天梯数据
2. 获取同花顺概念板块统计数据(limit_cpt_list)
3. 情绪周期分析 → 判断市场阶段
4. 板块轮动分析 → 识别热点概念
5. 概念-行业交叉验证 → 筛选有产业支撑的热点
6. 模式识别 → 发现交易机会
7. 生成交易计划 → 次日执行

### 决策流程
1. **看情绪**: 当前处于什么周期？冰点/回暖/上升/高潮/退潮？
2. **看板块**: 哪些概念板块评分高？信号类型是共振还是背离？
3. **看验证**: 概念热度是否有行业支撑？集中度如何？
4. **盯模式**: 在这些板块中，谁是"首板突破"？谁是"二板定龙"？
5. **定计划**: 根据情绪周期确定仓位，根据信号强度确定标的

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
├── config/
│   ├── __init__.py
│   └── settings.py              # 配置参数(Tushare Token等)
├── core/
│   ├── __init__.py
│   ├── analysis/                # 分析引擎
│   │   ├── emotion_cycle_engine.py      # 情绪周期识别引擎
│   │   ├── sector_rotation_tracker.py   # 板块轮动追踪器V2
│   │   ├── concept_industry_validator.py # 概念-行业交叉验证
│   │   └── pattern_recognition.py       # 模式识别整合
│   ├── data/                    # 数据层
│   │   ├── data_manager.py      # 数据获取与缓存管理
│   │   └── industry_mapper.py   # 行业映射(东财+同花顺)
│   ├── pattern/                 # 模式识别
│   │   ├── first_board_breakout.py      # 首板突破
│   │   ├── second_board_dragon.py       # 二板定龙
│   │   ├── weak_to_strong.py            # 弱转强
│   │   ├── divergence_to_consensus.py   # 分歧转一致
│   │   ├── position_battle.py           # 卡位板
│   │   ├── dragon_second_wave.py        # 龙头首阴
│   │   └── blast_reseal.py              # 炸板回封
│   ├── execution/               # 执行层
│   │   ├── execution_engine.py          # 交易执行引擎
│   │   └── retail_trader_support_v2.py  # 散户决策支持V2
│   ├── report/                  # 报告层
│   │   └── report_generator.py  # 报告生成器
│   └── utils/                   # 工具类
│       ├── date_utils.py        # 日期工具
│       ├── stock_code_utils.py  # 股票代码工具
│       ├── time_utils.py        # 时间工具
│       ├── calculation_utils.py # 计算工具
│       ├── validation_utils.py  # 验证工具
│       └── data_updater.py      # 数据更新工具
├── data/
│   ├── raw/                     # 原始数据按日期存储
│   ├── cache/                   # 缓存文件
│   └── Industry_Mapping.csv     # 东财行业映射表
├── output/                      # 生成报告
├── examples/
│   └── demo_usage.py            # 使用示例
├── scheduler.py                 # 定时任务调度
├── main.py                      # 主程序入口
└── requirements.txt             # 依赖列表
```

## 🔧 核心模块说明

### emotion_cycle_engine.py
- **五阶段识别**: 冰点期、回暖期、上升期、高潮期、退潮期
- **多维度数据**: 涨停家数、跌停家数、炸板率、昨日涨停溢价率
- **动态策略生成**: 根据周期自动调整仓位和禁忌操作
- **情绪拐点预警**: 识别周期切换信号

### sector_rotation_tracker.py
- **多因子模型**: 强度、资金、趋势、分化四大因子
- **动态权重**: 根据市场周期自动调整因子权重
- **板块生命周期**: 萌芽期、加速期、高潮期、衰退期
- **轮动图谱**: 资金迁移路径可视化

### concept_industry_validator.py
- **双轨验证**: 概念维度 + 行业维度
- **共振识别**: 概念热 + 行业热 = 强信号
- **背离预警**: 概念热 + 行业冷 = 谨慎
- **集中度分析**: 识别真正龙头板块

### data_manager.py
- **Tushare接口**: 涨停池、跌停池、连板天梯、概念板块
- **同花顺接口**: ths_index(板块指数)、ths_daily(行情)、ths_member(成分)
- **智能缓存**: 自动避免重复API调用
- **交易日处理**: 非交易日自动关联最近交易日

### industry_mapper.py
- **东财映射**: L1(一级) → L2(二级) → L3(三级) → 个股
- **同花顺映射**: 概念指数 → 成分股
- **双向查询**: 支持名称和代码互查

## 📈 数据流程

```
1. 数据采集 (Tushare/AkShare)
   ↓
2. 情绪周期分析 (EmotionCycleEngine)
   ↓
3. 板块轮动分析 (SectorRotationTracker)
   ↓
4. 概念-行业验证 (ConceptIndustryValidator)
   ↓
5. 模式识别 (Pattern Recognition)
   ↓
6. 散户决策支持 (RetailTraderSupport)
   ↓
7. 报告生成 (Excel + Markdown)
```

## 🔥 最新更新

### 2026-04-18 V2.0 重大更新
- ✅ 新增情绪周期识别引擎(EmotionCycleEngine)
- ✅ 板块轮动追踪器升级为V2多因子动态权重模型
- ✅ 新增概念-行业交叉验证功能
- ✅ 新增同花顺板块数据接口(ths_index/ths_daily/ths_member)
- ✅ 重构行业映射，分离东财和同花双体系
- ✅ 新增散户决策支持V2(隔夜预判、三阶过滤)
- ✅ 工具类封装为Class，简化导入
- ✅ 删除冗余代码(sentiment_engine.py, sector_heat_v2.py)

### 2026-04-17
- ✅ 新增涨停池数据获取(limit_up_pool/limit_down_pool)
- ✅ 新增连板天梯接口(limit_step)
- ✅ 新增最强板块统计(limit_cpt_list)
- ✅ 优化日期工具，修复非交易日处理逻辑
- ✅ 新增股票代码标准化工具

## 📞 联系方式

如有问题或建议，欢迎提出Issue或PR。
