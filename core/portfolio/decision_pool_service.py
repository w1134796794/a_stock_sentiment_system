"""Turn multi-strategy screening output into a compact daily decision pool."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from core.factors.sector_taxonomy import partition_sector_names, theme_cluster_name
from core.models.market_state import (
    EMOTION_PHASE_LABELS,
    EMOTION_PHASES,
    PHASE_ALLOWED_STRATEGIES,
    MarketStateSnapshot,
)
from core.portfolio.strategy_allocator import StrategyPortfolioAllocator
from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS

PRODUCTION_STRATEGIES = PRODUCTION_STRATEGY_IDS
REGIME_STRATEGIES = {
    "strong": PRODUCTION_STRATEGIES,
    "neutral": ("mainline_leader", "first_board_launch", "weak_to_strong", "limit_pullback", "limit_reversal"),
    "weak": ("mainline_leader", "weak_to_strong", "limit_reversal"),
}
REGIME_LABELS = {"strong": "强市", "neutral": "震荡市", "weak": "弱市"}
RISK_FLAG_LABELS = {
    "cycle_overheated": "强周期持续过久，接近退潮窗口",
    "market_emotion_divergence": "大盘与短线情绪明显背离",
    "echelon_broken": "连板梯队断层",
    "prev_limit_premium_weak": "昨日涨停今日溢价偏弱",
    "prev_limit_positive_weak": "昨日涨停今日收红率偏低",
    "first_board_no_premium": "昨日首板今日高开率不足",
}
ENTRY_MODE_LABELS = {
    "limit_pullback": "涨停回踩转强确认",
    "limit_reversal": "跌停反包确认",
    "weak_to_strong": "弱转强确认",
    "continuation": "强势延续确认",
    "acceleration": "高开加速确认",
}
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}
MAINLINE_CONFIRM_THRESHOLD = 55.0
CROWDING_POLICY = {
    "strong": {"focus_per_cluster": 2, "active_per_cluster": 3},
    "neutral": {"focus_per_cluster": 1, "active_per_cluster": 2},
    "weak": {"focus_per_cluster": 1, "active_per_cluster": 2},
}
EVIDENCE_FACTORS = (
    ("stk_sector_mainline_score", "主线地位", 55.0),
    ("stk_sector_resonance_score", "板块共振", 55.0),
    ("stk_sector_persistence_score", "主线持续", 55.0),
    ("stk_capital_flow_consensus", "资金共振", 55.0),
    ("stk_lhb_sector_resonance", "龙虎榜板块共振", 55.0),
    ("stk_lhb_net_buy_score", "龙虎榜净买认可", 55.0),
    ("stk_lhb_institution_score", "机构席位认可", 55.0),
    ("stk_behavior_repair", "量价修复", 55.0),
    ("stk_intraday_seal_quality", "封板质量", 55.0),
    ("stk_relative_strength_sector", "强于所属板块", 55.0),
)
FACTOR_ALIASES = {
    "stk_sector_mainline_score": ("sector_mainline_score",),
    "stk_sector_resonance_score": ("sector_resonance_score",),
    "stk_sector_persistence_score": ("sector_persistence_score",),
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _code(row: Mapping[str, Any]) -> str:
    value = str(row.get("code") or row.get("股票代码") or row.get("代码") or "").split(".", 1)[0]
    return value.zfill(6) if value else ""


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def _metric(row: Mapping[str, Any], factor: str) -> float:
    _, value = _metric_observation(row, factor)
    return value


def _metric_observation(row: Mapping[str, Any], factor: str) -> tuple[bool, float]:
    keys = (factor, *FACTOR_ALIASES.get(factor, ()))
    containers = (row, row.get("metrics") or {}, row.get("context") or {})
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            if key in container and container.get(key) not in (None, ""):
                return True, _number(container.get(key))
    return False, 0.0


class DecisionPoolService:
    """Select applicable strategies, merge overlap, and assign an action group."""

    def __init__(self, allocator: StrategyPortfolioAllocator | None = None) -> None:
        self.allocator = allocator or StrategyPortfolioAllocator()

    @staticmethod
    def market_regime(market_score: float) -> str:
        from core.models.market_state import classify_market_score

        return classify_market_score(market_score)

    def build(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
        profiles: Mapping[str, Mapping[str, Any]],
        *,
        market_score: float = 50.0,
        market_regime: str = "",
        market_state: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        state_payload = dict(market_state or {})
        state_context = dict(state_payload.get("context") or {})
        state_context.update(state_payload)
        snapshot = MarketStateSnapshot.resolve(
            market_score,
            trade_date=str(state_payload.get("trade_date") or ""),
            context=state_context,
        )
        supplied_regime = str(market_regime or state_payload.get("regime") or "")
        regime = snapshot.regime
        phase = str(state_payload.get("phase") or snapshot.phase)
        if phase not in EMOTION_PHASES:
            phase = snapshot.phase
        phase_label = EMOTION_PHASE_LABELS[phase]
        position_scale = _number(
            state_payload.get("position_scale"),
            snapshot.position_scale,
        )
        risk_flags = list(state_payload.get("risk_flags") or snapshot.risk_flags)
        phase_reasons = list(state_payload.get("phase_reasons") or snapshot.phase_reasons)
        allowed_strategies = set(PHASE_ALLOWED_STRATEGIES[phase]).intersection(
            REGIME_STRATEGIES[regime]
        )
        available = [
            strategy_id for strategy_id in PRODUCTION_STRATEGIES
            if strategy_id in payloads and strategy_id in profiles
        ]
        applicable = [
            strategy_id for strategy_id in available
            if strategy_id in allowed_strategies
        ]
        hidden = [strategy_id for strategy_id in available if strategy_id not in applicable]

        raw_rows: List[Dict[str, Any]] = []
        members_by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for strategy_id in applicable:
            payload = payloads.get(strategy_id) or {}
            profile = profiles.get(strategy_id) or {}
            strategy_name = str(payload.get("strategy_name") or profile.get("name") or strategy_id)
            execution = dict(profile.get("execution") or {})
            for source in payload.get("final") or []:
                if not isinstance(source, Mapping) or not _code(source):
                    continue
                row = dict(source)
                row.update({
                    "策略ID": strategy_id,
                    "策略名称": strategy_name,
                    "策略单票仓位上限%": _number(profile.get("position_cap_pct")),
                    "_entry_modes": list(execution.get("allowed_entry_modes") or []),
                    "_strategy_execution": execution,
                    "_evidence_rules": list(profile.get("evidence_rules") or []),
                    "_veto_rules": list(profile.get("veto_rules") or []),
                })
                raw_rows.append(row)
                members_by_code[_code(row)].append(row)

        merged = self.allocator.merge(raw_rows)
        total = len(PRODUCTION_STRATEGIES)
        for row in merged:
            members = members_by_code.get(_code(row), [])
            self._decorate(
                row,
                members,
                total,
                regime,
                phase=phase,
                phase_label=phase_label,
                position_scale=position_scale,
                risk_flags=risk_flags,
            )

        # A strategy being disabled by the current market phase is an execution
        # decision, not evidence that its screened candidates never existed.
        # Keep those rows visible for review while making them strictly
        # ineligible for live confirmation, notification and backtesting.
        visible_codes = {_code(row) for row in merged}
        hidden_raw_rows: List[Dict[str, Any]] = []
        hidden_members_by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for strategy_id in hidden:
            payload = payloads.get(strategy_id) or {}
            profile = profiles.get(strategy_id) or {}
            strategy_name = str(payload.get("strategy_name") or profile.get("name") or strategy_id)
            execution = dict(profile.get("execution") or {})
            for source in payload.get("final") or []:
                if not isinstance(source, Mapping) or not _code(source):
                    continue
                row = dict(source)
                row.update({
                    "策略ID": strategy_id,
                    "策略名称": strategy_name,
                    "策略单票仓位上限%": _number(profile.get("position_cap_pct")),
                    "_entry_modes": list(execution.get("allowed_entry_modes") or []),
                    "_strategy_execution": execution,
                    "_evidence_rules": list(profile.get("evidence_rules") or []),
                    "_veto_rules": list(profile.get("veto_rules") or []),
                })
                hidden_raw_rows.append(row)
                hidden_members_by_code[_code(row)].append(row)

        hidden_rows: List[Dict[str, Any]] = []
        for row in self.allocator.merge(hidden_raw_rows):
            code = _code(row)
            if code in visible_codes:
                continue
            members = hidden_members_by_code.get(code, [])
            self._decorate(
                row,
                members,
                total,
                regime,
                phase=phase,
                phase_label=phase_label,
                position_scale=position_scale,
                risk_flags=risk_flags,
            )
            strategy_names = _unique(str(item.get("策略名称") or "") for item in members)
            phase_detail = "；".join(str(reason) for reason in phase_reasons if reason)
            gate_reason = (
                f"当前为{REGIME_LABELS[regime]} / {phase_label}，"
                f"暂不启用{'、'.join(strategy_names) or '该策略'}"
                + (f"（判定依据：{phase_detail}）" if phase_detail else "")
            )
            row["_blocked"] = True
            row["_blocked_reasons"] = _unique([
                *list(row.get("_blocked_reasons") or []),
                gate_reason,
            ])
            row["市场门控说明"] = gate_reason
            row["失效条件"] = gate_reason
            self._set_group(row, "inactive", regime, phase)
            hidden_rows.append(row)

        merged.extend(hidden_rows)

        crowding_summary = self._annotate_crowding(merged, regime)

        actionable = [row for row in merged if not row["_blocked"]]
        inactive = [row for row in merged if row["_blocked"]]
        focus: List[Dict[str, Any]] = []
        watch: List[Dict[str, Any]] = []
        focus_clusters: Counter[str] = Counter()
        active_clusters: Counter[str] = Counter()
        crowding_policy = CROWDING_POLICY[regime]
        for row in actionable:
            grade = str(row.get("规则等级") or "D")
            cluster = str(row.get("主题簇") or "")
            focus_allowed = bool(
                not cluster
                or focus_clusters[cluster] < crowding_policy["focus_per_cluster"]
            )
            active_allowed = bool(
                not cluster
                or active_clusters[cluster] < crowding_policy["active_per_cluster"]
            )
            if len(focus) < 3 and grade in {"A", "B"} and focus_allowed and active_allowed:
                self._set_group(row, "focus", regime, phase)
                focus.append(row)
                if cluster:
                    focus_clusters[cluster] += 1
                    active_clusters[cluster] += 1
            elif (
                len(watch) < 5
                and len(focus) + len(watch) < 8
                and grade in {"A", "B", "C"}
                and active_allowed
            ):
                self._set_group(row, "watch", regime, phase)
                watch.append(row)
                if cluster:
                    active_clusters[cluster] += 1
            else:
                structural_watch = bool(set(row.get("allowed_entry_modes") or []).intersection({"limit_pullback", "limit_reversal"}))
                if structural_watch and active_allowed:
                    self._set_group(row, "watch", regime, phase)
                    row["观察池说明"] = "结构策略独立观察，等待盘中确认"
                    watch.append(row)
                    if cluster:
                        active_clusters[cluster] += 1
                    continue
                crowding_reason = ""
                if cluster and not active_allowed:
                    row["拥挤降级"] = True
                    crowding_reason = (
                        f"{cluster}候选过度集中，今日决策池最多保留"
                        f"{crowding_policy['active_per_cluster']}只，当前标的降为研判参考"
                    )
                    row["拥挤说明"] = crowding_reason
                self._set_group(row, "inactive", regime, phase)
                if crowding_reason:
                    row["失效条件"] = crowding_reason
                inactive.append(row)

        for row in inactive:
            if row.get("行动分组") != "暂不参与":
                self._set_group(row, "inactive", regime, phase)
            self._finalize_exclusion_reasons(row)

        active_names = [str((profiles.get(key) or {}).get("name") or key) for key in applicable]
        hidden_names = [str((profiles.get(key) or {}).get("name") or key) for key in hidden]
        return {
            "schema_version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "regime": regime,
            "regime_label": REGIME_LABELS[regime],
            "supplied_regime": supplied_regime,
            "regime_corrected": bool(
                supplied_regime in REGIME_STRATEGIES and supplied_regime != regime
            ),
            "market_score": round(market_score, 1),
            "emotion_phase": phase,
            "emotion_phase_label": phase_label,
            "emotion_phase_reasons": phase_reasons,
            "market_position_scale": round(position_scale, 4),
            "market_total_position_cap_pct": round(position_scale * 100.0, 2),
            "market_risk_flags": risk_flags,
            "market_risk_labels": [
                RISK_FLAG_LABELS.get(flag, flag) for flag in risk_flags
            ],
            "active_strategy_ids": applicable,
            "active_strategy_names": active_names,
            "hidden_strategy_names": hidden_names,
            "raw_candidate_count": len(merged),
            "hidden_candidate_count": len(hidden_rows),
            "inactive_count": len(inactive),
            "crowding_summary": crowding_summary,
            "cluster_limits": crowding_policy,
            "rows": focus + watch + inactive,
            "groups": [
                {"key": "focus", "name": "重点确认", "meaning": "多策略共识，盘中满足条件可交易", "rows": focus},
                {"key": "watch", "name": "盘中观察", "meaning": "具备优势，等待弱转强或板块确认", "rows": watch},
                {"key": "inactive", "name": "暂不参与", "meaning": "规则不适用、数据不足或风险否决", "rows": inactive},
            ],
            "decision_count": len(focus) + len(watch),
        }

    @staticmethod
    def _annotate_crowding(rows: Sequence[Dict[str, Any]], regime: str) -> List[Dict[str, Any]]:
        active_rows = [row for row in rows if not row.get("_blocked")]
        for row in rows:
            labels = list(row.get("共振板块") or row.get("相关题材") or [])
            mainline = str(row.get("所属主线") or "")
            if mainline and mainline != "主线待确认":
                labels.insert(0, mainline)
            row["主题簇"] = theme_cluster_name(labels)

        counts = Counter(str(row.get("主题簇") or "") for row in active_rows)
        counts.pop("", None)
        total = max(len(active_rows), 1)
        summary: List[Dict[str, Any]] = []
        for cluster, count in counts.most_common():
            ratio = count / total
            level = (
                "严重拥挤" if count >= 4 and ratio >= 0.5
                else "拥挤" if count >= 3 and ratio >= 0.35
                else "正常"
            )
            summary.append({
                "cluster": cluster,
                "count": count,
                "ratio_pct": round(ratio * 100.0, 1),
                "level": level,
            })

        summary_map = {item["cluster"]: item for item in summary}
        for row in rows:
            item = summary_map.get(str(row.get("主题簇") or ""), {})
            row["主题候选数"] = int(item.get("count") or 0)
            row["主题占比%"] = _number(item.get("ratio_pct"))
            row["拥挤等级"] = str(item.get("level") or "未识别")
            row["拥挤降级"] = False
            if row["拥挤等级"] in {"拥挤", "严重拥挤"}:
                row["拥挤说明"] = (
                    f"{row['主题簇']}占可用候选{row['主题占比%']:.1f}%"
                    f"（{row['主题候选数']}只），{regime}市场按主题限额保留"
                )
            else:
                row["拥挤说明"] = "主题集中度正常"
        return summary

    @staticmethod
    def persist(payload: Mapping[str, Any], output_dir: Path, trade_date: str) -> Path:
        """Persist the sole production decision artifact consumed by live and backtest."""
        path = Path(output_dir) / "decision_pool" / f"decision_pool_{trade_date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload or {})
        body["trade_date"] = str(trade_date)
        path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def _decorate(
        self,
        row: Dict[str, Any],
        members: Sequence[Mapping[str, Any]],
        strategy_total: int,
        regime: str,
        *,
        phase: str,
        phase_label: str,
        position_scale: float,
        risk_flags: Sequence[str],
    ) -> None:
        names = _unique(str(item.get("策略名称") or item.get("策略ID") or "") for item in members)
        sector_labels = _unique(
            str(sector).strip()
            for item in members
            for sector in str(item.get("resonance_sectors") or "").replace("，", ",").split(",")
        )
        sectors, attribute_labels = partition_sector_names(sector_labels)
        contexts = [item.get("context") or {} for item in members]
        strategy_ids = _unique(str(item.get("策略ID") or "") for item in members)
        sector_scores = [
            max(_number(ctx.get("sector_mainline_score")), _number(ctx.get("sector_resonance_score")))
            for ctx in contexts
        ]
        sector_strength = max(sector_scores or [0.0]) if sectors else 0.0
        mainline_strategy_hit = "mainline_leader" in strategy_ids
        mainline_confirmed = bool(
            mainline_strategy_hit
            and sectors
            and sector_strength >= MAINLINE_CONFIRM_THRESHOLD
        )
        mainline_name = sectors[0] if mainline_confirmed else "主线待确认"
        raw_modes = _unique(
            str(mode) for item in members for mode in (item.get("_entry_modes") or [])
        )
        modes = [ENTRY_MODE_LABELS.get(mode, mode) for mode in raw_modes]
        primary_strategy_id = str(
            row.get("策略ID") or (strategy_ids[0] if strategy_ids else "")
        )
        primary_member = next(
            (item for item in members if str(item.get("策略ID") or "") == primary_strategy_id),
            members[0] if members else {},
        )
        primary_execution = dict(primary_member.get("_strategy_execution") or {})
        combined_execution = dict(row.get("strategy_execution") or {})
        combined_execution.update({
            "mode_deadlines": {
                mode: (item.get("_strategy_execution") or {}).get("confirmation_deadline", "10:00:00")
                for item in members for mode in item.get("_entry_modes") or []
            },
            "allowed_entry_modes": raw_modes,
            "source_strategies": strategy_ids,
            "primary_strategy": primary_strategy_id,
        })
        if isinstance(primary_execution.get("exit"), Mapping):
            combined_execution["exit"] = dict(primary_execution["exit"])
        # 合并决策池由组合层统一限制持仓数量，不能沿用某个主策略的单策略上限。
        combined_execution.pop("max_positions", None)
        evidence = self._evidence(members)
        evidence_gaps = self._failed_evidence_rules(members)
        penalty_reasons = _unique(
            str(reason).strip()
            for item in members
            for reason in (item.get("penalty_reasons") or [])
        )
        rule_grade = self._rule_grade(
            members, evidence, sector_strength, _number(row.get("策略组合评分")),
        )
        blocked_reasons: List[str] = []
        data_completeness = min(
            (_number(item.get("data_completeness")) for item in members),
            default=100.0,
        )
        if members and 0 < data_completeness < 80.0:
            blocked_reasons.append("关键数据不足")
        if rule_grade == "D":
            blocked_reasons.append("规则优势或增强证据不足")
        veto_conditions = self._veto_conditions(members)

        row.update({
            "命中策略": names,
            "策略总数": strategy_total,
            "策略共识显示": f"{len(names)}/{strategy_total}",
            "所属主线": mainline_name,
            "主线确认": mainline_confirmed,
            "主线策略命中": mainline_strategy_hit,
            "共振板块": sectors[:4],
            "相关题材": sectors[:4],
            "证券属性标签": attribute_labels[:4],
            "板块强度": round(sector_strength, 1),
            "板块强度说明": (
                "强" if sector_strength >= 70
                else "中" if mainline_confirmed
                else "待确认"
            ),
            "明日入场模式": " / ".join(modes) if modes else "等待分钟行情分类",
            "allowed_entry_modes": raw_modes,
            "strategy_execution": combined_execution,
            "策略来源": ",".join(strategy_ids),
            "策略模式": "规则策略",
            "规则等级": rule_grade,
            "增强证据": evidence[:5],
            "增强证据缺口": evidence_gaps[:6],
            "规则扣分项": penalty_reasons[:6],
            "排除理由": [],
            "否决条件": veto_conditions[:4],
            "数据完整度%": round(data_completeness, 1),
            "情绪阶段": phase_label,
            "情绪阶段代码": phase,
            "市场总仓位上限%": round(position_scale * 100.0, 2),
            "市场风控": [RISK_FLAG_LABELS.get(flag, flag) for flag in risk_flags],
            "_blocked": bool(blocked_reasons),
            "_blocked_reasons": blocked_reasons,
        })
        mainline_summary = (
            f"主线题材为{row['所属主线']}"
            if mainline_confirmed
            else "主线题材待确认"
        )
        row["一句话结论"] = (
            f"{row.get('name') or row.get('股票名称') or row.get('代码')}命中{len(names)}个当前适用策略，"
            f"{mainline_summary}，"
            f"{'、'.join(evidence[:2]) if evidence else '资金与量价证据待确认'}；"
            + ("当前证据不足，先不参与。" if blocked_reasons else "次日只在分钟条件确认后参与。")
        )
        row["失效条件"] = (
            "；".join(blocked_reasons)
            if blocked_reasons
            else "；".join(veto_conditions[:2] or ["板块转弱", "跌破开盘低点或10:00前未确认"])
        )
        structural_reasons = []
        for mode, structure in (combined_execution.get("structures") or {}).items():
            if mode not in raw_modes:
                continue
            anchor = _number(structure.get("support") or structure.get("target"))
            structural_reasons.append(
                f"{ENTRY_MODE_LABELS.get(mode, mode)}：事件日{structure.get('event_date', '')}，"
                f"关键价{anchor:.2f}，结构保护{_number(structure.get('protection')):.2f}"
            )
        if structural_reasons:
            row["rule_reasons"] = _unique([*structural_reasons, *list(row.get("rule_reasons") or [])])
            row["次日确认条件"] = "；".join(
                f"{(combined_execution.get('mode_deadlines') or {}).get(mode, '14:30:00')[:5]}前"
                + ("回踩支撑、承接后放量突破" if mode == "limit_pullback" else "站稳反包目标后放量突破")
                for mode in raw_modes if mode in {"limit_pullback", "limit_reversal"}
            )

    @staticmethod
    def _evidence(members: Sequence[Mapping[str, Any]]) -> List[str]:
        evidence: List[str] = []
        for factor, label, threshold in EVIDENCE_FACTORS:
            value = max((_metric(item, factor) for item in members), default=0.0)
            if value >= threshold:
                evidence.append(label)
        for item in members:
            for rule in item.get("_evidence_rules") or []:
                if DecisionPoolService._rule_matches(item, rule):
                    evidence.append(str(rule.get("name") or rule.get("factor") or "增强证据"))
        return _unique(evidence)

    @staticmethod
    def _veto_conditions(members: Sequence[Mapping[str, Any]]) -> List[str]:
        labels = []
        for item in members:
            for rule in item.get("_veto_rules") or []:
                labels.append(str(rule.get("reason") or rule.get("name") or rule.get("factor") or "风险否决"))
        return _unique(labels)

    @staticmethod
    def _failed_evidence_rules(members: Sequence[Mapping[str, Any]]) -> List[str]:
        checks: Dict[tuple[str, str, str, str], Dict[str, Any]] = {}
        for item in members:
            for rule in item.get("_evidence_rules") or []:
                factor = str(rule.get("factor") or "")
                if not factor:
                    continue
                name = str(rule.get("name") or factor)
                signature = (factor, str(rule.get("op") or ">="), repr(rule.get("value")), name)
                state = checks.setdefault(signature, {"rule": rule, "observed": False, "values": [], "passed": False})
                observed, actual = _metric_observation(item, factor)
                state["observed"] = bool(state["observed"] or observed)
                if observed:
                    state["values"].append(actual)
                if DecisionPoolService._rule_matches(item, rule):
                    state["passed"] = True

        failures: List[str] = []
        for state in checks.values():
            if state["passed"]:
                continue
            rule = state["rule"]
            name = str(rule.get("name") or rule.get("factor") or "增强条件")
            factor = str(rule.get("factor") or "")
            if not state["observed"]:
                failures.append(f"{name}未通过：缺少{factor}数据")
                continue
            actual = max(state["values"], default=0.0)
            failures.append(
                f"{name}未达标（实际{actual:.1f}，要求{DecisionPoolService._rule_requirement(rule)}）"
            )
        return _unique(failures)

    @staticmethod
    def _rule_requirement(rule: Mapping[str, Any]) -> str:
        operator = str(rule.get("op") or ">=")
        value = rule.get("value")
        labels = {">=": "≥", ">": ">", "<=": "≤", "<": "<", "==": "=", "!=": "≠"}
        if operator == "between":
            values = list(value or [])
            if len(values) >= 2:
                return f"{values[0]}至{values[1]}"
        return f"{labels.get(operator, operator)}{value}"

    @staticmethod
    def _finalize_exclusion_reasons(row: Dict[str, Any]) -> None:
        if str(row.get("行动分组") or "") != "暂不参与":
            row["排除理由"] = []
            return
        reasons = _unique([
            *[str(item).strip() for item in row.get("_blocked_reasons") or []],
            *[str(item).strip() for item in row.get("规则扣分项") or []],
            *[str(item).strip() for item in row.get("增强证据缺口") or []],
            str(row.get("失效条件") or "").strip(),
        ])
        row["排除理由"] = reasons[:8]

    @staticmethod
    def _rule_matches(item: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
        actual = _metric(item, str(rule.get("factor") or ""))
        expected = rule.get("value")
        op = str(rule.get("op") or ">=")
        try:
            if op == ">=":
                return actual >= float(expected)
            if op == ">":
                return actual > float(expected)
            if op == "<=":
                return actual <= float(expected)
            if op == "<":
                return actual < float(expected)
            if op == "between":
                low, high = list(expected or [])[:2]
                return float(low) <= actual <= float(high)
            if op == "==":
                return actual == float(expected)
            if op == "!=":
                return actual != float(expected)
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _rule_grade(
        members: Sequence[Mapping[str, Any]], evidence: Sequence[str], sector_strength: float,
        combination_score: float,
    ) -> str:
        score = max(
            (_number(item.get("score"), _number(item.get("综合评分"))) for item in members),
            default=0.0,
        )
        consensus = len(_unique(str(item.get("策略ID") or "") for item in members))
        if score >= 84 and len(evidence) >= 3 and sector_strength >= 65:
            return "A"
        if (
            score >= 72 and len(evidence) >= 2 and sector_strength >= 50
        ) or (
            consensus >= 2 and len(evidence) >= 1 and combination_score >= 55
        ):
            return "B"
        if (score >= 60 and len(evidence) >= 1) or combination_score >= 50:
            return "C"
        return "D"

    @staticmethod
    def _set_group(
        row: Dict[str, Any],
        group: str,
        regime: str,
        phase: str,
    ) -> None:
        if group == "inactive":
            row["行动分组"] = "暂不参与"
            row["execution_eligible"] = False
            row["执行仓位上限%"] = 0.0
            row["建议仓位"] = "0%"
            if not row.get("_blocked_reasons"):
                row["失效条件"] = "优先级未进入今日8只决策池"
            return
        cap = _number(row.get("position_budget_pct"), _number(row.get("策略单票仓位上限%"), 10.0))
        if cap <= 0:
            cap = 10.0
        if phase in {"freeze", "decline"} or regime == "weak":
            cap = min(cap, 8.0)
        elif group == "watch":
            cap = min(cap, 10.0)
        row["行动分组"] = "重点确认" if group == "focus" else "盘中观察"
        row["execution_eligible"] = True
        row["执行仓位上限%"] = round(cap, 2)
        row["建议仓位"] = f"确认后参考 {cap:.0f}%"


__all__ = ["DecisionPoolService", "PRODUCTION_STRATEGIES", "REGIME_STRATEGIES"]
