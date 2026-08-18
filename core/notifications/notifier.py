"""Send deduplicated alerts through WeCom, DingTalk or ServerChan."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from typing import Any, Dict, List
from urllib import parse, request

from loguru import logger

from core.factors.sector_taxonomy import theme_cluster_name
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
        profile = str(payload.get("profile") or "")
        observation_source = str(payload.get("observation_source") or "")
        payload_strategy = payload.get("strategy") or {}
        payload_strategy_name = (
            str(payload_strategy.get("name") or "")
            if isinstance(payload_strategy, dict)
            else ""
        )
        confirmed_rows = [
            row for row in payload.get("rows") or []
            if str(row.get("confirm_status") or row.get("status") or "") == "confirmed"
        ]
        confirmed_rows.sort(key=self._notification_priority, reverse=True)
        cluster_counts = Counter(self._notification_cluster(row) for row in confirmed_rows)
        cluster_counts.pop("", None)
        cluster_limit = self._realtime_cluster_limit(payload)
        for row in confirmed_rows:
            status = str(row.get("confirm_status") or row.get("status") or "")
            if status != "confirmed":
                continue
            name = str(row.get("name") or row.get("code") or "候选股")
            code = str(row.get("code") or "")
            mode = str(row.get("entry_mode_text") or "盘中信号")
            pct = float(row.get("pct_chg") or row.get("change_pct") or 0.0)
            price = float(row.get("last_price") or row.get("entry_price") or 0.0)
            is_leader = bool(
                row.get("is_leader_observation")
                or profile == "leader_pool"
                or observation_source == "leader_pool"
            )
            strategy = str(
                row.get("strategy_name")
                or row.get("strategy_sources")
                or payload_strategy_name
                or ("近期龙头池" if is_leader else "今日决策池")
            )
            sectors = str(row.get("resonance_sectors") or "").strip()
            confirm_time = str(row.get("confirm_time") or row.get("entry_time") or "实时")
            position = str(row.get("suggested_position") or "按风控上限确认")
            cluster = self._notification_cluster(row)
            cluster_lock_key = ""
            cluster_lock_token = None
            if cluster:
                cluster_lock_key = f"notification-cluster:{market_date}:{cluster}"
                cluster_lock_token = self.backend.acquire_lock(cluster_lock_key, 30)
                if not cluster_lock_token:
                    row["notification_status"] = "同主题推送正在处理"
                    continue
                pushed = self.backend.get_json(cluster_lock_key + ":count") or {}
                pushed_count = int(pushed.get("count") or 0) if isinstance(pushed, dict) else 0
                if pushed_count >= cluster_limit:
                    row["notification_status"] = "同主题推送已达上限"
                    row["notification_skip_reason"] = (
                        f"{cluster}当日已推送{pushed_count}只，当前市场同主题上限{cluster_limit}只"
                    )
                    self.backend.release_lock(cluster_lock_key, cluster_lock_token)
                    continue
            lines = [
                f"股票：{name}（{code}）" if code else f"股票：{name}",
                f"信号：{mode}确认",
                f"行情：{price:.2f}，涨幅{pct:+.2f}%" if price > 0 else f"涨幅：{pct:+.2f}%",
                f"时间：{confirm_time}",
                f"策略：{strategy}",
            ]
            if is_leader:
                roles = row.get("leader_roles") or []
                if isinstance(roles, str):
                    roles = [item.strip() for item in roles.split(",") if item.strip()]
                role_text = "、".join(str(item) for item in roles if item) or str(
                    row.get("primary_role") or row.get("pool_type") or "近期龙头"
                )
                lifecycle = str(row.get("lifecycle_state") or "")
                leader_age = str(row.get("leader_time_label") or "")
                lines.append(f"龙头身份：{role_text}")
                if lifecycle or leader_age:
                    lines.append(f"龙头阶段：{'，'.join(item for item in (lifecycle, leader_age) if item)}")
            if sectors:
                lines.append(f"板块：{sectors}")
            if cluster:
                lines.append(f"风险主题簇：{cluster}")
                if cluster_counts[cluster] > cluster_limit:
                    lines.append(
                        f"拥挤控制：同主题{cluster_counts[cluster]}个确认信号，仅推送优先级前{cluster_limit}只"
                    )
            lines.extend([
                f"仓位：{position}",
                "动作：可进入买入确认，请再次核对价格、可成交性和账户风控。",
            ])
            if self.public_url:
                lines.append(f"详情：{self.public_url}/intraday")
            try:
                result = self.send(
                    f"{'龙头盘中转强确认' if is_leader else '盘中买点确认'}：{name}",
                    "\n".join(lines),
                    event_key=f"intraday:{market_date}:{code}:{row.get('entry_mode') or mode}",
                    ttl_seconds=60 * 60 * 12,
                )
                delivered = int(result.get("sent") or 0)
                sent += delivered
                row["notification_status"] = "已推送" if delivered else "已去重或渠道不可用"
                if cluster and delivered:
                    self.backend.set_json(
                        cluster_lock_key + ":count",
                        {"count": pushed_count + 1, "cluster": cluster, "market_date": market_date},
                        ttl_seconds=60 * 60 * 24,
                    )
            finally:
                if cluster_lock_key and cluster_lock_token:
                    self.backend.release_lock(cluster_lock_key, cluster_lock_token)
        return sent

    @staticmethod
    def _notification_priority(row: Dict[str, Any]) -> tuple[float, ...]:
        def number(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        consensus = row.get("strategy_consensus_count")
        if consensus in (None, ""):
            sources = row.get("strategy_sources") or row.get("命中策略") or []
            consensus = len(sources) if isinstance(sources, (list, tuple)) else 1
        return (
            1.0 if row.get("is_leader_observation") else 0.0,
            number(consensus),
            number(row.get("leader_score") or row.get("pool_score")),
            number(row.get("score") or row.get("综合评分")),
            number(row.get("sector_strength") or row.get("板块强度")),
        )

    @staticmethod
    def _notification_cluster(row: Dict[str, Any]) -> str:
        explicit = str(row.get("主题簇") or row.get("crowding_cluster") or "").strip()
        if explicit:
            return explicit
        labels: List[str] = []
        for key in ("所属主线", "mainline_name", "resonance_sectors", "sector_names", "所属板块"):
            value = row.get(key)
            if isinstance(value, (list, tuple, set)):
                labels.extend(str(item).strip() for item in value if str(item).strip())
            elif value:
                labels.extend(
                    item.strip() for item in str(value).replace("，", ",").split(",") if item.strip()
                )
        return theme_cluster_name(labels)

    @staticmethod
    def _realtime_cluster_limit(payload: Dict[str, Any]) -> int:
        context = payload.get("market_context") or payload.get("market") or {}
        if not isinstance(context, dict):
            context = {}
        regime = str(
            payload.get("market_regime") or payload.get("regime")
            or context.get("regime") or context.get("regime_label") or ""
        )
        try:
            score = float(payload.get("market_score") or context.get("market_score") or 50)
        except (TypeError, ValueError):
            score = 50.0
        if score < 45 or any(word in regime for word in ("弱", "退潮", "冰点")):
            return 1
        if score >= 65 or any(word in regime for word in ("强", "活跃")):
            return 3
        return 2

    def notify_exit_signal(
        self,
        position: Dict[str, Any],
        decision: Dict[str, Any],
        *,
        signal_date: str = "",
    ) -> Dict[str, Any]:
        """Send one alert when a holding enters reduce/sell/blocked state."""
        action = str(decision.get("action") or "watch")
        if action not in {"reduce", "sell", "blocked"}:
            return {"ok": True, "sent": 0, "skipped": True}
        name = str(position.get("name") or position.get("stock_name") or position.get("code") or "持仓")
        code = str(position.get("code") or "")
        label = str(decision.get("action_label") or action)
        current = float(decision.get("current_price") or 0.0)
        protect = float(decision.get("protect_price") or 0.0)
        pnl_pct = float(decision.get("pnl_pct") or 0.0)
        reasons = decision.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        lines = [
            f"股票：{name}（{code}）" if code else f"股票：{name}",
            f"建议动作：{label}",
            f"持仓收益：{pnl_pct:+.2f}%",
            f"当前价格：{current:.2f}" if current > 0 else "当前价格：--",
        ]
        if protect > 0:
            lines.append(f"保护价格：{protect:.2f}")
        lines.extend([
            f"市场：{decision.get('market_state') or '待确认'}",
            f"板块：{decision.get('sector_state') or '待确认'}",
            "原因：",
        ])
        lines.extend(f"{index}. {reason}" for index, reason in enumerate(reasons[:4], start=1))
        lines.append(f"当前可卖：{'是' if decision.get('can_sell') else '否，受T+1或交易状态限制'}")
        if self.public_url:
            lines.append(f"详情：{self.public_url}/portfolio")
        title_prefix = "持仓风险预警" if action == "blocked" else "持仓卖出提醒"
        return self.send(
            f"{title_prefix}：{name}",
            "\n".join(lines),
            event_key=f"portfolio-exit:{signal_date}:{position.get('id')}:{action}",
            ttl_seconds=60 * 60 * 24,
        )

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
