"""
卡位板策略 - 后发先至，以低打高
核心：低位股抢先涨停，试图取代高位龙头地位
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import loguru

logger = loguru.logger

class PositionBattleType(Enum):
    SUCCESS = "卡位成功"      # 低位股封死，高位股炸板或后封
    FAILED = "卡位失败"       # 低位股炸板，高位股回封
    UNCERTAIN = "卡位待定"    # 两者都封死，次日竞争
    FAKE = "假卡位"           # 高位股仍强，低位股是跟风

@dataclass
class PositionBattleSignal:
    battle_type: PositionBattleType
    low_stock_code: str        # 卡位股（低位）
    low_stock_name: str
    high_stock_code: str       # 被卡股（高位）
    high_stock_name: str
    lead_time: int             # 领先时间（分钟）
    confidence: float
    action: str                # 操作建议
    risk_warning: str

class PositionBattleStrategy:
    def __init__(self, data_manager):
        self.dm = data_manager
        
        # 核心参数
        self.params = {
            # 高位股标准（被卡对象）
            "min_high_board": 3,             # 至少3板
            "max_high_board": 6,             # 不超过6板（太高卡位意义不大）
            
            # 低位股标准（卡位者）
            "max_low_board": 2,              # 最多2板（位置优势）
            "ideal_low_board": 1,            # 理想1板
            
            # 卡位时间差
            "min_lead_time": 5,              # 领先至少5分钟
            "ideal_lead_time": 15,           # 理想领先15分钟
            
            # 封板质量对比
            "high_blast_threshold": 2,       # 高位股炸板次数>2视为疲态
            "low_seal_strength": 0.08,       # 低位股封单>流通市值8%
        }
    
    def detect_position_battle(self,
                               sector_stocks: pd.DataFrame,      # 同板块所有股今日数据
                               today_zt_pool: pd.DataFrame,      # 今日涨停池
                               yesterday_zt_pool: pd.DataFrame    # 昨日涨停池（判断连板）
                               ) -> List[PositionBattleSignal]:
        """
        检测板块内卡位板机会
        返回：卡位信号列表（可能多个，需筛选最优）
        """
        signals = []
        
        logger.debug(f"[卡位板] 开始检测，输入数据: 今日{len(sector_stocks)}只, 昨日涨停池{len(yesterday_zt_pool)}只")
        
        # 打印前5条数据的连板数字段，用于调试
        if not sector_stocks.empty and '连板数' in sector_stocks.columns:
            sample = sector_stocks[['代码', '名称', '连板数', '涨跌幅']].head(5)
            logger.debug(f"[卡位板] 样本数据:\n{sample}")
        else:
            logger.debug(f"[卡位板] 数据列: {sector_stocks.columns.tolist() if not sector_stocks.empty else '空'}")
        
        # 1. 识别板块内的高位龙头（3板+）
        high_stocks = self._identify_high_stocks(
            sector_stocks, yesterday_zt_pool
        )
        
        logger.debug(f"[卡位板] 识别到 {len(high_stocks)} 个高位龙头(3-6板)")
        for h in high_stocks:
            logger.debug(f"[卡位板]   高位龙头: {h['name']}({h['code']}) - {h['boards']}板")
        
        if not high_stocks:
            logger.debug("[卡位板] 无高位龙头，不存在卡位基础")
            return signals
        
        # 2. 识别板块内的低位股（1-2板）
        low_stocks = self._identify_low_stocks(
            sector_stocks, yesterday_zt_pool
        )
        
        logger.debug(f"[卡位板] 识别到 {len(low_stocks)} 个低位股(1-2板)")
        for l in low_stocks:
            logger.debug(f"[卡位板]   低位股: {l['name']}({l['code']}) - {l['boards']}板")
        
        if not low_stocks:
            logger.debug("[卡位板] 无低位股，无法形成卡位")
            return signals
        
        # 3. 对比每对高低位股的涨停时间
        for high in high_stocks:
            for low in low_stocks:
                if low['code'] == high['code']:
                    continue
                
                battle = self._analyze_battle(high, low, today_zt_pool)
                if battle:
                    signals.append(battle)
        
        # 4. 筛选最优卡位信号
        return self._filter_best_signals(signals)
    
    def _identify_high_stocks(self, sector: pd.DataFrame, yest_pool: pd.DataFrame) -> List[Dict]:
        """识别板块内高位龙头（3板+）"""
        high_stocks = []
        checked = 0
        
        logger.debug(f"[卡位板] _identify_high_stocks: 检查{len(sector)}只股票，目标3-6板")
        
        for _, row in sector.iterrows():
            code = str(row['代码']).zfill(6)
            name = row.get('名称', '')
            
            # 计算连板高度（从昨日涨停池+今日状态）
            boards = self._calculate_boards(code, yest_pool, row)
            checked += 1
            
            # 打印每只股票的连板数调试信息
            if checked <= 10 or boards >= 3:
                logger.debug(f"[卡位板]   检查 {name}({code}): 连板数={boards}, 涨跌幅={row.get('涨跌幅', 0):.2f}%")
            
            if self.params["min_high_board"] <= boards <= self.params["max_high_board"]:
                logger.debug(f"[卡位板]   ✓ 命中高位龙头: {name}({code}) - {boards}板")
                high_stocks.append({
                    'code': code,
                    'name': row['名称'],
                    'boards': boards,
                    'limit_up_time': row.get('首次封板时间', ''),
                    'blast_times': row.get('炸板次数', 0),
                    'seal_amount': row.get('封单额', 0),
                    'float_cap': row.get('流通市值', 1),
                    'turnover': row.get('换手率', 0)
                })
        
        logger.debug(f"[卡位板] _identify_high_stocks: 共检查{checked}只，命中{len(high_stocks)}只高位龙头")
        
        # 按高度排序，取最高
        high_stocks.sort(key=lambda x: x['boards'], reverse=True)
        return high_stocks[:3]  # 最多3个高位股
    
    def _identify_low_stocks(self, sector: pd.DataFrame, yest_pool: pd.DataFrame) -> List[Dict]:
        """识别板块内低位股（1-2板）"""
        low_stocks = []

        for _, row in sector.iterrows():
            code = str(row['代码']).zfill(6)
            boards = self._calculate_boards(code, yest_pool, row)

            if 1 <= boards <= self.params["max_low_board"]:
                low_stocks.append({
                    'code': code,
                    'name': row['名称'],
                    'boards': boards,
                    'limit_up_time': row.get('首次封板时间', ''),
                    'blast_times': row.get('炸板次数', 0),
                    'seal_amount': row.get('封单额', 0),
                    'float_cap': row.get('流通市值', 1),
                    'is_first_board': boards == 1
                })

        return low_stocks
    
    def _calculate_boards(self, code: str, yest_pool: pd.DataFrame, today_row: pd.Series) -> int:
        """计算当前连板高度 - 优先使用涨停池中的连板数字段"""
        name = today_row.get('名称', '')
        
        # 今日涨停？
        change_pct = today_row.get('涨跌幅', 0)
        if change_pct < 9.5:
            logger.debug(f"[卡位板]     _calculate_boards: {name}({code}) 涨跌幅{change_pct:.2f}% < 9.5%，未涨停")
            return 0
        
        # 优先使用涨停池中的连板数字段
        board_count = today_row.get('连板数', 0)
        logger.debug(f"[卡位板]     _calculate_boards: {name}({code}) 原始连板数字段={board_count}")
        
        if pd.notna(board_count) and board_count > 0:
            logger.debug(f"[卡位板]     _calculate_boards: {name}({code}) 使用连板数字段={int(board_count)}")
            return int(board_count)
        
        # 如果没有连板数字段，通过昨日涨停池判断
        if not yest_pool.empty and code in yest_pool['代码'].astype(str).str.zfill(6).values:
            logger.debug(f"[卡位板]     _calculate_boards: {name}({code}) 无连板数字段，通过昨日涨停池判断=2板")
            return 2  # 至少2板
        else:
            return 1  # 首板
    
    def _analyze_battle(self, high: Dict, low: Dict, today_pool: pd.DataFrame) -> Optional[PositionBattleSignal]:
        """
        分析高低位股的卡位关系
        """
        # 时间对比
        high_time = high['limit_up_time']
        low_time = low['limit_up_time']
        
        if not high_time or not low_time:
            return None
        
        # 低位股必须抢先涨停
        lead_minutes = self._calculate_time_diff(low_time, high_time)
        if lead_minutes < self.params["min_lead_time"]:
            return None  # 领先时间不够，不算卡位
        
        # 封板质量对比
        high_seal_ratio = high['seal_amount'] / (high['float_cap'] * 10000)
        low_seal_ratio = low['seal_amount'] / (low['float_cap'] * 10000)
        
        # 高位股疲态信号
        high_weak_signs = 0
        if high['blast_times'] >= self.params["high_blast_threshold"]:
            high_weak_signs += 1
        if high_seal_ratio < 0.05:  # 封单弱
            high_weak_signs += 1
        if high['turnover'] > 25:   # 换手过高
            high_weak_signs += 1
        
        # 低位股强势信号
        low_strong_signs = 0
        if low['blast_times'] == 0:  # 不炸板
            low_strong_signs += 1
        if low_seal_ratio > self.params["low_seal_strength"]:
            low_strong_signs += 1
        if lead_minutes >= self.params["ideal_lead_time"]:
            low_strong_signs += 1
        
        # 判断卡位类型
        if high_weak_signs >= 2 and low_strong_signs >= 2:
            # 高位弱+低位强=卡位成功概率高
            battle_type = PositionBattleType.SUCCESS
            confidence = 0.75
            action = "打板买入低位股，回避高位股"
            risk = "高位股可能回封反卡，需盯盘"
            
        elif high_weak_signs >= 1 and low_strong_signs >= 1:
            # 双方都有机会
            battle_type = PositionBattleType.UNCERTAIN
            confidence = 0.55
            action = "观望，等收盘确认谁封死"
            risk = "次日竞争，不确定性高"
            
        elif high_weak_signs == 0:
            # 高位股仍强，低位股是跟风
            battle_type = PositionBattleType.FAKE
            confidence = 0.30
            action = "不介入，低位股是跟风"
            risk = "假卡位，高位股继续领涨"
            
        else:
            battle_type = PositionBattleType.FAILED
            confidence = 0.40
            action = "放弃，卡位失败"
            risk = "低位股炸板，高位股回封"
        
        return PositionBattleSignal(
            battle_type=battle_type,
            low_stock_code=low['code'],
            low_stock_name=low['name'],
            high_stock_code=high['code'],
            high_stock_name=high['name'],
            lead_time=lead_minutes,
            confidence=confidence,
            action=action,
            risk_warning=risk
        )
    
    def _calculate_time_diff(self, early_time: str, late_time: str) -> int:
        """计算时间差（分钟），early必须早于late"""
        try:
            fmt = "%H:%M:%S"
            early = datetime.strptime(early_time, fmt)
            late = datetime.strptime(late_time, fmt)
            diff = (late - early).total_seconds() / 60
            return int(diff) if diff > 0 else 0
        except:
            return 0
    
    def _filter_best_signals(self, signals: List[PositionBattleSignal]) -> List[PositionBattleSignal]:
        """筛选最优卡位信号"""
        if not signals:
            return []
        
        # 只保留成功或待定类型
        valid = [s for s in signals if s.battle_type in 
                [PositionBattleType.SUCCESS, PositionBattleType.UNCERTAIN]]
        
        # 按置信度排序
        valid.sort(key=lambda x: x.confidence, reverse=True)
        
        return valid[:2]  # 最多2个

# ==================== 实战应用 ====================

class PositionBattleTrader:
    """
    卡位板交易执行
    """
    
    def execute_battle_trade(self, signal: PositionBattleSignal, 
                           account: Dict) -> Dict:
        """
        执行卡位交易
        """
        if signal.battle_type != PositionBattleType.SUCCESS:
            return {'action': '观望', 'reason': signal.action}
        
        # 卡位板买点：低位股涨停瞬间
        # 不是打板，而是"卡位确认瞬间"
        
        return {
            'action': '买入',
            'target': signal.low_stock_name,
            'price': '涨停价',
            'position': 'medium',
            'stop_loss': '涨停价*0.93',
            'reason': f"卡位{signal.high_stock_name}，领先{signal.lead_time}分钟涨停",
            'risk': signal.risk_warning
        }

# ==================== 实战口诀 ====================

"""
卡位板，后发先至
高位龙头疲态显，低位新秀抢皇位
时间差，是核心，领先五分才入门
封单对比见真章，低位坚决高位慌
高位炸，低位封，卡位成功新龙升
高位回，低位炸，卡位失败双杀来

买点就一瞬间，低位涨停即确认
打板买？已晚矣，涨停瞬间已卡死
提前买？是赌博，可能卡位失败

只打最强卡位股，杂毛卡位不跟风
板块效应要配合，孤龙卡位难成功
"""

if __name__ == "__main__":
    print("卡位板策略加载完成")
    print("核心：低位股抢先涨停，取代高位龙头地位")
    print("关键：时间差+封单质量对比，缺一不可")
    print("买点：低位股涨停瞬间，确认卡位即买入")