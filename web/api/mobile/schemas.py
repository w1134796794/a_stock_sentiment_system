"""Stable response contracts for mobile clients."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class _FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class MobileError(BaseModel):
    code: str
    message: str
    details: Dict[str, Any] | None = None


class MobileMeta(BaseModel):
    trade_date: str = ""
    generated_at: str = ""
    source_status: str = "ready"
    is_realtime: bool = False
    cache_age_seconds: float | None = None
    request_id: str = ""


class MobileEnvelope(BaseModel):
    ok: bool
    data: Any = None
    meta: MobileMeta = Field(default_factory=MobileMeta)
    error: MobileError | None = None


class WeChatLoginRequest(BaseModel):
    js_code: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=128)
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="wechat_miniprogram", max_length=64)


class WeChatBindRequest(BaseModel):
    binding_ticket: str = Field(min_length=16, max_length=256)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=128)
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="wechat_miniprogram", max_length=64)


class MobileRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=256)


class MarketSummary(_FlexibleModel):
    regime: str = ""
    regime_label: str = ""
    emotion_phase: str = ""
    market_score: float = 0.0
    limit_up_count: int = 0
    limit_down_count: int = 0
    broken_rate: float = 0.0
    amount_yuan: float = 0.0
    position_scale: float = 1.0
    risk_flags: List[str] = Field(default_factory=list)


class CandidateSummary(_FlexibleModel):
    code: str
    name: str = ""
    action_group: str = "暂不参与"
    hit_strategies: List[str] = Field(default_factory=list)
    strategy_consensus: int = 0
    strategy_total: int = 0
    mainline: str = ""
    mainline_confirmed: bool = False
    related_themes: List[str] = Field(default_factory=list)
    sector_strength: float = 0.0
    entry_mode: str = ""
    conclusion: str = ""
    confirmation: str = ""
    invalidation: str = ""
    position: str = ""
    position_cap_pct: float = 0.0
    confidence_grade: str = ""
    expected_return_pct: float = 0.0
    expected_excess_return_pct: float = 0.0


class DashboardData(_FlexibleModel):
    trade_date: str = ""
    market: MarketSummary = Field(default_factory=MarketSummary)
    groups: Dict[str, List[CandidateSummary]] = Field(default_factory=dict)
    counts: Dict[str, int] = Field(default_factory=dict)
    generated_at: str = ""
    data_status: str = "ready"
    data_completeness: float = 0.0


class CandidateListData(_FlexibleModel):
    trade_date: str = ""
    total: int = 0
    offset: int = 0
    limit: int = 20
    items: List[CandidateSummary] = Field(default_factory=list)
    generated_at: str = ""
    data_completeness: float = 0.0


class RealtimeData(_FlexibleModel):
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "cache_empty"
    trade_date: str = ""
    market_date: str = ""
    generated_at: str = ""
    cache_age_seconds: float | None = None


class LeaderData(_FlexibleModel):
    trade_date: str = ""
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "ready"


class LimitupData(_FlexibleModel):
    trade_date: str = ""
    limit_up_count: int = 0
    limit_down_count: int = 0
    max_board_height: int = 0
    echelon: List[Dict[str, Any]] = Field(default_factory=list)
    limit_down: List[Dict[str, Any]] = Field(default_factory=list)


class LhbData(_FlexibleModel):
    trade_date: str = ""
    stocks: List[Dict[str, Any]] = Field(default_factory=list)
    hot_money: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "ready"


class DashboardEnvelope(MobileEnvelope):
    data: DashboardData | None = None


class CandidateListEnvelope(MobileEnvelope):
    data: CandidateListData | None = None


class CandidateEnvelope(MobileEnvelope):
    data: CandidateSummary | None = None


class RealtimeEnvelope(MobileEnvelope):
    data: RealtimeData | None = None


class LeaderEnvelope(MobileEnvelope):
    data: LeaderData | None = None


class LimitupEnvelope(MobileEnvelope):
    data: LimitupData | None = None


class LhbEnvelope(MobileEnvelope):
    data: LhbData | None = None


__all__ = [
    "CandidateEnvelope",
    "CandidateListEnvelope",
    "CandidateSummary",
    "DashboardEnvelope",
    "LeaderEnvelope",
    "LhbEnvelope",
    "LimitupEnvelope",
    "MarketSummary",
    "MobileEnvelope",
    "MobileError",
    "MobileMeta",
    "MobileRefreshRequest",
    "RealtimeEnvelope",
    "WeChatBindRequest",
    "WeChatLoginRequest",
]
