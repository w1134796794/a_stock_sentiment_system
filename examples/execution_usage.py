"""
交易执行引擎使用示例
展示如何在复盘后和当日运行时使用
"""

# ==================== 1. 复盘后生成次日计划 ====================

def after_market_analysis(date_str: str):
    """
    收盘后分析，生成次日交易计划
    """
    # 初始化各模块
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    sector_engine = MultiDimensionSectorEngine(dm)
    strategy_engine = StrategyEngine(dm, sector_engine)
    execution_engine = UnifiedExecutionEngine(dm, strategy_engine)
    
    # 1. 获取数据
    today_zt = dm.get_limit_up_pool(date_str)
    yesterday_zt = dm.get_limit_up_pool(
        dm.get_date_offset(date_str, -1)
    )
    
    # 2. 运行所有策略检测
    all_signals = {
        "二板定龙": strategy_engine.detect_second_board_dragon(
            yesterday_zt, today_zt, today_zt  # 简化示例
        ),
        "分歧转一致": [],  # 从其他分析获取
        "弱转强": [],
        "首板突破": [],
        "炸板回封": [],
        "龙二波": []
    }
    
    # 3. 生成次日交易计划
    next_day_plans = execution_engine.generate_next_day_plans(
        date_str, all_signals
    )
    
    # 4. 保存到本地
    plan_file = f"trade_plans_{date_str}.csv"
    next_day_plans.to_csv(plan_file, index=False, encoding='utf-8-sig')
    
    # 5. 生成交易报告
    report = execution_engine.generate_trade_report(next_day_plans, date_str)
    print(report)
    
    # 6. 发送到微信/邮件（可选）
    send_notification(report)
    
    return next_day_plans

# ==================== 2. 次日开盘前加载计划 ====================

def load_today_plan(date_str: str) -> pd.DataFrame:
    """
    次日开盘前加载交易计划
    """
    plan_file = f"trade_plans_{date_str}.csv"
    
    if not os.path.exists(plan_file):
        print(f"未找到{date_str}的交易计划")
        return pd.DataFrame()
    
    plans = pd.read_csv(plan_file)
    
    # 按介入时间排序
    time_order = {
        "09:24:30-09:25:00": 1,  # 竞价末段
        "09:30:00-09:31:00": 2,  # 开盘
        "09:31-10:00": 3,        # 早盘
        "10:00-11:30": 4,
        "13:00-14:30": 5,
        "14:30-15:00": 6
    }
    
    plans['time_sort'] = plans['介入时机'].map(time_order)
    plans = plans.sort_values('time_sort')
    
    print(f"
今日交易计划（共{len(plans)}条）：")
    print("=" * 60)
    
    for timing, group in plans.groupby('介入时机'):
        print(f"
【{timing}】")
        for _, row in group.iterrows():
            print(f"  {row['模式']} | {row['名称']} | {row['动作']} | 仓位:{row['仓位']}")
    
    return plans

# ==================== 3. 当日实时监控 ====================

def real_time_monitor(date_str: str, plans: pd.DataFrame):
    """
    当日实时监控，触发交易信号
    """
    execution_engine = UnifiedExecutionEngine(None, None)
    
    # 转换为TradePlan对象
    watchlist = []
    for _, row in plans.iterrows():
        plan = TradePlan(
            pattern_type=row['模式'],
            stock_code=row['代码'],
            stock_name=row['名称'],
            action=TradeAction(row['动作']),
            entry_timing=TimeSlot(row['介入时机']),
            entry_price=row['目标价'],
            stop_loss=row['止损价'],
            take_profit=row['止盈价'],
            position_size=row['仓位'],
            pre_conditions=row['前置条件'].split("; "),
            cancel_conditions=row['取消条件'].split("; "),
            confidence=row['置信度'],
            reason=row['理由'],
            add_to_watchlist=row['加入观察池']
        )
        watchlist.append(plan)
    
    # 模拟实时监控（实际应接入行情API）
    print("
开始实时监控...")
    
    check_times = [
        time(9, 24, 45),  # 竞价末段
        time(9, 30, 0),   # 开盘
        time(9, 35, 0),   # 早盘
        time(10, 0, 0),   # 上午
    ]
    
    for check_time in check_times:
        print(f"
{'='*60}")
        print(f"检查时间: {check_time}")
        print(f"{'='*60}")
        
        # 获取实时数据（模拟）
        auction_data = get_auction_data(date_str)  # 竞价数据
        tick_data = get_tick_data(date_str)        # 分时数据
        
        # 检查触发
        triggers = execution_engine.real_time_check(
            check_time, auction_data, tick_data, watchlist
        )
        
        for trigger in triggers:
            plan = trigger['plan']
            action = trigger['action']
            
            if action == "EXECUTE":
                print(f"
🚨 触发交易信号！")
                print(f"  模式: {plan.pattern_type}")
                print(f"  股票: {plan.stock_name}({plan.stock_code})")
                print(f"  动作: {plan.action.value}")
                print(f"  价格: {trigger['price']:.2f}")
                print(f"  仓位: {plan.position_size}")
                print(f"  原因: {trigger['reason']}")
                
                # 执行交易（对接券商API）
                # execute_trade(plan, trigger['price'])
                
            elif action == "CANCEL":
                print(f"
❌ 取消交易计划")
                print(f"  股票: {plan.stock_name}")
                print(f"  原因: {trigger['reason']}")
                
                # 从观察池移除
                watchlist.remove(plan)

# ==================== 4. 一键运行示例 ====================

def main():
    """
    完整流程示例
    """
    today = datetime.now().strftime("%Y%m%d")
    
    # 盘后分析（15:40运行）
    print("="*60)
    print("盘后分析模式")
    print("="*60)
    plans = after_market_analysis(today)
    
    # 次日开盘前（09:15运行）
    print("
" + "="*60)
    print("次日开盘前加载")
    print("="*60)
    next_day = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    plans = load_today_plan(next_day)
    
    # 实时监控（09:25-10:00）
    print("
" + "="*60)
    print("实时监控模式")
    print("="*60)
    real_time_monitor(next_day, plans)

if __name__ == "__main__":
    main()
