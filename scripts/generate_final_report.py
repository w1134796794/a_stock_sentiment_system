"""
生成完整的市场分析报告
基于已采集的20日数据生成专业分析报告
"""
import pandas as pd
from pathlib import Path
from datetime import datetime

class FinalReportGenerator:
    def __init__(self):
        self.output_dir = Path("output")
        self.report_file = self.output_dir / f"市场主线分析报告_{datetime.now().strftime('%Y%m%d')}.md"
        
    def load_data(self):
        """加载所有分析数据"""
        self.industry_stats = pd.read_excel(self.output_dir / "industry_limit_up_stats_20d.xlsx")
        self.mainline = pd.read_excel(self.output_dir / "mainline_sectors.xlsx")
        self.focus = pd.read_excel(self.output_dir / "focus_stocks.xlsx")
        self.detail = pd.read_excel(self.output_dir / "limit_up_detail_20d.xlsx")
        
    def generate_report(self):
        """生成完整分析报告"""
        self.load_data()
        
        report = []
        
        # 报告标题
        report.append("# A股市场主线分析报告")
        report.append(f"\n**报告日期**: {datetime.now().strftime('%Y年%m月%d日')}")
        report.append(f"**数据周期**: 最近20个交易日")
        report.append("\n---\n")
        
        # 一、行业分类体系
        report.append("## 一、东方财富行业分类体系\n")
        report.append("基于东方财富手机APP行业标准分类：")
        report.append("- **一级行业**: 30个")
        report.append("- **二级行业**: 122个")
        report.append("- **三级行业**: 243个")
        report.append("\n---\n")
        
        # 二、20日涨停统计
        report.append("## 二、20日行业涨停统计\n")
        report.append(f"**统计范围**: 最近20个交易日")
        report.append(f"**总涨停次数**: {self.industry_stats['Total_Limit_Up'].sum()} 次")
        report.append(f"**涉及行业数**: {len(self.industry_stats)} 个三级行业")
        report.append(f"**涉及股票数**: {self.industry_stats['Unique_Stocks'].sum()} 只\n")
        
        # TOP15行业
        report.append("### TOP15 涨停行业排名\n")
        report.append("| 排名 | 一级行业 | 二级行业 | 三级行业 | 涨停次数 | 股票数 | 日均涨停 |")
        report.append("|------|----------|----------|----------|----------|--------|----------|")
        
        top15 = self.industry_stats.head(15)
        for idx, (_, row) in enumerate(top15.iterrows(), 1):
            report.append(f"| {idx} | {row['L1_Industry']} | {row['L2_Industry']} | {row['L3_Industry']} | {row['Total_Limit_Up']} | {row['Unique_Stocks']} | {row['Avg_Daily']} |")
        
        report.append("\n---\n")
        
        # 三、主线板块识别
        report.append("## 三、市场主线板块识别\n")
        report.append("基于20日涨停数据分析，识别出以下**5大主线板块**：\n")
        
        for idx, (_, row) in enumerate(self.mainline.iterrows(), 1):
            report.append(f"### {idx}. 【{row['L3_Industry']}】板块\n")
            report.append(f"**板块层级**: {row['L1_Industry']} > {row['L2_Industry']} > {row['L3_Industry']}")
            report.append(f"**20日涨停次数**: {row['Total_Limit_Up']} 次")
            report.append(f"**涉及股票数**: {row['Unique_Stocks']} 只")
            report.append(f"**日均涨停**: {row['Avg_Daily']} 次/天")
            report.append(f"**代表标的**: {row['Stock_List'][:80]}...")
            
            # 分析持续性
            daily_data = self.detail[self.detail['L3'] == row['L3_Industry']]
            daily_counts = daily_data.groupby('date').size()
            consistency = daily_counts.std() / daily_counts.mean() if daily_counts.mean() > 0 else 0
            
            if consistency < 0.5:
                persistence = "持续性强"
            elif consistency < 1.0:
                persistence = "持续性中等"
            else:
                persistence = "持续性较弱"
            
            report.append(f"**持续性分析**: {persistence} (变异系数: {consistency:.2f})")
            report.append("")
        
        report.append("\n---\n")
        
        # 四、重点关注标的
        report.append("## 四、重点关注标的筛选\n")
        report.append("基于以下筛选标准，从主线板块中精选出重点关注标的：\n")
        report.append("### 筛选标准")
        report.append("1. **活跃度**: 20日内涨停次数 ≥ 2次")
        report.append("2. **连板能力**: 最高连板数 ≥ 2板")
        report.append("3. **涨幅表现**: 平均涨幅处于强势区间")
        report.append("4. **所属板块**: 属于主线板块\n")
        
        report.append(f"**筛选结果**: 共 {len(self.focus)} 只标的符合条件\n")
        
        report.append("### TOP10 重点关注标的\n")
        report.append("| 排名 | 标的名称 | 所属板块 | 涨停次数 | 最高连板 | 平均涨幅 | 综合评分 |")
        report.append("|------|----------|----------|----------|----------|----------|----------|")
        
        top10 = self.focus.head(10)
        for idx, (_, row) in enumerate(top10.iterrows(), 1):
            report.append(f"| {idx} | {row['Stock_Name']} | {row['L3']} | {row['Appear_Count']} | {row['Max_Board']}板 | {row['Avg_Change']}% | {row['Score']:.2f} |")
        
        report.append("\n---\n")
        
        # 五、投资策略建议
        report.append("## 五、投资策略建议\n")
        
        # 按板块分组
        report.append("### 按板块分类投资建议\n")
        
        for l3 in self.mainline['L3_Industry'].head(3):
            sector_stocks = self.focus[self.focus['L3'] == l3]
            if not sector_stocks.empty:
                report.append(f"**{l3}板块**:")
                for _, stock in sector_stocks.head(3).iterrows():
                    report.append(f"  - {stock['Stock_Name']}: 20日涨停{stock['Appear_Count']}次，最高{stock['Max_Board']}连板")
                report.append("")
        
        report.append("### 风险提示")
        report.append("1. **市场风险**: 当前市场情绪偏冷，涨停家数较少，需控制仓位")
        report.append("2. **板块轮动**: 关注板块轮动节奏，避免追高")
        report.append("3. **个股风险**: 重点关注标的需结合技术面和资金面综合判断")
        report.append("4. **止损纪律**: 建议设置5-8%的止损位，严格执行\n")
        
        report.append("---\n")
        
        # 六、数据文件清单
        report.append("## 六、数据文件清单\n")
        report.append("本次分析生成的数据文件：")
        report.append("1. `industry_limit_up_stats_20d.xlsx` - 行业涨停统计")
        report.append("2. `limit_up_detail_20d.xlsx` - 涨停详细数据")
        report.append("3. `mainline_sectors.xlsx` - 主线板块识别结果")
        report.append("4. `focus_stocks.xlsx` - 重点关注标的")
        report.append("\n---\n")
        
        # 报告结尾
        report.append("## 免责声明\n")
        report.append("本报告仅供参考，不构成投资建议。")
        report.append("投资有风险，入市需谨慎。")
        report.append("\n**报告生成时间**: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 保存报告
        with open(self.report_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report))
        
        print(f"\n✓ 分析报告已生成: {self.report_file}")
        return '\n'.join(report)

def main():
    generator = FinalReportGenerator()
    report = generator.generate_report()
    
    print("\n" + "="*80)
    print("报告预览（前2000字符）：")
    print("="*80)
    print(report[:2000])
    print("\n...")

if __name__ == "__main__":
    main()
