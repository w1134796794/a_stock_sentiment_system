"""Persist a compact model-health decision after each screening run."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class ModelHealthMonitor:
    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            from config.settings import WEB_DATA_DIR

            output_dir = Path(WEB_DATA_DIR) / "models" / "health"
        self.output_dir = Path(output_dir)

    def evaluate(self, screening: Dict[str, Any]) -> Dict[str, Any]:
        metadata = screening.get("weight_metadata") or {}
        drift = metadata.get("feature_drift") or {}
        runtime = str(metadata.get("candidate_model_runtime") or "unavailable")
        fallback = runtime.startswith("fallback")
        candidates = screening.get("final") or []
        grades: Dict[str, int] = {}
        for row in candidates:
            grade = str(row.get("confidence_grade") or "D")
            grades[grade] = grades.get(grade, 0) + 1
        if fallback:
            status = "fallback"
            message = "机器学习模型当前不适配，已自动使用同市场IC/IR规则筛选"
        elif runtime == "active" and str(drift.get("status") or "") != "degraded":
            status = "healthy"
            message = "机器学习模型运行正常"
        else:
            status = "unavailable"
            message = "机器学习模型不可用，当前使用规则筛选"
        trade_date = str(screening.get("trade_date") or "")
        no_ab_streak = self.no_ab_streak(trade_date, current_grades=grades)
        diagnostic = no_ab_streak >= 3
        return {
            "trade_date": trade_date,
            "status": status,
            "message": message,
            "runtime": runtime,
            "market_regime": metadata.get("market_regime") or "",
            "drift_status": drift.get("status") or "unknown",
            "drift_psi": drift.get("max_psi"),
            "drift_ks": drift.get("max_ks"),
            "candidate_count": len(candidates),
            "grade_distribution": grades,
            "no_ab_streak": no_ab_streak,
            "training_diagnostic_triggered": diagnostic,
            "training_diagnostic_message": (
                f"连续{no_ab_streak}个交易日没有A/B级候选，建议检查标签、校准、漂移基准和发布闸门"
                if diagnostic else ""
            ),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    def no_ab_streak(
        self,
        trade_date: str,
        *,
        current_grades: Optional[Dict[str, int]] = None,
    ) -> int:
        """Count consecutive persisted screening days without A/B candidates."""
        dated: Dict[str, Dict[str, int]] = {}
        if self.output_dir.exists():
            for path in self.output_dir.glob("model_health_*.json"):
                date = path.stem.removeprefix("model_health_")
                if len(date) != 8 or date > str(trade_date):
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    dated[date] = dict(payload.get("grade_distribution") or {})
                except Exception:
                    continue
        if current_grades is not None and trade_date:
            dated[str(trade_date)] = dict(current_grades)
        streak = 0
        for date in sorted(dated, reverse=True):
            grades = dated[date]
            if int(grades.get("A") or 0) + int(grades.get("B") or 0) > 0:
                break
            streak += 1
        return streak

    def write(self, screening: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.evaluate(screening)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        date = payload.get("trade_date") or "unknown"
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (self.output_dir / f"model_health_{date}.json").write_text(text, encoding="utf-8")
        (self.output_dir / "latest.json").write_text(text, encoding="utf-8")
        return payload


__all__ = ["ModelHealthMonitor"]
