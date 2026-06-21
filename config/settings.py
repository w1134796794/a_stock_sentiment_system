"""
A股短线情绪量化系统 - 主配置文件
所有配置参数集中管理
"""
import os
import sys
from pathlib import Path

# ============================================
# 基础路径配置
# ============================================
# 打包成 exe（PyInstaller，sys.frozen）后，源码被收进归档，工作数据应落在
# exe 所在目录；否则按源码项目根目录解析。这样 webdata / logs / output /
# .env 始终位于用户可见的程序目录旁边。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"

# ============================================
# .env 加载（必须在任何 os.getenv 之前）
# 优先级：os 环境变量 > .env > 代码默认值
# 仅在 python-dotenv 可用时启用；缺失依赖不会破坏现有运行
# ============================================
try:
    from dotenv import load_dotenv  # type: ignore

    _env_file = BASE_DIR / ".env"
    if _env_file.exists():
        load_dotenv(dotenv_path=_env_file, override=False)  # 已设置的 os 环境变量优先
except ImportError:  # pragma: no cover
    pass

# ============================================
# Web / 快照 / 知识库 存储（P0：与 Excel 同源的结构化快照）
# 收盘跑批时，喂给 Excel 的 data_dict 会同步落到这里，供 Web 页面与 KB 复用。
# ============================================
WEB_DATA_DIR = BASE_DIR / "webdata"
SNAPSHOT_DIR = WEB_DATA_DIR / "snapshots"        # 每日整页 JSON 快照
RECAP_DIR = WEB_DATA_DIR / "recaps"              # 复盘短视频分镜脚本（storyboard JSON）
APP_DB_PATH = WEB_DATA_DIR / "app.sqlite"        # 结构化索引（计划/信号/快照）
FACTOR_DB_PATH = WEB_DATA_DIR / "factors.duckdb"  # 因子大表（定量查询，可选）
KB_DB_PATH = WEB_DATA_DIR / "kb.sqlite"          # 知识库块存储（向量 + 词法）
WINRATE_PATH = WEB_DATA_DIR / "winrate_matrix.json"  # 周期×模式胜率矩阵（复盘工具产物）

# ============================================
# 大模型 / 知识库（P2-P3）
# 全部走 OpenAI 兼容 HTTP 接口（用 requests 直连，无需额外 SDK）。
# 单 key 自动识别 provider：
#   - 配 DEEPSEEK_API_KEY（或通用 LLM_API_KEY）→ 对话走 DeepSeek(deepseek-chat)
#   - 配 DASHSCOPE_API_KEY（阿里云通义）→ 对话走 qwen-plus，且嵌入走 text-embedding-v3
# 嵌入仅 DashScope 提供：配了 DASHSCOPE/EMBEDDING key 才启用向量检索，
# 否则知识库自动降级为零依赖中文词法检索（离线可用）。
# 未配置任何 key 时，AI 解读 / 问答返回明确提示，绝不报错。
# 显式 LLM_BASE_URL / LLM_MODEL / EMBEDDING_* 始终优先于自动识别。
# ============================================
def _resolve_llm_config() -> dict:
    llm_key = os.getenv("LLM_API_KEY", "").strip()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()

    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()

    if llm_key or deepseek_key:                      # DeepSeek（或通用）优先
        api_key = llm_key or deepseek_key
        base_url = base_url or "https://api.deepseek.com/v1"
        model = model or "deepseek-chat"
    elif dashscope_key:                              # 阿里云通义
        api_key = dashscope_key
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = model or "qwen-plus"
    else:                                            # 未配置
        api_key = ""
        base_url = base_url or "https://api.deepseek.com/v1"
        model = model or "deepseek-chat"

    # 嵌入：仅 DashScope 体系提供；显式 EMBEDDING_* 优先
    embed_key = os.getenv("EMBEDDING_API_KEY", "").strip() or dashscope_key
    embed_base = os.getenv("EMBEDDING_BASE_URL", "").strip() \
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embed_model = os.getenv("EMBEDDING_MODEL", "").strip() or "text-embedding-v3"

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": int(os.getenv("LLM_TIMEOUT", "60")),
        "embed_api_key": embed_key,
        "embed_base_url": embed_base,
        "embed_model": embed_model,
    }


LLM_CONFIG = _resolve_llm_config()

# ============================================
# API配置
# ============================================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")  # 通过 .env 或环境变量提供

# ============================================
# 行业映射配置
# ============================================
INDUSTRY_MAPPING_FILE = DATA_DIR / "Industry_Mapping.csv"

# ============================================
# 交易日历
# ============================================
TRADE_CALENDAR_FILE = DATA_DIR / "trade_calendar.csv"

# ============================================
# 微信公众号配置
# ============================================
WECHAT_CONFIG = {
    "enabled": True,  # 是否启用公众号发布
    "app_id": os.getenv("WECHAT_APP_ID", ""),  # 公众号AppID
    "app_secret": os.getenv("WECHAT_APP_SECRET", ""),  # 公众号AppSecret
    "author": "A股情绪系统",  # 文章作者
    "preview_wx": "gh_f1c18d75c665",  # 预览微信号（测试用）
    "auto_publish": False,  # 是否自动发布（False则只生成预览）
    
    # LLM配置（用于生成描述性报告）
    "use_llm": True,  # 是否使用LLM生成报告
    "llm_api_key": os.getenv("DASHSCOPE_API_KEY", "your-api-key-here"),  # LLM API密钥
    # 支持的模型：
    # - OpenAI: "gpt-3.5-turbo", "gpt-4"
    # - 通义千问: "qwen-turbo", "qwen-plus", "qwen-max"
    "llm_model": "qwen-turbo",  # LLM模型名称
}

# ============================================
# 同花顺板块追踪器配置 (THSSectorTracker)
# ============================================
THS_SECTOR_CONFIG = {
    # 板块分析参数
    "analyze_sectors": {
        "top_n": 20,  # 默认返回前N个板块
        "use_limit_cpt": True,  # 是否使用limit_cpt_list数据
        "min_member_count": 10,  # 最小成分股数量（过滤小板块）
    },
    
    # 概念/行业差异化参数
    "sector_params": {
        "概念": {
            "min_pct_change": 5.0,      # 概念涨幅阈值更高
            "price_weight": 0.5,         # 概念价格权重更高（追热点）
            "amount_weight": 0.2,        # 概念资金权重更低
            "limit_weight": 0.3,         # 概念涨停权重
            "hot_threshold_pct": 0.15,   # 概念前15%算热点（更严格）
        },
        "行业": {
            "min_pct_change": 3.0,      # 行业涨幅阈值更低
            "price_weight": 0.35,        # 行业价格权重更低
            "amount_weight": 0.35,       # 行业资金权重更高（看资金）
            "limit_weight": 0.3,         # 行业涨停权重
            "hot_threshold_pct": 0.2,    # 行业前20%算热点
        }
    },
    
    # 板块关联分析参数
    "sector_relation": {
        "min_overlap": 0.05,  # 最小重叠度阈值（查找关联板块）
        "default_overlap": 0.1,  # 默认重叠度阈值
    },
    
    # 板块共振分析参数
    "resonance": {
        "top_n": 20,  # 分析前N个板块
        "min_overlap": 0.1,  # 最小重叠度
        "strong_resonance_threshold": 0.3,  # 强共振重叠度阈值
        "medium_resonance_threshold": 0.1,  # 中共振重叠度阈值
    },
    
    # 板块持续性分析参数
    "persistence": {
        "lookback_days": 10,  # 回溯交易日数量（增加历史数据分析天数，更准确判断持续性）
        "hot_threshold_days": 3,  # 判定为持续热门的最少天数
        "top_n": 10,  # 每日热点板块排名阈值
    },
    
    # 板块内部结构分析参数
    "internal_structure": {
        # 梯队完整性评分权重
        "hierarchy_weights": {
            "has_leader": 20,  # 有最高板
            "has_second_board": 20,  # 有2板
            "multiple_second_board": 10,  # 多个2板
            "has_third_plus": 20,  # 有3板及以上
            "first_board_count_3": 20,  # 首板>=3
            "first_board_count_5": 10,  # 首板>=5
        },
        # 龙头股评分
        "leader_score": {
            "space_leader": 10,  # 空间龙头
            "strength_leader": 10,  # 强度龙头
            "time_leader": 10,  # 时间龙头
        },
        # 中军股封单金额阈值（元）
        "mid_cap_min_amount": 100000000,  # 1亿
    },
}

# ============================================
# 因子 profile（Phase 4：Web 可切换）
# 空字符串 = 按当日情绪周期自动选取 profile（默认，行为不变）；
# 非空（如 "退潮期"）= 强制使用该 profile，忽略当日周期，便于回测/手动控盘。
# ============================================
FACTOR_PROFILE_OVERRIDE = ""

# ============================================
# 运行期覆盖（Web 可编辑）
# 必须在所有默认定义之后套用：把 webdata/config_overrides.json 的 settings 作用域
# 覆盖到本模块的全局常量上。首次套用会快照默认值以支持精确重置。
# 仅依赖标准库，失败时静默回退（绝不破坏既有运行）。
# ============================================
try:
    from config.overrides import apply_settings_overrides as _apply_settings_overrides

    _apply_settings_overrides(globals())
except Exception:  # pragma: no cover
    pass
