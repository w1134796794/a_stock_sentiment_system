"""
概念连板梯队分析器（简化版）

核心功能：
1. 通过limit_cpt_list获取最强板块
2. 通过ths_member获取板块成分股
3. 匹配涨停池中的成分股
4. 分析每个概念的涨停梯队分布
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


@dataclass
class ConceptHierarchy:
    """概念连板梯队信息"""
    concept_name: str              # 概念名称
    ts_code: str                   # 板块代码
    total_limit_up: int = 0        # 涨停总数
    board_distribution: Dict[int, int] = field(default_factory=dict)  # 连板分布
    stocks: List[Dict] = field(default_factory=list)  # 涨停股票列表
    max_board_count: int = 0       # 最高连板数
    leader_stock: Optional[Dict] = None  # 龙头股


class ConceptBoardHierarchyAnalyzer:
    """
    概念连板梯队分析器（简化版）
    
    使用示例：
        analyzer = ConceptBoardHierarchyAnalyzer(data_manager)
        result = analyzer.analyze_hierarchy(limit_up_df, trade_date)
    """

    def __init__(self, data_manager):
        self.dm = data_manager

    def analyze_hierarchy(self, limit_up_df: pd.DataFrame, trade_date: str) -> Dict[str, ConceptHierarchy]:
        """
        分析概念连板梯队

        Args:
            limit_up_df: 当日涨停池DataFrame
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            Dict[str, ConceptHierarchy]: 概念名称 -> 梯队信息
        """
        if limit_up_df.empty:
            logger.warning("[analyze_hierarchy] 涨停池为空")
            return {}

        logger.info("=" * 80)
        logger.info(f"【概念连板梯队分析】涨停股票数: {len(limit_up_df)}")

        # 1. 获取最强板块列表（limit_cpt_list）
        limit_cpt_df = self.dm.get_limit_cpt_list(trade_date=trade_date)
        if limit_cpt_df.empty:
            logger.warning("[analyze_hierarchy] 无法获取最强板块数据")
            return {}

        logger.info(f"[analyze_hierarchy] 获取到 {len(limit_cpt_df)} 个最强板块")
        logger.debug(f"[analyze_hierarchy] limit_cpt_list列名: {list(limit_cpt_df.columns)}")
        logger.debug(f"[analyze_hierarchy] limit_cpt_list前3行:\n{limit_cpt_df.head(3)}")

        # 2. 标准化涨停池股票代码
        limit_up_df = self._normalize_limit_up_df(limit_up_df)

        # 3. 分析每个板块的涨停梯队
        concept_hierarchies = {}

        for _, row in limit_cpt_df.iterrows():
            concept_name = row.get('name', row.get('概念', ''))
            ts_code = row.get('ts_code', row.get('code', ''))  # limit_cpt_list中的板块代码

            if not ts_code:
                continue

            # 获取板块成分股
            logger.debug(f"[{concept_name}] 获取成分股，ts_code={ts_code}")
            members = self.dm.get_ths_member(ts_code=ts_code)
            if members.empty:
                logger.debug(f"[{concept_name}] 成分股为空，ts_code={ts_code}")
                continue
            logger.debug(f"[{concept_name}] 获取到 {len(members)} 只成分股")

            # 匹配涨停成分股
            matched_stocks = self._match_limit_up_stocks(members, limit_up_df)

            if not matched_stocks:
                continue

            # 构建梯队信息
            hierarchy = self._build_hierarchy(concept_name, ts_code, matched_stocks)
            concept_hierarchies[concept_name] = hierarchy

        # 4. 输出分析结果
        self._print_hierarchy_report(concept_hierarchies)

        logger.info("=" * 80)
        return concept_hierarchies

    def _normalize_limit_up_df(self, limit_up_df: pd.DataFrame) -> pd.DataFrame:
        """标准化涨停池数据"""
        df = limit_up_df.copy()

        # 标准化代码列
        if '代码' in df.columns:
            df['code_normalized'] = df['代码'].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
            df['name'] = df['名称']
            board_col = '连板数'
        else:
            df['code_normalized'] = df['code'].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
            board_col = 'limit_up_count' if 'limit_up_count' in df.columns else '连板数'

        # 确保连板数列存在
        if board_col not in df.columns:
            df[board_col] = 1

        df['board_count'] = df[board_col].fillna(1).astype(int)

        return df

    def _match_limit_up_stocks(self, members: pd.DataFrame, limit_up_df: pd.DataFrame) -> List[Dict]:
        """
        匹配板块成分股与涨停池

        Returns:
            List[Dict]: 匹配的涨停股票列表
        """
        matched = []

        # 标准化成分股代码
        if 'con_code' in members.columns:
            members = members.copy()
            members['code_normalized'] = members['con_code'].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
        else:
            return []

        # 获取成分股代码集合
        member_codes = set(members['code_normalized'].values)

        # 匹配涨停池
        for _, row in limit_up_df.iterrows():
            stock_code = row['code_normalized']
            if stock_code in member_codes:
                matched.append({
                    'code': stock_code,
                    'name': row.get('name', ''),
                    'board_count': row.get('board_count', 1)
                })

        return matched

    def _build_hierarchy(self, concept_name: str, ts_code: str, stocks: List[Dict]) -> ConceptHierarchy:
        """构建概念梯队信息"""
        hierarchy = ConceptHierarchy(concept_name=concept_name, ts_code=ts_code)

        hierarchy.total_limit_up = len(stocks)
        hierarchy.stocks = stocks

        # 统计连板分布
        for stock in stocks:
            board_count = stock['board_count']
            if board_count not in hierarchy.board_distribution:
                hierarchy.board_distribution[board_count] = 0
            hierarchy.board_distribution[board_count] += 1

            # 更新最高连板
            if board_count > hierarchy.max_board_count:
                hierarchy.max_board_count = board_count
                hierarchy.leader_stock = stock

        return hierarchy

    def _print_hierarchy_report(self, concept_hierarchies: Dict[str, ConceptHierarchy]):
        """打印梯队分析报告"""
        if not concept_hierarchies:
            logger.info("[概念连板梯队] 无数据")
            return

        # 按涨停家数排序
        sorted_concepts = sorted(
            concept_hierarchies.items(),
            key=lambda x: x[1].total_limit_up,
            reverse=True
        )

        logger.info(f"[概念连板梯队] 共 {len(sorted_concepts)} 个概念有涨停梯队")
        logger.info("-" * 80)

        # 打印前10个概念
        for i, (concept_name, h) in enumerate(sorted_concepts[:10], 1):
            logger.info(f"\n{i}. 【{concept_name}】")
            logger.info(f"   涨停总数: {h.total_limit_up}家 | 最高连板: {h.max_board_count}板")

            # 梯队分布
            board_dist_str = " | ".join([
                f"{board}板:{count}家"
                for board, count in sorted(h.board_distribution.items(), reverse=True)
            ])
            logger.info(f"   梯队分布: {board_dist_str}")

            # 龙头股
            if h.leader_stock:
                logger.info(f"   龙头股: {h.leader_stock['name']}({h.leader_stock['code']})")

        logger.info("-" * 80)

    def format_hierarchy_for_report(self, concept_hierarchies: Dict[str, ConceptHierarchy],
                                     top_n: int = 10) -> str:
        """
        格式化梯队信息为报告文本

        Returns:
            str: 格式化后的报告文本
        """
        if not concept_hierarchies:
            return "  无概念连板梯队数据"

        # 按涨停家数排序
        sorted_concepts = sorted(
            concept_hierarchies.items(),
            key=lambda x: x[1].total_limit_up,
            reverse=True
        )

        lines = []
        lines.append(f"识别到 {len(sorted_concepts)} 个强势概念:")

        # 格式化前N个概念
        for concept_name, h in sorted_concepts[:top_n]:
            # 构建梯队分布字符串（从高到低）
            board_parts = []
            for board in sorted(h.board_distribution.keys(), reverse=True):
                count = h.board_distribution[board]
                board_parts.append(f"{board}板{count}家")

            board_str = ", ".join(board_parts)
            lines.append(f"  【{concept_name}】{board_str}")

        return "\n".join(lines)

    def get_strong_concepts(self, concept_hierarchies: Dict[str, ConceptHierarchy],
                            min_limit_up: int = 3, min_board_count: int = 2) -> List[ConceptHierarchy]:
        """
        获取强势概念板块

        Args:
            concept_hierarchies: 概念梯队数据
            min_limit_up: 最小涨停家数
            min_board_count: 最小连板高度

        Returns:
            List[ConceptHierarchy]: 强势概念列表
        """
        strong_concepts = []

        for hierarchy in concept_hierarchies.values():
            if hierarchy.total_limit_up >= min_limit_up and hierarchy.max_board_count >= min_board_count:
                strong_concepts.append(hierarchy)

        # 按涨停家数排序
        strong_concepts.sort(key=lambda x: x.total_limit_up, reverse=True)

        return strong_concepts

    def export_to_dataframe(self, concept_hierarchies: Dict[str, ConceptHierarchy]) -> pd.DataFrame:
        """导出为DataFrame"""
        if not concept_hierarchies:
            return pd.DataFrame()

        records = []
        for concept_name, h in concept_hierarchies.items():
            record = {
                'concept_name': concept_name,
                'ts_code': h.ts_code,
                'total_limit_up': h.total_limit_up,
                'max_board_count': h.max_board_count,
                'board_distribution': str(h.board_distribution),
            }

            if h.leader_stock:
                record['leader_name'] = h.leader_stock['name']
                record['leader_code'] = h.leader_stock['code']
            else:
                record['leader_name'] = ''
                record['leader_code'] = ''

            records.append(record)

        df = pd.DataFrame(records)
        df = df.sort_values('total_limit_up', ascending=False)

        return df
