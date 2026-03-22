"""
市场主线分析系统
基于东方财富行业分类，统计20日涨停数据，识别主线板块
"""
import pandas as pd
import akshare as ak
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import json

class MarketMainlineAnalyzer:
    def __init__(self):
        self.data_dir = Path("data/industry")
        self.cache_dir = Path("data/cache")
        self.output_dir = Path("output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载行业层级映射
        self.hierarchy_df = pd.read_excel(self.data_dir / "industry_hierarchy.xlsx")
        print(f"✓ 加载行业映射: {len(self.hierarchy_df)} 个三级行业")
        
    def get_trade_dates(self, days=20):
        """获取最近N个交易日"""
        dates = []
        current = datetime.now()
        
        while len(dates) < days:
            date_str = current.strftime("%Y%m%d")
            # 简单排除周末
            if current.weekday() < 5:  # 0-4是周一到周五
                dates.append(date_str)
            current -= timedelta(days=1)
            
        return dates
    
    def fetch_limit_up_data(self, date):
        """获取指定日期的涨停数据"""
        cache_file = self.cache_dir / f"zt_pool_{date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            df = ak.stock_zt_pool_em(date=date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                return df
        except Exception as e:
            print(f"  ✗ 获取{date}涨停数据失败: {e}")
        
        return pd.DataFrame()
    
    def classify_stock_to_l3(self, industry_name):
        """将股票所属行业分类到L3"""
        if pd.isna(industry_name):
            return "其他"
        
        # 直接匹配
        match = self.hierarchy_df[self.hierarchy_df['L3_Industry'] == industry_name]
        if not match.empty:
            return industry_name
        
        # 关键词匹配
        for _, row in self.hierarchy_df.iterrows():
            if row['L3_Industry'] in str(industry_name) or str(industry_name) in row['L3_Industry']:
                return row['L3_Industry']
        
        return "其他"
    
    def analyze_20day_limit_up(self):
        """分析最近20个交易日涨停数据"""
        print("\n" + "="*60)
        print("开始采集最近20个交易日涨停数据...")
        print("="*60)
        
        trade_dates = self.get_trade_dates(20)
        print(f"交易日: {trade_dates[:5]}... (共{len(trade_dates)}天)")
        
        # 统计每个行业的涨停次数
        industry_stats = defaultdict(lambda: {
            'L1': '',
            'L2': '',
            'count': 0,
            'stocks': set(),
            'daily_count': defaultdict(int)
        })
        
        all_limit_up_stocks = []  # 记录所有涨停股票详情
        
        for date in trade_dates:
            print(f"\n  分析日期: {date}")
            df = self.fetch_limit_up_data(date)
            
            if df.empty:
                print(f"    ✗ 无数据")
                continue
            
            print(f"    ✓ 获取到 {len(df)} 只涨停股票")
            
            for _, row in df.iterrows():
                stock_code = row.get('代码', '')
                stock_name = row.get('名称', '')
                industry = row.get('所属行业', '')
                board_height = row.get('连板数', 1)
                change_pct = row.get('涨跌幅', 0)
                
                # 分类到L3
                l3 = self.classify_stock_to_l3(industry)
                
                # 获取L1和L2
                hierarchy_info = self.hierarchy_df[self.hierarchy_df['L3_Industry'] == l3]
                if not hierarchy_info.empty:
                    l1 = hierarchy_info.iloc[0]['L1_Industry']
                    l2 = hierarchy_info.iloc[0]['L2_Industry']
                else:
                    l1 = l2 = "其他"
                
                # 统计
                industry_stats[l3]['L1'] = l1
                industry_stats[l3]['L2'] = l2
                industry_stats[l3]['count'] += 1
                industry_stats[l3]['stocks'].add(stock_name)
                industry_stats[l3]['daily_count'][date] += 1
                
                # 记录详情
                all_limit_up_stocks.append({
                    'date': date,
                    'code': stock_code,
                    'name': stock_name,
                    'L1': l1,
                    'L2': l2,
                    'L3': l3,
                    'board_height': board_height,
                    'change_pct': change_pct,
                    'industry': industry
                })
        
        # 转换为DataFrame
        stats_rows = []
        for l3, data in industry_stats.items():
            if l3 == "其他":
                continue
            stats_rows.append({
                'L1_Industry': data['L1'],
                'L2_Industry': data['L2'],
                'L3_Industry': l3,
                'Total_Limit_Up': data['count'],
                'Unique_Stocks': len(data['stocks']),
                'Avg_Daily': round(data['count'] / len(trade_dates), 2),
                'Stock_List': ','.join(list(data['stocks'])[:10])  # 前10只
            })
        
        stats_df = pd.DataFrame(stats_rows)
        stats_df = stats_df.sort_values('Total_Limit_Up', ascending=False)
        
        # 保存统计结果
        stats_df.to_excel(self.output_dir / "industry_limit_up_stats_20d.xlsx", index=False)
        print(f"\n✓ 行业涨停统计已保存: {self.output_dir / 'industry_limit_up_stats_20d.xlsx'}")
        
        # 保存详细数据
        detail_df = pd.DataFrame(all_limit_up_stocks)
        detail_df.to_excel(self.output_dir / "limit_up_detail_20d.xlsx", index=False)
        print(f"✓ 涨停详细数据已保存: {self.output_dir / 'limit_up_detail_20d.xlsx'}")
        
        return stats_df, detail_df
    
    def identify_mainline_sectors(self, stats_df, top_n=5):
        """识别主线板块"""
        print("\n" + "="*60)
        print("主线板块识别")
        print("="*60)
        
        mainline = stats_df.head(top_n)
        
        print(f"\nTOP {top_n} 主线板块:")
        print("-" * 80)
        for idx, row in mainline.iterrows():
            print(f"\n【{row['L3_Industry']}】")
            print(f"  一级行业: {row['L1_Industry']}")
            print(f"  二级行业: {row['L2_Industry']}")
            print(f"  20日涨停次数: {row['Total_Limit_Up']}")
            print(f"  涉及股票数: {row['Unique_Stocks']}")
            print(f"  日均涨停: {row['Avg_Daily']}")
            print(f"  代表标的: {row['Stock_List'][:50]}...")
        
        # 保存主线板块
        mainline.to_excel(self.output_dir / "mainline_sectors.xlsx", index=False)
        print(f"\n✓ 主线板块已保存: {self.output_dir / 'mainline_sectors.xlsx'}")
        
        return mainline
    
    def screen_focus_stocks(self, detail_df, mainline_sectors):
        """筛选重点关注标的"""
        print("\n" + "="*60)
        print("重点关注标的筛选")
        print("="*60)
        
        mainline_l3 = set(mainline_sectors['L3_Industry'].tolist())
        
        # 从主线板块中筛选股票
        focus_stocks = detail_df[detail_df['L3'].isin(mainline_l3)]
        
        # 筛选标准
        screened = []
        
        for name, group in focus_stocks.groupby('name'):
            # 统计该股票的出现次数和最高连板
            count = len(group)
            max_board = group['board_height'].max()
            avg_change = group['change_pct'].mean()
            l3 = group.iloc[0]['L3']
            l2 = group.iloc[0]['L2']
            l1 = group.iloc[0]['L1']
            
            # 筛选条件：
            # 1. 出现3次以上
            # 2. 最高连板>=2
            # 3. 平均涨幅>=10%
            if count >= 2 and max_board >= 2 and avg_change >= 9.5:
                screened.append({
                    'Stock_Name': name,
                    'L1': l1,
                    'L2': l2,
                    'L3': l3,
                    'Appear_Count': count,
                    'Max_Board': max_board,
                    'Avg_Change': round(avg_change, 2),
                    'Score': count * max_board * (avg_change / 10)  # 综合评分
                })
        
        screened_df = pd.DataFrame(screened)
        if not screened_df.empty:
            screened_df = screened_df.sort_values('Score', ascending=False)
            
            print(f"\n筛选出 {len(screened_df)} 只重点关注标的:")
            print("-" * 80)
            for idx, row in screened_df.head(20).iterrows():
                print(f"\n【{row['Stock_Name']}】")
                print(f"  所属板块: {row['L1']} > {row['L2']} > {row['L3']}")
                print(f"  20日涨停次数: {row['Appear_Count']}")
                print(f"  最高连板: {row['Max_Board']}板")
                print(f"  平均涨幅: {row['Avg_Change']}%")
                print(f"  综合评分: {row['Score']:.2f}")
            
            screened_df.to_excel(self.output_dir / "focus_stocks.xlsx", index=False)
            print(f"\n✓ 关注标的已保存: {self.output_dir / 'focus_stocks.xlsx'}")
        
        return screened_df

def main():
    analyzer = MarketMainlineAnalyzer()
    
    # 1. 分析20日涨停数据
    stats_df, detail_df = analyzer.analyze_20day_limit_up()
    
    # 2. 识别主线板块
    mainline = analyzer.identify_mainline_sectors(stats_df, top_n=5)
    
    # 3. 筛选关注标的
    focus = analyzer.screen_focus_stocks(detail_df, mainline)
    
    print("\n" + "="*60)
    print("分析完成！")
    print("="*60)
    print(f"\n输出文件:")
    print(f"  1. {analyzer.output_dir / 'industry_limit_up_stats_20d.xlsx'}")
    print(f"  2. {analyzer.output_dir / 'limit_up_detail_20d.xlsx'}")
    print(f"  3. {analyzer.output_dir / 'mainline_sectors.xlsx'}")
    print(f"  4. {analyzer.output_dir / 'focus_stocks.xlsx'}")

if __name__ == "__main__":
    main()
