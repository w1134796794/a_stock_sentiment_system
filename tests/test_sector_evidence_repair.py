from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from core.operations.preopen_evidence import PreopenEvidenceStore
from core.realtime.day_replay import _ReplayEntryService, replay_minutes, replay_request
from core.realtime.sector_service import RealtimeSectorService
from core.realtime.sector_evidence import prepare_sector_cache


def cache_sector(tmp_path, code="881158", name="文化传媒", kind="I"):
    directory = tmp_path / "sector/ths_index"
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"ts_code":code+".TI", "name":name, "type":kind}]).to_csv(directory / "index_I.csv", index=False)
    return directory


def cache_members(tmp_path, code="881158"):
    directory = tmp_path / "sector/ths_member"
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"con_code":"000001.SZ"}, {"con_code":"600000.SH"}]).to_csv(directory / (code+".TI.csv"), index=False)


def test_700_classification_is_rejected_even_with_metadata_members_and_override(tmp_path):
    cache_sector(tmp_path, "700632", "制造业指数")
    cache_members(tmp_path, "700632")
    service = RealtimeSectorService(cache_dir=tmp_path, quote_service=SimpleNamespace(
        get_quotes=lambda codes: pytest.fail("Rejected classifications must not request quotes")))
    assert service.resolve_codes_by_names(["制造业指数"]) == {}
    assert service.member_codes("700632.TI") == []
    assert service.get_sector_quotes(["700632"], member_map={"700632":["000001","600000"]})["missing"] == ["700632"]
    assert service.get_sector_quotes()["sectors"] == []


def test_verified_8_board_uses_real_constituents_and_rejects_unknown_8_code(tmp_path):
    cache_sector(tmp_path)
    cache_members(tmp_path)
    quotes = SimpleNamespace(get_quotes=lambda codes:{"quotes":[{"code":code,"change_pct":1,
        "last_price":10,"is_stale":False,"date":"20261013","time":"09:40:00"} for code in codes]})
    service = RealtimeSectorService(cache_dir=tmp_path, quote_service=quotes)
    assert service.resolve_codes_by_names(["文化传媒"]) == {"文化传媒":"881158"}
    result = service.get_sector_quotes(["881158"], include_raw=True)
    assert result["ok"] and result["source"] == "ths_constituent_aggregation"
    assert result["sectors"][0]["raw"]["coverage"] == 1
    cache_members(tmp_path, "881999")
    assert service.get_sector_quotes(["881999"])["missing"] == ["881999"]
    assert service.member_codes("881999") == []


def test_exact_sector_match_precedes_fuzzy_concept_proxy_and_reloads_metadata(tmp_path):
    directory = cache_sector(tmp_path, "881158", "文化传媒")
    pd.DataFrame([{"index_code":"885001","name":"文化传媒概念"}]).to_csv(directory / "adata_concept_ths.csv", index=False)
    service = RealtimeSectorService(cache_dir=tmp_path)
    assert service.resolve_codes_by_names(["文化传媒"])["文化传媒"] == "881158"
    cache_sector(tmp_path, "881001", "通用设备Ⅲ(A股)")
    assert service.resolve_codes_by_names(["通用设备III(A股)"]) == {"通用设备III(A股)":"881001"}
    assert service.resolve_codes_by_names(["通用设备", "文化传媒"]) == {}


def test_only_missing_real_constituents_are_fetched(tmp_path):
    cache_sector(tmp_path)
    calls = []
    def fetch(codes):
        calls.extend(codes)
        cache_members(tmp_path)
    dm = SimpleNamespace(get_ths_members_batch=fetch)
    result = prepare_sector_cache(["文化传媒"], cache_dir=tmp_path, data_manager=dm, allow_remote=True)
    assert calls == ["881158.TI"] and result["sector_members"]["文化传媒"] == ["000001","600000"]
    prepare_sector_cache(["文化传媒"], cache_dir=tmp_path, data_manager=dm, allow_remote=True)
    assert len(calls) == 1


def test_preparation_does_not_fetch_700_constituents(tmp_path):
    cache_sector(tmp_path, "700632", "制造业指数")
    dm = SimpleNamespace(get_ths_index=lambda kind: None,
        get_ths_members_batch=lambda codes: pytest.fail("Must not fetch 700 constituents"))
    result = prepare_sector_cache(["制造业指数"], cache_dir=tmp_path, data_manager=dm, allow_remote=True)
    assert result["sector_codes"] == {} and result["sector_members"] == {}
    assert result["unresolved_names"] == ["制造业指数"]


def test_old_frozen_classifications_cannot_confirm_live_breadth(tmp_path, monkeypatch):
    from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
    inputs = {"sector_codes":{"大类":"700632","电池":"885001"},
              "sector_members":{"大类":["000001","600000"],"电池":["000002","600001"]}}
    store = PreopenEvidenceStore(tmp_path)
    store.freeze("20261013", "20261014", now=datetime(2026,10,13,20), health={"ok":True}, inputs=inputs)
    path = store.path("20261013", "20261014")
    original = path.read_bytes()
    monkeypatch.setattr(PreopenEvidenceStore, "load", lambda self, *a: {"inputs":inputs})
    calls = []
    def quotes(codes):
        calls.extend(codes)
        return {"quotes":[{"code":c,"change_pct":1,"is_stale":False} for c in codes]}
    provider = RealtimeSectorBreadthProvider(quote_service=SimpleNamespace(get_quotes=quotes))
    provider.calendar = SimpleNamespace(prev=lambda date: "20261013")
    state, detail = provider.evaluate(["大类"], "20261014")
    assert state is None and detail["data_completeness"] == 0 and calls == []
    assert detail["unresolved_sector_names"] == ["大类"]
    state, detail = provider.evaluate(["电池"], "20261014")
    assert state is True and detail["coverage"] == 1
    assert set(calls) == {"000002","600001"}
    assert path.read_bytes() == original


def test_candidate_peer_fallback_cannot_confirm_an_unverified_classification():
    from backtest.minute_entry import MinuteEntryEvaluator
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    service = object.__new__(RealtimeEntrySignalService)
    service.evaluator = MinuteEntryEvaluator()
    service.sector_breadth = SimpleNamespace(evaluate=lambda *a:(None,{}), _members=lambda *a:set())
    frames = {c:pd.DataFrame([{"time":"09:36:00","close":11,"pre_close":10}])
              for c in ("000002","600000")}
    peers = [{"code":c,"sector_names":["制造业指数"]} for c in frames]
    checker, detail = service._sector_checker({"sector_names":["制造业指数"]},"000001",
        peers,frames,{},"20261013")
    assert checker("09:36:00") is None
    assert detail["peer_codes"] == [] and detail["data_completeness"] == 0


@pytest.mark.parametrize("sector_code,expected", [("700632",None),("885001",True),("",None)])
def test_saved_replay_cannot_reintroduce_700_or_unbound_members(sector_code, expected, monkeypatch):
    from core.realtime.day_replay import replay_saved_inputs
    def evaluate(self, row, quote, frame, *args):
        checker, detail = self._sector_checker(row,"000001",[],{},{},"20261013")
        state = checker(frame.iloc[-1]["time"])
        assert state is expected
        return {"signal_status":"observe","reason":detail["reason"],"sector_detail":detail,
                "data_status":"missing_sector" if state is None else "minute"}
    monkeypatch.setattr(_ReplayEntryService,"_evaluate_one",evaluate)
    bars = pd.DataFrame([{"time":f"09:{31+i:02d}:00","trade_date":"20261013","code":"000001",
        "open":10,"high":10.1,"low":9.9,"close":10.1,"volume":100} for i in range(20)])
    result = replay_minutes(request=replay_request({"market_date":"20261013","until":"09:51"}),
        candidate_date="20261012",rows=[{"code":"000001","sector_names":["电池"]}],
        frames={"000001":bars,"000002":bars.assign(code="000002"),"600000":bars.assign(code="600000")},
        previous={c:{"close":10} for c in ("000001","000002","600000")},
        members={"电池":["000002","600000"]},sector_codes={"电池":sector_code},
        opening_prices={"000001":10}, risk_config={"entry_sector_mode": "breadth_required"},
        profiles=SimpleNamespace(available=False,load=lambda:{},expected_fraction=lambda *a:(None,0)))
    inputs = result["replay_inputs"]
    if not sector_code:
        inputs.pop("sector_codes",None)
        inputs["members"] = {"电池":["000002","600000"]}
    repeated = replay_saved_inputs(inputs)
    assert bool(repeated["failures"]) is (expected is None)


def test_preopen_supplement_does_not_store_new_700_members(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    freeze(store)
    store.complete_sector_inputs("20261013","20261014",now=datetime(2026,10,14,8),
        sector_codes={"大类":"700632"},sector_members={"大类":["600000"]},daily_rows=[])
    assert "大类" not in store.load("20261013","20261014")["inputs"]["sector_members"]


def test_empty_cached_index_is_refetched_and_optional_null_count_is_preserved(tmp_path):
    from core.data.data_manager_sector import SectorDataManager
    directory = tmp_path / "ths_index"
    directory.mkdir()
    pd.DataFrame(columns=["ts_code","name"]).to_csv(directory / "index_I.csv",index=False)
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{"ts_code":"700632.TI","name":"制造业指数","count":None}])
    dm = SimpleNamespace(sector_dir=tmp_path, ts_pro=SimpleNamespace(ths_index=fetch))
    result = SectorDataManager.get_ths_index(dm, "I")
    assert len(calls) == 1 and len(result) == 1


def test_duplicate_700_names_never_resolve_and_8_board_publish_date_is_respected(tmp_path):
    directory = cache_sector(tmp_path)
    pd.DataFrame([{"ts_code":"700640.TI","name":"房地产业指数","type":"I","list_date":"20121231"},
                  {"ts_code":"700760.TI","name":"房地产业指数","type":"I","list_date":"20250805"},
                  {"ts_code":"881001.TI","name":"房地产","type":"I","list_date":"20250805"}]).to_csv(directory / "index_I.csv",index=False)
    service = RealtimeSectorService(cache_dir=tmp_path)
    assert service.resolve_codes_by_names(["房地产业指数"],as_of_date="20250101") == {}
    assert service.resolve_codes_by_names(["房地产业指数"],as_of_date="20261013") == {}
    assert service.resolve_codes_by_names(["房地产"],as_of_date="20250101") == {}
    assert service.resolve_codes_by_names(["房地产"],as_of_date="20261013") == {"房地产":"881001"}


def freeze(store):
    return store.freeze("20261013","20261014", now=datetime(2026,10,13,20), health={"ok":True}, inputs={
        "candidates":[{"code":"000001","sector_names":["电池"]}],
        "sector_members":{"电池":[],"已有":["000002"]},"sector_codes":{"电池":"885000","已有":"885002"},
        "daily_rows":[{"code":"000001","trade_date":"20261013","close":10}], "profiles":[{"version":"original"}]})


def test_preopen_supplement_preserves_original_candidates_config_and_bytes(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    freeze(store)
    path = store.path("20261013","20261014")
    original = path.read_bytes()
    old_id = store.load("20261013","20261014")["bundle_id"]
    result = store.complete_sector_inputs("20261013","20261014", now=datetime(2026,10,14,8,0),
        sector_codes={"电池":"885001","已有":"700632"},sector_members={"电池":["600000"],"已有":["999999"]},
        daily_rows=[{"code":"600000","trade_date":"20261013","close":20}, {"code":"000001","trade_date":"20261013","close":99}])
    assert result["ok"] and path.read_bytes() == original
    value = store.load("20261013","20261014")
    assert value["original_bundle_id"] == old_id and value["bundle_id"] != old_id
    assert value["inputs"]["profiles"] == [{"version":"original"}]
    assert value["inputs"]["sector_members"]["已有"] == ["000002"]
    assert value["inputs"]["sector_codes"]["电池"] == "885001"
    assert {r["code"]:r["close"] for r in value["inputs"]["daily_rows"]} == {"000001":10,"600000":20}


def test_preopen_supplement_cannot_backfill_after_cutoff_and_is_immutable(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    freeze(store)
    kwargs = dict(sector_codes={}, sector_members={}, daily_rows=[])
    assert not store.complete_sector_inputs("20261013","20261014", now=datetime(2026,10,14,9,25), **kwargs)["ok"]
    assert store.complete_sector_inputs("20261013","20261014", now=datetime(2026,10,14,8), **kwargs)["ok"]
    ident = store.load("20261013","20261014")["bundle_id"]
    assert store.complete_sector_inputs("20261013","20261014", now=datetime(2026,10,14,10), **kwargs)["existing"]
    assert store.load("20261013","20261014")["bundle_id"] == ident
    path = store.path("20261013","20261014").with_name("20261013.sectors.json")
    content = json.loads(path.read_text())
    content["captured_at"] = "2026-10-14T10:00:00"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="supplement"):
        store.load("20261013","20261014")


def test_more_preopen_missing_members_can_be_added_without_rewriting_first_supplement(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    freeze(store)
    args = dict(sector_codes={"电池":"885001"}, sector_members={"电池":["600000"]}, daily_rows=[])
    assert store.complete_sector_inputs("20261013","20261014",now=datetime(2026,10,14,8),**args)["ok"]
    first = store.path("20261013","20261014").with_name("20261013.sectors.json")
    original = first.read_bytes()
    args = dict(sector_codes={"新板块":"881760"}, sector_members={"新板块":["000003"]}, daily_rows=[])
    assert store.complete_sector_inputs("20261013","20261014",now=datetime(2026,10,14,8,30),**args)["ok"]
    assert first.read_bytes() == original
    value = store.load("20261013","20261014")
    assert len(value["sector_supplement_ids"]) == 2
    assert value["inputs"]["sector_members"]["电池"] == ["600000"]
    assert value["inputs"]["sector_members"]["新板块"] == ["000003"]
    assert not store.complete_sector_inputs("20261013","20261014",now=datetime(2026,10,14,10),
        sector_codes={"晚到":"881761"},sector_members={"晚到":["000004"]},daily_rows=[])["ok"]


def test_replay_does_not_report_unchecked_sector_as_missing_evidence():
    raw = pd.DataFrame([{"time":f"09:{31+i:02d}:00","trade_date":"20261013", "code":"000001",
                         "open":9.5,"high":9.6,"low":9.4,"close":9.5,"volume":100} for i in range(20)])
    profiles = SimpleNamespace(available=False,load=lambda:{},expected_fraction=lambda *a:(None,0))
    result = replay_minutes(request=replay_request({"market_date":"20261013","until":"09:51"}),candidate_date="20261012",
        rows=[{"code":"000001"}],frames={"000001":raw},previous={"000001":{"close":10}},
        opening_prices={"000001":9.5},profiles=profiles,
        risk_config={"three_board_feedback_enabled": False})
    assert result["stocks"][0]["status"] == "cancelled"
    assert not result["failures"]
    assert result["stocks"][0]["last_signal"]["sector_detail"]["assessed"] is False


def test_replay_distinguishes_missing_closes_from_missing_minutes():
    from backtest.minute_entry import MinuteEntryEvaluator
    raw = pd.DataFrame([{"time":"09:36:00","open":10,"high":10,"low":10,"close":10,"volume":100}])
    profiles = SimpleNamespace(available=False,load=lambda:{},expected_fraction=lambda *a:(None,0))
    service = _ReplayEntryService({}, {}, {"电池":["600000","000002"]}, {"600000":raw}, profiles, evaluator=MinuteEntryEvaluator())
    checker, detail = service._sector_checker({"sector_names":["电池"]},"000001",[],{},{},"20261013")
    assert checker("09:36:00") is None
    assert detail["missing_minute_codes"] == ["000002"]
    assert set(detail["missing_previous_close_codes"]) == {"600000","000002"}
