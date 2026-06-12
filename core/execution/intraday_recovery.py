"""盘中弱转强实时观测编排器。

把「弱转强走弱池」与「盘中实时行情」对照：逐只取 eltdx 实时快照，
以**昨收为基准**计算盘中涨幅，≥阈值即判定转强（v1 仅涨幅判据）。

设计要点：
  - 只读走弱池（dragon_pools.json），**不回写池子、不污染盘后快照**；
  - 信号隔离落盘到 webdata/realtime/{date}.json，带 新增→维持→失效 生命周期；
  - 同一交易日多次手动刷新会在原文件上累积 first_seen / peak_pct，并把
    本轮未再命中的标的标记为「失效」。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

from config.settings import WEB_DATA_DIR, TUSHARE_TOKEN, CACHE_DIR

logger = loguru.logger

REALTIME_DIR = Path(WEB_DATA_DIR) / "realtime"


class IntradayRecoveryMonitor:
    """走弱池盘中转强观测：跑一轮 + 读取当日结果。"""

    def __init__(self, data_manager=None, pool_file: str = None):
        self._dm = data_manager
        self._pool_file = pool_file

    # ------------------------------------------------------------------ #
    # 依赖装配（延迟初始化，避免 web 启动即连行情）
    # ------------------------------------------------------------------ #
    def _data_manager(self):
        if self._dm is None:
            from core.data.data_manager_main import DataManager
            self._dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        return self._dm

    def _strategy(self):
        from core.pattern.weak_to_strong import WeakToStrongStrategy
        kwargs = {"data_manager": self._data_manager()}
        if self._pool_file:
            kwargs["pool_file"] = self._pool_file
        return WeakToStrongStrategy(**kwargs)

    @staticmethod
    def _store_path(date_str: str) -> Path:
        REALTIME_DIR.mkdir(parents=True, exist_ok=True)
        return REALTIME_DIR / f"{date_str}.json"

    # ------------------------------------------------------------------ #
    # 跑一轮
    # ------------------------------------------------------------------ #
    def run_once(self, date_str: str = None, threshold=None) -> Dict[str, Any]:
        """执行一轮盘中观测并把结果合并落盘，返回当日最新结果（含生命周期）。"""
        now = datetime.now()
        date_str = date_str or now.strftime("%Y%m%d")

        strategy = self._strategy()
        round_result = strategy.scan_weakening_intraday(date_str=date_str, threshold=threshold)

        prev = self.load(date_str) or {}
        prev_signals: Dict[str, Any] = prev.get("signals", {}) or {}

        # 本轮观测：code -> 实时涨幅（用于刷新失效标的的当前价）
        observed_by_code = {o["code"]: o for o in round_result.get("observed", [])}
        hit_codes = set()
        signals: Dict[str, Any] = {}

        for hit in round_result.get("hits", []):
            code = hit["code"]
            hit_codes.add(code)
            old = prev_signals.get(code, {})
            first_seen = old.get("first_seen") or hit["time"]
            peak_pct = max(float(old.get("peak_pct", 0) or 0), hit["pct_chg"])
            trend = hit.get("trend") or {}
            signals[code] = {
                "code": code,
                "name": hit["name"],
                "weakening_type": hit["weakening_type"],
                "weakening_date": hit["weakening_date"],
                "monitor_days": hit["monitor_days"],
                "last_price": hit["last_price"],
                "pre_close": hit["pre_close"],
                "open_price": hit["open_price"],
                "current_pct": hit["pct_chg"],
                "open_change_pct": hit["open_change_pct"],
                "peak_pct": round(peak_pct, 4),
                "signal_type": hit["signal_type"],
                "trend_label": trend.get("label", ""),
                "trend": trend,
                "status": "active",
                "first_seen": first_seen,
                "last_seen": hit["time"],
                "hit_rounds": int(old.get("hit_rounds", 0)) + 1,
            }

        # 上一轮命中、本轮未再达标 → 失效（保留记录，刷新当前涨幅）
        for code, old in prev_signals.items():
            if code in hit_codes:
                continue
            rec = dict(old)
            rec["status"] = "expired"
            rec["last_seen"] = round_result.get("time")
            obs = observed_by_code.get(code)
            if obs is not None:
                rec["current_pct"] = obs["pct_chg"]
                rec["last_price"] = obs["last_price"]
            signals[code] = rec

        active = [s for s in signals.values() if s.get("status") == "active"]
        expired = [s for s in signals.values() if s.get("status") != "active"]

        payload = {
            "date": date_str,
            "update_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "threshold": round_result.get("threshold"),
            "pool_size": round_result.get("pool_size"),
            "rounds": int(prev.get("rounds", 0)) + 1,
            "active_count": len(active),
            "expired_count": len(expired),
            "last_round": {
                "time": round_result.get("time"),
                "quotes_ok": round_result.get("quotes_ok"),
                "pool_size": round_result.get("pool_size"),
                "hit_count": len(round_result.get("hits", [])),
                "errors": round_result.get("errors", []),
            },
            "signals": signals,
            "last_observed": round_result.get("observed", []),
        }

        try:
            self._store_path(date_str).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[盘中转强] 落盘失败 {date_str}: {e}")

        logger.info(
            f"[盘中转强] 第{payload['rounds']}轮完成：走弱池{payload['pool_size']}只，"
            f"活跃转强{payload['active_count']}只，失效{payload['expired_count']}只"
        )
        return payload

    # ------------------------------------------------------------------ #
    # 读取当日结果（供前端展示）
    # ------------------------------------------------------------------ #
    def load(self, date_str: str = None) -> Optional[Dict[str, Any]]:
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        path = self._store_path(date_str)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[盘中转强] 读取结果失败 {date_str}: {e}")
            return None

    def view(self, date_str: str = None) -> Dict[str, Any]:
        """整理成便于模板渲染的结构：活跃在前、按当前涨幅降序。"""
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        data = self.load(date_str)
        if not data:
            return {
                "date": date_str, "exists": False, "update_time": "",
                "threshold": None, "pool_size": 0, "rounds": 0,
                "active": [], "expired": [], "observed": [], "last_round": {},
            }
        signals = list((data.get("signals") or {}).values())
        active = sorted(
            [s for s in signals if s.get("status") == "active"],
            key=lambda s: float(s.get("current_pct", 0) or 0), reverse=True,
        )
        expired = sorted(
            [s for s in signals if s.get("status") != "active"],
            key=lambda s: float(s.get("peak_pct", 0) or 0), reverse=True,
        )
        return {
            "date": date_str,
            "exists": True,
            "update_time": data.get("update_time", ""),
            "threshold": data.get("threshold"),
            "pool_size": data.get("pool_size", 0),
            "rounds": data.get("rounds", 0),
            "active": active,
            "expired": expired,
            "observed": data.get("last_observed", []),
            "last_round": data.get("last_round", {}),
        }