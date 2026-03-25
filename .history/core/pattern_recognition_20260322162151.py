"""
模式识别算法 - 整合strategy_engine.py的高级模式
包含：弱转强、二板定龙、分歧转一致、首板突破、龙回头、卡位板等
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass
import loguru

logger = loguru.logger

@dataclass
class PatternSignal:
    pattern_type: str
    stock_code: str
    stock_name: str
    confidence: float
    description: str
    key_metrics: Dict

class PatternRecognition:
    def __init__(self, data_manager):
        self.dm = data_manager
        self.lookback_days = 20
        
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        弱转强模式识别：
        条件：昨日涨停但烂板/炸板 + 今日跳空高开2%以上 + 今日涨停
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', today_row.get('ts_code', ''))
            name = today_row.get('名称', today_row.get('name', ''))
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code] if '代码' in yesterday_df.columns else yesterday_df[yesterday_df['ts_code'] == code]
            
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 关键检查：昨日必须是真的涨停（涨幅>=9.5%）
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            
            if yest_change < 9.5:
                continue
            
            # 判断条件
            # 1. 昨日涨停但烂板（炸板次数>0 或 最后封板时间晚于10:00）
            last_limit_time = str(yest_data.get('最后封板时间', '')).strip()
            if last_limit_time.isdigit():
                last_limit_time = last_limit_time.zfill(6)
            if len(last_limit_time) == 6:
                last_limit_time = f"{last_limit_time[:2]}:{last_limit_time[2:4]}:{last_limit_time[4:]}"
            
            yesterday_bad_board = (yest_data.get('炸板次数', 0) > 0 or 
                                  last_limit_time > '10:00:00')
            
            # 2. 今日跳空高开（涨幅>2%）
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            today_gap_up = today_change > 2.0
            
            # 3. 今日也是涨停（>=9.5%）
            today_limit_up = today_change >= 9.5
            
            if yesterday_bad_board and today_gap_up and today_limit_up:
                signal = PatternSignal(
                    pattern_type="弱转强",
                    stock_code=code,
                    stock_name=name,
                    confidence=0.85,
                    description=f"昨日烂板后今日跳空高开{today_change:.2f}%",
                    key_metrics={
                        "昨日涨幅": f"{yest_change:.2f}%",
                        "昨日炸板次数": yest_data.get('炸板次数', 0),
                        "昨日最后封板": yest_data.get('最后封板时间', ''),
                        "今日涨幅": f"{today_change:.2f}%",
                        "所属概念": today_row.get('所属概念', '')
                    }
                )
                signals.append(signal)
        
        return signals
    
    def detect_second_board_dragon(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        二板定龙模式：昨日首板 + 今日高开3-7% + 快速涨停（15分钟内）
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 条件1: 昨日首板（涨停且前日未涨停）
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            if yest_change < 9.5:
                continue
            
            # 条件2: 今日高开3%-7%
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if not (9.5 <= today_change <= 11):  # 今日也是涨停
                continue
            
            # 获取开盘价计算高开幅度
            open_price = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            
            gap_ratio = (open_price - yest_close) / yest_close
            if not (0.03 <= gap_ratio <= 0.07):
                continue
            
            # 条件3: 快速涨停（15分钟内）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '09:45:00':  # 15分钟后涨停不算快速
                continue
            
            # 条件4: 昨日首板质量好（炸板次数=0）
            if yest_data.get('炸板次数', 0) > 0:
                continue
            
            signal = PatternSignal(
                pattern_type="二板定龙",
                stock_code=code,
                stock_name=name,
                confidence=0.88,
                description=f"首板硬逻辑+次日高开{gap_ratio*100:.1f}%+15分钟内涨停",
                key_metrics={
                    "昨日涨幅": f"{yest_change:.2f}%",
                    "今日高开": f"{gap_ratio*100:.1f}%",
                    "涨停时间": limit_up_time,
                    "昨日炸板": yest_data.get('炸板次数', 0)
                }
            )
            signals.append(signal)
        
        return signals
    
    def detect_first_board_breakout(self, today_df: pd.DataFrame) -> List[PatternSignal]:
        """
        首板突破模式：早盘秒封（9:40前）+ 放量 + 突破形态
        """
        signals = []
        
        if today_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 条件1: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 早盘秒封（9:40前）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '09:40:00':
                continue
            
            # 条件3: 封单强度（封单额/流通市值 > 5%）
            seal_amount = today_row.get('封单额', 0)
            float_cap = today_row.get('流通市值', 1) * 10000  # 万元转元
            seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
            
            if seal_ratio < 0.05:
                continue
            
            # 条件4: 换手率适中（5%-20%）
            turnover = today_row.get('换手率', 0)
            if not (5 <= turnover <= 20):
                continue
            
            signal = PatternSignal(
                pattern_type="首板突破",
                stock_code=code,
                stock_name=name,
                confidence=0.80,
                description=f"早盘秒封{limit_up_time}+封单强度{seal_ratio*100:.1f}%+换手{turnover:.1f}%",
                key_metrics={
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_ratio*100:.1f}%",
                    "换手率": f"{turnover:.1f}%",
                    "涨跌幅": f"{today_change:.2f}%"
                }
            )
            signals.append(signal)
        
        return signals
    
    def detect_divergence_to_consensus(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        分歧转一致模式：昨日烂板爆量 + 今日弱转强高开2-5% + 快速涨停
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 条件1: 昨日涨停但烂板
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            if yest_change < 9.5:
                continue
            
            # 昨日烂板判断
            yest_open_times = yest_data.get('炸板次数', 0)
            yest_last_time = str(yest_data.get('最后封板时间', '')).strip()
            if yest_last_time.isdigit():
                yest_last_time = yest_last_time.zfill(6)
            if len(yest_last_time) == 6:
                yest_last_time = f"{yest_last_time[:2]}:{yest_last_time[2:4]}:{yest_last_time[4:]}"
            
            if yest_open_times == 0 and yest_last_time <= '10:00:00':
                continue  # 昨日不是烂板
            
            # 条件2: 今日高开2%-5%
            open_price = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            
            gap_ratio = (open_price - yest_close) / yest_close
            if not (0.02 <= gap_ratio <= 0.05):
                continue
            
            # 条件3: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件4: 快速涨停（30分钟内）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '10:00:00':
                continue
            
            signal = PatternSignal(
                pattern_type="分歧转一致",
                stock_code=code,
                stock_name=name,
                confidence=0.82,
                description=f"昨日烂板后今日高开{gap_ratio*100:.1f}%转一致涨停",
                key_metrics={
                    "昨日炸板": yest_open_times,
                    "昨日最后封板": yest_last_time,
                    "今日高开": f"{gap_ratio*100:.1f}%",
                    "涨停时间": limit_up_time
                }
            )
            signals.append(signal)
        
        return signals
    
    def detect_position_battle(self, hierarchy_df: pd.DataFrame) -> List[PatternSignal]:
        """
        卡位板识别：同板块内，低位股抢先涨停
        """
        signals = []
        
        if hierarchy_df.empty:
            return signals
        
        # 按板块分组
        for sector_name, group in hierarchy_df.groupby('L3_Industry'):
            if len(group) < 2:  # 至少2只才能卡位
                continue
            
            # 按涨停时间排序
            group = group.copy()
            group['limit_up_time_sort'] = pd.to_datetime(group['LimitUpTime'], format='%H:%M:%S', errors='coerce')
            sorted_group = group.sort_values('limit_up_time_sort')
            
            if len(sorted_group) >= 2:
                first_limit = sorted_group.iloc[0]
                second_limit = sorted_group.iloc[1]
                
                # 第一个涨停时间早于第二个5分钟以上
                time_diff = (second_limit['limit_up_time_sort'] - first_limit['limit_up_time_sort']).total_seconds()
                if time_diff > 300:  # 5分钟
                    signal = PatternSignal(
                        pattern_type="卡位板",
                        stock_code=first_limit['Code'],
                        stock_name=first_limit['Name'],
                        confidence=0.75,
                        description=f"在{sector_name}板块中抢先{first_limit['LimitUpTime']}涨停，卡位成功",
                        key_metrics={
                            "涨停时间": first_limit['LimitUpTime'],
                            "板块内排名": 1,
                            "领先第二名": f"{int(time_diff)}秒",
                            "封板强度": "强" if first_limit['OpenTimes'] == 0 else "中"
                        }
                    )
                    signals.append(signal)
        
        return signals
    
    def scan_all_patterns(self, today_date: str, yesterday_date: str) -> Dict[str, List[PatternSignal]]:
        """
        扫描全市场所有模式
        """
        results = {
            "弱转强": [],
            "二板定龙": [],
            "首板突破": [],
            "分歧转一致": [],
            "卡位板": []
        }
        
        # 获取今日和昨日涨停池
        today_zt = self.dm.get_limit_up_pool(today_date)
        yesterday_zt = self.dm.get_limit_up_pool(yesterday_date)
        
        if today_zt.empty:
            logger.warning("今日涨停池为空，无法识别模式")
            return results
        
        logger.info(f"开始模式识别，今日涨停{len(today_zt)}只，昨日涨停{len(yesterday_zt)}只")
        
        # 1. 检测弱转强
        if not yesterday_zt.empty:
            results["弱转强"] = self.detect_weak_to_strong(today_zt, yesterday_zt)
            logger.info(f"  弱转强: {len(results['弱转强'])}个")
        
        # 2. 检测二板定龙
        if not yesterday_zt.empty:
            results["二板定龙"] = self.detect_second_board_dragon(today_zt, yesterday_zt)
            logger.info(f"  二板定龙: {len(results['二板定龙'])}个")
        
        # 3. 检测首板突破
        results["首板突破"] = self.detect_first_board_breakout(today_zt)
        logger.info(f"  首板突破: {len(results['首板突破'])}个")
        
        # 4. 检测分歧转一致
        if not yesterday_zt.empty:
            results["分歧转一致"] = self.detect_divergence_to_consensus(today_zt, yesterday_zt)
            logger.info(f"  分歧转一致: {len(results['分歧转一致'])}个")
        
        # 5. 检测卡位板
        from core.industry_mapper import IndustryMapper
        from config.settings import INDUSTRY_MAPPING_FILE
        mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
        hierarchy_df = mapper.build_hierarchy_dataframe(today_zt)
        results["卡位板"] = self.detect_position_battle(hierarchy_df)
        logger.info(f"  卡位板: {len(results['卡位板'])}个")
        
        total = sum(len(v) for v in results.values())
        logger.info(f"模式识别完成，共{total}个信号")
        
        return results

if __name__ == "__main__":
    print("模式识别模块初始化成功")
