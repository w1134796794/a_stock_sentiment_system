import base64
import hashlib
import hmac
import json
import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.notifications.notifier import NotificationService


@pytest.fixture
def service(monkeypatch):
    for key in ("WECOM_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "SERVERCHAN_SENDKEY",
                "SERVERCHAN_SENDKEYS", "FEISHU_WEBHOOK_URL", "FEISHU_SIGN_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/feishu-secret-hook")
    return NotificationService(backend=MemoryStateBackend("feishu-test"))


def transport(monkeypatch, body, status=200):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return body if isinstance(body, bytes) else json.dumps(body).encode()

    response = Response()
    response.status = status
    monkeypatch.setattr("core.notifications.notifier.request.urlopen",
                        lambda req, **kwargs: calls.append((req, kwargs)) or response)
    return calls


def test_payload_and_status_are_channel_specific_and_secret_free(service, monkeypatch):
    calls = transport(monkeypatch, {"code": 0, "msg": "success"})
    status = service.status()
    assert status["channels"]["飞书群机器人"] is True
    assert status["configured_endpoint_count"] == 1
    result = service.send("买点确认", "测试内容", event_key="one")
    req, options = calls[0]
    assert json.loads(req.data) == {"msg_type": "text", "content": {"text": "买点确认\n测试内容"}}
    assert options["timeout"] == service.timeout
    assert result["sent"] == 1 and result["all_delivered"]
    assert service.send("买点确认", "测试内容", event_key="one")["deduplicated"]
    assert len(calls) == 1
    assert "secret-hook" not in str(status) + str(result)


def test_feishu_signature_follows_timestamp_key_protocol(service, monkeypatch):
    service.feishu_secret = "sign-secret"
    monkeypatch.setattr("core.notifications.notifier.time.time", lambda: 1599360473)
    calls = transport(monkeypatch, {"code": 0})
    assert service.send("标题", "内容")["ok"]
    payload = json.loads(calls[0][0].data)
    assert payload["timestamp"] == "1599360473"
    assert payload["sign"] == base64.b64encode(hmac.new(
        b"1599360473\nsign-secret", b"", hashlib.sha256).digest()).decode()


@pytest.mark.parametrize("body", [
    {"code": 19021}, {"code": 9499}, {}, {"code": None}, {"code": False},
    {"code": "0"}, [], b"not-json", {"code": 0, "StatusCode": 1},
])
def test_business_failure_or_invalid_response_is_not_delivery(service, monkeypatch, body):
    transport(monkeypatch, body)
    # Prefer the modern code field if both fields are present.
    expected = isinstance(body, dict) and type(body.get("code")) is int and body["code"] == 0
    result = service.send("标题", "内容")
    assert result["ok"] is expected
    assert result["sent"] == int(expected)


def test_legacy_success_and_http_failure(service, monkeypatch):
    transport(monkeypatch, {"StatusCode": 0})
    assert service.send("title", "text")["ok"]
    transport(monkeypatch, {"code": 0}, status=503)
    assert not service.send("title", "text")["ok"]


def test_retry_only_failed_channel_not_successful_wechat(service, monkeypatch):
    service.wecom_url = "https://example.invalid/wecom"
    clock = [1000.0]
    monkeypatch.setattr("core.notifications.notifier.time.time", lambda: clock[0])
    wechat = []
    monkeypatch.setattr(service, "_post_json", lambda *args: wechat.append(args) or {"ok": True})
    calls = transport(monkeypatch, {"code": 19021})
    result = service.send("title", "text", event_key="retry")
    assert result["sent"] == 1 and not result["all_delivered"]
    pending = service.send("title", "text", event_key="retry")
    assert pending["sent"] == 0 and pending["results"][-1]["retry_pending"]
    assert len(calls) == 1
    clock[0] += 31
    calls = transport(monkeypatch, {"code": 0})
    result = service.send("title", "text", event_key="retry")
    assert result["sent"] == 1 and result["all_delivered"]
    assert len(wechat) == 1 and len(calls) == 1


def test_exception_cannot_expose_url_secret_or_signed_payload(service, monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("feishu-secret-hook sign-secret private message")

    monkeypatch.setattr("core.notifications.notifier.request.urlopen", fail)
    result = service.send("title", "text")
    assert not result["ok"]
    assert result["results"][0]["error_type"] == "TimeoutError"
    assert "secret" not in str(result)


def test_secret_alone_does_not_enable_channel(service):
    service.feishu_url = ""
    service.feishu_secret = "secret"
    assert not service.enabled
    assert service.send("title", "text")["sent"] == 0


def test_confirmation_and_exit_use_feishu_without_changing_account(service, monkeypatch):
    calls = transport(monkeypatch, {"code": 0})
    service.notify_realtime_payload({"market_date": "20261110", "rows": [
        {"code": "000001", "name": "测试", "confirm_status": "confirmed", "entry_mode": "weak_only"},
        {"code": "000002", "confirm_status": "observe"},
    ]})
    service.notify_exit_signal({"id": 1, "code": "000001"}, {"action": "sell"}, signal_date="20261110")
    assert len(calls) == 2
