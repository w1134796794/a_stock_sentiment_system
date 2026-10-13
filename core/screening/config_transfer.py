"""Portable effective strategy bundles with preview and optimistic concurrency."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time
from typing import Any, Mapping

import yaml

from core.screening.strategy_profiles import StrategyProfileRepository, STRATEGY_ID, _LOCK


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _changes(before: Any, after: Any, path: str = "") -> list[dict]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes = []
        for key in sorted(set(before) | set(after)):
            changes.extend(_changes(before.get(key), after.get(key), f"{path}.{key}" if path else key))
        return changes
    return [] if before == after else [{"field": path, "local": before, "incoming": after}]


class StrategyConfigTransfer:
    def __init__(self, repository: StrategyProfileRepository | None = None):
        self.repository = repository or StrategyProfileRepository()

    def export(self) -> dict:
        with _LOCK:
            repository = self.repository.frozen()
            profiles = {row["id"]: row for row in repository.list_profiles()}
            bases = repository._base_profiles()
            effective = {key: repository.resolve(key) for key in profiles}
        content = {"strategies": profiles, "base_profiles": bases}
        source = self.repository.path if self.repository.path.exists() else self.repository.default_path
        return {"kind": "strategy_effective_config", "schema_version": 1, **content,
                "bundle_version": _fingerprint(content),
                "effective_version": _fingerprint(effective),
                "strategy_versions": {key: row["version"] for key, row in profiles.items()},
                "source_path": str(source), "target_path": str(self.repository.path)}

    def _candidate(self, bundle: Mapping) -> StrategyProfileRepository:
        if not isinstance(bundle, Mapping):
            raise ValueError("配置包必须是JSON对象")
        if bundle.get("kind") != "strategy_effective_config" or bundle.get("schema_version") != 1:
            raise ValueError("不支持的策略配置包格式或版本")
        strategies, bases = bundle.get("strategies"), bundle.get("base_profiles")
        if not isinstance(strategies, dict) or not strategies or not isinstance(bases, dict):
            raise ValueError("配置包必须包含策略和基础方案")
        if len(strategies) > 200 or any(not isinstance(value, dict) for value in bases.values()):
            raise ValueError("策略数量过多或基础方案格式无效")
        if bundle.get("bundle_version") != _fingerprint({"strategies": strategies, "base_profiles": bases}):
            raise ValueError("配置包校验失败，内容与导出版本不一致")
        payload = deepcopy(self.repository._payload())
        payload["base_profiles"] = {**self.repository._base_profiles(), **deepcopy(bases)}
        candidate = StrategyProfileRepository(self.repository.path, self.repository.base_profile_path)
        candidate.default_path = self.repository.default_path
        candidate._payload = lambda: deepcopy(payload)
        candidate._base_profiles = lambda: deepcopy(payload["base_profiles"])
        imported_primary = []
        for key, raw in strategies.items():
            if not STRATEGY_ID.fullmatch(key) or not isinstance(raw, dict) or raw.get("id") != key:
                raise ValueError(f"策略标识或数据无效: {key}")
            normalized = candidate._validate(key, raw)
            # Keep exported rule metadata and numeric representations exactly;
            # editor normalization adds optional empty fields and drops zero
            # weights, which would change otherwise identical version hashes.
            for field in ("required_filters", "priority_filters", "exclusion_filters",
                          "evidence_rules", "veto_rules", "ranking_factors"):
                normalized[field] = deepcopy(raw.get(field) or [])
            existing = (payload.get("strategies") or {}).get(key) or {}
            normalized["protected"] = bool(existing.get("protected") or raw.get("protected"))
            payload.setdefault("strategies", {})[key] = normalized
            if normalized["primary"]:
                imported_primary.append(key)
        if len(imported_primary) > 1:
            raise ValueError("配置包不能包含多个自动任务主方案")
        if imported_primary:
            for key, raw in payload["strategies"].items():
                if key not in imported_primary:
                    raw["primary"] = False
        # Check the complete resolved contract, including inherited ranking/model settings.
        for key in strategies:
            if candidate.get_profile(key)["version"] != strategies[key].get("version"):
                raise ValueError(f"策略 {key} 规范化后版本不一致，请重新导出配置")
        return candidate

    def compare(self, bundle: Mapping) -> dict:
        with _LOCK:
            local = self.export()
            candidate = self._candidate(bundle)
            current = self.repository.frozen()
            incoming = StrategyConfigTransfer(candidate).export()
            rows = []
            for key in sorted(set(local["strategies"]) | set(incoming["strategies"])):
                before = current.get_profile(key)
                after = candidate.get_profile(key)
                changes = _changes({"profile": before, "resolved": current.resolve(key) if before else None},
                                   {"profile": after, "resolved": candidate.resolve(key) if after else None})
                rows.append({"id": key, "name": (after or before)["name"],
                    "local_version": (before or {}).get("version"),
                    "incoming_version": (after or {}).get("version"),
                    "status": "added" if before is None else "changed" if changes else "same",
                    "changes": changes})
            return {"current_version": local["effective_version"],
                    "result_version": incoming["effective_version"],
                    "source_version": bundle.get("effective_version"),
                    "changed_count": sum(row["status"] != "same" for row in rows), "strategies": rows,
                    "retained_local_ids": sorted(set(local["strategies"]) - set(bundle["strategies"]))}

    def apply(self, bundle: Mapping, expected_version: str) -> dict:
        with _LOCK:
            comparison = self.compare(bundle)
            if not expected_version or comparison["current_version"] != expected_version:
                raise ValueError("本地配置已变化，请重新比较后再导入")
            candidate = self._candidate(bundle)
            backup = self.repository.path.with_name(f"{self.repository.path.stem}.backup-{time.time_ns()}.yaml")
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(yaml.safe_dump(self.repository._payload(), allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.repository._write(candidate._payload())
            return {**comparison, "backup_path": str(backup), "effective_version": self.export()["effective_version"]}
