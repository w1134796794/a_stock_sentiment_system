"""
信号优先级与互斥规则 - Signal Priority & Mutual Exclusion

职责：处理多信号冲突，确保交易计划清晰可执行

规则：
  1. 优先级排序
     弱转强(100) > 二板定龙(85) > 龙二波(70) > 首板突破(50)

  2. 互斥规则
     - 同一板块最多推荐 3 只标的
     - 同一模式最多推荐 5 只标的
     - 总推荐标的 ≤ 10 只

  3. 去重规则
     - 同一股票触发多个模式 → 保留最高优先级
     - 保留次高优先级作为"备选逻辑"
"""
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from enum import IntEnum
import loguru

logger = loguru.logger


class PatternPriority(IntEnum):
    """模式优先级（数值越大优先级越高）"""
    WEAK_TO_STRONG = 100       # 弱转强
    SECOND_BOARD_DRAGON = 85   # 二板定龙
    DRAGON_SECOND_WAVE = 70    # 龙二波
    FIRST_BOARD_BREAKOUT = 50  # 首板突破
    DIVERGENCE_CONSENSUS = 40  # 分歧转一致（未启用）
    POSITION_BATTLE = 35       # 卡位板（未启用）
    BLAST_RESEAL = 30          # 炸板回封（未启用）


# 模式名称到优先级的映射
PATTERN_PRIORITY_MAP = {
    '弱转强': PatternPriority.WEAK_TO_STRONG,
    '二板定龙': PatternPriority.SECOND_BOARD_DRAGON,
    '龙二波': PatternPriority.DRAGON_SECOND_WAVE,
    '首板突破': PatternPriority.FIRST_BOARD_BREAKOUT,
    '分歧转一致': PatternPriority.DIVERGENCE_CONSENSUS,
    '卡位板': PatternPriority.POSITION_BATTLE,
    '炸板回封': PatternPriority.BLAST_RESEAL,
}


@dataclass
class PriorityConfig:
    """优先级配置"""
    max_per_sector: int = 3          # 同一板块最多推荐标的数
    max_per_pattern: int = 5         # 同一模式最多推荐标的数
    max_total_signals: int = 10      # 总推荐标的上限
    enable_backup_logic: bool = True  # 是否保留备选逻辑


@dataclass
class RankedSignal:
    """排序后的信号"""
    pattern_type: str
    stock_code: str
    stock_name: str
    priority: int
    confidence: float
    sector_name: str = ""
    sector_heat_score: float = 0.0
    is_primary: bool = True          # 是否为主要信号
    backup_pattern: str = ""         # 备选模式（如果被去重）
    original_signal: object = None   # 原始信号对象


class SignalPriorityManager:
    """
    信号优先级管理器

    处理多信号冲突，确保交易计划清晰可执行
    """

    def __init__(self, config: PriorityConfig = None):
        self.config = config or PriorityConfig()
        logger.info(f"[SignalPriority] 初始化完成: "
                   f"每板块最多{self.config.max_per_sector}只, "
                   f"每模式最多{self.config.max_per_pattern}只, "
                   f"总计最多{self.config.max_total_signals}只")

    def process_signals(self, patterns: Dict[str, List]) -> List[RankedSignal]:
        """
        处理所有模式信号，应用优先级和互斥规则

        Args:
            patterns: {模式名称: [信号列表]}

        Returns:
            排序后的信号列表（已去重、已限制数量）
        """
        logger.info(f"[SignalPriority] 开始处理信号，共{sum(len(v) for v in patterns.values())}个原始信号")

        # Step 1: 转换为RankedSignal并分配优先级
        ranked_signals = self._convert_to_ranked(patterns)
        logger.info(f"[SignalPriority] Step1-转换: {len(ranked_signals)}个信号")

        # Step 2: 同股票去重（保留最高优先级）
        ranked_signals = self._deduplicate_by_stock(ranked_signals)
        logger.info(f"[SignalPriority] Step2-去重: {len(ranked_signals)}个信号")

        # Step 3: 按优先级和置信度排序
        ranked_signals = self._sort_by_priority(ranked_signals)

        # Step 4: 应用板块数量限制
        ranked_signals = self._limit_by_sector(ranked_signals)

        # Step 5: 应用模式数量限制
        ranked_signals = self._limit_by_pattern(ranked_signals)

        # Step 6: 应用总数量限制
        ranked_signals = self._limit_total(ranked_signals)

        logger.info(f"[SignalPriority] 处理完成: {len(ranked_signals)}个最终信号")

        # 打印最终信号
        for i, sig in enumerate(ranked_signals):
            backup_info = f" (备选:{sig.backup_pattern})" if sig.backup_pattern else ""
            logger.info(f"[SignalPriority] {i+1}. [{sig.pattern_type}]{backup_info} "
                       f"{sig.stock_name}({sig.stock_code}) "
                       f"优先级={sig.priority} 置信度={sig.confidence:.2f} "
                       f"板块={sig.sector_name}")

        return ranked_signals

    def _convert_to_ranked(self, patterns: Dict[str, List]) -> List[RankedSignal]:
        """将原始信号转换为RankedSignal"""
        ranked = []

        for pattern_name, signals in patterns.items():
            priority = PATTERN_PRIORITY_MAP.get(pattern_name, 0)

            for signal in signals:
                stock_code = getattr(signal, 'stock_code', '')
                stock_name = getattr(signal, 'stock_name', '')
                confidence = getattr(signal, 'confidence', 0.5)
                sector_name = getattr(signal, 'l2_industry', '') or getattr(signal, 'industry', '')
                sector_heat = getattr(signal, 'sector_heat_score', 0)

                ranked.append(RankedSignal(
                    pattern_type=pattern_name,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    priority=int(priority),
                    confidence=confidence,
                    sector_name=sector_name,
                    sector_heat_score=sector_heat,
                    original_signal=signal,
                ))

        return ranked

    def _deduplicate_by_stock(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """同股票去重：保留最高优先级，记录备选模式"""
        stock_map: Dict[str, RankedSignal] = {}

        for sig in signals:
            code = sig.stock_code
            if code not in stock_map:
                stock_map[code] = sig
            else:
                existing = stock_map[code]
                if sig.priority > existing.priority:
                    # 当前信号优先级更高，替换
                    sig.backup_pattern = existing.pattern_type
                    stock_map[code] = sig
                    logger.debug(f"[SignalPriority] 去重: {sig.stock_name}({code}) "
                                f"保留[{sig.pattern_type}]优先级{sig.priority}, "
                                f"备选[{existing.pattern_type}]优先级{existing.priority}")
                elif sig.priority == existing.priority:
                    # 同优先级，保留置信度更高的
                    if sig.confidence > existing.confidence:
                        sig.backup_pattern = existing.pattern_type
                        stock_map[code] = sig
                else:
                    # 当前信号优先级更低，作为备选
                    if self.config.enable_backup_logic:
                        existing.backup_pattern = (
                            sig.pattern_type if not existing.backup_pattern
                            else f"{existing.backup_pattern}/{sig.pattern_type}"
                        )
                    logger.debug(f"[SignalPriority] 去重: {sig.stock_name}({code}) "
                                f"保留[{existing.pattern_type}]优先级{existing.priority}, "
                                f"忽略[{sig.pattern_type}]优先级{sig.priority}")

        return list(stock_map.values())

    def _sort_by_priority(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """按优先级降序、置信度降序排序"""
        return sorted(signals, key=lambda x: (x.priority, x.confidence), reverse=True)

    def _limit_by_sector(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制同一板块的标的数量"""
        sector_count: Dict[str, int] = {}
        result = []

        for sig in signals:
            sector = sig.sector_name
            if not sector:
                result.append(sig)
                continue

            count = sector_count.get(sector, 0)
            if count < self.config.max_per_sector:
                result.append(sig)
                sector_count[sector] = count + 1
            else:
                logger.debug(f"[SignalPriority] 板块限制: {sig.stock_name} "
                            f"板块[{sector}]已达上限{self.config.max_per_sector}只")

        return result

    def _limit_by_pattern(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制同一模式的标的数量"""
        pattern_count: Dict[str, int] = {}
        result = []

        for sig in signals:
            count = pattern_count.get(sig.pattern_type, 0)
            if count < self.config.max_per_pattern:
                result.append(sig)
                pattern_count[sig.pattern_type] = count + 1
            else:
                logger.debug(f"[SignalPriority] 模式限制: {sig.stock_name} "
                            f"模式[{sig.pattern_type}]已达上限{self.config.max_per_pattern}只")

        return result

    def _limit_total(self, signals: List[RankedSignal]) -> List[RankedSignal]:
        """限制总标的数量"""
        if len(signals) > self.config.max_total_signals:
            logger.info(f"[SignalPriority] 总数限制: {len(signals)}→{self.config.max_total_signals}")
            return signals[:self.config.max_total_signals]
        return signals

    def get_pattern_priority_order(self) -> List[str]:
        """获取模式优先级排序列表"""
        sorted_patterns = sorted(PATTERN_PRIORITY_MAP.items(),
                                key=lambda x: x[1].value, reverse=True)
        return [name for name, _ in sorted_patterns]

    def get_mutual_exclusion_groups(self, signals: List[RankedSignal]) -> Dict[str, List[RankedSignal]]:
        """
        获取互斥分组（同一板块的信号归为一组）

        Returns:
            {板块名称: [该板块的信号列表]}
        """
        groups: Dict[str, List[RankedSignal]] = {}

        for sig in signals:
            sector = sig.sector_name or '未分类'
            if sector not in groups:
                groups[sector] = []
            groups[sector].append(sig)

        return groups

    def generate_priority_report(self, signals: List[RankedSignal]) -> str:
        """生成优先级报告"""
        lines = []
        lines.append("=" * 60)
        lines.append("【信号优先级排序报告】")
        lines.append("=" * 60)

        # 按优先级分组
        priority_groups: Dict[int, List[RankedSignal]] = {}
        for sig in signals:
            if sig.priority not in priority_groups:
                priority_groups[sig.priority] = []
            priority_groups[sig.priority].append(sig)

        for priority in sorted(priority_groups.keys(), reverse=True):
            group = priority_groups[priority]
            pattern_name = group[0].pattern_type if group else "未知"
            lines.append(f"\n[{pattern_name}] 优先级={priority} ({len(group)}只):")
            for sig in group:
                backup = f" [备选:{sig.backup_pattern}]" if sig.backup_pattern else ""
                lines.append(f"  - {sig.stock_name}({sig.stock_code}) "
                           f"置信度={sig.confidence:.2f} 板块={sig.sector_name}{backup}")

        # 互斥分组
        groups = self.get_mutual_exclusion_groups(signals)
        lines.append(f"\n【板块互斥分组】({len(groups)}个板块):")
        for sector, group_signals in sorted(groups.items(),
                                            key=lambda x: len(x[1]), reverse=True):
            lines.append(f"  [{sector}] {len(group_signals)}只: "
                        f"{', '.join(s.stock_name for s in group_signals)}")

        lines.append("=" * 60)
        return "\n".join(lines)