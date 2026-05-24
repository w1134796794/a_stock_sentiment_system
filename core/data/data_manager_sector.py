"""
板块数据管理模块 - 同花顺板块指数、行情、成分股、资金流向

数据来源：Tushare 同花顺接口
- ths_index: 同花顺板块指数列表
- ths_daily: 同花顺板块指数行情
- ths_member: 同花顺板块成分股
- moneyflow_ind_dc: 板块资金流向（接口名含dc但非东财行业体系）
"""
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import loguru

from core.data.data_manager_base import DataManagerBase

logger = loguru.logger


class SectorDataManager(DataManagerBase):
    """板块数据管理器（统一使用同花顺数据）"""

    def get_ths_index(self, index_type: str = None) -> pd.DataFrame:
        """获取同花顺板块指数列表"""
        if not self.ts_pro:
            logger.warning("[get_ths_index] Tushare未初始化")
            return pd.DataFrame()

        type_suffix = f"_{index_type}" if index_type else "_all"
        cache_file = self.sector_dir / "ths_index" / f"index{type_suffix}.csv"

        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 1:
                return pd.read_csv(cache_file)

        try:
            df = self.ts_pro.ths_index(exchange='A', type=index_type).dropna()
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                return df
            else:
                logger.warning(f"[get_ths_index] 返回空数据")
        except Exception as e:
            logger.error(f"[get_ths_index] 获取同花顺板块指数异常: {e}")

        return pd.DataFrame()

    def get_ths_daily(self, ts_code: str = None, trade_date: str = None,
                      start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """获取同花顺板块指数行情"""
        if not self.ts_pro:
            logger.warning("[get_ths_daily] Tushare未初始化")
            return pd.DataFrame()

        if trade_date and (start_date or end_date):
            logger.warning("[get_ths_daily] trade_date与start_date/end_date互斥，优先使用trade_date")
            start_date = None
            end_date = None

        if trade_date:
            cache_key = f"{ts_code}_{trade_date}" if ts_code else f"all_{trade_date}"
        else:
            cache_key = f"{ts_code}_{start_date}_{end_date}" if ts_code else f"all_{start_date}_{end_date}"
        cache_file = self.sector_dir / "ths_daily" / f"{cache_key}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            params = {}
            if ts_code:
                params['ts_code'] = ts_code
            if trade_date:
                params['trade_date'] = trade_date
            if start_date:
                params['start_date'] = start_date
            if end_date:
                params['end_date'] = end_date

            df = self.ts_pro.ths_daily(**params)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                return df
            else:
                logger.warning(f"[get_ths_daily] 返回空数据")
        except Exception as e:
            logger.error(f"[get_ths_daily] 获取同花顺板块行情异常: {e}")

        return pd.DataFrame()

    def get_ths_member(self, ts_code: str) -> pd.DataFrame:
        """获取同花顺概念板块成分列表"""
        if not self.ts_pro:
            logger.warning("[get_ths_member] Tushare未初始化")
            return pd.DataFrame()

        if not ts_code:
            logger.warning("[get_ths_member] 必须提供ts_code参数")
            return pd.DataFrame()

        cache_file = self.sector_dir / "ths_member" / f"{ts_code}.csv"

        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 7:
                return pd.read_csv(cache_file)

        try:
            df = self.ts_pro.ths_member(ts_code=ts_code)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                return df
            else:
                logger.warning(f"[get_ths_member] {ts_code} 返回空数据")
        except Exception as e:
            logger.error(f"[get_ths_member] 获取同花顺板块{ts_code}成分异常: {e}")

        return pd.DataFrame()

    def get_ths_members_batch(self, ts_codes: List[str]) -> Dict[str, pd.DataFrame]:
        """批量获取多个同花顺板块的成分股"""
        results = {}
        codes_to_fetch = []

        for code in ts_codes:
            cache_key = f"ths_member_{code}"
            cached_data = self._get_from_memory_cache(cache_key)
            if cached_data is not None:
                results[code] = cached_data
                continue

            cache_file = self.sector_dir / "ths_member" / f"{code}.csv"
            if cache_file.exists():
                try:
                    file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
                    if (datetime.now() - file_time).days < 7:
                        df = pd.read_csv(cache_file)
                        results[code] = df
                        self._set_memory_cache(cache_key, df)
                        continue
                except Exception as e:
                    logger.debug(f"[批量获取成分] 读取缓存失败 {code}: {e}")

            codes_to_fetch.append(code)

        if codes_to_fetch and self.ts_pro:
            logger.info(f"[批量获取成分] 需要获取 {len(codes_to_fetch)} 个板块成分")
            for code in codes_to_fetch:
                try:
                    df = self.ts_pro.ths_member(ts_code=code)
                    if df is not None and not df.empty:
                        cache_file = self.sector_dir / "ths_member" / f"{code}.csv"
                        df.to_csv(cache_file, index=False)
                        cache_key = f"ths_member_{code}"
                        self._set_memory_cache(cache_key, df)
                        results[code] = df
                    else:
                        logger.warning(f"[批量获取成分] {code} 返回空数据")
                except Exception as e:
                    logger.error(f"[批量获取成分] 获取 {code} 失败: {e}")
                time.sleep(0.1)

        logger.info(f"[批量获取成分] 完成: {len(results)}/{len(ts_codes)} 个板块")
        return results

    def get_stock_sectors(self, stock_code: str) -> pd.DataFrame:
        """
        反向查询：根据股票代码获取所属所有同花顺板块

        第一步：用 ths_member(code=stock_code) 拿到该股票所有的板块 ts_code
        第二步：用 ths_index 的 type 字段分类（N=概念, I=行业, S=特色指数等）

        Args:
            stock_code: 股票代码（如 '002031.SZ'）

        Returns:
            DataFrame: 包含字段 ts_code, name, type, market
        """
        if not self.ts_pro:
            logger.warning("[get_stock_sectors] Tushare未初始化")
            return pd.DataFrame()

        if not stock_code:
            logger.warning("[get_stock_sectors] 必须提供stock_code参数")
            return pd.DataFrame()

        code_with_suffix = stock_code
        if '.' not in stock_code:
            from core.utils.stock_code_utils import StockCodeUtils
            code_with_suffix = StockCodeUtils.standardize_code(stock_code, add_suffix=True)
            if not code_with_suffix:
                logger.warning(f"[get_stock_sectors] 无法标准化代码: {stock_code}")
                return pd.DataFrame()

        cache_file = self.sector_dir / "stock_sectors" / f"{code_with_suffix}.csv"

        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 7:
                return pd.read_csv(cache_file)

        try:
            df = self.ts_pro.ths_member(con_code=code_with_suffix)

            if df is None or df.empty:
                logger.debug(f"[get_stock_sectors] {stock_code} 未找到所属板块")
                return pd.DataFrame()

            ths_index = self.get_ths_index()
            if ths_index.empty:
                logger.warning("[get_stock_sectors] ths_index为空，无法分类板块类型")
                return df

            if 'type' in ths_index.columns:
                merge_cols = ['ts_code', 'name', 'type']
                for extra_col in ['exchange']:
                    if extra_col in ths_index.columns:
                        merge_cols.append(extra_col)
                ths_index_filtered = ths_index[ths_index['type'].isin(['N', 'I'])]
                result = df.merge(
                    ths_index_filtered[merge_cols],
                    on='ts_code',
                    how='inner'
                )
                if 'name' in result.columns:
                    result['name'] = result['name'].fillna(result.get('ts_code', ''))
            else:
                result = df

            cache_file.parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(cache_file, index=False)
            logger.debug(f"[get_stock_sectors] {stock_code} 属于 {len(result)} 个板块")

            return result

        except Exception as e:
            logger.error(f"[get_stock_sectors] 获取 {stock_code} 的板块归属异常: {e}")
            return pd.DataFrame()

    def get_stock_sectors_batch(self, stock_codes: List[str]) -> Dict[str, pd.DataFrame]:
        """
        批量获取多个股票的板块归属

        Args:
            stock_codes: 股票代码列表（支持带后缀或不带后缀格式）

        Returns:
            Dict[str, DataFrame]: stock_code → 板块归属DataFrame
        """
        results = {}
        codes_to_fetch = []

        from core.utils.stock_code_utils import StockCodeUtils
        for code in stock_codes:
            code_with_suffix = code if '.' in code else StockCodeUtils.standardize_code(code, add_suffix=True)
            if not code_with_suffix:
                code_with_suffix = code

            cache_file = self.sector_dir / "stock_sectors" / f"{code_with_suffix}.csv"
            if cache_file.exists():
                file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if (datetime.now() - file_time).days < 7:
                    df = pd.read_csv(cache_file)
                    results[code] = df
                    continue
            codes_to_fetch.append(code)

        if codes_to_fetch:
            logger.info(f"[get_stock_sectors_batch] 需要获取 {len(codes_to_fetch)} 只股票的板块归属")
            for code in codes_to_fetch:
                df = self.get_stock_sectors(code)
                results[code] = df
                time.sleep(0.05)

        return results

    def get_sectors_daily_batch(self,
                                ts_codes: List[str],
                                trade_date: str = None,
                                start_date: str = None,
                                end_date: str = None) -> Dict[str, pd.DataFrame]:
        """批量获取多个板块的日线数据"""
        results = {}
        codes_to_fetch = []

        for code in ts_codes:
            if trade_date:
                cache_key = f"sector_daily_{code}_{trade_date}"
                cache_file = self.sector_dir / "ths_daily" / f"{code}_{trade_date}.csv"
            else:
                cache_key = f"sector_daily_{code}_{start_date}_{end_date}"
                cache_file = self.sector_dir / "ths_daily" / f"{code}_{start_date}_{end_date}.csv"

            cached_data = self._get_from_memory_cache(cache_key)
            if cached_data is not None:
                results[code] = cached_data
                continue

            if cache_file.exists():
                try:
                    df = pd.read_csv(cache_file)
                    results[code] = df
                    self._set_memory_cache(cache_key, df)
                    continue
                except Exception as e:
                    logger.debug(f"[批量获取板块] 读取缓存失败 {code}: {e}")

            codes_to_fetch.append(code)

        if codes_to_fetch and self.ts_pro:
            logger.info(f"[批量获取板块] 需要获取 {len(codes_to_fetch)} 个板块数据")
            for code in codes_to_fetch:
                try:
                    params = {'ts_code': code}
                    if trade_date:
                        params['trade_date'] = trade_date
                    if start_date:
                        params['start_date'] = start_date
                    if end_date:
                        params['end_date'] = end_date

                    df = self.ts_pro.ths_daily(**params)
                    if df is not None and not df.empty:
                        if trade_date:
                            cache_file = self.sector_dir / "ths_daily" / f"{code}_{trade_date}.csv"
                        else:
                            cache_file = self.sector_dir / "ths_daily" / f"{code}_{start_date}_{end_date}.csv"
                        df.to_csv(cache_file, index=False)

                        if trade_date:
                            cache_key = f"sector_daily_{code}_{trade_date}"
                        else:
                            cache_key = f"sector_daily_{code}_{start_date}_{end_date}"
                        self._set_memory_cache(cache_key, df)
                        results[code] = df
                    else:
                        logger.warning(f"[批量获取板块] {code} 返回空数据")
                except Exception as e:
                    logger.error(f"[批量获取板块] 获取 {code} 失败: {e}")
                time.sleep(0.1)

        logger.info(f"[批量获取板块] 完成: {len(results)}/{len(ts_codes)} 个板块")
        return results

    def get_all_sectors_daily(self, trade_date: str = None) -> pd.DataFrame:
        """获取所有同花顺板块的日线数据（单次请求）"""
        cache_key = f"all_sectors_{trade_date}" if trade_date else "all_sectors_latest"

        cached_data = self._get_from_batch_cache(cache_key)
        if cached_data is not None:
            return cached_data

        if trade_date:
            cache_file = self.sector_dir / "ths_daily" / f"all_{trade_date}.csv"
        else:
            cache_file = self.sector_dir / "ths_daily" / "all_latest.csv"

        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                self._set_batch_cache(cache_key, df)
                return df
            except Exception as e:
                logger.debug(f"[获取全部板块] 读取缓存失败: {e}")

        if not self.ts_pro:
            logger.warning("[获取全部板块] Tushare未初始化")
            return pd.DataFrame()

        try:
            params = {}
            if trade_date:
                params['trade_date'] = trade_date

            df = self.ts_pro.ths_daily(**params)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                self._set_batch_cache(cache_key, df)
                logger.info(f"[获取全部板块] 成功获取 {len(df)} 条板块数据")
                return df
            else:
                logger.warning("[获取全部板块] 返回空数据")
        except Exception as e:
            logger.error(f"[获取全部板块] 获取失败: {e}")

        return pd.DataFrame()

    def get_industry_sector_data(self, trade_date: str) -> pd.DataFrame:
        """
        获取行业板块数据（使用同花顺行业指数数据）

        替代原东财dc_index接口，使用同花顺ths_daily + ths_index组合：
        1. 获取同花顺行业指数列表
        2. 获取行业指数日线行情
        3. 计算衍生因子

        Args:
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            行业板块数据DataFrame，包含涨跌幅、成分股涨跌数等因子
        """
        if not self.ts_pro:
            return pd.DataFrame()

        cache_file = self.sector_dir / "industry" / f"{trade_date}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            industry_index_df = self.get_ths_index(index_type='行业指数')
            if industry_index_df.empty:
                logger.warning("[get_industry_sector_data] 同花顺行业指数列表为空")
                return pd.DataFrame()

            industry_codes = industry_index_df['ts_code'].tolist()
            logger.info(f"[get_industry_sector_data] 获取到 {len(industry_codes)} 个同花顺行业指数")

            daily_data = self.get_all_sectors_daily(trade_date)
            if daily_data.empty:
                daily_data = self.get_sectors_daily_batch(industry_codes, trade_date=trade_date)
                if not daily_data:
                    return pd.DataFrame()
                daily_df = pd.concat(daily_data.values(), ignore_index=True)
            else:
                daily_df = daily_data[daily_data['ts_code'].isin(industry_codes)].copy()

            if daily_df.empty:
                logger.warning("[get_industry_sector_data] 行业指数日线数据为空")
                return pd.DataFrame()

            result_df = daily_df.merge(
                industry_index_df[['ts_code', 'name']],
                on='ts_code', how='left'
            )

            result_df['up_down_ratio'] = 0.5
            result_df['up_down_diff'] = 0
            result_df['leading_strength'] = result_df.get('pct_change', 0) * 0.5
            result_df['activity_score'] = 0.0
            result_df['composite_strength'] = result_df.get('pct_change', 0) * 0.5

            result_df.to_csv(cache_file, index=False)
            logger.info(f"[get_industry_sector_data] 获取同花顺行业板块数据: {len(result_df)}个板块")
            return result_df

        except Exception as e:
            logger.error(f"[get_industry_sector_data] 获取行业板块数据失败: {e}")
            return pd.DataFrame()

    def get_sector_factors(self, industry_name: str = None, ts_code: str = None,
                           trade_date: str = None) -> dict:
        """获取指定行业的因子数据"""
        df = self.get_industry_sector_data(trade_date)
        if df.empty:
            return {}

        if ts_code:
            matched = df[df['ts_code'] == ts_code]
            if not matched.empty:
                row = matched.iloc[0]
                return self._row_to_factor_dict(row)

        if industry_name:
            matched = df[df['name'] == industry_name]
            if not matched.empty:
                row = matched.iloc[0]
                return self._row_to_factor_dict(row)

        return {}

    def _row_to_factor_dict(self, row) -> dict:
        """将DataFrame行转换为因子字典"""
        return {
            'industry_ts_code': row.get('ts_code', ''),
            'industry_name': row.get('name', ''),
            'pct_change': row.get('pct_change', 0),
            'up_num': row.get('up_num', 0),
            'down_num': row.get('down_num', 0),
            'up_down_ratio': row.get('up_down_ratio', 0),
            'up_down_diff': row.get('up_down_diff', 0),
            'leading': row.get('leading', ''),
            'leading_pct': row.get('leading_pct', 0),
            'leading_strength': row.get('leading_strength', 0),
            'turnover_rate': row.get('turnover_rate', 0),
            'activity_score': row.get('activity_score', 0),
            'composite_strength': row.get('composite_strength', 0),
            'total_mv': row.get('total_mv', 0)
        }

    def get_sector_moneyflow(self, trade_date: str, sector_type: str = '行业') -> pd.DataFrame:
        """获取板块资金流向数据（使用Tushare moneyflow_ind_dc接口）"""
        cache_file = self.sector_dir / "moneyflow" / f"{sector_type}_{trade_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)

        if not self.ts_pro:
            logger.warning("Tushare未初始化，无法获取板块资金流向数据")
            return pd.DataFrame()

        try:
            df = self.ts_pro.moneyflow_ind_dc(trade_date=trade_date, type=sector_type)
            if not df.empty:
                df['net_amount'] = df.get('buy_sm_amount', 0) / 10000
                df['net_mamount'] = df.get('buy_md_amount', 0) / 10000
                df['net_damount'] = df.get('buy_lg_amount', 0) / 10000
                df['total_net_amount'] = df['net_amount'] + df['net_mamount'] + df['net_damount']
                df.to_csv(cache_file, index=False)
                logger.info(f"获取板块资金流向数据成功: {len(df)}个板块")
                return df
            else:
                logger.warning("无法获取板块资金流向数据")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"获取板块资金流向数据失败: {e}")
            return pd.DataFrame()

    def get_sector_capital_flow_type(self, sector_name: str = None, ts_code: str = None,
                                     trade_date: str = None) -> Dict:
        """分析板块资金流向类型"""
        industry_df = self.get_sector_moneyflow(trade_date, '行业')
        concept_df = self.get_sector_moneyflow(trade_date, '概念')
        all_sectors = pd.concat([industry_df, concept_df], ignore_index=True)

        if all_sectors.empty:
            return {
                'capital_flow_type': 'UNKNOWN',
                'large_net': 0, 'medium_net': 0, 'small_net': 0, 'total_net': 0,
                'description': '数据获取失败'
            }

        matched = pd.DataFrame()
        if ts_code and 'ts_code' in all_sectors.columns:
            matched = all_sectors[all_sectors['ts_code'] == ts_code]

        if matched.empty and sector_name:
            matched = all_sectors[all_sectors['name'] == sector_name]

        if matched.empty and sector_name:
            matched = all_sectors[all_sectors['name'].str.contains(sector_name, na=False, case=False)]
            if matched.empty:
                matched = all_sectors[all_sectors['name'].apply(
                    lambda x: sector_name in str(x) if pd.notna(x) else False
                )]

        if matched.empty:
            return {
                'capital_flow_type': 'UNKNOWN',
                'large_net': 0, 'medium_net': 0, 'small_net': 0, 'total_net': 0,
                'description': f'未找到板块[{sector_name or ts_code}]的资金流向数据'
            }

        sector_data = matched.iloc[0]
        large_net = sector_data.get('net_damount', 0)
        medium_net = sector_data.get('net_mamount', 0)
        small_net = sector_data.get('net_amount', 0)
        total_net = sector_data.get('total_net_amount', large_net + medium_net + small_net)

        if large_net > medium_net + small_net and large_net > 0:
            flow_type = 'INSTITUTION_LEADING'
            description = '机构主导'
        elif small_net > large_net + medium_net and small_net > 0:
            flow_type = 'RETAIL_LEADING'
            description = '散户主导'
        elif total_net > 0:
            flow_type = 'BALANCED'
            description = '均衡流入'
        elif total_net < 0:
            flow_type = 'NET_OUTFLOW'
            description = '净流出'
        else:
            flow_type = 'UNKNOWN'
            description = '数据不足'

        return {
            'capital_flow_type': flow_type,
            'large_net': large_net,
            'medium_net': medium_net,
            'small_net': small_net,
            'total_net': total_net,
            'description': description,
            'sector_name': sector_data.get('name', sector_name),
            'pct_change': sector_data.get('pct_change', 0)
        }