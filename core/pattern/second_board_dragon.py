"""
二板定龙头 - 真正的龙头筛选器
核心：首板硬逻辑 + 次日资金表态 + 板块地位确立
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import loguru

from config.pattern_params import get_params

logger = loguru.logger


class PatternType(Enum):
    SECOND_BOARD_DRAGON = "二板定龙"


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
    buy_timing: str = ""  # 买点时机
    buy_strategy: str = ""  # 买点策略
    next_day_expectation: str = ""  # 次日预期


class SecondBoardDragonStrategy:
    def __init__(self, data_manager, sector_engine, mode="strict"):
        """
        data_manager: 数据管理器（DataManager）
        sector_engine: 板块热度引擎（可选）
        mode: 策略模式 - "strict"(严格模式) | "loose"(宽松模式)
        """
        self.dm = data_manager
        self.sector_engine = sector_engine
        self.mode = mode
        
        # 基础参数与严格模式参数（默认值见 config/pattern_params.py，支持网页覆盖）
        self.params = get_params("second_board_dragon")
        self.strict_params = get_params("second_board_dragon_strict")
    
    def detect_second_board_dragon(self,
                                   yesterday_zt: pd.DataFrame,      # 昨日首板池
                                   today_auction: pd.DataFrame,   # 今日竞价数据（9:25）
                                   today_tick: pd.DataFrame,      # 今日分时（实时）
                                   sector_mapping: Dict,           # 股票->板块映射
                                   today_first_board: pd.DataFrame = None  # 今日首板池（用于板块梯队）
                                   ) -> List[TradeSignal]:
        """
        二板定龙头：竞价定生死，开盘定地位
        买点：竞价末段或开盘第一笔，绝非打板！
        """
        signals = []

        # ══════════════════════════════════════════════════════════
        # 前置：统计板块数据（Layer 3/4 板块地位判断用）
        # ══════════════════════════════════════════════════════════
        sector_second_board_count = {}
        sector_first_board_count = {}
        if self.mode == "strict" and today_first_board is not None:
            if '所属行业' in today_first_board.columns:
                sector_first_board_count = today_first_board['所属行业'].value_counts().to_dict()
            elif 'L2_Industry' in today_first_board.columns:
                sector_first_board_count = today_first_board['L2_Industry'].value_counts().to_dict()
            logger.info(f"[二板定龙] 板块首板统计: {len(sector_first_board_count)}个行业有首板")

        # 统计各层过滤数量
        l0_no_board = 0
        l0_one_word = 0
        l1_skipped = 0
        l2_skipped = 0
        l3_skipped = 0
        total_checked = 0

        for _, yest_row in yesterday_zt.iterrows():
            code = str(yest_row['代码']).zfill(6)
            name = yest_row['名称']
            total_checked += 1

            # ═══════════ Layer 0: 硬性排除（零成本 — 仅用today_auction字段）═══════════
            today_row = today_auction[today_auction['代码'] == code]
            if today_row.empty:
                l0_no_board += 1
                continue
            today_row = today_row.iloc[0]

            sector = sector_mapping.get(code, '未知')
            first_board_count = sector_first_board_count.get(sector, 0)

            # L0a: 一字板排除
            first_time = str(today_row.get('首次封板时间', '')).strip()
            if first_time in ('09:25:00', '09:25'):
                l0_one_word += 1
                logger.debug(f"[二板定龙-L0] ✗ {name}({code}) 过滤: 一字板，无博弈空间")
                continue

            # L0b: 尾盘二板排除
            parsed_ft = self._parse_time(first_time)
            if parsed_ft:
                h, m = parsed_ft
                if h > 14 or (h == 14 and m >= 30):
                    l0_one_word += 1
                    logger.info(f"[二板定龙-L0] ✗ {name}({code}) 过滤: 尾盘二板({first_time})")
                    continue

            logger.info(f"[二板定龙-L1] ── 检测 {name}({code}), 板块={sector} ——")

            # ═══════════ Layer 1: 前身验证（昨日首板质量）═══════════
            if self.mode == "strict":
                skip_reason = self._should_skip_stock(yest_row, today_row, first_board_count)
                if skip_reason:
                    l1_skipped += 1
                    logger.info(f"[二板定龙-L1] ✗ {name}({code}) 过滤: {skip_reason}")
                    continue

            quality = self._check_first_board_quality(yest_row)
            if not quality['is_hard_logic']:
                l1_skipped += 1
                logger.info(f"[二板定龙-L1] ✗ {name}({code}) 过滤: 首板无硬逻辑(得分{quality['score']})")
                continue

            logger.info(f"[二板定龙-L1] ✓ {name}({code}) 通过前身验证 "
                        f"(硬逻辑:{quality['hard_logic']}, 分值{quality['score']})")

            # ═══════════ Layer 2: 资金确认（高开gap + 竞价量 + 资金态度）═══════════
            open_price = float(today_row.get('开盘价', 0) or today_row.get('open', 0))
            yest_close = float(yest_row.get('收盘价', 0) or yest_row.get('close', 0))
            gap_ratio = (open_price - yest_close) / yest_close if open_price > 0 and yest_close > 0 else 0

            # 动态gap阈值
            if self.mode == "strict":
                min_gap_dyn = self.strict_params["min_gap"]  # strict: 5%
                max_gap_dyn = self.strict_params["max_gap"]  # strict: 8%
            else:
                min_gap_dyn = self.params["min_gap"]  # normal: 3%

            l2_penalty = 0.0

            if self.mode == "strict":
                if gap_ratio < min_gap_dyn:
                    l2_skipped += 1
                    logger.info(f"[二板定龙-L2] ✗ {name}({code}) 过滤: 高开不足({gap_ratio*100:.1f}%<{min_gap_dyn*100:.0f}%)")
                    continue
                if gap_ratio > max_gap_dyn:
                    l2_skipped += 1
                    logger.info(f"[二板定龙-L2] ✗ {name}({code}) 过滤: 高开过多({gap_ratio*100:.1f}%>{max_gap_dyn*100:.0f}%)")
                    continue
            else:
                if gap_ratio < min_gap_dyn:
                    l2_skipped += 1
                    logger.info(f"[二板定龙-L2] ✗ {name}({code}) 过滤: 低开({gap_ratio*100:.1f}%<{min_gap_dyn*100:.0f}%)")
                    continue

            # 竞价量弹性评分
            auction_vol_ratio = 0
            try:
                auction_vol = float(today_row.get('竞价成交量', 0))
                yest_vol = float(yest_row.get('成交量', 1))
                if yest_vol > 0:
                    auction_vol_ratio = auction_vol / yest_vol
            except Exception:
                pass

            if auction_vol_ratio > 0:  # 有竞价数据时做弹性检查
                base_auction_min = self.strict_params.get('min_auction_vol', 0.08) if self.mode == "strict" else 0.05
                if auction_vol_ratio < base_auction_min:
                    shortfall = (base_auction_min - auction_vol_ratio) / base_auction_min
                    penalty = min(0.04, shortfall * 0.10)
                    l2_penalty += penalty
                    logger.info(f"[二板定龙-L2] ⚠ {name} 竞价量不足(量比{auction_vol_ratio*100:.1f}%<{base_auction_min*100:.0f}%), 扣{penalty:.2f}")

            attitude = self._check_fund_attitude(today_row, yest_row)
            if not attitude['is_strong_attitude']:
                l2_skipped += 1
                logger.info(f"[二板定龙-L2] ✗ {name}({code}) 过滤: 资金态度不积极(量比{attitude['auction_vol_ratio']*100:.1f}%)")
                continue

            # L2累计扣分上限
            if l2_penalty >= 0.05:
                l2_skipped += 1
                logger.info(f"[二板定龙-L2] ✗ {name}({code}) 过滤: L2累计扣分{l2_penalty:.2f}≥0.05")
                continue

            logger.info(f"[二板定龙-L2] {'✓' if l2_penalty == 0 else '⚠'} {name}({code}) 通过资金确认 "
                        f"(高开{gap_ratio*100:.1f}%|动态{min_gap_dyn*100:.0f}%, 竞价量比{attitude['auction_vol_ratio']*100:.1f}%, 扣分{l2_penalty:.2f})")

            # ═══════════ Layer 3: 质量指标 — 弹性评分 ═══════════
            l3_penalty = 0.0

            # L3a: 竞价金额弹性检查
            auction_amount = attitude.get('auction_amount', 0)
            if auction_amount > 0 and auction_amount < 5_000_000:
                l3_penalty += 0.02
                logger.info(f"[二板定龙-L3] ⚠ {name} 竞价金额偏低({auction_amount/1e4:.0f}万<500万), 扣{0.02:.2f}")

            # L3b: 市值上限
            float_cap = float(today_row.get('流通市值', 0))
            if float_cap > 150:
                l3_penalty += 0.02
                logger.info(f"[二板定龙-L3] ⚠ {name} 市值偏大({float_cap:.1f}亿>150亿), 扣{0.02:.2f}")

            # L3c: 严格模式 - 封板速度
            limit_up_time_str = str(today_row.get('首次封板时间', '')).strip()
            if self.mode == "strict" and limit_up_time_str:
                parsed_lt = self._parse_time(limit_up_time_str)
                if parsed_lt:
                    h, m = parsed_lt
                    max_h = int(self.strict_params['max_limit_up_time'][:2])
                    max_m = int(self.strict_params['max_limit_up_time'][3:5])
                    if h > max_h or (h == max_h and m > max_m):
                        l3_penalty += 0.04
                        logger.info(f"[二板定龙-L3] ⚠ {name} 封板过晚({limit_up_time_str}>{self.strict_params['max_limit_up_time']}), 扣{0.04:.2f}")

            # L3d: 严格模式 - 换手检查
            if self.mode == "strict":
                turnover = float(today_row.get('换手率', 0))
                if turnover < self.strict_params['min_turnover'] * 100:
                    l3_penalty += 0.03
                    logger.info(f"[二板定龙-L3] ⚠ {name} 换手不足({turnover:.1f}%<{self.strict_params['min_turnover']*100:.0f}%), 扣{0.03:.2f}")

            # L3e: 封单强度弹性检查
            seal_amount = float(today_row.get('封单额', 0) or today_row.get('封板资金', 0) or 0)
            base_cap = float_cap
            seal_ratio = seal_amount / (base_cap * 1e8) if base_cap > 0 and seal_amount > 0 else 0
            if seal_amount > 0 and seal_ratio < 0.01:
                l3_penalty += 0.02
                logger.info(f"[二板定龙-L3] ⚠ {name} 封单偏弱({seal_ratio*100:.2f}%<1%), 扣{0.02:.2f}")

            if l3_penalty >= 0.05:
                l3_skipped += 1
                logger.info(f"[二板定龙-L3] ✗ {name}({code}) 过滤: L3累计扣分{l3_penalty:.2f}≥0.05")
                continue

            logger.info(f"[二板定龙-L3] {'✓' if l3_penalty == 0 else '⚠'} {name}({code}) 通过质量检查 "
                        f"(市值{float_cap:.1f}亿, 扣分{l3_penalty:.2f})")

            # ═══════════ Layer 4: 板块地位 + 信号生成 ═══════════
            # 板块地位判断
            current_count = sector_second_board_count.get(sector, 0)
            if current_count >= self.params["max_sector_second_board"]:
                is_leader = False
            else:
                is_leader = True
                sector_second_board_count[sector] = current_count + 1

            entry_price, buy_timing = self._calculate_entry(
                gap_ratio, attitude['auction_vol_ratio'], today_row
            )

            if self.mode == "strict":
                if is_leader and gap_ratio >= 0.05 and first_board_count >= 2:
                    buy_strategy = "主买点: 回封时扫板（确认资金态度）"
                    next_day_expectation = "正常: 一字板或T字板（龙头溢价）"
                elif is_leader:
                    buy_strategy = "次买点: 确定为板块最强二板，可提前扫板"
                    next_day_expectation = "正常: 高开5%+（龙头预期）"
                else:
                    buy_strategy = "观望: 跟风二板，谨慎参与"
                    next_day_expectation = "低于预期: 低开或高开<3% → 立即止损"
            else:
                buy_strategy = "主买点: 回封时扫板"
                next_day_expectation = "正常预期"

            confidence = self._calculate_confidence(quality, attitude, is_leader)
            confidence -= (l2_penalty + l3_penalty)
            confidence = max(0.50, min(confidence, 0.95))

            position_size = "heavy" if is_leader and attitude['auction_vol_ratio'] > 0.15 else "medium"

            signal = TradeSignal(
                pattern_type=PatternType.SECOND_BOARD_DRAGON,
                stock_code=code,
                stock_name=name,
                trigger_time="09:25:00" if buy_timing == "竞价" else "09:30:00",
                confidence=round(confidence, 2),
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.12,
                position_size=position_size,
                reason=self._generate_reason(quality, attitude, is_leader, gap_ratio),
                key_metrics={
                    "首板质量分": quality['score'],
                    "硬逻辑": quality['hard_logic'],
                    "次日高开": f"{gap_ratio*100:.1f}%",
                    "竞价量比": f"{attitude['auction_vol_ratio']*100:.1f}%",
                    "竞价金额": f"{attitude['auction_amount']/1e4:.0f}万",
                    "板块地位": "龙头" if is_leader else "跟风",
                    "板块内排名": current_count + 1,
                    "首板助攻": f"{first_board_count}家",
                    "L2扣分": f"{l2_penalty:.2f}",
                    "L3扣分": f"{l3_penalty:.2f}",
                    "买点时机": buy_timing,
                    "买点策略": buy_strategy,
                    "次日预期": next_day_expectation
                },
                validation_rules=[
                    f"首板硬逻辑: {quality['hard_logic']}",
                    f"次日高开{gap_ratio*100:.1f}%（资金表态）",
                    f"竞价量{attitude['auction_vol_ratio']*100:.1f}%（抢筹）",
                    f"板块地位: {'龙头' if is_leader else '跟风（谨慎）'}",
                    f"首板助攻: {first_board_count}家",
                    "15分钟内涨停（盘中确认）"
                ],
                buy_timing=buy_timing,
                buy_strategy=buy_strategy,
                next_day_expectation=next_day_expectation
            )

            signals.append(signal)
            logger.info(f"[二板定龙-L4] ✓ {name}({code}) 生成信号: "
                        f"置信度{confidence:.2f}, {'龙头' if is_leader else '跟风'}, {position_size}")

        logger.info(f"[二板定龙] 检测完成: 共{len(signals)}个信号 "
                    f"(共检查{total_checked}只→无动静{l0_no_board}→一字/尾盘{l0_one_word}→"
                    f"L1过滤{l1_skipped}→L2过滤{l2_skipped}→L3过滤{l3_skipped}→通过{len(signals)})")

        signals.sort(key=lambda x: x.confidence, reverse=True)
        return signals[:3]
    
    # ==================== 核心判断方法 ====================
    
    def _should_skip_stock(self, yest_row: pd.Series, today_row: pd.Series, 
                          sector_first_board_count: int = 0) -> Optional[str]:
        """
        检查放弃信号（严格模式）
        
        放弃信号（满足任一即放弃）：
        1. 首板一字板（无换手，筹码断层）
        2. 尾盘偷鸡二板（14:30后）
        3. 板块无首板助攻（独龙难飞）
        4. 竞价无量高开（诱多陷阱）- 无竞价数据时跳过
        
        Returns:
            str: 放弃原因，None表示不放弃
        """
        if self.mode != "strict":
            return None
            
        code = str(yest_row.get('代码', '')).zfill(6)
        
        # 1. 检查首板是否一字板
        break_count = yest_row.get('开板次数', 0)
        limit_type = yest_row.get('涨停类型', '')
        turnover = yest_row.get('换手率', 0)
        
        if self.strict_params.get('skip_first_board_seal', True):
            # 一字板特征：开板次数=0 且 涨停类型为1 且 换手极低
            if break_count == 0 and ('一字' in str(limit_type) or limit_type == '1'):
                return f"首板一字板(无换手)"
            # 换手<3%视为无充分换手
            if turnover < 3.0:
                return f"首板换手不足({turnover:.1f}%<3%)"
        
        # 2. 检查是否尾盘二板
        limit_up_time = str(today_row.get('首次封板时间', '')).strip()
        if limit_up_time:
            parsed = self._parse_time(limit_up_time)
            if parsed:
                hour, minute = parsed
                skip_time = self.strict_params.get('skip_tail_board_time', '14:30')
                skip_hour = int(skip_time[:2])
                skip_minute = int(skip_time[3:5])
                if hour > skip_hour or (hour == skip_hour and minute >= skip_minute):
                    return f"尾盘二板({limit_up_time})"
        
        # 3. 检查板块是否有首板助攻
        if sector_first_board_count < self.strict_params.get('min_sector_first_board', 1):
            return f"板块无首板助攻({sector_first_board_count}家)"
        
        # 4. 检查竞价数据（如果有的话）
        auction_vol = today_row.get('竞价成交量', 0)
        if auction_vol > 0:  # 有竞价数据才检查
            yest_vol = yest_row.get('成交量', 1)
            auction_ratio = auction_vol / yest_vol if yest_vol > 0 else 0
            if auction_ratio < self.strict_params.get('min_auction_vol', 0.08):
                return f"竞价无量({auction_ratio*100:.1f}%<8%)"
        
        return None
    
    def _parse_time(self, time_str: str) -> Optional[Tuple[int, int]]:
        """
        统一解析时间字符串为 (hour, minute)
        支持格式: HHMMSS, HMMSS, HHMM, HH:MM:SS, HH:MM
        """
        if not time_str or time_str == '-':
            return None
        
        try:
            cleaned = str(time_str).strip().replace(':', '')
            
            if len(cleaned) == 6:  # HHMMSS
                return (int(cleaned[:2]), int(cleaned[2:4]))
            elif len(cleaned) == 5:  # HMMSS
                return (int(cleaned[0]), int(cleaned[1:3]))
            elif len(cleaned) == 4:  # HHMM
                return (int(cleaned[:2]), int(cleaned[2:4]))
            elif len(cleaned) == 3:  # HMM
                return (int(cleaned[0]), int(cleaned[1:3]))
        except Exception as e:
            logger.debug(f"解析时间失败: {time_str}, {e}")
        
        return None
    
    def _check_first_board_quality(self, row: pd.Series) -> Dict:
        """
        检查首板是否有"硬逻辑"——政策/业绩/重大事件驱动
        """
        seal_amount = row.get('封单额', 0)
        float_cap = row.get('流通市值', 1) * 10000
        turnover = row.get('换手率', 0)
        concept = row.get('所属概念', '')
        limit_up_time = row.get('首次封板时间', '')
        
        # 硬逻辑识别（从概念和新闻判断）
        hard_logics = []
        
        # 政策驱动
        policy_keywords = ['政策', '利好', '规划', '补贴', '国产替代']
        if any(kw in concept for kw in policy_keywords):
            hard_logics.append('政策驱动')
        
        # 业绩驱动
        if '业绩' in concept or row.get('业绩预告', '') != '':
            hard_logics.append('业绩预增')
        
        # 事件驱动
        event_keywords = ['订单', '中标', '合作', '突破', '量产']
        if any(kw in concept for kw in event_keywords):
            hard_logics.append('事件催化')
        
        # 板块龙头（首板时就是板块内第一个涨停）
        if row.get('板块内排名', 99) == 1:
            hard_logics.append('板块龙头')
        
        # 封单质量（硬逻辑必须有资金认可）
        seal_ratio = seal_amount / float_cap
        seal_score = 40 if seal_ratio > 0.15 else (30 if seal_ratio > 0.08 else 0)
        
        # 换手质量（根据市值调整）
        market_cap = row.get('总市值', 50)  # 亿元
        if market_cap < 100:  # 小盘
            turnover_score = 30 if 5 <= turnover <= 25 else 20
        elif market_cap < 500:  # 中盘
            turnover_score = 30 if 8 <= turnover <= 20 else 20
        else:  # 大盘
            turnover_score = 30 if 10 <= turnover <= 15 else 20
        
        # 涨停时间（早盘板加分）
        time_score = 0
        if limit_up_time and limit_up_time < "10:00:00":
            time_score = 30
        elif limit_up_time and limit_up_time < "11:30:00":
            time_score = 20
        
        total_score = seal_score + turnover_score + time_score + (20 if hard_logics else 0)
        
        return {
            'is_hard_logic': total_score >= 70 and len(hard_logics) > 0,
            'score': total_score,
            'hard_logic': ' + '.join(hard_logics) if hard_logics else '技术反弹',
            'seal_ratio': seal_ratio,
            'turnover': turnover
        }
    
    def _check_fund_attitude(self, today_row: pd.Series, yest_row: pd.Series) -> Dict:
        """
        检查资金次日态度——是否愿意承担隔夜风险抢筹
        这是二板定龙头的核心！
        """
        # 竞价量（最重要指标）
        auction_vol = today_row.get('竞价成交量', 0)
        yest_total_vol = yest_row.get('成交量', 1)
        auction_vol_ratio = auction_vol / yest_total_vol
        
        # 竞价金额（防止小量高价误导）
        auction_amount = today_row.get('竞价成交额', auction_vol * today_row['开盘价'])
        
        # 竞价走势（最后一分钟必须向上）
        price_trend = today_row.get('竞价价格序列', [])
        last_min_trend = price_trend[-2:] if len(price_trend) >= 2 else []
        is_end_up = len(last_min_trend) == 2 and last_min_trend[1] > last_min_trend[0]
        
        # 综合判断
        is_strong = (
            auction_vol_ratio >= self.params["min_auction_vol"] and
            auction_amount >= self.params["min_auction_amount"] and
            is_end_up
        )
        
        return {
            'is_strong_attitude': is_strong,
            'auction_vol_ratio': auction_vol_ratio,
            'auction_amount': auction_amount,
            'is_end_up': is_end_up,
            'score': auction_vol_ratio * 100 + (20 if is_end_up else 0)
        }
    
    def _calculate_entry(self, gap_ratio: float, auction_vol_ratio: float, today_row: pd.Series) -> Tuple[float, str]:
        """
        计算买点：竞价还是开盘
        """
        # 强一致：高开>5% + 竞价量>12%，直接竞价挂涨停价
        if gap_ratio > 0.05 and auction_vol_ratio > 0.12:
            return today_row['涨停价'], "竞价"
        
        # 弱转强：高开2-5%，竞价量8-12%，开盘观察第一笔
        elif 0.02 <= gap_ratio <= 0.05 and 0.08 <= auction_vol_ratio <= 0.15:
            return today_row['开盘价'], "开盘"
        
        # 其他情况：放弃或盘中确认（非核心买点）
        else:
            return today_row['最新价'], "观察"
    
    def _calculate_confidence(self, quality: Dict, attitude: Dict, is_leader: bool) -> float:
        """计算置信度"""
        base = 0.70
        base += 0.10 if quality['score'] > 80 else 0.05
        base += 0.10 if attitude['auction_vol_ratio'] > 0.15 else 0.05
        base += 0.10 if is_leader else 0
        base += 0.05 if attitude['is_end_up'] else 0
        return round(min(base, 0.95), 2)
    
    def _generate_reason(self, quality: Dict, attitude: Dict, is_leader: bool, gap: float) -> str:
        """生成交易理由"""
        status = "龙头" if is_leader else "跟风"
        strength = "强一致" if gap > 0.05 else "弱转强"
        return f"{status}二板{strength}，{quality['hard_logic']}，竞价量{attitude['auction_vol_ratio']*100:.1f}%"

# ==================== 实战口诀 ====================

"""
二板定龙头，竞价定生死
首板要硬板，换手要合理
次日高开抢，资金表了态
板块第一名，重仓往里干
跟风二板少，容易被人卡
竞价不抢筹，开盘就放弃
十五分上板，确立真龙头
"""

if __name__ == "__main__":
    print("二板定龙头策略加载完成")
    print("核心：首板硬逻辑 + 次日资金表态 + 板块地位确立")


# ==============================================================================
# 文件: core/pattern/signal_priority.py
# ==============================================================================
"""
信号优先级与互斥规则 - Signal Priority & Mutual Exclusion

职责：处理多信号冲突，确保交易计划清晰可执行

规则：
  1. 优先级排序
     弱转强(100) > 二板定龙(85) > 龙二波(70) > 首板突破(50)

  2. 互斥规则
     - 同一板块最多推荐 3 只标的
     - 同一模式最多推荐 5 只标的
     - 总推荐标的 ≤ 10 只

  3. 去重规则
     - 同一股票触发多个模式 → 保留最高优先级
     - 保留次高优先级作为"备选逻辑"
"""
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from enum import IntEnum
import loguru

logger = loguru.logger


class PatternPriority(IntEnum):
    """模式优先级（数值越大优先级越高）"""
    WEAK_TO_STRONG = 100       # 弱转强
    SECOND_BOARD_DRAGON = 85   # 二板定龙
    DRAGON_SECOND_WAVE = 70    # 龙二波
    FIRST_BOARD_BREAKOUT = 50  # 首板突破
    DIVERGENCE_CONSENSUS = 40  # 分歧转一致（未启用）
    POSITION_BATTLE = 35       # 卡位板（未启用）
    BLAST_RESEAL = 30          # 炸板回封（未启用）


# 模式名称到优先级的映射
PATTERN_PRIORITY_MAP = {
    '弱转强': PatternPriority.WEAK_TO_STRONG,
    '二板定龙': PatternPriority.SECOND_BOARD_DRAGON,
    '龙二波': PatternPriority.DRAGON_SECOND_WAVE,
    '首板突破': PatternPriority.FIRST_BOARD_BREAKOUT,
    '分歧转一致': PatternPriority.DIVERGENCE_CONSENSUS,
    '卡位板': PatternPriority.POSITION_BATTLE,
    '炸板回封': PatternPriority.BLAST_RESEAL,
}


@dataclass
class PriorityConfig:
    """优先级配置"""
    max_per_sector: int = 3          # 同一板块最多推荐标的数
    max_per_pattern: int = 5         # 同一模式最多推荐标的数
    max_total_signals: int = 10      # 总推荐标的上限
    enable_backup_logic: bool = True  # 是否保留备选逻辑


@dataclass
class RankedSignal:
    """排序后的信号"""
    pattern_type: str
    stock_code: str
    stock_name: str
    priority: int
    confidence: float
    sector_name: str = ""
    sector_heat_score: float = 0.0
    is_primary: bool = True          # 是否为主要信号
    backup_pattern: str = ""         # 备选模式（如果被去重）
    original_signal: object = None   # 原始信号对象
    lhb_adjust_note: str = ""        # Sprint F-7：龙虎榜游资信誉调整说明（如有）


class SignalPriorityManager:
    """
    信号优先级管理器

    处理多信号冲突，确保交易计划清晰可执行
    """

    def __init__(self, config: PriorityConfig = None):
        self.config = config or PriorityConfig()
        logger.info(f"[SignalPriority] 初始化完成: "
                   f"每板块最多{self.config.max_per_sector}只, "
                   f"每模式最多{self.config.max_per_pattern}只, "
                   f"总计最多{self.config.max_total_signals}只")

    def process_signals(self, patterns: Dict[str, List]) -> List[RankedSignal]:
        """
        处理所有模式信号，应用优先级和互斥规则

        Args:
            patterns: {模式名称: [信号列表]}

        Returns:
            排序后的信号列表（已去重、已限制数量）
        """
        logger.info(f"[SignalPriority] 开始处理信号，共{sum(len(v) for v in patterns.values())}个原始信号")

        # Step 1: 转换为RankedSignal并分配优先级
        ranked_signals = self._convert_to_ranked(patterns)
        logger.info(f"[SignalPriority] Step1-转换: {len(ranked_signals)}个信号")

        # Step 2: 同股票去重（保留最高优先级）
        ranked_signals = self._deduplicate_by_stock(ranked_signals)
        logger.info(f"[SignalPriority] Step2-去重: {len(ranked_signals)}个信号")

        # Step 3: 按优先级和置信度排序
        ranked_signals = self._sort_by_priority(ranked_signals)

        # Step 4: 应用板块数量限制
        ranked_signals = self._limit_by_sector(ranked_signals)

        # Step 5: 应用模式数量限制
        ranked_signals = self._limit_by_pattern(ranked_signals)

        # Step 6: 应用总数量限制
        ranked_signals = self._limit_total(ranked_signals)

        logger.info(f"[SignalPriority] 处理完成: {len(ranked_signals)}个最终信号")

        # 打印最终信号
        for i, sig in enumerate(ranked_signals):
            backup_info = f" (备选:{sig.backup_pattern})" if sig.backup_pattern else ""
            logger.info(f"[SignalPriority] {i+1}. [{sig.pattern_type}]{backup_info} "
                       f"{sig.stock_name}({sig.stock_code}) "
                       f"优先级={sig.priority} 置信度={sig.confidence:.2f} "
                       f"板块={sig.sector_name}")

        return ranked_signals

    def _convert_to_ranked(self, patterns: Dict[str, List]) -> List[RankedSignal]:
        """将原始信号转换为RankedSignal"""
        ranked = []

        for pattern_name, signals in patterns.items():
            priority = PATTERN_PRIORITY_MAP.get(pattern_name, 0)

            for signal in signals:
                stock_code = getattr(signal, 'stock_code', '')
                stock_name = getattr(signal, 'stock_name', '')
                confidence = getattr(signal, 'confidence', 0.5)
                sector_name = getattr(signal, 'l2_industry', '') or getattr(signal, 'industry', '')
                sector_heat = getattr(signal, 'sector_heat_score', 0)

                ranked.append(RankedSignal(
                    pattern_type=pattern_name,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    priority=int(priority),
                    confidence=confidence,
                    sector_name=sector_name,
                    sector_heat_score=sector_heat,
                    original_signal=signal,
                ))

        return ranked

    def _deduplicate_by_stock(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """同股票去重：保留最高优先级，记录备选模式"""
        stock_map: Dict[str, RankedSignal] = {}

        for sig in signals:
            code = sig.stock_code
            if code not in stock_map:
                stock_map[code] = sig
            else:
                existing = stock_map[code]
                if sig.priority > existing.priority:
                    # 当前信号优先级更高，替换
                    sig.backup_pattern = existing.pattern_type
                    stock_map[code] = sig
                    logger.debug(f"[SignalPriority] 去重: {sig.stock_name}({code}) "
                                f"保留[{sig.pattern_type}]优先级{sig.priority}, "
                                f"备选[{existing.pattern_type}]优先级{existing.priority}")
                elif sig.priority == existing.priority:
                    # 同优先级，保留置信度更高的
                    if sig.confidence > existing.confidence:
                        sig.backup_pattern = existing.pattern_type
                        stock_map[code] = sig
                else:
                    # 当前信号优先级更低，作为备选
                    if self.config.enable_backup_logic:
                        existing.backup_pattern = (
                            sig.pattern_type if not existing.backup_pattern
                            else f"{existing.backup_pattern}/{sig.pattern_type}"
                        )
                    logger.debug(f"[SignalPriority] 去重: {sig.stock_name}({code}) "
                                f"保留[{existing.pattern_type}]优先级{existing.priority}, "
                                f"忽略[{sig.pattern_type}]优先级{sig.priority}")

        return list(stock_map.values())

    def _sort_by_priority(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """按优先级降序、置信度降序排序"""
        return sorted(signals, key=lambda x: (x.priority, x.confidence), reverse=True)

    def _limit_by_sector(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制同一板块的标的数量"""
        sector_count: Dict[str, int] = {}
        result = []

        for sig in signals:
            sector = sig.sector_name
            if not sector:
                result.append(sig)
                continue

            count = sector_count.get(sector, 0)
            if count < self.config.max_per_sector:
                result.append(sig)
                sector_count[sector] = count + 1
            else:
                logger.debug(f"[SignalPriority] 板块限制: {sig.stock_name} "
                            f"板块[{sector}]已达上限{self.config.max_per_sector}只")

        return result

    def _limit_by_pattern(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制同一模式的标的数量"""
        pattern_count: Dict[str, int] = {}
        result = []

        for sig in signals:
            count = pattern_count.get(sig.pattern_type, 0)
            if count < self.config.max_per_pattern:
                result.append(sig)
                pattern_count[sig.pattern_type] = count + 1
            else:
                logger.debug(f"[SignalPriority] 模式限制: {sig.stock_name} "
                            f"模式[{sig.pattern_type}]已达上限{self.config.max_per_pattern}只")

        return result

    def _limit_total(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制总标的数量"""
        if len(signals) > self.config.max_total_signals:
            logger.info(f"[SignalPriority] 总数限制: {len(signals)}→{self.config.max_total_signals}")
            return signals[:self.config.max_total_signals]
        return signals

    def get_pattern_priority_order(self) -> List[str]:
        """获取模式优先级排序列表"""
        sorted_patterns = sorted(PATTERN_PRIORITY_MAP.items(),
                                key=lambda x: x[1].value, reverse=True)
        return [name for name, _ in sorted_patterns]

    def get_mutual_exclusion_groups(self, signals: List[RankedSignal]) -> Dict[str, List[RankedSignal]]:
        """
        获取互斥分组（同一板块的信号归为一组）

        Returns:
            {板块名称: [该板块的信号列表]}
        """
        groups: Dict[str, List[RankedSignal]] = {}

        for sig in signals:
            sector = sig.sector_name or '未分类'
            if sector not in groups:
                groups[sector] = []
            groups[sector].append(sig)

        return groups

    def generate_priority_report(self, signals: List[RankedSignal]) -> str:
        """生成优先级报告"""
        lines = []
        lines.append("=" * 60)
        lines.append("【信号优先级排序报告】")
        lines.append("=" * 60)

        # 按优先级分组
        priority_groups: Dict[int, List[RankedSignal]] = {}
        for sig in signals:
            if sig.priority not in priority_groups:
                priority_groups[sig.priority] = []
            priority_groups[sig.priority].append(sig)

        for priority in sorted(priority_groups.keys(), reverse=True):
            group = priority_groups[priority]
            pattern_name = group[0].pattern_type if group else "未知"
            lines.append(f"\n[{pattern_name}] 优先级={priority} ({len(group)}只):")
            for sig in group:
                backup = f" [备选:{sig.backup_pattern}]" if sig.backup_pattern else ""
                lines.append(f"  - {sig.stock_name}({sig.stock_code}) "
                           f"置信度={sig.confidence:.2f} 板块={sig.sector_name}{backup}")

        # 互斥分组
        groups = self.get_mutual_exclusion_groups(signals)
        lines.append(f"\n【板块互斥分组】({len(groups)}个板块):")
        for sector, group_signals in sorted(groups.items(),
                                            key=lambda x: len(x[1]), reverse=True):
            lines.append(f"  [{sector}] {len(group_signals)}只: "
                        f"{', '.join(s.stock_name for s in group_signals)}")

        lines.append("=" * 60)
        return "\n".join(lines)