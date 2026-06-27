import pandas as pd

from core.data.data_manager_concept import ConceptDataManager
from core.data.data_prep import DataPrep


def test_limit_up_concept_cache_keeps_only_concepts(tmp_path):
    manager = ConceptDataManager.__new__(ConceptDataManager)
    manager.concept_dir = tmp_path / "concept"
    manager.get_stock_sectors_batch = lambda codes: {
        "000001": pd.DataFrame([
            {"ts_code": "885001.TI", "name": "人工智能", "type": "N"},
            {"ts_code": "881001.TI", "name": "银行", "type": "I"},
        ]),
    }

    result = manager.cache_limit_up_stock_concepts(["000001"], "20260626")

    assert result.to_dict(orient="records") == [{
        "con_code": "000001",
        "concept_code": "885001.TI",
        "concept_name": "人工智能",
        "type": "N",
    }]
    cached = pd.read_csv(tmp_path / "concept" / "members" / "limit_up_20260626.csv")
    assert cached.iloc[0]["concept_name"] == "人工智能"


def test_data_prep_warms_limit_up_concepts_from_today_pool():
    class FakeDataManager:
        def __init__(self):
            self.calls = []

        def cache_limit_up_stock_concepts(self, codes, trade_date):
            self.calls.append((list(codes), trade_date))
            return pd.DataFrame([{"concept_name": "人工智能"}])

    dm = FakeDataManager()
    prep = DataPrep(dm)
    prep._prefetch_sectors = lambda *args, **kwargs: None

    dataset = prep.build(
        "20260626",
        zt_pool=pd.DataFrame([{"代码": "000001.SZ"}, {"代码": "600000.SH"}]),
        prefetch_all_daily=False,
        prefetch_limit_up=False,
        prefetch_limit_down=False,
        prefetch_universe_daily=False,
        prefetch_sectors=True,
    )

    assert dm.calls == [(["000001", "600000"], "20260626")]
    assert dataset.meta["limit_up_concept_relations"] == 1
    assert "limit_up_concepts" in dataset.prefetched
