"""
弱转强策略 - 动态龙头跟踪版本
核心：实时跟踪龙头候选股，确认走弱后监控转强信号，当日发现当日决策

龙头定义：
- 连板龙头：连续涨停，连板数>=4
- 趋势龙头：近10日累计涨幅>=40%，且期间至少2个涨停

使用方式：
1. 每日更新：调用update_dragon_pools()更新龙头候选池和走弱池
2. 盘中监控：调用monitor_intraday()实时监控转强信号
3. 生成报告：调用generate_report()输出Excel决策报告
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
import json
import loguru
import re

from core.utils import (
    time_to_minutes,
    minutes_from_market_open,
    calculate_gap,
    calculate_drawdown,
    is_time_after,
    is_late_board,
    is_lanban,
    calculate_stop_loss,
    calculate_take_profit,
    DataFrameFieldMapper,
    StockCodeUtils,
)
from config.pattern_params import get_params

logger = loguru.logger


class PatternType(Enum):
    WEAK_TO_STRONG = "弱转强"


class DragonType(Enum):
    CONTINUOUS = "连板龙头"  # 连续涨停
    TREND = "趋势龙头"       # 趋势上涨（间断涨停）
    SPACE = "空间龙头"       # 高连板回撤后的二波机会


class DragonStatus(Enum):
    MONITORING = "观察中"    # 刚入池，正在观察
    WEAKENING = "已走弱"     # 确认走弱，等待转强
    RECOVERING = "转强中"    # 出现转强信号
    EXPIRED = "已过期"       # 超过观察期或A杀


@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str
    reason: str
    key_metrics: Dict
    validation_rules: List[str]
    buy_timing: str = ""


@dataclass
class DragonCandidate:
    """龙头候选股信息"""
    stock_code: str
    stock_name: str
    dragon_type: DragonType      # 龙头类型
    peak_board_height: int       # 最高连板数（连板龙头）
    peak_price: float            # 最高点价格
    peak_date: str               # 达到最高点日期
    entry_date: str              # 入池日期
    sector_name: str             # 所属板块
    
    # 涨幅统计（用于趋势龙头）
    total_rise_10d: float        # 近10日累计涨幅
    limit_up_count: int          # 期间涨停次数
    
    # 状态管理
    status: DragonStatus = DragonStatus.MONITORING
    status_change_date: str = ""  # 状态变更日期
    
    # 走弱确认数据
    weakening_data: Dict = field(default_factory=dict)
    
    # 观察期限
    max_monitor_days: int = 5    # 最多观察5天
    
    def to_dict(self) -> Dict:
        """转换为字典，用于Excel输出"""
        return {
            "代码": self.stock_code,
            "名称": self.stock_name,
            "龙头类型": self.dragon_type.value,
            "最高连板": self.peak_board_height,
            "最高点": f"{self.peak_price:.2f}",
            "见顶日期": self.peak_date,
            "入池日期": self.entry_date,
            "所属板块": self.sector_name,
            "10日涨幅": f"{self.total_rise_10d*100:.1f}%",
            "涨停次数": self.limit_up_count,
            "当前状态": self.status.value,
            "状态变更日": self.status_change_date,
            "观察天数": self._get_monitor_days(),
        }
    
    def _get_monitor_days(self) -> int:
        """计算已观察天数"""
        try:
            entry = datetime.strptime(self.entry_date, "%Y%m%d")
            today = datetime.now()
            return (today - entry).days
        except:
            return 0


@dataclass
class WeakeningDragon(DragonCandidate):
    """已确认走弱的龙头（继承DragonCandidate）"""
    
    # 走弱确认信息
    weakening_date: str = ""           # 确认走弱日期
    weakening_type: str = ""           # 走弱类型：烂板/断板/尾盘板/放量滞涨/趋势回调
    weakening_price: float = 0.0       # 走弱时价格
    
    # 回调监控
    current_price: float = 0.0         # 当前价格
    max_drawdown: float = 0.0          # 最大回调幅度
    
    # 转强观察信号阈值
    recovery_signals: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """转换为字典，用于Excel输出"""
        base = super().to_dict()
        base.update({
            "走弱日期": self.weakening_date,
            "走弱类型": self.weakening_type,
            "走弱价格": f"{self.weakening_price:.2f}",
            "当前价格": f"{self.current_price:.2f}",
            "回调幅度": f"{self.max_drawdown*100:.1f}%",
            "观察信号": self._format_signals(),
        })
        return base
    
    def _format_signals(self) -> str:
        """格式化观察信号"""
        if not self.recovery_signals:
            return "高开>3%, 竞价量>10%"
        signals = []
        if 'min_gap' in self.recovery_signals:
            signals.append(f"高开>{self.recovery_signals['min_gap']*100:.0f}%")
        if 'min_auction_vol_ratio' in self.recovery_signals:
            signals.append(f"竞价量>{self.recovery_signals['min_auction_vol_ratio']*100:.0f}%")
        return ", ".join(signals) if signals else "观察竞价异动"


@dataclass
class RecoverySignal:
    """转强确认信号"""
    stock_code: str
    stock_name: str
    signal_time: str           # 信号触发时间
    signal_type: str           # 信号类型：竞价转强/开盘转强/盘中转强
    
    # 竞价数据
    gap_pct: float             # 高开幅度
    auction_vol_ratio: float   # 竞价量占比
    auction_amount: float      # 竞价金额
    
    # 开盘数据
    open_price: float
    open_change_pct: float     # 开盘涨幅
    
    # 确认数据
    confirmed: bool = False
    confirmation_time: str = ""
    confirmation_price: float = 0.0
    
    # 交易建议
    suggested_entry: float = 0.0
    confidence: float = 0.0
    
    def to_dict(self) -> Dict:
        """转换为字典，用于Excel输出"""
        return {
            "代码": self.stock_code,
            "名称": self.stock_name,
            "信号时间": self.signal_time,
            "信号类型": self.signal_type,
            "高开幅度": f"{self.gap_pct*100:.1f}%",
            "竞价量占比": f"{self.auction_vol_ratio*100:.1f}%",
            "竞价金额": f"{self.auction_amount/10000:.0f}万",
            "开盘涨幅": f"{self.open_change_pct*100:.1f}%",
            "是否确认": "是" if self.confirmed else "否",
            "确认时间": self.confirmation_time,
            "建议买入": f"{self.suggested_entry:.2f}" if self.suggested_entry else "-",
            "置信度": f"{self.confidence*100:.0f}%",
        }


class WeakToStrongStrategy:
    """弱转强策略 - 动态跟踪版本"""
    
    def __init__(self, data_manager, sector_engine=None, pool_file: str = None, repo=None):
        self.dm = data_manager
        self.se = sector_engine
        if repo is None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(data_manager)
        self.repo = repo
        
        # 池子文件路径
        self.pool_file = pool_file or "dragon_pools.json"
        
        # 龙头候选池：正在观察的龙头
        self.dragon_pool: Dict[str, DragonCandidate] = {}
        
        # 走弱池：确认走弱，等待转强
        self.weakening_pool: Dict[str, WeakeningDragon] = {}
        
        # 转强信号池：当日发现的转强信号
        self.recovery_signals: List[RecoverySignal] = []
        
        # ========== 参数配置（默认值见 config/pattern_params.py，支持网页覆盖）==========
        self.params = get_params("weak_to_strong")
        
        # 弹性评分缓存
        self._flexible_score_cache = {}
        self._market_sentiment = "neutral"  # 当前市场情绪
        
        # 加载已有池子
        self._load_pools()
    
    def _get_code_col(self, df: pd.DataFrame) -> str:
        """获取代码列名，处理不同命名 - 使用统一的字段映射工具"""
        return DataFrameFieldMapper.get_code_column(df)
    
    def _get_name_col(self, df: pd.DataFrame) -> str:
        """获取名称列名，处理不同命名 - 使用统一的字段映射工具"""
        return DataFrameFieldMapper.get_name_column(df)
    
    def _parse_enum(self, enum_class, value):
        """解析枚举值，支持多种格式"""
        if value is None:
            return None
        if isinstance(value, enum_class):
            return value
        if isinstance(value, str):
            # 处理格式如 "DragonType.TREND" 或 "TREND"
            if '.' in value:
                value = value.split('.')[-1]
            try:
                return enum_class[value]
            except KeyError:
                # 尝试通过值查找
                for member in enum_class:
                    if member.value == value:
                        return member
        return None
    
    # ==================== 池子管理 ====================
    
    def _load_pools(self):
        """从文件加载池子数据"""
        try:
            if Path(self.pool_file).exists():
                with open(self.pool_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 加载龙头池
                for code, item in data.get('dragon_pool', {}).items():
                    # 转换枚举类型
                    item['dragon_type'] = self._parse_enum(DragonType, item.get('dragon_type'))
                    item['status'] = self._parse_enum(DragonStatus, item.get('status'))
                    self.dragon_pool[code] = DragonCandidate(**item)
                
                # 加载走弱池
                for code, item in data.get('weakening_pool', {}).items():
                    # 转换枚举类型
                    item['dragon_type'] = self._parse_enum(DragonType, item.get('dragon_type'))
                    item['status'] = self._parse_enum(DragonStatus, item.get('status'))
                    self.weakening_pool[code] = WeakeningDragon(**item)
                
                logger.info(f"[弱转强] 加载池子: 龙头池{len(self.dragon_pool)}只, 走弱池{len(self.weakening_pool)}只")
        except Exception as e:
            logger.warning(f"[弱转强] 加载池子失败: {e}")
    
    def _save_pools(self):
        """保存池子数据到文件"""
        try:
            data = {
                'dragon_pool': {code: dc.__dict__ for code, dc in self.dragon_pool.items()},
                'weakening_pool': {code: wd.__dict__ for code, wd in self.weakening_pool.items()},
                'update_time': datetime.now().strftime('%Y%m%d %H:%M:%S')
            }
            
            with open(self.pool_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"[弱转强] 保存池子失败: {e}")
    
    # ==================== 盘中实时观测（只读走弱池，不回写池子）====================

    @staticmethod
    def _normalize_pct_threshold(value, default: float = 0.07) -> float:
        """归一化涨幅阈值：支持 0.07 / 7 / "7%" 等写法，>1 视为百分数。"""
        try:
            if isinstance(value, str):
                value = value.strip().replace('%', '')
            v = float(value)
        except (TypeError, ValueError):
            return default
        if v <= 0:
            return default
        return v / 100.0 if v > 1 else v

    @staticmethod
    def _classify_recovery_time(now: datetime) -> str:
        """按当前时间给转强信号打标签：竞价转强 / 开盘转强 / 盘中转强。"""
        t = now.time()
        if t <= time(9, 25):
            return "竞价转强"
        if t <= time(9, 35):
            return "开盘转强"
        return "盘中转强"

    def scan_weakening_intraday(self, date_str: str = None, threshold=None,
                                with_trend: bool = True, trend_limit: int = 50) -> Dict:
        """盘中实时观测走弱池：批量取 eltdx 实时快照，判断是否转强。

        判据（v1，仅涨幅）：以**昨收**为基准，盘中涨幅 ≥ ``threshold`` 即视为转强。
        本方法**只读走弱池**，不修改 ``dragon_pools.json``，也不写盘后快照。

        Args:
            date_str: 交易日 YYYYMMDD（默认今天，仅用于结果标注）。
            threshold: 涨幅阈值，缺省取 ``weak_to_strong.intraday_recovery_pct``；
                       支持 0.07 / 7 / "7%" 写法。

        Returns:
            Dict: {
                'date', 'time', 'threshold', 'pool_size',
                'quotes_ok', 'errors',
                'hits':     List[Dict]  # 命中转强（涨幅≥阈值），按涨幅降序
                'observed': List[Dict]  # 全部成功取价的观测，按涨幅降序
            }
        """
        now = datetime.now()
        date_str = date_str or now.strftime("%Y%m%d")
        thr = self._normalize_pct_threshold(
            threshold if threshold is not None else self.params.get("intraday_recovery_pct", 0.07),
            default=0.07,
        )
        time_str = now.strftime("%H:%M:%S")

        result = {
            "date": date_str,
            "time": time_str,
            "threshold": thr,
            "pool_size": len(self.weakening_pool),
            "quotes_ok": 0,
            "errors": [],
            "hits": [],
            "observed": [],
        }

        if not self.weakening_pool:
            logger.info("[弱转强-盘中] 走弱池为空，无可观测标的")
            return result

        logger.info(f"[弱转强-盘中] 开始观测走弱池 {len(self.weakening_pool)} 只，转强阈值={thr*100:.1f}%")

        # 批量取价（一条连接，数十只仅百毫秒级）；批量缺某只时再逐只兜底
        batch_quotes = {}
        codes = list(self.weakening_pool.keys())
        if self.dm is not None and hasattr(self.dm, "get_quote_snapshots"):
            try:
                batch_quotes = self.dm.get_quote_snapshots(codes) or {}
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[弱转强-盘中] 批量取快照失败: {e}")
                batch_quotes = {}
        logger.info(f"[弱转强-盘中] 批量取价命中 {len(batch_quotes)}/{len(codes)} 只")

        for code, wd in list(self.weakening_pool.items()):
            code6 = str(code).split(".")[0].zfill(6)
            quote = batch_quotes.get(code6, {})
            if not quote and self.dm is not None and hasattr(self.dm, "get_quote_snapshot"):
                try:
                    quote = self.dm.get_quote_snapshot(code) or {}
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[弱转强-盘中] 取快照失败 {code}: {e}")
                    quote = {}

            last_price = float(quote.get("last_price") or 0)
            pre_close = float(quote.get("pre_close") or 0)
            open_price = float(quote.get("open_price") or 0)
            if last_price <= 0 or pre_close <= 0:
                result["errors"].append(code)
                logger.debug(f"[弱转强-盘中] {wd.stock_name}({code}) 无有效实时行情，跳过")
                continue

            result["quotes_ok"] += 1
            pct_chg = (last_price - pre_close) / pre_close
            open_change_pct = (open_price - pre_close) / pre_close if open_price > 0 else 0.0
            monitor_days = self._safe_monitor_days(wd.weakening_date, date_str)

            obs = {
                "code": code,
                "name": wd.stock_name,
                "weakening_type": wd.weakening_type,
                "weakening_date": wd.weakening_date,
                "monitor_days": monitor_days,
                "last_price": round(last_price, 2),
                "pre_close": round(pre_close, 2),
                "open_price": round(open_price, 2),
                "pct_chg": round(pct_chg, 4),
                "open_change_pct": round(open_change_pct, 4),
                "is_recovery": pct_chg >= thr,
                "time": time_str,
            }
            result["observed"].append(obs)

            if pct_chg >= thr:
                signal_type = self._classify_recovery_time(now)
                signal = RecoverySignal(
                    stock_code=code,
                    stock_name=wd.stock_name,
                    signal_time=time_str,
                    signal_type=signal_type,
                    gap_pct=0.0,
                    auction_vol_ratio=0.0,
                    auction_amount=0.0,
                    open_price=open_price,
                    open_change_pct=open_change_pct,
                    confirmed=True,
                    confirmation_time=time_str,
                    confirmation_price=last_price,
                    suggested_entry=last_price,
                    confidence=0.0,
                )
                hit = dict(obs)
                hit["signal_type"] = signal_type
                hit["signal"] = signal.to_dict()
                result["hits"].append(hit)
                logger.info(
                    f"[弱转强-盘中] ✓ 转强 {wd.stock_name}({code}) "
                    f"涨幅{pct_chg*100:.1f}% ≥ {thr*100:.1f}% [{signal_type}]"
                )

        result["hits"].sort(key=lambda x: x["pct_chg"], reverse=True)
        result["observed"].sort(key=lambda x: x["pct_chg"], reverse=True)

        # 对命中的转强票补充「分时走强」判定（仅命中集合，控制额外耗时）
        if with_trend and result["hits"]:
            for hit in result["hits"][:max(0, int(trend_limit))]:
                hit["trend"] = self._intraday_trend(hit["code"], date_str)
            for hit in result["hits"][max(0, int(trend_limit)):]:
                hit["trend"] = {"label": "未计算", "above_avg": None, "near_high": None, "slope_up": None}

        logger.info(
            f"[弱转强-盘中] 观测完成：取价{result['quotes_ok']}/{result['pool_size']}，"
            f"转强命中{len(result['hits'])}只"
        )
        return result

    def _intraday_trend(self, code: str, date_str: str) -> Dict:
        """基于实时分时判定「分时走强/震荡/走弱」。

        判据：站上分时均价线 + 处于日内价格高位(>=60%分位) + 近段(末30点)上行。
        三者同时满足→走强；明显失守→走弱；其余→震荡。数据不足返回「数据不足」。
        """
        empty = {"label": "数据不足", "above_avg": None, "near_high": None,
                 "slope_up": None, "last": None, "avg": None}
        df = None
        if self.dm is not None and hasattr(self.dm, "get_minute_bars_live"):
            try:
                df = self.dm.get_minute_bars_live(code, date_str)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[弱转强-盘中] 取分时失败 {code}: {e}")
                df = None
        if df is None or df.empty or "close" not in df.columns:
            return empty
        try:
            closes = pd.to_numeric(df["close"], errors="coerce").dropna()
            if len(closes) < 5:
                return empty
            last = float(closes.iloc[-1])
            day_high = float(closes.max())
            day_low = float(closes.min())
            rng = day_high - day_low
            pos = (last - day_low) / rng if rng > 0 else 1.0  # 日内价格分位
            near_high = ((day_high - last) / day_high) if day_high > 0 else None  # 距日内高点

            avg_last = None
            if "avg_price" in df.columns:
                avg = pd.to_numeric(df["avg_price"], errors="coerce").dropna()
                if len(avg):
                    avg_last = float(avg.iloc[-1])
            above_avg = (last >= avg_last) if avg_last else None

            tail = closes.tail(min(30, len(closes)))
            slope_up = bool(tail.iloc[-1] >= tail.iloc[0])

            if (above_avg is not False) and pos >= 0.6 and slope_up:
                label = "分时走强"
            elif (above_avg is False) and pos <= 0.4:
                label = "分时走弱"
            else:
                label = "分时震荡"
            return {
                "label": label,
                "above_avg": above_avg,
                "near_high": round(near_high, 4) if near_high is not None else None,
                "slope_up": slope_up,
                "pos": round(pos, 3),
                "last": round(last, 2),
                "avg": round(avg_last, 2) if avg_last else None,
            }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[弱转强-盘中] 分时形态计算失败 {code}: {e}")
            return empty

    @staticmethod
    def _safe_monitor_days(weakening_date: str, date_str: str) -> int:
        """走弱至今的观察天数（自然日），解析失败返回 0。"""
        try:
            return (datetime.strptime(date_str, "%Y%m%d") -
                    datetime.strptime(weakening_date, "%Y%m%d")).days
        except Exception:  # noqa: BLE001
            return 0

    # ==================== 阶段1：龙头识别与入池 ====================
    
    def update_dragon_pools(self, 
                           today_zt: pd.DataFrame,
                           today_tick: Dict[str, pd.DataFrame],
                           history_pools: Dict[str, pd.DataFrame],
                           date_str: str,
                           today_daily: pd.DataFrame = None,
                           stock_to_ths_industry: Dict[str, str] = None,
                           stock_to_ths_concept: Dict[str, str] = None) -> Dict:
        """
        每日更新龙头池和走弱池
        
        Args:
            today_zt: 当日涨停数据
            today_tick: 当日tick数据
            history_pools: 历史涨停池数据
            date_str: 日期字符串
            today_daily: 当日全市场日线数据（用于更新走弱池价格）
        
        Returns:
            Dict: {
                'new_dragons': List[DragonCandidate],  # 新识别的龙头
                'weakened': List[WeakeningDragon],     # 新确认走弱的
                'recovered': List[RecoverySignal],     # 确认转强的
                'expired': List[str]                   # 过期的股票代码
            }
        """
        results = {
            'new_dragons': [],
            'weakened': [],
            'recovered': [],
            'expired': []
        }
        
        logger.info(f"[弱转强] 开始更新龙头池，日期: {date_str}")
        logger.info(f"[弱转强] 更新前状态: 龙头池{len(self.dragon_pool)}只, 走弱池{len(self.weakening_pool)}只")
        
        # 1. 识别新龙头并入池
        logger.info(f"[弱转强] ========== 阶段1: 识别新龙头 ==========")
        new_dragons = self._identify_new_dragons(
            today_zt, history_pools, date_str,
            stock_to_ths_industry=stock_to_ths_industry or {},
            stock_to_ths_concept=stock_to_ths_concept or {}
        )
        for dragon in new_dragons:
            if dragon.stock_code not in self.dragon_pool:
                self.dragon_pool[dragon.stock_code] = dragon
                results['new_dragons'].append(dragon)
                logger.info(f"[弱转强] 新龙头入池: {dragon.stock_name}({dragon.stock_code}) - {dragon.dragon_type.value}")
        
        # 2. 检查已有龙头是否走弱
        logger.info(f"[弱转强] ========== 阶段2: 检查龙头走弱 ==========")
        logger.info(f"[弱转强] 检查{len(self.dragon_pool)}只龙头是否走弱")
        for code, dragon in list(self.dragon_pool.items()):
            weakening = self._check_weakening(dragon, today_zt, today_tick.get(code, pd.DataFrame()), date_str)
            if weakening:
                # 转移到走弱池
                self.weakening_pool[code] = weakening
                del self.dragon_pool[code]
                results['weakened'].append(weakening)
                logger.info(f"[弱转强] 确认走弱: {weakening.stock_name}({code}) - {weakening.weakening_type}")
        
        # 3. 更新走弱池中的股票数据
        logger.info(f"[弱转强] ========== 阶段3: 更新走弱池 ==========")
        logger.info(f"[弱转强] 更新{len(self.weakening_pool)}只走弱股票数据")
        
        # 获取代码列名
        daily_code_col = self._get_code_col(today_daily) if today_daily is not None else None
        zt_code_col = self._get_code_col(today_zt) if today_zt is not None else None
        
        for code, weakening in list(self.weakening_pool.items()):
            # 更新当前价格 - 优先从全市场日线数据获取，其次从涨停池获取，最后通过data_manager获取
            current_price = 0
            if today_daily is not None and not today_daily.empty and daily_code_col:
                # 从全市场日线数据查找
                # 处理不同格式的代码列（如 ts_code: 002787.SZ -> 002787）
                daily_codes = today_daily[daily_code_col].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
                stock_row = today_daily[daily_codes == code]
                if not stock_row.empty:
                    current_price = stock_row.iloc[0].get('最新价', 0)
                    if current_price == 0:
                        current_price = stock_row.iloc[0].get('收盘', 0)
                    if current_price == 0:
                        current_price = stock_row.iloc[0].get('close', 0)
            
            # 如果在日线数据中没找到，尝试从涨停池查找
            if current_price == 0 and zt_code_col:
                # 处理不同格式的代码列
                zt_codes = today_zt[zt_code_col].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
                stock_row = today_zt[zt_codes == code]
                if not stock_row.empty:
                    current_price = stock_row.iloc[0].get('最新价', 0)
            
            # 如果仍然没有找到价格，通过data_manager获取该股票的日线数据
            if current_price == 0 and self.dm is not None:
                try:
                    # 获取该股票当日的日线数据
                    stock_daily = self.repo.get_stock_daily(code, date_str, date_str)
                    if stock_daily is not None and not stock_daily.empty:
                        current_price = stock_daily.iloc[0].get('close', 0)
                        if current_price == 0:
                            current_price = stock_daily.iloc[-1].get('close', 0)
                        logger.debug(f"[弱转强] 通过data_manager获取{weakening.stock_name}({code})价格: {current_price}")
                except Exception as e:
                    logger.debug(f"[弱转强] 通过data_manager获取价格失败 {code}: {e}")
            
            if current_price > 0:
                prev_close = weakening.current_price  # 昨日收盘价（Stage 3 在上次执行时写入的值）
                yesterday_drawdown = (weakening.peak_price - prev_close) / weakening.peak_price if prev_close > 0 else 0
                weakening.max_drawdown = max(weakening.max_drawdown, yesterday_drawdown)
                weakening.current_price = current_price  # 今日收盘价（明天用作"昨日"收盘）
                logger.info(f"[弱转强] 更新{weakening.stock_name}({code}) 昨收={prev_close:.2f}→今收={current_price:.2f}, 历史最大回调{weakening.max_drawdown*100:.1f}%")
            else:
                logger.warning(f"[弱转强] 无法获取{weakening.stock_name}({code})的当前价格")
            
            # 检查是否A杀（回调超过15%）
            if weakening.max_drawdown > self.params['max_drawdown_for_recovery']:
                weakening.status = DragonStatus.EXPIRED
                results['expired'].append(code)
                logger.info(f"[弱转强] A杀过期: {weakening.stock_name}({code}) 回调{weakening.max_drawdown*100:.1f}%")
                del self.weakening_pool[code]
                continue
            
            # 检查是否超过观察期
            monitor_days = (datetime.strptime(date_str, "%Y%m%d") - 
                          datetime.strptime(weakening.weakening_date, "%Y%m%d")).days
            if monitor_days > self.params['max_monitor_days']:
                weakening.status = DragonStatus.EXPIRED
                results['expired'].append(code)
                logger.info(f"[弱转强] 观察期过期: {weakening.stock_name}({code})")
                del self.weakening_pool[code]
        
        # 4. 清理过期的龙头池股票
        logger.info(f"[弱转强] ========== 阶段4: 清理过期股票 ==========")
        expired_from_pool = 0
        for code, dragon in list(self.dragon_pool.items()):
            monitor_days = (datetime.strptime(date_str, "%Y%m%d") - 
                          datetime.strptime(dragon.entry_date, "%Y%m%d")).days
            if monitor_days > self.params['max_monitor_days']:
                dragon.status = DragonStatus.EXPIRED
                results['expired'].append(code)
                expired_from_pool += 1
                logger.info(f"[弱转强] 龙头池过期: {dragon.stock_name}({code}) 观察{monitor_days}天")
                del self.dragon_pool[code]
        logger.info(f"[弱转强] 清理完成: 龙头池过期{expired_from_pool}只")
        
        # 保存池子
        self._save_pools()
        
        logger.info(f"[弱转强] ========== 更新完成 ==========")
        logger.info(f"[弱转强] 结果统计: 新龙头{len(results['new_dragons'])}只, "
                   f"走弱{len(results['weakened'])}只, 过期{len(results['expired'])}只")
        logger.info(f"[弱转强] 当前池子: 龙头池{len(self.dragon_pool)}只, 走弱池{len(self.weakening_pool)}只")
        
        return results
    
    def _identify_new_dragons(self, 
                             today_zt: pd.DataFrame,
                             history_pools: Dict[str, pd.DataFrame],
                             date_str: str,
                             stock_to_ths_industry: Dict[str, str] = None,
                             stock_to_ths_concept: Dict[str, str] = None) -> List[DragonCandidate]:
        """
        识别新的龙头候选股
        
        包括：
        1. 连板龙头：当日涨停且连板数>=4
        2. 趋势龙头（当日涨停）：当日涨停，近10日涨幅>=40%，至少2个涨停
        3. 趋势龙头（趋势中）：近5日有过涨停，今日未涨停但仍在趋势中，近10日涨幅>=40%
        """
        new_dragons = []
        
        logger.info(f"[弱转强-龙头识别] ========== 开始识别龙头 ==========")
        logger.info(f"[弱转强-龙头识别] 连板龙头标准: >={self.params['min_board_height']}板")
        logger.info(f"[弱转强-龙头识别] 趋势龙头标准: 近10日涨幅>={self.params['min_total_rise']*100:.0f}%，且>={self.params['min_limit_up_count']}个涨停")
        
        continuous_count = 0
        trend_count = 0
        
        # ========== 第1步：从当日涨停池中识别连板龙和当日涨停的趋势龙 ==========
        if not today_zt.empty:
            logger.info(f"[弱转强-龙头识别] 从当日涨停池({len(today_zt)}只)中识别...")
            
            for _, row in today_zt.iterrows():
                code = str(row.get('代码', '')).zfill(6)
                name = row.get('名称', '')
                board_height = row.get('连板数', 0)
                ths_ind = (stock_to_ths_industry or {}).get(code, '')
                ths_con = (stock_to_ths_concept or {}).get(code, '')
                sector = ths_ind or ths_con or row.get('所属行业', '') or row.get('L2_Industry', '')
                current_price = row.get('最新价', 0)
                
                # 检查是否已经是连板龙头（连板数>=4）
                if board_height >= self.params['min_board_height']:
                    logger.info(f"[弱转强-龙头识别] ✓ 连板龙头: {name}({code}) - {board_height}板，所属板块:{sector}")
                    
                    # 计算10日涨幅和涨停次数
                    total_rise_10d, limit_up_count = self._calc_10d_stats(code, date_str)
                    
                    dragon = DragonCandidate(
                        stock_code=code,
                        stock_name=name,
                        dragon_type=DragonType.CONTINUOUS,
                        peak_board_height=board_height,
                        peak_price=current_price,
                        peak_date=date_str,
                        entry_date=date_str,
                        sector_name=sector,
                        total_rise_10d=total_rise_10d,
                        limit_up_count=limit_up_count if limit_up_count > 0 else board_height
                    )
                    new_dragons.append(dragon)
                    continuous_count += 1
                    continue
                
                # 检查是否是趋势龙头（当日涨停）
                logger.debug(f"[弱转强-龙头识别] 检查趋势龙头: {name}({code})，当前{board_height}板")
                trend_dragon = self._check_trend_dragon(code, name, row, history_pools, date_str, sector)
                if trend_dragon:
                    logger.info(f"[弱转强-龙头识别] ✓ 趋势龙头(涨停): {name}({code}) - 10日涨幅{trend_dragon.total_rise_10d*100:.1f}%，{trend_dragon.limit_up_count}个涨停")
                    new_dragons.append(trend_dragon)
                    trend_count += 1
        
        # ========== 第2步：从历史涨停池中识别"趋势中"的龙头 ==========
        # 策略：近5日内有过涨停，但今日未涨停，仍在趋势中的股票
        if self.dm and history_pools:
            logger.info(f"[弱转强-龙头识别] 从历史涨停池({len(history_pools)}天)中识别趋势中龙头...")
            
            # 收集近5日有过涨停的股票（排除已在new_dragons中的）
            recent_limit_up_stocks = {}  # code -> {name, sector, last_limit_date, last_limit_price}
            sorted_dates = sorted(history_pools.keys(), reverse=True)[:5]  # 近5天
            
            for date in sorted_dates:
                pool = history_pools.get(date, pd.DataFrame())
                if pool.empty:
                    continue
                
                for _, row in pool.iterrows():
                    code = str(row.get('代码', '')).zfill(6)
                    
                    # 跳过已在new_dragons中的
                    if any(d.stock_code == code for d in new_dragons):
                        continue
                    
                    if code not in recent_limit_up_stocks:
                        ths_ind = (stock_to_ths_industry or {}).get(code, '')
                        ths_con = (stock_to_ths_concept or {}).get(code, '')
                        recent_limit_up_stocks[code] = {
                            'name': row.get('名称', ''),
                            'sector': ths_ind or ths_con or row.get('所属行业', '') or row.get('L2_Industry', ''),
                            'last_limit_date': date,
                            'last_limit_price': row.get('最新价', 0)
                        }
            
            logger.info(f"[弱转强-龙头识别] 近5日有过涨停的股票共{len(recent_limit_up_stocks)}只（排除已识别的）")
            
            # 检查这些股票是否构成趋势龙
            trend_in_progress_count = 0
            for code, info in list(recent_limit_up_stocks.items())[:50]:  # 限制检查数量，避免太慢
                # 使用日线数据检查趋势
                trend_dragon = self._check_trend_dragon_by_daily_for_non_limit(
                    code, info['name'], info['sector'], date_str
                )
                if trend_dragon:
                    logger.info(f"[弱转强-龙头识别] ✓ 趋势龙头(趋势中): {info['name']}({code}) - "
                               f"10日涨幅{trend_dragon.total_rise_10d*100:.1f}%，{trend_dragon.limit_up_count}个涨停，"
                               f"最近涨停{trend_dragon.days_since_last_limit}天前")
                    new_dragons.append(trend_dragon)
                    trend_count += 1
                    trend_in_progress_count += 1
            
            logger.info(f"[弱转强-龙头识别] 趋势中龙头识别完成: {trend_in_progress_count}只")

        # ========== 第3步：识别空间龙头（高位连板后回调中）==========
        if self.dm and history_pools:
            space_params = self.params.get('min_board_height_for_space', 5)
            logger.info(f"[弱转强-龙头识别] 搜索空间龙头：≥{space_params}板后回调中的...")
            space_count = 0
            for code, info in list(recent_limit_up_stocks.items())[:50]:
                if any(d.stock_code == code for d in new_dragons):
                    continue
                space_dragon = self._check_space_dragon(code, info['name'], info['sector'], date_str, history_pools)
                if space_dragon:
                    logger.info(f"[弱转强-龙头识别] ✓ 空间龙头: {info['name']}({code}) - "
                               f"{space_dragon.peak_board_height}板后回调{space_dragon._drawdown_pct:.1f}%")
                    new_dragons.append(space_dragon)
                    space_count += 1
            logger.info(f"[弱转强-龙头识别] 空间龙头识别完成: {space_count}只")

        # ========== 回退模式：若无任何龙头入选，放宽连板龙阈值 ==========
        if len(new_dragons) == 0 and not today_zt.empty:
            fallback_board = max(2, self.params['min_board_height'] - 1)
            logger.info(f"[弱转强-龙头识别] 主模式无龙头，回退模式：放宽至≥{fallback_board}板...")
            for _, row in today_zt.iterrows():
                code = str(row.get('代码', '')).zfill(6)
                name = row.get('名称', '')
                board_height = row.get('连板数', 0)

                if board_height >= fallback_board and board_height < self.params['min_board_height']:
                    if any(d.stock_code == code for d in new_dragons):
                        continue
                    ths_ind = (stock_to_ths_industry or {}).get(code, '')
                    ths_con = (stock_to_ths_concept or {}).get(code, '')
                    sector = ths_ind or ths_con or row.get('所属行业', '') or row.get('L2_Industry', '')
                    current_price = row.get('最新价', 0)
                    total_rise_10d, limit_up_count = self._calc_10d_stats(code, date_str)

                    dragon = DragonCandidate(
                        stock_code=code, stock_name=name,
                        dragon_type=DragonType.CONTINUOUS,
                        peak_board_height=board_height, peak_price=current_price,
                        peak_date=date_str, entry_date=date_str, sector_name=sector,
                        total_rise_10d=total_rise_10d,
                        limit_up_count=limit_up_count if limit_up_count > 0 else board_height,
                    )
                    new_dragons.append(dragon)
                    logger.info(f"[弱转强-龙头识别] ✓ 回退连板龙: {name}({code}) - {board_height}板")

        logger.info(f"[弱转强-龙头识别] ========== 识别完成 ==========")
        logger.info(f"[弱转强-龙头识别] 总计: 连板龙头{continuous_count}只，趋势龙头{trend_count}只，空间龙头{space_count}只，共{len(new_dragons)}只")
        return new_dragons
    
    def _check_trend_dragon_by_daily_for_non_limit(self,
                                                    code: str,
                                                    name: str,
                                                    sector: str,
                                                    date_str: str) -> Optional[DragonCandidate]:
        """
        检查今日未涨停的股票是否是趋势龙头
        
        标准：
        1. 近10日涨幅>=40%
        2. 近10日至少2个涨停
        3. 最近涨停在5天内
        4. 当前价格距离最高点回调不超过10%（仍在趋势中）
        """
        try:
            # 获取近15个交易日数据
            if hasattr(self.dm, 'date_utils'):
                last_n_dates = self.repo.date_utils.get_last_n_trade_dates(15, date_str)
                if not last_n_dates or len(last_n_dates) < 10:
                    return None
                start_date = last_n_dates[-1]
            else:
                from datetime import datetime, timedelta
                start_date = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
            
            # 获取日线数据
            daily_df = self.repo.get_stock_daily(code, start_date, date_str)
            
            if daily_df is None or daily_df.empty or len(daily_df) < 10:
                return None
            
            daily_df = daily_df.sort_values('trade_date')
            
            # 统计涨停次数和最近涨停日期（在取子集之前先计算）
            # Tushare使用 'pct_chg' 列表示涨跌幅
            pct_col = 'pct_chg' if 'pct_chg' in daily_df.columns else 'pct_change'
            daily_df['is_limit_up'] = daily_df[pct_col] >= 9.5
            
            # 取最近10个交易日
            last_10d = daily_df.tail(10)
            
            # 计算10日涨幅
            first_close = last_10d.iloc[0]['close']
            last_close = last_10d.iloc[-1]['close']
            total_rise = (last_close - first_close) / first_close if first_close > 0 else 0
            
            # 统计最近10日涨停次数
            limit_up_days_10d = last_10d[last_10d['is_limit_up']]
            limit_up_count = len(limit_up_days_10d)
            
            if limit_up_count < self.params['min_limit_up_count']:
                return None
            
            # 检查最近涨停是否在5天内
            last_limit_date = limit_up_days_10d.iloc[-1]['trade_date']
            last_limit_date_str = last_limit_date.strftime('%Y%m%d') if hasattr(last_limit_date, 'strftime') else str(last_limit_date)
            
            # 计算距离最近涨停的天数
            try:
                last_limit_dt = datetime.strptime(last_limit_date_str, '%Y%m%d')
                today_dt = datetime.strptime(date_str, '%Y%m%d')
                days_since_last_limit = (today_dt - last_limit_dt).days
            except:
                days_since_last_limit = 999
            
            if days_since_last_limit > 5:
                return None
            
            # 检查是否仍在趋势中（距离10日最高点回调不超过10%）
            high_10d = last_10d['high'].max()
            drawdown_from_high = (high_10d - last_close) / high_10d if high_10d > 0 else 0
            
            if drawdown_from_high > 0.10:  # 回调超过10%，可能已破位
                logger.debug(f"[弱转强-趋势龙头] {name}({code}) 回调{drawdown_from_high*100:.1f}%超过10%，趋势可能结束")
                return None
            
            # 检查最大连续涨停天数（排除连板龙）
            consecutive_limit = 0
            max_consecutive = 0
            for is_limit in last_10d['is_limit_up']:
                if is_limit:
                    consecutive_limit += 1
                    max_consecutive = max(max_consecutive, consecutive_limit)
                else:
                    consecutive_limit = 0
            
            if max_consecutive > 3:
                return None
            
            # 符合趋势龙头标准
            if total_rise >= self.params['min_total_rise']:
                dragon = DragonCandidate(
                    stock_code=code,
                    stock_name=name,
                    dragon_type=DragonType.TREND,
                    peak_board_height=0,
                    peak_price=high_10d,
                    peak_date=last_limit_date_str,
                    entry_date=date_str,
                    sector_name=sector,
                    total_rise_10d=total_rise,
                    limit_up_count=limit_up_count
                )
                # 附加信息
                dragon.days_since_last_limit = days_since_last_limit
                dragon.current_price = last_close
                dragon.drawdown_from_high = drawdown_from_high
                return dragon
            
        except Exception as e:
            logger.debug(f"[弱转强-趋势龙头] 非涨停股检查失败 {code}: {e}")
        
        return None

    def _check_space_dragon(self,
                            code: str,
                            name: str,
                            sector: str,
                            date_str: str,
                            history_pools: Dict[str, pd.DataFrame]) -> Optional[DragonCandidate]:
        """
        识别空间龙头：曾经高位连板（≥5板），近期不在涨停池，处于回调中但未A杀

        核心特征：高位连板后的回调蓄力，预期二波机会
        """
        try:
            min_boards = self.params.get('min_board_height_for_space', 5)

            # 从历史涨停池中找到该股票的最高连板记录
            sorted_dates = sorted(history_pools.keys(), reverse=True)[:15]
            max_board = 0
            peak_price = 0.0
            peak_date = date_str

            for pool_date in sorted_dates:
                pool = history_pools.get(pool_date, pd.DataFrame())
                if pool.empty:
                    continue

                pool_code_col = self._get_code_col(pool)
                if not pool_code_col:
                    continue

                pool_codes = pool[pool_code_col].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
                stock_row = pool[pool_codes == code]
                if not stock_row.empty:
                    boards = stock_row.iloc[0].get('连板数', 0)
                    price = stock_row.iloc[0].get('最新价', 0)
                    if boards > max_board:
                        max_board = boards
                        peak_price = price
                        peak_date = pool_date

            if max_board < min_boards:
                return None

            # 获取当前日线数据检查回调幅度
            if not self.dm:
                return None

            start_date = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
            daily_df = self.repo.get_stock_daily(code, start_date, date_str)
            if daily_df is None or daily_df.empty:
                return None

            daily_df = daily_df.sort_values('trade_date')
            last_10d = daily_df.tail(10)
            high_10d = last_10d['high'].max()
            last_close = last_10d.iloc[-1]['close']
            drawdown = (high_10d - last_close) / high_10d if high_10d > 0 else 0

            # 回调在5%~20%之间（不是A杀，也不是高位震荡）
            if drawdown < 0.05 or drawdown > 0.20:
                return None

            # 计算涨停次数
            pct_col = 'pct_chg' if 'pct_chg' in daily_df.columns else 'pct_change'
            limit_up_count = len(last_10d[last_10d[pct_col] >= 9.5])

            total_rise_10d = (last_close - last_10d.iloc[0]['close']) / last_10d.iloc[0]['close']

            dragon = DragonCandidate(
                stock_code=code,
                stock_name=name,
                dragon_type=DragonType.SPACE,
                peak_board_height=max_board,
                peak_price=peak_price,
                peak_date=peak_date,
                entry_date=date_str,
                sector_name=sector,
                total_rise_10d=total_rise_10d,
                limit_up_count=limit_up_count,
            )
            dragon._drawdown_pct = drawdown * 100
            return dragon

        except Exception as e:
            logger.debug(f"[弱转强-空间龙头] 检查失败 {code}: {e}")

        return None

    def _check_trend_dragon(self, 
                           code: str, 
                           name: str,
                           today_row: pd.Series,
                           history_pools: Dict[str, pd.DataFrame],
                           date_str: str,
                           sector: str) -> Optional[DragonCandidate]:
        """
        检查是否是趋势龙头（近10日涨幅大，有涨停但非连续）
        
        使用日线数据计算涨幅，解决历史涨停池数据不足的问题
        """
        try:
            # 优先使用日线数据计算涨幅（更准确）
            if self.dm:
                return self._check_trend_dragon_by_daily(code, name, today_row, date_str, sector)
            else:
                # 备用：使用涨停池数据
                return self._check_trend_dragon_by_pools(code, name, today_row, history_pools, date_str, sector)
        except Exception as e:
            logger.debug(f"[弱转强-趋势龙头] 检查失败 {code}: {e}")
        
        return None
    
    def _calc_10d_stats(self, code: str, date_str: str) -> Tuple[float, int]:
        """
        计算股票近10日涨幅和涨停次数
        
        Args:
            code: 股票代码
            date_str: 日期字符串
            
        Returns:
            Tuple[10日涨幅, 涨停次数]
        """
        try:
            # 获取近15个交易日数据（确保有足够数据）
            if hasattr(self.dm, 'date_utils'):
                last_n_dates = self.repo.date_utils.get_last_n_trade_dates(15, date_str)
                if not last_n_dates or len(last_n_dates) < 10:
                    return 0.0, 0
                start_date = last_n_dates[-1]
            else:
                from datetime import datetime, timedelta
                start_date = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
            
            # 获取日线数据
            daily_df = self.repo.get_stock_daily(code, start_date, date_str)
            
            if daily_df is None or daily_df.empty or len(daily_df) < 5:
                return 0.0, 0
            
            # 取最近10个交易日
            daily_df = daily_df.sort_values('trade_date').tail(10)
            
            # 计算累计涨幅
            first_close = daily_df.iloc[0]['close']
            last_close = daily_df.iloc[-1]['close']
            total_rise = (last_close - first_close) / first_close if first_close > 0 else 0
            
            # 统计涨停次数（涨幅>=9.5%）
            pct_col = 'pct_chg' if 'pct_chg' in daily_df.columns else 'pct_change'
            limit_up_count = len(daily_df[daily_df[pct_col] >= 9.5])
            
            return total_rise, limit_up_count
            
        except Exception as e:
            logger.debug(f"[弱转强] 计算10日统计失败 {code}: {e}")
            return 0.0, 0
    
    def _check_trend_dragon_by_daily(self,
                                     code: str,
                                     name: str,
                                     today_row: pd.Series,
                                     date_str: str,
                                     sector: str) -> Optional[DragonCandidate]:
        """使用日线数据检查趋势龙头"""
        try:
            # 获取近15个交易日数据（确保有足够数据）
            if hasattr(self.dm, 'date_utils'):
                last_n_dates = self.repo.date_utils.get_last_n_trade_dates(15, date_str)
                if not last_n_dates or len(last_n_dates) < 10:
                    logger.debug(f"[弱转强-趋势龙头] {name}({code}) 无法获取足够交易日数据")
                    return None
                start_date = last_n_dates[-1]  # 最早日期
            else:
                # 备用：简单计算15天前
                from datetime import datetime, timedelta
                start_date = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
            
            # 获取日线数据
            daily_df = self.repo.get_stock_daily(code, start_date, date_str)
            
            if daily_df is None or daily_df.empty or len(daily_df) < 5:
                logger.debug(f"[弱转强-趋势龙头] {name}({code}) 日线数据不足({len(daily_df) if daily_df is not None else 0}天)")
                return None
            
            # 取最近10个交易日
            daily_df = daily_df.sort_values('trade_date').tail(10)
            
            # 计算累计涨幅
            first_close = daily_df.iloc[0]['close']
            last_close = daily_df.iloc[-1]['close']
            total_rise = (last_close - first_close) / first_close if first_close > 0 else 0
            
            # 统计涨停次数（涨幅>=9.5%）
            # Tushare使用 'pct_chg' 列表示涨跌幅
            pct_col = 'pct_chg' if 'pct_chg' in daily_df.columns else 'pct_change'
            limit_up_count = len(daily_df[daily_df[pct_col] >= 9.5])
            
            # 检查涨停分布（不能连续涨停超过3天，否则是连板龙）
            daily_df['is_limit_up'] = daily_df[pct_col] >= 9.5
            consecutive_limit = 0
            max_consecutive = 0
            for is_limit in daily_df['is_limit_up']:
                if is_limit:
                    consecutive_limit += 1
                    max_consecutive = max(max_consecutive, consecutive_limit)
                else:
                    consecutive_limit = 0
            
            # ===== 计算价格斜率（线性回归）=====
            # 使用对数价格计算斜率，更准确反映增长率
            # x: 时间序列 (0, 1, 2, ..., n-1)
            # y: 对数收盘价 ln(close)
            n = len(daily_df)
            x = np.arange(n)
            y = np.log(daily_df['close'].values)
            
            # 计算斜率: slope = Σ[(x_i - x̄)(y_i - ȳ)] / Σ[(x_i - x̄)²]
            x_mean = np.mean(x)
            y_mean = np.mean(y)
            numerator = np.sum((x - x_mean) * (y - y_mean))
            denominator = np.sum((x - x_mean) ** 2)
            slope = numerator / denominator if denominator != 0 else 0
            
            # 计算R²（决定系数），衡量趋势性
            y_pred = y_mean + slope * (x - x_mean)
            ss_res = np.sum((y - y_pred) ** 2)
            ss_tot = np.sum((y - y_mean) ** 2)
            r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
            
            # 将斜率转换为日均涨幅百分比
            daily_growth_rate = (np.exp(slope) - 1) * 100
            
            logger.debug(f"[弱转强-趋势龙头] {name}({code}) 日线统计: 10日涨幅{total_rise*100:.1f}%，"
                        f"涨停{limit_up_count}次，最大连续{max_consecutive}天，"
                        f"日均斜率{daily_growth_rate:.2f}%，R²={r_squared:.2f}")
            
            # 趋势龙头标准（改进版）：
            # 1. 日均斜率>=3%（平均每天涨3%，10日累计约34%，考虑复利）
            # 2. 至少2个涨停（必须有资金攻击）
            # 3. 最大连续涨停<=3（排除连板龙）
            # 4. R²>=0.5（确保有一定趋势性，不是大幅波动）
            min_slope_daily = self.params.get('min_slope_daily', 0.03)  # 默认3%
            min_r_squared = self.params.get('min_r_squared', 0.5)  # 默认0.5
            
            if (daily_growth_rate >= min_slope_daily * 100 and 
                limit_up_count >= self.params['min_limit_up_count'] and
                max_consecutive <= 3 and
                r_squared >= min_r_squared):
                logger.info(f"[弱转强-趋势龙头] ✓ {name}({code}) 符合趋势龙头标准: "
                           f"日均斜率{daily_growth_rate:.2f}%>=3%，涨停{limit_up_count}次，R²={r_squared:.2f}")
                return DragonCandidate(
                    stock_code=code,
                    stock_name=name,
                    dragon_type=DragonType.TREND,
                    peak_board_height=0,
                    peak_price=last_close,
                    peak_date=date_str,
                    entry_date=date_str,
                    sector_name=sector,
                    total_rise_10d=total_rise,
                    limit_up_count=limit_up_count
                )
            else:
                reasons = []
                if daily_growth_rate < min_slope_daily * 100:
                    reasons.append(f"日均斜率{daily_growth_rate:.2f}%(<{min_slope_daily*100:.0f}%)")
                if limit_up_count < self.params['min_limit_up_count']:
                    reasons.append(f"涨停{limit_up_count}次(<{self.params['min_limit_up_count']})")
                if max_consecutive > 3:
                    reasons.append(f"连续涨停{max_consecutive}天(>3)")
                if r_squared < min_r_squared:
                    reasons.append(f"R²={r_squared:.2f}(<{min_r_squared})")
                logger.debug(f"[弱转强-趋势龙头] {name}({code}) 不符合标准: {'; '.join(reasons)}")
        except Exception as e:
            logger.debug(f"[弱转强-趋势龙头] 日线数据检查失败 {code}: {e}")
        
        return None
    
    def _check_trend_dragon_by_pools(self,
                                     code: str,
                                     name: str,
                                     today_row: pd.Series,
                                     history_pools: Dict[str, pd.DataFrame],
                                     date_str: str,
                                     sector: str) -> Optional[DragonCandidate]:
        """使用涨停池数据检查趋势龙头（备用方法）"""
        try:
            dates = sorted(history_pools.keys(), reverse=True)[:10]
            
            if len(dates) < 5:
                logger.debug(f"[弱转强-趋势龙头] {name}({code}) 历史池数据不足({len(dates)}天)")
                return None
            
            total_rise = 0.0
            limit_up_count = 0
            first_price = None
            last_price = today_row.get('最新价', 0)
            
            for date in dates:
                pool = history_pools.get(date, pd.DataFrame())
                if pool.empty:
                    continue
                
                # 获取代码列名
                pool_code_col = self._get_code_col(pool)
                if not pool_code_col:
                    continue
                
                # 处理不同格式的代码列
                pool_codes = pool[pool_code_col].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
                stock_row = pool[pool_codes == code]
                if not stock_row.empty:
                    price = stock_row.iloc[0].get('最新价', 0)
                    change = stock_row.iloc[0].get('涨跌幅', 0)
                    
                    if first_price is None:
                        first_price = price
                    
                    if isinstance(change, str):
                        change = float(change.replace('%', ''))
                    if change >= 9.5:
                        limit_up_count += 1
            
            if first_price and first_price > 0:
                total_rise = (last_price - first_price) / first_price
            
            if (total_rise >= self.params['min_total_rise'] and 
                limit_up_count >= self.params['min_limit_up_count']):
                logger.info(f"[弱转强-趋势龙头] ✓ {name}({code}) 符合趋势龙头标准(池数据): "
                           f"10日涨幅{total_rise*100:.1f}%，涨停{limit_up_count}次")
                return DragonCandidate(
                    stock_code=code,
                    stock_name=name,
                    dragon_type=DragonType.TREND,
                    peak_board_height=0,
                    peak_price=last_price,
                    peak_date=date_str,
                    entry_date=date_str,
                    sector_name=sector,
                    total_rise_10d=total_rise,
                    limit_up_count=limit_up_count
                )
        except Exception as e:
            logger.debug(f"[弱转强-趋势龙头] 池数据检查失败 {code}: {e}")
        
        return None
    
    # ==================== 阶段2：走弱确认 ====================
    
    def _check_weakening(self,
                        dragon: DragonCandidate,
                        today_zt: pd.DataFrame,
                        tick_data: pd.DataFrame,
                        date_str: str) -> Optional[WeakeningDragon]:
        """检查龙头是否走弱"""
        code = dragon.stock_code
        name = dragon.stock_name

        logger.debug(f"[弱转强-走弱检查] 检查 {name}({code}) {dragon.dragon_type.value}...")

        # 获取代码列名
        zt_code_col = self._get_code_col(today_zt)
        if not zt_code_col:
            logger.warning(f"[弱转强-走弱检查] 涨停池缺少代码列，可用列: {list(today_zt.columns)}")
            return None

        # 在今日涨停池中查找（处理不同格式的代码列）
        # 统一代码格式：移除后缀并补零
        zt_codes = today_zt[zt_code_col].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6)
        code_normalized = re.sub(r'\.\w+$', '', str(code)).zfill(6)
        stock_row = today_zt[zt_codes == code_normalized]
        
        if stock_row.empty:
            # 不在涨停池中 = 断板
            logger.info(f"[弱转强-走弱检查] ✓ {name}({code}) 确认走弱: 断板(未在今日涨停池)")
            return self._create_weakening_dragon(dragon, "断板", dragon.peak_price, date_str)
        
        row = stock_row.iloc[0]
        current_price = row.get('最新价', 0)
        
        # 检查烂板
        blast_times = row.get('炸板次数', 0)
        if is_lanban(blast_times):
            logger.info(f"[弱转强-走弱检查] ✓ {name}({code}) 确认走弱: 烂板({blast_times}次)")
            return self._create_weakening_dragon(dragon, f"烂板({blast_times}次)", current_price, date_str)
        
        # 检查尾盘板
        last_seal_time = row.get('最后封板时间', '')
        if is_late_board(last_seal_time):
            logger.info(f"[弱转强-走弱检查] ✓ {name}({code}) 确认走弱: 尾盘板({last_seal_time})")
            return self._create_weakening_dragon(dragon, "尾盘板", current_price, date_str)
        
        # 检查放量滞涨（涨停但换手异常高）
        turnover = row.get('换手率', 0)
        if turnover > 40:  # 换手率>40%视为异常
            logger.info(f"[弱转强-走弱检查] ✓ {name}({code}) 确认走弱: 放量滞涨(换手{turnover:.1f}%)")
            return self._create_weakening_dragon(dragon, f"放量滞涨(换手{turnover:.1f}%)", current_price, date_str)
        
        # 趋势龙头：检查是否趋势回调
        if dragon.dragon_type == DragonType.TREND:
            drawdown = (dragon.peak_price - current_price) / dragon.peak_price
            if drawdown > 0.05:  # 回调超过5%
                logger.info(f"[弱转强-走弱检查] ✓ {name}({code}) 确认走弱: 趋势回调({drawdown*100:.1f}%)")
                return self._create_weakening_dragon(dragon, f"趋势回调({drawdown*100:.1f}%)", current_price, date_str)
        
        logger.debug(f"[弱转强-走弱检查] {name}({code}) 未走弱，保持观察")
        return None
    
    def _create_weakening_dragon(self, 
                                dragon: DragonCandidate,
                                weakening_type: str,
                                weakening_price: float,
                                date_str: str) -> WeakeningDragon:
        """创建走弱龙头对象"""
        # 复制DragonCandidate的所有字段
        weakening = WeakeningDragon(
            stock_code=dragon.stock_code,
            stock_name=dragon.stock_name,
            dragon_type=dragon.dragon_type,
            peak_board_height=dragon.peak_board_height,
            peak_price=dragon.peak_price,
            peak_date=dragon.peak_date,
            entry_date=dragon.entry_date,
            sector_name=dragon.sector_name,
            total_rise_10d=dragon.total_rise_10d,
            limit_up_count=dragon.limit_up_count,
            status=DragonStatus.WEAKENING,
            status_change_date=date_str,
            weakening_date=date_str,
            weakening_type=weakening_type,
            weakening_price=weakening_price or dragon.peak_price,
            current_price=weakening_price or dragon.peak_price,
            max_drawdown=0.0,
            recovery_signals={
                'min_gap': self.params['min_gap'],
                'ideal_gap': self.params['ideal_gap'],
                'max_gap': self.params['max_gap'],
                'min_auction_vol_ratio': self.params['min_auction_vol_ratio'],
                'ideal_auction_vol_ratio': self.params['ideal_auction_vol_ratio'],
                'min_auction_amount': self.params['min_auction_amount'],
            }
        )
        return weakening
    
    # ==================== 阶段3：盘中转强监控 ====================
    
    def monitor_intraday(self,
                        auction_data: Dict[str, Dict],
                        tick_data: Dict[str, pd.DataFrame],
                        date_str: str,
                        time_str: str) -> List[RecoverySignal]:
        """
        盘中监控转强信号
        
        Args:
            auction_data: 竞价数据 Dict[code, {'开盘价': x, '竞价成交量': y, ...}]
            tick_data: 分时数据 Dict[code, DataFrame]
            date_str: 日期
            time_str: 当前时间
        
        Returns:
            List[RecoverySignal]: 检测到的转强信号
        """
        signals = []
        
        # 只在竞价和开盘初期监控（9:25-9:45）
        current_minutes = time_to_minutes(time_str)
        if not (565 <= current_minutes <= 585):  # 9:25-9:45
            return signals
        
        logger.info(f"[弱转强-盘中监控] {time_str} 开始监控，走弱池{len(self.weakening_pool)}只")
        
        for code, weakening in self.weakening_pool.items():
            # 获取竞价数据
            auction = auction_data.get(code, {})
            if not auction:
                continue
            
            # 检查竞价转强信号
            signal = self._check_auction_recovery(weakening, auction, date_str, time_str)
            if signal:
                signals.append(signal)
                logger.info(f"[弱转强-盘中] ✓ {weakening.stock_name}({code}) 竞价转强信号: 高开{signal.gap_pct*100:.1f}%")
                
                # 检查开盘确认
                tick = tick_data.get(code, pd.DataFrame())
                if not tick.empty:
                    confirmed = self._confirm_recovery(signal, tick, auction)
                    if confirmed:
                        signal.confirmed = True
                        signal.confirmation_time = time_str
                        logger.info(f"[弱转强-盘中] ✓✓ {weakening.stock_name}({code}) 转强确认!")
        
        # 保存信号
        self.recovery_signals.extend(signals)
        
        return signals
    
    def _get_dynamic_params(self, weakening: WeakeningDragon) -> Dict:
        """
        获取动态调整后的参数
        
        根据市场情绪、板块强度、个股特征动态调整阈值
        """
        if not self.params.get('dynamic_params_enabled', False):
            return self.params
        
        # 基础参数
        dynamic_params = dict(self.params)
        
        # 根据市场情绪调整
        sentiment_boost = 0
        if self._market_sentiment == "bullish":
            sentiment_boost = -self.params.get('sentiment_bullish_boost', 0.02)
        elif self._market_sentiment == "bearish":
            sentiment_boost = self.params.get('sentiment_bearish_penalty', 0.02)
        
        # 根据板块强度调整
        sector_boost = 0
        if weakening.sector_name and self.se:
            try:
                # 获取板块强度评分
                sector_score = self._get_sector_strength(weakening.sector_name)
                # 板块强度越高，阈值越低（越容易触发）
                sector_boost = -0.01 * (sector_score / 100)  # 最高降低1%
            except Exception:
                pass
        
        # 根据个股历史波动率调整
        volatility_boost = 0
        if weakening.max_drawdown > 0:
            # 回调越深，阈值越低（给更多机会）
            volatility_boost = -0.005 * min(weakening.max_drawdown / 0.1, 1.0)
        
        # 综合调整
        total_adjustment = sentiment_boost + sector_boost + volatility_boost
        
        # 应用调整（限制范围）
        dynamic_params['min_gap'] = max(0.01, min(0.05, self.params['min_gap'] + total_adjustment))
        dynamic_params['max_gap'] = max(0.05, min(0.12, self.params['max_gap'] + total_adjustment))
        dynamic_params['min_auction_vol_ratio'] = max(0.05, min(0.20, self.params['min_auction_vol_ratio'] + total_adjustment))
        
        return dynamic_params
    
    def _get_sector_strength(self, sector_name: str) -> float:
        """获取板块强度评分（0-100）"""
        try:
            # 如果板块引擎可用，获取板块强度
            if self.se and hasattr(self.se, 'get_sector_strength'):
                return self.se.get_sector_strength(sector_name)
            return 50  # 默认中等强度
        except Exception:
            return 50
    
    def _calculate_flexible_score(self, 
                                  gap_pct: float, 
                                  auction_vol_ratio: float,
                                  auction_amount: float,
                                  weakening: WeakeningDragon) -> float:
        """
        计算弹性评分（0-100）
        
        综合考虑多个因子，给出更灵活的评分
        """
        if not self.params.get('enable_flexible_scoring', False):
            return 0  # 不启用时返回0
        
        # 1. 高开评分（0-30分）
        gap_score = 0
        if gap_pct >= 0.05:
            gap_score = 30
        elif gap_pct >= 0.03:
            gap_score = 20 + (gap_pct - 0.03) / 0.02 * 10
        elif gap_pct >= 0.01:
            gap_score = (gap_pct - 0.01) / 0.02 * 20
        gap_score = min(30, max(0, gap_score))
        
        # 2. 竞价量评分（0-25分）
        vol_score = 0
        if auction_vol_ratio >= 0.15:
            vol_score = 25
        elif auction_vol_ratio >= 0.10:
            vol_score = 15 + (auction_vol_ratio - 0.10) / 0.05 * 10
        elif auction_vol_ratio >= 0.05:
            vol_score = (auction_vol_ratio - 0.05) / 0.05 * 15
        vol_score = min(25, max(0, vol_score))
        
        # 3. 竞价金额评分（0-20分）
        amount_score = 0
        if auction_amount >= 10000000:  # 1000万
            amount_score = 20
        elif auction_amount >= 5000000:  # 500万
            amount_score = 10 + (auction_amount - 5000000) / 5000000 * 10
        elif auction_amount >= 1000000:  # 100万
            amount_score = (auction_amount - 1000000) / 4000000 * 10
        amount_score = min(20, max(0, amount_score))
        
        # 4. 个股动量评分（0-15分）
        momentum_score = 0
        if weakening.max_drawdown <= 0.05:  # 回调小于5%
            momentum_score = 15
        elif weakening.max_drawdown <= 0.10:  # 回调小于10%
            momentum_score = 10
        elif weakening.max_drawdown <= 0.15:  # 回调小于15%
            momentum_score = 5
        
        # 5. 市场情绪评分（0-10分）
        sentiment_score = 0
        if self._market_sentiment == "bullish":
            sentiment_score = 10
        elif self._market_sentiment == "neutral":
            sentiment_score = 5
        
        # 综合评分
        total_score = gap_score + vol_score + amount_score + momentum_score + sentiment_score
        
        return min(100, max(0, total_score))
    
    def update_market_sentiment(self, sentiment: str):
        """更新市场情绪（用于动态参数调整）"""
        valid_sentiments = ["bullish", "neutral", "bearish"]
        if sentiment in valid_sentiments:
            self._market_sentiment = sentiment
            logger.info(f"[弱转强] 市场情绪更新为: {sentiment}")
        else:
            logger.warning(f"[弱转强] 无效的市场情绪: {sentiment}")
    
    def _check_auction_recovery(self,
                               weakening: WeakeningDragon,
                               auction: Dict,
                               date_str: str,
                               time_str: str) -> Optional[RecoverySignal]:
        """检查竞价转强信号 - 优化版本（支持动态参数和弹性评分）"""
        open_price = auction.get('开盘价', 0)
        yest_close = weakening.current_price  # 用走弱时的价格作为基准
        
        if yest_close <= 0:
            return None
        
        gap_pct = (open_price - yest_close) / yest_close
        
        # 获取动态参数
        dynamic_params = self._get_dynamic_params(weakening)
        
        # 高开范围检查（使用动态参数）
        if not (dynamic_params['min_gap'] <= gap_pct <= dynamic_params['max_gap']):
            logger.debug(f"[{weakening.stock_code}] 高开{gap_pct*100:.1f}%不在范围"
                        f"[{dynamic_params['min_gap']*100:.0f}%-{dynamic_params['max_gap']*100:.0f}%]")
            return None
        
        # 竞价量检查
        auction_vol = auction.get('竞价成交量', 0)
        yest_vol = auction.get('昨日成交量', 1000000)  # 默认值防止除0
        auction_vol_ratio = auction_vol / yest_vol if yest_vol > 0 else 0
        
        if auction_vol_ratio < dynamic_params['min_auction_vol_ratio']:
            logger.debug(f"[{weakening.stock_code}] 竞价量比例{auction_vol_ratio*100:.1f}%"
                        f"<{dynamic_params['min_auction_vol_ratio']*100:.0f}%")
            return None
        
        # 竞价金额检查
        auction_amount = auction.get('竞价成交额', auction_vol * open_price)
        if auction_amount < self.params['min_auction_amount']:
            logger.debug(f"[{weakening.stock_code}] 竞价金额{auction_amount/10000:.0f}万"
                        f"<{self.params['min_auction_amount']/10000:.0f}万")
            return None
        
        # 计算弹性评分
        flexible_score = self._calculate_flexible_score(
            gap_pct, auction_vol_ratio, auction_amount, weakening
        )
        
        # 计算置信度（Phase 3：confidence_mode=deduction 走统一扣分制，默认 legacy 不变）
        final_confidence, conf_breakdown = self._auction_recovery_confidence(
            gap_pct, dynamic_params, auction_vol_ratio, flexible_score, weakening
        )
        
        logger.info(f"[{weakening.stock_code}] 竞价转强信号: "
                   f"高开{gap_pct*100:.1f}%, 竞价量{auction_vol_ratio*100:.1f}%, "
                   f"弹性评分{flexible_score:.0f}, 置信度{final_confidence*100:.0f}%")
        if conf_breakdown:
            logger.debug(f"[{weakening.stock_code}] 置信扣分明细: {conf_breakdown}")

        return RecoverySignal(
            stock_code=weakening.stock_code,
            stock_name=weakening.stock_name,
            signal_time=time_str,
            signal_type="竞价转强",
            gap_pct=gap_pct,
            auction_vol_ratio=auction_vol_ratio,
            auction_amount=auction_amount,
            open_price=open_price,
            open_change_pct=gap_pct,
            suggested_entry=open_price * 1.01,  # 建议买入价：开盘价+1%
            confidence=final_confidence
        )

    def _auction_recovery_confidence(self, gap_pct, dynamic_params,
                                     auction_vol_ratio, flexible_score, weakening):
        """
        竞价转强置信度（Phase 3）。

        - confidence_mode="deduction"：统一扣分制（confidence_rules.yaml 的 weak_to_strong），
          返回 (value, breakdown_dict)。
        - 其它（默认 "legacy"）：旧的基础分+加分逻辑，返回 (value, None)，行为不变。
        """
        mode = self.params.get("confidence_mode", "legacy")
        if mode == "deduction":
            from core.scoring.confidence_scorer import score_or_none
            wt = weakening.weakening_type or ""
            wt_cat = "断板" if wt == "断板" else ("放量滞涨" if "放量滞涨" in wt else "其他")
            res = score_or_none("weak_to_strong", {
                "gap_pct": gap_pct,
                "auction_vol_ratio": auction_vol_ratio,
                "flexible_score": flexible_score,
                "weakening_type": wt_cat,
            })
            if res is not None:
                return res.value, res.to_dict()

        # legacy
        confidence = 0.60
        if gap_pct >= self.params['ideal_gap']:
            confidence += 0.15
        elif gap_pct >= dynamic_params['min_gap']:
            confidence += 0.05
        if auction_vol_ratio >= self.params['ideal_auction_vol_ratio']:
            confidence += 0.10
        elif auction_vol_ratio >= dynamic_params['min_auction_vol_ratio']:
            confidence += 0.05
        if self.params.get('enable_flexible_scoring', False):
            if flexible_score >= 80:
                confidence += 0.10
            elif flexible_score >= 60:
                confidence += 0.05
        if weakening.weakening_type == "断板":
            confidence += 0.05
        elif "放量滞涨" in weakening.weakening_type:
            confidence -= 0.05
        return min(0.95, max(0.50, confidence)), None

    def _confirm_recovery(self,
                         signal: RecoverySignal,
                         tick: pd.DataFrame,
                         auction: Dict) -> bool:
        """确认转强（开盘后不回踩，快速拉升）"""
        if tick.empty:
            return False
        
        open_price = auction.get('开盘价', tick.iloc[0]['price'] if not tick.empty else 0)
        
        # 开盘后5分钟数据
        first_5min = tick.head(5)
        if first_5min.empty:
            return False
        
        # 检查是否回踩过多
        min_price = first_5min['price'].min()
        max_drop = (open_price - min_price) / open_price if open_price > 0 else 0
        
        if max_drop > self.params['max_open_drop']:
            return False
        
        # 检查是否快速拉升
        max_price = first_5min['price'].max()
        rise_pct = (max_price - open_price) / open_price if open_price > 0 else 0
        
        if rise_pct < 0.02:  # 拉升不足2%
            return False
        
        signal.confirmation_price = max_price
        return True
    
    # ==================== 阶段4：生成报告 ====================
    
    def generate_report(self, output_file: str = None) -> str:
        """
        生成Excel决策报告
        
        Returns:
            str: 输出文件路径
        """
        if output_file is None:
            date_str = datetime.now().strftime('%Y%m%d')
            output_file = f"弱转强决策报告_{date_str}.xlsx"
        
        try:
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                # Sheet1: 龙头候选池
                if self.dragon_pool:
                    df_dragon = pd.DataFrame([d.to_dict() for d in self.dragon_pool.values()])
                    df_dragon.to_excel(writer, sheet_name='龙头候选池', index=False)
                else:
                    pd.DataFrame().to_excel(writer, sheet_name='龙头候选池', index=False)
                
                # Sheet2: 龙头走弱池（重点观察）
                if self.weakening_pool:
                    df_weakening = pd.DataFrame([w.to_dict() for w in self.weakening_pool.values()])
                    df_weakening.to_excel(writer, sheet_name='龙头走弱池-重点观察', index=False)
                else:
                    pd.DataFrame().to_excel(writer, sheet_name='龙头走弱池-重点观察', index=False)
                
                # Sheet3: 当日转强信号
                if self.recovery_signals:
                    df_signals = pd.DataFrame([s.to_dict() for s in self.recovery_signals])
                    df_signals.to_excel(writer, sheet_name='当日转强信号', index=False)
                else:
                    pd.DataFrame().to_excel(writer, sheet_name='当日转强信号', index=False)
            
            logger.info(f"[弱转强] 报告已生成: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"[弱转强] 生成报告失败: {e}")
            return ""
    
    def get_pools_summary(self) -> Dict:
        """获取池子汇总信息"""
        return {
            'dragon_pool_count': len(self.dragon_pool),
            'weakening_pool_count': len(self.weakening_pool),
            'recovery_signals_count': len(self.recovery_signals),
            'dragon_pool': [d.to_dict() for d in self.dragon_pool.values()],
            'weakening_pool': [w.to_dict() for w in self.weakening_pool.values()],
            'recovery_signals': [s.to_dict() for s in self.recovery_signals],
        }

    @staticmethod
    def _parse_time(time_str: str) -> Optional[Tuple[int, int]]:
        """解析时间字符串为(hour, minute)"""
        if not time_str:
            return None
        try:
            parts = str(time_str).strip().replace('：', ':').split(':')
            if len(parts) >= 2:
                return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _is_valid_limit_time(limit_up_time: str, max_time_str: str) -> bool:
        """检查涨停时间是否在规定时间之前"""
        parsed = WeakToStrongStrategy._parse_time(limit_up_time)
        max_parsed = WeakToStrongStrategy._parse_time(max_time_str)
        if not parsed:
            return False
        if not max_parsed:
            return True
        return (parsed[0] < max_parsed[0]) or \
               (parsed[0] == max_parsed[0] and parsed[1] <= max_parsed[1])


# ==================== 兼容旧接口 ====================

@dataclass
class WeakStockInfo:
    """弱股信息（兼容旧接口）"""
    stock_code: str
    stock_name: str
    board_height: int
    weak_type: str
    quality_score: int
    tier: str
    next_day_signals: Dict
    reason: str


# 为了保持兼容性，保留旧的类名但使用新的实现
WeakToStrongStrategyV2 = WeakToStrongStrategy


if __name__ == "__main__":
    # 示例用法
    print("弱转强策略 - 动态龙头跟踪版本")
    print("=" * 60)
    print("使用方式:")
    print("  1. 每日更新: update_dragon_pools()")
    print("  2. 盘中监控: monitor_intraday()")
    print("  3. 生成报告: generate_report()")
    print("=" * 60)
    print("\n龙头定义:")
    print("  - 连板龙头: 连续涨停>=4板")
    print("  - 趋势龙头: 近10日涨幅>=40%, 期间>=2个涨停")
    print("\n走弱确认:")
    print("  - 烂板/断板/尾盘板/放量滞涨/趋势回调")
    print("\n转强信号:")
    print("  - 高开3%-8%, 竞价量>10%, 竞价金额>500万")