**当前系统取数接口与字段字典**

整理日期：2026-10-25（北京时间）。代码基准：`59357a3`。本文仅梳理代码，不调用真实数据源；接口实际权限、成功率和线上开关状态未重新验证。

**阅读说明**

- 清单区分接口参数、代码消费/标准化字段、落地表字段；不把供应商可能返回的所有列都视为已验证字段。
- 多数 Tushare 调用没有 `fields` 参数，原始 CSV 缓存通常保留源响应全部列。只有少数接口显式限定字段。
- `_prefetch_*` 路径调用并不保证成功取到数据；缓存命中时不请求远端，增强接口还受账号权限影响。
- 字段可存在但值缺失。旧 Silver 标准化存在缺列补 0/空字符串的行为，0 不一律等于已观测到的真实零；新证据封装使用质量状态和 null。

**1. 数据链路与范围**

盘后：Tushare → DataManager/DataPrep → 原始缓存及来源清单 → 14 类 Silver 表 → 因子与策略。
实时：eltdx → 标准化报价/证据 → 可靠日志 → Redis → 策略/Web；历史补数写独立 backfill。
低频研究：eltdx/F10 → research.sqlite3 版本库；不覆盖 Tushare 数据。
历史兜底：AshareProvider 调用腾讯/新浪 K 线，受节点和调用方远程补数策略限制。
交易日历：DateUtils 读取本地 `TRADE_CALENDAR_FILE`，实际消费 `cal_date,is_open`；当前日常加载不是每次调用远端 trade_cal。
当前上述取数适配没有直接调用 AkShare/adata 的业务 API；它们存在于依赖清单，不应因此认定为正在使用的主数据源。

**2. Tushare：盘后/盘前主链相关接口**

共识别 30 个 Tushare 接口名：下列 23 个关联盘后预取/盘前身份或成员准备，其余 7 个属于按需、研究或兼容入口。成员接口可能由准备任务按需补齐，不表示每个盘后任务都逐只调用。

| 接口 | 当前请求参数 | 用途 | 代码明确消费/保留的主要源字段 |
| --- | --- | --- | --- |
| `daily` | trade_date；或 ts_code/start_date/end_date | 全市场/个股日线 | ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount |
| `daily_basic` | trade_date | 换手、估值与市值参考 | ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,total_share,float_share,free_share,total_mv,circ_mv（全量返回缓存；本系统按需使用） |
| `stock_basic` | exchange="",list_status="L"；盘前状态另查 L/D/P/G/UN | 代码、名称、行业及上市状态 | 日常显式字段 ts_code,symbol,name,area,industry,market,list_date；盘前显式字段 ts_code,name,list_status,list_date,delist_date |
| `suspend_d` | trade_date,fields | 正式停复牌证据 | ts_code,trade_date,suspend_timing,suspend_type |
| `index_daily` | ts_code,start_date,end_date | 大盘指数日线 | ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount |
| `limit_list_d` | trade_date；本地按 U/D 类型过滤 | 正式涨跌停池 | ts_code,name,trade_date,limit,industry,close,pct_chg,amount,limit_amount,float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,open_times,up_stat,limit_times |
| `ths_index` | exchange="A",type=N/I/未指定 | 同花顺概念、行业目录 | ts_code,name,type（其余原始列缓存保留） |
| `ths_daily` | ts_code/trade_date/start_date/end_date | 同花顺板块日线 | ts_code,trade_date,open,high,low,close,pre_close,pct_change,vol,avg_price；部分字段由适配补充 |
| `ths_member` | ts_code；或 con_code 反查 | 板块成分及股票板块归属 | ts_code,con_code,con_name；项目也兼容 name/code 等别名 |
| `kpl_concept_cons` | con_code | 股票的开盘啦概念绑定 | 项目消费 ts_code,con_code,name 等代码名称字段；保留原表 |
| `limit_cpt_list` | trade_date | 最强板块统计 | ts_code/code,name 等板块身份；排名、涨幅、涨停数量等按返回表读取，无固定 fields 请求 |
| `moneyflow` | trade_date；或 ts_code/日期范围 | 全市场及个股资金流 | ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,net_mf_vol,net_mf_amount |
| `top_list` | trade_date | 龙虎榜个股汇总 | ts_code,name,trade_date,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate,amount_rate,float_values,reason |
| `top_inst` | trade_date；可选 ts_code | 龙虎榜席位/机构明细 | ts_code,trade_date,exalter,buy,sell,net_buy,buy_rate,sell_rate,side,reason |
| `hm_detail` | trade_date | 游资活动明细 | trade_date,ts_code,ts_name,buy_amount,sell_amount,net_amount,hm_name,hm_orgs,tag |
| `moneyflow_ths` | trade_date | 同花顺个股资金增强 | ts_code,name,trade_date,pct_change,latest,net_amount,net_d5_amount,buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate |
| `moneyflow_dc` | trade_date | 东方财富个股资金增强 | ts_code,name,trade_date,pct_change,close,net_amount,buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate |
| `moneyflow_cnt_ths` | trade_date | 同花顺板块资金增强 | ts_code,name,trade_date,lead_stock,pct_change,company_num,net_buy_amount,net_sell_amount,net_amount |
| `ths_hot` | trade_date,market="热股" | 同花顺关注度 | ts_code,ts_name/name,trade_date,data_type,rank,hot,pct_change,current_price,concept,rank_time,rank_reason（按存在列读取） |
| `dc_hot` | trade_date,market="A股市场" | 东方财富关注度 | ts_code,ts_name/name,trade_date,data_type,rank,hot,pct_change,current_price,concept,rank_time,rank_reason（按存在列读取） |
| `kpl_list` | trade_date | 涨停行为与竞价标签 | ts_code,name,trade_date,lu_time,open_time,last_time,lu_desc,tag,theme,status,bid_amount,bid_turnover,lu_bid_vol,pct_chg,bid_pct_chg,rt_pct_chg,limit_order |
| `margin_detail` | trade_date | 融资融券证据 | ts_code,trade_date,rzye,rqye,rzmre,rzche,rqyl,rqchl,rqmcl,rzrqye |
| `block_trade` | trade_date | 大宗交易事件 | ts_code,trade_date,price,vol,amount,buyer,seller |

**3. Tushare：按需、研究与兼容入口**

| 接口 | 参数 | 当前定位 | 字段边界 |
| --- | --- | --- | --- |
| `rt_k` | ts_code 通配符 | get_all_rt_k_data；当前独立实时主链使用 eltdx/Redis，不能当作同时在线主源 | 原始实时表；适配消费代码、价格、涨幅、成交与时间字段，未固定 fields |
| `limit_step` | trade_date,可选 ts_code | 连板天梯按需查询 | 原表缓存；代码、日期、连板阶梯等按业务消费，不存在单独固定 Silver 表 |
| `moneyflow_ind_dc` | trade_date,type=行业/概念 | 板块资金旧/按需入口 | 原始板块资金表，未纳入 _prefetch_short_signals 的固定八接口列表 |
| `moneyflow_hsgt` | trade_date 或 start_date/end_date | 北向资金工具 | trade_date,north_money 等按返回字段使用；未保证当前数据源持续提供 |
| `hsgt_top10` | trade_date,market_type | 互联互通成交榜工具 | 代码、名称、交易日期、排名、买卖与成交金额；原表返回 |
| `cyq_perf` | ts_code,trade_date 或日期范围 | 筹码研究工具 | 项目方法返回源表，未声明强制字段集；不能从现有源码保证供应商全部列 |
| `hm_list` | 无日期参数 | 游资名录长期缓存 | name,desc,orgs |

**4. 14 类 Silver 落地表的完整列集合**

以下直接依据 `core/etl/schemas.py`。字段名表示标准化契约，不保证每个接口都返回或填满所有字段。基础资料、停复牌、目录与成员关系还有各自缓存/证据文件，不都落在这些表中。

**`stock_daily_silver`（18 列）**

`trade_date, code, ts_code, name, exchange, open, high, low, close, pre_close, pct_chg, vol_hand, amount_yuan, circ_mv, total_mv, source, as_of_date, ingested_at`

**`sector_daily_silver`（16 列）**

`trade_date, sector_code, sector_name, sector_type, open, high, low, close, pre_close, pct_chg, vol_hand, amount_yuan, member_count, source, as_of_date, ingested_at`

**`index_daily_silver`（14 列）**

`trade_date, index_code, index_name, open, high, low, close, pre_close, pct_chg, vol_hand, amount_yuan, source, as_of_date, ingested_at`

**`limit_up_pool_silver`（16 列）**

`trade_date, code, ts_code, name, pct_chg, first_time, last_time, open_times, limit_times, fd_amount, float_mv, total_mv, turnover_ratio, source, as_of_date, ingested_at`

**`limit_down_pool_silver`（16 列）**

`trade_date, code, ts_code, name, pct_chg, first_time, last_time, open_times, limit_times, fd_amount, float_mv, total_mv, turnover_ratio, source, as_of_date, ingested_at`

**`lhb_daily_silver`（19 列）**

`trade_date, code, ts_code, name, close, pct_chg, turnover_rate, amount_yuan, listed_sell_yuan, listed_buy_yuan, listed_amount_yuan, net_buy_yuan, net_buy_rate, listed_amount_rate, float_mv_yuan, reason, source, as_of_date, ingested_at`

**`lhb_institution_silver`（16 列）**

`trade_date, code, ts_code, seat_name, seat_type, is_institution, buy_yuan, sell_yuan, net_buy_yuan, buy_rate, sell_rate, side, reason, source, as_of_date, ingested_at`

**`lhb_hot_money_silver`（13 列）**

`trade_date, code, ts_code, name, actor_name, seat_name, tag, buy_yuan, sell_yuan, net_buy_yuan, source, as_of_date, ingested_at`

**`stock_capital_flow_silver`（20 列）**

`trade_date, effective_date, code, ts_code, name, source, pct_chg, close, net_amount_yuan, net_5d_amount_yuan, large_net_yuan, large_net_rate, extra_large_net_yuan, extra_large_net_rate, medium_net_yuan, medium_net_rate, small_net_yuan, small_net_rate, as_of_date, ingested_at`

**`sector_capital_flow_silver`（13 列）**

`trade_date, effective_date, sector_code, sector_name, lead_stock, pct_chg, member_count, net_buy_yuan, net_sell_yuan, net_amount_yuan, source, as_of_date, ingested_at`

**`stock_attention_silver`（16 列）**

`trade_date, effective_date, code, ts_code, name, source, data_type, rank, hot, pct_chg, current_price, concept, rank_time, rank_reason, as_of_date, ingested_at`

**`stock_leader_signal_silver`（22 列）**

`trade_date, effective_date, code, ts_code, name, source, lu_time, open_time, last_time, lu_desc, tag, theme, status, bid_amount, bid_turnover, lu_bid_vol, pct_chg, bid_pct_chg, rt_pct_chg, limit_order, as_of_date, ingested_at`

**`stock_margin_silver`（15 列）**

`trade_date, effective_date, code, ts_code, rzye_yuan, rqye_yuan, rzmre_yuan, rzche_yuan, rqyl, rqchl, rqmcl, rzrqye_yuan, source, as_of_date, ingested_at`

**`stock_event_silver`（13 列）**

`trade_date, effective_date, code, ts_code, event_type, price, vol, amount_yuan, buyer, seller, source, as_of_date, ingested_at`

**5. 金额、时间与派生字段**

| 字段或来源 | 当前处理 |
| --- | --- |
| Tushare `daily/index_daily.amount` | 原始千元，标准化为 `amount_yuan` 时 ×1000 |
| `moneyflow_ths/dc` 的相关金额 | 相关 Silver 标准化按万元 ×10000 |
| `moneyflow_cnt_ths` 相关金额 | 板块资金标准化按亿元 ×100000000 |
| `block_trade.amount` | 相关事件 Silver 表按万元 ×10000 |
| `daily_basic.circ_mv/total_mv` | 原字段为万元口径；股票 Silver 中这两个无 `_yuan` 后缀字段直接转数值，不能误认已统一成元 |
| eltdx `total_hand` / 档位 `volume` | 手；新证据显式给出 `volume_hand` 和 `volume_shares=手数×100` |
| `pct_chg/change_pct` | 一般为百分数，例如 5 表示 5%；盘口失衡、买方向占比等是比值，0.5 表示 50% |
| THS 板块 `amount_yuan` | 源成交额缺失时，代码可能用 `vol × avg_price` 形成流动性代理；不应称为接口返回的真实成交额 |
| `as_of_date` | 数据对应/截至日期，不等于网络接收时间 |
| `ingested_at/received_at/captured_at` | 分别描述入库、接收、研究抓取时间 |

**6. eltdx：核心实时及历史适配接口**

| SDK 接口 | 参数 | 项目输出字段/用途 |
| --- | --- | --- |
| `quotes.get_snapshots(codes)` | 代码列表，当前按 80 只分块 | 最新行情，字段见下 |
| `minutes.today(code)` | 当前交易日 | 分时点标准化 |
| `minutes.history(code,date)` | 历史交易日 | 历史分时点标准化 |
| `bars.get(code,period,count/分页参数)` | 周期、条数；研究版还传 `adjust/all_pages/max_pages/bar_time` | 真正 K 线 OHLC、量额 |
| `auctions.series(code,date)` | 显式交易日 | 竞价过程快照；旧接口只有当日能力时不冒充历史 |
| `helpers.auction_data(...)` | `include_series=False,include_quote=False` | 兼容路径提取 `snapshot_0925` |
| `helpers.board_quotes(category)` | 类别，准备阶段使用 | 板块目录 `code,name,prepared_date` |
| `helpers.board_member_quotes(full_code)` | 带市场的板块代码 | `members,excluded_members,prepared_date` |
| `quotes.get_snapshots(board_codes)` | 冻结板块代码 | 板块行情快照；代码保留市场前缀 |

**报价标准化字段**

`code, ts_code, open_price, open_amount, pre_close, last_price, high_price, low_price, bid1, ask1, bid_vol1, ask_vol1, vol_hand, amount_yuan, current_hand, inside_hand, outside_hand, change_pct, date, time, source_time_raw, received_at, source, depth`

其中 `last_price←last_price`、`pre_close←pre_close_price`、`vol_hand←total_hand`、`amount_yuan←amount`、`open_amount←open_amount_yuan`。Redis/热快照层还会添加或筛选 `collector_id,schema_version,delta_volume,delta_amount,sample_interval_seconds,quality_ok` 等。标准化提供器字段不等于每个消费层都保留的字段：例如 `QuoteSnapshot` 投影会筛选部分原始字段。

`depth` 包含 `buy_levels,sell_levels,spread_yuan,bid_depth_hand,ask_depth_hand,imbalance,book_state,quality_status,quality_reasons,exchange_timestamp_verified`。每档为 `level,price,volume_hand,volume_shares`，最多五档。盘口证据默认关闭，开启后才随缓存/快照保留。

**分时点标准化字段**

`ts_code,date,time,datetime,open,high,low,close,avg_price,volume,volume_unit,amount_unit,amount_is_estimated,amount,pre_close,source`

当前分时适配的 `open/high/low/close` 均由该分时点 `price` 映射，`amount` 用 `price×volume×100` 估算。它们不等同于 `bars.get(period="1m")` 的真实一分钟 OHLC。

**K 线适配字段**

兼容适配：`ts_code,trade_date,datetime,open,high,low,close,vol,volume_unit,amount_unit,amount_is_estimated,amount,source`。

研究适配：`event_time,date,open,high,low,close,volume_hand,amount_yuan,period,bar_time,adjust`，分别保存 `none/qfq/hfq` 版本，分钟周期使用结束时间语义。

**7. eltdx：新增竞价、成交与盘口证据**

| SDK 接口 | 参数与范围 | 项目字段 |
| --- | --- | --- |
| `auctions.series(code,date)` | 当日/源可支持的历史日期 | `event_time,time_precision,price,matched_volume_hand,matched_amount_estimated_yuan,unmatched_volume_hand,unmatched_direction_raw,event_kind` |
| `trades.opening_match_today(code)` / `opening_match_history(code,date)` | 正式 09:25 撮合，限制分页预算 | 复用下面成交行字段，只接受 `opening_match` 且时间 09:25 |
| `trades.today(code,start,count)` / `trades.history(code,date,start,count)` | 当前/历史成交分页；项目当前不是直接无限量调用 `all_today/all_history` | 复用下面成交行字段 |
| `quotes.get_depth([code])` | 当前五档；历史明确不支持 | 复用 `depth` 字段组；优先复用报价中已有的五档 |

**成交行字段**

`event_time,time_precision,time,price,volume_hand,volume_shares,amount_yuan,amount_is_estimated,side,event_kind,status_raw,order_count,fingerprint`

验证分页连续性后才追加 `sequence,trade_id`。分页覆盖元数据为 `complete,pages,terminal_empty_page,overlap_verified,incremental,anchor,next_sequence,sequence_verified,rows_observed`。未核验序列不计入可信成交汇总。

成交明细时间精度保留为分钟；`side` 为数据商分类的 `buy/sell/unknown`。竞价过程 `auction_snapshot` 不当作真实成交；正式撮合 `opening_match` 单独保存。成交额来自价量乘积，显式标记为估算。

**证据公共封装**

`schema_version,source,sdk_version,kind,code,market_date,event_time,time_precision,received_at,published_at,collection_plan_id,capture_mode,quality_status,quality_reasons,coverage,rows,summary,summary_scope,formal_eligible`

存储层增加 `observation_id`。`capture_mode=live/backfill` 分开存储；发布完成后另记 `evidence_publication`，缓存中的 `published_at` 可为空，不能在完成发布前伪造完成时间。

`quality_status` 为 `ok/empty/partial/unsupported/network_error/parse_error/request_error`。新证据默认不改变正式策略。

**8. eltdx / F10：低频研究接口与字段**

| 研究 kind | 实际 SDK 方法 | 参数/输出字段 |
| --- | --- | --- |
| `bars_raw/bars_qfq/bars_hfq` | `bars.get` | `period,adjust,all_pages=True,max_pages,bar_time="end"`；按 start/end 过滤；输出见 K 线字段 |
| `corporate_actions` | `corporate.capital_changes(code)` | `exchange,market_id,code,reserved_7,date_raw,date,category_raw,category_name,c1_raw,c2_raw,c3_raw,c4_raw,c1_float,c2_float,c3_float,c4_float,c1_value,c2_value,c3_value,c4_value` |
| `adjustment_factors` | `corporate.adjustment_factors(code,anchor_date,start_date)` | `date,qfq_scale,qfq_offset,hfq_scale,hfq_offset` |
| `money_flow` | `money_flow.daily(code)` | `date_raw,date,total_amount,buckets,main_net,main_ratio,main_buy_net,main_buy_ratio,main_buy_super_large_net,main_buy_large_net,main_buy_medium_net,main_buy_small_net,main_super_large_net,main_large_net,main_medium_net,main_small_net` |
| `company_profile` | `F10Client.company_profile(code)` | 动态资料表，封装见下 |
| `announcements` | `F10Client.announcements(code)` | 动态公告表，封装见下 |
| `finance_report` | `F10Client.finance_report(code)` | 动态财报表，封装见下 |
| `topics` | `F10Client.hot_topics(code)` | 动态题材表，封装见下 |
| `security_status` | `helpers.latest_stock_list/latest_st/latest_suspended` | `code,name,listed,is_st,is_suspended,delisted,effective_date`；未找到证券时返回未知/空，不推断退市 |

权息 c1/c2/c3/c4 含义依 `category_raw/category_name` 而变，不能统一解释为固定财务指标。原始 bytes 会转为 null，`raw/raw_payload/record_hex/request_body` 等传输细节不进入业务版本行。

复权系数为仿射系数，表达式 `raw_price×scale+offset`，不是所有情况都只乘一个比例。保留起点和锚点，不保证与服务端复权完全一致。

F10 行封装固定为：`table_index,columns,values,published_at,available_at`。其中 `values` 的键来自源表，当前代码没有固定映射为“每家公司必有的同一套字段”。`published_at/available_at` 当前未验证时为 null；报告期不等于公告发布时间。

研究版本公共字段：`schema_version,source,sdk_version,code,kind,start,end,period,adjust,quality_status,quality_reasons,rows,coverage,historical_eligible,availability_basis,published_at,formal_eligible,research_only,captured_at`。版本库读取时增加 `version_id`。

低频自动抓取默认关闭；手工命令默认预演。`security_status` 和复权版本等可显式选择，并非所有 kind 都在默认盘后任务中调用。

**9. 腾讯 / 新浪历史 K 线兜底**

| 远端入口 | 当前参数 | 标准化字段 |
| --- | --- | --- |
| 腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | `param=代码,day/week/month,,结束日,数量,qfq` | 见下；日/周/月可能选择前复权返回项 |
| 腾讯 `ifzq.gtimg.cn/appstock/app/kline/mkline` | `param=代码,m周期,,数量` | 见下 |
| 新浪 `money.finance.sina.com.cn/.../CN_MarketData.getKLineData` | `symbol,scale,ma=no,datalen` | 源字段 day,open,close,high,low,volume |

项目统一输出：`trade_date,time,datetime,open,high,low,close,vol,volume,amount,ts_code,source`。此适配当前把 `amount` 设为 0，不能把它当成已取到的真实成交额；成交量原单位也不能仅凭 vol 列名推断。腾讯请求含 qfq，不应与正式不复权日线混算。

这些是独立历史补数/兼容入口，实时只读策略不会因缺数自行调用它们。

**10. 当前需要特别区分的字段含义**

1. `daily`、分时点 `minutes`、真实 K 线 `bars` 的价格结构不同，不能只看相同列名。
2. `amount_yuan` 名称不能消除“估算、代理、缺失填零”等差异，要结合 source/quality/amount_is_estimated。
3. 竞价拟撮合量、正式撮合量、日累计成交量分开使用。
4. 内外盘、买卖方向分类和资金流口径不等于真实投资者身份或意图。
5. Tushare 原表列、Silver 列、Redis 投影列、研究行的字段集合不同。
6. F10 实际字段需要在获准联网后读取真实返回表的 `columns/values` 才能补成逐栏目字典；本文没有虚构固定公司/财报字段。

**代码依据**

`core/data/data_prep.py`、`data_manager_*.py`、`lhb_data.py`、`core/etl/schemas.py`、`normalizers.py`；`core/data/providers/eltdx_provider.py`、`eltdx_evidence.py`、`eltdx_research.py`、`ashare_provider.py`；`core/realtime/market_evidence.py`、`listing_evidence.py`。
