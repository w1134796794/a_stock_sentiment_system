"""Historical intraday amount curves grouped by stock liquidity."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd


class MinuteAmountProfileRepository:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            from config.settings import WEB_DATA_DIR

            path = Path(WEB_DATA_DIR) / "models" / "minute_amount_profiles.json"
        self.path = Path(path)
        self._payload: Optional[Dict[str, Any]] = None

    def load(self) -> Dict[str, Any]:
        if self._payload is None:
            try:
                self._payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._payload = {}
        return self._payload

    @property
    def available(self) -> bool:
        return bool((self.load().get("profiles") or {}))

    def expected_fraction(self, previous_amount: float, time_text: str) -> Tuple[Optional[float], int]:
        payload = self.load()
        thresholds = payload.get("amount_thresholds") or []
        bucket = "high"
        if len(thresholds) >= 2:
            if previous_amount <= float(thresholds[0]):
                bucket = "low"
            elif previous_amount <= float(thresholds[1]):
                bucket = "mid"
        profile = (payload.get("profiles") or {}).get(bucket) or {}
        times = sorted(profile)
        eligible = [key for key in times if key <= str(time_text)]
        key = eligible[-1] if eligible else (times[0] if times else "")
        if not key:
            return None, 0
        row = profile.get(key) or {}
        return float(row.get("fraction") or 0.0), int(row.get("samples") or 0)


class MinuteAmountProfileTrainer:
    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        repository: Optional[MinuteAmountProfileRepository] = None,
    ) -> None:
        if cache_dir is None:
            from config.settings import CACHE_DIR

            cache_dir = Path(CACHE_DIR) / "stock" / "tick"
        self.cache_dir = Path(cache_dir)
        self.repository = repository or MinuteAmountProfileRepository()

    def train(self) -> Dict[str, Any]:
        samples = []
        totals = []
        for path in self.cache_dir.glob("*.csv"):
            try:
                frame = pd.read_csv(path, usecols=lambda column: column in {"time", "amount", "close", "volume"})
            except Exception:
                continue
            if frame.empty or "time" not in frame.columns:
                continue
            amount = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0.0)
            if amount.sum() <= 0 and {"close", "volume"}.issubset(frame.columns):
                amount = pd.to_numeric(frame["close"], errors="coerce").fillna(0.0) * pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
            total = float(amount.sum())
            if total <= 0 or len(frame) < 30:
                continue
            time = frame["time"].astype(str).str[-8:].map(lambda value: value + ":00" if len(value) == 5 else value)
            day = pd.DataFrame({"time": time, "amount": amount}).sort_values("time")
            day["fraction"] = day["amount"].cumsum() / total
            day = day[day["time"].between("09:30:00", "10:00:00")]
            if day.empty:
                continue
            totals.append(total)
            samples.append((total, day[["time", "fraction"]]))
        if len(samples) < 20:
            return {"ok": False, "message": "分钟样本不足20个交易股票日", "sample_days": len(samples)}
        low, high = pd.Series(totals).quantile([0.33, 0.67]).tolist()
        records = []
        for total, day in samples:
            bucket = "low" if total <= low else "mid" if total <= high else "high"
            for row in day.itertuples(index=False):
                records.append({"bucket": bucket, "time": row.time, "fraction": row.fraction})
        table = pd.DataFrame(records)
        grouped = table.groupby(["bucket", "time"], as_index=False).agg(
            fraction=("fraction", "median"), samples=("fraction", "size"),
        )
        profiles: Dict[str, Dict[str, Any]] = {}
        for row in grouped.itertuples(index=False):
            profiles.setdefault(row.bucket, {})[row.time] = {
                "fraction": round(float(row.fraction), 6),
                "samples": int(row.samples),
            }
        payload = {
            "schema_version": 1,
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "sample_days": len(samples),
            "amount_thresholds": [float(low), float(high)],
            "profiles": profiles,
        }
        self.repository.path.parent.mkdir(parents=True, exist_ok=True)
        self.repository.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.repository._payload = payload
        return {"ok": True, **payload, "path": str(self.repository.path)}

    def refresh_if_due(self, max_age_days: int = 7) -> Dict[str, Any]:
        if self.repository.path.exists():
            modified = datetime.fromtimestamp(self.repository.path.stat().st_mtime)
            if datetime.now() - modified < timedelta(days=max(int(max_age_days), 1)):
                payload = self.repository.load()
                return {"ok": True, "skipped": True, **payload, "path": str(self.repository.path)}
        return self.train()


__all__ = ["MinuteAmountProfileRepository", "MinuteAmountProfileTrainer"]
