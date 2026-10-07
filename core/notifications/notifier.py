"""Send deduplicated alerts through WeCom, DingTalk or ServerChan."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List
from urllib import parse, request

from loguru import logger

from core.factors.sector_taxonomy import theme_cluster_name
from core.infrastructure.shared_state import get_shared_state_backend


class NotificationService:
    def __init__(self, *, timeout: float = 8.0, backend: Any = None) -> None:
        self.timeout = max(float(timeout), 1.0)
        self.retry_seconds = 30.0
        self.wecom_url = os.getenv("WECOM_WEBHOOK_URL", "").strip()
        self.dingtalk_url = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
        legacy_key = os.getenv("SERVERCHAN_SENDKEY", "").strip()
        configured_keys = os.getenv("SERVERCHAN_SENDKEYS", "").strip()
        self.serverchan_keys = self._parse_serverchan_keys(configured_keys, legacy_key)
        self.serverchan_key = self.serverchan_keys[0] if self.serverchan_keys else ""
        self.public_url = os.getenv("APP_PUBLIC_URL", "").strip().rstrip("/")
        self.backend = backend or get_shared_state_backend()

    @property
    def enabled(self) -> bool:
        return bool(self.wecom_url or self.dingtalk_url or self._active_serverchan_keys())

    def status(self) -> Dict[str, Any]:
        serverchan_keys = self._active_serverchan_keys()
        channels = {
            "企业微信机器人": bool(self.wecom_url),
            "Server酱个人微信": bool(serverchan_keys),
            "钉钉机器人": bool(self.dingtalk_url),
        }
        return {
            "enabled": self.enabled,
            "channels": channels,
            "configured_count": sum(channels.values()),
            "serverchan_recipient_count": len(serverchan_keys),
            "configured_endpoint_count": (
                int(bool(self.wecom_url)) + int(bool(self.dingtalk_url)) + len(serverchan_keys)
            ),
            "public_url_configured": bool(self.public_url),
        }

    def send(self, title: str, content: str, *, event_key: str = "", ttl_seconds: int = 86_400) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "sent": 0, "message": "未配置通知渠道"}
        dedup_key = f"notification:{event_key}" if event_key else ""
        endpoints = []
        for channel, url in (("企业微信", self.wecom_url), ("钉钉", self.dingtalk_url)):
            if url:
                endpoints.append((channel, url, False))
        endpoints.extend(("Server酱", f"https://sctapi.ftqq.com/{parse.quote(key)}.send", True)
                         for key in self._active_serverchan_keys())

        lock_key = ""
        lock_token = None
        if event_key:
            digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:24]
            lock_key = f"notification-send:{digest}"
            lock_token = self.backend.acquire_lock(lock_key, max(int(self.timeout * (len(endpoints) + 1)) + 30, 30))
            if not lock_token:
                return {"ok": False, "sent": 0, "all_delivered": False, "inflight": True}

        try:
            results = []
            for recipient, (channel, url, form) in enumerate(endpoints, start=1):
                endpoint_key = dedup_key + ":" + hashlib.sha256(url.encode()).hexdigest()[:24] if dedup_key else ""
                previous = self.backend.get_json(endpoint_key) or {} if endpoint_key else {}
                if previous.get("sent"):
                    results.append({"ok": True, "deduplicated": True, "channel": channel, "recipient": recipient})
                    continue
                if float(previous.get("retry_at") or 0) > time.time():
                    results.append({"ok": False, "retry_pending": True, "channel": channel, "recipient": recipient})
                    continue
                try:
                    result = (self._post_form(url, {"title": title, "desp": content}) if form else
                              self._post_json(url, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}))
                except Exception:
                    result = {"ok": False, "message": "通知渠道请求失败"}
                if endpoint_key:
                    attempts = int(previous.get("attempts") or 0) + 1
                    self.backend.set_json(endpoint_key, {"sent": bool(result.get("ok")), "attempts": attempts,
                                          "retry_at": time.time() + min(self.retry_seconds * 2 ** min(attempts-1, 6), 1800)},
                                          ttl_seconds=ttl_seconds)
                results.append({"channel": channel, "recipient": recipient, **result})
            sent = sum(bool(item.get("ok")) and not item.get("deduplicated", False) for item in results)
            complete = all(item.get("ok") for item in results)
            return {"ok": complete, "all_delivered": complete, "sent": sent,
                    "deduplicated": complete and not sent, "results": results}
        finally:
            if lock_key and lock_token:
                try:
                    self.backend.release_lock(lock_key, lock_token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[Notification] 释放去重锁失败: {exc}")

    @staticmethod
    def _parse_serverchan_keys(*values: str) -> List[str]:
        keys: List[str] = []
        for value in values:
            for key in re.split(r"[,;\s]+", str(value or "").strip()):
                if key and key not in keys:
                    keys.append(key)
        return keys

    def _active_serverchan_keys(self) -> List[str]:
        # Keep ``serverchan_key`` compatible with older integrations and tests
        # that assign the legacy single-key attribute after construction.
        return self._parse_serverchan_keys(
            str(getattr(self, "serverchan_key", "") or ""),
            *[str(key) for key in getattr(self, "serverchan_keys", [])],
        )

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
                pushed_codes = list(pushed.get("codes") or [])
                if pushed_count >= cluster_limit and code not in pushed_codes:
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
            candidate_date = str(payload.get("candidate_date") or row.get("candidate_date")
                                 or row.get("trade_date") or "").strip()
            if candidate_date:
                lines.append(f"候选日：{candidate_date}；行情日：{market_date}")
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
                leader_rank = self._notification_number(row.get("pool_rank"))
                leader_score = self._notification_number(row.get("leader_score"))
                parts = []
                if leader_rank is not None and leader_rank > 0:
                    parts.append(f"池排名#{leader_rank:.0f}")
                if leader_score is not None:
                    parts.append(f"龙头评分{leader_score:.1f}")
                if parts:
                    lines.append("龙头位置：" + "，".join(parts))
                source_rank = self._notification_number(row.get("source_rank"))
                if source_rank is not None and source_rank > 0:
                    lines.append(f"候选名次：#{source_rank:.0f}（仅作观察顺序）")
                dimensions = (
                    ("板块地位", "sector_status_score"),
                    ("市场辨识度", "market_status_score"),
                    ("身份持续性", "continuity_score"),
                    ("资金认可", "capital_recognition_score"),
                    ("接力安全", "safety_score"),
                )
                scores = [f"{label}{value:.0f}" for label, key in dimensions
                          if (value := self._notification_number(row.get(key))) is not None]
                if scores:
                    lines.append("候选日评分：" + " / ".join(scores))
                limit_progress = self._notification_number(row.get("limit_progress"))
                limit_pct = self._notification_number(row.get("limit_pct"))
                if limit_progress is not None and 0 <= limit_progress <= 1:
                    board = f"{limit_pct:g}cm" if limit_pct is not None and limit_pct > 0 else ""
                    lines.append(f"候选日涨停进度：{limit_progress * 100:.0f}%" + (f"（{board}）" if board else ""))
                evidence = row.get("evidence") or {}
                if isinstance(evidence, dict):
                    passed = [str(label) for label, value in evidence.items() if value is True]
                    if passed:
                        lines.append("身份依据：" + "、".join(passed[:4]))
            if sectors:
                lines.append("关联板块：" + "、".join(
                    part.strip() for part in sectors.replace("，", ",").split(",")[:5] if part.strip()
                ))
            open_gap = self._notification_number(row.get("open_gap_pct"))
            if open_gap is not None:
                lines.append(f"今日开盘：{open_gap:+.2f}%")
            reason = str(row.get("reason") or "").strip()
            if reason:
                lines.append(f"盘中确认依据：{reason}")
            structure = row.get("structure") or {}
            if structure:
                lines.append(f"事件日期：{structure.get('event_date', '')}")
                protection = self._notification_number(structure.get("protection"))
                if protection is not None and protection > 0:
                    lines.append(f"结构保护价：{protection:.2f}")
                lines.append("盘中结构确认，收盘形态仍需收盘后核验。")
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
                row["notification_result"] = result
                if cluster and delivered and code not in pushed_codes:
                    self.backend.set_json(
                        cluster_lock_key + ":count",
                        {"count": pushed_count + 1, "codes": [*pushed_codes, code], "cluster": cluster, "market_date": market_date},
                        ttl_seconds=60 * 60 * 24,
                    )
            finally:
                if cluster_lock_key and cluster_lock_token:
                    self.backend.release_lock(cluster_lock_key, cluster_lock_token)
        return sent

    @staticmethod
    def _notification_number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

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
