"""
统一风控配置（R-1a）

把原本散落在三处的风控参数收敛到唯一来源，杜绝"回测一套、风控包一套、
Layer4 又一套"导致的规则漂移：

- ``backtest/backtest_engine.py`` 的 ``BacktestConfig``（止损/止盈/费用/仓位）
- ``risk/risk_manager.py`` 的 ``RiskLimits``（组合层硬约束）
- ``core/pipeline/layer4_trade_plan.py`` 的启发式仓位/止损

加载优先级：``config/risk_control.yaml`` > 代码默认值。YAML 缺失时静默回退默认值，
不会破坏既有运行（PyYAML 已是项目硬依赖）。

下游用法::

    from risk.risk_config import RiskConfig
    cfg = RiskConfig.load()              # 读 config/risk_control.yaml，缺失则用默认
    limits = cfg.to_risk_limits()        # 喂给 RiskManager
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

logger = loguru.logger

# 默认 YAML 路径：config/risk_control.yaml
_DEFAULT_YAML = Path(__file__).resolve().parent.parent / "config" / "risk_control.yaml"


@dataclass
class RiskConfig:
    """风控统一配置（单一事实来源）。"""

    # ---- 账户 ----
    initial_capital: float = 100_000.0

    # ---- 组合层：仓位限制 ----
    max_position_per_stock: float = 0.20      # 单票最大仓位
    max_total_position: float = 0.80          # 总仓位上限
    min_cash_ratio: float = 0.20              # 最小现金比例
    max_positions: int = 8                    # 最多同时持仓只数

    # ---- 组合层：板块集中度 ----
    max_sector_concentration: float = 0.40    # 单一板块最大仓位
    max_correlated_positions: int = 3         # 相关板块最多持仓数

    # ---- 个股层：止损止盈（回测/实盘统一）----
    hard_stop_loss: float = 0.05              # 硬止损
    trailing_stop: float = 0.08               # 移动止损（从最高点回撤）
    trailing_activation: float = 0.05         # 盈利多少后启动移动止损
    take_profit: float = 0.10                 # 基础止盈
    time_stop_days: int = 5                   # 时间止损天数
    time_stop_profit_threshold: float = 0.02  # 时间止损时的盈利下限

    # ---- 流动性 ----
    min_daily_amount: float = 1.0e7           # 最小日成交额（元）
    max_volume_participation: float = 0.10    # 持仓不超过日成交额比例

    # ---- 账户层：熔断 / kill switch（R-3）----
    max_daily_loss: float = 0.03              # 当日已实现亏损达此比例 → 停开新仓
    max_drawdown: float = 0.15                # 权益较峰值回撤达此比例 → 强制降仓
    drawdown_reduce_to: float = 0.30          # 触发回撤熔断后总仓位降到此上限
    cooldown_days: int = 2                    # 回撤熔断后的冷静期（交易日）
    freeze_cycles: List[str] = field(default_factory=lambda: ["冰点期", "退潮期"])
    freeze_position_cap: float = 0.20         # 情绪冰点/退潮时的总仓位封顶

    # ---- 交易成本（回测）----
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.001
    slippage: float = 0.002
    min_holding_days: int = 1                 # T+1

    # ---- 凯利仓位占位（R-4 落地用）----
    kelly_fraction: float = 0.5               # 半凯利
    kelly_min_samples: int = 20               # 单模式最小样本数，不足回退
    kelly_max_position: float = 0.25          # 凯利结果单票封顶

    # ------------------------------------------------------------------
    # 构造 / 加载
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RiskConfig":
        """从字典构造，未知键忽略，缺失键用默认值。"""
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        unknown = set(data) - known
        if unknown:
            logger.debug(f"[RiskConfig] 忽略未知配置键: {sorted(unknown)}")
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RiskConfig":
        """从 YAML 加载；文件缺失或解析失败时回退默认值。"""
        yaml_path = Path(path) if path else _DEFAULT_YAML
        if not yaml_path.exists():
            logger.info(f"[RiskConfig] 未找到 {yaml_path.name}，使用内置默认风控参数")
            return cls()
        try:
            import yaml

            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = cls.from_dict(data)
            logger.info(f"[RiskConfig] 已加载风控配置: {yaml_path.name}")
            return cfg
        except Exception as e:  # pragma: no cover - 配置解析容错
            logger.warning(f"[RiskConfig] 加载 {yaml_path} 失败，回退默认值: {e}")
            return cls()

    # ------------------------------------------------------------------
    # 适配器：把统一配置投影给既有组件
    # ------------------------------------------------------------------
    def to_risk_limits(self):
        """投影成 ``risk.risk_manager.RiskLimits``，供 RiskManager 使用。"""
        from risk.risk_manager import RiskLimits

        return RiskLimits(
            max_position_per_stock=self.max_position_per_stock,
            max_total_position=self.max_total_position,
            min_cash_ratio=self.min_cash_ratio,
            max_sector_concentration=self.max_sector_concentration,
            max_correlated_positions=self.max_correlated_positions,
            hard_stop_loss=self.hard_stop_loss,
            trailing_stop=self.trailing_stop,
            max_daily_loss=self.max_daily_loss,
            min_daily_volume=self.min_daily_amount,
            max_position_in_small_cap=self.max_position_per_stock / 2,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


__all__ = ["RiskConfig"]
