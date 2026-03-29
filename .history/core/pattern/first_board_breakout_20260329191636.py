"""
热点首板突破策略 - 基于题材挖掘的补涨逻辑
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import loguru

logger = loguru.logger

class PatternType(Enum):
    HOTSPOT_FIRST_BOARD = "热点首板突破"  # 新增：热点驱动的首板

@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str
    reason: str
    key_metrics: Dict
    validation_rules: List[str]

class HotspotFirstBoardStrategy:
    def __init__(self, concept_mapper, sector_engine):
        """
        concept_mapper: 股票-概念映射器（EastMoneyConceptMapper）
        sector_engine: 板块热度引擎（MultiDimensionSectorEngine）
        """
        self.concept_mapper = concept_mapper
        self.sector_engine = sector_engine
        
        # 热点首板专用参数
        self.params = {
            "max_5d_rise": 0.15,        # 近5日涨幅<15%（低位要求）
            "min_volume_ratio": 3.0,       # 量比>3（资金突然介入）
            "max_limit_up_time": "14:30",  # 最晚14:30前涨停（拒绝偷袭板）
            "min_concept_match": 0.8,      # 概念匹配度>80%
            "hot_sector_heat_threshold": 5   # 板块3日涨停数>=5（确认是热点）
        }
    
    def detect_hotspot_first_board(self, 
                                   today_zt: pd.DataFrame,
                                   today_all: pd.DataFrame,
                                   hotspot_sectors: List[str],
                                   date_str: str) -> List[TradeSignal]:
        """
        检测热点首板突破机会
        
        Args:
            today_zt: 今日涨停池（已包含涨停时间、封单等）
            today_all: 今日全市场行情（用于计算5日涨幅、量比等）
            hotspot_sectors: 当日热点板块列表（从MultiDimensionSectorEngine获取）
            date_str: 日期字符串YYYYMMDD
        
        Returns:
            List[TradeSignal]: 符合条件的交易信号
        """
        signals = []
        
        if today_zt.empty or not hotspot_sectors:
            logger.warning("涨停池为空或无热点板块")
            return signals
        
        # 1. 筛选"首板"股票（近5日无涨停记录）
        first_board_candidates = self._filter_first_board(today_zt, date_str)
        logger.info(f"首板候选股: {len(first_board_candidates)}只")
        
        for _, stock in first_board_candidates.iterrows():
            code = stock.get('代码', '')
            name = stock.get('名称', '')
            
            # 2. 概念匹配检查：是否属于当日热点
            concept_match = self._check_concept_match(code, hotspot_sectors)
            if concept_match['score'] < self.params["min_concept_match"]:
                continue
            
            # 3. 低位检查：近5日涨幅<15%
            if not self._is_low_position(code, today_all):
                continue
            
            # 4. 量能检查：量比>3（资金突然介入）
            volume_ratio = self._calculate_volume_ratio(code, today_all)
            if volume_ratio < self.params["min_volume_ratio"]:
                continue
            
            # 5. 涨停时间检查：不能是尾盘偷袭
            limit_up_time = stock.get('首次封板时间', '')
            if not self._is_valid_limit_time(limit_up_time):
                continue
            
            # 6. 封单质量检查：封单持续增加，非烂板
            if not self._check_seal_quality(stock):
                continue
            
            # 7. 计算买点和风控
            entry_price = stock.get('涨停价', stock.get('最新价', 0))
            stop_loss = entry_price * 0.93  # 首板破板次日低开概率大，严格止损
            
            # 热点首板次日溢价预期：热点持续性决定
            hotspot_continuity = concept_match['hotspot_strength']
            take_profit = entry_price * (1.05 if hotspot_continuity == '强' else 1.03)
            
            signal = TradeSignal(
                pattern_type=PatternType.HOTSPOT_FIRST_BOARD,
                stock_code=code,
                stock_name=name,
                trigger_time=limit_up_time,
                confidence=concept_match['score'] * 0.9,  # 概念匹配度直接决定置信度
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size="light",  # 首板轻仓试错
                reason=f"热点[{concept_match['main_concept']}]首板突破+量比{volume_ratio:.1f}+低位",
                key_metrics={
                    "匹配热点": concept_match['main_concept'],
                    "概念匹配度": f"{concept_match['score']*100:.0f}%",
                    "近5日涨幅": f"{self._get_5d_rise(code, today_all)*100:.1f}%",
                    "量比": f"{volume_ratio:.1f}",
                    "涨停时间": limit_up_time,
                    "热点强度": hotspot_continuity,
                    "封单额": f"{stock.get('封单额', 0)/1e4:.0f}万"
                },
                validation_rules=[
                    f"属于当日热点: {concept_match['main_concept']}",
                    "近5日涨幅<15%（低位）",
                    f"量比>{self.params['min_volume_ratio']}（资金突然介入）",
                    f"涨停时间<{self.params['max_limit_up_time']}（非偷袭）",
                    "封单质量合格（非烂板）"
                ]
            )
            signals.append(signal)
        
        # 按置信度排序
        signals.sort(key=lambda x: x.confidence, reverse=True)
        return signals
    
    # ==================== 核心筛选方法 ====================
    
    def _filter_first_board(self, today_zt: pd.DataFrame, date_str: str) -> pd.DataFrame:
        """
        筛选真正的"首板"股：近5日无涨停记录
        """
        # 从缓存获取近5日涨停数据
        recent_zt = []
        for i in range(1, 6):
            prev_date = self._get_date_offset(date_str, -i)
            prev_zt = self.dm.get_limit_up_pool(prev_date) if hasattr(self, 'dm') else pd.DataFrame()
            if not prev_zt.empty:
                recent_zt.extend(prev_zt['代码'].tolist())
        
        recent_zt_set = set(recent_zt)
        
        # 今日涨停且不在近5日涨停列表中
        first_board = today_zt[~today_zt['代码'].isin(recent_zt_set)].copy()
        return first_board
    
    def _check_concept_match(self, stock_code: str, hotspot_sectors: List[str]) -> Dict:
        """
        检查股票与热点的概念匹配度
        返回: {'score': 匹配度0-1, 'main_concept': 主匹配概念, 'hotspot_strength': 热点强度}
        """
        # 获取股票所属概念
        stock_concepts = self.concept_mapper.get_concepts_by_stock(stock_code)
        
        if not stock_concepts:
            return {'score': 0, 'main_concept': '', 'hotspot_strength': '弱'}
        
        # 计算与热点的交集
        matched_concepts = set(stock_concepts) & set(hotspot_sectors)
        
        if not matched_concepts:
            return {'score': 0, 'main_concept': '', 'hotspot_strength': '弱'}
        
        # 匹配度 = 匹配概念数 / 热点板块数（上限1.0）
        match_score = min(len(matched_concepts) / len(hotspot_sectors), 1.0)
        
        # 主匹配概念（取第一个匹配的热点）
        main_concept = list(matched_concepts)[0]
        
        # 热点强度判断（从sector_engine获取）
        hotspot_strength = self._get_hotspot_strength(main_concept)
        
        return {
            'score': match_score,
            'main_concept': main_concept,
            'hotspot_strength': hotspot_strength
        }
    
    def _is_low_position(self, stock_code: str, today_all: pd.DataFrame) -> bool:
        """
        检查是否处于相对低位（近5日涨幅<15%）
        """
        stock_data = today_all[today_all['代码'] == stock_code]
        if stock_data.empty:
            return False
        
        # 获取近5日涨幅（从today_all中的字段或计算）
        rise_5d = stock_data.iloc[0].get('5日涨幅', 0) / 100
        
        return rise_5d < self.params["max_5d_rise"]
    
    def _calculate_volume_ratio(self, stock_code: str, today_all: pd.DataFrame) -> float:
        """
        计算量比（今日实时成交量 / 近5日均量）
        """
        stock_data = today_all[today_all['代码'] == stock_code]
        if stock_data.empty:
            return 0
        
        today_vol = stock_data.iloc[0].get('成交量', 0)
        vol_ma5 = stock_data.iloc[0].get('5日均量', today_vol)  #  fallback
        
        return today_vol / vol_ma5 if vol_ma5 > 0 else 0
    
    def _is_valid_limit_time(self, limit_up_time: str) -> bool:
        """
        检查涨停时间是否有效（非尾盘偷袭）
        """
        if not limit_up_time or limit_up_time == '-':
            return False
        
        try:
            hour, minute = map(int, limit_up_time.split(':')[:2])
            limit_dt = time(hour, minute)
            deadline = time(14, 30)
            return limit_dt <= deadline
        except:
            return False
    
    def _check_seal_quality(self, stock_data: pd.Series) -> bool:
        """
        检查封单质量（非烂板）
        """
        # 炸板次数=0
        blast_times = stock_data.get('炸板次数', 0)
        if blast_times > 0:
            return False
        
        # 封单额/流通市值 > 5%（首板要求可放宽）
        seal_amount = stock_data.get('封单额', 0)
        float_cap = stock_data.get('流通市值', 1) * 10000
        
        return seal_amount > float_cap * 0.05
    
    def _get_hotspot_strength(self, concept: str) -> str:
        """
        获取热点板块强度
        """
        # 从sector_engine查询该概念的3日涨停数
        heat_3d = self.sector_engine.get_concept_heat_3d(concept) if hasattr(self.sector_engine, 'get_concept_heat_3d') else 0
        
        if heat_3d >= 10:
            return "极强"
        elif heat_3d >= 5:
            return "强"
        elif heat_3d >= 3:
            return "中"
        return "弱"
    
    def _get_5d_rise(self, stock_code: str, today_all: pd.DataFrame) -> float:
        """获取近5日涨幅"""
        stock_data = today_all[today_all['代码'] == stock_code]
        if stock_data.empty:
            return 1.0  # 默认高位，排除
        return stock_data.iloc[0].get('5日涨幅', 100) / 100
    
    def _get_date_offset(self, date_str: str, offset: int) -> str:
        """日期偏移计算"""
        from datetime import datetime, timedelta
        dt = datetime.strptime(date_str, "%Y%m%d")
        target = dt + timedelta(days=offset)
        return target.strftime("%Y%m%d")

# ==================== 使用示例 ====================

if __name__ == "__main__":
    # 模拟数据测试
    strategy = HotspotFirstBoardStrategy(None, None)
    strategy.params["max_5d_rise"] = 0.20  # 测试放宽条件
    
    # 模拟今日涨停池
    mock_today_zt = pd.DataFrame({
        '代码': ['000001', '000002', '600000'],
        '名称': ['平安银行', '万科A', '浦发银行'],
        '首次封板时间': ['10:30:00', '14:00:00', '09:45:00'],
        '炸板次数': [0, 0, 0],
        '封单额': [50000000, 30000000, 80000000],
        '流通市值': [2000, 1500, 1800],  # 亿元
        '最新价': [10.5, 8.2, 7.8]
    })
    
    # 模拟全市场数据
    mock_today_all = pd.DataFrame({
        '代码': ['000001', '000002', '600000'],
        '5日涨幅': [10, 20, 8],  # %
        '成交量': [1000000, 800000, 1200000],
        '5日均量': [300000, 400000, 350000]
    })
    
    # 模拟热点板块
    hotspot_sectors = ['银行', '房地产', '金融科技']
    
    # 运行检测
    signals = strategy.detect_hotspot_first_board(
        mock_today_zt, mock_today_all, hotspot_sectors, "20260324"
    )
    
    print(f"检测到 {len(signals)} 个热点首板信号:")
    for sig in signals:
        print(f"\n【{sig.stock_name}】")
        print(f"  匹配热点: {sig.key_metrics['匹配热点']}")
        print(f"  置信度: {sig.confidence:.2f}")
        print(f"  量比: {sig.key_metrics['量比']}")
        print(f"  建议: {sig.reason}")