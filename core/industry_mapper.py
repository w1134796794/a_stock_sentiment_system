"""
行业层级映射管理器
处理L1(一级行业) -> L2(二级行业) -> L3(三级行业) -> 个股 的层级关系
使用 data/Industry_Mapping.csv 作为映射源
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple
import loguru

logger = loguru.logger

class IndustryMapper:
    def __init__(self, mapping_file: Path = None):
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
            
            logger.info(f"加载行业映射: {len(self.l1_l2_map)}个一级行业, {len(self.l2_l3_map)}个二级行业, {len(self.l3_to_l2_map)}个三级行业")
            
        except Exception as e:
            logger.error(f"加载映射文件失败: {e}")
            # 使用备用映射
            self._load_fallback_mapping()
    
    def _load_fallback_mapping(self):
        """加载备用映射（当CSV文件读取失败时使用）"""
        logger.warning("使用备用行业映射")
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
        """根据二级行业获取一级行业"""
        return self.l2_to_l1_map.get(l2_name, '其他')
    
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
        输入涨停池数据，输出带L1/L2/L3层级的结构化数据
        使用实际的连板数和所属行业数据
        """
        if limit_up_df.empty:
            return pd.DataFrame()
        
        result = []
        for _, row in limit_up_df.iterrows():
            code = row.get('代码', row.get('ts_code', ''))
            name = row.get('名称', row.get('name', ''))
            # 使用实际的所属行业字段
            l3_industry = row.get('所属行业', '')
            concept = row.get('所属概念', row.get('concept', ''))
            
            # 获取实际的连板数
            board_height = row.get('连板数', 1)
            if pd.isna(board_height):
                board_height = 1
            board_height = int(board_height)
            
            # 根据三级行业查找对应的二级和一级行业
            l2_industry = self.get_l2_by_l3(l3_industry)
            l1_industry = self.get_l1_by_l2(l2_industry)
            
            # 如果找不到映射（返回"其他"），可能是L2被当作L3的情况
            # 例如："电力"在映射中是L2，但数据中可能是L3
            if l2_industry == '其他' and l1_industry == '其他':
                # 尝试将输入当作L2来查找
                l1_from_l2 = self.get_l1_by_l2(l3_industry)
                if l1_from_l2 != '其他':
                    # 输入实际上是L2，需要找到对应的L3
                    # 使用L2下的第一个L3作为代表
                    l3_list = self.get_l3_by_l2(l3_industry)
                    if l3_list:
                        l2_industry = l3_industry
                        l1_industry = l1_from_l2
                        l3_industry = l3_list[0]  # 使用第一个L3
            
            result.append({
                'L1_Industry': l1_industry,  # 一级行业
                'L2_Industry': l2_industry,  # 二级行业
                'L3_Industry': l3_industry,  # 三级行业（使用实际数据）
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

if __name__ == "__main__":
    mapper = IndustryMapper()
    print("行业映射器初始化成功")
    print(f"一级行业数量: {len(mapper.get_all_l1())}")
    print(f"二级行业数量: {len(mapper.get_all_l2())}")
    print(f"三级行业数量: {len(mapper.get_all_l3())}")
    
    # 测试查找
    test_l3 = '光伏设备'
    l2 = mapper.get_l2_by_l3(test_l3)
    l1 = mapper.get_l1_by_l2(l2)
    print(f"\n测试: {test_l3} -> {l2} -> {l1}")
