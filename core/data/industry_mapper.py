"""
行业层级映射管理器
支持东财和同花顺两种行业体系

东财行业体系：L1(一级) -> L2(二级) -> L3(三级) -> 个股
同花顺行业体系：概念指数/行业指数/特色指数 -> 成分股
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import loguru

logger = loguru.logger


class DCIndustryMapper:
    """
    东财行业映射类
    处理L1(一级行业) -> L2(二级行业) -> L3(三级行业) -> 个股 的层级关系
    使用 data/Industry_Mapping.csv 作为映射源
    """
    
    def __init__(self, mapping_file: Path = None):
        """
        初始化东财行业映射器
        
        Args:
            mapping_file: 映射文件路径，默认使用 data/Industry_Mapping.csv
        """
        # 优先使用新的CSV文件
        csv_file = Path("g:/a_stock_sentiment_system/data/Industry_Mapping.csv")
        if csv_file.exists():
            self.mapping_file = csv_file
            self.file_type = 'csv'
        elif mapping_file and Path(mapping_file).exists():
            self.mapping_file = Path(mapping_file)
            self.file_type = 'excel'
        else:
            self.mapping_file = csv_file
            self.file_type = 'csv'
        
        self.l1_l2_map = {}  # 一级->二级映射
        self.l2_l3_map = {}  # 二级->三级映射
        self.l3_to_l2_map = {}  # 三级->二级映射
        self.l2_to_l1_map = {}  # 二级->一级映射
        self.l3_stocks_map = {}  # 三级->成分股映射
        self.stock_industry_map = {}  # 个股->行业映射
        self._load_mapping()
    
    def _load_mapping(self):
        """加载行业映射文件"""
        if not self.mapping_file.exists():
            logger.error(f"映射文件不存在: {self.mapping_file}")
            return
        
        try:
            # 读取CSV文件
            df = pd.read_csv(self.mapping_file, encoding='utf-8')
            
            # 列名映射（适配CSV文件的列名）
            # CSV格式: 一级行业,二级行业,三级行业
            l1_col = '一级行业'
            l2_col = '二级行业'  
            l3_col = '三级行业'
            
            # 构建映射关系
            for _, row in df.iterrows():
                l1 = row.get(l1_col, '其他')
                l2 = row.get(l2_col, '其他')
                l3 = row.get(l3_col, '其他')
                
                # 跳过空值
                if pd.isna(l1) or pd.isna(l2) or pd.isna(l3):
                    continue
                
                # L1 -> L2
                if l1 not in self.l1_l2_map:
                    self.l1_l2_map[l1] = []
                if l2 not in self.l1_l2_map[l1]:
                    self.l1_l2_map[l1].append(l2)
                
                # L2 -> L3
                if l2 not in self.l2_l3_map:
                    self.l2_l3_map[l2] = []
                if l3 not in self.l2_l3_map[l2]:
                    self.l2_l3_map[l2].append(l3)
                
                # 反向映射
                self.l3_to_l2_map[l3] = l2
                self.l2_to_l1_map[l2] = l1
            
            logger.info(f"[DCIndustryMapper] 加载行业映射: {len(self.l1_l2_map)}个一级行业, {len(self.l2_l3_map)}个二级行业, {len(self.l3_to_l2_map)}个三级行业")
            
        except Exception as e:
            logger.error(f"[DCIndustryMapper] 加载映射文件失败: {e}")
            # 使用备用映射
            self._load_fallback_mapping()
    
    def _load_fallback_mapping(self):
        """加载备用映射（当CSV文件读取失败时使用）"""
        logger.warning("[DCIndustryMapper] 使用备用行业映射")
        # 这里可以添加一些常用的行业映射作为备用
        pass
    
    def update_l3_stocks(self, l3_name: str, stocks_df: pd.DataFrame):
        """更新三级行业成分股"""
        if not stocks_df.empty:
            self.l3_stocks_map[l3_name] = stocks_df
            # 反向映射
            code_col = '代码' if '代码' in stocks_df.columns else 'ts_code'
            for code in stocks_df[code_col].values:
                self.stock_industry_map[code] = l3_name
    
    def get_l3_by_l2(self, l2_name: str) -> List[str]:
        """获取二级行业下的所有三级行业"""
        return self.l2_l3_map.get(l2_name, [])
    
    def get_l2_by_l3(self, l3_name: str) -> str:
        """根据三级行业获取二级行业"""
        return self.l3_to_l2_map.get(l3_name, '其他')
    
    def get_l1_by_l2(self, l2_name: str) -> str:
        """根据二级行业获取一级行业，使用包含关系匹配"""
        if not l2_name:
            return '其他'
        
        # 1. 精确匹配
        if l2_name in self.l2_to_l1_map:
            return self.l2_to_l1_map[l2_name]
        
        # 2. 包含关系匹配：遍历所有二级行业，检查是否有包含关系
        # 例如：l2_name="光学光电"，mapped_l2="光学光电子"，"光学光电子"包含"光学光电"
        for mapped_l2, l1 in self.l2_to_l1_map.items():
            # 检查mapped_l2是否包含l2_name（如"光学光电子"包含"光学光电"）
            # 或者l2_name是否包含mapped_l2
            if mapped_l2 in l2_name or l2_name in mapped_l2:
                return l1
        
        return '其他'
    
    def get_all_l1(self) -> List[str]:
        """获取所有一级行业"""
        return list(self.l1_l2_map.keys())
    
    def get_all_l2(self) -> List[str]:
        """获取所有二级行业"""
        return list(self.l2_l3_map.keys())
    
    def get_all_l3(self) -> List[str]:
        """获取所有三级行业"""
        return list(self.l3_to_l2_map.keys())
    
    def classify_stock_to_l3(self, stock_name: str, concept: str = "") -> str:
        """根据股票名称和概念分类到L3行业（简化规则匹配）"""
        # 这里可以实现更复杂的匹配逻辑
        mapping_keywords = {
            '服务器': ['服务器', '算力', 'IDC', '数据中心'],
            '芯片设计': ['芯片', '半导体', 'IC设计'],
            '光伏设备': ['光伏', '太阳能', '逆变器'],
            '锂电池': ['锂电池', '储能', '电池'],
            '汽车电子': ['汽车电子', '智能座舱', '自动驾驶']
        }
        
        for l3, keywords in mapping_keywords.items():
            for kw in keywords:
                if kw in stock_name or kw in concept:
                    return l3
        return "其他"
    
    def build_hierarchy_dataframe(self, limit_up_df: pd.DataFrame) -> pd.DataFrame:
        """
        构建层级化数据框
        输入涨停池数据，输出带L1/L2层级的结构化数据
        注：涨停池数据返回的是二级行业，直接使用即可
        """
        if limit_up_df.empty:
            return pd.DataFrame()
        
        result = []
        for _, row in limit_up_df.iterrows():
            code = row.get('代码', row.get('ts_code', ''))
            name = row.get('名称', row.get('name', ''))
            # 涨停池返回的是二级行业，直接使用
            l2_industry = row.get('所属行业', '')
            concept = row.get('所属概念', row.get('concept', ''))
            
            # 获取实际的连板数
            board_height = row.get('连板数', 1)
            if pd.isna(board_height):
                board_height = 1
            board_height = int(board_height)
            
            # 根据二级行业查找对应的一级行业
            l1_industry = self.get_l1_by_l2(l2_industry)
            
            result.append({
                'L1_Industry': l1_industry,  # 一级行业
                'L2_Industry': l2_industry,  # 二级行业（直接使用原始数据）
                'Code': code,
                'Name': name,
                'ChangePct': row.get('涨跌幅', row.get('pct_change', 0)),
                'LimitUpTime': row.get('首次封板时间', row.get('first_time', '')),
                'LastLimitUpTime': row.get('最后封板时间', row.get('last_time', '')),
                'OpenTimes': row.get('炸板次数', row.get('open_times', 0)),
                'BoardHeight': board_height,  # 实际的连板数
                'Concept': concept
            })
        
        return pd.DataFrame(result)
    
    def get_industry_path(self, l3_industry: str) -> Tuple[str, str, str]:
        """
        获取行业的完整路径 (L1, L2, L3)
        返回: (一级行业, 二级行业, 三级行业)
        """
        l2 = self.get_l2_by_l3(l3_industry)
        l1 = self.get_l1_by_l2(l2)
        return (l1, l2, l3_industry)


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
            data_manager: DataManager实例，用于调用Tushare接口
        """
        self.dm = data_manager
        self.index_df = None  # 板块指数列表
        self.index_cache = {}  # 板块成分缓存 {ts_code: DataFrame}
        self._load_index_list()
    
    def _load_index_list(self):
        """加载同花顺板块指数列表"""
        if self.dm is None:
            logger.warning("[THSIndustryMapper] 未提供DataManager，无法加载板块列表")
            return
        
        try:
            self.index_df = self.dm.get_ths_index()
            if not self.index_df.empty:
                logger.info(f"[THSIndustryMapper] 加载同花顺板块指数: {len(self.index_df)}个")
            else:
                logger.warning("[THSIndustryMapper] 板块指数列表为空")
        except Exception as e:
            logger.error(f"[THSIndustryMapper] 加载板块指数列表失败: {e}")
    
    def get_all_indices(self, index_type: str = None) -> pd.DataFrame:
        """
        获取所有板块指数
        
        Args:
            index_type: 指数类型，可选 '概念指数'/'行业指数'/'特色指数'，默认返回全部
            
        Returns:
            板块指数DataFrame
        """
        if self.index_df is None or self.index_df.empty:
            self._load_index_list()
        
        if self.index_df is None or self.index_df.empty:
            return pd.DataFrame()
        
        if index_type:
            return self.index_df[self.index_df.get('type', '') == index_type].copy()
        
        return self.index_df.copy()
    
    def get_index_by_name(self, name: str) -> Optional[pd.Series]:
        """
        根据名称查找板块指数
        
        Args:
            name: 板块名称（支持模糊匹配）
            
        Returns:
            匹配的板块信息Series，未找到返回None
        """
        if self.index_df is None or self.index_df.empty:
            return None
        
        # 精确匹配
        exact_match = self.index_df[self.index_df['name'] == name]
        if not exact_match.empty:
            return exact_match.iloc[0]
        
        # 模糊匹配
        fuzzy_match = self.index_df[self.index_df['name'].str.contains(name, na=False)]
        if not fuzzy_match.empty:
            return fuzzy_match.iloc[0]
        
        return None
    
    def get_index_members(self, ts_code: str, use_cache: bool = True) -> pd.DataFrame:
        """
        获取板块成分股
        
        Args:
            ts_code: 板块指数代码（如'885001.TI'）
            use_cache: 是否使用缓存
            
        Returns:
            成分股DataFrame
        """
        # 检查缓存
        if use_cache and ts_code in self.index_cache:
            return self.index_cache[ts_code].copy()
        
        if self.dm is None:
            logger.warning("[THSIndustryMapper] 未提供DataManager，无法获取成分股")
            return pd.DataFrame()
        
        try:
            members_df = self.dm.get_ths_member(ts_code)
            if not members_df.empty:
                # 缓存结果
                self.index_cache[ts_code] = members_df.copy()
            return members_df
        except Exception as e:
            logger.error(f"[THSIndustryMapper] 获取板块{ts_code}成分股失败: {e}")
            return pd.DataFrame()
    
    def find_index_by_stock(self, stock_code: str) -> pd.DataFrame:
        """
        根据股票代码查找所属板块
        
        Args:
            stock_code: 股票代码（如'000001.SZ'或'000001'）
            
        Returns:
            所属板块DataFrame
        """
        if self.index_df is None or self.index_df.empty:
            return pd.DataFrame()
        
        # 标准化股票代码
        stock_code = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
        
        result = []
        for _, idx_row in self.index_df.iterrows():
            ts_code = idx_row['ts_code']
            members = self.get_index_members(ts_code)
            
            if not members.empty:
                # 检查股票是否在成分股中
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
        """
        构建股票到概念板块的映射
        
        Args:
            stock_list: 股票代码列表
            
        Returns:
            {股票代码: [概念板块名称列表]}
        """
        mapping = {}
        
        # 获取所有概念指数
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
    
    def refresh_cache(self):
        """刷新缓存（重新加载板块列表和成分）"""
        self.index_cache.clear()
        self._load_index_list()
        logger.info("[THSIndustryMapper] 缓存已刷新")


# 为了向后兼容，保留IndustryMapper作为DCIndustryMapper的别名
IndustryMapper = DCIndustryMapper


if __name__ == "__main__":
    # 测试东财行业映射
    print("=" * 50)
    print("测试东财行业映射 (DCIndustryMapper)")
    print("=" * 50)
    
    dc_mapper = DCIndustryMapper()
    print(f"一级行业数量: {len(dc_mapper.get_all_l1())}")
    print(f"二级行业数量: {len(dc_mapper.get_all_l2())}")
    print(f"三级行业数量: {len(dc_mapper.get_all_l3())}")
    
    # 测试查找
    test_l3 = '光伏设备'
    l2 = dc_mapper.get_l2_by_l3(test_l3)
    l1 = dc_mapper.get_l1_by_l2(l2)
    print(f"\n测试: {test_l3} -> {l2} -> {l1}")
    
    # 测试同花顺行业映射（需要DataManager）
    print("\n" + "=" * 50)
    print("测试同花顺行业映射 (THSIndustryMapper)")
    print("=" * 50)
    
    try:
        from core.data.data_manager import DataManager
        from config.settings import TUSHARE_TOKEN, CACHE_DIR
        
        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        ths_mapper = THSIndustryMapper(dm)
        
        # 获取概念指数列表
        concept_df = ths_mapper.get_concept_indices()
        print(f"概念指数数量: {len(concept_df)}")
        
        if not concept_df.empty:
            print(f"\n前5个概念指数:")
            for _, row in concept_df.head(5).iterrows():
                print(f"  {row['ts_code']}: {row['name']}")
    except Exception as e:
        print(f"同花顺映射测试需要Tushare token: {e}")
