"""Versioned, declarative and backtestable short-term style skills."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class StyleSkillRegistry:
    def __init__(self, path: Optional[Path] = None) -> None:
        from config.settings import BASE_DIR

        self.path = Path(path or BASE_DIR / "config" / "style_skills.yaml")

    def load(self) -> Dict[str, Dict[str, Any]]:
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        return dict((payload or {}).get("skills") or {})

    def list(self) -> List[Dict[str, Any]]:
        return [{"id": key, **value} for key, value in self.load().items()]

    def get(self, skill_id: str) -> Dict[str, Any]:
        value = self.load().get(str(skill_id)) or {}
        return {"id": str(skill_id), **value} if value else {}

    def evaluate_readiness(self, skill_id: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        skill = self.get(skill_id)
        if not skill:
            return {"ok": False, "status": "unknown_skill", "missing": []}
        required = list(skill.get("required_evidence") or [])
        missing = [name for name in required if not evidence.get(name)]
        vetoes = [rule for rule in skill.get("veto_rules") or [] if rule in set(evidence.get("active_vetoes") or [])]
        status = "ready" if not missing and not vetoes else "vetoed" if vetoes else "data_insufficient"
        return {"ok": status == "ready", "status": status, "missing": missing, "vetoes": vetoes, "skill": skill}


__all__ = ["StyleSkillRegistry"]
