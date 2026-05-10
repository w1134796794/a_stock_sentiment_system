"""
板块持续性分析器

负责分析板块的历史持续性，判断板块所处的生命周期阶段

核心职责：
1. 概念板块持续性分析 - M天内N次模式
2. 行业板块持续性分析 - M天内N次模式
3. 计算持续性评分和所处阶段
4. 生成操作建议
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

import loguru

logger = loguru.logger


@dataclass
class PersistenceResult:
    """持续性分析结果"""
    sector_name: str
    sector_type: str
    hot_days: int  # N值：热点天数
    total_days: int  # M值：总统计天数
    hot_frequency: float  # 热点频率 N/M
    persistence_score: float  # 持续性评分
    stage: str  # 所处阶段
    operation_advice: str  # 操作建议


class SectorPersistenceAnalyzer:
    """
    板块持续性分析器

    将持续性分析逻辑从THSSectorTracker中分离出来，职责单一：
    - 分析板块在M天内出现N次热点的模式
    - 计算持续性评分
    - 判断板块所处生命周期阶段
    """

    def __init__(self, data_manager, persistence_config: Dict):
        self.dm = data_manager
        self.persistence_config = persistence_config

    def analyze_concept_persistence(self, trade_date: str,
                                     hot_concepts_df: pd.DataFrame,
                                     lookback_days: int = 10,
                                     top_n: int = 10) -> pd.DataFrame:
        """
        分析概念板块持续性 - M天内N次模式

        Args:
            trade_date: 交易日期
            hot_concepts_df: 当前热点概念分析结果
            lookback_days: 回溯交易日数量M
            top_n: 返回前N个持续热门概念

        Returns:
            DataFrame: 概念持续性分析结果
        """
        logger.info("[SectorPersistenceAnalyzer] 开始分析概念板块持续性...")

        hot_threshold_days = self.persistence_config.get('hot_threshold_days', 3)

        # 只关注当前被标记为热点的概念
        current_hot_concepts = hot_concepts_df[hot_concepts_df['is_hot'] == True]
        if current_hot_concepts.empty:
            logger.warning("[analyze_concept_persistence] 当前无热点概念")
            return pd.DataFrame()

        target_concept_names = set(current_hot_concepts['name'].tolist())
        logger.info(f"[analyze_concept_persistence] 分析 {len(target_concept_names)} 个热点概念的历史持续性")

        # 获取历史数据
        daily_results = self._collect_historical_data(
            target_concept_names, trade_date, lookback_days, '概念'
        )

        if not daily_results:
            logger.warning("[analyze_concept_persistence] 无法获取历史数据")
            return pd.DataFrame()

        # 分析持续性
        results = self._analyze_persistence_m_n(
            target_concept_names, daily_results, lookback_days,
            hot_threshold_days, '概念'
        )

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        result_df = result_df[result_df['热点天数'] >= 1]
        result_df = result_df.sort_values('持续性评分', ascending=False)

        logger.info(f"[analyze_concept_persistence] 分析完成，{len(result_df.head(top_n))} 个持续热门概念")
        return result_df.head(top_n)

    def analyze_industry_persistence(self, trade_date: str,
                                      hot_industries_df: pd.DataFrame,
                                      lookback_days: int = 10,
                                      top_n: int = 10) -> pd.DataFrame:
        """
        分析行业板块持续性 - M天内N次模式

        Args:
            trade_date: 交易日期
            hot_industries_df: 当前热点行业分析结果
            lookback_days: 回溯交易日数量M
            top_n: 返回前N个持续热门行业

        Returns:
            DataFrame: 行业持续性分析结果
        """
        logger.info("[SectorPersistenceAnalyzer] 开始分析行业板块持续性...")

        hot_threshold_days = self.persistence_config.get('hot_threshold_days', 3)

        # 只关注当前被标记为热点的行业
        current_hot_industries = hot_industries_df[hot_industries_df['is_hot'] == True]
        if current_hot_industries.empty:
            logger.warning("[analyze_industry_persistence] 当前无热点行业")
            return pd.DataFrame()

        target_industry_names = set(current_hot_industries['name'].tolist())
        logger.info(f"[analyze_industry_persistence] 分析 {len(target_industry_names)} 个热点行业的历史持续性")

        # 获取历史数据
        daily_results = self._collect_historical_data(
            target_industry_names, trade_date, lookback_days, '行业'
        )

        if not daily_results:
            logger.warning("[analyze_industry_persistence] 无法获取历史数据")
            return pd.DataFrame()

        # 分析持续性
        results = self._analyze_persistence_m_n(
            target_industry_names, daily_results, lookback_days,
            hot_threshold_days, '行业'
        )

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        result_df = result_df[result_df['热点天数'] >= 1]
        result_df = result_df.sort_values('持续性评分', ascending=False)

        logger.info(f"[analyze_industry_persistence] 分析完成，{len(result_df.head(top_n))} 个持续热门行业")
        return result_df.head(top_n)

    def _collect_historical_data(self, target_names: Set[str],
                                  trade_date: str,
                                  lookback_days: int,
                                  sector_type: str) -> Dict[str, pd.DataFrame]:
        """
        收集历史数据

        注意：这里需要从外部获取历史热点数据。
        由于热点识别逻辑在HotSpotDetector中，这里需要通过回调或传入历史数据。

        简化实现：返回空字典，由调用方提供历史数据
        """
        # 实际实现中，这里应该：
        # 1. 获取过去M天的日期列表
        # 2. 对每一天调用HotSpotDetector识别热点
        # 3. 收集目标板块的历史表现

        # 简化版本：返回空，由THSSectorTracker协调调用
        return {}

    def _analyze_persistence_m_n(self, target_sectors: Set[str],
                                  daily_results: Dict[str, pd.DataFrame],
                                  lookback_days: int,
                                  hot_threshold_days: int,
                                  sector_type: str) -> List[Dict]:
        """
        分析板块持续性 - M天内N次模式

        Args:
            target_sectors: 目标板块名称集合
            daily_results: 每日分析结果字典 {date: DataFrame}
            lookback_days: 回溯天数M
            hot_threshold_days: 热点阈值天数N
            sector_type: 板块类型

        Returns:
            List[Dict]: 持续性分析结果列表
        """
        results = []
        date_list = sorted(daily_results.keys(), reverse=True)

        for sector_name in target_sectors:
            sector_history = []
            hot_days_detail = []

            for date in date_list:
                if date in daily_results:
                    daily_df = daily_results[date]
                    sector_row = daily_df[daily_df['name'] == sector_name]

                    if not sector_row.empty:
                        is_hot = sector_row.iloc[0].get('is_hot', False)
                        sector_history.append({
                            'date': date,
                            'rank': sector_row.iloc[0]['rank'],
                            'pct_change': sector_row.iloc[0]['pct_change'],
                            'composite_score': sector_row.iloc[0]['composite_score'],
                            'is_hot': is_hot,
                            'name': sector_row.iloc[0]['name'],
                            'type': sector_row.iloc[0]['type'],
                            'limit_up_count': sector_row.iloc[0].get('limit_up_count', 0),
                            'ts_code': sector_row.iloc[0].get('ts_code', '')
                        })
                        if is_hot:
                            hot_days_detail.append(date)

            if not sector_history:
                continue

            latest = sector_history[0]
            hot_days = len(hot_days_detail)
            is_persistent_hot = hot_days >= hot_threshold_days

            # 计算平均排名和涨幅
            avg_rank = np.mean([h['rank'] for h in sector_history])
            avg_pct_change = np.mean([h['pct_change'] for h in sector_history])

            # 计算排名趋势
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

            # 计算持续性评分
            hot_frequency = hot_days / lookback_days if lookback_days > 0 else 0
            rank_score = (100 - avg_rank) / 100 * 30
            trend_score = max(0, rank_trend) / max(1, len(sector_history)) * 20
            persistence_score = hot_frequency * 50 + rank_score + trend_score

            # 判断所处阶段
            if hot_frequency >= 0.8:
                stage = '高潮期'
            elif hot_frequency >= 0.6:
                stage = '加速期'
            elif hot_frequency >= 0.4:
                stage = '主升浪'
            elif hot_frequency >= 0.2:
                stage = '启动期'
            else:
                stage = '观察期'

            up_count = latest.get('limit_up_count', 0)
            operation_advice = self._get_operation_advice(is_persistent_hot, stage, rank_trend)

            results.append({
                '板块名称': latest['name'],
                '板块类型': latest['type'],
                'ts_code': latest.get('ts_code', ''),
                '热点天数': hot_days,
                '统计天数': len(sector_history),
                '总天数': lookback_days,
                '热点频率': round(hot_frequency * 100, 1),
                '热点日期': ','.join(hot_days_detail),
                '平均排名': round(avg_rank, 1),
                '最新排名': int(latest['rank']),
                '排名趋势': trend_desc,
                '平均涨幅': round(avg_pct_change, 2),
                '最新涨幅': round(latest['pct_change'], 2),
                '持续性评分': round(persistence_score, 1),
                '是否持续热门': is_persistent_hot,
                '所处阶段': stage,
                '涨停家数': up_count,
                '操作建议': operation_advice,
                '策略理由': self._get_strategy_reason(latest, hot_days, trend_desc, stage)
            })

        return results

    def _get_operation_advice(self, is_persistent_hot: bool, stage: str, rank_trend: float) -> str:
        """生成操作建议"""
        if not is_persistent_hot:
            return '观察'

        if stage == '高潮期':
            return '持有观察' if rank_trend >= 0 else '考虑减仓'
        elif stage == '加速期':
            return '积极关注'
        elif stage == '主升浪':
            return '逢低布局'
        elif stage == '启动期':
            return '关注启动信号'
        else:
            return '观望'

    def _get_strategy_reason(self, latest: Dict, hot_days: int,
                              trend_desc: str, stage: str) -> str:
        """生成策略理由"""
        reasons = []

        if hot_days >= 6:
            reasons.append(f"近10天{hot_days}次热点")
        elif hot_days >= 3:
            reasons.append(f"近10天{hot_days}次热点")

        if trend_desc == '上升':
            reasons.append("排名持续上升")
        elif trend_desc == '下降':
            reasons.append("排名有所下降")

        reasons.append(f"当前处于{stage}")

        if latest.get('limit_up_count', 0) > 5:
            reasons.append(f"涨停{latest['limit_up_count']}家情绪高涨")

        return '; '.join(reasons) if reasons else '持续跟踪观察'

    def analyze_persistence_with_history(self, trade_date: str,
                                          hot_sectors_df: pd.DataFrame,
                                          historical_data: Dict[str, pd.DataFrame],
                                          lookback_days: int = 10,
                                          top_n: int = 10) -> pd.DataFrame:
        """
        使用提供的历史数据分析持续性

        这是主要的入口方法，由THSSectorTracker调用并提供历史数据

        Args:
            trade_date: 交易日期
            hot_sectors_df: 当前热点板块DataFrame
            historical_data: 历史数据字典 {date: DataFrame}
            lookback_days: 回溯天数
            top_n: 返回前N个

        Returns:
            DataFrame: 持续性分析结果
        """
        if hot_sectors_df.empty or 'is_hot' not in hot_sectors_df.columns:
            return pd.DataFrame()

        hot_threshold_days = self.persistence_config.get('hot_threshold_days', 3)
        current_hot = hot_sectors_df[hot_sectors_df['is_hot'] == True]

        if current_hot.empty:
            return pd.DataFrame()

        target_names = set(current_hot['name'].tolist())
        sector_type = current_hot.iloc[0].get('type', '概念') if not current_hot.empty else '概念'

        results = self._analyze_persistence_m_n(
            target_names, historical_data, lookback_days,
            hot_threshold_days, sector_type
        )

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        result_df = result_df[result_df['热点天数'] >= 1]
        result_df = result_df.sort_values('持续性评分', ascending=False)

        return result_df.head(top_n)
