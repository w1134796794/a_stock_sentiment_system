"""Production contracts shared by live simulation and historical replay."""
from __future__ import annotations

import math
from copy import deepcopy

RULESET_VERSION = "production_shared_v4_opening"


def number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def minute_evaluator(risk=None):
    from backtest.minute_entry import MinuteEntryEvaluator
    from risk.risk_config import RiskConfig
    risk = risk or RiskConfig.load()
    return MinuteEntryEvaluator(
        deadline=risk.entry_confirm_deadline,
        weak_min_gap=risk.weak_entry_min_gap, weak_max_gap=risk.weak_entry_max_gap,
        continuation_max_gap=risk.continuation_max_gap,
        min_amount_pace=risk.entry_min_amount_pace, max_amount_pace=risk.entry_max_amount_pace,
        min_auction_volume_ratio=risk.continuation_min_auction_volume_ratio,
        min_auction_amount=risk.continuation_min_auction_amount,
    )


def entry_parameters(evaluator):
    from dataclasses import asdict, is_dataclass
    values = {key: getattr(evaluator, key) for key in (
        'deadline', 'weak_min_gap', 'weak_max_gap', 'continuation_max_gap', 'min_amount_pace',
        'max_amount_pace', 'min_auction_volume_ratio', 'min_auction_amount') if hasattr(evaluator, key)}
    policy = getattr(evaluator, 'confirmation_policy', None)
    if is_dataclass(policy):
        values['confirmation_policy'] = asdict(policy)
    return values


def threshold_labels(evaluator=None):
    evaluator = evaluator or minute_evaluator()
    return {'weak_to_strong': f'{evaluator.weak_min_gap * 100:g}%至{evaluator.weak_max_gap * 100:+g}%',
            'continuation': f'{evaluator.weak_max_gap * 100:+g}%至{evaluator.continuation_max_gap * 100:+g}%',
            'acceleration': f'{evaluator.continuation_max_gap * 100:+g}%以上仅龙头/主线核心'}


def execution_defaults(execution, risk=None):
    from risk.risk_config import RiskConfig
    from backtest.exit_policy import normalize_exit_parameters
    risk = risk or RiskConfig.load()
    execution = deepcopy(execution or {})
    defaults = {key: getattr(risk, key) for key in (
        'hard_stop_loss', 'trailing_activation', 'trailing_early_stop', 'trailing_mid_profit',
        'trailing_mid_stop', 'trailing_high_profit', 'trailing_stop', 'time_stop_days',
        'time_stop_profit_threshold')}
    execution['exit'] = normalize_exit_parameters({**defaults, **(execution.get('exit') or {})})
    return execution


def production_candidates(inputs):
    from core.realtime.models import normalize_stock_code
    from core.utils.stock_eligibility import new_entry_exclusion
    leaders = ((inputs.get("leader_pools") or {}).get("10", {}).get("rows") or [])[:30]
    leader_codes = {normalize_stock_code(r.get('code'), add_suffix=False) for r in leaders}
    candidates = (inputs.get("candidates") or [])[:20]
    from risk.risk_config import RiskConfig
    risk = RiskConfig.from_dict(inputs['risk_config']) if inputs.get('risk_config') else RiskConfig.load()
    profiles = {p['id']: p for p in inputs.get('profiles', []) if p.get('id')}
    rows = {}
    excluded = set()
    for source in [*leaders, *candidates]:
        code = normalize_stock_code(source.get("code") or source.get("stock_code"), add_suffix=False)
        if not code:
            continue
        if new_entry_exclusion(source):
            excluded.add(code)
            rows.pop(code, None)
            continue
        if code in excluded:
            continue
        previous = rows.get(code, {})
        item = {**previous, **deepcopy(source), "code": code}
        item["metrics"] = {**(previous.get("metrics") or {}), **(source.get("metrics") or {})}
        item.setdefault("strategy_id", "leader_pool")
        item.setdefault("strategy_name", "近期龙头池")
        item['is_leader_observation'] = code in leader_codes
        item.setdefault("strategy_execution", {})
        profile = profiles.get(item['strategy_id']) or {}
        if profile.get('enabled') is False:
            rows.pop(code, None)
            continue
        if profile:
            item['strategy_execution'] = deepcopy(profile.get('execution') or {})
            item['strategy_version'] = str(profile.get('version') or '')
        item['strategy_execution'] = execution_defaults(item['strategy_execution'], risk)
        item['screening_score'] = (item.get('screening_score') or item.get('score')
                                  or item.get('策略组合评分') or item.get('leader_score') or 0)
        rows[code] = item
    return list(rows.values())


def production_context(rows, inputs):
    from core.realtime.models import normalize_stock_code
    canonical = {row['code']: row for row in production_candidates(inputs)}
    return [{**deepcopy(row), **deepcopy(canonical.get(normalize_stock_code(row.get('code'), add_suffix=False), {})),
             'execution_scope': 'production_candidate' if normalize_stock_code(row.get('code'), add_suffix=False) in canonical
                                else 'observation_only'} for row in rows]


def candidate_strength(row):
    base = next((number(row.get(key)) * (100 if 0 < number(row.get(key)) <= 1 else 1)
                 for key in ("turn_score", "screening_score", "leader_score", "score",
                             "success_probability", "confidence_score") if number(row.get(key)) > 0), 50.0)
    mode_bonus = {"weak_to_strong": 4.0, "continuation": 3.0,
                  "acceleration": 2.0, "high_open_acceleration": 2.0}.get(row.get("entry_mode"), 0.0)
    group_bonus = 4.0 if row.get("action_group") == "重点确认" else 0.0
    breadth = number((row.get("sector_detail") or {}).get("breadth"))
    breadth = breadth * 100 if 0 < breadth <= 1 else breadth
    sector_bonus = max(min((breadth - 50) / 20, 3), -3) if breadth else 0
    return round(max(0, min(100, base + mode_bonus + group_bonus + sector_bonus)), 2)


def holding_strength(position):
    base = number((position.get("metadata") or {}).get("entry_strength_score"), 50)
    entry = number(position.get("entry_price"))
    price = number(position.get("last_price"), entry)
    momentum = max(min((price / entry - 1) * 80, 12), -12) if entry > 0 else 0
    adjustment = {"hold": 0, "watch": -5, "reduce": -12, "sell": -25,
                  "blocked": -20, "data_insufficient": -3}.get(position.get("latest_action", "hold"), 0)
    return round(max(0, min(100, base + momentum + adjustment)), 2)
