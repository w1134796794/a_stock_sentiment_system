"""Read-only strategy comparison and portfolio preview service."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.portfolio.strategy_allocator import StrategyPortfolioAllocator
from core.models.strategy_diagnostics import StrategyDiagnosticsService
from core.screening.strategy_profiles import StrategyProfileRepository


class StrategyLabService:
    def __init__(self, screening_dir: Path | None = None) -> None:
        if screening_dir is None:
            from config.settings import WEB_DATA_DIR

            screening_dir = Path(WEB_DATA_DIR) / "screening"
        self.screening_dir = Path(screening_dir)
        self.repository = StrategyProfileRepository()

    def build(
        self, trade_date: str, strategy_ids: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        profiles = self.repository.list_profiles()
        enabled_ids = [
            str(profile.get("id") or "")
            for profile in profiles
            if profile.get("enabled")
        ]
        selected = self.repository.validate_selection(
            strategy_ids or enabled_ids,
            allow_disabled=bool(strategy_ids),
        )
        profile_by_id = {str(profile.get("id") or ""): profile for profile in profiles}
        strategy_rows: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        diagnostics_service = StrategyDiagnosticsService()

        for strategy_id in selected:
            profile = self.repository.get_profile(strategy_id) or profile_by_id.get(strategy_id) or {}
            payload = self._load(strategy_id, trade_date)
            final = list(payload.get("final") or [])
            diagnostics = diagnostics_service.build(profile, trade_date, final)
            strategy_rows.append({
                "id": strategy_id,
                "name": profile.get("name") or strategy_id,
                "version": profile.get("version") or payload.get("strategy_version") or "--",
                "candidate_count": len(final),
                "available": bool(payload),
                "market_regimes": list(profile.get("market_regimes") or []),
                "entry_modes": list((profile.get("execution") or {}).get("allowed_entry_modes") or []),
                "top_n": int(profile.get("top_n") or 0),
                "diagnostics": diagnostics,
            })
            for item in final:
                row = dict(item or {})
                row.update({
                    "策略ID": strategy_id,
                    "策略名称": profile.get("name") or payload.get("strategy_name") or strategy_id,
                    "策略版本": profile.get("version") or payload.get("strategy_version") or "",
                    "策略单票仓位上限%": profile.get("position_cap_pct") or payload.get("position_cap_pct") or 0,
                    "策略执行": profile.get("execution") or payload.get("strategy_execution") or {},
                })
                candidates.append(row)

        allocator = StrategyPortfolioAllocator()
        allocation = allocator.allocate(candidates)
        consensus_count = sum(1 for row in allocation if int(row.get("策略共识数") or 0) >= 2)
        total_weight = sum(float(row.get("组合建议仓位%") or 0.0) for row in allocation)
        return {
            "ok": True,
            "trade_date": str(trade_date or ""),
            "strategies": strategy_rows,
            "selected_strategy_ids": selected,
            "allocation": allocation,
            "portfolio_risk": allocator.last_risk_report,
            "summary": {
                "input_candidates": len(candidates),
                "unique_candidates": len({str(row.get("代码") or row.get("code") or "") for row in candidates}),
                "allocated_count": len(allocation),
                "consensus_count": consensus_count,
                "total_weight_pct": round(total_weight, 2),
            },
        }

    def _load(self, strategy_id: str, trade_date: str) -> Dict[str, Any]:
        path = self.screening_dir / "combinations" / strategy_id / f"screening_{trade_date}.json"
        if strategy_id == "default" and not path.exists():
            path = self.screening_dir / f"screening_{trade_date}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}


__all__ = ["StrategyLabService"]
