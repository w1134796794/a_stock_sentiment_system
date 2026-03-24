"""
A股短线情绪量化系统 - 主程序入口
整合所有模块，提供CLI交互
"""
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import loguru

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    TUSHARE_TOKEN, CACHE_DIR, OUTPUT_DIR, 
    INDUSTRY_MAPPING_FILE, TRADE_HOUR, TRADE_MINUTE
)
from core.data_manager import DataManager
from core.industry_mapper import IndustryMapper
from core.sentiment_engine import SentimentEngine
from core.pattern_recognition import PatternRecognition
from core.report_generator import ReportGenerator
from core.sector_heat_calculator import SectorHeatCalculator

logger = loguru.logger

class SentimentSystem:
    def __init__(self):
        self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        self.mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
        self.engine = SentimentEngine()
        self.reporter = ReportGenerator(OUTPUT_DIR)
        self.today = datetime.now().strftime("%Y%m%d")
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        
    def run_daily_analysis(self, date: str = None):
        """
        执行每日完整分析流程
        支持非交易日自动关联最近交易日
        """
        if date is None:
            date = self.today
        
        # 验证交易日，非交易日自动关联最近交易日
        is_valid, actual_date, message = self.dm.validate_trade_date(date)
        if not is_valid:
            logger.info(f"交易日验证: {message}")
            date = actual_date
            # 更新yesterday为实际日期的前一天
            date_obj = datetime.strptime(date, "%Y%m%d")
            yesterday_obj = date_obj - timedelta(days=1)
            self.yesterday = self.dm.get_nearest_trade_date(yesterday_obj.strftime("%Y%m%d"), "backward")
        
        logger.info(f"开始执行 {date} 的日度分析...")
        logger.info(f"对比日期: {self.yesterday}")
        
        # 1. 数据获取
        logger.info("[1/5] 获取涨停池数据...")
        zt_pool = self.dm.get_limit_up_pool(date)
        if zt_pool.empty:
            logger.warning(f"未获取到 {date} 的涨停数据，可能非交易日")
            return
        
        logger.info(f"获取到 {len(zt_pool)} 只涨停股票")
        
        # 2. 构建层级结构
        logger.info("[2/5] 构建行业层级映射...")
        hierarchy_df = self.mapper.build_hierarchy_dataframe(zt_pool)
        if not hierarchy_df.empty:
            logger.info(f"层级映射完成，覆盖 {hierarchy_df['L3_Industry'].nunique()} 个三级行业")
        
        # 3. 情绪分析
        logger.info("[3/5] 计算情绪指标...")
        mainline_df = self.engine.calculate_mainline_strength(hierarchy_df)
        gradient = self.engine.track_gradient(hierarchy_df)
        sentiment = self.engine.calculate_market_sentiment(hierarchy_df)
        
        logger.info(f"市场温度: {sentiment.get('temperature', '未知')}")
        logger.info(f"最高板: {gradient.get('highest_board', 0)}板 - {gradient.get('highest_stock', '')}")
        
        # 4. 模式识别
        logger.info("[4/5] 识别交易模式...")
        pr = PatternRecognition(self.dm)
        patterns = pr.scan_all_patterns(date, self.yesterday)
        
        total_signals = sum(len(v) for v in patterns.values())
        logger.info(f"识别到 {total_signals} 个交易信号")
        for ptype, signals in patterns.items():
            if signals:
                logger.info(f"  - {ptype}: {len(signals)}个")
        
        # 5. 综合当日数据和20日数据计算板块权重
        logger.info("[5/5] 计算综合板块权重（当日+20日）...")
        display_mainline_df = self._calculate_combined_mainline(mainline_df, hierarchy_df)
        
        # 6. 多维度板块热度计算（3日/5日/20日）
        logger.info("[6/6] 计算多维度板块热度（3日/5日/20日）...")
        heat_calculator = SectorHeatCalculator()
        sector_heat_df = self._calculate_sector_heat(heat_calculator, date)
        
        # 输出板块分类结果
        if not sector_heat_df.empty:
            new_mainlines = sector_heat_df[sector_heat_df['板块分类'] == '新主线候选']
            old_mainlines = sector_heat_df[sector_heat_df['板块分类'] == '老主线确认']
            decline_warnings = sector_heat_df[sector_heat_df['板块分类'] == '退潮预警']
            
            if not new_mainlines.empty:
                logger.info(f"🚀 新主线候选: {len(new_mainlines)}个")
                for _, row in new_mainlines.head(3).iterrows():
                    logger.info(f"   {row['L3_Industry']}: 动量{row['动量加速度']:.1%}, 综合得分{row['综合得分']:.1f}")
            
            if not old_mainlines.empty:
                logger.info(f"📈 老主线确认: {len(old_mainlines)}个")
                for _, row in old_mainlines.head(3).iterrows():
                    logger.info(f"   {row['L3_Industry']}: 20日{row['20日涨停数']}家, 综合得分{row['综合得分']:.1f}")
            
            if not decline_warnings.empty:
                logger.info(f"⚠️  退潮预警: {len(decline_warnings)}个")
                for _, row in decline_warnings.head(3).iterrows():
                    logger.info(f"   {row['L3_Industry']}: 动量{row['动量加速度']:.1%}, 3日{row['3日涨停数']}家")
        
        # 7. 为核心标的获取概念数据
        logger.info("[7/7] 获取核心标的所属概念...")
        if not hierarchy_df.empty:
            # 只获取10点半前封板的核心标的的概念
            core_stocks_mask = hierarchy_df.apply(lambda row: self._is_core_stock(row), axis=1)
            core_stocks_df = hierarchy_df[core_stocks_mask].copy()
            
            if not core_stocks_df.empty:
                # 获取概念数据
                core_stocks_df = self.dm.enrich_core_stocks_concepts(core_stocks_df)
                # 更新hierarchy_df中的概念数据
                hierarchy_df.loc[core_stocks_df.index, 'Concept'] = core_stocks_df['概念']
                logger.info(f"已获取{len(core_stocks_df)}只核心标的的概念数据")
        
        # 8. 生成报告
        logger.info("[8/8] 生成分析报告...")
        report_data = {
            'mainline_df': display_mainline_df,
            'gradient': gradient,
            'sentiment': sentiment,
            'patterns': patterns,
            'hierarchy_df': hierarchy_df,
            'sector_heat_df': sector_heat_df  # 新增板块热度数据
        }
        
        # 使用带时间戳的文件名避免文件被占用
        timestamp = datetime.now().strftime("%H%M%S")
        report_file_name = f"A股情绪分析报告_{date}_{timestamp}.xlsx"
        report_path = self.reporter.create_daily_report(report_data, file_name=report_file_name)
        logger.info(f"✅ 分析完成，报告保存至: {report_path}")
        
        # 7. 输出交易建议
        self._print_trading_advice(display_mainline_df, patterns, sentiment)
    
    def _print_trading_advice(self, mainline_df, patterns, sentiment):
        """输出简明的交易建议"""
        print("\n" + "="*60)
        print("【今日交易决策辅助】")
        print("="*60)
        
        # 情绪判断
        temp = sentiment.get('temperature', '')
        if '高潮' in temp:
            print("⚠️  市场情绪高潮，建议减仓观望，避免高位接盘")
        elif '冰点' in temp:
            print("❄️  市场情绪冰点，轻仓试错或空仓等待")
        elif '活跃' in temp:
            print("🔥  市场活跃，积极参与主线板块")
        
        # 主线推荐
        if not mainline_df.empty:
            print("\n📊 重点关注的L3板块（主线Top3）:")
            for i, row in mainline_df.head(3).iterrows():
                print(f"  {i+1}. {row['L3_Industry']} (涨停{row['LimitUp_Count']}家, 强度{row['Strength_Score']})")
        
        # 模式推荐
        print("\n🎯 明日竞价关注标的:")
        watchlist = []
        for ptype, signals in patterns.items():
            for sig in signals[:2]:  # 每类模式取前2
                watchlist.append(f"  • {sig.stock_name} ({sig.pattern_type}) - {sig.description}")
        if watchlist:
            print("\n".join(watchlist[:5]))  # 最多显示5个
        else:
            print("  暂无明确信号，建议结合明日竞价情况")
        
        print("="*60)
    
    def run_backtest_mode(self, start_date: str, end_date: str):
        """回测模式"""
        logger.info(f"启动回测: {start_date} 至 {end_date}")
        # 实现多日期回测逻辑
        pass
    
    def _calculate_combined_mainline(self, mainline_df: pd.DataFrame, hierarchy_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算综合板块权重（当日数据 + 20日统计数据）
        权重公式: 综合强度 = 20日涨停数×0.4 + 当日涨停数×0.4 + 最高连板×0.2
        """
        # 1. 统计当日各板块涨停数
        today_stats = {}
        if not hierarchy_df.empty:
            for l3_name, group in hierarchy_df.groupby('L3_Industry'):
                if l3_name == '其他':
                    continue
                
                # 获取该组的L1和L2（取第一个非空值）
                l1_values = group['L1_Industry'].dropna()
                l2_values = group['L2_Industry'].dropna()
                
                l1 = l1_values.iloc[0] if not l1_values.empty and l1_values.iloc[0] != '其他' else '未知'
                l2 = l2_values.iloc[0] if not l2_values.empty and l2_values.iloc[0] != '其他' else '未知'
                
                # 如果L1或L2为空，尝试从行业映射获取
                if l1 == '未知' or l2 == '未知' or l1 == '其他' or l2 == '其他':
                    from core.industry_mapper import IndustryMapper
                    from config.settings import INDUSTRY_MAPPING_FILE
                    mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
                    l2_mapped = mapper.get_l2_by_l3(l3_name)
                    l1_mapped = mapper.get_l1_by_l2(l2_mapped)
                    if l2 == '未知' or l2 == '其他':
                        l2 = l2_mapped if l2_mapped != '其他' else l2
                    if l1 == '未知' or l1 == '其他':
                        l1 = l1_mapped if l1_mapped != '其他' else l1
                
                today_stats[l3_name] = {
                    'today_count': len(group),
                    'max_board': group['BoardHeight'].max() if 'BoardHeight' in group.columns else 1,
                    'l1': l1,
                    'l2': l2
                }
        
        # 2. 加载20日统计数据
        mainline_20d_path = Path(OUTPUT_DIR) / "mainline_sectors.xlsx"
        combined_data = []
        
        if mainline_20d_path.exists():
            mainline_20d_df = pd.read_excel(mainline_20d_path)
            
            # 3. 合并数据计算综合权重
            for _, row in mainline_20d_df.iterrows():
                l3_name = row['L3_Industry']
                
                # 20日数据
                count_20d = row['Total_Limit_Up']
                
                # 当日数据（如果有）
                today_data = today_stats.get(l3_name, {})
                today_count = today_data.get('today_count', 0)
                today_max_board = today_data.get('max_board', 1)
                
                # 取20日和当日的最高连板
                max_board = max(
                    today_max_board,
                    today_data.get('max_board', 1)
                )
                
                # 计算综合强度分
                # 权重: 20日涨停数×0.4 + 当日涨停数×0.4 + 最高连板×0.2
                strength_score = (
                    count_20d * 0.4 +
                    today_count * 0.4 * 3 +  # 当日涨停权重放大3倍（当日重要性更高）
                    max_board * 0.2 * 5      # 连板权重放大5倍
                )
                
                combined_data.append({
                    'L1_Industry': row['L1_Industry'],
                    'L2_Industry': row['L2_Industry'],
                    'L3_Industry': l3_name,
                    'LimitUp_Count': count_20d,  # 兼容报告生成器
                    'LimitUp_Count_20d': count_20d,
                    'LimitUp_Count_Today': today_count,
                    'Max_BoardHeight': max_board,
                    'Strength_Score': round(strength_score, 2)
                })
            
            # 4. 检查当日新出现的强势板块（不在20日统计中但当日有多只涨停）
            for l3_name, data in today_stats.items():
                if l3_name not in mainline_20d_df['L3_Industry'].values and data['today_count'] >= 2:
                    # 新出现的强势板块 - 使用L2作为显示名称，但保留L3用于关联
                    strength_score = data['today_count'] * 0.4 * 3 + data['max_board'] * 0.2 * 5
                    
                    # 获取正确的L1和L2
                    l1 = data['l1']
                    l2 = data['l2']
                    
                    # 如果L1或L2是"其他"，尝试从行业映射获取
                    if l1 == '其他' or l2 == '其他' or l1 == '未知' or l2 == '未知':
                        # 从mapper获取正确的行业信息
                        from core.industry_mapper import IndustryMapper
                        from config.settings import INDUSTRY_MAPPING_FILE
                        mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
                        l2_correct = mapper.get_l2_by_l3(l3_name)
                        l1_correct = mapper.get_l1_by_l2(l2_correct)
                        l1 = l1_correct if l1_correct != '其他' else l1
                        l2 = l2_correct if l2_correct != '其他' else l2
                    
                    combined_data.append({
                        'L1_Industry': l1,
                        'L2_Industry': l2,
                        'L3_Industry': l3_name,
                        'LimitUp_Count': data['today_count'],  # 兼容报告生成器
                        'LimitUp_Count_20d': 0,
                        'LimitUp_Count_Today': data['today_count'],
                        'Max_BoardHeight': data['max_board'],
                        'Strength_Score': round(strength_score, 2),
                        'Is_New': True  # 标记为新板块
                    })
                    logger.info(f"发现新强势板块: {l3_name} (当日{data['today_count']}只涨停)")
        else:
            # 如果没有20日数据，使用当日数据
            logger.info("20日统计数据不存在，使用当日数据")
            return mainline_df
        
        # 5. 排序并返回
        result_df = pd.DataFrame(combined_data)
        if not result_df.empty:
            result_df = result_df.sort_values('Strength_Score', ascending=False)
            logger.info(f"✓ 综合板块分析完成: {len(result_df)} 个板块")
            # 显示TOP5
            for i, row in result_df.head(5).iterrows():
                new_flag = " [NEW]" if row.get('Is_New', False) else ""
                logger.info(f"  {row['L3_Industry']}: 强度{row['Strength_Score']:.1f} (20日{row['LimitUp_Count_20d']}, 当日{row['LimitUp_Count_Today']}){new_flag}")
        
        return result_df
    
    def _calculate_sector_heat(self, calculator, date: str) -> pd.DataFrame:
        """
        计算多维度板块热度（3日/5日/20日）
        """
        # 获取最近20个交易日的涨停数据
        limit_up_history = {}
        
        for i in range(20):
            check_date = (datetime.strptime(date, "%Y%m%d") - timedelta(days=i)).strftime("%Y%m%d")
            
            # 验证是否为交易日
            is_valid, actual_date, _ = self.dm.validate_trade_date(check_date)
            if actual_date not in limit_up_history:
                zt_pool = self.dm.get_limit_up_pool(actual_date)
                if not zt_pool.empty:
                    # 构建层级数据
                    hierarchy = self.mapper.build_hierarchy_dataframe(zt_pool)
                    limit_up_history[actual_date] = hierarchy
        
        # 使用计算器分析板块热度
        sector_heat_df = calculator.analyze_from_limit_up_data(limit_up_history, self.mapper)
        
        return sector_heat_df
    
    def _is_core_stock(self, row) -> bool:
        """
        判断是否为10点半前封板的核心标的
        """
        # 检查涨停时间
        limit_up_time = str(row.get('LimitUpTime', ''))
        if limit_up_time.isdigit():
            limit_up_time = limit_up_time.zfill(6)
        if len(limit_up_time) == 6:
            limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
        
        # 只保留10:30前封板的
        if limit_up_time and limit_up_time <= '10:30:00':
            l1 = row.get('L1_Industry', '')
            l2 = row.get('L2_Industry', '')
            l3 = row.get('L3_Industry', '')
            
            # 跳过"其他"行业
            if l3 != '其他' and l2 != '其他' and l1 != '其他':
                return True
        
        return False
    
    def update_industry_mapping(self):
        """手动更新行业映射"""
        logger.info("更新行业映射数据...")
        # 从AkShare获取最新行业列表并更新Excel
        try:
            concept_df = self.dm.get_concept_industry()
            if not concept_df.empty:
                logger.info(f"获取到 {len(concept_df)} 个行业板块")
                # 这里可以实现自动映射逻辑
        except Exception as e:
            logger.error(f"更新失败: {e}")

def main():
    parser = argparse.ArgumentParser(description='A股短线情绪量化系统')
    parser.add_argument('--date', type=str, help='分析日期(YYYYMMDD)，默认今日')
    parser.add_argument('--mode', type=str, default='daily', 
                       choices=['daily', 'backtest', 'update'], help='运行模式')
    parser.add_argument('--start', type=str, help='回测开始日期')
    parser.add_argument('--end', type=str, help='回测结束日期')
    
    args = parser.parse_args()
    
    # 配置日志
    loguru.logger.add(
        Path(CACHE_DIR) / "system.log",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8"
    )
    
    system = SentimentSystem()
    
    if args.mode == 'daily':
        date = args.date if args.date else system.today
        system.run_daily_analysis(date)
    elif args.mode == 'backtest':
        if not args.start or not args.end:
            print("回测模式需要指定 --start 和 --end 日期")
            return
        system.run_backtest_mode(args.start, args.end)
    elif args.mode == 'update':
        system.update_industry_mapping()

if __name__ == "__main__":
    # 如果直接运行，执行今日分析
    print("🚀 A股短线情绪量化系统启动...")
    print("提示: 首次运行请先在 config/settings.py 中配置Tushare Token")
    print("-" * 60)
    
    try:
        system = SentimentSystem()
        system.run_daily_analysis()
    except Exception as e:
        logger.error(f"系统运行错误: {e}")
        print(f"❌ 运行出错: {e}")
        print("请检查:")
        print("1. 是否已安装依赖: pip install pandas tushare akshare xlsxwriter loguru")
        print("2. 是否已配置Tushare Token")
        print("3. 网络连接是否正常")
