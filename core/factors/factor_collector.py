"""
因子结果收集器 - Factor Result Collector

核心职责：
  1. 从各层分析结果中提取所有因子值
  2. 按 Layer 分类组织为结构化数据
  3. 保存为 JSON 文件，方便复盘和后续量化分析

输出格式：
  output/factor_results/factor_results_{YYYYMMDD}.json
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import asdict

import pandas as pd
import numpy as np
from loguru import logger


class NumpyEncoder(json.JSONEncoder):
    """处理 numpy 类型的 JSON 编码器"""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Timestamp):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        return super().default(obj)


class FactorCollector:
    """
    因子结果收集器

    从 SharedContext 中提取所有因子计算结果，
    按 Layer 分类组织并保存为 JSON 文件
    """

    def __init__(self, output_dir: str = None):
        if output_dir is None:
            output_dir = str(Path(__file__).parent.parent.parent / "output" / "factor_results")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def collect_and_save(self, ctx, trade_date: str) -> str:
        """
        从 SharedContext 收集所有因子结果并保存

        Args:
            ctx: SharedContext 流水线上下文
            trade_date: 交易日期 YYYYMMDD

        Returns:
            str: 保存的文件路径
        """
        result = {
            "meta": {
                "trade_date": trade_date,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "version": "1.0",
            },
            "layer1_market_env": self._collect_layer1(ctx),
            "emotion_cycle": self._collect_emotion(ctx),
            "layer2_sector": self._collect_layer2(ctx),
            "layer3_stock_selection": self._collect_layer3(ctx),
            "layer4_trade_plan": self._collect_layer4(ctx),
        }

        filepath = self.output_dir / f"factor_results_{trade_date}.json"
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, cls=NumpyEncoder)

        logger.info(f"[因子收集器] 因子结果已保存: {filepath}")
        return str(filepath)

    def _collect_layer1(self, ctx) -> Dict:
        """收集 Layer1 大盘环境因子"""
        env = ctx.market_env
        if env is None:
            return {"status": "no_data"}

        return {
            "A1_多指数趋势": {
                "上证指数": {"收盘": env.sh_index_close, "涨跌幅": env.sh_index_change_pct},
                "深证成指": {"收盘": env.sz_index_close, "涨跌幅": env.sz_index_change_pct},
                "创业板指": {"收盘": env.cyb_index_close, "涨跌幅": env.cyb_index_change_pct},
                "科创50": {"收盘": env.kcb_index_close, "涨跌幅": env.kcb_index_change_pct},
                "北证50": {"收盘": env.bj_index_close, "涨跌幅": env.bj_index_change_pct},
                "趋势判断": env.index_trend.name if hasattr(env.index_trend, 'name') else str(env.index_trend),
                "趋势评分": env.trend_score,
            },
            "A2_量能分析": {
                "全市场成交额_亿": env.total_volume,
                "5日均量_亿": env.volume_5d_avg,
                "量比": env.volume_ratio,
                "量能状态": env.volume_state.name if hasattr(env.volume_state, 'name') else str(env.volume_state),
                "量能评分": env.volume_score,
            },
            "A3_市场宽度": {
                "上涨家数": env.up_count,
                "下跌家数": env.down_count,
                "平盘家数": env.flat_count,
                "上涨比例": env.up_ratio,
                "宽度状态": env.market_width.name if hasattr(env.market_width, 'name') else str(env.market_width),
                "宽度评分": env.width_score,
            },
            "A4_成交额环比变化率": env.amount_change_ratio,
            "A5_跌停家数": env.limit_down_count,
            "A6_炸板股次日表现": env.blasted_next_day_pct,
            "涨停连续性": {
                "昨日涨停总数": env.prev_limit_up_total,
                "今日高开比例": env.prev_limit_up_gap_up_ratio,
                "今日收红比例": env.prev_limit_up_positive_ratio,
            },
            "首板连续性": {
                "昨日首板总数": env.prev_first_board_total,
                "今日高开比例": env.prev_first_board_gap_up_ratio,
                "今日收红比例": env.prev_first_board_positive_ratio,
            },
            "综合评分": env.composite_score,
            "风险等级": env.risk_level,
            "建议仓位": env.suggested_position,
            "交叉判断": env.cross_judgment,
        }

    def _collect_emotion(self, ctx) -> Dict:
        """收集情绪周期因子"""
        emotion = ctx.emotion_result
        if not emotion:
            return {"status": "no_data"}

        metrics = emotion.get('metrics', {})
        scores = emotion.get('scores', {})

        result = {
            "情绪周期": emotion.get('cycle_name', 'N/A'),
            "策略建议": {},
            "原始统计": {
                "涨停家数": metrics.get('limit_up_count'),
                "跌停家数": metrics.get('nuclear_button_count'),
                "炸板率": metrics.get('broken_rate'),
                "最高连板": metrics.get('max_board_height'),
                "昨日涨停溢价率": metrics.get('prev_limit_up_premium'),
                "开盘卖出胜率": metrics.get('win_rate'),
                "平均赢面": metrics.get('avg_profit'),
            },
            "连板分布": metrics.get('board_distribution', {}),
            "周期评分": {
                "高潮期": scores.get('boom'),
                "上升期": scores.get('rise'),
                "震荡期": scores.get('shake'),
                "退潮期": scores.get('decline'),
                "冰点期": scores.get('freeze'),
            },
            "B1_首板连板比": metrics.get('first_to_consecutive_ratio'),
            "B2_一字板占比": metrics.get('straight_board_ratio'),
            "B3_尾盘板占比": metrics.get('late_board_ratio'),
            "C1_平均封板时间": metrics.get('avg_seal_time'),
        }

        strategy = emotion.get('strategy')
        if strategy:
            result["策略建议"] = {
                "策略": getattr(strategy, 'strategy', ''),
                "仓位": getattr(strategy, 'position', ''),
                "禁忌": getattr(strategy, 'forbidden_actions', []),
            }

        return result

    def _collect_layer2(self, ctx) -> Dict:
        """收集 Layer2 板块分析因子"""
        result = {
            "热点概念": [],
            "热点行业": [],
            "主线板块": [],
            "概念持续性": [],
            "行业持续性": [],
        }

        if not ctx.hot_concepts_df.empty:
            for _, row in ctx.hot_concepts_df.head(10).iterrows():
                result["热点概念"].append(self._safe_row_dict(row))

        if not ctx.hot_industries_df.empty:
            for _, row in ctx.hot_industries_df.head(10).iterrows():
                result["热点行业"].append(self._safe_row_dict(row))

        if not ctx.main_themes_df.empty:
            for _, row in ctx.main_themes_df.head(10).iterrows():
                result["主线板块"].append(self._safe_row_dict(row))

        if not ctx.concept_persistence_df.empty:
            for _, row in ctx.concept_persistence_df.head(10).iterrows():
                result["概念持续性"].append(self._safe_row_dict(row))

        if not ctx.industry_persistence_df.empty:
            for _, row in ctx.industry_persistence_df.head(10).iterrows():
                result["行业持续性"].append(self._safe_row_dict(row))

        return result

    def _collect_layer3(self, ctx) -> Dict:
        """收集 Layer3 个股筛选因子"""
        result = {
            "涨停池数量": len(ctx.zt_pool) if not ctx.zt_pool.empty else 0,
            "跌停池数量": len(ctx.limit_down_df) if not ctx.limit_down_df.empty else 0,
            "梯队结构": [],
            "模式信号": {},
            "D1_D5_个股技术因子": {},
            "E1_E4_资金流向因子": {},
            "龙头池": [],
            "走弱池": [],
        }

        if not ctx.hierarchy_df.empty:
            for _, row in ctx.hierarchy_df.iterrows():
                result["梯队结构"].append(self._safe_row_dict(row))

        for pattern_type, signals in ctx.patterns.items():
            result["模式信号"][pattern_type] = []
            for sig in signals:
                result["模式信号"][pattern_type].append({
                    "股票代码": getattr(sig, 'stock_code', ''),
                    "股票名称": getattr(sig, 'stock_name', ''),
                    "置信度": getattr(sig, 'confidence', 0),
                    "描述": getattr(sig, 'description', ''),
                    "关键指标": getattr(sig, 'key_metrics', {}),
                    "入场价": getattr(sig, 'entry_price', 0),
                    "止损价": getattr(sig, 'stop_loss', 0),
                    "止盈价": getattr(sig, 'take_profit', 0),
                    "仓位": getattr(sig, 'position_size', ''),
                })

        if hasattr(ctx, 'stock_tech_factors') and ctx.stock_tech_factors:
            result["D1_D5_个股技术因子"] = ctx.stock_tech_factors

        if hasattr(ctx, 'moneyflow_factors') and ctx.moneyflow_factors:
            result["E1_E4_资金流向因子"] = ctx.moneyflow_factors

        for item in ctx.dragon_pool_data:
            result["龙头池"].append(self._safe_row_dict(item) if isinstance(item, dict) else str(item))

        for item in ctx.weakening_pool_data:
            result["走弱池"].append(self._safe_row_dict(item) if isinstance(item, dict) else str(item))

        return result

    def _collect_layer4(self, ctx) -> Dict:
        """收集 Layer4 交易计划因子"""
        result = {
            "F2_大盘情绪背离度": None,
            "F3_昨日信号胜率": None,
            "交易计划": [],
        }

        if ctx.market_env and ctx.emotion_result:
            market_score = ctx.market_env.composite_score
            emotion_scores = ctx.emotion_result.get('scores', {})
            max_emotion = max(emotion_scores.values()) if emotion_scores else 50
            result["F2_大盘情绪背离度"] = round(abs(market_score - max_emotion), 1)

        if not ctx.trade_plans_df.empty:
            for _, row in ctx.trade_plans_df.iterrows():
                result["交易计划"].append(self._safe_row_dict(row))

        return result

    @staticmethod
    def _safe_row_dict(row) -> Dict:
        """安全地将 pandas Series 转为 dict，处理 numpy 类型"""
        if isinstance(row, dict):
            return {str(k): (float(v) if isinstance(v, (np.floating,)) else
                             int(v) if isinstance(v, (np.integer,)) else v)
                    for k, v in row.items()}
        if hasattr(row, 'to_dict'):
            d = row.to_dict()
            return {str(k): (float(v) if isinstance(v, (np.floating,)) else
                             int(v) if isinstance(v, (np.integer,)) else v)
                    for k, v in d.items()}
        return str(row)