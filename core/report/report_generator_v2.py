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
        """Sheet 2: 热点概念 - 包含详细的热点概念、行业和持续性数据"""
        
        # 获取各类数据
        mainline_df = data_dict.get('mainline_df', pd.DataFrame())
        hot_concepts_df = data_dict.get('hot_concepts_df', pd.DataFrame())
        hot_industries_df = data_dict.get('hot_industries_df', pd.DataFrame())
        concept_persistence_df = data_dict.get('concept_persistence_df', pd.DataFrame())
        industry_persistence_df = data_dict.get('industry_persistence_df', pd.DataFrame())
        
        # 创建工作表
        worksheet = writer.book.add_worksheet('热点概念')
        writer.sheets['热点概念'] = worksheet
        
        current_row = 0
        
        # ========== 第一部分：市场主线（共振分析结果）==========
        if not mainline_df.empty:
            # 写入标题
            worksheet.merge_range(current_row, 0, current_row, 10, '一、市场主线（概念-行业共振）', formats['header_green'])
            current_row += 1
            
            # 主线数据字段映射
            mainline_cols = {
                '主线名称': '主线名称',
                '共振度': '共振度(%)',
                '核心概念': '核心概念',
                '核心行业': '核心行业',
                '综合评分': '综合评分',
                '持续性评分': '持续性评分',
                '所处阶段': '所处阶段',
                '操作建议': '操作建议',
                '策略理由': '策略理由',
            }
            
            available_mainline_cols = {k: v for k, v in mainline_cols.items() if k in mainline_df.columns}
            if available_mainline_cols:
                df_mainline = mainline_df[list(available_mainline_cols.keys())].copy()
                df_mainline.columns = list(available_mainline_cols.values())
                
                # 写入表头
                for col_num, col_name in enumerate(df_mainline.columns):
                    worksheet.write(current_row, col_num, col_name, formats['header'])
                current_row += 1
                
                # 写入数据
                for _, row in df_mainline.iterrows():
                    for col_num, value in enumerate(row):
                        worksheet.write(current_row, col_num, value, formats['cell'])
                    current_row += 1
            
            current_row += 1  # 空行
        
        # ========== 第二部分：热点概念明细 ==========
        if not hot_concepts_df.empty:
            worksheet.merge_range(current_row, 0, current_row, 10, '二、热点概念明细（当日）', formats['header_green'])
            current_row += 1
            
            # 热点概念字段映射
            concept_cols = {
                'name': '概念名称',
                'ts_code': '板块代码',
                'pct_change': '涨跌幅(%)',
                'rank': '排名',
                'composite_score': '综合评分',
                'limit_up_count': '涨停家数',
                'amount': '成交额(千元)',
                'is_hot': '是否热点',
            }
            
            available_concept_cols = {k: v for k, v in concept_cols.items() if k in hot_concepts_df.columns}
            if available_concept_cols:
                df_concepts = hot_concepts_df[list(available_concept_cols.keys())].copy()
                df_concepts.columns = list(available_concept_cols.values())
                
                # 按综合评分排序
                if '综合评分' in df_concepts.columns:
                    df_concepts = df_concepts.sort_values('综合评分', ascending=False)
                
                # 写入表头
                for col_num, col_name in enumerate(df_concepts.columns):
                    worksheet.write(current_row, col_num, col_name, formats['header'])
                current_row += 1
                
                # 写入数据
                for _, row in df_concepts.iterrows():
                    for col_num, value in enumerate(row):
                        worksheet.write(current_row, col_num, value, formats['cell'])
                    current_row += 1
            
            current_row += 1  # 空行
        
        # ========== 第三部分：热点行业明细 ==========
        if not hot_industries_df.empty:
            worksheet.merge_range(current_row, 0, current_row, 10, '三、热点行业明细（当日）', formats['header_green'])
            current_row += 1
            
            # 热点行业字段映射
            industry_cols = {
                'name': '行业名称',
                'ts_code': '板块代码',
                'pct_change': '涨跌幅(%)',
                'rank': '排名',
                'composite_score': '综合评分',
                'limit_up_count': '涨停家数',
                'amount': '成交额(千元)',
                'is_hot': '是否热点',
            }
            
            available_industry_cols = {k: v for k, v in industry_cols.items() if k in hot_industries_df.columns}
            if available_industry_cols:
                df_industries = hot_industries_df[list(available_industry_cols.keys())].copy()
                df_industries.columns = list(available_industry_cols.values())
                
                # 按综合评分排序
                if '综合评分' in df_industries.columns:
                    df_industries = df_industries.sort_values('综合评分', ascending=False)
                
                # 写入表头
                for col_num, col_name in enumerate(df_industries.columns):
                    worksheet.write(current_row, col_num, col_name, formats['header'])
                current_row += 1
                
                # 写入数据
                for _, row in df_industries.iterrows():
                    for col_num, value in enumerate(row):
                        worksheet.write(current_row, col_num, value, formats['cell'])
                    current_row += 1
            
            current_row += 1  # 空行
        
        # ========== 第四部分：概念持续性分析 ==========
        if not concept_persistence_df.empty:
            worksheet.merge_range(current_row, 0, current_row, 12, '四、概念板块持续性分析（10日内热点频率）', formats['header_green'])
            current_row += 1
            
            # 持续性分析字段映射
            persistence_cols = {
                '板块名称': '概念名称',
                '热点天数': '热点天数(10日)',
                '热点频率': '热点频率(%)',
                '持续性评分': '持续性评分',
                '所处阶段': '所处阶段',
                '最新排名': '最新排名',
                '排名趋势': '排名趋势',
                '最新涨幅': '最新涨幅(%)',
                '涨停家数': '涨停家数',
                '操作建议': '操作建议',
                '策略理由': '策略理由',
            }
            
            available_persistence_cols = {k: v for k, v in persistence_cols.items() if k in concept_persistence_df.columns}
            if available_persistence_cols:
                df_concept_persist = concept_persistence_df[list(available_persistence_cols.keys())].copy()
                df_concept_persist.columns = list(available_persistence_cols.values())
                
                # 按持续性评分排序
                if '持续性评分' in df_concept_persist.columns:
                    df_concept_persist = df_concept_persist.sort_values('持续性评分', ascending=False)
                
                # 写入表头
                for col_num, col_name in enumerate(df_concept_persist.columns):
                    worksheet.write(current_row, col_num, col_name, formats['header'])
                current_row += 1
                
                # 写入数据
                for _, row in df_concept_persist.iterrows():
                    for col_num, value in enumerate(row):
                        worksheet.write(current_row, col_num, value, formats['cell'])
                    current_row += 1
            
            current_row += 1  # 空行
        
        # ========== 第五部分：行业持续性分析 ==========
        if not industry_persistence_df.empty:
            worksheet.merge_range(current_row, 0, current_row, 12, '五、行业板块持续性分析（10日内热点频率）', formats['header_green'])
            current_row += 1
            
            # 持续性分析字段映射
            industry_persistence_cols = {
                '板块名称': '行业名称',
                '热点天数': '热点天数(10日)',
                '热点频率': '热点频率(%)',
                '持续性评分': '持续性评分',
                '所处阶段': '所处阶段',
                '最新排名': '最新排名',
                '排名趋势': '排名趋势',
                '最新涨幅': '最新涨幅(%)',
                '涨停家数': '涨停家数',
                '操作建议': '操作建议',
                '策略理由': '策略理由',
            }
            
            available_ind_persist_cols = {k: v for k, v in industry_persistence_cols.items() if k in industry_persistence_df.columns}
            if available_ind_persist_cols:
                df_industry_persist = industry_persistence_df[list(available_ind_persist_cols.keys())].copy()
                df_industry_persist.columns = list(available_ind_persist_cols.values())
                
                # 按持续性评分排序
                if '持续性评分' in df_industry_persist.columns:
                    df_industry_persist = df_industry_persist.sort_values('持续性评分', ascending=False)
                
                # 写入表头
                for col_num, col_name in enumerate(df_industry_persist.columns):
                    worksheet.write(current_row, col_num, col_name, formats['header'])
                current_row += 1
                
                # 写入数据
                for _, row in df_industry_persist.iterrows():
                    for col_num, value in enumerate(row):
                        worksheet.write(current_row, col_num, value, formats['cell'])
                    current_row += 1
        
        # 设置列宽
        worksheet.set_column('A:A', 16)  # 名称列
        worksheet.set_column('B:B', 12)  # 代码/排名列
        worksheet.set_column('C:E', 12)  # 数值列
        worksheet.set_column('F:H', 12)  # 评分/阶段列
        worksheet.set_column('I:L', 12)  # 其他数值列
        worksheet.set_column('M:M', 30)  # 策略理由列

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
