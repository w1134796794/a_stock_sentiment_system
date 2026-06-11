"""
散户交易支持模块 V2 - 修复版
修复内容：
1. 隔夜决策清单为空问题 - 增加数据验证和备选方案
2. 板块关注逻辑错误 - 短期T+0视角替代20日滞后数据
3. 标的描述不精准 - 明确历史数据vs预判条件，增加执行细节

注：原"次日剧本推演（情景预测）"已移除——一切以真实数据说话，不做主观情景推演。

作者：量化交易系统
版本：2.0.0
日期：2026-04-06
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger


class RetailSuitability(Enum):
    """散户适配度等级"""
    EXCELLENT = "[5星]"  # 5星 - 闭眼参与
    GOOD = "[4星]"       # 4星 - 积极参与
    FAIR = "[3星]"       # 3星 - 谨慎参与
    POOR = "[2星]"       # 2星 - 回避
    AVOID = "[1星]"      # 1星 - 坚决回避


class SectorPriority(Enum):
    """板块优先级（基于T+0/T+1短期视角）"""
    S_TIER = "S级（超级主线）"   # 当日涨停>50家，3日增长>30%，梯队完整
    A_TIER = "A级（主线）"       # 当日涨停>20家，3日增长>15%，有连板梯队
    B_TIER = "B级（次主线）"     # 当日涨停>10家，3日增长>5%，跟风为主
    C_TIER = "C级（套利）"       # 当日涨停<10家，无梯队，首板套利
    D_TIER = "D级（回避）"       # 退潮中，当日涨停<3家或3日负增长


@dataclass
class OvernightDecision:
    """隔夜决策项 - 增强版"""
    stock_code: str
    stock_name: str
    current_board: int
    yesterday_board_type: str  # 昨日板型：秒板/烂板/尾盘板/T字板
    decision_type: str  # 最高标/2板梯队/1板套利/弱转强/分歧转一致
    
    # 历史数据（已发生）
    historical_data: Dict = field(default_factory=dict)
    # 例如：{'昨日涨停时间': '09:35', '昨日炸板次数': 2, '近5日溢价率': 8.5}
    
    # 次日预判条件（需满足）
    entry_conditions: List[str] = field(default_factory=list)
    # 例如：['竞价高开>3%', '竞价量>8%', '板块龙头未瀑布杀']
    
    # 取消条件（任一满足则放弃）
    cancel_conditions: List[str] = field(default_factory=list)
    # 例如：['低开', '竞价量<5%', '板块竞价封单<昨日50%']
    
    # 执行计划
    execution_plan: Dict = field(default_factory=dict)
    # 例如：{'买点': '竞价末段', '仓位': 'medium', '止损': '-7%', '止盈': '+15%'}
    
    retail_suitability: str = ""
    suggested_action: str = ""
    risk_level: str = "中"  # 低/中/高


@dataclass
class SectorAnalysis:
    """板块分析 - T+0短期视角"""
    sector_name: str
    priority: SectorPriority
    
    # T+0数据（当日）
    today_limit_up: int  # 当日涨停数
    today_blast_rate: float  # 当日炸板率
    today_leader_seal: float  # 龙头封单金额（亿）
    
    # 短期趋势（3日）
    growth_3d: float  # 3日涨停数增长率
    trend_3d: str  # 加速/减速/震荡
    
    # 梯队结构
    board_ladder: Dict[int, int]  # {连板高度: 股票数量}
    # 例如：{7: 1, 4: 2, 2: 5, 1: 88}
    
    # 20日数据（仅用于判断退潮）
    history_20d: int  # 20日涨停总数
    is_declining: bool  # 是否退潮（20日多但当日少）
    
    retail_suggestion: str  # 散户操作建议


class RetailTraderSupportV2:
    """
    散户交易支持系统 V2 - 修复版
    """
    
    def __init__(self, data_manager, repo=None):
        self.dm = data_manager
        if repo is None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(data_manager)
        self.repo = repo
        
        # 三阶过滤参数（放宽条件）
        self.filter_params = {
            "min_float_cap": 15,      # 降低至15亿（增加标的）
            "max_float_cap": 100,     # 放宽至100亿
            "exclude_st": True,
            "exclude_kcb": False,     # 允许科创板（增加标的）
            "max_limit_up_time": "10:30:00",  # 放宽至10:30
            "max_blast_times": 3,     # 放宽至3次
        }
    
    # ==================== 1. 隔夜决策清单（修复版）====================
    
    def generate_overnight_decisions_v2(self,
                                       today_zt_pool: pd.DataFrame,
                                       yesterday_zt_pool: pd.DataFrame,
                                       sector_analysis: Dict[str, SectorAnalysis]) -> List[OvernightDecision]:
        """
        生成隔夜决策清单 - 修复版
        
        Args:
            today_zt_pool: 当日涨停池（必须包含：代码、名称、连板高度、炸板次数、首次封板时间）
            yesterday_zt_pool: 昨日涨停池（同上）
            sector_analysis: 板块分析结果
        
        Returns:
            List[OvernightDecision]
        """
        decisions = []
        
        # 数据验证
        if today_zt_pool is None or today_zt_pool.empty:
            logger.warning("当日涨停池为空，尝试从data_manager获取")
            today_zt_pool = self.repo.get_limit_up_pool(datetime.now().strftime('%Y%m%d'))
        
        if today_zt_pool is None or today_zt_pool.empty:
            logger.error("无法获取当日涨停池，返回空决策清单")
            return []
        
        logger.info(f"当日涨停池：{len(today_zt_pool)}只股票")
        
        # 标准化列名（处理可能的中文/英文列名）
        today_zt_pool = self._standardize_columns(today_zt_pool)
        yesterday_zt_pool = self._standardize_columns(yesterday_zt_pool) if yesterday_zt_pool is not None else pd.DataFrame()
        
        # 1. 最高标决策
        highest_decision = self._create_highest_board_decision_v2(today_zt_pool, sector_analysis)
        if highest_decision:
            decisions.append(highest_decision)
        
        # 2. 2板梯队决策
        second_board_decisions = self._create_second_board_decisions_v2(today_zt_pool, yesterday_zt_pool, sector_analysis)
        decisions.extend(second_board_decisions)
        
        # 3. 1板套利决策（放宽条件）
        first_board_decisions = self._create_first_board_decisions_v2(today_zt_pool, yesterday_zt_pool, sector_analysis)
        decisions.extend(first_board_decisions)
        
        # 4. 弱转强标的（从昨日烂板中筛选）
        weak_to_strong_decisions = self._create_weak_to_strong_decisions_v2(today_zt_pool, yesterday_zt_pool)
        decisions.extend(weak_to_strong_decisions)
        
        logger.info(f"生成隔夜决策清单: {len(decisions)}项")
        for d in decisions:
            logger.info(f"  - {d.stock_name}({d.stock_code}): {d.decision_type}, "
                       f"适配度{d.retail_suitability}")
        
        return decisions
    
    def _standardize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化列名"""
        column_mapping = {
            '代码': 'code',
            '名称': 'name',
            '连板数': 'board_height',
            'BoardHeight': 'board_height',
            '炸板次数': 'blast_times',
            '首次封板时间': 'first_seal_time',
            '最后封板时间': 'last_seal_time',
            '所属行业': 'sector',
            'L2_Industry': 'sector',
            '换手率': 'turnover',
            '涨停价': 'limit_up_price',
            '封单金额': 'seal_amount'
        }
        
        df = df.copy()
        for old, new in column_mapping.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]
        
        return df
    
    def _create_highest_board_decision_v2(self, 
                                         today_df: pd.DataFrame,
                                         sector_analysis: Dict) -> Optional[OvernightDecision]:
        """创建最高标决策 - 修复版"""
        
        if 'board_height' not in today_df.columns:
            logger.error("涨停池缺少board_height列")
            return None
        
        # 获取最高标
        max_board = today_df['board_height'].max()
        if pd.isna(max_board) or max_board < 2:
            logger.info("无2板以上股票，跳过最高标决策")
            return None
        
        highest = today_df[today_df['board_height'] == max_board].iloc[0]
        
        code = highest.get('code', '')
        name = highest.get('name', '')
        blast_times = self._safe_int(highest.get('blast_times', 0))
        first_seal = highest.get('first_seal_time', '')
        sector = highest.get('sector', '')
        
        # 判断散户适配度
        if blast_times >= 3:
            suitability = RetailSuitability.AVOID.value
            action = "回避，股性恶劣"
            risk = "高"
        elif blast_times >= 1:
            suitability = RetailSuitability.POOR.value
            action = "谨慎，仅竞价确认"
            risk = "高"
        else:
            suitability = RetailSuitability.GOOD.value
            action = "可积极参与"
            risk = "中"
        
        # 获取板块信息
        sector_info = sector_analysis.get(sector, None)
        sector_trend = "强势" if sector_info and sector_info.growth_3d > 20 else "一般"
        
        return OvernightDecision(
            stock_code=code,
            stock_name=name,
            current_board=int(max_board),
            yesterday_board_type="秒板" if blast_times == 0 else f"烂板({blast_times}次)",
            decision_type="最高标",
            historical_data={
                '当前连板': int(max_board),
                '昨日炸板次数': blast_times,
                '昨日封板时间': str(first_seal),
                '所属板块': sector,
                '板块趋势': sector_trend
            },
            entry_conditions=[
                f"竞价高开>5%（当前{max_board}板，需强一致）",
                "竞价量>昨日成交量10%",
                f"{sector}板块龙头未瀑布杀",
                "封单金额>1亿"
            ],
            cancel_conditions=[
                "低开或平开（低于预期）",
                "竞价量<5%（无量上涨）",
                f"{sector}板块竞价封单<昨日50%",
                "开盘后快速跌破昨日涨停价"
            ],
            execution_plan={
                '买点': '竞价末段（9:24:50后）' if blast_times == 0 else '开盘第一笔（9:30:00）',
                '仓位': 'light' if max_board > 5 else 'medium',
                '止损': f"昨日涨停价或-7%",
                '止盈': f"+10%至+15%（{max_board+1}板预期）",
                '持有策略': '炸板不封死即减仓'
            },
            retail_suitability=suitability,
            suggested_action=action,
            risk_level=risk
        )
    
    def _create_second_board_decisions_v2(self,
                                         today_df: pd.DataFrame,
                                         yesterday_df: pd.DataFrame,
                                         sector_analysis: Dict) -> List[OvernightDecision]:
        """创建2板梯队决策 - 修复版"""
        decisions = []
        
        if yesterday_df.empty:
            logger.warning("昨日涨停池为空，无法识别2板")
            return decisions
        
        # 找出今日2板（今日涨停且昨日涨停且昨日1板）
        today_codes = set(today_df['code'].tolist())
        yesterday_codes = set(yesterday_df['code'].tolist())
        
        # 昨日1板的股票
        yesterday_1board = yesterday_df[yesterday_df['board_height'] == 1]['code'].tolist()
        
        # 今日2板 = 今日涨停 & 昨日1板
        second_board_codes = list(set(yesterday_1board) & today_codes)
        
        logger.info(f"识别到{len(second_board_codes)}只2板股票")
        
        for code in second_board_codes[:5]:  # 最多5只
            today_row = today_df[today_df['code'] == code].iloc[0]
            yesterday_row = yesterday_df[yesterday_df['code'] == code].iloc[0]
            
            name = today_row.get('name', '')
            blast_times = self._safe_int(today_row.get('blast_times', 0))
            first_seal = today_row.get('first_seal_time', '')
            sector = today_row.get('sector', '')
            
            # 判断板型
            if blast_times == 0:
                board_type = "秒板"
                suitability = RetailSuitability.EXCELLENT.value
                action = "优先参与"
            elif blast_times <= 2:
                board_type = f"T字板/烂板({blast_times}次)"
                suitability = RetailSuitability.GOOD.value
                action = "换手充分可参与"
            else:
                board_type = f"多次炸板({blast_times}次)"
                suitability = RetailSuitability.FAIR.value
                action = "谨慎，仅竞价超预期"
            
            # 获取板块强度
            sector_info = sector_analysis.get(sector, None)
            sector_strength = sector_info.today_limit_up if sector_info else 0
            
            decisions.append(OvernightDecision(
                stock_code=code,
                stock_name=name,
                current_board=2,
                yesterday_board_type=board_type,
                decision_type="2板梯队",
                historical_data={
                    '昨日板型': board_type,
                    '昨日封板时间': str(first_seal),
                    '所属板块': sector,
                    '板块当日涨停数': sector_strength
                },
                entry_conditions=[
                    "竞价高开>3%（超预期）",
                    "竞价量>昨日成交量8%",
                    f"{sector}板块有支撑（竞价至少1只一字或高开5%+）",
                    "开盘不回踩或回踩<1%"
                ],
                cancel_conditions=[
                    "竞价高开<2%（低于预期）",
                    "竞价量<5%",
                    f"同板块已有2只以上2板且封单更强",
                    "开盘后快速跌破开盘价"
                ],
                execution_plan={
                    '买点': '竞价末段（9:24:50）' if blast_times == 0 else '开盘第一笔',
                    '仓位': 'medium' if blast_times <= 1 else 'light',
                    '止损': '昨日涨停价或-5%',
                    '止盈': '+15%至+20%（3板预期）'
                },
                retail_suitability=suitability,
                suggested_action=action,
                risk_level="中"
            ))
        
        return decisions
    
    def _create_first_board_decisions_v2(self,
                                        today_df: pd.DataFrame,
                                        yesterday_df: pd.DataFrame,
                                        sector_analysis: Dict) -> List[OvernightDecision]:
        """创建1板套利决策 - 放宽条件"""
        decisions = []
        
        # 今日首板（今日涨停但昨日未涨停）
        if yesterday_df.empty:
            today_1board = today_df[today_df['board_height'] == 1]
        else:
            yesterday_codes = set(yesterday_df['code'].tolist())
            today_1board = today_df[
                (today_df['board_height'] == 1) & 
                (~today_df['code'].isin(yesterday_codes))
            ]
        
        logger.info(f"识别到{len(today_1board)}只首板股票")
        
        # 筛选条件放宽：早盘秒封（<9:40）或板块龙头（板块涨停数>10）
        for _, row in today_1board.iterrows():
            code = row.get('code', '')
            name = row.get('name', '')
            first_seal = row.get('first_seal_time', '')
            sector = row.get('sector', '')
            blast_times = self._safe_int(row.get('blast_times', 0))
            
            # 条件1：早盘秒封（<9:40）
            is_early = False
            if first_seal:
                try:
                    if isinstance(first_seal, str) and len(first_seal) >= 4:
                        hour_min = int(first_seal[:2]) * 100 + int(first_seal[3:5])
                        is_early = hour_min < 940
                except:
                    pass
            
            # 条件2：属于强势板块
            sector_info = sector_analysis.get(sector, None)
            is_strong_sector = sector_info and sector_info.today_limit_up >= 10
            
            # 条件3：非炸板
            is_clean = blast_times == 0
            
            # 满足任一条件即可
            if not (is_early or is_strong_sector or is_clean):
                continue
            
            suitability = RetailSuitability.GOOD.value if is_early else RetailSuitability.FAIR.value
            
            decisions.append(OvernightDecision(
                stock_code=code,
                stock_name=name,
                current_board=1,
                yesterday_board_type="首板",
                decision_type="1板套利",
                historical_data={
                    '封板时间': str(first_seal),
                    '是否秒封': is_early,
                    '所属板块': sector,
                    '板块强度': sector_info.today_limit_up if sector_info else 0
                },
                entry_conditions=[
                    f"次日竞价高开>2%（{'早盘秒封，预期高开' if is_early else '板块强势，有溢价'})",
                    "竞价量>昨日成交量5%",
                    f"{sector}板块龙头继续强势"
                ],
                cancel_conditions=[
                    "低开",
                    "板块龙头走弱",
                    "开盘后5分钟未翻红"
                ],
                execution_plan={
                    '买点': '竞价末段',
                    '仓位': 'light',
                    '止损': '-5%',
                    '止盈': '+10%'
                },
                retail_suitability=suitability,
                suggested_action="仅竞价介入，不追高" if not is_early else "可参与",
                risk_level="低"
            ))
            
            if len(decisions) >= 3:  # 最多3只
                break
        
        return decisions
    
    def _create_weak_to_strong_decisions_v2(self,
                                           today_df: pd.DataFrame,
                                           yesterday_df: pd.DataFrame) -> List[OvernightDecision]:
        """创建弱转强决策（基于昨日烂板）"""
        decisions = []
        
        if yesterday_df.empty:
            return decisions
        
        # 昨日烂板（炸板次数>=2）
        yesterday_blast = yesterday_df[yesterday_df['blast_times'] >= 2]
        
        for _, row in yesterday_blast.iterrows():
            code = row.get('code', '')
            name = row.get('name', '')
            blast_times = self._safe_int(row.get('blast_times', 0))
            
            # 检查今日是否涨停（已实现弱转强）或纳入观察
            if code in today_df['code'].values:
                # 今日已涨停，说明已实现弱转强
                continue
            
            decisions.append(OvernightDecision(
                stock_code=code,
                stock_name=name,
                current_board=0,  # 今日未涨停
                yesterday_board_type=f"烂板({blast_times}次)",
                decision_type="弱转强观察",
                historical_data={
                    '昨日炸板次数': blast_times,
                    '昨日最后封板时间': str(row.get('last_seal_time', ''))
                },
                entry_conditions=[
                    "次日竞价高开>2%（超预期）",
                    "竞价量>昨日成交量8%（资金抢筹）",
                    "开盘不回踩或快速翻红"
                ],
                cancel_conditions=[
                    "低开",
                    "开盘后10分钟未翻红",
                    "放量下跌"
                ],
                execution_plan={
                    '买点': '竞价末段或开盘第一笔',
                    '仓位': 'light',
                    '止损': '昨日最低价或-7%',
                    '止盈': '+15%'
                },
                retail_suitability=RetailSuitability.FAIR.value,
                suggested_action="仅超预期时参与",
                risk_level="高"
            ))
        
        return decisions[:2]  # 最多2只
    
    # ==================== 2. 板块分析（T+0短期视角）====================
    
    def analyze_sectors_v2(self,
                          today_zt_pool: pd.DataFrame,
                          history_pools: Dict[str, pd.DataFrame]) -> Dict[str, SectorAnalysis]:
        """
        板块分析 - 基于T+0/T+1短期视角
        
        Args:
            today_zt_pool: 当日涨停池
            history_pools: 历史涨停池（用于计算3日趋势）
        
        Returns:
            Dict[str, SectorAnalysis]
        """
        if today_zt_pool.empty:
            return {}
        
        # 标准化列名
        today_zt_pool = self._standardize_columns(today_zt_pool)
        
        # 标准化history_pools中的列名
        standardized_history_pools = {}
        for date, df in history_pools.items():
            if not df.empty:
                standardized_history_pools[date] = self._standardize_columns(df)
            else:
                standardized_history_pools[date] = df
        
        # 按板块统计
        sector_stats = {}
        
        for sector in today_zt_pool['sector'].unique():
            if pd.isna(sector) or sector == '其他':
                continue
            
            sector_df = today_zt_pool[today_zt_pool['sector'] == sector]
            
            # T+0数据
            today_count = len(sector_df)
            blast_rate = (sector_df['blast_times'] > 0).sum() / len(sector_df) * 100 if len(sector_df) > 0 else 0
            
            # 龙头封单（取最大封单金额）
            leader_seal = sector_df['seal_amount'].max() if 'seal_amount' in sector_df.columns else 0
            
            # 梯队结构
            ladder = {}
            if 'board_height' in sector_df.columns:
                for height, count in sector_df['board_height'].value_counts().items():
                    ladder[int(height)] = int(count)
            
            # 3日趋势
            growth_3d = self._calculate_3d_growth(sector, standardized_history_pools)
            
            # 20日数据（仅用于判断退潮）
            history_20d = 0
            for df in standardized_history_pools.values():
                if not df.empty and 'sector' in df.columns:
                    history_20d += len(df[df['sector'] == sector])
            is_declining = (history_20d > 50) and (today_count < 5)  # 历史多但当前少
            
            # 确定优先级
            if today_count >= 50 and growth_3d > 30 and len(ladder) >= 3:
                priority = SectorPriority.S_TIER
                suggestion = "超级主线，可参与2板换手标的"
            elif today_count >= 20 and growth_3d > 15 and len(ladder) >= 2:
                priority = SectorPriority.A_TIER
                suggestion = "主线明确，关注1板转2板"
            elif today_count >= 10 and growth_3d > 5:
                priority = SectorPriority.B_TIER
                suggestion = "次主线，仅首板套利"
            elif today_count >= 3:
                priority = SectorPriority.C_TIER
                suggestion = "套利板块，谨慎参与"
            else:
                priority = SectorPriority.D_TIER
                suggestion = "回避，无板块效应"
            
            sector_stats[sector] = SectorAnalysis(
                sector_name=sector,
                priority=priority,
                today_limit_up=today_count,
                today_blast_rate=blast_rate,
                today_leader_seal=leader_seal / 1e8 if leader_seal else 0,  # 亿
                growth_3d=growth_3d,
                trend_3d="加速" if growth_3d > 20 else "减速" if growth_3d < 0 else "震荡",
                board_ladder=ladder,
                history_20d=history_20d,
                is_declining=is_declining,
                retail_suggestion=suggestion
            )
        
        return sector_stats
    
    # ==================== 辅助方法 ====================
    
    def _safe_int(self, val, default=0):
        """安全转换为整数"""
        if val is None or pd.isna(val):
            return default
        try:
            return int(val)
        except:
            return default
    
    def _calculate_blast_rate(self, zt_pool: pd.DataFrame) -> float:
        """计算炸板率"""
        if zt_pool.empty or 'blast_times' not in zt_pool.columns:
            return 0.0
        return (zt_pool['blast_times'] > 0).sum() / len(zt_pool) * 100
    
    def _calculate_3d_growth(self, sector: str, history_pools: Dict) -> float:
        """计算3日增长率"""
        if not history_pools:
            return 0.0
        
        # 取最近3天
        dates = sorted(history_pools.keys())[-3:]
        counts = []
        
        for date in dates:
            df = history_pools[date]
            if not df.empty and 'sector' in df.columns:
                count = len(df[df['sector'] == sector])
                counts.append(count)
        
        if len(counts) >= 2 and counts[0] > 0:
            return (counts[-1] - counts[0]) / counts[0] * 100
        return 0.0
    
    # ==================== 报告生成（修复版）====================
    
    def generate_overnight_report_v2(self,
                                    decisions: List[OvernightDecision],
                                    indicators: Dict,
                                    sector_analysis: Dict[str, SectorAnalysis]) -> str:
        """生成隔夜决策报告 - 修复版"""
        report = []
        report.append("=" * 70)
        report.append("【次日开盘前决策清单 - V2修复版】")
        report.append("=" * 70)
        
        # 1. 散户特供指标
        report.append("\n📊 【市场情绪指标】")
        report.append("-" * 70)
        premium = indicators.get('昨日涨停溢价率', 0)
        one_word = indicators.get('一字板占比', 0)
        report.append(f"  昨日涨停溢价率: {premium:.1f}% {'(情绪高涨)' if premium > 5 else '(情绪一般)' if premium > 2 else '(情绪低迷)'}")
        report.append(f"  一字板占比: {one_word:.1f}% {'(一致性强)' if one_word > 20 else '(分歧较大)'}")
        report.append(f"  策略建议: {indicators.get('次日策略建议', '稳健')}")
        
        # 2. 板块优先级（T+0视角）
        report.append("\n🏆 【当前主线板块（T+0视角）】")
        report.append("-" * 70)
        
        sorted_sectors = sorted(sector_analysis.values(), 
                               key=lambda x: (x.today_limit_up, x.growth_3d), 
                               reverse=True)
        
        for i, sector in enumerate(sorted_sectors[:5], 1):
            ladder_str = "/".join([f"{h}板{c}只" for h, c in sorted(sector.board_ladder.items(), reverse=True)[:3]])
            trend_icon = "↑" if sector.growth_3d > 20 else "→" if sector.growth_3d > 0 else "↓"
            
            report.append(f"\n  {i}. {sector.sector_name} [{sector.priority.value}]")
            report.append(f"     当日涨停: {sector.today_limit_up}家 | 3日增长: {sector.growth_3d:+.0f}% {trend_icon}")
            report.append(f"     梯队: {ladder_str}")
            report.append(f"     建议: {sector.retail_suggestion}")
            
            if sector.is_declining:
                report.append(f"     [警告] 退潮预警: 20日涨停多但当前仅{sector.today_limit_up}家")
        
        # 3. 隔夜决策清单
        report.append("\n\n[清单] 【隔夜决策清单】")
        report.append("-" * 70)
        
        if not decisions:
            report.append("  暂无符合条件的标的")
        else:
            for i, d in enumerate(decisions, 1):
                report.append(f"\n  {i}. {d.stock_name}({d.stock_code}) - {d.decision_type}")
                report.append(f"     散户适配度: {d.retail_suitability} | 风险: {d.risk_level}")
                report.append(f"     昨日板型: {d.yesterday_board_type} | 当前{d.current_board}板")
                
                report.append(f"     历史数据:")
                for k, v in d.historical_data.items():
                    report.append(f"       • {k}: {v}")
                
                report.append(f"     次日介入条件:")
                for cond in d.entry_conditions:
                    report.append(f"       [OK] {cond}")
                
                report.append(f"     取消条件:")
                for cond in d.cancel_conditions:
                    report.append(f"       [NO] {cond}")
                
                if d.execution_plan:
                    report.append(f"     执行计划:")
                    for k, v in d.execution_plan.items():
                        report.append(f"       • {k}: {v}")
        
        report.append("\n" + "=" * 70)
        return "\n".join(report)


# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("=" * 70)
    print("散户交易支持系统 V2 - 修复版")
    print("=" * 70)
    print("\n修复内容：")
    print("1. 隔夜决策清单为空 → 增加数据验证和备选方案")
    print("2. 板块关注逻辑错误 → T+0短期视角替代20日滞后数据")
    print("3. 标的描述不精准 → 明确历史数据vs预判条件")
    print("\n核心改进：")
    print("• 板块优先级：S/A/B/C/D五级（基于当日涨停+3日趋势）")
    print("• 决策清单：每项包含历史数据+次日条件+执行计划+取消条件")
    print("=" * 70)