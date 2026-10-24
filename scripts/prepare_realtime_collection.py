"""Prepare and publish today's immutable collection plan before 09:25."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from backtest.trade_calendar import TradeCalendar
    from core.operations.preopen_evidence import PreopenEvidenceStore, freeze_preopen_inputs
    from core.realtime.collection_plan import DailyCollectionPlan
    from core.realtime.trading_session import shanghai_now
    parser = argparse.ArgumentParser(description="盘前整合个股、板块和策略输入")
    parser.add_argument("--market-date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--full", action="store_true", help="输出完整成分股列表")
    parser.add_argument("--tdx-boards", action="store_true", help="盘前准备独立通达信板块对照，仅用于研究")
    parser.add_argument("--board-mappings", type=Path, help="同花顺名称到通达信带市场代码的 JSON 对照文件")
    args = parser.parse_args()
    if args.board_mappings and not args.tdx_boards:
        parser.error("--board-mappings requires --tdx-boards")
    calendar = TradeCalendar()
    if not calendar.is_real:
        raise RuntimeError("缺少真实交易日历，禁止按工作日近似生成采集名单")
    if not calendar.is_trade_date(args.market_date):
        raise RuntimeError("非交易日，不生成采集名单")
    candidate = calendar.prev(args.market_date)
    store = PreopenEvidenceStore()
    existing = store.load(candidate, args.market_date)
    if not existing:
        from config.settings import WEB_DATA_DIR, SNAPSHOT_DIR
        required = [Path(WEB_DATA_DIR) / "fetch_status" / f"fetch_{candidate}.json",
                    Path(WEB_DATA_DIR) / "factor_status" / f"factors_{candidate}.json",
                    Path(WEB_DATA_DIR) / "screening" / "decision_pool" / f"decision_pool_{candidate}.json",
                    Path(SNAPSHOT_DIR) / f"{candidate}.json"]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("盘前输入缺失，禁止临时补取：" + "；".join(missing))
        from core.realtime.listing_evidence import capture_listing_evidence
        capture_listing_evidence(args.market_date)
    result = freeze_preopen_inputs(candidate, args.market_date)
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    bundle = store.load(candidate, args.market_date)
    plan = DailyCollectionPlan().freeze(bundle)
    board_summary = None
    if args.tdx_boards:
        from config.settings import ELTDX_TIMEOUT_SECONDS, ELTDX_HOST
        from core.data.providers.eltdx_provider import EltdxProvider
        from core.realtime.board_plan import BoardCollectionPlan
        hints = json.loads(args.board_mappings.read_text(encoding="utf-8-sig")) if args.board_mappings else {}
        boards = BoardCollectionPlan().prepare(plan,
            EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None), hints=hints, clock=shanghai_now)
        board_summary = {"plan_id": boards["plan_id"], "board_count": len(boards["codes"]),
                         "unmapped_count": sum(not row["candidates"] for row in boards["rows"]),
                         "formal_eligible": False, "warnings": boards["warnings"]}
    summary = plan if args.full else {**plan, "sectors": [
        {"name": row["name"], "code": row["code"], "member_count": len(row["members"])} for row in plan["sectors"]]}
    if board_summary is not None:
        summary = {**summary, "tdx_board_shadow": board_summary}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
