from web.app import _build_concept_echelon_section, _limit_up_sector_counts


def _stock(code, name, board, concepts, industry="软件开发", pct=10.0):
    return {
        "股票代码": code,
        "股票名称": name,
        "连板数": board,
        "涨幅%": pct,
        "行业": industry,
        "首次涨停时间": "09:30:00",
        "炸板次数": 0,
        "_concepts": concepts,
    }


def test_limit_up_sector_counts_deduplicates_stocks(tmp_path):
    member_dir = tmp_path / "sector" / "stock_sectors"
    member_dir.mkdir(parents=True)
    (member_dir / "000001.SZ.csv").write_text(
        "ts_code,con_code,con_name,name,type,exchange\n"
        "884036.TI,000001.SZ,甲公司,氟化工,I,A\n",
        encoding="utf-8",
    )
    rows = [
        _stock("000001.SZ", "甲公司", 2, ["云计算", "物联网"]),
        _stock("000002.SZ", "乙公司", 1, ["云计算"], industry="通信设备"),
        _stock("000001.SZ", "甲公司", 2, ["云计算"], industry="软件开发"),
    ]

    counts = _limit_up_sector_counts(rows, tmp_path)

    assert counts["云计算"] == 2
    assert counts["物联网"] == 1
    assert counts["软件开发"] == 1
    assert counts["通信设备"] == 1
    assert counts["氟化工"] == 1
    assert counts["884036.TI"] == 1


def test_concept_echelon_keeps_expandable_stock_details():
    rows = [
        _stock("000001.SZ", "甲公司", 1, ["云计算"]),
        _stock("000002.SZ", "乙公司", 3, ["云计算"], pct=9.98),
    ]

    section = _build_concept_echelon_section(rows, "20260701")

    assert section is not None
    concept = section["rows"][0]
    assert concept["概念名称"] == "云计算"
    assert concept["涨停总数"] == 2
    assert concept["最高连板"] == 3
    assert [stock["股票名称"] for stock in concept["_stocks"]] == ["乙公司", "甲公司"]
    assert concept["_stocks"][0]["_detail_url"] == "/stock/000002?date=20260701"
