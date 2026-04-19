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


class EmotionCycle(Enum):
    """情绪周期阶段枚举"""
    BOOM = "高潮期"      # 高潮期: 涨停>100家，连板高度>7板，无核按钮
    RISE = "上升期"      # 上升期: 涨停50-100家，连板高度4-6板，有主线
    SHAKE = "震荡期"     # 震荡期: 涨停30-60家，连板高度3-5板，轮动快
    DECLINE = "退潮期"   # 退潮期: 涨停<30家，核按钮多，高标A杀
    FREEZE = "冰点期"    # 冰点期: 涨停<20家，连板高度<3板，地量


@dataclass
class CycleThresholds:
    """情绪周期阈值配置"""
    # 涨停家数阈值
    limit_up_high: int = 100      # 高潮期门槛
    limit_up_mid_high: int = 80   # 上升期上限
    limit_up_mid_low: int = 50    # 上升期下限/震荡期上限
    limit_up_low: int = 30        # 震荡期下限/退潮期上限
    limit_up_freeze: int = 20     # 冰点期上限
    
    # 连板高度阈值
    board_height_boom: int = 7    # 高潮期最低高度
    board_height_high: int = 6    # 上升期上限
    board_height_mid: int = 4     # 上升期下限/震荡期上限
    board_height_low: int = 3     # 震荡期下限/冰点期上限
    
    # 炸板率阈值 (%)
    broken_rate_low: float = 15.0     # 低炸板率（健康）
    broken_rate_mid: float = 25.0     # 中等炸板率
    broken_rate_high: float = 40.0    # 高炸板率（危险）
    
    # 核按钮阈值 (跌停家数)
    nuclear_button_low: int = 3       # 少量核按钮
    nuclear_button_high: int = 10     # 大量核按钮
    
    # 昨日涨停溢价阈值 (%)
    premium_high: float = 3.0         # 高溢价
    premium_mid: float = 1.0          # 中等溢价
    premium_low: float = -1.0         # 负溢价


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
                     board_distribution: Dict[int, int] = None) -> EmotionCycle:
        """
        识别当前情绪周期
        
        Args:
            limit_up_count: 涨停家数
            max_board_height: 最高连板高度
            broken_rate: 炸板率 (%)
            nuclear_button_count: 核按钮数量（跌停家数）
            prev_limit_up_premium: 昨日涨停溢价 (%)
            board_distribution: 连板分布 {1: 20, 2: 10, 3: 5, ...}
            
        Returns:
            EmotionCycle: 情绪周期枚举
        """
        # 计算综合得分
        scores = self._calculate_cycle_scores(
            limit_up_count, max_board_height, broken_rate,
            nuclear_button_count, prev_limit_up_premium, board_distribution
        )
        
        # 根据得分判断周期
        cycle = self._determine_cycle(scores)
        
        # 记录历史
        self._record_cycle(cycle, scores)
        
        return cycle
    
    def _calculate_cycle_scores(self,
                               limit_up_count: int,
                               max_board_height: int,
                               broken_rate: float,
                               nuclear_button_count: int,
                               prev_limit_up_premium: Optional[float],
                               board_distribution: Optional[Dict[int, int]]) -> Dict[str, float]:
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
        
        return scores
    
    def _calculate_echelon_score(self, board_distribution: Dict[int, int]) -> float:
        """
        计算连板梯队完整性得分
        
        理想梯队: 1B > 2B > 3B > 4B > 5B (金字塔结构)
        得分越高表示梯队越完整
        """
        if not board_distribution or 1 not in board_distribution:
            return 0.0
        
        count_1b = board_distribution.get(1, 0)
        if count_1b == 0:
            return 0.0
        
        # 计算各层占比
        ratios = []
        for height in range(2, 7):  # 2B到6B
            count = board_distribution.get(height, 0)
            ratio = count / count_1b if count_1b > 0 else 0
            ratios.append(ratio)
        
        # 理想比例递减: 2B应该占1B的50-70%, 3B占2B的50-70%, etc.
        ideal_ratios = [0.6, 0.4, 0.25, 0.15, 0.1]
        
        # 计算与理想比例的匹配度
        score = 0
        for actual, ideal in zip(ratios, ideal_ratios):
            if actual >= ideal * 0.5:  # 至少达到理想值的50%
                score += 1
        
        return score / len(ideal_ratios)
    
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
        
        # 识别情绪周期
        cycle = self.detect_cycle(
            limit_up_count=limit_up_count,
            max_board_height=max_board_height,
            broken_rate=broken_rate,
            nuclear_button_count=nuclear_button_count,
            prev_limit_up_premium=prev_limit_up_premium,
            board_distribution=board_distribution
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
                'board_distribution': board_distribution
            },
            'scores': self.history_cycles[-1]['scores'] if self.history_cycles else {}
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
                    
                    # 获取昨日涨停价（close）和今日开盘价
                    yesterday_price = self.dm.get_stock_daily_price(ts_code, yesterday)
                    today_price = self.dm.get_stock_daily_price(ts_code, today)
                    
                    if not yesterday_price or not today_price:
                        continue
                    
                    prev_close = yesterday_price.get('close', 0)
                    today_open = today_price.get('open', 0)
                    
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
    
    def get_cycle_transition_probability(self) -> Dict[str, float]:
        """
        基于历史数据计算周期转换概率
        
        Returns:
            当前周期转移到其他周期的概率
        """
        if len(self.history_cycles) < 2:
            return {}
        
        current_cycle = self.history_cycles[-1]['cycle']
        
        # 统计历史转换情况
        transitions = {}
        for i in range(1, len(self.history_cycles)):
            prev = self.history_cycles[i-1]['cycle']
            curr = self.history_cycles[i]['cycle']
            if prev not in transitions:
                transitions[prev] = {}
            if curr not in transitions[prev]:
                transitions[prev][curr] = 0
            transitions[prev][curr] += 1
        
        # 计算当前周期的转移概率
        if current_cycle in transitions:
            total = sum(transitions[current_cycle].values())
            return {k: v/total for k, v in transitions[current_cycle].items()}
        
        return {}


if __name__ == "__main__":
    # 测试
    engine = EmotionCycleEngine()
    
    # 模拟上升期数据
    result = engine.detect_cycle(
        limit_up_count=65,
        max_board_height=5,
        broken_rate=18,
        nuclear_button_count=2,
        prev_limit_up_premium=2.5,
        board_distribution={1: 35, 2: 18, 3: 8, 4: 3, 5: 1}
    )
    
    print(f"识别到的情绪周期: {result.value}")
    
    strategy = engine.get_strategy(result)
    print(f"\n策略建议:")
    print(f"  描述: {strategy.description}")
    print(f"  策略: {strategy.strategy}")
    print(f"  仓位: {strategy.position}")
    print(f"  允许操作: {', '.join(strategy.allowed_actions)}")
    print(f"  禁止操作: {', '.join(strategy.forbidden_actions)}")
