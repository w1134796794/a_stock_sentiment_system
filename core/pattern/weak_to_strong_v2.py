
"""
弱转强策略 V2 - Tushare集成完整版
核心改进：
1. 连板高度分层：1-2板(低位) + 5板以上(高位)，排除3-4板中位股陷阱
2. 实际换手率计算：基于Tushare Pro股东数据，剔除大股东锁定筹码
3. 分层换手率阈值：低位实际换手>25%，高位实际换手>35%
4. 6000+积分优化：高频调用，批量获取，缓存机制

数据源：Tushare Pro (需2000+积分，6000积分每分钟500次无上限)
接口：top10_floatholders (前十大流通股东)

作者：量化交易系统
版本：2.1.0
日期：2026-04-06
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import tushare as ts
import time
import loguru

logger = loguru.logger


# ========== 配置层 ==========

class PatternType(Enum):
    WEAK_TO_STRONG_LOW = "低位弱转强"      # 1-2板
    WEAK_TO_STRONG_HIGH = "高位弱转强"     # 5板以上


@dataclass
class V2Config:
    """V2策略配置"""
    
    # 连板高度分层 (排除3-4板中位股)
    BOARD_HEIGHT_TIERS = {
        'low': {'min': 1, 'max': 2, 'name': '低位', 'exclude': False},
        'mid': {'min': 3, 'max': 4, 'name': '中位', 'exclude': True},  # 排除
        'high': {'min': 5, 'max': 8, 'name': '高位', 'exclude': False}
    }
    
    # 分层换手率阈值 (实际换手率)
    TURNOVER_THRESHOLDS = {
        'low': {   # 1-2板
            'min_real': 25,         # 最低25%
            'ideal_real': 40,       # 理想40%
            'min_nominal': 20       # 名义换手参考
        },
        'high': {   # 5板以上
            'min_real': 35,         # 最低35%
            'ideal_real': 50,       # 理想50%
            'min_nominal': 25       # 名义换手参考
        }
    }
    
    # 昨日"弱"的质量标准
    WEAK_QUALITY = {
        'max_blast_times': 5,
        'min_blast_times': 2,
        'last_seal_time': "14:30:00",
        'max_drawdown_in_blast': 0.05
    }
    
    # 今日"强"的竞价标准
    AUCTION_PARAMS = {
        'min_gap': 0.02,
        'ideal_gap_low': 0.04,
        'ideal_gap_high': 0.03,
        'max_gap': 0.06,
        'min_auction_vol_ratio': 0.08,
        'min_auction_amount': 5000000
    }
    
    # 开盘后确认
    OPEN_CONFIRM = {
        'max_time_to_limit': 10,
        'max_open_drop': 0.02,
        'min_open_rise': 0.01
    }


# ========== Tushare数据层 ==========

class TushareShareholderData:
    """Tushare股东数据管理"""
    
    def __init__(self, token: str):
        self.token = token
        self.pro = None
        self._cache = {}
        self._init_api()
        
        # 锁定股东识别关键词
        self.locked_keywords = {
            '实控人': ['实际控制人', '控股股东', '董事长', '创始人', '家族', '一致行动'],
            '员工持股': ['员工持股', '股权激励', '核心员工', '员工计划', 'ESOP'],
            '战略配售': ['战略配售', '战略投资', 'IPO战略', '首发战略'],
            '国资锁定': ['国资委', '国资公司', '国有资本', '社保基金', '养老金', '中央汇金'],
            '董监高': ['董事', '监事', '高管', '总经理', '财务总监']
        }
    
    def _init_api(self):
        """初始化Tushare API"""
        try:
            ts.set_token(self.token)
            self.pro = ts.pro_api()
            logger.info("✓ Tushare Pro API初始化成功")
        except Exception as e:
            logger.error(f"✗ Tushare初始化失败: {e}")
    
    def get_top10_floatholders(self, stock_code: str, use_cache: bool = True) -> pd.DataFrame:
        """获取前十大流通股东"""
        # 缓存检查
        if use_cache and stock_code in self._cache:
            cache_time, cache_data = self._cache[stock_code]
            if (datetime.now() - cache_time).seconds < 3600:
                return cache_data
        
        if not self.pro:
            return pd.DataFrame()
        
        # 标准化代码
        if '.' not in stock_code:
            stock_code = f"{stock_code}.SH" if stock_code.startswith('6') else f"{stock_code}.SZ"
        
        try:
            # 获取最近3年数据，取最新一期
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=365*3)).strftime('%Y%m%d')
            
            df = self.pro.top10_floatholders(
                ts_code=stock_code,
                start_date=start_date,
                end_date=end_date
            )
            
            if df is not None and not df.empty:
                # 取最新一期
                latest_period = df['end_date'].max()
                df = df[df['end_date'] == latest_period].copy()
                
                # 转换数值类型
                for col in ['hold_amount', 'hold_ratio', 'hold_float_ratio']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                
                # 缓存
                if use_cache:
                    self._cache[stock_code] = (datetime.now(), df)
                
                return df
            
        except Exception as e:
            logger.error(f"获取 {stock_code} 股东数据失败: {e}")
        
        return pd.DataFrame()
    
    def identify_locked_shares(self, df: pd.DataFrame) -> Tuple[float, List[Dict]]:
        """识别锁定筹码"""
        if df.empty:
            return 0, []
        
        locked_amount = 0
        locked_holders = []
        
        for _, row in df.iterrows():
            holder_name = str(row.get('holder_name', ''))
            hold_amount = float(row.get('hold_amount', 0))
            holder_type = str(row.get('holder_type', ''))
            
            is_locked = False
            lock_reason = []
            
            # 根据类型和名称判断
            if holder_type in ['个人', '个人股东']:
                for category, keywords in self.locked_keywords.items():
                    if any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            elif holder_type in ['国资', '国家队']:
                is_locked = True
                lock_reason.append('国资锁定')
            elif holder_type in ['公司', '企业']:
                for category, keywords in self.locked_keywords.items():
                    if category in ['员工持股', '战略配售'] and any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            
            # 关键词补充判断
            if not is_locked:
                for category, keywords in self.locked_keywords.items():
                    if any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            
            if is_locked:
                locked_amount += hold_amount
                locked_holders.append({
                    'name': holder_name,
                    'amount': hold_amount / 10000,  # 万股
                    'ratio': row.get('hold_ratio', 0),
                    'float_ratio': row.get('hold_float_ratio', 0),
                    'reason': lock_reason
                })
        
        return locked_amount / 10000, locked_holders  # 返回万股
    
    def calculate_real_turnover(self, 
                              stock_code: str,
                              total_float_shares: float,  # 万股
                              day_volume: float,           # 万股
                              use_cache: bool = True) -> Dict:
        """计算实际换手率"""
        # 获取股东数据
        df = self.get_top10_floatholders(stock_code, use_cache)
        
        if df.empty:
            # 无数据时使用名义换手率
            nominal = (day_volume / total_float_shares) * 100 if total_float_shares > 0 else 0
            return {
                'nominal_turnover': nominal,
                'real_turnover': nominal,
                'free_float_shares': total_float_shares,
                'locked_shares': 0,
                'locked_ratio': 0,
                'locked_holders': [],
                'data_source': 'nominal_only'
            }
        
        # 识别锁定筹码
        locked_shares, locked_holders = self.identify_locked_shares(df)
        
        # 估算总锁定筹码（前十大覆盖约60%）
        top10_total = df['hold_amount'].sum() / 10000  # 万股
        top10_ratio = top10_total / total_float_shares if total_float_shares > 0 else 0
        estimation_factor = 0.6 / top10_ratio if top10_ratio > 0 else 1.0
        estimated_total_locked = locked_shares * estimation_factor
        
        # 自由流通股本
        free_float = total_float_shares - estimated_total_locked
        if free_float <= 0:
            free_float = total_float_shares * 0.5
        
        # 计算换手率
        nominal = (day_volume / total_float_shares) * 100 if total_float_shares > 0 else 0
        real = (day_volume / free_float) * 100 if free_float > 0 else 0
        
        return {
            'nominal_turnover': round(nominal, 2),
            'real_turnover': round(real, 2),
            'free_float_shares': round(free_float, 2),
            'locked_shares': round(estimated_total_locked, 2),
            'locked_ratio': round(estimated_total_locked / total_float_shares, 4) if total_float_shares > 0 else 0,
            'locked_holders': locked_holders,
            'top10_coverage': round(top10_ratio, 4),
            'data_source': 'tushare_pro'
        }


# ========== 弱转强策略核心 ==========

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
    buy_timing: str = ""
    turnover_data: Optional[Dict] = None


class WeakToStrongStrategyV2:
    """
    弱转强策略 V2 - Tushare集成版
    """
    
    def __init__(self, tushare_token: str):
        self.config = V2Config()
        self.shareholder_data = TushareShareholderData(tushare_token)

    def _time_to_minutes(self, time_str: str) -> int:
        """
        将时间字符串转换为分钟数（从0点开始）
        支持格式: HHMMSS, HH:MM:SS, HHMM, HH:MM
        """
        if not time_str or time_str == '0':
            return 0

        time_str = str(time_str).strip()

        try:
            # 处理 HH:MM:SS 或 HH:MM 格式
            if ':' in time_str:
                parts = time_str.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                return hour * 60 + minute

            # 处理纯数字格式
            time_str = time_str.zfill(6)  # 补齐6位

            if len(time_str) == 6:
                # HHMMSS 格式
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                return hour * 60 + minute
            elif len(time_str) == 4:
                # HHMM 格式
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                return hour * 60 + minute
            else:
                return 0
        except (ValueError, IndexError):
            logger.warning(f"时间格式解析失败: {time_str}")
            return 0

    def detect(self,
              stock_code: str,
              stock_name: str,
              board_height: int,
              yesterday_data: pd.Series,
              yesterday_tick: pd.DataFrame,
              today_auction: Dict,
              today_tick: pd.DataFrame,
              sector_leader: bool,
              total_float_shares: float = None) -> Optional[TradeSignal]:
        """
        检测弱转强机会
        """
        logger.debug(f"[V2-{stock_code}] 开始检测: {stock_name}, 连板高度={board_height}, 龙头={sector_leader}")
        
        # Step 1: 连板高度分层
        tier = self._classify_board_height(board_height)
        logger.debug(f"[V2-{stock_code}] 连板分层结果: tier={tier}")
        if tier is None:
            logger.info(f"{stock_code} {board_height}板为中位股陷阱，排除")
            return None
        
        if tier == 'low' and not sector_leader:
            logger.info(f"{stock_code} 低位非龙头，排除")
            return None
        
        logger.debug(f"[V2-{stock_code}] 通过分层检查: tier={tier}")

        # Step 2: 获取实际换手率（直接从daily_basic接口获取）
        # 使用传入的换手率数据（turnover_rate_f）
        real_turnover = yesterday_data.get('换手率', 0)
        nominal_turnover = real_turnover  # 实际换手率已考虑自由流通股

        # 获取流通股本用于后续计算
        if total_float_shares is None:
            total_float_shares = yesterday_data.get('流通股本', 0) / 1e4  # 万股
            logger.debug(f"[V2-{stock_code}] 从yesterday_data获取流通股本: {total_float_shares:.2f}万股")
        else:
            logger.debug(f"[V2-{stock_code}] 传入流通股本: {total_float_shares:.2f}万股")

        # 构造turnover_data用于返回
        turnover_data = {
            'real_turnover': real_turnover,
            'nominal_turnover': nominal_turnover,
            'free_float_shares': total_float_shares,  # 简化为总流通股本
            'locked_shares': 0,
            'locked_ratio': 0,
            'data_source': 'daily_basic'
        }

        logger.info(f"{stock_code} 实际换手率: {real_turnover:.2f}% (来自daily_basic)")
        logger.debug(f"[V2-{stock_code}] 流通股本: {total_float_shares:.2f}万股")
        
        # Step 3: 昨日"弱"的质量
        logger.debug(f"[V2-{stock_code}] 开始分析昨日弱质量...")
        weak_quality = self._analyze_yesterday_weak(
            yesterday_data, yesterday_tick, real_turnover, tier
        )
        logger.debug(f"[V2-{stock_code}] 昨日弱质量: is_valid={weak_quality.get('is_valid')}, "
                    f"weak_type={weak_quality.get('weak_type')}, score={weak_quality.get('score')}")
        
        if not weak_quality['is_valid']:
            logger.info(f"{stock_code} 昨日弱质量不达标: {weak_quality['reason']}")
            return None
        
        # Step 4: 今日竞价"强"态度
        logger.debug(f"[V2-{stock_code}] 开始检查竞价强态度...")
        logger.debug(f"[V2-{stock_code}] 竞价数据: {today_auction}")
        strong_attitude = self._check_auction_strong(today_auction, yesterday_data, tier)
        logger.debug(f"[V2-{stock_code}] 竞价强态度: is_strong={strong_attitude.get('is_strong')}, "
                    f"gap={strong_attitude.get('gap', 0)*100:.2f}%, "
                    f"vol_ratio={strong_attitude.get('vol_ratio', 0)*100:.2f}%")
        
        if not strong_attitude['is_strong']:
            logger.info(f"{stock_code} 竞价态度不强: {strong_attitude['reason']}")
            return None
        
        # Step 5: 开盘后确认
        logger.debug(f"[V2-{stock_code}] 开始开盘确认检查...")
        logger.debug(f"[V2-{stock_code}] tick数据为空: {today_tick.empty}")
        open_confirm = self._check_open_confirmation(today_tick, today_auction)
        logger.debug(f"[V2-{stock_code}] 开盘确认: is_confirmed={open_confirm.get('is_confirmed')}")
        
        if not open_confirm['is_confirmed']:
            logger.info(f"{stock_code} 开盘确认失败: {open_confirm.get('reason', '')}")
            return None
        
        # Step 6: 生成信号
        logger.debug(f"[V2-{stock_code}] 所有检查通过，生成交易信号...")
        return self._generate_signal(
            stock_code, stock_name, board_height, tier,
            weak_quality, strong_attitude, open_confirm,
            turnover_data, today_auction
        )
    
    def _classify_board_height(self, height: int) -> Optional[str]:
        """判断连板高度层级"""
        for tier, config in self.config.BOARD_HEIGHT_TIERS.items():
            if config['min'] <= height <= config['max']:
                if config.get('exclude', False):
                    return None
                return tier
        return None
    
    def _analyze_yesterday_weak(self,
                               yesterday: pd.Series,
                               tick: pd.DataFrame,
                               real_turnover: float,
                               tier: str) -> Dict:
        """分析昨日弱的质量"""
        stock_code = yesterday.get('代码', 'unknown')
        blast_times = yesterday.get('炸板次数', 0)
        last_seal = yesterday.get('最后封板时间', '')
        zdf = yesterday.get('涨跌幅', 0)

        logger.debug(f"[V2-{stock_code}] _analyze_yesterday_weak: blast_times={blast_times}, "
                    f"last_seal={last_seal}, zdf={zdf}, tier={tier}")

        # 确保 last_seal 是字符串类型并转换为可比较的分钟数
        if last_seal is None or last_seal == 0 or last_seal == '':
            last_seal_minutes = 0
        else:
            last_seal_str = str(last_seal)
            # 将时间转换为分钟数进行比较
            # 支持格式: HHMMSS, HH:MM:SS, HHMM, HH:MM
            last_seal_minutes = self._time_to_minutes(last_seal_str)
            logger.debug(f"[V2-{stock_code}] 最后封板时间转换: {last_seal_str} -> {last_seal_minutes}分钟")

        # 尾盘板阈值: 14:30 = 870分钟
        late_board_threshold = self._time_to_minutes(self.config.WEAK_QUALITY['last_seal_time'])

        # 判断弱类型
        weak_type = ""
        if blast_times >= 2:
            weak_type = "烂板"
            logger.debug(f"[V2-{stock_code}] 弱类型判断: 烂板 (blast_times={blast_times})")
        elif last_seal_minutes > late_board_threshold:
            weak_type = "尾盘板"
            logger.debug(f"[V2-{stock_code}] 弱类型判断: 尾盘板 (last_seal={last_seal_minutes}分钟 > {late_board_threshold}分钟)")
        elif zdf < 9.5:
            weak_type = "断板"
            logger.debug(f"[V2-{stock_code}] 弱类型判断: 断板 (zdf={zdf} < 9.5)")
        else:
            logger.debug(f"[V2-{stock_code}] 弱类型判断: 昨日不弱，返回无效")
            return {'is_valid': False, 'reason': '昨日不弱'}

        # 质量评分
        score = 60
        reasons = []
        logger.debug(f"[V2-{stock_code}] 开始质量评分，初始分={score}")

        # 实际换手率检查（核心）
        min_real = self.config.TURNOVER_THRESHOLDS[tier]['min_real']
        ideal_real = self.config.TURNOVER_THRESHOLDS[tier]['ideal_real']
        logger.debug(f"[V2-{stock_code}] 换手率检查: real={real_turnover:.2f}%, min={min_real}%, ideal={ideal_real}%")

        if real_turnover >= ideal_real:
            score += 25
            reasons.append(f"实际换手优秀({real_turnover:.1f}%)")
            logger.debug(f"[V2-{stock_code}] 换手率优秀 +25分，当前={score}")
        elif real_turnover >= min_real:
            score += 15
            reasons.append(f"实际换手达标({real_turnover:.1f}%)")
            logger.debug(f"[V2-{stock_code}] 换手率达标 +15分，当前={score}")
        else:
            score -= 30
            reasons.append(f"实际换手不足({real_turnover:.1f}% < {min_real}%)")
            logger.debug(f"[V2-{stock_code}] 换手率不足 -30分，当前={score}")

        # 烂板次数
        if 2 <= blast_times <= 4:
            score += 10
            reasons.append(f"烂板次数适中({blast_times})")
            logger.debug(f"[V2-{stock_code}] 烂板次数适中({blast_times}) +10分，当前={score}")
        elif blast_times > 5:
            score -= 30
            reasons.append("烂板次数太多")
            logger.debug(f"[V2-{stock_code}] 烂板次数太多({blast_times}) -30分，当前={score}")

        # 封板时间
        if last_seal > "14:50:00":
            score += 10
            reasons.append("尾盘回封有维护")
            logger.debug(f"[V2-{stock_code}] 尾盘回封 +10分，当前={score}")

        # 分时承接
        if not tick.empty:
            limit_price = yesterday.get('涨停价', tick['price'].max())
            blast_period = tick[tick['price'] < limit_price * 0.99]
            if not blast_period.empty:
                min_price = blast_period['price'].min()
                drawdown = (limit_price - min_price) / limit_price
                logger.debug(f"[V2-{stock_code}] 分时承接: drawdown={drawdown:.4f}, max={self.config.WEAK_QUALITY['max_drawdown_in_blast']}")
                if drawdown <= self.config.WEAK_QUALITY['max_drawdown_in_blast']:
                    score += 10
                    reasons.append("炸板承接强")
                    logger.debug(f"[V2-{stock_code}] 承接强 +10分，当前={score}")
                else:
                    score -= 15
                    reasons.append("炸板承接弱")
                    logger.debug(f"[V2-{stock_code}] 承接弱 -15分，当前={score}")
        else:
            logger.debug(f"[V2-{stock_code}] tick数据为空，跳过分时承接检查")

        is_valid = score >= 60 and real_turnover >= min_real
        logger.debug(f"[V2-{stock_code}] 最终评分: score={score}, real_turnover={real_turnover:.2f}%, "
                    f"min_real={min_real}%, is_valid={is_valid}")

        return {
            'is_valid': is_valid,
            'weak_type': weak_type,
            'score': score,
            'real_turnover': real_turnover,
            'reason': ";".join(reasons)
        }
    
    def _check_auction_strong(self,
                            auction: Dict,
                            yesterday: pd.Series,
                            tier: str) -> Dict:
        """检查竞价强态度"""
        stock_code = yesterday.get('代码', 'unknown')
        open_price = auction.get('开盘价', 0)
        yest_close = yesterday.get('收盘价', 1)
        gap = (open_price - yest_close) / yest_close if yest_close > 0 else 0

        logger.debug(f"[V2-{stock_code}] _check_auction_strong: open={open_price}, yest_close={yest_close}, gap={gap*100:.2f}%, tier={tier}")

        # 分层高开标准
        if tier == 'low':
            min_gap = self.config.AUCTION_PARAMS['min_gap']
            ideal_gap = self.config.AUCTION_PARAMS['ideal_gap_low']
            max_gap = self.config.AUCTION_PARAMS['max_gap']
        else:
            min_gap = self.config.AUCTION_PARAMS['min_gap']
            ideal_gap = self.config.AUCTION_PARAMS['ideal_gap_high']
            max_gap = self.config.AUCTION_PARAMS['max_gap']

        logger.debug(f"[V2-{stock_code}] 高开标准: tier={tier}, min={min_gap*100:.1f}%, ideal={ideal_gap*100:.1f}%, max={max_gap*100:.1f}%")

        if not (min_gap <= gap <= max_gap):
            logger.debug(f"[V2-{stock_code}] 高开不符合标准: {gap*100:.2f}% 不在 [{min_gap*100:.1f}%, {max_gap*100:.1f}%] 范围内")
            return {'is_strong': False, 'reason': f'高开{gap*100:.1f}%不符合标准'}

        logger.debug(f"[V2-{stock_code}] 高开符合标准: {gap*100:.2f}%")

        # 竞价量
        auction_vol = auction.get('竞价成交量', 0)
        yest_vol = yesterday.get('成交量', 1)
        vol_ratio = auction_vol / yest_vol if yest_vol > 0 else 0
        min_vol_ratio = self.config.AUCTION_PARAMS['min_auction_vol_ratio']

        logger.debug(f"[V2-{stock_code}] 竞价量检查: auction_vol={auction_vol}, yest_vol={yest_vol}, vol_ratio={vol_ratio*100:.2f}%, min={min_vol_ratio*100:.1f}%")

        if vol_ratio < min_vol_ratio:
            logger.debug(f"[V2-{stock_code}] 竞价量不足: {vol_ratio*100:.2f}% < {min_vol_ratio*100:.1f}%")
            return {'is_strong': False, 'reason': f'竞价量{vol_ratio*100:.1f}%不足'}

        logger.debug(f"[V2-{stock_code}] 竞价量达标: {vol_ratio*100:.2f}%")

        # 竞价金额
        auction_amount = auction.get('竞价成交额', auction_vol * open_price)
        min_amount = self.config.AUCTION_PARAMS['min_auction_amount']

        logger.debug(f"[V2-{stock_code}] 竞价金额检查: amount={auction_amount:.0f}, min={min_amount:.0f}")

        if auction_amount < min_amount:
            logger.debug(f"[V2-{stock_code}] 竞价金额不足: {auction_amount:.0f} < {min_amount:.0f}")
            return {'is_strong': False, 'reason': '竞价金额不足'}

        logger.debug(f"[V2-{stock_code}] 竞价金额达标: {auction_amount:.0f}")

        # 竞价走势
        price_trend = auction.get('价格序列', [])
        logger.debug(f"[V2-{stock_code}] 竞价走势: price_trend长度={len(price_trend)}")

        if len(price_trend) >= 2 and price_trend[-1] < price_trend[-2]:
            logger.debug(f"[V2-{stock_code}] 竞价末端回落: {price_trend[-2]} -> {price_trend[-1]}")
            return {'is_strong': False, 'reason': '竞价末端回落'}

        if len(price_trend) >= 2:
            logger.debug(f"[V2-{stock_code}] 竞价走势正常: {price_trend[-2]} -> {price_trend[-1]}")

        # 置信度
        confidence = 0.70
        if gap >= ideal_gap:
            confidence += 0.15
            logger.debug(f"[V2-{stock_code}] 高开理想 +15%置信度")
        if vol_ratio >= 0.12:
            confidence += 0.10
            logger.debug(f"[V2-{stock_code}] 竞价量理想 +10%置信度")

        final_confidence = min(confidence, 0.95)
        logger.debug(f"[V2-{stock_code}] 最终置信度: {final_confidence:.2f}")

        return {
            'is_strong': True,
            'gap': gap,
            'vol_ratio': vol_ratio,
            'amount': auction_amount,
            'confidence': final_confidence
        }
    
    def _check_open_confirmation(self, tick: pd.DataFrame, auction: Dict) -> Dict:
        """开盘后确认"""
        logger.debug(f"[_check_open_confirmation] tick为空: {tick.empty}")

        if tick.empty:
            logger.debug("[_check_open_confirmation] tick数据为空，返回False")
            return {'is_confirmed': False}

        open_price = auction.get('开盘价', tick.iloc[0]['price'])
        first_10min = tick.head(10)

        logger.debug(f"[_check_open_confirmation] open_price={open_price}, first_10min长度={len(first_10min)}")

        if first_10min.empty:
            logger.debug("[_check_open_confirmation] 前10分钟数据为空，返回False")
            return {'is_confirmed': False}

        # 最大回踩
        min_price = first_10min['price'].min()
        max_drop = (open_price - min_price) / open_price if open_price > 0 else 0
        max_drop_threshold = self.config.OPEN_CONFIRM['max_open_drop']

        logger.debug(f"[_check_open_confirmation] 回踩检查: min_price={min_price}, max_drop={max_drop*100:.2f}%, threshold={max_drop_threshold*100:.1f}%")

        if max_drop > max_drop_threshold:
            logger.debug(f"[_check_open_confirmation] 回踩太深: {max_drop*100:.2f}% > {max_drop_threshold*100:.1f}%")
            return {'is_confirmed': False, 'reason': f'开盘回踩{max_drop*100:.1f}%太深'}

        logger.debug(f"[_check_open_confirmation] 回踩正常: {max_drop*100:.2f}%")

        # 涨停时间
        limit_price = tick['price'].max()
        limit_ticks = tick[tick['price'] >= limit_price * 0.995]

        logger.debug(f"[_check_open_confirmation] 涨停检查: limit_price={limit_price}, 涨停tick数={len(limit_ticks)}")

        if limit_ticks.empty:
            logger.debug("[_check_open_confirmation] 未找到涨停数据")
            return {'is_confirmed': False, 'reason': '未涨停'}

        first_limit_time = limit_ticks.iloc[0]['time']
        minutes = self._minutes_from_open(first_limit_time)
        max_minutes = self.config.OPEN_CONFIRM['max_time_to_limit']

        logger.debug(f"[_check_open_confirmation] 涨停时间: first_limit_time={first_limit_time}, minutes={minutes}, max={max_minutes}")

        if minutes > max_minutes:
            logger.debug(f"[_check_open_confirmation] 涨停太慢: {minutes}分钟 > {max_minutes}分钟")
            return {'is_confirmed': False, 'reason': f'{minutes}分钟才涨停，太慢'}

        logger.debug(f"[_check_open_confirmation] 涨停时间合格: {minutes}分钟")

        return {
            'is_confirmed': True,
            'max_drop': max_drop,
            'time_to_limit': minutes
        }
    
    def _generate_signal(self, stock_code, stock_name, board_height, tier,
                        weak_quality, strong_attitude, open_confirm,
                        turnover_data, auction):
        """生成交易信号"""
        
        pattern_type = PatternType.WEAK_TO_STRONG_LOW if tier == 'low' else PatternType.WEAK_TO_STRONG_HIGH
        
        # 买点
        gap = strong_attitude['gap']
        vol_ratio = strong_attitude['vol_ratio']
        
        if tier == 'low':
            if gap >= 0.04 and vol_ratio >= 0.12:
                entry_price = auction.get('涨停价', auction['开盘价'])
                buy_timing = "竞价末段"
            else:
                entry_price = auction['开盘价']
                buy_timing = "开盘第一笔"
        else:
            if gap >= 0.03 and vol_ratio >= 0.10:
                entry_price = auction.get('涨停价', auction['开盘价'])
                buy_timing = "竞价末段"
            else:
                entry_price = auction['开盘价']
                buy_timing = "开盘第一笔"
        
        # 风控
        stop_loss = max(
            yesterday_data.get('最低价', entry_price * 0.93),
            entry_price * 0.93
        )
        take_profit = entry_price * (1.20 if tier == 'low' else 1.15)
        
        # 仓位
        if tier == 'low':
            position_size = "medium" if gap >= 0.04 else "light"
        else:
            position_size = "heavy" if gap >= 0.03 and turnover_data['real_turnover'] >= 40 else "medium"
        
        # 置信度
        confidence = 0.65
        if turnover_data['real_turnover'] >= self.config.TURNOVER_THRESHOLDS[tier]['ideal_real']:
            confidence += 0.15
        if gap >= (0.04 if tier == 'low' else 0.03):
            confidence += 0.10
        if vol_ratio >= 0.12:
            confidence += 0.05
        
        # 理由
        reason_parts = [
            f"{board_height}板{tier}位弱转强",
            f"昨日{weak_quality['weak_type']}",
            f"实际换手{turnover_data['real_turnover']:.1f}%",
            f"锁定筹码{turnover_data['locked_ratio']:.1%}",
            f"次日高开{gap*100:.1f}%",
            f"竞价量{vol_ratio*100:.1f}%"
        ]
        
        return TradeSignal(
            pattern_type=pattern_type,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=buy_timing,
            confidence=round(min(confidence, 0.95), 2),
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            reason="，".join(reason_parts),
            key_metrics={
                "连板高度": board_height,
                "位置层级": tier,
                "昨日弱类型": weak_quality['weak_type'],
                "名义换手率": f"{turnover_data['nominal_turnover']:.1f}%",
                "实际换手率": f"{turnover_data['real_turnover']:.1f}%",
                "锁定筹码占比": f"{turnover_data['locked_ratio']:.1%}",
                "次日高开": f"{gap*100:.1f}%",
                "竞价量比": f"{vol_ratio*100:.1f}%",
                "涨停用时": f"{open_confirm['time_to_limit']}分钟",
                "买点": buy_timing,
                "主要锁定股东": [h['name'] for h in turnover_data['locked_holders'][:3]]
            },
            validation_rules=[
                f"{board_height}板{tier}位(排除中位股)",
                f"实际换手≥{self.config.TURNOVER_THRESHOLDS[tier]['min_real']}%",
                f"锁定筹码占比{turnover_data['locked_ratio']:.1%}",
                f"高开{gap*100:.1f}%且竞价量{vol_ratio*100:.1f}%",
                "开盘不回踩，10分钟内涨停"
            ],
            buy_timing=buy_timing,
            turnover_data=turnover_data
        )
    
    def _minutes_from_open(self, time_str: str) -> int:
        """计算分钟数"""
        try:
            h, m = map(int, time_str.split(':')[:2])
            return (h - 9) * 60 + (m - 30)
        except:
            return 999


# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("=" * 80)
    print("弱转强策略 V2 - Tushare集成版")
    print("=" * 80)
    print("\n核心改进：")
    print("1. 连板高度分层：1-2板(低位) + 5板以上(高位)，排除3-4板中位股陷阱")
    print("2. 实际换手率：基于Tushare Pro股东数据，剔除大股东锁定筹码")
    print("3. 分层阈值：低位实际换手>25%，高位实际换手>35%")
    print("4. 6000+积分优化：每分钟500次，无总量限制")
    
    print("\n" + "=" * 80)
    print("使用示例：")
    print("""
    # 初始化策略
    strategy = WeakToStrongStrategyV2(tushare_token='your_token')
    
    # 检测弱转强
    signal = strategy.detect(
        stock_code='000001.SZ',
        stock_name='平安银行',
        board_height=2,  # 2板（低位）
        yesterday_data=pd.Series({...}),
        yesterday_tick=pd.DataFrame({...}),
        today_auction={'开盘价': 12.5, '竞价成交量': 1000000, ...},
        today_tick=pd.DataFrame({...}),
        sector_leader=True,
        total_float_shares=194000  # 流通股本194亿股
    )
    
    if signal:
        print(f"发现弱转强信号: {signal.reason}")
        print(f"实际换手率: {signal.turnover_data['real_turnover']:.1f}%")
        print(f"买点: {signal.buy_timing} @ {signal.entry_price}")
    """)
    
    print("=" * 80)
