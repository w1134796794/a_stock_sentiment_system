"""Read already-produced historical pool membership without selecting stocks again."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from core.operations.preopen_evidence import PreopenEvidenceStore
from core.realtime.models import normalize_stock_code


def read_historical_pools(root, database, candidate_date, market_date):
    root, database = Path(root), Path(database)
    bundle = PreopenEvidenceStore(root).load(candidate_date, market_date)
    inputs = deepcopy(bundle["inputs"]) if bundle else {}
    if bundle:
        pool = (inputs.get("leader_pools") or {}).get("10")
        decisions = (inputs.get("candidates_by_profile") or {}).get("decision_pool", inputs.get("candidates"))
        if not isinstance(pool, dict) or not isinstance(pool.get("rows"), list) or not isinstance(decisions, list):
            raise ValueError(f"{candidate_date} 盘前归档缺少完整的龙头池或决策池名单；请同步已有归档")
        leaders = pool["rows"]
        basis = "preopen_frozen"
    else:
        path = root / "screening/decision_pool" / f"decision_pool_{candidate_date}.json"
        if not path.is_file():
            raise ValueError(f"缺少 {candidate_date} 已生成的决策池，请同步盘后产物；补数不会重新选股")
        decision = json.loads(path.read_text(encoding="utf-8"))
        if decision.get("trade_date") != candidate_date or not isinstance(decision.get("rows"), list):
            raise ValueError(f"{candidate_date} 决策池日期或名单格式不正确")
        decisions = decision["rows"]
        if not database.is_file():
            raise ValueError(f"缺少 {candidate_date} 历史龙头池档案；请同步已有盘后数据库")
        from core.infrastructure.duckdb_access import connect

        with connect(str(database), read_only=True) as con:
            if not con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='leader_signal_history'"
            ).fetchone():
                raise ValueError("缺少历史龙头池归档表；补数不会重算龙头身份")
            # These are the pool members already recorded by the postclose job,
            # not today's re-ranking or a backfill of earlier factor partitions.
            leaders = (
                con.execute(
                    "SELECT * FROM leader_signal_history WHERE CAST(signal_date AS VARCHAR)=? ORDER BY code",
                    [candidate_date],
                )
                .fetchdf()
                .to_dict("records")
            )
        if not leaders:
            raise ValueError(f"缺少 {candidate_date} 历史龙头池记录；请同步已有归档，不能假定为空池")
        basis = "postclose_pool_archive"
    rows = {}
    counts = {}
    for source, members in (("leader_pool", leaders), ("decision_pool", decisions)):
        codes = set()
        for member in members:
            if not isinstance(member, dict):
                raise ValueError(f"{candidate_date} {source} 名单格式不正确")
            code = normalize_stock_code(
                member.get("code") or member.get("stock_code") or member.get("代码") or "", add_suffix=False
            )
            if len(code) != 6 or not code.isdigit():
                raise ValueError(f"{candidate_date} {source} 含无效股票代码")
            codes.add(code)
            old = rows.get(code) or {}
            item = {**old, **deepcopy(member), "code": code}
            item["pool_sources"] = sorted(set(old.get("pool_sources", [])) | {source})
            item.setdefault("resonance_sectors", member.get("primary_sector") or "")
            if source == "leader_pool":
                item.setdefault("strategy_id", "leader_pool")
            rows[code] = item
        counts[source] = len(codes)
    return dict(
        market_date=market_date,
        candidate_date=candidate_date,
        rows=list(rows.values()),
        inputs=inputs,
        bundle_id=bundle["bundle_id"] if bundle else "",
        basis=basis,
        counts=counts,
    )
