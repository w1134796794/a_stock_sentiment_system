"""Send deduplicated alerts through WeCom, DingTalk or ServerChan."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable
from urllib import parse, request

from loguru import logger

from core.infrastructure.shared_state import get_shared_state_backend


class NotificationService:
    def __init__(self, *, timeout: float = 8.0) -> None:
        self.timeout = max(float(timeout), 1.0)
        self.wecom_url = os.getenv("WECOM_WEBHOOK_URL", "").strip()
        self.dingtalk_url = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
        self.serverchan_key = os.getenv("SERVERCHAN_SENDKEY", "").strip()
        self.backend = get_shared_state_backend()

    @property
    def enabled(self) -> bool:
        return bool(self.wecom_url or self.dingtalk_url or self.serverchan_key)

    def send(self, title: str, content: str, *, event_key: str = "", ttl_seconds: int = 86_400) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "sent": 0, "message": "未配置通知渠道"}
        if event_key and self.backend.get_json(f"notification:{event_key}"):
            return {"ok": True, "sent": 0, "deduplicated": True}
        results = []
        if self.wecom_url:
            results.append(self._post_json(self.wecom_url, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}))
        if self.dingtalk_url:
            results.append(self._post_json(self.dingtalk_url, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}))
        if self.serverchan_key:
            url = f"https://sctapi.ftqq.com/{parse.quote(self.serverchan_key)}.send"
            results.append(self._post_form(url, {"title": title, "desp": content}))
        sent = sum(bool(item.get("ok")) for item in results)
        if sent and event_key:
            self.backend.set_json(f"notification:{event_key}", {"sent": True}, ttl_seconds=ttl_seconds)
        return {"ok": sent > 0, "sent": sent, "results": results}

    def notify_realtime_payload(self, payload: Dict[str, Any]) -> int:
        sent = 0
        market_date = str(payload.get("market_date") or "")
        for row in payload.get("rows") or []:
            if str(row.get("confirm_status") or "") != "confirmed":
                continue
            name = str(row.get("name") or row.get("code") or "候选股")
            mode = str(row.get("entry_mode_text") or "盘中信号")
            pct = float(row.get("pct_chg") or 0.0)
            result = self.send(
                "盘中确认提醒",
                f"{name} {mode}确认，当前{pct:+.2f}%。请核对可成交性与仓位上限。",
                event_key=f"intraday:{market_date}:{row.get('code')}:{row.get('entry_mode') or mode}",
                ttl_seconds=60 * 60 * 12,
            )
            sent += int(result.get("sent") or 0)
        return sent

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._open(request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST"))

    def _post_form(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = parse.urlencode(payload).encode("utf-8")
        return self._open(request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))

    def _open(self, req: request.Request) -> Dict[str, Any]:
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return {"ok": 200 <= int(response.status) < 300, "status": int(response.status)}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Notification] 发送失败: {exc}")
            return {"ok": False, "error": str(exc)}


__all__ = ["NotificationService"]
