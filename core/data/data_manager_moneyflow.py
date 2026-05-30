"""
资金流向 / 龙虎榜 / 北向 / 筹码数据管理 Mixin

提供以下接口：
1. 个股资金流向 (moneyflow)
2. 龙虎榜数据 (top_list / top_inst)
3. 北向资金流向 (moneyflow_hsgt / hsgt_top10)
4. 筹码结构数据 (cyq_perf)

设计原则：
- 作为 Mixin 直接合并到 DataManager，避免 `self.dm.xxx` 间接调用与 `hasattr` 探测
- 复用 DataManagerBase 的 cache_dir / ts_pro / 缓存目录
"""
from typing import Dict, List

import numpy as np
import pandas as pd
import loguru

logger = loguru.logger


class MoneyflowDataManager:
    """资金流向 / 龙虎榜 / 北向 / 筹码 数据管理 Mixin"""

    # =========================================================================
    # 1. 个股资金流向 (moneyflow)
    # =========================================================================

    def get_stock_moneyflow(self, ts_code: str, trade_date: str = None,
                            start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """获取个股资金流向数据 (Tushare moneyflow)"""
        cache_file = self.cache_dir / "moneyflow" / "stock" / f"{ts_code}_{trade_date or start_date}_{end_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)

        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            if trade_date:
                df = self.ts_pro.moneyflow(ts_code=ts_code, trade_date=trade_date)
            else:
                df = self.ts_pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)

            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_stock_moneyflow] {ts_code} 资金流向: {len(df)}条")

            return df
        except Exception as e:
            logger.error(f"[get_stock_moneyflow] 获取失败 {ts_code}: {e}")
            return pd.DataFrame()

    def get_moneyflow(self, ts_code: str, trade_date: str = None,
                      lookback_days: int = None,
                      start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        get_stock_moneyflow 的便捷别名，兼容多种调用约定：
            - get_moneyflow(code, trade_date)
            - get_moneyflow(code, lookback_days=5)
            - get_moneyflow(code, start_date=..., end_date=...)
        """
        if lookback_days and lookback_days > 0:
            try:
                if hasattr(self, "date_utils"):
                    dates = self.date_utils.get_last_n_trade_dates(lookback_days, trade_date)
                    if dates:
                        start_date = dates[-1]
                        end_date = dates[0]
            except Exception:
                pass

        return self.get_stock_moneyflow(
            ts_code=ts_code,
            trade_date=trade_date if not (start_date or end_date) else None,
            start_date=start_date,
            end_date=end_date,
        )

    def get_moneyflow_summary(self, trade_date: str) -> pd.DataFrame:
        """获取当日全市场资金流向汇总"""
        cache_file = self.cache_dir / "moneyflow" / f"summary_{trade_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            df = self.ts_pro.moneyflow(trade_date=trade_date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_moneyflow_summary] {trade_date} 全市场资金流向: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_moneyflow_summary] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    # =========================================================================
    # 2. 龙虎榜数据 (top_list / top_inst)
    # =========================================================================

    def get_top_list(self, trade_date: str) -> pd.DataFrame:
        """获取龙虎榜每日明细 (Tushare top_list)"""
        cache_file = self.cache_dir / "top_list" / f"top_list_{trade_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            df = self.ts_pro.top_list(trade_date=trade_date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_top_list] {trade_date} 龙虎榜: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_top_list] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    def get_top_inst(self, trade_date: str, ts_code: str = None) -> pd.DataFrame:
        """获取龙虎榜机构交易明细 (Tushare top_inst)"""
        cache_file = self.cache_dir / "top_list" / f"top_inst_{trade_date}_{ts_code or 'all'}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            if ts_code:
                df = self.ts_pro.top_inst(trade_date=trade_date, ts_code=ts_code)
            else:
                df = self.ts_pro.top_inst(trade_date=trade_date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_top_inst] {trade_date} 机构交易: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_top_inst] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    def analyze_top_list_summary(self, trade_date: str) -> Dict:
        """分析龙虎榜汇总数据"""
        top_list = self.get_top_list(trade_date)
        top_inst = self.get_top_inst(trade_date)

        if top_list.empty:
            return {}

        summary = {
            'total_stocks': len(top_list),
            'total_buy': top_list['l_buy'].sum() if 'l_buy' in top_list.columns else 0,
            'total_sell': top_list['l_sell'].sum() if 'l_sell' in top_list.columns else 0,
            'net_amount': top_list['net_amount'].sum() if 'net_amount' in top_list.columns else 0,
        }

        if 'l_buy' in top_list.columns:
            top_buy = top_list.nlargest(5, 'l_buy')[['ts_code', 'name', 'l_buy', 'net_amount']]
            summary['top_buy_stocks'] = top_buy.to_dict('records')

        if 'l_sell' in top_list.columns:
            top_sell = top_list.nlargest(5, 'l_sell')[['ts_code', 'name', 'l_sell', 'net_amount']]
            summary['top_sell_stocks'] = top_sell.to_dict('records')

        if not top_inst.empty and 'net_buy' in top_inst.columns:
            inst_net_buy = top_inst['net_buy'].sum()
            summary['institution_net_buy'] = inst_net_buy
            summary['institution_activity'] = '活跃' if inst_net_buy > 100000000 else '一般'

        return summary

    # =========================================================================
    # 3. 北向资金流向 (moneyflow_hsgt)
    # =========================================================================

    def get_hsgt_moneyflow(self, trade_date: str = None,
                           start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """获取沪深港通资金流向 (Tushare moneyflow_hsgt)"""
        cache_file = self.cache_dir / "moneyflow" / "hsgt" / f"hsgt_{trade_date or start_date}_{end_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            if trade_date:
                df = self.ts_pro.moneyflow_hsgt(trade_date=trade_date)
            else:
                df = self.ts_pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hsgt_moneyflow] 北向资金: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_hsgt_moneyflow] 获取失败: {e}")
            return pd.DataFrame()

    def get_moneyflow_hsgt(self, trade_date: str = None,
                           start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """get_hsgt_moneyflow 的兼容别名"""
        return self.get_hsgt_moneyflow(trade_date=trade_date,
                                       start_date=start_date, end_date=end_date)

    def get_hsgt_top10(self, trade_date: str, market_type: str = '1') -> pd.DataFrame:
        """获取沪深股通十大成交股 (Tushare hsgt_top10)"""
        cache_file = self.cache_dir / "moneyflow" / "hsgt" / f"top10_{market_type}_{trade_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            df = self.ts_pro.hsgt_top10(trade_date=trade_date, market_type=market_type)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hsgt_top10] 十大成交股: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_hsgt_top10] 获取失败: {e}")
            return pd.DataFrame()

    def analyze_hsgt_trend(self, trade_date: str, days: int = 5) -> Dict:
        """分析北向资金趋势"""
        try:
            date_list = self.date_utils.get_last_n_trade_dates(days, trade_date)
        except Exception:
            from core.utils import DateUtils
            date_list = DateUtils().get_last_n_trade_dates(days, trade_date)

        if len(date_list) < days:
            logger.warning(f"[analyze_hsgt_trend] 历史数据不足 {len(date_list)}/{days}")

        flows = []
        for date in date_list:
            df = self.get_hsgt_moneyflow(trade_date=date)
            if not df.empty and 'north_money' in df.columns:
                flows.append({
                    'date': date,
                    'north_money': df['north_money'].iloc[0]
                })

        if not flows:
            return {}

        north_moneys = [f['north_money'] for f in flows]

        return {
            'current': north_moneys[0] if north_moneys else 0,
            'avg_5d': np.mean(north_moneys),
            'trend': '流入' if north_moneys[0] > 0 else '流出',
            'continuous_days': self._count_continuous_flow_days(north_moneys),
            'total_flow_5d': sum(north_moneys),
        }

    @staticmethod
    def _count_continuous_flow_days(values: List[float]) -> int:
        """计算连续流入/流出天数"""
        if not values:
            return 0

        sign = 1 if values[0] > 0 else -1
        count = 0
        for v in values:
            if (sign > 0 and v > 0) or (sign < 0 and v < 0):
                count += 1
            else:
                break
        return count

    # =========================================================================
    # 4. 筹码结构数据 (cyq_perf)
    # =========================================================================

    def get_cyq_perf(self, ts_code: str, trade_date: str = None,
                     start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """获取股票筹码分布数据 (Tushare cyq_perf)"""
        cache_file = self.cache_dir / "cyq_perf" / f"{ts_code}_{trade_date or start_date}_{end_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)
        if self.ts_pro is None:
            return pd.DataFrame()

        try:
            if trade_date:
                df = self.ts_pro.cyq_perf(ts_code=ts_code, trade_date=trade_date)
            else:
                df = self.ts_pro.cyq_perf(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_cyq_perf] {ts_code} 筹码数据: {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"[get_cyq_perf] 获取失败 {ts_code}: {e}")
            return pd.DataFrame()

    def analyze_chips_structure(self, ts_code: str, trade_date: str) -> Dict:
        """分析个股筹码结构"""
        df = self.get_cyq_perf(ts_code=ts_code, trade_date=trade_date)
        if df.empty:
            return {}

        row = df.iloc[0]
        profit_pct = row.get('profit_pct', 0)
        concentration = row.get('concentration', 0)
        avg_cost = row.get('avg_cost', 0)
        close = row.get('close', 0)

        if profit_pct >= 80:
            structure_type = '高位密集' if concentration > 30 else '高位分散'
        elif profit_pct <= 20:
            structure_type = '低位密集' if concentration > 30 else '低位分散'
        else:
            structure_type = '震荡整理'

        return {
            'profit_pct': profit_pct,
            'concentration': concentration,
            'avg_cost': avg_cost,
            'close': close,
            'cost_vs_price': ((close - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0,
            'structure_type': structure_type,
        }

    def get_batch_cyq_perf(self, ts_codes: List[str], trade_date: str) -> pd.DataFrame:
        """批量获取筹码数据"""
        results = []
        for ts_code in ts_codes:
            df = self.get_cyq_perf(ts_code=ts_code, trade_date=trade_date)
            if not df.empty:
                results.append(df)
        if results:
            return pd.concat(results, ignore_index=True)
        return pd.DataFrame()