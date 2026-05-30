"""
股票代码工具模块 - 提供统一的股票代码格式化处理
"""
import re
from typing import Optional, List


class StockCodeUtils:
    """股票代码工具类 - 提供股票代码格式化和验证功能"""

    @staticmethod
    def standardize_code(stock_code: str, add_suffix: bool = True) -> str:
        """
        标准化股票代码格式

        Args:
            stock_code: 原始股票代码（如 '000001', '000001.SZ'）
            add_suffix: 是否添加后缀，True返回'000001.SZ'，False返回'000001'

        Returns:
            标准化后的股票代码
        """
        if not stock_code:
            return ""

        # 去除空格并转为字符串
        code = str(stock_code).strip()

        # 移除已有后缀
        code = code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

        # 补齐6位
        code = code.zfill(6)

        # 根据前缀判断交易所并添加后缀
        if add_suffix:
            if code.startswith('6'):
                return f"{code}.SH"
            elif code.startswith('8') or code.startswith('4') or code.startswith('9'):
                return f"{code}.BJ"
            elif code.startswith('0') or code.startswith('3'):
                return f"{code}.SZ"

        return code

    @staticmethod
    def remove_suffix(stock_code: str) -> str:
        """
        移除股票代码的后缀

        Args:
            stock_code: 股票代码（如 '000001.SZ'）

        Returns:
            无后缀的股票代码（如 '000001'）
        """
        if not stock_code:
            return ""

        code = str(stock_code).strip()
        return code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

    @staticmethod
    def get_exchange(stock_code: str) -> str:
        """
        获取股票所属交易所

        Args:
            stock_code: 股票代码

        Returns:
            交易所代码：'SH'(上海), 'SZ'(深圳), 'BJ'(北京), ''(未知)
        """
        if not stock_code:
            return ""

        code = str(stock_code).strip()

        # 检查后缀
        if '.SH' in code:
            return 'SH'
        elif '.SZ' in code:
            return 'SZ'
        elif '.BJ' in code:
            return 'BJ'

        # 根据前缀判断
        code = StockCodeUtils.standardize_code(code, False)
        if code.startswith('6'):
            return 'SH'
        elif code.startswith('8') or code.startswith('4') or code.startswith('9'):
            return 'BJ'
        elif code.startswith('0') or code.startswith('3'):
            return 'SZ'

        return ""

    @staticmethod
    def is_shanghai_stock(stock_code: str) -> bool:
        """判断是否为上海股票"""
        return StockCodeUtils.get_exchange(stock_code) == 'SH'

    @staticmethod
    def is_shenzhen_stock(stock_code: str) -> bool:
        """判断是否为深圳股票"""
        return StockCodeUtils.get_exchange(stock_code) == 'SZ'

    @staticmethod
    def is_beijing_stock(stock_code: str) -> bool:
        """判断是否为北京股票（北交所）"""
        return StockCodeUtils.get_exchange(stock_code) == 'BJ'

    @staticmethod
    def is_chuangyeban(stock_code: str) -> bool:
        """判断是否为创业板股票（300/301开头）"""
        code = StockCodeUtils.standardize_code(stock_code, add_suffix=False)
        return code.startswith('300') or code.startswith('301')

    @staticmethod
    def is_kechuangban(stock_code: str) -> bool:
        """判断是否为科创板股票（688开头）"""
        code = StockCodeUtils.remove_suffix(stock_code).zfill(6)
        return code.startswith('688')

    @staticmethod
    def is_zhongxiaoban(stock_code: str) -> bool:
        """判断是否为中小板股票（002开头）"""
        code = StockCodeUtils.standardize_code(stock_code, False)
        return code.startswith('002')

    @staticmethod
    def is_zhuban(stock_code: str) -> bool:
        """判断是否为主板股票（600/601/603/000/001开头）"""
        code = StockCodeUtils.standardize_code(stock_code, False)
        return (code.startswith('600') or code.startswith('601') or
                code.startswith('603') or code.startswith('605') or
                code.startswith('000') or code.startswith('001'))

    @staticmethod
    def batch_standardize(codes: List[str], add_suffix: bool = True) -> List[str]:
        """
        批量标准化股票代码

        Args:
            codes: 股票代码列表
            add_suffix: 是否添加后缀

        Returns:
            标准化后的代码列表
        """
        return [StockCodeUtils.standardize_code(code, add_suffix) for code in codes if code]

    @staticmethod
    def to_akshare_symbol(stock_code: str) -> str:
        """
        转换为AkShare格式的symbol

        Args:
            stock_code: 股票代码

        Returns:
            AkShare格式（如 'sh600001', 'sz000001'）
        """
        code = StockCodeUtils.standardize_code(stock_code, add_suffix=False)
        exchange = StockCodeUtils.get_exchange(stock_code)

        prefix_map = {
            'SH': 'sh',
            'SZ': 'sz',
            'BJ': 'bj'
        }

        prefix = prefix_map.get(exchange, 'sz')
        return f"{prefix}{code}"

    @staticmethod
    def from_akshare_symbol(symbol: str) -> str:
        """
        从AkShare格式转换为标准格式

        Args:
            symbol: AkShare格式（如 'sh600001'）

        Returns:
            标准格式（如 '600001.SH'）
        """
        if not symbol or len(symbol) < 8:
            return ""

        prefix = symbol[:2]
        code = symbol[2:].zfill(6)

        suffix_map = {
            'sh': '.SH',
            'sz': '.SZ',
            'bj': '.BJ'
        }

        suffix = suffix_map.get(prefix, '.SZ')
        return f"{code}{suffix}"

    @staticmethod
    def is_valid_code(stock_code: str) -> bool:
        """
        验证股票代码是否有效

        Args:
            stock_code: 股票代码

        Returns:
            是否有效
        """
        if not stock_code:
            return False

        code = StockCodeUtils.remove_suffix(stock_code)

        # 检查是否为6位数字
        if not re.match(r'^\d{6}$', code):
            return False

        # 检查前缀
        valid_prefixes = ('600', '601', '603', '605', '000', '001',
                          '002', '003', '300', '301', '688', '689')

        return code.startswith(valid_prefixes)

    @staticmethod
    def extract_code_from_text(text: str) -> List[str]:
        """
        从文本中提取股票代码

        Args:
            text: 文本内容

        Returns:
            提取到的股票代码列表
        """
        if not text:
            return []

        # 匹配6位数字
        pattern = r'\b(\d{6})\b'
        codes = re.findall(pattern, str(text))

        # 过滤有效的股票代码
        valid_codes = [code for code in codes if StockCodeUtils.is_valid_code(code)]

        return list(set(valid_codes))


# ==================== 字段命名标准化 ====================

# FieldNames 的权威定义已迁移至 core.utils.field_names。
# 此处仅做向后兼容 re-export。
from core.utils.field_names import FieldNames  # noqa: F401  (re-export)


class DataFrameFieldMapper:
    """DataFrame 字段映射工具"""

    @staticmethod
    def get_code_column(df, fields=None):
        """
        获取 DataFrame 中的股票代码列名
        
        Args:
            df: DataFrame
            fields: 候选字段列表，默认使用 FieldNames.STOCK_CODE_FIELDS
        
        Returns:
            列名字符串，如果未找到返回 None
        """
        if df is None or df.empty:
            return None
        
        if fields is None:
            fields = FieldNames.STOCK_CODE_FIELDS
        
        for col in fields:
            if col in df.columns:
                return col
        
        return None

    @staticmethod
    def get_name_column(df, fields=None):
        """
        获取 DataFrame 中的股票名称列名
        
        Args:
            df: DataFrame
            fields: 候选字段列表，默认使用 FieldNames.STOCK_NAME_FIELDS
        
        Returns:
            列名字符串，如果未找到返回 None
        """
        if df is None or df.empty:
            return None
        
        if fields is None:
            fields = FieldNames.STOCK_NAME_FIELDS
        
        for col in fields:
            if col in df.columns:
                return col
        
        return None

    @staticmethod
    def extract_codes(df, remove_suffix=True, fields=None):
        """
        从 DataFrame 中提取股票代码列表
        
        Args:
            df: DataFrame
            remove_suffix: 是否移除后缀
            fields: 候选字段列表，默认使用 FieldNames.STOCK_CODE_FIELDS
        
        Returns:
            代码列表（6位数字格式）
        """
        if df is None or df.empty:
            return []
        
        code_col = DataFrameFieldMapper.get_code_column(df, fields)
        if not code_col:
            return []
        
        codes = df[code_col].astype(str).tolist()
        
        if remove_suffix:
            codes = [StockCodeUtils.remove_suffix(c).zfill(6) for c in codes]
        
        return codes

    @staticmethod
    def standardize_column_names(df, mapping=None):
        """
        标准化 DataFrame 列名
        
        Args:
            df: DataFrame
            mapping: 自定义映射字典，如 {'ts_code': 'code'}
        
        Returns:
            列名标准化后的 DataFrame
        """
        if df is None or df.empty:
            return df
        
        df = df.copy()
        
        # 默认映射：将各种代码字段统一为 'code'
        default_mapping = {
            'ts_code': 'code',
            'con_code': 'code',
            '股票代码': 'code',
            '代码': 'code',
            'stock_code': 'code',
            'name': 'name',
            '股票名称': 'name',
            '名称': 'name',
            'stock_name': 'name',
        }
        
        if mapping:
            default_mapping.update(mapping)
        
        # 重命名列
        rename_dict = {}
        for old_col, new_col in default_mapping.items():
            if old_col in df.columns and new_col not in df.columns:
                rename_dict[old_col] = new_col
        
        if rename_dict:
            df = df.rename(columns=rename_dict)
        
        return df


# 保持向后兼容的函数接口
standardize_code = StockCodeUtils.standardize_code
remove_suffix = StockCodeUtils.remove_suffix
get_exchange = StockCodeUtils.get_exchange
is_shanghai_stock = StockCodeUtils.is_shanghai_stock
is_shenzhen_stock = StockCodeUtils.is_shenzhen_stock
is_beijing_stock = StockCodeUtils.is_beijing_stock
is_chuangyeban = StockCodeUtils.is_chuangyeban
is_kechuangban = StockCodeUtils.is_kechuangban
is_zhongxiaoban = StockCodeUtils.is_zhongxiaoban
is_zhuban = StockCodeUtils.is_zhuban
batch_standardize = StockCodeUtils.batch_standardize
to_akshare_symbol = StockCodeUtils.to_akshare_symbol
from_akshare_symbol = StockCodeUtils.from_akshare_symbol
is_valid_code = StockCodeUtils.is_valid_code
extract_code_from_text = StockCodeUtils.extract_code_from_text


if __name__ == "__main__":
    # 测试
    test_codes = ['000001', '600001', '000001.SZ', '300001', '688001', '920001']

    for code in test_codes:
        print(f"\n原始代码: {code}")
        print(f"  标准化(带后缀): {StockCodeUtils.standardize_code(code, True)}")
        print(f"  标准化(无后缀): {StockCodeUtils.standardize_code(code, False)}")
        print(f"  交易所: {StockCodeUtils.get_exchange(code)}")
        print(f"  AkShare格式: {StockCodeUtils.to_akshare_symbol(code)}")
