"""Isolated minute-rule diagnostics. Never authorise live trades or delivery."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from backtest.minute_entry import normalize_minute_bars
from backtest.trade_calendar import TradeCalendar
from core.data.minute_quality import inspect_minute_bars
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
    return {"market_date": date, "until": until, "codes": sorted(set(raw_codes)),
            "profile": profile, "lookback": lookback, "fetch_missing": fetch}


class _ReplayEntryService(RealtimeEntrySignalService):
    def __init__(self, previous, auctions, members, frames, profiles):
        # Explicit adapters isolate the shared rule evaluator from live providers/stores.
        super().__init__(amount_profile_repository=profiles,
                         signal_stats_repository=SimpleNamespace(get=lambda *a, **kw: {}),
                         snapshot_signal_service=SimpleNamespace(), sector_breadth_provider=SimpleNamespace())
        self.previous = previous
        self.auctions = auctions
        self.members = members
        self.frames = frames
        self.sector_minutes = {}

    def _auction(self, code, market_date):
        return self.auctions.get(code, {})

    def _snapshot_frame(self, code, market_date):
        return pd.DataFrame()

    def _sector_checker(self, row, code, all_rows, frames, quotes, market_date):
        names = tuple(sorted(self._sectors(row)))
        codes = sorted({c for name in names for c in self.members.get(name, [])})
        detail = {"sector_names": list(names), "member_count": len(codes), "coverage": 0.0,
                  "data_completeness": 0.0, "sector_confirmation_version": "minute_constituents_diagnostic_v1",
                  "sector_confirmation_source": "historical_constituent_minutes",
                  "reason": "缺少冻结的同花顺成分股分钟证据"}

        def changes(at_time, selected):
            result = {}
            for member in selected:
                if member == code:  # A stock cannot confirm its own sector synchrony.
                    continue
                data = self.frames.get(member, pd.DataFrame())
                prior = float(self.previous.get(member, {}).get("close") or 0)
                if data.empty or prior <= 0:
                    continue
                known = data[data["time"] == at_time]
                if not known.empty:
                    result[member] = (float(known.iloc[-1]["close"]) / prior - 1) * 100
            return result

        def checker(at_time):
            key = (code, names, str(at_time)[:8])
            if key in self.sector_minutes:
                state, observed_detail = self.sector_minutes[key]
                detail.update(observed_detail)
                return state
            eligible = [c for c in codes if c != code]
            observed = changes(str(at_time)[:8], eligible[:80])
            coverage = len(observed) / min(len(eligible), 80) if eligible else 0.0
            index_changes = []
            for name in names:
                group = [c for c in self.members.get(name, []) if c != code]
                values = changes(str(at_time)[:8], group)
                if group and len(values) / len(group) >= .6:
                    index_changes.append(sum(values.values()) / len(values))
            breadth = sum(v > 0 for v in observed.values()) / len(observed) if observed else None
            average = sum(observed.values()) / len(observed) if observed else None
            complete = coverage >= .6 and len(observed) >= 2 and bool(index_changes)
            state = bool(breadth >= .55 and average >= 0 and sum(index_changes) >= 0) if complete else None
            detail.update({"coverage": round(coverage, 4), "observed_members": len(observed),
                           "breadth": breadth, "average_change_pct": average,
                           "data_completeness": 1.0 if complete else 0.0,
                           "observed_minute": str(at_time),
                           "reason": "成分股分钟同步" if state else ("板块同步不足" if state is False else "板块分钟覆盖不足")})
            self.sector_minutes[key] = state, dict(detail)
            return state
        return checker, detail


def replay_minutes(*, request, candidate_date, rows, frames, previous, auctions=None,
                   members=None, profiles=None, input_basis="research", bundle_id="", progress=None):
    """Run the actual entry service against growing prefixes, with no live effects."""
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    date, until = request["market_date"], request["until"]
    profiles = profiles or MinuteAmountProfileRepository()
    normalized = {code: normalize_minute_bars(frame) for code, frame in frames.items()}
    service = _ReplayEntryService(previous, auctions or {}, members or {}, normalized, profiles)
    stocks, failures = [], []
    for index, source in enumerate(rows):
        row = dict(source)
        code = normalize_stock_code(row.get("code") or row.get("stock_code"), add_suffix=False)
        frame = normalized.get(code, pd.DataFrame())
        end = datetime.strptime(date + until, "%Y%m%d%H:%M:%S")
        quality = inspect_minute_bars(frames.get(code), date, expected_code=code,
                                      require_full_day=False, now=end)
        available = frame[frame["time"] < until[:5] + ":00"] if not frame.empty else frame
        prior = previous.get(code, {})
        failure = new_entry_exclusion(row)
        if available.empty or not quality["valid"]:
            failure = failure or quality["reason"] or "缺少已收盘分钟"
        if not prior.get("close"):
            failure = failure or "缺少候选日昨收基础数据"
        stock = {"code": code, "name": row.get("name", ""), "quality": quality,
                 "first_confirm_time": "", "first_evaluated_at": "", "transitions": []}
        if failure:
            stock.update({"status": "missing_evidence", "reason": failure})
            failures.append({"date": date, "code": code, "evidence": "candidate_minutes_or_identity", "reason": failure})
        else:
            from scripts.diagnose_intraday_signal import price_diagnostic
            stock["price_evidence"] = price_diagnostic(available, {"pre_close": prior["close"],
                "open": float(available.iloc[0]["open"])},
                str((RealtimeEntrySignalService._row_execution(row) or {}).get("confirmation_deadline") or "10:00:00"))
            last_key = None
            for count in range(1, len(available) + 1):
                prefix = available.iloc[:count]
                source_time = str(prefix.iloc[-1]["time"])
                evaluated_at = (datetime.strptime(source_time, "%H:%M:%S") + timedelta(minutes=1)).strftime("%H:%M:%S")
                # Derived bar values are replay context, never quotes with invented receive timestamps.
                quote = {"date": date, "time": evaluated_at, "pre_close": prior["close"],
                         "open_price": float(available.iloc[0]["open"]), "last_price": float(prefix.iloc[-1]["close"]),
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
            if (signal.get("sector_detail") or {}).get("data_completeness", 0) < 1:
                failures.append({"date": date, "code": code, "evidence": "sector_minutes",
                                 "reason": (signal.get("sector_detail") or {}).get("reason") or "板块分钟证据不足"})
            if str(signal.get("data_status") or "").startswith("missing_") and signal.get("data_status") != "missing_sector":
                failures.append({"date": date, "code": code, "evidence": signal["data_status"], "reason": signal["reason"]})
        stocks.append(stock)
        if progress:
            progress({"stage": "逐分钟确认回放", "completed": index + 1, "total": len(rows), "code": code})
    inputs = {"request": {k:v for k,v in request.items() if k != "fetch_missing"}, "candidate_date": candidate_date,
              "input_basis": input_basis, "rows": rows, "previous": previous,
              "auctions": auctions or {}, "members": members or {}, "bundle_id": bundle_id,
              "amount_profile": profiles.load(),
              "minutes": {c: json.loads(f.to_json(orient="records")) for c, f in sorted(frames.items())}}
    root = Path(__file__).resolve().parents[2]
    files = ["core/realtime/day_replay.py", "core/realtime/entry_signal_service.py", "backtest/minute_entry.py",
             "backtest/confirmation_quality.py", "backtest/reversal_entry.py", "core/data/minute_quality.py",
             "core/realtime/models.py", "core/signals/minute_amount_profile.py", "core/utils/stock_eligibility.py",
             "scripts/diagnose_intraday_signal.py"]
    versions = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    return clean({"classification": "minute_rule_diagnostic", "market_date": date, "candidate_date": candidate_date,
                  "until": until, "profile": request["profile"], "generated_at": shanghai_now().isoformat(),
                  "input_basis": input_basis, "bundle_id": bundle_id, "input_hash": digest(inputs),
                  "replay_inputs": clean(inputs),
                  "code_hash": digest(versions), "code_versions": versions,
                  "live_runtime_verified": False, "notification_sent": False, "account_modified": False,
                  "complete": bool(rows) and not failures, "failures": failures, "stocks": stocks,
                  "counts": dict(Counter(stock["status"] for stock in stocks)),
                  "confirmed_stocks": sum(bool(s["first_confirm_time"]) for s in stocks),
                  "limitations": ["分钟回放不重建历史 3 秒快照、租约、接收时间及采集空窗。",
                                  "补齐后的分钟不能证明当时及时到达；规则触发不代表实时门禁通过或微信送达。",
                                  "板块使用冻结成分股的分钟观测，不冒充当时的批量快照。",
                                  "当前代码与保存的输入用于代码回归，不是策略收益或真实成交回测。"]})


class DayReplayLoader:
    def __init__(self, *, web_data_dir=None, cache_dir=None, db_path=None, data_manager=None, calendar=None,
                 minute_cache=None):
        from config.settings import WEB_DATA_DIR, CACHE_DIR, FACTOR_DB_PATH
        self.root = Path(web_data_dir or WEB_DATA_DIR)
        self.cache = Path(cache_dir or CACHE_DIR)
        self.database = Path(db_path or FACTOR_DB_PATH)
        self.dm = data_manager
        self.calendar = calendar or TradeCalendar()
        self.minute_cache = minute_cache

    def run(self, request, progress=None):
        date = request["market_date"]
        candidate = self.calendar.prev(date)
        if not candidate or candidate >= date:
            raise ValueError("无法确定前一交易日；不能使用当天盘后候选")
        frozen = PreopenEvidenceStore(self.root).load(candidate, date)
        inputs = frozen["inputs"] if frozen else {}
        if request["profile"] == "leader_pool":
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
        if not rows:
            raise ValueError("该范围没有候选；指定股票必须属于候选日的对应池")
        previous = {}
        daily_path = self.cache / "stock" / "all_daily" / f"{candidate}.csv"
        daily = pd.read_csv(daily_path, dtype={"ts_code": str, "code": str}) if daily_path.exists() else pd.DataFrame()
        if daily.empty and self.database.exists():
            from core.infrastructure.duckdb_access import connect
            with connect(self.database, read_only=True) as con:
                daily = con.execute("SELECT * FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR)=?", [candidate]).fetchdf()
        for record in [*daily.to_dict("records"), *(inputs.get("daily_rows") or [])]:
            previous[normalize_stock_code(record.get("code") or record.get("ts_code"), add_suffix=False)] = clean(record)
        names = set().union(*(RealtimeEntrySignalService._sectors(r) for r in rows))
        members = {name: [normalize_stock_code(c, add_suffix=False) for c in (inputs.get("sector_members") or {}).get(name, [])]
                   for name in names}
        if not frozen:
            from core.realtime.sector_service import RealtimeSectorService
            service = RealtimeSectorService(cache_dir=self.cache)
            mapping = service.resolve_codes_by_names(names, source="ths")
            members = {name: service.member_codes(mapping[name]) if name in mapping else [] for name in names}
        codes = sorted({normalize_stock_code(r.get("code"), add_suffix=False) for r in rows}
                       | {c for group in members.values() for c in group})
        frames, auctions, minute_sources = {}, {}, {}
        if request["fetch_missing"] and self.dm is None:
            from core.data.data_manager_main import DataManager
            from config.settings import TUSHARE_TOKEN
            self.dm = DataManager(TUSHARE_TOKEN, self.cache)
        for index, code in enumerate(codes):
            ts_code = normalize_stock_code(code, add_suffix=True)
            path = self.cache / "stock" / "tick" / f"{ts_code}_{date}.csv"
            frame = pd.read_csv(path, dtype={"ts_code": str, "code": str}) if path.exists() else pd.DataFrame()
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
            if inspect_minute_bars(frame, date, expected_code=code, require_full_day=False, now=cutoff)["valid"]:
                frames[code] = frame
            elif any(normalize_stock_code(r.get("code"), add_suffix=False) == code for r in rows):
                frames[code] = frame
            if progress:
                progress({"stage": "检查分钟与成分股证据", "completed": index + 1, "total": len(codes), "code": code})
        for row in rows:
            code = normalize_stock_code(row.get("code"), add_suffix=False)
            ts_code = normalize_stock_code(code, add_suffix=True)
            path = self.cache / "stock" / "auction" / f"{ts_code}_{date}.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("数据源") in {"eltdx", "eltdx_0925", "eltdx_open", "minute_tick"}:
                    auctions[code] = cached
            elif request["fetch_missing"]:
                try:
                    auctions[code] = self.dm.get_auction_data(ts_code, date) or {}
                except Exception:
                    pass
        report = replay_minutes(request=request, candidate_date=candidate, rows=rows, frames=frames,
                                previous=previous, auctions=auctions, members=members,
                                input_basis="preopen_frozen" if frozen else "research_candidates_not_frozen",
                                bundle_id=frozen["bundle_id"] if frozen else "", progress=progress)
        from scripts.diagnose_intraday_signal import runtime_evidence
        from config.settings import APP_DB_PATH, HOLDING_DB_PATH
        selected = [normalize_stock_code(r.get("code"), add_suffix=False) for r in rows]
        report["recorded_runtime"] = {
            "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", date, selected),
            "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", date, selected)}
        if not frozen:
            report["limitations"].append("缺少真实盘前冻结包；候选及配置仅用于研究，不证明当时运行输入。")
            report["limitations"].append("板块名单使用本地真实同花顺缓存，不具备历史时点证明，仅用于代码回归。")
        report["limitations"].append("成交进度模型使用当前本地版本并记录输入哈希；未冻结的历史模型不证明当时使用版本。")
        report["requested_codes_not_in_pool"] = sorted(set(request["codes"]) - set(selected))
        report["minute_sources"] = minute_sources
        return report


def replay_saved_inputs(inputs, progress=None):
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    profiles = MinuteAmountProfileRepository()
    profiles._payload = inputs["amount_profile"]
    return replay_minutes(request={**inputs["request"], "fetch_missing": False},
                          candidate_date=inputs["candidate_date"], rows=inputs["rows"],
                          frames={c: pd.DataFrame(rows) for c, rows in inputs["minutes"].items()},
                          previous=inputs["previous"], auctions=inputs["auctions"], members=inputs["members"],
                          profiles=profiles, input_basis=inputs["input_basis"], bundle_id=inputs["bundle_id"], progress=progress)


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
