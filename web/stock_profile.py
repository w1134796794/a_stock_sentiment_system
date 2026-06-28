"""Local stock industry and concept profiles for web display."""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List


def _code6(value: object) -> str:
    text = str(value or "").strip().split(".")[0]
    return text.zfill(6) if text.isdigit() else text


def _append_unique(values: List[str], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


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
    basic_industries = _basic_industry_map(str(Path(cache_dir).resolve()))
    membership_dir = Path(cache_dir) / "sector" / "stock_sectors"
    profiles: Dict[str, Dict[str, List[str]]] = {}

    for code in normalized:
        industries: List[str] = []
        concepts: List[str] = []
        _append_unique(industries, basic_industries.get(code))

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

        profiles[code] = {"industries": industries, "concepts": concepts}
    return profiles


def clear_stock_profile_cache() -> None:
    _basic_industry_map.cache_clear()
