from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from core.operations.preopen_evidence import PreopenEvidenceStore
from core.realtime.day_replay import _ReplayEntryService, DayReplayLoader
from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
from core.signals.sector_sync import assess_sector_sync


def assess(now=None, before=None, expected=2, **kwargs):
    return assess_sector_sync(complete=True, breadth=0, average_change=-2, index_positive=False,
        opening_changes=now or {}, earlier_changes=before or {}, expected_members=expected, **kwargs)


def test_green_sector_can_pass_on_opening_rise_without_55_percent_red():
    state, detail = assess({'a':2,'b':1}, {'a':1,'b':0})
    assert state is True and detail['confirmation_path'] == 'open_rising_3m'
    assert detail['open_gain_pct'] == 1.5 and detail['three_minute_change_pct'] == 1
    assert not detail['legacy_breadth_passed']


@pytest.mark.parametrize('now,before', [({'a':-1,'b':-2},{'a':-2,'b':-3}),
    ({'a':1,'b':2},{'a':2,'b':3}), ({'a':1,'b':2},{'a':1,'b':2}),
    ({'a':0,'b':0},{'a':-1,'b':-1}), ({'a':float('nan'),'b':1},{'a':0,'b':0})])
def test_rebound_below_open_falling_curve_flat_curve_and_nonfinite_do_not_pass(now,before):
    assert assess(now,before)[0] is False


def test_fixed_constituent_pairs_require_60_percent_and_two_stocks():
    assert assess({'a':2,'b':3},{'b':1,'c':0},3)[0] is False
    assert assess({'a':2,'b':3},{'a':1,'b':0},4)[0] is False
    assert assess({'a':2,'b':3,'c':4},{'a':1,'b':0,'c':1},5)[0] is True


def test_missing_base_health_cannot_be_bypassed_and_old_red_path_still_works():
    state,_ = assess_sector_sync(complete=False,breadth=0,average_change=-1,index_positive=False,
        opening_changes={'a':2,'b':3},earlier_changes={'a':1,'b':1},expected_members=2)
    assert state is None
    state,detail = assess_sector_sync(complete=True,breadth=.55,average_change=1,index_positive=True,
        opening_changes={},earlier_changes={},expected_members=2)
    assert state is True and detail['confirmation_path'] == 'red_breadth'


def provider(monkeypatch):
    import core.realtime.sector_breadth as module
    clock = [datetime(2026,10,14,9,33,tzinfo=ZoneInfo('Asia/Shanghai'))]
    price = [9.1]
    monkeypatch.setattr(module,'shanghai_now',lambda:clock[0])
    monkeypatch.setattr(module,'monotonic',lambda:clock[0].timestamp())
    members=['000002','600000']
    inputs={'sector_codes':{'电池':'885001'},'sector_members':{'电池':members}}
    monkeypatch.setattr(PreopenEvidenceStore,'load',lambda *a:{'inputs':inputs})
    def quotes(codes):
        return {'quotes':[{'code':c,'open_price':9,'last_price':price[0],'change_pct':(price[0]/10-1)*100,
            'date':'20261014','time':clock[0].strftime('%H:%M:%S'),'is_stale':False} for c in codes]}
    service=RealtimeSectorBreadthProvider(quote_service=SimpleNamespace(get_quotes=quotes))
    service.calendar=SimpleNamespace(prev=lambda *a:'20261013')
    return service,clock,price


def test_live_history_requires_actual_three_minutes_and_no_backfill(monkeypatch):
    service,clock,price=provider(monkeypatch)
    assert service.evaluate(['电池'],'20261014')[0] is False
    clock[0]+=timedelta(minutes=2)
    price[0]=9.3
    assert service.evaluate(['电池'],'20261014')[0] is False
    clock[0]+=timedelta(minutes=1)
    price[0]=9.4
    state,detail=service.evaluate(['电池'],'20261014')
    assert state is True and detail['confirmation_path']=='open_rising_3m'
    assert detail['breadth']==0 and detail['trend_coverage']==1


def test_live_gap_does_not_bridge_collector_outage(monkeypatch):
    service,clock,price=provider(monkeypatch)
    service.evaluate(['电池'],'20261014')
    clock[0]+=timedelta(minutes=5)
    price[0]=9.4
    state,detail=service.evaluate(['电池'],'20261014')
    assert state is False and not detail['opening_trend_available']


def test_live_changed_opening_baseline_cannot_manufacture_a_rise(monkeypatch):
    service,clock,price=provider(monkeypatch)
    service.evaluate(['电池'],'20261014')
    clock[0]+=timedelta(minutes=3)
    price[0]=9.4
    original=service.quote_service.get_quotes
    def changed(codes):
        result=original(codes)
        for row in result['quotes']:
            row['open_price']=8
        return result
    service.quote_service.get_quotes=changed
    state,detail=service.evaluate(['电池'],'20261014')
    assert state is False and detail['trend_coverage']==0


def test_live_stale_quotes_cannot_complete_new_path(monkeypatch):
    service,clock,price=provider(monkeypatch)
    service.evaluate(['电池'],'20261014')
    clock[0]+=timedelta(minutes=3)
    price[0]=9.4
    original=service.quote_service.get_quotes
    def stale(codes):
        result=original(codes)
        for row in result['quotes']:
            row['is_stale']=True
        return result
    service.quote_service.get_quotes=stale
    state,detail=service.evaluate(['电池'],'20261014')
    assert state is None and not detail['opening_trend_passed']


def test_replay_uses_true_open_not_first_minute_close_and_never_future_prices():
    from backtest.minute_entry import MinuteEntryEvaluator
    bars=pd.DataFrame([{'time':f'09:{31+i:02d}:00','close':9.05+i*.02,'open':9.05+i*.02,
        'high':9.05+i*.02,'low':9.05+i*.02,'volume':100} for i in range(10)])
    frames={c:bars.copy() for c in ('000002','600000')}
    profiles=SimpleNamespace(available=False,load=lambda:{},expected_fraction=lambda *a:(None,0))
    service=_ReplayEntryService({c:{'close':10} for c in frames},{},{'电池':list(frames)},frames,profiles,
        opening_prices={c:9 for c in frames}, evaluator=MinuteEntryEvaluator())
    checker,detail=service._sector_checker({'sector_names':['电池']},'000001',[],{},{},'20261014')
    assert checker('09:32:00') is False
    assert checker('09:36:00') is True
    assert detail['confirmation_path']=='open_rising_3m'
    for f in frames.values():
        f.loc[f['time']=='09:40:00','close']=100
    service.sector_minutes.clear()
    assert checker('09:32:00') is False
    service.opening_prices={}
    service.sector_minutes.clear()
    assert checker('09:36:00') is False


def test_replay_opening_evidence_reads_only_requested_date_open_fields(tmp_path):
    directory=tmp_path/'stock'/'all_daily'
    directory.mkdir(parents=True)
    pd.DataFrame([{'ts_code':'600000.SH','trade_date':'20261014','open':9,'close':999},
        {'ts_code':'000002.SZ','trade_date':'20261013','open':8,'close':999}]).to_csv(directory/'20261014.csv',index=False)
    loader=DayReplayLoader(cache_dir=tmp_path,db_path=tmp_path/'missing.db')
    prices,source=loader._opening_evidence('20261014',['600000','000002'],fetch_missing=False)
    assert prices=={'600000':9} and source=='stock_daily_open_cache'
