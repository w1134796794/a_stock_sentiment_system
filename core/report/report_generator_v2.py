"""
报告生成器V2 - 贴合4大核心策略的Excel报表
首板突破、二板定龙、弱转强、龙头二波
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Any
import xlsxwriter
import loguru
from datetime import datetime

logger = loguru.logger


class ReportGeneratorV2:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.date_str = datetime.now().strftime("%Y%m%d")

    # ------------------------------------------------------------------
    # 共享格式化工具（P0-3）—— 给所有 _write_* 复用
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_confidence(value, default="--"):
        """置信度统一保留 2 位小数（避免 0.8701155459246288 这种长串）。"""
        if value is None or value == "":
            return default
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt_zt_time(value, default="--"):
        """涨停时间格式化：'102439' -> '10:24:39'；'10:24:39' 原样返回。"""
        if value is None or value == "":
            return default
        s = str(value).strip()
        if not s or s in {"-", "0"}:
            return default
        if ":" in s:
            return s
        digits = s.zfill(6)
        if not digits.isdigit() or len(digits) > 6:
            return s
        return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"

    @staticmethod
    def _fmt_pct(value, decimals: int = 2, default="--"):
        """百分比格式化。支持纯数字（5.0 -> '5.00%'）和已带 % 的字符串（直接返回）。"""
        if value is None or value == "":
            return default
        s = str(value).strip()
        if s.endswith("%"):
            return s
        try:
            return f"{float(value):.{decimals}f}%"
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt_score(value, default="--"):
        """评分格式化（整数）。"""
        if value is None or value == "":
            return default
        try:
            return f"{float(value):.0f}"
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt_price(value, default="--"):
        """
        价格格式化：0 / None / 非数字 一律显示 '--'，避免给用户假数据。
        二板定龙等模式在 `今日涨停价` 字段缺失时会传 0，应统一回退到 '--'。
        """
        if value is None or value == "":
            return default
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if v == 0:
            return default
        return f"{v:.2f}"

    @staticmethod
    def _score_format(formats: Dict, value, kind: str = "score"):
        """
        根据评分大小返回对应的色阶格式键（红/黄/绿）。
        kind: 'score' (0-100) / 'pct' / 'confidence' (0-1)
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return formats.get("cell")
        if kind == "confidence":
            v = v * 100
        if kind == "pct":
            # 收益率：>=3% 绿，<=-3% 红，否则黄
            if v >= 3:
                return formats.get("highlight_green", formats.get("cell"))
            if v <= -3:
                return formats.get("highlight_red", formats.get("cell"))
            return formats.get("highlight_yellow", formats.get("cell"))
        # 默认 0-100 评分
        if v >= 70:
            return formats.get("highlight_green", formats.get("cell"))
        if v <= 35:
            return formats.get("highlight_red", formats.get("cell"))
        return formats.get("highlight_yellow", formats.get("cell"))

    def create_daily_report(self, data_dict: Dict, file_name: str = None,
                            sections: Optional[List] = None) -> Path:
        """
        生成每日分析报告V2

        data_dict应包含:
        - date: 分析日期
        - emotion_result: 情绪周期结果
        - mainline_df: 热点概念DataFrame
        - patterns: 模式识别结果 (首板突破、二板定龙、弱转强、龙头二波)
        - hierarchy_df: 涨停梯队数据
        - zt_pool: 涨停池数据

        Args:
            data_dict: 报告数据
            file_name: 输出文件名（默认 `短线情绪分析报告_{date}.xlsx`）
            sections: 自定义 section 列表，每项实现 `ReportSection` 协议；
                     为 None 时使用 `default_sections()` 内置 10 个 Sheet（P3-6）。
        """
        date = data_dict.get('date', self.date_str)
        if file_name is None:
            file_name = f"短线情绪分析报告_{date}.xlsx"

        file_path = self.output_dir / file_name

        # P3-6：通过 ReportSection 协议遍历 sections
        if sections is None:
            from core.report.sections import default_sections
            sections = default_sections(self)

        with pd.ExcelWriter(file_path, engine='xlsxwriter') as writer:
            workbook = writer.book
            formats = self._create_formats(workbook)

            for section in sections:
                logger.debug(f"[Report] 渲染 section: {getattr(section, 'sheet_name', '?')}")
                section.render(writer, data_dict, formats)

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
        """Sheet 1: 市场概览 — 整合大盘环境 + 情绪周期 + 涨停连续性

        Sprint E-3：底部追加"情绪相位预警 + 历史相似日 Top3"两段，让用户
        在打开第一张 sheet 时就能拿到前瞻性视角（"今天处于上升期晚期，
        历史最像 2025-12-15 那天，那天次日……"）。
        """
        emotion = data_dict.get('emotion_result', {})
        market_env = data_dict.get('market_env', {})
        date = data_dict.get('date', self.date_str)
        emotion_phase = data_dict.get('emotion_phase')
        similar_days = data_dict.get('similar_days')

        def safe_str(value, default='未知'):
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return value
            if hasattr(value, 'name'):
                return str(value.name)
            if hasattr(value, 'value'):
                return str(value.value)
            return str(value)

        def fmt_pct(value, default='--'):
            if value is None:
                return default
            try:
                return f"{float(value):.2f}%"
            except (ValueError, TypeError):
                return default

        def fmt_ratio(value, default='--'):
            if value is None:
                return default
            try:
                return f"{float(value):.1%}"
            except (ValueError, TypeError):
                return default

        metrics = emotion.get('metrics', {})
        strategy_obj = emotion.get('strategy', {})
        position = safe_str(strategy_obj.get('position') if isinstance(strategy_obj, dict) else getattr(strategy_obj, 'position', None))
        strategy_desc = safe_str(strategy_obj.get('strategy') if isinstance(strategy_obj, dict) else getattr(strategy_obj, 'strategy', None))

        trend = market_env.get('trend', {})
        volume = market_env.get('volume', {})
        width = market_env.get('width', {})
        continuity = market_env.get('limit_up_continuity', {})
        first_board = market_env.get('first_board_continuity', {})
        sh_idx = market_env.get('sh_index', {})
        sz_idx = market_env.get('sz_index', {})
        cyb_idx = market_env.get('cyb_index', {})
        kcb_idx = market_env.get('kcb_index', {})
        bj_idx = market_env.get('bj_index', {})

        dashboard_data = [
            ['报告日期', date, '', '', '', ''],
            ['', '', '', '', '', ''],
            ['━━━ 一、大盘环境（Layer1 多指数综合评估）', '', '', '', '', ''],
            ['', '', '', '', '', ''],
            ['指数名称', '收盘价', '涨跌幅', '', '', ''],
            ['上证指数', f"{sh_idx.get('close', 0):.2f}", fmt_pct(sh_idx.get('change_pct')), '', '', ''],
            ['深证成指', f"{sz_idx.get('close', 0):.2f}", fmt_pct(sz_idx.get('change_pct')), '', '', ''],
            ['创业板指', f"{cyb_idx.get('close', 0):.2f}", fmt_pct(cyb_idx.get('change_pct')), '', '', ''],
            ['科创50', f"{kcb_idx.get('close', 0):.2f}", fmt_pct(kcb_idx.get('change_pct')), '', '', ''],
            ['北证50', f"{bj_idx.get('close', 0):.2f}", fmt_pct(bj_idx.get('change_pct')), '', '', ''],
            # P0-3：把 5 个指数聚合后的趋势单独成行，避免误导用户以为是 SH 单独的评分
            ['综合趋势', trend.get('state', '--'), f"评分 {trend.get('score', 0):.0f}/100", '', '', ''],
            ['', '', '', '', '', ''],
            ['量能指标', '数值', '说明', '', '', ''],
            ['全市场成交额', f"{volume.get('total', 0):.0f}亿", '全市场个股成交额汇总', '', '', ''],
            ['量比(上证)', f"{volume.get('ratio', 0):.2f}", '当日成交额/5日均量', '', '', ''],
            ['量能状态', volume.get('state', '--'), '', '', '', ''],
            ['量能评分', f"{volume.get('score', 0):.0f}", '', '', '', ''],
            ['', '', '', '', '', ''],
            ['市场宽度', '数值', '说明', '', '', ''],
            ['上涨家数', width.get('up_count', 0), '', '', '', ''],
            ['下跌家数', width.get('down_count', 0), '', '', '', ''],
            ['平盘家数', width.get('flat_count', 0), '', '', '', ''],
            ['上涨比例', fmt_ratio(width.get('up_ratio')), '', '', '', ''],
            ['宽度状态', width.get('state', '--'), '', '', '', ''],
            ['宽度评分', f"{width.get('score', 0):.0f}", '', '', '', ''],
            ['', '', '', '', '', ''],
            ['━━━ 二、涨停连续性（昨日涨停股今日表现）', '', '', '', '', ''],
            ['', '', '', '', '', ''],
            ['指标', '数值', '说明', '', '', ''],
            ['昨日涨停总数', continuity.get('total', 0), '前一交易日涨停股票数量', '', '', ''],
            ['今日高开比例', fmt_ratio(continuity.get('gap_up_ratio')), '今日开盘价>昨日收盘价的比例', '', '', ''],
            ['今日收红比例', fmt_ratio(continuity.get('positive_ratio')), '今日收盘价>昨日收盘价的比例', '', '', ''],
            ['', '', '', '', '', ''],
            ['其中首板', '数值', '说明', '', '', ''],
            ['首板总数', first_board.get('total', 0), '昨日首板（连板数=1）数量', '', '', ''],
            ['首板高开比例', fmt_ratio(first_board.get('gap_up_ratio')), '首板股今日高开比例', '', '', ''],
            ['首板收红比例', fmt_ratio(first_board.get('positive_ratio')), '首板股今日收红比例', '', '', ''],
            ['', '', '', '', '', ''],
            ['━━━ 三、情绪周期', '', '', '', '', ''],
            ['', '', '', '', '', ''],
            ['情绪指标', '数值', '说明', '', '', ''],
            ['涨停家数', metrics.get('limit_up_count', 0), '当日涨停股票数量', '', '', ''],
            ['跌停家数', metrics.get('nuclear_button_count', 0), '当日跌停股票数量', '', '', ''],
            ['炸板率', f"{metrics.get('broken_rate', 0):.1f}%", '炸板数/曾涨停数', '', '', ''],
            ['昨日涨停溢价', f"{metrics.get('prev_limit_up_premium', 0):.1f}%", '昨日涨停股今日平均收益', '', '', ''],
            ['最高连板高度', metrics.get('max_board_height', 0), '市场最高连板数', '', '', ''],
            ['', '', '', '', '', ''],
            ['━━━ 四、综合建议', '', '', '', '', ''],
            ['', '', '', '', '', ''],
            ['大盘综合评分', f"{market_env.get('composite_score', 0):.0f}/100", '', '', '', ''],
            ['大盘风险等级', market_env.get('risk_level', '--'), '', '', '', ''],
            ['大盘建议仓位', market_env.get('suggested_position', '--'), '', '', '', ''],
            ['情绪周期', safe_str(emotion.get('cycle_name')), '', '', '', ''],
            ['策略建议仓位', position, '', '', '', ''],
            ['操作策略', strategy_desc, '', '', '', ''],
            ['', '', '', '', '', ''],
            ['交叉判断', market_env.get('cross_judgment', '--'), '', '', '', ''],
        ]

        # Sprint E-3：情绪相位 / 转换预警（前瞻视角）
        if emotion_phase is not None:
            dashboard_data.extend([
                ['', '', '', '', '', ''],
                ['━━━ 五、情绪相位预警（Sprint E-1 前瞻分析）', '', '', '', '', ''],
                ['', '', '', '', '', ''],
                ['指标', '数值', '说明', '', '', ''],
                ['当前周期', getattr(emotion_phase, 'cycle_name', '--'),
                 f"主分 {getattr(emotion_phase, 'main_score', 0):.1f}", '', '', ''],
                ['相位进度', f"{getattr(emotion_phase, 'phase_progress', 0):.0%}",
                 getattr(emotion_phase, 'phase_label', '--'), '', '', ''],
                ['可能转入', getattr(emotion_phase, 'next_likely_cycle', '--') or '--',
                 f"次分 {getattr(emotion_phase, 'next_score', 0):.1f} "
                 f"(差 {getattr(emotion_phase, 'score_gap', 0):.1f} 分)",
                 '', '', ''],
                ['转换预警', getattr(emotion_phase, 'transition_warning', '--'),
                 '差距越小越要警惕', '', '', ''],
            ])

        # Sprint E-3：历史相似日 Top3（直观经验回放）
        if similar_days is not None and getattr(similar_days, 'similar_days', None):
            dashboard_data.extend([
                ['', '', '', '', '', ''],
                ['━━━ 六、历史相似日 Top3（Sprint E-2 KNN 经验回放）', '', '', '', '', ''],
                ['', '', '', '', '', ''],
                ['样本池', f"{similar_days.sample_pool_size} 个历史日",
                 'output/factor_results/*.json', '', '', ''],
                ['排名', '相似日', '距离(归一化)', '简述', '', ''],
            ])
            for i, sd in enumerate(similar_days.similar_days[:3], start=1):
                dashboard_data.append([
                    f"#{i}",
                    getattr(sd, 'trade_date', '--'),
                    f"{getattr(sd, 'distance', 0):.3f}",
                    getattr(sd, 'description', '') or '--',
                    '', '',
                ])

        df = pd.DataFrame(dashboard_data)
        df.to_excel(writer, sheet_name='市场概览', index=False, header=False)

        worksheet = writer.sheets['市场概览']
        worksheet.set_column('A:A', 18)
        worksheet.set_column('B:B', 16)
        worksheet.set_column('C:C', 36)
        worksheet.set_column('D:D', 16)
        worksheet.set_column('E:E', 16)
        worksheet.set_column('F:F', 10)

        # P0-3：动态识别 section（"━━━" 开头）与 header（首格为预知关键词），
        # 不再硬编码行号，否则未来增减行就会错位。
        section_keywords = ("━━━",)
        header_keywords = {"指数名称", "量能指标", "市场宽度", "指标",
                           "其中首板", "情绪指标", "排名"}
        for row_idx, row_data in enumerate(dashboard_data):
            first_cell = str(row_data[0])
            if first_cell.startswith(section_keywords):
                row_fmt = formats['header_green']
            elif first_cell in header_keywords:
                row_fmt = formats['header']
            else:
                row_fmt = formats['cell']
            for col in range(6):
                worksheet.write(row_idx, col, row_data[col], row_fmt)

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
        
        # ========== 第一部分：市场主线（四维评分模型）==========
        if not mainline_df.empty:
            worksheet.merge_range(current_row, 0, current_row, 10, '一、市场主线（涨停集中度+梯队完整性+持续性+龙头强度）', formats['header_green'])
            current_row += 1

            mainline_cols = {
                '排名': '排名',
                '板块名称': '板块名称',
                '板块类型': '板块类型',
                '涨跌幅': '涨跌幅(%)',
                '涨停家数': '涨停家数',
                '涨停集中度': '涨停集中度',
                '梯队完整性': '梯队完整性',
                '梯队详情': '梯队详情',
                '持续性评分': '持续性评分',
                '热点天数': '热点天数',
                '龙头强度': '龙头强度',
                '最高连板': '最高连板',
                '综合评分': '综合评分',
                # Sprint F-8：板块游资共识度（多游资同买=真主线，派发=降温）
                '游资共识': '游资共识',
                '游资加分': '游资加分',
                '所处阶段': '所处阶段',
                '操作建议': '操作建议',
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
                '置信度': self._fmt_confidence(getattr(signal, 'confidence', 0)),
                '涨停时间': self._fmt_zt_time(key_metrics.get('涨停时间', '')),
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
        
        # 列名与 pattern_recognition.detect_second_board_dragon 产出的
        # key_metrics 完全对齐（"首板类型"、"首板质量分"、"买点时机"），
        # 删去了原来"首板质量/是否快速"两个永远为空的字段。
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': self._fmt_confidence(getattr(signal, 'confidence', 0)),
                '首板类型': key_metrics.get('首板类型', '--') or '--',
                '首板质量分': self._fmt_score(key_metrics.get('首板质量分')),
                '次日高开': key_metrics.get('次日高开', '--') or '--',
                '涨停时间': self._fmt_zt_time(key_metrics.get('涨停时间', '')),
                '封单强度': key_metrics.get('封单强度', '--') or '--',
                '买点时机': key_metrics.get('买点时机', '--') or '--',
                '买点': self._fmt_price(getattr(signal, 'entry_price', 0)),
                '止损': self._fmt_price(getattr(signal, 'stop_loss', 0)),
                '止盈': self._fmt_price(getattr(signal, 'take_profit', 0)),
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
        worksheet.set_column('N:N', 10)
        worksheet.set_column('O:O', 40)
        
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
        
        # 列名与 pattern_recognition.detect_weak_to_strong 产出的 key_metrics 对齐：
        # 主信号字段：龙头类型/最高连板/走弱类型/总评分/信号级别/次日高开/竞价量比/涨停时间/回调幅度
        # 日内反转信号字段更少（走弱类型/涨停收复/龙头类型），不存在的列统一显示 '--'
        rows = []
        for signal in signals:
            key_metrics = signal.key_metrics if hasattr(signal, 'key_metrics') else {}
            rows.append({
                '股票代码': getattr(signal, 'stock_code', ''),
                '股票名称': getattr(signal, 'stock_name', ''),
                '龙头类型': key_metrics.get('龙头类型', '--') or '--',
                '最高连板': self._fmt_score(key_metrics.get('最高连板')),
                '所属行业': getattr(signal, 'l2_industry', ''),
                '置信度': self._fmt_confidence(getattr(signal, 'confidence', 0)),
                '走弱类型': key_metrics.get('走弱类型', '--') or '--',
                '总评分': self._fmt_score(key_metrics.get('总评分')),
                '信号级别': key_metrics.get('信号级别', '--') or '--',
                '次日高开': key_metrics.get('次日高开', '--') or '--',
                '竞价量比': key_metrics.get('竞价量比', '--') or '--',
                '涨停时间': self._fmt_zt_time(key_metrics.get('涨停时间', '')),
                '回调幅度': key_metrics.get('回调幅度', '--') or '--',
                '买点': self._fmt_price(getattr(signal, 'entry_price', 0)),
                '止损': self._fmt_price(getattr(signal, 'stop_loss', 0)),
                '止盈': self._fmt_price(getattr(signal, 'take_profit', 0)),
                '仓位': getattr(signal, 'position_size', ''),
                '描述': getattr(signal, 'description', ''),
            })
        
        df = pd.DataFrame(rows)
        df.to_excel(writer, sheet_name='弱转强', index=False)
        
        worksheet = writer.sheets['弱转强']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:D', 12)
        worksheet.set_column('E:E', 15)
        worksheet.set_column('F:F', 10)
        worksheet.set_column('G:L', 12)
        worksheet.set_column('M:P', 10)
        worksheet.set_column('Q:Q', 40)
        
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
                '置信度': self._fmt_confidence(getattr(signal, 'confidence', 0)),
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
        """Sheet 7: 涨停梯队（P1-2：附加 D/E per-stock 因子）"""
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

        # P0-3：'首次涨停时间' 从 'HHMMSS' 格式化成 'HH:MM:SS'
        if '首次涨停时间' in df_display.columns:
            df_display['首次涨停时间'] = df_display['首次涨停时间'].apply(self._fmt_zt_time)

        # P1-2：把 D/E per-stock 因子合并到梯队表
        tech = data_dict.get('stock_tech_factors', {}) or {}
        money = data_dict.get('moneyflow_factors', {}) or {}
        if (tech or money) and '股票代码' in df_display.columns:
            def _pick(d, key, default="--"):
                v = d.get(key)
                if v is None:
                    return default
                try:
                    return f"{float(v):.1f}"
                except (TypeError, ValueError):
                    return str(v)

            codes = df_display['股票代码'].astype(str).tolist()
            df_display['D3封板强度'] = [_pick(tech.get(c, {}), 'D3_seal_strength') for c in codes]
            df_display['D2量价配合'] = [_pick(tech.get(c, {}), 'D2_vol_price_coord') for c in codes]
            df_display['D4换手健康'] = [_pick(tech.get(c, {}), 'D4_turnover_health') for c in codes]
            df_display['D5均线多头'] = [_pick(tech.get(c, {}), 'D5_ma_bull_align') for c in codes]
            df_display['E1主力净占比'] = [_pick(money.get(c, {}), 'E1_main_net_ratio') for c in codes]
            df_display['E4资金趋势'] = [_pick(money.get(c, {}), 'E4_moneyflow_trend') for c in codes]

        df_display.to_excel(writer, sheet_name='涨停梯队', index=False)
        
        worksheet = writer.sheets['涨停梯队']
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:D', 15)
        worksheet.set_column('E:H', 12)
        worksheet.set_column('I:N', 12)
        
        # 应用表头格式
        for col_num in range(len(df_display.columns)):
            worksheet.write(0, col_num, df_display.columns[col_num], formats['header'])

        # P1-2：对因子列做色阶（0-100 分）
        factor_cols = ['D3封板强度', 'D2量价配合', 'D4换手健康', 'D5均线多头',
                       'E1主力净占比', 'E4资金趋势']
        if any(c in df_display.columns for c in factor_cols):
            col_index = {name: idx for idx, name in enumerate(df_display.columns)}
            for r_idx in range(len(df_display)):
                for fc in factor_cols:
                    if fc not in col_index:
                        continue
                    c = col_index[fc]
                    val = df_display.iloc[r_idx, c]
                    worksheet.write(r_idx + 1, c, val,
                                    self._score_format(formats, val, kind='score'))

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
        """Sheet 9: 龙头池 - 正在观察的龙头候选股（P1-2：附加 D/E 因子）"""
        dragon_pool = data_dict.get('dragon_pool', [])
        
        if not dragon_pool:
            df = pd.DataFrame({'提示': ['暂无龙头候选股']})
            df.to_excel(writer, sheet_name='龙头池', index=False)
            return

        tech = data_dict.get('stock_tech_factors', {}) or {}
        money = data_dict.get('moneyflow_factors', {}) or {}

        # Sprint F：龙虎榜画像（按代码查席位摘要 + 信誉，支持降级 None）
        lhb_result = data_dict.get('lhb_result', None)
        lhb_available = bool(lhb_result is not None and getattr(lhb_result, 'available', False))

        def _pick(d, key, default="--"):
            v = d.get(key)
            if v is None:
                return default
            try:
                return f"{float(v):.1f}"
            except (TypeError, ValueError):
                return str(v)

        rows = []
        for dragon in dragon_pool:
            code = str(dragon.get('代码', ''))
            t = tech.get(code, {})
            m = money.get(code, {})
            # Sprint F：游资席位 + 信誉（纯数据，来自当日龙虎榜明细）
            lhb_seats = ''
            lhb_label = '--'
            if lhb_available:
                prof = lhb_result.get_stock(code)
                if prof is not None:
                    lhb_seats = prof.seats_summary(top=3)
                    if prof.has_bad_buyer:
                        lhb_label = '黑买入⚠'
                    elif prof.has_good_buyer:
                        lhb_label = '白买入'
                    else:
                        lhb_label = '中性'
            rows.append({
                '股票代码': code,
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
                # Sprint F：游资席位 + 信誉
                '游资信誉': lhb_label,
                '上榜游资': lhb_seats or '--',
                # 既有因子列
                'D3封板强度': _pick(t, 'D3_seal_strength'),
                'D5均线多头': _pick(t, 'D5_ma_bull_align'),
                'E1主力净占比': _pick(m, 'E1_main_net_ratio'),
                'E4资金趋势': _pick(m, 'E4_moneyflow_trend'),
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
        worksheet.set_column('L:L', 10)
        # Sprint F 游资 2 列（M-N）
        worksheet.set_column('M:M', 10)  # 游资信誉
        worksheet.set_column('N:N', 46)  # 上榜游资
        worksheet.set_column('O:R', 12)  # 既有因子列

        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_purple'])

        factor_cols = ['D3封板强度', 'D5均线多头', 'E1主力净占比', 'E4资金趋势']
        col_index = {name: idx for idx, name in enumerate(df.columns)}
        for r_idx in range(len(df)):
            for fc in factor_cols:
                if fc not in col_index:
                    continue
                c = col_index[fc]
                val = df.iloc[r_idx, c]
                worksheet.write(r_idx + 1, c, val,
                                self._score_format(formats, val, kind='score'))

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


    # ==================================================================
    # 新增 Sheet 方法（P0-2 / P1-1 / P2-1 / P3-1）
    # ==================================================================

    @staticmethod
    def _cn_num(n: int) -> str:
        """1→一, 2→二 ... 用于动态章节编号。"""
        cn = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
        return cn[n] if 0 <= n < len(cn) else str(n)

    def _write_lhb(self, writer, data_dict: Dict, formats: Dict):
        """Sprint F-5：龙虎榜 / 游资 sheet。

        两部分：
          1. 上半：当日上榜股 Top N，按"信誉加权净买入"降序——黑名单游资买入飘红，
             白名单买入飘绿，让"谁在做这只票"一眼可见。
          2. 下半：板块游资共识度（同板块多个游资同买 = 真主线信号）。

        降级：lhb_result 缺失 / available=False（积分不足）时写提示占位。
        """
        sheet_name = '龙虎榜'
        lhb = data_dict.get('lhb_result', None)

        if lhb is None or not getattr(lhb, 'available', False) or not getattr(lhb, 'stock_profiles', None):
            df = pd.DataFrame({'提示': [
                '今日无游资明细数据（Tushare hm_detail 需 10000 积分；'
                '或当日无龙虎榜 / 账户无权限 → 已降级跳过）'
            ]})
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            return

        pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 14)
        worksheet.set_column('D:D', 14)
        worksheet.set_column('E:E', 8)
        worksheet.set_column('F:F', 60)

        title_fmt = formats.get('header_purple') or formats.get('header')
        bold = formats.get('header') or title_fmt
        red = formats.get('highlight_red')
        green = formats.get('highlight_green')

        row = 0
        worksheet.merge_range(row, 0, row, 5,
                              f"龙虎榜游资动向  ({lhb.trade_date})  "
                              f"信誉名录版本 {getattr(lhb, 'reputation_version', '')}",
                              title_fmt)
        row += 2

        # ---- 一、上榜个股（按信誉加权净买入降序）----
        worksheet.merge_range(row, 0, row, 5, '一、上榜个股（按信誉加权净买入排序）', bold)
        row += 1
        headers = ['股票代码', '股票名称', '净买入(万)', '信誉加权净(万)', '席位数', '主要席位[信誉]']
        for c, h in enumerate(headers):
            worksheet.write(row, c, h, bold)
        row += 1

        profiles = sorted(
            lhb.stock_profiles.values(),
            key=lambda p: -p.reputation_weighted_net,
        )
        for prof in profiles[:40]:
            # 行级 format：有黑买飘红、有白买飘绿
            row_fmt = None
            if prof.has_bad_buyer and red is not None:
                row_fmt = red
            elif prof.has_good_buyer and green is not None:
                row_fmt = green
            cell = row_fmt or formats['cell']
            worksheet.write(row, 0, prof.ts_code, cell)
            worksheet.write(row, 1, prof.ts_name, cell)
            worksheet.write(row, 2, round(prof.total_net / 1e4, 0), cell)
            worksheet.write(row, 3, round(prof.reputation_weighted_net / 1e4, 0), cell)
            worksheet.write(row, 4, len(prof.seats), cell)
            worksheet.write(row, 5, prof.seats_summary(top=4), row_fmt or formats['cell_left'])
            row += 1
        row += 2

        # ---- 二、板块游资共识度 ----
        sectors = getattr(lhb, 'sector_profiles', {}) or {}
        if sectors:
            worksheet.merge_range(row, 0, row, 5, '二、板块游资共识度（多游资同买 = 主线确认）', bold)
            row += 1
            sec_headers = ['板块', '上榜票数', '涉及游资数', '白名单游资', '合计净买入(万)', '共识度']
            for c, h in enumerate(sec_headers):
                worksheet.write(row, c, h, bold)
            row += 1
            for sp in sorted(sectors.values(),
                             key=lambda s: (-s.distinct_hm_count, -s.net_buy_total)):
                worksheet.write(row, 0, sp.sector, formats['cell'])
                worksheet.write(row, 1, sp.stock_count, formats['cell'])
                worksheet.write(row, 2, sp.distinct_hm_count, formats['cell'])
                worksheet.write(row, 3, sp.good_hm_count, formats['cell'])
                worksheet.write(row, 4, round(sp.net_buy_total / 1e4, 0), formats['cell'])
                lvl = sp.consensus_level
                lvl_fmt = green if (lvl == '高' and green is not None) else formats['cell']
                worksheet.write(row, 5, lvl, lvl_fmt)
                row += 1
            row += 2

        # ---- 三、游资信誉对今日信号的调整（Sprint F-7：坑货降权降仓 / 优质加权）----
        adjustments = data_dict.get('lhb_adjustments', []) or []
        section_no = 3
        if adjustments:
            worksheet.merge_range(
                row, 0, row, 5,
                f"{self._cn_num(section_no)}、游资信誉对今日信号的调整（黑名单接盘→降权降仓，优质进场→加权）",
                bold,
            )
            row += 1
            adj_headers = ['代码', '名称', '类型', '置信度', '综合分→建议', '说明']
            for c, h in enumerate(adj_headers):
                worksheet.write(row, c, h, bold)
            row += 1
            # 坑货规避（bad）排在前面
            adjustments_sorted = sorted(
                adjustments,
                key=lambda a: (0 if getattr(a, 'kind', '') == 'bad' else 1,
                               getattr(a, 'score_after', 0.0)),
            )
            for a in adjustments_sorted:
                kind = getattr(a, 'kind', '')
                row_fmt = (red if kind == 'bad' else green)
                cell = row_fmt or formats['cell']
                action_b = getattr(a, 'action_before', '') or ''
                action_a = getattr(a, 'action_after', '') or ''
                action_txt = f"{action_b}→{action_a}" if action_b and action_b != action_a else action_a
                worksheet.write(row, 0, getattr(a, 'stock_code', ''), cell)
                worksheet.write(row, 1, getattr(a, 'stock_name', ''), cell)
                worksheet.write(row, 2, '坑货规避' if kind == 'bad' else '优质加权', cell)
                worksheet.write(row, 3,
                                f"{getattr(a, 'conf_before', 0.0):.2f}→{getattr(a, 'conf_after', 0.0):.2f}",
                                cell)
                worksheet.write(row, 4,
                                f"{getattr(a, 'score_before', 0.0):.0f}→{getattr(a, 'score_after', 0.0):.0f} "
                                f"({action_txt})",
                                cell)
                worksheet.write(row, 5, getattr(a, 'note', ''), row_fmt or formats['cell_left'])
                row += 1
            row += 2
            section_no += 1

        # ---- 解读 ----
        notes = [
            "信誉加权净 = Σ 各席位净买入 ×(信誉-50)/50：好游资(>50)买为正、坏游资(<50)买为负。",
            "红行 = 有黑名单游资在买方（次日高位接盘风险）；绿行 = 有白名单游资买入（龙头确认）。",
            "游资信誉调整：黑名单接盘的信号置信度×0.65、综合分−15（仓位档位下沉甚至放弃）；优质进场反向加权。",
            "信誉名录见 data/reputation/hm_reputation.yaml，可人工维护；评分为内部经验值，非投资建议。",
        ]
        worksheet.merge_range(row, 0, row, 5, f'{self._cn_num(section_no)}、解读', bold)
        row += 1
        for note in notes:
            worksheet.write(row, 0, '•')
            worksheet.merge_range(row, 1, row, 5, note, formats['cell_left'])
            row += 1

    def _write_cycle_pattern_matrix(self, writer, data_dict: Dict, formats: Dict):
        """Sprint D-2：周期 × 模式胜率矩阵 sheet。

        两张子表（上下排列）：
          1. 胜率矩阵 ``win_rate(n=N)``
          2. 平均收益矩阵 ``avg_return(n=N)``

        样本数 < 3 的单元格统一显示 ``N/A(n=K)``，避免小样本误导。
        """
        matrix = data_dict.get('cycle_pattern_matrix', None)
        sheet_name = '周期模式胜率'

        if matrix is None or not getattr(matrix, 'cells', None):
            df = pd.DataFrame({
                '提示': ['周期 × 模式矩阵尚无足够数据，需先回填近 30 天 factor_results JSON']
            })
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            return

        pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:I', 16)

        title_fmt = formats.get('title') or formats.get('header_purple') or formats.get('header')
        bold = formats.get('header_purple') or formats.get('header')

        row = 0
        worksheet.write(row, 0, '周期 × 模式胜率矩阵', title_fmt)
        row += 1
        worksheet.write(row, 0, '样本窗口')
        worksheet.write(
            row, 1,
            f"{matrix.sample_window[0]} ~ {matrix.sample_window[1]} "
            f"（共 {matrix.sample_count_total} 个历史信号）"
        )
        row += 2

        # ---- 上半：胜率视图 ----
        worksheet.merge_range(row, 0, row, len(matrix.patterns), '一、胜率视图（n=有效样本数）', bold)
        row += 1
        # 表头
        worksheet.write(row, 0, '情绪周期 \\ 模式', bold)
        for c, pat in enumerate(matrix.patterns, start=1):
            worksheet.write(row, c, pat, bold)
        row += 1
        # 数据行
        df_win = matrix.to_dataframe(value="win_rate", min_n=3)
        for cyc in matrix.cycles:
            worksheet.write(row, 0, cyc)
            for c, pat in enumerate(matrix.patterns, start=1):
                cell_val = df_win.loc[cyc, pat] if pat in df_win.columns and cyc in df_win.index else "--"
                worksheet.write(row, c, cell_val)
            row += 1
        row += 2

        # ---- 下半：平均收益视图 ----
        worksheet.merge_range(row, 0, row, len(matrix.patterns), '二、平均 T+1 收益视图（n=有效样本数）', bold)
        row += 1
        worksheet.write(row, 0, '情绪周期 \\ 模式', bold)
        for c, pat in enumerate(matrix.patterns, start=1):
            worksheet.write(row, c, pat, bold)
        row += 1
        df_ret = matrix.to_dataframe(value="avg_return", min_n=3)
        for cyc in matrix.cycles:
            worksheet.write(row, 0, cyc)
            for c, pat in enumerate(matrix.patterns, start=1):
                cell_val = df_ret.loc[cyc, pat] if pat in df_ret.columns and cyc in df_ret.index else "--"
                worksheet.write(row, c, cell_val)
            row += 1
        row += 2

        # ---- 解读提示 ----
        notes = [
            "解读：每格代表 [情绪周期 × 模式] 这个组合的历史表现。",
            "n=K 表示该组合有 K 个历史样本；样本 < 3 时显示 N/A，避免小样本误导。",
            "建议：明日开盘后先看大盘是哪个情绪周期，再对应着这张表找当前周期下胜率最高的模式重点关注。",
        ]
        worksheet.merge_range(row, 0, row, len(matrix.patterns), '三、解读', bold)
        row += 1
        for note in notes:
            worksheet.write(row, 0, '•')
            worksheet.merge_range(row, 1, row, len(matrix.patterns), note)
            row += 1

    def _write_action_list(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 今日操作清单（P2-1）

        把 4 类模式信号（首板突破/二板定龙/弱转强/龙二波）合并到一张表，
        按置信度降序排列，给出股票代码+名称+模式+置信度+买点止损止盈仓位。
        用户不再需要切换 4 个 sheet 才能拼出"今天买什么"。
        """
        patterns = data_dict.get('patterns', {}) or {}
        rows = []
        for pattern_name in ('首板突破', '二板定龙', '弱转强', '龙二波', '龙头二波'):
            for signal in patterns.get(pattern_name, []):
                rows.append({
                    '模式': pattern_name,
                    '股票代码': getattr(signal, 'stock_code', ''),
                    '股票名称': getattr(signal, 'stock_name', ''),
                    '所属行业': getattr(signal, 'l2_industry', ''),
                    '置信度': self._fmt_confidence(getattr(signal, 'confidence', 0)),
                    '买点': getattr(signal, 'entry_price', 0),
                    '止损': getattr(signal, 'stop_loss', 0),
                    '止盈': getattr(signal, 'take_profit', 0),
                    '仓位': getattr(signal, 'position_size', ''),
                    '描述': getattr(signal, 'description', ''),
                })

        if not rows:
            df = pd.DataFrame({'提示': ['今日暂无任何信号 —— 建议空仓观望']})
            df.to_excel(writer, sheet_name='今日操作清单', index=False)
            return

        df = pd.DataFrame(rows)
        try:
            df = df.sort_values('置信度', ascending=False, key=lambda s: pd.to_numeric(s, errors='coerce'))
        except Exception:
            pass

        df.to_excel(writer, sheet_name='今日操作清单', index=False)
        worksheet = writer.sheets['今日操作清单']
        widths = [12, 12, 12, 16, 10, 10, 10, 10, 10, 40]
        for i, w in enumerate(widths[:len(df.columns)]):
            worksheet.set_column(i, i, w)
        for col_num in range(len(df.columns)):
            worksheet.write(0, col_num, df.columns[col_num], formats['header_orange'])

        # 置信度色阶
        if '置信度' in df.columns:
            ci = list(df.columns).index('置信度')
            for r_idx in range(len(df)):
                val = df.iloc[r_idx, ci]
                worksheet.write(r_idx + 1, ci, val,
                                self._score_format(formats, val, kind='confidence'))

    def _write_trade_plans(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 交易计划（P0-2）

        Layer4 已经在跑，但产物只落到 CSV，Excel 里完全看不见。
        这里把 trade_plans_df 直接落到一张 sheet。
        """
        df: pd.DataFrame = data_dict.get('trade_plans_df', pd.DataFrame())
        if df is None or df.empty:
            placeholder = pd.DataFrame({'提示': ['今日无交易计划（信号数为 0 或大盘风险过高）']})
            placeholder.to_excel(writer, sheet_name='交易计划', index=False)
            return

        df_out = df.copy()
        df_out.to_excel(writer, sheet_name='交易计划', index=False)
        worksheet = writer.sheets['交易计划']

        col_widths = {
            '股票代码': 12, '股票名称': 12, '模式类型': 12,
            '优先级': 8, '综合评分': 10, '仓位等级': 10,
            '建议仓位': 10, '仓位依据': 22, '竞价条件': 12, '竞价区间': 16,
            '入场区间': 18, '止损': 10, '止盈': 10,
            '次日预期': 28, '风险提示': 28,
            '风控动作': 10, '风控后仓位': 12, '风控提示': 30,
        }
        for i, col in enumerate(df_out.columns):
            worksheet.set_column(i, i, col_widths.get(col, 14))

        for col_num, col in enumerate(df_out.columns):
            worksheet.write(0, col_num, col, formats['header_orange'])

        if '综合评分' in df_out.columns:
            ci = list(df_out.columns).index('综合评分')
            for r_idx in range(len(df_out)):
                val = df_out.iloc[r_idx, ci]
                worksheet.write(r_idx + 1, ci, val,
                                self._score_format(formats, val, kind='score'))

    def _write_risk_gate(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 风控闸门（Sprint R-2/R-3）

        把 Layer4.5 风控闸门的结果落成一页，纯数据展示：
        - 顶部：账户级熔断状态（单日亏损/回撤/情绪冰点）+ 有效总仓位上限
        - 中段：逐条计划的风控决策（通过/降级/拒绝）+ 可解释理由
        """
        result = data_dict.get('risk_gate_result')

        worksheet = writer.book.add_worksheet('风控闸门')
        writer.sheets['风控闸门'] = worksheet
        worksheet.set_column('A:A', 16)
        worksheet.set_column('B:B', 12)
        worksheet.set_column('C:C', 12)
        worksheet.set_column('D:D', 12)
        worksheet.set_column('E:E', 10)
        worksheet.set_column('F:F', 48)

        if result is None:
            worksheet.merge_range(0, 0, 0, 5,
                                  '风控闸门未执行（无交易计划或降级跳过）',
                                  formats['cell_left'])
            return

        cb = getattr(result, 'cb_status', None)
        row = 0

        # 顶部横幅：熔断状态
        level = getattr(cb, 'level', 'NORMAL') if cb else 'NORMAL'
        banner_fmt = formats['header_purple']
        if level == 'HALT':
            banner_fmt = formats['header']  # 蓝底，醒目
        worksheet.merge_range(
            row, 0, row, 5,
            f"风控闸门  ({getattr(result, 'trade_date', '--')})  熔断状态: {level}",
            banner_fmt,
        )
        row += 1

        # 熔断触发明细
        triggers = getattr(cb, 'triggers', []) if cb else []
        if triggers:
            for t in triggers:
                worksheet.merge_range(row, 0, row, 5, f"⚠ {t}", formats['highlight_red'])
                row += 1
        else:
            worksheet.merge_range(row, 0, row, 5, '账户级熔断: 无触发（NORMAL）',
                                  formats['highlight_green'])
            row += 1
        row += 1

        # 概览指标
        worksheet.merge_range(row, 0, row, 5, '一、组合层概览', formats['header_green'])
        row += 1
        overview = [
            ('有效总仓位上限', f"{getattr(result, 'effective_total_cap', 1.0):.0%}"),
            ('开闸前持仓比例', f"{getattr(result, 'total_position_before', 0.0):.0%}"),
            ('账户权益', f"{getattr(result, 'equity', 0.0):,.0f}"),
            ('决策汇总', f"通过 {result.passed} / 降级 {result.downgraded} / 拒绝 {result.rejected}"),
        ]
        for label, val in overview:
            worksheet.write(row, 0, label, formats['header'])
            worksheet.merge_range(row, 1, row, 5, val, formats['cell_left'])
            row += 1

        # 板块暴露
        exposure = getattr(result, 'sector_exposure_before', {}) or {}
        if exposure:
            worksheet.write(row, 0, '开闸前板块暴露', formats['header'])
            exp_text = ", ".join(f"{k}:{v:.0%}" for k, v in
                                 sorted(exposure.items(), key=lambda x: -x[1]))
            worksheet.merge_range(row, 1, row, 5, exp_text, formats['cell_left'])
            row += 1
        row += 1

        # 决策明细表
        worksheet.merge_range(row, 0, row, 5, '二、逐条计划风控决策', formats['header_green'])
        row += 1
        headers = ['股票名称', '股票代码', '模式', '原仓位', '风控后', '动作 / 理由']
        for c, h in enumerate(headers):
            worksheet.write(row, c, h, formats['header'])
        row += 1

        decisions = getattr(result, 'decisions', []) or []
        action_fmt = {
            'PASS': formats['highlight_green'],
            'DOWNGRADE': formats['highlight_yellow'],
            'REJECT': formats['highlight_red'],
        }
        action_label = {'PASS': '通过', 'DOWNGRADE': '降级', 'REJECT': '拒绝'}
        # 只展示参与买入（原仓位 > 0）的计划，避免观察/回避刷屏
        shown = [d for d in decisions if d.original_position_pct > 1e-6]
        if not shown:
            worksheet.merge_range(row, 0, row, 5, '今日无买入计划进入风控闸门', formats['cell'])
            row += 1
        else:
            for d in shown:
                fmt = action_fmt.get(d.action, formats['cell'])
                worksheet.write(row, 0, d.stock_name, formats['cell'])
                worksheet.write(row, 1, d.stock_code, formats['cell'])
                worksheet.write(row, 2, d.pattern_type, formats['cell'])
                worksheet.write(row, 3, f"{d.original_position_pct:.0%}", formats['cell'])
                worksheet.write(row, 4, f"{d.final_position_pct:.0%}", fmt)
                worksheet.write(row, 5, f"[{action_label.get(d.action, d.action)}] {d.reason_text}", formats['cell_left'])
                row += 1

    def _write_review(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 复盘总结（P0-2）

        把 Layer5 的 review_result 落成一页：
        - 上半页：模式胜率统计（pattern_stats）
        - 中段：情绪周期趋势（emotion_trends）
        - 底部：综合总结 + 参数调优建议（review_summary / parameter_advice）
        """
        review = data_dict.get('review_result', {}) or {}

        worksheet = writer.book.add_worksheet('复盘总结')
        writer.sheets['复盘总结'] = worksheet
        worksheet.set_column('A:A', 18)
        worksheet.set_column('B:H', 14)

        # 解析统计来源标签
        stats_source = str(review.get('stats_source', 'today') or 'today')
        stats_window = review.get('stats_window', ['', '']) or ['', '']
        pending_n = int(review.get('pending_signal_count', 0) or 0)

        if stats_source == 'history':
            source_label = (
                f"统计源：历史 {stats_window[0]}~{stats_window[1]} "
                f"（今日 {pending_n} 个信号等待 T+1 确认）"
            )
        elif stats_source == 'pending':
            source_label = f"统计源：等待 T+1 数据（今日 {pending_n} 个信号尚未验证）"
        else:
            source_label = "统计源：今日 T+1 已验证"
            if pending_n:
                source_label += f"（{pending_n} 只仍未拿到 T+1 行情）"

        row = 0
        worksheet.merge_range(row, 0, row, 7,
                              f"复盘总结  ({review.get('trade_date', '--')})",
                              formats['header_purple'])
        row += 1
        worksheet.merge_range(row, 0, row, 7, source_label, formats['cell_left'])
        row += 2

        # 模式胜率
        worksheet.merge_range(row, 0, row, 7, '一、模式胜率统计', formats['header_green'])
        row += 1
        headers = ['模式', '信号数', '盈利数', '胜率', '平均收益', '最大收益', '最小收益', '平均置信度']
        for c, h in enumerate(headers):
            worksheet.write(row, c, h, formats['header'])
        row += 1

        pattern_stats = review.get('pattern_stats', {}) or {}
        if not pattern_stats:
            worksheet.merge_range(row, 0, row, 7, '暂无可统计的历史信号', formats['cell'])
            row += 1
        elif stats_source == 'pending':
            # T+1 数据完全没到，把 0 渲染成 "等 T+1"，避免误导
            for name, stats in pattern_stats.items():
                worksheet.write(row, 0, name, formats['cell'])
                worksheet.write(row, 1, stats.get('total_signals', 0), formats['cell'])
                worksheet.write(row, 2, '等 T+1', formats['cell'])
                worksheet.write(row, 3, '等 T+1', formats['cell'])
                worksheet.write(row, 4, '等 T+1', formats['cell'])
                worksheet.write(row, 5, '等 T+1', formats['cell'])
                worksheet.write(row, 6, '等 T+1', formats['cell'])
                worksheet.write(row, 7, self._fmt_confidence(stats.get('avg_confidence', 0)), formats['cell'])
                row += 1
        else:
            for name, stats in pattern_stats.items():
                worksheet.write(row, 0, name, formats['cell'])
                worksheet.write(row, 1, stats.get('total_signals', 0), formats['cell'])
                worksheet.write(row, 2, stats.get('profitable_signals', 0), formats['cell'])
                wr = stats.get('win_rate', 0)
                worksheet.write(row, 3, self._fmt_pct(wr * 100 if wr <= 1 else wr),
                                self._score_format(formats, (wr * 100 if wr <= 1 else wr), kind='score'))
                worksheet.write(row, 4, self._fmt_pct(stats.get('avg_return', 0)),
                                self._score_format(formats, stats.get('avg_return', 0), kind='pct'))
                worksheet.write(row, 5, self._fmt_pct(stats.get('max_return', 0)), formats['cell'])
                worksheet.write(row, 6, self._fmt_pct(stats.get('min_return', 0)), formats['cell'])
                worksheet.write(row, 7, self._fmt_confidence(stats.get('avg_confidence', 0)), formats['cell'])
                row += 1
        row += 1

        # 情绪趋势
        worksheet.merge_range(row, 0, row, 7, '二、近期情绪周期趋势', formats['header_green'])
        row += 1
        trend_headers = ['日期', '周期', '涨停数', '炸板率', '溢价率', '最高连板']
        for c, h in enumerate(trend_headers):
            worksheet.write(row, c, h, formats['header'])
        row += 1
        for t in review.get('emotion_trends', []) or []:
            worksheet.write(row, 0, self._fmt_zt_time(t.get('date', '--')) if False else t.get('date', '--'), formats['cell'])
            worksheet.write(row, 1, t.get('cycle_name', '--'), formats['cell'])
            worksheet.write(row, 2, t.get('limit_up_count', 0), formats['cell'])
            worksheet.write(row, 3, self._fmt_pct(t.get('broken_rate', 0)), formats['cell'])
            worksheet.write(row, 4, self._fmt_pct(t.get('premium_rate', 0)),
                            self._score_format(formats, t.get('premium_rate', 0), kind='pct'))
            worksheet.write(row, 5, t.get('max_board_height', 0), formats['cell'])
            row += 1

        summary_text = str(review.get('emotion_trend_summary', '') or '')
        if summary_text:
            row += 1
            worksheet.merge_range(row, 0, row, 7, '情绪趋势小结', formats['header'])
            row += 1
            worksheet.merge_range(row, 0, row, 7, summary_text, formats['cell_left'])
            row += 2

        # 综合总结
        worksheet.merge_range(row, 0, row, 7, '三、综合总结', formats['header_green'])
        row += 1
        review_summary = str(review.get('review_summary', '') or '今日无复盘总结')
        worksheet.merge_range(row, 0, row, 7, review_summary, formats['cell_left'])
        row += 2
        worksheet.merge_range(row, 0, row, 7, '四、参数调优建议', formats['header_green'])
        row += 1
        worksheet.merge_range(row, 0, row, 7,
                              str(review.get('parameter_advice', '') or '无建议'),
                              formats['cell_left'])
        row += 2

        # Sprint E-3：情绪相位 / 转换预警（前瞻分析）
        emotion_phase = data_dict.get('emotion_phase')
        if emotion_phase is not None:
            worksheet.merge_range(row, 0, row, 7, '五、情绪相位 / 转换预警', formats['header_green'])
            row += 1
            phase_headers = ['当前周期', '相位进度', '相位标签', '可能转入', '主分', '次分', '差距', '预警']
            for c, h in enumerate(phase_headers):
                worksheet.write(row, c, h, formats['header'])
            row += 1
            worksheet.write(row, 0, getattr(emotion_phase, 'cycle_name', '--'), formats['cell'])
            worksheet.write(row, 1,
                            f"{getattr(emotion_phase, 'phase_progress', 0):.0%}",
                            formats['cell'])
            worksheet.write(row, 2, getattr(emotion_phase, 'phase_label', '--'), formats['cell'])
            worksheet.write(row, 3, getattr(emotion_phase, 'next_likely_cycle', '--') or '--',
                            formats['cell'])
            worksheet.write(row, 4, f"{getattr(emotion_phase, 'main_score', 0):.1f}", formats['cell'])
            worksheet.write(row, 5, f"{getattr(emotion_phase, 'next_score', 0):.1f}", formats['cell'])
            worksheet.write(row, 6, f"{getattr(emotion_phase, 'score_gap', 0):.1f}", formats['cell'])
            warn_text = getattr(emotion_phase, 'transition_warning', '--')
            warn_fmt = formats['cell_left']
            if '警惕' in warn_text:
                warn_fmt = formats.get('highlight_red') or formats.get('cell_left') or warn_fmt
            elif '关注' in warn_text:
                warn_fmt = formats.get('highlight_yellow') or formats.get('cell_left') or warn_fmt
            worksheet.write(row, 7, warn_text, warn_fmt)
            row += 2

        # Sprint E-3：历史相似日 Top3
        similar_days = data_dict.get('similar_days')
        if similar_days is not None and getattr(similar_days, 'similar_days', None):
            worksheet.merge_range(row, 0, row, 7,
                                  f'六、历史相似日 Top3（样本池 {similar_days.sample_pool_size} 个）',
                                  formats['header_green'])
            row += 1
            sd_headers = ['排名', '日期', '距离', '周期', '描述']
            for c, h in enumerate(sd_headers):
                worksheet.write(row, c, h, formats['header'])
            # 后 3 列空表头占位（merge）
            row += 1
            for i, sd in enumerate(similar_days.similar_days[:3], start=1):
                worksheet.write(row, 0, f"#{i}", formats['cell'])
                worksheet.write(row, 1, getattr(sd, 'trade_date', '--'), formats['cell'])
                worksheet.write(row, 2, f"{getattr(sd, 'distance', 0):.3f}", formats['cell'])
                worksheet.write(row, 3, getattr(sd, 'cycle_name', '--') or '--', formats['cell'])
                worksheet.merge_range(row, 4, row, 7,
                                      getattr(sd, 'description', '') or '--',
                                      formats['cell_left'])
                row += 1
            # 解读提示
            row += 1
            worksheet.merge_range(
                row, 0, row, 7,
                '解读：相似日的"次日实际表现"是最直观的经验参考——可拿这些日期的'
                '后续 trade_plans / factor_results 做对照。',
                formats['cell_left'],
            )

    def _write_factor_dashboard(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 因子总览（P1-1）

        把六大类因子（A大盘/B涨停情绪/C板块/D个股/E资金/F跨周期）的当日原始值
        + 各自归一化评分 + 综合评分 一次性铺出来，并应用色阶。
        """
        market_env = data_dict.get('market_env', {}) or {}
        emotion = data_dict.get('emotion_result', {}) or {}
        metrics = emotion.get('metrics', {}) or {}
        tech = data_dict.get('stock_tech_factors', {}) or {}
        money = data_dict.get('moneyflow_factors', {}) or {}

        worksheet = writer.book.add_worksheet('因子总览')
        writer.sheets['因子总览'] = worksheet
        worksheet.set_column('A:A', 24)
        worksheet.set_column('B:B', 14)
        worksheet.set_column('C:C', 14)
        worksheet.set_column('D:D', 14)
        worksheet.set_column('E:E', 36)

        def _avg_factor(d: Dict[str, Dict], key: str) -> Optional[float]:
            vals = []
            for v in d.values():
                if isinstance(v, dict) and v.get(key) is not None:
                    try:
                        vals.append(float(v[key]))
                    except (TypeError, ValueError):
                        continue
            return sum(vals) / len(vals) if vals else None

        rows = [
            # (类别, 因子ID, 因子名, 原始值, 说明)
            ('A 大盘环境', 'A1 趋势评分',
             market_env.get('trend', {}).get('score'),
             '5 指数综合趋势 0-100'),
            ('A 大盘环境', 'A2 量能评分',
             market_env.get('volume', {}).get('score'),
             f"量比 {market_env.get('volume', {}).get('ratio', 0):.2f}"),
            ('A 大盘环境', 'A3 宽度评分',
             market_env.get('width', {}).get('score'),
             f"上涨比例 {market_env.get('width', {}).get('up_ratio', 0):.1%}"),
            ('A 大盘环境', 'A4 成交额环比',
             market_env.get('amount_change_ratio'),
             '今日成交额 / 昨日成交额 - 1'),
            ('A 大盘环境', 'A5 跌停家数',
             market_env.get('limit_down_count'),
             '当日跌停数（负向因子）'),
            ('A 大盘环境', 'A6 炸板次日表现',
             market_env.get('blasted_next_day_pct'),
             '昨日炸板股今日平均涨跌幅 %'),
            ('B 涨停情绪', 'B1 首板/连板比',
             metrics.get('first_board_ratio'),
             '首板占比；高=情绪扩散初期'),
            ('B 涨停情绪', 'B2 一字板占比',
             metrics.get('one_word_ratio'),
             '一字板/总涨停；过高=情绪极端'),
            ('B 涨停情绪', 'B3 尾盘板占比',
             metrics.get('tail_board_ratio'),
             '尾盘涨停占比；高=资金抢筹'),
            ('B 涨停情绪', 'B4 反转板',
             metrics.get('extreme_reversal_count'),
             '地天/天地板数'),
            ('B 涨停情绪', 'B5 平均封单',
             metrics.get('avg_seal_ratio'),
             '涨停股平均封单金额'),
            ('C 板块强度', 'C1 平均封板时间',
             metrics.get('avg_seal_time'),
             '分钟（从开盘算起，越小越强）'),
            ('C 板块强度', '炸板率',
             metrics.get('broken_rate'),
             '炸板数/曾涨停数 %'),
            ('C 板块强度', '昨日涨停溢价',
             metrics.get('prev_limit_up_premium'),
             '昨日涨停股今日平均涨幅 %'),
            ('D 个股技术', 'D1 N日新高均值',
             _avg_factor(tech, 'D1_n_day_high_low'),
             '涨停池均值，0-100'),
            ('D 个股技术', 'D2 量价配合均值',
             _avg_factor(tech, 'D2_vol_price_coord'),
             '涨停池均值，0-100'),
            ('D 个股技术', 'D3 封板强度均值',
             _avg_factor(tech, 'D3_seal_strength'),
             '涨停池均值，0-100'),
            ('D 个股技术', 'D4 换手健康均值',
             _avg_factor(tech, 'D4_turnover_health'),
             '涨停池均值，0-100'),
            ('D 个股技术', 'D5 均线多头均值',
             _avg_factor(tech, 'D5_ma_bull_align'),
             '涨停池均值，0-100'),
            ('E 资金流向', 'E1 主力净占比均值',
             _avg_factor(money, 'E1_main_net_ratio'),
             '涨停池均值 %，正=流入'),
            ('E 资金流向', 'E2 散户净占比均值',
             _avg_factor(money, 'E2_retail_net_ratio'),
             '涨停池均值 %'),
            ('E 资金流向', 'E3 大单买入占比',
             _avg_factor(money, 'E3_large_buy_ratio'),
             '涨停池均值 %'),
            ('E 资金流向', 'E4 5日资金趋势',
             _avg_factor(money, 'E4_moneyflow_trend'),
             '5日净流入正向天数比例'),
            ('F 跨周期', '大盘综合评分',
             market_env.get('composite_score'),
             '0-100'),
            ('F 跨周期', '风险等级',
             market_env.get('risk_level'),
             ''),
            ('F 跨周期', '情绪周期',
             emotion.get('cycle_name'),
             ''),
            ('F 跨周期', '建议仓位',
             market_env.get('suggested_position'),
             ''),
        ]

        # 表头
        r = 0
        worksheet.merge_range(r, 0, r, 4,
                              '因子总览（6 大类因子原始值 / 归一化得分）',
                              formats['header_green'])
        r += 1
        for c, h in enumerate(['因子类别', '因子', '当前值', '类型', '说明']):
            worksheet.write(r, c, h, formats['header'])
        r += 1

        # 数据
        category_color_map = {
            'A': 'header_green', 'B': 'header_orange', 'C': 'header_purple',
            'D': 'header', 'E': 'header_green', 'F': 'header_purple',
        }
        current_cat = None
        for cat, fname, raw, desc in rows:
            cat_prefix = cat.split(' ')[0]
            if cat != current_cat:
                worksheet.merge_range(r, 0, r, 4, cat,
                                      formats[category_color_map.get(cat_prefix, 'header')])
                r += 1
                current_cat = cat
            worksheet.write(r, 0, '', formats['cell'])
            worksheet.write(r, 1, fname, formats['cell_left'])
            # 智能格式化原始值 + 色阶
            if raw is None:
                worksheet.write(r, 2, '--', formats['cell'])
                worksheet.write(r, 3, '无数据', formats['cell'])
            elif isinstance(raw, (int, float)):
                # 默认按 0-100 评分着色；对带 % 含义的字段单独说明
                fval = float(raw)
                if 0 <= fval <= 100 and 'evaluation' not in fname:
                    worksheet.write(r, 2, f"{fval:.2f}",
                                    self._score_format(formats, fval, kind='score'))
                    worksheet.write(r, 3, '0-100', formats['cell'])
                else:
                    worksheet.write(r, 2, f"{fval:.4f}", formats['cell'])
                    worksheet.write(r, 3, '数值', formats['cell'])
            else:
                worksheet.write(r, 2, str(raw), formats['cell_left'])
                worksheet.write(r, 3, '文本', formats['cell'])
            worksheet.write(r, 4, desc, formats['cell_left'])
            r += 1

    def _write_factor_raw(self, writer, data_dict: Dict, formats: Dict):
        """
        Sheet: 因子原始数据（P3-1）

        把 FactorCollector 落盘的 JSON 扁平化成一张审计表。
        每一行 = (Layer, Path, Value)。给量化复盘/喂 LLM/回测用。
        """
        path_str = str(data_dict.get('factor_results_path', '') or '')
        worksheet = writer.book.add_worksheet('因子原始数据')
        writer.sheets['因子原始数据'] = worksheet
        worksheet.set_column('A:A', 12)
        worksheet.set_column('B:B', 60)
        worksheet.set_column('C:C', 30)

        worksheet.write(0, 0, 'Layer', formats['header'])
        worksheet.write(0, 1, '因子路径', formats['header'])
        worksheet.write(0, 2, '当前值', formats['header'])

        if not path_str:
            worksheet.merge_range(1, 0, 1, 2,
                                  '未找到因子结果 JSON —— pipeline 可能未执行 FactorCollector',
                                  formats['cell_left'])
            return

        try:
            import json
            with open(path_str, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception as e:
            worksheet.merge_range(1, 0, 1, 2,
                                  f'读取 {path_str} 失败: {e}',
                                  formats['cell_left'])
            return

        def _flatten(prefix: str, node, out: list, layer: str):
            if isinstance(node, dict):
                for k, v in node.items():
                    new_prefix = f"{prefix}.{k}" if prefix else str(k)
                    _flatten(new_prefix, v, out, layer)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    new_prefix = f"{prefix}[{i}]"
                    _flatten(new_prefix, v, out, layer)
            else:
                out.append((layer, prefix, node))

        rows: list = []
        for top_key in ('layer1_market_env', 'emotion_cycle',
                        'layer2_sector', 'layer3_stock_selection',
                        'layer4_trade_plan'):
            sub = raw.get(top_key)
            if sub is None:
                continue
            _flatten('', sub, rows, top_key)

        r = 1
        for layer, p, v in rows:
            worksheet.write(r, 0, layer, formats['cell'])
            worksheet.write(r, 1, p, formats['cell_left'])
            if isinstance(v, float):
                worksheet.write(r, 2, f"{v:.6f}", formats['cell'])
            elif v is None:
                worksheet.write(r, 2, '--', formats['cell'])
            else:
                worksheet.write(r, 2, str(v), formats['cell_left'])
            r += 1


if __name__ == "__main__":
    from config.settings import OUTPUT_DIR
    rg = ReportGeneratorV2(OUTPUT_DIR)
    print("报告生成器V2初始化成功")

