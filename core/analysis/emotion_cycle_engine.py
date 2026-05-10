"""
情绪周期识别引擎 - 系统化判断市场所处的情绪阶段

核心决策公式: 赚钱效应 = 情绪周期 × 资金共识 × 筹码结构

思维模式:
  - 不预测，只跟随
  - 不抄底，只接力
  - 不幻想，只应对
  - 不做杂毛，只做核心

禁止行为:
  - 线性外推（"已经涨了这么多应该..."）
  - 价值投资思维（"基本面好可以拿..."）
  - 成本锚定（"等回本再卖..."）
  - 随意补仓（亏损加仓摊低成本）
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
import loguru

logger = loguru.logger

# 导入外置配置
from config.config_loader import EmotionCycleConfig


class EmotionCycle(Enum):
    """情绪周期阶段枚举"""
    BOOM = "高潮期"      # 高潮期: 涨停>100家，连板高度>7板，无核按钮
    RISE = "上升期"      # 上升期: 涨停50-100家，连板高度4-6板，有主线
    SHAKE = "震荡期"     # 震荡期: 涨停30-60家，连板高度3-5板，轮动快
    DECLINE = "退潮期"   # 退潮期: 涨停<30家，核按钮多，高标A杀
    FREEZE = "冰点期"    # 冰点期: 涨停<20家，连板高度<3板，地量


@dataclass
class CycleThresholds:
    """
    情绪周期阈值配置
    
    注意: 现在阈值从YAML配置文件加载，此类保留用于兼容性
    建议使用 EmotionCycleConfig() 获取最新配置
    """
    
    def __init__(self):
        # 从外置配置加载
        config = EmotionCycleConfig()
        
        # 涨停家数阈值
        self.limit_up_high = config.limit_up_high
        self.limit_up_mid_high = config.limit_up_mid_high
        self.limit_up_mid_low = config.limit_up_mid_low
        self.limit_up_low = config.limit_up_low
        self.limit_up_freeze = config.limit_up_freeze
        
        # 连板高度阈值
        self.board_height_boom = config.board_height_boom
        self.board_height_high = config.board_height_high
        self.board_height_mid = config.board_height_mid
        self.board_height_low = config.board_height_low
        
        # 炸板率阈值
        self.broken_rate_low = config.broken_rate_low
        self.broken_rate_mid = config.broken_rate_mid
        self.broken_rate_high = config.broken_rate_high
        
        # 核按钮阈值
        self.nuclear_button_low = config.nuclear_button_low
        self.nuclear_button_high = config.nuclear_button_high
        
        # 昨日涨停溢价阈值
        self.premium_high = config.premium_high
        self.premium_mid = config.premium_mid
        self.premium_low = config.premium_low
        
        # 连板率阈值
        self.continuous_rate_high = config.continuous_rate_high
        self.continuous_rate_mid = config.continuous_rate_mid
        self.continuous_rate_low = config.continuous_rate_low
        
        # 跌停惩罚阈值
        self.limit_down_ratio_low = config.limit_down_ratio_low
        self.limit_down_ratio_mid = config.limit_down_ratio_mid
        self.limit_down_ratio_high = config.limit_down_ratio_high


@dataclass
class CycleStrategy:
    """情绪周期对应策略"""
    cycle: EmotionCycle
    description: str
    strategy: str
    position: str
    risk_level: str
    allowed_actions: List[str]
    forbidden_actions: List[str]


# 情绪周期策略配置
CYCLE_STRATEGIES = {
    EmotionCycle.BOOM: CycleStrategy(
        cycle=EmotionCycle.BOOM,
        description="涨停>100家，连板高度>7板，无核按钮",
        strategy="只卖不买，减仓龙头，准备撤退",
        position="<30%",
        risk_level="极高",
        allowed_actions=["减仓", "止盈", "空仓观望"],
        forbidden_actions=["开新仓", "追高", "打板", "接力"]
    ),
    EmotionCycle.RISE: CycleStrategy(
        cycle=EmotionCycle.RISE,
        description="涨停50-100家，连板高度4-6板，有主线",
        strategy="重仓龙头，做最强，积极参与",
        position="50-80%",
        risk_level="中等",
        allowed_actions=["打板", "接力", "做龙头", "做主线"],
        forbidden_actions=["做杂毛", "做跟风", "随意止损"]
    ),
    EmotionCycle.SHAKE: CycleStrategy(
        cycle=EmotionCycle.SHAKE,
        description="涨停30-60家，连板高度3-5板，轮动快",
        strategy="快进快出，只做模式内，严格止损",
        position="30-50%",
        risk_level="中高",
        allowed_actions=["低吸", "首板", "严格止损"],
        forbidden_actions=["追高", "重仓", "格局", "补仓"]
    ),
    EmotionCycle.DECLINE: CycleStrategy(
        cycle=EmotionCycle.DECLINE,
        description="涨停<30家，核按钮多，高标A杀",
        strategy="空仓或1成试错，禁止接力",
        position="0-10%",
        risk_level="高",
        allowed_actions=["空仓", "极小仓位试错", "观察"],
        forbidden_actions=["接力", "打板", "重仓", "抄底"]
    ),
    EmotionCycle.FREEZE: CycleStrategy(
        cycle=EmotionCycle.FREEZE,
        description="涨停<20家，连板高度<3板，地量",
        strategy="准备新周期，试首板，等信号",
        position="10-20%",
        risk_level="中等",
        allowed_actions=["试首板", "小仓位试错", "观察信号"],
        forbidden_actions=["重仓", "接力高位", "盲目抄底"]
    )
}


class EmotionCycleEngine:
    """
    情绪周期识别引擎
    
    基于多维度指标判断市场所处的情绪阶段：
    1. 涨停家数 - 市场活跃度
    2. 连板高度 - 风险偏好
    3. 炸板率 - 市场分歧程度
    4. 核按钮数量 - 恐慌程度
    5. 昨日涨停溢价 - 赚钱效应（基于开盘价计算）
    6. 昨日涨停胜率 - 开盘卖出胜率
    7. 平均赢面 - 盈利股票的平均收益
    8. 连板梯队完整性 - 生态健康度
    """
    
    def __init__(self, thresholds: CycleThresholds = None, dm=None):
        self.thresholds = thresholds or CycleThresholds()
        self.history_cycles: List[Dict] = []  # 历史周期记录
        self.dm = dm  # DataManager实例，用于获取价格数据
        
    def detect_cycle(self,
                     limit_up_count: int,
                     max_board_height: int,
                     broken_rate: float,
                     nuclear_button_count: int = 0,
                     prev_limit_up_premium: float = None,
                     board_distribution: Dict[int, int] = None,
                     continuous_rate: float = None,
                     limit_down_count: int = 0) -> Tuple[EmotionCycle, Dict[str, float]]:
        """
        识别当前情绪周期

        Args:
            limit_up_count: 涨停家数
            max_board_height: 最高连板高度
            broken_rate: 炸板率 (%)
            nuclear_button_count: 核按钮数量（跌停家数）
            prev_limit_up_premium: 昨日涨停溢价 (%)
            board_distribution: 连板分布 {1: 20, 2: 10, 3: 5, ...}
            continuous_rate: 连板率 (连板股数/涨停股总数 %)
            limit_down_count: 跌停家数

        Returns:
            Tuple[EmotionCycle, Dict]: (情绪周期枚举, 各周期得分)
        """
        # 计算综合得分
        scores = self._calculate_cycle_scores(
            limit_up_count, max_board_height, broken_rate,
            nuclear_button_count, prev_limit_up_premium, board_distribution,
            continuous_rate, limit_down_count
        )

        # 根据得分判断周期
        cycle = self._determine_cycle(scores)

        # 记录历史
        self._record_cycle(cycle, scores)

        return cycle, scores
    
    def _calculate_cycle_scores(self,
                               limit_up_count: int,
                               max_board_height: int,
                               broken_rate: float,
                               nuclear_button_count: int,
                               prev_limit_up_premium: Optional[float],
                               board_distribution: Optional[Dict[int, int]],
                               continuous_rate: Optional[float] = None,
                               limit_down_count: int = 0) -> Dict[str, float]:
        """计算各周期维度的匹配得分"""
        scores = {
            'boom': 0,      # 高潮期得分
            'rise': 0,      # 上升期得分
            'shake': 0,     # 震荡期得分
            'decline': 0,   # 退潮期得分
            'freeze': 0     # 冰点期得分
        }

        th = self.thresholds

        # 1. 涨停家数评分
        if limit_up_count >= th.limit_up_high:
            scores['boom'] += 3
            scores['rise'] += 1
        elif limit_up_count >= th.limit_up_mid_high:
            scores['boom'] += 1
            scores['rise'] += 3
        elif limit_up_count >= th.limit_up_mid_low:
            scores['rise'] += 2
            scores['shake'] += 2
        elif limit_up_count >= th.limit_up_low:
            scores['shake'] += 2
            scores['decline'] += 1
        elif limit_up_count >= th.limit_up_freeze:
            scores['decline'] += 3
            scores['freeze'] += 1
        else:
            scores['freeze'] += 3
            scores['decline'] += 1

        # 2. 连板高度评分
        if max_board_height >= th.board_height_boom:
            scores['boom'] += 3
        elif max_board_height >= th.board_height_high:
            scores['boom'] += 1
            scores['rise'] += 2
        elif max_board_height >= th.board_height_mid:
            scores['rise'] += 2
            scores['shake'] += 1
        elif max_board_height >= th.board_height_low:
            scores['shake'] += 2
            scores['decline'] += 1
        else:
            scores['freeze'] += 2
            scores['decline'] += 1

        # 3. 炸板率评分
        if broken_rate <= th.broken_rate_low:
            scores['boom'] += 1
            scores['rise'] += 2
        elif broken_rate <= th.broken_rate_mid:
            scores['rise'] += 1
            scores['shake'] += 2
        elif broken_rate <= th.broken_rate_high:
            scores['shake'] += 1
            scores['decline'] += 2
        else:
            scores['decline'] += 3
            scores['freeze'] += 1

        # 4. 核按钮评分
        if nuclear_button_count <= th.nuclear_button_low:
            scores['boom'] += 1
            scores['rise'] += 1
        elif nuclear_button_count <= th.nuclear_button_high:
            scores['shake'] += 2
        else:
            scores['decline'] += 3
            scores['freeze'] += 1

        # 5. 昨日涨停溢价评分
        if prev_limit_up_premium is not None:
            if prev_limit_up_premium >= th.premium_high:
                scores['boom'] += 1
                scores['rise'] += 2
            elif prev_limit_up_premium >= th.premium_mid:
                scores['rise'] += 1
                scores['shake'] += 1
            elif prev_limit_up_premium >= th.premium_low:
                scores['shake'] += 1
                scores['decline'] += 1
            else:
                scores['decline'] += 2
                scores['freeze'] += 1

        # 6. 连板梯队完整性评分
        if board_distribution:
            echelon_score = self._calculate_echelon_score(board_distribution)
            if echelon_score >= 0.8:
                scores['boom'] += 1
                scores['rise'] += 2
            elif echelon_score >= 0.5:
                scores['rise'] += 1
                scores['shake'] += 1
            else:
                scores['decline'] += 1
                scores['freeze'] += 1

        # 7. 连板率奖赏因子 (连板股数/涨停股总数)
        if continuous_rate is not None and limit_up_count > 0:
            if continuous_rate >= th.continuous_rate_high:
                # 高连板率 - 情绪高涨，资金接力意愿强
                scores['boom'] += 2
                scores['rise'] += 2
            elif continuous_rate >= th.continuous_rate_mid:
                # 中等连板率 - 情绪健康
                scores['rise'] += 2
                scores['shake'] += 1
            elif continuous_rate >= th.continuous_rate_low:
                # 低连板率 - 情绪低迷，首板多连板少
                scores['shake'] += 1
                scores['decline'] += 2
            else:
                # 极低连板率 - 情绪冰点
                scores['decline'] += 1
                scores['freeze'] += 2

        # 8. 跌停惩罚因子 (跌停家数/涨停家数比例)
        if limit_up_count > 0:
            limit_down_ratio = limit_down_count / limit_up_count
            if limit_down_ratio <= th.limit_down_ratio_low:
                # 跌停很少 - 市场健康
                scores['boom'] += 1
                scores['rise'] += 1
            elif limit_down_ratio <= th.limit_down_ratio_mid:
                # 中等跌停 - 市场分歧
                scores['shake'] += 2
            elif limit_down_ratio <= th.limit_down_ratio_high:
                # 跌停较多 - 风险信号
                scores['decline'] += 3
                scores['shake'] += 1
            else:
                # 跌停极多 - 恐慌情绪
                scores['decline'] += 2
                scores['freeze'] += 3

        return scores
    
    def _calculate_echelon_score(self, board_distribution: Dict[int, int]) -> float:
        """
        计算连板梯队完整性得分
        
        使用公共组件库计算，保持向后兼容
        """
        from core.analysis.market_indicators import calculate_echelon_score
        from config.config_loader import get_emotion_cycle_config
        
        # 从配置获取理想比例和阈值
        config = get_emotion_cycle_config()
        echelon_config = config.get('echelon_scoring', {})
        ideal_ratios = echelon_config.get('ideal_ratios', [0.6, 0.4, 0.25, 0.15, 0.1])
        min_ratio_threshold = echelon_config.get('min_ratio_threshold', 0.5)
        
        return calculate_echelon_score(
            board_distribution,
            ideal_ratios=ideal_ratios,
            min_ratio_threshold=min_ratio_threshold
        )
    
    def _determine_cycle(self, scores: Dict[str, float]) -> EmotionCycle:
        """根据得分确定情绪周期"""
        max_score = max(scores.values())
        
        # 找到得分最高的周期
        if scores['boom'] == max_score and max_score >= 5:
            return EmotionCycle.BOOM
        elif scores['freeze'] == max_score and max_score >= 5:
            return EmotionCycle.FREEZE
        elif scores['decline'] == max_score and max_score >= 5:
            return EmotionCycle.DECLINE
        elif scores['rise'] == max_score:
            return EmotionCycle.RISE
        else:
            return EmotionCycle.SHAKE
    
    def _record_cycle(self, cycle: EmotionCycle, scores: Dict[str, float]):
        """记录周期判断历史"""
        self.history_cycles.append({
            'timestamp': datetime.now(),
            'cycle': cycle.value,
            'scores': scores
        })
        
        # 只保留最近30条记录
        if len(self.history_cycles) > 30:
            self.history_cycles = self.history_cycles[-30:]
    
    def get_strategy(self, cycle: EmotionCycle) -> CycleStrategy:
        """获取对应周期的策略建议"""
        return CYCLE_STRATEGIES.get(cycle, CYCLE_STRATEGIES[EmotionCycle.SHAKE])
    
    def analyze_market_data(self, 
                           limit_up_df: pd.DataFrame,
                           limit_down_df: pd.DataFrame = None,
                           prev_limit_up_df: pd.DataFrame = None) -> Dict:
        """
        分析市场数据，识别情绪周期
        
        Args:
            limit_up_df: 当日涨停数据
            limit_down_df: 当日跌停数据（可选）
            prev_limit_up_df: 昨日涨停数据（可选，用于计算溢价）
            
        Returns:
            包含情绪周期分析结果的字典
        """
        if limit_up_df.empty:
            return {
                'cycle': EmotionCycle.FREEZE,
                'strategy': self.get_strategy(EmotionCycle.FREEZE),
                'metrics': {},
                'warning': '无涨停数据，可能为冰点期或数据异常'
            }
        
        # 计算各项指标
        limit_up_count = len(limit_up_df)
        
        # 最高连板高度
        max_board_height = limit_up_df.get('limit_times', limit_up_df.get('连板数', 1)).max()
        if pd.isna(max_board_height):
            max_board_height = 1
        max_board_height = int(max_board_height)
        
        # 炸板率
        open_times_col = 'open_times' if 'open_times' in limit_up_df.columns else '炸板次数'
        if open_times_col in limit_up_df.columns:
            broken_count = len(limit_up_df[limit_up_df[open_times_col] > 0])
            broken_rate = (broken_count / limit_up_count) * 100 if limit_up_count > 0 else 0
        else:
            broken_rate = 0
        
        # 核按钮数量
        nuclear_button_count = len(limit_down_df) if limit_down_df is not None else 0
        
        # 昨日涨停溢价率和胜率计算
        prev_limit_up_premium = None
        win_rate = None  # 胜率
        avg_profit = None  # 平均赢面
        
        if prev_limit_up_df is not None and not prev_limit_up_df.empty:
            # 计算昨日涨停股票今日开盘的溢价情况
            premium_data = self._calculate_prev_limit_up_performance(prev_limit_up_df)
            prev_limit_up_premium = premium_data.get('avg_premium')
            win_rate = premium_data.get('win_rate')
            avg_profit = premium_data.get('avg_profit')
        
        # 连板分布
        board_distribution = {}
        limit_times_col = 'limit_times' if 'limit_times' in limit_up_df.columns else '连板数'
        if limit_times_col in limit_up_df.columns:
            board_distribution = limit_up_df[limit_times_col].value_counts().to_dict()

        # 计算连板率 (连板股数/涨停股总数)
        continuous_count = 0
        if limit_times_col in limit_up_df.columns:
            # 连板数>=2的认为是连板股
            continuous_count = len(limit_up_df[limit_up_df[limit_times_col] >= 2])
        continuous_rate = (continuous_count / limit_up_count * 100) if limit_up_count > 0 else 0

        # 跌停家数
        limit_down_count = len(limit_down_df) if limit_down_df is not None else 0

        # 识别情绪周期
        cycle, scores = self.detect_cycle(
            limit_up_count=limit_up_count,
            max_board_height=max_board_height,
            broken_rate=broken_rate,
            nuclear_button_count=nuclear_button_count,
            prev_limit_up_premium=prev_limit_up_premium,
            board_distribution=board_distribution,
            continuous_rate=continuous_rate,
            limit_down_count=limit_down_count
        )

        # 获取策略
        strategy = self.get_strategy(cycle)

        return {
            'cycle': cycle,
            'cycle_name': cycle.value,
            'strategy': strategy,
            'metrics': {
                'limit_up_count': limit_up_count,
                'max_board_height': max_board_height,
                'broken_rate': round(broken_rate, 2),
                'nuclear_button_count': nuclear_button_count,
                'prev_limit_up_premium': prev_limit_up_premium,
                'win_rate': win_rate,
                'avg_profit': avg_profit,
                'board_distribution': board_distribution,
                'continuous_rate': round(continuous_rate, 2),
                'limit_down_count': limit_down_count,
                'limit_down_ratio': round(limit_down_count / limit_up_count, 2) if limit_up_count > 0 else 0
            },
            'scores': scores
        }
    
    def _calculate_prev_limit_up_performance(self, prev_limit_up_df: pd.DataFrame) -> Dict:
        """
        计算昨日涨停股票今日开盘的表现
        
        计算指标：
        1. 平均溢价率：今日开盘价相对昨日涨停价的平均涨幅
        2. 胜率：开盘卖出的胜率（开盘价 > 昨日涨停价的比例）
        3. 平均赢面：盈利股票的平均收益率
        
        Args:
            prev_limit_up_df: 昨日涨停数据，需要包含股票代码
            
        Returns:
            {
                'avg_premium': 平均溢价率(%),
                'win_rate': 胜率(%),
                'avg_profit': 平均赢面(%)
            }
        """
        result = {
            'avg_premium': None,
            'win_rate': None,
            'avg_profit': None
        }
        
        if prev_limit_up_df.empty:
            return result
        
        # 需要DataManager来获取价格数据
        if not hasattr(self, 'dm') or self.dm is None:
            logger.warning("[_calculate_prev_limit_up_performance] 未提供DataManager，无法计算溢价率")
            return result
        
        try:
            premiums = []
            profits = []
            
            # 获取股票代码列
            code_col = None
            for col in ['ts_code', '代码', 'code', 'stock_code']:
                if col in prev_limit_up_df.columns:
                    code_col = col
                    break
            
            if code_col is None:
                logger.warning("[_calculate_prev_limit_up_performance] 未找到股票代码列")
                return result
            
            # 获取昨日和今日的日期
            # 从数据中获取日期，或使用当前日期
            trade_date = None
            for col in ['trade_date', '日期', 'date']:
                if col in prev_limit_up_df.columns:
                    trade_date = prev_limit_up_df[col].iloc[0]
                    break
            
            if trade_date is None:
                logger.warning("[_calculate_prev_limit_up_performance] 未找到交易日期")
                return result
            
            # 计算今日日期（下一个交易日）
            today = self.dm.date_utils.get_next_trade_date(str(trade_date))
            yesterday = str(trade_date)
            
            logger.info(f"[_calculate_prev_limit_up_performance] 计算 {yesterday} 涨停股票 {today} 开盘表现")
            
            for _, row in prev_limit_up_df.iterrows():
                try:
                    ts_code = row[code_col]
                    
                    # 优化：一次性获取两日的数据，而不是分别查询两次
                    df = self.dm.get_stock_daily(ts_code, yesterday, today)
                    if df.empty or len(df) < 2:
                        continue
                    
                    # 按日期排序，确保顺序正确
                    df = df.sort_values('trade_date')
                    
                    # 获取昨日收盘价（第一行）和今日开盘价（第二行）
                    prev_close = float(df.iloc[0].get('close', 0))
                    today_open = float(df.iloc[1].get('open', 0))
                    
                    if prev_close <= 0 or today_open <= 0:
                        continue
                    
                    # 计算溢价率
                    premium = (today_open - prev_close) / prev_close * 100
                    premiums.append(premium)
                    
                    # 如果开盘价高于昨日涨停价，计入盈利
                    if today_open > prev_close:
                        profits.append(premium)
                    
                except Exception as e:
                    logger.debug(f"计算单只股票溢价失败: {e}")
                    continue
            
            if premiums:
                result['avg_premium'] = round(sum(premiums) / len(premiums), 2)
                result['win_rate'] = round(len(profits) / len(premiums) * 100, 2)
                
                if profits:
                    result['avg_profit'] = round(sum(profits) / len(profits), 2)
                else:
                    result['avg_profit'] = 0
                
                logger.info(f"[_calculate_prev_limit_up_performance] 统计完成: "
                           f"样本数={len(premiums)}, 平均溢价={result['avg_premium']}%, "
                           f"胜率={result['win_rate']}%, 平均赢面={result['avg_profit']}%")
            else:
                logger.warning("[_calculate_prev_limit_up_performance] 无有效溢价数据")
                
        except Exception as e:
            logger.error(f"[_calculate_prev_limit_up_performance] 计算失败: {e}")

        return result


if __name__ == "__main__":
    # 测试
    engine = EmotionCycleEngine()
    
    # 模拟上升期数据
    cycle, scores = engine.detect_cycle(
        limit_up_count=65,
        max_board_height=5,
        broken_rate=18,
        nuclear_button_count=2,
        prev_limit_up_premium=2.5,
        board_distribution={1: 35, 2: 18, 3: 8, 4: 3, 5: 1}
    )

    print(f"识别到的情绪周期: {cycle.value}")
    print(f"各周期得分: {scores}")

    strategy = engine.get_strategy(cycle)
    print(f"\n策略建议:")
    print(f"  描述: {strategy.description}")
    print(f"  策略: {strategy.strategy}")
    print(f"  仓位: {strategy.position}")
    print(f"  允许操作: {', '.join(strategy.allowed_actions)}")
    print(f"  禁止操作: {', '.join(strategy.forbidden_actions)}")
