"""
板块热点识别器

负责识别当日热点概念板块和热点行业板块

核心职责：
1. 概念板块热点识别 - 基于limit_cpt_list和涨停数据
2. 行业板块热点识别 - 基于涨幅、资金和涨停统计
3. 提供统一的热点板块评分和排序
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass

import loguru

logger = loguru.logger


@dataclass
class HotSpotResult:
    """热点识别结果"""
    ts_code: str
    name: str
    sector_type: str  # 概念/行业
    pct_change: float
    limit_up_count: int
    composite_score: float
    is_hot: bool
    hot_reason: str  # 成为热点的原因


class HotSpotDetector:
    """
    板块热点识别器

    将热点识别逻辑从THSSectorTracker中分离出来，职责单一：
    - 识别哪些板块是当日热点
    - 计算热点强度评分
    - 提供热点板块排序
    """

    def __init__(self, data_manager, sector_params: Dict):
        self.dm = data_manager
        self.sector_params = sector_params
        self._member_cache: Dict[str, pd.DataFrame] = {}

    def detect_concept_hotspots(self, concept_daily: pd.DataFrame,
                                 concept_codes: Set[str],
                                 code_to_name: Dict[str, str],
                                 limit_cpt_df: pd.DataFrame) -> pd.DataFrame:
        """
        识别热点概念板块

        概念板块特点：
        - 与limit_cpt_list数据高度相关
        - 情绪驱动，涨停家数是关键指标
        - 可以直接合并limit_cpt_list的涨停数据

        Args:
            concept_daily: 概念板块行情数据
            concept_codes: 概念板块代码集合
            code_to_name: 代码到名称的映射
            limit_cpt_df: limit_cpt_list数据

        Returns:
            DataFrame: 带热点标记的概念板块数据
        """
        logger.info("[HotSpotDetector] 开始识别热点概念板块...")

        # 1. 处理基础数据
        result_df = self._process_concept_data(
            concept_daily, concept_codes, code_to_name, limit_cpt_df
        )

        if result_df.empty:
            return pd.DataFrame()

        # 2. 概念板块专属评分
        result_df = self._score_concept_sectors(result_df)

        # 3. 标记热点概念
        result_df = self._mark_hot_concepts(result_df)

        hot_count = result_df['is_hot'].sum()
        logger.info(f"[HotSpotDetector] 热点概念识别完成: {hot_count}个热点")

        return result_df

    def detect_industry_hotspots(self, industry_daily: pd.DataFrame,
                                  industry_codes: Set[str],
                                  code_to_name: Dict[str, str],
                                  trade_date: str) -> pd.DataFrame:
        """
        识别热点行业板块

        行业板块特点：
        - 不直接使用limit_cpt_list（该数据主要是概念板块）
        - 通过成分股计算涨停家数
        - 更关注资金容量和持续性

        Args:
            industry_daily: 行业板块行情数据
            industry_codes: 行业板块代码集合
            code_to_name: 代码到名称的映射
            trade_date: 交易日期（用于获取涨停数据）

        Returns:
            DataFrame: 带热点标记的行业板块数据
        """
        logger.info("[HotSpotDetector] 开始识别热点行业板块...")

        # 1. 处理基础数据
        result_df = self._process_industry_data(
            industry_daily, industry_codes, code_to_name, trade_date
        )

        if result_df.empty:
            return pd.DataFrame()

        # 2. 行业板块专属评分
        result_df = self._score_industry_sectors(result_df)

        # 3. 标记热点行业
        result_df = self._mark_hot_industries(result_df)

        hot_count = result_df['is_hot'].sum()
        logger.info(f"[HotSpotDetector] 热点行业识别完成: {hot_count}个热点")

        return result_df

    def _process_concept_data(self, daily_df: pd.DataFrame,
                               concept_codes: Set[str],
                               code_to_name: Dict[str, str],
                               limit_cpt_df: pd.DataFrame) -> pd.DataFrame:
        """处理概念板块基础数据"""
        results = []

        for _, row in daily_df.iterrows():
            ts_code = row.get('ts_code')
            if ts_code not in concept_codes:
                continue

            name = code_to_name.get(ts_code, ts_code)
            pct_change = row.get('pct_change', 0) or 0
            avg_price = row.get('avg_price', 0) or 0
            vol = row.get('vol', 0) or 0

            # 计算成交额
            if avg_price > 0 and vol > 0:
                amount = (avg_price * vol) / 1e6
            else:
                amount = row.get('amount', 0) or 0

            results.append({
                'ts_code': ts_code,
                'name': name,
                'type': '概念',
                'pct_change': pct_change,
                'amount': amount,
                'vol': vol,
            })

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)

        # 添加成分股数量
        result_df['member_count'] = result_df['ts_code'].apply(
            lambda x: len(self._get_sector_members(x))
        )

        # 过滤小板块
        result_df = result_df[result_df['member_count'] >= 10]

        if result_df.empty:
            return pd.DataFrame()

        # 融合limit_cpt_list数据
        if not limit_cpt_df.empty:
            result_df = self._merge_limit_cpt_data(result_df, limit_cpt_df)
        else:
            result_df['limit_up_count'] = 0
            result_df['limit_cpt_rank'] = 999

        return result_df

    def _process_industry_data(self, daily_df: pd.DataFrame,
                               industry_codes: Set[str],
                               code_to_name: Dict[str, str],
                               trade_date: str) -> pd.DataFrame:
        """处理行业板块基础数据"""
        results = []

        for _, row in daily_df.iterrows():
            ts_code = row.get('ts_code')
            if ts_code not in industry_codes:
                continue

            name = code_to_name.get(ts_code, '')
            pct_change = row.get('pct_change', 0) or 0
            vol = row.get('vol', 0) or 0

            # 计算成交额
            high = row.get('high', 0) or 0
            low = row.get('low', 0) or 0
            avg_price = (high + low) / 2 if high > 0 and low > 0 else 0

            if avg_price > 0 and vol > 0:
                amount = (avg_price * vol) / 1e6
            else:
                amount = row.get('amount', 0) or 0

            results.append({
                'ts_code': ts_code,
                'name': name,
                'type': '行业',
                'pct_change': pct_change,
                'amount': amount,
                'vol': vol,
            })

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)

        # 添加成分股数量
        result_df['member_count'] = result_df['ts_code'].apply(
            lambda x: len(self._get_sector_members(x))
        )

        # 过滤小板块
        result_df = result_df[result_df['member_count'] >= 10]

        if result_df.empty:
            return pd.DataFrame()

        # 计算涨停统计
        result_df = self._calc_industry_limit_up_stats(result_df, trade_date)

        return result_df

    def _get_sector_members(self, ts_code: str) -> pd.DataFrame:
        """获取板块成分股（带缓存）"""
        if ts_code not in self._member_cache:
            members = self.dm.get_ths_member(ts_code=ts_code)
            self._member_cache[ts_code] = members
        return self._member_cache.get(ts_code, pd.DataFrame())

    def _calc_industry_limit_up_stats(self, industry_df: pd.DataFrame,
                                       trade_date: str) -> pd.DataFrame:
        """计算行业板块涨停统计"""
        # 获取当日涨停股票列表
        limit_up_codes = set()
        try:
            limit_up_df = self.dm.get_limit_up_pool(date=trade_date)
            if not limit_up_df.empty and 'code' in limit_up_df.columns:
                limit_up_codes = set(limit_up_df['code'].astype(str).str.replace(r'\.[A-Z]+$', '', regex=True))
        except Exception as e:
            logger.warning(f"[_calc_industry_limit_up_stats] 获取涨停数据失败: {e}")

        limit_up_counts = []
        for ts_code in industry_df['ts_code']:
            count = 0
            if limit_up_codes:
                members = self._get_sector_members(ts_code)
                if not members.empty:
                    if 'con_code' in members.columns:
                        member_codes = set(members['con_code'].astype(str).str.replace(r'\.[A-Z]+$', '', regex=True))
                    elif 'code' in members.columns:
                        member_codes = set(members['code'].astype(str).str.replace(r'\.[A-Z]+$', '', regex=True))
                    else:
                        member_codes = set()
                    count = len(member_codes & limit_up_codes)
            limit_up_counts.append(count)

        industry_df['limit_up_count'] = limit_up_counts
        industry_df['limit_cpt_rank'] = 999  # 行业板块不使用limit_cpt排名

        return industry_df

    def _merge_limit_cpt_data(self, ths_df: pd.DataFrame,
                               limit_cpt_df: pd.DataFrame) -> pd.DataFrame:
        """融合limit_cpt_list数据到同花顺板块数据"""
        name_col = 'name' if 'name' in limit_cpt_df.columns else 'concept'

        limit_cpt_df = limit_cpt_df.copy()
        limit_cpt_df['limit_cpt_rank'] = range(1, len(limit_cpt_df) + 1)
        limit_cpt_df['limit_cpt_score'] = (100 - limit_cpt_df['limit_cpt_rank']) * 0.5

        if 'up_nums' in limit_cpt_df.columns:
            limit_cpt_df['limit_up_count'] = limit_cpt_df['up_nums']
        if 'cons_nums' in limit_cpt_df.columns:
            limit_cpt_df['limit_cons_count'] = limit_cpt_df['cons_nums']

        merged_df = ths_df.merge(
            limit_cpt_df[[name_col, 'limit_cpt_rank', 'limit_cpt_score',
                         'limit_up_count', 'limit_cons_count']],
            left_on='name',
            right_on=name_col,
            how='left'
        )

        if name_col in merged_df.columns and name_col != 'name':
            merged_df = merged_df.drop(columns=[name_col])

        merged_df['limit_cpt_rank'] = merged_df['limit_cpt_rank'].fillna(999)
        merged_df['limit_up_count'] = merged_df['limit_up_count'].fillna(0)

        return merged_df

    def _score_concept_sectors(self, result_df: pd.DataFrame) -> pd.DataFrame:
        """概念板块专属评分"""
        max_rank = len(result_df)

        result_df['rank'] = result_df['pct_change'].rank(ascending=False, method='min')
        result_df['price_score'] = (max_rank - result_df['rank'] + 1) / max_rank * 100

        result_df['avg_amount'] = result_df['amount'] / result_df['member_count']
        result_df['amount_rank'] = result_df['avg_amount'].rank(ascending=False, method='min')
        result_df['amount_score'] = (max_rank - result_df['amount_rank'] + 1) / max_rank * 100

        if 'limit_cpt_rank' in result_df.columns:
            max_limit_up = result_df['limit_up_count'].max()

            def calc_concept_limit_score(row):
                if row['limit_up_count'] <= 0:
                    return 0
                count_score = min(row['limit_up_count'] / max_limit_up * 100, 100) if max_limit_up > 0 else 0
                rank_score = (20 - row['limit_cpt_rank']) / 20 * 100 if row['limit_cpt_rank'] <= 20 else 0
                return count_score * 0.7 + rank_score * 0.3

            result_df['limit_score'] = result_df.apply(calc_concept_limit_score, axis=1)
        else:
            result_df['limit_score'] = 0

        result_df['composite_score'] = (
            result_df['price_score'] * 0.35 +
            result_df['amount_score'] * 0.25 +
            result_df['limit_score'] * 0.40
        )

        return result_df

    def _score_industry_sectors(self, result_df: pd.DataFrame) -> pd.DataFrame:
        """行业板块专属评分"""
        max_rank = len(result_df)

        result_df['rank'] = result_df['pct_change'].rank(ascending=False, method='min')
        result_df['price_score'] = (max_rank - result_df['rank'] + 1) / max_rank * 100

        result_df['avg_amount'] = result_df['amount'] / result_df['member_count']
        result_df['amount_rank'] = result_df['avg_amount'].rank(ascending=False, method='min')
        result_df['amount_score'] = (max_rank - result_df['amount_rank'] + 1) / max_rank * 100

        if 'limit_up_count' in result_df.columns:
            max_limit_up = result_df['limit_up_count'].max()

            def calc_industry_limit_score(row):
                if row['limit_up_count'] <= 0:
                    return 0
                count_score = min(row['limit_up_count'] / max_limit_up * 100, 100) if max_limit_up > 0 else 0
                return count_score * 0.6

            result_df['limit_score'] = result_df.apply(calc_industry_limit_score, axis=1)
        else:
            result_df['limit_score'] = 0

        result_df['composite_score'] = (
            result_df['price_score'] * 0.30 +
            result_df['amount_score'] * 0.40 +
            result_df['limit_score'] * 0.30
        )

        return result_df

    def _mark_hot_concepts(self, result_df: pd.DataFrame) -> pd.DataFrame:
        """标记热点概念"""
        params = self.sector_params.get('概念', {})
        min_pct_change = params.get('min_pct_change', 5.0)

        def is_hot_concept(row):
            limit_up_count = row.get('limit_up_count', 0)
            limit_cpt_rank = row.get('limit_cpt_rank', 999)
            member_count = row.get('member_count', 1)
            pct_change = row.get('pct_change', 0)

            has_limit_up = limit_up_count > 0
            if not has_limit_up:
                return False

            is_top10_limit = limit_cpt_rank <= 10
            is_top20_limit = limit_cpt_rank <= 20

            limit_up_ratio = limit_up_count / member_count if member_count > 0 else 0
            has_good_spread = limit_up_ratio > 0.10

            has_strong_move = pct_change > min_pct_change

            if is_top10_limit:
                return True
            elif is_top20_limit and (has_good_spread or has_strong_move):
                return True
            elif has_good_spread and has_strong_move:
                return True

            return False

        result_df['is_hot'] = result_df.apply(is_hot_concept, axis=1)
        result_df['is_hot_concept'] = result_df['is_hot']
        result_df['is_hot_industry'] = False

        return result_df

    def _mark_hot_industries(self, result_df: pd.DataFrame) -> pd.DataFrame:
        """标记热点行业"""
        params = self.sector_params.get('行业', {})
        min_pct_change = params.get('min_pct_change', 3.0)

        def is_hot_industry(row):
            limit_up_count = row.get('limit_up_count', 0)
            member_count = row.get('member_count', 1)
            pct_change = row.get('pct_change', 0)

            rank = row.get('rank', 999)
            total_count = len(result_df)
            is_top_20_pct = rank <= total_count * 0.20
            has_strong_move = pct_change > min_pct_change
            is_hot_by_price = is_top_20_pct and has_strong_move

            amount_rank = row.get('amount_rank', 999)
            is_high_amount = amount_rank <= total_count * 0.30

            has_limit_up = limit_up_count > 0

            limit_up_ratio = limit_up_count / member_count if member_count > 0 else 0
            has_spread = limit_up_ratio > 0.05

            if is_hot_by_price and is_high_amount:
                return True
            elif is_hot_by_price and has_limit_up:
                return True
            elif is_high_amount and has_spread:
                return True

            return False

        result_df['is_hot'] = result_df.apply(is_hot_industry, axis=1)
        result_df['is_hot_concept'] = False
        result_df['is_hot_industry'] = result_df['is_hot']

        return result_df
