"""
情绪周期机器学习模型

功能：
1. 马尔可夫链模型 - 预测情绪周期状态转移概率
2. 贝叶斯推断模型 - 基于多指标综合判断情绪周期
3. 隐马尔可夫模型(HMM) - 从观测指标推断隐藏情绪状态

设计原则：
- 概率化输出，提供置信度
- 支持增量学习，随数据积累优化
- 与现有规则系统互补，而非替代
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import json
from pathlib import Path
import loguru

logger = loguru.logger


class EmotionCycleState(Enum):
    """情绪周期状态枚举"""
    BOOM = "高潮期"
    RISE = "上升期"
    SHAKE = "震荡期"
    DECLINE = "退潮期"
    FREEZE = "冰点期"


@dataclass
class StateTransition:
    """状态转移数据"""
    from_state: str
    to_state: str
    probability: float
    count: int = 0


@dataclass
class CyclePrediction:
    """周期预测结果"""
    current_state: str
    predicted_state: str
    confidence: float
    transition_probs: Dict[str, float]
    recommended_action: str


# =============================================================================
# 1. 马尔可夫链模型
# =============================================================================

class MarkovChainEmotionModel:
    """
    马尔可夫链情绪周期模型
    
    核心思想：
    - 情绪周期的状态转移具有马尔可夫性（下一状态只依赖当前状态）
    - 通过历史数据统计状态转移概率矩阵
    - 预测未来最可能的情绪状态
    
    使用场景：
    - 预测明天最可能的情绪状态
    - 计算从当前状态转移到目标状态的概率
    - 评估周期持续性
    """

    def __init__(self, model_path: str = None):
        """
        初始化
        
        Args:
            model_path: 模型保存路径
        """
        self.states = [s.value for s in EmotionCycleState]
        self.transition_matrix = pd.DataFrame(
            0.0,
            index=self.states,
            columns=self.states
        )
        self.state_counts = defaultdict(int)
        self.transition_counts = defaultdict(lambda: defaultdict(int))
        self.model_path = model_path or "cache/models/markov_emotion_model.json"
        
        # 加载已有模型
        self._load_model()

    def _load_model(self):
        """加载已训练的模型"""
        model_file = Path(self.model_path)
        if model_file.exists():
            try:
                with open(model_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.transition_matrix = pd.DataFrame(data['transition_matrix'])
                    self.state_counts = defaultdict(int, data['state_counts'])
                    self.transition_counts = defaultdict(
                        lambda: defaultdict(int),
                        {k: defaultdict(int, v) for k, v in data['transition_counts'].items()}
                    )
                logger.info(f"[MarkovChainEmotionModel] 加载模型成功")
            except Exception as e:
                logger.warning(f"[MarkovChainEmotionModel] 加载模型失败: {e}")

    def _save_model(self):
        """保存模型"""
        try:
            model_file = Path(self.model_path)
            model_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'transition_matrix': self.transition_matrix.to_dict(),
                'state_counts': dict(self.state_counts),
                'transition_counts': {k: dict(v) for k, v in self.transition_counts.items()}
            }
            
            with open(model_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[MarkovChainEmotionModel] 保存模型成功")
        except Exception as e:
            logger.error(f"[MarkovChainEmotionModel] 保存模型失败: {e}")

    def fit(self, state_sequence: List[str]):
        """
        训练模型
        
        Args:
            state_sequence: 历史情绪状态序列，如 ['上升期', '上升期', '高潮期', '震荡期', ...]
        """
        if len(state_sequence) < 2:
            logger.warning("[MarkovChainEmotionModel] 训练数据不足")
            return
        
        # 统计状态转移
        for i in range(len(state_sequence) - 1):
            current = state_sequence[i]
            next_state = state_sequence[i + 1]
            
            self.state_counts[current] += 1
            self.transition_counts[current][next_state] += 1
        
        # 计算转移概率
        for from_state in self.states:
            total = self.state_counts[from_state]
            if total > 0:
                for to_state in self.states:
                    count = self.transition_counts[from_state][to_state]
                    # 拉普拉斯平滑
                    self.transition_matrix.loc[from_state, to_state] = (count + 1) / (total + len(self.states))
            else:
                # 没有观测到的状态，使用均匀分布
                self.transition_matrix.loc[from_state] = 1.0 / len(self.states)
        
        self._save_model()
        logger.info(f"[MarkovChainEmotionModel] 训练完成，共{len(state_sequence)}个样本")

    def predict_next_state(self, current_state: str, top_k: int = 3) -> List[Tuple[str, float]]:
        """
        预测下一个最可能的状态
        
        Args:
            current_state: 当前状态
            top_k: 返回前K个最可能的状态
            
        Returns:
            List[Tuple[str, float]]: [(状态, 概率), ...]
        """
        if current_state not in self.states:
            logger.warning(f"[MarkovChainEmotionModel] 未知状态: {current_state}")
            return []
        
        probs = self.transition_matrix.loc[current_state].sort_values(ascending=False)
        return [(state, prob) for state, prob in probs.head(top_k).items()]

    def predict_n_steps(self, current_state: str, n: int = 3) -> Dict[str, float]:
        """
        预测n步后的状态分布
        
        Args:
            current_state: 当前状态
            n: 预测步数
            
        Returns:
            Dict[str, float]: 各状态的概率分布
        """
        if current_state not in self.states:
            return {}
        
        # 初始分布
        current_dist = pd.Series({s: 0.0 for s in self.states})
        current_dist[current_state] = 1.0
        
        # n步转移
        for _ in range(n):
            current_dist = current_dist.dot(self.transition_matrix)
        
        return current_dist.to_dict()

    def get_transition_probability(self, from_state: str, to_state: str) -> float:
        """
        获取从from_state转移到to_state的概率
        
        Args:
            from_state: 起始状态
            to_state: 目标状态
            
        Returns:
            float: 转移概率
        """
        if from_state in self.states and to_state in self.states:
            return self.transition_matrix.loc[from_state, to_state]
        return 0.0

    def get_cycle_duration_prob(self, current_state: str, max_days: int = 10) -> List[float]:
        """
        计算当前周期持续的期望天数概率分布
        
        Args:
            current_state: 当前状态
            max_days: 最大天数
            
        Returns:
            List[float]: 持续n天的概率
        """
        if current_state not in self.states:
            return []
        
        # 自转移概率（周期持续的概率）
        stay_prob = self.transition_matrix.loc[current_state, current_state]
        
        # 计算持续n天的概率
        probs = []
        for n in range(1, max_days + 1):
            # 持续n-1天后转移
            prob = (stay_prob ** (n - 1)) * (1 - stay_prob)
            probs.append(prob)
        
        return probs


# =============================================================================
# 2. 贝叶斯推断模型
# =============================================================================

class BayesianEmotionInference:
    """
    贝叶斯情绪周期推断模型
    
    核心思想：
    - 将情绪周期判断视为分类问题
    - 利用贝叶斯定理综合多个指标的后验概率
    - P(周期|指标) = P(指标|周期) * P(周期) / P(指标)
    
    使用场景：
    - 综合多个指标判断当前最可能的情绪周期
    - 提供概率化输出，而非单一判断
    - 量化各指标对周期判断的贡献度
    """

    def __init__(self, model_path: str = None):
        """
        初始化
        
        Args:
            model_path: 模型保存路径
        """
        self.states = [s.value for s in EmotionCycleState]
        self.prior_probs = {s: 1.0 / len(self.states) for s in self.states}  # 先验概率
        self.likelihood_tables = {}  # 似然表
        self.indicator_ranges = {}   # 指标范围
        self.model_path = model_path or "cache/models/bayesian_emotion_model.json"
        
        self._load_model()

    def _load_model(self):
        """加载模型"""
        model_file = Path(self.model_path)
        if model_file.exists():
            try:
                with open(model_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.prior_probs = data['prior_probs']
                    self.likelihood_tables = data['likelihood_tables']
                    self.indicator_ranges = data['indicator_ranges']
                logger.info("[BayesianEmotionInference] 加载模型成功")
            except Exception as e:
                logger.warning(f"[BayesianEmotionInference] 加载模型失败: {e}")
                self._init_default_model()
        else:
            self._init_default_model()

    def _init_default_model(self):
        """初始化默认模型（基于经验阈值）"""
        # 基于emotion_cycle_config.yaml的阈值初始化
        self.indicator_ranges = {
            'limit_up_count': [(0, 20), (20, 30), (30, 50), (50, 80), (80, 1000)],
            'max_board_height': [(0, 2), (2, 3), (3, 4), (4, 6), (6, 20)],
            'broken_rate': [(0, 15), (15, 25), (25, 40), (40, 100)],
            'continuous_rate': [(0, 10), (10, 20), (20, 30), (30, 100)],
        }
        
        # 默认似然表（基于经验）
        self.likelihood_tables = self._build_default_likelihood()

    def _build_default_likelihood(self) -> Dict:
        """构建默认似然表"""
        # 简化的似然表，实际应从历史数据学习
        likelihood = {
            'limit_up_count': {
                '冰点期': [0.5, 0.3, 0.15, 0.05, 0.0],
                '退潮期': [0.3, 0.4, 0.2, 0.08, 0.02],
                '震荡期': [0.05, 0.2, 0.5, 0.2, 0.05],
                '上升期': [0.0, 0.05, 0.2, 0.5, 0.25],
                '高潮期': [0.0, 0.02, 0.08, 0.3, 0.6],
            },
            'max_board_height': {
                '冰点期': [0.6, 0.3, 0.08, 0.02, 0.0],
                '退潮期': [0.3, 0.4, 0.2, 0.08, 0.02],
                '震荡期': [0.1, 0.2, 0.4, 0.2, 0.1],
                '上升期': [0.02, 0.08, 0.2, 0.4, 0.3],
                '高潮期': [0.0, 0.02, 0.08, 0.3, 0.6],
            },
            'broken_rate': {
                '冰点期': [0.1, 0.2, 0.3, 0.4],
                '退潮期': [0.05, 0.15, 0.3, 0.5],
                '震荡期': [0.2, 0.4, 0.3, 0.1],
                '上升期': [0.4, 0.35, 0.2, 0.05],
                '高潮期': [0.6, 0.3, 0.08, 0.02],
            },
            'continuous_rate': {
                '冰点期': [0.5, 0.3, 0.15, 0.05],
                '退潮期': [0.3, 0.4, 0.2, 0.1],
                '震荡期': [0.15, 0.35, 0.35, 0.15],
                '上升期': [0.05, 0.2, 0.4, 0.35],
                '高潮期': [0.02, 0.08, 0.3, 0.6],
            },
        }
        return likelihood

    def _discretize_indicator(self, indicator_name: str, value: float) -> int:
        """将连续指标离散化为区间索引"""
        if indicator_name not in self.indicator_ranges:
            return 0
        
        ranges = self.indicator_ranges[indicator_name]
        for i, (low, high) in enumerate(ranges):
            if low <= value < high:
                return i
        return len(ranges) - 1

    def infer(self, indicators: Dict[str, float]) -> Dict[str, float]:
        """
        推断情绪周期
        
        Args:
            indicators: 指标字典，如 {'limit_up_count': 65, 'max_board_height': 5, ...}
            
        Returns:
            Dict[str, float]: 各周期的后验概率
        """
        # 计算各周期的后验概率
        posteriors = {}
        
        for state in self.states:
            # 先验概率
            prob = np.log(self.prior_probs.get(state, 0.2))
            
            # 乘以各指标的似然
            for indicator_name, value in indicators.items():
                if indicator_name in self.likelihood_tables:
                    bin_idx = self._discretize_indicator(indicator_name, value)
                    likelihood_table = self.likelihood_tables[indicator_name]
                    if state in likelihood_table:
                        likelihood = likelihood_table[state][bin_idx]
                        prob += np.log(likelihood + 1e-10)  # 防止log(0)
            
            posteriors[state] = prob
        
        # 转换为概率（softmax）
        max_log_prob = max(posteriors.values())
        exp_probs = {s: np.exp(p - max_log_prob) for s, p in posteriors.items()}
        total = sum(exp_probs.values())
        
        return {s: p / total for s, p in exp_probs.items()}

    def fit(self, data: List[Dict]):
        """
        从数据学习模型参数
        
        Args:
            data: 训练数据，每个元素为 {'state': '上升期', 'limit_up_count': 65, ...}
        """
        if not data:
            return
        
        # 统计先验概率
        state_counts = defaultdict(int)
        for d in data:
            state_counts[d['state']] += 1
        
        total = len(data)
        self.prior_probs = {s: state_counts[s] / total for s in self.states}
        
        # 统计似然表
        for indicator_name in self.indicator_ranges.keys():
            for state in self.states:
                state_data = [d for d in data if d['state'] == state]
                if not state_data:
                    continue
                
                # 统计各区间出现频率
                bin_counts = [0] * len(self.indicator_ranges[indicator_name])
                for d in state_data:
                    if indicator_name in d:
                        bin_idx = self._discretize_indicator(indicator_name, d[indicator_name])
                        bin_counts[bin_idx] += 1
                
                # 拉普拉斯平滑
                total_state = len(state_data)
                num_bins = len(bin_counts)
                self.likelihood_tables[indicator_name][state] = [
                    (count + 1) / (total_state + num_bins) for count in bin_counts
                ]
        
        self._save_model()
        logger.info(f"[BayesianEmotionInference] 训练完成，共{len(data)}个样本")

    def _save_model(self):
        """保存模型"""
        try:
            model_file = Path(self.model_path)
            model_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'prior_probs': self.prior_probs,
                'likelihood_tables': self.likelihood_tables,
                'indicator_ranges': self.indicator_ranges,
            }
            
            with open(model_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[BayesianEmotionInference] 保存模型失败: {e}")


# =============================================================================
# 3. 集成预测器
# =============================================================================

class EmotionCycleMLPredictor:
    """
    情绪周期机器学习预测器（集成多个模型）
    """

    def __init__(self):
        self.markov_model = MarkovChainEmotionModel()
        self.bayesian_model = BayesianEmotionInference()

    def predict(self,
                current_state: str,
                indicators: Dict[str, float],
                use_ensemble: bool = True) -> CyclePrediction:
        """
        预测情绪周期
        
        Args:
            current_state: 当前状态
            indicators: 当前指标
            use_ensemble: 是否使用集成预测
            
        Returns:
            CyclePrediction: 预测结果
        """
        # 马尔可夫链预测
        markov_probs = self.markov_model.predict_next_state(current_state)
        markov_pred = markov_probs[0][0] if markov_probs else current_state
        markov_conf = markov_probs[0][1] if markov_probs else 0.0
        
        # 贝叶斯推断
        bayesian_probs = self.bayesian_model.infer(indicators)
        bayesian_pred = max(bayesian_probs, key=bayesian_probs.get)
        bayesian_conf = bayesian_probs[bayesian_pred]
        
        if use_ensemble:
            # 集成预测：加权平均
            ensemble_probs = {}
            for state in self.markov_model.states:
                markov_p = next((p for s, p in markov_probs if s == state), 0.0)
                bayesian_p = bayesian_probs.get(state, 0.0)
                # 马尔可夫权重0.4，贝叶斯权重0.6（贝叶斯利用更多信息）
                ensemble_probs[state] = 0.4 * markov_p + 0.6 * bayesian_p
            
            predicted_state = max(ensemble_probs, key=ensemble_probs.get)
            confidence = ensemble_probs[predicted_state]
        else:
            # 单独使用贝叶斯（利用实时指标）
            predicted_state = bayesian_pred
            confidence = bayesian_conf
            ensemble_probs = bayesian_probs
        
        # 推荐操作
        recommended_action = self._get_recommended_action(predicted_state)
        
        return CyclePrediction(
            current_state=current_state,
            predicted_state=predicted_state,
            confidence=confidence,
            transition_probs=ensemble_probs,
            recommended_action=recommended_action
        )

    def _get_recommended_action(self, state: str) -> str:
        """根据预测状态推荐操作"""
        action_map = {
            '高潮期': '减仓止盈，准备撤退',
            '上升期': '积极参与，重仓龙头',
            '震荡期': '快进快出，严格止损',
            '退潮期': '空仓观望，禁止接力',
            '冰点期': '小仓位试错，等待信号',
        }
        return action_map.get(state, '观望')

    def train(self, historical_data: List[Dict]):
        """
        训练所有模型
        
        Args:
            historical_data: 历史数据列表
                [{'date': '20240101', 'state': '上升期', 'limit_up_count': 65, ...}, ...]
        """
        # 训练马尔可夫链
        state_sequence = [d['state'] for d in historical_data]
        self.markov_model.fit(state_sequence)
        
        # 训练贝叶斯模型
        self.bayesian_model.fit(historical_data)
        
        logger.info("[EmotionCycleMLPredictor] 所有模型训练完成")


# 便捷函数
def create_ml_predictor() -> EmotionCycleMLPredictor:
    """创建ML预测器"""
    return EmotionCycleMLPredictor()
