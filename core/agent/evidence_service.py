"""Standardized, read-only evidence contracts consumed by agents and UI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class AgentEvidenceService:
    def __init__(self, *, web_data_dir: Optional[Path] = None, duckdb_path: Optional[Path] = None) -> None:
        from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

        self.web_data_dir = Path(web_data_dir or WEB_DATA_DIR)
        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)

    def _screening(self, trade_date: str) -> Dict[str, Any]:
        path = self.web_data_dir / "screening" / f"screening_{trade_date}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _find(rows: Iterable[Dict[str, Any]], code: str) -> Dict[str, Any]:
        target = str(code or "").split(".")[0].zfill(6)
        return next((row for row in rows if str(row.get("code") or "").split(".")[0].zfill(6) == target), {})

    def get_candidate_evidence(self, trade_date: str, code: str) -> Dict[str, Any]:
        payload = self._screening(str(trade_date))
        row = self._find(payload.get("candidate_pool") or payload.get("final") or [], code)
        if not row:
            return {"ok": False, "reason": "候选证据不存在", "trade_date": str(trade_date), "code": str(code)}
        return {
            "ok": True,
            "evidence_type": "candidate",
            "trade_date": str(trade_date),
            "code": str(row.get("code") or code),
            "name": row.get("name") or "",
            "decision": row.get("decision_label") or "观察",
            "confidence": row.get("confidence") or {},
            "trust_layers": row.get("trust_layers") or (row.get("confidence") or {}).get("trust_layers") or {},
            "score": row.get("score"),
            "rank": row.get("rank"),
            "sectors": row.get("resonance_sectors") or "",
            "reasons": row.get("reasons") or [],
            "exclusion_reasons": row.get("排除理由") or [],
            "shap": row.get("shap_explanation") or [],
            "model": payload.get("weight_metadata") or {},
        }

    def get_leader_lifecycle(self, trade_date: str, code: str = "", *, lookback: int = 10) -> Dict[str, Any]:
        from core.realtime.leader_pool_service import LeaderPoolService

        payload = LeaderPoolService(screening_dir=self.web_data_dir / "screening", duckdb_path=self.duckdb_path).build_pool(
            str(trade_date), lookback=lookback, limit=100,
        )
        if code:
            row = self._find(payload.get("rows") or [], code)
            return {"ok": bool(row), "evidence_type": "leader_lifecycle", "trade_date": str(trade_date), "row": row}
        return {"ok": bool(payload.get("rows")), "evidence_type": "leader_lifecycle", **payload}

    def get_intraday_signal(self, trade_date: str, code: str, *, cached_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(cached_payload or {})
        row = self._find(payload.get("rows") or [], code)
        if not row:
            return {
                "ok": False,
                "evidence_type": "intraday_signal",
                "trade_date": str(trade_date),
                "code": str(code),
                "reason": "未命中共享实时缓存；Agent不得临时请求外部行情",
            }
        return {"ok": True, "evidence_type": "intraday_signal", "trade_date": str(trade_date), "row": row}

    def get_sector_breadth(self, trade_date: str, sector: str) -> Dict[str, Any]:
        if not self.duckdb_path.exists():
            return {"ok": False, "reason": "因子仓库不存在"}
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT sector_code, sector_name, mainline_score, momentum_score, amount_score, "
                    "persistence_score FROM factor_sector_wide "
                    "WHERE CAST(trade_date AS VARCHAR)=? AND sector_name LIKE ? ORDER BY mainline_score DESC",
                    [str(trade_date), f"%{sector}%"],
                ).fetchdf().to_dict("records")
            finally:
                con.close()
            return {"ok": bool(rows), "evidence_type": "sector_breadth", "trade_date": str(trade_date), "rows": rows}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    def get_similar_samples(self, trade_date: str, code: str, *, limit: int = 20) -> Dict[str, Any]:
        candidate = self.get_candidate_evidence(trade_date, code)
        if not candidate.get("ok") or not self.duckdb_path.exists():
            return {"ok": False, "reason": "候选或历史结果不存在", "rows": []}
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                current = con.execute(
                    "SELECT tech_score, liquidity_score, sector_resonance_score, amount_ratio, board_score "
                    "FROM factor_stock_wide WHERE CAST(trade_date AS VARCHAR)=? "
                    "AND LPAD(CAST(code AS VARCHAR), 6, '0')=? LIMIT 1",
                    [str(trade_date), str(code).zfill(6)],
                ).fetchone()
                if not current:
                    return {"ok": False, "reason": "当前候选缺少因子向量", "rows": []}
                rows = con.execute(
                    "SELECT s.*, f.tech_score, f.liquidity_score, f.sector_resonance_score, "
                    "f.amount_ratio, f.board_score, "
                    "(ABS(COALESCE(f.tech_score,50)-?)/100 + "
                    " ABS(COALESCE(f.liquidity_score,50)-?)/100 + "
                    " ABS(COALESCE(f.sector_resonance_score,50)-?)/100 + "
                    " ABS(LN(1+GREATEST(COALESCE(f.amount_ratio,0),0))-LN(1+GREATEST(?,0))) + "
                    " ABS(COALESCE(f.board_score,50)-?)/100) AS distance "
                    "FROM signal_outcome_wide s JOIN factor_stock_wide f "
                    "ON CAST(s.trade_date AS VARCHAR)=CAST(f.trade_date AS VARCHAR) "
                    "AND LPAD(CAST(s.code AS VARCHAR),6,'0')=LPAD(CAST(f.code AS VARCHAR),6,'0') "
                    "WHERE CAST(s.trade_date AS VARCHAR) < ? AND COALESCE(s.tradable_next_day,0)=1 "
                    "ORDER BY distance LIMIT ?",
                    [*current, str(trade_date), int(limit)],
                ).fetchdf().to_dict("records")
            finally:
                con.close()
            return {"ok": bool(rows), "evidence_type": "similar_samples", "rows": rows}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "rows": []}

    @staticmethod
    def get_position_budget(
        *, win_rate: float, payoff_ratio: float, samples: int, stop_distance: float = 0.05,
        data_quality: float = 1.0, regime_match: float = 1.0, tradability: float = 1.0,
        correlation_penalty: float = 0.0,
    ) -> Dict[str, Any]:
        from risk.kelly_sizer import KellySizer
        from risk.risk_config import RiskConfig

        result = KellySizer(RiskConfig.load()).size(
            win_rate, payoff_ratio, samples, 0.10,
            stop_distance=stop_distance,
            data_quality=data_quality,
            regime_match=regime_match,
            tradability=tradability,
            correlation_penalty=correlation_penalty,
        )
        return {"ok": True, "evidence_type": "position_budget", **result}

    def explain_prediction(self, trade_date: str, code: str) -> Dict[str, Any]:
        evidence = self.get_candidate_evidence(trade_date, code)
        if not evidence.get("ok"):
            return evidence
        return {
            "ok": True,
            "evidence_type": "prediction_explanation",
            "trade_date": str(trade_date),
            "code": str(code),
            "decision": evidence.get("decision"),
            "rule_reasons": evidence.get("reasons") or [],
            "exclusion_reasons": evidence.get("exclusion_reasons") or [],
            "model_contributions": evidence.get("shap") or [],
            "trust_layers": evidence.get("trust_layers") or {},
        }


__all__ = ["AgentEvidenceService"]
