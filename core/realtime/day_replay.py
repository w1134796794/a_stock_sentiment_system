"""Isolated minute-rule diagnostics. Never authorise live trades or delivery."""
from __future__ import annotations

from core.data.minute_cache_files import minute_cache_exists, read_minute_csv

import hashlib
import json
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from backtest.minute_entry import normalize_minute_bars
from backtest.trade_calendar import TradeCalendar
from core.data.minute_quality import inspect_minute_bars
from core.data.minute_clock import completed_minutes
from core.operations.preopen_evidence import PreopenEvidenceStore
from core.realtime.entry_signal_service import RealtimeEntrySignalService
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now
from core.utils.stock_eligibility import new_entry_exclusion


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def digest(value):
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True, ensure_ascii=False,
                                     default=str, allow_nan=False).encode()).hexdigest()


def replay_request(payload, *, now=None):
    replay_mode = payload.get("replay_mode") or "minute"
    if replay_mode not in {"minute", "archived_snapshot"}:
        raise ValueError("回放模式无效")
    clock = shanghai_now(now)
    date = str(payload.get("market_date") or clock.strftime("%Y%m%d")).replace("-", "")
    parsed = datetime.strptime(date, "%Y%m%d")
    if parsed.strftime("%Y%m%d") != date or date > clock.strftime("%Y%m%d"):
        raise ValueError("行情日无效或尚未发生")
    until = str(payload.get("until") or "15:01:00")
    until = datetime.strptime(until, "%H:%M:%S" if len(until) == 8 else "%H:%M").strftime("%H:%M:%S")
    if not "09:30:00" <= until <= "15:01:00":
        raise ValueError("回放结束时间应在 09:30 至 15:01")
    if date == clock.strftime("%Y%m%d"):
        until = min(until, clock.strftime("%H:%M:%S"))
    if replay_mode == "minute":
        until = until[:5] + ":00"
    if until < "09:30:00":
        raise ValueError("当天连续交易尚未开始")
    raw_codes = payload.get("codes") or []
    if isinstance(raw_codes, str):
        raw_codes = [v.strip() for v in raw_codes.replace("，", ",").split(",") if v.strip()]
    if not isinstance(raw_codes, list) or any(not re.fullmatch(r"\d{6}", str(v)) for v in raw_codes):
        raise ValueError("股票代码必须为六位数字，以逗号分隔")
    lookback = int(payload.get("lookback") or 10)
    if lookback not in (5, 10, 20):
        raise ValueError("龙头回看范围必须为 5、10 或 20 日")
    profile = str(payload.get("profile") or "leader_pool")
    if profile not in ("leader_pool", "decision_pool"):
        raise ValueError("请选择近期龙头池或生产决策池")
    if len(raw_codes) > 100:
        raise ValueError("一次最多指定 100 只股票")
    fetch = payload.get("fetch_missing", False)
    if not isinstance(fetch, bool):
        raise ValueError("fetch_missing 必须为布尔值")
    if replay_mode == "archived_snapshot" and (fetch or lookback != 10):
        raise ValueError("归档快照回放仅使用盘前冻结的10日龙头范围，禁止补取行情")
    request = {"market_date": date, "until": until, "codes": sorted(set(raw_codes)),
            "profile": profile, "lookback": lookback, "fetch_missing": fetch}
    if replay_mode != "minute":
        request["replay_mode"] = replay_mode
    return request


class _ReplayEntryService(RealtimeEntrySignalService):
    record_execution_timing = False
    snapshot_primary_enabled = False
    def __init__(self, previous, auctions, members, frames, profiles, opening_prices=None, *, evaluator=None,
                 opening_evidence=None, board_feedback_inputs=None, sector_quote_minutes=None, board_trend_inputs=None):
        # Explicit adapters isolate the shared rule evaluator from live providers/stores.
        super().__init__(evaluator=evaluator, amount_profile_repository=profiles,
                         signal_stats_repository=SimpleNamespace(get=lambda *a, **kw: {}),
                         snapshot_signal_service=SimpleNamespace(), sector_breadth_provider=SimpleNamespace())
        self.previous = previous
        self.auctions = auctions
        self.members = members
        self.frames = frames
        from core.signals.sector_sync import constituent_minute_prices
        self.minute_prices = constituent_minute_prices(frames, sector_quote_minutes)
        self.opening_prices = opening_prices or {}
        self.opening_evidence = opening_evidence or {}
        self.sector_minutes = {}
        self._feedback_inputs = board_feedback_inputs or {}
        self.board_trend_inputs = board_trend_inputs or {}

    def _auction(self, code, market_date):
        return self.auctions.get(code, {})

    def _snapshot_frame(self, code, market_date):
        return pd.DataFrame()

    def _sector_checker(self, row, code, all_rows, frames, quotes, market_date):
        if not self.evaluator.confirmation_policy.require_sector:
            return self._trend_checker(self._sectors(row), market_date)
        from core.signals.sector_sync import make_constituent_sync_checker
        from core.signals.opening import sector_reference_rows
        return make_constituent_sync_checker(
            code=code, names=self._sectors(row), members=self.members, frames=self.frames,
            previous=sector_reference_rows(self.previous, self.opening_evidence),
            opening_prices=lambda: self.opening_prices, cache=self.sector_minutes,
            minute_prices=self.minute_prices)


def replay_minutes(*, request, candidate_date, rows, frames, previous, auctions=None,
                   members=None, sector_codes=None, opening_prices=None, opening_price_source="",
                   opening_evidence=None, risk_config=None, board_feedback_inputs=None,
                   configuration_basis="", sector_quote_minutes=None, board_trend_inputs=None,
                   profiles=None, input_basis="research", bundle_id="", progress=None):
    """Run the actual entry service against growing prefixes, with no live effects."""
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    from core.signals.sector_sync import SECTOR_SYNC_VERSION, finite_number
    from core.realtime.sector_service import RealtimeSectorService
    supplied_names = {name for name, values in (members or {}).items() if values}
    members = RealtimeSectorService.confirmation_members({
        "sector_codes": sector_codes or {}, "sector_members": members or {}})
    excluded_sector_names = sorted(supplied_names - set(members))
    date, until = request["market_date"], request["until"]
    profiles = profiles or MinuteAmountProfileRepository()
    normalized = {code: normalize_minute_bars(frame) for code, frame in frames.items()}
    from core.signals.production_rules import entry_parameters, minute_evaluator, RULESET_VERSION
    from risk.risk_config import RiskConfig
    risk = RiskConfig.from_dict(risk_config) if risk_config is not None else RiskConfig.load()
    configuration_basis = configuration_basis or ("provided_config" if risk_config is not None else "current_config_not_frozen")
    service = _ReplayEntryService(previous, auctions or {}, members or {}, normalized, profiles, opening_prices,
                                  evaluator=minute_evaluator(risk), opening_evidence=opening_evidence,
                                  board_feedback_inputs=board_feedback_inputs, sector_quote_minutes=sector_quote_minutes,
                                  board_trend_inputs=board_trend_inputs)
    stocks, failures = [], []
    for index, source in enumerate(rows):
        row = dict(source)
        code = normalize_stock_code(row.get("code") or row.get("stock_code"), add_suffix=False)
        frame = normalized.get(code, pd.DataFrame())
        end = datetime.strptime(date + until, "%Y%m%d%H:%M:%S")
        quality = inspect_minute_bars(frames.get(code), date, expected_code=code,
                                      require_full_day=False, now=end)
        available = completed_minutes(frame, until)
        prior = previous.get(code, {})
        opening = finite_number((opening_prices or {}).get(code))
        evidence = (opening_evidence or {}).get(code) or {}
        from core.signals.opening import opening_context
        context = opening_context({**evidence, "open_price": opening}, prior)
        reference = context["pre_close"]
        failure = new_entry_exclusion(row)
        failure_evidence = "candidate_minutes_or_identity"
        if available.empty or not quality["valid"]:
            failure = failure or quality["reason"] or "缺少已收盘分钟"
        if reference is None:
            failure = failure or "缺少候选日昨收基础数据"
        if not failure and (opening is None or opening <= 0):
            failure = "缺少当日真实开盘价，不能用首个分钟价格代替或判断入场模式"
            failure_evidence = "session_open"
        stock = {"code": code, "name": row.get("name", ""), "quality": quality,
                 "session_open": opening if opening is not None and opening > 0 else None,
                 "opening_evidence": context,
                 "board_feedback": clean(service._board_feedback_context(code, date)),
                 "first_confirm_time": "", "first_evaluated_at": "", "transitions": []}
        if failure:
            stock.update({"status": "missing_evidence", "reason": failure})
            failures.append({"date": date, "code": code, "evidence": failure_evidence, "reason": failure})
        else:
            from scripts.diagnose_intraday_signal import price_diagnostic
            stock["price_evidence"] = price_diagnostic(available, {"pre_close": reference,
                "open": opening},
                str((RealtimeEntrySignalService._row_execution(row) or {}).get("confirmation_deadline") or service.evaluator.deadline),
                evaluator=service.evaluator)
            last_key = None
            for count in range(1, len(available) + 1):
                prefix = available.iloc[:count]
                source_time = str(prefix.iloc[-1]["time"])
                evaluated_at = (source_time if prefix.iloc[-1].get("bar_label") == "end" else
                    (datetime.strptime(source_time, "%H:%M:%S") + timedelta(minutes=1)).strftime("%H:%M:%S"))
                # Derived bar values are replay context, never quotes with invented receive timestamps.
                quote = {"date": date, "time": evaluated_at, "pre_close": reference,
                         "open_price": opening,
                         "opening_source": context["opening_source"] or opening_price_source,
                         "opening_received_at": context["received_at"],
                         "last_price": float(prefix.iloc[-1]["close"]),
                         "source": "historical_minute_context", "is_stale": False}
                signal = service._evaluate_one(row, quote, prefix, {}, {}, prior, date, rows, {})
                if signal.get("signal_status") == "confirmed" and not stock["first_confirm_time"]:
                    stock["first_confirm_time"] = signal.get("confirm_time", "")
                    stock["first_evaluated_at"] = evaluated_at
                key = (signal.get("signal_status"), signal.get("reason"), signal.get("confirm_time"), signal.get("entry_time"))
                if key != last_key:
                    stock["transitions"].append({"evaluated_at": evaluated_at, "source_minute": source_time,
                                                  **clean(signal)})
                    last_key = key
                if progress and count % 10 == 0:
                    progress({"stage": "逐分钟确认回放", "completed": index, "total": len(rows),
                              "code": code, "minute": evaluated_at, "minutes_completed": count,
                              "minutes_total": len(available)})
            stock.update({"status": signal["signal_status"], "reason": signal["reason"], "last_signal": clean(signal)})
            sector_detail = signal.get("sector_detail") or {}
            if service.evaluator.confirmation_policy.require_sector and (sector_detail.get("assessed") or signal.get("data_status") == "missing_sector") and sector_detail.get("data_completeness", 0) < 1:
                failures.append({"date": date, "code": code, "evidence": "sector_minutes",
                                 "reason": sector_detail.get("reason") or "板块分钟证据不足",
                                 "coverage": sector_detail.get("coverage", 0),
                                 "missing_minute_codes": sector_detail.get("missing_minute_codes", []),
                                 "missing_previous_close_codes": sector_detail.get("missing_previous_close_codes", []),
                                 "unresolved_sector_names": sector_detail.get("unresolved_sector_names", [])})
            if str(signal.get("data_status") or "").startswith("missing_") and signal.get("data_status") != "missing_sector":
                failures.append({"date": date, "code": code, "evidence": signal["data_status"], "reason": signal["reason"]})
        stocks.append(stock)
        if progress:
            progress({"stage": "逐分钟确认回放", "completed": index + 1, "total": len(rows), "code": code})
    inputs = {"request": {k:v for k,v in request.items() if k != "fetch_missing"}, "candidate_date": candidate_date,
              "input_basis": input_basis, "rows": rows, "previous": previous,
              "auctions": auctions or {}, "members": members or {}, "bundle_id": bundle_id,
              "sector_codes": {name: sector_codes[name] for name in members},
              "opening_prices": opening_prices or {}, "opening_price_source": opening_price_source,
              "opening_evidence": opening_evidence or {}, "risk_config": risk.to_dict(),
              "configuration_basis": configuration_basis,
              "board_feedback_inputs": board_feedback_inputs or {},
              "sector_quote_minutes": sector_quote_minutes or {},
              "board_trend_inputs": board_trend_inputs or {},
              "amount_profile": profiles.load(),
              "minutes": {c: json.loads(f.to_json(orient="records")) for c, f in sorted(frames.items())}}
    root = Path(__file__).resolve().parents[2]
    files = ["core/realtime/day_replay.py", "core/realtime/entry_signal_service.py", "backtest/minute_entry.py",
             "backtest/confirmation_quality.py", "backtest/reversal_entry.py", "core/data/minute_quality.py",
             "core/realtime/models.py", "core/signals/minute_amount_profile.py", "core/utils/stock_eligibility.py",
             "scripts/diagnose_intraday_signal.py", "core/realtime/sector_service.py", "core/realtime/sector_evidence.py",
             "core/signals/sector_sync.py", "core/realtime/sector_breadth.py", "core/signals/opening.py",
             "core/realtime/session_open_repository.py", "core/realtime/sector_quote_minutes.py", "core/signals/production_rules.py",
             "core/signals/board_feedback.py", "core/realtime/board_feedback_repository.py", "risk/risk_config.py",
             "core/realtime/runtime_diagnostics.py", "core/operations/evidence.py",
             "core/realtime/snapshot_signal_service.py", "core/realtime/read_only_market.py",
             "core/realtime/collection_plan.py", "core/realtime/strategy_runtime.py",
             "core/realtime/listing_evidence.py", "core/realtime/market_events.py"]
    files += ["core/signals/sector_trend.py", "core/realtime/board_plan.py", "core/realtime/board_quote_store.py"]
    versions = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    return clean({"classification": "minute_rule_diagnostic", "market_date": date, "candidate_date": candidate_date,
                  "until": until, "profile": request["profile"], "generated_at": shanghai_now().isoformat(),
                  "input_basis": input_basis, "bundle_id": bundle_id, "input_hash": digest(inputs),
                  "replay_inputs": clean(inputs),
                  "code_hash": digest(versions), "code_versions": versions,
                  "live_runtime_verified": False, "notification_sent": False, "account_modified": False,
                  "complete": bool(rows) and not failures, "failures": failures, "stocks": stocks,
                  "factor_warnings": [{"code": stock["code"], "factor": "three_board_negative_feedback",
                                       "status": stock["board_feedback"]["factor_status"],
                                       "reason": stock["board_feedback"]["reason"]}
                                      for stock in stocks if stock["board_feedback"]["active"]
                                      or not stock["board_feedback"]["assessed"]],
                  "counts": dict(Counter(stock["status"] for stock in stocks)),
                  "confirmed_stocks": sum(bool(s["first_confirm_time"]) for s in stocks),
                  "sector_confirmation_policy": SECTOR_SYNC_VERSION,
                  "opening_price_source": opening_price_source,
                  "configuration_basis": configuration_basis,
                  "entry_parameters": entry_parameters(service.evaluator), "ruleset_version": RULESET_VERSION,
                  "excluded_sector_names": excluded_sector_names,
                  "limitations": ["分钟回放不重建历史 3 秒快照、租约、接收时间及采集空窗。",
                                  "补齐后的分钟不能证明当时及时到达；规则触发不代表实时门禁通过或微信送达。",
                                  "板块使用冻结成分股的分钟观测或实际采集报价的分钟价格留档，不冒充完整3秒快照或成交K线。",
                                  "当前代码与保存的输入用于代码回归，不是策略收益或真实成交回测。",
                                  "板块确认仅使用绑定到同花顺8开头代码的成分；700分类及无代码绑定的旧成分不参与。",
                                  "候选缺少真实开盘价时标记缺证据，不用首个分钟价格推断开盘分层。",
                                  "开盘走强要求真实开盘价、同组成分股与3分钟前的分钟；日线仅取开盘字段，不取当日收盘或最高价。",
                                  "盘后取得的当日开盘字段只用于规则诊断，不证明盘中及时到达。"]})


class DayReplayLoader:
    def __init__(self, *, web_data_dir=None, cache_dir=None, db_path=None, data_manager=None, calendar=None,
                 minute_cache=None, opening_repository=None, snapshot_repository=None):
        from config.settings import WEB_DATA_DIR, CACHE_DIR, FACTOR_DB_PATH
        self.root = Path(web_data_dir or WEB_DATA_DIR)
        self.cache = Path(cache_dir or CACHE_DIR)
        self.database = Path(db_path or FACTOR_DB_PATH)
        self.dm = data_manager
        self.calendar = calendar
        self.minute_cache = minute_cache
        self.openings = opening_repository
        self.snapshot_repository = snapshot_repository
        self.opening_evidence = {}

    def _opening_evidence(self, date, codes, *, fetch_missing, until="23:59:59", progress=None):
        if self.openings is None:
            from core.realtime.session_open_repository import SessionOpenRepository
            self.openings = SessionOpenRepository(self.cache)
        from core.signals.opening import positive_price
        from core.realtime.session_open_repository import quote_opening
        from core.realtime.quote_cache import RealtimeQuoteCache
        wanted, records = set(codes), {}

        def accept_quotes(rows, source, cutoff=until):
            for row in rows:
                evidence = quote_opening(row, date, until=cutoff)
                if evidence and evidence["code"] in wanted and evidence["code"] not in records:
                    records[evidence["code"]] = {**evidence, "source": source}

        archived = self.openings.read(date, codes, until=until)
        accept_quotes(({**row, "date": date} for row in archived.values()), "eltdx_open_archive")
        missing = wanted - records.keys()
        if missing:
            latest = self.openings.backend.get_many_json([RealtimeQuoteCache._quote_key(c) for c in sorted(missing)])
            accept_quotes((row for row in latest.values() if isinstance(row, dict)), "eltdx_quote_cache")
        if wanted - records.keys():
            from core.realtime.snapshot_repository import RealtimeSnapshotRepository
            snapshots = self.snapshot_repository or RealtimeSnapshotRepository()
            for code in sorted(wanted - records.keys()):
                accept_quotes(snapshots.read(date, code), "eltdx_snapshot_cache")
        if (fetch_missing and date == shanghai_now().strftime("%Y%m%d")
                and shanghai_now().strftime("%H:%M:%S") >= "09:30:00"
                and callable(getattr(self.dm, "get_quote_snapshots", None)) and wanted - records.keys()):
            try:
                quotes = self.dm.get_quote_snapshots(sorted(wanted - records.keys())) or {}
                accept_quotes(quotes.values(), "eltdx_quote_posthoc", cutoff="23:59:59")
            except Exception:
                pass

        def accept_daily(frame, source):
            if frame is None or frame.empty or not {"open", "trade_date"}.issubset(frame.columns):
                return
            for row in frame.to_dict("records"):
                code = normalize_stock_code(row.get("code") or row.get("ts_code"), add_suffix=False)
                opening = positive_price(row.get("open"))
                if (code in wanted and code not in records and opening is not None
                        and str(row["trade_date"]).replace("-", "")[:8] == date):
                    records[code] = {"code": code, "market_date": date, "open_price": opening,
                                     "pre_close": positive_price(row.get("pre_close")), "source": source,
                                     "received_at": ""}

        path = self.cache / "stock" / "all_daily" / f"{date}.csv"
        if wanted - records.keys() and path.exists():
            accept_daily(pd.read_csv(path, dtype={"code": str, "ts_code": str}), "stock_daily_open_cache")
        if wanted - records.keys() and self.database.exists():
            from core.infrastructure.duckdb_access import connect
            try:
                with connect(self.database, read_only=True) as con:
                    frame = con.execute("SELECT code, trade_date, open, pre_close FROM stock_daily_silver "
                        "WHERE CAST(trade_date AS VARCHAR)=?", [date]).fetchdf()
                accept_daily(frame, "stock_daily_silver_open")
            except Exception:
                pass
        if wanted - records.keys() and fetch_missing and getattr(self.dm, "prefer_axdata_history", False):
            missing = [code for code in codes if code not in records]
            for index, code in enumerate(missing):
                if progress:
                    progress({"stage": "补取真实开盘价", "completed": index, "total": len(missing), "code": code})
                try:
                    frame = self.dm.get_stock_daily(code, date, date)
                    source = "axdata_daily_open_posthoc" if not frame.empty and "source" in frame and frame["source"].eq("axdata").all() else "stock_daily_open_posthoc"
                    accept_daily(frame, source)
                except Exception:
                    pass
        if wanted - records.keys() and fetch_missing and callable(getattr(self.dm, "get_all_stocks_daily", None)):
            try:
                accept_daily(self.dm.get_all_stocks_daily(date), "tushare_daily_open_posthoc")
            except Exception:
                pass
        self.opening_evidence = records
        source = "+".join(sorted({row["source"] for row in records.values()})) or "missing_real_session_open"
        return {code: row["open_price"] for code, row in records.items()}, source

    def run(self, request, progress=None, *, inputs_only=False, extra_codes=(), extra_rows=(), pool_inputs=None, rule_risk=None):
        if request.get("replay_mode") == "archived_snapshot":
            from core.realtime.snapshot_replay import run_snapshot_replay
            return run_snapshot_replay(self.root, request, progress)
        self.calendar = self.calendar or TradeCalendar()
        date = request["market_date"]
        candidate = self.calendar.prev(date)
        if not candidate or candidate >= date:
            raise ValueError("无法确定前一交易日；不能使用当天盘后候选")
        frozen = (PreopenEvidenceStore(self.root).load(candidate, date) if pool_inputs is None else
                  {"inputs": pool_inputs["inputs"], "bundle_id": pool_inputs["bundle_id"]} if pool_inputs.get("bundle_id") else None)
        inputs = frozen["inputs"] if frozen else {}
        from risk.risk_config import RiskConfig
        risk = RiskConfig.from_dict(rule_risk or inputs.get("risk_config")) if rule_risk or inputs.get("risk_config") else RiskConfig.load()
        breadth_required = risk.entry_sector_mode == "breadth_required"
        if pool_inputs is not None:
            if pool_inputs.get("candidate_date") != candidate or pool_inputs.get("market_date") != date:
                raise ValueError("历史池名单与行情交易日不匹配")
            rows = deepcopy(pool_inputs["rows"])
        elif request["profile"] == "production":
            if not frozen:
                from config.settings import SNAPSHOT_DIR
                snapshot = Path(SNAPSHOT_DIR) / f'{candidate}.json'
                decision = self.root / 'screening' / 'decision_pool' / f'decision_pool_{candidate}.json'
                if not snapshot.exists() or not decision.exists():
                    raise ValueError(f'缺少候选日 {candidate} 快照或决策池，不能假定空候选继续')
            from core.signals.production_rules import production_candidates
            if not frozen:
                from core.realtime.leader_pool_service import LeaderPoolService
                from core.realtime.overlay_service import RealtimeOverlayService
                from core.screening.strategy_profiles import StrategyProfileRepository
                inputs = {"leader_pools": {"10": LeaderPoolService(
                    screening_dir=self.root / "screening", duckdb_path=self.database).build_pool(
                        candidate, lookback=10, limit=30)},
                    "candidates": RealtimeOverlayService(screening_dir=self.root / "screening").candidate_rows(
                        candidate, profile="decision_pool", limit=20),
                    "profiles": StrategyProfileRepository().list_profiles()}
            rows = production_candidates(inputs)
        elif request["profile"] == "leader_pool":
            if frozen:
                rows = (inputs.get("leader_pools", {}).get(str(request["lookback"]), {}).get("rows") or [])[:30]
            else:
                from core.realtime.leader_pool_service import LeaderPoolService
                rows = LeaderPoolService(screening_dir=self.root / "screening", duckdb_path=self.database).build_pool(
                    candidate, lookback=request["lookback"], limit=30).get("rows") or []
        elif frozen:
            rows = (inputs.get("candidates_by_profile") or {}).get("decision_pool", inputs.get("candidates", []))
        else:
            from core.realtime.overlay_service import RealtimeOverlayService
            rows = RealtimeOverlayService(screening_dir=self.root / "screening").candidate_rows(candidate, profile="decision_pool", limit=10000)
        if request["codes"]:
            rows = [r for r in rows if normalize_stock_code(r.get("code"), add_suffix=False) in request["codes"]]
        if not rows and not inputs_only:
            raise ValueError("该范围没有候选；指定股票必须属于候选日的对应池")
        if frozen and request['profile'] != 'production':
            from core.signals.production_rules import production_context
            rows = production_context(rows, inputs)
        previous = {}
        daily_path = self.cache / "stock" / "all_daily" / f"{candidate}.csv"
        daily = pd.read_csv(daily_path, dtype={"ts_code": str, "code": str}) if daily_path.exists() else pd.DataFrame()
        if daily.empty and self.database.exists():
            from core.infrastructure.duckdb_access import connect
            with connect(self.database, read_only=True) as con:
                daily = con.execute("SELECT * FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR)=?", [candidate]).fetchdf()
        for record in [*daily.to_dict("records"), *(inputs.get("daily_rows") or [])]:
            previous[normalize_stock_code(record.get("code") or record.get("ts_code"), add_suffix=False)] = clean(record)
        context_rows = [*rows, *extra_rows]
        names = set().union(*(RealtimeEntrySignalService._sectors(r) for r in context_rows)) if context_rows else set()
        from core.realtime.sector_service import RealtimeSectorService
        verified_members = RealtimeSectorService.confirmation_members(inputs)
        sector_codes = inputs.get("sector_codes") or {}
        members = {name: [normalize_stock_code(c, add_suffix=False) for c in verified_members.get(name, [])]
                   for name in names}
        if not frozen and breadth_required:
            if request["fetch_missing"] and self.dm is None:
                from core.data.data_manager_main import DataManager
                from config.settings import TUSHARE_TOKEN
                self.dm = DataManager(TUSHARE_TOKEN, self.cache)
            from core.realtime.sector_evidence import prepare_sector_cache
            prepared = prepare_sector_cache(names, cache_dir=self.cache, data_manager=self.dm,
                                            allow_remote=request["fetch_missing"], as_of_date=candidate)
            members = {name: prepared["sector_members"].get(name, []) for name in names}
            sector_codes = prepared["sector_codes"]
        if not breadth_required:
            members, sector_codes = {}, {}
        from core.signals.sector_trend import archived_trend_inputs
        board_trend_inputs = archived_trend_inputs(self.root, date) if not breadth_required else {}
        primary = {normalize_stock_code(r.get("code"), add_suffix=False) for r in rows} | set(extra_codes)
        # Pool securities must not wait behind thousands of sector constituents.
        codes = sorted(primary) + sorted({c for group in members.values() for c in group} - primary)
        from core.realtime.board_feedback_repository import BoardFeedbackRepository
        feedback_codes = [normalize_stock_code(row.get("code"), add_suffix=False) for row in context_rows]
        feedback_codes.extend(extra_codes)
        board_feedback_inputs = BoardFeedbackRepository(self.cache, db_path=self.database, calendar=self.calendar).capture(
            date, feedback_codes, frozen_inputs=inputs if frozen else None)
        frames, auctions, minute_sources = {}, {}, {}
        if request["fetch_missing"] and self.dm is None:
            from core.data.data_manager_main import DataManager
            from config.settings import TUSHARE_TOKEN
            self.dm = DataManager(TUSHARE_TOKEN, self.cache)
        from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
        sector_quote_minutes = SectorQuoteMinuteRepository(self.cache).read_prices(date, codes, until=request["until"])
        acquisition = {"success": 0, "failed": 0, "skipped": 0, "pool_success": 0,
                       "pool_skipped": 0, "pool_total": len(primary),
                       "total": len(codes), "failures": [], "skips": []}
        skipped_codes = set()
        for index, code in enumerate(codes):
            if progress:
                progress({"stage": "补取池内分钟" if code in primary else "补取板块成分分钟",
                          "completed": index, "code": code, **{k: v for k, v in acquisition.items() if k not in {"failures", "skips"}}})
            ts_code = normalize_stock_code(code, add_suffix=True)
            if request["fetch_missing"]:
                from core.data.data_manager_suspension import suspension_status
                status = suspension_status(self.dm, code, date, allow_remote=False)
                if status.get("state") == "full_day" and status.get("trade_date") == date:
                    skipped_codes.add(code)
                    acquisition["skipped"] += 1
                    acquisition["pool_skipped"] += int(code in primary)
                    result = {"code": code, "valid": False, "rows": 0, "source": status.get("source"),
                              "state": "full_day", "reason": status.get("reason") or "已有证据确认全天停牌"}
                    acquisition["skips"].append(result)
                    if progress:
                        progress({"stage": "跳过已确认全天停牌", "completed": index + 1, "code": code,
                                  "last_result": result, **{k: v for k, v in acquisition.items() if k not in {"failures", "skips"}}})
                    continue
            path = self.cache / "stock" / "tick" / f"{ts_code}_{date}.csv"
            frame = read_minute_csv(path, dtype={"ts_code": str, "code": str}) if minute_cache_exists(path) else pd.DataFrame()
            cutoff = datetime.strptime(date + request["until"], "%Y%m%d%H:%M:%S")
            quality = inspect_minute_bars(frame, date, expected_code=code, require_full_day=False, now=cutoff)
            minute_sources[code] = "local_csv"
            if not quality["valid"]:
                if self.minute_cache is None:
                    from core.realtime.minute_cache import RealtimeMinuteCache
                    self.minute_cache = RealtimeMinuteCache()
                cached = self.minute_cache.read(date, code)
                if inspect_minute_bars(cached, date, expected_code=code, require_full_day=False, now=cutoff)["valid"]:
                    frame = cached
                    quality["valid"] = True
                    minute_sources[code] = "collector_minute_cache"
            if request["fetch_missing"] and not quality["valid"]:
                try:
                    frame = self.dm.get_stock_tick(ts_code, date)
                    minute_sources[code] = "data_manager_history"
                except Exception:
                    frame = pd.DataFrame()
            # Invalid constituents are absent, not interpolated or forward filled.
            checked = inspect_minute_bars(frame, date, expected_code=code, require_full_day=False, now=cutoff)
            reason, source = checked["reason"], minute_sources[code]
            if checked["valid"]:
                frames[code] = frame
                acquisition["success"] += 1
                acquisition["pool_success"] += int(code in primary)
                if "source" in frame and not frame.empty:
                    source = "+".join(sorted(set(frame["source"].dropna().astype(str)))) or source
            else:
                acquisition["failed"] += 1
                report = getattr(self.dm, "get_minute_quality_report", lambda *_: {})(ts_code, date) or {}
                reason = report.get("reason") or reason
                acquisition["failures"].append({"code": code, "reason": reason, "attempts": report.get("attempts", [])})
                if code in primary:
                    frames[code] = frame
            result = {"code": code, "valid": checked["valid"], "rows": len(frame), "source": source, "reason": reason}
            if request["fetch_missing"] and (code in primary or not checked["valid"] or (index + 1) % 50 == 0):
                from loguru import logger
                logger.info("[历史补数] {} {} {}/{} rows={} source={} reason={}", date, code, index + 1,
                            len(codes), len(frame), source, reason or "通过")
            if progress:
                progress({"stage": "补取池内分钟" if code in primary else "补取板块成分分钟",
                          "completed": index + 1, "code": code, "last_result": result,
                          **{k: v for k, v in acquisition.items() if k not in {"failures", "skips"}}})
        if progress:
            progress({"stage": "读取真实开盘价", "completed": 0, "total": len(codes)})
        opening_prices, opening_price_source = self._opening_evidence(
            date, [c for c in codes if c not in skipped_codes], fetch_missing=request["fetch_missing"],
            until=request["until"], progress=progress)
        for index, row in enumerate(rows):
            code = normalize_stock_code(row.get("code"), add_suffix=False)
            if progress:
                progress({"stage": "补取竞价证据", "completed": index, "total": len(rows), "code": code})
            if code in skipped_codes:
                continue
            ts_code = normalize_stock_code(code, add_suffix=True)
            path = self.cache / "stock" / "auction" / f"{ts_code}_{date}.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("数据源") in {"eltdx", "eltdx_0925", "eltdx_open", "minute_tick", "axdata_0925"}:
                    auctions[code] = cached
            elif request["fetch_missing"]:
                try:
                    auctions[code] = self.dm.get_auction_data(ts_code, date) or {}
                except Exception:
                    pass
        if inputs_only:
            return {"rows": rows, "frames": frames, "previous": previous, "auctions": auctions,
                    "members": members, "sector_codes": sector_codes, "opening_prices": opening_prices,
                    "opening_price_source": opening_price_source, "bundle_id": frozen["bundle_id"] if frozen else "",
                    "opening_evidence": self.opening_evidence,
                    "board_feedback_inputs": board_feedback_inputs,
                    "sector_quote_minutes": sector_quote_minutes,
                    "risk_config": risk.to_dict(),
                    "board_trend_inputs": board_trend_inputs,
                    "minute_amount_profile": inputs.get("minute_amount_profile") if frozen else None,
                    "minute_acquisition": acquisition,
                    "input_basis": "preopen_frozen" if frozen else "research_candidates_not_frozen"}
        report = replay_minutes(request=request, candidate_date=candidate, rows=rows, frames=frames,
                                previous=previous, auctions=auctions, members=members,
                                sector_codes=sector_codes,
                                opening_prices=opening_prices, opening_price_source=opening_price_source,
                                opening_evidence=self.opening_evidence, risk_config=risk.to_dict(),
                                board_trend_inputs=board_trend_inputs,
                                board_feedback_inputs=board_feedback_inputs,
                                sector_quote_minutes=sector_quote_minutes,
                                configuration_basis="preopen_frozen" if inputs.get("risk_config") else "current_config_not_frozen",
                                input_basis="preopen_frozen" if frozen else "research_candidates_not_frozen",
                                bundle_id=frozen["bundle_id"] if frozen else "", progress=progress)
        from scripts.diagnose_intraday_signal import runtime_evidence
        from config.settings import APP_DB_PATH, HOLDING_DB_PATH
        selected = [normalize_stock_code(r.get("code"), add_suffix=False) for r in rows]
        report["recorded_runtime"] = {
            "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", date, selected),
            "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", date, selected)}
        from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
        attach_runtime_diagnostics(report)
        if not frozen:
            report["limitations"].append("缺少真实盘前冻结包；候选及配置仅用于研究，不证明当时运行输入。")
            report["limitations"].append("板块名单使用本地真实同花顺缓存，不具备历史时点证明，仅用于代码回归。")
        report["limitations"].append("成交进度模型使用当前本地版本并记录输入哈希；未冻结的历史模型不证明当时使用版本。")
        report["requested_codes_not_in_pool"] = sorted(set(request["codes"]) - set(selected))
        report["minute_sources"] = minute_sources
        report["sector_quote_minute_codes"] = sorted(sector_quote_minutes)
        from core.realtime.board_replay import attach_board_replay
        return attach_board_replay(report, self.root)


def replay_saved_inputs(inputs, progress=None):
    if inputs.get("replay_mode") == "archived_snapshot":
        from core.realtime.snapshot_replay import replay_snapshot_inputs
        return replay_snapshot_inputs(inputs, progress)
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    profiles = MinuteAmountProfileRepository()
    profiles._payload = inputs["amount_profile"]
    report = replay_minutes(request={**inputs["request"], "fetch_missing": False},
                          candidate_date=inputs["candidate_date"], rows=inputs["rows"],
                          frames={c: pd.DataFrame(rows) for c, rows in inputs["minutes"].items()},
                          previous=inputs["previous"], auctions=inputs["auctions"], members=inputs["members"],
                          sector_codes=inputs.get("sector_codes") or {},
                          opening_prices=inputs.get("opening_prices") or {},
                          opening_price_source=inputs.get("opening_price_source") or "",
                          board_trend_inputs=inputs.get("board_trend_inputs") or {},
                          opening_evidence=inputs.get("opening_evidence") or {}, risk_config=inputs.get("risk_config"),
                          board_feedback_inputs=inputs.get("board_feedback_inputs") or {},
                          sector_quote_minutes=inputs.get("sector_quote_minutes") or {},
                          configuration_basis=inputs.get("configuration_basis") or "",
                          profiles=profiles, input_basis=inputs["input_basis"], bundle_id=inputs["bundle_id"], progress=progress)
    if "board_shadow_inputs" in inputs:
        from core.realtime.board_replay import replay_board_inputs
        report["board_shadow"] = replay_board_inputs(inputs["board_shadow_inputs"])
        report["replay_inputs"]["board_shadow_inputs"] = inputs["board_shadow_inputs"]
    return report


def compare_reports(before, after):
    same = before.get("input_hash") == after.get("input_hash")
    old = {s["code"]: s for s in before.get("stocks", [])}
    changes = []
    for stock in after.get("stocks", []):
        previous = old.get(stock["code"], {})
        keys = ("status", "first_confirm_time", "first_evaluated_at", "reason")
        if any(previous.get(k) != stock.get(k) for k in keys):
            changes.append({"code": stock["code"], "before": {k: previous.get(k) for k in keys},
                            "after": {k: stock.get(k) for k in keys}})
    return {"comparable": same, "reason": "同输入代码回归" if same else "输入版本不同，不能把差异归因于代码",
            "before_code_hash": before.get("code_hash"), "after_code_hash": after.get("code_hash"), "changes": changes}
