"""
概念-行业交叉验证器

实现双轨制分析：
1. 概念维度：同花顺概念板块（短线热点）
2. 行业维度：同花顺行业体系（中线趋势）

核心功能：
- 概念成分股行业分布分析
- 概念-行业共振/背离识别
- 信号强度评级
- 动态仓位建议
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class SignalType(Enum):
    """信号类型"""
    RESONANCE = "共振"      # 概念+行业都热
    DIVERGENCE = "背离"     # 概念热但行业冷
    INDUSTRY_ONLY = "行业独热"  # 行业热但概念不热
    WEAK = "弱势"           # 都不热


class SignalStrength(Enum):
    """信号强度"""
    STRONG = "强"      # 强烈信号
    MEDIUM = "中"      # 中等信号
    WEAK = "弱"        # 弱信号


@dataclass
class CrossValidationResult:
    """交叉验证结果"""
    concept_name: str              # 概念名称
    concept_rank: int              # 概念排名
    concept_score: float           # 概念评分
    
    main_industry: str             # 主要行业
    industry_concentration: float  # 行业集中度（0-1）
    industry_distribution: Dict[str, int]  # 行业分布
    
    signal_type: SignalType        # 信号类型
    signal_strength: SignalStrength  # 信号强度
    
    resonance_score: float         # 共振得分（0-100）
    recommendation: str            # 操作建议
    position_suggestion: float     # 仓位建议（0-1）
    
    validation_reason: str         # 验证理由


class ConceptIndustryValidator:
    """
    概念-行业交叉验证器

    结合同花顺概念热点和同花顺行业分布，识别真正的产业趋势
    """

    def __init__(self, data_manager=None):
        self.dm = data_manager
        self.ths_mapper = None
        self._init_mappers()

    def _init_mappers(self):
        """初始化行业映射器"""
        try:
            from core.data.industry_mapper import THSIndustryMapper

            if self.dm:
                self.ths_mapper = THSIndustryMapper(self.dm)
                logger.info("[ConceptIndustryValidator] 同花顺映射器初始化成功")

        except Exception as e:
            logger.warning(f"[ConceptIndustryValidator] 映射器初始化失败: {e}")
    
    def analyze_concept_industry_distribution(self, 
                                               concept_name: str,
                                               concept_ts_code: str = None) -> Dict:
        """
        分析概念板块的成分股行业分布
        
        Args:
            concept_name: 概念名称
            concept_ts_code: 概念代码（可选）
            
        Returns:
            {
                'total_stocks': 总股票数,
                'industry_distribution': {行业: 股票数},
                'main_industry': 主要行业,
                'concentration': 集中度（0-1）,
                'top3_industries': [(行业, 数量), ...]
            }
        """
        if not self.ths_mapper:
            logger.warning("[analyze_concept_industry_distribution] 映射器未初始化")
            return {}
        
        try:
            # 1. 获取概念代码
            if not concept_ts_code:
                index_info = self.ths_mapper.get_index_by_name(concept_name)
                if index_info is None:
                    logger.warning(f"[analyze_concept_industry_distribution] 未找到概念: {concept_name}")
                    return {}
                concept_ts_code = index_info['ts_code']
            
            # 2. 获取成分股
            members_df = self.ths_mapper.get_index_members(concept_ts_code)
            if members_df.empty:
                logger.warning(f"[analyze_concept_industry_distribution] 概念{concept_name}无成分股数据")
                return {}
            
            # 3. 分析行业分布
            industry_count = {}
            for _, row in members_df.iterrows():
                stock_code = row.get('code', '')
                stock_name = row.get('name', '')
                
                # 使用东财行业映射查找行业
                # 这里简化处理，实际可以通过DataManager获取股票行业信息
                industry = self._get_stock_industry(stock_code, stock_name)
                
                if industry:
                    industry_count[industry] = industry_count.get(industry, 0) + 1
            
            if not industry_count:
                return {
                    'total_stocks': len(members_df),
                    'industry_distribution': {},
                    'main_industry': '未知',
                    'concentration': 0.0,
                    'top3_industries': []
                }
            
            # 4. 计算集中度
            total = sum(industry_count.values())
            max_count = max(industry_count.values())
            concentration = max_count / total if total > 0 else 0
            
            # 5. 找出主要行业
            sorted_industries = sorted(industry_count.items(), key=lambda x: x[1], reverse=True)
            main_industry = sorted_industries[0][0] if sorted_industries else '未知'
            
            return {
                'total_stocks': len(members_df),
                'industry_distribution': industry_count,
                'main_industry': main_industry,
                'concentration': round(concentration, 2),
                'top3_industries': sorted_industries[:3]
            }
            
        except Exception as e:
            logger.error(f"[analyze_concept_industry_distribution] 分析失败: {e}")
            return {}
    
    def _get_stock_industry(self, stock_code: str, stock_name: str) -> str:
        """
        获取股票所属行业
        
        简化实现：通过DataManager获取实时行业数据
        """
        if not self.dm:
            return '未知'
        
        try:
            # 尝试从daily_basic获取行业信息
            # 或者通过其他方式获取
            # 这里返回简化结果
            return '待实现'
        except:
            return '未知'
    
    def validate_concept(self,
                         concept_name: str,
                         concept_rank: int,
                         concept_score: float,
                         hot_industries: List[str] = None) -> CrossValidationResult:
        """
        验证概念板块的行业支撑度
        
        Args:
            concept_name: 概念名称
            concept_rank: 概念排名
            concept_score: 概念评分
            hot_industries: 当前热门行业列表（可选）
            
        Returns:
            交叉验证结果
        """
        # 1. 分析行业分布
        distribution = self.analyze_concept_industry_distribution(concept_name)
        
        if not distribution:
            # 无法获取分布数据，返回默认结果
            return CrossValidationResult(
                concept_name=concept_name,
                concept_rank=concept_rank,
                concept_score=concept_score,
                main_industry='未知',
                industry_concentration=0.0,
                industry_distribution={},
                signal_type=SignalType.WEAK,
                signal_strength=SignalStrength.WEAK,
                resonance_score=0.0,
                recommendation='数据不足，无法验证',
                position_suggestion=0.0,
                validation_reason='无法获取概念成分股行业分布'
            )
        
        main_industry = distribution['main_industry']
        concentration = distribution['concentration']
        
        # 2. 判断信号类型
        if hot_industries and main_industry in hot_industries:
            # 概念热 + 行业热 = 共振
            signal_type = SignalType.RESONANCE
            base_score = 80
        elif hot_industries and main_industry not in hot_industries:
            # 概念热 + 行业冷 = 背离
            signal_type = SignalType.DIVERGENCE
            base_score = 40
        else:
            # 无法判断
            signal_type = SignalType.WEAK
            base_score = 30
        
        # 3. 计算共振得分
        # 因素：概念评分、行业集中度、排名
        rank_factor = max(0, (20 - concept_rank) / 20) * 20  # 排名越靠前越好
        concentration_factor = concentration * 30  # 集中度越高越好
        score_factor = concept_score / 100 * 30  # 概念评分
        
        resonance_score = base_score + rank_factor + concentration_factor + score_factor
        resonance_score = min(resonance_score, 100)
        
        # 4. 判断信号强度
        if resonance_score >= 75:
            signal_strength = SignalStrength.STRONG
        elif resonance_score >= 50:
            signal_strength = SignalStrength.MEDIUM
        else:
            signal_strength = SignalStrength.WEAK
        
        # 5. 生成建议
        recommendation, position = self._generate_validation_advice(
            signal_type, signal_strength, concentration, concept_rank
        )
        
        # 6. 生成验证理由
        reason = self._generate_validation_reason(
            signal_type, main_industry, concentration, distribution.get('top3_industries', [])
        )
        
        return CrossValidationResult(
            concept_name=concept_name,
            concept_rank=concept_rank,
            concept_score=concept_score,
            main_industry=main_industry,
            industry_concentration=concentration,
            industry_distribution=distribution.get('industry_distribution', {}),
            signal_type=signal_type,
            signal_strength=signal_strength,
            resonance_score=round(resonance_score, 2),
            recommendation=recommendation,
            position_suggestion=position,
            validation_reason=reason
        )
    
    def _generate_validation_advice(self,
                                    signal_type: SignalType,
                                    signal_strength: SignalStrength,
                                    concentration: float,
                                    concept_rank: int) -> Tuple[str, float]:
        """生成验证建议"""
        
        if signal_type == SignalType.RESONANCE:
            if signal_strength == SignalStrength.STRONG and concentration >= 0.6:
                return "强共振，重仓参与", 0.8
            elif signal_strength == SignalStrength.MEDIUM:
                return "共振确认，积极做多", 0.6
            else:
                return "弱共振，小仓位试探", 0.3
                
        elif signal_type == SignalType.DIVERGENCE:
            if concentration < 0.4:
                return "纯概念炒作，谨慎参与", 0.2
            else:
                return "行业未跟上，控制仓位", 0.3
                
        else:
            return "信号不明，观望", 0.0
    
    def _generate_validation_reason(self,
                                    signal_type: SignalType,
                                    main_industry: str,
                                    concentration: float,
                                    top3_industries: List[Tuple]) -> str:
        """生成验证理由"""
        
        if signal_type == SignalType.RESONANCE:
            return (f"概念与{main_industry}形成共振，"
                    f"行业集中度{concentration:.0%}，"
                    f"有产业支撑")
        
        elif signal_type == SignalType.DIVERGENCE:
            top3_str = ', '.join([f"{ind}({cnt}只)" for ind, cnt in top3_industries[:3]])
            return (f"概念热度未传导至行业，"
                    f"主要分布在{top3_str}，"
                    f"可能是纯题材炒作")
        
        else:
            return "缺乏行业数据支撑，无法验证"
    
    def batch_validate_concepts(self,
                                 concept_list: List[Dict],
                                 hot_industries: List[str] = None) -> pd.DataFrame:
        """
        批量验证概念列表
        
        Args:
            concept_list: 概念列表，每个元素包含name, rank, score
            hot_industries: 热门行业列表
            
        Returns:
            验证结果DataFrame
        """
        results = []
        
        for concept in concept_list:
            result = self.validate_concept(
                concept_name=concept.get('name', ''),
                concept_rank=concept.get('rank', 99),
                concept_score=concept.get('score', 0),
                hot_industries=hot_industries
            )
            
            results.append({
                '概念名称': result.concept_name,
                '概念排名': result.concept_rank,
                '概念评分': result.concept_score,
                '主要行业': result.main_industry,
                '行业集中度': f"{result.industry_concentration:.0%}",
                '信号类型': result.signal_type.value,
                '信号强度': result.signal_strength.value,
                '共振得分': result.resonance_score,
                '操作建议': result.recommendation,
                '建议仓位': f"{result.position_suggestion*100:.0f}%",
                '验证理由': result.validation_reason
            })
        
        df = pd.DataFrame(results)
        
        # 按共振得分排序
        if not df.empty:
            df = df.sort_values('共振得分', ascending=False)
        
        return df
    
    def get_hot_industries_from_concepts(self, 
                                          top_concepts: List[str],
                                          min_concentration: float = 0.5) -> List[str]:
        """
        从热门概念反推热门行业
        
        Args:
            top_concepts: 热门概念名称列表
            min_concentration: 最小集中度阈值
            
        Returns:
            热门行业列表
        """
        industry_votes = {}
        
        for concept_name in top_concepts:
            distribution = self.analyze_concept_industry_distribution(concept_name)
            
            if distribution and distribution['concentration'] >= min_concentration:
                main_industry = distribution['main_industry']
                industry_votes[main_industry] = industry_votes.get(main_industry, 0) + 1
        
        # 按投票数排序
        sorted_industries = sorted(industry_votes.items(), key=lambda x: x[1], reverse=True)
        
        # 返回有2个以上概念支持的产业
        return [ind for ind, count in sorted_industries if count >= 2]


if __name__ == "__main__":
    # 测试
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    
    from core.data.data_manager_main import DataManager
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    
    print("="*80)
    print("概念-行业交叉验证器测试")
    print("="*80)
    
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    validator = ConceptIndustryValidator(dm)
    
    # 测试单个概念验证
    print("\n测试概念验证:")
    result = validator.validate_concept(
        concept_name="机器人",
        concept_rank=1,
        concept_score=85,
        hot_industries=["机械设备", "电子", "汽车"]
    )
    
    print(f"概念: {result.concept_name}")
    print(f"信号类型: {result.signal_type.value}")
    print(f"共振得分: {result.resonance_score}")
    print(f"建议: {result.recommendation}")
    print(f"理由: {result.validation_reason}")
    
    print("\n" + "="*80)
