"""
情绪分析引擎 - 主线强度、梯度追踪、联动判定
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from collections import defaultdict
import loguru
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import SENTIMENT_WEIGHTS, LIMIT_UP_THRESHOLD

logger = loguru.logger

class SentimentEngine:
    def __init__(self, weights: Dict[str, float] = None):
        self.weights = weights or SENTIMENT_WEIGHTS
        self.limit_up_threshold = LIMIT_UP_THRESHOLD
    
    def calculate_mainline_strength(self, hierarchy_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算主线板块强度
        公式: 涨停贡献分 = 涨停家数×权重 + 最高连板高度×权重
        使用实际的连板数数据
        """
        if hierarchy_df.empty:
            return pd.DataFrame()
        
        # 按L3行业聚合
        stats = []
        for l2_name, group in hierarchy_df.groupby('L2_Industry'):
            if l2_name == '其他':
                continue
                
            limit_up_count = len(group)
            
            # 使用实际的连板数，计算该行业的最高连板
            board_heights = group['BoardHeight'].fillna(1).astype(int)
            max_board_height = board_heights.max() if len(board_heights) > 0 else 1
            avg_board_height = board_heights.mean()
            
            # 计算贡献分
            score = (limit_up_count * self.weights['limit_up_count'] + 
                    max_board_height * self.weights['continuing_board_height'])
            
            # 获取该行业最高连板的股票
            top_stock = group.loc[group['BoardHeight'].idxmax(), 'Name'] if 'BoardHeight' in group.columns else ''
            
            stats.append({
                'L2_Industry': l2_name,
                'L2_Industry': group['L2_Industry'].iloc[0] if not group.empty else '未知',
                'L1_Industry': group['L1_Industry'].iloc[0] if not group.empty and 'L1_Industry' in group.columns else '未知',
                'LimitUp_Count': limit_up_count,
                'Max_BoardHeight': max_board_height,
                'Avg_BoardHeight': round(avg_board_height, 2),
                'Top_Stock': top_stock,
                'Strength_Score': round(score, 2),
                'Stocks': ','.join(group['Name'].tolist()[:5])  # 前5只股票
            })
        
        result_df = pd.DataFrame(stats)
        if not result_df.empty:
            result_df = result_df.sort_values('Strength_Score', ascending=False)
        return result_df
    
    def track_gradient(self, hierarchy_df: pd.DataFrame) -> Dict[str, any]:
        """
        梯度追踪：统计1B,2B,3B,4B,5B,6B+分布及联动判定
        使用实际的连板数数据
        """
        gradient = {
            '1B': [],
            '2B': [],
            '3B': [],
            '4B': [],
            '5B': [],
            '6B+': [],
            'highest_board': 0,
            'highest_stock': '',
            'industry_linkage': {}
        }
        
        if hierarchy_df.empty:
            return gradient
        
        # 使用实际的连板数数据
        for _, row in hierarchy_df.iterrows():
            name = row['Name']
            l3 = row['L2_Industry']
            # 使用实际的连板数字段
            board_height = row.get('BoardHeight', 1)
            if pd.isna(board_height):
                board_height = 1
            board_height = int(board_height)
            
            # 根据连板数分类
            if board_height >= 6:
                key = '6B+'
            elif board_height >= 5:
                key = '5B'
            elif board_height >= 4:
                key = '4B'
            elif board_height >= 3:
                key = '3B'
            elif board_height >= 2:
                key = '2B'
            else:
                key = '1B'
            
            gradient[key].append({
                'name': name,
                'board_height': board_height,
                'l3_industry': l3,
                'change_pct': row.get('ChangePct', 0)
            })
            
            if board_height > gradient['highest_board']:
                gradient['highest_board'] = board_height
                gradient['highest_stock'] = name
        
        # 联动判定：检查最高板所属行业的跟风情况
        if gradient['highest_stock']:
            leader_l3 = hierarchy_df[hierarchy_df['Name'] == gradient['highest_stock']]['L2_Industry'].values
            if len(leader_l3) > 0:
                leader_l3 = leader_l3[0]
                l3_stocks = hierarchy_df[hierarchy_df['L2_Industry'] == leader_l3]
                
                # 检查是否有2个以上一字板或秒板
                fast_limit = l3_stocks[l3_stocks['LimitUpTime'].astype(str) < '09:35:00']
                gradient['industry_linkage'] = {
                    'leader': gradient['highest_stock'],
                    'industry': leader_l3,
                    'followers_count': len(l3_stocks) - 1,
                    'fast_limit_count': len(fast_limit),
                    'is_strong_linkage': len(fast_limit) >= 2
                }
        
        return gradient
    
    def calculate_market_sentiment(self, hierarchy_df: pd.DataFrame, prev_limit_up_df: pd.DataFrame = None) -> Dict:
        """
        全市场情绪指标计算
        """
        sentiment = {}
        
        if hierarchy_df.empty:
            return sentiment
        
        total_limit_up = len(hierarchy_df)
        
        # 炸板率 = 炸板次数>0的股票 / 总涨停数
        broken_boards = len(hierarchy_df[hierarchy_df['OpenTimes'] > 0])
        sentiment['broken_board_rate'] = round(broken_boards / total_limit_up * 100, 2) if total_limit_up > 0 else 0
        
        # 昨日涨停溢价（需要前一日数据）
        if prev_limit_up_df is not None and not prev_limit_up_df.empty:
            # 简化计算：假设前日涨停股票今日平均收益
            sentiment['prev_limit_up_premium'] = 2.5  # 模拟数据
        else:
            sentiment['prev_limit_up_premium'] = None
        
        # 市场情绪温度
        sentiment['temperature'] = self._calculate_temperature(total_limit_up, sentiment['broken_board_rate'])
        sentiment['total_limit_up'] = total_limit_up
        sentiment['broken_boards'] = broken_boards
        
        return sentiment
    
    def _calculate_temperature(self, limit_up_count: int, broken_rate: float) -> str:
        """计算市场情绪温度"""
        if limit_up_count > 80 and broken_rate < 15:
            return "高潮(谨慎)"
        elif limit_up_count > 50 and broken_rate < 25:
            return "活跃(积极参与)"
        elif limit_up_count > 30:
            return "温和(精选个股)"
        elif limit_up_count < 15 or broken_rate > 40:
            return "冰点(观望)"
        else:
            return "震荡(控制仓位)"
    
    def detect_sector_rotation(self, current_df: pd.DataFrame, prev_df: pd.DataFrame) -> List[Dict]:
        """
        板块切换检测
        对比两期数据，检测新崛起的板块
        """
        if current_df.empty or prev_df.empty:
            return []
        
        rotation = []
        
        current_sectors = set(current_df['L2_Industry'].unique())
        prev_sectors = set(prev_df['L2_Industry'].unique())
        
        # 新出现的板块
        new_sectors = current_sectors - prev_sectors
        for sector in new_sectors:
            if sector != '其他':
                rotation.append({
                    'type': 'new_emerging',
                    'sector': sector,
                    'stocks': current_df[current_df['L2_Industry'] == sector]['Name'].tolist()
                })
        
        # 强度提升的板块
        current_strength = self.calculate_mainline_strength(current_df)
        prev_strength = self.calculate_mainline_strength(prev_df)
        
        if not current_strength.empty and not prev_strength.empty:
            for _, row in current_strength.head(3).iterrows():
                sector = row['L2_Industry']
                prev_row = prev_strength[prev_strength['L2_Industry'] == sector]
                if prev_row.empty:
                    continue
                if row['Strength_Score'] > prev_row['Strength_Score'].values[0] * 1.5:
                    rotation.append({
                        'type': 'strength_surge',
                        'sector': sector,
                        'score_change': f"{prev_row['Strength_Score'].values[0]:.2f} -> {row['Strength_Score']:.2f}"
                    })
        
        return rotation

if __name__ == "__main__":
    engine = SentimentEngine()
    print("情绪分析引擎初始化成功")
