"""
因子权重工具

Phase 2：禁用部分因子后，剩余启用因子的权重必须重新归一化到和为 1，
否则综合分会被系统性拉低。统一在此处理，供 Layer3 复用。
"""
from typing import Dict, Iterable, List, Optional


def normalize_weights(raw_weights: Dict[str, float],
                      enabled_ids: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """
    取启用因子的原始权重，重新归一化到和为 1.0。

    Args:
        raw_weights: {factor_id: weight} 原始权重表（可包含未启用因子）。
        enabled_ids: 启用因子 ID 列表；None 表示 raw_weights 中所有键都启用。

    Returns:
        {factor_id: normalized_weight}，仅含启用因子，权重和为 1.0。
        - 若启用集为空 → 返回 {}。
        - 若启用因子权重总和 <= 0（全 0 或负）→ 退化为等权（每个 1/N）。

    保证：对同一启用集，结果与启用顺序无关；不修改入参。
    """
    if enabled_ids is None:
        selected: List[str] = list(raw_weights.keys())
    else:
        # 保留 enabled_ids 顺序，仅取在 raw_weights 中存在的键
        seen = set()
        selected = []
        for fid in enabled_ids:
            if fid in raw_weights and fid not in seen:
                selected.append(fid)
                seen.add(fid)

    if not selected:
        return {}

    total = sum(float(raw_weights[fid]) for fid in selected)

    if total <= 0:
        equal = 1.0 / len(selected)
        return {fid: equal for fid in selected}

    return {fid: float(raw_weights[fid]) / total for fid in selected}