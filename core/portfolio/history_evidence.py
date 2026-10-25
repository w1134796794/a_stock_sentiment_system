"""Sealed production-rule inputs for historical minute research, never live delivery."""

from __future__ import annotations

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pandas as pd

from core.operations.replay_research import FrozenCalendar, clean, digest, implementation_version


def code_version():
    root = Path(__file__).resolve().parents[2]
    files = (
        "core/portfolio/history_evidence.py",
        "core/portfolio/history_workflow.py",
        "core/realtime/day_replay.py",
        "core/realtime/snapshot_replay.py",
        "core/realtime/runtime_diagnostics.py",
        "core/signals/production_rules.py",
        "core/data/providers/axdata_provider.py",
        "core/portfolio/historical_pools.py",
        "core/realtime/board_plan.py",
        "core/realtime/board_quote_store.py",
    )
    return digest(
        {
            "engine": implementation_version(),
            **{name: hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in files},
        }
    )


def write_packet(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json.dumps(clean(value), ensure_ascii=False, allow_nan=False).encode(), mtime=0)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_bytes(raw)
    pending.replace(path)
    return hashlib.sha256(raw).hexdigest()


def read_packet(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def pack_inputs(inputs):
    return {
        **inputs,
        "frames": {
            code: json.loads(frame.to_json(orient="records", double_precision=15))
            for code, frame in inputs["frames"].items()
        },
    }


def unpack_inputs(inputs):
    return {**deepcopy(inputs), "frames": {code: pd.DataFrame(rows) for code, rows in inputs["frames"].items()}}


class FrozenHistory:
    allow_remote_history = False
    allow_remote_minutes = False

    def __init__(self, directory):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = self.manifest
        if manifest.get("contract_hash") != digest({k: v for k, v in manifest.items() if k != "contract_hash"}):
            raise ValueError("冻结合同被修改")
        if manifest["code_hash"] != code_version():
            raise ValueError("执行代码已变化，需要重新校验冻结；不能续跑旧版本")
        for name, expected in manifest["files"].items():
            path = (self.directory / name).resolve()
            if (
                not path.is_relative_to(self.directory.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError(f"冻结文件校验失败: {name}")
        self.calendar = FrozenCalendar(manifest["calendar"])
        self.production_loader = self
        self._date, self._inputs = "", {}

    def day(self, date):
        if date not in self.manifest["dates"]:
            return {}
        if self._date != date:
            self._inputs = read_packet(self.directory / "days" / f"{date}.json.gz")
            self._date = date
        return self._inputs

    def run(self, request, *, inputs_only=False, extra_codes=(), **_):
        if not inputs_only or request.get("fetch_missing"):
            raise ValueError("冻结加载器只允许读取已封存的生产输入")
        day = self.day(request["market_date"])
        if not day:
            raise ValueError("冻结包缺少请求交易日")
        missing = set(extra_codes) - set(day["daily"])
        if missing:
            raise ValueError(f"冻结包未覆盖持仓: {sorted(missing)}")
        return unpack_inputs(day)

    def get_stock_tick(self, code, date, **_):
        return pd.DataFrame(self.day(date).get("frames", {}).get(str(code).split(".")[0].zfill(6), []))

    def get_stock_daily_data(self, code, date):
        code = str(code).split(".")[0].zfill(6)
        day = self.day(date)
        if day:
            return deepcopy(day.get("daily", {}).get(code, {}))
        # The first trading day's T-1 references are sealed inside that packet.
        for target in self.manifest["dates"]:
            if self.calendar.prev(target) == date:
                return deepcopy(self.day(target).get("previous", {}).get(code, {}))
        return {}

    def get_auction_data(self, code, date):
        return deepcopy(self.day(date).get("auctions", {}).get(str(code).split(".")[0].zfill(6), {}))

    def get_suspension_status(self, code, date, **_):
        return deepcopy(
            self.day(date).get("suspensions", {}).get(str(code).split(".")[0].zfill(6), {"state": "unknown"})
        )

    def get_minute_quality_report(self, code, date):
        return {"reason": "冻结包缺少分钟；请返回独立补数阶段并创建新冻结版本"}


def audit_day(day, date, potential_rows):
    """Check evidence, not whether prices are bullish or a buy is confirmed."""
    from backtest.evidence_policy import requires_auction
    from core.data.minute_quality import inspect_minute_bars
    from core.realtime.entry_signal_service import RealtimeEntrySignalService as Signals
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    from core.signals.opening import sector_reference_rows
    from core.signals.sector_sync import constituent_minute_prices, make_constituent_sync_checker

    failures = []
    from risk.risk_config import RiskConfig

    breadth_required = RiskConfig.from_dict(day.get("risk_config")).entry_sector_mode == "breadth_required"
    frames = {code: pd.DataFrame(rows) for code, rows in day["frames"].items()}
    prices = constituent_minute_prices(frames, day.get("sector_quote_minutes"))
    previous = sector_reference_rows(day["previous"], day["opening_evidence"])
    profile = MinuteAmountProfileRepository.__new__(MinuteAmountProfileRepository)
    profile._payload = day.get("minute_amount_profile") or {}
    active = {row["code"]: row for row in day["rows"]}
    samples = []
    for hour, first, last in ((9, 36, 59), (10, 0, 59), (11, 0, 30), (13, 4, 59), (14, 0, 59), (15, 0, 0)):
        samples.extend(f"{hour:02}:{minute:02}:00" for minute in range(first, last + 1))
    for code, row in potential_rows.items():

        def missing(kind, reason, code=code, **detail):
            failures.append({"trade_date": date, "code": code, "evidence": kind, "reason": reason, **detail})

        status = day.get("suspensions", {}).get(code, {}).get("state")
        if status == "full_day":
            continue
        if status not in {"trading", "normal", "not_suspended"}:
            missing("suspension", "停复牌状态未知或存在盘中停牌，不能假定可成交")
        daily = day["daily"].get(code) or {}
        if not float(daily.get("close") or 0) > 0:
            missing("daily", "缺少当日真实收盘价，不能估值")
        quality = inspect_minute_bars(frames.get(code), date, expected_code=code)
        if not quality["valid"]:
            missing("minutes", quality["reason"])
        elif float(daily.get("close") or 0) > 0:
            close = float(frames[code].iloc[-1]["close"])
            if abs(close - float(daily["close"])) > 0.01001:
                missing("price_basis", "日线与分钟收盘价格口径不一致")
        if code in active:
            prior = day["previous"].get(code) or {}
            amount = Signals._previous_amount_yuan(prior)
            if not float(prior.get("close") or 0) > 0 or amount <= 0:
                missing("prior_daily", "缺少候选日昨收或成交额")
            fraction, count = profile.expected_fraction(amount, "09:36:00")
            if not fraction or fraction <= 0 or count <= 0:
                missing("amount_profile", "历史量能模型缺失或该流动性组无有效样本")
        if not float(day["opening_prices"].get(code) or 0) > 0:
            missing("real_open", "缺少真实开盘价")
        if code in active and requires_auction(Signals._row_execution(active[code])):
            auction = day["auctions"].get(code) or {}
            if not float(auction.get("竞价成交额") or 0) > 0 or not float(auction.get("竞价成交量") or 0) > 0:
                missing("auction", "策略要求真实竞价证据")
        if not breadth_required:
            continue
        checker, detail = make_constituent_sync_checker(
            code=code,
            names=Signals._sectors(active.get(code) or row),
            members=day["members"],
            frames=frames,
            previous=previous,
            opening_prices=day["opening_prices"],
            minute_prices=prices,
        )
        gaps = [minute for minute in samples if checker(minute) is None]
        if gaps:
            missing(
                "sector_minutes",
                "完整成分分母下的板块证据不连续",
                missing_count=len(gaps),
                missing_times=gaps[:8],
                detail=deepcopy(detail),
            )
    return failures
