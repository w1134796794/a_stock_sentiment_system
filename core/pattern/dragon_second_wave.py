"""
龙二波策略 - 正确的历史连板判断
核心：从每日涨停池取近期连板，非日线涨幅计算
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import loguru

from config.pattern_params import get_params

logger = loguru.logger


class PatternType(Enum):
    DRAGON_SECOND_WAVE = "龙二波"


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
    description: str = ""  # 描述字段，用于报告展示


class DragonSecondWaveStrategyV2:
    def __init__(self, data_manager, sentiment_engine, repo=None):
        self.dm = data_manager
        self.se = sentiment_engine
        if repo is None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(data_manager)
        self.repo = repo
        
        # 时间参数（近期记忆）- 默认值见 config/pattern_params.py，支持网页覆盖
        self.params = get_params("dragon_second_wave")
    
    def _parse_time(self, time_str: str) -> Optional[Tuple[int, int]]:
        """解析时间字符串 HH:MM 或 HH:MM:SS"""
        if not time_str or not str(time_str).strip():
            return None
        try:
            parts = str(time_str).strip().split(':')
            return (int(parts[0]), int(parts[1]))
        except Exception:
            return None

    def _is_valid_limit_time(self, limit_up_time: str, max_time_str: str) -> bool:
        """检查涨停时间是否在规定时间之前"""
        parsed = self._parse_time(limit_up_time)
        max_parsed = self._parse_time(max_time_str)
        if not parsed:
            return False
        if not max_parsed:
            return True
        return (parsed[0] < max_parsed[0]) or (parsed[0] == max_parsed[0] and parsed[1] <= max_parsed[1])

    def _add_suffix(self, stock_code: str) -> str:
        """为股票代码添加交易所后缀"""
        code = str(stock_code).strip()
        if '.' not in code:
            code = code.zfill(6)
            if code.startswith('6'):
                return f"{code}.SH"
            else:
                return f"{code}.SZ"
        return code

    def _get_daily_data(self, stock_code: str, date_str: str) -> Optional[Dict]:
        """获取日线数据并计算 MA / 量比 / 5日涨幅"""
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            end_date = date_str
            start_date = (dt - timedelta(days=30)).strftime("%Y%m%d")

            if hasattr(self.dm, 'get_stock_daily'):
                ts_code = self._add_suffix(stock_code.split('.')[0].zfill(6))
                df = self.repo.get_stock_daily(ts_code, start_date, end_date)

                if not df.empty and len(df) >= 10:
                    df = df.sort_values('trade_date')

                    df['ma5'] = df['close'].rolling(window=5).mean()
                    df['ma10'] = df['close'].rolling(window=10).mean()

                    latest = df.iloc[-1]
                    prev_5d = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

                    rise_5d = (latest['close'] - prev_5d['close']) / prev_5d['close'] if prev_5d['close'] > 0 else 0

                    if len(df) >= 6:
                        avg_vol_5d = df.iloc[-6:-1]['vol'].mean()
                        volume_ratio = latest['vol'] / avg_vol_5d if avg_vol_5d > 0 else 0
                    else:
                        volume_ratio = 0

                    return {
                        'rise_5d': rise_5d,
                        'volume_ratio': volume_ratio,
                        'close': latest['close'],
                        'ma5': latest['ma5'],
                        'ma10': latest['ma10'],
                    }
        except Exception as e:
            logger.debug(f"[龙二波] 获取日线数据失败 {stock_code}: {e}")

        return None

    def _filter_layer_0_hard_exclusions(self, today_data: pd.Series) -> Optional[str]:
        """
        Layer 0: 硬性排除（零成本 — 仅用 today_data 自带字段）

        排除条件（按判断成本从低到高）：
        1. 今日未涨停 / 非首板
        2. 一字板（无换手）
        3. 尾盘板（14:30后）
        """
        change = today_data.get('涨跌幅', 0)
        if isinstance(change, str):
            change = float(change.replace('%', ''))
        if change < 9.5:
            return f"涨幅不足({change:.2f}%<9.5%)"

        today_consecutive = 1
        for col in ['连板数', '连板', 'consecutive', 'limit_up_days', '连板天数']:
            if col in today_data.index:
                today_consecutive = int(today_data[col])
                break
        if today_consecutive > 1:
            return f"非首板(连板{today_consecutive})"

        break_count = today_data.get('开板次数', 0)
        limit_type = str(today_data.get('涨停类型', ''))
        if break_count == 0 and ('一字' in limit_type or limit_type == '1'):
            return "一字板(无换手)"

        limit_up_time = str(today_data.get('首次封板时间', '')).strip()
        parsed = self._parse_time(limit_up_time)
        if parsed:
            hour, minute = parsed
            if hour > 14 or (hour == 14 and minute >= 30):
                return f"尾盘板({limit_up_time})"

        return None

    def _calculate_sector_effect_score(self, is_hot_sector: bool,
                                         sector_limit_up_count: int = 0) -> float:
        """
        Layer 4 评分：板块效应（加分项，非硬排除）

        评分逻辑：
        - 板块涨停家数 → 板块强度分（0.00 ~ 0.06）
        - 是否热点板块 → 热点分（0.00 ~ 0.04）

        Returns:
            float: 板块效应评分（0.00 ~ 0.10）
        """
        score = 0.0

        if sector_limit_up_count >= 8:
            score += 0.06
        elif sector_limit_up_count >= 5:
            score += 0.04
        elif sector_limit_up_count >= 3:
            score += 0.02

        if is_hot_sector:
            score += 0.04

        return min(score, 0.10)

    def _get_dynamic_thresholds(self, board_height: int, days_since_peak: int,
                                 is_hot_sector: bool) -> Tuple[float, float]:
        """
        根据市场上下文动态调整量能/封单阈值

        调整因素：
        1. 连板高度越高 → 龙越真 → 阈值越宽松（资金记忆更强）
        2. 调整天数越短 → 记忆越新鲜 → 阈值越宽松
        3. 热点板块 → 情绪助攻 → 阈值越宽松

        Returns:
            (min_vol_ratio, min_seal_ratio): 调整后的阈值
        """
        base_vol = self.params['min_volume_ratio']      # 1.5
        base_seal = self.params['min_seal_ratio']        # 0.02

        # 因子1: 连板高度调整
        if board_height >= 7:
            vol_adj = -0.30
            seal_adj = -0.008
        elif board_height >= 5:
            vol_adj = -0.20
            seal_adj = -0.005
        elif board_height >= 3:
            vol_adj = -0.10
            seal_adj = -0.003
        else:
            vol_adj = 0.0
            seal_adj = 0.0

        # 因子2: 调整天数调整
        if days_since_peak <= 5:
            vol_adj += -0.15
            seal_adj += -0.003
        elif days_since_peak <= 10:
            vol_adj += -0.05
            seal_adj += -0.001

        # 因子3: 板块热度调整
        if is_hot_sector:
            vol_adj += -0.10
            seal_adj += -0.003

        dyn_vol = max(0.8, base_vol + vol_adj)
        dyn_seal = max(0.005, base_seal + seal_adj)

        return dyn_vol, dyn_seal

    def detect_second_wave(self,
                          stock_code: str,
                          stock_name: str,
                          today_str: str,
                          recent_zt_pools: Dict[str, pd.DataFrame],  # 近20日每日涨停池
                          today_data: pd.Series,
                          sector_hot: bool,
                          hist_data: pd.DataFrame = None,
                          sector_info: Dict = None) -> Optional[TradeSignal]:
        """
        四层优先级过滤管线 — 检测龙二波机会

        设计原则：先确认"今天是不是首板"（零成本），再查历史"是不是真龙"（核心），
        最后用技术指标验证"二波是否有效"（资金确认+质量过滤）。

        ┌──────────────────────────────────────────────────────────────┐
        │ Layer 0: 硬性排除（零成本 — 仅用 today_data 自带字段）       │
        │   ├── 今日未涨停 (pct_chg < 9.5%)                            │
        │   ├── 非首板 (连板数 > 1)                                    │
        │   ├── 一字板（无换手）                                       │
        │   └── 尾盘板（14:30后）                                      │
        ├──────────────────────────────────────────────────────────────┤
        │ Layer 1: 前身验证（策略核心 — 扫描历史涨停池 + 日线涨幅）     │
        │   ├── 双轨制第一波识别（连板 / 5日涨幅 / 10日涨幅 / 涨停次数）│
        │   ├── 调整期时间窗口（2-15个交易日）                          │
        │   ├── 第一波高度确认（真龙标准）                              │
        │   └── 调整期形态质量（深度5%-30% + MA10/MA20支撑）           │
        ├──────────────────────────────────────────────────────────────┤
        │ Layer 2: 技术确认（拉取日线数据 — 新增）                      │
        │   ├── 启动日量能确认（量比 ≥ 1.5 → 资金记忆苏醒）            │
        │   ├── 封单强度确认（封单 ≥ 2% → 市场认可）                   │
        │   └── MA5/MA10突破确认（低位启动确认）                        │
        ├──────────────────────────────────────────────────────────────┤
        │ Layer 3: 质量指标（硬性过滤 — 新增）                          │
        │   ├── 涨停时间 ≤ 10:30（跟风板排除）                          │
        │   ├── 开板次数 ≤ 1                                           │
        │   ├── 流通市值 ≤ 100亿                                       │
        │   └── 5日涨幅 ≤ 15%（低位启动）                               │
        ├──────────────────────────────────────────────────────────────┤
        │ Layer 4: 板块效应评分 + 信号生成                              │
        │   ├── 板块涨停家数 → 板块强度分                               │
        │   ├── 是否热点板块 → 热点分                                   │
        │   ├── 综合置信度（基础0.60 + 各层加分）                       │
        │   └── TradeSignal 生成                                       │
        └──────────────────────────────────────────────────────────────┘
        """
        code_padded = str(stock_code).split('.')[0].zfill(6)
        name = stock_name

        # ══════════════════════════════════════════════════════════
        # Layer 0: 硬性排除（零成本 — 仅用 today_data 自带字段）
        # ══════════════════════════════════════════════════════════
        skip_reason = self._filter_layer_0_hard_exclusions(today_data)
        if skip_reason:
            logger.debug(f"[龙二波-L0] ✗ {name}({code_padded}) 排除: {skip_reason}")
            return None

        limit_up_time = str(today_data.get('首次封板时间', '')).strip()
        break_count = int(today_data.get('开板次数', 0))
        float_cap = float(today_data.get('流通市值', 0)) / 100000000
        if isinstance(today_data.get('流通市值', 0), str):
            float_cap = float(str(today_data.get('流通市值', '')).replace('亿', ''))

        logger.info(f"[龙二波-L0] ✓ {name}({code_padded}) 通过硬性排除 "
                    f"(首板 {limit_up_time}封, 开板{break_count}次, 市值{float_cap:.1f}亿)")

        # ══════════════════════════════════════════════════════════
        # Layer 1: 前身验证（策略核心 — 扫描历史涨停池）
        # ══════════════════════════════════════════════════════════
        consecutive_record = self._rebuild_consecutive_from_pools(
            code_padded, recent_zt_pools, hist_data
        )

        if not consecutive_record['is_valid']:
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 第一波记录无效 "
                        f"- {consecutive_record.get('reason', '未知原因')}")
            return None

        first_wave_info = consecutive_record['first_wave']
        wave_type = first_wave_info.get('wave_type', 'consecutive')

        # 1a. 调整期时间窗口
        days_since_peak = self._calculate_days_since_peak(
            first_wave_info['peak_date'], today_str
        )

        if days_since_peak > self.params["max_adjust_days"] + 5:
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 第一波距今太久 "
                        f"({days_since_peak}天 > {self.params['max_adjust_days'] + 5}天)")
            return None

        if days_since_peak < self.params["min_adjust_days"]:
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 仅{days_since_peak}天，未形成有效调整期")
            return None

        # 1b. 第一波高度确认（真龙标准 — 双轨制）
        max_boards = first_wave_info['max_boards']
        rise_5d = first_wave_info.get('rise_5d', 0)
        rise_10d = first_wave_info.get('rise_10d', 0)
        limit_up_count = first_wave_info.get('limit_up_count', 0)

        is_consecutive_valid = self.params["min_first_wave"] <= max_boards <= self.params["max_first_wave"]
        is_rise_5d_valid = rise_5d >= self.params["min_rise_5d"]
        is_rise_10d_valid = rise_10d >= self.params["min_rise_10d"]
        is_limit_up_count_valid = limit_up_count >= self.params["min_limit_up_count"]

        if not (is_consecutive_valid or is_rise_5d_valid or is_rise_10d_valid or is_limit_up_count_valid):
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 第一波强度不符合 "
                        f"(连板{max_boards}, 5日{rise_5d*100:.1f}%, 10日{rise_10d*100:.1f}%, 涨停{limit_up_count}次)")
            return None

        passed_criteria = []
        if is_consecutive_valid:
            passed_criteria.append(f"连板{max_boards}板")
        if is_rise_5d_valid:
            passed_criteria.append(f"5日{rise_5d*100:.1f}%")
        if is_rise_10d_valid:
            passed_criteria.append(f"10日{rise_10d*100:.1f}%")
        if is_limit_up_count_valid:
            passed_criteria.append(f"{limit_up_count}次涨停")

        # 1c. 调整期形态质量（深度 + MA支撑）
        adjust_period = self._get_adjust_period(
            stock_code, first_wave_info['peak_date'], today_str
        )

        if not adjust_period:
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 无法获取调整期数据")
            return None

        if not self._check_adjust_quality(adjust_period):
            logger.info(f"[龙二波-L1] ✗ {name}({code_padded}) 过滤: 调整期质量不符合")
            return None

        actual_adjust_days = adjust_period.get('days', days_since_peak)
        decline_days = adjust_period.get('decline_days', actual_adjust_days // 2)
        consolidation_days = adjust_period.get('consolidation_days', actual_adjust_days - decline_days)

        logger.info(f"[龙二波-L1] ✓ {name}({code_padded}) 通过前身验证 "
                    f"({', '.join(passed_criteria)}, 调整{actual_adjust_days}天(跌{decline_days}+震{consolidation_days}), "
                    f"深度{adjust_period.get('depth', 0)*100:.1f}%)")

        # ══════════════════════════════════════════════════════════
        # Layer 2: 技术确认（量能/封单 → 弹性评分，非硬排除）
        # ══════════════════════════════════════════════════════════
        daily_data = self._get_daily_data(stock_code, today_str)
        vol_ratio = 0.0
        seal_ratio = 0.0
        ma_breakthrough = False
        layer2_penalty = 0.0
        layer2_details = []

        if not daily_data:
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 无日线数据")
            return None

        # 量能数据
        if 'volume_ratio' in daily_data:
            vol_ratio = daily_data['volume_ratio']
        else:
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 无量比数据")
            return None

        # 动态阈值
        dyn_vol_min, dyn_seal_min = self._get_dynamic_thresholds(
            max_boards, days_since_peak, sector_hot
        )

        # 2a. 量能检查 — 弹性
        if vol_ratio < 0.8:
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 缩量启动(量比{vol_ratio:.2f}<0.8)")
            return None

        if vol_ratio > self.params['volume_abs_max']:
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 异常放量(量比{vol_ratio:.2f}>{self.params['volume_abs_max']})")
            return None

        vol_target = dyn_vol_min
        if vol_ratio < vol_target:
            vol_shortfall = (vol_target - vol_ratio) / vol_target
            penalty = min(0.05, vol_shortfall * 0.10)  # 最高扣0.05
            layer2_penalty += penalty
            layer2_details.append(f"量能偏弱(量比{vol_ratio:.2f}<动态阈值{vol_target:.2f}, 扣{penalty:.2f})")

        # 2b. 封单强度 — 弹性
        seal_amount = float(today_data.get('封单额', 0) or
                           today_data.get('封板资金', 0) or
                           today_data.get('封单金额', 0))
        float_cap_raw = float(today_data.get('流通市值', 0))

        free_float_cap = 0
        if hasattr(self.dm, 'get_stock_daily_basic'):
            try:
                daily_basic_data = self.repo.get_stock_daily_basic(code_padded, today_str)
                if daily_basic_data:
                    free_share = daily_basic_data.get('free_share', 0)
                    close_price = daily_basic_data.get('close', 0)
                    free_float_cap = free_share * close_price * 10000
            except Exception:
                pass

        base_cap = free_float_cap if free_float_cap > 0 else float_cap_raw
        seal_ratio = seal_amount / base_cap if base_cap > 0 and seal_amount > 0 else 0

        if seal_ratio < dyn_seal_min:
            seal_shortfall = (dyn_seal_min - seal_ratio) / dyn_seal_min
            penalty = min(0.05, seal_shortfall * 0.15)
            layer2_penalty += penalty
            layer2_details.append(f"封单偏弱({seal_ratio*100:.2f}%<动态阈值{dyn_seal_min*100:.1f}%, 扣{penalty:.2f})")

        # 2c. MA5/MA10突破确认（低位启动）— 硬性要求
        if all(k in daily_data for k in ['close', 'ma5', 'ma10']):
            if daily_data['close'] > daily_data['ma5'] and daily_data['close'] > daily_data['ma10']:
                ma_breakthrough = True

        if not ma_breakthrough:
            close_val = daily_data.get('close', 0)
            ma5_val = daily_data.get('ma5', 0)
            ma10_val = daily_data.get('ma10', 0)
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 未突破MA5/MA10 "
                        f"(close:{close_val:.2f}, ma5:{ma5_val:.2f}, ma10:{ma10_val:.2f})")
            return None

        # 累计扣分超过阈值 → 淘汰
        if layer2_penalty >= 0.06:
            logger.info(f"[龙二波-L2] ✗ {name}({code_padded}) 过滤: 累计扣分{layer2_penalty:.2f}≥0.06 "
                        f"({'; '.join(layer2_details)})")
            return None

        volume_pass = vol_ratio <= 3.0

        if layer2_penalty > 0:
            logger.info(f"[龙二波-L2] ⚠ {name}({code_padded}) 技术确认扣分{layer2_penalty:.2f} "
                        f"(量比{vol_ratio:.2f}|动态{vol_target:.1f}, 封单{seal_ratio*100:.2f}%|动态{dyn_seal_min*100:.1f}%, "
                        f"MA突破) [{'; '.join(layer2_details)}]")
        else:
            logger.info(f"[龙二波-L2] ✓ {name}({code_padded}) 通过技术确认 "
                        f"(量比{vol_ratio:.2f}|动态{vol_target:.1f}, 封单{seal_ratio*100:.2f}%|动态{dyn_seal_min*100:.1f}%, "
                        f"MA突破, 量能{'健康' if volume_pass else '偏大'})")

        # ══════════════════════════════════════════════════════════
        # Layer 3: 质量指标（硬性过滤 — 新增）
        # ══════════════════════════════════════════════════════════
        layer3_fail = []

        # # 3a. 涨停时间
        # if not self._is_valid_limit_time(limit_up_time, self.params['max_limit_up_time']):
        #     layer3_fail.append(f"涨停时间过晚({limit_up_time}>{self.params['max_limit_up_time']})")

        # 3b. 开板次数
        if break_count > self.params['max_break_count']:
            layer3_fail.append(f"开板过多({break_count}次)")

        # 3c. 流通市值
        if float_cap > self.params['max_float_cap']:
            layer3_fail.append(f"市值过大({float_cap:.1f}亿)")

        # 3d. 5日涨幅
        # if daily_data and 'rise_5d' in daily_data:
        #     if daily_data['rise_5d'] >= self.params['max_5d_rise']:
        #         layer3_fail.append(f"5日涨幅过高({daily_data['rise_5d']*100:.1f}%)")

        if layer3_fail:
            logger.info(f"[龙二波-L3] ✗ {name}({code_padded}) 过滤: {'; '.join(layer3_fail)}")
            return None

        logger.info(f"[龙二波-L3] ✓ {name}({code_padded}) 通过质量指标 "
                    f"(时间{limit_up_time}, 开板{break_count}次, 市值{float_cap:.1f}亿, "
                    f"5日涨幅{daily_data.get('rise_5d', 0)*100:.1f}%)")

        # ══════════════════════════════════════════════════════════
        # Layer 4: 板块效应评分 + 信号生成
        # ══════════════════════════════════════════════════════════
        sector_limit_up_count = sector_info.get('stats', {}).get('涨停家数', 0) if sector_info else 0
        sector_score = self._calculate_sector_effect_score(sector_hot, sector_limit_up_count)

        # 计算动态置信度（Phase 3：confidence_mode=deduction 走统一扣分制，默认 legacy 不变）
        early_seal = 1 if (limit_up_time and limit_up_time <= '09:40') else 0
        layer2_clean = 1 if (layer2_penalty or 0) <= 0 else 0

        conf_breakdown = None
        res = None
        if self.params.get("confidence_mode", "legacy") == "deduction":
            from core.scoring.confidence_scorer import score_or_none
            res = score_or_none("dragon_second_wave", {
                "max_boards": max_boards,
                "days_since_peak": days_since_peak,
                "seal_ratio": seal_ratio,
                "break_count": break_count,
                "early_seal": early_seal,
                "sector_score": sector_score,
                "layer2_clean": layer2_clean,
            })

        if res is not None:
            confidence = res.value
            conf_breakdown = res.to_dict()
        else:
            # legacy：基础分 0.60 + 阶梯/连续加分 − L2扣分，封顶 0.95
            confidence = 0.60  # 基础分（通过Layer 1/2/3已证质量）
            if max_boards >= 5:
                confidence += 0.06  # 5板以上真龙
            elif max_boards >= 3:
                confidence += 0.04  # 3-4板龙
            if days_since_peak <= 7:
                confidence += 0.04  # 调整7天以内，记忆新鲜
            elif days_since_peak <= 10:
                confidence += 0.02  # 调整7-10天
            confidence += min(actual_adjust_days * 0.002, 0.02)  # 调整天数恰到好处
            tech_bonus = min(seal_ratio * 2, 0.04)  # 封单强度
            if volume_pass:
                excess_vol = daily_data.get('volume_ratio', 0) - vol_target
                tech_bonus += min(max(excess_vol, 0) * 0.02, 0.03)  # 量能超预期
            confidence += tech_bonus
            confidence -= layer2_penalty  # 减去L2扣分
            if break_count == 0:
                confidence += 0.02
            if limit_up_time and limit_up_time <= '09:40':
                confidence += 0.01
            confidence += sector_score  # 板块效应分
            confidence = min(confidence, 0.95)

        # 确定仓位
        if sector_hot and sector_limit_up_count >= 5:
            position_size = "heavy"
        elif sector_hot:
            position_size = "medium"
        else:
            position_size = "light"

        # 构建龙类型描述
        if wave_type == 'consecutive' and max_boards >= 3:
            wave_type_desc = "连板"
            wave_detail = f"{max_boards}连板"
        else:
            wave_type_desc = "趋势"
            wave_detail = f"{limit_up_count}次涨停"

        adjust_desc = f"调整{actual_adjust_days}天(下跌{decline_days}+震荡{consolidation_days})"
        description = f"龙二波+{wave_detail}{wave_type_desc}龙+{adjust_desc}+深度{adjust_period['depth']*100:.1f}%"

        reason_parts = [
            f"龙二波+{wave_detail}{wave_type_desc}龙",
            adjust_desc,
            f"深度{adjust_period['depth']*100:.1f}%",
            f"量比{vol_ratio:.1f}",
            f"封单{seal_ratio*100:.1f}%",
        ]
        if ma_breakthrough:
            reason_parts.append("突破均线")

        key_metrics = {
            "第一波强度": f"{max_boards}板",
            "第一波类型": wave_type_desc,
            "第一波日期": f"{first_wave_info['start_date']}至{first_wave_info['peak_date']}",
            "调整天数": actual_adjust_days,
            "下跌天数": decline_days,
            "震荡天数": consolidation_days,
            "调整深度": f"{adjust_period['depth']*100:.1f}%",
            "支撑均线": f"MA10:{adjust_period['ma10']:.2f}",
            "启动量比": f"{vol_ratio:.2f}",
            "封单强度": f"{seal_ratio*100:.1f}%",
            "涨停时间": limit_up_time,
            "板块效应": f"{sector_score*100:.0f}分(板块涨停{sector_limit_up_count}家)",
        }
        if conf_breakdown:
            key_metrics["置信扣分明细"] = conf_breakdown
        if daily_data and 'rise_5d' in daily_data:
            key_metrics["5日涨幅"] = f"{daily_data['rise_5d']*100:.1f}%"
            key_metrics["均线突破"] = "是"

        validation_rules = [
            f"近{self.params['max_adjust_days']}日内{max_boards}连板（真龙）",
            f"{adjust_desc}（记忆未散）",
            f"回踩MA10获支撑（{adjust_period['ma10']:.2f}）",
            f"放量启动(量比{vol_ratio:.2f})",
            f"封单{seal_ratio*100:.1f}%",
            f"涨停时间{limit_up_time}",
            "板块热度未退" if sector_hot else "板块已冷（风险）",
        ]

        if sector_hot:
            buy_strategy = "主买点: 回封时扫板（确认二波启动）"
        else:
            buy_strategy = "次买点: 次日竞价确认（非热点需额外验证）"

        if sector_hot and sector_limit_up_count >= 5:
            next_day_expectation = "超预期: 一字板或T字板（板块共振）"
        elif sector_hot:
            next_day_expectation = "正常: 高开3%-7%（龙记忆+板块热度）"
        else:
            next_day_expectation = "低于预期: 高开<3%（需竞价确认）"

        logger.info(f"[龙二波-L4] ✓ {name}({code_padded}) 生成信号: "
                    f"置信度{confidence:.2f}, 仓位{position_size}, "
                    f"板块效应{sector_score*100:.0f}分, "
                    f"{'热点' if sector_hot else '非热点'}")

        return TradeSignal(
            pattern_type=PatternType.DRAGON_SECOND_WAVE,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=limit_up_time,
            confidence=confidence,
            entry_price=today_data.get('涨停价', 0),
            stop_loss=adjust_period['ma10'] * 0.97,
            take_profit=today_data.get('涨停价', 0) * 1.15,
            position_size=position_size,
            reason="+".join(reason_parts),
            key_metrics=key_metrics,
            validation_rules=validation_rules,
            description=description
        )
    
    # ==================== 核心方法：双轨制第一波判断 ====================

    def _rebuild_consecutive_from_pools(self,
                                       stock_code: str,
                                       recent_pools: Dict[str, pd.DataFrame],
                                       hist_data: pd.DataFrame = None) -> Dict:
        """
        双轨制判断第一波：
        轨道1：连板高度（3板以上）
        轨道2：累计涨幅（5日25%或10日40%）
        轨道3：涨停次数（10天内4次以上）
        
        简化逻辑：直接从涨停池的连板数字段获取最大连板数
        """
        dates = sorted(recent_pools.keys())
        logger.debug(f"[{stock_code}] 检查近{len(dates)}天涨停池")

        # 收集该股票在所有涨停池中的连板数
        max_boards = 0
        peak_date = None
        first_date = None
        zt_dates = []
        
        for date in dates:
            pool = recent_pools[date]
            if pool.empty:
                continue

            # 兼容不同的列名
            code_col = None
            if '代码' in pool.columns:
                code_col = '代码'
            elif 'Code' in pool.columns:
                code_col = 'Code'
            elif 'ts_code' in pool.columns:
                code_col = 'ts_code'

            if code_col is None:
                continue

            # 标准化代码格式
            pool[code_col] = pool[code_col].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
            
            # 查找该股票
            stock_row = pool[pool[code_col] == stock_code]
            if not stock_row.empty:
                zt_dates.append(date)
                
                # 获取连板数（支持不同字段名）
                consecutive_col = None
                for col in ['连板数', '连板', 'consecutive', 'limit_up_days', '连板天数']:
                    if col in stock_row.columns:
                        consecutive_col = col
                        break
                
                if consecutive_col:
                    boards = int(stock_row[consecutive_col].iloc[0])
                    if boards > max_boards:
                        max_boards = boards
                        peak_date = date
                        # 计算起涨日（假设连板N天，则起涨日是N-1天前）
                        try:
                            peak_dt = datetime.strptime(date, "%Y%m%d")
                            first_dt = peak_dt - timedelta(days=(boards - 1))
                            first_date = first_dt.strftime("%Y%m%d")
                        except:
                            first_date = date

        logger.debug(f"[{stock_code}] 涨停日期: {zt_dates}, 最大连板: {max_boards}")

        # ========== 双轨制判断 ==========
        
        # 轨道1：连板高度检查
        consecutive_valid = self.params["min_first_wave"] <= max_boards <= self.params["max_first_wave"]
        
        # 轨道2&3：涨幅和涨停次数检查（需要历史数据）
        rise_valid = False
        limit_up_count_valid = False
        rise_stats = {}
        
        logger.debug(f"[{stock_code}] 双轨制判断: hist_data={'有' if hist_data is not None and not hist_data.empty else '无'}, "
                    f"连板有效={consecutive_valid}")
        
        if hist_data is not None and not hist_data.empty:
            logger.debug(f"[{stock_code}] 开始计算涨幅统计, 历史数据行数={len(hist_data)}, 列={list(hist_data.columns)}")
            rise_stats = self._get_rise_stats(stock_code, hist_data)
            logger.debug(f"[{stock_code}] 涨幅统计结果: {rise_stats}")
            if rise_stats.get('is_valid'):
                rise_valid = True
                limit_up_count_valid = rise_stats.get('limit_up_count', 0) >= self.params["min_limit_up_count"]
        
        # 综合判断：满足任一轨道即有效
        is_valid_wave = consecutive_valid or rise_valid or limit_up_count_valid
        
        if not is_valid_wave:
            reasons = []
            if not consecutive_valid:
                reasons.append(f"连板{max_boards}板(要求{self.params['min_first_wave']}-{self.params['max_first_wave']}板)")
            if hist_data is not None and not rise_valid:
                reasons.append(f"5日涨幅{rise_stats.get('rise_5d', 0)*100:.1f}%(要求>25%)")
                reasons.append(f"10日涨幅{rise_stats.get('rise_10d', 0)*100:.1f}%(要求>40%)")
            if hist_data is not None and not limit_up_count_valid:
                reasons.append(f"涨停次数{rise_stats.get('limit_up_count', 0)}次(要求>={self.params['min_limit_up_count']}次)")
            
            return {'is_valid': False, 'reason': '不满足任何第一波标准: ' + '; '.join(reasons)}

        # 确定第一波类型和关键日期
        if consecutive_valid:
            # 优先使用连板数据
            wave_type = 'consecutive'
            # peak_date 和 first_date 已在前面计算
            if peak_date is None:
                peak_date = dates[-1] if dates else ''
            if first_date is None:
                first_date = dates[0] if dates else ''
        elif rise_valid or limit_up_count_valid:
            # 使用涨幅数据
            wave_type = 'rise'
            peak_date = rise_stats.get('peak_date', dates[-1] if dates else '')
            first_date = rise_stats.get('start_date', dates[0] if dates else '')
            max_boards = max(max_boards, 3)  # 非连板但至少算3板强度
        else:
            return {'is_valid': False, 'reason': '无法确定第一波类型'}

        # 检查是否是近期这一波（非开头几天）
        if dates:
            today = datetime.strptime(dates[-1], "%Y%m%d")
            try:
                peak = datetime.strptime(peak_date, "%Y%m%d")
                days_since_peak = (today - peak).days
            except:
                days_since_peak = 0

            if days_since_peak > self.params["max_adjust_days"] + 5:
                return {'is_valid': False, 'reason': f'第一波距今太久({days_since_peak}天)'}

        logger.debug(f"[{stock_code}] 第一波类型: {wave_type}, 连板: {max_boards}板, 起涨日: {first_date}, 见顶日: {peak_date}")

        return {
            'is_valid': True,
            'first_wave': {
                'max_boards': max_boards,
                'start_date': first_date,
                'peak_date': peak_date,
                'zt_dates': zt_dates if wave_type == 'consecutive' else [],
                'wave_type': wave_type,
                'rise_5d': rise_stats.get('rise_5d', 0),
                'rise_10d': rise_stats.get('rise_10d', 0),
                'limit_up_count': rise_stats.get('limit_up_count', len(zt_dates))
            },
            'all_zt_dates': zt_dates
        }

    def _get_rise_stats(self, stock_code: str, hist_data: pd.DataFrame) -> Dict:
        """
        计算近期涨幅统计 - 用于双轨制判断
        
        核心逻辑：在历史数据中找出任意连续5日/10日的最大涨幅，
        以及该时间段内的涨停次数
        
        返回：
            is_valid: 是否满足涨幅标准
            rise_5d: 任意连续5日最大累计涨幅
            rise_10d: 任意连续10日最大累计涨幅
            limit_up_count: 最佳连续期间内的涨停次数
            start_date: 最佳连续期间的起涨日
            peak_date: 最佳连续期间的见顶日
        """
        if hist_data.empty or len(hist_data) < 5:
            return {'is_valid': False, 'reason': '历史数据不足'}
        
        # 确保按日期排序（升序）
        hist = hist_data.copy()
        if 'trade_date' in hist.columns:
            hist = hist.sort_values('trade_date').reset_index(drop=True)
        
        # 获取涨跌幅列名
        pct_col = None
        if 'pct_chg' in hist.columns:
            pct_col = 'pct_chg'
        elif 'pct_change' in hist.columns:
            pct_col = 'pct_change'
        elif 'change_pct' in hist.columns:
            pct_col = 'change_pct'
        
        # ========== 计算任意连续5日最大涨幅 ==========
        max_rise_5d = 0
        best_5d_start_idx = 0
        best_5d_end_idx = 0
        
        if len(hist) >= 5:
            for i in range(len(hist) - 4):
                start_close = hist.iloc[i]['close']
                end_close = hist.iloc[i + 4]['close']
                rise = (end_close - start_close) / start_close
                if rise > max_rise_5d:
                    max_rise_5d = rise
                    best_5d_start_idx = i
                    best_5d_end_idx = i + 4
        
        # ========== 计算任意连续10日最大涨幅 ==========
        max_rise_10d = 0
        best_10d_start_idx = 0
        best_10d_end_idx = 0
        
        if len(hist) >= 10:
            for i in range(len(hist) - 9):
                start_close = hist.iloc[i]['close']
                end_close = hist.iloc[i + 9]['close']
                rise = (end_close - start_close) / start_close
                if rise > max_rise_10d:
                    max_rise_10d = rise
                    best_10d_start_idx = i
                    best_10d_end_idx = i + 9
        
        # ========== 确定最佳连续期间（5日或10日中涨幅更大的）==========
        min_rise_5d = self.params.get("min_rise_5d", 0.25)
        min_rise_10d = self.params.get("min_rise_10d", 0.40)
        
        # 选择满足条件且涨幅更大的期间
        best_start_idx = 0
        best_end_idx = 0
        best_period = ''
        
        if max_rise_10d >= min_rise_10d and max_rise_10d > max_rise_5d:
            # 10日涨幅更大且满足条件
            best_start_idx = best_10d_start_idx
            best_end_idx = best_10d_end_idx
            best_period = '10日'
        elif max_rise_5d >= min_rise_5d:
            # 5日涨幅满足条件
            best_start_idx = best_5d_start_idx
            best_end_idx = best_5d_end_idx
            best_period = '5日'
        elif max_rise_10d > 0:
            # 都不满足，但选10日作为参考
            best_start_idx = best_10d_start_idx
            best_end_idx = best_10d_end_idx
            best_period = '10日'
        else:
            # 选5日作为参考
            best_start_idx = best_5d_start_idx
            best_end_idx = best_5d_end_idx
            best_period = '5日'
        
        # ========== 统计最佳期间内的涨停次数 ==========
        best_period_data = hist.iloc[best_start_idx:best_end_idx + 1]
        limit_up_count = 0
        
        if pct_col:
            limit_up_count = len(best_period_data[best_period_data[pct_col] > 9.5])
        else:
            # 手动计算涨停次数
            for i in range(1, len(best_period_data)):
                pct_change = (best_period_data.iloc[i]['close'] - best_period_data.iloc[i-1]['close']) / best_period_data.iloc[i-1]['close'] * 100
                if pct_change > 9.5:
                    limit_up_count += 1
        
        # ========== 获取起涨日和见顶日 ==========
        start_date = ''
        peak_date = ''
        if 'trade_date' in hist.columns:
            # 处理日期格式，去除时间部分
            start_date_raw = str(hist.iloc[best_start_idx]['trade_date'])
            peak_date_raw = str(hist.iloc[best_end_idx]['trade_date'])
            
            # 如果包含时间部分，只取日期部分
            if ' ' in start_date_raw:
                start_date_raw = start_date_raw.split(' ')[0]
            if ' ' in peak_date_raw:
                peak_date_raw = peak_date_raw.split(' ')[0]
            
            # 统一格式为 YYYYMMDD
            start_date = start_date_raw.replace('-', '').replace('/', '')
            peak_date = peak_date_raw.replace('-', '').replace('/', '')
        
        # ========== 判断是否满足任一标准 ==========
        min_limit_up_count = self.params.get("min_limit_up_count", 4)
        
        is_valid = (
            max_rise_5d >= min_rise_5d or
            max_rise_10d >= min_rise_10d or
            limit_up_count >= min_limit_up_count
        )
        
        logger.debug(f"[{stock_code}] 涨幅统计(双轨制): 任意5日最大{max_rise_5d*100:.1f}%, "
                    f"任意10日最大{max_rise_10d*100:.1f}%, 最佳期间({best_period})涨停{limit_up_count}次, "
                    f"起涨日={start_date}, 见顶日={peak_date}, 有效:{is_valid}")
        
        return {
            'is_valid': is_valid,
            'rise_5d': max_rise_5d,
            'rise_10d': max_rise_10d,
            'limit_up_count': limit_up_count,
            'start_date': start_date,
            'peak_date': peak_date,
            'best_period': best_period
        }

    def _count_trading_days_between(self, start_date: str, end_date: str) -> int:
        """
        计算两个日期之间有多少个交易日（不包括start_date，包括end_date）

        例如：
        - 周五(20260320)到下周一(20260323)：中间没有交易日，返回0
        - 周一(20260323)到周三(20260325)：中间有1个交易日(周二)，返回1
        - 周一(20260323)到周四(20260326)：中间有2个交易日(周二、周三)，返回2

        Args:
            start_date: 开始日期，格式YYYYMMDD
            end_date: 结束日期，格式YYYYMMDD

        Returns:
            两个日期之间的交易日数量
        """
        try:
            # 尝试使用交易日管理器
            if self.dm and hasattr(self.dm, 'trade_date_mgr'):
                trade_dates = self.dm.trade_date_mgr.get_trade_dates_between(start_date, end_date)
                # 过滤掉start_date当天
                trade_dates = [d for d in trade_dates if d > start_date]
                return len(trade_dates)
        except Exception as e:
            logger.debug(f"使用交易日管理器失败，使用简化计算: {e}")

        # 简化计算：使用日历天数减去周末
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        # 计算总天数差
        total_days = (end - start).days

        # 计算中间有多少个周末
        # 从start的下一天开始算
        weekend_days = 0
        current = start + timedelta(days=1)
        while current <= end:
            if current.weekday() >= 5:  # 周六或周日
                weekend_days += 1
            current += timedelta(days=1)

        trading_days = total_days - weekend_days
        return max(0, trading_days)

    def _get_adjust_period(self, stock_code: str,
                          peak_date: str, today: str) -> Dict:
        """
        获取调整期数据（peak_date到today之间）- 优化版本
        
        改进点：
        1. 修复边界条件 - 允许peak_date等于today的情况
        2. 增加观察列表机制 - 当数据不足时返回部分信息
        3. 优化日期处理逻辑，支持更多日期格式
        """
        # 标准化日期格式
        peak_date = self._normalize_date(peak_date)
        today = self._normalize_date(today)
        
        # 边界条件检查 - 如果peak_date等于或晚于today，说明数据有问题
        try:
            peak_dt = datetime.strptime(peak_date, "%Y%m%d")
            today_dt = datetime.strptime(today, "%Y%m%d")
            
            if peak_dt >= today_dt:
                logger.debug(f"[{stock_code}] peak_date({peak_date}) >= today({today})，第一波尚未结束，无法计算调整期")
                return {}
        except Exception as e:
            logger.warning(f"[{stock_code}] 日期解析失败: {e}")
            return {}

        # 计算需要提前获取的天数（至少10天数据用于计算MA10）
        extended_start_dt = peak_dt - timedelta(days=30)  # 增加到30天，确保有足够数据
        extended_start = extended_start_dt.strftime("%Y%m%d")

        # 从data_manager获取日线数据
        hist = self.repo.get_stock_daily(stock_code, extended_start, today)
        
        if hist.empty:
            logger.debug(f"[{stock_code}] 无法获取历史数据({extended_start}-{today})，尝试扩大范围")
            # 尝试扩大范围获取数据
            extended_start_dt = peak_dt - timedelta(days=60)
            extended_start = extended_start_dt.strftime("%Y%m%d")
            hist = self.repo.get_stock_daily(stock_code, extended_start, today)
            
            if hist.empty:
                logger.warning(f"[{stock_code}] 扩大范围后仍无法获取数据")
                return {}
        
        if 'trade_date' in hist.columns:
            hist = hist.sort_values('trade_date').reset_index(drop=True)

        # 筛选出peak_date之后的数据
        if 'trade_date' in hist.columns:
            peak_dt_ts = pd.Timestamp(peak_date)
            adjust_hist = hist[hist['trade_date'] >= peak_dt_ts].copy()
        else:
            adjust_hist = hist.copy()

        # 放宽条件：至少2条数据（原来是3条）
        if len(adjust_hist) < 2:
            logger.debug(f"[{stock_code}] 调整期数据不足({len(adjust_hist)}条)，尝试使用全部历史数据")
            adjust_hist = hist.copy()
            if len(adjust_hist) < 2:
                return {}

        # 计算调整深度（从peak到最低点的跌幅）
        try:
            peak_price = adjust_hist.iloc[0]['high']
            lowest = adjust_hist['low'].min()
            if peak_price > 0:
                depth = (peak_price - lowest) / peak_price
            else:
                depth = 0
        except Exception as e:
            logger.debug(f"[{stock_code}] 计算调整深度失败: {e}")
            depth = 0

        # 计算均线 - 优化：即使数据不足也尝试计算
        total_days = len(hist)
        
        # 动态计算均线周期
        ma10_period = min(10, total_days)
        ma20_period = min(20, total_days)
        
        if total_days >= 5:  # 放宽到至少5天
            hist['MA10'] = hist['close'].rolling(ma10_period).mean()
        else:
            hist['MA10'] = hist['close']  # 数据不足时使用收盘价
            
        if total_days >= 10:  # 放宽到至少10天
            hist['MA20'] = hist['close'].rolling(ma20_period).mean()
        else:
            hist['MA20'] = np.nan

        # 获取today对应的均线值
        ma10 = None
        ma20 = None
        try:
            if 'trade_date' in hist.columns:
                today_dt_ts = pd.Timestamp(today)
                today_row = hist[hist['trade_date'] == today_dt_ts]
                if not today_row.empty:
                    ma10 = today_row.iloc[-1]['MA10']
                    ma20 = today_row.iloc[-1]['MA20']
                else:
                    ma10 = hist.iloc[-1]['MA10']
                    ma20 = hist.iloc[-1]['MA20']
            else:
                ma10 = hist.iloc[-1]['MA10']
                ma20 = hist.iloc[-1]['MA20']
        except Exception as e:
            logger.debug(f"[{stock_code}] 获取均线值失败: {e}")

        # 如果MA10为NaN，尝试使用最近的有效值
        if pd.isna(ma10):
            valid_ma10 = hist['MA10'].dropna()
            if not valid_ma10.empty:
                ma10 = valid_ma10.iloc[-1]
            else:
                # 使用最新收盘价作为备选
                ma10 = hist.iloc[-1]['close'] if not hist.empty else None

        if pd.isna(ma20) or ma20 is None:
            valid_ma20 = hist['MA20'].dropna()
            if not valid_ma20.empty:
                ma20 = valid_ma20.iloc[-1]
            else:
                ma20 = None

        if ma10 is None or pd.isna(ma10):
            logger.warning(f"[{stock_code}] 无法计算MA10")
            return {}

        # 计算调整天数：见顶日**之后**、二波启动日**之前**的交易日数。
        # 见顶日(peak)是第一波的封板日（顶），二波启动日(today)是再次涨停日，
        # 二者都不属于"调整期"，必须同时排除。
        # 例：20260528涨停(peak) → 20260529收阴(调整) → 20260601再涨停(today)，
        #     调整窗口仅含 20260529，故调整天数=1（而非把 peak 一起算成 2）。
        if 'trade_date' in hist.columns:
            today_dt_ts = pd.Timestamp(today)
            adjust_window = adjust_hist[
                (adjust_hist['trade_date'] > peak_dt_ts)
                & (adjust_hist['trade_date'] < today_dt_ts)
            ]
            adjust_days = len(adjust_window)

            # 下跌 / 震荡天数：按调整窗口内每日**真实涨跌**统计，而非"到最低点为止
            # 的全部交易日"。下跌天 = 当日 pct_chg < 0（真实收跌）；震荡天 = pct_chg >= 0
            # （横盘或反弹）。如此 下跌天数 + 震荡天数 == 调整天数，且如实反映每天涨跌。
            daily_changes: List[float] = []
            if adjust_days > 0:
                if 'pct_chg' in adjust_window.columns:
                    for x in adjust_window['pct_chg'].tolist():
                        if x is not None and not pd.isna(x):
                            daily_changes.append(float(x))
                else:
                    # 无 pct_chg 列时用收盘价环比近似（见顶日收盘作为首个调整日的基准）
                    ah = adjust_hist.sort_values('trade_date')
                    closes = ah['close'].tolist()
                    dates = ah['trade_date'].tolist()
                    for i in range(1, len(closes)):
                        d = dates[i]
                        if peak_dt_ts < d < today_dt_ts:
                            try:
                                daily_changes.append(float(closes[i]) - float(closes[i - 1]))
                            except (TypeError, ValueError):
                                pass
            decline_days = sum(1 for c in daily_changes if c < 0)

            # 震荡天数 = 总调整天数 - 真实下跌天数
            consolidation_days = max(0, adjust_days - decline_days)
        else:
            # 无 trade_date 列时的近似：行数减去 peak 当天
            adjust_days = max(0, len(adjust_hist) - 1)
            decline_days = max(1, adjust_days // 2) if adjust_days else 0
            consolidation_days = max(0, adjust_days - decline_days)

        return {
            'depth': depth,
            'ma10': ma10,
            'ma20': ma20 if not pd.isna(ma20) else None,
            'lowest_price': lowest,
            'days': adjust_days,
            'decline_days': decline_days,
            'consolidation_days': consolidation_days,
            'peak_date': peak_date,  # 返回实际使用的peak_date
            'data_quality': 'full' if len(adjust_hist) >= 5 else 'partial'  # 数据质量标记
        }
    
    def _normalize_date(self, date_str: str) -> str:
        """
        标准化日期格式为YYYYMMDD
        
        支持格式：
        - YYYYMMDD
        - YYYY-MM-DD
        - YYYY/MM/DD
        - YYYY-MM-DD HH:MM:SS
        """
        if not date_str:
            return date_str
        
        date_str = str(date_str).strip()
        
        # 去除时间部分
        if ' ' in date_str:
            date_str = date_str.split(' ')[0]
        
        # 去除分隔符
        date_str = date_str.replace('-', '').replace('/', '')
        
        # 确保是8位数字
        if len(date_str) == 8 and date_str.isdigit():
            return date_str
        
        # 尝试解析其他格式
        try:
            for fmt in ["%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"]:
                try:
                    dt = datetime.strptime(date_str, fmt)
                    return dt.strftime("%Y%m%d")
                except ValueError:
                    continue
        except Exception:
            pass
        
        logger.warning(f"日期格式无法解析: {date_str}")
        return date_str
    
    def _check_adjust_quality(self, adjust: Dict) -> bool:
        """
        检查调整质量 - 优化版本
        
        改进点：
        1. 增加数据质量检查
        2. 放宽部分边界条件
        3. 增加观察列表支持
        """
        if not adjust:
            return False
        
        # 检查数据质量
        data_quality = adjust.get('data_quality', 'full')
        if data_quality == 'partial':
            logger.debug(f"数据质量为partial，放宽检查标准")
            # 对于部分数据，仅检查基本条件
            depth = adjust.get('depth', 0)
            min_depth = self.params.get("min_adjust_depth", 0.05)
            max_depth = self.params.get("max_adjust_depth", 0.35)  # 放宽到35%
            
            if depth > max_depth:
                logger.debug(f"调整深度过大: {depth*100:.1f}% > {max_depth*100:.0f}%")
                return False
            
            # 部分数据时，不过度依赖均线
            return True

        depth = adjust.get('depth', 0)
        min_depth = self.params.get("min_adjust_depth", 0.05)   # 最小5%
        max_depth = self.params.get("max_adjust_depth", 0.30)   # 最大30%
        
        # 调整深度检查：5%-30%
        if not (min_depth <= depth <= max_depth):
            logger.debug(f"调整深度不符合: {depth*100:.1f}% (要求{min_depth*100:.0f}%-{max_depth*100:.0f}%)")
            return False

        # 均线支撑检查 - 优化后增加MA20备选和更灵活的容忍度
        lowest_price = adjust.get('lowest_price')
        ma10 = adjust.get('ma10')
        ma20 = adjust.get('ma20')
        
        if lowest_price and ma10:
            ma10_tolerance = self.params.get("ma10_tolerance", 0.10)  # 容忍10%
            
            # 检查MA10支撑
            ma10_support = lowest_price >= ma10 * (1 - ma10_tolerance)
            
            if ma10_support:
                logger.debug(f"MA10支撑有效: 最低价{lowest_price:.2f} >= MA10*{1-ma10_tolerance:.0%}({ma10*(1-ma10_tolerance):.2f})")
                return True
            
            # MA10未支撑，尝试MA20备选
            if self.params.get("use_ma20_fallback", True) and ma20:
                ma20_tolerance = 0.08  # 放宽到8%
                ma20_support = lowest_price >= ma20 * (1 - ma20_tolerance)
                
                if ma20_support:
                    logger.debug(f"MA20支撑有效(备选): 最低价{lowest_price:.2f} >= MA20*{1-ma20_tolerance:.0%}({ma20*(1-ma20_tolerance):.2f})")
                    return True
                else:
                    logger.debug(f"均线支撑无效: 最低价{lowest_price:.2f}, MA10={ma10:.2f}, MA20={ma20:.2f}")
                    return False
            else:
                logger.debug(f"MA10支撑无效: 最低价{lowest_price:.2f} < MA10*{1-ma10_tolerance:.0%}({ma10*(1-ma10_tolerance):.2f})")
                return False
        
        # 没有均线数据时，仅通过深度检查
        return True
    
    def _calculate_days_since_peak(self, peak_date: str, today: str) -> int:
        """计算从第一波见顶到今天的交易日天数"""
        try:
            # 尝试使用交易日管理器计算交易日天数
            if self.dm and hasattr(self.dm, 'trade_date_mgr'):
                trade_dates = self.dm.trade_date_mgr.get_trade_dates_between(peak_date, today)
                # 过滤掉peak_date当天
                trade_dates = [d for d in trade_dates if d > peak_date]
                return len(trade_dates)
        except Exception as e:
            logger.debug(f"使用交易日管理器失败，使用简化计算: {e}")
        
        # 简化计算：使用日历天数减去周末
        peak = datetime.strptime(peak_date, "%Y%m%d")
        today_dt = datetime.strptime(today, "%Y%m%d")
        
        # 计算总天数差
        total_days = (today_dt - peak).days
        
        # 计算中间有多少个周末
        weekend_days = 0
        current = peak + timedelta(days=1)
        while current <= today_dt:
            if current.weekday() >= 5:  # 周六或周日
                weekend_days += 1
            current += timedelta(days=1)
        
        trading_days = total_days - weekend_days
        return max(0, trading_days)

# ==================== 数据准备示例 ====================

def prepare_recent_pools(data_manager, today: str, days: int = 15) -> Dict[str, pd.DataFrame]:
    """
    准备近15日每日涨停池
    """
    pools = {}
    
    for i in range(days):
        date = data_manager.get_date_offset(today, -i)
        pool = data_manager.get_limit_up_pool(date)
        if not pool.empty:
            pools[date] = pool
    
    return pools

# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("龙二波策略V2 - 正确的历史连板判断")
    print("核心：从每日涨停池取近期连板，非日线涨幅计算")
    print("时间范围：近15日内，记忆未散")
    print("")
    print("正确做法：")
    print("1. 取近15日每日涨停池")
    print("2. 检查目标股在哪些日期出现在涨停池")
    print("3. 计算连续出现次数（允许断板1天）")
    print("4. 确认是近期这一波（非3个月前的行情）")
    print("5. 今日再次出现在涨停池=二波启动确认")