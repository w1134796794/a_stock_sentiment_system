"""
筹码结构分析器

功能：
1. 个股筹码分布分析（获利盘/套牢盘）
2. 筹码集中度分析
3. 成本与价格偏离度分析
4. 筹码结构类型判断
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class ChipStructureType(Enum):
    """筹码结构类型"""
    HIGH_DENSE = "高位密集"      # 高位获利盘多且集中
    HIGH_SCATTER = "高位分散"    # 高位获利盘多但分散
    LOW_DENSE = "低位密集"       # 低位套牢盘多且集中
    LOW_SCATTER = "低位分散"     # 低位套牢盘多但分散
    OSCILLATING = "震荡整理"     # 中间状态


@dataclass
class ChipStructure:
    """筹码结构数据"""
    ts_code: str
    name: str
    trade_date: str
    
    # 基础数据
    close: float = 0              # 收盘价
    avg_cost: float = 0           # 平均成本
    
    # 获利盘数据
    profit_pct: float = 0         # 获利盘比例（%）
    avg_profit: float = 0         # 平均获利比例（%）
    max_profit: float = 0         # 最大获利比例（%）
    
    # 套牢盘数据
    loss_pct: float = 0           # 套牢盘比例（%）
    avg_loss: float = 0           # 平均亏损比例（%）
    max_loss: float = 0           # 最大亏损比例（%）
    
    # 集中度
    concentration: float = 0      # 筹码集中度（%）
    
    # 结构类型
    structure_type: str = ""      # 筹码结构类型
    
    # 成本偏离
    cost_bias: float = 0          # 成本偏离度（%）


class ChipStructureAnalyzer:
    """筹码结构分析器"""

    def __init__(self, data_manager):
        self.dm = data_manager

    @property
    def extensions(self):
        """[Deprecated] 历史兼容入口，等价于 self.dm。

        新版 DataManager 已直接合并资金流向 / 筹码接口，可直接使用 self.dm 调用。
        """
        return self.dm

    # =========================================================================
    # 基础筹码分析
    # =========================================================================

    def analyze_chip_structure(self, ts_code: str, trade_date: str) -> ChipStructure:
        """
        分析个股筹码结构
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            ChipStructure: 筹码结构分析结果
        """
        df = self.extensions.get_cyq_perf(ts_code, trade_date=trade_date)
        
        if df.empty:
            return ChipStructure(ts_code=ts_code, name="", trade_date=trade_date)
        
        row = df.iloc[0]
        
        close = row.get('close', 0)
        avg_cost = row.get('avg_cost', 0)
        profit_pct = row.get('profit_pct', 0)
        concentration = row.get('concentration', 0)
        
        # 计算成本偏离度
        cost_bias = ((close - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
        
        # 判断结构类型
        structure_type = self._classify_structure(profit_pct, concentration)
        
        return ChipStructure(
            ts_code=ts_code,
            name=row.get('name', ''),
            trade_date=trade_date,
            close=close,
            avg_cost=avg_cost,
            profit_pct=profit_pct,
            avg_profit=row.get('avg_profit', 0),
            max_profit=row.get('max_profit', 0),
            loss_pct=100 - profit_pct,
            avg_loss=row.get('avg_loss', 0),
            max_loss=row.get('max_loss', 0),
            concentration=concentration,
            structure_type=structure_type,
            cost_bias=cost_bias,
        )

    def _classify_structure(self, profit_pct: float, concentration: float) -> str:
        """
        分类筹码结构
        
        Args:
            profit_pct: 获利盘比例
            concentration: 集中度
            
        Returns:
            str: 结构类型
        """
        if profit_pct >= 80:
            return ChipStructureType.HIGH_DENSE.value if concentration > 30 else ChipStructureType.HIGH_SCATTER.value
        elif profit_pct <= 20:
            return ChipStructureType.LOW_DENSE.value if concentration > 30 else ChipStructureType.LOW_SCATTER.value
        else:
            return ChipStructureType.OSCILLATING.value

    # =========================================================================
    # 筹码变化趋势分析
    # =========================================================================

    def analyze_chip_trend(self, ts_code: str, trade_date: str,
                           days: int = 5) -> Dict:
        """
        分析筹码变化趋势
        
        Args:
            ts_code: 股票代码
            trade_date: 结束日期
            days: 回溯天数
            
        Returns:
            Dict: 筹码趋势分析
        """
        from core.utils import DateUtils
        
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(days, trade_date)
        
        chips = []
        for date in date_list:
            chip = self.analyze_chip_structure(ts_code, date)
            if chip.profit_pct > 0:  # 有数据
                chips.append(chip)
        
        if len(chips) < 2:
            return {}
        
        # 计算趋势
        profit_trend = chips[0].profit_pct - chips[-1].profit_pct
        concentration_trend = chips[0].concentration - chips[-1].concentration
        
        return {
            'ts_code': ts_code,
            'profit_trend': profit_trend,
            'concentration_trend': concentration_trend,
            'current_profit': chips[0].profit_pct,
            'current_concentration': chips[0].concentration,
            'structure_changed': chips[0].structure_type != chips[-1].structure_type,
            'trend_description': self._describe_chip_trend(profit_trend, concentration_trend),
        }

    def _describe_chip_trend(self, profit_trend: float, concentration_trend: float) -> str:
        """描述筹码趋势"""
        if profit_trend > 10 and concentration_trend > 5:
            return "获利盘增加且集中，主力吸筹"
        elif profit_trend > 10 and concentration_trend < -5:
            return "获利盘增加但分散，散户追涨"
        elif profit_trend < -10 and concentration_trend > 5:
            return "获利盘减少且集中，主力派发"
        elif profit_trend < -10 and concentration_trend < -5:
            return "获利盘减少且分散，恐慌抛售"
        elif abs(profit_trend) < 5 and concentration_trend > 5:
            return "筹码趋于集中，可能变盘"
        else:
            return "筹码结构稳定"

    # =========================================================================
    # 压力位与支撑位分析
    # =========================================================================

    def analyze_support_resistance(self, ts_code: str, trade_date: str) -> Dict:
        """
        分析筹码密集区的压力位和支撑位
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            Dict: 压力位和支撑位分析
        """
        chip = self.analyze_chip_structure(ts_code, trade_date)
        
        if not chip.avg_cost:
            return {}
        
        close = chip.close
        avg_cost = chip.avg_cost
        
        # 基于成本判断压力/支撑
        if close > avg_cost * 1.05:  # 现价高于成本5%以上
            support = avg_cost
            resistance = avg_cost * 1.1  # 假设压力位在成本上方10%
            position = "成本上方"
        elif close < avg_cost * 0.95:  # 现价低于成本5%以上
            support = avg_cost * 0.9
            resistance = avg_cost
            position = "成本下方"
        else:
            support = avg_cost * 0.95
            resistance = avg_cost * 1.05
            position = "成本附近"
        
        # 套牢盘压力评估
        if chip.loss_pct > 60:
            pressure_level = "高"
        elif chip.loss_pct > 40:
            pressure_level = "中等"
        else:
            pressure_level = "低"
        
        return {
            'ts_code': ts_code,
            'close': close,
            'avg_cost': avg_cost,
            'support': support,
            'resistance': resistance,
            'position': position,
            'loss_pct': chip.loss_pct,
            'pressure_level': pressure_level,
            'break_even_price': avg_cost,
        }

    # =========================================================================
    # 批量分析
    # =========================================================================

    def analyze_batch_chips(self, ts_codes: List[str], trade_date: str) -> pd.DataFrame:
        """
        批量分析筹码结构
        
        Args:
            ts_codes: 股票代码列表
            trade_date: 交易日期
            
        Returns:
            DataFrame: 批量筹码分析结果
        """
        results = []
        
        for ts_code in ts_codes:
            try:
                chip = self.analyze_chip_structure(ts_code, trade_date)
                results.append({
                    'ts_code': chip.ts_code,
                    'name': chip.name,
                    'close': chip.close,
                    'profit_pct': chip.profit_pct,
                    'concentration': chip.concentration,
                    'structure_type': chip.structure_type,
                    'cost_bias': chip.cost_bias,
                })
            except Exception as e:
                logger.warning(f"[analyze_batch_chips] 分析失败 {ts_code}: {e}")
        
        if results:
            return pd.DataFrame(results)
        
        return pd.DataFrame()

    def find_low_position_stocks(self, trade_date: str,
                                  stock_list: List[str] = None,
                                  min_profit_pct: float = 70,
                                  max_concentration: float = 40) -> pd.DataFrame:
        """
        查找低位筹码结构的股票
        
        条件：
        - 获利盘比例低（套牢盘多）
        - 筹码集中度适中
        
        Args:
            trade_date: 交易日期
            stock_list: 股票列表
            min_profit_pct: 最大获利盘比例
            max_concentration: 最大集中度
            
        Returns:
            DataFrame: 符合条件的股票
        """
        if stock_list is None:
            # 获取当日涨停股票
            limit_up_df = self.dm.get_limit_up_pool(trade_date)
            if not limit_up_df.empty:
                code_col = '代码' if '代码' in limit_up_df.columns else ('code' if 'code' in limit_up_df.columns else None)
                if code_col:
                    stock_list = limit_up_df[code_col].tolist()
        
        if not stock_list:
            return pd.DataFrame()
        
        df = self.analyze_batch_chips(stock_list, trade_date)
        
        if df.empty:
            return df
        
        # 筛选低位筹码
        low_position = df[
            (df['profit_pct'] <= min_profit_pct) &
            (df['concentration'] <= max_concentration)
        ]
        
        return low_position.sort_values('profit_pct')

    def find_breakout_stocks(self, trade_date: str,
                             stock_list: List[str] = None) -> pd.DataFrame:
        """
        查找筹码突破的股票
        
        条件：
        - 获利盘比例快速增加
        - 筹码集中度提高
        - 成本偏离度转正
        
        Args:
            trade_date: 交易日期
            stock_list: 股票列表
            
        Returns:
            DataFrame: 筹码突破股票
        """
        if stock_list is None:
            limit_up_df = self.dm.get_limit_up_pool(trade_date)
            if not limit_up_df.empty:
                code_col = '代码' if '代码' in limit_up_df.columns else ('code' if 'code' in limit_up_df.columns else None)
                if code_col:
                    stock_list = limit_up_df[code_col].tolist()
        
        if not stock_list:
            return pd.DataFrame()
        
        results = []
        for ts_code in stock_list:
            try:
                trend = self.analyze_chip_trend(ts_code, trade_date, days=3)
                if trend and trend.get('profit_trend', 0) > 10:
                    chip = self.analyze_chip_structure(ts_code, trade_date)
                    results.append({
                        'ts_code': ts_code,
                        'name': chip.name,
                        'profit_pct': chip.profit_pct,
                        'profit_trend': trend['profit_trend'],
                        'structure_type': chip.structure_type,
                        'cost_bias': chip.cost_bias,
                    })
            except Exception as e:
                logger.warning(f"[find_breakout_stocks] 分析失败 {ts_code}: {e}")
        
        if results:
            return pd.DataFrame(results).sort_values('profit_trend', ascending=False)
        
        return pd.DataFrame()

    # =========================================================================
    # 与首板突破策略结合
    # =========================================================================

    def analyze_breakout_chip_quality(self, ts_code: str, trade_date: str) -> Dict:
        """
        分析首板突破股票的筹码质量
        
        评估维度：
        1. 套牢盘压力（越低越好）
        2. 筹码集中度（适中最好）
        3. 成本偏离度（正值表示突破成本）
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            Dict: 筹码质量评估
        """
        chip = self.analyze_chip_structure(ts_code, trade_date)
        sr = self.analyze_support_resistance(ts_code, trade_date)
        
        if not chip.profit_pct:
            return {'quality_score': 0, 'assessment': '无筹码数据'}
        
        score = 0
        factors = []
        
        # 1. 套牢盘压力评分（越低越好，满分30）
        loss_pct = 100 - chip.profit_pct
        if loss_pct < 20:
            score += 30
            factors.append("套牢盘少(优秀)")
        elif loss_pct < 40:
            score += 20
            factors.append("套牢盘适中")
        elif loss_pct < 60:
            score += 10
            factors.append("套牢盘较多")
        else:
            factors.append("套牢盘压力大")
        
        # 2. 筹码集中度评分（20-40%最好，满分30）
        if 20 <= chip.concentration <= 40:
            score += 30
            factors.append("筹码集中度适中(优秀)")
        elif 10 <= chip.concentration < 20 or 40 < chip.concentration <= 50:
            score += 20
            factors.append("筹码集中度一般")
        else:
            score += 10
            factors.append("筹码分散或过度集中")
        
        # 3. 成本偏离度评分（正值且适中最好，满分40）
        if 0 < chip.cost_bias <= 10:
            score += 40
            factors.append("刚突破成本区(优秀)")
        elif 10 < chip.cost_bias <= 20:
            score += 30
            factors.append("突破成本区一段距离")
        elif chip.cost_bias > 20:
            score += 15
            factors.append("远离成本区，注意追高风险")
        else:
            score += 10
            factors.append("仍在成本区下方")
        
        # 综合评估
        if score >= 80:
            assessment = "筹码结构优秀，适合参与"
        elif score >= 60:
            assessment = "筹码结构良好，可考虑参与"
        elif score >= 40:
            assessment = "筹码结构一般，谨慎参与"
        else:
            assessment = "筹码结构较差，建议观望"
        
        return {
            'ts_code': ts_code,
            'quality_score': score,
            'assessment': assessment,
            'factors': factors,
            'profit_pct': chip.profit_pct,
            'concentration': chip.concentration,
            'cost_bias': chip.cost_bias,
            'structure_type': chip.structure_type,
            'support_price': sr.get('support', 0),
            'resistance_price': sr.get('resistance', 0),
        }


# 便捷函数
def create_chip_analyzer(data_manager) -> ChipStructureAnalyzer:
    """创建筹码结构分析器"""
    return ChipStructureAnalyzer(data_manager)
