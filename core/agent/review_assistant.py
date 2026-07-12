"""Plain-language review assistant built only from persisted system evidence."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.agent.evidence_service import AgentEvidenceService
from risk.capital_presets import CapitalPreset, resolve_capital_preset


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _signed(value: Any) -> str:
    return f"{_number(value):+.2f}%"


class ReviewAssistantService:
    """Turn research fields into auditable, novice-friendly actions."""

    REGIME_LABELS = {"strong": "强势", "neutral": "震荡", "weak": "弱势"}
    GRADE_LABELS = {
        "A": "证据较强",
        "B": "有一定优势",
        "C": "只适合观察",
        "D": "证据不足",
    }

    def __init__(self, evidence_service: Optional[AgentEvidenceService] = None) -> None:
        self.evidence = evidence_service or AgentEvidenceService()

    def build_brief(self, trade_date: str, capital: float = 100_000.0) -> Dict[str, Any]:
        date = str(trade_date or "")
        preset = resolve_capital_preset(capital)
        payload = self.evidence._screening(date)
        rows = list(payload.get("final") or [])
        metadata = payload.get("weight_metadata") or {}
        regime = str(metadata.get("market_regime") or "neutral")
        cards = [self._candidate_card(row, preset, market_regime=regime) for row in rows[:10]]
        actionable = [row for row in cards if row["action"] in {"重点观察", "等待确认"}]
        degraded = any(str(row.get("model_drift_status") or "") == "degraded" for row in rows)
        fallback = any(str(row.get("model_drift_status") or "") == "fallback_active" for row in rows)
        high_risk = sum(_number(row.get("stop_probability")) >= 65.0 for row in rows)

        if not rows:
            headline = "今日没有可用候选"
            stance = "休息"
            summary = "候选数据尚未生成，今天不根据本系统主动开仓。"
        elif degraded:
            headline = "模型与当前行情不匹配"
            stance = "只观察"
            summary = "候选可以用于复盘和盘中观察，但暂不把模型分数当作买入依据。"
        elif not actionable:
            headline = "今天没有足够可信的机会"
            stance = "休息"
            summary = "没有候选同时通过收益、风险和可信度检查，空仓也是有效决策。"
        elif regime == "weak":
            headline = "弱势行情，优先保护本金"
            stance = "少做"
            summary = "只保留最强候选的盘中确认机会，未确认不买。"
        else:
            headline = f"发现 {len(actionable)} 只值得等待确认的候选"
            stance = "等待确认"
            summary = "盘后结果只确定观察名单，真正买点由下一交易日实时行情确认。"

        return {
            "ok": bool(rows),
            "trade_date": date,
            "headline": headline,
            "stance": stance,
            "summary": summary,
            "market_regime": self.REGIME_LABELS.get(regime, "震荡"),
            "candidate_count": len(rows),
            "actionable_count": len(actionable),
            "high_risk_count": int(high_risk),
            "model_status": "需谨慎" if degraded else "正常",
            "model_mode": "同市场规则模式" if fallback else ("模型暂停" if degraded else "模型正常"),
            "capital_preset": preset.to_dict(),
            "candidates": cards,
            "prompts": ["今天能不能出手？", "哪只风险最低？", "为什么建议只观察？", "看看当前龙头"],
        }

    def answer(self, trade_date: str, question: str, capital: float = 100_000.0) -> Dict[str, Any]:
        brief = self.build_brief(trade_date, capital=capital)
        text = str(question or "").strip()
        candidate = self._match_candidate(brief.get("candidates") or [], text)
        if candidate:
            return self._candidate_answer(brief, candidate)

        if "龙头" in text:
            leaders = self.evidence.get_leader_lifecycle(str(trade_date), lookback=10).get("rows") or []
            names = [f"{row.get('name')}（{row.get('lifecycle_state') or row.get('pool_type')}）" for row in leaders[:5]]
            answer = "当前没有形成可用的龙头证据。" if not names else "当前优先观察：" + "、".join(names) + "。龙头身份仍需盘中强度确认。"
            return {"ok": bool(names), "answer": answer, "facts": []}

        if "风险" in text or "最低" in text:
            rows = brief.get("candidates") or []
            if not rows:
                return {"ok": False, "answer": "当前没有候选，无法比较风险。", "facts": []}
            safest = min(rows, key=lambda row: _number(row.get("stop_probability"), 100.0))
            return {
                "ok": True,
                "answer": f"候选中 {safest['name']} 的历史先止损比例相对最低，但仍需等待盘中确认，不能仅凭这一项买入。",
                "facts": [
                    f"先触发-5%比例 {safest['stop_probability']:.2f}%",
                    f"3日预期总收益 {_signed(safest['expected_total_return'])}",
                    f"当前动作 {safest['action']}",
                ],
                "code": safest["code"],
            }

        return {
            "ok": brief.get("ok", False),
            "answer": f"{brief['headline']}。{brief['summary']}",
            "facts": [
                f"市场状态：{brief['market_regime']}",
                f"候选：{brief['candidate_count']} 只",
                f"可等待确认：{brief['actionable_count']} 只",
                f"高波动风险：{brief['high_risk_count']} 只",
            ],
        }

    def _candidate_card(
        self, row: Dict[str, Any], preset: CapitalPreset, *, market_regime: str = "neutral",
    ) -> Dict[str, Any]:
        grade = str(row.get("confidence_grade") or "D")
        status = str(row.get("decision_status") or "")
        expected_total = _number(row.get("expected_gross_return_pct"))
        expected_excess = _number(row.get("expected_excess_return_pct", row.get("expected_return_pct")))
        stop_probability = _number(row.get("stop_probability"), 100.0)
        probability = _number(row.get("candidate_probability"))
        baseline = _number(row.get("baseline_probability"))

        if status == "model_degraded":
            action, tone = "只观察", "muted"
        elif status in {"data_insufficient", "no_edge"} or expected_total <= 0:
            action, tone = "暂不参与", "danger"
        elif grade in {"A", "B"} and expected_excess > 0 and stop_probability < 60:
            action, tone = "重点观察", "positive"
        elif expected_excess > 0 and probability > baseline:
            action, tone = "等待确认", "warning"
        else:
            action, tone = "谨慎观察", "muted"

        if stop_probability >= 65:
            risk_text = f"波动风险高，历史先触发-5%的比例约 {stop_probability:.0f}%"
        elif stop_probability >= 50:
            risk_text = f"波动风险中等，历史先触发-5%的比例约 {stop_probability:.0f}%"
        else:
            risk_text = f"历史先触发-5%的比例约 {stop_probability:.0f}%"
        if status == "model_degraded":
            risk_text = "模型与当前行情不匹配，以下数字仅供复盘观察"

        base_position = min(
            preset.max_position_per_stock,
            preset.max_total_position / max(preset.max_positions, 1),
        )
        candidate_cap = _number(row.get("position_budget_pct")) / 100.0
        if candidate_cap > 0:
            base_position = min(base_position, candidate_cap)
        if market_regime == "weak":
            base_position = min(base_position, 0.08)
        if action == "重点观察":
            position = min(base_position * 1.15, preset.max_position_per_stock)
        elif action == "等待确认":
            position = base_position
        else:
            position = 0.0

        sectors = [item.strip() for item in str(row.get("resonance_sectors") or "").split(",") if item.strip()][:4]
        pct = _number(row.get("pct_chg", row.get("stock_pct_chg")))
        limit_progress = _number(row.get("limit_progress"))
        if limit_progress >= 90 or pct >= 8:
            strength_text = "当日走势接近涨停，短线强度较高"
        elif pct >= 3:
            strength_text = "当日走势偏强，但仍需次日确认承接"
        else:
            strength_text = "当日强度一般，暂不追价"
        sector_text = f"{sectors[0]}方向有共振" if sectors else "板块共振证据暂不充分"
        lhb_adjustment = _number(row.get("lhb_adjustment"))
        if lhb_adjustment > 0:
            lhb_text = "龙虎榜资金偏积极"
        elif lhb_adjustment < 0:
            lhb_text = "龙虎榜存在拥挤或流出风险"
        else:
            lhb_text = "龙虎榜暂无明显加分"
        position_text = f"确认后参考仓位 {position * 100:.0f}%" if position > 0 else "当前不建议开仓"
        one_line = (
            f"{str(row.get('name') or '')}：{strength_text}，{sector_text}，{lhb_text}；"
            f"未来3日总收益 {_signed(expected_total)}（超额收益 {_signed(expected_excess)}），"
            f"{risk_text}，{position_text}，次日需确认早盘量能与可成交性。"
        )

        return {
            "code": str(row.get("code") or "").zfill(6),
            "name": str(row.get("name") or ""),
            "rank": int(_number(row.get("rank"), 0)),
            "action": action,
            "tone": tone,
            "confidence": self.GRADE_LABELS.get(grade, "证据不足"),
            "confidence_grade": grade,
            "candidate_probability": probability,
            "baseline_probability": baseline,
            "expected_total_return": expected_total,
            "expected_excess_return": expected_excess,
            "stop_probability": stop_probability,
            "sectors": sectors,
            "reason": (row.get("reasons") or ["暂无明确优势证据"])[0],
            "risk": risk_text,
            "position_pct": round(position * 100, 1),
            "position_text": position_text,
            "one_line": one_line,
            "next_step": "下一交易日等待实时确认；未确认、买不到或板块转弱就放弃。",
        }

    @staticmethod
    def _match_candidate(candidates: List[Dict[str, Any]], question: str) -> Optional[Dict[str, Any]]:
        code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", question)
        code = code_match.group(1) if code_match else ""
        for row in candidates:
            if code and row.get("code") == code:
                return row
            if row.get("name") and str(row["name"]) in question:
                return row
        return None

    @staticmethod
    def _candidate_answer(brief: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
        answer = row.get("one_line") or f"{row['name']}当前结论是“{row['action']}”。"
        return {
            "ok": True,
            "answer": answer,
            "facts": [row["reason"], row["next_step"], f"可信程度：{row['confidence']}"],
            "code": row["code"],
            "market_regime": brief.get("market_regime"),
        }


__all__ = ["ReviewAssistantService"]
