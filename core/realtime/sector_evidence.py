"""Prepare genuine THS reference data, never prices or synthetic live proof."""
from __future__ import annotations

from core.realtime.sector_service import RealtimeSectorService


def prepare_sector_cache(names, *, cache_dir=None, data_manager=None, allow_remote=False, as_of_date=""):
    service = RealtimeSectorService(cache_dir=cache_dir)
    wanted = sorted(set(names))
    mapping = service.resolve_codes_by_names(wanted, source="ths", as_of_date=as_of_date)
    if allow_remote and data_manager is not None and set(wanted) - set(mapping):
        for kind in ("I", "N"):
            data_manager.get_ths_index(kind)
        mapping = service.resolve_codes_by_names(wanted, source="ths", as_of_date=as_of_date)
    members = {name: service.member_codes(code) for name, code in mapping.items()}
    missing_codes = sorted({mapping[name] for name, values in members.items() if not values})
    if allow_remote and data_manager is not None and missing_codes:
        data_manager.get_ths_members_batch([code + ".TI" for code in missing_codes])
        service = RealtimeSectorService(cache_dir=cache_dir)
        members = {name: service.member_codes(code) for name, code in mapping.items()}
    return {"sector_codes": mapping, "sector_members": members,
            "unresolved_names": sorted(set(wanted) - set(mapping)),
            "empty_member_names": sorted(name for name, values in members.items() if not values)}


def prepare_preopen_sector_evidence(candidate_date, market_date, *, web_data_dir=None,
                                    db_path=None, cache_dir=None, data_manager=None, allow_remote=True):
    from config.settings import CACHE_DIR, FACTOR_DB_PATH, TUSHARE_TOKEN
    from core.infrastructure.duckdb_access import connect
    from core.operations.preopen_evidence import PreopenEvidenceStore, cutoff, _stamp
    from core.realtime.trading_session import shanghai_now
    from core.signals.sector_confirmation import sector_names_from_row

    store = PreopenEvidenceStore(web_data_dir)
    bundle = store.load(candidate_date, market_date)
    if not bundle:
        return {"ok": False, "reason": "missing_preopen_bundle"}
    if _stamp(shanghai_now()) >= cutoff(market_date):
        return {"ok": False, "reason": "past_preopen_cutoff"}
    inputs = bundle["inputs"]
    rows = [*(inputs.get("candidates") or []), *(inputs.get("holdings") or [])]
    rows += [r for pool in (inputs.get("leader_pools") or {}).values() for r in pool.get("rows", [])]
    rows += [r for pool in (inputs.get("candidates_by_profile") or {}).values() for r in pool]
    names = set().union(*(sector_names_from_row(row) for row in rows)) if rows else set()
    if bundle.get("sector_supplement_id") and all(RealtimeSectorService.confirmation_members(inputs).get(name) for name in names):
        return {"ok": True, "existing": True, "bundle_id": bundle["bundle_id"]}
    cache = cache_dir or CACHE_DIR
    if data_manager is None and allow_remote:
        from core.data.data_manager_main import DataManager
        data_manager = DataManager(TUSHARE_TOKEN, cache)
    prepared = prepare_sector_cache(names, cache_dir=cache, data_manager=data_manager, allow_remote=allow_remote,
                                    as_of_date=candidate_date)
    with connect(db_path or FACTOR_DB_PATH, read_only=True) as con:
        daily = con.execute("SELECT * FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR)=?", [candidate_date]).fetchdf()
    import json
    records = json.loads(daily.to_json(orient="records", date_format="iso"))
    result = store.complete_sector_inputs(candidate_date, market_date,
        sector_codes=prepared["sector_codes"], sector_members=prepared["sector_members"], daily_rows=records)
    return {**result, "unresolved_names": prepared["unresolved_names"],
            "empty_member_names": prepared["empty_member_names"]}
