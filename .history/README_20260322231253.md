# A股短线情绪量化系统

基于龙头战法、弱转强、板块切换逻辑的短线交易辅助系统。

## 🎯 核心功能

### 1. 数据层 (Data Layer)
- **双源备份**: Tushare(历史数据) + AkShare(实时情绪)
- **本地缓存**: 自动避免重复API调用，支持盘中/盘后增量更新
- **智能交易日判断**: 非交易日自动关联最近交易日数据

### 2. 分析引擎 (Core Engine)
- **层级映射**: L1(一级行业) -> L2(二级行业) -> L3(三级行业) -> 核心标的
  - 使用 `data/Industry_Mapping.csv` 作为行业映射源
  - 28个一级行业，117个二级行业，294个三级行业
- **主线强度**: 综合权重 = 20日涨停数×0.4 + 当日涨停数×0.4 + 最高连板×0.2
- **梯度追踪**: 1B/2B/3B/4B/5B/6B+ 分布统计，板块联动判定
- **情绪指标**: 炸板率、昨日涨停溢价、市场温度

### 3. 模式识别 (Pattern Recognition)
- **弱转强**: 昨日烂板/炸板 + 今日跳空高开 > 2% + 今日涨停
- **二板定龙**: 昨日首板 + 今日高开3-7% + 15分钟内涨停
- **首板突破**: 早盘秒封(9:40前) + 封单强度>5% + 换手5-20%
- **分歧转一致**: 昨日烂板爆量 + 今日高开2-5% + 30分钟内涨停
- **卡位板**: 同板块内低位股抢先涨停5分钟以上

### 4. 市场主线分析 (Market Mainline)
- **20日涨停统计**: 按三级行业统计涨停次数、涉及股票数
- **主线板块识别**: TOP5主线板块，包含层级信息
- **关注标的筛选**: 出现2次以上 + 最高连板≥2 + 平均涨幅≥9.5%

### 5. 可视化报告 (Reporting)
- **Excel多Sheet报表**:
  - Dashboard: 市场情绪、主线Top5、涨停梯队
  - 核心标的: 按L1-L2-L3层级分组
  - 模式信号: 交易机会列表
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
系统使用 `data/Industry_Mapping.csv` 作为行业映射源，格式如下：
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
python main.py --date 20260321
```

### 20日市场主线分析
```bash
python scripts/analyze_market_mainline.py
```

### 生成完整分析报告
```bash
python scripts/generate_final_report.py
```

### 高级标的筛选
```bash
python scripts/advanced_stock_screener.py
```

### 定时任务（每日15:40自动运行）
```bash
python scheduler.py
```

## 📊 输出文件

### 主系统输出
- `output/A股情绪分析报告_YYYYMMDD.xlsx` - 每日情绪分析报告

### 市场主线分析输出
- `output/industry_limit_up_stats_20d.xlsx` - 20日行业涨停统计
- `output/limit_up_detail_20d.xlsx` - 涨停详细数据
- `output/mainline_sectors.xlsx` - 主线板块识别结果
- `output/focus_stocks.xlsx` - 重点关注标的
- `output/市场主线分析报告_YYYYMMDD.md` - 完整分析报告

## 🧠 交易逻辑整合

### 数据落库 (15:40)
系统自动抓取今日数据，存入 `data/raw/YYYYMMDD/`

### 决策流程
1. **看梯度**: 今天市场最高板是多少？断层了吗？
2. **看主线**: 哪些L3板块涨停数在递增？
3. **盯核心**: 在这些主线L3中，谁是"弱转强"？谁是"卡位板"？

### 制定计划
从Excel的Watchlist中选出3-5只符合逻辑的个股，作为次日竞价关注标的。

## ⚠️ 免责声明

本系统仅供学习研究使用，不构成投资建议。股市有风险，投资需谨慎。

## 📁 项目结构

```
a_stock_sentiment_system/
├── config/
│   ├── __init__.py
│   └── settings.py          # 配置参数(Tushare Token等)
├── core/
│   ├── __init__.py
│   ├── data_manager.py      # 数据获取与缓存管理
│   ├── industry_mapper.py   # 行业层级映射(L1/L2/L3)
│   ├── sentiment_engine.py  # 情绪分析引擎
│   ├── pattern_recognition.py # 模式识别(弱转强/二板定龙等)
│   ├── strategy_engine.py   # 高级策略引擎
│   └── report_generator.py  # Excel报告生成
├── scripts/
│   ├── analyze_market_mainline.py  # 20日市场主线分析
│   ├── generate_final_report.py    # 生成完整分析报告
│   └── advanced_stock_screener.py  # 高级标的筛选
├── data/
│   ├── raw/                 # 原始数据按日期存储
│   ├── cache/               # 缓存文件
│   └── Industry_Mapping.csv # 行业映射表(CSV格式)
├── output/                  # 生成报告
├── utils/
│   └── data_updater.py      # 数据更新工具
├── examples/
│   └── demo_usage.py        # 使用示例
├── scheduler.py             # 定时任务调度
├── main.py                  # 主程序入口
└── requirements.txt         # 依赖列表
```

## 🔧 核心模块说明

### industry_mapper.py
- 加载 `data/Industry_Mapping.csv`
- 提供 L1/L2/L3 层级映射查询
- 支持根据三级行业查找上级行业

### pattern_recognition.py
- 检测5种交易模式
- 基于涨停池数据进行模式匹配
- 输出带置信度的交易信号

### analyze_market_mainline.py
- 采集最近20个交易日涨停数据
- 按三级行业统计涨停次数
- 识别主线板块和关注标的

## 📈 数据流程

```
1. 数据采集 (AkShare/Tushare)
   ↓
2. 行业映射 (Industry_Mapping.csv)
   ↓
3. 模式识别 (5种模式)
   ↓
4. 主线分析 (20日统计)
   ↓
5. 报告生成 (Excel + Markdown)
```
