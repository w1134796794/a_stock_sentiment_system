"""
行业层级映射管理器 - 统一使用同花顺行业体系

同花顺行业体系：概念指数/行业指数/特色指数 -> 成分股
使用Tushare的ths_index和ths_member接口
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import loguru

logger = loguru.logger


class THSIndustryMapper:
    """
    同花顺行业映射类
    处理同花顺概念指数/行业指数/特色指数 -> 成分股 的映射关系
    使用Tushare的ths_index和ths_member接口
    """

    def __init__(self, data_manager=None):
        """
        初始化同花顺行业映射器

        Args:
            data_manager: DataManager实例（用于调用Tushare接口），
                         或文件路径字符串（兼容旧接口，此时仅加载本地缓存）
        """
        if isinstance(data_manager, (str, Path)):
            logger.info("[THSIndustryMapper] 收到文件路径参数，将仅使用本地缓存模式")
            self.dm = None
            self._mapping_file = Path(data_manager)
        else:
            self.dm = data_manager
            self._mapping_file = None

        # Phase 1：只读仓库（仅在有 dm 时构造透传）
        if self.dm is not None:
            from core.data.repository import StockRepository
            self.repo = StockRepository.passthrough(self.dm)
        else:
            self.repo = None

        self.index_df = None
        self.index_cache = {}
        self._load_index_list()

    def _load_index_list(self):
        """加载同花顺板块指数列表"""
        if self.dm is None:
            logger.warning("[THSIndustryMapper] 未提供DataManager，无法加载板块列表")
            return

        try:
            self.index_df = self.repo.get_ths_index()
            if not self.index_df.empty:
                logger.info(f"[THSIndustryMapper] 加载同花顺板块指数: {len(self.index_df)}个")
            else:
                logger.warning("[THSIndustryMapper] 板块指数列表为空")
        except Exception as e:
            logger.error(f"[THSIndustryMapper] 加载板块指数列表失败: {e}")

    def get_all_indices(self, index_type: str = None) -> pd.DataFrame:
        """获取所有板块指数"""
        if self.index_df is None or self.index_df.empty:
            self._load_index_list()

        if self.index_df is None or self.index_df.empty:
            return pd.DataFrame()

        if index_type:
            return self.index_df[self.index_df.get('type', '') == index_type].copy()

        return self.index_df.copy()

    def get_index_by_name(self, name: str) -> Optional[pd.Series]:
        """根据名称查找板块指数（支持模糊匹配）"""
        if self.index_df is None or self.index_df.empty:
            return None

        exact_match = self.index_df[self.index_df['name'] == name]
        if not exact_match.empty:
            return exact_match.iloc[0]

        fuzzy_match = self.index_df[self.index_df['name'].str.contains(name, na=False)]
        if not fuzzy_match.empty:
            return fuzzy_match.iloc[0]

        return None

    def get_index_members(self, ts_code: str, use_cache: bool = True) -> pd.DataFrame:
        """获取板块成分股"""
        if use_cache and ts_code in self.index_cache:
            return self.index_cache[ts_code].copy()

        if self.dm is None:
            logger.warning("[THSIndustryMapper] 未提供DataManager，无法获取成分股")
            return pd.DataFrame()

        try:
            members_df = self.repo.get_ths_member(ts_code)
            if not members_df.empty:
                self.index_cache[ts_code] = members_df.copy()
            return members_df
        except Exception as e:
            logger.error(f"[THSIndustryMapper] 获取板块{ts_code}成分股失败: {e}")
            return pd.DataFrame()

    def find_index_by_stock(self, stock_code: str) -> pd.DataFrame:
        """根据股票代码查找所属板块"""
        if self.index_df is None or self.index_df.empty:
            return pd.DataFrame()

        stock_code = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

        result = []
        for _, idx_row in self.index_df.iterrows():
            ts_code = idx_row['ts_code']
            members = self.get_index_members(ts_code)

            if not members.empty:
                member_codes = members['code'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True)
                if stock_code in member_codes.values:
                    result.append({
                        'ts_code': ts_code,
                        'name': idx_row['name'],
                        'type': idx_row.get('type', ''),
                        'market': idx_row.get('market', '')
                    })

        return pd.DataFrame(result)

    def get_concept_indices(self) -> pd.DataFrame:
        """获取所有概念指数"""
        return self.get_all_indices(index_type='概念指数')

    def get_industry_indices(self) -> pd.DataFrame:
        """获取所有行业指数"""
        return self.get_all_indices(index_type='行业指数')

    def get_feature_indices(self) -> pd.DataFrame:
        """获取所有特色指数"""
        return self.get_all_indices(index_type='特色指数')

    def build_stock_concept_mapping(self, stock_list: List[str]) -> Dict[str, List[str]]:
        """构建股票到概念板块的映射"""
        mapping = {}

        concept_df = self.get_concept_indices()
        if concept_df.empty:
            return mapping

        for stock_code in stock_list:
            stock_code_clean = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
            concepts = []

            for _, idx_row in concept_df.iterrows():
                ts_code = idx_row['ts_code']
                members = self.get_index_members(ts_code)

                if not members.empty:
                    member_codes = members['code'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True)
                    if stock_code_clean in member_codes.values:
                        concepts.append(idx_row['name'])

            mapping[stock_code] = concepts

        return mapping

    def build_hierarchy_dataframe(self, limit_up_df: pd.DataFrame) -> pd.DataFrame:
        """
        构建层级化数据框（兼容原DCIndustryMapper接口）

        输入涨停池数据，输出带行业层级的结构化数据
        """
        if limit_up_df.empty:
            return pd.DataFrame()

        result = []
        for _, row in limit_up_df.iterrows():
            code = row.get('代码', row.get('ts_code', ''))
            name = row.get('名称', row.get('name', ''))
            industry = row.get('所属行业', '')
            concept = row.get('所属概念', row.get('concept', ''))

            board_height = row.get('连板数', 1)
            if pd.isna(board_height):
                board_height = 1
            board_height = int(board_height)

            result.append({
                'L1_Industry': industry,
                'L2_Industry': industry,
                'Code': code,
                'Name': name,
                'ChangePct': row.get('涨跌幅', row.get('pct_change', 0)),
                'LimitUpTime': row.get('首次封板时间', row.get('first_time', '')),
                'LastLimitUpTime': row.get('最后封板时间', row.get('last_time', '')),
                'OpenTimes': row.get('炸板次数', row.get('open_times', 0)),
                'BoardHeight': board_height,
                'Concept': concept
            })

        return pd.DataFrame(result)

    def refresh_cache(self):
        """刷新缓存"""
        self.index_cache.clear()
        self._load_index_list()
        logger.info("[THSIndustryMapper] 缓存已刷新")


IndustryMapper = THSIndustryMapper


if __name__ == "__main__":
    print("=" * 50)
    print("测试同花顺行业映射 (THSIndustryMapper)")
    print("=" * 50)

    try:
        from core.data.data_manager_main import DataManager
        from config.settings import TUSHARE_TOKEN, CACHE_DIR

        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        ths_mapper = THSIndustryMapper(dm)

        concept_df = ths_mapper.get_concept_indices()
        print(f"概念指数数量: {len(concept_df)}")

        industry_df = ths_mapper.get_industry_indices()
        print(f"行业指数数量: {len(industry_df)}")

        if not concept_df.empty:
            print(f"\n前5个概念指数:")
            for _, row in concept_df.head(5).iterrows():
                print(f"  {row['ts_code']}: {row['name']}")
    except Exception as e:
        print(f"测试需要Tushare token: {e}")