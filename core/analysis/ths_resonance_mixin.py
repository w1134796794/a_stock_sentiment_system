"""
THS 概念-行业共振分析 Mixin

从 ``ths_sector_tracker.py`` 拆分出来的共振分析相关方法集合。

该 Mixin 假设宿主类（``THSSectorTracker``）已提供以下属性/方法：
  - ``self.dm``                       : DataManager
  - ``self.analyze_concept_sectors``  : 概念板块当日分析
  - ``self.analyze_industry_sectors`` : 行业板块当日分析
  - ``self.analyze_concept_persistence``  : 概念持续性分析（由 ``THSPersistenceMixin`` 提供）
  - ``self.analyze_industry_persistence`` : 行业持续性分析
  - ``self._load_sector_list``        : 加载板块清单
  - ``self.get_sector_members``       : 板块成分股
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

import numpy as np
import pandas as pd
import loguru

logger = loguru.logger


class THSResonanceMixin:
    """概念-行业共振分析 Mixin。

    包含以下方法：
      - analyze_concept_industry_resonance (公开入口)
      - _get_sector_stocks_set
      - _get_sector_limit_up_stocks
      - _get_sector_leaders
      - _get_sector_trend
      - _calculate_trend_sync
      - _get_all_stocks_performance
      - _get_sector_top_performers
      - _get_sector_moneyflow_trend
      - _calculate_moneyflow_sync
    """

    def analyze_concept_industry_resonance(self, trade_date: str,
                                            hot_concepts_df: pd.DataFrame = None,
                                            hot_industries_df: pd.DataFrame = None,
                                            lookback_days: int = 10) -> pd.DataFrame:
        """分析概念-行业共振（市场主线）- 多维度共振模型。"""
        logger.info("=" * 80)
        logger.info(f"【analyze_concept_industry_resonance】开始分析概念-行业共振，日期: {trade_date}")

        if hot_concepts_df is None or hot_concepts_df.empty:
            hot_concepts_df = self.analyze_concept_sectors(trade_date, top_n=20)

        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=20)

        concept_persistence_df = self.analyze_concept_persistence(trade_date, top_n=15, lookback_days=lookback_days)
        industry_persistence_df = self.analyze_industry_persistence(trade_date, top_n=15, lookback_days=lookback_days)

        limit_up_df = self.repo.get_limit_up_pool(trade_date)
        all_stocks_df = self._get_all_stocks_performance(trade_date)

        main_themes = []

        for _, concept_row in concept_persistence_df.iterrows():
            concept_name = concept_row['板块名称']
            concept_stocks = self._get_sector_stocks_set(concept_name, trade_date)

            if not concept_stocks:
                continue

            concept_limit_up_stocks = self._get_sector_limit_up_stocks(concept_name, trade_date, limit_up_df)
            concept_top_performers = self._get_sector_top_performers(concept_name, trade_date, all_stocks_df, top_n=10)
            concept_trend = self._get_sector_trend(concept_name, trade_date, days=5)
            concept_moneyflow = self._get_sector_moneyflow_trend(concept_name, trade_date, days=5)

            for _, industry_row in industry_persistence_df.iterrows():
                industry_name = industry_row['板块名称']
                industry_stocks = self._get_sector_stocks_set(industry_name, trade_date)

                if not industry_stocks:
                    continue

                # 维度1：成分股重叠度
                overlap = concept_stocks & industry_stocks
                stock_overlap_score = len(overlap) / len(concept_stocks) * 100 if concept_stocks else 0

                # 维度2：涨停股重叠度
                industry_limit_up_stocks = self._get_sector_limit_up_stocks(industry_name, trade_date, limit_up_df)
                limit_up_overlap = concept_limit_up_stocks & industry_limit_up_stocks
                limit_up_overlap_score = len(limit_up_overlap) / max(len(concept_limit_up_stocks), 1) * 100 if concept_limit_up_stocks else 0

                # 维度3：龙头股重叠度
                industry_top_performers = self._get_sector_top_performers(industry_name, trade_date, all_stocks_df, top_n=10)
                top_performer_overlap = concept_top_performers & industry_top_performers
                top_performer_score = len(top_performer_overlap) / max(len(concept_top_performers), 1) * 100 if concept_top_performers else 0

                # 维度4：领涨股重叠度
                concept_leaders = self._get_sector_leaders(concept_name, trade_date, limit_up_df, top_n=3)
                industry_leaders = self._get_sector_leaders(industry_name, trade_date, limit_up_df, top_n=3)
                leader_overlap = concept_leaders & industry_leaders
                leader_overlap_score = len(leader_overlap) / max(len(concept_leaders), 1) * 100 if concept_leaders else 0

                # 维度5：热度趋势同步性
                industry_trend = self._get_sector_trend(industry_name, trade_date, days=5)
                trend_sync_score = self._calculate_trend_sync(concept_trend, industry_trend)

                # 维度6：资金流向共振
                industry_moneyflow = self._get_sector_moneyflow_trend(industry_name, trade_date, days=5)
                moneyflow_sync_score = self._calculate_moneyflow_sync(concept_moneyflow, industry_moneyflow)

                composite_resonance = (
                    stock_overlap_score * 0.15 +
                    limit_up_overlap_score * 0.15 +
                    top_performer_score * 0.20 +
                    leader_overlap_score * 0.10 +
                    trend_sync_score * 0.20 +
                    moneyflow_sync_score * 0.20
                )

                if composite_resonance >= 12:
                    concept_score = concept_row['持续性评分']
                    industry_score = industry_row['持续性评分']
                    persistence_avg = (concept_score + industry_score) / 2
                    composite_score = (composite_resonance * 0.5 + persistence_avg * 0.5)

                    concept_days = concept_row['热点天数']
                    industry_days = industry_row['热点天数']
                    min_days = min(concept_days, industry_days)

                    if min_days >= lookback_days * 0.7:
                        stage = '成熟期'
                    elif min_days >= lookback_days * 0.5:
                        stage = '成长期'
                    elif min_days >= lookback_days * 0.3:
                        stage = '萌芽期'
                    else:
                        stage = '衰退期'

                    if stage == '成熟期':
                        advice = '持有观察，谨慎追高'
                    elif stage == '成长期':
                        advice = '积极关注，逢低布局'
                    elif stage == '萌芽期':
                        advice = '重点关注，试错参与'
                    else:
                        advice = '观望，等待信号'

                    main_themes.append({
                        '主线名称': f"{concept_name}+{industry_name}",
                        '核心概念': concept_name,
                        '核心行业': industry_name,
                        '共振度': round(composite_resonance, 1),
                        '综合共振度': round(composite_resonance, 1),
                        '成分股重叠': round(stock_overlap_score, 1),
                        '涨停股重叠': round(limit_up_overlap_score, 1),
                        '龙头股重叠': round(top_performer_score, 1),
                        '领涨股重叠': round(leader_overlap_score, 1),
                        '趋势同步性': round(trend_sync_score, 1),
                        '资金共振度': round(moneyflow_sync_score, 1),
                        '概念持续性': concept_days,
                        '行业持续性': industry_days,
                        '重叠股票数': len(overlap),
                        '涨停重叠数': len(limit_up_overlap),
                        '龙头重叠数': len(top_performer_overlap),
                        '领涨重叠数': len(leader_overlap),
                        '综合评分': round(composite_score, 1),
                        '所处阶段': stage,
                        '操作建议': advice
                    })

        if not main_themes:
            logger.warning("[analyze_concept_industry_resonance] 未找到概念和行业的强共振关系")
            return pd.DataFrame()

        result_df = pd.DataFrame(main_themes)
        result_df = result_df.sort_values('综合评分', ascending=False)

        logger.info("-" * 80)
        logger.info(f"【共振分析结果】共识别 {len(result_df)} 条市场主线")
        for idx, row in result_df.head(5).iterrows():
            logger.info(f"  Top{idx+1}: {row['主线名称']} - "
                       f"综合共振度{row['综合共振度']}% "
                       f"(成分股{row['成分股重叠']}%|涨停{row['涨停股重叠']}%|龙头{row['龙头股重叠']}%|领涨{row['领涨股重叠']}%|趋势{row['趋势同步性']}%|资金{row['资金共振度']}%), "
                       f"综合评分{row['综合评分']}, 阶段:{row['所处阶段']}")
        logger.info("=" * 80)

        return result_df

    # ------------------------------------------------------------------
    # 板块成分股 / 涨停股 / 领涨股集合
    # ------------------------------------------------------------------
    def _get_sector_stocks_set(self, sector_name: str, trade_date: str) -> set:
        """获取板块的成分股代码集合"""
        try:
            concept_list, industry_list = self._load_sector_list()

            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return set()

            members = self.get_sector_members(ts_code)
            if not members.empty and 'ts_code' in members.columns:
                return set(members['ts_code'].tolist())
            return set()
        except Exception as e:
            logger.warning(f"[_get_sector_stocks_set] 获取 {sector_name} 成分股失败: {e}")
            return set()

    def _get_sector_limit_up_stocks(self, sector_name: str, trade_date: str,
                                     limit_up_df: pd.DataFrame = None) -> set:
        """获取板块的涨停股代码集合（6位数字）"""
        try:
            if limit_up_df is None or limit_up_df.empty:
                return set()

            sector_stocks = self._get_sector_stocks_set(sector_name, trade_date)
            if not sector_stocks:
                return set()

            sector_codes = set()
            for code in sector_stocks:
                clean_code = str(code).split('.')[0].zfill(6)
                sector_codes.add(clean_code)

            limit_up_codes = set()
            code_col = None
            if '代码' in limit_up_df.columns:
                code_col = '代码'
            elif 'code' in limit_up_df.columns:
                code_col = 'code'
            elif 'ts_code' in limit_up_df.columns:
                code_col = 'ts_code'

            if code_col:
                for _, row in limit_up_df.iterrows():
                    code = str(row[code_col]).split('.')[0].zfill(6)
                    if code in sector_codes:
                        limit_up_codes.add(code)

            return limit_up_codes
        except Exception as e:
            logger.warning(f"[_get_sector_limit_up_stocks] 获取 {sector_name} 涨停股失败: {e}")
            return set()

    def _get_sector_leaders(self, sector_name: str, trade_date: str,
                            limit_up_df: pd.DataFrame = None, top_n: int = 3) -> set:
        """获取板块的领涨股代码集合（封单金额最大的前 top_n 只）"""
        try:
            if limit_up_df is None or limit_up_df.empty:
                return set()

            limit_up_stocks = self._get_sector_limit_up_stocks(sector_name, trade_date, limit_up_df)
            if not limit_up_stocks:
                return set()

            code_col = None
            if '代码' in limit_up_df.columns:
                code_col = '代码'
            elif 'code' in limit_up_df.columns:
                code_col = 'code'
            elif 'ts_code' in limit_up_df.columns:
                code_col = 'ts_code'

            if not code_col:
                return set()

            limit_up_df = limit_up_df.copy()
            limit_up_df['clean_code'] = limit_up_df[code_col].astype(str).str.split('.').str[0].str.zfill(6)

            sector_limit_up = limit_up_df[limit_up_df['clean_code'].isin(limit_up_stocks)]
            if sector_limit_up.empty:
                return set()

            amount_col = None
            for col in ['封单额', '封单金额', 'seal_amount', 'bid_amount']:
                if col in sector_limit_up.columns:
                    amount_col = col
                    break

            if amount_col:
                sector_limit_up = sector_limit_up.sort_values(amount_col, ascending=False)

            return set(sector_limit_up.head(top_n)['clean_code'].tolist())
        except Exception as e:
            logger.warning(f"[_get_sector_leaders] 获取 {sector_name} 领涨股失败: {e}")
            return set()

    # ------------------------------------------------------------------
    # 趋势 / 资金流向相关
    # ------------------------------------------------------------------
    def _get_sector_trend(self, sector_name: str, trade_date: str, days: int = 5) -> List[float]:
        """获取板块近N日的涨幅趋势（list, 早→近）"""
        try:
            concept_list, industry_list = self._load_sector_list()

            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return []

            end_date = datetime.strptime(trade_date, "%Y%m%d")
            start_date = end_date - timedelta(days=days * 2)

            daily_data = self.repo.get_ths_daily(ts_code=ts_code,
                                               start_date=start_date.strftime("%Y%m%d"),
                                               end_date=trade_date)

            if daily_data.empty or 'pct_change' not in daily_data.columns:
                return []

            daily_data = daily_data.sort_values('trade_date')
            return daily_data['pct_change'].tail(days).tolist()
        except Exception as e:
            logger.warning(f"[_get_sector_trend] 获取 {sector_name} 趋势失败: {e}")
            return []

    def _calculate_trend_sync(self, trend1: List[float], trend2: List[float]) -> float:
        """计算两个趋势的同步性得分（0-100）"""
        try:
            if not trend1 or not trend2 or len(trend1) < 2 or len(trend2) < 2:
                return 50.0

            min_len = min(len(trend1), len(trend2))
            trend1 = trend1[-min_len:]
            trend2 = trend2[-min_len:]

            if len(trend1) < 2:
                return 50.0

            correlation = np.corrcoef(trend1, trend2)[0, 1]
            if np.isnan(correlation):
                return 50.0

            sync_score = (correlation + 1) / 2 * 100
            return round(sync_score, 1)
        except Exception as e:
            logger.warning(f"[_calculate_trend_sync] 计算趋势同步性失败: {e}")
            return 50.0

    def _get_all_stocks_performance(self, trade_date: str) -> pd.DataFrame:
        """获取全市场股票当日涨幅数据"""
        try:
            daily_data = self.repo.get_all_rt_k_data(trade_date=trade_date)
            if daily_data.empty:
                return pd.DataFrame()

            if 'ts_code' in daily_data.columns:
                daily_data['code'] = daily_data['ts_code'].astype(str).str.split('.').str[0].str.zfill(6)
            elif 'code' in daily_data.columns:
                daily_data['code'] = daily_data['code'].astype(str).str.zfill(6)

            return daily_data
        except Exception as e:
            logger.warning(f"[_get_all_stocks_performance] 获取全市场数据失败: {e}")
            return pd.DataFrame()

    def _get_sector_top_performers(self, sector_name: str, trade_date: str,
                                    all_stocks_df: pd.DataFrame = None, top_n: int = 10) -> set:
        """获取板块涨幅前 top_n 的股票代码集合"""
        try:
            sector_stocks = self._get_sector_stocks_set(sector_name, trade_date)
            if not sector_stocks:
                return set()

            sector_codes = set()
            for code in sector_stocks:
                clean_code = str(code).split('.')[0].zfill(6)
                sector_codes.add(clean_code)

            if all_stocks_df is None or all_stocks_df.empty:
                all_stocks_df = self._get_all_stocks_performance(trade_date)

            if all_stocks_df.empty:
                return set()

            if 'code' not in all_stocks_df.columns:
                return set()

            sector_df = all_stocks_df[all_stocks_df['code'].isin(sector_codes)]
            if sector_df.empty:
                return set()

            if 'pct_change' in sector_df.columns:
                sector_df = sector_df.sort_values('pct_change', ascending=False)
            elif '涨跌幅' in sector_df.columns:
                sector_df = sector_df.sort_values('涨跌幅', ascending=False)
            else:
                return set()

            return set(sector_df.head(top_n)['code'].tolist())
        except Exception as e:
            logger.warning(f"[_get_sector_top_performers] 获取 {sector_name} 龙头股失败: {e}")
            return set()

    def _get_sector_moneyflow_trend(self, sector_name: str, trade_date: str, days: int = 5) -> List[float]:
        """获取板块近N日的资金流向趋势（亿元，简化估算）"""
        try:
            concept_list, industry_list = self._load_sector_list()

            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return []

            end_date = datetime.strptime(trade_date, "%Y%m%d")
            start_date = end_date - timedelta(days=days * 2)

            daily_data = self.repo.get_ths_daily(ts_code=ts_code,
                                               start_date=start_date.strftime("%Y%m%d"),
                                               end_date=trade_date)

            if daily_data.empty:
                return []

            daily_data = daily_data.sort_values('trade_date')

            moneyflow_list = []
            for _, row in daily_data.tail(days).iterrows():
                pct_change = row.get('pct_change', 0)
                amount = row.get('amount', 0)
                if pct_change > 0:
                    net_flow = amount * pct_change / 100
                else:
                    net_flow = amount * pct_change / 100
                moneyflow_list.append(net_flow / 100000)

            return moneyflow_list
        except Exception as e:
            logger.warning(f"[_get_sector_moneyflow_trend] 获取 {sector_name} 资金流向失败: {e}")
            return []

    def _calculate_moneyflow_sync(self, moneyflow1: List[float], moneyflow2: List[float]) -> float:
        """计算两个资金流向序列的同步性得分（0-100）"""
        try:
            if not moneyflow1 or not moneyflow2 or len(moneyflow1) < 2 or len(moneyflow2) < 2:
                return 50.0

            min_len = min(len(moneyflow1), len(moneyflow2))
            moneyflow1 = moneyflow1[-min_len:]
            moneyflow2 = moneyflow2[-min_len:]

            direction1 = [1 if m > 0 else -1 for m in moneyflow1]
            direction2 = [1 if m > 0 else -1 for m in moneyflow2]

            same_direction_count = sum(1 for d1, d2 in zip(direction1, direction2) if d1 == d2)
            direction_sync = same_direction_count / len(direction1) * 100

            try:
                correlation = np.corrcoef(moneyflow1, moneyflow2)[0, 1]
                if np.isnan(correlation):
                    correlation = 0
            except Exception:
                correlation = 0

            correlation_score = (correlation + 1) / 2 * 100
            sync_score = direction_sync * 0.6 + correlation_score * 0.4

            return round(sync_score, 1)
        except Exception as e:
            logger.warning(f"[_calculate_moneyflow_sync] 计算资金同步性失败: {e}")
            return 50.0