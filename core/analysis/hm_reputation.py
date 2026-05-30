"""Sprint F-2：游资信誉名录注册表

定位
====
把 ``data/reputation/hm_reputation.yaml`` 里维护的"游资 → 信誉评分 + 风格标签"
加载为可查询的注册表，供 ``lhb_analyzer`` 在龙虎榜分析时调用：

* **按游资名号查**（来自 ``hm_detail.hm_name``）—— 主路径
* **按营业部关键字查**（来自 ``hm_detail.hm_orgs`` / ``top_list`` 的席位名）—— 兜底，
  支持子串匹配与 ``*`` 前缀通配

设计原则
========
* **纯逻辑、零网络**：只读 YAML，便于单测（Sprint F-6）。
* **未登记即中性**：查不到的席位返回"未知 / 默认信誉"，绝不抛异常。
* **人工权威**：``最终信誉 = 信誉 + 手工调整``，自动评分（路径 C）后续只改 YAML。

本模块对应 brainstorm 的"路径 C 混合方案"的人工锚点部分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import loguru

logger = loguru.logger

# 默认 YAML 位置（config.settings.DATA_DIR / "reputation" / ...）
_DEFAULT_YAML = Path(__file__).resolve().parent.parent.parent / "data" / "reputation" / "hm_reputation.yaml"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class HotMoneyProfile:
    """单个游资的信誉档案。"""
    name: str                              # 名号（权威 key）
    aliases: List[str] = field(default_factory=list)
    orgs: List[str] = field(default_factory=list)   # 营业部关键字（支持 * 前缀通配）
    tag: str = ""                          # 标签：白马接力 / 高度博弈 / 散户伪装 ...
    score: float = 50.0                    # 已含手工调整后的最终信誉 0-100
    style: str = ""                        # 风格一句话
    note: str = ""                         # 经验提示

    def label(self, good_th: float, bad_th: float) -> str:
        """信誉档位标签：白 / 灰 / 黑。"""
        if self.score >= good_th:
            return "白"
        if self.score < bad_th:
            return "黑"
        return "灰"


@dataclass
class ReputationLookup:
    """一次查询结果（命中或未命中都返回此对象，绝不返回 None）。"""
    matched: bool
    name: str                # 命中的名号；未命中时回填原始查询串
    score: float
    label: str               # 白 / 灰 / 黑 / 未知
    tag: str = ""
    style: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

class HotMoneyReputationRegistry:
    """游资信誉注册表。

    用法::

        reg = HotMoneyReputationRegistry.load()
        r = reg.lookup_by_name("章盟主")          # → ReputationLookup(score=85, label="白")
        r = reg.lookup_by_org("华泰证券...武定路") # 营业部兜底
    """

    def __init__(
        self,
        profiles: List[HotMoneyProfile],
        default_score: float = 50.0,
        good_threshold: float = 70.0,
        bad_threshold: float = 40.0,
        version: str = "",
    ):
        self.profiles = profiles
        self.default_score = default_score
        self.good_threshold = good_threshold
        self.bad_threshold = bad_threshold
        self.version = version

        # 名号 / 别名 → profile 的精确索引
        self._by_name: Dict[str, HotMoneyProfile] = {}
        for p in profiles:
            self._by_name[p.name] = p
            for a in p.aliases:
                self._by_name[a] = p

    # -------------------- 构造 --------------------

    @classmethod
    def load(cls, yaml_path: Optional[Path] = None) -> "HotMoneyReputationRegistry":
        """从 YAML 加载。文件缺失 / PyYAML 不可用时返回空注册表（全部走默认中性）。"""
        path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
        if not path.exists():
            logger.warning(f"[HMReputation] 信誉名录不存在: {path}，使用空注册表（全部中性）")
            return cls(profiles=[], version="(empty)")

        try:
            import yaml  # 延迟 import，缺依赖时优雅降级
        except ImportError:
            logger.warning("[HMReputation] 未安装 PyYAML，使用空注册表")
            return cls(profiles=[], version="(no-yaml)")

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[HMReputation] 解析 {path} 失败: {e}")
            return cls(profiles=[], version="(parse-error)")

        default_score = float(raw.get("默认信誉", 50) or 50)
        thresholds = raw.get("阈值", {}) or {}
        good_th = float(thresholds.get("优质", 70) or 70)
        bad_th = float(thresholds.get("高风险", 40) or 40)

        profiles: List[HotMoneyProfile] = []
        for item in raw.get("游资", []) or []:
            if not isinstance(item, dict):
                continue
            base_score = float(item.get("信誉", default_score) or default_score)
            manual = float(item.get("手工调整", 0) or 0)
            score = max(0.0, min(100.0, base_score + manual))
            profiles.append(HotMoneyProfile(
                name=str(item.get("名号", "")).strip(),
                aliases=[str(a).strip() for a in (item.get("别名") or []) if str(a).strip()],
                orgs=[str(o).strip() for o in (item.get("营业部") or []) if str(o).strip()],
                tag=str(item.get("标签", "") or ""),
                score=score,
                style=str(item.get("风格", "") or ""),
                note=str(item.get("备注", "") or ""),
            ))

        logger.info(f"[HMReputation] 加载 {len(profiles)} 个游资档案，版本={raw.get('版本', '?')}")
        return cls(
            profiles=profiles,
            default_score=default_score,
            good_threshold=good_th,
            bad_threshold=bad_th,
            version=str(raw.get("版本", "")),
        )

    # -------------------- 查询 --------------------

    def _unknown(self, query: str) -> ReputationLookup:
        return ReputationLookup(
            matched=False, name=query or "未知",
            score=self.default_score, label="未知",
        )

    def _to_lookup(self, p: HotMoneyProfile) -> ReputationLookup:
        return ReputationLookup(
            matched=True, name=p.name, score=p.score,
            label=p.label(self.good_threshold, self.bad_threshold),
            tag=p.tag, style=p.style, note=p.note,
        )

    def lookup_by_name(self, hm_name: str) -> ReputationLookup:
        """按游资名号 / 别名精确查（来自 hm_detail.hm_name）。"""
        if not hm_name:
            return self._unknown("")
        key = str(hm_name).strip()
        p = self._by_name.get(key)
        if p is not None:
            return self._to_lookup(p)
        # 别名可能带后缀差异，做一次包含匹配
        for name_key, prof in self._by_name.items():
            if name_key and (name_key in key or key in name_key):
                return self._to_lookup(prof)
        return self._unknown(key)

    def lookup_by_org(self, org_text: str) -> ReputationLookup:
        """按营业部关键字兜底查（来自 hm_detail.hm_orgs / top_list 席位名）。

        匹配规则：profile.orgs 里任一关键字命中即算。``*`` 结尾 = 前缀通配。
        """
        if not org_text:
            return self._unknown("")
        text = str(org_text).strip()
        for p in self.profiles:
            for org in p.orgs:
                if self._org_match(org, text):
                    return self._to_lookup(p)
        return self._unknown(text)

    @staticmethod
    def _org_match(pattern: str, text: str) -> bool:
        if not pattern:
            return False
        if pattern.endswith("*"):
            return text.startswith(pattern[:-1]) or (pattern[:-1] in text)
        return pattern in text or text in pattern

    def lookup(self, hm_name: str = "", org_text: str = "") -> ReputationLookup:
        """组合查询：先按名号，未命中再按营业部。"""
        r = self.lookup_by_name(hm_name)
        if r.matched:
            return r
        return self.lookup_by_org(org_text)


__all__ = [
    "HotMoneyProfile",
    "ReputationLookup",
    "HotMoneyReputationRegistry",
]
