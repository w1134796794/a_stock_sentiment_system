from pathlib import Path

from web.stock_profile import (
    clear_stock_profile_cache,
    enrich_stock_sector_labels,
    load_stock_profiles,
)


def test_stock_profile_reads_prefetched_industry_and_concepts(tmp_path: Path):
    market_dir = tmp_path / "market"
    member_dir = tmp_path / "sector" / "stock_sectors"
    market_dir.mkdir(parents=True)
    member_dir.mkdir(parents=True)
    (market_dir / "stock_basic.csv").write_text(
        "ts_code,symbol,name,industry\n301629.SZ,301629,矽电股份,半导体\n",
        encoding="utf-8",
    )
    (member_dir / "301629.SZ.csv").write_text(
        "ts_code,con_code,name,type\n"
        "881121.TI,301629.SZ,半导体,I\n"
        "884229.TI,301629.SZ,半导体设备,I\n"
        "885756.TI,301629.SZ,芯片概念,N\n"
        "886042.TI,301629.SZ,存储芯片,N\n",
        encoding="utf-8",
    )

    clear_stock_profile_cache()
    profile = load_stock_profiles(["301629.SZ"], tmp_path)["301629"]

    assert profile["industries"] == ["半导体", "半导体设备"]
    assert profile["concepts"] == ["芯片概念", "存储芯片"]

    rows = [{"code": "301629", "resonance_sectors": "半导体设备,存储芯片"}]
    enrich_stock_sector_labels(rows, tmp_path)

    assert rows[0]["industry_names"] == ["半导体设备", "半导体"]
    assert rows[0]["concept_names"] == ["存储芯片", "芯片概念"]
    assert rows[0]["sector_tags"][0] == {
        "name": "半导体设备", "type": "行业", "resonant": True,
    }
