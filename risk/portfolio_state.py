"""
虚拟账户状态（R-1b）

``PortfolioState`` 是实盘风控与回测共用的"键石"：两边实例化同一个账户对象 +
同一套 ``RiskConfig``，风控规则就不会再三套漂移。

职责：
- 维护现金 / 持仓 / 当日已实现盈亏 / 权益曲线 / 峰值权益
- 盯市（mark-to-market）并刷新持仓最高价（供移动止损）
- 暴露组合层指标：总仓位比例、板块暴露、回撤
- JSON 落盘 / 读取（默认 ``data/cache/portfolio_state.json``）

设计取舍：
- 所有比例型指标都基于"总权益"= 现金 + 持仓市值。
- ``price_map`` 缺失某只股票时，回退用持仓的 ``last_price``（再退到 ``avg_cost``），
  保证实盘日内即使没有最新价也能给出一个保守的暴露估计。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import loguru

logger = loguru.logger


@dataclass
class Position:
    """单只持仓。"""
    code: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    last_price: float = 0.0
    peak_price: float = 0.0           # 持仓期间最高价（移动止损用）
    entry_date: str = ""
    sector: str = ""
    pattern: str = ""

    def market_value(self, price: Optional[float] = None) -> float:
        px = price if (price is not None and price > 0) else self._fallback_price()
        return px * self.shares

    def _fallback_price(self) -> float:
        if self.last_price and self.last_price > 0:
            return self.last_price
        return self.avg_cost

    def unrealized_pnl(self, price: Optional[float] = None) -> float:
        px = price if (price is not None and price > 0) else self._fallback_price()
        return (px - self.avg_cost) * self.shares


@dataclass
class PortfolioState:
    """虚拟账户状态。"""

    initial_capital: float = 100_000.0
    cash: float = 100_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    equity_curve: List[Dict] = field(default_factory=list)   # [{date, equity}]
    peak_equity: float = 100_000.0
    cooldown_until: str = ""          # 回撤熔断冷静期截止交易日（含），YYYYMMDD
    last_mark_date: str = ""

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------
    def position_value(self, price_map: Optional[Dict[str, float]] = None) -> float:
        price_map = price_map or {}
        return sum(
            pos.market_value(price_map.get(code)) for code, pos in self.positions.items()
        )

    def total_equity(self, price_map: Optional[Dict[str, float]] = None) -> float:
        return self.cash + self.position_value(price_map)

    def total_position_ratio(self, price_map: Optional[Dict[str, float]] = None) -> float:
        equity = self.total_equity(price_map)
        if equity <= 0:
            return 0.0
        return self.position_value(price_map) / equity

    def sector_exposure(self, price_map: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """返回 {板块: 占总权益比例}。"""
        price_map = price_map or {}
        equity = self.total_equity(price_map)
        if equity <= 0:
            return {}
        out: Dict[str, float] = {}
        for code, pos in self.positions.items():
            sector = pos.sector or "未知"
            out[sector] = out.get(sector, 0.0) + pos.market_value(price_map.get(code))
        return {k: v / equity for k, v in out.items()}

    def drawdown(self, price_map: Optional[Dict[str, float]] = None) -> float:
        """当前权益相对峰值的回撤（负数；-0.1 表示回撤 10%）。"""
        equity = self.total_equity(price_map)
        peak = max(self.peak_equity, equity)
        if peak <= 0:
            return 0.0
        return (equity - peak) / peak

    # ------------------------------------------------------------------
    # 成交
    # ------------------------------------------------------------------
    def apply_buy(self, code: str, price: float, shares: int, *, fee: float = 0.0,
                  name: str = "", sector: str = "", pattern: str = "",
                  date: str = "") -> None:
        if shares <= 0 or price <= 0:
            return
        cost = price * shares + fee
        self.cash -= cost
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = Position(
                code=code, name=name, shares=shares, avg_cost=price,
                last_price=price, peak_price=price, entry_date=date,
                sector=sector, pattern=pattern,
            )
        else:
            total_shares = pos.shares + shares
            pos.avg_cost = (pos.avg_cost * pos.shares + price * shares) / total_shares
            pos.shares = total_shares
            pos.last_price = price
            pos.peak_price = max(pos.peak_price, price)
            if sector and not pos.sector:
                pos.sector = sector

    def apply_sell(self, code: str, price: float, shares: int, *, fee: float = 0.0) -> float:
        """卖出，返回本笔已实现盈亏（计入 realized_pnl_today）。"""
        pos = self.positions.get(code)
        if pos is None or shares <= 0 or price <= 0:
            return 0.0
        shares = min(shares, pos.shares)
        proceeds = price * shares - fee
        realized = (price - pos.avg_cost) * shares - fee
        self.cash += proceeds
        self.realized_pnl_today += realized
        pos.shares -= shares
        if pos.shares <= 0:
            del self.positions[code]
        else:
            pos.last_price = price
        return realized

    # ------------------------------------------------------------------
    # 盯市 / 日切
    # ------------------------------------------------------------------
    def mark_to_market(self, price_map: Dict[str, float], date: str = "") -> float:
        """用最新价刷新持仓最高价 + 记录权益曲线，返回总权益。"""
        price_map = price_map or {}
        for code, pos in self.positions.items():
            px = price_map.get(code)
            if px and px > 0:
                pos.last_price = px
                pos.peak_price = max(pos.peak_price, px)
        equity = self.total_equity(price_map)
        self.peak_equity = max(self.peak_equity, equity)
        if date:
            self.equity_curve.append({"date": date, "equity": round(equity, 2)})
            self.last_mark_date = date
        return equity

    def reset_daily(self) -> None:
        """日切：清当日已实现盈亏。"""
        self.realized_pnl_today = 0.0

    # ------------------------------------------------------------------
    # 风控适配
    # ------------------------------------------------------------------
    def to_risk_positions(self, price_map: Optional[Dict[str, float]] = None) -> Dict[str, Dict]:
        """投影成 RiskManager 期望的 ``current_positions`` 结构。"""
        price_map = price_map or {}
        out: Dict[str, Dict] = {}
        for code, pos in self.positions.items():
            out[code] = {
                "market_value": pos.market_value(price_map.get(code)),
                "sector": pos.sector,
                "shares": pos.shares,
                "avg_cost": pos.avg_cost,
            }
        return out

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["positions"] = {code: asdict(pos) for code, pos in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "PortfolioState":
        data = dict(data or {})
        raw_positions = data.pop("positions", {}) or {}
        state = cls(**{k: v for k, v in data.items() if k in {
            "initial_capital", "cash", "realized_pnl_today", "equity_curve",
            "peak_equity", "cooldown_until", "last_mark_date",
        }})
        for code, pd_ in raw_positions.items():
            state.positions[code] = Position(**{
                k: v for k, v in pd_.items()
                if k in Position.__dataclass_fields__
            })
        return state

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path, initial_capital: float = 100_000.0) -> "PortfolioState":
        """读取落盘状态；文件缺失/损坏时返回一个全新空账户。"""
        p = Path(path)
        if not p.exists():
            return cls(initial_capital=initial_capital, cash=initial_capital,
                       peak_equity=initial_capital)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception as e:  # pragma: no cover - 文件容错
            logger.warning(f"[PortfolioState] 读取 {p} 失败，使用空账户: {e}")
            return cls(initial_capital=initial_capital, cash=initial_capital,
                       peak_equity=initial_capital)

    @classmethod
    def new(cls, initial_capital: float) -> "PortfolioState":
        return cls(initial_capital=initial_capital, cash=initial_capital,
                   peak_equity=initial_capital)


__all__ = ["PortfolioState", "Position"]

