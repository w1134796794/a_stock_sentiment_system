"""T+1 盘前集合竞价确认引擎。

把「盘后复盘(T)产出的交易计划/竞价观察清单」与「次日(T+1)真实集合竞价」对照，
逐只判定放量/缩量、是否符合计划竞价区间，并给出可执行的操作建议。

口径（与 config/auction_confirm.yaml 一致）：
  竞价量比 = 集合竞价成交量(手) ÷ 昨日全天成交量(手)
  高开幅度 = (竞价/开盘价 − 昨收) ÷ 昨收

数据来源：
  - 竞价：DataManager.get_auction_data(code, T+1)（eltdx 真实竞价，量已归一为手）
  - 昨收/昨量：DataManager.get_stock_daily_data(code, T)（Tushare 日线，vol 单位手）
  - 观察清单：snapshot(T) 的 trade_plans.rows
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

logger = loguru.logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / "config" / "auction_confirm.yaml"

# 操作建议等级 → 用于前端配色
LEVEL_EXECUTE = "执行"
LEVEL_STANDARD = "标准"
LEVEL_OBSERVE = "观察"
LEVEL_DOWNGRADE = "降级"
LEVEL_ABANDON = "放弃"
LEVEL_VETOED = "风控否决"
LEVEL_NODATA = "数据缺失"


class AuctionConfirmer:
    """对复盘清单做次日竞价确认。"""

    def __init__(self, data_manager, snapshot_reader=None):
        self.dm = data_manager
        if snapshot_reader is None:
            from config.settings import SNAPSHOT_DIR
            from snapshot.reader import SnapshotReader
            snapshot_reader = SnapshotReader(SNAPSHOT_DIR)
        self.reader = snapshot_reader
        self._thresholds = self._load_thresholds()

    # ------------------------------------------------------------------ #
    # 阈值
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_thresholds() -> Dict[str, Any]:
        try:
            import yaml
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if cfg.get("default"):
                return cfg
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AuctionConfirm] 读取阈值配置失败，使用内置默认: {e}")
        return {
            "default": {
                "surge_vol_ratio": 0.030, "shrink_vol_ratio": 0.008,
                "huge_vol_ratio": 0.070, "min_amount": 5_000_000,
                "surge_amount": 20_000_000, "low_open_gap": -0.02, "huge_open_gap": 0.07,
            },
            "emotion_overrides": {},
        }

    def _thresholds_for(self, emotion: Optional[str]) -> Dict[str, float]:
        th = dict(self._thresholds.get("default", {}))
        override = (self._thresholds.get("emotion_overrides", {}) or {}).get(emotion or "")
        if isinstance(override, dict):
            th.update(override)
        return th

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def confirm(self, confirm_date: str) -> Dict[str, Any]:
        """对 confirm_date(=T+1) 做盘前竞价确认；计划取自前一交易日(T)的快照。"""
        from core.utils.date_utils import get_prev_trade_date

        prev_date = get_prev_trade_date(confirm_date)
        snap = None
        try:
            snap = self.reader.load(prev_date)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AuctionConfirm] 读取 {prev_date} 快照失败: {e}")

        emotion = (snap or {}).get("cycle_name") or "震荡期"
        th = self._thresholds_for(emotion)
        rows = (((snap or {}).get("trade_plans") or {}).get("rows")) or []

        items = [self._confirm_one(row, confirm_date, prev_date, th) for row in rows]
        # 按操作等级排序：执行/标准在前，放弃/否决在后
        order = {LEVEL_EXECUTE: 0, LEVEL_STANDARD: 1, LEVEL_OBSERVE: 2,
                 LEVEL_DOWNGRADE: 3, LEVEL_ABANDON: 4, LEVEL_VETOED: 5, LEVEL_NODATA: 6}
        items.sort(key=lambda x: (order.get(x["level"], 9), -(x.get("vol_ratio") or 0)))

        return {
            "confirm_date": confirm_date,
            "prev_date": prev_date,
            "emotion": emotion,
            "thresholds": th,
            "count": len(items),
            "has_plan": snap is not None and bool(rows),
            "items": items,
        }

    # ------------------------------------------------------------------ #
    # 单只确认
    # ------------------------------------------------------------------ #
    def _confirm_one(self, plan: Dict, confirm_date: str, prev_date: str,
                     th: Dict[str, float]) -> Dict[str, Any]:
        code = str(plan.get("股票代码") or "")
        name = plan.get("股票名称") or ""
        pattern = plan.get("模式类型") or ""
        plan_cond = plan.get("竞价条件") or "不限"
        plan_range = plan.get("竞价区间") or ""
        suggest_pos = plan.get("建议仓位") or "--"
        risk_action = plan.get("风控动作") or ""
        risk_pos = plan.get("风控后仓位") or ""

        is_vetoed = risk_action in ("拒绝", "否决") or str(risk_pos).strip() in ("0%", "0")
        base = {
            "code": code.split(".")[0], "ts_code": code, "name": name,
            "pattern": pattern, "plan_cond": plan_cond, "plan_range": plan_range,
            "suggest_pos": suggest_pos, "risk_action": risk_action, "risk_pos": risk_pos,
            "vetoed": is_vetoed,
            "open_price": None, "gap": None, "vol_ratio": None,
            "amount": None, "vol_hand": None, "trend": None, "source": None,
            "vol_label": "", "match": "", "level": LEVEL_NODATA, "advice": "",
        }

        # 风控否决的票同样拉取竞价数据用于观察（不参与，但展示放量/缩量与高开，
        # 兼作风控误杀的反向校验）。
        auction = {}
        prev_daily = {}
        try:
            auction = self.dm.get_auction_data(code, confirm_date) or {}
            prev_daily = self.dm.get_stock_daily_data(code, prev_date) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[AuctionConfirm] {code} 取数失败: {e}")

        open_price = float(auction.get("开盘价") or 0)
        amount = float(auction.get("竞价成交额") or 0)
        vol_hand = float(auction.get("竞价成交量") or 0)
        prev_close = float(prev_daily.get("close") or 0)
        prev_vol = float(prev_daily.get("vol") or 0)
        source = auction.get("数据源")

        base.update({"open_price": open_price or None, "amount": amount or None,
                     "vol_hand": vol_hand or None, "source": source})

        # 数据不足：无法判定
        if open_price <= 0 or prev_close <= 0 or amount <= 0:
            if is_vetoed:
                base["level"] = LEVEL_VETOED
                base["advice"] = f"复盘已被风控否决（剩余仓位{risk_pos or '0%'}），暂无竞价数据可观察。"
            else:
                base["level"] = LEVEL_NODATA
                base["advice"] = "竞价成交额/昨收数据缺失，待 T+1 真实集合竞价数据后再判定。"
            return base

        gap = (open_price - prev_close) / prev_close
        vol_ratio = (vol_hand / prev_vol) if prev_vol > 0 else 0.0
        trend = self._trend_label(auction.get("价格趋势"))
        gmin, gmax = self._parse_range(plan_range)
        gap_in_range = (gmin is None) or (gmin <= gap * 100 <= gmax)

        base.update({"gap": gap, "vol_ratio": vol_ratio, "trend": trend,
                     "match": "符合" if gap_in_range else "不符合"})

        is_surge = vol_ratio >= th["surge_vol_ratio"] and amount >= th["min_amount"]
        is_shrink = vol_ratio < th["shrink_vol_ratio"]
        base["vol_label"] = "放量" if is_surge else ("缩量" if is_shrink else "正常")

        amt_wan = amount / 1e4
        vr_txt = f"{vol_ratio * 100:.1f}%"
        gap_txt = f"{gap * 100:+.1f}%"
        trend_txt = f"，竞价末段{trend}" if trend else ""

        # 风控否决：不参与，但展示真实竞价表现供观察/跟踪（兼作风控误杀的反向校验）
        if is_vetoed:
            base["level"] = LEVEL_VETOED
            base["advice"] = (f"复盘已被风控否决（剩余仓位{risk_pos or '0%'}），不参与；"
                              f"仅观察竞价：{base['vol_label']}高开{gap_txt}(量比{vr_txt}/额{amt_wan:.0f}万){trend_txt}。")
            return base

        # 决策优先级：低开 > 巨量高开滞涨 > 放量高开 > 缩量 > 区间外 > 温和高开
        if gap <= th["low_open_gap"]:
            base["level"] = LEVEL_ABANDON
            base["advice"] = f"低开{gap_txt}，放弃竞价买点；若看好转『盘中弱转强』确认再低吸{trend_txt}。"
        elif is_surge and vol_ratio >= th["huge_vol_ratio"] and gap >= th["huge_open_gap"]:
            base["level"] = LEVEL_OBSERVE
            base["advice"] = (f"巨量高开{gap_txt}(量比{vr_txt}/额{amt_wan:.0f}万)，警惕一字烂板或高位出货，"
                              f"仅认首板逻辑或减半{trend_txt}。")
        elif is_surge and gap_in_range:
            base["level"] = LEVEL_EXECUTE
            base["advice"] = (f"放量高开{gap_txt}(量比{vr_txt}/额{amt_wan:.0f}万)符合计划区间{plan_range}，"
                              f"按建议仓{suggest_pos}执行，可竞价挂单{trend_txt}。")
        elif is_surge and not gap_in_range:
            base["level"] = LEVEL_OBSERVE
            base["advice"] = f"放量但高开{gap_txt}不在计划区间{plan_range}，开盘看承接、盘中观察{trend_txt}。"
        elif is_shrink:
            base["level"] = LEVEL_DOWNGRADE
            base["advice"] = f"缩量{gap_txt}(量比{vr_txt})资金分歧，降级/减半或观察，不竞价追高{trend_txt}。"
        elif gap_in_range:
            base["level"] = LEVEL_STANDARD
            base["advice"] = f"温和高开{gap_txt}(量比{vr_txt})符合区间，开盘看首笔承接，标准仓{suggest_pos}{trend_txt}。"
        else:
            base["level"] = LEVEL_OBSERVE
            base["advice"] = f"高开{gap_txt}不在计划区间{plan_range}且非放量，观察为主{trend_txt}。"
        return base

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_range(text: str):
        """'+2.0%~+9.0%' → (2.0, 9.0)；'不限'/空 → (None, None)。"""
        if not text or "不限" in text:
            return None, None
        nums = re.findall(r"[-+]?\d+(?:\.\d+)?", str(text))
        if len(nums) >= 2:
            lo, hi = float(nums[0]), float(nums[1])
            return (lo, hi) if lo <= hi else (hi, lo)
        return None, None

    @staticmethod
    def _trend_label(price_trend) -> Optional[str]:
        if not price_trend or not isinstance(price_trend, list) or len(price_trend) < 2:
            return None
        try:
            return "走强" if float(price_trend[-1]) > float(price_trend[-2]) else "走弱"
        except (TypeError, ValueError):
            return None
