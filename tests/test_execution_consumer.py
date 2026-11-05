from scripts.run_realtime_execution import consume_paper_payload


class Paper:
    def __init__(self):
        self.calls = []

    def process_realtime_payload(self, payload):
        self.calls.append(payload)
        return {"opened": [], "skipped": {"waiting": 1}}


def test_unrelated_stale_row_does_not_block_fresh_confirmation():
    paper = Paper()
    payload = {"ok": False, "market_revision": "same", "rows": [
        {"code": "000001", "confirm_status": "observe", "health_gate_reason": "expired"},
        {"code": "000002", "confirm_status": "confirmed"}]}
    consume_paper_payload(paper, payload)
    assert [row["code"] for row in paper.calls[0]["rows"]] == ["000002"]


def test_same_revision_can_retry_after_waiting_for_execution_quote():
    paper = Paper()
    payload = {"ok": True, "market_revision": "same", "rows": [
        {"code": "000002", "confirm_status": "confirmed"}]}
    consume_paper_payload(paper, payload)
    consume_paper_payload(paper, payload)
    assert len(paper.calls) == 2


def test_expired_history_only_or_missing_payload_never_executes():
    paper = Paper()
    for payload in ({}, {"ok": False, "rows": [{"confirm_status": "observe"}]},
                    {"rows": [{"confirm_status": "confirmed_history"}]}):
        assert consume_paper_payload(paper, payload)["confirmed_candidates"] == 0
    assert not paper.calls
