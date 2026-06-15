from core.data.providers.quotation_provider import QuotationProvider


def test_quotation_provider_normalizes_sina_quote():
    provider = QuotationProvider()
    out = provider._normalize_quote(
        "000001",
        {
            "name": "平安银行",
            "open": 11.0,
            "close": 10.94,
            "now": 11.24,
            "high": 11.25,
            "low": 10.88,
            "turnover": 203235546,
            "volume": 2263042930.57,
            "date": "2026-06-12",
            "time": "15:00:00",
        },
        "pqquotation",
    )

    assert out["code"] == "000001"
    assert out["ts_code"] == "000001.SZ"
    assert out["last_price"] == 11.24
    assert out["pre_close"] == 10.94
    assert out["vol_hand"] == 2032355.46
    assert out["amount_yuan"] == 2263042930.57
    assert out["source"] == "pqquotation_sina"
