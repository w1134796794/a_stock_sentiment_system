"""Low-frequency research adapters; no writes to authoritative market stores."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from importlib.metadata import version

from core.data.providers.eltdx_evidence import failure
from core.realtime.market_evidence import number
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now

RESEARCH_KINDS = frozenset(
    {
        "bars_raw",
        "bars_qfq",
        "bars_hfq",
        "corporate_actions",
        "adjustment_factors",
        "money_flow",
        "company_profile",
        "announcements",
        "finance_report",
        "topics",
        "security_status",
    }
)
PERIODS = frozenset({"day", "week", "month", "1m", "5m", "15m", "30m", "60m"})


def json_value(value):
    """Drop transport/raw bytes; preserve public business fields with explicit unknowns."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return number(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return None
    if is_dataclass(value):
        return {
            field.name: json_value(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"raw", "raw_payload", "raw_payloads", "record_hex", "request_body"}
        }
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items() if k not in {"raw", "raw_payload", "request_body"}}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    raise ValueError(f"Unsupported research response type: {type(value).__name__}")


class EltdxResearchMixin:
    def _f10_client(self):
        from eltdx.f10 import F10Client

        # urllib retains the environment proxy and normal TLS verification.
        return F10Client(timeout=self.timeout, prefer_ipv4=False)

    def get_research(self, kind, code, start, end, *, period="day", max_pages=20):
        if kind not in RESEARCH_KINDS or period not in PERIODS or not 1 <= max_pages <= 100:
            raise ValueError("Invalid research kind, period or pagination budget")
        first, last = datetime.strptime(start, "%Y%m%d").date(), datetime.strptime(end, "%Y%m%d").date()
        if first > last:
            raise ValueError("Reversed research range")
        code6 = normalize_stock_code(code)
        if len(code6) != 6 or not code6.isdigit():
            raise ValueError("Invalid research security code")
        result = dict(
            schema_version=1,
            source="eltdx",
            sdk_version=version("eltdx"),
            code=code6,
            kind=kind,
            start=start,
            end=end,
            period=period,
            adjust={"bars_raw": "none", "bars_qfq": "qfq", "bars_hfq": "hfq"}.get(kind),
            quality_status="ok",
            quality_reasons=[],
            rows=[],
            coverage={},
            historical_eligible=False,
            availability_basis="captured_at_only",
            published_at=None,
            formal_eligible=False,
            research_only=True,
        )
        try:
            full = self._to_tdx_code(code)
            if kind in {"company_profile", "announcements", "finance_report", "topics"}:
                api = self._f10_client()
                method = {"topics": "hot_topics"}.get(kind, kind)
                response = getattr(api, method)(full)
                if not response.ok:
                    raise RuntimeError(f"F10 response error: {response.error_code}")
                result["rows"] = [
                    dict(
                        table_index=i,
                        columns=list(table.columns),
                        values=json_value(row),
                        published_at=None,
                        available_at=None,
                    )
                    for i, table in enumerate(response.result_sets)
                    for row in table.rows
                ]
                result["coverage"] = dict(
                    historical_range_supported=False,
                    publication_time_verified=False,
                    entry=response.entry,
                    scope="current_response_only",
                )
            else:
                with self._client() as client:
                    if kind.startswith("bars_"):
                        series = client.bars.get(
                            full,
                            period=period,
                            adjust=result["adjust"],
                            all_pages=True,
                            max_pages=max_pages,
                        )
                        self._validate_evidence_identity(series, code, end)
                        rows = []
                        for bar in series.bars:
                            day = bar.time.date()
                            if not first <= day <= last:
                                continue
                            values = {key: number(getattr(bar, key, None)) for key in ("open", "high", "low", "close")}
                            if any(v is None or v <= 0 for v in values.values()):
                                raise ValueError("Invalid bar price")
                            if (
                                not values["low"]
                                <= min(values["open"], values["close"])
                                <= max(values["open"], values["close"])
                                <= values["high"]
                            ):
                                raise ValueError("Inconsistent OHLC")
                            rows.append(
                                dict(
                                    event_time=shanghai_now(bar.time).isoformat(),
                                    date=day.strftime("%Y%m%d"),
                                    **values,
                                    volume_hand=number(bar.volume_lots),
                                    amount_yuan=number(bar.amount),
                                    period=period,
                                    bar_time="native",
                                    bar_label="end" if period == "1m" else "unknown",
                                    adjust=result["adjust"],
                                )
                            )
                        result["rows"] = sorted(rows, key=lambda row: row["event_time"])
                        result["coverage"] = dict(
                            pagination_complete=True,
                            first_date=result["rows"][0]["date"] if rows else None,
                            last_date=result["rows"][-1]["date"] if rows else None,
                            requested_range_complete=None,
                            reason="Trading calendar and missing sessions require independent validation",
                        )
                    elif kind in {"corporate_actions", "adjustment_factors"}:
                        api = client.corporate
                        response = (
                            api.capital_changes(full)
                            if kind == "corporate_actions"
                            else api.adjustment_factors(full, anchor_date=last, start_date=first)
                        )
                        self._validate_evidence_identity(response, code, end)
                        records = response.records if kind == "corporate_actions" else response.items
                        result["rows"] = [json_value(row) for row in records if row.date and first <= row.date <= last]
                        result["coverage"] = dict(
                            anchor_date=end if kind == "adjustment_factors" else None,
                            start_date=start,
                            local_affine_factors=kind == "adjustment_factors",
                            interchangeable_with_server_adjustment=False,
                        )
                    elif kind == "money_flow":
                        response = client.money_flow.daily(full)
                        self._validate_evidence_identity(response, code, end)
                        result["rows"] = [
                            json_value(row) for row in response.records if row.date and first <= row.date <= last
                        ]
                        result["coverage"] = dict(
                            scope="server_retained_daily_records",
                            amount_unit="yuan",
                            classification="tdx_vendor_inference",
                            interchangeable_with_tushare=False,
                            historical_range_complete=None,
                        )
                    elif kind == "security_status":
                        market = full[:2]
                        listings = client.helpers.latest_stock_list(market)
                        matched = [row for row in listings if row.full_code == full]
                        # Absence is unknown, never a delisting inference.
                        if matched:
                            st = {row.full_code for row in client.helpers.latest_st(market)}
                            suspended = {row.full_code for row in client.helpers.latest_suspended(market)}
                            result["rows"] = [
                                dict(
                                    code=code6,
                                    name=matched[0].name,
                                    listed=True,
                                    is_st=full in st,
                                    is_suspended=full in suspended,
                                    delisted=None,
                                    effective_date=shanghai_now().strftime("%Y%m%d"),
                                )
                            ]
                        result["coverage"] = dict(historical_range_supported=False, absence_means="unknown")
            if not result["rows"]:
                result["quality_status"] = "empty"
        except Exception as exc:
            result.update(quality_status=failure(exc), quality_reasons=[str(exc)], rows=[])
        result["captured_at"] = shanghai_now().isoformat(timespec="milliseconds")
        return result
