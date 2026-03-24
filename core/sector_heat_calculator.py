"""
多维度板块热度计算器
基于3日/5日/20日涨停数据计算板块热度、动量加速度和持续性
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import loguru

logger = loguru.logger


class SectorHeatCalculator:
    """
    多维度板块热度计算器
    
    输入层：每日涨停数据 → 按L3行业聚合 → 计算3日/5日/20日涨停数
    
    计算层：
      1. 原始热度 = 3日×权重A + 5日×权重B + 20日×权重C
      2. 动量加速度 = (3日-5日均值)/5日均值 → 判断新主线爆发力
      3. 持续性得分 = 20日热度 × 持续天数 → 判断老主线是否退潮
      4. 综合得分 = 原始热度 × (1 + 加速度系数) × 持续性系数
    
    输出层：
      - 新主线候选（3日/5日突增，加速度>阈值）
      - 老主线确认（20日稳定，加速度平稳）
      - 退潮预警（3日/5日骤降，20日仍高）
    """
    
    def __init__(self, weights: Dict[str, float] = None):
        """
        初始化计算器
        
        Args:
            weights: 权重配置
                - weight_3d: 3日权重
                - weight_5d: 5日权重  
                - weight_20d: 20日权重
                - momentum_threshold: 动量加速度阈值
                - continuity_threshold: 持续性阈值
        """
        self.weights = weights or {
            'weight_3d': 0.4,      # 3日权重（短期热度）
            'weight_5d': 0.3,      # 5日权重（中期热度）
            'weight_20d': 0.3,     # 20日权重（长期热度）
            'momentum_threshold': 0.3,  # 动量加速度阈值（30%）
            'decline_threshold': -0.2,  # 退潮阈值（-20%）
            'continuity_days': 5,   # 持续性判断天数
        }
    
    def calculate_sector_heat(self, sector_stats: Dict[str, Dict]) -> pd.DataFrame:
        """
        计算板块热度
        
        Args:
            sector_stats: 板块统计数据，格式：
                {
                    'L3行业名': {
                        '3d_count': 3日涨停数,
                        '5d_count': 5日涨停数,
                        '20d_count': 20日涨停数,
                        'continuity_days': 持续天数,
                        'L1_Industry': '一级行业',
                        'L2_Industry': '二级行业'
                    }
                }
        
        Returns:
            DataFrame包含各维度得分和分类
        """
        results = []
        
        for l3_name, stats in sector_stats.items():
            if l3_name == '其他':
                continue
            
            count_3d = stats.get('3d_count', 0)
            count_5d = stats.get('5d_count', 0)
            count_20d = stats.get('20d_count', 0)
            continuity_days = stats.get('continuity_days', 0)
            
            # 1. 计算原始热度
            raw_heat = self._calculate_raw_heat(count_3d, count_5d, count_20d)
            
            # 2. 计算动量加速度
            momentum = self._calculate_momentum(count_3d, count_5d)
            
            # 3. 计算持续性得分
            continuity_score = self._calculate_continuity(count_20d, continuity_days)
            
            # 4. 计算综合得分
            total_score = self._calculate_total_score(raw_heat, momentum, continuity_score)
            
            # 5. 分类判断
            category = self._classify_sector(momentum, count_3d, count_5d, count_20d)
            
            results.append({
                'L1_Industry': stats.get('L1_Industry', '未知'),
                'L2_Industry': stats.get('L2_Industry', '未知'),
                'L3_Industry': l3_name,
                '3日涨停数': count_3d,
                '5日涨停数': count_5d,
                '20日涨停数': count_20d,
                '原始热度': round(raw_heat, 2),
                '动量加速度': round(momentum, 4),
                '持续性得分': round(continuity_score, 2),
                '综合得分': round(total_score, 2),
                '持续天数': continuity_days,
                '板块分类': category
            })
        
        df = pd.DataFrame(results)
        if not df.empty:
            df = df.sort_values('综合得分', ascending=False)
        
        return df
    
    def _calculate_raw_heat(self, count_3d: int, count_5d: int, count_20d: int) -> float:
        """
        计算原始热度
        公式：原始热度 = 3日×权重A + 5日×权重B + 20日×权重C
        """
        # 标准化处理（避免数值过大）
        norm_3d = count_3d / 3  # 日均涨停数
        norm_5d = count_5d / 5
        norm_20d = count_20d / 20
        
        raw_heat = (
            norm_3d * self.weights['weight_3d'] +
            norm_5d * self.weights['weight_5d'] +
            norm_20d * self.weights['weight_20d']
        ) * 100  # 放大100倍便于阅读
        
        return raw_heat
    
    def _calculate_momentum(self, count_3d: int, count_5d: int) -> float:
        """
        计算动量加速度
        公式：动量加速度 = (3日-5日均值)/5日均值
        
        正值表示短期加速，负值表示短期减速
        """
        avg_5d = count_5d / 5 if count_5d > 0 else 0
        avg_3d = count_3d / 3 if count_3d > 0 else 0
        
        if avg_5d == 0:
            return 0 if avg_3d == 0 else 1.0  # 如果是新出现的热点，加速度设为1
        
        momentum = (avg_3d - avg_5d) / avg_5d
        return momentum
    
    def _calculate_continuity(self, count_20d: int, continuity_days: int) -> float:
        """
        计算持续性得分
        公式：持续性得分 = 20日热度 × 持续天数系数
        
        持续天数系数：持续天数越长，系数越高
        """
        norm_20d = count_20d / 20 if count_20d > 0 else 0
        
        # 持续天数系数（ sigmoid函数形式）
        if continuity_days <= 0:
            continuity_factor = 0.5
        else:
            continuity_factor = min(continuity_days / self.weights['continuity_days'], 2.0)
        
        continuity_score = norm_20d * continuity_factor * 100
        return continuity_score
    
    def _calculate_total_score(self, raw_heat: float, momentum: float, 
                               continuity_score: float) -> float:
        """
        计算综合得分
        公式：综合得分 = 原始热度 × (1 + 加速度系数) × 持续性系数
        
        加速度系数：将动量加速度映射到0.5-2.0的范围
        持续性系数：将持续性得分映射到0.8-1.5的范围
        """
        # 加速度系数（限制在0.5-2.0之间）
        momentum_factor = 1 + momentum
        momentum_factor = max(0.5, min(2.0, momentum_factor))
        
        # 持续性系数（限制在0.8-1.5之间）
        continuity_factor = 0.8 + (continuity_score / 100) * 0.7
        continuity_factor = max(0.8, min(1.5, continuity_factor))
        
        total_score = raw_heat * momentum_factor * continuity_factor
        return total_score
    
    def _classify_sector(self, momentum: float, count_3d: int, 
                         count_5d: int, count_20d: int) -> str:
        """
        分类板块类型
        
        分类逻辑：
        - 新主线候选：3日/5日突增，加速度>阈值
        - 老主线确认：20日稳定，加速度平稳(-0.1~0.3)
        - 退潮预警：3日/5日骤降，20日仍高
        - 观察期：其他情况
        """
        threshold = self.weights['momentum_threshold']
        decline_threshold = self.weights['decline_threshold']
        
        # 新主线候选：短期突增，加速度高
        if momentum > threshold and count_3d >= 2:
            return '新主线候选'
        
        # 退潮预警：短期下降，但长期仍高
        if momentum < decline_threshold and count_20d >= 10 and count_3d < count_20d / 10:
            return '退潮预警'
        
        # 老主线确认：持续稳定，加速度平稳
        if count_20d >= 15 and -0.1 <= momentum <= 0.3:
            return '老主线确认'
        
        # 观察期：其他情况
        return '观察期'
    
    def analyze_from_limit_up_data(self, limit_up_history: Dict[str, pd.DataFrame],
                                   industry_mapper=None) -> pd.DataFrame:
        """
        从涨停历史数据计算板块热度
        
        Args:
            limit_up_history: 按日期分组的涨停数据字典
                {
                    '20260320': DataFrame,
                    '20260321': DataFrame,
                    ...
                }
            industry_mapper: 行业映射器（可选）
        
        Returns:
            板块热度分析结果
        """
        # 按L3行业统计各时间段涨停数
        sector_stats = defaultdict(lambda: {
            '3d_count': 0,
            '5d_count': 0,
            '20d_count': 0,
            'continuity_days': 0,
            'daily_counts': defaultdict(int),
            'L1_Industry': '未知',
            'L2_Industry': '未知'
        })
        
        # 获取日期列表并排序
        dates = sorted(limit_up_history.keys(), reverse=True)
        
        for idx, date in enumerate(dates):
            df = limit_up_history[date]
            if df.empty:
                continue
            
            for _, row in df.iterrows():
                # 获取L3行业
                l3 = row.get('L3_Industry', row.get('所属行业', '其他'))
                if l3 == '其他':
                    continue
                
                # 统计各时间段
                if idx < 3:  # 最近3日
                    sector_stats[l3]['3d_count'] += 1
                if idx < 5:  # 最近5日
                    sector_stats[l3]['5d_count'] += 1
                if idx < 20:  # 最近20日
                    sector_stats[l3]['20d_count'] += 1
                
                # 记录每日涨停数（用于计算持续天数）
                sector_stats[l3]['daily_counts'][date] += 1
                
                # 获取L1和L2
                if 'L1_Industry' in row and 'L2_Industry' in row:
                    sector_stats[l3]['L1_Industry'] = row['L1_Industry']
                    sector_stats[l3]['L2_Industry'] = row['L2_Industry']
        
        # 计算持续天数（连续有涨停的天数）
        for l3, stats in sector_stats.items():
            daily_counts = stats['daily_counts']
            sorted_dates = sorted(daily_counts.keys(), reverse=True)
            
            continuity_days = 0
            for date in sorted_dates:
                if daily_counts[date] > 0:
                    continuity_days += 1
                else:
                    break
            
            stats['continuity_days'] = continuity_days
        
        # 计算板块热度
        return self.calculate_sector_heat(sector_stats)
    
    def get_top_sectors(self, heat_df: pd.DataFrame, category: str = None, 
                        top_n: int = 5) -> pd.DataFrame:
        """
        获取排名靠前的板块
        
        Args:
            heat_df: 板块热度DataFrame
            category: 筛选特定分类（可选）
            top_n: 返回前N个
        
        Returns:
            筛选后的DataFrame
        """
        if heat_df.empty:
            return heat_df
        
        if category:
            filtered = heat_df[heat_df['板块分类'] == category]
        else:
            filtered = heat_df
        
        return filtered.head(top_n)


if __name__ == "__main__":
    # 测试代码
    calculator = SectorHeatCalculator()
    
    # 模拟数据
    test_stats = {
        '光伏设备': {
            '3d_count': 12,
            '5d_count': 15,
            '20d_count': 45,
            'continuity_days': 8,
            'L1_Industry': '电力设备',
            'L2_Industry': '光伏设备'
        },
        '锂电池': {
            '3d_count': 3,
            '5d_count': 8,
            '20d_count': 50,
            'continuity_days': 2,
            'L1_Industry': '电力设备',
            'L2_Industry': '电池'
        },
        '人工智能': {
            '3d_count': 15,
            '5d_count': 12,
            '20d_count': 20,
            'continuity_days': 5,
            'L1_Industry': '计算机',
            'L2_Industry': '软件开发'
        }
    }
    
    result = calculator.calculate_sector_heat(test_stats)
    print("板块热度分析结果：")
    print(result.to_string())
    
    print("\n新主线候选：")
    new_mainlines = calculator.get_top_sectors(result, category='新主线候选')
    print(new_mainlines[['L3_Industry', '动量加速度', '综合得分']].to_string())
