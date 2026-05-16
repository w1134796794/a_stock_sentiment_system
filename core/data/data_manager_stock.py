"""
个股数据管理模块 - 日线、分时、竞价、批量获取

数据来源：Tushare
- daily: 个股日线行情
- daily_basic: 个股基本面指标
- rt_min: 分时数据（1分钟线）
"""
import json
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import loguru

from core.data.data_manager_base import DataManagerBase

logger = loguru.logger


class StockDataManager(DataManagerBase):
    """个股数据管理器"""

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取个股历史日线数据"""
        code = ts_code
        cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=['trade_date'])

        try:
            if self.ts_pro:
                df = self.ts_pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
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
        return pd.DataFrame()

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
        """获取个股分时数据（1分钟线，使用Tushare rt_min接口）"""
        cache_file = self.stock_dir / "tick" / f"{ts_code}_{trade_date}.csv"

        if cache_file.exists():
            try:
                return pd.read_csv(cache_file)
            except Exception as e:
                logger.warning(f"读取缓存分时数据失败: {e}")

        if self.ts_pro:
            try:
                code = ts_code
                df = self.ts_pro.rt_min(ts_code=ts_code, freq='1MIN')

                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    logger.warning(f"[get_stock_tick] Tushare rt_min返回空数据: {code}")
                    return pd.DataFrame()

                required_cols = ['time', 'open', 'close', 'high', 'low', 'vol', 'amount']
                missing_cols = [col for col in required_cols if col not in df.columns]
                if missing_cols:
                    logger.warning(f"[get_stock_tick] 缺少列: {missing_cols}")
                    return pd.DataFrame()

                df = df.rename(columns={'vol': 'volume'})
                df['date'] = df['time'].str[:10]
                df['time'] = df['time'].str[11:19]

                target_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                df = df[df['date'] == target_date]

                if not df.empty:
                    df.to_csv(cache_file, index=False)

                return df

            except Exception as e:
                logger.error(f"[get_stock_tick] Tushare rt_min获取失败 {code}: {e}")

        return pd.DataFrame()

    def get_auction_data(self, ts_code: str, trade_date: str) -> Dict:
        """获取个股竞价数据（集合竞价）"""
        cache_file = self.stock_dir / "auction" / f"{ts_code}_{trade_date}.json"

        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取竞价数据缓存失败: {e}")

        try:
            daily_price = self.get_stock_daily_price(ts_code, trade_date)
            if daily_price and daily_price.get('open', 0) > 0:
                open_price = daily_price['open']
                result = {
                    '开盘价': float(open_price),
                    '竞价成交量': 0,
                    '竞价成交额': 0,
                    '价格趋势': []
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False)
                return result

            tick_df = self.get_stock_tick(ts_code, trade_date)
            if tick_df.empty:
                return {}

            auction_data = tick_df[tick_df['time'] == '09:25:00']
            if auction_data.empty:
                first_tick = tick_df[tick_df['time'] >= '09:30:00'].iloc[0] if not tick_df.empty else None
                if first_tick is not None:
                    result = {
                        '开盘价': float(first_tick['open']),
                        '竞价成交量': 0,
                        '竞价成交额': 0,
                        '价格趋势': []
                    }
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False)
                    return result
                return {}

            auction_row = auction_data.iloc[0]
            morning_ticks = tick_df[
                (tick_df['time'] >= '09:15:00') &
                (tick_df['time'] <= '09:25:00')
            ]
            price_trend = morning_ticks['close'].tolist() if not morning_ticks.empty else []

            result = {
                '开盘价': float(auction_row['close']),
                '竞价成交量': float(auction_row['volume']),
                '竞价成交额': float(auction_row['amount']),
                '价格趋势': price_trend
            }

            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False)
            return result

        except Exception as e:
            logger.error(f"[get_auction_data] 获取竞价数据失败 {ts_code} {trade_date}: {e}")
            return {}

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
            logger.info(f"[批量获取] 需要获取 {len(codes_to_fetch)} 只股票数据")
            for code in codes_to_fetch:
                try:
                    df = self.ts_pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                    if not df.empty:
                        df['trade_date'] = pd.to_datetime(df['trade_date'])
                        cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
                        df.to_csv(cache_file, index=False)
                        cache_key = f"stock_daily_{code}_{start_date}_{end_date}"
                        self._set_memory_cache(cache_key, df)
                        results[code] = df
                    else:
                        logger.warning(f"[批量获取] {code} 返回空数据")
                except Exception as e:
                    logger.error(f"[批量获取] 获取 {code} 失败: {e}")
                time.sleep(0.1)

        logger.info(f"[批量获取] 完成: {len(results)}/{len(ts_codes)} 只股票")
        return results