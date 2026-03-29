"""
散户交易支持模块 - 专为无法实时盯盘的散户设计
功能：
1. 隔夜预判 - 次日开盘前决策清单
2. 三阶过滤 - 生存/确定性/盈亏比过滤
3. 散户特供指标 - 昨日涨停溢价率、炸板股表现等
4. 板块优先级 - T0-T3分级
5. 散户适配度评分
6. 次日剧本推演
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger


class RetailSuitability(Enum):
    """散户适配度等级"""
    EXCELLENT = "⭐⭐⭐⭐"  # 非常适合
    GOOD = "⭐⭐⭐"       # 适合
    FAIR = "⭐⭐"         # 一般
    POOR = "⭐"           # 不适合
    AVOID = "❌"          # 回避


class SectorPriority(Enum):
    """板块优先级"""
    T0 = "T0级（闭眼跟）"
    T1 = "T1级（竞价确认）"
    T2 = "T2级（只看不买）"
    T3 = "T3级（回避）"


@dataclass
class OvernightDecision:
    """隔夜决策项"""
    stock_code: str
    stock_name: str
    current_board: int
    decision_type: str  # 最高标、2板梯队、1板套利
    conditions: List[str]  # 介入条件
    cancel_conditions: List[str]  # 取消条件
    retail_suitability: str
    suggested_action: str


@dataclass
class ScenarioForecast:
    """剧本推演"""
    scenario_name: str
    probability: float
    description: str
    retail_action: str
    key_stocks: List[str]


class RetailTraderSupport:
    """
    散户交易支持系统
    解决无法盯盘、信息过载、决策困难等问题
    """
    
    def __init__(self, data_manager):
        self.dm = data_manager
        
        # 三阶过滤参数
        self.filter_params = {
            # 第一阶：生存过滤
            "min_float_cap": 20,      # 最小流通市值（亿）
            "max_float_cap": 80,      # 最大流通市值（亿）
            "exclude_st": True,       # 排除ST
            "exclude_kcb": True,      # 排除科创板
            
            # 第二阶：确定性过滤
            "max_limit_up_time": "10:00:00",  # 最晚首封时间
            "max_blast_times": 1,     # 最大炸板次数
            "avoid_weak_boards": ["烂板", "尾盘板"],  # 回避的板型
            
            # 第三阶：盈亏比过滤
            "min_sector_limit_up": 2,  # 板块至少2只连板
            "need_sector_effect": True  # 需要板块效应
        }
    
    # ==================== 1. 隔夜预判模块 ====================
    
    def generate_overnight_decisions(self, 
                                     today_df: pd.DataFrame,
                                     yesterday_df: pd.DataFrame,
                                     patterns: Dict) -> List[OvernightDecision]:
        """
        生成次日开盘前决策清单
        
        Returns:
            List[OvernightDecision]: 决策清单
        """
        decisions = []
        
        # 1. 最高标决策
        highest_board = self._get_highest_board_stock(today_df)
        if highest_board is not None:
            decisions.append(self._create_highest_board_decision(highest_board))
        
        # 2. 2板梯队决策
        second_board_stocks = self._get_second_board_stocks(today_df, yesterday_df)
        for stock in second_board_stocks:
            decisions.append(self._create_second_board_decision(stock))
        
        # 3. 1板套利决策
        first_board_arbitrage = self._get_first_board_arbitrage(today_df, yesterday_df)
        for stock in first_board_arbitrage:
            decisions.append(self._create_first_board_decision(stock))
        
        logger.info(f"生成隔夜决策清单: {len(decisions)}项")
        return decisions
    
    def _get_highest_board_stock(self, today_df: pd.DataFrame) -> Optional[pd.Series]:
        """获取最高标股票"""
        if today_df.empty or 'BoardHeight' not in today_df.columns:
            return None
        max_board = today_df['BoardHeight'].max()
        highest = today_df[today_df['BoardHeight'] == max_board].iloc[0]
        return highest
    
    def _create_highest_board_decision(self, stock: pd.Series) -> OvernightDecision:
        """创建最高标决策"""
        code = stock.get('代码', '')
        name = stock.get('名称', '')
        board = stock.get('BoardHeight', 0)
        blast_times = stock.get('炸板次数', 0)
        
        # 判断散户适配度
        if blast_times >= 3:
            suitability = RetailSuitability.POOR.value
            action = "回避，股性恶劣"
        elif blast_times >= 1:
            suitability = RetailSuitability.FAIR.value
            action = "谨慎，仅竞价确认"
        else:
            suitability = RetailSuitability.GOOD.value
            action = "可参与"
        
        return OvernightDecision(
            stock_code=code,
            stock_name=name,
            current_board=board,
            decision_type="最高标",
            conditions=[
                f"若竞价涨幅>5%且封单>5万手 → 持仓/轻仓跟",
                f"若竞价涨幅>3%且封单>3万手 → 小仓位试"
            ],
            cancel_conditions=[
                f"若竞价涨幅<2%或封单<2万手 → 放弃，防止瀑布杀",
                f"若低开 → 直接放弃"
            ],
            retail_suitability=suitability,
            suggested_action=action
        )
    
    def _get_second_board_stocks(self, today_df: pd.DataFrame, 
                                  yesterday_df: pd.DataFrame) -> List[pd.Series]:
        """获取2板梯队股票"""
        if today_df.empty or yesterday_df.empty:
            return []
        
        # 今日2板 = 今日涨停且昨日涨停
        second_boards = []
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            if code in yesterday_df['代码'].values:
                # 检查昨日是否首板
                yest_row = yesterday_df[yesterday_df['代码'] == code]
                if not yest_row.empty:
                    yest_board = yest_row.iloc[0].get('BoardHeight', 0)
                    if yest_board == 1:
                        second_boards.append(today_row)
        
        return second_boards
    
    def _create_second_board_decision(self, stock: pd.Series) -> OvernightDecision:
        """创建2板梯队决策"""
        code = stock.get('代码', '')
        name = stock.get('名称', '')
        blast_times = stock.get('炸板次数', 0)
        if isinstance(blast_times, str):
            try:
                blast_times = int(blast_times)
            except:
                blast_times = 0
        limit_up_time = stock.get('首次封板时间', '')
        if isinstance(limit_up_time, (int, float)):
            limit_up_time = str(limit_up_time)
        
        # 根据炸板次数判断
        if blast_times == 0:
            suitability = RetailSuitability.GOOD.value
            action = "优先关注，非炸板标的"
            note = "股性好"
        elif blast_times <= 2:
            suitability = RetailSuitability.FAIR.value
            action = "谨慎关注"
            note = "炸板次数适中"
        else:
            suitability = RetailSuitability.AVOID.value
            action = "回避"
            note = f"多次炸板({blast_times}次)，股性恶劣"
        
        return OvernightDecision(
            stock_code=code,
            stock_name=name,
            current_board=2,
            decision_type="2板梯队",
            conditions=[
                f"{name} - {note}",
                "首封时间<10:00，封单稳定"
            ],
            cancel_conditions=[
                "竞价涨幅<2%",
                "同板块已有2只以上2板"
            ],
            retail_suitability=suitability,
            suggested_action=action
        )
    
    def _get_first_board_arbitrage(self, today_df: pd.DataFrame,
                                    yesterday_df: pd.DataFrame) -> List[pd.Series]:
        """获取1板套利标的"""
        if today_df.empty:
            return []
        
        arbitrage_stocks = []
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            # 昨日未涨停（首板）
            if yesterday_df.empty or code not in yesterday_df['代码'].values:
                blast_times = today_row.get('炸板次数', 0)
                if isinstance(blast_times, str):
                    try:
                        blast_times = int(blast_times)
                    except:
                        blast_times = 0
                
                limit_up_time = today_row.get('首次封板时间', '')
                if isinstance(limit_up_time, (int, float)):
                    limit_up_time = str(limit_up_time)
                
                # 条件：非炸板 + 早盘秒封
                if blast_times == 0 and limit_up_time and limit_up_time < "09:40:00":
                    arbitrage_stocks.append(today_row)
        
        # 最多取3只
        return arbitrage_stocks[:3]
    
    def _create_first_board_decision(self, stock: pd.Series) -> OvernightDecision:
        """创建1板套利决策"""
        code = stock.get('代码', '')
        name = stock.get('名称', '')
        concept = stock.get('Concept', '')
        
        return OvernightDecision(
            stock_code=code,
            stock_name=name,
            current_board=1,
            decision_type="1板套利",
            conditions=[
                "昨日首板+今日竞价放量高开3-5%",
                "同板块有连板龙头",
                f"板块: {concept}"
            ],
            cancel_conditions=[
                "竞价放量但低开",
                "板块龙头走弱"
            ],
            retail_suitability=RetailSuitability.FAIR.value,
            suggested_action="仅竞价介入，不追高"
        )
    
    # ==================== 2. 三阶过滤机制 ====================
    
    def apply_three_stage_filter(self, stocks_df: pd.DataFrame,
                                  sector_data: Dict) -> pd.DataFrame:
        """
        应用三阶过滤机制
        
        Returns:
            pd.DataFrame: 过滤后的股票，带过滤标记
        """
        if stocks_df.empty:
            return stocks_df
        
        filtered_stocks = stocks_df.copy()
        
        # 第一阶：生存过滤
        filtered_stocks = self._apply_survival_filter(filtered_stocks)
        
        # 第二阶：确定性过滤
        filtered_stocks = self._apply_certainty_filter(filtered_stocks)
        
        # 第三阶：盈亏比过滤
        filtered_stocks = self._apply_profit_loss_filter(filtered_stocks, sector_data)
        
        logger.info(f"三阶过滤完成: {len(stocks_df)}只 → {len(filtered_stocks)}只")
        return filtered_stocks
    
    def _apply_survival_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """第一阶：生存过滤"""
        df = df.copy()
        df['生存过滤'] = '通过'
        
        # 流通市值过滤
        if '流通市值' in df.columns:
            mask = (df['流通市值'] >= self.filter_params['min_float_cap']) & \
                   (df['流通市值'] <= self.filter_params['max_float_cap'])
            df.loc[~mask, '生存过滤'] = '流通市值不符'
        
        # ST过滤
        if self.filter_params['exclude_st'] and '名称' in df.columns:
            st_mask = df['名称'].str.contains('ST', na=False)
            df.loc[st_mask, '生存过滤'] = 'ST股'
        
        # 科创板过滤（688开头）
        if self.filter_params['exclude_kcb'] and '代码' in df.columns:
            kcb_mask = df['代码'].str.startswith('688', na=False)
            df.loc[kcb_mask, '生存过滤'] = '科创板'
        
        return df[df['生存过滤'] == '通过']
    
    def _apply_certainty_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """第二阶：确定性过滤"""
        df = df.copy()
        df['确定性过滤'] = '通过'
        
        # 首封时间过滤
        if '首次封板时间' in df.columns:
            late_mask = df['首次封板时间'] > self.filter_params['max_limit_up_time']
            df.loc[late_mask, '确定性过滤'] = '封板太晚'
        
        # 炸板次数过滤
        if '炸板次数' in df.columns:
            blast_mask = df['炸板次数'] > self.filter_params['max_blast_times']
            df.loc[blast_mask, '确定性过滤'] = f"炸板>{self.filter_params['max_blast_times']}次"
        
        return df[df['确定性过滤'] == '通过']
    
    def _apply_profit_loss_filter(self, df: pd.DataFrame,
                                   sector_data: Dict) -> pd.DataFrame:
        """第三阶：盈亏比过滤"""
        df = df.copy()
        df['盈亏比过滤'] = '通过'
        
        # 这里简化处理，实际需要板块数据
        # 检查板块是否有连板龙头
        
        return df
    
    # ==================== 3. 散户特供指标 ====================
    
    def calculate_retail_indicators(self, today_date: str,
                                     yesterday_date: str) -> Dict:
        """
        计算散户特供指标
        
        Returns:
            Dict: {
                '昨日涨停溢价率': float,
                '炸板股次日表现': float,
                '一字板占比': float,
                '散户可用标的数': int,
                '次日策略建议': str
            }
        """
        indicators = {}
        
        # 1. 昨日涨停溢价率
        indicators['昨日涨停溢价率'] = self._calc_yesterday_limit_up_premium(
            today_date, yesterday_date
        )
        
        # 2. 炸板股次日表现
        indicators['炸板股次日表现'] = self._calc_blast_stock_performance(
            today_date, yesterday_date
        )
        
        # 3. 一字板占比
        indicators['一字板占比'] = self._calc_one_word_ratio(today_date)
        
        # 4. 策略建议
        indicators['次日策略建议'] = self._generate_strategy_advice(indicators)
        
        logger.info(f"散户指标计算完成: {indicators}")
        return indicators
    
    def _calc_yesterday_limit_up_premium(self, today_date: str,
                                          yesterday_date: str) -> float:
        """计算昨日涨停溢价率"""
        try:
            yesterday_zt = self.dm.get_limit_up_pool(yesterday_date)
            if yesterday_zt.empty:
                return 0.0
            
            # 获取今日涨停池数据（包含今日开盘价信息）
            today_zt = self.dm.get_limit_up_pool(today_date)
            if today_zt.empty:
                return 0.0
            
            premiums = []
            for _, yest_row in yesterday_zt.iterrows():
                code = yest_row.get('代码', '')
                # 在今日涨停池中查找该股票
                today_row = today_zt[today_zt['代码'] == code]
                if not today_row.empty:
                    # 使用最新价作为参考（涨停价或接近涨停价）
                    # 简化计算：假设昨日涨停今日继续涨停，溢价约10%
                    # 实际应该从历史数据获取开盘价
                    premiums.append(0.10)  # 默认10%溢价
            
            return np.mean(premiums) * 100 if premiums else 0.0
            
        except Exception as e:
            logger.warning(f"计算昨日涨停溢价率失败: {e}")
            return 0.0
    
    def _calc_blast_stock_performance(self, today_date: str,
                                       yesterday_date: str) -> float:
        """计算炸板股次日表现"""
        # 简化实现，实际需要获取昨日炸板股数据
        return -2.5  # 默认假设
    
    def _calc_one_word_ratio(self, today_date: str) -> float:
        """计算一字板占比"""
        try:
            today_zt = self.dm.get_limit_up_pool(today_date)
            if today_zt.empty:
                return 0.0
            
            # 一字板：开盘即涨停
            if '首次封板时间' in today_zt.columns:
                # 处理时间格式
                def is_one_word(time_val):
                    if pd.isna(time_val):
                        return False
                    if isinstance(time_val, str):
                        return time_val < '09:30:00'
                    return False
                
                one_word = today_zt[today_zt['首次封板时间'].apply(is_one_word)]
                return len(one_word) / len(today_zt) * 100
            
            return 0.0
            
        except Exception as e:
            logger.warning(f"计算一字板占比失败: {e}")
            return 0.0
    
    def _generate_strategy_advice(self, indicators: Dict) -> str:
        """生成策略建议"""
        premium = indicators.get('昨日涨停溢价率', 0)
        blast_perf = indicators.get('炸板股次日表现', 0)
        one_word = indicators.get('一字板占比', 0)
        
        # 综合判断
        if premium < 2 or blast_perf < -5:
            return "防守"
        elif premium > 5 and one_word < 20:
            return "激进"
        else:
            return "稳健"
    
    # ==================== 4. 板块优先级重构 ====================
    
    def classify_sector_priority(self, sector_name: str,
                                  sector_stats: Dict,
                                  leading_stocks: List[Dict]) -> SectorPriority:
        """
        对板块进行T0-T3优先级分类
        
        Args:
            sector_name: 板块名称
            sector_stats: 板块统计数据
            leading_stocks: 龙头股列表
            
        Returns:
            SectorPriority: 优先级
        """
        # T0级条件：有高度龙+板块效应+防御属性
        if self._is_t0_sector(sector_stats, leading_stocks):
            return SectorPriority.T0
        
        # T1级条件：有连板+板块容量适中
        if self._is_t1_sector(sector_stats, leading_stocks):
            return SectorPriority.T1
        
        # T2级条件：高度够但跟风少
        if self._is_t2_sector(sector_stats, leading_stocks):
            return SectorPriority.T2
        
        # T3级：纯首板套利
        return SectorPriority.T3
    
    def _is_t0_sector(self, stats: Dict, leaders: List[Dict]) -> bool:
        """判断是否T0级板块"""
        # 有4板以上龙头
        has_high_leader = any(s.get('board_height', 0) >= 4 for s in leaders)
        # 板块有5只以上涨停
        has_sector_effect = stats.get('limit_up_count', 0) >= 5
        # 防御属性（医药、消费等）
        is_defensive = stats.get('is_defensive', False)
        
        return has_high_leader and has_sector_effect and is_defensive
    
    def _is_t1_sector(self, stats: Dict, leaders: List[Dict]) -> bool:
        """判断是否T1级板块"""
        has_leader = any(s.get('board_height', 0) >= 2 for s in leaders)
        moderate_capacity = 3 <= stats.get('limit_up_count', 0) <= 8
        return has_leader and moderate_capacity
    
    def _is_t2_sector(self, stats: Dict, leaders: List[Dict]) -> bool:
        """判断是否T2级板块"""
        has_height = any(s.get('board_height', 0) >= 3 for s in leaders)
        few_followers = stats.get('limit_up_count', 0) <= 3
        return has_height and few_followers
    
    # ==================== 5. 散户适配度评分 ====================
    
    def calculate_retail_suitability_score(self, stock: pd.Series,
                                            sector_context: Dict) -> Tuple[str, str, str]:
        """
        计算散户适配度评分
        
        Returns:
            Tuple[星级, 关键理由, 建议操作]
        """
        score = 0
        reasons = []
        
        # 1. 炸板次数评分
        blast_times = stock.get('炸板次数', 0)
        if isinstance(blast_times, str):
            try:
                blast_times = int(blast_times)
            except:
                blast_times = 0
        if blast_times == 0:
            score += 2
            reasons.append("非炸板")
        elif blast_times <= 2:
            score += 1
            reasons.append(f"炸板{blast_times}次可接受")
        else:
            score -= 2
            reasons.append(f"炸板{blast_times}次，股性恶劣")
        
        # 2. 封板时间评分
        limit_up_time = stock.get('首次封板时间', '')
        if isinstance(limit_up_time, (int, float)):
            limit_up_time = str(limit_up_time)
        if limit_up_time and limit_up_time < '09:40:00':
            score += 2
            reasons.append("早盘秒封")
        elif limit_up_time and limit_up_time < '10:00:00':
            score += 1
            reasons.append("上午封板")
        else:
            score -= 1
            reasons.append("封板较晚")
        
        # 3. 板块支撑评分
        if sector_context.get('has_leader', False):
            score += 1
            reasons.append("有板块支撑")
        
        # 4. 高度评分（避免太高）
        board_height = stock.get('BoardHeight', 0)
        if 2 <= board_height <= 4:
            score += 1
            reasons.append("高度适中")
        elif board_height > 5:
            score -= 1
            reasons.append("高度太高风险大")
        
        # 确定星级
        if score >= 4:
            stars = RetailSuitability.EXCELLENT.value
            action = "可积极参与"
        elif score >= 2:
            stars = RetailSuitability.GOOD.value
            action = "竞价确认后可参与"
        elif score >= 0:
            stars = RetailSuitability.FAIR.value
            action = "谨慎，小仓位试"
        else:
            stars = RetailSuitability.POOR.value
            action = "回避"
        
        return stars, "；".join(reasons), action
    
    # ==================== 6. 次日剧本推演 ====================
    
    def forecast_next_day_scenarios(self, today_data: Dict) -> List[ScenarioForecast]:
        """
        推演次日可能的市场剧本
        
        Returns:
            List[ScenarioForecast]: 剧本列表
        """
        scenarios = []
        
        # 获取当前市场情绪
        sentiment = today_data.get('sentiment', {})
        temperature = sentiment.get('temperature', '')
        
        # 获取主线板块
        main_sectors = today_data.get('mainline_df', pd.DataFrame())
        
        # 剧本A：主线分化，资金挖掘低位补涨
        if not main_sectors.empty:
            top_sector = main_sectors.iloc[0]['L2_Industry'] if 'L2_Industry' in main_sectors.columns else ""
            scenarios.append(ScenarioForecast(
                scenario_name="剧本A：主线分化，资金挖掘低位",
                probability=0.40,
                description=f"{top_sector}分化，龙头分歧转一致，资金挖掘低位的补涨",
                retail_action="持有龙头，或9:35前介入低位补涨首板",
                key_stocks=["首板补涨标的"]
            ))
        
        # 剧本B：主线回流，板块强化
        scenarios.append(ScenarioForecast(
            scenario_name="剧本B：主线回流，板块强化",
            probability=0.35,
            description="主线回流，龙头继续连板带动板块",
            retail_action="放弃，位置太高，风险收益比差",
            key_stocks=["连板龙头"]
        ))
        
        # 剧本C：情绪退潮
        if '冰点' in temperature or '退潮' in temperature:
            prob = 0.35
        else:
            prob = 0.25
        
        scenarios.append(ScenarioForecast(
            scenario_name="剧本C：情绪退潮",
            probability=prob,
            description="情绪退潮，昨日涨停普遍低开",
            retail_action="空仓，或1成仓试错1板（仅竞价）",
            key_stocks=[]
        ))
        
        # 按概率排序
        scenarios.sort(key=lambda x: x.probability, reverse=True)
        
        return scenarios
    
    # ==================== 7. 炸板回封重新分级 ====================
    
    def classify_blast_reseal(self, stock: pd.Series) -> Dict:
        """
        对炸板回封进行A/B/C分级
        
        Returns:
            Dict: {分级, 条件, 散户策略}
        """
        blast_times = stock.get('炸板次数', 0)
        turnover = stock.get('换手率', 0)
        is_sector_leader = stock.get('is_sector_leader', False)
        
        if blast_times == 1 and turnover < 15 and is_sector_leader:
            return {
                '分级': 'A级',
                '条件': '炸板1次+换手<15%+板块龙头',
                '散户策略': '可参与'
            }
        elif blast_times <= 3 and 15 <= turnover <= 25:
            return {
                '分级': 'B级',
                '条件': '炸板2-3次+换手15-25%',
                '散户策略': '谨慎，竞价确认'
            }
        else:
            return {
                '分级': 'C级',
                '条件': f'炸板≥{blast_times}次或换手>{turnover}%',
                '散户策略': '回避，股性恶劣'
            }
    
    # ==================== 报告生成 ====================
    
    def _format_indicator(self, val, decimal_places=2):
        """格式化指标值，处理numpy类型"""
        if val is None:
            return 0.0
        if isinstance(val, (np.integer, np.int64, np.int32)):
            return int(val)
        if isinstance(val, (np.floating, np.float64, np.float32)):
            return round(float(val), decimal_places)
        if isinstance(val, (int, float)):
            if isinstance(val, float):
                return round(val, decimal_places)
            return val
        return val
    
    def generate_overnight_report(self, decisions: List[OvernightDecision],
                                   indicators: Dict,
                                   scenarios: List[ScenarioForecast]) -> str:
        """生成隔夜决策报告"""
        report = []
        report.append("=" * 60)
        report.append("【次日开盘前决策清单】")
        report.append("=" * 60)
        
        # 散户指标 - 格式化数值
        premium_rate = self._format_indicator(indicators.get('昨日涨停溢价率', 0), 2)
        one_word_ratio = self._format_indicator(indicators.get('一字板占比', 0), 1)
        
        report.append("\n📊 散户特供指标:")
        report.append(f"  昨日涨停溢价率: {premium_rate}%")
        report.append(f"  一字板占比: {one_word_ratio}%")
        report.append(f"  次日策略建议: {indicators.get('次日策略建议', '稳健')}")
        
        # 决策清单
        report.append("\n📝 决策清单:")
        for decision in decisions:
            report.append(f"\n□ {decision.stock_name}({decision.stock_code}) - {decision.decision_type}")
            report.append(f"  散户适配度: {decision.retail_suitability}")
            report.append(f"  建议: {decision.suggested_action}")
            for cond in decision.conditions:
                report.append(f"    ✓ {cond}")
            for cancel in decision.cancel_conditions:
                report.append(f"    ✗ {cancel}")
        
        # 剧本推演
        report.append("\n\n🔮 次日剧本推演:")
        for i, scenario in enumerate(scenarios[:3], 1):
            report.append(f"\n  {i}. {scenario.scenario_name} (概率{scenario.probability:.0%})")
            report.append(f"     描述: {scenario.description}")
            report.append(f"     散户动作: {scenario.retail_action}")
        
        report.append("\n" + "=" * 60)
        return "\n".join(report)


if __name__ == "__main__":
    print("散户交易支持模块加载完成")
    print("功能:")
    print("  1. 隔夜预判 - 次日开盘前决策清单")
    print("  2. 三阶过滤 - 生存/确定性/盈亏比")
    print("  3. 散户特供指标 - 溢价率、炸板表现等")
    print("  4. 板块优先级 - T0-T3分级")
    print("  5. 散户适配度评分")
    print("  6. 次日剧本推演")
    print("  7. 炸板回封分级")
