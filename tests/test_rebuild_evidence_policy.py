from types import SimpleNamespace
import json

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.evidence_policy import ReplayEvidenceBlocked, missing_entry_evidence
from backtest.minute_entry import EntryDecision


DATE = "20260901"


def bars():
    times = [f"{m // 60:02d}:{m % 60:02d}:00"
             for start in (570, 780) for m in range(start + 1, start + 121)]
    return pd.DataFrame({"trade_date": DATE, "time": times, "close": 10.0, "volume": 100.0})


def plan(code="000001", execution=None):
    return pd.Series({"代码": code, "名称": "测试", "目标价": 10, "策略ID": "test",
                      "策略执行": execution or {}, "模式": "指标筛选/test"})


def engine(policy="skip_candidates", frames=None):
    frames = frames or {}
    dm = SimpleNamespace(
        get_stock_tick=lambda code, date: frames.get(code.split('.')[0], pd.DataFrame()),
        get_auction_data=lambda *args: {},
        get_stock_daily_data=lambda *args: {"open": 10, "high": 10.3, "low": 9.8, "pre_close": 10},
    )
    instance = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy=policy))
    instance._load_sector_peer_codes = lambda *args: {}
    instance._get_stock_daily_bar = lambda *args: {}
    return instance


def test_skip_missing_candidate_keeps_valid_candidate_and_never_uses_daily_price():
    instance = engine(frames={"000002": bars()})
    plans = pd.DataFrame([plan(), plan("000002")])
    instance._prefetch_entry_data(plans, DATE)
    visited = []
    def evaluate(**kwargs):
        visited.append(len(kwargs['bars']))
        return EntryDecision(status="filled", signal="弱转强", entry_price=10.01,
                             confirm_time="09:36:00", entry_time="09:37:00")
    instance.minute_evaluator.evaluate = evaluate
    assert instance._check_buy_conditions(plan(), DATE, "000001", "测试") == (False, 0)
    assert instance._check_buy_conditions(plan("000002"), DATE, "000002", "测试")[0]
    assert visited == [240]
    quality = instance.replay_data_quality()
    assert quality['status'] == 'incomplete'
    assert quality['skipped_candidate_count'] == 1
    assert quality['candidate_coverage_pct'] == 50
    assert not quality['full_sample_metrics_valid']
    assert quality['skipped_candidates'][0]['trade_date'] == DATE
    assert quality['skipped_candidates'][0]['code'] == '000001'
    assert instance.cash == instance.config.initial_capital


@pytest.mark.parametrize("policy", ["strict", "skip_candidates"])
@pytest.mark.parametrize("partial", [False, True])
def test_holding_missing_or_partial_stops_before_any_exit_or_nav_mutation(policy, partial):
    instance = engine(policy, {"000001": bars().iloc[:120]} if partial else {})
    position = {"shares": 100, "entry_price": 10, "market_value": 1000}
    instance.current_positions = {"000001": position.copy()}
    instance._check_stop_loss_take_profit = lambda *args: pytest.fail('must not use daily fallback')
    for _ in range(2):
        with pytest.raises(ReplayEvidenceBlocked) as exc:
            instance._process_date(DATE, 'unused')
        assert exc.value.details[0]['scope'] == 'holding'
        assert exc.value.details[0]['code'] == '000001'
    assert instance.current_positions['000001'] == position
    assert instance.daily_nav == []


def test_strict_candidate_missing_stops_and_partial_candidate_is_not_accepted():
    instance = engine('strict', {'000001': bars().iloc[:120]})
    instance._prefetch_entry_data(pd.DataFrame([plan()]), DATE)
    with pytest.raises(ReplayEvidenceBlocked) as exc:
        instance._check_buy_conditions(plan(), DATE, '000001', '测试')
    assert exc.value.details[0]['scope'] == 'candidate'
    assert instance.replay_data_quality()['candidate_coverage_pct'] == 0


@pytest.mark.parametrize('required', [False, True])
def test_auction_only_blocks_when_execution_contract_requires_it(required):
    instance = engine(frames={'000001': bars()})
    candidate = plan(execution={'auction_required': required})
    instance._prefetch_entry_data(pd.DataFrame([candidate]), DATE)
    instance.minute_evaluator.evaluate = lambda **kw: EntryDecision(status='filled', entry_price=10.01)
    allowed, _ = instance._check_buy_conditions(candidate, DATE, '000001', '测试')
    assert allowed is not required
    quality = instance.replay_data_quality()
    assert quality['skipped_candidate_count'] == int(required)
    if required:
        assert quality['skipped_candidates'][0]['evidence'] == 'missing_auction'


def test_missing_daily_data_strict_error_is_not_swallowed():
    instance = engine('strict', {'000001': bars()})
    instance.dm.get_stock_daily_data = lambda *args: {}
    instance._prefetch_entry_data(pd.DataFrame([plan()]), DATE)
    with pytest.raises(ReplayEvidenceBlocked) as exc:
        instance._check_buy_conditions(plan(), DATE, '000001', '测试')
    assert len(instance.entry_attempts) == 1
    assert exc.value.details[0]['evidence'] == 'missing_daily'


def test_skip_counts_unique_date_stock_and_excludes_valid_no_signal():
    instance = engine()
    for code in ('000001', '000002'):
        instance._candidate_evidence_samples.add((DATE, code))
    for evidence in ('missing_sector', 'missing_amount_profile', 'missing_sector'):
        instance._record_entry_attempt(plan(), DATE, '000001', '测试',
            EntryDecision(status='data_insufficient', data_status=evidence, reason=evidence), entry_mode='weak_to_strong')
    instance._record_entry_attempt(plan('000002'), DATE, '000002', '测试',
        EntryDecision(status='cancelled', reason='10:00前未完成弱转强确认'), entry_mode='weak_to_strong')
    quality = instance.replay_data_quality()
    assert quality['skipped_candidate_count'] == 1
    assert len(quality['skipped_candidates']) == 2
    assert quality['candidate_coverage_pct'] == 50
    assert not missing_entry_evidence(instance.entry_attempts[-1])


def test_report_audit_persists_and_only_audited_candidates_can_be_ignored(tmp_path, monkeypatch):
    from run_backtest import save_replay_evidence_audit
    from core.portfolio.paper_trading_service import PaperTradingService
    import core.portfolio.paper_trading_service as module
    monkeypatch.setattr(module, 'OUTPUT_DIR', tmp_path)
    instance = engine()
    instance._prefetch_entry_data(pd.DataFrame([plan()]), DATE)
    instance._check_buy_conditions(plan(), DATE, '000001', '测试')
    quality = instance.replay_data_quality()
    save_replay_evidence_audit(quality, tmp_path, 'audit')
    directory = tmp_path / 'backtest_results'
    pd.DataFrame(instance.entry_attempts).to_csv(directory / 'backtest_entry_attempts_audit.csv', index=False)
    service = object.__new__(PaperTradingService)
    assert service.rebuild_data_quality('audit')['label'] == '数据不完整'
    assert len(service.rebuild_evidence_failures('audit')) == 1
    assert service.rebuild_evidence_failures('audit', allow_candidate_skips=True) == []
    pd.DataFrame([{'trade_date': DATE, 'code': '000001', 'evidence': 'exit_minute_bars', 'reason': 'missing'}]).to_csv(
        directory / 'backtest_evidence_failures_audit.csv', index=False)
    assert service.rebuild_evidence_failures('audit', allow_candidate_skips=True)[0]['evidence'] == 'exit_minute_bars'
    assert service.rebuild_data_quality('../audit') == {}


def test_default_account_cannot_request_skip_mode():
    import web.app as module
    response = module.api_portfolio_replay({'missing_evidence_policy': 'skip_candidates'})
    assert response.status_code == 422


def test_independent_rebuild_passes_skip_policy_despite_prefetch_failure(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    failure = {'trade_date': DATE, 'code': '000001', 'evidence': 'minute_bars', 'reason': 'missing'}
    calls = []
    monkeypatch.setattr(module, '_PAPER_REPLAY_STATE', {'mode': 'rebuild'})
    monkeypatch.setattr(module, '_get_paper_trading_service', lambda: SimpleNamespace(
        prefetch_replay_minutes=lambda *a, **kw: {'minute_failed': 1, 'evidence_failures': [failure]}))
    monkeypatch.setattr(BACKTEST_CONTROLLER, 'start', lambda *a, **kw: (calls.append(kw) or True, 'started'))
    module._run_paper_rebuild(DATE, DATE, 1000000, 'skip_candidates')
    assert module._PAPER_REPLAY_STATE['state'] == 'running'
    assert calls[0]['missing_evidence_policy'] == 'skip_candidates'
    assert calls[0]['prefetch_failures'] == [failure]


def test_completed_incomplete_rebuild_imports_only_independent_account(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    imported = []
    quality = {'policy': 'skip_candidates', 'status': 'incomplete', 'skipped_candidate_count': 1}
    monkeypatch.setattr(module, '_PAPER_REPLAY_STATE', {
        'mode': 'rebuild', 'state': 'running', 'missing_evidence_policy': 'skip_candidates'})
    monkeypatch.setattr(BACKTEST_CONTROLLER, 'status', lambda *a: {'state': 'done', 'run_id': 'audit'})
    monkeypatch.setattr(module, '_get_paper_trading_service', lambda: SimpleNamespace(
        rebuild_data_quality=lambda run: quality,
        rebuild_evidence_failures=lambda run, **kw: [] if kw['allow_candidate_skips'] else ['failure'],
        import_backtest_run=lambda run, **kw: imported.append(kw)))
    response = json.loads(module.api_portfolio_replay_status().body)
    assert response['state'] == 'done'
    assert imported == [{'reset': True, 'account_key': 'history_audit'}]
    assert response['paper_replay']['data_quality'] == quality


def test_legacy_does_not_claim_full_sample_evidence_validity():
    quality = engine('legacy').replay_data_quality()
    assert quality['status'] == 'not_checked'
    assert not quality['full_sample_metrics_valid']


def test_saved_summary_and_performance_report_warn_about_incomplete_sample(tmp_path):
    from run_backtest import save_backtest_results
    from backtest.performance_analyzer import PerformanceAnalyzer
    instance = engine()
    instance._prefetch_entry_data(pd.DataFrame([plan()]), DATE)
    instance._check_buy_conditions(plan(), DATE, '000001', '测试')
    result = instance._generate_backtest_report()
    save_backtest_results(result, tmp_path, timestamp='quality_test')
    summary = pd.read_csv(tmp_path / 'backtest_results/backtest_summary_quality_test.csv')
    assert summary.iloc[0]['data_quality_label'] == '数据不完整'
    assert summary.iloc[0]['skipped_candidate_count'] == 1
    assert not bool(summary.iloc[0]['full_sample_metrics_valid'])
    report = PerformanceAnalyzer().generate_performance_report(result)
    assert '数据不完整' in report
    assert '不是完整候选样本' in report
