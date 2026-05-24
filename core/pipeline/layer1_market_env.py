"""
Layer 1: 大盘环境判断 - Market Environment Analyzer

职责：替代当前缺失的"看大盘"步骤，量化整体市场环境

输入数据：
  - 上证指数/深证成指/创业板指 日线数据
  - 全市场涨跌家数
  - 全市场成交额

输出指标：
  - 指数趋势（多头/空头/震荡）
  - 量能状态（放量/缩量/平量）
  - 市场宽度（上涨家数/下跌家数）
  - 综合环境评分（0-100）

与情绪周期的关系：
  - 大盘环境 + 涨停情绪 = 综合仓位建议
  - 大盘空头 + 涨停高潮 = 警惕诱多
  - 大盘多头 + 涨停冰点 = 可能是低吸机会
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from core.utils.date_utils import DateUtils
import loguru

logger = loguru.logger


class MarketTrend(Enum):
    BULL = "多头"
    BEAR = "空头"
    SIDEWAYS = "震荡"


class VolumeState(Enum):
    EXPANDING = "放量"
    SHRINKING = "缩量"
    FLAT = "平量"


class MarketWidth(Enum):
    STRONG = "强势"       # 上涨>70%
    NORMAL = "正常"       # 上涨40-70%
    WEAK = "弱势"         # 上涨20-40%
    EXTREME_WEAK = "极度弱势"  # 上涨<20%


@dataclass
class MarketEnvResult:
    """大盘环境分析结果"""
    trade_date: str

    # 指数数据
    sh_index_close: float = 0.0
    sh_index_change_pct: float = 0.0
    sz_index_close: float = 0.0
    sz_index_change_pct: float = 0.0
    cyb_index_close: float = 0.0
    cyb_index_change_pct: float = 0.0
    kcb_index_close: float = 0.0
    kcb_index_change_pct: float = 0.0
    bj_index_close: float = 0.0
    bj_index_change_pct: float = 0.0

    # 趋势判断
    index_trend: MarketTrend = MarketTrend.SIDEWAYS
    trend_score: float = 0.0          # 趋势评分 0-100

    # 量能判断
    total_volume: float = 0.0         # 全市场成交额（亿）
    volume_5d_avg: float = 0.0        # 5日均量
    volume_ratio: float = 1.0         # 量比
    volume_state: VolumeState = VolumeState.FLAT
    volume_score: float = 0.0         # 量能评分 0-100

    # 市场宽度
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    up_ratio: float = 0.0             # 上涨比例
    market_width: MarketWidth = MarketWidth.NORMAL
    width_score: float = 0.0          # 宽度评分 0-100

    # 涨停连续性（昨日涨停股今日表现）
    prev_limit_up_total: int = 0           # 昨日涨停总数
    prev_limit_up_gap_up_ratio: float = 0.0  # 今日高开比例
    prev_limit_up_positive_ratio: float = 0.0  # 今日收红比例

    # 首板连续性（昨日首板股今日表现）
    prev_first_board_total: int = 0            # 昨日首板总数
    prev_first_board_gap_up_ratio: float = 0.0   # 今日高开比例
    prev_first_board_positive_ratio: float = 0.0  # 今日收红比例

    # 综合评分
    composite_score: float = 0.0      # 综合环境评分 0-100
    risk_level: str = "中等"           # 风险等级：低/中等/高/极高
    suggested_position: str = "30-50%"  # 建议仓位

    # 与情绪周期的交叉判断
    cross_judgment: str = ""          # 大盘+情绪交叉判断

    # 详细分析文本
    analysis_summary: str = ""

    # 补充因子字段（供 factor_collector 使用）
    amount_change_ratio: float = 0.0     # 成交额环比变化率
    limit_down_count: int = 0            # 跌停家数
    blasted_next_day_pct: float = 0.0    # 炸板股次日表现


class MarketEnvAnalyzer:
    """
    大盘环境分析器 - Layer 1

    量化整体市场环境，为仓位决策提供依据
    """

    def __init__(self, data_manager):
        self.dm = data_manager
        self.date_utils = DateUtils()

        # 指数代码映射
        self.index_codes = {
            'sh': '000001.SH',    # 上证指数
            'sz': '399001.SZ',    # 深证成指
            'cyb': '399006.SZ',   # 创业板指
            'kcb': '000688.SH',   # 科创50
            'bj': '899050.BJ',    # 北证50
        }

        # 各指数在趋势综合评分中的权重
        self.index_trend_weights = {
            'sh': 0.35,    # 上证权重最高
            'sz': 0.25,    # 深证
            'cyb': 0.20,   # 创业板
            'kcb': 0.10,   # 科创50
            'bj': 0.10,    # 北证50
        }

        # 评分权重
        self.weights = {
            'trend': 0.40,       # 趋势权重
            'volume': 0.30,      # 量能权重
            'width': 0.30,       # 宽度权重
        }

        # 趋势判断参数
        self.trend_params = {
            'ma_short': 5,       # 短期均线
            'ma_mid': 20,        # 中期均线
            'ma_long': 60,       # 长期均线
            'bull_threshold': 0.01,   # 多头阈值（指数在MA20上方1%）
            'bear_threshold': -0.01,  # 空头阈值（指数在MA20下方1%）
        }

        # 量能判断参数
        self.volume_params = {
            'expand_ratio': 1.2,      # 放量阈值（量比>1.2）
            'shrink_ratio': 0.8,      # 缩量阈值（量比<0.8）
            'lookback_days': 5,       # 均量计算天数
        }

        # 市场宽度参数
        self.width_params = {
            'strong_threshold': 0.70,       # 强势阈值
            'normal_threshold': 0.40,       # 正常阈值
            'weak_threshold': 0.20,         # 弱势阈值
        }

        logger.info("[MarketEnvAnalyzer] 初始化完成")

    def analyze(self, trade_date: str) -> MarketEnvResult:
        """
        执行大盘环境分析

        Args:
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            MarketEnvResult: 大盘环境分析结果
        """
        logger.info("=" * 60)
        logger.info(f"[Layer1-大盘环境] 开始分析: {trade_date}")
        logger.info("=" * 60)

        result = MarketEnvResult(trade_date=trade_date)

        # 1. 指数趋势分析
        self._analyze_index_trend(trade_date, result)

        # 2. 量能分析
        self._analyze_volume(trade_date, result)

        # 3. 市场宽度分析
        self._analyze_market_width(trade_date, result)

        # 4. 涨停连续性分析（昨日涨停今日表现）
        self._analyze_limit_up_continuity(trade_date, result)

        # 5. 综合评分
        self._calculate_composite_score(result)

        # 6. 生成分析摘要
        self._generate_summary(result)

        logger.info(f"[Layer1-大盘环境] 分析完成: 趋势={result.index_trend.value}, "
                    f"量能={result.volume_state.value}, 宽度={result.market_width.value}, "
                    f"综合评分={result.composite_score:.0f}, 建议仓位={result.suggested_position}")

        return result

    def _analyze_index_trend(self, trade_date: str, result: MarketEnvResult):
        """分析指数趋势（多指数综合评分：上证/深证/创业板/科创50/北证50）"""
        try:
            end_date = trade_date
            start_date = self.date_utils.get_n_trade_dates_before(120, trade_date)

            index_scores = {}
            index_trends = {}

            for idx_name, idx_code in self.index_codes.items():
                try:
                    df = self.dm.get_index_daily(idx_code, start_date, end_date)
                    if df is None or df.empty:
                        logger.warning(f"[Layer1] 获取{idx_name}指数数据为空")
                        continue

                    df = df.sort_values('trade_date')
                    close = df['close'].values
                    latest_close = float(close[-1]) if len(close) > 0 else 0.0

                    if 'pct_chg' in df.columns and len(df) > 0:
                        change_pct = float(df['pct_chg'].values[-1])
                    elif len(close) >= 2:
                        change_pct = (close[-1] - close[-2]) / close[-2] * 100
                    else:
                        change_pct = 0.0

                    if idx_name == 'sh':
                        result.sh_index_close = latest_close
                        result.sh_index_change_pct = float(change_pct)
                    elif idx_name == 'sz':
                        result.sz_index_close = latest_close
                        result.sz_index_change_pct = float(change_pct)
                    elif idx_name == 'cyb':
                        result.cyb_index_close = latest_close
                        result.cyb_index_change_pct = float(change_pct)
                    elif idx_name == 'kcb':
                        result.kcb_index_close = latest_close
                        result.kcb_index_change_pct = float(change_pct)
                    elif idx_name == 'bj':
                        result.bj_index_close = latest_close
                        result.bj_index_change_pct = float(change_pct)

                    if len(close) < self.trend_params['ma_mid']:
                        continue

                    ma5 = float(np.mean(close[-self.trend_params['ma_short']:]))
                    ma20 = float(np.mean(close[-self.trend_params['ma_mid']:]))
                    ma60 = float(np.mean(close[-self.trend_params['ma_long']:])) if len(close) >= self.trend_params['ma_long'] else ma20

                    latest = float(close[-1])
                    deviation_20 = (latest - ma20) / ma20 if ma20 > 0 else 0.0

                    if deviation_20 > self.trend_params['bull_threshold'] and latest > ma5 > ma20:
                        trend = MarketTrend.BULL
                        score = min(100.0, 60.0 + deviation_20 * 200)
                    elif deviation_20 < self.trend_params['bear_threshold'] and latest < ma5 < ma20:
                        trend = MarketTrend.BEAR
                        score = max(0.0, 40.0 + deviation_20 * 200)
                    else:
                        trend = MarketTrend.SIDEWAYS
                        score = 50.0 + deviation_20 * 100

                    score = max(0.0, min(100.0, score))
                    index_scores[idx_name] = score
                    index_trends[idx_name] = trend

                    logger.info(f"[Layer1] {idx_name}指数: {latest:.2f} "
                                f"(MA5={ma5:.2f}, MA20={ma20:.2f}), "
                                f"偏离={deviation_20:.2%}, 趋势={trend.value}, 评分={score:.0f}")

                except Exception as e:
                    logger.warning(f"[Layer1] 获取{idx_name}指数数据失败: {e}")

            if index_scores:
                weighted_score = sum(
                    index_scores[name] * self.index_trend_weights.get(name, 0.2)
                    for name in index_scores
                )
                total_weight = sum(
                    self.index_trend_weights.get(name, 0.2)
                    for name in index_scores
                )
                result.trend_score = weighted_score / total_weight if total_weight > 0 else 50.0

                bull_count = sum(1 for t in index_trends.values() if t == MarketTrend.BULL)
                bear_count = sum(1 for t in index_trends.values() if t == MarketTrend.BEAR)
                if bull_count > bear_count and bull_count >= len(index_trends) * 0.5:
                    result.index_trend = MarketTrend.BULL
                elif bear_count > bull_count and bear_count >= len(index_trends) * 0.5:
                    result.index_trend = MarketTrend.BEAR
                else:
                    result.index_trend = MarketTrend.SIDEWAYS

                logger.info(f"[Layer1] 综合趋势: {result.index_trend.value}, "
                            f"加权评分={result.trend_score:.0f} "
                            f"(多头{bull_count}/空头{bear_count}/{len(index_trends)}个指数)")
            else:
                result.trend_score = 50.0
                logger.warning("[Layer1] 所有指数数据获取失败，使用默认趋势评分")

        except Exception as e:
            logger.error(f"[Layer1] 指数趋势分析失败: {e}")
            result.trend_score = 50.0

    def _analyze_volume(self, trade_date: str, result: MarketEnvResult):
        """分析市场量能（全市场总成交额 + 各指数成交额明细）"""
        try:
            end_date = trade_date
            start_date = self.date_utils.get_n_trade_dates_before(30, trade_date)

            total_amount = 0.0
            index_volumes = {}

            for idx_name, idx_code in self.index_codes.items():
                try:
                    df = self.dm.get_index_daily(idx_code, start_date, end_date)
                    if df is None or df.empty:
                        continue

                    df = df.sort_values('trade_date')
                    if 'amount' in df.columns and len(df) > 0:
                        idx_amount = float(df['amount'].values[-1]) / 1e5
                        index_volumes[idx_name] = idx_amount
                        logger.info(f"[Layer1] {idx_name}成交额: {idx_amount:.0f}亿")
                except Exception as e:
                    logger.debug(f"[Layer1] 获取{idx_name}成交额失败: {e}")

            try:
                all_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
                if all_df is not None and not all_df.empty and 'amount' in all_df.columns:
                    amounts = pd.to_numeric(all_df['amount'], errors='coerce')
                    total_amount = float(amounts.sum()) / 1e5
            except Exception as e:
                logger.debug(f"[Layer1] 全市场成交额汇总失败: {e}")

            if total_amount > 0:
                result.total_volume = total_amount
            elif index_volumes:
                result.total_volume = index_volumes.get('sh', 0.0)
            else:
                result.total_volume = 0.0

            sh_df = self.dm.get_index_daily(
                self.index_codes['sh'], start_date, end_date
            )

            if sh_df is not None and not sh_df.empty and 'amount' in sh_df.columns:
                sh_df = sh_df.sort_values('trade_date')
                amounts = sh_df['amount'].values

                lookback = self.volume_params['lookback_days']
                if len(amounts) >= lookback + 1:
                    avg_amount = np.mean(amounts[-(lookback+1):-1])
                    result.volume_5d_avg = float(avg_amount) / 1e5
                    result.volume_ratio = float(amounts[-1] / avg_amount) if avg_amount > 0 else 1.0
                else:
                    result.volume_ratio = 1.0
            else:
                result.volume_ratio = 1.0

            if result.volume_ratio > self.volume_params['expand_ratio']:
                result.volume_state = VolumeState.EXPANDING
                result.volume_score = min(100.0, 50.0 + (result.volume_ratio - 1) * 100)
            elif result.volume_ratio < self.volume_params['shrink_ratio']:
                result.volume_state = VolumeState.SHRINKING
                result.volume_score = max(0.0, 50.0 - (1 - result.volume_ratio) * 100)
            else:
                result.volume_state = VolumeState.FLAT
                result.volume_score = 50.0

            logger.info(f"[Layer1] 全市场成交额: {result.total_volume:.0f}亿, "
                       f"量比: {result.volume_ratio:.2f}, "
                       f"状态={result.volume_state.value}, 评分={result.volume_score:.0f}")

        except Exception as e:
            logger.error(f"[Layer1] 量能分析失败: {e}")
            result.volume_score = 50.0

    def _analyze_market_width(self, trade_date: str, result: MarketEnvResult):
        """分析市场宽度（涨跌家数比，使用全市场个股日线数据统计）"""
        try:
            up_count = 0
            down_count = 0
            flat_count = 0

            try:
                all_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
                if all_df is not None and not all_df.empty:
                    if 'pct_chg' in all_df.columns:
                        pct_values = pd.to_numeric(all_df['pct_chg'], errors='coerce')
                        up_count = int((pct_values > 0).sum())
                        down_count = int((pct_values < 0).sum())
                        flat_count = int((pct_values == 0).sum())

                    result.up_count = up_count
                    result.down_count = down_count
                    result.flat_count = flat_count
                    logger.info(f"[Layer1] 通过全市场日线数据统计涨跌家数: 涨{up_count}/跌{down_count}/平{flat_count}")
            except Exception as e:
                logger.debug(f"[Layer1] 全市场日线数据获取失败: {e}")

            total = result.up_count + result.down_count + result.flat_count
            if total > 0:
                result.up_ratio = result.up_count / total

                if result.up_ratio >= self.width_params['strong_threshold']:
                    result.market_width = MarketWidth.STRONG
                    result.width_score = 80.0 + (result.up_ratio - 0.7) * 100
                elif result.up_ratio >= self.width_params['normal_threshold']:
                    result.market_width = MarketWidth.NORMAL
                    result.width_score = 50.0 + (result.up_ratio - 0.4) * 100
                elif result.up_ratio >= self.width_params['weak_threshold']:
                    result.market_width = MarketWidth.WEAK
                    result.width_score = 20.0 + (result.up_ratio - 0.2) * 100
                else:
                    result.market_width = MarketWidth.EXTREME_WEAK
                    result.width_score = result.up_ratio * 100

                result.width_score = max(0.0, min(100.0, result.width_score))

                logger.info(f"[Layer1] 涨跌比: {result.up_count}/{result.down_count}/{result.flat_count}, "
                           f"上涨比例={result.up_ratio:.1%}, "
                           f"宽度={result.market_width.value}, 评分={result.width_score:.0f}")
            else:
                if result.sh_index_change_pct > 1.0:
                    result.market_width = MarketWidth.STRONG
                    result.width_score = 75.0
                elif result.sh_index_change_pct > 0:
                    result.market_width = MarketWidth.NORMAL
                    result.width_score = 55.0
                elif result.sh_index_change_pct > -1.0:
                    result.market_width = MarketWidth.WEAK
                    result.width_score = 35.0
                else:
                    result.market_width = MarketWidth.EXTREME_WEAK
                    result.width_score = 15.0

                logger.info(f"[Layer1] 无法获取涨跌家数，使用指数代理: "
                           f"宽度={result.market_width.value}, 评分={result.width_score:.0f}")

        except Exception as e:
            logger.error(f"[Layer1] 市场宽度分析失败: {e}")
            result.width_score = 50.0

    def _analyze_limit_up_continuity(self, trade_date: str, result: MarketEnvResult):
        """分析昨日涨停股今日表现（高开比例 + 收红比例），同时拆解首板子集"""
        try:
            prev_date = self.date_utils.get_n_trade_dates_before(1, trade_date)

            prev_zt = self.dm.get_limit_up_pool(prev_date)
            if prev_zt is None or prev_zt.empty:
                logger.warning(f"[Layer1] 无法获取{prev_date}涨停数据，跳过涨停连续性分析")
                return

            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in prev_zt.columns:
                    ts_code_col = col
                    break

            if ts_code_col is None:
                logger.warning("[Layer1] 涨停池缺少股票代码列")
                return

            prev_zt[ts_code_col] = prev_zt[ts_code_col].astype(str)

            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty:
                logger.warning("[Layer1] 无法获取今日全市场数据")
                return

            today_df['ts_code'] = today_df['ts_code'].astype(str)

            def _calc_continuity(subset_df, label):
                """对涨停子集计算高开比例和收红比例"""
                codes = subset_df[ts_code_col].tolist()
                today_sub = today_df[today_df['ts_code'].isin(codes)]
                if today_sub.empty:
                    return 0, 0.0, 0.0

                total = len(today_sub)
                gap_up = int((today_sub['open'] > today_sub['pre_close']).sum())
                positive = int((today_sub['close'] > today_sub['pre_close']).sum())
                gap_ratio = gap_up / total if total > 0 else 0.0
                pos_ratio = positive / total if total > 0 else 0.0

                logger.info(f"[Layer1] {label}: {total}只, 高开{gap_up}只({gap_ratio:.1%}), "
                            f"收红{positive}只({pos_ratio:.1%})")
                return total, gap_ratio, pos_ratio

            # 全部涨停
            total, gap_ratio, pos_ratio = _calc_continuity(prev_zt, "昨日全部涨停")
            result.prev_limit_up_total = total
            result.prev_limit_up_gap_up_ratio = gap_ratio
            result.prev_limit_up_positive_ratio = pos_ratio

            # 首板子集（连板数 == 1）
            board_col = None
            for col in ['连板数', 'limit_times']:
                if col in prev_zt.columns:
                    board_col = col
                    break

            if board_col:
                first_board = prev_zt[prev_zt[board_col].fillna(1).astype(int) == 1]
                if not first_board.empty:
                    fb_total, fb_gap, fb_pos = _calc_continuity(first_board, "昨日首板")
                    result.prev_first_board_total = fb_total
                    result.prev_first_board_gap_up_ratio = fb_gap
                    result.prev_first_board_positive_ratio = fb_pos
                else:
                    logger.info("[Layer1] 昨日无首板股票")
            else:
                logger.warning("[Layer1] 涨停池缺少连板数列，无法拆解首板")

        except Exception as e:
            logger.error(f"[Layer1] 涨停连续性分析失败: {e}")

    def _calculate_composite_score(self, result: MarketEnvResult):
        """计算综合环境评分"""
        result.composite_score = (
            self.weights['trend'] * result.trend_score +
            self.weights['volume'] * result.volume_score +
            self.weights['width'] * result.width_score
        )

        # 风险等级判断
        if result.composite_score >= 75:
            result.risk_level = "低"
            result.suggested_position = "60-80%"
        elif result.composite_score >= 60:
            result.risk_level = "中等"
            result.suggested_position = "40-60%"
        elif result.composite_score >= 40:
            result.risk_level = "中等偏高"
            result.suggested_position = "20-40%"
        elif result.composite_score >= 25:
            result.risk_level = "高"
            result.suggested_position = "10-20%"
        else:
            result.risk_level = "极高"
            result.suggested_position = "0-10%（空仓或极小仓位）"

        logger.info(f"[Layer1] 综合评分={result.composite_score:.0f}, "
                   f"风险等级={result.risk_level}, 建议仓位={result.suggested_position}")

    def _generate_summary(self, result: MarketEnvResult):
        """生成分析摘要"""
        parts = []

        # 趋势描述
        trend_desc = {
            MarketTrend.BULL: "指数处于多头排列，均线向上发散",
            MarketTrend.BEAR: "指数处于空头排列，均线向下发散",
            MarketTrend.SIDEWAYS: "指数处于震荡格局，方向不明",
        }
        parts.append(f"【趋势】{trend_desc.get(result.index_trend, '未知')}")

        # 量能描述
        volume_desc = {
            VolumeState.EXPANDING: "市场放量，资金参与度提升",
            VolumeState.SHRINKING: "市场缩量，资金观望情绪浓厚",
            VolumeState.FLAT: "量能平稳，维持正常水平",
        }
        parts.append(f"【量能】{volume_desc.get(result.volume_state, '未知')}")

        # 宽度描述
        width_desc = {
            MarketWidth.STRONG: "普涨格局，赚钱效应好",
            MarketWidth.NORMAL: "分化行情，结构性机会",
            MarketWidth.WEAK: "多数下跌，亏钱效应明显",
            MarketWidth.EXTREME_WEAK: "极端弱势，系统性风险",
        }
        parts.append(f"【宽度】{width_desc.get(result.market_width, '未知')}")

        parts.append(f"【综合】评分{result.composite_score:.0f}/100，风险{result.risk_level}，建议仓位{result.suggested_position}")

        result.analysis_summary = "；".join(parts)

    def cross_analyze_with_emotion(self, market_env: MarketEnvResult,
                                    emotion_cycle: str) -> str:
        """
        大盘环境与情绪周期交叉分析

        Args:
            market_env: 大盘环境分析结果
            emotion_cycle: 情绪周期名称（高潮期/上升期/震荡期/退潮期/冰点期）

        Returns:
            交叉判断文本
        """
        score = market_env.composite_score

        if score >= 60 and emotion_cycle in ['上升期', '高潮期']:
            return "大盘+情绪共振向上，可积极做多，重仓参与主线"
        elif score >= 60 and emotion_cycle in ['冰点期', '退潮期']:
            return "大盘强势但情绪退潮，警惕高位股补跌，关注低位新方向"
        elif score < 40 and emotion_cycle in ['高潮期']:
            return "大盘弱势但情绪高潮，警惕诱多陷阱，逢高减仓"
        elif score < 40 and emotion_cycle in ['冰点期', '退潮期']:
            return "大盘+情绪共振向下，建议空仓观望，等待冰点后的转暖信号"
        elif score < 40 and emotion_cycle in ['上升期']:
            return "大盘弱势但情绪回暖，可能是结构性机会，轻仓试错"
        else:
            return "大盘与情绪均处于中性状态，控制仓位，精选个股"

    def to_dict(self, result: MarketEnvResult) -> Dict:
        """将分析结果转换为字典"""
        return {
            'trade_date': result.trade_date,
            'sh_index': {'close': result.sh_index_close, 'change_pct': result.sh_index_change_pct},
            'sz_index': {'close': result.sz_index_close, 'change_pct': result.sz_index_change_pct},
            'cyb_index': {'close': result.cyb_index_close, 'change_pct': result.cyb_index_change_pct},
            'kcb_index': {'close': result.kcb_index_close, 'change_pct': result.kcb_index_change_pct},
            'bj_index': {'close': result.bj_index_close, 'change_pct': result.bj_index_change_pct},
            'trend': {'state': result.index_trend.value, 'score': result.trend_score},
            'volume': {
                'total': result.total_volume,
                'ratio': result.volume_ratio,
                'state': result.volume_state.value,
                'score': result.volume_score,
            },
            'width': {
                'up_count': result.up_count,
                'down_count': result.down_count,
                'flat_count': result.flat_count,
                'up_ratio': result.up_ratio,
                'state': result.market_width.value,
                'score': result.width_score,
            },
            'limit_up_continuity': {
                'total': result.prev_limit_up_total,
                'gap_up_ratio': result.prev_limit_up_gap_up_ratio,
                'positive_ratio': result.prev_limit_up_positive_ratio,
            },
            'first_board_continuity': {
                'total': result.prev_first_board_total,
                'gap_up_ratio': result.prev_first_board_gap_up_ratio,
                'positive_ratio': result.prev_first_board_positive_ratio,
            },
            'composite_score': result.composite_score,
            'risk_level': result.risk_level,
            'suggested_position': result.suggested_position,
            'cross_judgment': result.cross_judgment,
            'analysis_summary': result.analysis_summary,
        }