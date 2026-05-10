"""
报告生成器V2 - 贴合4大核心策略的Excel报表
首板突破、二板定龙、弱转强、龙头二波
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional
import xlsxwriter
import loguru
from datetime import datetime

logger = loguru.logger


class ReportGeneratorV2:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.date_str = datetime.now().strftime("%Y%m%d")

    def create_daily_report(self, data_dict: Dict, file_name: str = None) -> Path:
        """
        生成每日分析报告V2
        
        data_dict应包含:
        - date: 分析日期
        - emotion_result: 情绪周期结果
        - mainline_df: 热点概念DataFrame
        - patterns: 模式识别结果 (首板突破、二板定龙、弱转强、龙头二波)
        - hierarchy_df: 涨停梯队数据
        - zt_pool: 涨停池数据
        """
        date = data_dict.get('date', self.date_str)
        if file_name is None:
            file_name = f"短线情绪分析报告_{date}.xlsx"

        file_path = self.output_dir / file_name

        with pd.ExcelWriter(file_path, engine='xlsxwriter') as writer:
            workbook = writer.book
            
            # 定义格式
            formats = self._create_formats(workbook)
            
            # Sheet 1: 市场概览
            self._write_dashboard(writer, data_dict, formats)
            
            # Sheet 2: 热点概念
            self._write_hot_sectors(writer, data_dict, formats)
            
            # Sheet 3: 首板突破
            self._write_first_board(writer, data_dict, formats)
            
            # Sheet 4: 二板定龙
            self._write_second_board(writer, data_dict, formats)
            
            # Sheet 5: 弱转强
            self._write_weak_to_strong(writer, data_dict, formats)
            
            # Sheet 6: 龙头二波
            self._write_dragon_second_wave(writer, data_dict, formats)
            
            # Sheet 7: 涨停梯队
            self._write_limit_up_hierarchy(writer, data_dict, formats)

            # Sheet 8: 概念连板梯队
            self._write_concept_hierarchy(writer, data_dict, formats)

            # Sheet 9: 龙头池
            self._write_dragon_pool(writer, data_dict, formats)

            # Sheet 10: 走弱池
            self._write_weakening_pool(writer, data_dict, formats)

        logger.info(f"报告已生成: {file_path}")
        return file_path

    def _create_formats(self, workbook):
        """创建Excel格式"""
        return {
            'header': workbook.add_format({
                'bold': True, 'bg_color': '#4472C4', 'font_color': 'white',
                'border': 1, 'align': 'center', 'valign': 'vcenter'
            }),
            'header_green': workbook.add_format({
                'bold': True, 'bg_color': '#70AD47', 'font_color': 'white',
                'border': 1, 'align': 'center', 'valign': 'vcenter'
            }),
            'header_orange': workbook.add_format({
                'bold': True, 'bg_color': '#ED7D31', 'font_color': 'white',
                'border': 1, 'align': 'center', 'valign': 'vcenter'
            }),
            'header_purple': workbook.add_format({
                'bold': True, 'bg_color': '#7030A0', 'font_color': 'white',
                'border': 1, 'align': 'center', 'valign': 'vcenter'
            }),
            'cell': workbook.add_format({'border': 1, 'align': 'center', 'valign': 'vcenter'}),
            'cell_left': workbook.add_format({'border': 1, 'align': 'left', 'valign': 'vcenter'}),
            'highlight_red': workbook.add_format({
                'bg_color': '#FFC7CE', 'font_color': '#9C0006', 'border': 1, 'align': 'center'
            }),
            'highlight_green': workbook.add_format({
                'bg_color': '#C6EFCE', 'font_color': '#006100', 'border': 1, 'align': 'center'
            }),
            'highlight_yellow': workbook.add_format({
                'bg_color': '#FFEB9C', 'font_color': '#9C5700', 'border': 1, 'align': 'center'
            }),
            'number': workbook.add_format({'border': 1, 'align': 'center', 'num_format': '0.00'}),
            'percent': workbook.add_format({'border': 1, 'align': 'center', 'num_format': '0.00%'}),
        }

    def _write_dashboard(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 1: 市场概览"""
        emotion = data_dict.get('emotion_result', {})
        date = data_dict.get('date', self.date_str)
        
        # 辅助函数：安全获取字符串值
        def safe_str(value, default='未知'):
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return value
            # 如果是对象，尝试获取name或value属性
            if hasattr(value, 'name'):
                return str(value.name)
            if hasattr(value, 'value'):
                return str(value.value)
            return str(value)
        
        # 从metrics中获取数值指标
        metrics = emotion.get('metrics', {})
        
        # 从strategy对象中获取建议
        strategy_obj = emotion.get('strategy', {})
        position = safe_str(strategy_obj.get('position') if isinstance(strategy_obj, dict) else getattr(strategy_obj, 'position', None))
        strategy_desc = safe_str(strategy_obj.get('strategy') if isinstance(strategy_obj, dict) else getattr(strategy_obj, 'strategy', None))
        
        # 基础数据
        dashboard_data = [
            ['报告日期', date, '', '情绪周期', safe_str(emotion.get('cycle_name')), ''],
            ['', '', '', '', '', ''],
            ['市场情绪指标', '数值', '说明', '策略建议', '', ''],
            ['涨停家数', metrics.get('limit_up_count', 0), '当日涨停股票数量', '', '', ''],
            ['跌停家数', metrics.get('nuclear_button_count', 0), '当日跌停股票数量', '', '', ''],
            ['炸板率', f"{metrics.get('broken_rate', 0):.1f}%", '炸板数/曾涨停数', '', '', ''],
            ['昨日涨停溢价', f"{metrics.get('prev_limit_up_premium', 0):.1f}%", '昨日涨停股今日平均收益', '', '', ''],
            ['最高连板高度', metrics.get('max_board_height', 0), '市场最高连板数', '', '', ''],
            ['', '', '', '', '', ''],
            ['策略建议', '', '', '', '', ''],
            ['当前周期', safe_str(emotion.get('cycle_name')), '', '', '', ''],
            ['建议仓位', position, '', '', '', ''],
            ['操作策略', strategy_desc, '', '', '', ''],
        ]
        
        df = pd.DataFrame(dashboard_data)
        df.to_excel(writer, sheet_name='市场概览', index=False, header=False)
        
        worksheet = writer.sheets['市场概览']
        worksheet.set_column('A:A', 15)
        worksheet.set_column('B:B', 15)
        worksheet.set_column('C:C', 30)
        worksheet.set_column('D:D', 15)
        worksheet.set_column('E:E', 20)
        
        # 应用格式
        for row in range(len(dashboard_data)):
            for col in range(6):
                if row == 0 or row == 2 or row == 9:
                    worksheet.write(row, col, dashboard_data[row][col], formats['header'])
                else:
                    worksheet.write(row, col, dashboard_data[row][col], formats['cell'])

    def _write_hot_sectors(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 2: 热点概念 - 核心数据展示"""
        mainline_df = data_dict.get('mainline_df', pd.DataFrame())
        
        if mainline_df.empty:
            df = pd.DataFrame({'提示': ['暂无热点概念数据']})
            df.to_excel(writer, sheet_name='热点概念', index=False)
            return
        
        # 核心概念数据字段映射（按重要程度排序）
        # 主线特征指标优先，帮助识别真正的主线概念而非一日游
        core_concept_cols = {
            # 基础信息
            'ts_code': '板块代码',
            '板块名称': '概念名称',
            '当前排名': '当日排名',
            # 主线特征指标（核心）
            '10日进前10次数': '10日进前10',
            '10日进前5次数': '10日进前5',
            '10日进前3次数': '10日进前3',
            '连续进前10天数': '连续前10天',
            '是否主线': '是否主线',
            '10日平均排名': '10日均排名',
            '10日最佳排名': '10日最佳',
            # 涨停数据
            '涨停家数': '涨停数量',
            '最高连板': '最高连板',
            # 评分与阶段
            '综合评分': '综合评分',
            '共振得分': '共振得分',
            '持续性评分': '持续性评分',
            '所处阶段': '所处阶段',
            '市场周期': '市场周期',
            # 趋势因子
            '排名动量': '排名动量',
            '涨停趋势': '涨停趋势',
            # 资金因子
            '成交额变化': '成交额变化',
            '换手率': '换手率',
            # 信号与策略
            '信号类型': '信号类型',
            '信号强度': '信号强度',
            '操作建议': '操作建议',
            '建议仓位': '建议仓位',
            '紧急度': '紧急度',
            '策略理由': '策略理由',
        }
        
        # 检查哪些列存在，并按核心字段顺序构建DataFrame
        available_cols = {}
        for k, v in core_concept_cols.items():
            if k in mainline_df.columns:
                available_cols[k] = v
        
        if available_cols:
            # 按核心字段顺序选择列
            df_display = mainline_df[list(available_cols.keys())].copy()
            df_display.columns = list(available_cols.values())
        else:
            # 如果没有匹配的列，显示所有列（将板块名称改为概念名称）
            df_display = mainline_df.copy()
            if '板块名称' in df_display.columns:
                df_display = df_display.rename(columns={'板块名称': '概念名称'})
        
        # 按综合评分降序排序
        if '综合评分' in df_display.columns:
            df_display = df_display.sort_values('综合评分', ascending=False)
        elif '共振得分' in df_display.columns:
            df_display = df_display.sort_values('共振得分', ascending=False)
        
        df_display.to_excel(writer, sheet_name='热点概念', index=False)
        
        worksheet = writer.sheets['热点概念']
        # 设置列宽
        worksheet.set_column('A:A', 12)  # 板块代码
        worksheet.set_column('B:B', 16)  # 概念名称
        worksheet.set_column('C:C', 10)  # 当日排名
        # 主线特征指标（核心列，稍宽以突出显示）
        worksheet.set_column('D:D', 12)  # 10日进前10
        worksheet.set_column('E:E', 12)  # 10日进前5
        worksheet.set_column('F:F', 12)  # 10日进前3
        worksheet.set_column('G:G', 12)  # 连续前10天
        worksheet.set_column('H:H', 10)  # 是否主线
        worksheet.set_column('I:J', 11)  # 10日均排名、10日最佳
        # 其他数据
        worksheet.set_column('K:L', 10)  # 涨停数量、最高连板
        worksheet.set_column('M:O', 11)  # 综合评分、共振得分、持续性评分
        worksheet.set_column('P:Q', 10)  # 所处阶段、市场周期
        worksheet.set_column('R:S', 10)  # 排名动量、涨停趋势
        worksheet.set_column('T:U', 11)  # 成交额变化、换手率
        worksheet.set_column('V:W', 10)  # 信号类型、信号强度
        worksheet.set_column('X:X', 14)  # 操作建议
        worksheet.set_column('Y:Y', 10)  # 建议仓位
        worksheet.set_column('Z:Z', 8)   # 紧急度
        worksheet.set_column('AA:AA', 30)  # 策略理由
        
        # 应用表头格式
        for col_num in range(len(df_display.columns)):
            worksheet.write(0, col_num, df_display.columns[col_num], formats['header_green'])

    def _write_first_board(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 3: 首板突破"""
        patterns = data_dict.get('patterns', {})
        signals = patterns.get('首板突破', [])
        
        if not signals:
            df = pd.DataFrame({'提示': ['暂无首板突破信号']})
            df.to_excel(writer, sheet_name='首板突破', index=False)
            return
        
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': getattr(signal, 'confidence', 0),
                '涨停时间': key_metrics.get('涨停时间', ''),
                '封单强度': key_metrics.get('封单强度', ''),
                '量比': key_metrics.get('量比', ''),
                '距前高': key_metrics.get('距前高', ''),
                '量能说明': key_metrics.get('量能说明', ''),
                '买点': getattr(signal, 'entry_price', 0),
                '止损': getattr(signal, 'stop_loss', 0),
                '止盈': getattr(signal, 'take_profit', 0),
                '仓位': getattr(signal, 'position_size', ''),
                '描述': getattr(signal, 'description', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='首板突破', index=False)
        
        worksheet = writer.sheets['首板突破']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 15)
        worksheet.set_column('D:D', 10)
        worksheet.set_column('E:H', 12)
        worksheet.set_column('I:I', 30)
        worksheet.set_column('J:L', 10)
        worksheet.set_column('M:M', 10)
        worksheet.set_column('N:N', 40)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_orange'])

    def _write_second_board(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 4: 二板定龙"""
        patterns = data_dict.get('patterns', {})
        signals = patterns.get('二板定龙', [])
        
        if not signals:
            df = pd.DataFrame({'提示': ['暂无二板定龙信号']})
            df.to_excel(writer, sheet_name='二板定龙', index=False)
            return
        
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': getattr(signal, 'confidence', 0),
                '首板质量': key_metrics.get('首板质量', ''),
                '次日高开': key_metrics.get('次日高开', ''),
                '涨停时间': key_metrics.get('涨停时间', ''),
                '是否快速': key_metrics.get('是否快速', ''),
                '封单强度': key_metrics.get('封单强度', ''),
                '买点': getattr(signal, 'entry_price', 0),
                '止损': getattr(signal, 'stop_loss', 0),
                '止盈': getattr(signal, 'take_profit', 0),
                '仓位': getattr(signal, 'position_size', ''),
                '描述': getattr(signal, 'description', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='二板定龙', index=False)
        
        worksheet = writer.sheets['二板定龙']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 15)
        worksheet.set_column('D:D', 10)
        worksheet.set_column('E:J', 12)
        worksheet.set_column('K:M', 10)
        worksheet.set_column('N:N', 40)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_purple'])

    def _write_weak_to_strong(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 5: 弱转强"""
        patterns = data_dict.get('patterns', {})
        signals = patterns.get('弱转强', [])
        
        if not signals:
            df = pd.DataFrame({'提示': ['暂无弱转强信号']})
            df.to_excel(writer, sheet_name='弱转强', index=False)
            return
        
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '连板高度': key_metrics.get('连板高度', ''),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': getattr(signal, 'confidence', 0),
                '昨日弱类型': key_metrics.get('昨日弱类型', ''),
                '昨日质量分': key_metrics.get('昨日烂板质量', ''),
                '次日高开': key_metrics.get('次日高开', ''),
                '竞价量比': key_metrics.get('竞价量比', ''),
                '涨停时间': key_metrics.get('涨停时间', ''),
                '买点': getattr(signal, 'entry_price', 0),
                '止损': getattr(signal, 'stop_loss', 0),
                '止盈': getattr(signal, 'take_profit', 0),
                '仓位': getattr(signal, 'position_size', ''),
                '描述': getattr(signal, 'description', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='弱转强', index=False)
        
        worksheet = writer.sheets['弱转强']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 10)
        worksheet.set_column('D:D', 15)
        worksheet.set_column('E:E', 10)
        worksheet.set_column('F:K', 12)
        worksheet.set_column('L:N', 10)
        worksheet.set_column('O:O', 40)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header'])

    def _write_dragon_second_wave(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 6: 龙二波"""
        patterns = data_dict.get('patterns', {})
        # 注意：pattern_recognition.py中使用的是"龙二波"作为键
        signals = patterns.get('龙二波', [])
        
        if not signals:
            df = pd.DataFrame({'提示': ['暂无龙二波信号']})
            df.to_excel(writer, sheet_name='龙二波', index=False)
            return
        
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': getattr(signal, 'confidence', 0),
                '第一波强度': key_metrics.get('第一波强度', ''),
                '第一波类型': key_metrics.get('第一波类型', ''),
                '调整天数': key_metrics.get('调整天数', ''),
                '5日涨幅': key_metrics.get('5日涨幅', ''),
                '10日涨幅': key_metrics.get('10日涨幅', ''),
                '买点': getattr(signal, 'entry_price', 0),
                '止损': getattr(signal, 'stop_loss', 0),
                '止盈': getattr(signal, 'take_profit', 0),
                '仓位': getattr(signal, 'position_size', ''),
                '描述': getattr(signal, 'description', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='龙二波', index=False)
        
        worksheet = writer.sheets['龙二波']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 15)
        worksheet.set_column('D:D', 10)
        worksheet.set_column('E:J', 12)
        worksheet.set_column('K:M', 10)
        worksheet.set_column('N:N', 40)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_green'])

    def _write_limit_up_hierarchy(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 7: 涨停梯队"""
        hierarchy_df = data_dict.get('hierarchy_df', pd.DataFrame())
        
        if hierarchy_df.empty:
            df = pd.DataFrame({'提示': ['暂无涨停梯队数据']})
            df.to_excel(writer, sheet_name='涨停梯队', index=False)
            return
        
        # 选择关键列
        display_cols = {
            'Code': '股票代码',
            'Name': '股票名称',
            'L1_Industry': '一级行业',
            'L2_Industry': '二级行业',
            'BoardHeight': '连板数',
            'ChangePct': '涨幅%',
            'LimitUpTime': '首次涨停时间',
            'OpenTimes': '炸板次数',
        }
        
        available_cols = {k: v for k, v in display_cols.items() if k in hierarchy_df.columns}
        if available_cols:
            df_display = hierarchy_df[list(available_cols.keys())].copy()
            df_display.columns = list(available_cols.values())
        else:
            df_display = hierarchy_df.copy()
        
        # 按连板数降序排序
        if '连板数' in df_display.columns:
            df_display = df_display.sort_values('连板数', ascending=False)
        
        df_display.to_excel(writer, sheet_name='涨停梯队', index=False)
        
        worksheet = writer.sheets['涨停梯队']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:D', 15)
        worksheet.set_column('E:H', 12)
        
        # 应用表头格式
        for col_num in range(len(df_display.columns)):
            worksheet.write(0, col_num, df_display.columns[col_num], formats['header'])

    def _write_concept_hierarchy(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 8: 概念连板梯队 - 各概念板块的涨停梯队分布"""
        concept_hierarchy = data_dict.get('concept_hierarchy', {})
        concept_hierarchy_report = data_dict.get('concept_hierarchy_report', '')

        if not concept_hierarchy:
            df = pd.DataFrame({'提示': ['暂无概念连板梯队数据']})
            df.to_excel(writer, sheet_name='概念连板梯队', index=False)
            return

        # 按涨停家数排序
        sorted_concepts = sorted(
            concept_hierarchy.items(),
            key=lambda x: x[1].total_limit_up,
            reverse=True
        )

        rows = []
        for concept_name, h in sorted_concepts:
            # 构建梯队分布字符串
            board_parts = []
            for board in sorted(h.board_distribution.keys(), reverse=True):
                count = h.board_distribution[board]
                board_parts.append(f"{board}板{count}家")
            board_str = ", ".join(board_parts)

            rows.append({
                '概念名称': concept_name,
                '涨停总数': h.total_limit_up,
                '最高连板': h.max_board_count,
                '梯队分布': board_str,
                '龙头股': f"{h.leader_stock['name']}({h.leader_stock['code']})" if h.leader_stock else '-',
                '板块代码': h.ts_code,
            })

        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='概念连板梯队', index=False)

        worksheet = writer.sheets['概念连板梯队']
        worksheet.set_column('A:A', 20)  # 概念名称
        worksheet.set_column('B:B', 12)  # 涨停总数
        worksheet.set_column('C:C', 12)  # 最高连板
        worksheet.set_column('D:D', 40)  # 梯队分布
        worksheet.set_column('E:E', 25)  # 龙头股
        worksheet.set_column('F:F', 15)  # 板块代码

        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_green'])

        # 高亮涨停总数较多的行
        for row_num in range(1, len(df) + 1):
            limit_up_count = df.iloc[row_num - 1]['涨停总数']
            if limit_up_count >= 20:
                for col_num in range(len(df.columns)):
                    worksheet.write(row_num, col_num, df.iloc[row_num - 1, col_num], formats['highlight_green'])

    def _write_dragon_pool(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 9: 龙头池 - 正在观察的龙头候选股"""
        dragon_pool = data_dict.get('dragon_pool', [])
        
        if not dragon_pool:
            df = pd.DataFrame({'提示': ['暂无龙头候选股']})
            df.to_excel(writer, sheet_name='龙头池', index=False)
            return
        
        rows = []
        for dragon in dragon_pool:
            rows.append({
                '股票代码': dragon.get('代码', ''),
                '股票名称': dragon.get('名称', ''),
                '龙头类型': dragon.get('龙头类型', ''),
                '最高连板': dragon.get('最高连板', 0),
                '最高点': dragon.get('最高点', ''),
                '见顶日期': dragon.get('见顶日期', ''),
                '入池日期': dragon.get('入池日期', ''),
                '所属板块': dragon.get('所属板块', ''),
                '10日涨幅': dragon.get('10日涨幅', ''),
                '涨停次数': dragon.get('涨停次数', 0),
                '当前状态': dragon.get('当前状态', ''),
                '观察天数': dragon.get('观察天数', 0),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='龙头池', index=False)
        
        worksheet = writer.sheets['龙头池']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 12)
        worksheet.set_column('D:D', 10)
        worksheet.set_column('E:F', 12)
        worksheet.set_column('G:G', 12)
        worksheet.set_column('H:H', 15)
        worksheet.set_column('I:I', 10)
        worksheet.set_column('J:J', 12)
        worksheet.set_column('K:K', 10)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_purple'])

    def _write_weakening_pool(self, writer, data_dict: Dict, formats: Dict):
        """Sheet 10: 走弱池 - 已确认走弱等待转强的龙头"""
        weakening_pool = data_dict.get('weakening_pool', [])
        
        if not weakening_pool:
            df = pd.DataFrame({'提示': ['暂无走弱龙头']})
            df.to_excel(writer, sheet_name='走弱池', index=False)
            return
        
        rows = []
        for weakening in weakening_pool:
            rows.append({
                '股票代码': weakening.get('代码', ''),
                '股票名称': weakening.get('名称', ''),
                '龙头类型': weakening.get('龙头类型', ''),
                '最高连板': weakening.get('最高连板', 0),
                '最高点': weakening.get('最高点', ''),
                '见顶日期': weakening.get('见顶日期', ''),
                '入池日期': weakening.get('入池日期', ''),
                '所属板块': weakening.get('所属板块', ''),
                '10日涨幅': weakening.get('10日涨幅', ''),
                '涨停次数': weakening.get('涨停次数', 0),
                '走弱日期': weakening.get('走弱日期', ''),
                '走弱类型': weakening.get('走弱类型', ''),
                '走弱价格': weakening.get('走弱价格', ''),
                '当前价格': weakening.get('当前价格', ''),
                '回调幅度': weakening.get('回调幅度', ''),
                '观察信号': weakening.get('观察信号', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='走弱池', index=False)
        
        worksheet = writer.sheets['走弱池']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 12)
        worksheet.set_column('D:D', 10)
        worksheet.set_column('E:F', 12)
        worksheet.set_column('G:G', 12)
        worksheet.set_column('H:H', 15)
        worksheet.set_column('I:I', 10)
        worksheet.set_column('J:K', 12)
        worksheet.set_column('L:L', 15)
        worksheet.set_column('M:N', 12)
        worksheet.set_column('O:O', 25)
        
        # 应用表头格式
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_orange'])


if __name__ == "__main__":
    from config.settings import OUTPUT_DIR
    rg = ReportGeneratorV2(OUTPUT_DIR)
    print("报告生成器V2初始化成功")
