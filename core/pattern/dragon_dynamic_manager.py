"""
龙头股动态管理机制 - 技术标准与实现

核心规则：
1. 龙头池包含趋势龙和连板龙两种类型
2. 出现走弱信号时移至走弱池
3. 走弱池观察周期最长10个交易日
4. 出现走强信号判定为弱转强，提示买入

所有判断标准基于客观市场数据，可回测验证
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum
import pandas as pd
import numpy as np
import loguru

logger = loguru.logger


class DragonType(Enum):
    """龙头类型"""
    CONTINUOUS = "连板龙"      # 连续涨停
    TREND = "趋势龙"          # 趋势上涨


class DragonStatus(Enum):
    """龙头状态"""
    ACTIVE = "活跃中"         # 在龙头池中
    WEAKENING = "已走弱"      # 在走弱池中观察
    RECOVERED = "已转强"      # 弱转强成功
    EXPIRED = "已过期"        # 超过观察期移除


@dataclass
class DragonStock:
    """龙头股票信息"""
    stock_code: str
    stock_name: str
    dragon_type: DragonType
    
    # 入池信息
    entry_date: str
    entry_price: float
    
    # 龙头特征数据
    peak_board_height: int = 0          # 最高连板数（连板龙）
    total_rise_10d: float = 0.0         # 10日累计涨幅（趋势龙）
    limit_up_count: int = 0             # 期间涨停次数
    
    # 当前状态
    status: DragonStatus = DragonStatus.ACTIVE
    status_change_date: str = ""
    
    # 走弱信息（移入走弱池时填写）
    weakening_date: str = ""
    weakening_price: float = 0.0
    weakening_type: str = ""            # 走弱类型
    max_drawdown: float = 0.0           # 最大回调幅度
    
    # 观察期限
    weakening_entry_date: str = ""      # 进入走弱池日期
    max_observation_days: int = 10      # 最长观察10天
    
    def get_observation_days(self, current_date: str) -> int:
        """计算已在走弱池观察的天数"""
        if not self.weakening_entry_date:
            return 0
        try:
            entry = datetime.strptime(self.weakening_entry_date, "%Y%m%d")
            current = datetime.strptime(current_date, "%Y%m%d")
            return (current - entry).days
        except:
            return 0
    
    def is_observation_expired(self, current_date: str) -> bool:
        """检查观察期是否过期"""
        return self.get_observation_days(current_date) >= self.max_observation_days


class DragonDynamicManager:
    """
    龙头股动态管理器
    
    技术标准文档：
    ====================
    
    一、趋势龙头走弱判断标准（满足任一即判定走弱）
    ====================
    
    1. 【断板信号】- 最强烈的走弱信号
       条件：当日未涨停（涨幅<9.5%）且收盘价 < 昨日涨停价
       阈值：无（一旦发生即确认）
       权重：★★★★★
    
    2. 【烂板信号】- 资金分歧加大
       条件：
       - 当日涨停但开板次数 >= 3次
       - 或封单金额 < 1000万（小盘股<500万）
       - 且收盘封单强度 < 1%
       阈值：开板>=3次 或 封单<1000万
       权重：★★★★☆
    
    3. 【尾盘板信号】- 弱势涨停
       条件：
       - 当日涨停但首次封板时间 > 14:30
       - 且封单强度 < 2%
       阈值：封板时间>14:30
       权重：★★★☆☆
    
    4. 【放量滞涨信号】- 上涨动能衰竭
       条件：
       - 当日涨幅 < 5%（未涨停）
       - 成交量 > 前5日均量 × 2.5倍
       - 上影线长度 > 实体长度的1.5倍
       阈值：量比>2.5 且 上影线/实体>1.5
       权重：★★★★☆
    
    5. 【趋势回调信号】- 跌破关键均线
       条件：
       - 连续2日收盘价 < 5日均线
       - 且5日均线拐头向下（当日5日线 < 昨日5日线）
       - 且累计回调幅度 > 8%
       阈值：跌破5日线2天 且 回调>8%
       权重：★★★☆☆
    
    6. 【A杀预警信号】- 极端走弱
       条件：
       - 3日内从最高点回调 > 15%
       - 或出现跌停（跌幅<-9.5%）
       阈值：3日回调>15% 或 跌停
       权重：★★★★★
       说明：直接移入走弱池，观察期缩短至5天
    
    ====================
    
    二、趋势龙头走强判断标准（弱转强，需同时满足）
    ====================
    
    【核心逻辑】：走弱后的再次主动攻击，资金重新介入
    
    1. 【竞价转强】- 开盘即显强势
       必要条件：
       - 高开幅度 >= 3%（表明资金抢筹）
       - 竞价成交量 >= 昨日总成交量 × 10%（资金认可度高）
       - 竞价金额 >= 1000万（小盘股>=500万）
       阈值：高开>=3%，竞价量比>=10%，金额>=1000万
       
    2. 【开盘确认】- 开盘后不补跌
       必要条件：
       - 开盘后5分钟内最低价 >= 开盘价 × 0.98（不大幅回踩）
       - 开盘后15分钟内出现向上攻击（涨幅扩大或持平）
       阈值：回踩<2%，15分钟内维持强势
       
    3. 【盘中确认】- 主动攻击涨停（可选，提高置信度）
       加分条件：
       - 盘中触及涨停（即使未封住）
       - 或收盘涨幅 >= 7%
       阈值：触及涨停 或 收盘>=7%
       
    【买入信号判定】：
    - 基础信号：满足竞价转强 + 开盘确认 → 可轻仓试错
    - 确认信号：满足竞价转强 + 开盘确认 + 盘中触及涨停 → 可积极介入
    
    ====================
    
    三、连板龙走弱判断标准（满足任一即判定走弱）
    ====================
    
    【核心逻辑】：连板龙的走弱信号更敏感，一旦断板即确认
    
    1. 【断板信号】- 连板中断
       条件：
       - 当日未涨停（涨幅<9.5%）
       - 或虽然涨停但尾盘炸板未回封
       阈值：未涨停 或 炸板未回封
       权重：★★★★★
       说明：连板龙的核心就是"连"，一旦中断即走弱
    
    2. 【放量烂板信号】- 分歧巨大
       条件：
       - 当日涨停但开板次数 >= 2次
       - 且成交量 > 前3日平均成交量 × 3倍
       - 且收盘封单金额 < 昨日封单金额的30%
       阈值：开板>=2次 且 量比>3 且 封单<30%
       权重：★★★★☆
    
    3. 【尾盘偷袭信号】- 弱势封板
       条件：
       - 当日涨停但首次封板时间 > 14:45
       - 且封单强度 < 1.5%
       - 且封单金额 < 2000万
       阈值：封板时间>14:45
       权重：★★★★☆
       说明：尾盘偷袭通常是主力实力不足的表现
    
    4. 【竞价走弱信号】- 开盘即弱势
       条件：
       - 竞价低开（开盘价 < 昨日收盘价）
       - 且竞价成交量 > 昨日总成交量 × 15%（恐慌盘涌出）
       阈值：低开 且 竞价量比>15%
       权重：★★★☆☆
    
    5. 【A杀信号】- 极端走弱
       条件：
       - 2日内从最高点回调 > 12%
       - 或当日跌停（跌幅<-9.5%）
       - 或出现天地板（从涨停到跌停）
       阈值：2日回调>12% 或 跌停 或 天地板
       权重：★★★★★
       说明：直接移入走弱池，观察期缩短至3天
    
    ====================
    
    四、连板龙走强判断标准（弱转强，需同时满足）
    ====================
    
    【核心逻辑】：断板后的超预期修复，资金重新聚集
    
    1. 【竞价超预期】- 断板次日高开
       必要条件：
       - 昨日断板（未涨停）
       - 今日高开幅度 >= 5%（超预期强势）
       - 竞价成交量 >= 昨日总成交量 × 12%
       - 竞价金额 >= 1500万
       阈值：高开>=5%，竞价量比>=12%，金额>=1500万
       说明：断板后次日高开是最强的弱转强信号
       
    2. 【开盘承接】- 抛压被承接
       必要条件：
       - 开盘后3分钟内不跌破开盘价
       - 且3分钟内出现向上脉冲（涨幅扩大）
       阈值：3分钟不破开盘价
       
    3. 【盘中确认】- 再次攻击（可选）
       加分条件：
       - 盘中再次触及涨停
       - 或收盘涨幅 >= 8%
       阈值：触及涨停 或 收盘>=8%
       
    【买入信号判定】：
    - 基础信号：满足竞价超预期 + 开盘承接 → 可轻仓试错
    - 确认信号：满足竞价超预期 + 开盘承接 + 盘中触及涨停 → 可积极介入
    
    ====================
    
    五、观察期管理规则
    ====================
    
    1. 【标准观察期】
       - 趋势龙：10个交易日
       - 连板龙：7个交易日（连板龙节奏更快）
       
    2. 【缩短观察期】（A杀情况）
       - 趋势龙3日回调>15%：缩短至5天
       - 连板龙2日回调>12%或天地板：缩短至3天
       
    3. 【提前移除条件】
       - 观察期内再次出现走弱信号
       - 累计回调 > 20%（防止深套）
       - 成交量连续3日萎缩（资金彻底离场）
    """
    
    def __init__(self, data_manager):
        self.dm = data_manager
        
        # 池子管理
        self.dragon_pool: Dict[str, DragonStock] = {}      # 活跃龙头池
        self.weakening_pool: Dict[str, DragonStock] = {}   # 走弱观察池
        self.recovered_pool: Dict[str, DragonStock] = {}   # 已转强池（历史记录）
        
        # 参数配置
        self.params = self._init_params()
        
    def _init_params(self) -> Dict:
        """初始化判断参数"""
        return {
            # ========== 趋势龙走弱参数 ==========
            "trend_weakening": {
                # 断板信号
                "broken_limit_up": {
                    "enabled": True,
                    "priority": 5,
                },
                # 烂板信号
                "bad_limit_up": {
                    "enabled": True,
                    "min_break_count": 3,
                    "min_seal_amount": 10_000_000,  # 1000万
                    "min_seal_amount_small": 5_000_000,  # 小盘股500万
                    "max_seal_ratio": 0.01,
                    "priority": 4,
                },
                # 尾盘板信号
                "late_limit_up": {
                    "enabled": True,
                    "max_limit_up_time": "14:30",
                    "max_seal_ratio": 0.02,
                    "priority": 3,
                },
                # 放量滞涨信号
                "volume_stagnation": {
                    "enabled": True,
                    "max_rise_pct": 5.0,
                    "min_volume_ratio": 2.5,
                    "min_upper_shadow_ratio": 1.5,
                    "priority": 4,
                },
                # 趋势回调信号
                "trend_callback": {
                    "enabled": True,
                    "max_days_below_ma5": 2,
                    "max_drawdown_pct": 8.0,
                    "priority": 3,
                },
                # A杀预警信号
                "a_shock_warning": {
                    "enabled": True,
                    "max_drawdown_3d": 15.0,
                    "limit_down_threshold": -9.5,
                    "shorten_observation_to": 5,
                    "priority": 5,
                },
            },
            
            # ========== 趋势龙走强参数 ==========
            "trend_strengthening": {
                # 竞价转强
                "auction_strong": {
                    "min_gap_pct": 3.0,
                    "min_auction_vol_ratio": 0.10,
                    "min_auction_amount": 10_000_000,
                    "min_auction_amount_small": 5_000_000,
                },
                # 开盘确认
                "open_confirm": {
                    "max_pullback_pct": 2.0,
                    "check_duration_minutes": 15,
                },
                # 盘中确认（加分项）
                "intraday_confirm": {
                    "touch_limit_up": True,
                    "min_close_rise_pct": 7.0,
                },
            },
            
            # ========== 连板龙走弱参数 ==========
            "continuous_weakening": {
                # 断板信号
                "broken_board": {
                    "enabled": True,
                    "limit_up_threshold": 9.5,
                    "priority": 5,
                },
                # 放量烂板信号
                "volume_bad_board": {
                    "enabled": True,
                    "min_break_count": 2,
                    "min_volume_ratio": 3.0,
                    "max_seal_amount_ratio": 0.30,
                    "priority": 4,
                },
                # 尾盘偷袭信号
                "late_sneak": {
                    "enabled": True,
                    "max_limit_up_time": "14:45",
                    "max_seal_ratio": 0.015,
                    "max_seal_amount": 20_000_000,
                    "priority": 4,
                },
                # 竞价走弱信号
                "auction_weak": {
                    "enabled": True,
                    "max_auction_vol_ratio": 0.15,
                    "priority": 3,
                },
                # A杀信号
                "a_shock": {
                    "enabled": True,
                    "max_drawdown_2d": 12.0,
                    "limit_down_threshold": -9.5,
                    "shorten_observation_to": 3,
                    "priority": 5,
                },
            },
            
            # ========== 连板龙走强参数 ==========
            "continuous_strengthening": {
                # 竞价超预期
                "auction_surprise": {
                    "min_gap_pct": 5.0,
                    "min_auction_vol_ratio": 0.12,
                    "min_auction_amount": 15_000_000,
                },
                # 开盘承接
                "open_support": {
                    "check_duration_minutes": 3,
                },
                # 盘中确认（加分项）
                "intraday_confirm": {
                    "touch_limit_up": True,
                    "min_close_rise_pct": 8.0,
                },
            },
            
            # ========== 观察期参数 ==========
            "observation": {
                "trend_dragon_days": 10,
                "continuous_dragon_days": 7,
                "max_total_drawdown": 20.0,  # 累计回调超过20%提前移除
                "min_volume_days": 3,  # 成交量萎缩天数
            },
        }
    
    # ==================== 趋势龙走弱判断 ====================
    
    def check_trend_dragon_weakening(self, dragon: DragonStock, 
                                     daily_data: pd.DataFrame,
                                     today_data: Dict) -> Tuple[bool, str, Dict]:
        """
        判断趋势龙头是否走弱
        
        Returns:
            (是否走弱, 走弱类型, 详细数据)
        """
        params = self.params["trend_weakening"]
        
        # 1. 检查断板信号
        if params["broken_limit_up"]["enabled"]:
            is_broken, detail = self._check_broken_limit_up(today_data)
            if is_broken:
                return True, "断板", detail
        
        # 2. 检查烂板信号
        if params["bad_limit_up"]["enabled"]:
            is_bad, detail = self._check_bad_limit_up(today_data, params["bad_limit_up"])
            if is_bad:
                return True, "烂板", detail
        
        # 3. 检查尾盘板信号
        if params["late_limit_up"]["enabled"]:
            is_late, detail = self._check_late_limit_up(today_data, params["late_limit_up"])
            if is_late:
                return True, "尾盘板", detail
        
        # 4. 检查放量滞涨信号
        if params["volume_stagnation"]["enabled"] and not daily_data.empty:
            is_stagnation, detail = self._check_volume_stagnation(
                today_data, daily_data, params["volume_stagnation"]
            )
            if is_stagnation:
                return True, "放量滞涨", detail
        
        # 5. 检查趋势回调信号
        if params["trend_callback"]["enabled"] and not daily_data.empty:
            is_callback, detail = self._check_trend_callback(
                dragon, daily_data, params["trend_callback"]
            )
            if is_callback:
                return True, "趋势回调", detail
        
        # 6. 检查A杀预警信号
        if params["a_shock_warning"]["enabled"] and not daily_data.empty:
            is_a_shock, detail = self._check_a_shock_warning(
                dragon, daily_data, params["a_shock_warning"]
            )
            if is_a_shock:
                # A杀情况缩短观察期
                dragon.max_observation_days = params["a_shock_warning"]["shorten_observation_to"]
                return True, "A杀预警", detail
        
        return False, "", {}
    
    def _check_broken_limit_up(self, today_data: Dict) -> Tuple[bool, Dict]:
        """检查断板信号"""
        change_pct = today_data.get("涨跌幅", 0)
        close_price = today_data.get("收盘价", 0)
        yesterday_limit_price = today_data.get("昨涨停价", 0)
        
        # 未涨停且收盘价低于昨日涨停价
        if change_pct < 9.5 and close_price < yesterday_limit_price:
            return True, {
                "change_pct": change_pct,
                "close_price": close_price,
                "yesterday_limit_price": yesterday_limit_price,
            }
        return False, {}
    
    def _check_bad_limit_up(self, today_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查烂板信号"""
        change_pct = today_data.get("涨跌幅", 0)
        break_count = today_data.get("开板次数", 0)
        seal_amount = today_data.get("封单额", 0)
        seal_ratio = today_data.get("封单资金占比", 0)
        market_cap = today_data.get("流通市值", 0)
        
        # 当日涨停
        if change_pct < 9.5:
            return False, {}
        
        # 判断小盘股（流通市值<50亿）
        is_small_cap = market_cap < 5_000_000_000 if market_cap else False
        min_seal = params["min_seal_amount_small"] if is_small_cap else params["min_seal_amount"]
        
        # 开板次数过多 或 封单不足
        if (break_count >= params["min_break_count"] or 
            (seal_amount < min_seal and seal_ratio < params["max_seal_ratio"])):
            return True, {
                "break_count": break_count,
                "seal_amount": seal_amount,
                "seal_ratio": seal_ratio,
                "is_small_cap": is_small_cap,
            }
        return False, {}
    
    def _check_late_limit_up(self, today_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查尾盘板信号"""
        change_pct = today_data.get("涨跌幅", 0)
        first_limit_time = today_data.get("首次封板时间", "")
        seal_ratio = today_data.get("封单资金占比", 0)
        
        # 当日涨停
        if change_pct < 9.5:
            return False, {}
        
        # 解析封板时间
        try:
            hour, minute = map(int, first_limit_time.split(":"))
            limit_time = hour * 60 + minute
            max_time = 14 * 60 + 30  # 14:30
            
            if limit_time > max_time and seal_ratio < params["max_seal_ratio"]:
                return True, {
                    "first_limit_time": first_limit_time,
                    "seal_ratio": seal_ratio,
                }
        except:
            pass
        
        return False, {}
    
    def _check_volume_stagnation(self, today_data: Dict, 
                                  daily_data: pd.DataFrame,
                                  params: Dict) -> Tuple[bool, Dict]:
        """检查放量滞涨信号"""
        change_pct = today_data.get("涨跌幅", 0)
        volume = today_data.get("成交量", 0)
        
        # 未涨停但有一定涨幅
        if change_pct >= 5.0:
            return False, {}
        
        # 计算前5日均量
        if len(daily_data) < 6:
            return False, {}
        
        avg_volume_5d = daily_data.iloc[-6:-1]["vol"].mean()
        volume_ratio = volume / avg_volume_5d if avg_volume_5d > 0 else 0
        
        # 计算K线形态
        open_price = today_data.get("开盘价", 0)
        high_price = today_data.get("最高价", 0)
        low_price = today_data.get("最低价", 0)
        close_price = today_data.get("收盘价", 0)
        
        if close_price > open_price:
            body = close_price - open_price
            upper_shadow = high_price - close_price
        else:
            body = open_price - close_price
            upper_shadow = high_price - open_price
        
        body = max(body, 0.01)  # 避免除零
        shadow_ratio = upper_shadow / body
        
        # 放量滞涨条件
        if volume_ratio >= params["min_volume_ratio"] and shadow_ratio >= params["min_upper_shadow_ratio"]:
            return True, {
                "change_pct": change_pct,
                "volume_ratio": volume_ratio,
                "upper_shadow_ratio": shadow_ratio,
            }
        
        return False, {}
    
    def _check_trend_callback(self, dragon: DragonStock,
                               daily_data: pd.DataFrame,
                               params: Dict) -> Tuple[bool, Dict]:
        """检查趋势回调信号"""
        if len(daily_data) < 3:
            return False, {}
        
        # 计算5日均线
        daily_data = daily_data.sort_values("trade_date")
        daily_data["ma5"] = daily_data["close"].rolling(window=5).mean()
        
        # 获取最近2日数据
        last_2d = daily_data.tail(2)
        
        # 检查是否连续2日收盘价 < 5日均线
        days_below_ma5 = 0
        for _, row in last_2d.iterrows():
            if row["close"] < row["ma5"]:
                days_below_ma5 += 1
        
        if days_below_ma5 < params["max_days_below_ma5"]:
            return False, {}
        
        # 检查5日均线是否拐头向下
        ma5_today = last_2d.iloc[-1]["ma5"]
        ma5_yesterday = last_2d.iloc[-2]["ma5"]
        ma5_declining = ma5_today < ma5_yesterday
        
        if not ma5_declining:
            return False, {}
        
        # 计算回调幅度
        peak_price = dragon.entry_price * (1 + dragon.total_rise_10d)
        current_price = last_2d.iloc[-1]["close"]
        drawdown = (peak_price - current_price) / peak_price * 100
        
        if drawdown >= params["max_drawdown_pct"]:
            return True, {
                "days_below_ma5": days_below_ma5,
                "ma5_declining": ma5_declining,
                "drawdown_pct": drawdown,
            }
        
        return False, {}
    
    def _check_a_shock_warning(self, dragon: DragonStock,
                                daily_data: pd.DataFrame,
                                params: Dict) -> Tuple[bool, Dict]:
        """检查A杀预警信号"""
        if len(daily_data) < 3:
            return False, {}
        
        # 获取最近3日数据
        last_3d = daily_data.tail(3)
        
        # 计算3日最大回调
        highest = last_3d["high"].max()
        lowest = last_3d["low"].min()
        drawdown_3d = (highest - lowest) / highest * 100
        
        # 检查是否出现跌停
        has_limit_down = any(last_3d["pct_chg"] <= params["limit_down_threshold"])
        
        if drawdown_3d >= params["max_drawdown_3d"] or has_limit_down:
            return True, {
                "drawdown_3d": drawdown_3d,
                "has_limit_down": has_limit_down,
            }
        
        return False, {}
    
    # ==================== 趋势龙走强判断 ====================
    
    def check_trend_dragon_strengthening(self, dragon: DragonStock,
                                          auction_data: Dict,
                                          open_data: Dict,
                                          intraday_data: Dict = None) -> Tuple[bool, str, float]:
        """
        判断趋势龙头是否走强（弱转强）
        
        Returns:
            (是否走强, 信号类型, 置信度0-1)
        """
        params = self.params["trend_strengthening"]
        
        # 1. 检查竞价转强
        auction_ok, auction_detail = self._check_auction_strong(
            auction_data, params["auction_strong"]
        )
        if not auction_ok:
            return False, "", 0.0
        
        # 2. 检查开盘确认
        open_ok, open_detail = self._check_open_confirm(
            open_data, params["open_confirm"]
        )
        if not open_ok:
            return False, "", 0.0
        
        # 基础信号已满足，计算置信度
        confidence = 0.6  # 基础置信度
        
        # 3. 检查盘中确认（加分项）
        if intraday_data:
            intraday_ok, intraday_detail = self._check_intraday_confirm(
                intraday_data, params["intraday_confirm"]
            )
            if intraday_ok:
                confidence = 0.85  # 盘中确认提高置信度
                return True, "竞价转强+开盘确认+盘中攻击", confidence
        
        return True, "竞价转强+开盘确认", confidence
    
    def _check_auction_strong(self, auction_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查竞价转强"""
        gap_pct = auction_data.get("竞价涨幅", 0)
        auction_vol = auction_data.get("竞价成交量", 0)
        yesterday_vol = auction_data.get("昨日成交量", 1)
        auction_amount = auction_data.get("竞价金额", 0)
        market_cap = auction_data.get("流通市值", 0)
        
        # 判断小盘股
        is_small_cap = market_cap < 5_000_000_000 if market_cap else False
        min_amount = params["min_auction_amount_small"] if is_small_cap else params["min_auction_amount"]
        
        # 检查条件
        vol_ratio = auction_vol / yesterday_vol
        
        if (gap_pct >= params["min_gap_pct"] and 
            vol_ratio >= params["min_auction_vol_ratio"] and
            auction_amount >= min_amount):
            return True, {
                "gap_pct": gap_pct,
                "vol_ratio": vol_ratio,
                "auction_amount": auction_amount,
            }
        
        return False, {}
    
    def _check_open_confirm(self, open_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查开盘确认"""
        open_price = open_data.get("开盘价", 0)
        low_5min = open_data.get("5分钟最低价", open_price)
        max_pullback = (open_price - low_5min) / open_price * 100 if open_price > 0 else 0
        
        # 检查开盘后是否大幅回踩
        if max_pullback <= params["max_pullback_pct"]:
            return True, {
                "max_pullback": max_pullback,
            }
        
        return False, {}
    
    def _check_intraday_confirm(self, intraday_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查盘中确认"""
        touch_limit_up = intraday_data.get("触及涨停", False)
        close_rise_pct = intraday_data.get("收盘涨幅", 0)
        
        if touch_limit_up or close_rise_pct >= params["min_close_rise_pct"]:
            return True, {
                "touch_limit_up": touch_limit_up,
                "close_rise_pct": close_rise_pct,
            }
        
        return False, {}
    
    # ==================== 连板龙走弱判断 ====================
    
    def check_continuous_dragon_weakening(self, dragon: DragonStock,
                                          daily_data: pd.DataFrame,
                                          today_data: Dict,
                                          yesterday_data: Dict = None) -> Tuple[bool, str, Dict]:
        """
        判断连板龙是否走弱
        
        Returns:
            (是否走弱, 走弱类型, 详细数据)
        """
        params = self.params["continuous_weakening"]
        
        # 1. 检查断板信号
        if params["broken_board"]["enabled"]:
            is_broken, detail = self._check_continuous_broken_board(today_data, params["broken_board"])
            if is_broken:
                return True, "断板", detail
        
        # 2. 检查放量烂板信号
        if params["volume_bad_board"]["enabled"] and yesterday_data:
            is_bad, detail = self._check_volume_bad_board(
                today_data, yesterday_data, params["volume_bad_board"]
            )
            if is_bad:
                return True, "放量烂板", detail
        
        # 3. 检查尾盘偷袭信号
        if params["late_sneak"]["enabled"]:
            is_late, detail = self._check_late_sneak(today_data, params["late_sneak"])
            if is_late:
                return True, "尾盘偷袭", detail
        
        # 4. 检查竞价走弱信号
        if params["auction_weak"]["enabled"]:
            is_auction_weak, detail = self._check_auction_weak(today_data, params["auction_weak"])
            if is_auction_weak:
                return True, "竞价走弱", detail
        
        # 5. 检查A杀信号
        if params["a_shock"]["enabled"] and not daily_data.empty:
            is_a_shock, detail = self._check_continuous_a_shock(
                dragon, daily_data, params["a_shock"]
            )
            if is_a_shock:
                dragon.max_observation_days = params["a_shock"]["shorten_observation_to"]
                return True, "A杀", detail
        
        return False, "", {}
    
    def _check_continuous_broken_board(self, today_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查连板龙断板信号"""
        change_pct = today_data.get("涨跌幅", 0)
        is_limit_up = change_pct >= params["limit_up_threshold"]
        
        # 未涨停即断板
        if not is_limit_up:
            return True, {
                "change_pct": change_pct,
                "is_limit_up": is_limit_up,
            }
        
        # 涨停但炸板未回封（收盘价<涨停价）
        close_price = today_data.get("收盘价", 0)
        limit_up_price = today_data.get("涨停价", 0)
        if is_limit_up and close_price < limit_up_price * 0.995:
            return True, {
                "change_pct": change_pct,
                "close_price": close_price,
                "limit_up_price": limit_up_price,
                "炸板未回封": True,
            }
        
        return False, {}
    
    def _check_volume_bad_board(self, today_data: Dict,
                                 yesterday_data: Dict,
                                 params: Dict) -> Tuple[bool, Dict]:
        """检查放量烂板信号"""
        change_pct = today_data.get("涨跌幅", 0)
        break_count = today_data.get("开板次数", 0)
        volume = today_data.get("成交量", 0)
        seal_amount = today_data.get("封单额", 0)
        yesterday_seal_amount = yesterday_data.get("封单额", 0)
        
        # 当日涨停
        if change_pct < 9.5:
            return False, {}
        
        # 获取前3日平均成交量（简化处理，实际需要历史数据）
        volume_ratio = today_data.get("量比", 1.0)
        
        # 封单金额比例
        seal_ratio = seal_amount / yesterday_seal_amount if yesterday_seal_amount > 0 else 1.0
        
        if (break_count >= params["min_break_count"] and 
            volume_ratio >= params["min_volume_ratio"] and
            seal_ratio <= params["max_seal_amount_ratio"]):
            return True, {
                "break_count": break_count,
                "volume_ratio": volume_ratio,
                "seal_ratio": seal_ratio,
            }
        
        return False, {}
    
    def _check_late_sneak(self, today_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查尾盘偷袭信号"""
        change_pct = today_data.get("涨跌幅", 0)
        first_limit_time = today_data.get("首次封板时间", "")
        seal_ratio = today_data.get("封单资金占比", 0)
        seal_amount = today_data.get("封单额", 0)
        
        # 当日涨停
        if change_pct < 9.5:
            return False, {}
        
        # 解析封板时间
        try:
            hour, minute = map(int, first_limit_time.split(":"))
            limit_time = hour * 60 + minute
            max_time = 14 * 60 + 45  # 14:45
            
            if (limit_time > max_time and 
                seal_ratio < params["max_seal_ratio"] and
                seal_amount < params["max_seal_amount"]):
                return True, {
                    "first_limit_time": first_limit_time,
                    "seal_ratio": seal_ratio,
                    "seal_amount": seal_amount,
                }
        except:
            pass
        
        return False, {}
    
    def _check_auction_weak(self, today_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查竞价走弱信号"""
        open_price = today_data.get("开盘价", 0)
        yesterday_close = today_data.get("昨收", 0)
        auction_vol = today_data.get("竞价成交量", 0)
        yesterday_vol = today_data.get("昨日成交量", 1)
        
        # 低开
        if open_price >= yesterday_close:
            return False, {}
        
        # 竞价放量
        auction_vol_ratio = auction_vol / yesterday_vol
        if auction_vol_ratio >= params["max_auction_vol_ratio"]:
            return True, {
                "open_price": open_price,
                "yesterday_close": yesterday_close,
                "auction_vol_ratio": auction_vol_ratio,
            }
        
        return False, {}
    
    def _check_continuous_a_shock(self, dragon: DragonStock,
                                   daily_data: pd.DataFrame,
                                   params: Dict) -> Tuple[bool, Dict]:
        """检查连板龙A杀信号"""
        if len(daily_data) < 2:
            return False, {}
        
        # 获取最近2日数据
        last_2d = daily_data.tail(2)
        
        # 计算2日最大回调
        highest = last_2d["high"].max()
        lowest = last_2d["low"].min()
        drawdown_2d = (highest - lowest) / highest * 100
        
        # 检查是否出现跌停
        has_limit_down = any(last_2d["pct_chg"] <= params["limit_down_threshold"])
        
        # 检查天地板（从涨停到跌停）
        heaven_earth = False
        if len(last_2d) >= 1:
            today = last_2d.iloc[-1]
            if today["high"] >= today["close"] * 1.095 and today["close"] <= today["open"] * 0.905:
                heaven_earth = True
        
        if drawdown_2d >= params["max_drawdown_2d"] or has_limit_down or heaven_earth:
            return True, {
                "drawdown_2d": drawdown_2d,
                "has_limit_down": has_limit_down,
                "heaven_earth": heaven_earth,
            }
        
        return False, {}
    
    # ==================== 连板龙走强判断 ====================
    
    def check_continuous_dragon_strengthening(self, dragon: DragonStock,
                                               auction_data: Dict,
                                               open_data: Dict,
                                               intraday_data: Dict = None) -> Tuple[bool, str, float]:
        """
        判断连板龙是否走强（弱转强）
        
        Returns:
            (是否走强, 信号类型, 置信度0-1)
        """
        params = self.params["continuous_strengthening"]
        
        # 1. 检查竞价超预期
        auction_ok, auction_detail = self._check_auction_surprise(
            auction_data, params["auction_surprise"]
        )
        if not auction_ok:
            return False, "", 0.0
        
        # 2. 检查开盘承接
        open_ok, open_detail = self._check_open_support(
            open_data, params["open_support"]
        )
        if not open_ok:
            return False, "", 0.0
        
        # 基础信号已满足，计算置信度
        confidence = 0.65  # 连板龙基础置信度更高（断板反包预期更强）
        
        # 3. 检查盘中确认（加分项）
        if intraday_data:
            intraday_ok, intraday_detail = self._check_intraday_confirm(
                intraday_data, params["intraday_confirm"]
            )
            if intraday_ok:
                confidence = 0.90
                return True, "竞价超预期+开盘承接+盘中攻击", confidence
        
        return True, "竞价超预期+开盘承接", confidence
    
    def _check_auction_surprise(self, auction_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查竞价超预期（断板次日高开）"""
        gap_pct = auction_data.get("竞价涨幅", 0)
        auction_vol = auction_data.get("竞价成交量", 0)
        yesterday_vol = auction_data.get("昨日成交量", 1)
        auction_amount = auction_data.get("竞价金额", 0)
        
        vol_ratio = auction_vol / yesterday_vol
        
        # 连板龙要求更高的高开幅度
        if (gap_pct >= params["min_gap_pct"] and 
            vol_ratio >= params["min_auction_vol_ratio"] and
            auction_amount >= params["min_auction_amount"]):
            return True, {
                "gap_pct": gap_pct,
                "vol_ratio": vol_ratio,
                "auction_amount": auction_amount,
            }
        
        return False, {}
    
    def _check_open_support(self, open_data: Dict, params: Dict) -> Tuple[bool, Dict]:
        """检查开盘承接（连板龙版本，要求更严格）"""
        open_price = open_data.get("开盘价", 0)
        low_3min = open_data.get("3分钟最低价", open_price)
        
        # 3分钟内不破开盘价
        if low_3min >= open_price * 0.995:  # 允许0.5%的滑点
            return True, {
                "open_price": open_price,
                "low_3min": low_3min,
            }
        
        return False, {}
    
    # ==================== 池子管理接口 ====================
    
    def add_to_dragon_pool(self, stock: DragonStock):
        """添加股票到龙头池"""
        self.dragon_pool[stock.stock_code] = stock
        logger.info(f"[龙头池] 添加 {stock.stock_name}({stock.stock_code}) - {stock.dragon_type.value}")
    
    def move_to_weakening_pool(self, stock_code: str, date_str: str, 
                                weakening_type: str, weakening_price: float,
                                detail: Dict):
        """将股票从龙头池移至走弱池"""
        if stock_code not in self.dragon_pool:
            logger.warning(f"[走弱池] 股票 {stock_code} 不在龙头池中")
            return
        
        dragon = self.dragon_pool.pop(stock_code)
        dragon.status = DragonStatus.WEAKENING
        dragon.status_change_date = date_str
        dragon.weakening_date = date_str
        dragon.weakening_price = weakening_price
        dragon.weakening_type = weakening_type
        dragon.weakening_entry_date = date_str
        
        # 计算当前回调幅度
        dragon.max_drawdown = (dragon.entry_price - weakening_price) / dragon.entry_price
        
        self.weakening_pool[stock_code] = dragon
        logger.info(f"[走弱池] 移入 {dragon.stock_name}({stock_code}) - 类型:{weakening_type}, "
                   f"回调:{dragon.max_drawdown*100:.1f}%, 观察期:{dragon.max_observation_days}天")
    
    def move_to_recovered_pool(self, stock_code: str, date_str: str, signal_type: str):
        """将股票从走弱池移至已转强池"""
        if stock_code not in self.weakening_pool:
            logger.warning(f"[转强池] 股票 {stock_code} 不在走弱池中")
            return
        
        dragon = self.weakening_pool.pop(stock_code)
        dragon.status = DragonStatus.RECOVERED
        dragon.status_change_date = date_str
        
        self.recovered_pool[stock_code] = dragon
        logger.info(f"[转强池] ✓ {dragon.stock_name}({stock_code}) 弱转强成功 - {signal_type}")
    
    def remove_expired_stock(self, stock_code: str, reason: str = "观察期过期"):
        """移除过期股票"""
        if stock_code in self.weakening_pool:
            dragon = self.weakening_pool.pop(stock_code)
            dragon.status = DragonStatus.EXPIRED
            logger.info(f"[移除] {dragon.stock_name}({stock_code}) - 原因:{reason}")
    
    def update_daily(self, date_str: str, market_data: Dict):
        """
        每日更新池子状态
        
        Args:
            date_str: 日期字符串 YYYYMMDD
            market_data: 市场数据字典，包含各股票的数据
        """
        # 1. 检查龙头池中的股票是否走弱
        for code, dragon in list(self.dragon_pool.items()):
            if code not in market_data:
                continue
            
            today_data = market_data[code]
            daily_data = today_data.get("daily_df", pd.DataFrame())
            
            if dragon.dragon_type == DragonType.TREND:
                is_weakening, w_type, detail = self.check_trend_dragon_weakening(
                    dragon, daily_data, today_data
                )
            else:
                yesterday_data = today_data.get("yesterday_data", {})
                is_weakening, w_type, detail = self.check_continuous_dragon_weakening(
                    dragon, daily_data, today_data, yesterday_data
                )
            
            if is_weakening:
                weakening_price = today_data.get("收盘价", dragon.entry_price)
                self.move_to_weakening_pool(code, date_str, w_type, weakening_price, detail)
        
        # 2. 检查走弱池中的股票是否转强或过期
        for code, dragon in list(self.weakening_pool.items()):
            # 检查观察期是否过期
            if dragon.is_observation_expired(date_str):
                self.remove_expired_stock(code, "观察期过期")
                continue
            
            if code not in market_data:
                continue
            
            today_data = market_data[code]
            auction_data = today_data.get("auction", {})
            open_data = today_data.get("open", {})
            intraday_data = today_data.get("intraday", {})
            
            if dragon.dragon_type == DragonType.TREND:
                is_strengthening, s_type, confidence = self.check_trend_dragon_strengthening(
                    dragon, auction_data, open_data, intraday_data
                )
            else:
                is_strengthening, s_type, confidence = self.check_continuous_dragon_strengthening(
                    dragon, auction_data, open_data, intraday_data
                )
            
            if is_strengthening and confidence >= 0.6:
                self.move_to_recovered_pool(code, date_str, s_type)
                # 这里可以触发买入信号
                logger.info(f"[买入信号] {dragon.stock_name}({code}) 置信度:{confidence*100:.0f}% - {s_type}")
    
    def get_buy_signals(self) -> List[Dict]:
        """获取当前可买入的信号列表"""
        signals = []
        for code, dragon in self.recovered_pool.items():
            if dragon.status == DragonStatus.RECOVERED:
                signals.append({
                    "code": code,
                    "name": dragon.stock_name,
                    "type": dragon.dragon_type.value,
                    "weakening_type": dragon.weakening_type,
                    "entry_price": dragon.entry_price,
                })
        return signals
