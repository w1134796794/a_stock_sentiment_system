
"""
多维度板块热度计算器 V3 - 领先预警版 (完整实现)
核心改进：从"涨停后识别"升级为"涨停前预警"
作者：量化交易系统
版本：3.0.0
日期：2026-04-02

核心特性：
1. T-24h 资金前置预警 (龙虎榜+北向+融资)
2. T-2h 情绪先行预警 (舆情+搜索+热股)  
3. T-15min 竞价监控预警 (高开率+量能+隔夜单)
4. 产业链传导预判 (期货+海外映射)
5. 重构权重体系：当日40% + 领先指标40% + 历史20%
6. 新增"预启动"阶段 (最佳买点识别)
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum, auto
from datetime import datetime, timedelta
from collections import defaultdict
import json
import os
import tushare as ts
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# ========== 配置层 ==========

@dataclass
class V3Config:
    """V3版本配置参数"""
    # 权重配置 (当日+领先指标占80%)
    WEIGHTS = {
        'today_zt': 0.40,           # 当日涨停确认
        'capital_leading': 0.25,    # 资金前置预警  
        'sentiment_leading': 0.20,  # 情绪先行预警
        'auction_signal': 0.15,     # 竞价监控预警
        'momentum_3d': 0.05,        # 3日动量 (权重降低)
        'momentum_5d': 0.03,        # 5日动量
        'momentum_20d': 0.02        # 20日趋势 (几乎忽略)
    }
    
    # 资金前置指标阈值
    CAPITAL_THRESHOLDS = {
        'dragon_net_buy_strong': 5.0,      # 龙虎榜净买入(亿)
        'dragon_count_strong': 5,          # 上榜家数
        'northbound_strong': 3.0,          # 北向流入(亿)
        'margin_change_strong': 0.10,      # 融资余额变化率
        'block_trade_premium': 0.05        # 大宗交易溢价率
    }
    
    # 情绪先行指标阈值
    SENTIMENT_THRESHOLDS = {
        'hot_rank_jump': 20,               # 热股榜排名提升位数
        'search_index_surge': 2.0,         # 搜索指数增长率(200%)
        'news_sentiment_positive': 0.7,    # 新闻情绪正面阈值
        'block_order_strong': 2.0          # 板块封单金额(亿)
    }
    
    # 竞价预警阈值
    AUCTION_THRESHOLDS = {
        'high_open_rate': 0.60,            # 板块高开率
        'volume_ratio_surge': 3.0,         # 竞价量能比
        'block_order_amount': 1.0,         # 隔夜单金额(亿)
        'net_inflow_min': 0.5              # 竞价净流入(亿)
    }
    
    # 趋势判断阈值
    TREND_THRESHOLDS = {
        'pre_wakeup_capital': 70,          # 预启动资金得分
        'pre_wakeup_sentiment': 70,        # 预启动情绪得分
        'explosion_ratio': 1.5,            # 爆发倍数
        'explosion_min_today': 2,          # 爆发期最小涨停数
        'decline_momentum': -0.20          # 退潮动量阈值
    }


# ========== 数据模型层 ==========

class TrendStageV3(Enum):
    """V3趋势阶段枚举"""
    PRE_WAKEUP = "预启动"       # 新增：资金/情绪已动，股价未动 (最佳买点)
    START = "启动期"            # 首板出现，趋势确立
    EXPLOSION = "爆发期"        # 涨停倍增，板块效应
    ACCELERATION = "加速期"     # 持续加速，趋势强化
    CONFIRMED = "确认期"        # 多周期共振，主线确立
    MATURE = "成熟期"           # 高位震荡，风险累积
    DECLINE_EARLY = "早期退潮"  # 3日骤降，敏感撤退
    DECLINE_LATE = "晚期退潮"   # 确认退潮，坚决回避
    WATCH = "观察期"            # 无明确趋势


class ResonanceType(Enum):
    """共振类型枚举"""
    STRONG = "强共振"           # 涨停多+板块涨幅大+大票动
    QUANTITY_LEADS = "数量引领" # 涨停多+板块涨幅小
    PRICE_LEADS = "价格引领"    # 涨停少+板块涨幅大
    WEAK = "弱共振"             # 涨停少+板块涨幅小
    NONE = "无共振"
    ANY = "任意共振"            # 用于决策矩阵匹配任意共振类型


@dataclass
class LeadingIndicatorScore:
    """领先指标得分数据类"""
    total_score: float                      # 总分(0-100)
    components: Dict[str, float] = field(default_factory=dict)  # 分项得分
    signal_level: str = "观望"              # 信号级别：强烈预警/关注/观望
    details: List[str] = field(default_factory=list)  # 详细说明
    timestamp: Optional[datetime] = None    # 时间戳


@dataclass
class SectorSignalV3:
    """V3板块信号数据类 - 散户可直接使用的行动建议"""
    # 基础信息
    l2_name: str                            # 二级行业名称
    l1_name: str                            # 一级行业名称
    
    # 信号核心
    trend_stage: TrendStageV3               # 趋势阶段
    resonance_type: ResonanceType           # 共振类型
    combined_signal: str                    # 联动信号名称
    
    # 行动建议
    action: str                             # 具体行动建议
    priority: int                           # 优先级(1-5，1最高)
    position_size: str                      # 仓位建议：heavy/medium/light/none
    confidence: float                       # 置信度(0-1)
    
    # 领先指标详情
    capital_score: Optional[LeadingIndicatorScore] = None      # 资金前置得分
    sentiment_score: Optional[LeadingIndicatorScore] = None    # 情绪先行得分
    auction_score: Optional[LeadingIndicatorScore] = None      # 竞价预警得分
    chain_signals: List[Dict] = field(default_factory=list)    # 产业链信号
    
    # 核心指标
    key_metrics: Dict = field(default_factory=dict)
    watch_reason: str = ""                  # 关注理由
    risk_warning: str = ""                  # 风险提示
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)


# ========== 领先指标计算层 ==========

class CapitalLeadingIndicator:
    """
    资金前置指标计算器 (T-24h预警)
    基于龙虎榜、北向资金、融资盘、大宗交易的提前预警
    """
    
    def __init__(self, config: V3Config = None):
        self.config = config or V3Config()
        self.thresholds = self.config.CAPITAL_THRESHOLDS
        
    def calculate(self, data: Dict) -> LeadingIndicatorScore:
        """
        计算资金前置得分
        
        Args:
            data: {
                'dragon_tiger': {
                    'net_buy': 5.2,          # 机构净买入(亿)
                    'count': 8,              # 上榜家数
                    'stocks': ['股1', '股2'] # 具体股票
                },
                'northbound': {
                    'net_flow': 3.8,         # 净流入(亿)
                    'leader': '隆基绿能'      # 领涨股
                },
                'margin': {
                    'balance_change': 0.12   # 融资余额变化率(12%)
                },
                'block_trade': {
                    'premium_rate': 0.03     # 大宗交易溢价率
                }
            }
        
        Returns:
            LeadingIndicatorScore对象
        """
        scores = {}
        details = []
        
        # 1. 龙虎榜机构净买入评分 (权重35%)
        if 'dragon_tiger' in data:
            dt = data['dragon_tiger']
            net_buy = dt.get('net_buy', 0)
            count = dt.get('count', 0)
            
            # 评分逻辑：净买入5亿=50分，每增加1亿加10分；上榜家数5家=50分
            score_buy = min(50, (net_buy / self.thresholds['dragon_net_buy_strong']) * 50)
            score_count = min(50, (count / self.thresholds['dragon_count_strong']) * 50)
            scores['dragon_tiger'] = score_buy + score_count
            
            if net_buy >= self.thresholds['dragon_net_buy_strong']:
                details.append(f"龙虎榜机构净买入{net_buy:.1f}亿，强势抢筹")
            elif net_buy >= self.thresholds['dragon_net_buy_strong'] * 0.5:
                details.append(f"龙虎榜机构净买入{net_buy:.1f}亿，积极布局")
        else:
            scores['dragon_tiger'] = 0
            
        # 2. 北向资金流向评分 (权重30%)
        if 'northbound' in data:
            nb = data['northbound']
            flow = nb.get('net_flow', 0)
            
            # 评分逻辑：3亿=100分
            scores['northbound'] = min(100, (flow / self.thresholds['northbound_strong']) * 100)
            
            if flow >= self.thresholds['northbound_strong']:
                details.append(f"北向资金净流入{flow:.1f}亿，外资看好")
            elif flow >= self.thresholds['northbound_strong'] * 0.5:
                details.append(f"北向资金净流入{flow:.1f}亿，外资流入")
        else:
            scores['northbound'] = 0
            
        # 3. 融资余额变化评分 (权重20%)
        if 'margin' in data:
            mg = data['margin']
            change = mg.get('balance_change', 0)
            
            # 评分逻辑：变化率10%=100分
            scores['margin'] = min(100, (change / self.thresholds['margin_change_strong']) * 100)
            
            if change >= self.thresholds['margin_change_strong']:
                details.append(f"融资余额暴增{change:.1%}，杠杆资金进场")
            elif change >= self.thresholds['margin_change_strong'] * 0.5:
                details.append(f"融资余额增加{change:.1%}，杠杆资金加仓")
        else:
            scores['margin'] = 0
            
        # 4. 大宗交易溢价评分 (权重15%)
        if 'block_trade' in data:
            bt = data['block_trade']
            premium = bt.get('premium_rate', 0)
            
            # 评分逻辑：溢价率5%=100分
            scores['block_trade'] = min(100, (premium / self.thresholds['block_trade_premium']) * 100)
            
            if premium >= self.thresholds['block_trade_premium']:
                details.append(f"大宗交易溢价{premium:.1%}，机构溢价抢筹")
        else:
            scores['block_trade'] = 0
        
        # 计算加权总分
        weights = {'dragon_tiger': 0.35, 'northbound': 0.30, 'margin': 0.20, 'block_trade': 0.15}
        total_score = sum(scores.get(k, 0) * weights.get(k, 0) for k in weights)
        
        # 确定信号级别
        if total_score >= 70:
            signal_level = "强烈预警"
        elif total_score >= 50:
            signal_level = "关注"
        else:
            signal_level = "观望"
            
        return LeadingIndicatorScore(
            total_score=total_score,
            components=scores,
            signal_level=signal_level,
            details=details,
            timestamp=datetime.now()
        )


class SentimentLeadingIndicator:
    """
    情绪先行指标计算器 (T-2h~T-6h预警)
    基于舆情热度、搜索量、热股榜、封单金额的提前预警
    """
    
    def __init__(self, config: V3Config = None):
        self.config = config or V3Config()
        self.thresholds = self.config.SENTIMENT_THRESHOLDS
        
    def calculate(self, data: Dict) -> LeadingIndicatorScore:
        """
        计算情绪先行得分
        
        Args:
            data: {
                'hot_rank': {
                    'current': 5,            # 当前排名
                    'previous': 25,          # 之前排名
                    'change': -20            # 排名变化(负数为上升)
                },
                'search_index': {
                    'current': 15000,        # 当前搜索量
                    'previous': 5000,        # 之前搜索量
                    'change_rate': 2.0       # 变化率(200%)
                },
                'news_sentiment': 0.8,       # 新闻情绪得分(0-1)
                'block_orders': 2.5e8        # 板块涨停封单金额(元)
            }
        """
        scores = {}
        details = []
        
        # 1. 热股榜排名变化评分 (权重30%)
        if 'hot_rank' in data:
            hr = data['hot_rank']
            rank_change = abs(hr.get('change', 0))
            current = hr.get('current', 999)
            
            # 评分逻辑：排名上升20位=60分，进入前10名额外加40分
            score_change = min(60, (rank_change / self.thresholds['hot_rank_jump']) * 60)
            score_top = 40 if current <= 10 else 0
            scores['hot_rank'] = score_change + score_top
            
            if rank_change >= self.thresholds['hot_rank_jump'] and current <= 10:
                details.append(f"热股榜排名飙升{rank_change}位至第{current}名，关注度爆棚")
            elif rank_change >= self.thresholds['hot_rank_jump']:
                details.append(f"热股榜排名上升{rank_change}位，关注度快速提升")
        else:
            scores['hot_rank'] = 0
            
        # 2. 搜索指数变化评分 (权重25%)
        if 'search_index' in data:
            si = data['search_index']
            change_rate = si.get('change_rate', 0)
            
            # 评分逻辑：增长率200%=100分
            scores['search_index'] = min(100, (change_rate / self.thresholds['search_index_surge']) * 100)
            
            if change_rate >= self.thresholds['search_index_surge']:
                details.append(f"搜索指数暴增{change_rate:.0%}，散户情绪高涨")
            elif change_rate >= self.thresholds['search_index_surge'] * 0.5:
                details.append(f"搜索指数增长{change_rate:.0%}，关注度提升")
        else:
            scores['search_index'] = 0
            
        # 3. 新闻情绪评分 (权重25%)
        if 'news_sentiment' in data:
            sentiment = data['news_sentiment']
            
            # 评分逻辑：情绪得分直接作为分数(0-1映射到0-100)
            scores['news_sentiment'] = sentiment * 100
            
            if sentiment >= self.thresholds['news_sentiment_positive']:
                details.append(f"新闻情绪极度正面({sentiment:.0%})，媒体集中报道")
            elif sentiment >= 0.6:
                details.append(f"新闻情绪正面({sentiment:.0%})，报道偏多")
        else:
            scores['news_sentiment'] = 50  # 中性
            
        # 4. 封单金额评分 (权重20%)
        if 'block_orders' in data:
            block = data['block_orders']
            
            # 评分逻辑：2亿=100分
            scores['block_orders'] = min(100, (block / 1e8 / self.thresholds['block_order_strong']) * 100)
            
            if block >= self.thresholds['block_order_strong'] * 1e8:
                details.append(f"板块封单{block/1e8:.1f}亿，资金决心强烈")
            elif block >= self.thresholds['block_order_strong'] * 0.5 * 1e8:
                details.append(f"板块封单{block/1e8:.1f}亿，资金态度坚决")
        else:
            scores['block_orders'] = 0
        
        # 计算加权总分
        weights = {'hot_rank': 0.30, 'search_index': 0.25, 'news_sentiment': 0.25, 'block_orders': 0.20}
        total_score = sum(scores.get(k, 0) * weights.get(k, 0) for k in weights)
        
        # 确定信号级别
        if total_score >= 70:
            signal_level = "情绪高涨"
        elif total_score >= 50:
            signal_level = "情绪升温"
        else:
            signal_level = "情绪平稳"
            
        return LeadingIndicatorScore(
            total_score=total_score,
            components=scores,
            signal_level=signal_level,
            details=details,
            timestamp=datetime.now()
        )


class AuctionLeadingIndicator:
    """
    竞价预警指标计算器 (T-15min预警)
    基于集合竞价数据的最后机会预警
    """
    
    def __init__(self, config: V3Config = None):
        self.config = config or V3Config()
        self.thresholds = self.config.AUCTION_THRESHOLDS
        
    def calculate(self, data: Dict) -> LeadingIndicatorScore:
        """
        计算竞价预警得分
        
        Args:
            data: {
                'high_open_count': 15,       # 高开家数
                'total_stocks': 20,          # 板块总家数
                'auction_volume_ratio': 4.5,  # 竞价量能/昨日同期
                'block_order_amount': 1.8e8, # 板块隔夜单金额(元)
                'net_inflow': 5.2e8          # 竞价净流入(元)
            }
        """
        scores = {}
        details = []
        
        # 1. 板块高开率评分 (权重30%)
        if 'high_open_count' in data and 'total_stocks' in data:
            high_count = data['high_open_count']
            total = data['total_stocks']
            high_rate = high_count / total if total > 0 else 0
            
            # 评分逻辑：高开率60%=100分
            scores['high_open_rate'] = min(100, (high_rate / self.thresholds['high_open_rate']) * 100)
            
            if high_rate >= self.thresholds['high_open_rate']:
                details.append(f"板块高开率{high_rate:.0%}({high_count}/{total})，极度强势")
            elif high_rate >= 0.4:
                details.append(f"板块高开率{high_rate:.0%}，表现强势")
        else:
            scores['high_open_rate'] = 0
            
        # 2. 竞价量能比评分 (权重30%)
        if 'auction_volume_ratio' in data:
            vol_ratio = data['auction_volume_ratio']
            
            # 评分逻辑：量能比3倍=100分
            scores['volume_ratio'] = min(100, (vol_ratio / self.thresholds['volume_ratio_surge']) * 100)
            
            if vol_ratio >= self.thresholds['volume_ratio_surge']:
                details.append(f"竞价放量{vol_ratio:.1f}倍，资金抢筹明显")
            elif vol_ratio >= 2.0:
                details.append(f"竞价放量{vol_ratio:.1f}倍，资金积极")
        else:
            scores['volume_ratio'] = 0
            
        # 3. 隔夜单金额评分 (权重25%)
        if 'block_order_amount' in data:
            block = data['block_order_amount']
            
            # 评分逻辑：1亿=100分
            scores['block_order'] = min(100, (block / 1e8 / self.thresholds['block_order_amount']) * 100)
            
            if block >= self.thresholds['block_order_amount'] * 1e8:
                details.append(f"隔夜单{block/1e8:.1f}亿，资金决心极强")
            elif block >= 0.5 * 1e8:
                details.append(f"隔夜单{block/1e8:.1f}亿，资金态度坚决")
        else:
            scores['block_order'] = 0
            
        # 4. 竞价净流入评分 (权重15%)
        if 'net_inflow' in data:
            inflow = data['net_inflow']
            
            # 评分逻辑：5000万=100分
            scores['net_inflow'] = min(100, (inflow / 1e8 / self.thresholds['net_inflow_min']) * 100)
            
            if inflow >= self.thresholds['net_inflow_min'] * 1e8:
                details.append(f"竞价净流入{inflow/1e8:.1f}亿，主动买盘主导")
            elif inflow > 0:
                details.append(f"竞价净流入{inflow/1e8:.2f}亿，资金流入")
        else:
            scores['net_inflow'] = 0
        
        # 计算加权总分
        weights = {'high_open_rate': 0.30, 'volume_ratio': 0.30, 'block_order': 0.25, 'net_inflow': 0.15}
        total_score = sum(scores.get(k, 0) * weights.get(k, 0) for k in weights)
        
        # 确定信号级别
        if total_score >= 70:
            signal_level = "立即关注"
        elif total_score >= 50:
            signal_level = "重点关注"
        else:
            signal_level = "观察"
            
        return LeadingIndicatorScore(
            total_score=total_score,
            components=scores,
            signal_level=signal_level,
            details=details,
            timestamp=datetime.now()
        )


class DataFetcher:
    """
    数据获取器
    期货数据：使用Tushare接口（需要2000积分）
    注：美股和港股数据因接口稳定性问题，暂时使用模拟数据
    """
    
    # 期货合约代码映射
    FUTURE_CODES = {
        '碳酸锂': 'LC.SHF',      # 碳酸锂主力合约
        '硅料': 'SI.GFE',        # 工业硅主力合约（广州期货交易所）
        '多晶硅': 'PS.GFE',      # 多晶硅主力合约
        '原油': 'SC.INE',        # 原油主力合约
        '黄金': 'AU.SHF',        # 黄金主力合约
        '铜': 'CU.SHF',          # 铜主力合约
        '铝': 'AL.SHF',          # 铝主力合约
    }
    
    def __init__(self, tushare_token: str = None):
        """
        初始化数据获取器
        
        Args:
            tushare_token: Tushare Pro API Token，如果为None则从环境变量获取
        """
        self.ts_pro = None
        
        # 优先使用传入的token，否则从环境变量获取
        token = tushare_token or os.getenv('TUSHARE_TOKEN')
        
        if token:
            try:
                self.ts_pro = ts.pro_api(token)
                logger.info("[DataFetcher] Tushare Pro 初始化成功")
            except Exception as e:
                logger.error(f"[DataFetcher] Tushare Pro 初始化失败: {e}")
        else:
            logger.warning("[DataFetcher] 未提供有效的Tushare Token，将使用模拟数据")
    
    def get_future_daily(self, name: str, trade_date: str = None) -> Optional[Dict]:
        """
        获取期货日线数据
        
        Args:
            name: 期货名称（如'碳酸锂'、'硅料'）
            trade_date: 交易日期（YYYYMMDD），默认为最近交易日
            
        Returns:
            期货数据字典，包含open, close, change, pct_change等
        """
        if not self.ts_pro:
            logger.debug(f"[DataFetcher] Tushare未初始化，无法获取期货数据: {name}")
            return None
        
        ts_code = self.FUTURE_CODES.get(name)
        if not ts_code:
            logger.warning(f"[DataFetcher] 未知的期货名称: {name}")
            return None
        
        try:
            # 获取最近2条数据用于计算涨跌幅
            df = self.ts_pro.fut_daily(ts_code=ts_code, limit=2)
            
            if df is None or df.empty:
                logger.warning(f"[DataFetcher] 期货数据为空: {name} ({ts_code})")
                return None
            
            # 获取最新数据
            latest = df.iloc[0]
            
            result = {
                'name': name,
                'ts_code': ts_code,
                'trade_date': latest['trade_date'],
                'open': float(latest['open']),
                'close': float(latest['close']),
                'high': float(latest['high']),
                'low': float(latest['low']),
                'pre_close': float(latest['pre_close']) if 'pre_close' in latest else float(latest['pre_settle']),
                'change': float(latest['change1']) if 'change1' in latest else 0,
                'pct_change': 0,
                'vol': float(latest['vol']),
                'amount': float(latest['amount'])
            }
            
            # 计算涨跌幅
            if result['pre_close'] > 0:
                result['pct_change'] = (result['close'] - result['pre_close']) / result['pre_close']
            
            logger.debug(f"[DataFetcher] 获取期货数据成功: {name}, 涨跌幅={result['pct_change']*100:.2f}%")
            return result
            
        except Exception as e:
            logger.error(f"[DataFetcher] 获取期货数据失败 {name}: {e}")
            return None
    
    def get_commodity_prices(self) -> List[Dict]:
        """
        获取商品价格数据（期货）
        
        Returns:
            商品价格列表
        """
        commodities = ['碳酸锂', '硅料', '原油', '黄金']
        results = []
        
        for name in commodities:
            data = self.get_future_daily(name)
            if data:
                results.append({
                    'name': name,
                    'change': data['pct_change'],
                    'trend': self._get_trend_description(data['pct_change']),
                    'close': data['close'],
                    'pre_close': data['pre_close']
                })
        
        return results
    
    def _get_trend_description(self, pct_change: float) -> str:
        """根据涨跌幅获取趋势描述"""
        if pct_change >= 0.05:
            return '强势上涨'
        elif pct_change >= 0.03:
            return '突破前高'
        elif pct_change >= 0.01:
            return '持续上涨'
        elif pct_change > 0:
            return '小幅上涨'
        elif pct_change > -0.01:
            return '小幅下跌'
        elif pct_change > -0.03:
            return '持续下跌'
        else:
            return '大幅下跌'


class IndustryChainIndicator:
    """
    产业链传导指标计算器
    基于期货价格的跨板块预判（仅使用Tushare可获取的期货数据）
    """
    
    def __init__(self, data_fetcher: DataFetcher = None):
        # 数据获取器
        self.data_fetcher = data_fetcher
        
        # 产业链映射图谱 - 基于Tushare可获取的期货品种
        self.chain_map = {
            # 新能源产业链
            '碳酸锂': {
                'upstream': ['锂矿', '盐湖提锂'],
                'midstream': ['锂电池材料', '正极材料', '负极材料'],
                'downstream': ['锂电池', '新能源车', '储能'],
                'lead_time': '1-3个交易日',
                'impact_factor': 0.8
            },
            '硅料': {
                'upstream': ['多晶硅', '工业硅'],
                'midstream': ['硅片', '电池片', '光伏玻璃'],
                'downstream': ['光伏组件', '光伏电站', '逆变器'],
                'lead_time': '1-2个交易日',
                'impact_factor': 0.75
            },
            '原油': {
                'upstream': ['油气开采', '油服设备'],
                'midstream': ['炼油', '石油化工', '煤化工'],
                'downstream': ['塑料制品', '交通运输', '航空'],
                'lead_time': '即时',
                'impact_factor': 0.85
            },
            '黄金': {
                'upstream': ['黄金开采'],
                'midstream': ['黄金冶炼'],
                'downstream': ['黄金饰品', '央行购金'],
                'lead_time': '即时',
                'impact_factor': 0.95
            },
            '铜': {
                'upstream': ['铜矿开采'],
                'midstream': ['铜冶炼', '铜材加工'],
                'downstream': ['电线电缆', '电子元件', '家电'],
                'lead_time': '即时',
                'impact_factor': 0.9
            },
            '铝': {
                'upstream': ['铝土矿', '氧化铝'],
                'midstream': ['电解铝', '铝材加工'],
                'downstream': ['建筑铝材', '汽车铝材', '包装'],
                'lead_time': '即时',
                'impact_factor': 0.85
            }
        }
        
    def calculate(self, data: Dict) -> List[Dict]:
        """
        计算产业链传导信号
        
        Args:
            data: {
                'commodity_prices': [
                    {'name': '碳酸锂', 'change': 0.052, 'trend': '突破前高'},
                    {'name': '硅料', 'change': 0.03, 'trend': '持续上涨'}
                ],
                'use_real_data': True/False  # 是否使用真实数据
            }
        
        Returns:
            List[Dict]: 传导信号列表
        """
        signals = []
        
        # 如果使用真实数据获取
        if data.get('use_real_data', False) and self.data_fetcher:
            logger.info("[IndustryChainIndicator] 使用真实数据获取")
            
            # 获取期货数据
            commodity_prices = self.data_fetcher.get_commodity_prices()
            for commodity in commodity_prices:
                name = commodity.get('name', '')
                change = commodity.get('change', 0)
                trend = commodity.get('trend', '')
                
                # 查找匹配的产业链
                for key, chain in self.chain_map.items():
                    if name in key and abs(change) >= 0.03:  # 涨跌幅>3%触发
                        confidence = min(90, 60 + abs(change) * 500)
                        
                        signals.append({
                            'type': '期货价格传导',
                            'source': f"{name}期货涨{change:.1%}",
                            'upstream': chain['upstream'],
                            'midstream': chain['midstream'],
                            'downstream': chain['downstream'],
                            'lead_time': chain['lead_time'],
                            'confidence': confidence,
                            'impact_factor': chain['impact_factor'],
                            'trend': trend
                        })
        else:
            # 使用传入的模拟数据
            if 'commodity_prices' in data:
                for commodity in data['commodity_prices']:
                    name = commodity.get('name', '')
                    change = commodity.get('change', 0)
                    trend = commodity.get('trend', '')
                    
                    # 查找匹配的产业链
                    for key, chain in self.chain_map.items():
                        if name in key and abs(change) >= 0.03:  # 涨跌幅>3%触发
                            confidence = min(90, 60 + abs(change) * 500)  # 变化越大置信度越高
                            
                            signals.append({
                                'type': '期货价格传导',
                                'source': f"{name}期货涨{change:.1%}",
                                'upstream': chain['upstream'],
                                'midstream': chain['midstream'],
                                'downstream': chain['downstream'],
                                'lead_time': chain['lead_time'],
                                'confidence': confidence,
                                'impact_factor': chain['impact_factor'],
                                'trend': trend
                            })
        
        return signals


# ========== 核心计算引擎 ==========

class SectorHeatCalculatorV3:
    """
    板块热度计算器 V3 - 领先预警版 (核心引擎)
    """
    
    def __init__(self, config: V3Config = None, tushare_token: str = None):
        self.config = config or V3Config()
        self.weights = self.config.WEIGHTS
        
        # 初始化数据获取器（传入Tushare Token以获取期货真实数据）
        self.data_fetcher = DataFetcher(tushare_token)
        
        # 初始化领先指标计算器
        self.capital_indicator = CapitalLeadingIndicator(self.config)
        self.sentiment_indicator = SentimentLeadingIndicator(self.config)
        self.auction_indicator = AuctionLeadingIndicator(self.config)
        self.chain_indicator = IndustryChainIndicator(self.data_fetcher)
        
        # 决策矩阵：趋势阶段 × 共振类型 → 行动建议
        self.decision_matrix = self._build_decision_matrix()
        
    def _build_decision_matrix(self) -> Dict:
        """构建决策矩阵"""
        return {
            # 预启动阶段 (新增)
            (TrendStageV3.PRE_WAKEUP, ResonanceType.STRONG): {
                'signal': '预启动-强准备',
                'priority': 1,
                'action': '[预启动]资金情绪已动，提前埋伏，待启动加仓',
                'position': 'light',
                'risk': '最佳买点，可能等待1-2天'
            },
            (TrendStageV3.PRE_WAKEUP, ResonanceType.QUANTITY_LEADS): {
                'signal': '预启动-数量准备',
                'priority': 2,
                'action': '[预启动]小仓位试探，确认后加仓',
                'position': 'light',
                'risk': '虚热风险，严格止损'
            },
            
            # 启动期
            (TrendStageV3.START, ResonanceType.STRONG): {
                'signal': '强共振启动',
                'priority': 1,
                'action': '[启动]立即重仓，做首板/二板',
                'position': 'heavy',
                'risk': '最佳机会，次日有溢价'
            },
            (TrendStageV3.START, ResonanceType.QUANTITY_LEADS): {
                'signal': '数量启动-虚热',
                'priority': 2,
                'action': '[启动]只做龙头，不做跟风',
                'position': 'light',
                'risk': '大票未动，持续性存疑'
            },
            
            # 爆发期
            (TrendStageV3.EXPLOSION, ResonanceType.STRONG): {
                'signal': '强共振爆发',
                'priority': 1,
                'action': '[爆发]积极参与，做前排',
                'position': 'heavy',
                'risk': '加速期，注意分歧'
            },
            (TrendStageV3.EXPLOSION, ResonanceType.QUANTITY_LEADS): {
                'signal': '数量爆发-虚热警告',
                'priority': 3,
                'action': '[爆发]回避，小票乱炒',
                'position': 'none',
                'risk': '虚热，次日分化严重'
            },
            
            # 加速期
            (TrendStageV3.ACCELERATION, ResonanceType.STRONG): {
                'signal': '强共振加速',
                'priority': 2,
                'action': '[加速]做龙头分歧转一致',
                'position': 'medium',
                'risk': '后期，精选个股'
            },
            
            # 确认期
            (TrendStageV3.CONFIRMED, ResonanceType.STRONG): {
                'signal': '强共振确认',
                'priority': 3,
                'action': '[确认]做核心龙头，不杂毛',
                'position': 'medium',
                'risk': '主线后期，控制仓位'
            },
            
            # 成熟期
            (TrendStageV3.MATURE, ResonanceType.ANY): {
                'signal': '成熟期-回避',
                'priority': 5,
                'action': '[成熟]不介入，等退潮后',
                'position': 'none',
                'risk': '高位震荡，风险大于机会'
            },
            
            # 退潮期
            (TrendStageV3.DECLINE_EARLY, ResonanceType.ANY): {
                'signal': '早期退潮',
                'priority': 1,
                'action': '[退潮]坚决回避，不抄底',
                'position': 'none',
                'risk': '资金撤离，还有下跌空间'
            },
            (TrendStageV3.DECLINE_LATE, ResonanceType.ANY): {
                'signal': '晚期退潮',
                'priority': 1,
                'action': '[退潮]彻底放弃，等下一轮',
                'position': 'none',
                'risk': '已确认死亡，不关注'
            }
        }
    
    def _classify_resonance(self, zt_count: int, sector_change: float = 0.0,
                           large_cap_change: float = 0.0) -> ResonanceType:
        """判断共振类型"""
        if zt_count >= 5 and sector_change >= 0.03 and large_cap_change >= 0.02:
            return ResonanceType.STRONG
        elif zt_count >= 5 and sector_change < 0.03:
            return ResonanceType.QUANTITY_LEADS
        elif zt_count < 5 and zt_count >= 2 and sector_change >= 0.03:
            return ResonanceType.PRICE_LEADS
        elif zt_count >= 2 and sector_change >= 0.015:
            return ResonanceType.WEAK
        else:
            return ResonanceType.NONE
    
    def _lookup_decision(self, trend: TrendStageV3, resonance: ResonanceType) -> Dict:
        """查询决策矩阵"""
        key = (trend, resonance)
        
        if key in self.decision_matrix:
            return self.decision_matrix[key]
        
        # 尝试ANY匹配
        for (t, r), decision in self.decision_matrix.items():
            if t == trend and r == ResonanceType.ANY:
                return decision
        
        # 默认
        return {
            'signal': f'{trend.value}-{resonance.value}',
            'priority': 5,
            'action': '观察',
            'position': 'none',
            'risk': '不明确状态'
        }
    
    def calculate_sector_heat_v3(self,
                                today_count: int,
                                yesterday_count: int,
                                # 领先指标数据 (可选但强烈建议提供)
                                capital_data: Optional[Dict] = None,
                                sentiment_data: Optional[Dict] = None,
                                auction_data: Optional[Dict] = None,
                                chain_data: Optional[Dict] = None,
                                # 传统数据
                                rolling_3d: int = 0,
                                rolling_5d: int = 0,
                                rolling_20d: int = 0,
                                sector_change: float = 0.0,
                                large_cap_change: float = 0.0,
                                # 行业信息
                                l1_name: str = '',
                                l2_name: str = '') -> Optional[SectorSignalV3]:
        """
        计算板块热度 (V3核心方法)
        
        Args:
            today_count: 当日涨停数 (T+0)
            yesterday_count: 昨日涨停数
            capital_data: 资金前置数据 (T-24h)
            sentiment_data: 情绪先行数据 (T-2h)
            auction_data: 竞价预警数据 (T-15min)
            chain_data: 产业链传导数据
            rolling_3d: 3日滚动涨停数
            rolling_5d: 5日滚动涨停数
            rolling_20d: 20日滚动涨停数
            sector_change: 板块涨跌幅
            large_cap_change: 大票涨跌幅
            l1_name: 一级行业
            l2_name: 二级行业
        
        Returns:
            SectorSignalV3对象或None
        """
        
        # ========== 1. 计算领先指标得分 ==========
        capital_score = None
        sentiment_score = None
        auction_score = None
        chain_signals = []
        
        if capital_data:
            capital_score = self.capital_indicator.calculate(capital_data)
        if sentiment_data:
            sentiment_score = self.sentiment_indicator.calculate(sentiment_data)
        if auction_data:
            auction_score = self.auction_indicator.calculate(auction_data)
        if chain_data:
            chain_signals = self.chain_indicator.calculate(chain_data)
        
        # ========== 2. V3趋势判断逻辑 (重构) ==========
        
        # 2.1 预启动识别 (新增)
        pre_wakeup_score = 0
        if capital_score and sentiment_score:
            pre_wakeup_score = (capital_score.total_score * 0.6 + sentiment_score.total_score * 0.4)
        elif capital_score:
            pre_wakeup_score = capital_score.total_score
        elif sentiment_score:
            pre_wakeup_score = sentiment_score.total_score
        
        is_pre_wakeup = (
            pre_wakeup_score >= self.config.TREND_THRESHOLDS['pre_wakeup_capital'] and
            today_count < 2  # 股价尚未启动
        )
        
        # 2.2 启动期识别 (重构)
        is_traditional_start = (yesterday_count == 0 and today_count >= 2)
        is_leading_start = (
            (capital_score and capital_score.total_score >= 60) or
            (sentiment_score and sentiment_score.total_score >= 60)
        ) and today_count >= 1
        is_start = is_traditional_start or is_leading_start
        
        # 2.3 爆发期识别 (优化)
        explosion_threshold = self.config.TREND_THRESHOLDS['explosion_ratio']
        explosion_min = self.config.TREND_THRESHOLDS['explosion_min_today']
        is_explosion = (
            yesterday_count > 0 and 
            today_count >= yesterday_count * explosion_threshold and 
            today_count >= explosion_min
        )
        # 竞价验证爆发
        if auction_score and is_explosion:
            is_explosion = is_explosion and auction_score.total_score >= 50
        
        # 2.4 加速期识别
        avg_3d = rolling_3d / 3 if rolling_3d > 0 else 0
        avg_5d = rolling_5d / 5 if rolling_5d > 0 else 0
        momentum_3d_5d = (avg_3d - avg_5d) / avg_5d if avg_5d > 0 else 0
        is_acceleration = (
            momentum_3d_5d >= 0.25 and 
            rolling_3d >= 3 and 
            today_count >= 2
        )
        
        # 2.5 确认期识别
        is_confirmed = (
            rolling_3d >= 5 and 
            rolling_5d >= 7 and 
            rolling_20d >= 10 and 
            momentum_3d_5d > 0
        )
        
        # 2.6 退潮识别
        decline_threshold = self.config.TREND_THRESHOLDS['decline_momentum']
        is_decline_early = (
            momentum_3d_5d <= decline_threshold and 
            rolling_3d < yesterday_count
        )
        is_decline_late = (rolling_5d < rolling_20d * 0.3 and rolling_20d > 0)
        
        # 2.7 确定趋势阶段
        if is_pre_wakeup:
            trend_stage = TrendStageV3.PRE_WAKEUP
        elif is_start:
            trend_stage = TrendStageV3.START
        elif is_explosion:
            trend_stage = TrendStageV3.EXPLOSION
        elif is_acceleration:
            trend_stage = TrendStageV3.ACCELERATION
        elif is_confirmed:
            trend_stage = TrendStageV3.CONFIRMED
        elif is_decline_early:
            trend_stage = TrendStageV3.DECLINE_EARLY
        elif is_decline_late:
            trend_stage = TrendStageV3.DECLINE_LATE
        else:
            trend_stage = TrendStageV3.WATCH
        
        # ========== 3. 共振判断 ==========
        resonance = self._classify_resonance(today_count, sector_change, large_cap_change)
        
        # ========== 4. 查询决策矩阵 ==========
        decision = self._lookup_decision(trend_stage, resonance)
        
        # 如果建议不操作且不是风险信号，则忽略
        if decision['position'] == 'none' and '退潮' not in trend_stage.value:
            return None
        
        # ========== 5. 计算综合得分 (V3权重) ==========
        base_score = today_count * 10  # 基础分
        
        # 领先指标加成
        leading_bonus = 0
        if capital_score:
            leading_bonus += capital_score.total_score * self.weights['capital_leading']
        if sentiment_score:
            leading_bonus += sentiment_score.total_score * self.weights['sentiment_leading']
        if auction_score:
            leading_bonus += auction_score.total_score * self.weights['auction_signal']
        
        # 历史动量 (权重降低)
        momentum_score = max(0, momentum_3d_5d * 10) * self.weights['momentum_3d']
        
        total_score = base_score * self.weights['today_zt'] + leading_bonus + momentum_score
        
        # 置信度计算
        confidence = min(0.95, 0.5 + (today_count * 0.05) + (pre_wakeup_score * 0.002))
        
        # 构建关注理由
        watch_reason_parts = [f"{trend_stage.value} + {resonance.value}"]
        if capital_score and capital_score.total_score >= 50:
            watch_reason_parts.append(f"资金前置得分{capital_score.total_score:.0f}")
        if sentiment_score and sentiment_score.total_score >= 50:
            watch_reason_parts.append(f"情绪得分{sentiment_score.total_score:.0f}")
        if auction_score and auction_score.total_score >= 50:
            watch_reason_parts.append(f"竞价得分{auction_score.total_score:.0f}")
        if chain_signals:
            watch_reason_parts.append(f"产业链传导信号{len(chain_signals)}个")
        
        watch_reason = "，".join(watch_reason_parts)
        
        # 构建核心指标
        key_metrics = {
            "今日涨停": today_count,
            "昨日涨停": yesterday_count,
            "3日滚动": rolling_3d,
            "5日滚动": rolling_5d,
            "20日滚动": rolling_20d,
            "短期动量": f"{momentum_3d_5d:.1%}",
            "综合得分": f"{total_score:.1f}",
            "预启动得分": f"{pre_wakeup_score:.0f}",
            "趋势阶段": trend_stage.value,
            "共振类型": resonance.value,
        }
        
        if capital_score:
            key_metrics["资金前置得分"] = f"{capital_score.total_score:.0f}"
        if sentiment_score:
            key_metrics["情绪先行得分"] = f"{sentiment_score.total_score:.0f}"
        if auction_score:
            key_metrics["竞价预警得分"] = f"{auction_score.total_score:.0f}"
        
        return SectorSignalV3(
            l2_name=l2_name,
            l1_name=l1_name,
            trend_stage=trend_stage,
            resonance_type=resonance,
            combined_signal=decision['signal'],
            action=decision['action'],
            priority=decision['priority'],
            position_size=decision['position'],
            confidence=confidence,
            capital_score=capital_score,
            sentiment_score=sentiment_score,
            auction_score=auction_score,
            chain_signals=chain_signals,
            key_metrics=key_metrics,
            watch_reason=watch_reason,
            risk_warning=decision['risk']
        )
    
    def pre_market_analysis(self,
                           sectors_capital_data: Dict[str, Dict],
                           sectors_sentiment_data: Dict[str, Dict],
                           sectors_auction_data: Dict[str, Dict]) -> pd.DataFrame:
        """
        盘前综合分析 (9:15-9:25使用)
        
        Args:
            sectors_capital_data: {板块名: 资金数据}
            sectors_sentiment_data: {板块名: 情绪数据}
            sectors_auction_data: {板块名: 竞价数据}
        
        Returns:
            DataFrame: 盘前预警结果
        """
        results = []
        all_sectors = set(sectors_capital_data.keys()) |                      set(sectors_sentiment_data.keys()) |                      set(sectors_auction_data.keys())
        
        for sector in all_sectors:
            capital_data = sectors_capital_data.get(sector)
            sentiment_data = sectors_sentiment_data.get(sector)
            auction_data = sectors_auction_data.get(sector)
            
            # 计算各指标得分
            capital_score = self.capital_indicator.calculate(capital_data) if capital_data else None
            sentiment_score = self.sentiment_indicator.calculate(sentiment_data) if sentiment_data else None
            auction_score = self.auction_indicator.calculate(auction_data) if auction_data else None
            
            # 计算综合预警得分
            total_score = 0
            weights_used = 0
            
            if capital_score:
                total_score += capital_score.total_score * 0.35
                weights_used += 0.35
            if sentiment_score:
                total_score += sentiment_score.total_score * 0.35
                weights_used += 0.35
            if auction_score:
                total_score += auction_score.total_score * 0.30
                weights_used += 0.30
            
            if weights_used > 0:
                total_score = total_score / weights_used * (0.35 + 0.35 + 0.30)
            
            # 确定行动建议
            if total_score >= 75:
                action = "【强烈预警】多指标共振，准备抢筹"
                priority = 1
            elif total_score >= 60:
                action = "【明确预警】资金情绪双热，重点关注"
                priority = 2
            elif total_score >= 45:
                action = "【潜在机会】单指标异动，观察竞价"
                priority = 3
            else:
                action = "【观望】无明显信号"
                priority = 5
            
            results.append({
                '板块': sector,
                '综合预警得分': round(total_score, 1),
                '资金前置得分': round(capital_score.total_score, 1) if capital_score else None,
                '情绪先行得分': round(sentiment_score.total_score, 1) if sentiment_score else None,
                '竞价预警得分': round(auction_score.total_score, 1) if auction_score else None,
                '行动建议': action,
                '优先级': priority,
                '资金信号详情': capital_score.details if capital_score else [],
                '情绪信号详情': sentiment_score.details if sentiment_score else [],
                '竞价信号详情': auction_score.details if auction_score else []
            })
        
        df = pd.DataFrame(results)
        if not df.empty:
            df = df.sort_values('综合预警得分', ascending=False)
        return df


# ========== 使用示例 ==========

def demo():
    """演示V3系统的使用"""
    
    print("=" * 80)
    print("板块热度计算器V3 - 领先预警版 演示")
    print("=" * 80)
    
    calculator = SectorHeatCalculatorV3()
    
    # 场景1：预启动识别 (最佳买点)
    print("\n【场景1：预启动识别 - 资金先动，股价未动】")
    print("-" * 80)
    
    signal1 = calculator.calculate_sector_heat_v3(
        today_count=1,              # 今日只有1只涨停 (股价未完全启动)
        yesterday_count=0,
        capital_data={              # T-24h资金数据：机构大举买入
            'dragon_tiger': {'net_buy': 5.5, 'count': 8},
            'northbound': {'net_flow': 4.2},
            'margin': {'balance_change': 0.15}
        },
        sentiment_data={            # T-2h情绪数据：热度飙升
            'hot_rank': {'current': 3, 'previous': 28, 'change': -25},
            'search_index': {'change_rate': 3.0},
            'news_sentiment': 0.85,
            'block_orders': 3.0e8
        },
        rolling_3d=2,
        rolling_5d=3,
        rolling_20d=8,
        l1_name='电力设备',
        l2_name='光伏设备'
    )
    
    if signal1:
        print(f"板块: {signal1.l1_name} > {signal1.l2_name}")
        print(f"趋势阶段: {signal1.trend_stage.value}")
        print(f"联动信号: {signal1.combined_signal}")
        print(f"行动建议: {signal1.action}")
        print(f"仓位建议: {signal1.position_size}")
        print(f"置信度: {signal1.confidence:.0%}")
        print(f"资金前置得分: {signal1.capital_score.total_score:.0f}")
        print(f"情绪先行得分: {signal1.sentiment_score.total_score:.0f}")
        print(f"关注理由: {signal1.watch_reason}")
        print(f"风险提示: {signal1.risk_warning}")
    
    # 场景2：启动期确认
    print("\n【场景2：启动期确认 - 首板出现】")
    print("-" * 80)
    
    signal2 = calculator.calculate_sector_heat_v3(
        today_count=3,
        yesterday_count=0,
        auction_data={              # T-15min竞价数据确认
            'high_open_count': 12,
            'total_stocks': 15,
            'auction_volume_ratio': 4.0,
            'block_order_amount': 2.0e8,
            'net_inflow': 4.5e8
        },
        rolling_3d=3,
        rolling_5d=4,
        sector_change=0.035,
        large_cap_change=0.025,
        l1_name='计算机',
        l2_name='AI算力'
    )
    
    if signal2:
        print(f"板块: {signal2.l1_name} > {signal2.l2_name}")
        print(f"趋势阶段: {signal2.trend_stage.value}")
        print(f"行动建议: {signal2.action}")
        print(f"竞价预警得分: {signal2.auction_score.total_score:.0f}")
    
    # 场景3：产业链传导预判
    print("\n【场景3：产业链传导预判 - 期货→现货】")
    print("-" * 80)
    
    # 方式1：使用模拟数据
    print("\n方式1：使用模拟数据")
    chain_data_simulated = {
        'commodity_prices': [
            {'name': '碳酸锂', 'change': 0.052, 'trend': '突破前期高点'},
            {'name': '硅料', 'change': 0.038, 'trend': '持续上涨'}
        ]
    }
    
    chain_signals = calculator.chain_indicator.calculate(chain_data_simulated)
    
    print("检测到产业链传导信号（模拟数据）：")
    for sig in chain_signals:
        print(f"  [{sig['type']}] {sig['source']}")
        print(f"    传导路径: {' → '.join(sig['upstream'])} → {' → '.join(sig['midstream'])} → {' → '.join(sig['downstream'])}")
        print(f"    领先时间: {sig['lead_time']}")
        print(f"    置信度: {sig['confidence']:.0f}%")
    
    # 方式2：使用Tushare真实数据
    print("\n方式2：使用Tushare真实数据（需要有效的Token）")
    chain_data_real = {
        'use_real_data': True  # 启用真实数据获取
    }
    
    chain_signals_real = calculator.chain_indicator.calculate(chain_data_real)
    
    if chain_signals_real:
        print("检测到产业链传导信号（真实数据）：")
        for sig in chain_signals_real:
            print(f"  [{sig['type']}] {sig['source']}")
            print(f"    传导路径: {' → '.join(sig['upstream'])} → {' → '.join(sig['midstream'])} → {' → '.join(sig['downstream'])}")
            print(f"    领先时间: {sig['lead_time']}")
            print(f"    置信度: {sig['confidence']:.0f}%")
    else:
        print("  未检测到有效信号或Tushare未初始化")
    
    # 场景4：盘前综合分析
    print("\n【场景4：盘前综合分析 - 9:15-9:25使用】")
    print("-" * 80)
    
    # 模拟多个板块的盘前数据
    sectors_capital = {
        '光伏设备': {
            'dragon_tiger': {'net_buy': 5.2, 'count': 8},
            'northbound': {'net_flow': 3.8}
        },
        '锂电池': {
            'dragon_tiger': {'net_buy': 3.5, 'count': 5},
            'margin': {'balance_change': 0.12}
        },
        'AI算力': {
            'northbound': {'net_flow': 2.5},
            'margin': {'balance_change': 0.08}
        }
    }
    
    sectors_sentiment = {
        '光伏设备': {
            'hot_rank': {'current': 2, 'previous': 22, 'change': -20},
            'search_index': {'change_rate': 2.5},
            'news_sentiment': 0.8
        },
        '锂电池': {
            'hot_rank': {'current': 8, 'previous': 15, 'change': -7},
            'search_index': {'change_rate': 1.2}
        }
    }
    
    sectors_auction = {
        '光伏设备': {
            'high_open_count': 15,
            'total_stocks': 20,
            'auction_volume_ratio': 4.5,
            'block_order_amount': 1.8e8
        },
        'AI算力': {
            'high_open_count': 8,
            'total_stocks': 12,
            'auction_volume_ratio': 3.2
        }
    }
    
    pre_market_df = calculator.pre_market_analysis(
        sectors_capital, sectors_sentiment, sectors_auction
    )
    
    print("盘前预警排名：")
    print(pre_market_df[['板块', '综合预警得分', '行动建议', '优先级']].to_string(index=False))

    print("\n" + "=" * 80)
    print("V3系统优势总结：")
    print("1. 提前6-12小时预警 (资金前置+情绪先行)")
    print("2. 提前15分钟最后确认 (竞价监控)")
    print("3. 跨板块产业链预判 (期货价格传导)")
    print("4. 新增'预启动'阶段，捕捉最佳买点")
    print("5. 当日权重40%+领先指标40%，反应更灵敏")
    print("=" * 80)


if __name__ == "__main__":
    demo()
