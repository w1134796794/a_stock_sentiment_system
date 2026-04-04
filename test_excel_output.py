"""
测试Excel输出效果，验证核心数据指标是否直观显示
"""
from core.sector_heat_v2 import SectorHeatCalculatorV2
from core.data_manager import DataManager
from config.settings import TUSHARE_TOKEN, CACHE_DIR
import pandas as pd
from datetime import datetime, timedelta

def test_excel_output():
    """测试Excel输出效果"""
    print("="*70)
    print("测试Excel输出效果 - 验证核心数据指标是否直观显示")
    print("="*70)
    
    calculator = SectorHeatCalculatorV2()
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    
    # 获取真实的交易日数据
    trade_date = '20260403'
    print(f"\n分析日期: {trade_date}")
    
    # 获取当日涨停池数据
    today_zt = dm.get_limit_up_pool(trade_date)
    if today_zt.empty:
        print("❌ 当日涨停池数据为空")
        return
    
    print(f"✓ 获取到当日涨停池数据: {len(today_zt)} 只股票")
    
    # 构建层级数据
    from core.industry_mapper import IndustryMapper
    from config.settings import INDUSTRY_MAPPING_FILE
    
    mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
    hierarchy_df = mapper.build_hierarchy_dataframe(today_zt)
    
    # 获取历史数据
    history_pools = {}
    for i in range(1, 3):  # 获取前2个交易日
        prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=i)).strftime("%Y%m%d")
        prev_zt = dm.get_limit_up_pool(prev_date)
        if not prev_zt.empty:
            prev_hierarchy = mapper.build_hierarchy_dataframe(prev_zt)
            history_pools[prev_date] = prev_hierarchy
            print(f"✓ 获取到历史数据: {prev_date} - {len(prev_zt)} 只股票")
    
    # 运行板块热度分析
    print(f"\n运行板块热度分析...")
    result = calculator.analyze_all_sectors_v2(
        hierarchy_df, history_pools, mapper, dm, trade_date
    )
    
    if result.empty:
        print("❌ 板块热度分析结果为空")
        return
    
    print(f"✓ 板块热度分析完成，输出 {len(result)} 个板块信号")
    
    # 显示Excel输出格式
    print(f"\nExcel输出格式预览:")
    print("-" * 100)
    
    # 显示列名
    columns = result.columns.tolist()
    print("列名:", columns)
    
    # 显示前几个板块的数据
    print(f"\n前{min(3, len(result))}个板块的核心数据指标:")
    print("-" * 100)
    
    for idx, row in result.head(3).iterrows():
        print(f"\n【{row['二级行业']}】")
        print(f"  趋势阶段: {row['趋势阶段']}")
        print(f"  共振类型: {row['共振类型']}")
        print(f"  涨停趋势: {row['涨停趋势']}")
        print(f"  爆发倍数: {row['爆发倍数']}")
        print(f"  短期动量: {row['短期动量']}")
        print(f"  综合得分: {row['综合得分']}")
        print(f"  资金流向: {row['资金流向']}")
        print(f"  龙头质量: {row['龙头质量']}")
        print(f"  行动建议: {row['行动建议']}")
        print(f"  仓位建议: {row['仓位建议']}")
    
    # 检查是否有你提到的数据模式
    print(f"\n检查涨停趋势数据:")
    print("-" * 100)
    
    target_pattern = "{'今日涨停': 5, '昨日涨停': 2, '2日滚动': 7, '3日滚动': 10, '5日滚动': 14, '20日滚动': 41}"
    print(f"目标模式: {target_pattern}")
    
    # 显示所有板块的涨停趋势
    print(f"\n所有板块的涨停趋势数据:")
    for idx, row in result.iterrows():
        print(f"  {row['二级行业']}: {row['涨停趋势']}")
    
    # 检查是否有类似的数据模式
    found_match = False
    for idx, row in result.iterrows():
        zt_trend_str = row['涨停趋势']
        # 简单的字符串匹配检查
        if "'今日涨停': 5" in zt_trend_str and "'昨日涨停': 2" in zt_trend_str:
            print(f"\n✅ 找到类似模式: {row['二级行业']}")
            print(f"   涨停趋势: {zt_trend_str}")
            found_match = True
    
    if not found_match:
        print("\n❌ 未找到完全匹配的数据模式")
        print("   当前数据中最接近的模式:")
        
        # 显示涨停数最多的板块
        max_today_zt = 0
        max_zt_sector = ""
        max_zt_trend = ""
        
        for idx, row in result.iterrows():
            zt_trend_str = row['涨停趋势']
            # 从字符串中提取今日涨停数
            import re
            match = re.search(r"'今日涨停': (\d+)", zt_trend_str)
            if match:
                today_zt = int(match.group(1))
                if today_zt > max_today_zt:
                    max_today_zt = today_zt
                    max_zt_sector = row['二级行业']
                    max_zt_trend = zt_trend_str
        
        if max_zt_sector:
            print(f"   涨停最多的板块: {max_zt_sector}")
            print(f"   涨停趋势: {max_zt_trend}")
    
    # 生成Excel文件测试
    print(f"\n生成Excel文件测试...")
    
    # 创建测试Excel文件
    excel_file = f"g:\\a_stock_sentiment_system\\output\\test_sector_heat_{trade_date}.xlsx"
    
    try:
        with pd.ExcelWriter(excel_file, engine='openpyxl') as writer:
            # 板块热度分析表
            result.to_excel(writer, sheet_name='板块热度分析', index=False)
            worksheet = writer.sheets['板块热度分析']
            
            # 设置列宽
            worksheet.column_dimensions['A'].width = 8   # 优先级
            worksheet.column_dimensions['B'].width = 15  # 联动信号
            worksheet.column_dimensions['C'].width = 10  # 趋势阶段
            worksheet.column_dimensions['D'].width = 10  # 共振类型
            worksheet.column_dimensions['E'].width = 15  # 一级行业
            worksheet.column_dimensions['F'].width = 15  # 二级行业
            worksheet.column_dimensions['G'].width = 20  # 行动建议
            worksheet.column_dimensions['H'].width = 10  # 仓位建议
            worksheet.column_dimensions['I'].width = 8   # 置信度
            # 涨停趋势列（需要更宽的列）
            worksheet.column_dimensions['J'].width = 40  # 涨停趋势
            worksheet.column_dimensions['K'].width = 10  # 爆发倍数
            worksheet.column_dimensions['L'].width = 10  # 短期动量
            worksheet.column_dimensions['M'].width = 10  # 综合得分
            
        print(f"✅ Excel文件生成成功: {excel_file}")
        print(f"   文件包含 {len(result)} 个板块的详细数据")
        
    except Exception as e:
        print(f"❌ Excel文件生成失败: {e}")

def check_key_metrics_structure():
    """检查key_metrics的数据结构"""
    print(f"\n" + "="*70)
    print("检查key_metrics的数据结构")
    print("="*70)
    
    calculator = SectorHeatCalculatorV2()
    
    # 创建一个测试信号来检查key_metrics结构
    from core.sector_heat_v2 import SectorSignal, TrendStage, ResonanceType, CapitalFlowType, LeaderQuality
    
    test_key_metrics = {
        "今日涨停": 5,
        "昨日涨停": 2,
        "2日滚动": 7,
        "3日滚动": 10,
        "5日滚动": 14,
        "20日滚动": 41,
        "爆发倍数": "2.5x",
        "短期动量": "19.0%",
        "综合得分": "85.6",
        "趋势阶段": "爆发期",
        "共振类型": "强共振",
        "资金流向": "机构主导",
        "龙头质量": "强龙头",
        "轮动信号": "加速中"
    }
    
    test_signal = SectorSignal(
        l2_name="通信设备",
        l1_name="信息技术",
        action="[爆发]积极参与，做前排",
        priority=1,
        confidence=0.85,
        key_metrics=test_key_metrics,
        watch_reason="爆发期 + 强共振 + 机构主导 + 强龙头",
        risk_warning="加速期，注意分歧",
        trend_stage=TrendStage.EXPLOSION,
        resonance_type=ResonanceType.STRONG,
        combined_signal="强共振爆发",
        position_size="heavy",
        capital_flow=CapitalFlowType.INSTITUTION_LEADING,
        leader_quality=LeaderQuality.STRONG_LEADER,
        risk_score=0.3
    )
    
    print(f"测试信号的核心数据指标:")
    for key, value in test_key_metrics.items():
        print(f"  {key}: {value}")

if __name__ == "__main__":
    try:
        test_excel_output()
        check_key_metrics_structure()
        
        print("\n" + "="*70)
        print("测试完成！")
        print("="*70)
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()