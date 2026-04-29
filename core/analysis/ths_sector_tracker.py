"""
同花顺板块追踪器 - 概念+行业统一分析

核心设计：
1. 统一使用同花顺数据（ths_index/ths_daily/ths_member）
2. 同时追踪概念指数和行业指数
3. 通过成分股重叠度建立概念-行业关联
4. 热点板块识别基于涨幅+成交额+涨停家数
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger


@dataclass
class THSSectorData:
    """同花顺板块数据"""
    ts_code: str               # 板块代码（如 885001.TI）
    name: str                  # 板块名称
    sector_type: str           # 类型：概念/行业
    trade_date: str            # 交易日期
    pct_change: float          # 涨跌幅
    amount: float              # 成交额（千元）
    vol: float                 # 成交量（手）
    up_count: int = 0          # 涨停家数（需额外计算）
    cons_count: int = 0        # 连板家数（需额外计算）


@dataclass
class THSSectorMetrics:
    """板块指标"""
    # 基础指标
    pct_change: float = 0.0        # 当日涨跌幅
    amount: float = 0.0            # 成交额
    amount_change: float = 0.0     # 成交额变化率

    # 强度指标
    up_count: int = 0              # 涨停家数
    cons_count: int = 0            # 连板家数
    up_ratio: float = 0.0          # 涨停占比（涨停数/成分股数）

    # 趋势指标
    rank: int = 0                  # 涨幅排名
    rank_change: int = 0           # 排名变化

    # 综合评分
    composite_score: float = 0.0   # 综合得分
    is_hot: bool = False           # 是否热点


class THSSectorTracker:
    """
    同花顺板块追踪器

    功能：
    1. 获取同花顺概念和行业板块数据
    2. 计算板块强度指标
    3. 建立概念-行业关联（通过成分股重叠）
    4. 识别热点板块
    """

    def __init__(self, data_manager):
        self.dm = data_manager
        self._concept_list: Optional[pd.DataFrame] = None
        self._industry_list: Optional[pd.DataFrame] = None
        self._member_cache: Dict[str, pd.DataFrame] = {}  # 成分股缓存

    def _load_sector_list(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """加载同花顺板块列表（概念+行业）"""
        if self._concept_list is None or self._industry_list is None:
            # 获取概念指数
            self._concept_list = self.dm.get_ths_index(index_type='概念指数')
            # 获取行业指数
            self._industry_list = self.dm.get_ths_index(index_type='行业指数')

            if self._concept_list.empty:
                logger.warning("[THSSectorTracker] 无法获取同花顺概念指数列表")
            else:
                logger.info(f"[THSSectorTracker] 加载 {len(self._concept_list)} 个概念板块")

            if self._industry_list.empty:
                logger.warning("[THSSectorTracker] 无法获取同花顺行业指数列表")
            else:
                logger.info(f"[THSSectorTracker] 加载 {len(self._industry_list)} 个行业板块")

        return self._concept_list, self._industry_list

    def get_sector_members(self, ts_code: str) -> pd.DataFrame:
        """
        获取板块成分股（带缓存）

        Args:
            ts_code: 板块代码（如 885001.TI）

        Returns:
            DataFrame: 成分股列表
        """
        if ts_code not in self._member_cache:
            members = self.dm.get_ths_member(ts_code=ts_code)
            self._member_cache[ts_code] = members
        return self._member_cache.get(ts_code, pd.DataFrame())

    def calculate_sector_overlap(self, ts_code1: str, ts_code2: str) -> float:
        """
        计算两个板块的成分股重叠度

        Returns:
            float: 重叠度（0-1），越高表示关联越强
        """
        members1 = self.get_sector_members(ts_code1)
        members2 = self.get_sector_members(ts_code2)

        if members1.empty or members2.empty:
            return 0.0

        # 获取股票代码集合
        codes1 = set(members1['code'].tolist()) if 'code' in members1.columns else set()
        codes2 = set(members2['code'].tolist()) if 'code' in members2.columns else set()

        if not codes1 or not codes2:
            return 0.0

        # 计算重叠度（Jaccard系数）
        intersection = len(codes1 & codes2)
        union = len(codes1 | codes2)

        return intersection / union if union > 0 else 0.0

    def find_related_sectors(self, ts_code: str, sector_type: str = None,
                            min_overlap: float = 0.3) -> List[Dict]:
        """
        查找与指定板块关联度高的其他板块

        Args:
            ts_code: 板块代码
            sector_type: 查找类型（'概念'/'行业'），None表示查找相反类型
            min_overlap: 最小重叠度阈值

        Returns:
            List[Dict]: 关联板块列表，包含 ts_code, name, overlap
        """
        concept_list, industry_list = self._load_sector_list()

        # 确定当前板块类型和要查找的类型
        current_type = None
        if concept_list is not None and ts_code in concept_list['ts_code'].values:
            current_type = '概念'
        elif industry_list is not None and ts_code in industry_list['ts_code'].values:
            current_type = '行业'

        if current_type is None:
            logger.warning(f"[find_related_sectors] 无法确定板块 {ts_code} 的类型")
            return []

        # 如果未指定查找类型，查找相反类型
        if sector_type is None:
            sector_type = '行业' if current_type == '概念' else '概念'

        # 获取目标列表
        target_list = industry_list if sector_type == '行业' else concept_list
        if target_list.empty:
            return []

        # 计算与所有目标板块的重叠度
        related = []
        for _, row in target_list.iterrows():
            target_code = row['ts_code']
            if target_code == ts_code:
                continue

            overlap = self.calculate_sector_overlap(ts_code, target_code)
            if overlap >= min_overlap:
                related.append({
                    'ts_code': target_code,
                    'name': row.get('name', ''),
                    'type': sector_type,
                    'overlap': overlap,
                    'overlap_pct': f"{overlap*100:.1f}%"
                })

        # 按重叠度排序
        related.sort(key=lambda x: x['overlap'], reverse=True)
        return related

    def analyze_sectors(self, trade_date: str, top_n: int = 20) -> pd.DataFrame:
        """
        分析同花顺板块（概念+行业）

        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 返回前N个热点板块

        Returns:
            DataFrame: 板块分析结果
        """
        # 1. 获取板块行情数据
        daily_df = self.dm.get_ths_daily(trade_date=trade_date)
        if daily_df.empty:
            logger.warning(f"[analyze_sectors] 无法获取 {trade_date} 板块行情")
            return pd.DataFrame()

        # 2. 加载板块列表
        concept_list, industry_list = self._load_sector_list()

        # 3. 合并板块类型信息
        if not concept_list.empty:
            concept_codes = set(concept_list['ts_code'].tolist())
        else:
            concept_codes = set()

        if not industry_list.empty:
            industry_codes = set(industry_list['ts_code'].tolist())
        else:
            industry_codes = set()

        # 4. 计算板块强度指标
        results = []
        for _, row in daily_df.iterrows():
            ts_code = row.get('ts_code', '')
            name = row.get('name', '')
            pct_change = row.get('pct_change', 0)
            amount = row.get('amount', 0)

            # 确定板块类型
            if ts_code in concept_codes:
                sector_type = '概念'
            elif ts_code in industry_codes:
                sector_type = '行业'
            else:
                continue  # 跳过未知类型

            results.append({
                'ts_code': ts_code,
                'name': name,
                'type': sector_type,
                'pct_change': pct_change,
                'amount': amount,
                'vol': row.get('vol', 0),
            })

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)

        # 5. 计算排名和综合得分
        # 按涨跌幅排名
        result_df['rank'] = result_df['pct_change'].rank(ascending=False, method='min')

        # 计算成交额排名（归一化）
        result_df['amount_rank'] = result_df['amount'].rank(ascending=False, method='min')
        max_rank = len(result_df)
        result_df['amount_score'] = (max_rank - result_df['amount_rank']) / max_rank * 100

        # 综合得分 = 涨幅得分 * 0.6 + 成交额得分 * 0.4
        result_df['composite_score'] = (
            result_df['pct_change'] * 6 +  # 涨幅直接加权
            result_df['amount_score'] * 0.4
        )

        # 标记热点板块（前20%且涨幅>3%）
        hot_threshold = len(result_df) * 0.2
        result_df['is_hot'] = (result_df['rank'] <= hot_threshold) & (result_df['pct_change'] > 3)

        # 6. 按综合得分排序
        result_df = result_df.sort_values('composite_score', ascending=False)

        return result_df.head(top_n)

    def get_hot_sectors_with_relation(self, trade_date: str, top_n: int = 10) -> pd.DataFrame:
        """
        获取热点板块及其关联板块

        Returns:
            DataFrame: 包含热点板块和关联的概念/行业
        """
        # 1. 获取热点板块
        hot_df = self.analyze_sectors(trade_date, top_n=top_n)
        if hot_df.empty:
            return pd.DataFrame()

        # 2. 为每个热点板块查找关联板块
        results = []
        for _, row in hot_df.iterrows():
            ts_code = row['ts_code']
            sector_type = row['type']

            # 查找关联板块（相反类型）
            related = self.find_related_sectors(ts_code, min_overlap=0.2)

            # 只保留前3个关联度最高的
            top_related = related[:3]

            results.append({
                'ts_code': ts_code,
                'name': row['name'],
                'type': sector_type,
                'pct_change': row['pct_change'],
                'composite_score': row['composite_score'],
                'is_hot': row['is_hot'],
                'related_sectors': top_related,
                'related_count': len(top_related)
            })

        return pd.DataFrame(results)

    def get_sector_stocks(self, ts_code: str, trade_date: str,
                         limit_up_df: pd.DataFrame = None) -> Dict:
        """
        获取板块内的股票详情

        Args:
            ts_code: 板块代码
            trade_date: 交易日期
            limit_up_df: 涨停池数据（用于标记涨停股）

        Returns:
            Dict: 板块内股票信息
        """
        # 1. 获取成分股
        members = self.get_sector_members(ts_code)
        if members.empty:
            return {}

        # 2. 获取板块行情
        sector_daily = self.dm.get_ths_daily(ts_code=ts_code, trade_date=trade_date)

        # 3. 统计涨停股
        up_stocks = []
        if limit_up_df is not None and not limit_up_df.empty:
            # 从涨停池筛选属于该板块的股票
            # 处理成分股代码列（可能是 'con_code' 或 'code'）
            if 'con_code' in members.columns:
                # con_code 格式: 000831.SZ -> 提取前6位
                member_codes = set(members['con_code'].astype(str).str.replace(r'\.\w+$', '', regex=True).str.zfill(6).tolist())
            elif 'code' in members.columns:
                member_codes = set(members['code'].astype(str).str.zfill(6).tolist())
            else:
                member_codes = set()

            if '代码' in limit_up_df.columns:
                for _, stock in limit_up_df.iterrows():
                    # 涨停池代码格式: 000039.SZ -> 提取前6位
                    code = str(stock.get('代码', '')).replace('.SZ', '').replace('.SH', '').replace('.BJ', '').zfill(6)
                    if code in member_codes:
                        up_stocks.append({
                            'code': code,
                            'name': stock.get('名称', ''),
                            'limit_up_time': stock.get('首次封板时间', ''),
                            'board_height': stock.get('连板数', 1)
                        })

        return {
            'ts_code': ts_code,
            'member_count': len(members),
            'up_count': len(up_stocks),
            'up_stocks': up_stocks,
            'sector_daily': sector_daily.to_dict() if not sector_daily.empty else {}
        }

    # ==================== 兼容旧接口的方法 ====================

    def analyze_sectors_persistence(self, trade_date: str, top_n: int = 10) -> pd.DataFrame:
        """
        兼容旧接口：分析板块持续性

        返回与旧SectorRotationTracker兼容的DataFrame格式
        """
        # 获取热点板块分析结果
        hot_df = self.analyze_sectors(trade_date, top_n=top_n * 2)  # 获取更多数据用于筛选

        if hot_df.empty:
            return pd.DataFrame()

        # 获取涨停池数据（用于计算涨停家数）
        limit_up_df = self.dm.get_limit_up_pool(trade_date)

        # 转换为旧接口格式
        results = []
        for _, row in hot_df.head(top_n).iterrows():
            ts_code = row['ts_code']

            # 获取板块详细信息（包含涨停家数）
            sector_detail = self.get_sector_stocks(ts_code, trade_date, limit_up_df)
            up_count = sector_detail.get('up_count', 0) if sector_detail else 0
            up_stocks = sector_detail.get('up_stocks', []) if sector_detail else []

            # 计算连续涨停数（连板数>=2的股票数量）
            cons_count = sum(1 for s in up_stocks if s.get('board_height', 1) >= 2)

            # 根据涨跌幅判断所处阶段
            pct_change = row['pct_change']
            if pct_change > 5:
                stage = '高潮期'
            elif pct_change > 3:
                stage = '加速期'
            elif pct_change > 1:
                stage = '萌芽期'
            else:
                stage = '衰退期'

            results.append({
                '板块名称': row['name'],
                '当前排名': int(row['rank']),
                '涨停家数': up_count,
                '连续涨停数': cons_count,
                '综合评分': row['composite_score'],
                '所处阶段': stage,
                '市场周期': '上升期',  # 简化处理
                '成交额变化': 0.0,  # 需要历史数据计算
                '换手率': 0.0,
                '排名动量': 0.0,
                '涨停趋势': 0.0,
                '持续性评分': row['composite_score'],
                '操作建议': '积极关注' if row['is_hot'] else '观望',
                '建议仓位': 'medium' if row['is_hot'] else 'light',
                '紧急度': '高' if row['is_hot'] else '低',
                '策略理由': f"涨幅{pct_change:.2f}%，{'热点板块' if row['is_hot'] else '普通板块'}"
            })

        return pd.DataFrame(results)

    def get_hot_industries_from_sectors(self, top_n: int = 5) -> List[str]:
        """
        兼容旧接口：从板块获取热门行业

        返回热门行业名称列表
        """
        # 使用当前日期
        trade_date = datetime.now().strftime("%Y%m%d")

        # 获取热点板块
        hot_df = self.analyze_sectors(trade_date, top_n=top_n * 2)

        if hot_df.empty:
            return []

        # 筛选行业类型的热点板块
        industries = hot_df[hot_df['type'] == '行业']['name'].head(top_n).tolist()

        # 如果没有足够的行业，补充概念板块
        if len(industries) < top_n:
            concepts = hot_df[hot_df['type'] == '概念']['name'].head(top_n - len(industries)).tolist()
            industries.extend(concepts)

        return industries[:top_n]

    def analyze_with_validation(self, trade_date: str, top_n: int = 10,
                                hot_industries: List[str] = None) -> pd.DataFrame:
        """
        兼容旧接口：带交叉验证的板块分析

        简化的交叉验证实现，基于同花顺概念-行业关联
        """
        # 获取板块持续性分析结果
        persistence_df = self.analyze_sectors_persistence(trade_date, top_n=top_n)

        if persistence_df.empty:
            return pd.DataFrame()

        # 添加交叉验证相关字段
        results = []
        for _, row in persistence_df.iterrows():
            sector_name = row['板块名称']

            # 查找关联板块
            related = self._get_related_sector_names(sector_name)

            # 判断信号类型
            if hot_industries and sector_name in hot_industries:
                signal_type = '强势'
                signal_strength = '强'
                resonance_score = 85
            elif related and hot_industries:
                # 检查是否有重叠
                overlap = set(related) & set(hot_industries)
                if overlap:
                    signal_type = '共振'
                    signal_strength = '中'
                    resonance_score = 70
                else:
                    signal_type = '独立'
                    signal_strength = '弱'
                    resonance_score = 50
            else:
                signal_type = '普通'
                signal_strength = '弱'
                resonance_score = 40

            results.append({
                **row,
                '主要行业': related[0] if related else sector_name,
                '行业集中度': f"{len(related)}个",
                '信号类型': signal_type,
                '信号强度': signal_strength,
                '共振得分': resonance_score,
                '调整后仓位': row['建议仓位'],
                '验证理由': f"关联板块: {', '.join(related[:3])}" if related else "无关联数据"
            })

        return pd.DataFrame(results)

    def _get_related_sector_names(self, sector_name: str) -> List[str]:
        """获取关联板块名称列表（内部辅助方法）"""
        # 查找板块代码
        concept_list, industry_list = self._load_sector_list()

        ts_code = None
        if not concept_list.empty and sector_name in concept_list['name'].values:
            ts_code = concept_list[concept_list['name'] == sector_name]['ts_code'].iloc[0]
        elif not industry_list.empty and sector_name in industry_list['name'].values:
            ts_code = industry_list[industry_list['name'] == sector_name]['ts_code'].iloc[0]

        if not ts_code:
            return []

        # 查找关联板块
        related = self.find_related_sectors(ts_code, min_overlap=0.2)
        return [r['name'] for r in related[:5]]

    def calculate_unified_mainline(self, limit_cpt_df: pd.DataFrame,
                                   hierarchy_df: pd.DataFrame,
                                   top_n: int = 5) -> pd.DataFrame:
        """
        兼容旧接口：统一主线计算

        基于同花顺数据的简化实现
        """
        trade_date = datetime.now().strftime("%Y%m%d")
        return self.analyze_with_validation(trade_date, top_n=top_n)

    def detect_market_cycle(self, trade_date: str = None) -> str:
        """
        兼容旧接口：检测市场周期

        简化的市场周期判断
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y%m%d")

        # 获取板块数据
        df = self.analyze_sectors(trade_date, top_n=50)

        if df.empty:
            return '回暖期'

        # 计算平均涨跌幅
        avg_change = df['pct_change'].mean()
        hot_count = df[df['is_hot']].shape[0]

        # 简单判断
        if avg_change > 4 and hot_count >= 10:
            return '高潮期'
        elif avg_change > 2 and hot_count >= 5:
            return '上升期'
        elif avg_change > 0:
            return '回暖期'
        else:
            return '退潮期'


if __name__ == "__main__":
    # 测试
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from core.data.data_manager import DataManager
    from config.settings import TUSHARE_TOKEN, CACHE_DIR

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    tracker = THSSectorTracker(dm)

    # 测试板块分析
    print("="*80)
    print("同花顺板块分析测试")
    print("="*80)

    trade_date = datetime.now().strftime("%Y%m%d")
    result_df = tracker.analyze_sectors(trade_date, top_n=20)

    if not result_df.empty:
        print(f"\n热点板块TOP20:")
        print(result_df[['name', 'type', 'pct_change', 'composite_score', 'is_hot']].to_string(index=False))

        # 测试关联分析
        print("\n" + "="*80)
        print("概念-行业关联分析")
        print("="*80)

        hot_with_relation = tracker.get_hot_sectors_with_relation(trade_date, top_n=5)
        for _, row in hot_with_relation.iterrows():
            print(f"\n【{row['name']}】({row['type']}) 涨幅:{row['pct_change']:.2f}%")
            if row['related_sectors']:
                print("  关联板块:")
                for rel in row['related_sectors']:
                    print(f"    - {rel['name']}({rel['type']}): 重叠度{rel['overlap_pct']}")
    else:
        print("未获取到数据")
