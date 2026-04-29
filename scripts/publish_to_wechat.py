"""
微信公众号发布脚本

用法：
    python scripts/publish_to_wechat.py --date 20250421 --preview
    python scripts/publish_to_wechat.py --date 20250421 --publish

功能：
1. 读取指定日期的分析报告
2. 生成适合公众号的HTML格式
3. 上传到微信公众号素材库
4. 预览或正式发布
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import WECHAT_CONFIG, OUTPUT_DIR
from core.publish.wechat_publisher import WechatPublisher
from core.publish.report_formatter import ReportFormatter
import pandas as pd
import loguru

logger = loguru.logger


def find_report_file(date: str) -> Path:
    """
    查找指定日期的报告文件
    
    支持两种格式：
    1. 短线情绪分析报告_YYYYMMDD.xlsx
    2. A股情绪分析报告_YYYYMMDD_HHMMSS.xlsx
    
    如果找不到精确匹配，返回该日期最新的报告
    """
    # 首先尝试精确匹配
    report_file = OUTPUT_DIR / f"短线情绪分析报告_{date}.xlsx"
    if report_file.exists():
        return report_file
    
    # 尝试匹配带时间戳的格式
    pattern = f"A股情绪分析报告_{date}_*.xlsx"
    matching_files = list(OUTPUT_DIR.glob(pattern))
    
    if matching_files:
        # 返回最新的文件
        return max(matching_files, key=lambda p: p.stat().st_mtime)
    
    # 如果没有找到，尝试查找最近交易日的报告（包括短线情绪分析报告）
    # 从指定日期往前找，直到找到最近的报告
    all_reports = list(OUTPUT_DIR.glob("A股情绪分析报告_*.xlsx")) + list(OUTPUT_DIR.glob("短线情绪分析报告_*.xlsx"))
    if all_reports:
        # 提取文件名中的日期并排序
        dated_reports = []
        for report in all_reports:
            # 文件名格式: A股情绪分析报告_YYYYMMDD_HHMMSS.xlsx 或 短线情绪分析报告_YYYYMMDD_HHMMSS.xlsx
            name = report.stem  # 去掉扩展名
            parts = name.split('_')
            # 两种格式都是: XXX_YYYYMMDD_HHMMSS，所以日期都是parts[1]
            if len(parts) >= 2:
                try:
                    report_date = parts[1]  # YYYYMMDD
                    if report_date.isdigit() and len(report_date) == 8:
                        dated_reports.append((report_date, report))
                except:
                    continue
        
        if dated_reports:
            # 按日期排序，找最近的（小于等于指定日期的最新报告）
            dated_reports.sort(key=lambda x: x[0], reverse=True)
            
            # 找最近的交易日（小于等于指定日期）
            for report_date, report in dated_reports:
                if report_date <= date:
                    logger.info(f"使用最近交易日报告: {report.name} (日期: {report_date})")
                    return report
            
            # 如果没有找到小于等于的，返回最新的
            latest = dated_reports[0][1]
            logger.warning(f"未找到 {date} 及之前的报告，使用最新报告: {latest.name}")
            return latest
    
    return None


def load_report_data(date: str) -> dict:
    """
    加载指定日期的报告数据
    
    从Excel报告中提取数据
    """
    report_file = find_report_file(date)
    
    if not report_file:
        logger.error(f"未找到任何报告文件，请确认已生成报告")
        return None
    
    try:
        # 读取各个sheet - 使用实际sheet名称
        data = {'date': date}
        
        # 读取市场概览（原情绪概览）
        try:
            emotion_df = pd.read_excel(report_file, sheet_name='市场概览', header=None)
            if not emotion_df.empty:
                # 市场概览sheet是按行组织的，需要特殊解析
                emotion_data = {}
                
                # 遍历所有行查找数据
                for idx, row in emotion_df.iterrows():
                    if pd.notna(row[0]):
                        key = str(row[0]).strip()
                        if key == '涨停家数':
                            emotion_data['limit_up_count'] = row[1] if pd.notna(row[1]) else 0
                        elif key == '跌停家数':
                            emotion_data['limit_down_count'] = row[1] if pd.notna(row[1]) else 0
                        elif key == '炸板率':
                            emotion_data['炸板率'] = row[1] if pd.notna(row[1]) else '0%'
                        elif key == '昨日涨停溢价':
                            emotion_data['昨日涨停溢价'] = row[1] if pd.notna(row[1]) else '0%'
                        elif key == '最高连板高度':
                            emotion_data['max_board_height'] = row[1] if pd.notna(row[1]) else 0
                        elif key == '当前周期':
                            emotion_data['emotion_level'] = row[1] if pd.notna(row[1]) else '未知'
                        elif key == '建议仓位':
                            emotion_data['建议仓位'] = row[1] if pd.notna(row[1]) else '0%'
                        elif key == '操作策略':
                            emotion_data['description'] = row[1] if pd.notna(row[1]) else ''
                
                # 计算综合评分（基于多个指标）
                limit_up = int(emotion_data.get('limit_up_count', 0))
                limit_down = int(emotion_data.get('limit_down_count', 0))
                
                # 简单评分算法：涨停越多、跌停越少，评分越高
                if limit_up + limit_down > 0:
                    score = min(100, max(0, int((limit_up / (limit_up + limit_down + 50)) * 100)))
                else:
                    score = 50
                
                data['emotion_result'] = {
                    'composite_score': score,
                    'emotion_level': emotion_data.get('emotion_level', '未知'),
                    'description': emotion_data.get('description', ''),
                    'limit_up_count': emotion_data.get('limit_up_count', 0),
                    'limit_down_count': emotion_data.get('limit_down_count', 0),
                    'max_board_height': emotion_data.get('max_board_height', 0)
                }
        except Exception as e:
            logger.warning(f"读取市场概览失败: {e}")
        
        # 读取热点概念
        try:
            mainline_df = pd.read_excel(report_file, sheet_name='热点概念')
            data['mainline_df'] = mainline_df
        except Exception as e:
            logger.warning(f"读取热点概念失败: {e}")
            data['mainline_df'] = pd.DataFrame()
        
        # 读取龙头池
        try:
            dragon_df = pd.read_excel(report_file, sheet_name='龙头池')
            data['dragon_pool'] = dragon_df.to_dict('records')
        except Exception as e:
            logger.warning(f"读取龙头池失败: {e}")
            data['dragon_pool'] = []
        
        # 读取走弱池
        try:
            weakening_df = pd.read_excel(report_file, sheet_name='走弱池')
            data['weakening_pool'] = weakening_df.to_dict('records')
        except Exception as e:
            logger.warning(f"读取走弱池失败: {e}")
            data['weakening_pool'] = []
        
        # 读取首板突破（次日可关注标的）
        try:
            first_break_df = pd.read_excel(report_file, sheet_name='首板突破')
            data['first_break'] = first_break_df.to_dict('records')
        except Exception as e:
            logger.warning(f"读取首板突破失败: {e}")
            data['first_break'] = []
        
        logger.info(f"成功加载报告数据: {date}")
        return data
        
    except Exception as e:
        logger.error(f"加载报告数据失败: {e}")
        return None


def publish_report(date: str, preview: bool = True, preview_wx: str = None):
    """
    发布报告到微信公众号
    
    Args:
        date: 报告日期 (YYYYMMDD)
        preview: True=预览模式, False=正式发布
        preview_wx: 预览微信号
    """
    # 检查配置
    if not WECHAT_CONFIG.get('enabled'):
        logger.info("微信公众号发布未启用，仅生成HTML报告文件")
        # 仍然生成HTML文件，方便手动复制到公众号
        generate_html_only = True
    else:
        generate_html_only = False
    
    app_id = WECHAT_CONFIG.get('app_id')
    app_secret = WECHAT_CONFIG.get('app_secret')
    
    if not app_id or not app_secret:
        logger.error("未配置微信公众号AppID或AppSecret")
        logger.info("请设置环境变量 WECHAT_APP_ID 和 WECHAT_APP_SECRET")
        logger.info("或在 config/settings.py 中直接配置")
        return False
    
    # 加载报告数据
    report_data = load_report_data(date)
    if not report_data:
        return False
    
    # 检查是否使用LLM生成报告
    use_llm = WECHAT_CONFIG.get('use_llm', False)
    llm_api_key = WECHAT_CONFIG.get('llm_api_key')
    
    if use_llm and llm_api_key:
        logger.info("使用LLM生成描述性报告...")
        try:
            from core.publish.llm_report_generator import LLMReportGenerator
            llm_model = WECHAT_CONFIG.get('llm_model', 'qwen-turbo')
            llm_generator = LLMReportGenerator(api_key=llm_api_key, model=llm_model)
            html_content = llm_generator.generate_report(report_data)
            logger.info("✓ LLM报告生成完成")
        except Exception as e:
            logger.warning(f"LLM报告生成失败，使用默认格式: {e}")
            # 降级为默认格式
            formatter = ReportFormatter()
            html_content = formatter.format_sentiment_report(report_data)
    else:
        # 使用默认格式
        formatter = ReportFormatter()
        html_content = formatter.format_sentiment_report(report_data)
    
    # 保存HTML文件
    html_file = OUTPUT_DIR / f"公众号报告_{date}.html"
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    logger.info(f"HTML报告已保存: {html_file}")
    
    # 如果未启用公众号功能，仅生成HTML文件
    if generate_html_only:
        logger.info("✓ HTML报告已生成，可以手动复制到公众号编辑器")
        logger.info(f"文件路径: {html_file}")
        return True
    
    # 检查AppID和AppSecret
    if not app_id or not app_secret:
        logger.error("未配置微信公众号AppID或AppSecret")
        logger.info("请设置环境变量 WECHAT_APP_ID 和 WECHAT_APP_SECRET")
        logger.info("或在 config/settings.py 中直接配置")
        return False
    
    # 初始化发布器
    publisher = WechatPublisher(
        app_id=app_id,
        app_secret=app_secret,
        token_cache_file="data/wechat_token.json"
    )
    
    # 发布
    title = f"情绪分析报告 - {date[:4]}年{date[4:6]}月{date[6:]}日"
    author = WECHAT_CONFIG.get('author', 'A股情绪系统')
    
    if preview:
        wx_id = preview_wx or WECHAT_CONFIG.get('preview_wx')
        if not wx_id:
            logger.error("未配置预览微信号，请设置 WECHAT_CONFIG['preview_wx']")
            return False
        
        logger.info(f"正在发送预览给: {wx_id}")
        result = publisher.publish_report(
            title=title,
            content=html_content,
            author=author,
            preview_wx=wx_id
        )
        
        if result:
            logger.info("✓ 预览消息已发送，请查看微信")
        else:
            logger.error("✗ 预览消息发送失败")
        
        return result
    else:
        # 正式发布
        logger.info("正在正式发布...")
        result = publisher.publish_report(
            title=title,
            content=html_content,
            author=author
        )
        
        if result:
            logger.info("✓ 报告已正式发布到公众号")
        else:
            logger.error("✗ 发布失败")
        
        return result


def publish_report_draft_only(date: str) -> bool:
    """
    仅生成草稿，不发送预览
    
    用于JS接口安全域名未配置时，生成草稿后可在公众号后台手动预览
    
    Args:
        date: 报告日期 (YYYYMMDD)
        
    Returns:
        是否成功
    """
    # 检查配置
    app_id = WECHAT_CONFIG.get('app_id')
    app_secret = WECHAT_CONFIG.get('app_secret')
    
    if not app_id or not app_secret:
        logger.error("未配置微信公众号AppID或AppSecret")
        return False
    
    # 加载报告数据
    report_data = load_report_data(date)
    if not report_data:
        return False
    
    # 检查是否使用LLM生成报告
    use_llm = WECHAT_CONFIG.get('use_llm', False)
    llm_api_key = WECHAT_CONFIG.get('llm_api_key')
    
    if use_llm and llm_api_key:
        logger.info("使用LLM生成描述性报告...")
        try:
            from core.publish.llm_report_generator import LLMReportGenerator
            llm_model = WECHAT_CONFIG.get('llm_model', 'qwen-turbo')
            llm_generator = LLMReportGenerator(api_key=llm_api_key, model=llm_model)
            html_content = llm_generator.generate_report(report_data)
            logger.info("✓ LLM报告生成完成")
        except Exception as e:
            logger.warning(f"LLM报告生成失败，使用默认格式: {e}")
            formatter = ReportFormatter()
            html_content = formatter.format_sentiment_report(report_data)
    else:
        formatter = ReportFormatter()
        html_content = formatter.format_sentiment_report(report_data)
    
    # 保存HTML文件
    html_file = OUTPUT_DIR / f"公众号报告_{date}.html"
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    logger.info(f"HTML报告已保存: {html_file}")
    
    # 初始化发布器
    publisher = WechatPublisher(
        app_id=app_id,
        app_secret=app_secret,
        token_cache_file="data/wechat_token.json"
    )
    
    # 生成草稿
    title = f"情绪分析报告 - {date[:4]}年{date[4:6]}月{date[6:]}日"
    author = WECHAT_CONFIG.get('author', 'A股情绪系统')
    
    logger.info("正在生成草稿...")
    media_id = publisher.publish_report(
        title=title,
        content=html_content,
        author=author,
        preview_wx=None  # 不发送预览，只生成草稿
    )
    
    if media_id:
        logger.info(f"✓ 草稿已生成: {media_id}")
        logger.info("请登录公众号后台 → 内容与互动 → 草稿箱 → 找到草稿进行预览")
        return True
    else:
        logger.error("✗ 草稿生成失败")
        return False


def main():
    parser = argparse.ArgumentParser(description='发布A股情绪分析报告到微信公众号')
    parser.add_argument('--date', type=str, default=datetime.now().strftime('%Y%m%d'),
                       help='报告日期 (YYYYMMDD)，默认今天')
    parser.add_argument('--preview', action='store_true',
                       help='预览模式（发送给指定微信号预览）')
    parser.add_argument('--publish', action='store_true',
                       help='正式发布（群发给所有粉丝）')
    parser.add_argument('--draft-only', action='store_true',
                       help='仅生成草稿，不发送预览（用于JS接口安全域名未配置时）')
    parser.add_argument('--wx', type=str, default=None,
                       help='预览微信号（覆盖配置中的preview_wx）')
    
    args = parser.parse_args()
    
    if not args.preview and not args.publish and not args.draft_only:
        # 默认预览模式
        args.preview = True
    
    if args.draft_only:
        # 仅生成草稿模式
        success = publish_report_draft_only(
            date=args.date
        )
    elif args.publish and not WECHAT_CONFIG.get('auto_publish'):
        # 安全确认
        confirm = input("⚠️  确认要正式发布吗？粉丝将收到推送。(yes/no): ")
        if confirm.lower() != 'yes':
            logger.info("已取消发布")
            return
        
        success = publish_report(
            date=args.date,
            preview=False,
            preview_wx=args.wx
        )
    else:
        success = publish_report(
            date=args.date,
            preview=args.preview,
            preview_wx=args.wx
        )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
