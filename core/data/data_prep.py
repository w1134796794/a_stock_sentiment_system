"""数据预取阶段（Phase 1 数据解耦的取数入口）。

``DataPrep`` 在分析开始前，依据当日 universe（涨停池等）一次性把后续业务层所需的
数据批量拉齐，组装成 ``MarketDataset``。之后业务层只经 ``StockRepository`` 只读访问，
不再在计算过程中直连 ``DataManager``。

等价性原则（保证「只改取数位置、不改数据本身」）：
- 预取所用的 **code 形态** 与业务层查询时一致（沿用涨停池 '代码' 列的 6 位代码）；
- 预取 **窗口** 覆盖业务层会查询的日期区间；对窗口帧按单日切片，结果与
  ``dm.get_stock_daily(code, date, date)`` 等价（同源 Tushare 数据，仅缓存键不同）。
因此「数据集命中返回值 == dm 直连返回值」，迁移不引入行为变化。

健壮性：``build`` 全程 try/except，任何失败都返回**已部分填充或空**的数据集；
配合 Repository 非严格模式（未命中回退 dm），DataPrep 永远不会让流水线崩溃。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional

import loguru
import pandas as pd

from core.data.market_dataset import MarketDataset, call_key

logger = loguru.logger


def _norm_code(v: Any) -> str:
    """规范成 6 位代码字符串（与业务层 zfill(6) 调用一致）。"""
    s = str(v).strip()
    # 去掉可能的交易所后缀（.SH/.SZ），与业务层传 6 位代码保持一致
    if "." in s:
        s = s.split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _extract_codes(df: Optional[pd.DataFrame]) -> List[str]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    for col in ("代码", "code", "ts_code", "Code", "股票代码"):
        if col in df.columns:
            return [_norm_code(v) for v in df[col].tolist()]
    return []


class DataPrep:
    def __init__(self, data_manager: Any):
        self.dm = data_manager

    def build(self,
              trade_date: str,
              prev_trade_date: str = "",
              *,
              zt_pool: Optional[pd.DataFrame] = None,
              prev_zt_pool: Optional[pd.DataFrame] = None,
              extra_codes: Optional[Iterable[str]] = None,
              daily_lookback_calendar_days: int = 120,
              prefetch_limit_up: bool = True,
              limit_up_history_days: int = 16,
              prefetch_all_daily: bool = True,
              prefetch_auction: bool = False,
              prefetch_sectors: bool = True) -> MarketDataset:
        """构建当日只读数据集。

        预取域：
        - daily       ：universe 个股历史日线（默认开）
        - limit_up    ：近 N 交易日涨停池（默认开；只是把分析期的多次取数提前，已缓存→无额外成本）
        - all_daily   ：当日全市场日线（默认开）
        - auction     ：universe 集合竞价（默认关；逐股取数、eager 会增加运行耗时，需显式开启）

        universe = 今日涨停池 ∪ 昨日涨停池 ∪ extra_codes（均规范为 6 位代码）。
        所有域均按「键命中→用数据集 / 未命中→回退 dm」，因此预取范围不全也不影响正确性。
        """
        ds = MarketDataset(trade_date=str(trade_date), prev_trade_date=str(prev_trade_date))

        try:
            universe: List[str] = []
            universe += _extract_codes(zt_pool)
            universe += _extract_codes(prev_zt_pool)
            if extra_codes:
                universe += [_norm_code(c) for c in extra_codes]
            # 去重保序
            seen = set()
            universe = [c for c in universe if not (c in seen or seen.add(c))]

            ds.meta["universe_size"] = len(universe)

            # all_daily / limit_up / 板块列表 不依赖 universe，先做（即使 universe 为空也有价值）
            if prefetch_all_daily:
                self._prefetch_all_daily(ds, trade_date)
            if prefetch_limit_up:
                self._prefetch_limit_up(ds, trade_date, limit_up_history_days)
            if prefetch_sectors:
                self._prefetch_sectors(ds, trade_date, prev_trade_date)

            if not universe:
                logger.info("[DataPrep] universe 为空，跳过 daily/auction 预取")
                logger.info(f"[DataPrep] {ds.summary()}")
                return ds

            self._prefetch_daily(ds, universe, trade_date, daily_lookback_calendar_days)
            if prefetch_auction:
                self._prefetch_auction(ds, universe, trade_date)

        except Exception as e:  # noqa: BLE001  —— 预取永不致命
            import traceback
            logger.warning(f"[DataPrep] 预取过程异常（将回退 dm）：{e}")
            logger.debug(traceback.format_exc())

        logger.info(f"[DataPrep] {ds.summary()}")
        return ds

    # ------------------------------------------------------------------
    def _prefetch_daily(self, ds: MarketDataset, universe: List[str],
                        trade_date: str, lookback_cal_days: int) -> None:
        """批量预取 universe 的历史日线，窗口 [trade_date-N日, trade_date]。

        用 dm.get_stocks_daily_batch（与逐股 get_stock_daily 同源、同 code 形态），
        因此对窗口帧按单日切片 == dm.get_stock_daily(code, date, date)。
        """
        try:
            end = str(trade_date)
            start_dt = datetime.strptime(end, "%Y%m%d") - timedelta(days=int(lookback_cal_days))
            start = start_dt.strftime("%Y%m%d")
        except Exception:
            logger.warning(f"[DataPrep] 无法解析 trade_date={trade_date}，跳过 daily 预取")
            return

        if not hasattr(self.dm, "get_stocks_daily_batch"):
            logger.warning("[DataPrep] dm 无 get_stocks_daily_batch，跳过 daily 预取")
            return

        batch = self.dm.get_stocks_daily_batch(universe, start, end) or {}
        n_ok = 0
        for code in universe:
            df = batch.get(code)
            if df is None:
                # 批量结果可能用不同 code 形态作键，做一次规范化兜底匹配
                df = batch.get(_norm_code(code))
            if isinstance(df, pd.DataFrame):
                ds.put_daily(code, df)
                if not df.empty:
                    n_ok += 1
        ds.daily_start = start
        ds.daily_end = end
        ds.meta["daily_nonempty"] = n_ok
        logger.info(f"[DataPrep] daily 预取：{len(universe)} 只 universe，"
                    f"非空 {n_ok} 只，窗口 {start}~{end}")

    # ------------------------------------------------------------------
    def _prefetch_all_daily(self, ds: MarketDataset, trade_date: str) -> None:
        """预取当日全市场日线（单次接口调用）。"""
        if not hasattr(self.dm, "get_all_stocks_daily"):
            return
        try:
            df = self.dm.get_all_stocks_daily(str(trade_date))
            if isinstance(df, pd.DataFrame):
                ds.all_daily[str(trade_date)] = df
                ds.prefetched.add("all_daily")
                logger.info(f"[DataPrep] all_daily 预取：{len(df)} 行 @ {trade_date}")
        except Exception as e:
            logger.warning(f"[DataPrep] all_daily 预取失败（将回退 dm）：{e}")

    def _prefetch_limit_up(self, ds: MarketDataset, trade_date: str, history_days: int) -> None:
        """预取近 N 交易日涨停池（与分析期同一套交易日历，最大化命中）。"""
        if not hasattr(self.dm, "get_limit_up_pool"):
            return
        try:
            from core.utils.date_utils import get_last_n_trade_dates
            dates = get_last_n_trade_dates(int(history_days), str(trade_date)) or []
        except Exception:
            dates = [str(trade_date)]
        if str(trade_date) not in dates:
            dates = [str(trade_date)] + list(dates)
        n_ok = 0
        for d in dates:
            try:
                df = self.dm.get_limit_up_pool(d)
                if isinstance(df, pd.DataFrame):
                    ds.limit_up[d] = df
                    if not df.empty:
                        n_ok += 1
            except Exception as e:
                logger.debug(f"[DataPrep] limit_up {d} 预取失败（将回退 dm）：{e}")
        if ds.limit_up:
            ds.prefetched.add("limit_up")
        logger.info(f"[DataPrep] limit_up 预取：{len(ds.limit_up)} 个交易日，非空 {n_ok} 个")

    def _prefetch_sectors(self, ds: MarketDataset, trade_date: str, prev_trade_date: str) -> None:
        """预取 Layer2 板块域中「签名稳定、与个股无关」的批量调用，消除分析期回退告警。

        覆盖：
        - ths_index：概念(N)/行业(I)/全部 三类板块列表（业务层多处复用，各只调一次）
        - ths_daily：当日(及昨日)全板块指数行情（trade_date 维度的批量查询）
        - limit_cpt_list：当日最强板块统计
        - moneyflow_summary：当日(及昨日)全市场资金流汇总

        不预取逐板块的 ths_member / 单板块 ths_daily —— 其入参由运行时命中的热点板块决定，
        无法在预取阶段枚举，留给 Repository 的 _cached 记忆化（按需取一次、后续命中）。
        键由 ``call_key`` 生成，与 Repository 读取端共用，保证命中。
        """
        dm = self.dm

        def _try(domain: str, key: str, fetch) -> None:
            try:
                ds.put_call(key, fetch(), domain)
            except Exception as e:  # noqa: BLE001 —— 预取永不致命
                logger.debug(f"[DataPrep] {domain} 预取失败（将回退 dm）：{e}")

        if hasattr(dm, "get_ths_index"):
            for index_type in (None, "N", "I"):
                _try("ths_index", call_key("ths_index", index_type=index_type),
                     lambda it=index_type: dm.get_ths_index(index_type=it))

        dates = [d for d in (str(trade_date), str(prev_trade_date)) if d and d != "None"]
        if hasattr(dm, "get_ths_daily"):
            for d in dates:
                _try("ths_daily",
                     call_key("ths_daily", ts_code=None, trade_date=d, start_date=None, end_date=None),
                     lambda dd=d: dm.get_ths_daily(trade_date=dd))

        if hasattr(dm, "get_limit_cpt_list"):
            _try("limit_cpt_list", call_key("limit_cpt_list", trade_date=str(trade_date)),
                 lambda: dm.get_limit_cpt_list(str(trade_date)))

        if hasattr(dm, "get_moneyflow_summary"):
            for d in dates:
                _try("moneyflow_summary", call_key("moneyflow_summary", trade_date=d),
                     lambda dd=d: dm.get_moneyflow_summary(dd))

        prefetched = [k for k in ("ths_index", "ths_daily", "limit_cpt_list", "moneyflow_summary")
                      if k in ds.prefetched]
        logger.info(f"[DataPrep] 板块/资金流预取：{prefetched}（调用缓存 {len(ds.calls)} 条）")

    def _prefetch_auction(self, ds: MarketDataset, universe: List[str], trade_date: str) -> None:
        """预取 universe 当日集合竞价（逐股；默认关闭，开启会增加运行耗时）。"""
        if not hasattr(self.dm, "get_auction_data"):
            return
        n_ok = 0
        for code in universe:
            try:
                data = self.dm.get_auction_data(code, str(trade_date))
                # 即使为空也登记键：表示「已尝试预取」，命中后不再回退 dm（与 dm 行为一致）
                ds.put_auction(code, str(trade_date), data if data else {})
                if data:
                    n_ok += 1
            except Exception as e:
                logger.debug(f"[DataPrep] auction {code} 预取失败（将回退 dm）：{e}")
        logger.info(f"[DataPrep] auction 预取：{len(universe)} 只 universe，非空 {n_ok} 只 @ {trade_date}")