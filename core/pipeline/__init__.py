"""
复盘流水线子层 - Review Pipeline

五层复盘流水线架构：
  Layer 1: 看大盘（定仓位）- MarketEnvAnalyzer
  Layer 2: 看板块（定方向）- SectorAnalysisOrchestrator（封装现有）
  Layer 3: 看个股（定标的）- StockSelectionEngine
  Layer 4: 定计划（定执行）- TradePlanGenerator
  Layer 5: 盘后总结 - ReviewAnalyzer

流水线编排器: ReviewPipeline
共享上下文: SharedContext
"""
