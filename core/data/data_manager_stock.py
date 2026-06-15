"""
个股数据管理模块 - 日线、分时、竞价、批量获取

数据来源：
- Tushare daily / daily_basic：盘后历史日线行情与基本面指标
- pqquotation / easyquotation：实时快照（可选，优先用于盘中轮询）
- eltdx：实时/历史分时、K 线、集合竞价
- AshareProvider：分钟线 / K 线兜底
"""
import json
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import loguru

from core.data.data_manager_base import DataManagerBase
from core.utils.stock_code_utils import StockCodeUtils

logger = loguru.logger


class StockDataManager(DataManagerBase):
    """个股数据管理器"""

    def _get_eltdx_provider(self):
        provider = getattr(self, "_eltdx_provider", None)
        if provider is not None:
            return provider
        try:
            from core.data.providers.eltdx_provider import EltdxProvider

            if not EltdxProvider.available():
                return None
            provider = EltdxProvider(timeout=3.0)
            self._eltdx_provider = provider
            return provider
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[StockDataManager] eltdx provider unavailable: {e}")
            return None

    def _get_quotation_provider(self):
        provider = getattr(self, "_quotation_provider", None)
        if provider is not None:
            return provider
        try:
            from core.data.providers.quotation_provider import QuotationProvider

            if not QuotationProvider.available():
                return None
            provider = QuotationProvider(source="sina")
            self._quotation_provider = provider
            return provider
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[StockDataManager] quotation provider unavailable: {e}")
            return None

    def _get_ashare_provider(self):
        provider = getattr(self, "_ashare_provider", None)
        if provider is not None:
            return provider
        try:
            from core.data.providers.ashare_provider import AshareProvider

            provider = AshareProvider()
            self._ashare_provider = provider
            return provider
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[StockDataManager] Ashare provider unavailable: {e}")
            return None

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取个股历史日线数据"""
        code = ts_code
        cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=['trade_date'])

        try:
            if self.ts_pro:
                # Tushare daily 必须传带交易所后缀的 ts_code（如 002119.SZ）；
                # 系统内部统一用 6 位代码，这里在调用边界补全后缀。
                ts_code_full = StockCodeUtils.standardize_code(code, add_suffix=True)
                df = self.ts_pro.daily(ts_code=ts_code_full, start_date=start_date, end_date=end_date)
                if not df.empty:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    df.to_csv(cache_file, index=False)
                    return df
                else:
                    logger.debug(f"[get_stock_daily] Tushare返回空数据")
            else:
                logger.debug(f"[get_stock_daily] Tushare未初始化")
        except Exception as e:
            logger.error(f"[get_stock_daily] 获取个股{code}历史数据失败: {e}")

        # 盘中实时兜底：Tushare daily 仅在收盘后才有当日K线，盘中查询「当日」可能返回空。
        # 此时用实时行情快照拼出当日动态日线（不落盘，收盘后由 Tushare 真实日线覆盖）。
        rt_df = self._get_realtime_daily_via_quote(code, start_date, end_date)
        if rt_df is not None and not rt_df.empty:
            return rt_df
        return pd.DataFrame()

    def _get_realtime_daily_via_quote(self, ts_code: str, start_date: str, end_date: str):
        """盘中用实时行情快照拼出「当日」日线。

        仅当查询的是「当前交易日」的单日（start==end==今天，且今天是交易日）时生效；
        其余情况返回 None（继续走 Tushare 历史日线）。返回的当日行情是动态的，
        故**不写缓存**，避免收盘后污染真实日线。
        """
        from core.utils.date_utils import get_nearest_trade_date, get_today_str
        today = get_today_str()
        if not (str(start_date) == str(end_date) == today == get_nearest_trade_date(today)):
            return None

        try:
            q = self.get_quote_snapshot(ts_code) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[get_stock_daily] 实时快照失败 {ts_code}: {e}")
            return None

        last = float(q.get('last_price') or 0)
        open_p = float(q.get('open_price') or 0)
        pre_close = float(q.get('pre_close') or 0)
        if last <= 0 or open_p <= 0:
            return None

        vol_hand = float(q.get('vol_hand') or 0)
        amount_yuan = float(q.get('amount_yuan') or 0)
        change = last - pre_close
        pct_chg = (change / pre_close * 100.0) if pre_close > 0 else 0.0
        row = {
            'ts_code': ts_code,
            'trade_date': pd.to_datetime(today),
            'open': open_p,
            'high': float(q.get('high_price') or last),
            'low': float(q.get('low_price') or last),
            'close': last,                       # 盘中以现价作为「当前收盘」
            'pre_close': pre_close,
            'change': change,
            'pct_chg': pct_chg,
            'vol': vol_hand,                     # 手，与 Tushare vol 同口径
            'amount': amount_yuan / 1000.0,      # 千元，与 Tushare amount 同口径
        }
        logger.info(
            f"[get_stock_daily] 实时快照拼当日日线 {ts_code} {today}: "
            f"source={q.get('source', '')} close={last} "
            f"vol={vol_hand:.0f}手 amount={amount_yuan / 1e8:.2f}亿"
        )
        return pd.DataFrame([row])

    def get_stock_daily_price(self, ts_code: str, trade_date: str) -> Dict:
        """获取个股某日的开盘价、收盘价、昨收价"""
        df = self.get_stock_daily(ts_code, trade_date, trade_date)
        if df.empty:
            return {}

        row = df.iloc[0]
        return {
            'open': float(row.get('open', 0)),
            'close': float(row.get('close', 0)),
            'pre_close': float(row.get('pre_close', 0))
        }

    def get_stock_daily_data(self, ts_code: str, trade_date: str) -> Dict:
        """获取股票日行情完整数据"""
        df = self.get_stock_daily(ts_code, trade_date, trade_date)
        if df.empty:
            return {}

        row = df.iloc[0]
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'open': float(row.get('open', 0)),
            'high': float(row.get('high', 0)),
            'low': float(row.get('low', 0)),
            'close': float(row.get('close', 0)),
            'pre_close': float(row.get('pre_close', 0)),
            'change': float(row.get('change', 0)),
            'pct_chg': float(row.get('pct_chg', 0)),
            'vol': float(row.get('vol', 0)),
            'amount': float(row.get('amount', 0))
        }

    def get_all_stocks_daily(self, trade_date: str) -> pd.DataFrame:
        """
        获取全市场所有股票日线行情（Tushare daily 接口，仅传 trade_date）

        返回字段：ts_code, trade_date, open, high, low, close, pre_close,
                  change, pct_chg, vol, amount

        用途：
        - 统计全市场涨跌家数（市场宽度）
        - 统计全市场总成交额
        - 筛选满足量价条件的个股

        Args:
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            DataFrame: 全市场个股日线数据
        """
        cache_file = self.stock_dir / "all_daily" / f"{trade_date}.csv"

        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                if not df.empty:
                    return df
            except Exception:
                pass

        if not self.ts_pro:
            logger.warning("[get_all_stocks_daily] Tushare未初始化")
            return pd.DataFrame()

        try:
            df = self.ts_pro.daily(trade_date=trade_date)
            if df is None or df.empty:
                logger.warning(f"[get_all_stocks_daily] {trade_date} 无数据")
                return pd.DataFrame()

            df.to_csv(cache_file, index=False)
            logger.info(f"[get_all_stocks_daily] {trade_date} 获取 {len(df)} 只股票日线数据")
            return df
        except Exception as e:
            logger.error(f"[get_all_stocks_daily] {trade_date} 获取失败: {e}")
            return pd.DataFrame()

    def get_stock_daily_basic(self, ts_code: str, trade_date: str) -> Dict:
        """获取股票每日基本面指标（换手率、流通股本等）"""
        df = self.get_daily_basic(trade_date)
        if df.empty:
            return {}

        stock_data = df[df['ts_code'] == ts_code]
        if stock_data.empty:
            return {}

        row = stock_data.iloc[0]
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'close': float(row.get('close', 0)),
            'turnover_rate': float(row.get('turnover_rate', 0)),
            'turnover_rate_f': float(row.get('turnover_rate_f', 0)),
            'float_share': float(row.get('float_share', 0)),
            'free_share': float(row.get('free_share', 0)),
            'total_share': float(row.get('total_share', 0)),
            'circ_mv': float(row.get('circ_mv', 0)),
            'total_mv': float(row.get('total_mv', 0)),
        }

    def get_stock_tick(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """获取个股分时数据（1分钟线）。

        优先使用 eltdx：当日走实时分时，历史交易日走 ``get_history_minute``。
        eltdx 不可用时，用 AshareProvider 公共接口兜底。
        """
        cache_file = self.stock_dir / "tick" / f"{ts_code}_{trade_date}.csv"

        if cache_file.exists():
            try:
                cached = pd.read_csv(cache_file)
                # 仅当缓存是“完整分时序列”时采用；历史遗留的单根收盘快照(<=1行)忽略并重取，自动修复
                if cached is not None and len(cached) > 1:
                    return cached
            except Exception as e:
                logger.warning(f"读取缓存分时数据失败: {e}")

        eltdx_provider = self._get_eltdx_provider()
        if eltdx_provider is not None:
            df = eltdx_provider.get_minute_bars(ts_code, trade_date)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_stock_tick] eltdx 获取 {ts_code} {trade_date} 分时: {len(df)}条")
                return df

        ashare_provider = self._get_ashare_provider()
        if ashare_provider is not None:
            df = ashare_provider.get_minute_bars(ts_code, trade_date)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_stock_tick] Ashare 获取 {ts_code} {trade_date} 分时: {len(df)}条")
                return df

        return pd.DataFrame()

    @staticmethod
    def _auction_vol_hand(amount, price, raw_volume=None) -> float:
        """集合竞价成交量统一归一为「手」(1手=100股)。

        优先用 撮合额÷价格÷100（与成交额自洽、单位确定）；额或价缺失时退回
        原始 volume（来源单位未知，仅兜底）。这样竞价量与日线成交量(vol, 手)同口径，
        放量/缩量量比 = 竞价量(手) ÷ 昨日全天量(手) 才有意义。
        """
        try:
            a = float(amount or 0)
            p = float(price or 0)
            if a > 0 and p > 0:
                return round(a / p / 100.0, 0)
        except (TypeError, ValueError):
            pass
        try:
            return float(raw_volume or 0)
        except (TypeError, ValueError):
            return 0.0

    def _is_latest_session(self, trade_date: str) -> bool:
        """trade_date 是否为「最近一个交易日」（行情快照/当日竞价序列仅对此日有效）。"""
        try:
            from core.utils.date_utils import get_nearest_trade_date, get_today_str
            return str(trade_date) == get_nearest_trade_date(get_today_str())
        except Exception:  # noqa: BLE001
            return False

    def get_auction_data(self, ts_code: str, trade_date: str) -> Dict:
        """获取个股竞价数据（集合竞价）。

        数据源优先级（真实 → 兜底）：
          eltdx 09:25 历史撮合快照 → eltdx 当日竞价序列(仅最近交易日)
          → eltdx 行情快照开盘竞价额(仅最近交易日) → 分时 09:25 → 日线开盘价兜底。
        """
        cache_file = self.stock_dir / "auction" / f"{ts_code}_{trade_date}.json"

        # 仅信任「真实/精确」来源的缓存；兜底或不可靠来源(日线兜底、过期的当日竞价序列)
        # 一律忽略并重取，装好 eltdx 后自动自愈。
        trusted_sources = {'eltdx', 'eltdx_0925', 'eltdx_open', 'minute_tick'}
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if cached and cached.get('数据源') in trusted_sources:
                    return cached
            except Exception as e:
                logger.warning(f"读取竞价数据缓存失败: {e}")

        try:
            eltdx_provider = self._get_eltdx_provider()
            if eltdx_provider is not None:
                # (a) 09:25 历史撮合快照：按日期精确扫描历史逐笔，适用于任意交易日
                snapshot_0925 = eltdx_provider.get_auction_0925(ts_code, trade_date)
                if snapshot_0925:
                    s_price = float(snapshot_0925.get('price') or 0)
                    s_amount = float(snapshot_0925.get('amount') or 0)
                    result = {
                        '开盘价': s_price,
                        # 统一为「手」，与日线成交量(vol, 手)同口径，供放量/缩量量比计算
                        '竞价成交量': self._auction_vol_hand(s_amount, s_price, snapshot_0925.get('volume')),
                        '竞价成交额': s_amount,
                        '价格趋势': [],
                        '竞价方向': snapshot_0925.get('side'),
                        '数据源': 'eltdx',
                    }
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False)
                    return result

                # (b) 行情快照开盘竞价额：open_amount 即开盘集合竞价撮合额（仅最近交易日）。
                # 注：当日竞价序列接口(get_call_auction)不带日期、仅在 09:15–09:25 实时有效，
                # 盘后/历史会返回过期数据，故复盘场景不采用。
                if self._is_latest_session(trade_date):
                    quote = eltdx_provider.get_quote_snapshot(ts_code)
                    price = float(quote.get('open_price') or 0) if quote else 0.0
                    amount = float(quote.get('open_amount') or 0) if quote else 0.0
                    if price > 0 and amount > 0:
                        result = {
                            '开盘价': price,
                            # 统一为「手」(撮合额÷开盘价÷100)，与日线成交量同口径
                            '竞价成交量': self._auction_vol_hand(amount, price),
                            '竞价成交额': amount,
                            '价格趋势': [],
                            '数据源': 'eltdx_open',
                            '说明': '集合竞价撮合额取自最新行情快照(open_amount)，成交量=撮合额÷开盘价÷100(手)。',
                        }
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            json.dump(result, f, ensure_ascii=False)
                        return result

            tick_df = self.get_stock_tick(ts_code, trade_date)
            if not tick_df.empty and 'time' in tick_df.columns:
                auction_data = tick_df[tick_df['time'].astype(str) == '09:25:00']
                if not auction_data.empty:
                    auction_row = auction_data.iloc[0]
                    morning_ticks = tick_df[
                        (tick_df['time'].astype(str) >= '09:15:00') &
                        (tick_df['time'].astype(str) <= '09:25:00')
                    ]
                    price_trend = [float(x) for x in morning_ticks['close'].dropna().tolist()] if not morning_ticks.empty else []
                    result = {
                        '开盘价': float(auction_row.get('close') or 0),
                        '竞价成交量': float(auction_row.get('volume') or auction_row.get('vol') or 0),
                        '竞价成交额': float(auction_row.get('amount') or 0),
                        '价格趋势': price_trend,
                        '数据源': 'minute_tick',
                    }
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False)
                    return result

            daily_price = self.get_stock_daily_price(ts_code, trade_date)
            if daily_price and daily_price.get('open', 0) > 0:
                result = {
                    '开盘价': float(daily_price['open']),
                    '竞价成交量': 0,
                    '竞价成交额': 0,
                    '价格趋势': [],
                    '数据源': 'daily_open_fallback',
                    '说明': '仅取到日线开盘价；该交易日 TDX 历史逐笔无 09:25 撮合记录，集合竞价成交量/额暂缺。',
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False)
                return result

        except Exception as e:
            logger.error(f"[get_auction_data] 获取竞价数据失败 {ts_code} {trade_date}: {e}")
            return {}

    def get_auction_series(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """获取集合竞价序列，用于识别抢筹/砸盘/平稳图形。"""
        cache_file = self.stock_dir / "auction" / f"{ts_code}_{trade_date}_series.csv"
        if cache_file.exists():
            try:
                return pd.read_csv(cache_file)
            except Exception:
                pass

        eltdx_provider = self._get_eltdx_provider()
        if eltdx_provider is None:
            return pd.DataFrame()
        df = eltdx_provider.get_auction_series(ts_code, trade_date)
        if df is not None and not df.empty:
            df.to_csv(cache_file, index=False)
            return df
        return pd.DataFrame()

    def get_auction_0925(self, ts_code: str, trade_date: str) -> Dict:
        """获取 09:25 集合竞价最终撮合快照。"""
        data = self.get_auction_data(ts_code, trade_date)
        if not data:
            return {}
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'time': '09:25:00',
            'price': data.get('开盘价'),
            'volume': data.get('竞价成交量'),
            'amount': data.get('竞价成交额'),
            'source': data.get('数据源'),
        }

    def get_quote_snapshot(self, ts_code: str) -> Dict:
        """获取个股最新实时行情快照。

        仅含 ``last_price`` / ``open_price`` / ``pre_close`` 等盘中实时字段，**不落盘**
        （快照随行情变化）。优先 pqquotation/easyquotation，失败再回退 eltdx。

        主要服务盘中实时观测（如弱转强走弱池的盘中转强监控）。
        """
        provider = self._get_quotation_provider()
        if provider is not None:
            try:
                quote = provider.get_quote_snapshot(ts_code) or {}
                if quote:
                    return quote
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[get_quote_snapshot] quotation 实时快照失败 {ts_code}: {e}")

        provider = self._get_eltdx_provider()
        if provider is None:
            return {}
        try:
            return provider.get_quote_snapshot(ts_code) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[get_quote_snapshot] eltdx 实时快照失败 {ts_code}: {e}")
            return {}

    def get_quote_snapshots(self, ts_codes) -> Dict[str, Dict]:
        """**批量**获取多只实时行情快照。

        返回 ``{6位代码: 快照dict}``。优先 pqquotation/easyquotation 批量 HTTP，
        失败再回退 eltdx，适合走弱池/候选池盘中轮询。
        """
        provider = self._get_quotation_provider()
        if provider is not None:
            try:
                quotes = provider.get_quote_snapshots(ts_codes) or {}
                if quotes:
                    return quotes
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[get_quote_snapshots] quotation 批量快照失败: {e}")

        provider = self._get_eltdx_provider()
        if provider is None:
            return {}
        try:
            return provider.get_quote_snapshots(ts_codes) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[get_quote_snapshots] eltdx 批量快照失败: {e}")
            return {}

    def get_minute_bars_live(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """获取个股**实时分时**（eltdx，不落盘）。

        与 ``get_stock_tick`` 不同：本方法**不读写 CSV 缓存**，每次都取最新分时，
        避免盘中把「半截分时序列」缓存后读到过期数据。专供盘中实时形态判定。
        """
        provider = self._get_eltdx_provider()
        if provider is None:
            return pd.DataFrame()
        try:
            df = provider.get_minute_bars(ts_code, trade_date)
            return df if df is not None else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[get_minute_bars_live] eltdx 实时分时失败 {ts_code} {trade_date}: {e}")
            return pd.DataFrame()

    def get_kline(self, ts_code: str, period: str = "day", count: int = 120) -> pd.DataFrame:
        """获取个股 K 线：eltdx 优先，AshareProvider 兜底。

        盘后批量分析仍使用 Tushare 的 ``get_stock_daily`` / ``get_all_stocks_daily``；
        本方法主要服务前端图表与无 token 数据展示。
        """
        cache_file = self.stock_dir / "kline" / f"{ts_code}_{period}_{count}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            try:
                return pd.read_csv(cache_file)
            except Exception:
                pass

        eltdx_provider = self._get_eltdx_provider()
        if eltdx_provider is not None:
            df = eltdx_provider.get_kline(ts_code, period=period, count=count)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                return df

        ashare_provider = self._get_ashare_provider()
        if ashare_provider is not None:
            df = ashare_provider.get_kline(ts_code, period=period, count=count)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                return df

        return pd.DataFrame()

    def get_stocks_daily_batch(self,
                               ts_codes: List[str],
                               start_date: str,
                               end_date: str) -> Dict[str, pd.DataFrame]:
        """批量获取多只股票的历史日线数据"""
        results = {}
        codes_to_fetch = []

        for code in ts_codes:
            cache_key = f"stock_daily_{code}_{start_date}_{end_date}"
            cached_data = self._get_from_memory_cache(cache_key)
            if cached_data is not None:
                results[code] = cached_data
                continue

            cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
            if cache_file.exists():
                try:
                    df = pd.read_csv(cache_file, parse_dates=['trade_date'])
                    results[code] = df
                    self._set_memory_cache(cache_key, df)
                    continue
                except Exception as e:
                    logger.debug(f"[批量获取] 读取缓存失败 {code}: {e}")

            codes_to_fetch.append(code)

        if codes_to_fetch and self.ts_pro:
            logger.info(f"[批量获取] 需要获取 {len(codes_to_fetch)} 只股票数据 (并发)")

            def _fetch_one(code: str):
                try:
                    # 内部用 6 位代码做键/缓存名，但 Tushare 需要带后缀的 ts_code
                    ts_code_full = StockCodeUtils.standardize_code(code, add_suffix=True)
                    df = self.ts_pro.daily(ts_code=ts_code_full, start_date=start_date, end_date=end_date)
                    if df is None or df.empty:
                        logger.warning(f"[批量获取] {code} 返回空数据")
                        return code, pd.DataFrame()
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
                    df.to_csv(cache_file, index=False)
                    return code, df
                except Exception as e:
                    logger.error(f"[批量获取] 获取 {code} 失败: {e}")
                    return code, pd.DataFrame()

            # P3-8：并发拉取（默认 4 worker，避免 Tushare 限流）
            from core.utils.parallel import parallel_map
            fetched = parallel_map(_fetch_one, codes_to_fetch, max_workers=4)
            for entry in fetched:
                if entry is None:
                    continue
                code, df = entry
                if not df.empty:
                    cache_key = f"stock_daily_{code}_{start_date}_{end_date}"
                    self._set_memory_cache(cache_key, df)
                    results[code] = df

        logger.info(f"[批量获取] 完成: {len(results)}/{len(ts_codes)} 只股票")
        return results
