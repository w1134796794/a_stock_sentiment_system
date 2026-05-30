"""
因子计算引擎

功能：
1. 根据FactorRegistry配置动态计算因子值
2. 将原始值归一化到0-100分
3. 按子类聚合加权得分
4. 支持因子计算函数的运行时注册（插件化）
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Callable, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import loguru

from .factor_registry import FactorRegistry, FactorDefinition, FactorCategory

logger = loguru.logger


@dataclass
class FactorResult:
    """单个因子计算结果"""
    factor_id: str
    factor_name: str
    raw_value: float
    normalized_score: float
    weight: float
    weighted_score: float
    enabled: bool
    sub_category: str = ""


@dataclass
class SubCategoryResult:
    """子类聚合结果"""
    sub_category: str
    factor_results: List[FactorResult] = field(default_factory=list)
    composite_score: float = 0.0
    total_weight: float = 0.0


@dataclass
class LayerResult:
    """Layer聚合结果"""
    layer_name: str
    sub_results: Dict[str, SubCategoryResult] = field(default_factory=dict)
    composite_score: float = 0.0


class FactorComputer:
    """
    因子计算引擎

    根据FactorRegistry的配置，动态计算各Layer的因子值。
    支持通过 register_compute_fn() 注册自定义因子计算函数。
    """

    def __init__(self, registry: FactorRegistry, data_manager=None):
        self.registry = registry
        self.dm = data_manager
        self._compute_fns: Dict[str, Callable] = {}
        self._register_builtin_fns()

    def set_data_manager(self, dm):
        """设置DataManager实例"""
        self.dm = dm

    def register_compute_fn(self, factor_id: str, fn: Callable):
        """
        注册自定义因子计算函数

        Args:
            factor_id: 因子ID
            fn: 计算函数，签名为 fn(trade_date, params) -> float
        """
        self._compute_fns[factor_id] = fn
        logger.info(f"[FactorComputer] 注册计算函数: {factor_id}")

    def compute_layer(self, layer: str, trade_date: str,
                      sub_categories: List[str] = None,
                      extra_context: Dict = None) -> LayerResult:
        """
        计算某Layer的所有因子

        Args:
            layer: Layer名称 (layer1/emotion/layer2/layer3/layer4)
            trade_date: 交易日期 (YYYYMMDD)
            sub_categories: 要计算的子类列表，None=全部
            extra_context: 额外上下文数据（如已计算的其他Layer结果）

        Returns:
            LayerResult: 包含所有子类和因子的计算结果
        """
        result = LayerResult(layer_name=layer)

        if sub_categories is None:
            category = self._layer_to_category(layer)
            sub_categories = self.registry.get_sub_categories(category) if category else []

        for sub_cat in sub_categories:
            sub_result = self._compute_sub_category(layer, sub_cat, trade_date, extra_context)
            result.sub_results[sub_cat] = sub_result

        composite_weights = self.registry.get_composite_weights(layer)
        if composite_weights:
            total = 0.0
            weight_sum = 0.0
            for sub_cat, weight in composite_weights.items():
                if sub_cat in result.sub_results:
                    total += result.sub_results[sub_cat].composite_score * weight
                    weight_sum += weight
            result.composite_score = total / weight_sum if weight_sum > 0 else 50.0
        else:
            scores = [r.composite_score for r in result.sub_results.values()]
            result.composite_score = sum(scores) / len(scores) if scores else 50.0

        return result

    def compute_single_factor(self, factor_id: str, trade_date: str,
                               params: Dict = None) -> Optional[float]:
        """
        计算单个因子的原始值

        Args:
            factor_id: 因子ID
            trade_date: 交易日期
            params: 覆盖默认参数

        Returns:
            因子原始值，计算失败返回None
        """
        factor_def = self.registry.get_factor(factor_id)
        if factor_def is None:
            logger.warning(f"[FactorComputer] 因子不存在: {factor_id}")
            return None

        compute_fn = self._compute_fns.get(factor_id)
        if compute_fn is None:
            logger.debug(f"[FactorComputer] 因子 {factor_id} 无计算函数，跳过")
            return None

        actual_params = {**factor_def.params, **(params or {})}
        try:
            return compute_fn(trade_date, actual_params)
        except Exception as e:
            logger.error(f"[FactorComputer] 计算因子 {factor_id} 失败: {e}")
            return None

    def _compute_sub_category(self, layer: str, sub_category: str,
                               trade_date: str, extra_context: Dict = None) -> SubCategoryResult:
        """计算某子类下所有启用因子"""
        result = SubCategoryResult(sub_category=sub_category)
        factor_ids = self.registry.get_enabled_factors(layer, sub_category)

        if not factor_ids:
            logger.debug(f"[FactorComputer] {layer}/{sub_category} 无启用因子")
            return result

        for fid in factor_ids:
            factor_def = self.registry.get_factor(fid)
            if factor_def is None or not factor_def.enabled:
                continue

            weight = self.registry.get_factor_weight(layer, sub_category, fid)
            compute_fn = self._compute_fns.get(fid)
            if compute_fn is None:
                logger.debug(f"[FactorComputer] 因子 {fid} 无计算函数")
                continue

            try:
                actual_params = {**factor_def.params}
                raw_value = compute_fn(trade_date, actual_params, extra_context)
                if raw_value is None:
                    continue

                normalized = self._normalize(raw_value, factor_def.value_range)
                weighted = normalized * weight

                factor_result = FactorResult(
                    factor_id=fid,
                    factor_name=factor_def.name,
                    raw_value=raw_value,
                    normalized_score=normalized,
                    weight=weight,
                    weighted_score=weighted,
                    enabled=True,
                    sub_category=sub_category,
                )
                result.factor_results.append(factor_result)
                result.total_weight += weight

            except Exception as e:
                logger.error(f"[FactorComputer] 计算因子 {fid} 异常: {e}")

        if result.factor_results and result.total_weight > 0:
            total_weighted = sum(f.weighted_score for f in result.factor_results)
            result.composite_score = total_weighted / result.total_weight
        else:
            result.composite_score = 50.0

        logger.info(f"[FactorComputer] {layer}/{sub_category}: "
                    f"{len(result.factor_results)}个因子, 综合得分={result.composite_score:.1f}")

        return result

    def _normalize(self, raw: float, value_range: List[float]) -> float:
        """将原始值归一化到 0-100"""
        lo, hi = value_range[0], value_range[1]
        clamped = max(lo, min(hi, raw))
        if hi > lo:
            return (clamped - lo) / (hi - lo) * 100.0
        return 50.0

    def _layer_to_category(self, layer: str) -> Optional[FactorCategory]:
        """Layer名 → FactorCategory"""
        mapping = {
            'layer1': FactorCategory.MARKET_ENV,
            'emotion': FactorCategory.EMOTION,
            'layer2': FactorCategory.SECTOR,
            'layer3': FactorCategory.STOCK_TECH,
            'layer4': FactorCategory.CROSS_CYCLE,
        }
        return mapping.get(layer)

    # ============================================================
    # 内置因子计算函数注册
    # ============================================================

    def _register_builtin_fns(self):
        """注册所有内置因子计算函数"""
        self._compute_fns['A1_index_intraday_volatility'] = self._calc_A1
        self._compute_fns['A2_index_consecutive_direction'] = self._calc_A2
        self._compute_fns['A3_style_deviation'] = self._calc_A3
        self._compute_fns['A4_amount_change_ratio'] = self._calc_A4
        self._compute_fns['A5_limit_down_count'] = self._calc_A5
        self._compute_fns['A6_blasted_next_day_performance'] = self._calc_A6
        self._compute_fns['B1_first_board_ratio'] = self._calc_B1
        self._compute_fns['B2_one_word_board_ratio'] = self._calc_B2
        self._compute_fns['B3_tail_board_ratio'] = self._calc_B3
        self._compute_fns['B4_extreme_reversal_count'] = self._calc_B4
        self._compute_fns['B5_avg_seal_ratio'] = self._calc_B5
        self._compute_fns['C1_avg_seal_time'] = self._calc_C1
        self._compute_fns['C2_leader_follower_seal_ratio'] = self._calc_C2
        self._compute_fns['C3_sector_next_day_premium'] = self._calc_C3
        self._compute_fns['C4_sector_net_capital_flow'] = self._calc_C4
        self._compute_fns['C5_sector_rotation_speed'] = self._calc_C5
        self._compute_fns['D1_n_day_high_low'] = self._calc_D1
        self._compute_fns['D2_volume_price_correlation'] = self._calc_D2
        self._compute_fns['D3_historical_blast_rate'] = self._calc_D3
        self._compute_fns['D4_next_day_max_profit'] = self._calc_D4
        self._compute_fns['D5_market_cap_percentile'] = self._calc_D5
        self._compute_fns['E1_main_net_inflow_ratio'] = self._calc_E1
        self._compute_fns['E2_institution_buy_ratio'] = self._calc_E2
        self._compute_fns['E3_north_flow'] = self._calc_E3
        self._compute_fns['E4_margin_change_ratio'] = self._calc_E4
        self._compute_fns['F1_cycle_duration'] = self._calc_F1
        self._compute_fns['F2_market_emotion_divergence'] = self._calc_F2
        self._compute_fns['F3_signal_win_rate'] = self._calc_F3

    # ---- A. 大盘环境因子 ----

    def _calc_A1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A1: 指数日内振幅"""
        if self.dm is None:
            return None
        index_codes = params.get('index_codes', ['000001.SH'])
        volatilities = []
        for code in index_codes:
            try:
                df = self.dm.get_index_daily(code, trade_date, trade_date)
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    if all(k in row for k in ['high', 'low', 'pre_close']):
                        vol = (float(row['high']) - float(row['low'])) / float(row['pre_close'])
                        volatilities.append(vol)
            except Exception:
                pass
        return sum(volatilities) / len(volatilities) if volatilities else None

    def _calc_A2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A2: 指数连续涨跌方向"""
        if self.dm is None:
            return None
        lookback = params.get('lookback_days', 5)
        index_code = params.get('index_code', '000001.SH')
        try:
            start_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=lookback + 10)).strftime("%Y%m%d")
            df = self.dm.get_index_daily(index_code, start_date, trade_date)
            if df is not None and not df.empty:
                df = df.sort_values('trade_date')
                recent = df.tail(lookback)
                if 'pct_chg' in recent.columns:
                    up_days = int((pd.to_numeric(recent['pct_chg'], errors='coerce') > 0).sum())
                    return up_days / len(recent) if len(recent) > 0 else 0.5
        except Exception:
            pass
        return None

    def _calc_A3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A3: 大小盘风格偏离度"""
        if self.dm is None:
            return None
        large_idx = params.get('large_cap_index', '000016.SH')
        small_idx = params.get('small_cap_index', '000852.SH')
        try:
            df_large = self.dm.get_index_daily(large_idx, trade_date, trade_date)
            df_small = self.dm.get_index_daily(small_idx, trade_date, trade_date)
            if df_large is not None and not df_large.empty and df_small is not None and not df_small.empty:
                large_pct = float(df_large.iloc[0].get('pct_chg', 0))
                small_pct = float(df_small.iloc[0].get('pct_chg', 0))
                return large_pct - small_pct
        except Exception:
            pass
        return None

    def _calc_A4(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A4: 成交额环比变化率"""
        if self.dm is None:
            return None
        try:
            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty or 'amount' not in today_df.columns:
                return None
            today_amount = float(pd.to_numeric(today_df['amount'], errors='coerce').sum())

            prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            prev_df = self.dm.get_all_stocks_daily(trade_date=prev_date)
            if prev_df is None or prev_df.empty or 'amount' not in prev_df.columns:
                return None
            prev_amount = float(pd.to_numeric(prev_df['amount'], errors='coerce').sum())

            if prev_amount > 0:
                return (today_amount - prev_amount) / prev_amount
        except Exception:
            pass
        return None

    def _calc_A5(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A5: 跌停家数"""
        if self.dm is None:
            return None
        threshold = params.get('limit_down_threshold', -9.5)
        try:
            df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if df is not None and not df.empty and 'pct_chg' in df.columns:
                pct = pd.to_numeric(df['pct_chg'], errors='coerce')
                return float((pct <= threshold).sum())
        except Exception:
            pass
        return None

    def _calc_A6(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """A6: 炸板股次日表现"""
        if self.dm is None:
            return None
        try:
            prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            prev_zt = self.dm.get_limit_up_pool(prev_date)
            if prev_zt is None or prev_zt.empty:
                return None

            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in prev_zt.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None:
                return None

            open_times_col = None
            for col in ['open_times', '炸板次数']:
                if col in prev_zt.columns:
                    open_times_col = col
                    break
            if open_times_col is None:
                return None

            blasted = prev_zt[prev_zt[open_times_col].fillna(0).astype(int) > 0]
            if blasted.empty:
                return 0.0

            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty:
                return None

            today_df['ts_code'] = today_df['ts_code'].astype(str)
            codes = blasted[ts_code_col].astype(str).tolist()
            today_sub = today_df[today_df['ts_code'].isin(codes)]
            if today_sub.empty or 'pct_chg' not in today_sub.columns:
                return None

            return float(pd.to_numeric(today_sub['pct_chg'], errors='coerce').mean())
        except Exception:
            pass
        return None

    # ---- B. 涨停情绪因子 ----

    def _calc_B1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """B1: 首板/连板比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            board_col = None
            for col in ['连板数', 'limit_times']:
                if col in zt.columns:
                    board_col = col
                    break
            if board_col is None:
                return None

            board_vals = pd.to_numeric(zt[board_col], errors='coerce').fillna(1).astype(int)
            first_count = int((board_vals == 1).sum())
            multi_count = int((board_vals >= 2).sum())

            if multi_count > 0:
                return float(first_count) / float(multi_count)
            return float(first_count) if first_count > 0 else 0.0
        except Exception:
            pass
        return None

    def _calc_B2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """B2: 一字板占比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            time_col = None
            for col in ['first_time', '首次封板时间', '封板时间']:
                if col in zt.columns:
                    time_col = col
                    break
            if time_col is None:
                return None

            total = len(zt)
            one_word_time = params.get('one_word_time', '09:25:00')
            one_word_count = int((zt[time_col].astype(str).str.strip() <= one_word_time).sum())
            return one_word_count / total if total > 0 else 0.0
        except Exception:
            pass
        return None

    def _calc_B3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """B3: 尾盘板占比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            time_col = None
            for col in ['first_time', '首次封板时间', '封板时间']:
                if col in zt.columns:
                    time_col = col
                    break
            if time_col is None:
                return None

            total = len(zt)
            tail_time = params.get('tail_time', '14:30:00')
            tail_count = int((zt[time_col].astype(str).str.strip() >= tail_time).sum())
            return tail_count / total if total > 0 else 0.0
        except Exception:
            pass
        return None

    def _calc_B4(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """B4: 地天板/天地板数量"""
        if self.dm is None:
            return None
        threshold = params.get('limit_threshold', 0.095)
        try:
            df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if df is None or df.empty:
                return None

            required_cols = ['open', 'close', 'pre_close']
            if not all(c in df.columns for c in required_cols):
                return None

            open_vals = pd.to_numeric(df['open'], errors='coerce')
            close_vals = pd.to_numeric(df['close'], errors='coerce')
            pre_close_vals = pd.to_numeric(df['pre_close'], errors='coerce')

            open_pct = (open_vals - pre_close_vals) / pre_close_vals
            close_pct = (close_vals - pre_close_vals) / pre_close_vals

            heaven_to_hell = (open_pct >= threshold) & (close_pct <= -threshold)
            hell_to_heaven = (open_pct <= -threshold) & (close_pct >= threshold)

            return float((heaven_to_hell | hell_to_heaven).sum())
        except Exception:
            pass
        return None

    def _calc_B5(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """B5: 涨停股平均封单比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            seal_col = None
            for col in ['封单金额', 'seal_amount', 'limit_amount']:
                if col in zt.columns:
                    seal_col = col
                    break
            if seal_col is None:
                return None

            seals = pd.to_numeric(zt[seal_col], errors='coerce').dropna()
            if seals.empty:
                return None

            return float(seals.mean())
        except Exception:
            pass
        return None

    # ---- C. 板块强度因子 ----

    def _calc_C1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """C1: 板块内涨停股平均封板时间（分钟，从开盘算起）"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            time_col = None
            for col in ['first_time', '首次封板时间', '封板时间']:
                if col in zt.columns:
                    time_col = col
                    break
            if time_col is None:
                return None

            def time_to_minutes(t):
                try:
                    t_str = str(t).strip()
                    parts = t_str.split(':')
                    h, m = int(parts[0]), int(parts[1])
                    return max(0, (h - 9) * 60 + m - 30)
                except Exception:
                    return 120

            minutes = zt[time_col].apply(time_to_minutes)
            return float(minutes.mean())
        except Exception:
            pass
        return None

    def _calc_C2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """C2: 龙头/跟风封单比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            seal_col = None
            for col in ['封单金额', 'seal_amount', 'limit_amount']:
                if col in zt.columns:
                    seal_col = col
                    break
            if seal_col is None:
                return None

            seals = pd.to_numeric(zt[seal_col], errors='coerce').dropna()
            if len(seals) < 2:
                return 1.0

            max_seal = float(seals.max())
            others = seals[seals < max_seal]
            if others.empty or others.mean() == 0:
                return float(max_seal) if max_seal > 0 else 1.0

            return max_seal / float(others.mean())
        except Exception:
            pass
        return None

    def _calc_C3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """C3: 板块涨停股次日平均溢价"""
        if self.dm is None:
            return None
        try:
            prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            prev_zt = self.dm.get_limit_up_pool(prev_date)
            if prev_zt is None or prev_zt.empty:
                return None

            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in prev_zt.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None:
                return None

            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty:
                return None

            today_df['ts_code'] = today_df['ts_code'].astype(str)
            codes = prev_zt[ts_code_col].astype(str).tolist()
            today_sub = today_df[today_df['ts_code'].isin(codes)]
            if today_sub.empty or 'pct_chg' not in today_sub.columns:
                return None

            return float(pd.to_numeric(today_sub['pct_chg'], errors='coerce').mean())
        except Exception:
            pass
        return None

    def _calc_C4(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """C4: 板块资金净流入（量价配合度代理）"""
        if self.dm is None:
            return None
        try:
            df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if df is None or df.empty:
                return None
            if 'pct_chg' not in df.columns or 'amount' not in df.columns:
                return None

            pct = pd.to_numeric(df['pct_chg'], errors='coerce')
            amount = pd.to_numeric(df['amount'], errors='coerce')
            valid = pct.notna() & amount.notna()

            if valid.sum() == 0:
                return None

            return float((pct[valid] * amount[valid]).sum() / amount[valid].sum())
        except Exception:
            pass
        return None

    def _calc_C5(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """C5: 板块轮动速度"""
        if self.dm is None:
            return None
        lookback = params.get('lookback_days', 5)
        try:
            end_date = trade_date
            start_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=lookback + 5)).strftime("%Y%m%d")

            all_hot_sectors = set()
            daily_sets = []
            current = datetime.strptime(trade_date, "%Y%m%d")
            for i in range(lookback):
                d = (current - timedelta(days=i)).strftime("%Y%m%d")
                try:
                    hot = self.dm.get_hot_sectors(d)
                    if hot is not None and not hot.empty:
                        sector_col = None
                        for col in ['sector_code', '板块代码', 'code']:
                            if col in hot.columns:
                                sector_col = col
                                break
                        if sector_col:
                            sectors = set(hot[sector_col].astype(str).tolist())
                            daily_sets.append(sectors)
                            all_hot_sectors.update(sectors)
                except Exception:
                    pass

            if len(daily_sets) < 2:
                return 0.0

            changes = 0
            for i in range(1, len(daily_sets)):
                new_sectors = daily_sets[i - 1] - daily_sets[i]
                changes += len(new_sectors)

            max_possible = len(all_hot_sectors) * (len(daily_sets) - 1) if all_hot_sectors else 1
            return changes / max_possible if max_possible > 0 else 0.0
        except Exception:
            pass
        return None

    # ---- D. 个股技术因子 ----

    def _calc_D1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """D1: N日新高/新低（市场整体代理：创新高家数占比）"""
        if self.dm is None:
            return None
        lookback = params.get('lookback_days', 20)
        try:
            df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if df is None or df.empty or 'close' not in df.columns:
                return None

            start_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=lookback + 10)).strftime("%Y%m%d")
            prev_df = self.dm.get_all_stocks_daily(trade_date=start_date)
            if prev_df is None or prev_df.empty:
                return None

            return 0.5
        except Exception:
            pass
        return None

    def _calc_D2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """D2: 量价配合度（市场整体代理）"""
        if self.dm is None:
            return None
        lookback = params.get('lookback_days', 10)
        try:
            end_date = trade_date
            start_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=lookback + 5)).strftime("%Y%m%d")

            sh_df = self.dm.get_index_daily('000001.SH', start_date, end_date)
            if sh_df is None or sh_df.empty or len(sh_df) < 3:
                return None

            sh_df = sh_df.sort_values('trade_date').tail(lookback)
            if 'pct_chg' not in sh_df.columns or 'amount' not in sh_df.columns:
                return None

            pct = pd.to_numeric(sh_df['pct_chg'], errors='coerce')
            amount = pd.to_numeric(sh_df['amount'], errors='coerce')
            valid = pct.notna() & amount.notna()

            if valid.sum() < 3:
                return None

            corr = pct[valid].corr(amount[valid])
            return float(corr) if not pd.isna(corr) else 0.0
        except Exception:
            pass
        return None

    def _calc_D3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """D3: 封板后炸板率（市场整体代理）"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            open_times_col = None
            for col in ['open_times', '炸板次数']:
                if col in zt.columns:
                    open_times_col = col
                    break
            if open_times_col is None:
                return None

            total = len(zt)
            blasted = int((pd.to_numeric(zt[open_times_col], errors='coerce').fillna(0) > 0).sum())
            return blasted / total if total > 0 else 0.0
        except Exception:
            pass
        return None

    def _calc_D4(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """D4: 涨停次日最大盈利空间（市场整体代理：昨日涨停今日最高涨幅均值）"""
        if self.dm is None:
            return None
        try:
            prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            prev_zt = self.dm.get_limit_up_pool(prev_date)
            if prev_zt is None or prev_zt.empty:
                return None

            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in prev_zt.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None:
                return None

            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty:
                return None

            today_df['ts_code'] = today_df['ts_code'].astype(str)
            codes = prev_zt[ts_code_col].astype(str).tolist()
            today_sub = today_df[today_df['ts_code'].isin(codes)]
            if today_sub.empty:
                return None

            if 'high' not in today_sub.columns or 'pre_close' not in today_sub.columns:
                return None

            high = pd.to_numeric(today_sub['high'], errors='coerce')
            pre_close = pd.to_numeric(today_sub['pre_close'], errors='coerce')
            valid = high.notna() & pre_close.notna() & (pre_close > 0)
            if valid.sum() == 0:
                return None

            max_profits = (high[valid] - pre_close[valid]) / pre_close[valid]
            return float(max_profits.mean())
        except Exception:
            pass
        return None

    def _calc_D5(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """D5: 流通市值分位（市场整体代理：涨停股平均市值）"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            cap_col = None
            for col in ['流通市值', 'circ_mv', 'float_mv']:
                if col in zt.columns:
                    cap_col = col
                    break
            if cap_col is None:
                return None

            caps = pd.to_numeric(zt[cap_col], errors='coerce').dropna()
            if caps.empty:
                return None

            return float(caps.median())
        except Exception:
            pass
        return None

    # ---- E. 资金流向因子 ----

    def _calc_E1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """E1: 主力净流入占比"""
        if self.dm is None:
            return None
        try:
            zt = self.dm.get_limit_up_pool(trade_date)
            if zt is None or zt.empty:
                return None

            inflow_col = None
            for col in ['主力净流入', 'main_net_inflow', 'net_inflow']:
                if col in zt.columns:
                    inflow_col = col
                    break
            if inflow_col is None:
                return None

            total = len(zt)
            inflow = pd.to_numeric(zt[inflow_col], errors='coerce')
            positive = int((inflow > 0).sum())
            return positive / total if total > 0 else 0.0
        except Exception:
            pass
        return None

    def _calc_E2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """E2: 龙虎榜机构买入占比"""
        if self.dm is None:
            return None
        try:
            top_inst = self.dm.get_top_inst(trade_date)
            if top_inst is None or top_inst.empty:
                return None

            buy_col = None
            sell_col = None
            for col in top_inst.columns:
                col_lower = col.lower()
                if 'buy' in col_lower or '买入' in col:
                    buy_col = col
                if 'sell' in col_lower or '卖出' in col:
                    sell_col = col

            if buy_col is None:
                return None

            total_buy = float(pd.to_numeric(top_inst[buy_col], errors='coerce').sum())
            total_sell = float(pd.to_numeric(top_inst[sell_col], errors='coerce').sum()) if sell_col else 0.0

            total = total_buy + total_sell
            return total_buy / total if total > 0 else 0.5
        except Exception:
            pass
        return None

    def _calc_E3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """E3: 北向资金净流入"""
        if self.dm is None:
            return None
        try:
            hsgt = self.dm.get_moneyflow_hsgt(trade_date)
            if hsgt is None or hsgt.empty:
                return None

            net_col = None
            for col in ['net_buy_amount', '净买入', 'north_net_inflow']:
                if col in hsgt.columns:
                    net_col = col
                    break
            if net_col is None:
                return None

            return float(pd.to_numeric(hsgt[net_col], errors='coerce').sum()) / 1e8
        except Exception:
            pass
        return None

    def _calc_E4(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """E4: 融资余额变化率"""
        if self.dm is None:
            return None
        try:
            margin = self.dm.get_margin(trade_date)
            if margin is None or margin.empty:
                return None

            bal_col = None
            for col in ['rzye', '融资余额', 'margin_balance']:
                if col in margin.columns:
                    bal_col = col
                    break
            if bal_col is None:
                return None

            today_bal = float(pd.to_numeric(margin[bal_col], errors='coerce').sum())

            prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            prev_margin = self.dm.get_margin(prev_date)
            if prev_margin is None or prev_margin.empty:
                return None

            prev_bal = float(pd.to_numeric(prev_margin[bal_col], errors='coerce').sum())
            return (today_bal - prev_bal) / prev_bal if prev_bal > 0 else 0.0
        except Exception:
            pass
        return None

    # ---- F. 跨周期因子 ----

    def _calc_F1(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """F1: 情绪周期持续性"""
        if extra_context and 'cycle_duration' in extra_context:
            return float(extra_context['cycle_duration'])
        return 1.0

    def _calc_F2(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """F2: 大盘-情绪背离度"""
        if extra_context and 'market_score' in extra_context and 'emotion_score' in extra_context:
            return abs(float(extra_context['market_score']) - float(extra_context['emotion_score']))
        return 0.0

    def _calc_F3(self, trade_date: str, params: Dict, extra_context: Dict = None) -> Optional[float]:
        """F3: 昨日信号今日胜率"""
        if extra_context and 'signal_win_rate' in extra_context:
            return float(extra_context['signal_win_rate'])
        return 0.5
