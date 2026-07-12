from core.portfolio.strategy_allocator import AllocationConfig, StrategyPortfolioAllocator


def test_allocator_merges_strategy_overlap_and_respects_concentration_limits():
    allocator = StrategyPortfolioAllocator(
        AllocationConfig(
            max_positions=4,
            max_total_weight=0.50,
            max_stock_weight=0.20,
            max_sector_weight=0.25,
            min_position_weight=0.01,
        )
    )
    rows = [
        {"代码": "000001", "名称": "共识股", "策略ID": "default", "策略名称": "默认", "综合评分": 95, "优先级": 1, "所属板块": "芯片"},
        {"代码": "000001", "名称": "共识股", "策略ID": "ultra_short_board", "策略名称": "打板", "综合评分": 75, "优先级": 2, "所属板块": "芯片"},
        {"代码": "000002", "名称": "同板块", "策略ID": "default", "策略名称": "默认", "综合评分": 90, "优先级": 2, "所属板块": "芯片"},
        {"代码": "000003", "名称": "另一板块", "策略ID": "trend_follow", "策略名称": "趋势", "综合评分": 88, "优先级": 1, "所属板块": "通信"},
    ]

    allocated = allocator.allocate(rows)

    consensus = next(row for row in allocated if row["代码"] == "000001")
    assert consensus["策略来源"] == "default,ultra_short_board"
    assert consensus["策略共识数"] == 2
    assert consensus["组合建议仓位%"] <= 20.0
    assert sum(row["组合建议仓位%"] for row in allocated) <= 50.01
    chip_weight = sum(row["组合建议仓位%"] for row in allocated if row["组合板块"] == "芯片")
    assert chip_weight <= 25.01
