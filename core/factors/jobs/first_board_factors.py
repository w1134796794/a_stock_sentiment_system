"""First-board and sector resonance factor helpers.

The official limit-up pool determines whether a stock is a first board.  Sector
membership and sector tape only explain the quality of that first board; they
never infer a limit-up event from ``pct_chg``.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from config.settings import CACHE_DIR
from core.factors.jobs.gold_utils import safe_weighted_score, score_between, to_float
from core.factors.sector_taxonomy import is_trade_theme_sector

FIRST_BOARD_FACTOR_COLUMNS = (
    "first_board_sector_sync_score",
    "first_board_leadership_score",
    "first_board_sector_pioneer_score",
    "first_board_breadth_score",
    "first_board_amount_surge_score",
    "first_board_new_theme_score",
    "first_board_resonance_score",
)


def _code6(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _sector_code(value: Any) -> str:
    return str(value or "").split(".")[0]


def _first_time_seconds(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"0", "0.0", "nan", "none", "00:00:00"}:
        return None
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) < 6:
        return None
    try:
        hour, minute, second = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    except ValueError:
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def load_stock_memberships(code: Any, cache_dir: Path = CACHE_DIR) -> list[dict[str, str]]:
    """Load only concept/industry memberships that can represent a trade theme."""
    code6 = _code6(code)
    folder = Path(cache_dir) / "sector" / "stock_sectors"
    files = list(folder.glob(f"{code6}.*.csv"))
    if not files:
        return []
    try:
        frame = pd.read_csv(files[0])
    except Exception:
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in frame.to_dict("records"):
        sector_type = str(row.get("type") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        code_value = _sector_code(row.get("ts_code"))
        if sector_type not in {"N", "I", "概念", "行业"}:
            continue
        if not code_value or code_value in seen or not is_trade_theme_sector(name, sector_type):
            continue
        seen.add(code_value)
        result.append({"code": code_value, "name": name, "type": sector_type})
    return result


def load_membership_map(codes: list[Any], cache_dir: Path = CACHE_DIR) -> dict[str, list[dict[str, str]]]:
    return {_code6(code): load_stock_memberships(code, cache_dir) for code in codes}


def _prior_first_board_sector_counts(con, trade_date: str, days: int = 5) -> dict[str, int]:
    """Count recent first-board appearances by the point-in-time primary sector."""
    frame = pd.DataFrame()
    for sector_column in ("first_board_primary_sector_code", "primary_sector_code"):
        try:
            frame = con.execute(
                f"""
                SELECT {sector_column} AS sector_code, COUNT(*) AS appearances
                FROM factor_stock_wide
                WHERE CAST(trade_date AS VARCHAR) IN (
                    SELECT DISTINCT CAST(trade_date AS VARCHAR)
                    FROM factor_stock_wide
                    WHERE CAST(trade_date AS VARCHAR) < ?
                    ORDER BY CAST(trade_date AS VARCHAR) DESC
                    LIMIT ?
                )
                  AND CAST(board_height AS DOUBLE) = 1
                  AND COALESCE(CAST({sector_column} AS VARCHAR), '') <> ''
                GROUP BY {sector_column}
                """,
                [str(trade_date), max(int(days), 1)],
            ).fetchdf()
            break
        except Exception:
            frame = pd.DataFrame()
    if frame.empty:
        return {}
    return {
        _sector_code(row.get("sector_code")): int(to_float(row.get("appearances"), 0))
        for row in frame.to_dict("records")
    }


def build_first_board_stock_metrics(
    con,
    trade_date: str,
    today: pd.DataFrame,
    pool_by_code: dict[str, dict[str, Any]],
    sector_scores: dict[str, dict[str, Any]],
    membership_map: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    """Build stock-level first-board quality scores from official board records."""
    code_rows = {
        _code6(row.get("code")): row
        for row in today.to_dict("records")
    }
    first_board_codes = {
        _code6(code)
        for code, row in pool_by_code.items()
        if int(round(to_float(row.get("limit_times"), 0))) == 1
    }
    limit_codes = {_code6(code) for code in pool_by_code}
    sector_members: dict[str, set[str]] = defaultdict(set)
    sector_first_boards: dict[str, set[str]] = defaultdict(set)
    sector_limit_codes: dict[str, set[str]] = defaultdict(set)
    sector_names: dict[str, str] = {}
    for code, memberships in membership_map.items():
        for sector in memberships:
            sector_code = _sector_code(sector.get("code"))
            if not sector_code or sector_code not in sector_scores:
                continue
            sector_names[sector_code] = str(sector.get("name") or sector_code)
            sector_members[sector_code].add(code)
            if code in first_board_codes:
                sector_first_boards[sector_code].add(code)
            if code in limit_codes:
                sector_limit_codes[sector_code].add(code)

    prior_counts = _prior_first_board_sector_counts(con, trade_date)
    results: dict[str, dict[str, Any]] = {}
    for code in code_rows:
        empty = {column: 0.0 for column in FIRST_BOARD_FACTOR_COLUMNS}
        empty.update({
            "first_board_factor_available": 0,
            "first_board_primary_sector_code": "",
            "first_board_primary_sector_name": "",
            "first_board_sector_top20": 0,
        })
        if code not in first_board_codes:
            results[code] = empty
            continue

        row = code_rows[code]
        candidates: list[dict[str, Any]] = []
        for sector in membership_map.get(code, []):
            sector_code = _sector_code(sector.get("code"))
            sector_row = sector_scores.get(sector_code)
            if not sector_row:
                continue
            members = sector_members.get(sector_code, set())
            board_peers = sector_first_boards.get(sector_code, set())
            member_pct = [
                to_float(code_rows[member].get("pct_chg"))
                for member in members if member in code_rows
            ]
            breadth_ratio = (
                sum(value > 5.0 for value in member_pct) / len(member_pct)
                if member_pct else 0.0
            )
            breadth_score = score_between(breadth_ratio, 0.0, 0.25)
            first_board_ratio = len(board_peers) / max(len(members), 1)
            pioneer_score = safe_weighted_score([
                (score_between(first_board_ratio, 0.0, 0.08), 0.65),
                (score_between(len(board_peers), 1.0, 4.0), 0.35),
            ])

            current_seconds = _first_time_seconds((pool_by_code.get(code) or {}).get("first_time"))
            peer_seconds = [
                value for value in (
                    _first_time_seconds((pool_by_code.get(peer) or {}).get("first_time"))
                    for peer in board_peers
                ) if value is not None
            ]
            if current_seconds is None:
                leadership_score = 0.0
                time_available = False
            elif len(peer_seconds) <= 1:
                leadership_score = 75.0
                time_available = True
            else:
                leadership_score = (
                    sum(value > current_seconds for value in peer_seconds)
                    / (len(peer_seconds) - 1)
                    * 100.0
                )
                time_available = True

            momentum_score = to_float(sector_row.get("momentum_score"), 50.0)
            amount_score = to_float(sector_row.get("amount_ratio_score"), 50.0)
            limit_diffusion = score_between(len(sector_limit_codes.get(sector_code, set())), 0.0, 6.0)
            stock_amount_score = to_float(row.get("amount_ratio_score"), 50.0)
            sync_score = safe_weighted_score([
                (momentum_score, 0.35),
                (limit_diffusion, 0.25),
                (amount_score, 0.20),
                (stock_amount_score, 0.20),
            ])
            amount_surge_score = safe_weighted_score([
                (amount_score, 0.55), (stock_amount_score, 0.45),
            ])
            prior_count = prior_counts.get(sector_code, 0)
            new_theme_score = 100.0 if prior_count == 0 else max(10.0, 100.0 - prior_count * 30.0)
            composite_parts = [
                (sync_score, 0.25),
                (pioneer_score, 0.15),
                (breadth_score, 0.15),
                (amount_surge_score, 0.15),
                (new_theme_score, 0.10),
            ]
            if time_available:
                composite_parts.append((leadership_score, 0.20))
            composite = safe_weighted_score(composite_parts)
            candidates.append({
                "first_board_sector_sync_score": sync_score,
                "first_board_leadership_score": leadership_score,
                "first_board_sector_pioneer_score": pioneer_score,
                "first_board_breadth_score": breadth_score,
                "first_board_amount_surge_score": amount_surge_score,
                "first_board_new_theme_score": new_theme_score,
                "first_board_resonance_score": composite,
                "first_board_factor_available": int(time_available and len(member_pct) >= 3),
                "first_board_primary_sector_code": sector_code,
                "first_board_primary_sector_name": sector_names.get(sector_code, sector_code),
                "first_board_sector_top20": 0,
                "_momentum_score": momentum_score,
            })
        if not candidates:
            results[code] = empty
            continue
        best = max(candidates, key=lambda item: item["first_board_resonance_score"])
        results[code] = best

    momentum_values = sorted(
        [to_float(row.get("momentum_score"), 50.0) for row in sector_scores.values()]
    )
    top20_cutoff = (
        pd.Series(momentum_values).quantile(0.80) if momentum_values else 100.0
    )
    for row in results.values():
        momentum_score = to_float(row.pop("_momentum_score", 0.0))
        row["first_board_sector_top20"] = int(
            bool(row.get("first_board_primary_sector_code"))
            and momentum_score >= top20_cutoff
        )
    return results


def first_board_market_metrics(
    con,
    trade_date: str,
    limit_pool: pd.DataFrame,
    cache_dir: Path = CACHE_DIR,
) -> dict[str, float | int | None]:
    """Compute market-level first-board sector structure from official pools."""
    defaults = {
        "first_board_sector_resonance_ratio": None,
        "first_board_cluster_count": None,
        "first_board_follow_through_ratio": None,
    }
    if limit_pool.empty:
        return defaults
    pool = limit_pool.copy()
    pool["code"] = pool.get("code", pd.Series(dtype=str)).map(_code6)
    heights = pd.to_numeric(pool.get("limit_times"), errors="coerce").fillna(0).round()
    current_codes = list(pool.loc[heights.eq(1), "code"].astype(str))
    if not current_codes:
        return {**defaults, "first_board_sector_resonance_ratio": 0.0, "first_board_cluster_count": 0}
    try:
        sector_today = con.execute(
            "SELECT sector_code, sector_name, pct_chg FROM sector_daily_silver "
            "WHERE CAST(trade_date AS VARCHAR)=?",
            [str(trade_date)],
        ).fetchdf()
    except Exception:
        return defaults
    if sector_today.empty:
        return defaults
    sector_today["sector_code"] = sector_today["sector_code"].map(_sector_code)
    sector_today["pct_chg"] = pd.to_numeric(sector_today.get("pct_chg"), errors="coerce")
    top_cutoff = sector_today["pct_chg"].quantile(0.80)
    top_codes = set(sector_today.loc[sector_today["pct_chg"] >= top_cutoff, "sector_code"])
    positive_codes = set(sector_today.loc[sector_today["pct_chg"] > 0, "sector_code"])
    current_memberships = load_membership_map(current_codes, cache_dir)
    current_sector_counts: dict[str, int] = defaultdict(int)
    resonant = 0
    for code in current_codes:
        codes = {_sector_code(item.get("code")) for item in current_memberships.get(code, [])}
        resonant += int(bool(codes.intersection(top_codes)))
        for sector_code in codes:
            current_sector_counts[sector_code] += 1

    follow_through: float | None = None
    try:
        previous_date = con.execute(
            "SELECT MAX(CAST(trade_date AS VARCHAR)) FROM stock_daily_silver "
            "WHERE CAST(trade_date AS VARCHAR) < ?", [str(trade_date)],
        ).fetchone()[0]
        previous_pool = con.execute(
            "SELECT code, limit_times FROM limit_up_pool_silver "
            "WHERE CAST(trade_date AS VARCHAR)=?", [str(previous_date)],
        ).fetchdf()
        previous_heights = pd.to_numeric(previous_pool.get("limit_times"), errors="coerce").fillna(0).round()
        previous_codes = list(previous_pool.loc[previous_heights.eq(1), "code"].map(_code6))
        previous_memberships = load_membership_map(previous_codes, cache_dir)
        previous_sectors = {
            _sector_code(item.get("code"))
            for code in previous_codes for item in previous_memberships.get(code, [])
        }
        if previous_sectors:
            follow_through = len(previous_sectors.intersection(positive_codes)) / len(previous_sectors)
    except Exception:
        follow_through = None
    return {
        "first_board_sector_resonance_ratio": resonant / len(current_codes),
        "first_board_cluster_count": sum(count >= 3 for count in current_sector_counts.values()),
        "first_board_follow_through_ratio": follow_through,
    }


__all__ = [
    "FIRST_BOARD_FACTOR_COLUMNS",
    "build_first_board_stock_metrics",
    "first_board_market_metrics",
    "load_membership_map",
    "load_stock_memberships",
]
