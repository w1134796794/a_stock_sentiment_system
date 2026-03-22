# A股短线情绪量化系统

基于龙头战法、弱转强、板块切换逻辑的短线交易辅助系统。

## 🎯 核心功能

### 1. 数据层 (Data Layer)
- **双源备份**: Tushare(历史数据) + AkShare(实时情绪)
- **本地缓存**: 自动避免重复API调用，支持盘中/盘后增量更新
- **智能更新**: 每日15:35自动触发数据落库

### 2. 分析引擎 (Core Engine)
- **层级映射**: L2(二级行业) -> L3(三级行业) -> 核心标的
- **主线强度**: 涨停贡献分 = 涨停家数×权重 + 连板高度×权重
- **梯度追踪**: 1B/2B/3B...分布统计，板块联动判定
- **情绪指标**: 炸板率、昨日涨停溢价、市场温度

### 3. 模式识别 (Pattern Recognition)
- **弱转强**: 昨日烂板/炸板 + 今日跳空高开 > 2%
- **龙回头**: 历史3连板以上 + 回踩MA10/MA20 + 缩量反弹
- **卡位板**: 同板块内低位股抢先涨停，卡位高位股

### 4. 可视化报告 (Reporting)
- **Excel多Sheet报表**:
  - Dashboard: 市场情绪、主线Top5、涨停梯队
  - 核心标的: 按L2-L3层级分组，多级索引
  - 模式信号: 交易机会列表
- **5日K线微图**: mplfinance生成的走势缩略图

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

### 步骤3: 初始化行业映射
首次运行会自动创建 `data/L2_L3_Mapping.xlsx`，请根据实际交易习惯维护二三级行业映射关系。

## 🚀 使用方式

### 每日分析（收盘后）
```bash
python main.py
```

### 指定日期分析
```bash
python main.py --date 20260321
```

### 更新行业映射
```bash
python main.py --mode update
```

## 📊 输出示例

系统会生成带格式的Excel报告，包含：
1. **Dashboard**: 今日市场概览、情绪温度、最高板
2. **主线板块Top5**: 涨停家数、强度评分、代表性个股
3. **涨停梯队**: 1B/2B/3B分布、板块联动分析
4. **模式信号**: 弱转强、龙回头、卡位板标的
5. **核心标的**: 按L2->L3层级排列的涨停股票

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
│   └── settings.py          # 配置参数
├── core/
│   ├── data_manager.py      # 数据获取与缓存
│   ├── industry_mapper.py   # 行业层级映射
│   ├── sentiment_engine.py  # 情绪分析引擎
│   ├── pattern_recognition.py # 模式识别
│   └── report_generator.py  # Excel报告生成
├── data/
│   ├── raw/                 # 原始数据按日期存储
│   ├── cache/               # 缓存文件
│   └── L2_L3_Mapping.xlsx   # 行业映射表
├── output/                  # 生成报告
├── main.py                  # 主程序入口
└── requirements.txt         # 依赖列表
```
