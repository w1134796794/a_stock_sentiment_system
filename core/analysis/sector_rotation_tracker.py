"""
板块轮动追踪器 V2 - 多因子动态权重模型

核心优化：
1. 统一主线计算逻辑（融合limit_cpt_list + hierarchy_df）
2. 增加资金确认维度（成交额变化率参与评分）
3. 引入市场周期检测（自动调整权重）
4. 优化趋势计算（加权移动平均+动态阈值）
5. 增加板块内部分化度指标
6. 构建轮动图谱（资金迁移路径）

删除项：
- avg_rank_5d 独立字段 → 改为动量指标
- SectorStage.DORMANT → 未上榜板块无需追踪
- 固定操作建议文本 → 改为基于评分的动态策略
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger


class SectorStage(Enum):
    """板块生命周期阶段（精简版）"""
    EMERGING = "萌芽期"      # 首次进入榜单
    ACCELERATING = "加速期"  # 排名快速上升
    PEAK = "高潮期"          # 排名靠前且稳定
    DECLINING = "衰退期"     # 排名下降


class MarketCycle(Enum):
    """市场周期阶段"""
    FREEZING = "冰点期"      # 情绪冰点，高度谨慎
    WARMING = "回暖期"       # 情绪修复，试探性参与
    RISING = "上升期"        # 情绪上升，积极做多
    BOOMING = "高潮期"       # 情绪高潮，注意风险
    DECLINING = "退潮期"     # 情绪退潮，收缩战线


@dataclass
class SectorStrength:
    """板块强度数据"""
    concept_name: str          # 概念板块名称
    rank: int                  # 当日排名
    up_nums: int               # 涨停家数
    cons_nums: int             # 连续涨停数
    turnover_rate: float       # 板块换手率
    amount: float              # 成交金额（万元）
    trade_date: str            # 交易日期
    amount_change: float = 0.0 # 成交额变化率


@dataclass
class SectorMetrics:
    """板块多因子指标"""
    # 强度因子
    up_nums: int = 0                    # 涨停家数
    cons_nums: int = 0                  # 连续涨停数
    max_board_height: int = 1           # 最高连板高度
    
    # 资金因子
    amount: float = 0.0                 # 成交金额
    amount_change: float = 0.0          # 成交额变化率
    turnover_rate: float = 0.0          # 换手率
    
    # 趋势因子
    rank_momentum: float = 0.0          # 排名动量（加权移动平均变化）
    up_nums_trend: float = 0.0          # 涨停家数趋势
    persistence_score: float = 0.0      # 持续性评分
    
    # 分化因子
    divergence_index: float = 0.0       # 板块内部分化度（0-1，越小越一致）
    leader_strength: float = 0.0        # 龙头强度
    
    # 综合评分
    composite_score: float = 0.0        # 综合评分
    stage: SectorStage = SectorStage.EMERGING  # 所处阶段


@dataclass
class DynamicWeights:
    """动态权重配置"""
    strength_weight: float = 0.30       # 强度因子权重
    capital_weight: float = 0.25        # 资金因子权重
    trend_weight: float = 0.25          # 趋势因子权重
    divergence_weight: float = 0.20     # 分化因子权重
    
    def adjust_by_market_cycle(self, cycle: MarketCycle):
        """根据市场周期调整权重"""
        if cycle == MarketCycle.FREEZING:
            # 冰点期：更看重资金确认和趋势
            self.strength_weight = 0.20
            self.capital_weight = 0.35
            self.trend_weight = 0.30
            self.divergence_weight = 0.15
        elif cycle == MarketCycle.WARMING:
            # 回暖期：平衡配置
            self.strength_weight = 0.30
            self.capital_weight = 0.25
            self.trend_weight = 0.25
            self.divergence_weight = 0.20
        elif cycle == MarketCycle.RISING:
            # 上升期：更看重强度和趋势
            self.strength_weight = 0.35
            self.capital_weight = 0.20
            self.trend_weight = 0.30
            self.divergence_weight = 0.15
        elif cycle == MarketCycle.BOOMING:
            # 高潮期：更看重分化和资金
            self.strength_weight = 0.25
            self.capital_weight = 0.30
            self.trend_weight = 0.20
            self.divergence_weight = 0.25
        elif cycle == MarketCycle.DECLINING:
            # 退潮期：更看重趋势和分化
            self.strength_weight = 0.20
            self.capital_weight = 0.20
            self.trend_weight = 0.35
            self.divergence_weight = 0.25


class SectorRotationTracker:
    """
    板块轮动追踪器 V2
    
    基于多因子动态权重模型，实现：
    1. 统一主线计算（融合limit_cpt_list + 涨停池层级数据）
    2. 资金确认维度（成交额变化率）
    3. 市场周期适配（自动调整权重）
    4. 动态趋势计算（加权移动平均+自适应阈值）
    5. 板块分化度指标
    6. 轮动图谱构建
    """
    
    def __init__(self, data_manager=None, emotion_engine=None):
        self.dm = data_manager
        self.emotion_engine = emotion_engine
        self.sector_history: Dict[str, List[SectorStrength]] = {}
        self.max_history_days = 20
        self.weights = DynamicWeights()
        self.current_market_cycle: MarketCycle = MarketCycle.WARMING
        
        # 初始化概念-行业交叉验证器
        self._init_validator()
    
    def _init_validator(self):
        """初始化交叉验证器"""
        try:
            from core.analysis.concept_industry_validator import ConceptIndustryValidator
            self.validator = ConceptIndustryValidator(self.dm)
            logger.info("[SectorRotationTracker] 交叉验证器初始化成功")
        except Exception as e:
            logger.warning(f"[SectorRotationTracker] 交叉验证器初始化失败: {e}")
            self.validator = None
        
    def detect_market_cycle(self, trade_date: str = None) -> MarketCycle:
        """
        检测当前市场周期
        
        基于情绪周期引擎或涨停数据自动判断
        """
        if self.emotion_engine:
            # 使用情绪周期引擎
            try:
                # 获取必要数据
                limit_up_df = self.dm.get_limit_up_pool(trade_date) if self.dm else pd.DataFrame()
                limit_down_df = self.dm.get_limit_down_pool(trade_date) if self.dm else pd.DataFrame()
                
                # 获取昨日涨停数据用于计算溢价
                if self.dm:
                    prev_date = self.dm.date_utils.get_prev_trade_date(trade_date)
                    prev_limit_up_df = self.dm.get_limit_up_pool(prev_date)
                else:
                    prev_limit_up_df = pd.DataFrame()
                
                emotion_result = self.emotion_engine.analyze_market_data(
                    limit_up_df=limit_up_df,
                    limit_down_df=limit_down_df,
                    prev_limit_up_df=prev_limit_up_df
                )
                
                cycle_name = emotion_result.get('cycle_name', '')
                cycle_map = {
                    '冰点期': MarketCycle.FREEZING,
                    '回暖期': MarketCycle.WARMING,
                    '上升期': MarketCycle.RISING,
                    '高潮期': MarketCycle.BOOMING,
                    '退潮期': MarketCycle.DECLINING,
                }
                self.current_market_cycle = cycle_map.get(cycle_name, MarketCycle.WARMING)
                
            except Exception as e:
                logger.warning(f"[detect_market_cycle] 情绪周期检测失败: {e}")
                self._detect_cycle_by_data(trade_date)
        else:
            self._detect_cycle_by_data(trade_date)
        
        # 根据市场周期调整权重
        self.weights.adjust_by_market_cycle(self.current_market_cycle)
        logger.info(f"[detect_market_cycle] 当前市场周期: {self.current_market_cycle.value}")
        
        return self.current_market_cycle
    
    def _detect_cycle_by_data(self, trade_date: str = None):
        """基于涨停数据检测市场周期"""
        if not self.dm:
            return
        
        try:
            limit_up_df = self.dm.get_limit_up_pool(trade_date)
            limit_down_df = self.dm.get_limit_down_pool(trade_date)
            
            if limit_up_df.empty:
                return
            
            # 计算关键指标
            zt_count = len(limit_up_df)
            dt_count = len(limit_down_df) if not limit_down_df.empty else 0
            
            # 计算连板高度分布
            if '连板数' in limit_up_df.columns:
                board_heights = limit_up_df['连板数'].fillna(1).astype(int)
                high_board_count = len(board_heights[board_heights >= 5])
                mid_board_count = len(board_heights[(board_heights >= 3) & (board_heights < 5)])
            else:
                high_board_count = 0
                mid_board_count = 0
            
            # 判断市场周期
            if zt_count < 30 or dt_count > zt_count * 0.3:
                self.current_market_cycle = MarketCycle.FREEZING
            elif zt_count < 50 and high_board_count == 0:
                self.current_market_cycle = MarketCycle.WARMING
            elif zt_count >= 50 and high_board_count >= 3:
                self.current_market_cycle = MarketCycle.BOOMING
            elif zt_count < 40 or (dt_count > 10 and high_board_count < 2):
                self.current_market_cycle = MarketCycle.DECLINING
            else:
                self.current_market_cycle = MarketCycle.RISING
                
        except Exception as e:
            logger.warning(f"[_detect_cycle_by_data] 数据检测失败: {e}")
    
    def fetch_daily_sectors(self, trade_date: str) -> pd.DataFrame:
        """获取某日最强板块数据"""
        if not self.dm:
            logger.warning("[SectorRotationTracker] 未提供DataManager")
            return pd.DataFrame()
        
        return self.dm.get_limit_cpt_list(trade_date)
    
    def fetch_sector_history(self, concept_name: str, end_date: str, days: int = 10) -> List[SectorStrength]:
        """获取某板块最近N天的历史数据"""
        if not self.dm:
            return []
        
        trade_dates = self.dm.date_utils.get_last_n_trade_dates(n=days, end_date=end_date)
        
        history = []
        prev_amount = None
        
        for date in trade_dates:
            df = self.fetch_daily_sectors(date)
            if df.empty:
                continue
            
            # 查找该板块数据
            sector_col = 'name' if 'name' in df.columns else 'concept'
            sector_row = df[df[sector_col] == concept_name]
            
            if not sector_row.empty:
                row = sector_row.iloc[0]
                amount = float(row.get('amount', 0))
                
                # 计算成交额变化率
                amount_change = 0.0
                if prev_amount and prev_amount > 0:
                    amount_change = (amount - prev_amount) / prev_amount
                
                history.append(SectorStrength(
                    concept_name=concept_name,
                    rank=sector_row.index[0] + 1,
                    up_nums=int(row.get('up_nums', 0)),
                    cons_nums=int(row.get('cons_nums', 0)),
                    turnover_rate=float(row.get('turnover_rate', 0)),
                    amount=amount,
                    trade_date=date,
                    amount_change=amount_change
                ))
                
                prev_amount = amount
        
        return history
    
    def _calculate_mainline_metrics(self, history: List[SectorStrength], current_rank: int) -> Dict:
        """
        计算概念主线特征指标
        
        用于判断概念是否具有主线特征（持续性强）还是一日游（突发性）
        
        Args:
            history: 历史数据列表（最近10天）
            current_rank: 当前排名
            
        Returns:
            Dict: 主线特征指标
                - top10_count_10d: 10日内进入前10的次数
                - top5_count_10d: 10日内进入前5的次数
                - top3_count_10d: 10日内进入前3的次数
                - avg_rank_10d: 10日平均排名
                - best_rank_10d: 10日内最好排名
                - consecutive_top10: 连续进入前10的天数
                - is_mainline: 是否具备主线特征（综合判断）
        """
        if not history:
            # 没有历史数据，只有今天
            return {
                'top10_count_10d': 1 if current_rank <= 10 else 0,
                'top5_count_10d': 1 if current_rank <= 5 else 0,
                'top3_count_10d': 1 if current_rank <= 3 else 0,
                'avg_rank_10d': current_rank,
                'best_rank_10d': current_rank,
                'consecutive_top10': 1 if current_rank <= 10 else 0,
                'is_mainline': False  # 单日数据无法判断主线特征
            }
        
        # 提取历史排名（包括今天）
        historical_ranks = [h.rank for h in history]
        historical_ranks.append(current_rank)  # 加入今天
        
        # 只取最近10天
        historical_ranks = historical_ranks[-10:]
        
        # 计算各项指标
        top10_count = sum(1 for r in historical_ranks if r <= 10)
        top5_count = sum(1 for r in historical_ranks if r <= 5)
        top3_count = sum(1 for r in historical_ranks if r <= 3)
        avg_rank = sum(historical_ranks) / len(historical_ranks)
        best_rank = min(historical_ranks)
        
        # 计算连续进入前10的天数（从最近一天往前数）
        consecutive_top10 = 0
        for r in reversed(historical_ranks):
            if r <= 10:
                consecutive_top10 += 1
            else:
                break
        
        # 判断是否具有主线特征
        # 标准：10日内至少5次进入前10，且最近连续3天在前10
        is_mainline = (top10_count >= 5 and consecutive_top10 >= 3)
        
        return {
            'top10_count_10d': top10_count,
            'top5_count_10d': top5_count,
            'top3_count_10d': top3_count,
            'avg_rank_10d': round(avg_rank, 1),
            'best_rank_10d': best_rank,
            'consecutive_top10': consecutive_top10,
            'is_mainline': is_mainline
        }
    
    def calculate_weighted_momentum(self, values: List[float], weights: List[float] = None) -> float:
        """
        计算加权动量（加权移动平均变化率）
        
        Args:
            values: 数值序列（如排名序列）
            weights: 权重序列，默认线性递减权重
            
        Returns:
            动量值（正值表示改善，负值表示恶化）
        """
        if len(values) < 2:
            return 0.0
        
        n = len(values)
        if weights is None:
            # 线性递减权重：最近的数据权重更高
            weights = [i / sum(range(1, n + 1)) for i in range(1, n + 1)]
        
        # 计算加权平均值
        weighted_avg = np.average(values, weights=weights)
        
        # 计算动量（与最新值的对比）
        momentum = (values[-1] - weighted_avg) / max(weighted_avg, 1)
        
        return momentum
    
    def calculate_divergence_index(self, concept_name: str, hierarchy_df: pd.DataFrame) -> Tuple[float, float]:
        """
        计算板块内部分化度指标
        
        Args:
            concept_name: 板块名称
            hierarchy_df: 涨停池层级数据
            
        Returns:
            (divergence_index, leader_strength)
            - divergence_index: 分化度（0-1，越小越一致）
            - leader_strength: 龙头强度（最高连板/平均连板）
        """
        if hierarchy_df.empty or 'L2_Industry' not in hierarchy_df.columns:
            return 0.5, 1.0
        
        # 筛选该板块的股票
        sector_stocks = hierarchy_df[hierarchy_df['L2_Industry'] == concept_name]
        
        if len(sector_stocks) < 2:
            return 0.0, 1.0
        
        # 获取连板高度分布
        board_heights = sector_stocks['BoardHeight'].fillna(1).astype(int)
        
        if len(board_heights) < 2:
            return 0.0, 1.0
        
        # 计算分化度（变异系数）
        mean_height = board_heights.mean()
        std_height = board_heights.std()
        
        if mean_height > 0:
            divergence_index = min(std_height / mean_height, 1.0)
        else:
            divergence_index = 0.0
        
        # 计算龙头强度
        max_height = board_heights.max()
        leader_strength = max_height / max(mean_height, 1.0)
        
        return divergence_index, leader_strength
    
    def calculate_sector_metrics(self, 
                                  concept_name: str,
                                  current_data: SectorStrength,
                                  history: List[SectorStrength],
                                  hierarchy_df: pd.DataFrame = None) -> SectorMetrics:
        """
        计算板块多因子指标
        
        融合limit_cpt_list数据和hierarchy_df数据
        """
        metrics = SectorMetrics()
        
        # 1. 强度因子（来自limit_cpt_list）
        metrics.up_nums = current_data.up_nums
        metrics.cons_nums = current_data.cons_nums
        
        # 2. 资金因子
        metrics.amount = current_data.amount
        metrics.amount_change = current_data.amount_change
        metrics.turnover_rate = current_data.turnover_rate
        
        # 3. 趋势因子（加权移动平均）
        if history:
            # 排名动量（排名数字越小越好，所以取负值）
            ranks = [h.rank for h in history]
            rank_momentum = -self.calculate_weighted_momentum(ranks)
            metrics.rank_momentum = rank_momentum
            
            # 涨停家数趋势
            up_nums_history = [h.up_nums for h in history]
            metrics.up_nums_trend = self.calculate_weighted_momentum(up_nums_history)
            
            # 持续性评分（基于上榜频率和稳定性）
            frequency_score = min(len(history) / 10 * 40, 40)
            if len(ranks) >= 2:
                stability_score = max(30 - np.std(ranks) * 2, 0)
            else:
                stability_score = 15
            momentum_score = max(30 + rank_momentum * 20, 0)
            metrics.persistence_score = frequency_score + stability_score + momentum_score
        else:
            metrics.rank_momentum = 0.0
            metrics.up_nums_trend = 0.0
            metrics.persistence_score = 20.0  # 新上榜基础分
        
        # 4. 分化因子（需要hierarchy_df）
        if hierarchy_df is not None and not hierarchy_df.empty:
            # 从hierarchy_df获取该板块的最高连板
            sector_stocks = hierarchy_df[hierarchy_df['L2_Industry'] == concept_name]
            if not sector_stocks.empty and 'BoardHeight' in sector_stocks.columns:
                board_heights = sector_stocks['BoardHeight'].fillna(1).astype(int)
                metrics.max_board_height = board_heights.max()
            
            # 计算分化度
            metrics.divergence_index, metrics.leader_strength = self.calculate_divergence_index(
                concept_name, hierarchy_df
            )
        
        # 5. 计算综合评分（多因子加权）
        metrics.composite_score = self._calculate_composite_score(metrics)
        
        # 6. 判断阶段
        metrics.stage = self._determine_stage(metrics, current_data.rank)
        
        return metrics
    
    def _calculate_composite_score(self, metrics: SectorMetrics) -> float:
        """计算综合评分（多因子动态加权）"""
        # 强度因子得分
        strength_score = (
            min(metrics.up_nums / 10, 1.0) * 0.5 +
            min(metrics.cons_nums / 5, 1.0) * 0.3 +
            min(metrics.max_board_height / 5, 1.0) * 0.2
        ) * 100
        
        # 资金因子得分
        capital_score = (
            min(max(metrics.amount_change, -0.5) / 0.5 + 1, 1.0) * 0.6 +  # 成交额变化
            min(metrics.turnover_rate / 5, 1.0) * 0.4  # 换手率
        ) * 100
        
        # 趋势因子得分
        trend_score = (
            min(max(metrics.rank_momentum + 1, 0) / 2, 1.0) * 0.5 +  # 排名动量
            min(max(metrics.up_nums_trend + 0.5, 0) / 1.5, 1.0) * 0.3 +  # 涨停趋势
            min(metrics.persistence_score / 80, 1.0) * 0.2  # 持续性
        ) * 100
        
        # 分化因子得分（分化度越小越好，龙头强度越大越好）
        divergence_score = (
            (1 - metrics.divergence_index) * 0.6 +  # 一致性
            min(metrics.leader_strength / 3, 1.0) * 0.4  # 龙头强度
        ) * 100
        
        # 动态加权
        composite = (
            strength_score * self.weights.strength_weight +
            capital_score * self.weights.capital_weight +
            trend_score * self.weights.trend_weight +
            divergence_score * self.weights.divergence_weight
        )
        
        return round(composite, 2)
    
    def _determine_stage(self, metrics: SectorMetrics, current_rank: int) -> SectorStage:
        """判断板块所处阶段（动态阈值）"""
        # 根据市场周期调整阈值
        if self.current_market_cycle == MarketCycle.BOOMING:
            peak_rank_threshold = 5
            accelerating_rank_threshold = 15
        elif self.current_market_cycle == MarketCycle.RISING:
            peak_rank_threshold = 3
            accelerating_rank_threshold = 10
        else:
            peak_rank_threshold = 3
            accelerating_rank_threshold = 8
        
        # 判断阶段
        if current_rank <= peak_rank_threshold and metrics.persistence_score >= 60:
            if metrics.rank_momentum < -0.2:  # 排名动量恶化
                return SectorStage.DECLINING
            return SectorStage.PEAK
        
        if metrics.rank_momentum > 0.3 and current_rank <= accelerating_rank_threshold:
            return SectorStage.ACCELERATING
        
        if current_rank <= 10 and metrics.persistence_score < 50:
            return SectorStage.EMERGING
        
        if metrics.rank_momentum < -0.3 or current_rank > 15:
            return SectorStage.DECLINING
        
        return SectorStage.EMERGING
    
    def generate_dynamic_strategy(self, metrics: SectorMetrics, concept_name: str) -> Dict:
        """
        生成动态策略（基于评分和市场周期）
        
        替代固定的操作建议文本
        """
        score = metrics.composite_score
        stage = metrics.stage
        cycle = self.current_market_cycle
        
        strategy = {
            'action': '观望',
            'position': 0.0,
            'urgency': '低',
            'reason': ''
        }
        
        # 基于评分的仓位建议
        if score >= 80:
            base_position = 1.0
        elif score >= 65:
            base_position = 0.7
        elif score >= 50:
            base_position = 0.4
        elif score >= 35:
            base_position = 0.2
        else:
            base_position = 0.0
        
        # 根据阶段和市场周期调整
        if stage == SectorStage.PEAK:
            if cycle in [MarketCycle.BOOMING, MarketCycle.DECLINING]:
                strategy['action'] = '减仓/兑现'
                strategy['position'] = base_position * 0.5
                strategy['urgency'] = '高'
                strategy['reason'] = f'高潮期+{cycle.value}，注意风险'
            else:
                strategy['action'] = '持有/谨慎加仓'
                strategy['position'] = base_position * 0.8
                strategy['urgency'] = '中'
                strategy['reason'] = '高潮期但情绪尚好，持有观察'
                
        elif stage == SectorStage.ACCELERATING:
            if cycle in [MarketCycle.RISING, MarketCycle.BOOMING]:
                strategy['action'] = '积极做多'
                strategy['position'] = base_position
                strategy['urgency'] = '高'
                strategy['reason'] = f'加速期+{cycle.value}，主升浪机会'
            else:
                strategy['action'] = '小仓位试探'
                strategy['position'] = base_position * 0.5
                strategy['urgency'] = '中'
                strategy['reason'] = f'加速期但{cycle.value}，控制仓位'
                
        elif stage == SectorStage.EMERGING:
            strategy['action'] = '观察/准备'
            strategy['position'] = base_position * 0.3
            strategy['urgency'] = '低'
            strategy['reason'] = '萌芽期，等待确认信号'
            
        elif stage == SectorStage.DECLINING:
            strategy['action'] = '回避'
            strategy['position'] = 0.0
            strategy['urgency'] = '高'
            strategy['reason'] = '衰退期，坚决回避'
        
        return strategy
    
    def analyze_sectors_persistence(self, 
                                   trade_date: str = None,
                                   hierarchy_df: pd.DataFrame = None,
                                   top_n: int = 10) -> pd.DataFrame:
        """
        分析板块持续性（V2版本）
        
        Args:
            trade_date: 交易日期
            hierarchy_df: 涨停池层级数据（用于计算分化度）
            top_n: 分析前N个板块
            
        Returns:
            持续性分析结果DataFrame
        """
        # 检测市场周期并调整权重
        self.detect_market_cycle(trade_date)
        
        # 获取当日数据
        df = self.fetch_daily_sectors(trade_date)
        if df.empty:
            logger.warning(f"[analyze_sectors_persistence] 未获取到 {trade_date} 数据")
            return pd.DataFrame()
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y%m%d")
        
        results = []
        
        for idx, row in df.head(top_n).iterrows():
            sector_col = 'name' if 'name' in df.columns else 'concept'
            concept_name = row.get(sector_col, '')
            if not concept_name:
                continue
            
            rank = int(row.get('rank', idx + 1))
            
            current_data = SectorStrength(
                concept_name=concept_name,
                rank=rank,
                up_nums=int(row.get('up_nums', 0)),
                cons_nums=int(row.get('cons_nums', 0)),
                turnover_rate=float(row.get('turnover_rate', 0)),
                amount=float(row.get('amount', 0)),
                trade_date=trade_date
            )
            
            # 获取历史数据
            history = self.fetch_sector_history(concept_name, trade_date, days=10)
            
            # 计算主线特征指标
            mainline_metrics = self._calculate_mainline_metrics(history, rank)
            
            # 计算多因子指标
            metrics = self.calculate_sector_metrics(
                concept_name, current_data, history, hierarchy_df
            )
            
            # 生成动态策略
            strategy = self.generate_dynamic_strategy(metrics, concept_name)
            
            results.append({
                '板块名称': concept_name,
                '当前排名': rank,
                '10日进前10次数': mainline_metrics['top10_count_10d'],
                '10日进前5次数': mainline_metrics['top5_count_10d'],
                '10日进前3次数': mainline_metrics['top3_count_10d'],
                '10日平均排名': mainline_metrics['avg_rank_10d'],
                '10日最佳排名': mainline_metrics['best_rank_10d'],
                '连续进前10天数': mainline_metrics['consecutive_top10'],
                '是否主线': '是' if mainline_metrics['is_mainline'] else '否',
                '涨停家数': metrics.up_nums,
                '综合评分': metrics.composite_score,
                '所处阶段': metrics.stage.value,
                '市场周期': self.current_market_cycle.value,
                # 资金因子
                '成交额变化': f"{metrics.amount_change:+.1%}",
                '换手率': f"{metrics.turnover_rate:.1f}%",
                # 趋势因子
                '排名动量': f"{metrics.rank_momentum:+.2f}",
                '涨停趋势': f"{metrics.up_nums_trend:+.2f}",
                '持续性评分': round(metrics.persistence_score, 1),
                # 分化因子
                '分化度': f"{metrics.divergence_index:.2f}",
                '龙头强度': f"{metrics.leader_strength:.1f}x",
                '最高连板': metrics.max_board_height,
                # 策略建议
                '操作建议': strategy['action'],
                '建议仓位': f"{strategy['position']*100:.0f}%",
                '紧急度': strategy['urgency'],
                '策略理由': strategy['reason']
            })
        
        result_df = pd.DataFrame(results)
        
        if not result_df.empty:
            result_df = result_df.sort_values('综合评分', ascending=False)
        
        return result_df
    
    def calculate_unified_mainline(self, 
                                    limit_cpt_df: pd.DataFrame,
                                    hierarchy_df: pd.DataFrame,
                                    top_n: int = 5) -> pd.DataFrame:
        """
        统一主线计算（融合limit_cpt_list + hierarchy_df）
        
        消除两个数据源的信号冲突，生成一致的主线排名
        """
        if limit_cpt_df.empty and hierarchy_df.empty:
            return pd.DataFrame()
        
        # 从hierarchy_df计算各板块强度
        hierarchy_scores = {}
        if not hierarchy_df.empty and 'L2_Industry' in hierarchy_df.columns:
            for l2_name, group in hierarchy_df.groupby('L2_Industry'):
                if l2_name == '其他':
                    continue
                
                limit_up_count = len(group)
                board_heights = group['BoardHeight'].fillna(1).astype(int)
                max_height = board_heights.max()
                avg_height = board_heights.mean()
                
                # hierarchy强度分
                hierarchy_scores[l2_name] = {
                    '涨停家数': limit_up_count,
                    '最高连板': max_height,
                    '平均连板': avg_height,
                    'hierarchy_score': limit_up_count * 1.0 + max_height * 2.0
                }
        
        # 从limit_cpt_list获取数据
        unified_results = []
        sector_col = 'name' if 'name' in limit_cpt_df.columns else 'concept'
        
        for idx, row in limit_cpt_df.head(top_n * 2).iterrows():
            concept_name = row.get(sector_col, '')
            if not concept_name:
                continue
            
            # limit_cpt_list数据
            cpt_up_nums = int(row.get('up_nums', 0))
            cpt_cons_nums = int(row.get('cons_nums', 0))
            cpt_amount = float(row.get('amount', 0))
            
            # hierarchy数据（如果有）
            h_data = hierarchy_scores.get(concept_name, {})
            
            # 融合计算
            unified_score = (
                cpt_up_nums * 1.5 +  # limit_cpt_list涨停家数权重更高
                cpt_cons_nums * 2.0 +
                h_data.get('hierarchy_score', 0) * 0.8  # hierarchy数据作为验证
            )
            
            unified_results.append({
                '板块名称': concept_name,
                '综合强度分': round(unified_score, 2),
                'cpt涨停家数': cpt_up_nums,
                'cpt连板数': cpt_cons_nums,
                'hierarchy涨停家数': h_data.get('涨停家数', 0),
                'hierarchy最高连板': h_data.get('最高连板', 0),
                '成交额': cpt_amount,
                '数据来源': '融合' if concept_name in hierarchy_scores else 'cpt_only'
            })
        
        # 补充hierarchy中有但limit_cpt_list中没有的板块
        for l2_name, h_data in hierarchy_scores.items():
            if not any(r['板块名称'] == l2_name for r in unified_results):
                unified_results.append({
                    '板块名称': l2_name,
                    '综合强度分': round(h_data['hierarchy_score'] * 0.7, 2),  # 降权处理
                    'cpt涨停家数': 0,
                    'cpt连板数': 0,
                    'hierarchy涨停家数': h_data['涨停家数'],
                    'hierarchy最高连板': h_data['最高连板'],
                    '成交额': 0,
                    '数据来源': 'hierarchy_only'
                })
        
        result_df = pd.DataFrame(unified_results)
        if not result_df.empty:
            result_df = result_df.sort_values('综合强度分', ascending=False).head(top_n)
        
        return result_df
    
    def analyze_with_validation(self,
                                 trade_date: str = None,
                                 top_n: int = 10,
                                 hot_industries: List[str] = None) -> pd.DataFrame:
        """
        带交叉验证的板块分析
        
        结合概念热点和行业支撑度，识别真正的产业趋势
        
        Args:
            trade_date: 交易日期
            top_n: 分析前N个板块
            hot_industries: 热门行业列表（可选）
            
        Returns:
            带验证信号的DataFrame
        """
        # 1. 基础概念分析
        concept_df = self.analyze_sectors_persistence(trade_date, top_n=top_n)
        
        if concept_df.empty or not self.validator:
            logger.warning("[analyze_with_validation] 基础分析为空或验证器未初始化")
            return concept_df
        
        # 2. 准备验证数据
        concept_list = []
        for _, row in concept_df.iterrows():
            concept_list.append({
                'name': row['板块名称'],
                'rank': row['当前排名'],
                'score': row['综合评分']
            })
        
        # 3. 批量交叉验证
        validation_df = self.validator.batch_validate_concepts(
            concept_list=concept_list,
            hot_industries=hot_industries
        )
        
        # 4. 合并结果
        if validation_df.empty:
            return concept_df
        
        # 合并两个DataFrame
        merged_df = pd.merge(
            concept_df,
            validation_df[['概念名称', '主要行业', '行业集中度', '信号类型', 
                          '信号强度', '共振得分', '验证理由']],
            left_on='板块名称',
            right_on='概念名称',
            how='left'
        )
        
        # 删除重复的列
        if '概念名称' in merged_df.columns:
            merged_df = merged_df.drop(columns=['概念名称'])
        
        # 5. 调整建议仓位（结合共振得分）
        def adjust_position(row):
            base_position = float(row['建议仓位'].rstrip('%')) / 100
            resonance = row.get('共振得分', 50)
            
            # 根据共振得分调整
            if resonance >= 80:
                multiplier = 1.0
            elif resonance >= 60:
                multiplier = 0.8
            elif resonance >= 40:
                multiplier = 0.5
            else:
                multiplier = 0.2
            
            adjusted = base_position * multiplier
            return f"{adjusted*100:.0f}%"
        
        merged_df['调整后仓位'] = merged_df.apply(adjust_position, axis=1)
        
        # 6. 按共振得分重新排序
        merged_df = merged_df.sort_values('共振得分', ascending=False)
        
        return merged_df
    
    def get_hot_industries_from_sectors(self, top_n: int = 5) -> List[str]:
        """
        从当前热点板块反推热门行业
        
        Args:
            top_n: 取前N个板块分析
            
        Returns:
            热门行业列表
        """
        if not self.validator:
            return []
        
        # 获取当前热点概念
        concept_df = self.analyze_sectors_persistence(top_n=top_n)
        if concept_df.empty:
            return []
        
        top_concepts = concept_df['板块名称'].head(top_n).tolist()
        
        # 使用验证器反推行业
        return self.validator.get_hot_industries_from_concepts(top_concepts)
    
    def build_rotation_graph(self, start_date: str, end_date: str) -> Dict:
        """
        构建板块轮动图谱（资金迁移路径）
        
        用于预判接力板块
        """
        if not self.dm:
            return {}
        
        trade_dates = self.dm.date_utils.get_trade_dates_between(start_date, end_date)
        
        # 收集每日Top3板块
        daily_top3 = {}
        for date in trade_dates:
            df = self.fetch_daily_sectors(date)
            if not df.empty:
                sector_col = 'name' if 'name' in df.columns else 'concept'
                top3 = df.head(3)[sector_col].tolist()
                daily_top3[date] = top3
        
        # 构建迁移矩阵
        sectors = set()
        for top3 in daily_top3.values():
            sectors.update(top3)
        sectors = sorted(list(sectors))
        
        migration_matrix = pd.DataFrame(0, index=sectors, columns=sectors)
        
        dates = sorted(daily_top3.keys())
        for i in range(1, len(dates)):
            prev_sectors = daily_top3[dates[i-1]]
            curr_sectors = daily_top3[dates[i]]
            
            for prev in prev_sectors:
                for curr in curr_sectors:
                    migration_matrix.loc[prev, curr] += 1
        
        # 识别资金迁移路径
        migration_paths = []
        for i, row in migration_matrix.iterrows():
            for j, count in row.items():
                if count > 0 and i != j:
                    migration_paths.append({
                        'from': i,
                        'to': j,
                        'count': int(count),
                        'probability': count / len(dates)
                    })
        
        migration_paths.sort(key=lambda x: x['count'], reverse=True)
        
        return {
            'date_range': f"{start_date} - {end_date}",
            'total_days': len(dates),
            'daily_top3': daily_top3,
            'migration_matrix': migration_matrix.to_dict(),
            'top_migration_paths': migration_paths[:10]
        }


if __name__ == "__main__":
    # 测试
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    
    from core.data.data_manager import DataManager
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    tracker = SectorRotationTracker(dm)
    
    # 测试板块持续性分析
    print("="*80)
    print("板块持续性分析测试 V2")
    print("="*80)
    
    result_df = tracker.analyze_sectors_persistence(top_n=10)
    if not result_df.empty:
        print(result_df.to_string(index=False))
    else:
        print("未获取到数据")
    
    print("\n" + "="*80)
    print("统一主线计算测试")
    print("="*80)
    
    # 获取测试数据
    limit_cpt_df = dm.get_limit_cpt_list()
    if not limit_cpt_df.empty:
        unified_df = tracker.calculate_unified_mainline(limit_cpt_df, pd.DataFrame(), top_n=5)
        if not unified_df.empty:
            print(unified_df.to_string(index=False))
    
    print("\n" + "="*80)
