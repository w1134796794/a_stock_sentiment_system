"""Local stock industry and concept profiles for web display."""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List


_LOW_SIGNAL_CONCEPTS = {
    "融资融券", "沪股通", "深股通", "国企改革", "MSCI概念",
    "富时罗素概念", "标普道琼斯A股",
}


def _code6(value: object) -> str:
    text = str(value or "").strip().split(".")[0]
    return text.zfill(6) if text.isdigit() else text


def _append_unique(values: List[str], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


@lru_cache(maxsize=8192)
def _stock_profile_cached(code: str, cache_dir_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    cache_dir = Path(cache_dir_text)
    basic_industries = _basic_industry_map(cache_dir_text)
    industries: List[str] = []
    concepts: List[str] = []
    _append_unique(industries, basic_industries.get(code))

    membership_dir = cache_dir / "sector" / "stock_sectors"
    try:
        files = sorted(membership_dir.glob(f"{code}.*.csv"))
    except OSError:
        files = []
    for path in files[:1]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    sector_name = row.get("name") or row.get("sector_name") or row.get("板块名称")
                    sector_type = str(row.get("type") or row.get("sector_type") or "").strip().upper()
                    if sector_type in {"I", "行业"}:
                        _append_unique(industries, sector_name)
                    elif sector_type in {"N", "概念"}:
                        _append_unique(concepts, sector_name)
        except OSError:
            continue
    return tuple(industries), tuple(concepts)


@lru_cache(maxsize=8)
def _basic_industry_map(cache_dir_text: str) -> Dict[str, str]:
    path = Path(cache_dir_text) / "market" / "stock_basic.csv"
    mapping: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = _code6(row.get("symbol") or row.get("ts_code") or row.get("code"))
                industry = str(row.get("industry") or row.get("所属行业") or "").strip()
                if code and industry:
                    mapping[code] = industry
    except OSError:
        pass
    return mapping


def load_stock_profiles(codes: Iterable[object], cache_dir: Path) -> Dict[str, Dict[str, List[str]]]:
    """Read prefetched per-stock THS memberships without making network calls."""
    normalized = list(dict.fromkeys(_code6(code) for code in codes if _code6(code)))
    cache_dir_text = str(Path(cache_dir).resolve())
    profiles: Dict[str, Dict[str, List[str]]] = {}

    for code in normalized:
        industries, concepts = _stock_profile_cached(code, cache_dir_text)
        profiles[code] = {"industries": list(industries), "concepts": list(concepts)}
    return profiles


def _split_sector_names(value: object) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        text = str(value or "").replace("，", ",").replace("/", ",")
        raw = text.split(",")
    values: List[str] = []
    for item in raw:
        _append_unique(values, item)
    return values


def _prioritize(values: List[str], resonance: List[str], limit: int, *, concepts: bool = False) -> List[str]:
    clean = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    preferred_pool = [value for value in clean if not concepts or value not in _LOW_SIGNAL_CONCEPTS]
    if not preferred_pool:
        preferred_pool = clean
    ordered = [value for value in resonance if value in preferred_pool]
    ordered.extend(value for value in preferred_pool if value not in ordered)
    return ordered[: max(int(limit), 0)]


def enrich_stock_sector_labels(
    rows: Iterable[Dict[str, Any]],
    cache_dir: Path,
    *,
    industry_limit: int = 2,
    concept_limit: int = 3,
) -> None:
    """Attach compact industry/concept tags using prefetched local memberships only."""
    row_list = [row for row in rows or [] if isinstance(row, dict)]
    codes = [
        _code6(row.get("code") or row.get("stock_code") or row.get("股票代码"))
        for row in row_list
    ]
    profiles = load_stock_profiles(codes, cache_dir)
    for row, code in zip(row_list, codes):
        profile = profiles.get(code) or {"industries": [], "concepts": []}
        resonance = _split_sector_names(row.get("resonance_sectors"))
        industries = _prioritize(
            list(profile.get("industries") or []), resonance, industry_limit,
        )
        concepts = _prioritize(
            list(profile.get("concepts") or []), resonance, concept_limit, concepts=True,
        )
        tags = [
            {"name": name, "type": "行业", "resonant": name in resonance}
            for name in industries
        ]
        tags.extend(
            {"name": name, "type": "概念", "resonant": name in resonance}
            for name in concepts
        )
        known = set(industries) | set(concepts)
        for name in resonance:
            if name in known or name in _LOW_SIGNAL_CONCEPTS:
                continue
            tags.append({"name": name, "type": "共振", "resonant": True})
            if len(tags) >= industry_limit + concept_limit:
                break
        row["industry_names"] = industries
        row["concept_names"] = concepts
        row["sector_tags"] = tags[: industry_limit + concept_limit]


def clear_stock_profile_cache() -> None:
    _basic_industry_map.cache_clear()
    _stock_profile_cached.cache_clear()
