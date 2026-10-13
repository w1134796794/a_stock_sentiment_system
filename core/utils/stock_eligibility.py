"""New-entry eligibility from the supplied, dated stock observation only."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any


def new_entry_exclusion(row: Mapping[str, Any]) -> str:
    # Never consult today's listing status to remove a historical stock.
    containers = [row]
    for key in ("context", "metrics"):
        if isinstance(row.get(key), Mapping):
            containers.append(row[key])
    for container in containers:
        for key in ("is_st", "原始_is_st"):
            if str(container.get(key, "")).strip().lower() in {"1", "1.0", "true", "yes"}:
                return "ST/*ST风险股票，不进入新候选或龙头池"
        for key in ("name", "stock_name", "名称", "股票名称"):
            value = container.get(key)
            if not isinstance(value, str):
                continue
            name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()
            if re.match(r"^(?:S\*?ST|\*?ST)(?=[^A-Z]|$)", name):
                return "ST/*ST风险股票，不进入新候选或龙头池"
            if name.startswith("退市") or name.endswith("退"):
                return "退市整理风险股票，不进入新候选或龙头池"
    return ""


def factor_risk_projection(connection: Any) -> str:
    """Keep old factor partitions without optional risk fields readable."""
    columns = {row[0] for row in connection.execute("DESCRIBE factor_stock_wide").fetchall()}
    return "".join(f", {key}" for key in ("name", "is_st") if key in columns)
