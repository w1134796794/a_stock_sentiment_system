"""
市场指标公共组件库

功能：
1. 连板梯队完整性计算（消除emotion_cycle_engine和ths_sector_tracker的重复）
2. 龙头股识别（空间/强度/时间龙头）
3. 涨停结构分析
4. 市场情绪指标计算

设计原则：
- 纯函数设计，无副作用
- 输入输出明确，易于测试
- 配置外置，通过参数传入
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from datetime import datetime
import loguru

logger = loguru.logger


# =============================================================================
# 数据模型
# =============================================================================

@dataclass
class LimitUpStock:
    """涨停股票数据模型"""
    code: str                   # 股票代码
    name: str                   # 股票名称
    board_height: int = 1       # 连板数
    limit_up_amount: float = 0  # 封单金额
    first_limit_time: Optional[str] = None  # 首次涨停时间 (HH:MM:SS)
    open_times: int = 0         # 炸板次数
    industry: str = ""          # 所属行业


@dataclass
class HierarchyAnalysisResult:
    """梯队分析结果"""
    completeness_score: float   # 完整性得分 (0-100)
    max_height: int             # 最高连板数
    total_count: int            # 涨停总数
    structure_type: str         # 结构类型
    height_distribution: Dict[int, int]  # 各高度分布 {1: 20, 2: 10, ...}
    stats_by_height: Dict[str, Dict]     # 各高度统计


@dataclass
class LeaderStock:
    """龙头股数据"""
    type: str                   # 龙头类型：空间/强度/时间
    code: str
    name: str
    board_height: int = 1
    limit_up_amount: float = 0
    first_limit_time: Optional[str] = None
    reason: str = ""            # 成为龙头的原因


@dataclass
class LeadersAnalysisResult:
    """龙头股分析结果"""
    leaders: List[LeaderStock]  # 识别的龙头股列表
    space_leader: Optional[LeaderStock] = None   # 空间龙头
    strength_leader: Optional[LeaderStock] = None  # 强度龙头
    time_leader: Optional[LeaderStock] = None    # 时间龙头
    leader_score: int = 0       # 龙头股综合得分


# =============================================================================
# 梯队完整性计算
# =============================================================================

def calculate_echelon_score(
    board_distribution: Dict[int, int],
    ideal_ratios: List[float] = None,
    min_ratio_threshold: float = 0.5
) -> float:
    """
    计算连板梯队完整性得分
    
    理想梯队: 1B > 2B > 3B > 4B > 5B (金字塔结构)
    得分越高表示梯队越完整
    
    Args:
        board_distribution: 连板分布 {1: 20, 2: 10, 3: 5, ...}
        ideal_ratios: 理想比例递减 [0.6, 0.4, 0.25, 0.15, 0.1]
        min_ratio_threshold: 至少达到理想值的比例阈值
        
    Returns:
        float: 完整性得分 (0-1)
    """
    if not board_distribution or 1 not in board_distribution:
        return 0.0
    
    count_1b = board_distribution.get(1, 0)
    if count_1b == 0:
        return 0.0
    
    # 默认理想比例
    if ideal_ratios is None:
        ideal_ratios = [0.6, 0.4, 0.25, 0.15, 0.1]
    
    # 计算各层占比
    ratios = []
    for height in range(2, 7):  # 2B到6B
        count = board_distribution.get(height, 0)
        ratio = count / count_1b if count_1b > 0 else 0
        ratios.append(ratio)
    
    # 计算与理想比例的匹配度
    score = 0
    for actual, ideal in zip(ratios, ideal_ratios):
        if actual >= ideal * min_ratio_threshold:  # 至少达到理想值的50%
            score += 1
    
    return score / len(ideal_ratios)


def analyze_limit_up_hierarchy(
    zt_stocks: List[Dict],
    hierarchy_weights: Dict[str, int] = None
) -> HierarchyAnalysisResult:
    """
    分析涨停梯队结构
    
    Args:
        zt_stocks: 涨停股票列表，每个元素包含board_height等字段
        hierarchy_weights: 梯队评分权重配置
        
    Returns:
        HierarchyAnalysisResult: 梯队分析结果
    """
    if not zt_stocks:
        return HierarchyAnalysisResult(
            completeness_score=0,
            max_height=0,
            total_count=0,
            structure_type='无涨停',
            height_distribution={},
            stats_by_height={}
        )
    
    # 默认权重
    if hierarchy_weights is None:
        hierarchy_weights = {
            'has_leader': 20,
            'has_second_board': 20,
            'multiple_second_board': 10,
            'has_third_plus': 20,
            'first_board_count_3': 20,
            'first_board_count_5': 10,
        }
    
    # 按连板高度分组
    height_groups = {}
    for stock in zt_stocks:
        height = stock.get('board_height', 1)
        if height not in height_groups:
            height_groups[height] = []
        height_groups[height].append(stock)
    
    max_height = max(height_groups.keys()) if height_groups else 0
    total_count = len(zt_stocks)
    
    # 统计各梯队数量
    stats_by_height = {}
    for height in range(1, max_height + 1):
        count = len(height_groups.get(height, []))
        stats_by_height[f'{height}板'] = {
            'count': count,
            'stocks': [s.get('name', s.get('code', '')) for s in height_groups.get(height, [])]
        }
    
    # 计算梯队完整性得分
    completeness_score = 0
    first_board_count = len(height_groups.get(1, []))
    
    # 1. 有最高板（1板也算）
    if max_height >= 1:
        completeness_score += hierarchy_weights.get('has_leader', 20)
    
    # 2. 有2板梯队
    if max_height >= 2:
        second_board_count = len(height_groups.get(2, []))
        completeness_score += hierarchy_weights.get('has_second_board', 20)
        if second_board_count >= 2:
            completeness_score += hierarchy_weights.get('multiple_second_board', 10)
    
    # 3. 有3板及以上
    if max_height >= 3:
        completeness_score += hierarchy_weights.get('has_third_plus', 20)
    
    # 4. 首板数量充足
    if first_board_count >= 3:
        completeness_score += hierarchy_weights.get('first_board_count_3', 20)
    if first_board_count >= 5:
        completeness_score += hierarchy_weights.get('first_board_count_5', 10)
    
    # 分类梯队结构类型
    structure_type = classify_hierarchy_structure(max_height, height_groups)
    
    return HierarchyAnalysisResult(
        completeness_score=completeness_score,
        max_height=max_height,
        total_count=total_count,
        structure_type=structure_type,
        height_distribution={h: len(stocks) for h, stocks in height_groups.items()},
        stats_by_height=stats_by_height
    )


def classify_hierarchy_structure(max_height: int, height_groups: Dict) -> str:
    """
    分类梯队结构类型
    
    Args:
        max_height: 最高连板数
        height_groups: 按高度分组的字典
        
    Returns:
        str: 结构类型描述
    """
    if max_height == 0:
        return '无涨停'
    
    if max_height == 1:
        first_board_count = len(height_groups.get(1, []))
        if first_board_count >= 5:
            return '首板爆发'
        elif first_board_count >= 3:
            return '首板活跃'
        else:
            return '零星首板'
    
    if max_height >= 5:
        first_board_count = len(height_groups.get(1, []))
        return '超级强势' if first_board_count >= 3 else '龙头独舞'
    
    elif max_height >= 3:
        has_second = len(height_groups.get(2, [])) > 0
        return '梯队完整' if has_second else '高位独苗'
    
    elif max_height == 2:
        second_count = len(height_groups.get(2, []))
        first_count = len(height_groups.get(1, []))
        if second_count >= 2 and first_count >= 3:
            return '梯队较好'
        else:
            return '初步成型'
    
    return '未知'


# =============================================================================
# 龙头股识别
# =============================================================================

def identify_leaders(
    zt_stocks: List[Dict],
    leader_weights: Dict[str, int] = None
) -> LeadersAnalysisResult:
    """
    识别龙头股
    
    龙头标准：
    1. 空间龙头：连板数最高
    2. 强度龙头：封单金额最大
    3. 时间龙头：最先涨停
    
    Args:
        zt_stocks: 涨停股票列表
        leader_weights: 龙头评分权重
        
    Returns:
        LeadersAnalysisResult: 龙头股分析结果
    """
    if not zt_stocks:
        return LeadersAnalysisResult(leaders=[])
    
    if leader_weights is None:
        leader_weights = {
            'space_leader': 10,
            'strength_leader': 10,
            'time_leader': 10
        }
    
    leaders = []
    result = LeadersAnalysisResult(leaders=[])
    
    # 1. 空间龙头（连板数最高）
    max_height = max(s.get('board_height', 1) for s in zt_stocks)
    space_leaders = [s for s in zt_stocks if s.get('board_height', 1) == max_height]
    if space_leaders:
        leader = LeaderStock(
            type='空间龙头',
            code=space_leaders[0].get('code', ''),
            name=space_leaders[0].get('name', ''),
            board_height=max_height,
            limit_up_amount=space_leaders[0].get('limit_up_amount', 0),
            reason=f'最高{max_height}连板'
        )
        leaders.append(leader)
        result.space_leader = leader
    
    # 2. 强度龙头（封单金额最大）
    max_amount_stock = max(zt_stocks, key=lambda x: x.get('limit_up_amount', 0))
    if max_amount_stock.get('limit_up_amount', 0) > 0:
        leader = LeaderStock(
            type='强度龙头',
            code=max_amount_stock.get('code', ''),
            name=max_amount_stock.get('name', ''),
            board_height=max_amount_stock.get('board_height', 1),
            limit_up_amount=max_amount_stock.get('limit_up_amount', 0),
            reason=f'封单金额最大 {max_amount_stock.get("limit_up_amount", 0)/10000:.0f}万'
        )
        leaders.append(leader)
        result.strength_leader = leader
    
    # 3. 时间龙头（最先涨停）
    stocks_with_time = [s for s in zt_stocks if s.get('first_limit_time')]
    if stocks_with_time:
        earliest_stock = min(stocks_with_time, key=lambda x: x.get('first_limit_time', '99:99:99'))
        leader = LeaderStock(
            type='时间龙头',
            code=earliest_stock.get('code', ''),
            name=earliest_stock.get('name', ''),
            board_height=earliest_stock.get('board_height', 1),
            first_limit_time=earliest_stock.get('first_limit_time'),
            reason=f'最先涨停 {earliest_stock.get("first_limit_time", "")}'
        )
        leaders.append(leader)
        result.time_leader = leader
    
    result.leaders = leaders
    
    # 计算龙头得分
    leader_score = 0
    leader_score_per_type = leader_weights.get('space_leader', 10)
    if len(leaders) >= 3:
        leader_score = leader_score_per_type * 3  # 有空间、强度、时间龙头
    elif len(leaders) >= 2:
        leader_score = leader_score_per_type * 2
    elif len(leaders) >= 1:
        leader_score = leader_score_per_type
    
    result.leader_score = leader_score
    
    return result


# =============================================================================
# 市场情绪指标计算
# =============================================================================

def calculate_market_sentiment_indicators(
    limit_up_df: pd.DataFrame,
    limit_down_df: pd.DataFrame = None,
    prev_limit_up_df: pd.DataFrame = None
) -> Dict[str, Any]:
    """
    计算市场情绪指标
    
    Args:
        limit_up_df: 当日涨停数据
        limit_down_df: 当日跌停数据（可选）
        prev_limit_up_df: 前天涨停数据（用于T+1溢价计算：前天涨停→昨日开盘买→今日开盘卖）
        
    Returns:
        Dict: 市场情绪指标
    """
    if limit_up_df is None or limit_up_df.empty:
        return {
            'limit_up_count': 0,
            'max_board_height': 0,
            'broken_rate': 0.0,
            'continuous_rate': 0.0,
            'limit_down_count': 0,
            'limit_down_ratio': 0.0,
        }
    
    # 涨停家数
    limit_up_count = len(limit_up_df)
    
    # 最高连板高度
    limit_times_col = 'limit_times' if 'limit_times' in limit_up_df.columns else '连板数'
    if limit_times_col in limit_up_df.columns:
        max_board_height = limit_up_df[limit_times_col].max()
        if pd.isna(max_board_height):
            max_board_height = 1
    else:
        max_board_height = 1
    max_board_height = int(max_board_height)
    
    # 炸板率
    open_times_col = 'open_times' if 'open_times' in limit_up_df.columns else '炸板次数'
    if open_times_col in limit_up_df.columns:
        broken_count = len(limit_up_df[limit_up_df[open_times_col] > 0])
        broken_rate = (broken_count / limit_up_count) * 100 if limit_up_count > 0 else 0
    else:
        broken_rate = 0.0
    
    # 连板率
    if limit_times_col in limit_up_df.columns:
        continuous_count = len(limit_up_df[limit_up_df[limit_times_col] >= 2])
        continuous_rate = (continuous_count / limit_up_count * 100) if limit_up_count > 0 else 0
    else:
        continuous_rate = 0.0
    
    # 跌停数据
    limit_down_count = len(limit_down_df) if limit_down_df is not None else 0
    limit_down_ratio = limit_down_count / limit_up_count if limit_up_count > 0 else 0
    
    # 连板分布
    board_distribution = {}
    if limit_times_col in limit_up_df.columns:
        board_distribution = limit_up_df[limit_times_col].value_counts().to_dict()
    
    return {
        'limit_up_count': limit_up_count,
        'max_board_height': max_board_height,
        'broken_rate': round(broken_rate, 2),
        'continuous_rate': round(continuous_rate, 2),
        'limit_down_count': limit_down_count,
        'limit_down_ratio': round(limit_down_ratio, 2),
        'board_distribution': board_distribution,
    }


# =============================================================================
# 工具函数
# =============================================================================

def convert_df_to_limit_up_stocks(df: pd.DataFrame) -> List[Dict]:
    """
    将DataFrame转换为涨停股票列表
    
    Args:
        df: 涨停数据DataFrame
        
    Returns:
        List[Dict]: 标准化格式的股票列表
    """
    if df is None or df.empty:
        return []
    
    stocks = []
    for _, row in df.iterrows():
        stock = {
            'code': row.get('code', row.get('ts_code', '')),
            'name': row.get('name', ''),
            'board_height': row.get('limit_times', row.get('连板数', 1)),
            'limit_up_amount': row.get('limit_up_amount', row.get('封单金额', 0)),
            'first_limit_time': row.get('first_limit_time', row.get('首次涨停时间', None)),
            'open_times': row.get('open_times', row.get('炸板次数', 0)),
            'industry': row.get('industry', row.get('所属行业', '')),
        }
        stocks.append(stock)
    
    return stocks