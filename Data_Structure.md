# 数据结构文档

本文档整理了 `core/data/data_manager.py` 中获取的主要数据类型及其结构。

## 一、市场基础数据

### 1.1 每日行情基础数据
**方法**: `get_daily_basic(trade_date: str)`

**调用接口**:
- 主源: `Tushare daily_basic` - 获取每日指标数据
- 备用: `AkShare stock_zh_a_spot_em` - 获取东方财富实时行情

获取全市场股票的每日基础行情数据，包含市盈率、换手率、成交量等基础指标。

**主要字段**:
- `ts_code`: 股票代码
- `trade_date`: 交易日期
- `close`: 收盘价
- `turnover_rate`: 换手率
- `turnover_rate_f`: 自由流通股换手率
- `volume_ratio`: 量比
- `pe`: 市盈率
- `pb`: 市净率
- `total_share`: 总股本
- `float_share`: 流通股本

---

### 1.2 个股历史日线数据
**方法**: `get_stock_daily(ts_code: str, start_date: str, end_date: str)`

**调用接口**:
- 主源: `Tushare daily` - 日线行情数据
- 备用: `AkShare stock_zh_a_hist` - 个股历史行情

获取指定股票在日期范围内的历史K线数据。

**主要字段**:
- `trade_date`: 交易日期
- `open`: 开盘价
- `high`: 最高价
- `low`: 最低价
- `close`: 收盘价
- `pre_close`: 昨收价
- `change`: 涨跌额
- `pct_chg`: 涨跌幅(%)
- `vol`: 成交量(手)
- `amount`: 成交额(千元)

---

### 1.3 个股单日行情数据
**方法**: `get_stock_daily_data(symbol: str, trade_date: str)`

**调用接口**:
- 内部调用 `get_stock_daily()` 获取个股日线数据

获取指定股票单日的完整行情数据（包含基础指标）。

**主要字段**:
- `open`, `high`, `low`, `close`: OHLC价格
- `pre_close`: 昨收价
- `change`, `pct_chg`: 涨跌额和涨跌幅
- `vol`, `amount`: 成交量和成交额
- `pe`, `pb`: 市盈率和市净率
- `total_mv`, `float_mv`: 总市值和流通市值

---

### 1.4 个股单日基础指标
**方法**: `get_stock_daily_basic(symbol: str, trade_date: str)`

**调用接口**:
- 内部调用 `get_daily_basic()` 获取全市场基础指标后筛选

获取指定股票单日的基础指标数据（不包含价格）。

**主要字段**:
- `turnover_rate`: 换手率
- `turnover_rate_f`: 自由流通股换手率
- `volume_ratio`: 量比
- `pe`, `pe_ttm`: 市盈率和TTM市盈率
- `pb`: 市净率
- `total_share`, `float_share`: 总股本和流通股本

---

### 1.5 个股单日价格数据
**方法**: `get_stock_daily_price(ts_code: str, trade_date: str)`

**调用接口**:
- 主源: `Tushare daily` - 日线行情
- 备用: `AkShare stock_zh_a_hist` - 历史行情

获取指定股票单日的价格数据（简化版）。

**返回**: Dict 包含
- `open`, `high`, `low`, `close`: OHLC价格
- `pre_close`: 昨收价
- `volume`, `amount`: 成交量和成交额

---

## 二、涨跌停数据

### 2.1 涨停池数据
**方法**: `get_limit_up_pool(date: str)`

**调用接口**:
- 主源: `Tushare limit_list_d` - 涨跌停数据（包含板上成交金额）
- 备用: `AkShare stock_zt_pool_em` - 东方财富涨停池

获取指定日期的涨停股票池，优先使用Tushare（包含板上成交金额等详细数据）。

**主要字段**:
- `代码`: 股票代码(6位)
- `名称`: 股票名称
- `最新价`: 涨停价
- `涨跌幅`: 当日涨跌幅
- `所属行业`: 行业名称
- `行业代码`: 东财行业代码
- `首次封板时间`: 首次涨停时间
- `最后封板时间`: 最后涨停时间
- `炸板次数`: 开板次数
- `连板数`: 连续涨停天数
- `涨停统计`: 涨停统计信息(如"3/3"表示3天3板)
- `成交额`: 当日成交额
- `换手率`: 当日换手率
- `流通市值`: 流通市值
- `板上成交金额`: 涨停板上成交金额(Tushare特有)
- `封单金额`: 涨停封单金额(Tushare特有)

---

### 2.2 跌停池数据
**方法**: `get_limit_down_pool(date: str)`

**调用接口**:
- 主源: `Tushare limit_list_d` - 涨跌停数据(limit='D'表示跌停)
- 备用: `AkShare stock_zt_pool_dtgc_em` - 东方财富跌停池

获取指定日期的跌停股票池。

**主要字段**: 与涨停池类似，但为跌停数据
- `代码`, `名称`, `最新价`, `涨跌幅`
- `所属行业`, `行业代码`
- `跌停封单量`, `跌停封单额`
- `首次跌停时间`, `最后跌停时间`

---

### 2.3 连板阶梯数据
**方法**: `get_limit_step(trade_date: str, ts_code: str)`

**调用接口**:
- `Tushare limit_list_d` - 涨跌停数据

获取涨停股票的连板阶梯数据，用于分析连板高度分布。

**主要字段**:
- `ts_code`: 股票代码
- `trade_date`: 交易日期
- `name`: 股票名称
- `limit`: 涨停类型(U=涨停,D=跌停)
- `limit_amount`: 封单金额
- `limit_times`: 连板数

---

## 三、板块/行业数据

### 3.1 东财行业板块列表
**方法**: `get_dc_industry_list(trade_date: str)`

**调用接口**:
- `Tushare dc_index` - 东财板块指数列表(idx_type='行业板块')

获取东财行业板块列表（使用dc_index接口，包含行业代码）。

**主要字段**:
- `ts_code`: 行业代码(如"BK1627.DC")
- `name`: 行业名称
- `level`: 行业级别
- `trade_date`: 交易日期

---

### 3.2 东财行业成分股
**方法**: `get_dc_industry_cons(ts_code: str, trade_date: str)`

**调用接口**:
- `Tushare dc_member` - 东财板块成分股

获取指定东财行业板块的成分股列表。

**主要字段**:
- `ts_code`: 行业代码
- `con_code`: 成分股代码
- `con_name`: 成分股名称
- `trade_date`: 交易日期

---

### 3.3 同花顺板块指数列表
**方法**: `get_ths_index(index_type: str)`

**调用接口**:
- `Tushare ths_index` - 同花顺板块指数列表

获取同花顺板块指数列表（概念板块、行业板块等）。

**主要字段**:
- `ts_code`: 板块代码(如"885760.TI")
- `name`: 板块名称
- `type`: 板块类型(概念/行业)
- `exchange`: 交易所

---

### 3.4 同花顺板块指数日线
**方法**: `get_ths_daily(ts_code: str, trade_date: str, start_date: str, end_date: str)`

**调用接口**:
- `Tushare ths_daily` - 同花顺板块指数日线

获取同花顺板块指数的历史日线数据。

**主要字段**:
- `ts_code`: 板块代码
- `trade_date`: 交易日期
- `close`, `open`, `high`, `low`: 价格数据
- `change`, `pct_change`: 涨跌额和涨跌幅
- `vol`, `amount`: 成交量和成交额
- `num`: 板块内涨停家数

---

### 3.5 同花顺板块成分股
**方法**: `get_ths_member(ts_code: str)`

**调用接口**:
- `Tushare ths_member` - 同花顺板块成分股

获取同花顺板块指数的成分股列表。

**主要字段**:
- `ts_code`: 板块代码
- `code`: 成分股代码
- `name`: 成分股名称

---

### 3.6 行业板块成分股(东方财富)
**方法**: `get_industry_cons(industry: str)`

**调用接口**:
- `AkShare stock_board_industry_cons_em` - 东方财富行业板块成分股

获取指定行业板块的成分股列表（使用AkShare）。

**主要字段**:
- `代码`: 股票代码
- `名称`: 股票名称
- `最新价`, `涨跌幅`: 当前价格和涨跌幅

---

### 3.7 行业板块数据
**方法**: `get_industry_sector_data(trade_date: str)`

**调用接口**:
- `Tushare dc_index` - 东财板块指数数据
- `Tushare dc_member` - 板块成分股数据

获取行业板块的详细数据（包含成交额、涨停家数等）。

**主要字段**:
- `行业名称`: 行业名称
- `行业代码`: 行业代码
- `涨停家数`: 行业内涨停股票数量
- `连板家数`: 行业内连板股票数量
- `成交额`: 行业总成交额
- `换手率`: 行业平均换手率
- `涨幅`: 行业平均涨幅

---

### 3.8 板块资金流入流出
**方法**: `get_sector_moneyflow(trade_date: str, sector_type: str)`

**调用接口**:
- `AkShare stock_sector_fund_flow_hist` - 板块资金流向历史
- `AkShare stock_sector_fund_flow_rank` - 板块资金流向排名

获取板块的资金流向数据。

**主要字段**:
- `板块名称`: 板块名称
- `主力净流入`: 主力资金净流入金额
- `主力净流入占比`: 主力资金净流入占比
- `超大单净流入`: 超大单净流入金额
- `大单净流入`: 大单净流入金额
- `中单净流入`: 中单净流入金额
- `小单净流入`: 小单净流入金额

---

### 3.9 涨停概念板块列表
**方法**: `get_limit_cpt_list(trade_date: str)`

**调用接口**:
- `AkShare stock_zt_pool_em` - 涨停池数据
- 内部统计概念涨停家数

获取涨停概念板块列表（按涨停家数排序）。

**主要字段**:
- `name`: 概念名称
- `up_nums`: 涨停家数
- `cons_nums`: 连续涨停家数
- `turnover_rate`: 板块换手率
- `amount`: 板块成交金额
- `rank`: 当日排名

---

## 四、概念数据

### 4.1 概念成分股映射
**方法**: `get_concept_members(trade_date: str)`

**调用接口**:
- `Tushare ths_member` - 同花顺概念成分股

获取概念板块与成分股的映射关系。

**主要字段**:
- `ts_code`: 概念代码
- `name`: 概念名称
- `con_code`: 成分股代码
- `con_name`: 成分股名称

---

### 4.2 个股所属概念
**方法**: `get_stock_concepts(stock_code: str)` / `get_stock_concepts_from_members(stock_code: str)`

**调用接口**:
- `Tushare ths_member` - 同花顺概念成分股
- `AkShare stock_board_concept_name_ths` - 同花顺概念列表

获取指定股票所属的概念板块列表。

**返回**: 字符串，包含多个概念名称（逗号分隔）

---

## 五、资金流向数据

### 5.1 板块资金流向类型
**方法**: `get_sector_capital_flow_type(sector_name: str, ts_code: str, trade_date: str)`

**调用接口**:
- `get_sector_moneyflow()` - 获取板块资金流向
- 内部计算流向类型

获取板块的资金流向类型判断（流入/流出/震荡）。

**返回**: Dict 包含
- `flow_type`: 流向类型(流入/流出/震荡)
- `main_inflow`: 主力净流入
- `total_amount`: 总成交额
- `inflow_ratio`: 流入占比

---

### 5.2 板块因子数据
**方法**: `get_sector_factors(industry_name: str, ts_code: str, trade_date: str)`

**调用接口**:
- `get_ths_daily()` - 板块日线数据
- `get_sector_moneyflow()` - 板块资金流向
- 内部计算因子

获取板块的量化因子数据。

**返回**: Dict 包含
- `momentum`: 动量因子
- `volatility`: 波动率因子
- `liquidity`: 流动性因子
- `sentiment`: 情绪因子

---

## 六、Tick/分时数据

### 6.1 个股Tick数据
**方法**: `get_stock_tick(code: str, trade_date: str)`

**调用接口**:
- `AkShare stock_zh_a_tick_tx` - 腾讯财经Tick数据
- `AkShare stock_zh_a_tick_sina` - 新浪财经Tick数据

获取指定股票的分时成交明细(Tick数据)。

**主要字段**:
- `time`: 时间
- `price`: 成交价
- `volume`: 成交量
- `amount`: 成交额
- `type`: 成交类型(买盘/卖盘/中性盘)

---

### 6.2 集合竞价数据
**方法**: `get_auction_data(code: str, trade_date: str)`

**调用接口**:
- `AkShare stock_zh_a_daily` - 日线数据
- 内部计算集合竞价相关指标

获取指定股票的集合竞价数据。

**返回**: Dict 包含
- `open_price`: 开盘价
- `pre_close`: 昨收价
- `volume`: 竞价成交量
- `amount`: 竞价成交额
- `matched_volume`: 匹配成交量
- `unmatched_buy_volume`: 未匹配买量
- `unmatched_sell_volume`: 未匹配卖量

---

## 七、实时数据

### 7.1 全市场实时行情
**方法**: `get_all_rt_k_data(trade_date: str)`

**调用接口**:
- `AkShare stock_zh_a_spot_em` - 东方财富实时行情

获取全市场股票的实时行情数据。

**主要字段**:
- `代码`, `名称`: 股票代码和名称
- `最新价`, `涨跌幅`, `涨跌额`: 价格和涨跌
- `成交量`, `成交额`: 成交数据
- `买一价`~`买五价`, `卖一价`~`卖五价`: 五档盘口
- `买一量`~`买五量`, `卖一量`~`卖五量`: 五档量

---

## 八、数据缓存策略

所有数据获取方法都实现了缓存机制：

1. **缓存位置**: `{cache_dir}/{trade_date}/`
2. **缓存格式**: CSV文件
3. **缓存命名**: `{data_type}_{date}.csv`
4. **缓存策略**: 
   - 优先读取本地缓存
   - 缓存不存在时从API获取
   - 获取成功后写入缓存

---

## 九、数据源优先级

1. **Tushare**: 优先使用（数据质量高，字段完整）
   - 需要Token认证
   - 部分接口需要积分权限
   - 数据标准化程度高

2. **AkShare**: 备用方案（免费，覆盖范围广）
   - 无需认证
   - 实时性较好
   - 字段命名可能不统一

---

## 十、工具类

DataManager还提供了以下工具类：

- `date_utils`: 日期工具（交易日判断、日期转换等）
  - `is_trade_date()`: 判断是否为交易日
  - `get_prev_trade_date()`: 获取上一交易日
  - `get_next_trade_date()`: 获取下一交易日
  - `get_last_n_trade_dates()`: 获取最近N个交易日

- `stock_code_utils`: 股票代码工具（代码格式转换等）
  - `standardize_code()`: 标准化股票代码
  - `add_suffix()`: 添加交易所后缀
  - `remove_suffix()`: 移除交易所后缀

- `time_utils`: 时间工具
  - `is_trading_time()`: 判断是否为交易时间
  - `get_current_time()`: 获取当前时间

- `calculation_utils`: 计算工具
  - `calculate_ma()`: 计算移动平均线
  - `calculate_rsi()`: 计算RSI指标
  - `calculate_macd()`: 计算MACD指标

- `validation_utils`: 数据验证工具
  - `validate_trade_date()`: 验证交易日期格式
  - `validate_stock_code()`: 验证股票代码格式
