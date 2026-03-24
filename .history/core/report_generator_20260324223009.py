"""
报告生成器 - 自动化Excel报表与可视化
使用XlsxWriter生成专业格式报表
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional
import xlsxwriter
import loguru
from datetime import datetime

# 可选的图表功能（需要mplfinance）
try:
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import io
    import base64
    MPLFINANCE_AVAILABLE = True
except ImportError:
    MPLFINANCE_AVAILABLE = False

logger = loguru.logger


class ReportGenerator:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.date_str = datetime.now().strftime("%Y%m%d")

    def create_daily_report(self, data_dict: Dict, file_name: str = None) -> Path:
        """
        生成每日分析报告
        data_dict应包含:
        - mainline_df: 主线强度DataFrame
        - gradient: 梯度追踪结果
        - sentiment: 情绪指标
        - patterns: 模式识别结果
        - hierarchy_df: 层级化数据
        """
        if file_name is None:
            file_name = f"A股情绪分析报告_{self.date_str}.xlsx"

        file_path = self.output_dir / file_name

        with pd.ExcelWriter(file_path, engine='xlsxwriter') as writer:
            workbook = writer.book

            # 定义格式
            header_format = workbook.add_format({
                'bold': True, 'bg_color': '#4472C4', 'font_color': 'white',
                'border': 1, 'align': 'center', 'valign': 'vcenter'
            })
            cell_format = workbook.add_format({'border': 1, 'align': 'center'})
            highlight_format = workbook.add_format({
                'bg_color': '#FFC7CE', 'font_color': '#9C0006', 'border': 1
            })
            strong_format = workbook.add_format({
                'bg_color': '#C6EFCE', 'font_color': '#006100', 'bold': True, 'border': 1
            })
            new_flag_format = workbook.add_format({
                'bg_color': '#FFD966', 'font_color': '#7F6000', 'bold': True, 'border': 1
            })

            # Sheet 1: Dashboard
            self._write_dashboard(writer, data_dict.get('sentiment', {}),
                                  data_dict.get('mainline_df', pd.DataFrame()),
                                  data_dict.get('gradient', {}), header_format, cell_format)

            # Sheet 2: 主线板块详情
            if not data_dict.get('mainline_df', pd.DataFrame()).empty:
                mainline_df = data_dict['mainline_df'].copy()
                # 处理Is_New标记，显示正确的行业信息
                if 'Is_New' in mainline_df.columns:
                    # 对于新板块，使用L2_Industry作为显示的行业名称
                    mainline_df['行业名称'] = mainline_df.apply(
                        lambda row: row['L2_Industry'] if row.get('Is_New', False) else row['L3_Industry'],
                        axis=1
                    )
                
                # 添加板块代码列（从hierarchy_df中获取该板块下的股票代码列表）
                hierarchy_df = data_dict.get('hierarchy_df', pd.DataFrame())
                if not hierarchy_df.empty:
                    # 为每个L3行业获取股票代码列表
                    def get_sector_codes(row):
                        l3 = row['L3_Industry']
                        codes = hierarchy_df[hierarchy_df['L3_Industry'] == l3]['Code'].tolist()
                        return ','.join(codes[:5]) if codes else ''  # 最多显示5个代码
                    
                    mainline_df['板块代码'] = mainline_df.apply(get_sector_codes, axis=1)
                
                # 调整列顺序，将板块代码放在前面
                cols = mainline_df.columns.tolist()
                if '板块代码' in cols:
                    # 将板块代码移到L3_Industry后面
                    cols.remove('板块代码')
                    l3_idx = cols.index('L3_Industry') if 'L3_Industry' in cols else 2
                    cols.insert(l3_idx + 1, '板块代码')
                    mainline_df = mainline_df[cols]
                
                mainline_df.to_excel(writer, sheet_name='主线板块Top5', index=False)
                worksheet = writer.sheets['主线板块Top5']
                worksheet.set_column('A:G', 15)

            # Sheet 3: 梯队追踪
            self._write_gradient_sheet(writer, data_dict.get('gradient', {}), header_format, cell_format)

            # Sheet 4: 模式信号
            self._write_patterns_sheet(writer, data_dict.get('patterns', {}), header_format, cell_format,
                                       strong_format)

            # Sheet 5: 核心标的
            self._write_core_stocks(writer, data_dict.get('hierarchy_df', pd.DataFrame()),
                                    header_format, cell_format, highlight_format, new_flag_format)

        logger.info(f"报告已生成: {file_path}")
        return file_path

    def _write_dashboard(self, writer, sentiment: Dict, mainline_df: pd.DataFrame,
                         gradient: Dict, header_fmt, cell_fmt):
        """写入Dashboard工作表"""
        df_dashboard = pd.DataFrame({
            '指标': ['报告日期', '市场情绪', '涨停家数', '炸板数', '炸板率(%)',
                   '昨日涨停溢价(%)', '最高板高度', '最高板标的'],
            '数值': [
                self.date_str,
                sentiment.get('temperature', '未知'),
                sentiment.get('total_limit_up', 0),
                sentiment.get('broken_boards', 0),
                sentiment.get('broken_board_rate', 0),
                sentiment.get('prev_limit_up_premium', 'N/A'),
                gradient.get('highest_board', 0),
                gradient.get('highest_stock', '')
            ]
        })

        df_dashboard.to_excel(writer, sheet_name='Dashboard', index=False)
        worksheet = writer.sheets['Dashboard']
        worksheet.set_column('A:B', 20)

        # 添加Top5板块（包含一级、二级、三级行业）
        if not mainline_df.empty:
            start_row = len(df_dashboard) + 2
            worksheet.write(start_row, 0, '今日主线Top5', header_fmt)

            # 过滤掉"其他"行业
            filtered_df = mainline_df[mainline_df['L3_Industry'] != '其他'].copy()

            # 处理Is_New标记，显示正确的行业信息
            if 'Is_New' in filtered_df.columns:
                display_df = pd.DataFrame({
                    '一级行业': filtered_df['L1_Industry'],
                    '二级行业': filtered_df.apply(
                        lambda row: row['L2_Industry'] if row.get('Is_New', False) else row['L2_Industry'],
                        axis=1
                    ),
                    '三级行业': filtered_df.apply(
                        lambda row: row['L2_Industry'] if row.get('Is_New', False) else row['L3_Industry'],
                        axis=1
                    ),
                    '涨停数': filtered_df['LimitUp_Count'],
                    '最高连板': filtered_df['Max_BoardHeight'],
                    '强度评分': filtered_df['Strength_Score']
                })
            else:
                display_cols = ['L1_Industry', 'L2_Industry', 'L3_Industry', 'LimitUp_Count', 'Max_BoardHeight',
                                'Strength_Score']
                display_df = filtered_df[display_cols] if all(col in filtered_df.columns for col in display_cols) else filtered_df

            top5 = display_df.head(5)
            top5.to_excel(writer, sheet_name='Dashboard', startrow=start_row, index=False)

    def _write_gradient_sheet(self, writer, gradient: Dict, header_fmt, cell_fmt):
        """写入梯队追踪工作表 - 展示详细的涨停梯队数据（按连板数倒序）"""
        # 按梯队分类展示详细数据
        rows = []
        # 按6B+ -> 1B的顺序遍历（倒序）
        for board_type in ['6B+', '5B', '4B', '3B', '2B', '1B']:
            stocks = gradient.get(board_type, [])
            for stock_info in stocks:
                rows.append({
                    '梯队': board_type,
                    '股票名称': stock_info.get('name', ''),
                    '连板数': stock_info.get('board_height', 1),
                    '三级行业': stock_info.get('l3_industry', ''),
                    '涨幅%': round(stock_info.get('change_pct', 0), 2)
                })

        df_gradient = pd.DataFrame(rows)
        if not df_gradient.empty:
            df_gradient.to_excel(writer, sheet_name='涨停梯队', index=False)
            worksheet = writer.sheets['涨停梯队']
            worksheet.set_column('A:E', 15)

        # 写入联动信息
        linkage = gradient.get('industry_linkage', {})
        if linkage:
            worksheet = writer.sheets['涨停梯队']
            start_row = len(df_gradient) + 3 if not df_gradient.empty else 0
            worksheet.write(start_row, 0, '板块联动', header_fmt)
            worksheet.write(start_row + 1, 0, f"龙头: {linkage.get('leader', '')}")
            worksheet.write(start_row + 2, 0, f"板块: {linkage.get('industry', '')}")
            worksheet.write(start_row + 3, 0, f"跟风数: {linkage.get('followers_count', 0)}")
            worksheet.write(start_row + 4, 0, f"强联动: {'是' if linkage.get('is_strong_linkage') else '否'}")

    def _write_patterns_sheet(self, writer, patterns: Dict, header_fmt, cell_fmt, strong_fmt):
        """写入模式识别工作表"""
        all_signals = []
        for pattern_type, signals in patterns.items():
            for signal in signals:
                all_signals.append({
                    '模式': signal.pattern_type,
                    '代码': signal.stock_code,
                    '名称': signal.stock_name,
                    '置信度': signal.confidence,
                    '描述': signal.description,
                    '关键指标': str(signal.key_metrics)
                })

        if all_signals:
            df_patterns = pd.DataFrame(all_signals)
            df_patterns.to_excel(writer, sheet_name='模式信号', index=False)
            worksheet = writer.sheets['模式信号']
            worksheet.set_column('A:F', 20)

            # 高亮高置信度信号
            for row_num in range(1, len(df_patterns) + 1):
                if df_patterns.iloc[row_num - 1]['置信度'] >= 0.8:
                    worksheet.write(row_num, 3, df_patterns.iloc[row_num - 1]['置信度'], strong_fmt)

    def _write_core_stocks(self, writer, hierarchy_df: pd.DataFrame,
                           header_fmt, cell_fmt, highlight_fmt, new_flag_fmt):
        """写入核心标的（筛选10点半前封板的标的，标记炸板）"""
        if hierarchy_df.empty:
            return

        # 筛选10点半前封板的标的
        rows = []

        for _, row in hierarchy_df.iterrows():
            # 检查涨停时间
            limit_up_time = str(row.get('LimitUpTime', ''))
            # 转换时间格式
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
                if l3 == '其他' or l2 == '其他' or l1 == '其他':
                    continue

                open_times = row.get('OpenTimes', 0)

                rows.append({
                    '一级行业': l1,
                    '二级行业': l2,
                    '三级行业': l3,
                    '代码': row['Code'],
                    '名称': row['Name'],
                    '连板数': row.get('BoardHeight', 1),
                    '涨幅%': row['ChangePct'],
                    '涨停时间': limit_up_time,
                    '炸板次数': open_times,
                    '是否炸板': '是' if open_times > 0 else '否',
                    '概念': row.get('Concept', '')
                })

        df_core = pd.DataFrame(rows)

        if df_core.empty:
            # 如果没有10点半前封板的，显示所有涨停股
            for _, row in hierarchy_df.iterrows():
                l1 = row.get('L1_Industry', '')
                l2 = row.get('L2_Industry', '')
                l3 = row.get('L3_Industry', '')

                if l3 == '其他' or l2 == '其他' or l1 == '其他':
                    continue

                limit_up_time = str(row.get('LimitUpTime', ''))
                if limit_up_time.isdigit():
                    limit_up_time = limit_up_time.zfill(6)
                if len(limit_up_time) == 6:
                    limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"

                open_times = row.get('OpenTimes', 0)

                rows.append({
                    '一级行业': l1,
                    '二级行业': l2,
                    '三级行业': l3,
                    '代码': row['Code'],
                    '名称': row['Name'],
                    '连板数': row.get('BoardHeight', 1),
                    '涨幅%': row['ChangePct'],
                    '涨停时间': limit_up_time,
                    '炸板次数': open_times,
                    '是否炸板': '是' if open_times > 0 else '否',
                    '概念': row.get('Concept', '')
                })
            df_core = pd.DataFrame(rows)

        if not df_core.empty:
            # 按一级行业、二级行业、三级行业排序，连板数倒序
            df_core = df_core.sort_values(['一级行业', '二级行业', '三级行业', '连板数'],
                                          ascending=[True, True, True, False])

            df_core.to_excel(writer, sheet_name='核心标的', index=False)
            worksheet = writer.sheets['核心标的']
            worksheet.set_column('A:K', 15)

            # 炸板次数>0的行高亮
            for row_num in range(1, len(df_core) + 1):
                if df_core.iloc[row_num - 1]['炸板次数'] > 0:
                    # 高亮炸板次数列
                    worksheet.write(row_num, 8, df_core.iloc[row_num - 1]['炸板次数'], highlight_fmt)
                    # 高亮是否炸板列
                    worksheet.write(row_num, 9, df_core.iloc[row_num - 1]['是否炸板'], highlight_fmt)

    def generate_chart_image(self, stock_code: str, hist_data: pd.DataFrame) -> Optional[bytes]:
        """
        生成5日K线缩略图（需要mplfinance）
        """
        if not MPLFINANCE_AVAILABLE or hist_data.empty or len(hist_data) < 5:
            return None

        try:
            # 准备数据
            hist_data = hist_data.tail(5).copy()
            hist_data.index = pd.to_datetime(hist_data['trade_date'])
            hist_data = hist_data.rename(columns={
                'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'vol': 'Volume'
            })

            # 创建图表
            fig, axes = mpf.plot(hist_data, type='candle', style='yahoo',
                                 figsize=(3, 2), returnfig=True,
                                 volume=False, ylabel='')

            # 转换为图片bytes
            buf = io.BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight', dpi=50)
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()
        except Exception as e:
            logger.error(f"生成图表失败 {stock_code}: {e}")
            return None


if __name__ == "__main__":
    from config.settings import OUTPUT_DIR

    rg = ReportGenerator(OUTPUT_DIR)
    print("报告生成器初始化成功")
