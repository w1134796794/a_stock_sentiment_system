"""
个股板块地位量化 - Stock Sector Position Quantification

职责：量化每只股票在其板块中的"地位"

地位分类：
  - 空间龙头：板块内最高连板
  - 强度龙头：板块内最早封板/最强封单
  - 中军：板块内最大市值涨停
  - 跟风：板块内后排涨停
  - 补涨：板块内首板
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import loguru

logger = loguru.logger


class StockPosition(Enum):
    SPACE_LEADER = "空间龙头"       # 最高连板
    STRENGTH_LEADER = "强度龙头"    # 最早封板/最强封单
    CORE_LEADER = "核心中军"        # 大市值涨停
    FOLLOWER = "跟风"              # 后排涨停
    SUPPLEMENT = "补涨"            # 板块内首板
    LONE_WOLF = "独狼"             # 无板块效应
    UNKNOWN = "未知"


@dataclass
class StockPositionResult:
    """个股板块地位分析结果"""
    stock_code: str
    stock_name: str
    sector_name: str
    position: StockPosition = StockPosition.UNKNOWN
    position_score: float = 0.0       # 地位评分 0-100

    # 量化指标
    board_height: int = 0             # 连板数
    limit_up_time: str = ""           # 封板时间
    seal_amount: float = 0.0          # 封单金额（亿）
    market_cap: float = 0.0           # 市值（亿）
    turnover_rate: float = 0.0        # 换手率

    # 板块内排名
    board_height_rank: int = 0        # 连板排名
    seal_time_rank: int = 0           # 封板时间排名
    market_cap_rank: int = 0          # 市值排名

    # 板块统计
    sector_total_stocks: int = 0      # 板块内涨停总数
    sector_max_board: int = 0         # 板块最高连板
    sector_avg_board: float = 0.0     # 板块平均连板


class SectorPositionAnalyzer:
    """
    个股板块地位分析器

    量化每只股票在其所属板块中的地位
    """

    def __init__(self):
        # 地位评分权重
        self.position_weights = {
            'board_height': 0.35,      # 连板高度权重
            'seal_time': 0.30,         # 封板时间权重
            'market_cap': 0.20,        # 市值权重
            'seal_amount': 0.15,       # 封单权重
        }

        # 封板时间评分映射（越早越好）
        self.seal_time_score_map = {
            'ultra_early': 100,   # 9:30前（一字板/秒板）
            'early': 80,          # 9:30-10:00
            'morning': 60,        # 10:00-11:30
            'afternoon': 40,      # 13:00-14:00
            'late': 20,           # 14:00-15:00
            'tail': 10,           # 尾盘板
        }

        logger.info("[SectorPositionAnalyzer] 初始化完成")

    def analyze(self, zt_pool: pd.DataFrame,
                hierarchy_df: pd.DataFrame = None) -> Dict[str, StockPositionResult]:
        """
        分析涨停池中每只股票在各自板块中的地位

        Args:
            zt_pool: 涨停池数据
            hierarchy_df: 行业层级数据（可选）

        Returns:
            {股票代码: StockPositionResult}
        """
        if zt_pool.empty:
            logger.warning("[SectorPosition] 涨停池为空")
            return {}

        logger.info(f"[SectorPosition] 开始分析 {len(zt_pool)} 只股票的板块地位...")

        # 确定板块列名
        sector_col = self._find_sector_column(zt_pool)

        if sector_col is None:
            logger.warning("[SectorPosition] 未找到板块列")
            return {}

        # 按板块分组
        sector_groups = self._group_by_sector(zt_pool, sector_col)

        results: Dict[str, StockPositionResult] = {}

        for sector_name, group_df in sector_groups.items():
            sector_results = self._analyze_sector_group(sector_name, group_df)
            results.update(sector_results)

        # 统计
        position_counts = {}
        for r in results.values():
            pos = r.position.value
            position_counts[pos] = position_counts.get(pos, 0) + 1

        logger.info(f"[SectorPosition] 分析完成: {position_counts}")

        return results

    def _find_sector_column(self, df: pd.DataFrame) -> Optional[str]:
        """查找板块列名"""
        candidates = ['L2_Industry', 'industry', 'sector', '板块', '行业', '概念']
        for col in candidates:
            if col in df.columns:
                return col
        return None

    def _group_by_sector(self, df: pd.DataFrame, sector_col: str) -> Dict[str, pd.DataFrame]:
        """按板块分组"""
        groups = {}
        for sector_name, group_df in df.groupby(sector_col):
            if sector_name and str(sector_name) != 'nan' and str(sector_name) != '其他':
                groups[str(sector_name)] = group_df
        return groups

    def _analyze_sector_group(self, sector_name: str,
                               group_df: pd.DataFrame) -> Dict[str, StockPositionResult]:
        """分析单个板块内的股票地位"""
        results = {}

        if len(group_df) == 0:
            return results

        # 提取关键指标
        board_heights = self._extract_board_heights(group_df)
        seal_times = self._extract_seal_times(group_df)
        market_caps = self._extract_market_caps(group_df)
        seal_amounts = self._extract_seal_amounts(group_df)

        # 板块统计
        sector_max_board = max(board_heights.values()) if board_heights else 0
        sector_avg_board = np.mean(list(board_heights.values())) if board_heights else 0
        sector_total = len(group_df)

        # 排名
        board_ranks = self._rank_dict(board_heights, reverse=True)
        seal_time_ranks = self._rank_dict(seal_times, reverse=False)
        market_cap_ranks = self._rank_dict(market_caps, reverse=True)

        # 逐只分析
        for _, row in group_df.iterrows():
            code = str(row.get('code', row.get('代码', ''))).zfill(6)
            name = str(row.get('name', row.get('名称', '')))

            if not code or code == '0' * 6:
                continue

            board_h = board_heights.get(code, 1)
            seal_t = seal_times.get(code, '')
            mcap = market_caps.get(code, 0)
            seal_amt = seal_amounts.get(code, 0)

            result = StockPositionResult(
                stock_code=code,
                stock_name=name,
                sector_name=sector_name,
                board_height=board_h,
                limit_up_time=seal_t,
                seal_amount=seal_amt,
                market_cap=mcap,
                board_height_rank=board_ranks.get(code, 0),
                seal_time_rank=seal_time_ranks.get(code, 0),
                market_cap_rank=market_cap_ranks.get(code, 0),
                sector_total_stocks=sector_total,
                sector_max_board=sector_max_board,
                sector_avg_board=sector_avg_board,
            )

            # 判断地位
            result.position = self._determine_position(result, sector_total)
            result.position_score = self._calculate_position_score(result, sector_total)

            results[code] = result

        return results

    def _extract_board_heights(self, df: pd.DataFrame) -> Dict[str, int]:
        """提取连板高度"""
        heights = {}
        board_cols = ['board_height', '连板数', 'limit_times', '连续涨停天数']

        for _, row in df.iterrows():
            code = str(row.get('code', row.get('代码', ''))).zfill(6)
            if not code or code == '0' * 6:
                continue

            height = 1
            for col in board_cols:
                if col in df.columns:
                    val = row.get(col, 1)
                    try:
                        height = int(val)
                        break
                    except (ValueError, TypeError):
                        pass
            heights[code] = max(1, height)

        return heights

    def _extract_seal_times(self, df: pd.DataFrame) -> Dict[str, str]:
        """提取封板时间"""
        times = {}
        time_cols = ['first_time', 'limit_up_time', '封板时间', 'LimitUpTime']

        for _, row in df.iterrows():
            code = str(row.get('code', row.get('代码', ''))).zfill(6)
            if not code or code == '0' * 6:
                continue

            seal_time = ''
            for col in time_cols:
                if col in df.columns:
                    val = row.get(col, '')
                    if val and str(val) != 'nan':
                        seal_time = self._normalize_time(str(val))
                        break
            times[code] = seal_time

        return times

    def _extract_market_caps(self, df: pd.DataFrame) -> Dict[str, float]:
        """提取市值"""
        caps = {}
        cap_cols = ['market_cap', 'total_mv', '总市值', '市值']

        for _, row in df.iterrows():
            code = str(row.get('code', row.get('代码', ''))).zfill(6)
            if not code or code == '0' * 6:
                continue

            mcap = 0.0
            for col in cap_cols:
                if col in df.columns:
                    val = row.get(col, 0)
                    try:
                        mcap = float(val) / 1e8  # 转换为亿
                        break
                    except (ValueError, TypeError):
                        pass
            caps[code] = mcap

        return caps

    def _extract_seal_amounts(self, df: pd.DataFrame) -> Dict[str, float]:
        """提取封单金额"""
        amounts = {}
        amt_cols = ['seal_amount', '封单额', 'limit_up_amount']

        for _, row in df.iterrows():
            code = str(row.get('code', row.get('代码', ''))).zfill(6)
            if not code or code == '0' * 6:
                continue

            amt = 0.0
            for col in amt_cols:
                if col in df.columns:
                    val = row.get(col, 0)
                    try:
                        amt = float(val) / 1e8  # 转换为亿
                        break
                    except (ValueError, TypeError):
                        pass
            amounts[code] = amt

        return amounts

    def _normalize_time(self, time_str: str) -> str:
        """标准化时间格式为 HH:MM:SS"""
        time_str = time_str.strip()
        if not time_str:
            return ''

        # 处理 HHMMSS 格式
        if time_str.isdigit():
            if len(time_str) == 6:
                return f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
            elif len(time_str) == 4:
                return f"{time_str[:2]}:{time_str[2:]}:00"

        # 处理 HH:MM:SS 格式
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 2:
                return f"{parts[0]}:{parts[1]}:00"
            return time_str

        return time_str

    def _rank_dict(self, data: Dict[str, float], reverse: bool = True) -> Dict[str, int]:
        """对字典值进行排名"""
        if not data:
            return {}

        sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=reverse)
        ranks = {}
        current_rank = 1
        prev_value = None

        for i, (code, value) in enumerate(sorted_items):
            if prev_value is not None and value != prev_value:
                current_rank = i + 1
            ranks[code] = current_rank
            prev_value = value

        return ranks

    def _determine_position(self, result: StockPositionResult,
                            sector_total: int) -> StockPosition:
        """判断个股在板块中的地位"""
        # 独狼：板块内只有1只涨停
        if sector_total <= 1:
            return StockPosition.LONE_WOLF

        # 空间龙头：连板最高
        if result.board_height >= result.sector_max_board and result.board_height >= 2:
            return StockPosition.SPACE_LEADER

        # 强度龙头：最早封板（排名第1）且连板>=2
        if result.seal_time_rank == 1 and result.board_height >= 2:
            return StockPosition.STRENGTH_LEADER

        # 核心中军：市值排名前2且市值>100亿
        if result.market_cap_rank <= 2 and result.market_cap > 100:
            return StockPosition.CORE_LEADER

        # 补涨：首板
        if result.board_height == 1:
            return StockPosition.SUPPLEMENT

        # 跟风：其余
        return StockPosition.FOLLOWER

    def _calculate_position_score(self, result: StockPositionResult,
                                   sector_total: int) -> float:
        """计算地位评分"""
        if sector_total <= 1:
            return 50.0

        scores = []

        # 连板高度评分
        if result.sector_max_board > 0:
            board_score = (result.board_height / result.sector_max_board) * 100
        else:
            board_score = 50
        scores.append(board_score * self.position_weights['board_height'])

        # 封板时间评分
        time_category = self._classify_seal_time(result.limit_up_time)
        time_score = self.seal_time_score_map.get(time_category, 50)
        scores.append(time_score * self.position_weights['seal_time'])

        # 市值评分（中军加分）
        if result.market_cap > 500:
            cap_score = 100
        elif result.market_cap > 200:
            cap_score = 80
        elif result.market_cap > 100:
            cap_score = 60
        elif result.market_cap > 50:
            cap_score = 40
        else:
            cap_score = 20
        scores.append(cap_score * self.position_weights['market_cap'])

        # 封单评分
        if result.seal_amount > 10:
            seal_score = 100
        elif result.seal_amount > 5:
            seal_score = 80
        elif result.seal_amount > 1:
            seal_score = 60
        elif result.seal_amount > 0:
            seal_score = 40
        else:
            seal_score = 20
        scores.append(seal_score * self.position_weights['seal_amount'])

        return sum(scores)

    def _classify_seal_time(self, time_str: str) -> str:
        """分类封板时间"""
        if not time_str:
            return 'morning'

        try:
            parts = time_str.split(':')
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0

            if hour < 9 or (hour == 9 and minute < 30):
                return 'ultra_early'
            elif hour == 9 and minute >= 30 or hour == 10 and minute == 0:
                return 'early'
            elif hour < 12:
                return 'morning'
            elif hour < 14:
                return 'afternoon'
            elif hour < 15:
                return 'late'
            else:
                return 'tail'
        except (ValueError, IndexError):
            return 'morning'

    def get_sector_leaders(self, results: Dict[str, StockPositionResult],
                           top_n: int = 3) -> Dict[str, List[StockPositionResult]]:
        """
        获取每个板块的龙头股列表

        Returns:
            {板块名称: [龙头股列表]}
        """
        sector_leaders: Dict[str, List[StockPositionResult]] = {}

        for code, result in results.items():
            sector = result.sector_name
            if sector not in sector_leaders:
                sector_leaders[sector] = []

            # 只收集龙头和中军
            if result.position in [StockPosition.SPACE_LEADER,
                                    StockPosition.STRENGTH_LEADER,
                                    StockPosition.CORE_LEADER]:
                sector_leaders[sector].append(result)

        # 按地位评分排序，取top_n
        for sector in sector_leaders:
            sector_leaders[sector] = sorted(
                sector_leaders[sector],
                key=lambda x: x.position_score,
                reverse=True
            )[:top_n]

        return sector_leaders

    def to_dataframe(self, results: Dict[str, StockPositionResult]) -> pd.DataFrame:
        """将分析结果转换为DataFrame"""
        rows = []
        for code, result in results.items():
            rows.append({
                '代码': result.stock_code,
                '名称': result.stock_name,
                '板块': result.sector_name,
                '地位': result.position.value,
                '地位评分': round(result.position_score, 1),
                '连板数': result.board_height,
                '封板时间': result.limit_up_time,
                '封单(亿)': round(result.seal_amount, 2),
                '市值(亿)': round(result.market_cap, 2),
                '连板排名': result.board_height_rank,
                '封板排名': result.seal_time_rank,
                '市值排名': result.market_cap_rank,
                '板块涨停数': result.sector_total_stocks,
                '板块最高连板': result.sector_max_board,
            })

        return pd.DataFrame(rows)
