"""
回测引擎 - 基于交易计划的历史回测
核心功能：
1. 加载历史交易计划
2. 模拟T+1交易执行
3. 计算收益和回撤
4. 生成回测报告
"""
from core.data.minute_cache_files import minute_cache_exists, read_minute_csv
import pandas as pd
import numpy as np
import json
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple, Any
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import loguru

from backtest.matching_rules import limit_up_price, open_gap_pct
from backtest.minute_entry import (
    ENTRY_ACCELERATION,
    ENTRY_COMPARE,
    ENTRY_CONTINUATION,
    ENTRY_FIXED,
    ENTRY_HYBRID,
    ENTRY_MODES,
    ENTRY_WEAK,
    EntryDecision,
    MinuteEntryEvaluator,
    normalize_minute_bars,
    normalize_strategy_entry_modes,
    resolve_entry_deadline,
)
from backtest.trade_calendar import TradeCalendar
from core.signals.minute_amount_profile import MinuteAmountProfileRepository
from core.utils.sector_names import normalize_sector_names
from backtest.evidence_policy import (
    SKIP_CANDIDATES, ReplayEvidenceBlocked, missing_entry_evidence, requires_auction,
)

logger = loguru.logger


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 1000000.0  # 初始资金
    max_position_per_stock: float = 0.2  # 单票最大仓位20%
    max_total_position: float = 0.8  # 总仓位上限80%
    max_positions: int = 8  # 最多同时持仓只数（仅风控开启时生效）
    max_sector_concentration: float = 0.4  # 单一板块最大仓位（仅风控开启时生效）
    max_plan_rank: int = 0  # 0=不按名次截断，全部候选交给买点规则判断

    # 仓位算法：固定账户风险或基于历史已平仓样本的保守凯利。
    position_sizing_mode: str = "fixed_risk"
    fixed_risk_per_trade: float = 0.005
    kelly_fraction: float = 0.25
    kelly_min_samples: int = 50
    kelly_max_position: float = 0.10
    kelly_credibility: float = 0.80
    kelly_payoff_haircut: float = 0.80
    account_position_pct: float = 0.0  # >0 时由账户策略直接指定单票目标仓位
    rotation_enabled: bool = False
    rotation_min_edge: float = 6.0

    # 入场与市场分层
    entry_mode: str = ENTRY_HYBRID  # 默认按分钟执行弱转强+强势延续
    entry_confirm_deadline: str = "10:00:00"
    weak_entry_min_gap: float = -0.03
    weak_entry_max_gap: float = 0.01
    continuation_max_gap: float = 0.05
    acceleration_opening_minutes: int = 1
    continuation_opening_minutes: int = 3
    weak_opening_minutes: int = 5
    entry_min_amount_pace: float = 0.80
    entry_max_amount_pace: float = 3.00
    continuation_min_auction_volume_ratio: float = 0.008
    continuation_min_auction_amount: float = 5_000_000
    entry_confirmation_policy: Optional[Dict[str, Any]] = None
    three_board_feedback_enabled: Optional[bool] = None  # Defaults to production_rules for direct construction.
    three_board_feedback_limit_fraction: float = 0.50
    three_board_feedback_score_penalty: float = 20.0
    three_board_rebound_exit_gain: float = 0.02
    min_open_gap: float = 0.0  # 必须严格高开
    max_open_gap: float = 0.03  # 高开超过3%不追
    reduced_position_gap: float = 0.02  # 高开2%-3%仍可买，但降低追高风险
    high_gap_position_multiplier: float = 0.75  # 高开2%-3%使用原计划75%仓位
    market_entry_threshold: float = 60.0  # 模拟交易：60分以下停止开仓
    market_strong_threshold: float = 65.0  # 65分以上进入中性可交易区
    market_hot_threshold: float = 70.0  # 70分以上按强市处理
    neutral_market_max_rank: int = 1  # 中性市场只买第1名
    direct_entry_min_score: float = 74.0  # 达标候选可走竞价买点
    neutral_market_min_score: float = 80.0  # 60-65分市场只交易高确信候选
    active_market_min_score: float = 76.0  # 65-70分市场提高买点质量门槛
    intraday_strength_trigger_pct: float = 0.01  # 观察候选从开盘价上冲1%确认转强
    intraday_min_tech_score: float = 80.0
    intraday_min_sector_resonance: float = 60.0
    intraday_min_amount_ratio: float = 0.80
    intraday_max_amount_ratio: float = 1.50

    # 风控闸门总开关：关闭后不施加组合层约束（单票/总仓/持仓数/板块集中度），
    # 仅保留现金/价格有效性等基础校验，用于对比"无风控"模拟结果。个股止损止盈
    # 属于交易计划的退出策略，始终生效，不受此开关影响。
    risk_control: bool = True

    # 硬止损
    stop_loss_pct: float = 0.04  # 模拟交易硬止损线4%

    # 移动止盈（跟踪止损）
    trailing_stop: bool = True  # 启用跟踪止损
    trailing_stop_pct: float = 0.10  # 大趋势阶段从最高点回撤10%触发
    trailing_activation_pct: float = 0.05  # 盈利5%后启动跟踪止损
    trailing_mid_profit_pct: float = 0.10  # 盈利10%进入中段
    trailing_high_profit_pct: float = 0.20  # 盈利20%进入趋势段
    trailing_early_stop_pct: float = 0.04  # 盈利5%-10%时回撤4%退出
    trailing_mid_stop_pct: float = 0.06  # 盈利10%-20%时回撤6%退出

    # 时间止损
    time_stop_days: int = 5  # 持仓超过5天强制卖出
    time_stop_profit_threshold: float = 0.02  # 盈利低于2%时触发时间止损

    # 费用设置
    commission_rate: float = 0.0003  # 佣金率0.03%
    stamp_duty_rate: float = 0.0005  # Current sell-side stamp duty; historical runs must freeze costs.
    slippage: float = 0.002  # 滑点0.2%
    min_holding_days: int = 1  # 最小持仓天数（T+1）

    # 数据缺失时是否用随机价格兜底（B-1：默认关闭，缺数据则跳过该票，避免回测失真）
    use_simulated_prices: bool = False
    exit_minute_data_policy: str = "cache_or_fetch"
    replay_missing_evidence_policy: str = "legacy"
    replay_prefetch_failures: Optional[List[Dict[str, Any]]] = None
    production_rules: bool = False  # Explicit legacy research engines remain reproducible.
    production_strategy_ids: Optional[List[str]] = None
    daily_ohlc_path_policy: str = "conservative_stop_first"
    exit_policy_mode: str = "strategy"

    @classmethod
    def from_risk_config(
        cls, risk_config, *, initial_capital: Optional[float] = None,
        risk_control: Optional[bool] = None,
    ) -> "BacktestConfig":
        """Project the unified RiskConfig into the active backtest engine."""
        trailing_pct = max(float(risk_config.trailing_stop), 0.0)
        from backtest.confirmation_quality import policy_from_config
        return cls(
            initial_capital=float(initial_capital or risk_config.initial_capital),
            max_position_per_stock=float(risk_config.max_position_per_stock),
            max_total_position=float(risk_config.max_total_position),
            max_positions=int(risk_config.max_positions),
            max_sector_concentration=float(risk_config.max_sector_concentration),
            position_sizing_mode=str(getattr(risk_config, "position_sizing_mode", "fixed_risk")),
            fixed_risk_per_trade=float(getattr(risk_config, "fixed_risk_per_trade", 0.005)),
            kelly_fraction=float(getattr(risk_config, "kelly_fraction", 0.25)),
            kelly_min_samples=int(getattr(risk_config, "kelly_min_samples", 50)),
            kelly_max_position=float(getattr(risk_config, "kelly_max_position", 0.10)),
            kelly_credibility=float(getattr(risk_config, "kelly_credibility", 0.80)),
            kelly_payoff_haircut=float(getattr(risk_config, "kelly_payoff_haircut", 0.80)),
            min_open_gap=float(risk_config.min_open_gap),
            max_open_gap=float(risk_config.max_open_gap),
            reduced_position_gap=float(risk_config.reduced_position_gap),
            high_gap_position_multiplier=float(risk_config.high_gap_position_multiplier),
            entry_confirm_deadline=str(risk_config.entry_confirm_deadline),
            weak_entry_min_gap=float(risk_config.weak_entry_min_gap),
            weak_entry_max_gap=float(risk_config.weak_entry_max_gap),
            continuation_max_gap=float(risk_config.continuation_max_gap),
            acceleration_opening_minutes=risk_config.acceleration_opening_minutes,
            continuation_opening_minutes=risk_config.continuation_opening_minutes,
            weak_opening_minutes=risk_config.weak_opening_minutes,
            entry_min_amount_pace=float(risk_config.entry_min_amount_pace),
            entry_max_amount_pace=float(risk_config.entry_max_amount_pace),
            continuation_min_auction_volume_ratio=float(risk_config.continuation_min_auction_volume_ratio),
            continuation_min_auction_amount=float(risk_config.continuation_min_auction_amount),
            entry_confirmation_policy=asdict(policy_from_config(risk_config)),
            three_board_feedback_enabled=bool(risk_config.three_board_feedback_enabled),
            three_board_feedback_limit_fraction=float(risk_config.three_board_feedback_limit_fraction),
            three_board_feedback_score_penalty=float(risk_config.three_board_feedback_score_penalty),
            three_board_rebound_exit_gain=float(risk_config.three_board_rebound_exit_gain),
            market_entry_threshold=float(risk_config.market_entry_threshold),
            market_strong_threshold=float(risk_config.market_active_threshold),
            market_hot_threshold=float(risk_config.market_strong_threshold),
            neutral_market_max_rank=int(risk_config.neutral_market_max_rank),
            direct_entry_min_score=float(risk_config.direct_entry_min_score),
            neutral_market_min_score=float(risk_config.neutral_market_min_score),
            active_market_min_score=float(risk_config.active_market_min_score),
            intraday_strength_trigger_pct=float(risk_config.intraday_strength_trigger_pct),
            intraday_min_tech_score=float(risk_config.intraday_min_tech_score),
            intraday_min_sector_resonance=float(risk_config.intraday_min_sector_resonance),
            intraday_min_amount_ratio=float(risk_config.intraday_min_amount_ratio),
            intraday_max_amount_ratio=float(risk_config.intraday_max_amount_ratio),
            risk_control=bool(risk_config.enabled if risk_control is None else risk_control),
            stop_loss_pct=float(risk_config.hard_stop_loss),
            trailing_stop=trailing_pct > 0,
            trailing_stop_pct=trailing_pct,
            trailing_activation_pct=max(float(risk_config.trailing_activation), 0.0),
            trailing_mid_profit_pct=float(risk_config.trailing_mid_profit),
            trailing_high_profit_pct=float(risk_config.trailing_high_profit),
            trailing_early_stop_pct=float(risk_config.trailing_early_stop),
            trailing_mid_stop_pct=float(risk_config.trailing_mid_stop),
            time_stop_days=int(risk_config.time_stop_days),
            time_stop_profit_threshold=float(risk_config.time_stop_profit_threshold),
            commission_rate=float(risk_config.commission_rate),
            stamp_duty_rate=float(risk_config.stamp_duty_rate),
            slippage=float(risk_config.slippage),
            min_holding_days=int(risk_config.min_holding_days),
            exit_minute_data_policy=str(getattr(risk_config, "exit_minute_data_policy", "cache_or_fetch")),
            daily_ohlc_path_policy=str(getattr(risk_config, "daily_ohlc_path_policy", "conservative_stop_first")),
            exit_policy_mode=str(getattr(risk_config, "exit_policy_mode", "strategy")),
        )


@dataclass
class TradeRecord:
    """交易记录"""
    date: str
    stock_code: str
    stock_name: str
    pattern_type: str
    action: str  # BUY/SELL
    entry_price: float
    exit_price: Optional[float]
    shares: int
    position_size: float
    pnl: float
    pnl_pct: float
    holding_days: int
    hot_resonance: bool
    resonance_sectors: str
    stop_loss_triggered: bool = False
    take_profit_triggered: bool = False
    entry_date: str = ""
    exit_reason: str = ""
    plan_rank: int = 0
    plan_score: float = 0.0
    plan_reason: str = ""
    factor_metrics_json: str = ""
    factor_context_json: str = ""
    open_gap_pct: float = 0.0
    market_score: float = 0.0
    amount_ratio: float = 0.0
    entry_signal: str = ""
    entry_time: str = ""
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    sizing_method: str = ""
    sizing_rationale: str = ""
    sizing_sample_size: int = 0
    strategy_id: str = "default"
    strategy_name: str = ""
    strategy_version: str = ""
    strategy_sources: str = ""
    exit_time: str = ""
    exit_rule_version: str = ""
    cost_version: str = ""
    result_available_at: str = ""
    exit_evidence_json: str = ""


class BacktestEngine:
    """
    回测引擎
    基于每日交易计划进行历史回测
    """

    def __init__(self, data_manager, config: BacktestConfig = None, *, frozen_context=None):
        self.dm = data_manager
        self.config = config or BacktestConfig()
        self.calendar = frozen_context['calendar'] if frozen_context is not None else TradeCalendar()
        self.trade_history: List[TradeRecord] = []
        self.daily_nav: List[Dict] = []  # 每日净值
        self.current_positions: Dict[str, Dict] = {}  # 当前持仓
        self.cash: float = self.config.initial_capital
        self.total_capital: float = self.config.initial_capital
        self._last_entry_gap: Dict[str, float] = {}
        self._last_entry_signal: Dict[str, str] = {}
        self._last_entry_meta: Dict[str, Dict[str, Any]] = {}
        self._last_sizing_meta: Dict[str, Any] = {}
        self._minute_frames: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._exit_minute_frames: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._exit_scan_times: Dict[Tuple[str, str], str] = {}
        self._exit_evidence_failures: List[Dict[str, Any]] = []
        self._candidate_evidence_samples: set = set()
        self._candidate_evidence_skips: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._entry_evidence_missing: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._replay_blocked_evidence: List[Dict[str, Any]] = []
        self._suspension_statuses: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._suspended_sessions: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._exit_execution_audit: Dict[str, int] = {
            "minute_days": 0,
            "daily_fallback_days": 0,
            "minute_exit_triggers": 0,
            "daily_exit_triggers": 0,
            "ambiguous_daily_bars": 0,
        }
        self._auction_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._sector_peers: Dict[str, List[str]] = {}
        self._sector_evidence: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._sector_evidence_snapshots: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        self._sector_peer_failures: Dict[Tuple[str, str], str] = {}
        self._day_plans = pd.DataFrame()
        self._production_inputs = {}
        self._production_minute_prices = None
        self._replay_progress = None
        self._production_input_manifest = []
        self._production_rows = {}
        self._production_profiles = {}
        if self.config.production_rules:
            from risk.risk_config import RiskConfig
            if frozen_context is not None:
                self._production_profiles = deepcopy(frozen_context['profiles'])
                self._production_risk = RiskConfig.from_dict(frozen_context['risk_config'])
            else:
                from core.screening.strategy_profiles import StrategyProfileRepository
                profiles = StrategyProfileRepository().frozen()
                self._production_profiles = {p['id']: profiles.get_profile(p['id']) for p in profiles.list_profiles()}
                self._production_risk = RiskConfig.load()
        self.entry_attempts: List[Dict[str, Any]] = []
        self.entry_candidate_count: int = 0
        self.amount_profiles = MinuteAmountProfileRepository()
        from backtest.confirmation_quality import policy_from_config
        self.minute_evaluator = MinuteEntryEvaluator(
            deadline=self.config.entry_confirm_deadline,
            weak_min_gap=self.config.weak_entry_min_gap,
            weak_max_gap=self.config.weak_entry_max_gap,
            continuation_max_gap=self.config.continuation_max_gap,
            acceleration_opening_minutes=self.config.acceleration_opening_minutes,
            continuation_opening_minutes=self.config.continuation_opening_minutes,
            weak_opening_minutes=self.config.weak_opening_minutes,
            min_amount_pace=self.config.entry_min_amount_pace,
            max_amount_pace=self.config.entry_max_amount_pace,
            min_auction_volume_ratio=self.config.continuation_min_auction_volume_ratio,
            min_auction_amount=self.config.continuation_min_auction_amount,
            confirmation_policy=policy_from_config(self.config),
        )
        from core.signals.board_feedback import feedback_policy
        self.minute_evaluator.board_feedback_policy = feedback_policy(self.config)
        self._board_feedback_repository = None
        self._board_feedback_contexts = {}

    def export_state(self) -> Dict[str, Any]:
        """导出可落盘的账户状态，用于单日接力回测。"""
        return {
            "version": 1,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": str(self.daily_nav[-1].get("date") if self.daily_nav else ""),
            "initial_capital": self.config.initial_capital,
            "cash": self.cash,
            "total_capital": self.total_capital,
            "current_positions": json.loads(json.dumps(self.current_positions, ensure_ascii=False)),
            "daily_nav": json.loads(json.dumps(self.daily_nav, ensure_ascii=False)),
            "trade_history": [asdict(t) for t in self.trade_history],
            "entry_attempts": json.loads(json.dumps(self.entry_attempts, ensure_ascii=False)),
            "entry_candidate_count": self.entry_candidate_count,
            "suspended_sessions": list(self._suspended_sessions.values()),
            "sector_peer_failures": [
                {"trade_date": date, "code": code, "reason": reason}
                for (date, code), reason in self._sector_peer_failures.items()],
        }

    def import_state(self, state: Dict[str, Any]) -> None:
        """恢复账户状态；调用后可继续处理下一个交易日。"""
        state = dict(state or {})
        self._exit_scan_times.clear()
        self._production_minute_prices = None
        initial = self._float(state.get("initial_capital"), self.config.initial_capital)
        self.config.initial_capital = initial
        self.cash = self._float(state.get("cash"), initial)
        self.total_capital = self._float(state.get("total_capital"), self.cash)
        self.current_positions = {
            str(code).zfill(6): dict(pos or {})
            for code, pos in (state.get("current_positions") or {}).items()
        }
        self.daily_nav = [dict(row or {}) for row in (state.get("daily_nav") or [])]
        self.entry_attempts = [dict(row or {}) for row in (state.get("entry_attempts") or [])]
        self.entry_candidate_count = int(state.get("entry_candidate_count") or 0)
        self._suspended_sessions = {
            (str(row['trade_date']), str(row['code']), str(row['scope'])): dict(row)
            for row in state.get('suspended_sessions') or []
        }
        self._sector_peer_failures = {(str(row['trade_date']), str(row['code'])): str(row['reason'])
                                     for row in state.get('sector_peer_failures') or []}
        allowed = set(TradeRecord.__dataclass_fields__.keys())
        self.trade_history = []
        for row in state.get("trade_history") or []:
            if isinstance(row, TradeRecord):
                self.trade_history.append(row)
                continue
            if not isinstance(row, dict):
                continue
            data = {k: row.get(k) for k in allowed}
            defaults = {
                "date": "", "stock_code": "", "stock_name": "", "pattern_type": "",
                "action": "", "entry_price": 0.0, "exit_price": 0.0, "shares": 0,
                "position_size": 0.0, "pnl": 0.0, "pnl_pct": 0.0,
                "holding_days": 0, "hot_resonance": False, "resonance_sectors": "",
            }
            defaults.update({k: v for k, v in data.items() if v is not None})
            self.trade_history.append(TradeRecord(**defaults))

    def run_backtest(self,
                     start_date: str,
                     end_date: str,
                     trade_plans_dir: str,
                     progress: Optional[Callable[[Dict], None]] = None) -> Dict:
        """
        执行回测

        Args:
            start_date: 回测开始日期 YYYYMMDD
            end_date: 回测结束日期 YYYYMMDD
            trade_plans_dir: 交易计划文件目录

        Returns:
            回测结果报告
        """
        logger.info(f"开始回测: {start_date} 至 {end_date}")
        logger.info(f"初始资金: {self.config.initial_capital:,.2f}")

        # 生成交易日历
        trade_dates = self._get_trade_dates(start_date, end_date)

        self._replay_progress = progress
        for index, date in enumerate(trade_dates):
            if progress:
                progress({"stage": "运行分钟回放", "trade_date": date,
                          "completed": index, "total": len(trade_dates), "unit": "交易日"})
            self._process_date(date, trade_plans_dir)
            if progress:
                progress({"stage": "运行分钟回放", "trade_date": date,
                          "completed": index + 1, "total": len(trade_dates), "unit": "交易日"})

        self._replay_progress = None
        # 生成回测报告
        if progress:
            progress({"stage": "生成回测报告", "completed": len(trade_dates),
                      "total": len(trade_dates), "unit": "交易日"})
        report = self._generate_backtest_report()

        logger.info(f"回测完成: 最终资金 {self.total_capital:,.2f}")
        logger.info(f"总收益率: {report['total_return']:.2%}")

        return report

    def run_one_day(self, trade_date: str, trade_plans_dir: str) -> Dict:
        """只处理一个交易日，账户状态由调用方提前 import_state。"""
        logger.info(f"开始单日接力回测: {trade_date}")
        trade_dates = self._get_trade_dates(trade_date, trade_date)
        if not trade_dates:
            logger.warning(f"{trade_date} 不是交易日或交易日历无数据，跳过")
            return self._generate_backtest_report()
        self._process_date(trade_dates[0], trade_plans_dir)
        report = self._generate_backtest_report()
        logger.info(f"单日接力完成: {trade_date}，当前资金 {self.total_capital:,.2f}")
        return report

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """获取交易日列表（B-1：使用真实交易日历，自动剔除节假日）"""
        return self.calendar.get_trade_dates(start_date, end_date)

    def _process_date(self, date: str, trade_plans_dir: str):
        """处理单日回测
        
        注意：交易计划是前一天收盘后制定的，所以当天执行的是前一天的计划
        例如：20260413 收盘后制定 20260414 的计划，20260414 开盘时执行
        """
        production_plans = self._prepare_production_day(date) if self.config.production_rules else None
        if self.config.replay_missing_evidence_policy != "legacy":
            # Check all overnight holdings before any exit or daily-OHLC fallback.
            missing = []
            for code in self.current_positions:
                if self._is_full_day_suspended(code, date, scope='holding'):
                    continue
                bars = self._load_exit_minute_bars(code, date)
                if bars.empty:
                    failure = next((row for row in reversed(self._exit_evidence_failures)
                                    if row['trade_date'] == str(date) and row['code'] == str(code).zfill(6)),
                                   {"trade_date": str(date), "code": str(code).zfill(6),
                                    "evidence": "exit_minute_bars", "reason": "持仓退出分钟证据缺失或残缺"})
                    missing.append(dict(failure, scope="holding", action="blocked"))
            if missing:
                self._raise_replay_evidence(missing)
        # Fixed-price legacy plans have no reliable intraday event timestamps.
        if self.config.entry_mode == ENTRY_FIXED:
            self._check_stop_loss_take_profit(date)

        # 2. 加载前一日的交易计划（T日执行T-1日的计划）
        prev_date = self._get_prev_trade_date(date)
        plans_df = production_plans if production_plans is not None else self._load_trade_plans(prev_date, trade_plans_dir)

        if not plans_df.empty:
            logger.info(f"[{date}] 加载 {prev_date} 制定的 {len(plans_df)} 条交易计划")

            # 分钟策略先把当日所有候选的一分钟行情和竞价数据落入磁盘/内存缓存，
            # 后续逐票判定只读缓存，不在某个条件分支里临时访问行情源。
            self._day_plans = plans_df.copy()
            if self.config.entry_mode != ENTRY_FIXED:
                self._prefetch_entry_data(plans_df, date)

            if self.config.entry_mode == ENTRY_FIXED:
                for _, plan in plans_df.iterrows():
                    self._execute_buy(plan, date)
            else:
                events = []
                for order, (_, plan) in enumerate(plans_df.iterrows()):
                    code = str(plan['代码']).zfill(6)
                    decision = self._check_buy_conditions(plan, date, code, str(plan['名称']))
                    if decision[0]:
                        at_time = str((self._last_entry_meta.get(code) or {}).get('entry_time') or '')
                        if not at_time:
                            self._record_gate_attempt(plan, date, 'matching_gate', 'missing_entry_time', '缺少分钟成交时间')
                            self._clear_entry_state(code)
                            continue
                        state = (dict(self._last_entry_meta.get(code) or {}),
                                 self._last_entry_gap.get(code, 0.0), self._last_entry_signal.get(code, ''))
                        events.append((at_time, order, plan, decision, state))
                        self._clear_entry_state(code)
                    else:
                        self._clear_entry_state(code)
                # Exits and cash are advanced only through completed bars before
                # each entry. A later signal cannot consume an earlier slot.
                for at_time, _, plan, decision, state in sorted(events, key=lambda item: (
                    item[0], -item[4][0].get('entry_strength_score', 0) if self.config.production_rules else item[1])):
                    self._check_stop_loss_take_profit(date, until_time=at_time)
                    self._mark_positions_at_time(date, at_time)
                    code = str(plan['代码']).zfill(6)
                    self._last_entry_meta[code], self._last_entry_gap[code], self._last_entry_signal[code] = state
                    self._execute_buy(plan, date, entry_decision=decision)

            # 开盘建仓全部完成后，再统一处理当日硬止损。这样不会把盘中止损
            # 回笼的现金用于同一开盘时刻后续候选，避免时间穿越。
            new_positions = [
                (code, str(position.get('entry_signal') or '竞价买点'))
                for code, position in self.current_positions.items()
                if str(position.get('entry_date') or '') == str(date)
            ]
            for stock_code, entry_signal in new_positions:
                self._check_entry_day_stop(stock_code, date, entry_signal)

            self._day_plans = pd.DataFrame()

        # 4. 计算当日净值
        if self.config.entry_mode != ENTRY_FIXED:
            self._check_stop_loss_take_profit(date)
        self._calculate_daily_nav(date)

    def _mark_positions_at_time(self, date: str, at_time: str) -> None:
        for code, position in self.current_positions.items():
            bars = self._load_exit_minute_bars(code, date)
            known = bars[bars['time'].astype(str) < at_time] if not bars.empty else bars
            price = self._float(known.iloc[-1].get('close')) if not known.empty else 0.0
            if price > 0:
                position['market_value'] = position['shares'] * price
        self.total_capital = self.cash + sum(pos['market_value'] for pos in self.current_positions.values())

    def _get_prev_trade_date(self, date: str) -> str:
        """获取前一个交易日（B-1：真实交易日历）"""
        return self.calendar.prev(date)

    def _prepare_production_day(self, date: str) -> pd.DataFrame:
        if self._production_inputs.get('date') == str(date):
            return self._production_plans.copy()
        self._release_completed_minute_data(str(date))
        from core.realtime.day_replay import DayReplayLoader, digest
        from core.realtime.entry_signal_service import RealtimeEntrySignalService
        from backtest.plan_source import _to_backtest_row
        from core.signals.production_rules import RULESET_VERSION
        holdings = [{"code": c, "sector_names": p.get('resonance_sectors') or ''}
                    for c, p in self.current_positions.items()]
        loader = getattr(self.dm, 'production_loader', None)
        if loader is None:
            loader = DayReplayLoader(data_manager=self.dm, calendar=self.calendar,
                cache_dir=getattr(self.dm, 'cache_dir', None))
        fetch_missing = bool(getattr(self.dm, 'allow_remote_minutes', getattr(self.dm, 'allow_remote_history', True)))
        def evidence_progress(item):
            if self._replay_progress:
                self._replay_progress({**item, 'trade_date': str(date), 'unit': '证据项'})
        inputs = loader.run({"market_date": str(date), "until": "15:01:00", "profile": "production",
                             "lookback": 10, "codes": [], "fetch_missing": fetch_missing},
                            progress=evidence_progress,
                            inputs_only=True, extra_codes=self.current_positions, extra_rows=holdings)
        self._production_inputs = inputs
        inputs['date'] = str(date)
        frozen_config = inputs.get('input_basis') == 'preopen_frozen' or inputs.get('execution_frozen') is True
        if 'minute_amount_profile' in inputs:
            self.amount_profiles._payload = deepcopy(inputs['minute_amount_profile'])
        from core.signals.production_rules import minute_evaluator
        from risk.risk_config import RiskConfig
        self.minute_evaluator = minute_evaluator(
            RiskConfig.from_dict(inputs['risk_config']) if frozen_config and inputs.get('risk_config') else self.config)
        latest_rows = []
        from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS
        enabled = {key for key, p in self._production_profiles.items() if key in PRODUCTION_STRATEGY_IDS and p.get('enabled', True)}
        selected = set(self.config.production_strategy_ids or
                       ([r.get('strategy_id') for r in inputs['rows']] if frozen_config else enabled))
        for source in inputs['rows']:
            row = dict(source)
            if row.get('strategy_id') not in selected:
                continue
            profile = {} if frozen_config else self._production_profiles.get(row.get('strategy_id')) or {}
            if profile and profile.get('enabled') is False:
                continue
            if profile:
                from core.signals.production_rules import execution_defaults
                row['strategy_execution'] = execution_defaults(profile.get('execution') or {}, self._production_risk)
                row['strategy_version'] = str(profile.get('version') or '')
            latest_rows.append(row)
        inputs['rows'] = latest_rows
        self._production_rows = {str(r['code']): r for r in inputs['rows']}
        plans = []
        for rank, row in enumerate(inputs['rows'], 1):
            item = _to_backtest_row({**row, "综合评分": row.get('screening_score') or row.get('leader_score') or row.get('score'),
                                    "共振板块": ','.join(RealtimeEntrySignalService._sectors(row)),
                                    "优先级": rank, "入场门控来源": "production_decision_v1",
                                    "生产决策允许入场": True})
            if item:
                item['pool_type'] = row.get('pool_type') or ''
                plans.append(item)
        for code, raw in inputs['frames'].items():
            self._minute_frames[(str(date), code)] = normalize_minute_bars(raw)
        for code, auction in inputs.get('auctions', {}).items():
            self._auction_rows[(str(date), code)] = auction
        for code in self.current_positions:
            self._exit_minute_frames.pop((str(date), code), None)
        manifest = {"date": str(date), "ruleset": RULESET_VERSION, "input_basis": inputs['input_basis'],
                    "bundle_id": inputs['bundle_id'], "candidate_codes": sorted(self._production_rows),
                    "sector_codes": inputs['sector_codes'], "opening_price_source": inputs['opening_price_source']}
        from core.signals.production_rules import entry_parameters
        manifest['entry_parameters'] = entry_parameters(self.minute_evaluator)
        manifest['configuration_basis'] = ('preopen_frozen' if inputs.get('input_basis') == 'preopen_frozen'
                                           else 'research_frozen' if inputs.get('execution_frozen') else 'run_config')
        manifest['input_hash'] = digest({**manifest, 'rows': inputs['rows'], 'previous': inputs['previous'],
            'board_feedback_inputs': inputs.get('board_feedback_inputs', {}),
            'opening_prices': inputs['opening_prices'], 'members': inputs['members'],
            'opening_evidence': inputs.get('opening_evidence', {}),
            'amount_profile': self.amount_profiles.load(),
            'minutes': {c: f.to_dict('records') for c, f in inputs['frames'].items()}})
        self._production_input_manifest.append(manifest)
        self._production_plans = pd.DataFrame(plans)
        return self._production_plans.copy()

    def _release_completed_minute_data(self, date: str) -> None:
        # Constituent histories can cover thousands of stocks; retain only this session.
        for cache in (self._minute_frames, self._exit_minute_frames, self._auction_rows,
                      self._exit_scan_times, self._sector_evidence, self._sector_evidence_snapshots):
            for key in list(cache):
                if str(key[0]) != date:
                    del cache[key]
        if self._production_inputs.get('date') != date:
            self._production_inputs = {}
        self._production_minute_prices = None

    def _load_trade_plans(self, date: str, trade_plans_dir: str) -> pd.DataFrame:
        """加载交易计划"""
        # 尝试两种文件名格式
        plan_file = Path(trade_plans_dir) / f"交易计划_{date}.csv"

        if not plan_file.exists():
            # 尝试旧格式
            plan_file = Path(trade_plans_dir) / f"trade_plans_{date}.csv"
            if not plan_file.exists():
                return pd.DataFrame()

        try:
            df = pd.read_csv(plan_file)
            # 只保留买入计划
            if '动作' in df.columns:
                df = df[df['动作'] == '买入']
            self.entry_candidate_count += int(len(df))
            from core.utils.stock_eligibility import new_entry_exclusion
            exclusions = df.apply(new_entry_exclusion, axis=1)
            for index, row in df[exclusions.ne('')].iterrows():
                self._record_gate_attempt(row, self.calendar.next(date), "stock_risk_gate",
                                          "restricted_stock", exclusions.loc[index])
            df = df[exclusions.eq('')].copy()
            if df.empty:
                return df
            keep_indices = []
            for index, row in df.iterrows():
                production = str(row.get('入场门控来源') or '') == 'production_decision_v1'
                if production:
                    # Production has already applied the strategy's regime/phase gates.
                    # Its percentile score is not a calibrated probability or legacy score.
                    eligible = str(row.get('生产决策允许入场')).strip().lower() in {'true', '1', '1.0'}
                    if eligible and self._plan_execution(row):
                        keep_indices.append(index)
                    else:
                        self._record_gate_attempt(row, self.calendar.next(date), 'market_gate',
                                                  'production_entry_ineligible', '生产决策未允许入场或缺少执行配置')
                    continue
                market_score = self._float(row.get('原始_mkt_market_score'))
                score = self._float(row.get('综合评分'))
                reason_code = reason = ''
                if 0 < market_score < self.config.market_entry_threshold:
                    reason_code = 'market_too_weak'
                    reason = f'市场评分{market_score:.1f}低于开仓阈值{self.config.market_entry_threshold:.1f}'
                elif 0 < market_score < self.config.market_strong_threshold and score < self.config.neutral_market_min_score:
                    reason_code = 'score_below_neutral_threshold'
                    reason = f'中性偏弱市场评分不足{self.config.neutral_market_min_score:.0f}'
                elif 0 < market_score < self.config.market_hot_threshold and score < self.config.active_market_min_score:
                    reason_code = 'score_below_active_threshold'
                    reason = f'中性市场评分不足{self.config.active_market_min_score:.0f}'
                if reason_code:
                    self._record_gate_attempt(row, self.calendar.next(date), 'market_gate', reason_code, reason)
                else:
                    keep_indices.append(index)
            df = df.loc[keep_indices].copy()
            # 不再按固定名次截断；当资金或持仓数有限时，评分高者先接受买点校验。
            if '综合评分' in df.columns:
                df['_score'] = pd.to_numeric(df['综合评分'], errors='coerce').fillna(0)
                df = df.sort_values('_score', ascending=False).drop(columns=['_score'])
            return df
        except Exception as e:
            logger.error(f"加载交易计划失败 {date}: {e}")
            return pd.DataFrame()

    def _execute_buy(self, plan: pd.Series, date: str, *, entry_decision: Optional[Tuple[bool, float]] = None):
        """执行买入
        
        根据交易计划中的介入时机和当日开盘情况决定是否买入
        """
        stock_code = str(plan['代码']).zfill(6)  # 标准化为6位代码
        stock_name = plan['名称']

        # 检查是否已有持仓
        if stock_code in self.current_positions:
            logger.debug(f"{stock_name} 已有持仓，跳过")
            self._record_gate_attempt(plan, date, "portfolio_gate", "already_held", "已有持仓")
            self._clear_entry_state(stock_code)
            return

        # 先识别客观分钟信号，再由仓位、现金和集中度决定账户是否接纳。
        # 这样回测能区分“市场没有买点”和“有买点但账户已满”，避免把组合约束
        # 错误解释为策略没有交易机会。
        can_buy, entry_price = (entry_decision if entry_decision is not None
                                else self._check_buy_conditions(plan, date, stock_code, stock_name))
        if not can_buy:
            self._clear_entry_state(stock_code)
            return

        if (
            self.config.rotation_enabled
            and self.config.risk_control
            and len(self.current_positions) >= self.config.max_positions
        ):
            self._rotate_to_stronger_candidate(plan, date, stock_code, stock_name)

        position_size = self._calculate_position_size(plan)
        sizing_meta = dict(self._last_sizing_meta)
        if position_size <= 0:
            logger.info(
                f"{stock_name} 仓位模型拒绝开仓: {sizing_meta.get('rationale') or '无正期望'}"
            )
            self._record_gate_attempt(
                plan, date, "sizing_gate", "position_sizing_rejected",
                str(sizing_meta.get('rationale') or '仓位模型无正期望'),
            )
            self._clear_entry_state(stock_code)
            return
        current_position_value = sum(pos['market_value'] for pos in self.current_positions.values())
        strategy_id = str(plan.get('策略ID') or 'default')
        strategy_name = str(plan.get('策略名称') or strategy_id)
        strategy_version = str(plan.get('策略版本') or '')
        strategy_sources = str(plan.get('策略来源') or strategy_id)
        execution = self._plan_execution(plan)
        exit_config = self._execution_exit_config(execution)
        strategy_position_cap = self._float(plan.get('策略单票仓位上限%')) / 100.0
        if strategy_position_cap > 0 and self.config.account_position_pct <= 0:
            position_size = min(position_size, self.total_capital * strategy_position_cap)
        portfolio_position_cap = self._float(plan.get('组合建议仓位%')) / 100.0
        if portfolio_position_cap > 0 and self.config.account_position_pct <= 0:
            position_size = min(position_size, self.total_capital * portfolio_position_cap)
        strategy_max_positions = int(execution.get('max_positions') or 0)
        if strategy_max_positions > 0:
            strategy_open_count = sum(
                1 for position in self.current_positions.values()
                if str(position.get('strategy_id') or 'default') == strategy_id
            )
            if strategy_open_count >= strategy_max_positions:
                logger.info(f"{strategy_name} 已达策略持仓上限{strategy_max_positions}只，跳过 {stock_name}")
                self._record_gate_attempt(
                    plan, date, "portfolio_gate", "strategy_position_limit",
                    f"策略持仓已达{strategy_max_positions}只",
                )
                self._clear_entry_state(stock_code)
                return

        # 组合层风控闸门（仅风控开启时施加：持仓数 / 单票 / 总仓 / 板块集中度）
        if self.config.risk_control:
            # a) 持仓数上限
            if len(self.current_positions) >= self.config.max_positions:
                logger.warning(f"持仓数已达上限{self.config.max_positions}只，跳过买入 {stock_name}")
                self._record_gate_attempt(plan, date, "portfolio_gate", "account_position_limit", "账户持仓数已达上限")
                self._clear_entry_state(stock_code)
                return

            # b) 单票上限
            max_position_value = self.total_capital * self.config.max_position_per_stock
            if position_size > max_position_value:
                position_size = max_position_value

            # c) 总仓位上限
            # 激进三仓账户由账户画像统一管理仓位，不再叠加交易计划中的
            # 市场阶段仓位折扣，否则第三仓经常在已有明确信号时被提前拦截。
            market_total_position_cap = (
                1.0
                if self.config.account_position_pct > 0
                else self._float(plan.get('市场总仓位上限%'), 100.0) / 100.0
            )
            effective_total_position_cap = min(
                self.config.max_total_position,
                max(market_total_position_cap, 0.0),
            )
            max_total = self.total_capital * effective_total_position_cap
            if current_position_value + position_size > max_total:
                remaining_total = max(max_total - current_position_value, 0.0)
                if (
                    self.config.account_position_pct > 0
                    and remaining_total >= self.total_capital * 0.005
                ):
                    position_size = remaining_total
                else:
                    logger.warning(
                        f"总仓位超限({effective_total_position_cap:.0%})，跳过买入 {stock_name}"
                    )
                    self._record_gate_attempt(
                        plan, date, "portfolio_gate", "total_position_limit",
                        f"当前情绪阶段总仓位上限{effective_total_position_cap:.0%}",
                    )
                    self._clear_entry_state(stock_code)
                    return

            # d) 板块集中度
            sector = next(iter(normalize_sector_names(plan.get('共振板块'))), '')
            if sector:
                sector_value = sum(
                    pos['market_value'] for pos in self.current_positions.values()
                    if next(iter(normalize_sector_names(pos.get('resonance_sectors'))), '') == sector
                )
                max_sector = self.total_capital * self.config.max_sector_concentration
                if sector_value + position_size > max_sector:
                    allowed = max(max_sector - sector_value, 0.0)
                    if allowed < self.total_capital * 0.005:
                        logger.warning(f"板块[{sector}]集中度超限，跳过买入 {stock_name}")
                        self._record_gate_attempt(
                            plan, date, "portfolio_gate", "sector_concentration_limit",
                            f"板块[{sector}]集中度超限",
                        )
                        self._clear_entry_state(stock_code)
                        return
                    position_size = allowed

        entry_gap = self._last_entry_gap.get(stock_code, 0.0)
        gap_multiplier = self._entry_gap_position_multiplier(entry_gap)
        if gap_multiplier < 1.0:
            position_size *= gap_multiplier
            logger.info(
                f"{stock_name} 高开{entry_gap:.2%}，模拟交易仓位降至原计划{gap_multiplier:.0%}"
            )

        # 入场确认和追高降仓后再检查现金。
        max_cash_budget = self.cash / (1 + self.config.commission_rate)
        if position_size > max_cash_budget:
            if self.config.account_position_pct > 0 and max_cash_budget > 0:
                position_size = max_cash_budget
            else:
                logger.warning(f"现金不足，跳过买入 {stock_name}")
                self._record_gate_attempt(plan, date, "portfolio_gate", "insufficient_cash", "可用现金不足")
                self._clear_entry_state(stock_code)
                return
        
        if entry_price <= 0:
            logger.warning(f"{stock_name} 买入价格无效，跳过")
            self._record_gate_attempt(plan, date, "matching_gate", "invalid_entry_price", "买入价格无效")
            self._clear_entry_state(stock_code)
            return

        # 确保买入价格不超过对应板块涨停价。
        # 从DataManager获取昨日收盘价计算涨停价
        try:
            prev_close = self._get_prev_close(stock_code, date)
            if prev_close and prev_close > 0:
                lu_price = limit_up_price(prev_close, stock_code, stock_name)
                if lu_price is not None and entry_price > lu_price:
                    self._record_gate_attempt(plan, date, 'matching_gate', 'entry_above_limit', '含滑点买入价超过涨停价，不虚构涨停价成交')
                    self._clear_entry_state(stock_code)
                    return
        except Exception as e:
            logger.debug(f"获取昨日收盘价失败 {stock_code}: {e}")

        if entry_price <= 0:
            logger.warning(f"{stock_name} 买入价格无效，跳过")
            self._clear_entry_state(stock_code)
            return
        shares = int(position_size / entry_price / 100) * 100  # 整手

        if shares < 100:
            logger.warning(f"{stock_name} 计算股数不足1手，跳过")
            self._record_gate_attempt(plan, date, "sizing_gate", "below_one_lot", "目标仓位不足一手")
            self._clear_entry_state(stock_code)
            return

        actual_cost = shares * entry_price
        commission = actual_cost * self.config.commission_rate

        # 执行买入
        self.cash -= (actual_cost + commission)

        plan_rank = self._int(plan.get('优先级'), 0)
        plan_score = self._float(plan.get('综合评分'), 0.0)
        plan_reason = str(plan.get('理由') or '')
        factor_metrics_json = self._factor_metrics_json(plan)
        factor_context_json = self._factor_context_json(plan)
        factor_context = json.loads(factor_context_json or '{}')
        from backtest.exit_policy import ExitPolicyRepository, resolve_exit_config

        exit_policy = str(self.config.exit_policy_mode or "strategy").lower()
        if exit_policy == "oos_selected":
            selected_policy = ExitPolicyRepository().resolve(strategy_id, date)
            exit_policy = str(selected_policy.get("policy") or "strategy")
        exit_config = resolve_exit_config(exit_config, policy=exit_policy, factor_context=factor_context)
        open_gap = self._last_entry_gap.pop(stock_code, 0.0)
        entry_signal = self._last_entry_signal.pop(stock_code, "竞价买点")
        entry_meta = self._last_entry_meta.pop(stock_code, {})
        market_score = self._float(factor_context.get('mkt_market_score'))
        amount_ratio = self._float(
            factor_context.get('amount_ratio_5d'),
            self._float(factor_context.get('amount_ratio')),
        )

        self.current_positions[stock_code] = {
            'stock_name': stock_name,
            'entry_date': date,
            'entry_price': entry_price,
            'shares': shares,
            'cost_basis': actual_cost + commission,
            'market_value': actual_cost,
            'pattern_type': plan['模式'],
            'hot_resonance': plan.get('热点共振', False),
            'resonance_sectors': ','.join(normalize_sector_names(plan.get('共振板块'))),
            'plan_rank': plan_rank,
            'plan_score': plan_score,
            'entry_strength_score': self._float(entry_meta.get('entry_strength_score'), plan_score),
            'plan_reason': plan_reason,
            'factor_metrics_json': factor_metrics_json,
            'factor_context_json': factor_context_json,
            'open_gap_pct': open_gap,
            'market_score': market_score,
            'amount_ratio': amount_ratio,
            'entry_signal': entry_signal,
            'entry_time': str(entry_meta.get('entry_time') or '09:30:00'),
            'confirm_time': str(entry_meta.get('confirm_time') or ''),
            'breakout_level': self._float(entry_meta.get('breakout_level')),
            'initial_confirm_time': str(entry_meta.get('initial_confirm_time') or ''),
            'confirmation_policy': str(entry_meta.get('confirmation_policy') or 'legacy'),
            'confirmation_reason': str(entry_meta.get('confirmation_reason') or ''),
            'stop_loss_price': max(entry_price * (1 - exit_config['hard_stop_loss']), self._float(entry_meta.get('structural_stop'))),
            'highest_price': entry_price,  # 用于跟踪回撤
            'max_favorable_price': entry_price,
            'min_adverse_price': entry_price,
            'last_close': self._float((self._get_stock_daily_bar(stock_code, date) or {}).get('close'), entry_price),
            'sizing_method': str(sizing_meta.get('method') or ''),
            'sizing_rationale': str(sizing_meta.get('rationale') or ''),
            'sizing_sample_size': int(sizing_meta.get('sample_size') or 0),
            'strategy_id': strategy_id,
            'strategy_name': strategy_name,
            'strategy_version': strategy_version,
            'strategy_sources': strategy_sources,
            'strategy_execution': execution,
            'exit_config': exit_config,
        }

        logger.info(f"[{date}] 买入 {stock_name}({stock_code}): {shares}股 @ {entry_price:.2f}, 成本:{actual_cost+commission:.2f}")

        # 记录买入交易
        trade_record = TradeRecord(
            date=date,
            stock_code=stock_code,
            stock_name=stock_name,
            pattern_type=plan['模式'],
            action='BUY',
            entry_price=entry_price,
            exit_price=0,
            shares=shares,
            position_size=actual_cost + commission,
            pnl=0,
            pnl_pct=0,
            holding_days=0,
            hot_resonance=plan.get('热点共振', False),
            resonance_sectors=','.join(normalize_sector_names(plan.get('共振板块'))),
            stop_loss_triggered=False,
            take_profit_triggered=False,
            entry_date=date,
            exit_reason='buy',
            plan_rank=plan_rank,
            plan_score=plan_score,
            plan_reason=plan_reason,
            factor_metrics_json=factor_metrics_json,
            factor_context_json=factor_context_json,
            open_gap_pct=open_gap,
            market_score=market_score,
            amount_ratio=amount_ratio,
            entry_signal=entry_signal,
            entry_time=str(entry_meta.get('entry_time') or '09:30:00'),
            sizing_method=str(sizing_meta.get('method') or ''),
            sizing_rationale=str(sizing_meta.get('rationale') or ''),
            sizing_sample_size=int(sizing_meta.get('sample_size') or 0),
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            strategy_sources=strategy_sources,
            exit_rule_version=self._exit_rule_version(self.current_positions[stock_code]),
            cost_version=self._cost_version(),
        )
        self.trade_history.append(trade_record)

    def _check_entry_day_stop(self, stock_code: str, date: str, entry_signal: str) -> None:
        """竞价成交后立即执行当日硬止损，避免把日内大跌拖成次日跳空亏损。"""
        position = self.current_positions.get(stock_code)
        if not position:
            return
        daily_bar = self._get_stock_daily_bar(stock_code, date)
        if not daily_bar:
            return
        stop_price = self._float(position.get('stop_loss_price'))
        session_low = self._float(daily_bar.get('low'))
        session_close = self._float(daily_bar.get('close'))
        if stop_price <= 0:
            return
        entry_time = str(position.get('entry_time') or '')
        minute_frame = self._minute_frames.get((str(date), stock_code), pd.DataFrame())
        if entry_time and not minute_frame.empty and entry_signal != "竞价买点":
            post_entry = minute_frame[minute_frame['time'] >= entry_time]
            if not post_entry.empty:
                session_low = self._float(post_entry['low'].min(), session_low)
                session_high = self._float(post_entry['high'].max(), position['entry_price'])
                self._update_excursion(position, session_high, session_low)
                triggered = bool((pd.to_numeric(post_entry['low'], errors='coerce') <= stop_price).any())
            else:
                triggered = session_close <= stop_price
        else:
            self._update_excursion(position, self._float(daily_bar.get('high')), session_low)
            triggered = session_low <= stop_price
        if triggered:
            # A股现货当日买入不可卖出。只记录入场后不利波动，下一交易日再按
            # 开盘跳空或盘中止损规则成交，避免虚构 T+0 止损。
            position['entry_day_stop_breached'] = True
            logger.info(
                f"[{date}] {position['stock_name']} 买入当日跌破止损线{stop_price:.2f}，"
                f"受T+1限制仅记录风险，不执行当日卖出"
            )

    @staticmethod
    def _update_excursion(position: Dict[str, Any], high_price: float, low_price: float) -> None:
        entry = float(position.get('entry_price') or 0.0)
        if entry <= 0:
            return
        high = float(high_price or entry)
        low = float(low_price or entry)
        position['max_favorable_price'] = max(
            float(position.get('max_favorable_price') or entry), high, entry
        )
        position['min_adverse_price'] = min(
            float(position.get('min_adverse_price') or entry), low, entry
        )

    def _prefetch_entry_data(self, plans: pd.DataFrame, date: str) -> None:
        if self.config.production_rules:
            from core.data.minute_quality import inspect_minute_bars, minute_failure
            for _, plan in plans.iterrows():
                code = str(plan['代码']).zfill(6)
                if self._is_full_day_suspended(code, date, scope='candidate'):
                    continue
                self._candidate_evidence_samples.add((str(date), code))
                frame = self._production_inputs['frames'].get(code)
                if not inspect_minute_bars(frame, str(date), expected_code=code)['valid']:
                    self._entry_evidence_missing[(str(date), code)] = minute_failure(self.dm, frame, str(date), code)
                self._auction_rows[(str(date), code)] = self._production_inputs['auctions'].get(code, {})
            return
        from core.utils.stock_eligibility import new_entry_exclusion
        requested = loaded = 0
        sector_map: Dict[str, set[str]] = {}
        for _, plan in plans.iterrows():
            if new_entry_exclusion(plan):
                continue
            code = str(plan.get('代码') or '').zfill(6)
            if not code:
                continue
            sector_map[code] = set(normalize_sector_names(plan.get('共振板块') or plan.get('所属板块')))
            requested += 1
            ts_code = self._standardize_stock_code(code)
            key = (str(date), code)
            checking = self.config.replay_missing_evidence_policy != "legacy"
            if self._is_full_day_suspended(code, date, scope='candidate'):
                self._minute_frames[key], self._auction_rows[key] = pd.DataFrame(), {}
                continue
            if checking and code not in self.current_positions:
                self._candidate_evidence_samples.add(key)
            try:
                previous_failure = next((row for row in (self.config.replay_prefetch_failures or [])
                    if str(row.get('trade_date')) == str(date)
                    and str(row.get('code') or row.get('ts_code') or '').split('.')[0].zfill(6) == code
                    and row.get('evidence') == 'minute_bars'), None)
                if checking and previous_failure:
                    self._entry_evidence_missing[key] = dict(previous_failure, code=code)
                    frame = pd.DataFrame()
                else:
                    frame = self.dm.get_stock_tick(ts_code, str(date))
                    if checking:
                        from core.data.minute_quality import inspect_minute_bars, minute_failure
                        if not inspect_minute_bars(frame, str(date), expected_code=ts_code)["valid"]:
                            self._entry_evidence_missing[key] = minute_failure(self.dm, frame, str(date), ts_code)
                            frame = pd.DataFrame()
                self._minute_frames[key] = normalize_minute_bars(frame)
                if not self._minute_frames[key].empty:
                    loaded += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{date}] {code} 分钟行情预取失败: {exc}")
                self._minute_frames[key] = pd.DataFrame()
                if checking:
                    self._entry_evidence_missing[key] = {"trade_date": str(date), "code": code,
                        "evidence": "minute_bars", "reason": str(exc)}
            try:
                self._auction_rows[key] = dict(self.dm.get_auction_data(ts_code, str(date)) or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{date}] {code} 竞价数据预取失败: {exc}")
                self._auction_rows[key] = {}
        self._sector_peers = self._load_sector_peer_codes(date, sector_map) if self.minute_evaluator.confirmation_policy.require_sector else {}
        peer_codes = sorted({peer for peers in self._sector_peers.values() for peer in peers})
        peer_loaded = 0
        for peer in peer_codes:
            key = (str(date), peer)
            if self._is_full_day_suspended(peer, date, scope='sector_peer'):
                self._minute_frames[key] = pd.DataFrame()
                continue
            if key in self._minute_frames and not self._minute_frames[key].empty:
                peer_loaded += 1
                continue
            try:
                frame = self.dm.get_stock_tick(self._standardize_stock_code(peer), str(date))
                if self.config.replay_missing_evidence_policy != "legacy":
                    from core.data.minute_quality import inspect_minute_bars
                    quality = inspect_minute_bars(frame, str(date), expected_code=peer)
                    if not quality["valid"]:
                        self._sector_peer_failures[key] = str(quality['reason'])
                        frame = pd.DataFrame()
                self._minute_frames[key] = normalize_minute_bars(frame)
                peer_loaded += int(not self._minute_frames[key].empty)
            except Exception as exc:
                self._sector_peer_failures[key] = str(exc)
                self._minute_frames[key] = pd.DataFrame()
        logger.info(f"[{date}] 候选分钟行情预取完成: {loaded}/{requested}")
        logger.info(f"[{date}] 板块成分分钟行情预取完成: {peer_loaded}/{len(peer_codes)}")

    @staticmethod
    def _load_sector_peer_codes(date: str, sector_map: Dict[str, set[str]], *, duckdb_path=None) -> Dict[str, List[str]]:
        if not sector_map:
            return {}
        try:
            import duckdb  # type: ignore
            from core.infrastructure.duckdb_access import connect as duckdb_connect
            from config.settings import FACTOR_DB_PATH
            from core.utils.stock_eligibility import factor_risk_projection, new_entry_exclusion

            con = duckdb_connect(str(duckdb_path or FACTOR_DB_PATH), read_only=True)
            try:
                risk_columns = factor_risk_projection(con)
                rows = con.execute(
                    f"SELECT code, resonance_sectors, total_score{risk_columns} FROM factor_stock_wide "
                    "WHERE CAST(trade_date AS VARCHAR) = ("
                    "SELECT MAX(CAST(trade_date AS VARCHAR)) FROM factor_stock_wide "
                    "WHERE CAST(trade_date AS VARCHAR) < ?)",
                    [str(date)],
                ).fetchdf()
            finally:
                con.close()
            if not rows.empty:
                rows = rows.loc[[not new_entry_exclusion(row) for row in rows.to_dict('records')]]
        except Exception as exc:
            logger.warning(f"[{date}] 历史板块样本映射读取失败: {exc}")
            return {}
        output: Dict[str, List[str]] = {}
        for code, sectors in sector_map.items():
            if not sectors:
                output[code] = []
                continue
            matches = rows[
                rows["resonance_sectors"].fillna("").astype(str).map(
                    lambda text: bool(set(normalize_sector_names(text)).intersection(sectors))
                )
                & rows["code"].astype(str).str.zfill(6).ne(code)
            ].sort_values("total_score", ascending=False)
            output[code] = matches["code"].astype(str).str.zfill(6).drop_duplicates().head(8).tolist()
        return output

    def _sector_sync_checker(self, plan: pd.Series, date: str, stock_code: str):
        if not self.minute_evaluator.confirmation_policy.require_sector:
            from core.signals.sector_trend import make_trend_checker
            inputs = getattr(self, "_production_inputs", {}) or {}
            checker, evidence = make_trend_checker(
                set(normalize_sector_names(plan.get('共振板块') or plan.get('所属板块'))),
                inputs.get("board_trend_inputs") or {}, date=str(date),
                weight=self.minute_evaluator.confirmation_policy.sector_score_weight)
            key = (str(date), stock_code)
            self._sector_evidence[key] = evidence
            snapshots = self._sector_evidence_snapshots[key] = {}
            def observe_trend(at_time):
                state = checker(at_time)
                snapshots[str(at_time)] = deepcopy(evidence)
                return state
            return observe_trend
        if self.config.production_rules:
            from core.signals.sector_sync import constituent_minute_prices, make_constituent_sync_checker
            from core.signals.opening import sector_reference_rows
            inputs = self._production_inputs
            frames = {c: self._minute_frames.get((str(date), c)) for c in inputs.get('frames', {})}
            if getattr(self, '_production_minute_prices', None) is None:
                self._production_minute_prices = constituent_minute_prices(frames, inputs.get('sector_quote_minutes'))
            checker, evidence = make_constituent_sync_checker(
                code=stock_code, names=normalize_sector_names(plan.get('共振板块') or plan.get('所属板块')),
                members=inputs.get('members', {}),
                frames=frames,
                previous=sector_reference_rows(inputs.get('previous', {}), inputs.get('opening_evidence', {})),
                opening_prices=inputs.get('opening_prices', {}),
                minute_prices=self._production_minute_prices)
            key = (str(date), stock_code)
            self._sector_evidence[key] = evidence
            snapshots = self._sector_evidence_snapshots[key] = {}
            def observe(at_time):
                result = checker(at_time)
                snapshots[str(at_time)] = deepcopy(evidence)
                return result
            return observe
        sectors = set(normalize_sector_names(plan.get('共振板块') or plan.get('所属板块')))
        key = (str(date), stock_code)
        evidence = self._sector_evidence[key] = {'sector_names': sorted(sectors), 'peer_codes': [],
            'observed_count': 0, 'minimum_observations': 2, 'at_time': '', 'peer_failures': {}}
        peers: List[str] = list(self._sector_peers.get(stock_code) or [])
        for _, other in self._day_plans.iterrows():
            other_code = str(other.get('代码') or '').zfill(6)
            if not other_code or other_code == stock_code:
                continue
            other_sectors = set(normalize_sector_names(other.get('共振板块') or other.get('所属板块')))
            if sectors.intersection(other_sectors):
                if other_code not in peers:
                    peers.append(other_code)
        evidence['peer_codes'] = peers

        from core.signals.sector_confirmation import make_sector_sync_checker
        peer_rows = [{'code': peer, 'resonance_sectors': list(sectors)} for peer in peers]
        peer_rows.extend({**row, 'code': row.get('代码'),
                          'resonance_sectors': row.get('共振板块') or row.get('所属板块')}
                         for row in self._day_plans.to_dict('records'))
        evidence['peer_failures'] = {
            peer: self._sector_peer_failures[(str(date), peer)] for peer in peers
            if (str(date), peer) in self._sector_peer_failures
        }
        checker = make_sector_sync_checker(
            stock_code, sectors, peer_rows,
            frames={peer: self._minute_frames.get((str(date), peer), pd.DataFrame()) for peer in peers},
            previous_closes={peer: self._get_prev_close(peer, date) for peer in peers},
            trade_date=str(date), evidence=evidence,
        )
        snapshots = self._sector_evidence_snapshots[key] = {}
        def observe(at_time):
            state = checker(at_time)
            snapshots[str(at_time)] = deepcopy(evidence)
            return state
        return observe

    def _is_leader_or_mainline_core(self, plan: pd.Series) -> bool:
        if str(plan.get('pool_type') or '') in {'核心龙头', '板块龙头'}:
            return True
        leader_quality = self._float(plan.get('因子_stk_kpl_leader_quality'))
        mainline = self._float(plan.get('因子_stk_sector_mainline_score'))
        board_position = self._float(plan.get('因子_stk_board_position'))
        return leader_quality >= 65.0 or (mainline >= 75.0 and board_position >= 60.0)

    @staticmethod
    def _plan_execution(plan: pd.Series) -> Dict[str, Any]:
        raw = plan.get('策略执行')
        if isinstance(raw, dict):
            return dict(raw)
        try:
            return dict(json.loads(str(raw or '{}')) or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _entry_mode_for_plan(self, plan: pd.Series, gap: float) -> Optional[str]:
        """Resolve the actual minute mode, constrained by the source strategy."""
        from backtest.reversal_entry import STRUCTURAL_MODES
        allowed_structures = self._plan_execution(plan).get('allowed_entry_modes') or []
        for structural in STRUCTURAL_MODES:
            if structural in allowed_structures:
                return structural
        configured = self.config.entry_mode if self.config.entry_mode in ENTRY_MODES else ENTRY_HYBRID
        if configured == ENTRY_COMPARE:
            configured = ENTRY_HYBRID
        if configured == ENTRY_FIXED:
            return ENTRY_FIXED
        if configured == ENTRY_HYBRID:
            from core.signals.opening import classify_opening_gap
            resolved = classify_opening_gap(gap, weak_max_gap=self.minute_evaluator.weak_max_gap,
                                           continuation_max_gap=self.minute_evaluator.continuation_max_gap)
            if not resolved:
                return None
        else:
            resolved = configured
        allowed = normalize_strategy_entry_modes(
            self._plan_execution(plan).get('allowed_entry_modes') or []
        )
        if allowed and resolved not in allowed:
            return None
        return resolved

    def _record_entry_attempt(
        self, plan: pd.Series, date: str, stock_code: str, stock_name: str,
        decision: EntryDecision, *, entry_mode: str,
    ) -> None:
        reason_code = str(decision.status or 'entry_signal')
        reason_text = str(decision.reason or '')
        detailed_reasons = {
            '等待当日一分钟行情': 'missing_minutes',
            '等待开盘前5分钟完成': 'incomplete_opening_window',
            '开盘不在弱转强区间': 'weak_gap_out_of_range',
            '开盘不在强势延续区间': 'continuation_gap_out_of_range',
            '高开超过5%，仅高开加速模式参与': 'acceleration_mode_required',
            '低开超过3%，取消': 'gap_below_entry_floor',
            '跌破开盘前5分钟低点': 'broke_opening_low',
            '缺少真实板块指数或成分股宽度，保持观察': 'missing_sector_confirmation',
            '缺少历史同分钟成交进度模型，保持观察': 'missing_amount_profile',
            '弱转强条件尚未全部满足': 'weak_signal_pending',
            '10:00前未完成弱转强确认': 'weak_confirmation_timeout',
            '缺少真实竞价成交额、竞价量或昨日日量': 'missing_auction_evidence',
            '竞价成交额或竞价量比不足': 'auction_volume_insufficient',
            '强势延续条件尚未全部满足': 'continuation_signal_pending',
            '10:00前未出现有效承接或突破': 'continuation_confirmation_timeout',
            '缺少竞价明细且10:00前未完成开盘强势确认': 'opening_strength_confirmation_timeout',
            '非龙头或主线核心，不参与高开加速': 'acceleration_not_leader',
            '接近涨停开盘，暂无可成交证据': 'locked_limit_unfilled',
            '高开加速条件尚未全部满足': 'acceleration_signal_pending',
            '10:00前未出现龙头加速确认': 'acceleration_confirmation_timeout',
        }
        for minutes in range(1, 6):
            detailed_reasons[f'等待开盘前{minutes}分钟完成'] = 'incomplete_opening_window'
            detailed_reasons[f'等待完整开盘前{minutes}分钟结构'] = 'incomplete_opening_window'
            detailed_reasons[f'跌破开盘前{minutes}分钟低点'] = 'broke_opening_low'
        if reason_code in {'rejected', 'cancelled', 'observing', 'data_insufficient', 'signal_unfilled'}:
            reason_code = detailed_reasons.get(reason_text, reason_code)
        key = (str(date), stock_code)
        snapshots = self._sector_evidence_snapshots.get(key, {})
        sector_evidence = deepcopy(snapshots.get(decision.confirm_time, self._sector_evidence.get(key, {})))
        if reason_code == 'missing_sector_confirmation' and sector_evidence and not self.config.production_rules:
            reason_text += (f"；板块：{'、'.join(sector_evidence['sector_names']) or '未解析到有效板块'}"
                f"；同板块样本{len(sector_evidence['peer_codes'])}只"
                f"，{sector_evidence['at_time'] or '确认时点'}有效观测{sector_evidence['observed_count']}/2只")
            if sector_evidence['peer_failures']:
                reason_text += '；' + '；'.join(f"{code}: {reason}" for code, reason in sector_evidence['peer_failures'].items())
        self.entry_attempts.append({
            'date': str(date),
            'stock_code': stock_code,
            'stock_name': stock_name,
            'plan_rank': self._int(plan.get('优先级')),
            'plan_score': self._float(plan.get('综合评分')),
            'entry_mode': entry_mode,
            'strategy_id': str(plan.get('策略ID') or 'default'),
            'strategy_name': str(plan.get('策略名称') or ''),
            'strategy_version': str(plan.get('策略版本') or ''),
            'stage': 'entry_signal',
            'reason_code': reason_code,
            'signal': decision.signal,
            'status': decision.status,
            'reason': reason_text,
            'sector_evidence': sector_evidence,
            'confirm_time': decision.confirm_time,
            'entry_time': decision.entry_time,
            'entry_price': decision.entry_price,
            'open_gap_pct': decision.open_gap_pct,
            'opening_evidence': deepcopy(self._last_entry_meta.get(stock_code, {}).get('opening_evidence', {})),
            'board_feedback': deepcopy(self._last_entry_meta.get(stock_code, {}).get('board_feedback', {})),
            'amount_pace': decision.amount_pace,
            'sector_confirmed': decision.sector_confirmed,
            'data_status': decision.data_status,
            'data_completeness': decision.data_completeness,
            'profile_samples': decision.profile_samples,
            'hold_minutes': decision.hold_minutes,
            'false_break_count': decision.false_break_count,
            'pullback_quality': decision.pullback_quality,
            'active_buy_ratio': decision.active_buy_ratio,
            'breakout_level': decision.breakout_level,
            'initial_confirm_time': decision.initial_confirm_time,
            'confirmation_policy': decision.confirmation_policy,
            'confirmation_reason': decision.confirmation_reason,
        })
        if self.config.replay_missing_evidence_policy != "legacy" and missing_entry_evidence(self.entry_attempts[-1]):
            failure = self._entry_evidence_missing.get((str(date), stock_code)) or {
                "trade_date": str(date), "code": stock_code,
                "evidence": decision.data_status if decision.data_status != 'complete' else reason_code,
                "reason": reason_text,
            }
            failure = dict(failure, code=stock_code, scope="candidate", action="skipped",
                           strategy_id=str(plan.get('策略ID') or 'default'))
            if reason_code == 'missing_sector_confirmation':
                failure['sector_evidence'] = dict(sector_evidence)
            self._candidate_evidence_samples.add((str(date), stock_code))
            if self.config.replay_missing_evidence_policy != SKIP_CANDIDATES:
                self._raise_replay_evidence([dict(failure, action="blocked")])
            self._candidate_evidence_skips[(str(date), stock_code, str(failure['evidence']))] = failure

    def _clear_entry_state(self, stock_code: str) -> None:
        """Discard transient signal metadata when a later account gate rejects the order."""
        self._last_entry_gap.pop(stock_code, None)
        self._last_entry_signal.pop(stock_code, None)
        self._last_entry_meta.pop(stock_code, None)

    def _record_gate_attempt(
        self, plan: pd.Series, date: str, stage: str, reason_code: str, reason: str,
        *, status: str = "rejected",
    ) -> None:
        """Record a deterministic non-signal rejection in the transaction funnel."""
        self.entry_attempts.append({
            'date': str(date or ''),
            'stock_code': str(plan.get('代码') or '').split('.', 1)[0].zfill(6),
            'stock_name': str(plan.get('名称') or ''),
            'plan_rank': self._int(plan.get('优先级')),
            'plan_score': self._float(plan.get('综合评分')),
            'entry_mode': '',
            'strategy_id': str(plan.get('策略ID') or 'default'),
            'strategy_name': str(plan.get('策略名称') or ''),
            'strategy_version': str(plan.get('策略版本') or ''),
            'stage': str(stage),
            'reason_code': str(reason_code),
            'signal': '',
            'status': str(status),
            'reason': str(reason),
            'confirm_time': '',
            'entry_time': '',
            'entry_price': 0.0,
            'open_gap_pct': 0.0,
            'amount_pace': 0.0,
            'sector_confirmed': False,
            'data_status': 'complete',
            'data_completeness': 1.0,
            'profile_samples': 0,
            'hold_minutes': 0,
            'false_break_count': 0,
            'pullback_quality': 0.0,
            'active_buy_ratio': 0.0,
        })

    def _check_buy_conditions(self, plan: pd.Series, date: str, stock_code: str, stock_name: str) -> Tuple[bool, float]:
        """
        检查买入条件
        
        Returns:
            (是否可以买入, 买入价格)
        """
        target_price = plan['目标价']
        self._last_entry_meta.get(stock_code, {}).pop('opening_evidence', None)
        entry_timing = plan.get('介入时机', '09:31-10:00')
        def reject(reason_code: str, reason: str, *, data_status: str = 'complete') -> Tuple[bool, float]:
            decision = EntryDecision(
                status=reason_code,
                reason=reason,
                open_gap_pct=self._last_entry_gap.get(stock_code, 0.0),
                data_status=data_status,
                data_completeness=0.0 if data_status != 'complete' else 1.0,
            )
            self._record_entry_attempt(plan, date, stock_code, stock_name, decision, entry_mode='')
            return False, 0.0
        from core.utils.stock_eligibility import new_entry_exclusion
        exclusion = new_entry_exclusion(dict(plan, stock_name=stock_name))
        if exclusion:
            return reject('restricted_stock', exclusion)
        feedback = self._board_feedback_context(stock_code, date)
        self._last_entry_meta.setdefault(stock_code, {})['board_feedback'] = feedback
        if self._is_full_day_suspended(stock_code, date, scope='candidate'):
            return reject('suspended', 'suspend_d确认全天停牌，当日不能买入', data_status='suspended')
        missing = self._entry_evidence_missing.get((str(date), stock_code))
        if missing:
            return reject('missing_minutes', str(missing.get('reason') or '分钟证据缺失或残缺'),
                          data_status='missing_minutes')
        
        # 获取当日开盘数据
        try:
            standardized_code = self._standardize_stock_code(stock_code)
            if self.config.production_rules:
                opening_evidence = self._production_inputs.get('opening_evidence', {}).get(stock_code, {})
                daily_data = {'open': self._production_inputs.get('opening_prices', {}).get(stock_code),
                              'pre_close': opening_evidence.get('pre_close')}
            else:
                daily_data = self.dm.get_stock_daily_data(standardized_code, date)
            
            if not daily_data:
                logger.info(f"{stock_name} 无法获取当日开盘数据，不能确认高开，放弃买入")
                return reject('missing_daily_open', '缺少当日开盘数据', data_status='missing_daily')
            
            from core.signals.opening import positive_price
            open_price = positive_price(daily_data.get('open'))
            high_price = daily_data.get('high', 0)
            low_price = daily_data.get('low', 0)
            
            if open_price is None:
                logger.info(f"{stock_name} 开盘价无效，不能确认高开，放弃买入")
                return reject('invalid_daily_open', '当日真实开盘价缺失或无效', data_status='missing_session_open')
                
        except ReplayEvidenceBlocked:
            raise
        except Exception as e:
            logger.debug(f"{stock_name} 获取开盘数据失败: {e}，不能确认高开，放弃买入")
            return reject('daily_open_error', f'获取开盘数据失败: {e}', data_status='daily_error')
        
        prev_close = positive_price(daily_data.get('pre_close'))
        if prev_close is None:
            prev_close = (self._production_inputs.get('previous', {}).get(stock_code, {}).get('close')
                          if self.config.production_rules else self._get_prev_close(stock_code, date))
        gap = open_gap_pct({"open": open_price, "pre_close": prev_close}, prev_close)
        if gap is None:
            logger.info(f"{stock_name} 昨收价缺失，无法计算开盘状态，放弃买入")
            return reject('missing_previous_close', '缺少昨收，无法计算开盘状态', data_status='missing_previous_close')
        self._last_entry_gap[stock_code] = gap
        from core.signals.opening import opening_context
        opening_facts = opening_context({'open_price': open_price, 'pre_close': daily_data.get('pre_close'),
                                         'opening_source': (opening_evidence.get('source') if self.config.production_rules else 'historical_daily'),
                                         'opening_received_at': (opening_evidence.get('received_at') if self.config.production_rules else '')},
                                        {'close': prev_close})
        self._last_entry_meta.setdefault(stock_code, {})['opening_evidence'] = opening_facts

        lu_price = None
        if prev_close and prev_close > 0:
            lu_price = limit_up_price(prev_close, stock_code, stock_name)

        entry_mode = self._entry_mode_for_plan(plan, gap)
        if entry_mode is None:
            allowed = self._plan_execution(plan).get('allowed_entry_modes') or []
            logger.info(
                f"{stock_name} 当前开盘分层不在策略允许入场模式内: {','.join(str(item) for item in allowed)}"
            )
            return reject('entry_mode_not_allowed', '开盘分层不在策略允许入场模式内')
        if entry_mode != ENTRY_FIXED:
            previous_date = self.calendar.prev(date)
            previous_bar = (self._production_inputs.get('previous', {}).get(stock_code, {}) if self.config.production_rules
                            else self._get_stock_daily_bar(stock_code, previous_date) or {})
            auction = self._auction_rows.get((str(date), stock_code), {})
            amount_ratio = self._float(
                plan.get('原始_amount_ratio_5d'), self._float(plan.get('原始_amount_ratio'))
            )
            execution = self._plan_execution(plan)
            if self.config.production_rules:
                from core.realtime.entry_signal_service import RealtimeEntrySignalService
                amount_ratio = RealtimeEntrySignalService._metric(self._production_rows.get(stock_code, {}), 'amount_ratio', 0)
            if requires_auction(execution) and not (
                self._float(auction.get('竞价成交额')) > 0
                and self._float(auction.get('竞价成交量')) > 0
            ):
                return reject('missing_auction_evidence', '策略要求的集合竞价证据缺失',
                              data_status='missing_auction')
            structural = bool(set(execution.get('allowed_entry_modes') or []).intersection({'limit_pullback', 'limit_reversal'}))
            evaluate = self.minute_evaluator.evaluate_strategy if structural else self.minute_evaluator.evaluate
            evaluated = evaluate(
                **({'execution': execution} if structural else {}),
                structure=(self._plan_execution(plan).get('structures') or {}).get(entry_mode, {}),
                confirmation_deadline=resolve_entry_deadline(self._plan_execution(plan), entry_mode),
                mode=entry_mode,
                bars=self._minute_frames.get((str(date), stock_code), pd.DataFrame()),
                open_gap=gap,
                prev_close=prev_close,
                previous_session_close=self._float(previous_bar.get('close')),
                previous_amount=self._daily_amount_yuan(previous_bar),
                previous_volume=self._float(previous_bar.get('vol_hand'), self._float(previous_bar.get('vol'))),
                auction_amount=self._float(auction.get('竞价成交额')),
                auction_volume=self._float(auction.get('竞价成交量')),
                plan_amount_ratio=amount_ratio,
                limit_price=self._float(lu_price),
                is_leader=self._is_leader_or_mainline_core(plan),
                sector_sync=self._sector_sync_checker(plan, date, stock_code),
                expected_amount_fraction=(
                    lambda time_text, amount=self._daily_amount_yuan(previous_bar):
                    self.amount_profiles.expected_fraction(amount, time_text)[0]
                ) if self.amount_profiles.available else None,
                amount_profile_samples=self.amount_profiles.expected_fraction(
                    self._daily_amount_yuan(previous_bar), "10:00:00"
                )[1],
            )
            if structural:
                entry_mode, decision = evaluated
            else:
                decision = evaluated
            if (decision.filled and self._float(lu_price) > 0
                    and decision.entry_price * (1 + self.config.slippage) >= self._float(lu_price)):
                decision = replace(decision, status="signal_unfilled", entry_price=0,
                                   reason="计入滑点后达到涨停价，缺少可成交证据")
            self._record_entry_attempt(plan, date, stock_code, stock_name, decision, entry_mode=entry_mode)
            if not decision.filled:
                logger.info(
                    f"{stock_name} {decision.signal or '分钟入场'} {decision.status}: {decision.reason}"
                )
                return False, 0
            self._last_entry_signal[stock_code] = decision.signal
            from backtest.reversal_entry import STRUCTURAL_LABELS
            entry_mode = next((key for key, label in STRUCTURAL_LABELS.items() if label == decision.signal), entry_mode)
            self._last_entry_meta[stock_code] = {
                'opening_evidence': opening_facts,
                'board_feedback': feedback,
                'structural_stop': (self._plan_execution(plan).get('structures') or {}).get(entry_mode, {}).get('protection', 0),
                'confirm_time': decision.confirm_time,
                'entry_time': decision.entry_time,
                'amount_pace': decision.amount_pace,
                'sector_confirmed': decision.sector_confirmed,
                'breakout_level': decision.breakout_level,
                'initial_confirm_time': decision.initial_confirm_time,
                'confirmation_policy': decision.confirmation_policy,
                'confirmation_reason': decision.confirmation_reason,
            }
            entry_price = decision.entry_price * (1 + self.config.slippage)
            if self.config.production_rules:
                from core.utils.price_limit import round_price
                entry_price = round_price(entry_price)
                from core.signals.production_rules import candidate_strength
                row = self._production_rows.get(stock_code, {})
                evidence = self._sector_evidence_snapshots.get((str(date), stock_code), {}).get(decision.confirm_time, {})
                self._last_entry_meta[stock_code]['entry_strength_score'] = candidate_strength(
                    {**row, 'entry_mode': entry_mode, 'sector_detail': evidence, 'board_feedback': feedback})
            logger.info(
                f"{stock_name} {decision.confirm_time}确认{decision.signal}，"
                f"{decision.entry_time}按下一分钟开盘价{decision.entry_price:.2f}成交"
            )
            return True, entry_price

        # 原固定区间仅作为基线对照，不再是默认入场方式。
        if gap <= self.config.min_open_gap:
            label = "低开" if gap < 0 else "平开"
            logger.info(f"{stock_name} {label}{gap:.2%}，未高开，放弃竞价买点")
            return reject('fixed_gap_not_high_open', f'{label}不符合固定开盘区间')
        if gap > self.config.max_open_gap:
            logger.info(f"{stock_name} 高开{gap:.2%}超过{self.config.max_open_gap:.2%}，不追高")
            return reject('fixed_gap_too_high', '高开超过固定区间上限')
        if lu_price is not None and open_price >= lu_price * 0.998:
            logger.info(f"{stock_name} 涨停开盘，无法买入")
            return reject('locked_limit_unfilled', '涨停开盘无法成交')

        plan_score = self._float(plan.get('综合评分'))
        if plan_score < self.config.direct_entry_min_score:
            if not self._intraday_strength_ready(plan):
                logger.info(f"{stock_name} 评分{plan_score:.1f}未达到竞价买点，盘中转强条件不足")
                return reject('score_and_strength_insufficient', '评分不足且盘中转强前置条件未满足')
            trigger_price = open_price * (1 + self.config.intraday_strength_trigger_pct)
            if high_price < trigger_price:
                logger.info(
                    f"{stock_name} 盘中最高价未触及转强价{trigger_price:.2f}，继续观察"
                )
                return reject('intraday_trigger_not_reached', '盘中最高价未触及转强触发价')
            self._last_entry_signal[stock_code] = "盘中转强"
            entry_price = trigger_price * (1 + self.config.slippage)
            logger.info(f"{stock_name} 盘中触及转强价{trigger_price:.2f}，确认买入")
            return True, entry_price

        self._last_entry_signal[stock_code] = "竞价买点"
        
        # 根据介入时机判断买入价格
        # 竞价时段 (09:24:30-09:25:00)
        if '09:24' in entry_timing or '竞价' in entry_timing:
            # 集合竞价买入，使用开盘价
            entry_price = open_price * (1 + self.config.slippage)
            logger.debug(f"{stock_name} 集合竞价买入，开盘价: {open_price:.2f}")
            return True, entry_price
        
        # 开盘后时段 (09:31-10:00 等)
        # 检查目标价是否在当日价格范围内
        if target_price > 0:
            if low_price <= target_price <= high_price:
                # 目标价在范围内，可以成交
                entry_price = target_price * (1 + self.config.slippage)
                logger.debug(f"{stock_name} 目标价{target_price:.2f}在当日价格范围[{low_price:.2f}, {high_price:.2f}]内")
                return True, entry_price
            elif target_price < low_price:
                # 目标价低于最低价，以最低价成交
                entry_price = low_price * (1 + self.config.slippage)
                logger.debug(f"{stock_name} 目标价{target_price:.2f}低于最低价{low_price:.2f}，以最低价买入")
                return True, entry_price
            else:
                # 目标价高于最高价，无法成交
                logger.info(f"{stock_name} 目标价{target_price:.2f}高于最高价{high_price:.2f}，无法买入")
                return reject('target_price_not_reached', '目标价高于当日最高价')
        else:
            # 目标价为0，使用开盘价
            entry_price = open_price * (1 + self.config.slippage)
            return True, entry_price

    def _board_feedback_context(self, stock_code, date):
        from core.signals.board_feedback import assess_feedback, feedback_policy
        policy = getattr(self.minute_evaluator, 'board_feedback_policy', feedback_policy(self.config))
        key = (date, stock_code, tuple(sorted(policy.items())))
        if key not in self._board_feedback_contexts:
            inputs = self._production_inputs.get('board_feedback_inputs', {}) if self.config.production_rules else {}
            if policy['enabled'] and not self.config.production_rules:
                from core.realtime.board_feedback_repository import BoardFeedbackRepository
                if self._board_feedback_repository is None:
                    self._board_feedback_repository = BoardFeedbackRepository(
                        getattr(self.dm, 'cache_dir', None), calendar=self.calendar)
                inputs = self._board_feedback_repository.capture(date, [stock_code])
            self._board_feedback_contexts[key] = assess_feedback(inputs, stock_code, date, policy)
        return self._board_feedback_contexts[key]

    def _intraday_strength_ready(self, plan: pd.Series) -> bool:
        """Point-in-time factor gate used before waiting for the intraday trigger."""
        tech_score = self._float(plan.get('因子_tech_score'))
        sector_score = self._float(plan.get('因子_stk_sector_resonance_score'))
        amount_ratio = self._float(plan.get('原始_amount_ratio'))
        return (
            tech_score >= self.config.intraday_min_tech_score
            and sector_score >= self.config.intraday_min_sector_resonance
            and self.config.intraday_min_amount_ratio
            <= amount_ratio
            <= self.config.intraday_max_amount_ratio
        )

    def _calculate_position_size(self, plan: pd.Series) -> float:
        """Use only prior closed trades to size the next order without look-ahead."""
        if self.config.account_position_pct > 0:
            position_pct = min(
                float(self.config.account_position_pct),
                float(self.config.max_position_per_stock),
            )
            self._last_sizing_meta = {
                "position_pct": round(position_pct, 4),
                "method": "aggressive_three_position_account",
                "rationale": f"激进三仓账户按单票{position_pct:.0%}配置",
                "sample_size": 0,
            }
            return self.total_capital * position_pct

        position_map = {
            'light': 0.1,
            'medium': 0.15,
            'heavy': 0.2
        }

        position_str = plan.get('仓位', 'medium')
        position_pct = position_map.get(position_str, 0.1)
        explicit_position_pct = self._float(plan.get('计划基础仓位%')) / 100.0
        if explicit_position_pct > 0:
            position_pct = explicit_position_pct

        # 热点共振增加仓位
        if plan.get('热点共振', False):
            position_pct *= 1.2
        position_pct = min(position_pct, self.config.max_position_per_stock)

        from risk.kelly_sizer import KellySizer
        from risk.risk_config import RiskConfig

        sizing_config = RiskConfig(
            max_position_per_stock=self.config.max_position_per_stock,
            position_sizing_mode=self.config.position_sizing_mode,
            fixed_risk_per_trade=self.config.fixed_risk_per_trade,
            kelly_fraction=self.config.kelly_fraction,
            kelly_min_samples=self.config.kelly_min_samples,
            kelly_max_position=self.config.kelly_max_position,
            kelly_credibility=self.config.kelly_credibility,
            kelly_payoff_haircut=self.config.kelly_payoff_haircut,
        )
        sizer = KellySizer(sizing_config)
        mode = str(self.config.position_sizing_mode or "fixed_risk").strip().lower()
        closed = [
            row for row in self.trade_history
            if str(row.action).upper().startswith("SELL")
            and str(row.pattern_type) == str(plan.get('模式') or row.pattern_type)
        ]
        if mode == "conservative_kelly":
            wins = [float(row.pnl_pct) for row in closed if float(row.pnl_pct) > 0]
            losses = [abs(float(row.pnl_pct)) for row in closed if float(row.pnl_pct) < 0]
            win_rate = len(wins) / len(closed) if closed else 0.0
            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = float(np.mean(losses)) if losses else 0.0
            payoff = avg_win / avg_loss if avg_loss > 0 else 0.0
            result = sizer.size(
                win_rate=win_rate,
                payoff_ratio=payoff,
                n=len(closed),
                base_position_pct=position_pct,
                stop_distance=self.config.stop_loss_pct,
                data_quality=self._quality_ratio(plan, "数据完整度", "data_completeness"),
                regime_match=self._quality_ratio(plan, "市场适配度", "regime_match"),
                tradability=self._quality_ratio(plan, "可成交系数", "tradability"),
            )
        else:
            result = sizer.fixed_risk_size(
                self.config.stop_loss_pct, base_position_pct=position_pct,
            )
        result["sample_size"] = len(closed)
        self._last_sizing_meta = result
        return self.total_capital * float(result.get("position_pct") or 0.0)

    def _rotate_to_stronger_candidate(
        self,
        plan: pd.Series,
        date: str,
        stock_code: str,
        stock_name: str,
    ) -> bool:
        """Use a confirmed intraday signal to replace a weaker T+1 holding."""
        candidate_strength = (self._float((self._last_entry_meta.get(stock_code) or {}).get('entry_strength_score'), self._plan_strength(plan))
                              if getattr(self.config, 'production_rules', False) else self._plan_strength(plan))
        entry_time = str((self._last_entry_meta.get(stock_code) or {}).get("entry_time") or "09:30:00")
        strategy_cap = int(self._plan_execution(plan).get('max_positions') or 0)
        strategy_id = str(plan.get('策略ID') or 'default')
        at_strategy_cap = strategy_cap and sum(p.get('strategy_id') == strategy_id for p in self.current_positions.values()) >= strategy_cap
        eligible = []
        for code, position in self.current_positions.items():
            if at_strategy_cap and str(position.get('strategy_id') or 'default') != strategy_id:
                continue
            if self._is_full_day_suspended(code, date, scope='holding'):
                continue
            if str(position.get("entry_date") or "") >= str(date):
                continue
            price = self._minute_price_at(code, date, entry_time)
            if price <= 0:
                continue
            entry_price = self._float(position.get("entry_price"))
            pnl_pct = (price / entry_price - 1.0) * 100.0 if entry_price > 0 else 0.0
            current_strength = max(
                0.0,
                min(
                    100.0,
                    self._float(position.get("plan_score"), 50.0)
                    + max(min(pnl_pct * 0.8, 12.0), -12.0),
                ),
            )
            if getattr(self.config, 'production_rules', False):
                from core.signals.production_rules import holding_strength
                current_strength = holding_strength({
                    'metadata': {'entry_strength_score': position.get('entry_strength_score', position.get('plan_score', 50))},
                    'entry_price': entry_price, 'last_price': price, 'latest_action': position.get('latest_action', 'hold')})
            eligible.append((current_strength, code, price, position))
        if not eligible:
            return False
        current_strength, weak_code, sell_price, weak_position = min(eligible, key=lambda item: item[0])
        edge = candidate_strength - current_strength
        if edge < float(self.config.rotation_min_edge):
            return False
        weak_name = str(weak_position.get("stock_name") or weak_code)
        self._execute_sell(weak_code, sell_price, date, "rotation_to_stronger", exit_time=entry_time)
        logger.info(
            f"[{date} {entry_time}] 换仓 {weak_name}({current_strength:.1f}) -> "
            f"{stock_name}({candidate_strength:.1f})，强度差{edge:.1f}"
        )
        self._record_gate_attempt(
            plan,
            date,
            "portfolio_gate",
            "rotation_to_stronger",
            f"卖出{weak_name}，新信号强度高{edge:.1f}分",
            status="rotated",
        )
        return True

    @classmethod
    def _plan_strength(cls, plan: pd.Series) -> float:
        for key in (
            "综合评分",
            "计划评分",
            "转强分",
            "龙头评分",
            "3日强势成功率%",
            "score",
        ):
            value = cls._float(plan.get(key), -1.0)
            if value >= 0:
                return max(0.0, min(100.0, value))
        return 50.0

    def _minute_price_at(self, stock_code: str, date: str, at_time: str) -> float:
        frame = self._load_exit_minute_bars(stock_code, date)
        if frame.empty:
            return 0.0
        bars = self._normalize_full_minute_bars(frame)
        if bars.empty or "time" not in bars.columns:
            return 0.0
        matched = bars[bars["time"].astype(str) <= str(at_time)]
        if matched.empty:
            return 0.0
        row = matched.iloc[-1]
        field = 'open' if str(row['time']) == str(at_time) else 'close'
        return self._float(row.get(field))

    @staticmethod
    def _quality_ratio(plan: pd.Series, *keys: str) -> float:
        for key in keys:
            value = plan.get(key)
            if value not in (None, ""):
                try:
                    number = float(value)
                    return min(max(number / 100.0 if number > 1.0 else number, 0.0), 1.0)
                except (TypeError, ValueError):
                    continue
        return 1.0

    def _entry_gap_position_multiplier(self, gap: float) -> float:
        """Reduce size near the upper edge of the allowed opening-gap range."""
        value = self._float(gap)
        if value >= self.config.reduced_position_gap:
            return max(0.0, min(self.config.high_gap_position_multiplier, 1.0))
        return 1.0

    def _check_stop_loss_take_profit(self, date: str, *, until_time: Optional[str] = None):
        """检查退出：硬止损/时间止损保持不变，盈利只按高点回撤退出。"""
        stocks_to_sell = []
        if not hasattr(self, '_exit_scan_times'):
            self._exit_scan_times = {}

        for stock_code, position in self.current_positions.items():
            if str(position.get('entry_date') or '') >= str(date):
                continue
            if self._is_full_day_suspended(stock_code, date, scope='holding'):
                continue
            daily_bar = self._get_stock_daily_bar(stock_code, date)
            if not daily_bar:
                continue
            if (date, stock_code) not in self._exit_scan_times:
                self._apply_corporate_action_adjustment(stock_code, position, daily_bar, date)
            current_price = self._float(daily_bar.get('close'))
            if current_price <= 0:
                continue
            session_open = self._float(daily_bar.get('open'), current_price)
            session_low = self._float(daily_bar.get('low'), current_price)

            minute_result = self._minute_exit_decision(stock_code, position, date, daily_bar, until_time=until_time)
            if minute_result is not None:
                self._exit_execution_audit["minute_days"] += 1
                if minute_result.get("sell"):
                    self._exit_execution_audit["minute_exit_triggers"] += 1
                    stocks_to_sell.append((
                        stock_code,
                        self._float(minute_result.get("price"), current_price),
                        str(minute_result.get("reason") or "minute_exit"),
                        str(minute_result.get("time") or ''),
                        minute_result.get('shares'),
                    ))
                continue
            if until_time is not None:
                continue
            self._exit_execution_audit["daily_fallback_days"] += 1

            # 最高价采用当日 high，而不是只看收盘价；触发仍按收盘确认，避免
            # 仅有 OHLC 时假设无法得知的日内高低点先后顺序。
            session_high = self._float(daily_bar.get('high'), current_price)
            prior_peak = max(
                self._float(position.get('highest_price'), position['entry_price']),
                position['entry_price'],
            )
            self._update_excursion(position, session_high, session_low)
            position['highest_price'] = max(
                self._float(position.get('highest_price'), position['entry_price']),
                session_high,
                current_price,
            )

            # 计算当前盈亏比例
            current_pnl_pct = (current_price - position['entry_price']) / position['entry_price']

            # 更新市值
            position['market_value'] = position['shares'] * current_price
            position['last_close'] = current_price

            # ========== 1. 硬止损（必须执行）==========
            stop_price = self._float(position.get('stop_loss_price'))
            if session_open <= stop_price:
                stocks_to_sell.append((stock_code, session_open, 'stop_loss_gap'))
                self._exit_execution_audit["daily_exit_triggers"] += 1
                logger.info(f"[{date}] {position['stock_name']} 跳空跌破止损线，按开盘价止损: {session_open:.2f}")
                continue
            if session_low <= stop_price:
                stocks_to_sell.append((stock_code, stop_price, 'stop_loss'))
                self._exit_execution_audit["daily_exit_triggers"] += 1
                logger.info(f"[{date}] {position['stock_name']} 盘中触发硬止损: {stop_price:.2f}")
                continue

            # ========== 2. 跟踪止损（移动止盈）==========
            exit_config = self._position_exit_config(position)
            peak_profit_today = (session_high - position['entry_price']) / position['entry_price']
            possible_distance = self._trailing_stop_distance(peak_profit_today, exit_config)
            if (
                peak_profit_today >= exit_config['trailing_activation']
                and session_low <= session_high * (1.0 - possible_distance)
                and session_high > prior_peak
            ):
                self._exit_execution_audit["ambiguous_daily_bars"] += 1
            # Daily OHLC cannot establish whether today's new high occurred
            # before the low. Only a peak known before this session may trigger
            # a daily fallback trailing exit; today's high becomes tomorrow's peak.
            trailing_peak = (
                session_high
                if str(self.config.daily_ohlc_path_policy).lower() == "optimistic_high_first"
                else prior_peak
            )
            if exit_config['trailing_stop'] > 0 and trailing_peak > position['entry_price']:
                # 计算从最高点的回撤
                drawdown_from_high = (trailing_peak - current_price) / trailing_peak

                # 只有当盈利超过激活阈值后才启动跟踪止损
                profit_pct = (trailing_peak - position['entry_price']) / position['entry_price']

                trailing_distance = self._trailing_stop_distance(profit_pct, exit_config)
                if profit_pct >= exit_config['trailing_activation'] and drawdown_from_high >= trailing_distance:
                    stocks_to_sell.append((stock_code, current_price, 'trailing_stop'))
                    self._exit_execution_audit["daily_exit_triggers"] += 1
                    logger.info(f"[{date}] {position['stock_name']} 触发跟踪止损: {current_price:.2f} "
                               f"(最高点{position['highest_price']:.2f}, 回撤{drawdown_from_high:.2%}, "
                               f"阶段线{trailing_distance:.2%})")
                    continue

            # ========== 3. 时间止损 ==========
            holding_days = self._calculate_holding_days(position['entry_date'], date)
            if holding_days >= int(exit_config['time_stop_days']):
                # 持仓时间过长且盈利未达到预期，强制卖出
                if current_pnl_pct < exit_config['time_stop_profit_threshold']:
                    stocks_to_sell.append((stock_code, current_price, 'time_stop'))
                    self._exit_execution_audit["daily_exit_triggers"] += 1
                    logger.info(f"[{date}] {position['stock_name']} 触发时间止损: {current_price:.2f} "
                               f"(持仓{holding_days}天, 盈利{current_pnl_pct:.2%})")
                    continue

        # 执行全部卖出
        for event in stocks_to_sell:
            stock_code, sell_price, reason = event[:3]
            at_time = event[3] if len(event) > 3 else ''
            self._execute_sell(stock_code, sell_price, date, reason, exit_time=at_time,
                               shares_to_sell=event[4] if len(event) > 4 else None)

    def _minute_exit_decision(
        self, stock_code: str, position: Dict[str, Any], date: str, daily_bar: Dict[str, Any],
        *, until_time: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Chronologically evaluate exits when one-minute bars are available.

        ``None`` requests the explicitly audited daily-OHLC fallback. A dict
        means the minute session was handled, even when no exit was triggered.
        """
        feedback = self._board_feedback_context(stock_code, date)
        bars = (pd.DataFrame() if str(self.config.exit_minute_data_policy or "").lower() == "daily_only"
                else self._load_exit_minute_bars(stock_code, date))
        if bars.empty:
            if (feedback['active'] and feedback.get('board_day') == 5
                    and position.get('entry_date') == feedback.get('feedback_date')):
                failure = {'trade_date': date, 'code': stock_code, 'scope': 'holding',
                           'evidence': 'board_feedback_exit_minutes',
                           'reason': '缺少第5日反抽退出分钟证据，不使用全天最高价伪造卖出',
                           'action': 'blocked'}
                if failure not in self._exit_evidence_failures:
                    self._exit_evidence_failures.append(failure)
                if self.config.production_rules and self.config.replay_missing_evidence_policy != 'legacy':
                    self._raise_replay_evidence([failure])
            return None
        cursors = getattr(self, '_exit_scan_times', {})
        previous_time = cursors.get((date, stock_code))
        if previous_time:
            bars = bars[bars['time'].astype(str) > previous_time]
        if until_time is not None:
            bars = bars[bars['time'].astype(str) < until_time]
        if bars.empty:
            return {'sell': False}
        entry_price = self._float(position.get("entry_price"))
        stop_price = self._float(position.get("stop_loss_price"))
        exit_config = self._position_exit_config(position)
        peak = max(self._float(position.get("highest_price"), entry_price), entry_price)
        holding_days = self._calculate_holding_days(position["entry_date"], date)
        session_low = entry_price
        session_high = peak
        first = not previous_time
        for row in bars.itertuples(index=False):
            if hasattr(self, '_exit_scan_times'):
                self._exit_scan_times[(date, stock_code)] = str(row.time)
            minute_open = self._float(getattr(row, "open", 0.0))
            minute_high = self._float(getattr(row, "high", minute_open), minute_open)
            minute_low = self._float(getattr(row, "low", minute_open), minute_open)
            minute_close = self._float(getattr(row, "close", minute_open), minute_open)
            if first and stop_price > 0 and minute_open <= stop_price:
                self._update_excursion(position, minute_open, minute_open)
                return {"sell": True, "price": minute_open, "reason": "stop_loss_gap_minute", "time": str(row.time)}
            first = False
            session_low = min(session_low, minute_low)
            session_high = max(session_high, minute_high)

            # One-minute OHLC still hides tick order. Use the conservative
            # adverse-first path inside a minute before accepting a new peak.
            if stop_price > 0 and minute_low <= stop_price:
                self._update_excursion(position, session_high, session_low)
                price = min(minute_open, stop_price) if minute_open > 0 else stop_price
                return {"sell": True, "price": price, "reason": "stop_loss_minute", "time": str(row.time)}

            peak = max(peak, minute_high)
            position["highest_price"] = peak
            profit_pct = (peak - entry_price) / entry_price if entry_price > 0 else 0.0
            trailing_distance = self._trailing_stop_distance(profit_pct, exit_config)
            trailing_price = peak * (1.0 - trailing_distance)
            if (
                exit_config["trailing_stop"] > 0
                and profit_pct >= exit_config["trailing_activation"]
                and minute_low <= trailing_price
            ):
                self._update_excursion(position, session_high, session_low)
                price = min(minute_open, trailing_price) if 0 < minute_open < trailing_price else trailing_price
                return {"sell": True, "price": price, "reason": "trailing_stop_minute", "time": str(row.time)}
            position["last_close"] = minute_close
            from core.signals.board_feedback import rebound_exit
            feedback_quote = {'code': stock_code, 'last_price': minute_close, 'date': date, 'is_stale': False,
                              'open_price': self._float(daily_bar.get('open'))}
            rebound = rebound_exit(feedback, {**position, 'code': stock_code}, feedback_quote, date)
            if rebound:
                position['board_feedback_exit'] = {'context': feedback, 'trigger': rebound, 'time': str(row.time)}
                return {'sell': True, 'price': minute_close, 'reason': 'three_board_feedback_rebound', 'time': str(row.time)}
            if self.config.production_rules:
                from core.portfolio.exit_decision_service import ExitDecisionService
                quote = {'last_price': minute_close, 'high_price': peak, 'time': str(row.time),
                         'date': date, 'is_stale': False,
                         'open_price': self._float(daily_bar.get('open')),
                         'change_pct': (minute_close / self._float(daily_bar.get('pre_close')) - 1) * 100
                                      if self._float(daily_bar.get('pre_close')) > 0 else 0}
                checker = self._sector_sync_checker(pd.Series({'共振板块': position.get('resonance_sectors') or ''}), date, stock_code)
                state = checker(str(row.time))
                sector = {**self._sector_evidence[(str(date), stock_code)], 'state': state}
                if self.minute_evaluator.confirmation_policy.require_sector and state is None and position.get('resonance_sectors') and self.config.replay_missing_evidence_policy != 'legacy':
                    self._raise_replay_evidence([{'trade_date': date, 'code': stock_code,
                        'scope': 'holding', 'evidence': 'holding_sector_minutes',
                        'reason': '持仓板块分钟证据不足', 'action': 'blocked', 'sector_evidence': sector}])
                decision = ExitDecisionService().evaluate(
                    {'code': stock_code, 'board_feedback': feedback,
                     'entry_price': entry_price, 'entry_date': position['entry_date'], 'shares': position['shares'],
                     'sellable_shares': position['shares'] if position['entry_date'] < date else 0,
                     'strategy_id': position.get('strategy_id'), 'high_watermark': peak,
                     'structural_stop': stop_price,
                     'metadata': {'strategy_execution': {'exit': exit_config}, 'protection_price_source': '账户风险底线'}},
                    quote, signal_date=date, sector_context=sector,
                    market_context=self._production_market_context(date, str(row.time)))
                position['latest_action'] = decision.action
                if decision.action in {'sell', 'reduce'} and decision.can_sell:
                    exit_key = (str(date), decision.action, ';'.join(decision.reasons))
                    if list(exit_key) in position.get('context_exit_keys', []):
                        continue
                    shares = (position['shares'] if decision.action == 'sell' else
                              int(position['shares'] * .5 / 100) * 100)
                    if shares > 0:
                        position.setdefault('context_exit_keys', []).append(list(exit_key))
                        return {'sell': True, 'price': minute_close, 'reason': 'context_' + decision.action,
                                'time': str(row.time), 'shares': shares}

            minute_pnl = minute_close / entry_price - 1 if entry_price > 0 else 0.0
            if (str(getattr(row, "time", "")) >= "14:55:00"
                    and holding_days >= int(exit_config["time_stop_days"])
                    and minute_pnl < exit_config["time_stop_profit_threshold"]):
                self._update_excursion(position, session_high, session_low)
                return {"sell": True, "price": minute_close, "reason": "time_stop_minute_close", "time": str(row.time)}

        self._update_excursion(position, session_high, session_low)
        close_price = self._float(bars.iloc[-1].get("close"), self._float(daily_bar.get("close")))
        position["market_value"] = position["shares"] * close_price
        position["last_close"] = close_price
        holding_days = self._calculate_holding_days(position["entry_date"], date)
        current_pnl_pct = (close_price - entry_price) / entry_price if entry_price > 0 else 0.0
        if (
            until_time is None
            and
            holding_days >= int(exit_config["time_stop_days"])
            and current_pnl_pct < exit_config["time_stop_profit_threshold"]
        ):
            return {"sell": True, "price": close_price, "reason": "time_stop_minute_close", "time": str(bars.iloc[-1]["time"])}
        return {"sell": False}

    def _production_market_context(self, date, at_time):
        # A daily close or T-1 regime is not an intraday index observation.
        return {'market_score': 50, 'regime': '数据不足', 'index_count': 0,
                'data_completeness': 0, 'source': 'historical_market_context_unavailable'}

    def _load_exit_minute_bars(self, stock_code: str, date: str) -> pd.DataFrame:
        key = (str(date), str(stock_code).zfill(6))
        if key in self._exit_minute_frames:
            return self._exit_minute_frames[key]
        frame = pd.DataFrame()
        policy = str(self.config.exit_minute_data_policy or "cache_or_fetch").lower()
        ts_code = self._standardize_stock_code(stock_code)
        stock_dir_value = getattr(self.dm, "stock_dir", None)
        cache_file = Path(stock_dir_value) / "tick" / f"{ts_code}_{date}.csv" if stock_dir_value else None
        try:
            if cache_file is not None and minute_cache_exists(cache_file):
                frame = read_minute_csv(cache_file)
                from core.data.minute_quality import inspect_minute_bars

                if not inspect_minute_bars(frame, str(date), expected_code=ts_code)["valid"]:
                    frame = pd.DataFrame()
            if frame.empty and policy == "cache_or_fetch":
                frame = self.dm.get_stock_tick(ts_code, str(date))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{date}] {stock_code} 退出分钟行情不可用: {exc}")
        normalized = self._normalize_full_minute_bars(frame)
        if self.config.replay_missing_evidence_policy != "legacy":
            from core.data.minute_quality import inspect_minute_bars
            if not inspect_minute_bars(frame, str(date), expected_code=ts_code)["valid"]:
                normalized = pd.DataFrame()
        if normalized.empty:
            getter = getattr(self.dm, "get_minute_quality_report", None)
            quality = getter(ts_code, str(date)) if callable(getter) else {}
            self._exit_evidence_failures.append({"trade_date": str(date), "code": str(stock_code).zfill(6),
                "evidence": "exit_minute_bars", "reason": (quality or {}).get("reason") or "持仓退出分钟证据缺失或残缺"})
        self._exit_minute_frames[key] = normalized
        return normalized

    @staticmethod
    def _normalize_full_minute_bars(frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        data = frame.copy()
        if "time" not in data.columns and "datetime" in data.columns:
            data["time"] = pd.to_datetime(data["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        if "time" not in data.columns:
            return pd.DataFrame()

        def normalize_time(value: Any) -> str:
            text = str(value or "").strip().split(" ")[-1]
            if len(text) == 5 and text[2] == ":":
                return text + ":00"
            parsed = pd.to_datetime(text, errors="coerce")
            return parsed.strftime("%H:%M:%S") if pd.notna(parsed) else text[-8:]

        data["time"] = data["time"].map(normalize_time)
        for column in ("open", "high", "low", "close"):
            if column not in data.columns:
                data[column] = data.get("price", 0.0)
            data[column] = pd.to_numeric(data[column], errors="coerce")
        return data[
            data["time"].between("09:30:00", "15:00:59") & (data["close"] > 0)
        ].sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)

    def _apply_corporate_action_adjustment(
        self, stock_code: str, position: Dict, daily_bar: Dict, date: str,
    ) -> None:
        """按官方前收价修正除权除息造成的价格断点，保持持仓价值连续。"""
        official_pre_close = self._float(daily_bar.get('pre_close'))
        reference_close = self._float(position.get('last_close'))
        if reference_close <= 0:
            reference_close = self._float(self._get_prev_close(stock_code, date))
        if official_pre_close <= 0 or reference_close <= 0:
            return
        ratio = official_pre_close / reference_close
        if abs(ratio - 1.0) <= 0.001 or not 0.2 <= ratio <= 5.0:
            return
        if str(position.get('last_adjustment_date') or '') == str(date):
            return

        old_shares = self._float(position.get('shares'))
        position['entry_price'] = self._float(position.get('entry_price')) * ratio
        position['highest_price'] = self._float(position.get('highest_price')) * ratio
        position['stop_loss_price'] = self._float(position.get('stop_loss_price')) * ratio
        for key in ('max_favorable_price', 'min_adverse_price', 'last_close'):
            value = self._float(position.get(key))
            if value > 0:
                position[key] = value * ratio
        position['shares'] = old_shares / ratio if ratio > 0 else old_shares
        position['last_adjustment_date'] = str(date)
        logger.info(
            f"[{date}] {position.get('stock_name') or stock_code} 检测到除权除息价格调整: "
            f"昨收{reference_close:.2f} -> 前收{official_pre_close:.2f}, 比例{ratio:.6f}"
        )

    @classmethod
    def _daily_amount_yuan(cls, daily_bar: Dict[str, Any]) -> float:
        """将日线成交额统一为元，与分钟 amount 口径一致。

        Silver 使用 ``amount_yuan``；DataManager/Tushare ``daily.amount`` 的原始
        单位是千元。不能直接把两者相比。
        """
        if not daily_bar:
            return 0.0
        amount_yuan = cls._float(daily_bar.get('amount_yuan'))
        if amount_yuan > 0:
            return amount_yuan
        return cls._float(daily_bar.get('amount')) * 1000.0

    def _trailing_stop_distance(
        self, peak_profit_pct: float, exit_config: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Return the pullback distance for the current profit stage."""
        cfg = exit_config or self._execution_exit_config({})
        from backtest.exit_policy import trailing_distance

        return trailing_distance(max(self._float(peak_profit_pct), 0.0), cfg)

    def _execution_exit_config(self, execution: Dict[str, Any]) -> Dict[str, Any]:
        from backtest.exit_policy import normalize_exit_parameters

        raw = execution.get('exit') if isinstance(execution.get('exit'), dict) else {}
        return normalize_exit_parameters({
            'hard_stop_loss': self._float(raw.get('hard_stop_loss'), self.config.stop_loss_pct),
            'trailing_activation': self._float(raw.get('trailing_activation'), self.config.trailing_activation_pct),
            'trailing_early_stop': self._float(raw.get('trailing_early_stop'), self.config.trailing_early_stop_pct),
            'trailing_mid_profit': self._float(raw.get('trailing_mid_profit'), self.config.trailing_mid_profit_pct),
            'trailing_mid_stop': self._float(raw.get('trailing_mid_stop'), self.config.trailing_mid_stop_pct),
            'trailing_high_profit': self._float(raw.get('trailing_high_profit'), self.config.trailing_high_profit_pct),
            'trailing_stop': self._float(raw.get('trailing_stop'), self.config.trailing_stop_pct),
            'time_stop_days': max(1, self._int(raw.get('time_stop_days'), self.config.time_stop_days)),
            'time_stop_profit_threshold': self._float(
                raw.get('time_stop_profit_threshold'), self.config.time_stop_profit_threshold,
            ),
        })

    def _position_exit_config(self, position: Dict[str, Any]) -> Dict[str, Any]:
        saved = position.get('exit_config')
        if isinstance(saved, dict) and saved:
            return self._execution_exit_config({'exit': saved})
        execution = position.get('strategy_execution')
        return self._execution_exit_config(execution if isinstance(execution, dict) else {})

    def _execute_sell(self, stock_code: str, sell_price: float, date: str, reason: str, *, exit_time: str = '', shares_to_sell=None):
        """执行卖出"""
        if self._is_full_day_suspended(stock_code, date, scope='holding'):
            return
        if stock_code not in self.current_positions:
            return

        position = self.current_positions[stock_code]
        total_shares = position['shares']
        shares = min(int(shares_to_sell or total_shares), total_shares)
        if shares <= 0:
            return

        # 计算卖出金额（减去滑点）
        actual_sell_price = sell_price * (1 - self.config.slippage)
        if getattr(self.config, 'production_rules', False):
            from core.utils.price_limit import round_price
            actual_sell_price = round_price(actual_sell_price)
        sell_value = shares * actual_sell_price

        # 计算费用
        commission = sell_value * self.config.commission_rate
        stamp_duty = sell_value * self.config.stamp_duty_rate

        # 计算盈亏
        total_cost = position['cost_basis'] * shares / total_shares
        total_revenue = sell_value - commission - stamp_duty
        pnl = total_revenue - total_cost
        pnl_pct = pnl / total_cost if total_cost > 0 else 0

        # 更新现金
        self.cash += total_revenue

        # 记录交易
        holding_days = self._calculate_holding_days(position['entry_date'], date)

        trade_record = TradeRecord(
            date=date,
            stock_code=stock_code,
            stock_name=position['stock_name'],
            pattern_type=position['pattern_type'],
            action='SELL',
            entry_price=position['entry_price'],
            exit_price=actual_sell_price,
            shares=shares,
            position_size=total_cost,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            hot_resonance=position['hot_resonance'],
            resonance_sectors=position['resonance_sectors'],
            stop_loss_triggered=reason.startswith('stop_loss'),
            take_profit_triggered=(reason == 'trailing_stop'),
            entry_date=position.get('entry_date', ''),
            exit_reason=reason,
            plan_rank=int(position.get('plan_rank') or 0),
            plan_score=float(position.get('plan_score') or 0.0),
            plan_reason=str(position.get('plan_reason') or ''),
            factor_metrics_json=str(position.get('factor_metrics_json') or ''),
            factor_context_json=str(position.get('factor_context_json') or ''),
            open_gap_pct=float(position.get('open_gap_pct') or 0.0),
            market_score=float(position.get('market_score') or 0.0),
            amount_ratio=float(position.get('amount_ratio') or 0.0),
            entry_signal=str(position.get('entry_signal') or ''),
            entry_time=str(position.get('entry_time') or ''),
            exit_time=exit_time,
            mfe_pct=(float(position.get('max_favorable_price') or position['entry_price']) / position['entry_price'] - 1.0),
            mae_pct=(float(position.get('min_adverse_price') or position['entry_price']) / position['entry_price'] - 1.0),
            sizing_method=str(position.get('sizing_method') or ''),
            sizing_rationale=str(position.get('sizing_rationale') or ''),
            sizing_sample_size=int(position.get('sizing_sample_size') or 0),
            strategy_id=str(position.get('strategy_id') or 'default'),
            strategy_name=str(position.get('strategy_name') or ''),
            strategy_version=str(position.get('strategy_version') or ''),
            strategy_sources=str(position.get('strategy_sources') or ''),
            exit_rule_version=self._exit_rule_version(position),
            cost_version=self._cost_version(),
            result_available_at=datetime.now().isoformat(timespec="seconds"),
            exit_evidence_json=(json.dumps(position['board_feedback_exit'], ensure_ascii=False, sort_keys=True)
                                if position.get('board_feedback_exit') else ''),
        )

        self.trade_history.append(trade_record)

        logger.info(f"[{date}] 卖出 {position['stock_name']}({stock_code}): {shares}股 @ {actual_sell_price:.2f}, 盈亏:{pnl:.2f}({pnl_pct:.2%})")

        # 移除持仓
        if shares == total_shares:
            del self.current_positions[stock_code]
        else:
            position['shares'] -= shares
            position['cost_basis'] -= total_cost
            position['market_value'] = position['shares'] * sell_price

    def _cost_version(self) -> str:
        from core.operations.ledger import stable_id
        return stable_id("engine_cost_v1", self.config.commission_rate,
                         self.config.stamp_duty_rate, self.config.slippage)

    def _exit_rule_version(self, position: Dict[str, Any]) -> str:
        from core.operations.ledger import stable_id
        from core.signals.board_feedback import FEEDBACK_VERSION, feedback_policy
        policy = getattr(self.minute_evaluator, 'board_feedback_policy', feedback_policy(self.config))
        if not policy['enabled']:
            return stable_id("engine_exit_v1", self._position_exit_config(position),
                             self.config.exit_policy_mode, self.config.daily_ohlc_path_policy)
        return stable_id("engine_exit_v2", self._position_exit_config(position), FEEDBACK_VERSION,
                         policy,
                         self.config.exit_policy_mode, self.config.daily_ohlc_path_policy)

    def _get_stock_price(self, stock_code: str, date: str) -> Optional[float]:
        """获取股票当日价格"""
        bar = self._get_stock_daily_bar(stock_code, date)
        price = self._float((bar or {}).get('close'))
        return price if price > 0 else None

    def _get_stock_daily_bar(self, stock_code: str, date: str) -> Optional[Dict[str, float]]:
        """获取当日 OHLC；回测默认只允许命中预取缓存。"""
        # 标准化股票代码为6位数字
        normalized_code = str(stock_code).zfill(6)
        # 标准化股票代码（添加交易所后缀）
        standardized_code = self._standardize_stock_code(normalized_code)

        # 首先尝试从DataManager获取真实数据
        try:
            result = self.dm.get_stock_daily_data(standardized_code, date)
            if result and self._float(result.get('close')) > 0:
                return result
        except Exception as e:
            logger.debug(f"获取真实价格失败 {stock_code}({standardized_code}) {date}: {e}")

        # B-1：默认不再用随机价格兜底（回测失真之源）。仅当显式开启
        # use_simulated_prices 时才回退模拟价格，否则返回 None 由调用方跳过。
        if not self.config.use_simulated_prices:
            return None

        # 如果没有真实数据，使用模拟价格（基于前一日收盘价或持仓成本）
        if normalized_code in self.current_positions:
            position = self.current_positions[normalized_code]

            # 使用当前市值计算昨日收盘价
            prev_close = position['market_value'] / position['shares'] if position['shares'] > 0 else position['entry_price']

            # 使用日期作为种子，确保同一天价格一致
            date_seed = int(date)
            np.random.seed(date_seed + hash(stock_code) % 10000)

            # 模拟价格波动 (-8% 到 +12%)，增加波动性以测试各种卖出条件
            daily_change = np.random.uniform(-0.08, 0.12)
            simulated_price = prev_close * (1 + daily_change)

            # 确保价格不会低于0.1元
            simulated_price = max(simulated_price, 0.1)

            logger.debug(f"[{date}] {normalized_code} 使用模拟价格: {simulated_price:.2f} (基于昨收{prev_close:.2f}, 变动{daily_change:.2%})")
            return {"open": simulated_price, "high": simulated_price,
                    "low": simulated_price, "close": simulated_price}

        return None

    def _standardize_stock_code(self, stock_code) -> str:
        """
        标准化股票代码，添加交易所后缀
        例如: 000001 -> 000001.SZ, 600000 -> 600000.SH
        """
        from core.utils.stock_code_utils import StockCodeUtils
        return StockCodeUtils.standardize_code(stock_code)

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or pd.isna(value):
                return default
        except Exception:
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _int(cls, value: Any, default: int = 0) -> int:
        try:
            return int(cls._float(value, float(default)))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _factor_metrics_json(cls, plan: pd.Series) -> str:
        text = str(plan.get('因子指标') or '').strip()
        if text and text.lower() not in ('nan', 'none'):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            except Exception:
                pass
        metrics = {}
        for key, value in plan.items():
            key = str(key)
            if key.startswith('因子_'):
                metrics[key.replace('因子_', '', 1)] = cls._float(value)
        return json.dumps(metrics, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _factor_context_json(cls, plan: pd.Series) -> str:
        text = str(plan.get('原始指标') or '').strip()
        if text and text.lower() not in ('nan', 'none'):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            except Exception:
                pass
        context = {}
        for key, value in plan.items():
            key = str(key)
            if key.startswith('原始_'):
                context[key.replace('原始_', '', 1)] = cls._float(value)
        return json.dumps(context, ensure_ascii=False, sort_keys=True)

    def _get_prev_close(self, stock_code: str, date: str) -> Optional[float]:
        """获取昨日收盘价（B-1：真实交易日历取前一交易日）"""
        try:
            prev_date_str = self.calendar.prev(date)
            standardized_code = self._standardize_stock_code(stock_code)

            result = self.dm.get_stock_daily_data(standardized_code, prev_date_str)
            if result and 'close' in result:
                return float(result['close'])
        except Exception as e:
            logger.debug(f"获取昨日收盘价失败 {stock_code} {date}: {e}")
        return None

    def _calculate_holding_days(self, entry_date: str, exit_date: str) -> int:
        """计算持仓天数（B-1：按交易日计，时间止损更准确）"""
        return self.calendar.holding_days(entry_date, exit_date)

    def _calculate_daily_nav(self, date: str):
        """计算每日净值"""
        # Entry-day shares are T+1 locked, but still must be marked at today's close.
        for code, position in self.current_positions.items():
            if self._is_full_day_suspended(code, date, scope='holding'):
                continue
            bar = self._get_stock_daily_bar(code, date)
            close = self._float((bar or {}).get('close'))
            if close <= 0:
                if self.config.replay_missing_evidence_policy != 'legacy':
                    self._raise_replay_evidence([{'trade_date': date, 'code': code,
                        'scope': 'holding', 'evidence': 'daily_bar',
                        'reason': '缺少当日收盘价，不能计算完整净值', 'action': 'blocked'}])
                continue
            position['market_value'] = float(position['shares']) * close
            position['last_close'] = close
        position_value = sum(pos['market_value'] for pos in self.current_positions.values())
        total_value = self.cash + position_value

        self.daily_nav.append({
            'date': date,
            'cash': self.cash,
            'position_value': position_value,
            'total_value': total_value,
            'position_count': len(self.current_positions)
        })

        self.total_capital = total_value

    def _is_full_day_suspended(self, code: str, date: str, *, scope: str) -> bool:
        from core.data.data_manager_suspension import suspension_status
        key = (str(date), str(code).split('.')[0].zfill(6))
        if key not in self._suspension_statuses:
            self._suspension_statuses[key] = suspension_status(self.dm, key[1], key[0], allow_remote=False)
        status = self._suspension_statuses[key]
        if status.get('state') != 'full_day':
            return False
        self._suspended_sessions[(*key, scope)] = {
            **status, 'trade_date': key[0], 'code': key[1], 'scope': scope,
            'action': 'carry_forward' if scope == 'holding' else 'not_tradable',
            'valuation_basis': 'last_available_mark' if scope == 'holding' else '',
        }
        return True

    def _raise_replay_evidence(self, failures: List[Dict[str, Any]]) -> None:
        self._replay_blocked_evidence = failures
        raise ReplayEvidenceBlocked(failures, self.replay_data_quality())

    def replay_data_quality(self) -> Dict[str, Any]:
        skipped = list(self._candidate_evidence_skips.values())
        pairs = {(str(row['trade_date']), str(row['code'])) for row in skipped}
        uncovered = pairs | {(str(row['trade_date']), str(row['code'])) for row in self._replay_blocked_evidence
                             if row.get('scope') == 'candidate'}
        total = len(self._candidate_evidence_samples)
        incomplete = bool(skipped or self._replay_blocked_evidence)
        checked = self.config.replay_missing_evidence_policy != "legacy"
        from core.signals.production_rules import RULESET_VERSION
        return {"ruleset_version": RULESET_VERSION if self.config.production_rules else 'legacy_research',
            "execution_precision": 'historical_minute_estimate',
            "live_runtime_verified": False,
            "input_manifest": list(self._production_input_manifest),
            "policy": self.config.replay_missing_evidence_policy,
            "status": "not_checked" if self.config.replay_missing_evidence_policy == "legacy" else
                      "incomplete" if incomplete else "complete",
            "label": "未执行严格证据检查" if not checked else "数据不完整" if incomplete else "证据完整",
            "candidate_samples": total, "skipped_candidate_count": len(pairs),
            "candidate_loaded_count": self.entry_candidate_count,
            "coverage_denominator_scope": "入场门禁通过后实际检查的候选股票×交易日，不含门禁否决项",
            "covered_candidate_count": total - len(uncovered),
            "candidate_coverage_pct": round((total - len(uncovered)) / total * 100, 2) if total else None,
            "skipped_candidates": skipped, "blocked_evidence": self._replay_blocked_evidence,
            "suspended_sessions": list(self._suspended_sessions.values()),
            "optional_evidence_warnings": [{"trade_date": date, "code": code,
                "evidence": "minute_bars", "scope": "sector_peer", "required": False, "reason": reason}
                for (date, code), reason in self._sector_peer_failures.items()],
            "suspended_candidate_count": sum(row['scope'] == 'candidate' for row in self._suspended_sessions.values()),
            "full_sample_metrics_valid": checked and not incomplete,
            "metrics_scope": "未执行严格证据检查" if not checked else
                "仅剩余证据可用样本，不能视为完整样本的胜率、收益与回撤" if incomplete else "完整证据样本"}

    def _generate_backtest_report(self) -> Dict:
        """生成回测报告"""
        from backtest.run_audit import build_entry_funnel, build_entry_opportunity_summary

        entry_funnel = build_entry_funnel(
            self.entry_attempts,
            candidate_count=self.entry_candidate_count,
        )
        if not self.trade_history:
            entry_opportunities = build_entry_opportunity_summary(
                self.entry_attempts, candidate_count=self.entry_candidate_count,
            )
            nav_series = pd.DataFrame(self.daily_nav)
            max_drawdown = 0
            if not nav_series.empty and 'total_value' in nav_series.columns:
                nav_series['cummax'] = nav_series['total_value'].cummax().clip(lower=self.config.initial_capital)
                nav_series['drawdown'] = (nav_series['total_value'] - nav_series['cummax']) / nav_series['cummax']
                max_drawdown = nav_series['drawdown'].min()
            total_return = self.total_capital / self.config.initial_capital - 1 if self.config.initial_capital > 0 else 0.0
            from backtest.nav_metrics import daily_returns, sharpe_ratio as account_sharpe
            return {
                'total_return': total_return,
                'annualized_return': (1 + total_return) ** (252 / len(nav_series)) - 1 if len(nav_series) else 0,
                'sharpe_ratio': account_sharpe(daily_returns(nav_series['total_value'], self.config.initial_capital))
                    if not nav_series.empty else 0,
                'max_drawdown': max_drawdown,
                'win_rate': 0,
                'profit_loss_ratio': 0,
                'total_trades': 0,
                'buy_trades': 0,
                'closed_trades': 0,
                'initial_capital': self.config.initial_capital,
                'final_capital': self.total_capital,
                'pattern_stats': pd.DataFrame(),
                'resonance_stats': pd.DataFrame(),
                'daily_nav': self.daily_nav,
                'trade_history': self.trade_history,
                'current_positions': self.current_positions,
                'entry_attempts': self.entry_attempts,
                'entry_mode': self.config.entry_mode,
                'entry_candidate_count': self.entry_candidate_count,
                'entry_funnel': entry_funnel,
                'entry_opportunity_summary': entry_opportunities,
                'backtest_config': asdict(self.config),
                'exit_execution_audit': dict(self._exit_execution_audit),
                'exit_evidence_failures': list(self._exit_evidence_failures),
                'data_quality': self.replay_data_quality(),
                'as_of_date': str(self.daily_nav[-1].get('date') if self.daily_nav else ''),
            }

        # 计算收益率
        total_return = (self.total_capital - self.config.initial_capital) / self.config.initial_capital

        # 计算年化收益
        days = len(self.daily_nav)
        annualized_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0

        # 计算最大回撤
        nav_series = pd.DataFrame(self.daily_nav)
        nav_series['cummax'] = nav_series['total_value'].cummax().clip(lower=self.config.initial_capital)
        nav_series['drawdown'] = (nav_series['total_value'] - nav_series['cummax']) / nav_series['cummax']
        max_drawdown = nav_series['drawdown'].min()

        # 计算胜率
        trades_df = pd.DataFrame([{
            'action': t.action,
            'pnl': t.pnl,
            'pnl_pct': t.pnl_pct,
            'pattern_type': t.pattern_type,
            'hot_resonance': t.hot_resonance
        } for t in self.trade_history])
        closed_df = trades_df[trades_df['action'].astype(str).str.upper().str.startswith('SELL')].copy()
        buy_count = int((trades_df['action'].astype(str).str.upper() == 'BUY').sum())
        entry_opportunities = build_entry_opportunity_summary(
            self.entry_attempts,
            candidate_count=self.entry_candidate_count,
            executed_buys=buy_count,
            closed_trades=len(closed_df),
        )

        if closed_df.empty:
            win_rate = 0
            profit_loss_ratio = 0
            pattern_stats = pd.DataFrame()
            resonance_stats = pd.DataFrame()
        else:
            win_rate = (closed_df['pnl'] > 0).mean()

            # 计算盈亏比
            avg_profit = closed_df[closed_df['pnl'] > 0]['pnl'].mean() if len(closed_df[closed_df['pnl'] > 0]) > 0 else 0
            avg_loss = abs(closed_df[closed_df['pnl'] < 0]['pnl'].mean()) if len(closed_df[closed_df['pnl'] < 0]) > 0 else 1
            profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

            # 按模式统计
            pattern_stats = closed_df.groupby('pattern_type').agg({
                'pnl': ['count', 'sum', 'mean'],
                'pnl_pct': 'mean'
            }).round(4)

            # 热点共振 vs 非共振统计
            resonance_stats = closed_df.groupby('hot_resonance').agg({
                'pnl': ['count', 'sum', 'mean'],
                'pnl_pct': 'mean'
            }).round(4)

        # 计算Sharpe比率（简化）
        from backtest.nav_metrics import daily_returns, sharpe_ratio as account_sharpe
        sharpe_ratio = account_sharpe(daily_returns(nav_series['total_value'], self.config.initial_capital))

        report = {
            'total_return': total_return,
            'annualized_return': annualized_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'total_trades': len(closed_df),
            'buy_trades': buy_count,
            'closed_trades': len(closed_df),
            'initial_capital': self.config.initial_capital,
            'final_capital': self.total_capital,
            'pattern_stats': pattern_stats,
            'resonance_stats': resonance_stats,
            'daily_nav': self.daily_nav,
            'trade_history': self.trade_history,
            'current_positions': self.current_positions,
            'entry_attempts': self.entry_attempts,
            'entry_mode': self.config.entry_mode,
            'entry_candidate_count': self.entry_candidate_count,
            'entry_funnel': entry_funnel,
            'entry_opportunity_summary': entry_opportunities,
            'backtest_config': asdict(self.config),
            'exit_execution_audit': dict(self._exit_execution_audit),
            'exit_evidence_failures': list(self._exit_evidence_failures),
            'data_quality': self.replay_data_quality(),
            'as_of_date': str(self.daily_nav[-1].get('date') if self.daily_nav else ''),
        }

        return report
