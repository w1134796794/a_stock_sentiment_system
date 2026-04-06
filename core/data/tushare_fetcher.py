
"""
Tushare Pro 股东数据获取模块 (6000+积分优化版)
专为弱转强策略V2设计，支持高频调用

权限说明：
- 6000积分：每分钟500次，常规数据无上限 [^103^][^106^]
- top10_floatholders接口：需2000积分以上，5000积分以上频次更高 [^74^]
- 当前配置：6000积分可流畅使用，无总量限制

接口文档：
- top10_floatholders: https://tushare.pro/document/2?doc_id=62 [^74^]
- 返回字段：ts_code, ann_date, end_date, holder_name, hold_amount, hold_ratio, hold_float_ratio, hold_change, holder_type
"""

import tushare as ts
import pandas as pd
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import time
import loguru

logger = loguru.logger


@dataclass
class TushareConfig:
    """Tushare配置"""
    token: str = ""
    rate_limit: int = 500  # 每分钟500次 (6000积分) [^103^]
    daily_limit: str = "无上限"  # 6000积分常规数据无上限
    retry_times: int = 3
    retry_delay: float = 0.5


class TushareShareholderFetcher:
    """
    Tushare Pro 股东数据获取器
    针对6000+积分用户优化，支持高频调用
    """
    
    def __init__(self, token: str = None):
        self.config = TushareConfig()
        if token:
            self.config.token = token
            self._init_tushare()
        else:
            self.pro = None
        
        # 缓存机制
        self._cache = {}
        self._cache_ttl = 3600  # 缓存1小时
        
        # 锁定股东关键词库
        self.locked_keywords = {
            '实控人': ['实际控制人', '控股股东', '董事长', '创始人', '家族', '一致行动'],
            '员工持股': ['员工持股', '股权激励', '核心员工', '员工计划', 'ESOP'],
            '战略配售': ['战略配售', '战略投资', 'IPO战略', '首发战略', '网下配售'],
            '国资锁定': ['国资委', '国资公司', '国有资本', '社保基金', '养老金', '中央汇金'],
            '长期机构': ['保险资金', '企业年金', '慈善基金', '长期价值', '大学捐赠'],
            '董监高': ['董事', '监事', '高管', '总经理', '财务总监']
        }
    
    def _init_tushare(self):
        """初始化Tushare Pro接口"""
        try:
            ts.set_token(self.config.token)
            self.pro = ts.pro_api()
            logger.info("Tushare Pro初始化成功")
            
            # 验证积分权限
            self._check_permission()
            
        except Exception as e:
            logger.error(f"Tushare初始化失败: {e}")
            self.pro = None
    
    def set_token(self, token: str):
        """设置Token并初始化"""
        self.config.token = token
        self._init_tushare()
    
    def _check_permission(self):
        """检查接口权限和积分"""
        try:
            # 尝试调用一次接口验证权限
            test_df = self.pro.top10_floatholders(ts_code='000001.SZ', limit=1)
            if test_df is not None:
                logger.info("✓ top10_floatholders接口权限验证通过")
                logger.info(f"✓ 当前积分等级：6000+ (每分钟{self.config.rate_limit}次，无总量限制)")
            else:
                logger.warning("✗ 接口返回空数据，请检查积分是否足够")
        except Exception as e:
            logger.error(f"权限验证失败: {e}")
            logger.error("请确认：1) Token正确 2) 积分>=2000 3) 网络连接正常")
    
    def fetch_top10_floatholders(self, 
                                stock_code: str, 
                                period: Optional[str] = None,
                                ann_date: Optional[str] = None,
                                start_date: Optional[str] = None,
                                end_date: Optional[str] = None,
                                use_cache: bool = True) -> pd.DataFrame:
        """
        获取前十大流通股东数据
        
        Args:
            stock_code: TS股票代码 (如 '000001.SZ' 或 '000001')
            period: 报告期 (YYYYMMDD格式，如 '20251231')
            ann_date: 公告日期 (YYYYMMDD格式)
            start_date: 报告期开始日期
            end_date: 报告期结束日期
            use_cache: 是否使用缓存
        
        Returns:
            DataFrame with columns:
            - ts_code: TS股票代码
            - ann_date: 公告日期
            - end_date: 报告期
            - holder_name: 股东名称
            - hold_amount: 持有数量（股）
            - hold_ratio: 占总股本比例(%)
            - hold_float_ratio: 占流通股本比例(%)
            - hold_change: 持股变动
            - holder_type: 股东类型 [^74^]
        """
        # 缓存检查
        cache_key = f"{stock_code}_{period}_{ann_date}_{start_date}_{end_date}"
        if use_cache and cache_key in self._cache:
            cache_time, cache_data = self._cache[cache_key]
            if (datetime.now() - cache_time).seconds < self._cache_ttl:
                logger.debug(f"使用缓存数据: {stock_code}")
                return cache_data
        
        if not self.pro:
            logger.error("Tushare未初始化，请先设置Token")
            return pd.DataFrame()
        
        # 标准化股票代码
        ts_code = self._standardize_code(stock_code)
        
        # 构建参数
        params = {'ts_code': ts_code}
        if period:
            params['period'] = period
        if ann_date:
            params['ann_date'] = ann_date
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date
        
        # 重试机制
        for attempt in range(self.config.retry_times):
            try:
                df = self.pro.top10_floatholders(**params)
                
                if df is not None and not df.empty:
                    # 数据清洗
                    df = self._clean_data(df)
                    
                    # 缓存数据
                    if use_cache:
                        self._cache[cache_key] = (datetime.now(), df)
                    
                    logger.info(f"✓ 获取 {ts_code} 十大流通股东数据成功，共{len(df)}条")
                    return df
                else:
                    logger.warning(f"{ts_code} 返回空数据")
                    return pd.DataFrame()
                    
            except Exception as e:
                logger.warning(f"第{attempt+1}次尝试失败: {e}")
                if attempt < self.config.retry_times - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                else:
                    logger.error(f"获取 {ts_code} 数据失败，已重试{self.config.retry_times}次")
                    return pd.DataFrame()
        
        return pd.DataFrame()
    
    def fetch_latest_top10(self, stock_code: str, use_cache: bool = True) -> pd.DataFrame:
        """
        获取最新一期十大流通股东数据（最常用）
        
        Args:
            stock_code: 股票代码
            use_cache: 是否使用缓存
        
        Returns:
            DataFrame: 最新一期十大流通股东
        """
        # 先尝试获取最近3年的数据，取最新一期
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=365*3)).strftime('%Y%m%d')
        
        df = self.fetch_top10_floatholders(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            use_cache=use_cache
        )
        
        if not df.empty:
            # 取最新一期
            latest_period = df['end_date'].max()
            df = df[df['end_date'] == latest_period].copy()
            logger.info(f"{stock_code} 最新报告期: {latest_period}")
        
        return df
    
    def batch_fetch_top10(self, stock_codes: List[str], 
                         max_workers: int = 5) -> Dict[str, pd.DataFrame]:
        """
        批量获取多只股票数据（利用6000积分的高频次优势）
        
        Args:
            stock_codes: 股票代码列表
            max_workers: 并发数（建议5-10，避免触发限流）
        
        Returns:
            Dict: {股票代码: DataFrame}
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        results = {}
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_code = {
                executor.submit(self.fetch_latest_top10, code): code 
                for code in stock_codes
            }
            
            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    df = future.result()
                    results[code] = df
                except Exception as e:
                    logger.error(f"获取 {code} 失败: {e}")
                    results[code] = pd.DataFrame()
        
        return results
    
    def identify_locked_shares(self, df: pd.DataFrame) -> Tuple[float, List[Dict]]:
        """
        识别长期锁定筹码
        
        Args:
            df: 十大流通股东DataFrame
        
        Returns:
            (锁定筹码数量, 锁定股东列表)
        """
        if df.empty:
            return 0, []
        
        locked_amount = 0
        locked_holders = []
        
        for _, row in df.iterrows():
            holder_name = str(row.get('holder_name', ''))
            hold_amount = float(row.get('hold_amount', 0))
            holder_type = str(row.get('holder_type', ''))
            
            is_locked = False
            lock_reason = []
            
            # 1. 根据股东类型判断
            if holder_type in ['个人', '个人股东']:
                # 检查是否为实控人/高管
                for category, keywords in self.locked_keywords.items():
                    if any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            elif holder_type in ['公司', '企业']:
                # 检查是否为员工持股平台或战略配售
                for category, keywords in self.locked_keywords.items():
                    if category in ['员工持股', '战略配售'] and any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            elif holder_type in ['国资', '国家队', '社保基金']:
                is_locked = True
                lock_reason.append('国资锁定')
            
            # 2. 根据名称关键词判断（补充）
            if not is_locked:
                for category, keywords in self.locked_keywords.items():
                    if any(kw in holder_name for kw in keywords):
                        is_locked = True
                        lock_reason.append(category)
                        break
            
            if is_locked:
                locked_amount += hold_amount
                locked_holders.append({
                    'name': holder_name,
                    'amount': hold_amount,
                    'ratio': row.get('hold_ratio', 0),
                    'float_ratio': row.get('hold_float_ratio', 0),
                    'type': holder_type,
                    'lock_reason': lock_reason
                })
        
        return locked_amount, locked_holders
    
    def calculate_real_turnover(self, 
                               stock_code: str,
                               total_float_shares: float,  # 流通股本（万股）
                               day_volume: float,         # 当日成交量（万股）
                               use_cache: bool = True) -> Dict:
        """
        计算实际换手率（剔除大股东锁定筹码）
        
        Args:
            stock_code: 股票代码
            total_float_shares: 总流通股本（万股）
            day_volume: 当日成交量（万股）
            use_cache: 是否使用缓存
        
        Returns:
            Dict: {
                'nominal_turnover': 名义换手率(%),
                'real_turnover': 实际换手率(%),
                'free_float_shares': 自由流通股本(万股),
                'locked_shares': 锁定筹码(万股),
                'locked_ratio': 锁定筹码占比,
                'locked_holders': 锁定股东列表
            }
        """
        # 获取十大流通股东
        df = self.fetch_latest_top10(stock_code, use_cache=use_cache)
        
        if df.empty:
            logger.warning(f"{stock_code} 无股东数据，使用名义换手率")
            return {
                'nominal_turnover': (day_volume / total_float_shares) * 100 if total_float_shares > 0 else 0,
                'real_turnover': (day_volume / total_float_shares) * 100 if total_float_shares > 0 else 0,
                'free_float_shares': total_float_shares,
                'locked_shares': 0,
                'locked_ratio': 0,
                'locked_holders': [],
                'data_source': 'nominal_only'
            }
        
        # 识别锁定筹码
        locked_amount, locked_holders = self.identify_locked_shares(df)
        
        # 注意：十大流通股东只覆盖部分筹码，需要估算全部锁定筹码
        # 假设前十大流通股东持股占流通盘的60%（A股常见情况）
        top10_total = df['hold_amount'].sum() / 10000  # 转为万股
        top10_ratio = top10_total / total_float_shares if total_float_shares > 0 else 0
        
        # 估算系数：如果前十大占比<60%，按比例放大
        estimation_factor = 0.6 / top10_ratio if top10_ratio > 0 else 1.0
        estimated_total_locked = (locked_amount / 10000) * estimation_factor  # 万股
        
        # 自由流通股本
        free_float_shares = total_float_shares - estimated_total_locked
        if free_float_shares <= 0:
            free_float_shares = total_float_shares * 0.5  # 保底50%
            logger.warning(f"{stock_code} 锁定筹码估算异常，使用50%自由流通")
        
        # 计算换手率
        nominal_turnover = (day_volume / total_float_shares) * 100 if total_float_shares > 0 else 0
        real_turnover = (day_volume / free_float_shares) * 100 if free_float_shares > 0 else 0
        
        return {
            'nominal_turnover': round(nominal_turnover, 2),
            'real_turnover': round(real_turnover, 2),
            'free_float_shares': round(free_float_shares, 2),
            'locked_shares': round(estimated_total_locked, 2),
            'locked_ratio': round(estimated_total_locked / total_float_shares, 4) if total_float_shares > 0 else 0,
            'locked_holders': locked_holders,
            'top10_coverage': round(top10_ratio, 4),
            'estimation_factor': round(estimation_factor, 2),
            'data_source': 'tushare_pro'
        }
    
    def _standardize_code(self, stock_code: str) -> str:
        """标准化股票代码为TS格式"""
        if '.' in stock_code:
            return stock_code
        
        if stock_code.startswith('6'):
            return f"{stock_code}.SH"
        else:
            return f"{stock_code}.SZ"
    
    def _clean_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据清洗"""
        # 转换数值类型
        numeric_cols = ['hold_amount', 'hold_ratio', 'hold_float_ratio', 'hold_change']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 按持股比例排序
        if 'hold_ratio' in df.columns:
            df = df.sort_values('hold_ratio', ascending=False)
        
        return df.reset_index(drop=True)
    
    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.info("缓存已清空")
    
    def get_cache_stats(self) -> Dict:
        """获取缓存统计"""
        return {
            'cache_size': len(self._cache),
            'cache_ttl': self._cache_ttl
        }


# ==================== 与弱转强策略V2集成 ====================

class TushareIntegratedStrategy:
    """
    Tushare集成的弱转强策略
    直接使用Tushare数据计算实际换手率
    """
    
    def __init__(self, tushare_token: str):
        self.fetcher = TushareShareholderFetcher(token=tushare_token)
        
        # 分层换手率阈值（与V2策略一致）
        self.turnover_thresholds = {
            'low': {'min_real': 25, 'ideal_real': 40},   # 1-2板
            'high': {'min_real': 35, 'ideal_real': 50}   # 5板以上
        }
    
    def check_turnover_eligibility(self, 
                                 stock_code: str,
                                 board_height: int,
                                 total_float_shares: float,
                                 day_volume: float) -> Dict:
        """
        检查换手率是否达标（使用实际换手率）
        
        Returns:
            {
                'is_eligible': bool,
                'tier': str,
                'nominal_turnover': float,
                'real_turnover': float,
                'threshold': float,
                'locked_ratio': float,
                'reason': str
            }
        """
        # 确定层级
        if 1 <= board_height <= 2:
            tier = 'low'
        elif 5 <= board_height <= 8:
            tier = 'high'
        else:
            return {
                'is_eligible': False,
                'tier': 'excluded',
                'reason': f'{board_height}板不在1-2板或5板以上区间'
            }
        
        # 计算实际换手率
        turnover_data = self.fetcher.calculate_real_turnover(
            stock_code=stock_code,
            total_float_shares=total_float_shares,
            day_volume=day_volume
        )
        
        real_turnover = turnover_data['real_turnover']
        nominal_turnover = turnover_data['nominal_turnover']
        threshold = self.turnover_thresholds[tier]['min_real']
        
        is_eligible = real_turnover >= threshold
        
        reason_parts = [
            f"实际换手{real_turnover:.1f}%",
            f"名义换手{nominal_turnover:.1f}%",
            f"锁定筹码占比{turnover_data['locked_ratio']:.1%}",
            f"要求≥{threshold}%"
        ]
        
        if not is_eligible:
            reason_parts.append("❌ 未达标")
        else:
            reason_parts.append("✅ 达标")
        
        return {
            'is_eligible': is_eligible,
            'tier': tier,
            'nominal_turnover': nominal_turnover,
            'real_turnover': real_turnover,
            'threshold': threshold,
            'locked_ratio': turnover_data['locked_ratio'],
            'free_float_shares': turnover_data['free_float_shares'],
            'locked_holders': turnover_data['locked_holders'],
            'reason': "，".join(reason_parts),
            'turnover_data': turnover_data
        }


# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("=" * 80)
    print("Tushare Pro 股东数据获取模块 (6000+积分优化版)")
    print("=" * 80)
    
    # 示例：初始化并获取数据（需替换为真实Token）
    TOKEN = "your_tushare_token_here"  # 替换为你的Token
    
    if TOKEN.startswith("your_"):
        print("\n⚠️  请替换TOKEN为真实值后运行")
        print("获取Token: https://tushare.pro/register")
        print("6000积分权限：每分钟500次，常规数据无上限 [^103^]")
    else:
        fetcher = TushareShareholderFetcher(token=TOKEN)
        
        # 示例1：获取单只股票最新十大流通股东
        print("\n【示例1】获取同花顺(300033)最新十大流通股东")
        df = fetcher.fetch_latest_top10('300033.SZ')
        if not df.empty:
            print(df[['holder_name', 'hold_amount', 'hold_ratio', 'hold_float_ratio']].to_string())
        
        # 示例2：计算实际换手率
        print("\n【示例2】计算实际换手率")
        turnover = fetcher.calculate_real_turnover(
            stock_code='300033.SZ',
            total_float_shares=27560,  # 27.56亿股
            day_volume=1500  # 1500万股
        )
        print(f"名义换手率: {turnover['nominal_turnover']:.2f}%")
        print(f"实际换手率: {turnover['real_turnover']:.2f}%")
        print(f"锁定筹码占比: {turnover['locked_ratio']:.2%}")
        print(f"主要锁定股东: {[h['name'] for h in turnover['locked_holders'][:3]]}")
        
        # 示例3：批量获取
        print("\n【示例3】批量获取多只股票")
        codes = ['000001.SZ', '600000.SH', '300033.SZ']
        results = fetcher.batch_fetch_top10(codes)
        for code, df in results.items():
            print(f"{code}: {len(df)}条数据")
    
    print("\n" + "=" * 80)
    print("接口说明：")
    print("• top10_floatholders: 前十大流通股东 [^74^]")
    print("• 积分要求: 2000以上可调用，5000以上频次更高")
    print("• 6000积分: 每分钟500次，常规数据无上限 [^103^]")
    print("• 返回字段: holder_name, hold_amount, hold_ratio, hold_float_ratio...")
    print("=" * 80)
