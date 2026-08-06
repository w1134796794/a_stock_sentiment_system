"""Send deduplicated alerts through WeCom, DingTalk or ServerChan."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict
from urllib import parse, request

from loguru import logger

from core.infrastructure.shared_state import get_shared_state_backend


class NotificationService:
    def __init__(self, *, timeout: float = 8.0, backend: Any = None) -> None:
        self.timeout = max(float(timeout), 1.0)
        self.wecom_url = os.getenv("WECOM_WEBHOOK_URL", "").strip()
        self.dingtalk_url = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
        self.serverchan_key = os.getenv("SERVERCHAN_SENDKEY", "").strip()
        self.public_url = os.getenv("APP_PUBLIC_URL", "").strip().rstrip("/")
        self.backend = backend or get_shared_state_backend()

    @property
    def enabled(self) -> bool:
        return bool(self.wecom_url or self.dingtalk_url or self.serverchan_key)

    def status(self) -> Dict[str, Any]:
        channels = {
            "企业微信机器人": bool(self.wecom_url),
            "Server酱个人微信": bool(self.serverchan_key),
            "钉钉机器人": bool(self.dingtalk_url),
        }
        return {
            "enabled": self.enabled,
            "channels": channels,
            "configured_count": sum(channels.values()),
            "public_url_configured": bool(self.public_url),
        }

    def send(self, title: str, content: str, *, event_key: str = "", ttl_seconds: int = 86_400) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "sent": 0, "message": "未配置通知渠道"}
        dedup_key = f"notification:{event_key}" if event_key else ""
        if dedup_key and self.backend.get_json(dedup_key):
            return {"ok": True, "sent": 0, "deduplicated": True}

        lock_key = ""
        lock_token = None
        if event_key:
            digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:24]
            lock_key = f"notification-send:{digest}"
            lock_token = self.backend.acquire_lock(lock_key, max(int(self.timeout * 4), 30))
            if not lock_token:
                return {"ok": True, "sent": 0, "deduplicated": True, "inflight": True}

        try:
            if dedup_key and self.backend.get_json(dedup_key):
                return {"ok": True, "sent": 0, "deduplicated": True}
            results = []
            if self.wecom_url:
                results.append(self._post_json(
                    self.wecom_url,
                    {"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
                ))
            if self.dingtalk_url:
                results.append(self._post_json(
                    self.dingtalk_url,
                    {"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
                ))
            if self.serverchan_key:
                url = f"https://sctapi.ftqq.com/{parse.quote(self.serverchan_key)}.send"
                results.append(self._post_form(url, {"title": title, "desp": content}))
            sent = sum(bool(item.get("ok")) for item in results)
            if sent and dedup_key:
                self.backend.set_json(dedup_key, {"sent": True}, ttl_seconds=ttl_seconds)
            return {"ok": sent > 0, "sent": sent, "results": results}
        finally:
            if lock_key and lock_token:
                try:
                    self.backend.release_lock(lock_key, lock_token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[Notification] 释放去重锁失败: {exc}")

    def notify_realtime_payload(self, payload: Dict[str, Any]) -> int:
        sent = 0
        market_date = str(payload.get("market_date") or "")
        for row in payload.get("rows") or []:
            if str(row.get("confirm_status") or "") != "confirmed":
                continue
            name = str(row.get("name") or row.get("code") or "候选股")
            code = str(row.get("code") or "")
            mode = str(row.get("entry_mode_text") or "盘中信号")
            pct = float(row.get("pct_chg") or 0.0)
            price = float(row.get("last_price") or row.get("entry_price") or 0.0)
            strategy = str(row.get("strategy_name") or row.get("strategy_sources") or "今日决策池")
            sectors = str(row.get("resonance_sectors") or "").strip()
            confirm_time = str(row.get("confirm_time") or row.get("entry_time") or "实时")
            position = str(row.get("suggested_position") or "按风控上限确认")
            lines = [
                f"股票：{name}（{code}）" if code else f"股票：{name}",
                f"信号：{mode}确认",
                f"行情：{price:.2f}，涨幅{pct:+.2f}%" if price > 0 else f"涨幅：{pct:+.2f}%",
                f"时间：{confirm_time}",
                f"策略：{strategy}",
            ]
            if sectors:
                lines.append(f"板块：{sectors}")
            lines.extend([
                f"仓位：{position}",
                "动作：可进入买入确认，请再次核对价格、可成交性和账户风控。",
            ])
            if self.public_url:
                lines.append(f"详情：{self.public_url}/intraday")
            result = self.send(
                f"盘中买点确认：{name}",
                "\n".join(lines),
                event_key=f"intraday:{market_date}:{code}:{row.get('entry_mode') or mode}",
                ttl_seconds=60 * 60 * 12,
            )
            sent += int(result.get("sent") or 0)
        return sent

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._open(request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST"))

    def _post_form(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = parse.urlencode(payload).encode("utf-8")
        return self._open(request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        ))

    def _open(self, req: request.Request) -> Dict[str, Any]:
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return {"ok": 200 <= int(response.status) < 300, "status": int(response.status)}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Notification] 发送失败: {exc}")
            return {"ok": False, "error": str(exc)}


__all__ = ["NotificationService"]
