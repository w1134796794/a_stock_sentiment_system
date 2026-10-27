"""Prefetch T+1 minute and auction evidence required by strategy training.

The trainer is intentionally local-only. Run this command before model training
so strict executable-entry labels never trigger remote requests implicitly.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, Mapping, Set, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data.minute_cache_files import minute_cache_exists, read_minute_csv

from config.settings import CACHE_DIR, FACTOR_DB_PATH, TUSHARE_TOKEN
from core.data import DataManager
from core.factors.strategy_training import STRATEGY_TRAINING_SPECS, StrategyMinuteTrainingBuilder
from core.utils.stock_code_utils import StockCodeUtils
from core.utils.sector_names import normalize_sector_names
from core.utils.stock_eligibility import factor_risk_projection, new_entry_exclusion
from core.data.minute_quality import inspect_minute_bars, minute_failure


Requirement = Tuple[str, str]


def _canonical(code: str) -> str:
    return StockCodeUtils.standardize_code(str(code), add_suffix=True)


def _first_sector(value: Any) -> str:
    return next(iter(normalize_sector_names(value)), '')


def _trade_date_map(connection: Any) -> Dict[str, str]:
    dates = [str(row[0]) for row in connection.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_silver ORDER BY trade_date"
    ).fetchall()]
    return {date: dates[index + 1] for index, date in enumerate(dates[:-1])}


def _strategy_candidates(
    profiles: Iterable[str], start_date: str, end_date: str,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    builder = StrategyMinuteTrainingBuilder(duckdb_path=Path(FACTOR_DB_PATH))
    for profile in profiles:
        for key, row in builder.candidate_map(
            profile, start_date=start_date, end_date=end_date,
        ).items():
            merged.setdefault(key, dict(row))
    return merged


def build_requirements(
    profiles: Iterable[str],
    start_date: str,
    end_date: str,
    *,
    include_sector_peers: bool = True,
    peer_count: int = 4,
) -> tuple[Set[Requirement], Set[Requirement], Dict[str, Any]]:
    import duckdb  # type: ignore
    from core.infrastructure.duckdb_access import connect as duckdb_connect

    all_candidates = _strategy_candidates(profiles, start_date, end_date)
    candidates = {key: row for key, row in all_candidates.items() if not new_entry_exclusion(row)}
    minutes: Set[Requirement] = set()
    auctions: Set[Requirement] = set()
    missing_entry_date = 0
    peer_requirements = 0
    by_date: Dict[str, list[Tuple[str, Mapping[str, Any]]]] = defaultdict(list)

    connection = duckdb_connect(str(FACTOR_DB_PATH), read_only=True)
    try:
        next_dates = _trade_date_map(connection)
        for (plan_date, code), row in candidates.items():
            entry_date = next_dates.get(str(plan_date), "")
            if not entry_date:
                missing_entry_date += 1
                continue
            ts_code = _canonical(code)
            minutes.add((entry_date, ts_code))
            auctions.add((entry_date, ts_code))
            by_date[str(plan_date)].append((str(code).zfill(6), row))

        if include_sector_peers:
            risk_columns = factor_risk_projection(connection)
            for plan_date, rows in sorted(by_date.items()):
                factor_rows = connection.execute(
                    f"SELECT code, ts_code, resonance_sectors, total_score{risk_columns} "
                    "FROM factor_stock_wide WHERE trade_date=? "
                    "QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY COALESCE(computed_at, '') DESC)=1",
                    [plan_date],
                ).fetchdf()
                if factor_rows.empty:
                    continue
                eligible = [not new_entry_exclusion(row) for row in factor_rows.to_dict("records")]
                factor_rows = factor_rows.loc[eligible].copy()
                factor_rows["code"] = factor_rows["code"].astype(str).str.zfill(6)
                factor_rows["resonance_sectors"] = factor_rows["resonance_sectors"].fillna("").astype(str)
                factor_rows["total_score"] = pd.to_numeric(
                    factor_rows.get("total_score"), errors="coerce",
                ).fillna(0.0)
                entry_date = next_dates.get(plan_date, "")
                for code, candidate in rows:
                    own = factor_rows[factor_rows["code"] == code]
                    sectors = set(normalize_sector_names(candidate.get("resonance_sectors")))
                    if not sectors and not own.empty:
                        sectors = set(normalize_sector_names(own.iloc[0].get("resonance_sectors")))
                    if not sectors:
                        continue
                    peers = factor_rows[
                        factor_rows["code"].ne(code)
                        & factor_rows["resonance_sectors"].map(
                            lambda text: bool(sectors.intersection(normalize_sector_names(text))))
                    ].nlargest(max(int(peer_count), 3), "total_score")
                    for peer in peers.itertuples(index=False):
                        peer_code = peer.ts_code if pd.notna(peer.ts_code) and str(peer.ts_code) else peer.code
                        minutes.add((entry_date, _canonical(peer_code)))
                        peer_requirements += 1
    finally:
        connection.close()

    audit = {
        "profiles": list(profiles),
        "candidate_rows": len(candidates),
        "excluded_risk_candidates": len(all_candidates) - len(candidates),
        "minute_requirements": len(minutes),
        "auction_requirements": len(auctions),
        "peer_requirements_before_dedup": peer_requirements,
        "missing_next_trade_date": missing_entry_date,
    }
    return minutes, auctions, audit


def _minute_cached(cache_dir: Path, trade_date: str, ts_code: str) -> bool:
    path = cache_dir / "stock" / "tick" / f"{ts_code}_{trade_date}.csv"
    if not minute_cache_exists(path):
        return False
    try:
        return inspect_minute_bars(read_minute_csv(path), trade_date, expected_code=ts_code)["valid"]
    except (OSError, ValueError, pd.errors.ParserError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="预取策略训练所需的T+1分钟与集合竞价数据")
    parser.add_argument("--start", required=True, help="候选开始日 YYYYMMDD")
    parser.add_argument("--end", required=True, help="候选结束日 YYYYMMDD")
    parser.add_argument(
        "--profiles", default="all",
        help="逗号分隔策略ID；all表示全部正式策略",
    )
    parser.add_argument("--peer-count", type=int, default=4, help="每只候选预取的板块同伴数")
    parser.add_argument("--no-sector-peers", action="store_true", help="不预取板块同伴（训练通常不建议）")
    parser.add_argument("--skip-auction", action="store_true", help="不预取集合竞价")
    parser.add_argument("--sleep", type=float, default=0.0, help="每次远程请求后的等待秒数")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前N个分钟需求，用于小批量验证")
    parser.add_argument("--dry-run", action="store_true", help="只统计需求，不发起请求")
    args = parser.parse_args()

    profiles = (
        list(STRATEGY_TRAINING_SPECS)
        if args.profiles.strip().lower() == "all"
        else [item.strip() for item in args.profiles.split(",") if item.strip()]
    )
    unknown = [profile for profile in profiles if profile not in STRATEGY_TRAINING_SPECS]
    if unknown:
        raise SystemExit(f"未知策略ID: {unknown}")

    minute_requirements, auction_requirements, audit = build_requirements(
        profiles,
        args.start,
        args.end,
        include_sector_peers=not args.no_sector_peers,
        peer_count=args.peer_count,
    )
    cached = {
        requirement for requirement in minute_requirements
        if _minute_cached(Path(CACHE_DIR), *requirement)
    }
    pending = sorted(minute_requirements - cached)
    if args.limit > 0:
        pending = pending[: args.limit]
    audit.update({"minute_cached": len(cached), "minute_pending": len(pending)})
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, **audit}, ensure_ascii=False, indent=2))
        return

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    fetched = 0
    failed = []
    for index, (trade_date, ts_code) in enumerate(pending, start=1):
        frame = dm.get_stock_tick(ts_code, trade_date, require_full_day=True)
        if inspect_minute_bars(frame, trade_date)["valid"]:
            fetched += 1
        else:
            failed.append(minute_failure(dm, frame, trade_date, ts_code))
        if index % 100 == 0:
            print(f"[minute-prefetch] {index}/{len(pending)} fetched={fetched} failed={len(failed)}", flush=True)
        if args.sleep > 0:
            time.sleep(args.sleep)

    auction_fetched = 0
    auction_failed = []
    if not args.skip_auction:
        for trade_date, ts_code in sorted(auction_requirements):
            payload = dm.get_auction_data(ts_code, trade_date)
            if payload:
                auction_fetched += 1
            else:
                auction_failed.append({"trade_date": trade_date, "ts_code": ts_code})
            if args.sleep > 0:
                time.sleep(args.sleep)

    result = {
        "ok": not failed and (args.skip_auction or not auction_failed),
        **audit,
        "minute_fetched": fetched,
        "minute_failed": len(failed),
        "minute_failures_preview": failed[:20],
        "auction_fetched": auction_fetched,
        "auction_failed": len(auction_failed),
        "auction_failures_preview": auction_failed[:20],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
