"""Evidence contracts for isolated paper-account historical rebuilds."""
from __future__ import annotations

from typing import Any

STRICT = "strict"
SKIP_CANDIDATES = "skip_candidates"


def normalize_policy(value: Any = STRICT) -> str:
    value = str(value or STRICT)
    if value not in {STRICT, SKIP_CANDIDATES}:
        raise ValueError("缺失证据策略只能为 strict 或 skip_candidates")
    return value


def missing_entry_evidence(row: dict) -> bool:
    status = str(row.get("data_status") or "")
    reason = str(row.get("reason_code") or "")
    return (row.get("status") == "data_insufficient"
            or reason.startswith("missing_")
            or status.startswith(("missing_", "invalid_"))
            or status == "daily_error"
            or reason == "incomplete_opening_window")


def requires_auction(execution: dict) -> bool:
    return (execution.get("auction_required") is True
            or "auction" in (execution.get("required_evidence") or []))


class ReplayEvidenceBlocked(ValueError):
    def __init__(self, details: list[dict], quality: dict):
        self.details = details
        self.quality = quality
        self.trade_date = str(details[0].get("trade_date") or "")
        super().__init__("; ".join(
            f"{row.get('trade_date')} / {row.get('code')}: {row.get('reason')}" for row in details[:5]))
