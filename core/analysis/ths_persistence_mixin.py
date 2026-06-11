"""
THS 板块持续性分析 Mixin

从 ``ths_sector_tracker.py`` 拆分出来的持续性相关方法集合。

该 Mixin 假设宿主类（``THSSectorTracker``）已提供以下属性：
  - ``self.dm``                       : DataManager
  - ``self.config``                   : 板块追踪器配置
  - ``self.persistence_analyzer``     : SectorPersistenceAnalyzer 实例
  - ``self.analyze_concept_sectors``  : 概念板块当日分析
  - ``self.analyze_industry_sectors`` : 行业板块当日分析
  - ``self.get_sector_stocks``        : 板块成分股查询
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import loguru

logger = loguru.logger


class THSPersistenceMixin:
    """板块持续性分析 Mixin。

    包含以下公开方法（保持原签名）：
      - analyze_sectors_persistence
      - analyze_concept_persistence
      - analyze_industry_persistence

    及内部辅助方法 ``_get_operation_advice`` / ``_get_strategy_reason`` /
    ``_analyze_persistence_for_sectors``。
    """

    # ------------------------------------------------------------------
    # 综合持续性分析（兼容旧接口）
    # ------------------------------------------------------------------
    def analyze_sectors_persistence(self, trade_date: str, top_n: int = None,
                                     lookback_days: int = None,
                                     hot_threshold_days: int = None) -> pd.DataFrame:
        """
        分析板块持续性热度

        "持续性"定义：板块在最近 lookback_days 个交易日内，至少有 hot_threshold_days 天
        进入涨幅排名前 top_n，且平均涨幅保持较高水平。

        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 每日热点板块排名阈值（进入前N名才算当日热门）
            lookback_days: 回溯交易日数量（默认5天）
            hot_threshold_days: 判定为持续热门的最少天数（默认3天）

        Returns:
            DataFrame: 包含持续性分析的板块数据
        """
        # 使用配置参数（如果未提供）
        persistence_config = self.config.get('persistence', {})
        top_n = top_n or persistence_config.get('top_n', 10)
        lookback_days = lookback_days or persistence_config.get('lookback_days', 5)
        hot_threshold_days = hot_threshold_days or persistence_config.get('hot_threshold_days', 3)

        # 1. 计算回溯日期列表（使用DateUtils，正确处理节假日）
        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)

        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_sectors_persistence] 只能获取 {len(date_list)} 个交易日，少于要求的 {lookback_days} 天")

        # 2. 收集多日的板块分析数据
        daily_results = {}
        all_sectors = set()

        for date in date_list:
            try:
                concept_df = self.analyze_concept_sectors(date, top_n=top_n * 3)
                industry_df = self.analyze_industry_sectors(date, top_n=top_n * 3)

                if not concept_df.empty and not industry_df.empty:
                    daily_df = pd.concat([concept_df, industry_df], ignore_index=True)
                elif not concept_df.empty:
                    daily_df = concept_df
                elif not industry_df.empty:
                    daily_df = industry_df
                else:
                    continue

                if not daily_df.empty:
                    daily_results[date] = daily_df
                    all_sectors.update(daily_df['ts_code'].tolist())
            except Exception as e:
                logger.warning(f"[analyze_sectors_persistence] 获取 {date} 数据失败: {e}")

        if not daily_results:
            logger.warning(f"[analyze_sectors_persistence] 无法获取任何历史数据")
            return pd.DataFrame()

        results = []
        limit_up_df = self.repo.get_limit_up_pool(trade_date)

        for ts_code in all_sectors:
            sector_history = []

            for date in date_list:
                if date in daily_results:
                    daily_df = daily_results[date]
                    sector_row = daily_df[daily_df['ts_code'] == ts_code]
                    if not sector_row.empty:
                        sector_history.append({
                            'date': date,
                            'rank': sector_row.iloc[0]['rank'],
                            'pct_change': sector_row.iloc[0]['pct_change'],
                            'composite_score': sector_row.iloc[0]['composite_score'],
                            'is_hot': sector_row.iloc[0]['is_hot'],
                            'name': sector_row.iloc[0]['name'],
                            'type': sector_row.iloc[0]['type']
                        })

            if not sector_history:
                continue

            latest = sector_history[0]

            hot_days = sum(1 for h in sector_history if h['rank'] <= top_n)

            avg_rank = sum(h['rank'] for h in sector_history) / len(sector_history)

            if len(sector_history) >= 4:
                recent_avg = sum(h['rank'] for h in sector_history[:3]) / 3
                earlier_avg = sum(h['rank'] for h in sector_history[3:]) / (len(sector_history) - 3)
                rank_trend = earlier_avg - recent_avg

                if rank_trend > 5:
                    trend_desc = '快速上升'
                elif rank_trend > 2:
                    trend_desc = '稳步上升'
                elif rank_trend < -5:
                    trend_desc = '快速下降'
                elif rank_trend < -2:
                    trend_desc = '逐步下降'
                else:
                    trend_desc = '相对平稳'
            else:
                trend_desc = '数据不足'

            avg_pct_change = sum(h['pct_change'] for h in sector_history) / len(sector_history)

            if len(sector_history) >= 4:
                recent_pct = sum(h['pct_change'] for h in sector_history[:3]) / 3
                earlier_pct = sum(h['pct_change'] for h in sector_history[3:]) / (len(sector_history) - 3)
                pct_trend = recent_pct - earlier_pct
            else:
                pct_trend = 0

            is_persistent_hot = hot_days >= hot_threshold_days

            sector_detail = self.get_sector_stocks(ts_code, trade_date, limit_up_df)
            up_count = sector_detail.get('up_count', 0) if sector_detail else 0
            up_stocks = sector_detail.get('up_stocks', []) if sector_detail else []
            cons_count = sum(1 for s in up_stocks if s.get('board_height', 1) >= 2)

            limit_up_score = min(100, up_count * 5 + cons_count * 10)

            persistence_ratio = hot_days / len(sector_history) * 100
            rank_score = max(0, (100 - avg_rank))
            pct_score = min(100, max(0, avg_pct_change * 10))

            persistence_score = (
                limit_up_score * 0.35 +
                persistence_ratio * 0.25 +
                rank_score * 0.20 +
                pct_score * 0.20
            )

            if is_persistent_hot:
                if latest['pct_change'] > 5 and pct_trend > 1:
                    stage = '高潮期'
                elif latest['pct_change'] > 3 and pct_trend > 0:
                    stage = '加速期'
                elif latest['pct_change'] > 1:
                    stage = '主升浪'
                else:
                    stage = '震荡整理'
            else:
                if hot_days >= 2:
                    stage = '启动期'
                else:
                    stage = '观察期'

            operation_advice = self._get_operation_advice(is_persistent_hot, stage, pct_trend)

            position_mapping = {
                '积极关注': 'medium',
                '重点关注': 'medium',
                '逢低布局': 'medium',
                '持有观察': 'light',
                '谨慎追高': 'light',
                '观望': 'light',
                '观察': 'light'
            }
            urgency_mapping = {
                '积极关注': '高',
                '重点关注': '高',
                '逢低布局': '中',
                '持有观察': '中',
                '谨慎追高': '低',
                '观望': '低',
                '观察': '低'
            }

            results.append({
                '板块名称': latest['name'],
                '板块类型': latest['type'],
                'ts_code': ts_code,
                '持续天数': hot_days,
                '统计天数': len(sector_history),
                '平均排名': round(avg_rank, 1),
                '最新排名': int(latest['rank']),
                '排名趋势': trend_desc,
                '平均涨幅': round(avg_pct_change, 2),
                '最新涨幅': round(latest['pct_change'], 2),
                '涨幅趋势': round(pct_trend, 2),
                '持续性评分': round(persistence_score, 1),
                '是否持续热门': is_persistent_hot,
                '所处阶段': stage,
                '涨停家数': up_count,
                '连板家数': cons_count,
                '操作建议': operation_advice,
                '建议仓位': position_mapping.get(operation_advice, 'light'),
                '紧急度': urgency_mapping.get(operation_advice, '低'),
                '策略理由': self._get_strategy_reason(latest, hot_days, trend_desc, stage),
                '当前排名': int(latest['rank']),
                '综合评分': round(persistence_score, 1),
                '市场周期': '上升期' if is_persistent_hot else '震荡期',
                '成交额变化': 0.0,
                '换手率': 0.0,
                '排名动量': round(pct_trend, 2),
                '涨停趋势': float(cons_count)
            })

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        result_df = result_df[result_df['涨停家数'] > 0]

        if result_df.empty:
            logger.warning(f"[analyze_sectors_persistence] 过滤后没有板块满足条件（涨停家数>0）")
            return pd.DataFrame()

        result_df = result_df.sort_values('持续性评分', ascending=False)

        return result_df.head(top_n)

    # ------------------------------------------------------------------
    # 操作建议生成
    # ------------------------------------------------------------------
    def _get_operation_advice(self, is_persistent_hot: bool, stage: str, pct_trend: float) -> str:
        """根据持续性状态生成操作建议"""
        if not is_persistent_hot:
            return '观望'

        if stage == '高潮期':
            return '谨慎追高' if pct_trend < 0 else '持有观察'
        elif stage == '加速期':
            return '积极关注'
        elif stage == '主升浪':
            return '重点关注'
        elif stage == '启动期':
            return '逢低布局'
        else:
            return '观察'

    def _get_strategy_reason(self, latest: dict, hot_days: int, trend_desc: str, stage: str) -> str:
        """生成策略理由"""
        reasons = []
        if hot_days >= 3:
            reasons.append(f"连续{hot_days}天热门")
        reasons.append(f"排名{trend_desc}")
        reasons.append(f"当前处于{stage}")
        if latest.get('is_hot'):
            reasons.append("当日热点")
        return "；".join(reasons)

    # ------------------------------------------------------------------
    # 概念/行业持续性 (M 天 N 次)
    # ------------------------------------------------------------------
    def analyze_concept_persistence(self, trade_date: str, top_n: int = 10,
                                     lookback_days: int = 10,
                                     hot_concepts_df: pd.DataFrame = None) -> pd.DataFrame:
        """分析概念板块持续性 (M 天内 N 次模式)。"""
        logger.info("=" * 80)
        logger.info(f"【analyze_concept_persistence】开始分析概念板块持续性，日期: {trade_date}")
        logger.info(f"[analyze_concept_persistence] 采用M天内N次模式，M={lookback_days}")

        persistence_config = self.config.get('persistence', {})

        if hot_concepts_df is None or hot_concepts_df.empty:
            hot_concepts_df = self.analyze_concept_sectors(trade_date, top_n=top_n * 2)

        if hot_concepts_df.empty or 'is_hot' not in hot_concepts_df.columns:
            logger.warning("[analyze_concept_persistence] 无法获取当前热点概念")
            return pd.DataFrame()

        current_hot_concepts = hot_concepts_df[hot_concepts_df['is_hot'] == True]
        if current_hot_concepts.empty:
            logger.warning("[analyze_concept_persistence] 当前无热点概念")
            return pd.DataFrame()

        logger.info(f"[analyze_concept_persistence] 当前热点概念: {len(current_hot_concepts)}个")
        logger.info(f"[analyze_concept_persistence] 当前热点概念列表: {current_hot_concepts['name'].tolist()}")

        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)

        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_concept_persistence] 只能获取 {len(date_list)} 个交易日")

        daily_results = {}
        target_concept_names = set(current_hot_concepts['name'].tolist())

        for date in date_list:
            try:
                daily_df = self.analyze_concept_sectors(date, top_n=100)
                if not daily_df.empty:
                    matched_df = daily_df[daily_df['name'].isin(target_concept_names)]
                    if not matched_df.empty:
                        daily_results[date] = matched_df
            except Exception as e:
                logger.warning(f"[analyze_concept_persistence] 获取 {date} 数据失败: {e}")

        if not daily_results:
            logger.warning("[analyze_concept_persistence] 无法获取任何历史数据")
            return pd.DataFrame()

        result_df = self.persistence_analyzer.analyze_persistence_with_history(
            trade_date=trade_date,
            hot_sectors_df=hot_concepts_df,
            historical_data=daily_results,
            lookback_days=lookback_days,
            top_n=top_n
        )

        if result_df.empty:
            logger.warning("[analyze_concept_persistence] 无持续性分析结果")
            return pd.DataFrame()

        logger.info(f"[analyze_concept_persistence] 分析完成，返回 {len(result_df)} 个持续热门概念")
        for _, row in result_df.head(5).iterrows():
            logger.info(f"  - {row['板块名称']}: {lookback_days}天内{row['热点天数']}次热点, 评分{row['持续性评分']:.1f}, 阶段[{row['所处阶段']}]")
        logger.info("=" * 80)

        return result_df

    def analyze_industry_persistence(self, trade_date: str, top_n: int = 10,
                                      lookback_days: int = 10,
                                      hot_industries_df: pd.DataFrame = None) -> pd.DataFrame:
        """分析行业板块持续性 (M 天内 N 次模式)。"""
        logger.info("=" * 80)
        logger.info(f"【analyze_industry_persistence】开始分析行业板块持续性，日期: {trade_date}")
        logger.info(f"[analyze_industry_persistence] 采用M天内N次模式，M={lookback_days}")

        persistence_config = self.config.get('persistence', {})

        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=top_n * 2)

        if hot_industries_df.empty or 'is_hot' not in hot_industries_df.columns:
            logger.warning("[analyze_industry_persistence] 无法获取当前热点行业")
            return pd.DataFrame()

        current_hot_industries = hot_industries_df[hot_industries_df['is_hot'] == True]
        if current_hot_industries.empty:
            logger.warning("[analyze_industry_persistence] 当前无热点行业")
            return pd.DataFrame()

        logger.info(f"[analyze_industry_persistence] 当前热点行业: {len(current_hot_industries)}个")
        logger.info(f"[analyze_industry_persistence] 当前热点行业列表: {current_hot_industries['name'].tolist()}")

        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)

        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_industry_persistence] 只能获取 {len(date_list)} 个交易日")

        daily_results = {}
        target_industry_names = set(current_hot_industries['name'].tolist())

        for date in date_list:
            try:
                daily_df = self.analyze_industry_sectors(date, top_n=100)
                if not daily_df.empty:
                    matched_df = daily_df[daily_df['name'].isin(target_industry_names)]
                    if not matched_df.empty:
                        daily_results[date] = matched_df
            except Exception as e:
                logger.warning(f"[analyze_industry_persistence] 获取 {date} 数据失败: {e}")

        if not daily_results:
            logger.warning("[analyze_industry_persistence] 无法获取任何历史数据")
            return pd.DataFrame()

        result_df = self.persistence_analyzer.analyze_persistence_with_history(
            trade_date=trade_date,
            hot_sectors_df=hot_industries_df,
            historical_data=daily_results,
            lookback_days=lookback_days,
            top_n=top_n
        )

        if result_df.empty:
            logger.warning("[analyze_industry_persistence] 无持续性分析结果")
            return pd.DataFrame()

        logger.info(f"[analyze_industry_persistence] 分析完成，返回 {len(result_df)} 个持续热门行业")
        for _, row in result_df.head(5).iterrows():
            logger.info(f"  - {row['板块名称']}: {lookback_days}天内{row['热点天数']}次热点, 评分{row['持续性评分']:.1f}, 阶段[{row['所处阶段']}]")
        logger.info("=" * 80)

        return result_df

    # ------------------------------------------------------------------
    # 内部通用持续性分析（保留，给以后扩展用）
    # 注：旧版引用了未定义的 ``match_by`` / ``sector_key``，原文件中
    # 该方法实际上未被任何对外接口调用。这里保留逻辑但用 ts_code 作为默认匹配键。
    # ------------------------------------------------------------------
    def _analyze_persistence_for_sectors(self, all_sectors: set, daily_results: dict,
                                          date_list: list, hot_threshold_days: int,
                                          lookback_days: int, sector_type: str) -> list:
        """通用持续性分析方法（内部使用）。"""
        results = []

        for ts_code in all_sectors:
            sector_history = []

            for date in date_list:
                if date in daily_results:
                    daily_df = daily_results[date]
                    sector_row = daily_df[daily_df['ts_code'] == ts_code]
                    if not sector_row.empty:
                        sector_history.append({
                            'date': date,
                            'rank': sector_row.iloc[0]['rank'],
                            'pct_change': sector_row.iloc[0]['pct_change'],
                            'composite_score': sector_row.iloc[0]['composite_score'],
                            'is_hot': sector_row.iloc[0].get('is_hot', False),
                            'name': sector_row.iloc[0]['name'],
                            'type': sector_row.iloc[0]['type']
                        })

            if not sector_history:
                continue

            latest = sector_history[0]
            hot_days = sum(1 for h in sector_history if h['is_hot'])
            is_persistent_hot = hot_days >= hot_threshold_days

            avg_rank = np.mean([h['rank'] for h in sector_history])
            avg_pct_change = np.mean([h['pct_change'] for h in sector_history])

            if len(sector_history) >= 2:
                early_rank = np.mean([h['rank'] for h in sector_history[-2:]])
                recent_rank = np.mean([h['rank'] for h in sector_history[:2]])
                rank_trend = early_rank - recent_rank
            else:
                rank_trend = 0

            if rank_trend > 5:
                trend_desc = '上升'
            elif rank_trend < -5:
                trend_desc = '下降'
            else:
                trend_desc = '平稳'

            persistence_score = (
                hot_days / len(date_list) * 50 +
                (100 - avg_rank) / 100 * 30 +
                max(0, rank_trend) / len(date_list) * 20
            )

            if hot_days >= lookback_days * 0.8:
                stage = '高潮期'
            elif hot_days >= lookback_days * 0.6:
                stage = '加速期'
            elif hot_days >= lookback_days * 0.4:
                stage = '主升浪'
            elif hot_days >= lookback_days * 0.2:
                stage = '启动期'
            else:
                stage = '观察期'

            up_count = 0
            cons_count = 0
            if date_list and date_list[0] in daily_results:
                latest_df = daily_results[date_list[0]]
                sector_data = latest_df[latest_df['ts_code'] == ts_code]
                if not sector_data.empty:
                    up_count = sector_data.iloc[0].get('limit_up_count', 0)
                    cons_count = sector_data.iloc[0].get('consecutive_count', 0)

            operation_advice = self._get_operation_advice(is_persistent_hot, stage, rank_trend)

            results.append({
                '板块名称': latest['name'],
                '板块类型': latest['type'],
                'ts_code': latest.get('ts_code', ''),
                '持续天数': hot_days,
                '统计天数': len(sector_history),
                '平均排名': round(avg_rank, 1),
                '最新排名': int(latest['rank']),
                '排名趋势': trend_desc,
                '平均涨幅': round(avg_pct_change, 2),
                '最新涨幅': round(latest['pct_change'], 2),
                '持续性评分': round(persistence_score, 1),
                '是否持续热门': is_persistent_hot,
                '所处阶段': stage,
                '涨停家数': up_count,
                '连板家数': cons_count,
                '操作建议': operation_advice,
                '策略理由': self._get_strategy_reason(latest, hot_days, trend_desc, stage)
            })

        return results