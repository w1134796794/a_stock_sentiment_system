import csv

from desktop import status


def test_limitup_overlay_reads_configured_external_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "external-cache"
    summary_dir = cache_dir / "summary"
    summary_dir.mkdir(parents=True)
    path = summary_dir / "limit_up_stocks.csv"
    rows = [
        {"trade_date": "20260629", "代码": "000001", "流通市值": 5_000_000_000, "连板数": 1, "炸板次数": 0},
        {"trade_date": "20260629", "代码": "000002", "流通市值": 20_000_000_000, "连板数": 2, "炸板次数": 0},
        {"trade_date": "20260630", "代码": "000001", "流通市值": 5_000_000_000, "连板数": 2, "炸板次数": 0},
        {"trade_date": "20260630", "代码": "000002", "流通市值": 20_000_000_000, "连板数": 3, "炸板次数": 1},
        {"trade_date": "20260630", "代码": "600001", "流通市值": 60_000_000_000, "连板数": 1, "炸板次数": 0},
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    monkeypatch.setattr(status, "CACHE_DIR", cache_dir)

    overlay = status._limitup_cache_overlay("20260630")

    assert overlay["cohorts"]["small"]["limit_up_count"] == 1
    assert overlay["cohorts"]["mid"]["limit_up_count"] == 1
    assert overlay["cohorts"]["large"]["limit_up_count"] == 1
    assert overlay["promotion"]["overall"] == 100.0
    assert overlay["promotion"]["rate_1to2"] == 100.0
    assert overlay["promotion"]["rate_2to3"] == 100.0
