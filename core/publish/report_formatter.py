"""
报告格式化器

将分析报告转换为适合微信公众号的HTML格式
"""
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional
import loguru

logger = loguru.logger


class ReportFormatter:
    """
    报告格式化器
    
    将DataFrame和字典数据转换为微信公众号支持的HTML格式
    """
    
    # 微信公众号支持的HTML标签
    ALLOWED_TAGS = [
        'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'span',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li',
        'table', 'thead', 'tbody', 'tr', 'th', 'td',
        'img', 'a', 'section'
    ]
    
    def __init__(self):
        self.style = """
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }
            h1 { color: #1a1a1a; font-size: 22px; border-bottom: 2px solid #e74c3c; padding-bottom: 10px; }
            h2 { color: #2c3e50; font-size: 18px; margin-top: 25px; border-left: 4px solid #3498db; padding-left: 10px; }
            h3 { color: #34495e; font-size: 16px; margin-top: 20px; }
            table { width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 14px; }
            th { background-color: #3498db; color: white; padding: 10px; text-align: left; }
            td { padding: 8px; border-bottom: 1px solid #ddd; }
            tr:nth-child(even) { background-color: #f8f9fa; }
            .highlight { background-color: #fff3cd; padding: 2px 5px; border-radius: 3px; }
            .positive { color: #e74c3c; font-weight: bold; }
            .negative { color: #27ae60; font-weight: bold; }
            .neutral { color: #7f8c8d; }
            .mainline-yes { background-color: #d4edda; color: #155724; padding: 2px 8px; border-radius: 12px; font-weight: bold; }
            .mainline-no { background-color: #f8d7da; color: #721c24; padding: 2px 8px; border-radius: 12px; }
            .summary-box { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; margin: 20px 0; }
            .metric-card { background: #f8f9fa; border-left: 4px solid #3498db; padding: 15px; margin: 10px 0; }
            .warning { background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 10px; margin: 10px 0; }
            .danger { background-color: #f8d7da; border-left: 4px solid #dc3545; padding: 10px; margin: 10px 0; }
            .success { background-color: #d4edda; border-left: 4px solid #28a745; padding: 10px; margin: 10px 0; }
        </style>
        """
    
    def format_sentiment_report(self, report_data: Dict) -> str:
        """
        格式化情绪分析报告为HTML
        
        Args:
            report_data: 报告数据字典
            
        Returns:
            HTML格式的报告内容
        """
        date = report_data.get('date', datetime.now().strftime('%Y%m%d'))
        emotion_result = report_data.get('emotion_result', {})
        mainline_df = report_data.get('mainline_df', pd.DataFrame())
        
        html_parts = []
        
        # 标题
        html_parts.append(f"<h1>📊 A股情绪分析报告</h1>")
        html_parts.append(f"<p style='color: #7f8c8d; font-size: 14px;'>报告日期：{date}</p>")
        
        # 情绪概览
        html_parts.append(self._format_emotion_overview(emotion_result))
        
        # 热点概念
        if not mainline_df.empty:
            html_parts.append(self._format_mainline_concepts(mainline_df))
        
        # 首板突破（次日可关注）
        first_break = report_data.get('first_break', [])
        if first_break:
            html_parts.append(self._format_first_break(first_break))
        
        # 龙头池
        dragon_pool = report_data.get('dragon_pool', [])
        if dragon_pool:
            html_parts.append(self._format_dragon_pool(dragon_pool))
        
        # 走弱池
        weakening_pool = report_data.get('weakening_pool', [])
        if weakening_pool:
            html_parts.append(self._format_weakening_pool(weakening_pool))
        
        # 免责声明
        html_parts.append(self._format_disclaimer())
        
        # 组合完整HTML
        content = '\n'.join(html_parts)
        return f"<!DOCTYPE html><html><head>{self.style}</head><body>{content}</body></html>"
    
    def _format_emotion_overview(self, emotion_result: Dict) -> str:
        """格式化情绪概览"""
        if not emotion_result:
            return ""
        
        score = emotion_result.get('composite_score', 0)
        level = emotion_result.get('emotion_level', '未知')
        description = emotion_result.get('description', '')
        
        # 根据分数设置颜色
        if score >= 70:
            color_class = "positive"
            bg_class = "success"
        elif score >= 40:
            color_class = "neutral"
            bg_class = "warning"
        else:
            color_class = "negative"
            bg_class = "danger"
        
        html = f"""
        <div class="summary-box">
            <h2 style="color: white; border: none; margin-top: 0;">📈 市场情绪概览</h2>
            <p style="font-size: 24px; margin: 10px 0;">
                综合评分：<span style="font-size: 36px; font-weight: bold;">{score}</span>/100
            </p>
            <p style="font-size: 18px;">情绪级别：<span class="{color_class}">{level}</span></p>
            <p>{description}</p>
        </div>
        """
        return html
    
    def _format_mainline_concepts(self, df: pd.DataFrame) -> str:
        """格式化热点概念表格"""
        html = ["<h2>🔥 热点概念排行</h2>"]
        html.append("<p>以下概念按综合评分排序，重点关注<span class='mainline-yes'>是</span>主线特征的概念</p>")
        
        # 选择关键列
        key_cols = ['概念名称', '当日排名', '10日进前10', '10日进前5', '10日进前3', 
                   '连续前10天', '是否主线', '涨停数量', '综合评分', '所处阶段']
        
        # 过滤存在的列
        available_cols = [col for col in key_cols if col in df.columns]
        display_df = df[available_cols].head(15)  # 只显示前15个
        
        # 生成表格
        html.append("<table>")
        html.append("<thead><tr>")
        for col in available_cols:
            html.append(f"<th>{col}</th>")
        html.append("</tr></thead>")
        html.append("<tbody>")
        
        for _, row in display_df.iterrows():
            html.append("<tr>")
            for col in available_cols:
                value = row[col]
                # 特殊格式化
                if col == '是否主线':
                    if value == '是':
                        html.append(f"<td><span class='mainline-yes'>是</span></td>")
                    else:
                        html.append(f"<td><span class='mainline-no'>否</span></td>")
                elif col == '综合评分':
                    html.append(f"<td><strong>{value}</strong></td>")
                elif col == '涨停数量' and value >= 10:
                    html.append(f"<td><span class='positive'>{value}</span></td>")
                else:
                    html.append(f"<td>{value}</td>")
            html.append("</tr>")
        
        html.append("</tbody></table>")
        return '\n'.join(html)
    
    def _format_first_break(self, first_break: List[Dict]) -> str:
        """格式化首板突破（次日可关注标的）"""
        html = ["<h2>🎯 首板突破 - 次日关注</h2>"]
        html.append("<div class='success'>")
        html.append("<strong>明日关注：</strong>以下为首板突破标的，次日可重点关注开盘表现")
        html.append("</div>")
        
        html.append("<table>")
        html.append("<thead><tr><th>股票名称</th><th>股票代码</th><th>所属行业</th><th>涨停时间</th><th>封单强度</th><th>买点</th><th>描述</th></tr></thead>")
        html.append("<tbody>")
        
        for item in first_break[:15]:  # 最多显示15个
            name = item.get('股票名称', '')
            code = item.get('股票代码', '')
            industry = item.get('所属行业', '')
            limit_time = item.get('涨停时间', '')
            strength = item.get('封单强度', '')
            buy_point = item.get('买点', '')
            desc = item.get('描述', '')
            
            html.append(f"<tr>")
            html.append(f"<td><strong>{name}</strong></td>")
            html.append(f"<td>{code}</td>")
            html.append(f"<td>{industry}</td>")
            html.append(f"<td>{limit_time}</td>")
            html.append(f"<td>{strength}</td>")
            html.append(f"<td>{buy_point}</td>")
            html.append(f"<td><small>{desc}</small></td>")
            html.append("</tr>")
        
        html.append("</tbody></table>")
        return '\n'.join(html)
    
    def _format_dragon_pool(self, dragon_pool: List[Dict]) -> str:
        """格式化龙头池"""
        html = ["<h2>🐲 龙头候选池</h2>"]
        html.append("<p>近期表现强势，具备龙头特征的股票</p>")
        
        html.append("<table>")
        html.append("<thead><tr><th>股票名称</th><th>股票代码</th><th>龙头类型</th><th>所属板块</th><th>10日涨幅</th><th>涨停次数</th><th>当前状态</th></tr></thead>")
        html.append("<tbody>")
        
        for dragon in dragon_pool[:20]:  # 最多显示20个
            # 支持两种可能的列名格式
            name = dragon.get('股票名称', dragon.get('stock_name', ''))
            code = dragon.get('股票代码', dragon.get('stock_code', ''))
            dragon_type = dragon.get('龙头类型', dragon.get('dragon_type', ''))
            sector = dragon.get('所属板块', dragon.get('sector_name', ''))
            rise = dragon.get('10日涨幅', dragon.get('total_rise_10d', 0))
            limit_count = dragon.get('涨停次数', dragon.get('limit_up_count', 0))
            status = dragon.get('当前状态', dragon.get('status', '观察中'))
            
            # 处理涨幅格式（可能是字符串如"41.1%"或数字）
            if isinstance(rise, str):
                rise_pct = rise
                try:
                    rise_val = float(rise.replace('%', '')) / 100
                except:
                    rise_val = 0
            else:
                rise_pct = f"{rise*100:.1f}%" if isinstance(rise, (int, float)) else str(rise)
                rise_val = rise if isinstance(rise, (int, float)) else 0
            
            rise_class = "positive" if rise_val > 0.3 else "neutral"
            
            html.append(f"<tr>")
            html.append(f"<td><strong>{name}</strong></td>")
            html.append(f"<td>{code}</td>")
            html.append(f"<td>{dragon_type}</td>")
            html.append(f"<td>{sector}</td>")
            html.append(f"<td class='{rise_class}'>{rise_pct}</td>")
            html.append(f"<td>{limit_count}</td>")
            html.append(f"<td>{status}</td>")
            html.append("</tr>")
        
        html.append("</tbody></table>")
        return '\n'.join(html)
    
    def _format_weakening_pool(self, weakening_pool: List[Dict]) -> str:
        """格式化走弱池"""
        html = ["<h2>⚠️ 龙头走弱池</h2>"]
        html.append("<div class='warning'>")
        html.append("<strong>风险提示：</strong>以下股票近期出现走弱信号，建议关注风险")
        html.append("</div>")
        
        html.append("<table>")
        html.append("<thead><tr><th>股票名称</th><th>股票代码</th><th>龙头类型</th><th>走弱日期</th><th>走弱类型</th><th>回调幅度</th><th>观察信号</th></tr></thead>")
        html.append("<tbody>")
        
        for item in weakening_pool[:15]:  # 最多显示15个
            # 支持两种可能的列名格式
            name = item.get('股票名称', item.get('stock_name', ''))
            code = item.get('股票代码', item.get('stock_code', ''))
            dragon_type = item.get('龙头类型', item.get('dragon_type', ''))
            weaken_date = item.get('走弱日期', item.get('weakening_date', '-'))
            weaken_type = item.get('走弱类型', item.get('weakening_reason', item.get('weakening_type', '')))
            callback = item.get('回调幅度', item.get('callback_pct', '0%'))
            signal = item.get('观察信号', item.get('suggestion', item.get('observe_signal', '')))
            
            html.append(f"<tr>")
            html.append(f"<td><strong>{name}</strong></td>")
            html.append(f"<td>{code}</td>")
            html.append(f"<td>{dragon_type}</td>")
            html.append(f"<td><small>{weaken_date}</small></td>")
            html.append(f"<td><span class='negative'>{weaken_type}</span></td>")
            html.append(f"<td>{callback}</td>")
            html.append(f"<td><small>{signal}</small></td>")
            html.append("</tr>")
        
        html.append("</tbody></table>")
        return '\n'.join(html)
    
    def _format_disclaimer(self) -> str:
        """格式化免责声明"""
        return """
        <div style="margin-top: 40px; padding: 20px; background-color: #f8f9fa; border-radius: 8px; font-size: 12px; color: #6c757d;">
            <h3>⚠️ 免责声明</h3>
            <p>1. 本报告仅供参考，不构成投资建议。</p>
            <p>2. 股市有风险，投资需谨慎。</p>
            <p>3. 报告中的数据和分析基于历史数据，不保证未来收益。</p>
            <p>4. 请投资者根据自身风险承受能力做出投资决策。</p>
            <p style="margin-top: 15px; text-align: center;">
                © 2025 A股情绪分析系统 | 数据仅供参考
            </p>
        </div>
        """
    
    def format_simple_report(self, title: str, content: str, 
                            highlight_points: List[str] = None) -> str:
        """
        格式化简单报告
        
        Args:
            title: 报告标题
            content: 报告内容（纯文本或Markdown）
            highlight_points: 要点列表
            
        Returns:
            HTML格式内容
        """
        html_parts = [f"<h1>{title}</h1>"]
        
        if highlight_points:
            html_parts.append("<div class='metric-card'>")
            html_parts.append("<h3>📌 核心要点</h3>")
            html_parts.append("<ul>")
            for point in highlight_points:
                html_parts.append(f"<li>{point}</li>")
            html_parts.append("</ul>")
            html_parts.append("</div>")
        
        # 将纯文本转换为HTML段落
        paragraphs = content.split('\n\n')
        for para in paragraphs:
            if para.strip():
                html_parts.append(f"<p>{para.strip()}</p>")
        
        html_parts.append(self._format_disclaimer())
        
        content_html = '\n'.join(html_parts)
        return f"<!DOCTYPE html><html><head>{self.style}</head><body>{content_html}</body></html>"


if __name__ == "__main__":
    # 测试代码
    formatter = ReportFormatter()
    
    # 模拟报告数据
    test_data = {
        'date': '20250421',
        'emotion_result': {
            'composite_score': 65,
            'emotion_level': '偏乐观',
            'description': '市场情绪较好，可适当参与'
        },
        'mainline_df': pd.DataFrame({
            '概念名称': ['机器人', '人工智能', '新能源汽车'],
            '当日排名': [1, 2, 3],
            '10日进前10': [8, 7, 6],
            '是否主线': ['是', '是', '否'],
            '涨停数量': [15, 12, 8],
            '综合评分': [85, 78, 65],
            '所处阶段': ['加速期', '发酵期', '萌芽期']
        }),
        'dragon_pool': [
            {'stock_name': '测试股1', 'stock_code': '000001', 'dragon_type': '趋势龙', 
             'sector_name': '机器人', 'total_rise_10d': 0.45, 'limit_up_count': 3}
        ],
        'weakening_pool': [
            {'stock_name': '测试股2', 'stock_code': '000002', 'dragon_type': '连板龙',
             'weakening_reason': '断板', 'suggestion': '减仓观望'}
        ]
    }
    
    html = formatter.format_sentiment_report(test_data)
    print(html[:1000])  # 打印前1000字符
    print("...")
