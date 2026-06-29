"""Data models for the ETL screening engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class FilterTrace:
    stage: str
    name: str
    factor: str
    op: str
    value: Any
    before_count: int
    passed_count: int
    kept_count: int
    relaxed: bool = False
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "factor": self.factor,
            "op": self.op,
            "value": self.value,
            "before_count": self.before_count,
            "passed_count": self.passed_count,
            "kept_count": self.kept_count,
            "relaxed": self.relaxed,
            "message": self.message,
        }


@dataclass
class ScreeningResult:
    trade_date: str
    profile: str
    ok: bool = True
    message: str = ""
    input_count: int = 0
    after_hard_filter: int = 0
    after_priority_filter: int = 0
    final: List[Dict[str, Any]] = field(default_factory=list)
    scenarios: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    traces: List[FilterTrace] = field(default_factory=list)
    output_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "profile": self.profile,
            "ok": self.ok,
            "message": self.message,
            "input_count": self.input_count,
            "after_hard_filter": self.after_hard_filter,
            "after_priority_filter": self.after_priority_filter,
            "final_count": len(self.final),
            "final": self.final,
            "scenarios": self.scenarios,
            "rejected": self.rejected,
            "traces": [trace.to_dict() for trace in self.traces],
            "output_path": self.output_path,
        }
