"""
情绪周期识别引擎 - 系统化判断市场所处的情绪阶段

核心决策公式: 赚钱效应 = 情绪周期 × 资金共识 × 筹码结构

思维模式:
  - 不预测，只跟随
  - 不抄底，只接力
  - 不幻想，只应对
  - 不做杂毛，只做核心

禁止行为:
  - 线性外推（"已经涨了这么多应该..."）
  - 价值投资思维（"基本面好可以拿..."）
  - 成本锚定（"等回本再卖..."）
  - 随意补仓（亏损加仓摊低成本）
"""
import pandas as pd
from typing import Dict, List, Optional
from enum import Enum
from dataclasses import dataclass
import loguru

logger = loguru.logger

# —— 分群口径（流通市值，单位：元）。默认 小<100亿 / 中军100-500亿 / 大>500亿。
# 仅用于 metrics 展示，不参与情绪周期评分。运行时优先读 emotion_cycle_config.yaml
# 的 phase_model.cohort（单位：亿元），失败回退下列默认值。
COHORT_CAP_SMALL_MAX = 100 * 1e8     # 小票流通市值上限
COHORT_CAP_LARGE_MIN = 500 * 1e8     # 大票流通市值下限


def _load_cohort_cutoffs():
    """从 YAML 单一真源读取分群市值阈值（亿元→元），失败回退默认。"""
    try:
        from config.config_loader import get_emotion_cycle_config
        coh = ((get_emotion_cycle_config() or {}).get("phase_model") or {}).get("cohort") or {}
        small = float(coh.get("small_max_yi", 100)) * 1e8
        large = float(coh.get("large_min_yi", 500)) * 1e8
        return small, large
    except Exception:
        return COHORT_CAP_SMALL_MAX, COHORT_CAP_LARGE_MIN


class EmotionCycle(Enum):
    """情绪周期阶段枚举"""
    BOOM = "高潮期"      # 高潮期: 涨停>100家，连板高度>7板，无核按钮
    RISE = "上升期"      # 上升期: 涨停50-100家，连板高度4-6板，有主线
    SHAKE = "震荡期"     # 震荡期: 涨停30-60家，连板高度3-5板，轮动快
    DECLINE = "退潮期"   # 退潮期: 涨停<30家，核按钮多，高标A杀
    FREEZE = "冰点期"    # 冰点期: 涨停<20家，连板高度<3板，地量


@dataclass
class CycleStrategy:
    """情绪周期对应策略"""
    cycle: EmotionCycle
    description: str
    strategy: str
    position: str
    risk_level: str
    allowed_actions: List[str]
    forbidden_actions: List[str]


# 情绪周期策略配置
CYCLE_STRATEGIES = {
    EmotionCycle.BOOM: CycleStrategy(
        cycle=EmotionCycle.BOOM,
        description="涨停>100家，连板高度>7板，无核按钮",
        strategy="只卖不买，减仓龙头，准备撤退",
        position="<30%",
        risk_level="极高",
        allowed_actions=["减仓", "止盈", "空仓观望"],
        forbidden_actions=["开新仓", "追高", "打板", "接力"]
    ),
    EmotionCycle.RISE: CycleStrategy(
        cycle=EmotionCycle.RISE,
        description="涨停50-100家，连板高度4-6板，有主线",
        strategy="重仓龙头，做最强，积极参与",
        position="50-80%",
        risk_level="中等",
        allowed_actions=["打板", "接力", "做龙头", "做主线"],
        forbidden_actions=["做杂毛", "做跟风", "随意止损"]
    ),
    EmotionCycle.SHAKE: CycleStrategy(
        cycle=EmotionCycle.SHAKE,
        description="涨停30-60家，连板高度3-5板，轮动快",
        strategy="快进快出，只做模式内，严格止损",
        position="30-50%",
        risk_level="中高",
        allowed_actions=["低吸", "首板", "严格止损"],
        forbidden_actions=["追高", "重仓", "格局", "补仓"]
    ),
    EmotionCycle.DECLINE: CycleStrategy(
        cycle=EmotionCycle.DECLINE,
        description="涨停<30家，核按钮多，高标A杀",
        strategy="空仓或1成试错，禁止接力",
        position="0-10%",
        risk_level="高",
        allowed_actions=["空仓", "极小仓位试错", "观察"],
        forbidden_actions=["接力", "打板", "重仓", "抄底"]
    ),
    EmotionCycle.FREEZE: CycleStrategy(
        cycle=EmotionCycle.FREEZE,
        description="涨停<20家，连板高度<3板，地量",
        strategy="准备新周期，试首板，等信号",
        position="10-20%",
        risk_level="中等",
        allowed_actions=["试首板", "小仓位试错", "观察信号"],
        forbidden_actions=["重仓", "接力高位", "盲目抄底"]
    )
}


class EmotionCycleEngine:
    """
    情绪周期识别引擎
    
    基于多维度指标判断市场所处的情绪阶段：
    1. 涨停家数 - 市场活跃度
    2. 连板高度 - 风险偏好
    3. 炸板率 - 市场分歧程度
    4. 核按钮数量 - 恐慌程度
    5. 涨停溢价 - 赚钱效应（T+1真实路径：前天涨停→昨日开盘买→今日开盘卖）
    6. 涨停胜率 - T+1开盘卖出胜率
    7. 平均赢面 - 盈利股票的平均收益
    8. 连板梯队完整性 - 生态健康度
    """
    
    def __init__(self, dm=None, repo=None):
        self.dm = dm  # DataManager 实例，用于获取价格数据
        # 只读仓库（仅在有 dm 时构造透传）
        if repo is None and dm is not None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(dm)
        self.repo = repo

    def get_strategy(self, cycle: EmotionCycle) -> CycleStrategy:
        """获取对应周期的策略建议"""
        return CYCLE_STRATEGIES.get(cycle, CYCLE_STRATEGIES[EmotionCycle.SHAKE])
    
    def analyze_market_data(self, 
                           limit_up_df: pd.DataFrame,
                           limit_down_df: pd.DataFrame = None,
                           prev_limit_up_df: pd.DataFrame = None,
                           prev_day_limit_up_df: pd.DataFrame = None,
                           prev_metrics: Dict = None) -> Dict:
        """
        分析市场数据，识别情绪周期
        
        Args:
            limit_up_df: 当日涨停数据
            limit_down_df: 当日跌停数据（可选）
            prev_limit_up_df: 前天(T-2)涨停数据（用于T+1溢价计算：前天涨停→昨日开盘买→今日开盘卖）
            prev_day_limit_up_df: 昨日(T-1)涨停数据（P1：用于真·晋级率，仅展示不参与评分）
            prev_metrics: 昨日快照的情绪 metrics（P3：用于真·环比动量，可选）
            
        Returns:
            包含情绪周期分析结果的字典
        """
        if limit_up_df.empty:
            return {
                'cycle': EmotionCycle.FREEZE,
                'strategy': self.get_strategy(EmotionCycle.FREEZE),
                'metrics': {},
                'warning': '无涨停数据，可能为冰点期或数据异常'
            }
        
        # 计算各项指标
        limit_up_count = len(limit_up_df)
        
        # 最高连板高度
        max_board_height = limit_up_df.get('limit_times', limit_up_df.get('连板数', 1)).max()
        if pd.isna(max_board_height):
            max_board_height = 1
        max_board_height = int(max_board_height)
        
        # 炸板率
        open_times_col = 'open_times' if 'open_times' in limit_up_df.columns else '炸板次数'
        if open_times_col in limit_up_df.columns:
            broken_count = len(limit_up_df[limit_up_df[open_times_col] > 0])
            broken_rate = (broken_count / limit_up_count) * 100 if limit_up_count > 0 else 0
        else:
            broken_rate = 0
        
        # 核按钮数量
        nuclear_button_count = len(limit_down_df) if limit_down_df is not None else 0
        
        # 涨停溢价率和胜率计算（T+1真实路径：前天涨停→昨日开盘买→今日开盘卖）
        prev_limit_up_premium = None
        win_rate = None  # 胜率
        avg_profit = None  # 平均赢面
        
        if prev_limit_up_df is not None and not prev_limit_up_df.empty:
            # 计算前天涨停股票在T+1模式下的真实溢价表现
            premium_data = self._calculate_prev_limit_up_performance(prev_limit_up_df)
            prev_limit_up_premium = premium_data.get('avg_premium')
            win_rate = premium_data.get('win_rate')
            avg_profit = premium_data.get('avg_profit')
        
        # 连板分布
        board_distribution = {}
        limit_times_col = 'limit_times' if 'limit_times' in limit_up_df.columns else '连板数'
        if limit_times_col in limit_up_df.columns:
            board_distribution = limit_up_df[limit_times_col].value_counts().to_dict()

        # 计算连板率 (连板股数/涨停股总数)
        continuous_count = 0
        if limit_times_col in limit_up_df.columns:
            # 连板数>=2的认为是连板股
            continuous_count = len(limit_up_df[limit_up_df[limit_times_col] >= 2])
        continuous_rate = (continuous_count / limit_up_count * 100) if limit_up_count > 0 else 0

        # 跌停家数
        limit_down_count = len(limit_down_df) if limit_down_df is not None else 0

        # B1: 首板/连板比
        first_board_ratio = None
        if limit_times_col in limit_up_df.columns:
            board_vals = pd.to_numeric(limit_up_df[limit_times_col], errors='coerce').fillna(1).astype(int)
            first_count = int((board_vals == 1).sum())
            multi_count = int((board_vals >= 2).sum())
            first_board_ratio = first_count / multi_count if multi_count > 0 else (first_count if first_count > 0 else 0.0)

        # B2: 一字板占比
        one_word_ratio = None
        time_col = None
        for col in ['first_time', '首次封板时间', '封板时间']:
            if col in limit_up_df.columns:
                time_col = col
                break
        if time_col and limit_up_count > 0:
            one_word_count = int((limit_up_df[time_col].astype(str).str.strip() <= '09:25:00').sum())
            one_word_ratio = one_word_count / limit_up_count

        # B3: 尾盘板占比
        tail_board_ratio = None
        if time_col and limit_up_count > 0:
            tail_count = int((limit_up_df[time_col].astype(str).str.strip() >= '14:30:00').sum())
            tail_board_ratio = tail_count / limit_up_count

        # B4: 地天板/天地板数量（需要daily数据，此处用涨停池代理）
        extreme_reversal_count = 0

        # B5: 涨停股平均封单比
        avg_seal_ratio = None
        seal_col = None
        for col in ['封单金额', 'seal_amount', 'limit_amount']:
            if col in limit_up_df.columns:
                seal_col = col
                break
        if seal_col:
            seals = pd.to_numeric(limit_up_df[seal_col], errors='coerce').dropna()
            if not seals.empty:
                avg_seal_ratio = float(seals.mean())

        # C1: 平均封板时间（从首次封板到收盘的时长，秒）
        avg_seal_time = None
        if time_col and limit_up_count > 0:
            times = limit_up_df[time_col].astype(str).str.strip()
            valid_times = times[times.str.match(r'^\d{2}:\d{2}:\d{2}$')]
            if not valid_times.empty:
                def _time_to_seconds(t):
                    parts = t.split(':')
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                seconds = valid_times.apply(_time_to_seconds)
                avg_seal_time = float(seconds.mean())

        metrics = {
            'limit_up_count': limit_up_count,
            'max_board_height': max_board_height,
            'broken_rate': round(broken_rate, 2),
            'nuclear_button_count': nuclear_button_count,
            'prev_limit_up_premium': prev_limit_up_premium,
            'win_rate': win_rate,
            'avg_profit': avg_profit,
            'board_distribution': board_distribution,
            'continuous_rate': round(continuous_rate, 2),
            'limit_down_count': limit_down_count,
            'limit_down_ratio': round(limit_down_count / limit_up_count, 2) if limit_up_count > 0 else 0,
            'first_board_ratio': first_board_ratio,
            'one_word_ratio': one_word_ratio,
            'tail_board_ratio': tail_board_ratio,
            'extreme_reversal_count': extreme_reversal_count,
            'avg_seal_ratio': avg_seal_ratio,
            'avg_seal_time': avg_seal_time,
            # P1 新增（仅展示，不参与评分）：大/中军/小票分群子指标 + 真·晋级率
            'cohorts': self._calculate_cohort_metrics(limit_up_df, limit_down_df),
            'promotion': self._calculate_promotion_rate(limit_up_df, prev_day_limit_up_df),
        }

        # 循环相位模型：情绪周期的唯一判定来源（小票 / 中军 / 大票分群 + 方向性相位）
        phase_model = None
        try:
            from core.analysis.emotion_phase_model import compute_phase_model
            phase_model = compute_phase_model(metrics, prev_metrics=prev_metrics)
        except Exception as e:
            logger.warning(f"[phase_model] 相位模型计算失败: {e}")

        cycle_name = (phase_model or {}).get('legacy_cycle_name') or EmotionCycle.SHAKE.value
        cycle = next((c for c in EmotionCycle if c.value == cycle_name), EmotionCycle.SHAKE)
        strategy = self.get_strategy(cycle)

        return {
            'cycle': cycle,
            'cycle_name': cycle_name,
            'strategy': strategy,
            'metrics': metrics,
            'scores': (phase_model or {}).get('scores', {}),
            'phase_model': phase_model,
        }
    
    @staticmethod
    def _first_col(df: pd.DataFrame, names) -> Optional[str]:
        """返回 df 中第一个存在的列名（兼容中英文列）。"""
        for n in names:
            if n in df.columns:
                return n
        return None

    def _calculate_cohort_metrics(self, limit_up_df: pd.DataFrame,
                                  limit_down_df: pd.DataFrame = None) -> Optional[Dict]:
        """P1：按流通市值把涨停/跌停拆为 小票/中军/大票，各算子情绪指标。

        仅写入 metrics 供展示与后续建模观察，**不参与情绪周期评分**。
        失败返回 None，绝不影响主流程。
        """
        try:
            if limit_up_df is None or limit_up_df.empty:
                return None
            cap_col = self._first_col(limit_up_df, ['float_mv', '流通市值'])
            if cap_col is None:
                return None
            board_col = self._first_col(limit_up_df, ['limit_times', '连板数'])
            open_col = self._first_col(limit_up_df, ['open_times', '炸板次数'])

            caps = pd.to_numeric(limit_up_df[cap_col], errors='coerce')

            down_caps = None
            if limit_down_df is not None and not limit_down_df.empty:
                dcol = self._first_col(limit_down_df, ['float_mv', '流通市值'])
                if dcol is not None:
                    down_caps = pd.to_numeric(limit_down_df[dcol], errors='coerce')

            small_cut, large_cut = _load_cohort_cutoffs()

            def _masks(series):
                small = series < small_cut
                large = series >= large_cut
                mid = (~small) & (~large)
                return {'small': small, 'mid': mid, 'large': large}

            up_masks = _masks(caps)
            down_masks = _masks(down_caps) if down_caps is not None else None

            result = {}
            for name in ('small', 'mid', 'large'):
                mask = up_masks[name].fillna(False)
                sub = limit_up_df[mask.values]
                cnt = int(len(sub))
                if board_col and cnt > 0:
                    boards = pd.to_numeric(sub[board_col], errors='coerce').fillna(1).astype(int)
                    max_board = int(boards.max())
                    cont = int((boards >= 2).sum())
                else:
                    max_board, cont = 0, 0
                cont_rate = round(cont / cnt * 100, 2) if cnt > 0 else 0.0
                if open_col and cnt > 0:
                    broken = int((pd.to_numeric(sub[open_col], errors='coerce').fillna(0) > 0).sum())
                    broken_rate = round(broken / cnt * 100, 2)
                else:
                    broken_rate = 0.0
                down_cnt = int(down_masks[name].fillna(False).sum()) if down_masks is not None else 0
                result[name] = {
                    'limit_up_count': cnt,
                    'max_board_height': max_board,
                    'continuous_count': cont,
                    'continuous_rate': cont_rate,
                    'broken_rate': broken_rate,
                    'limit_down_count': down_cnt,
                }
            return result
        except Exception as e:
            logger.warning(f"[cohort] 分群指标计算失败: {e}")
            return None

    def _calculate_promotion_rate(self, limit_up_df: pd.DataFrame,
                                  prev_day_limit_up_df: pd.DataFrame = None) -> Optional[Dict]:
        """P1：真·晋级率。昨日(T-1) k 板今日(T) 仍涨停（晋级 k+1）的比例。

        与 continuous_rate（存量连板占比）不同，这是跨日的**领先**指标。
        仅写入 metrics 供展示，**不参与评分**。失败返回 None。
        """
        try:
            if (prev_day_limit_up_df is None or prev_day_limit_up_df.empty
                    or limit_up_df is None or limit_up_df.empty):
                return None
            up_code = self._first_col(limit_up_df, ['ts_code', '代码', 'code'])
            pv_code = self._first_col(prev_day_limit_up_df, ['ts_code', '代码', 'code'])
            pv_board = self._first_col(prev_day_limit_up_df, ['limit_times', '连板数'])
            if not all([up_code, pv_code, pv_board]):
                return None

            today_codes = set(limit_up_df[up_code].astype(str))
            prev = prev_day_limit_up_df[[pv_code, pv_board]].copy()
            prev[pv_board] = pd.to_numeric(prev[pv_board], errors='coerce').fillna(1).astype(int)

            def _rate(level_mask):
                codes = prev[level_mask][pv_code].astype(str)
                denom = int(len(codes))
                if denom == 0:
                    return None, denom
                promoted = int(sum(1 for c in codes if c in today_codes))
                return round(promoted / denom * 100, 2), denom

            r_1to2, d12 = _rate(prev[pv_board] == 1)
            r_2to3, d23 = _rate(prev[pv_board] == 2)
            r_high, dh = _rate(prev[pv_board] >= 3)

            overall_denom = int(len(prev))
            overall_prom = int(sum(1 for c in prev[pv_code].astype(str) if c in today_codes))
            overall = round(overall_prom / overall_denom * 100, 2) if overall_denom else None

            return {
                'overall': overall,
                'rate_1to2': r_1to2,
                'rate_2to3': r_2to3,
                'rate_high': r_high,
                'sample': {'prev_total': overall_denom, 'b1': d12, 'b2': d23, 'b3plus': dh},
            }
        except Exception as e:
            logger.warning(f"[promotion] 晋级率计算失败: {e}")
            return None

    def _calculate_prev_limit_up_performance(self, prev_limit_up_df: pd.DataFrame) -> Dict:
        """
        计算前天涨停股票在T+1交易模式下的真实表现

        T+1市场真实交易路径：
        - T-2日（前天）：股票涨停，进入候选池
        - T-1日（昨日）：开盘买入（T+1市场下最早可买入时机）
        - T日（今日）：开盘卖出（买入后次日方可卖出）

        计算指标：
        1. 平均溢价率：(今日开盘价 - 昨日开盘价) / 昨日开盘价 × 100%
        2. 胜率：今日开盘价 > 昨日开盘价的比例
        3. 平均赢面：盈利股票的平均收益率

        Args:
            prev_limit_up_df: 前天涨停数据，需要包含股票代码

        Returns:
            {
                'avg_premium': 平均溢价率(%),
                'win_rate': 胜率(%),
                'avg_profit': 平均赢面(%)
            }
        """
        result = {
            'avg_premium': None,
            'win_rate': None,
            'avg_profit': None
        }

        if prev_limit_up_df.empty:
            return result

        if not hasattr(self, 'dm') or self.dm is None:
            logger.warning("[_calculate_prev_limit_up_performance] 未提供DataManager，无法计算溢价率")
            return result

        try:
            premiums = []
            profits = []

            code_col = None
            for col in ['ts_code', '代码', 'code', 'stock_code']:
                if col in prev_limit_up_df.columns:
                    code_col = col
                    break

            if code_col is None:
                logger.warning("[_calculate_prev_limit_up_performance] 未找到股票代码列")
                return result

            trade_date = None
            for col in ['trade_date', '日期', 'date']:
                if col in prev_limit_up_df.columns:
                    trade_date = prev_limit_up_df[col].iloc[0]
                    break

            if trade_date is None:
                logger.warning("[_calculate_prev_limit_up_performance] 未找到交易日期")
                return result

            # T+1真实交易路径：
            # trade_date = T-2（前天，涨停日）
            # buy_date = T-1（昨日，开盘买入）
            # sell_date = T（今日，开盘卖出）
            buy_date = self.repo.date_utils.get_next_trade_date(str(trade_date))
            sell_date = self.repo.date_utils.get_next_trade_date(buy_date)

            logger.info(f"[_calculate_prev_limit_up_performance] T+1模拟: "
                       f"{trade_date}涨停 → {buy_date}开盘买入 → {sell_date}开盘卖出")

            for _, row in prev_limit_up_df.iterrows():
                try:
                    ts_code = row[code_col]

                    df = self.repo.get_stock_daily(ts_code, buy_date, sell_date)
                    if df.empty or len(df) < 2:
                        continue

                    df = df.sort_values('trade_date')

                    buy_open = float(df.iloc[0].get('open', 0))
                    sell_open = float(df.iloc[1].get('open', 0))

                    if buy_open <= 0 or sell_open <= 0:
                        continue

                    premium = (sell_open - buy_open) / buy_open * 100
                    premiums.append(premium)

                    if sell_open > buy_open:
                        profits.append(premium)

                except Exception as e:
                    logger.debug(f"计算单只股票溢价失败: {e}")
                    continue

            if premiums:
                result['avg_premium'] = round(sum(premiums) / len(premiums), 2)
                result['win_rate'] = round(len(profits) / len(premiums) * 100, 2)

                if profits:
                    result['avg_profit'] = round(sum(profits) / len(profits), 2)
                else:
                    result['avg_profit'] = 0

                logger.info(f"[_calculate_prev_limit_up_performance] 统计完成: "
                           f"样本数={len(premiums)}, 平均溢价={result['avg_premium']}%, "
                           f"胜率={result['win_rate']}%, 平均赢面={result['avg_profit']}%")
            else:
                logger.warning("[_calculate_prev_limit_up_performance] 无有效溢价数据")

        except Exception as e:
            logger.error(f"[_calculate_prev_limit_up_performance] 计算失败: {e}")

        return result