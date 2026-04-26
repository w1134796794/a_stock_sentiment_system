"""
LLM报告生成器

使用大语言模型将量化数据转换为描述性文章
"""
import json
from typing import Dict, List, Optional
from datetime import datetime
import loguru

logger = loguru.logger


class LLMReportGenerator:
    """
    LLM报告生成器
    
    将量化数据转换为自然语言描述的文章
    """
    
    def __init__(self, api_key: str = None, model: str = "gpt-3.5-turbo"):
        """
        初始化
        
        Args:
            api_key: LLM API密钥
            model: 模型名称
        """
        self.api_key = api_key
        self.model = model
        
    def generate_report(self, report_data: Dict) -> str:
        """
        生成LLM润色后的报告
        
        Args:
            report_data: 报告数据字典
            
        Returns:
            生成的文章（HTML格式）
        """
        # 构建prompt
        prompt = self._build_prompt(report_data)
        
        # 调用LLM
        try:
            content = self._call_llm(prompt)
            return content
        except Exception as e:
            logger.error(f"[LLMReportGenerator] LLM调用失败: {e}")
            # 降级为模板生成
            return self._generate_fallback_report(report_data)
    
    def _build_prompt(self, report_data: Dict) -> str:
        """
        构建LLM prompt
        
        Args:
            report_data: 报告数据
            
        Returns:
            prompt字符串
        """
        date = report_data.get('date', datetime.now().strftime('%Y%m%d'))
        emotion = report_data.get('emotion_result', {})
        mainline_df = report_data.get('mainline_df')
        first_break = report_data.get('first_break', [])
        dragon_pool = report_data.get('dragon_pool', [])
        weakening_pool = report_data.get('weakening_pool', [])
        
        # 提取关键数据
        emotion_desc = self._format_emotion(emotion)
        mainline_desc = self._format_mainline(mainline_df)
        first_break_desc = self._format_first_break(first_break)
        dragon_desc = self._format_dragon_pool(dragon_pool)
        weakening_desc = self._format_weakening_pool(weakening_pool)
        
        prompt = f"""你是一位专业的A股短线交易分析师，擅长将量化数据转化为通俗易懂的市场复盘文章。

请根据以下数据，撰写一篇个人交易复盘计划笔记。本文仅为作者个人学习记录，不构成任何投资建议。文章要求：
1. 语言通俗易懂，避免过于专业的术语
2. 以复盘总结为主，记录个人观察和思考
3. 结构清晰，重点突出
4. 适当使用emoji增加可读性
5. 必须包含风险提示和免责声明

=== 报告日期 ===
{date}

=== 市场情绪数据 ===
{emotion_desc}

=== 热点概念数据 ===
{mainline_desc}

=== 首板突破标的（个人观察记录）===
{first_break_desc}

=== 龙头候选池（个人跟踪记录）===
{dragon_desc}

=== 龙头走弱池（风险提示）===
{weakening_desc}

请生成一篇完整的复盘笔记，包含以下部分：
1. 市场情绪概览（记录当日情绪周期状态，个人思考）
2. 主线概念分析（记录热点变化，个人观察）
3. 次日观察计划（记录个人关注的标的及观察逻辑，非推荐）
4. 龙头动态跟踪（记录候选池和走弱池的变化，重点说明走弱龙头从何时开始走弱，至今涨跌幅情况）
5. 个人交易策略总结（记录个人仓位管理和风险控制思路）

针对走弱池的特别说明：
- 需要强调每只走弱股票从何时（日期）开始走弱
- 说明从走弱至今累计上涨或下跌了多少个百分点
- 重点关注那些走弱后反而上涨的股票（可能是假摔，有转强机会）
- 不需要统计是几日内完成的涨跌

重要提示：
- 文中所有涉及个股的内容仅为数据展示，不构成买卖建议
- 必须添加免责声明：本文仅为个人学习记录，不构成投资建议。股市有风险，入市需谨慎。
- 避免使用"买入"、"卖出"、"推荐"、"必涨"等荐股性质的词汇
- 使用"观察"、"跟踪"、"记录"、"复盘"等中性词汇

输出格式要求：
- 使用HTML格式，使用适当的标签（h2, h3, p, ul, strong等）
- 不要添加markdown代码块标记（如```html或```）
- 直接输出纯HTML内容"""
        
        return prompt
    
    def _format_emotion(self, emotion: Dict) -> str:
        """格式化情绪数据"""
        if not emotion:
            return "暂无数据"
        
        return f"""
- 综合评分: {emotion.get('composite_score', 0)}/100
- 情绪级别: {emotion.get('emotion_level', '未知')}
- 涨停家数: {emotion.get('limit_up_count', 0)}
- 跌停家数: {emotion.get('limit_down_count', 0)}
- 最高连板: {emotion.get('max_board_height', 0)}板
- 操作策略: {emotion.get('description', '暂无')}
"""
    
    def _format_mainline(self, mainline_df) -> str:
        """格式化主线概念数据"""
        if mainline_df is None or mainline_df.empty:
            return "暂无数据"
        
        lines = []
        for _, row in mainline_df.head(8).iterrows():
            concept = row.get('概念名称', '')
            rank = row.get('当日排名', 0)
            top10_count = row.get('10日进前10', 0)
            is_mainline = row.get('是否主线', '否')
            stage = row.get('所处阶段', '')
            score = row.get('综合评分', 0)
            
            lines.append(f"- {concept}: 排名{rank}, 10日进前10共{top10_count}次, 是否主线{is_mainline}, 所处阶段{stage}, 评分{score}")
        
        return "\n".join(lines) if lines else "暂无数据"
    
    def _format_first_break(self, first_break: List[Dict]) -> str:
        """格式化首板突破数据"""
        if not first_break:
            return "暂无数据"
        
        lines = []
        for item in first_break[:8]:
            name = item.get('股票名称', '')
            code = item.get('股票代码', '')
            industry = item.get('所属行业', '')
            strength = item.get('封单强度', '')
            desc = item.get('描述', '')
            
            lines.append(f"- {name}({code}): {industry}, 封单强度{strength}, {desc}")
        
        return "\n".join(lines) if lines else "暂无数据"
    
    def _format_dragon_pool(self, dragon_pool: List[Dict]) -> str:
        """格式化龙头池数据"""
        if not dragon_pool:
            return "暂无数据"
        
        lines = []
        for item in dragon_pool[:8]:
            name = item.get('股票名称', item.get('stock_name', ''))
            code = item.get('股票代码', item.get('stock_code', ''))
            dragon_type = item.get('龙头类型', item.get('dragon_type', ''))
            sector = item.get('所属板块', item.get('sector_name', ''))
            rise = item.get('10日涨幅', item.get('total_rise_10d', ''))
            
            lines.append(f"- {name}({code}): {dragon_type}, 所属{sector}, 10日涨幅{rise}")
        
        return "\n".join(lines) if lines else "暂无数据"
    
    def _format_weakening_pool(self, weakening_pool: List[Dict]) -> str:
        """格式化走弱池数据"""
        if not weakening_pool:
            return "暂无数据"
        
        lines = []
        for item in weakening_pool[:8]:
            name = item.get('股票名称', item.get('stock_name', ''))
            code = item.get('股票代码', item.get('stock_code', ''))
            weaken_type = item.get('走弱类型', item.get('weakening_type', ''))
            weaken_date = item.get('走弱日期', item.get('weakening_date', ''))
            
            # 计算从走弱日至今的涨跌幅（使用走弱日价格和当前价格）
            weaken_price = item.get('走弱价格', item.get('weakening_price', 0))
            current_price = item.get('当前价格', item.get('current_price', 0))
            
            change_str = ""
            if weaken_price and current_price and float(weaken_price) > 0:
                try:
                    change_pct = (float(current_price) - float(weaken_price)) / float(weaken_price) * 100
                    
                    if change_pct >= 0:
                        change_str = f"较走弱日上涨{change_pct:.1f}%"
                    else:
                        change_str = f"较走弱日下跌{abs(change_pct):.1f}%"
                except (ValueError, TypeError):
                    pass
            
            # 如果无法计算走弱日涨跌幅，回退到使用回调幅度字段
            if not change_str:
                callback = item.get('回调幅度', item.get('callback_pct', ''))
                try:
                    callback_val = float(callback) if isinstance(callback, (int, float, str)) else 0
                    if callback_val < 0:
                        change_str = f"继续上涨{abs(callback_val):.1f}%"
                    else:
                        change_str = f"回调{callback:.1f}%"
                except (ValueError, TypeError):
                    change_str = f"回调{callback}"
            
            date_str = f", 走弱日期:{weaken_date}" if weaken_date else ""
            lines.append(f"- {name}({code}): {weaken_type}, {change_str}{date_str}")
        
        return "\n".join(lines) if lines else "暂无数据"
    
    def _calc_days_since(self, date_str: str) -> Optional[int]:
        """计算从指定日期到今天的交易日天数"""
        if not date_str:
            return None
        try:
            from datetime import datetime
            target = datetime.strptime(str(date_str), "%Y%m%d")
            today = datetime.now()
            days = (today - target).days
            return max(0, days)
        except:
            return None
    
    def _call_llm(self, prompt: str) -> str:
        """
        调用LLM API
        
        支持OpenAI、通义千问等模型
        
        Args:
            prompt: 提示词
            
        Returns:
            生成的内容
        """
        # 根据模型名称选择对应的API
        model_lower = self.model.lower()
        if "qwen" in model_lower or "dashscope" in model_lower:
            logger.info(f"[LLMReportGenerator] 使用通义千问API: {self.model}")
            return self._call_qwen(prompt)
        elif "gpt" in model_lower or "openai" in model_lower:
            logger.info(f"[LLMReportGenerator] 使用OpenAI API: {self.model}")
            return self._call_openai(prompt)
        else:
            # 默认使用千问
            logger.info(f"[LLMReportGenerator] 默认使用通义千问API: {self.model}")
            return self._call_qwen(prompt)
    
    def _call_openai(self, prompt: str) -> str:
        """调用OpenAI API"""
        try:
            import openai
            openai.api_key = self.api_key
            
            response = openai.ChatCompletion.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一位专业的A股短线交易分析师。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=3000
            )
            
            return response.choices[0].message.content
            
        except ImportError:
            logger.warning("[LLMReportGenerator] 未安装openai库")
            raise
        except Exception as e:
            logger.error(f"[LLMReportGenerator] OpenAI API调用失败: {e}")
            raise
    
    def _call_qwen(self, prompt: str) -> str:
        """
        调用通义千问API (DashScope)
        
        文档: https://help.aliyun.com/document_detail/611472.html
        """
        try:
            import http.client
            import json
            
            # DashScope API配置
            api_key = self.api_key
            
            # 构建请求
            conn = http.client.HTTPSConnection("dashscope.aliyuncs.com")
            
            payload = json.dumps({
                "model": self.model,  # 如 "qwen-turbo", "qwen-plus", "qwen-max"
                "input": {
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一位专业的A股短线交易分析师。"
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                },
                "parameters": {
                    "temperature": 0.7,
                    "max_tokens": 3000,
                    "result_format": "message"
                }
            })
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            
            conn.request("POST", "/api/v1/services/aigc/text-generation/generation", payload, headers)
            
            res = conn.getresponse()
            data = res.read()
            result = json.loads(data.decode("utf-8"))
            
            # 解析响应
            if "output" in result and "choices" in result["output"]:
                return result["output"]["choices"][0]["message"]["content"]
            elif "output" in result and "text" in result["output"]:
                return result["output"]["text"]
            else:
                logger.error(f"[LLMReportGenerator] 千问API响应格式异常: {result}")
                raise Exception(f"API响应异常: {result}")
                
        except Exception as e:
            logger.error(f"[LLMReportGenerator] 千问API调用失败: {e}")
            raise
    
    def _generate_fallback_report(self, report_data: Dict) -> str:
        """
        备用报告生成（当LLM不可用时）
        
        Args:
            report_data: 报告数据
            
        Returns:
            基础HTML报告
        """
        # 使用原有的ReportFormatter生成基础报告
        from core.publish.report_formatter import ReportFormatter
        formatter = ReportFormatter()
        return formatter.format_sentiment_report(report_data)


if __name__ == "__main__":
    # 测试代码
    test_data = {
        'date': '20260424',
        'emotion_result': {
            'composite_score': 65,
            'emotion_level': '退潮期',
            'limit_up_count': 59,
            'limit_down_count': 20,
            'max_board_height': 3,
            'description': '空仓或1成试错，禁止接力'
        },
        'mainline_df': None,
        'first_break': [],
        'dragon_pool': [],
        'weakening_pool': []
    }
    
    generator = LLMReportGenerator()
    prompt = generator._build_prompt(test_data)
    print("Prompt预览:")
    print(prompt[:1000] + "...")
