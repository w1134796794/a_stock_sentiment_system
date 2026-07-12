"""Generate an evidence-based Markdown review journal."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.agent.review_assistant import ReviewAssistantService


class DailyJournalService:
    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            from config.settings import WEB_DATA_DIR

            output_dir = Path(WEB_DATA_DIR) / "reports" / "journals"
        self.output_dir = Path(output_dir)

    def generate(self, trade_date: str, *, capital: float = 100_000.0) -> Dict[str, Any]:
        brief = ReviewAssistantService().build_brief(str(trade_date), capital=capital)
        preset = brief.get("capital_preset") or {}
        manual_trades = self._manual_trades(str(trade_date))
        lines = [
            f"# {trade_date} 每日复盘",
            "",
            f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
            f"> 账户方案：{preset.get('label') or '10万账户'}，最多持有 {preset.get('max_positions') or 4} 只",
            "",
            "## 今日结论",
            "",
            f"**{brief.get('headline') or '暂无结论'}**",
            "",
            str(brief.get("summary") or ""),
            "",
            f"- 市场状态：{brief.get('market_regime') or '未知'}",
            f"- 操作倾向：{brief.get('stance') or '休息'}",
            f"- 模型模式：{brief.get('model_mode') or brief.get('model_status') or '未知'}",
            f"- 候选数量：{brief.get('candidate_count') or 0}，等待确认：{brief.get('actionable_count') or 0}",
            "",
            "## 候选股",
            "",
        ]
        candidates = brief.get("candidates") or []
        if candidates:
            for row in candidates:
                lines.extend([
                    f"### {row.get('name')}（{row.get('code')}） · {row.get('action')}",
                    "",
                    str(row.get("one_line") or row.get("next_step") or ""),
                    "",
                    f"- 证据：{row.get('reason') or '暂无'}",
                    f"- 风险：{row.get('risk') or '暂无'}",
                    "",
                ])
        else:
            lines.extend(["今日没有可用候选，不主动开仓。", ""])

        lines.extend(["## 成交记录", ""])
        if manual_trades:
            for row in manual_trades:
                lines.append(
                    f"- {row.get('action') or '记录'} {row.get('name') or row.get('code') or ''} "
                    f"{row.get('price') or ''}，{row.get('note') or ''}".strip()
                )
        else:
            lines.append("未发现手工成交记录。本节不会用模拟交易冒充真实成交。")
        lines.extend([
            "",
            "## 明日检查清单",
            "",
            "- 只处理盘前计划内的股票，不临盘随意扩池。",
            "- 等待弱转强、强势延续或高开加速条件确认，无法成交的信号只记录不追价。",
            "- 板块转弱、数据不足或模型失效时主动放弃。",
            "- 收盘后记录实际买卖与偏差，供后续归因。",
            "",
            "> 本文由规则化证据生成，不构成投资建议。",
        ])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"journal_{trade_date}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return {"ok": True, "trade_date": str(trade_date), "path": str(path), "candidate_count": len(candidates)}

    @staticmethod
    def _manual_trades(trade_date: str) -> list[Dict[str, Any]]:
        from config.settings import WEB_DATA_DIR

        path = Path(WEB_DATA_DIR) / "trades" / f"manual_trades_{trade_date}.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("rows") if isinstance(payload, dict) else payload
            return [row for row in (rows or []) if isinstance(row, dict)]
        except (OSError, ValueError, TypeError):
            return []


__all__ = ["DailyJournalService"]
