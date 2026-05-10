"""
情绪周期综合判断引擎（规则+机器学习）

功能：
1. 整合规则引擎和ML模型的判断结果
2. 当两者不一致时，提供置信度分析和建议
3. 支持渐进式切换：从规则主导到ML主导
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import loguru

logger = loguru.logger


@dataclass
class IntegratedCycleResult:
    """综合判断结果"""
    # 规则引擎结果
    rule_based_state: str
    rule_confidence: float
    
    # ML模型结果
    ml_predicted_state: str
    ml_confidence: float
    ml_transition_probs: Dict[str, float]
    
    # 综合结果
    final_state: str
    final_confidence: float
    agreement: bool  # 两者是否一致
    
    # 详细分析
    analysis: str
    recommendation: str
    risk_level: str  # 高/中/低


class IntegratedEmotionCycleEngine:
    """
    情绪周期综合判断引擎
    
    设计理念：
    - 规则引擎：基于明确阈值，可解释性强，适合极端行情
    - ML模型：基于统计规律，泛化能力强，适合常态行情
    - 综合判断：两者结合，互相验证
    """

    def __init__(self, rule_engine, ml_predictor=None):
        """
        初始化
        
        Args:
            rule_engine: 规则引擎实例 (EmotionCycleEngine)
            ml_predictor: ML预测器实例 (EmotionCycleMLPredictor)
        """
        self.rule_engine = rule_engine
        
        if ml_predictor is None:
            from core.analysis.emotion_cycle_ml import create_ml_predictor
            self.ml_predictor = create_ml_predictor()
        else:
            self.ml_predictor = ml_predictor
        
        # 模型权重（可配置）
        self.rule_weight = 0.6  # 规则引擎权重
        self.ml_weight = 0.4    # ML模型权重
        
        # 一致性阈值
        self.agreement_threshold = 0.7  # 置信度阈值

    def detect_cycle_integrated(self,
                                 market_data: Dict,
                                 indicators: Dict[str, float],
                                 use_ml: bool = True) -> IntegratedCycleResult:
        """
        综合判断情绪周期
        
        Args:
            market_data: 市场数据（用于规则引擎）
            indicators: 指标数据（用于ML模型）
            use_ml: 是否使用ML模型
            
        Returns:
            IntegratedCycleResult: 综合判断结果
        """
        # 1. 规则引擎判断 - 从indicators中提取所需参数
        rule_state_enum, rule_scores = self.rule_engine.detect_cycle(
            limit_up_count=indicators.get('limit_up_count', 0),
            max_board_height=indicators.get('max_board_height', 0),
            broken_rate=indicators.get('broken_rate', 0),
            nuclear_button_count=indicators.get('nuclear_button_count', 0),
            prev_limit_up_premium=indicators.get('prev_limit_up_premium'),
            board_distribution=indicators.get('board_distribution'),
            continuous_rate=indicators.get('continuous_rate'),
            limit_down_count=indicators.get('limit_down_count', 0)
        )
        # 将Enum转换为字符串（ML模型需要字符串状态）
        rule_state = rule_state_enum.value if hasattr(rule_state_enum, 'value') else str(rule_state_enum)
        rule_confidence = self._calculate_rule_confidence(rule_scores, rule_state_enum)
        
        if not use_ml:
            # 仅使用规则引擎
            return IntegratedCycleResult(
                rule_based_state=rule_state,
                rule_confidence=rule_confidence,
                ml_predicted_state="未使用",
                ml_confidence=0.0,
                ml_transition_probs={},
                final_state=rule_state,
                final_confidence=rule_confidence,
                agreement=True,
                analysis="仅使用规则引擎判断",
                recommendation=self._get_recommendation_by_state(rule_state),
                risk_level="中"
            )
        
        # 2. ML模型预测
        ml_prediction = self.ml_predictor.predict(
            current_state=rule_state,
            indicators=indicators,
            use_ensemble=True
        )
        
        # 3. 综合判断
        final_state, final_confidence, agreement = self._integrate_results(
            rule_state, rule_confidence,
            ml_prediction.predicted_state, ml_prediction.confidence,
            ml_prediction.transition_probs
        )
        
        # 4. 生成分析
        analysis = self._generate_analysis(
            rule_state, rule_confidence,
            ml_prediction.predicted_state, ml_prediction.confidence,
            agreement, final_state
        )
        
        # 5. 风险评估
        risk_level = self._assess_risk(
            agreement, final_confidence,
            ml_prediction.transition_probs
        )
        
        return IntegratedCycleResult(
            rule_based_state=rule_state,
            rule_confidence=rule_confidence,
            ml_predicted_state=ml_prediction.predicted_state,
            ml_confidence=ml_prediction.confidence,
            ml_transition_probs=ml_prediction.transition_probs,
            final_state=final_state,
            final_confidence=final_confidence,
            agreement=agreement,
            analysis=analysis,
            recommendation=ml_prediction.recommended_action,
            risk_level=risk_level
        )

    def _calculate_rule_confidence(self, scores: Dict, state: str) -> float:
        """
        计算规则引擎的置信度
        
        基于各周期得分的离散程度
        """
        if not scores:
            return 0.5
        
        values = list(scores.values())
        max_score = max(values)
        total = sum(values)
        
        if total == 0:
            return 0.5
        
        # 最高分占比越高，置信度越高
        confidence = max_score / total
        
        # 如果有明显第二高分，降低置信度
        sorted_values = sorted(values, reverse=True)
        if len(sorted_values) >= 2 and sorted_values[1] > 0:
            ratio = sorted_values[1] / sorted_values[0]
            confidence *= (1 - ratio * 0.5)
        
        return min(1.0, max(0.3, confidence))

    def _integrate_results(self,
                           rule_state: str, rule_conf: float,
                           ml_state: str, ml_conf: float,
                           ml_probs: Dict[str, float]) -> Tuple[str, float, bool]:
        """
        整合规则引擎和ML模型的结果
        
        Returns:
            Tuple[str, float, bool]: (最终状态, 置信度, 是否一致)
        """
        agreement = (rule_state == ml_state)
        
        if agreement:
            # 两者一致，提高置信度
            final_conf = min(1.0, (rule_conf + ml_conf) / 2 * 1.2)
            return rule_state, final_conf, True
        
        # 两者不一致，加权投票
        # 计算各状态的综合得分
        integrated_scores = {}
        
        for state in set(list(ml_probs.keys()) + [rule_state]):
            score = 0.0
            
            # 规则引擎贡献
            if state == rule_state:
                score += self.rule_weight * rule_conf
            
            # ML模型贡献
            ml_prob = ml_probs.get(state, 0.0)
            score += self.ml_weight * ml_conf * ml_prob
            
            integrated_scores[state] = score
        
        # 选择得分最高的
        final_state = max(integrated_scores, key=integrated_scores.get)
        final_conf = integrated_scores[final_state]
        
        return final_state, final_conf, False

    def _generate_analysis(self,
                           rule_state: str, rule_conf: float,
                           ml_state: str, ml_conf: float,
                           agreement: bool, final_state: str) -> str:
        """生成分析说明"""
        if agreement:
            return f"规则引擎和ML模型一致判断为【{final_state}】，置信度高"
        
        analysis_parts = []
        analysis_parts.append(f"规则引擎判断：{rule_state}（置信度{rule_conf:.1%}）")
        analysis_parts.append(f"ML模型预测：{ml_state}（置信度{ml_conf:.1%}）")
        
        # 分析分歧原因
        if rule_conf > 0.8 and ml_conf < 0.6:
            analysis_parts.append("规则引擎置信度显著高于ML模型，可能处于极端行情")
        elif ml_conf > 0.8 and rule_conf < 0.6:
            analysis_parts.append("ML模型置信度显著高于规则引擎，可能处于过渡行情")
        else:
            analysis_parts.append("两者置信度均不高，市场处于模糊状态")
        
        analysis_parts.append(f"综合判断为【{final_state}】，建议谨慎操作")
        
        return "；".join(analysis_parts)

    def _assess_risk(self,
                     agreement: bool,
                     final_conf: float,
                     ml_probs: Dict[str, float]) -> str:
        """评估风险等级"""
        # 计算ML预测的不确定性（熵）
        entropy = 0.0
        for prob in ml_probs.values():
            if prob > 0:
                entropy -= prob * np.log2(prob)
        
        max_entropy = np.log2(len(ml_probs)) if ml_probs else 1.0
        normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
        
        if not agreement and final_conf < 0.6 and normalized_entropy > 0.7:
            return "高"
        elif not agreement or final_conf < 0.7 or normalized_entropy > 0.5:
            return "中"
        else:
            return "低"

    def _get_recommendation_by_state(self, state: str) -> str:
        """根据状态获取建议"""
        recommendations = {
            '高潮期': '减仓止盈，准备撤退',
            '上升期': '积极参与，重仓龙头',
            '震荡期': '快进快出，严格止损',
            '退潮期': '空仓观望，禁止接力',
            '冰点期': '小仓位试错，等待信号',
        }
        return recommendations.get(state, '观望')

    def predict_next_days(self,
                          current_state: str,
                          indicators: Dict[str, float],
                          days: int = 3) -> List[Dict]:
        """
        预测未来N天的情绪状态分布
        
        Args:
            current_state: 当前状态
            indicators: 当前指标
            days: 预测天数
            
        Returns:
            List[Dict]: 每天的预测结果
        """
        predictions = []
        
        # 使用马尔可夫链预测
        markov_model = self.ml_predictor.markov_model
        
        for day in range(1, days + 1):
            # n步后的状态分布
            state_dist = markov_model.predict_n_steps(current_state, day)
            
            # 最可能的状态
            most_likely = max(state_dist, key=state_dist.get)
            prob = state_dist[most_likely]
            
            predictions.append({
                'day': day,
                'predicted_state': most_likely,
                'probability': prob,
                'state_distribution': state_dist,
            })
        
        return predictions

    def get_transition_advice(self, from_state: str, to_state: str) -> str:
        """
        获取状态转移建议
        
        Args:
            from_state: 起始状态
            to_state: 目标状态
            
        Returns:
            str: 转移建议
        """
        markov_model = self.ml_predictor.markov_model
        prob = markov_model.get_transition_probability(from_state, to_state)
        
        advice_map = {
            ('冰点期', '上升期'): "冰点转暖，积极试错",
            ('冰点期', '退潮期'): "冰点延续，继续观望",
            ('退潮期', '冰点期'): "退潮末期，准备试错",
            ('退潮期', '震荡期'): "退潮转震荡，小仓位参与",
            ('震荡期', '上升期'): "震荡转升，加仓参与",
            ('震荡期', '退潮期'): "震荡转退，及时减仓",
            ('上升期', '高潮期'): "加速赶顶，注意风险",
            ('上升期', '震荡期'): "上升受阻，控制仓位",
            ('高潮期', '退潮期'): "高潮转退，果断离场",
            ('高潮期', '震荡期'): "高位震荡，逐步减仓",
        }
        
        advice = advice_map.get((from_state, to_state), "状态转换，谨慎观察")
        
        if prob > 0.5:
            return f"{advice}（转移概率{prob:.1%}，较大概率）"
        elif prob > 0.3:
            return f"{advice}（转移概率{prob:.1%}，中等概率）"
        else:
            return f"{advice}（转移概率{prob:.1%}，低概率）"


# 便捷函数
def create_integrated_engine(rule_engine, ml_predictor=None):
    """创建综合判断引擎"""
    return IntegratedEmotionCycleEngine(rule_engine, ml_predictor)
