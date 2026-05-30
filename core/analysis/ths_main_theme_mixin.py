"""
THS 市场主线识别 Mixin

从 ``ths_sector_tracker.py`` 拆分出来的主线识别（四维评分）方法集合。

该 Mixin 假设宿主类提供以下属性/方法：
  - ``self.dm``                          : DataManager
  - ``self.analyze_concept_sectors``     : 概念板块当日分析
  - ``self.analyze_industry_sectors``    : 行业板块当日分析
  - ``self.analyze_concept_persistence`` : 概念持续性分析（``THSPersistenceMixin``）
  - ``self.analyze_industry_persistence``: 行业持续性分析
  - ``self.get_sector_members``          : 板块成分股
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import loguru

logger = loguru.logger


class THSMainThemeMixin:
    """市场主线识别 Mixin（四维评分模型）。

    四维评分体系：
      1. 涨停集中度 (30%)
      2. 梯队完整性 (30%)
      3. 持续性     (25%)
      4. 龙头强度   (15%)
    """

    def identify_main_themes(self, trade_date: str,
                             hot_concepts_df: pd.DataFrame = None,
                             hot_industries_df: pd.DataFrame = None,
                             concept_persistence_df: pd.DataFrame = None,
                             industry_persistence_df: pd.DataFrame = None,
                             top_n: int = 10) -> pd.DataFrame:
        """识别市场主线 (四维评分模型)。"""
        logger.info("=" * 80)
        logger.info(f"【identify_main_themes】开始识别市场主线，日期: {trade_date}")

        if hot_concepts_df is None or hot_concepts_df.empty:
            hot_concepts_df = self.analyze_concept_sectors(trade_date, top_n=20)
        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=20)
        if concept_persistence_df is None:
            concept_persistence_df = self.analyze_concept_persistence(trade_date, top_n=15)
        if industry_persistence_df is None:
            industry_persistence_df = self.analyze_industry_persistence(trade_date, top_n=15)

        limit_up_df = self.dm.get_limit_up_pool(trade_date)

        all_candidates = []

        for _, row in hot_concepts_df.iterrows():
            sector_name = row.get('name', '')
            sector_code = row.get('ts_code', '')
            if not sector_name:
                continue
            scores = self._calc_main_theme_scores(
                sector_name, sector_code, '概念', trade_date,
                row, limit_up_df, concept_persistence_df
            )
            if scores:
                all_candidates.append(scores)

        for _, row in hot_industries_df.iterrows():
            sector_name = row.get('name', '')
            sector_code = row.get('ts_code', '')
            if not sector_name:
                continue
            scores = self._calc_main_theme_scores(
                sector_name, sector_code, '行业', trade_date,
                row, limit_up_df, industry_persistence_df
            )
            if scores:
                all_candidates.append(scores)

        if not all_candidates:
            logger.warning("[identify_main_themes] 未找到任何候选主线")
            return pd.DataFrame()

        result_df = pd.DataFrame(all_candidates)
        result_df = result_df.sort_values('综合评分', ascending=False).head(top_n).reset_index(drop=True)
        result_df['排名'] = range(1, len(result_df) + 1)

        logger.info("-" * 80)
        logger.info(f"【主线识别结果】共 {len(result_df)} 条主线")
        for idx, row in result_df.iterrows():
            logger.info(
                f"  #{row['排名']} {row['板块名称']}({row['板块类型']}) "
                f"综合{row['综合评分']:.1f} "
                f"| 涨停{row['涨停家数']}家(集中度{row['涨停集中度']:.1f}) "
                f"| 梯队{row['梯队完整性']:.1f} "
                f"| 持续{row['持续性评分']:.1f} "
                f"| 龙头{row['龙头强度']:.1f} "
                f"| 阶段:{row['所处阶段']}"
            )
        logger.info("=" * 80)

        return result_df

    def _calc_main_theme_scores(self, sector_name: str, sector_code: str,
                                 sector_type: str, trade_date: str,
                                 hot_row: pd.Series,
                                 limit_up_df: pd.DataFrame,
                                 persistence_df: pd.DataFrame) -> Optional[Dict]:
        """计算单个板块的主线强度四维评分"""
        try:
            members = self.get_sector_members(sector_code)
            if members.empty:
                return None

            member_codes = self._extract_member_codes(members)
            if not member_codes:
                return None

            limit_up_count, limit_up_board_heights = self._count_sector_limit_up_detail(
                member_codes, limit_up_df
            )
            echelon_score, echelon_detail = self._calc_echelon_completeness(limit_up_board_heights)
            persistence_score, hot_days, stage = self._get_persistence_info(
                sector_name, persistence_df
            )
            leader_strength, max_board_height = self._calc_leader_strength(
                limit_up_board_heights, limit_up_df, member_codes
            )

            composite = (
                limit_up_count * 0.30 +
                echelon_score * 0.30 +
                persistence_score * 0.25 +
                leader_strength * 0.15
            )

            pct_change = hot_row.get('pct_change', 0) or 0
            amount = hot_row.get('amount', 0) or 0

            return {
                '板块名称': sector_name,
                '板块代码': sector_code,
                '板块类型': sector_type,
                '涨跌幅': round(pct_change, 2),
                '成交额': amount,
                '涨停家数': limit_up_count,
                '涨停集中度': round(limit_up_count, 1),
                '梯队完整性': round(echelon_score, 1),
                '梯队详情': echelon_detail,
                '持续性评分': round(persistence_score, 1),
                '热点天数': hot_days,
                '龙头强度': round(leader_strength, 1),
                '最高连板': max_board_height,
                '综合评分': round(composite, 1),
                '所处阶段': stage,
                '操作建议': self._get_main_theme_advice(stage, composite),
            }
        except Exception as e:
            logger.warning(f"[_calc_main_theme_scores] {sector_name} 评分失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _extract_member_codes(self, members: pd.DataFrame) -> Set[str]:
        """从成分股 DataFrame 提取标准化代码集合"""
        codes = set()
        col = None
        if 'con_code' in members.columns:
            col = 'con_code'
        elif 'code' in members.columns:
            col = 'code'
        elif 'ts_code' in members.columns:
            col = 'ts_code'

        if col:
            for c in members[col].astype(str):
                codes.add(c.split('.')[0].zfill(6))
        return codes

    def _count_sector_limit_up_detail(self, member_codes: Set[str],
                                       limit_up_df: pd.DataFrame) -> Tuple[int, List[int]]:
        """统计板块内涨停家数及连板高度"""
        if limit_up_df.empty:
            return 0, []

        code_col = None
        for col_name in ['代码', 'code', 'ts_code']:
            if col_name in limit_up_df.columns:
                code_col = col_name
                break
        if not code_col:
            return 0, []

        board_col = None
        for col_name in ['连板数', 'limit_times', 'board_height']:
            if col_name in limit_up_df.columns:
                board_col = col_name
                break

        count = 0
        heights = []
        for _, row in limit_up_df.iterrows():
            code = str(row[code_col]).split('.')[0].zfill(6)
            if code in member_codes:
                count += 1
                height = int(row[board_col]) if board_col and pd.notna(row.get(board_col)) else 1
                heights.append(height)

        return count, heights

    def _calc_echelon_completeness(self, board_heights: List[int]) -> Tuple[float, str]:
        """计算梯队完整性得分（0-100）"""
        if not board_heights:
            return 0.0, '无涨停'

        height_counts = {}
        for h in board_heights:
            height_counts[h] = height_counts.get(h, 0) + 1

        score = 0.0
        detail_parts = []

        if max(board_heights) >= 4:
            score += 40
            count_4plus = sum(v for h, v in height_counts.items() if h >= 4)
            detail_parts.append(f'高标{count_4plus}只')
        if 3 in height_counts:
            score += 30
            detail_parts.append(f'三板{height_counts[3]}只')
        if 2 in height_counts:
            score += 20
            detail_parts.append(f'二板{height_counts[2]}只')
        if 1 in height_counts:
            score += 10
            detail_parts.append(f'首板{height_counts[1]}只')

        for h, cnt in height_counts.items():
            if cnt >= 3:
                score += 5

        score = min(score, 100.0)
        detail = ' | '.join(detail_parts) if detail_parts else '无梯队'
        return score, detail

    def _get_persistence_info(self, sector_name: str,
                               persistence_df: pd.DataFrame) -> Tuple[float, int, str]:
        """从持续性结果中查询给定板块的评分/天数/阶段"""
        if persistence_df.empty:
            return 30.0, 1, '观察期'

        match = persistence_df[persistence_df['板块名称'] == sector_name]
        if match.empty:
            return 30.0, 1, '观察期'

        row = match.iloc[0]
        score = float(row.get('持续性评分', 30))
        days = int(row.get('热点天数', 1))
        stage = str(row.get('所处阶段', '观察期'))
        return score, days, stage

    def _calc_leader_strength(self, board_heights: List[int],
                               limit_up_df: pd.DataFrame,
                               member_codes: Set[str]) -> Tuple[float, int]:
        """计算龙头强度（最高连板 + 封单强度）"""
        max_height = max(board_heights) if board_heights else 0
        if max_height <= 0:
            return 0.0, 0

        base_score = min(max_height * 10, 100)
        fd_bonus = 0
        if not limit_up_df.empty and max_height >= 2:
            code_col = None
            for col_name in ['代码', 'code', 'ts_code']:
                if col_name in limit_up_df.columns:
                    code_col = col_name
                    break

            board_col = None
            for col_name in ['连板数', 'limit_times']:
                if col_name in limit_up_df.columns:
                    board_col = col_name
                    break

            fd_col = None
            for col_name in ['封单金额', 'fd_amount']:
                if col_name in limit_up_df.columns:
                    fd_col = col_name
                    break

            if code_col and board_col:
                for _, row in limit_up_df.iterrows():
                    code = str(row[code_col]).split('.')[0].zfill(6)
                    height = int(row[board_col]) if board_col and pd.notna(row.get(board_col)) else 1
                    if code in member_codes and height == max_height:
                        if fd_col and pd.notna(row.get(fd_col)):
                            fd_amount = float(row[fd_col])
                            if fd_amount > 1e8:
                                fd_bonus = 10
                            elif fd_amount > 5e7:
                                fd_bonus = 5
                        break

        strength = min(base_score + fd_bonus, 100.0)
        return strength, max_height

    @staticmethod
    def _get_main_theme_advice(stage: str, composite: float) -> str:
        """根据阶段和评分生成操作建议"""
        if stage in ('启动期', '加速期', '主升浪'):
            if composite >= 80:
                return '核心主线，积极做多'
            elif composite >= 60:
                return '主线确认，逢低参与'
            else:
                return '关注主线，试错参与'
        elif stage == '高潮期':
            return '持有观察，注意分歧'
        elif stage == '衰退期':
            return '逐步退出，等待新主线'
        else:
            return '观察等待，确认信号'
