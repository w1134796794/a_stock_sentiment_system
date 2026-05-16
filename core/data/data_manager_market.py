"""
市场数据管理模块 - 每日行情基础、涨停池、跌停池、实时行情、连板天梯

数据来源：Tushare
- daily_basic: 每日行情基础
- limit_list_d: 涨跌停列表
- rt_k: 实时行情
- limit_cpt_list: 最强板块统计
- limit_step: 连板天梯
"""
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Optional
import loguru

from core.data.data_manager_base import DataManagerBase

logger = loguru.logger


class MarketDataManager(DataManagerBase):
    """市场数据管理器"""

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """获取每日行情基础数据（带缓存）"""
        cache_file = self.market_dir / "daily_basic" / f"{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            if self.ts_pro:
                df = self.ts_pro.daily_basic(trade_date=trade_date)
                if not df.empty:
                    df.to_csv(cache_file, index=False)
                    return df
        except Exception as e:
            logger.error(f"Tushare获取daily_basic失败: {e}")

        return pd.DataFrame()

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取指数日线行情（Tushare index_daily 接口）

        支持的上证/深证/创业板指数代码：
        - 000001.SH  上证指数
        - 399001.SZ  深证成指
        - 399006.SZ  创业板指
        - 000016.SH  上证50
        - 000300.SH  沪深300
        - 399005.SZ  中小板指
        - 000688.SH  科创50
        - 000852.SH  中证1000

        返回字段：ts_code, trade_date, close, open, high, low, pre_close,
                  change, pct_chg, vol, amount

        Args:
            ts_code: 指数代码（如 000001.SH）
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD）

        Returns:
            DataFrame: 指数日线数据
        """
        code_clean = ts_code.split('.')[0]
        cache_file = self.market_dir / "index_daily" / f"{code_clean}_{start_date}_{end_date}.csv"

        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                if not df.empty:
                    return df
            except Exception:
                pass

        if not self.ts_pro:
            logger.warning("[get_index_daily] Tushare未初始化")
            return pd.DataFrame()

        try:
            df = self.ts_pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                logger.warning(f"[get_index_daily] {ts_code} 无数据")
                return pd.DataFrame()

            df = df.sort_values('trade_date')
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_index_daily] {ts_code} 获取 {len(df)} 条数据")
            return df
        except Exception as e:
            logger.error(f"[get_index_daily] {ts_code} 获取失败: {e}")
            return pd.DataFrame()

    def get_limit_up_pool(self, date: str) -> pd.DataFrame:
        """获取涨停池数据（使用Tushare limit_list_d接口）"""
        cache_file = self.market_dir / "limit_up" / f"{date}.csv"

        if cache_file.exists():
            df = pd.read_csv(cache_file)
            df = self._normalize_limit_up_format(df, date)
            self._append_to_limit_up_summary(df, date)
            return df

        if not self.ts_pro:
            logger.warning("[get_limit_up_pool] Tushare未初始化")
            return pd.DataFrame()

        try:
            df = self.ts_pro.limit_list_d(trade_date=date)
            if df.empty:
                logger.warning(f"[get_limit_up_pool] {date} 未获取到数据")
                return pd.DataFrame()

            df = df[df['limit'] == 'U'].copy()
            if df.empty:
                logger.info(f"[get_limit_up_pool] {date} 无涨停股票")
                return pd.DataFrame()

            df = self._normalize_limit_up_format(df, date)
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_limit_up_pool] 获取 {date} 涨停池数据: {len(df)}条")
            self._append_to_limit_up_summary(df, date)
            return df

        except Exception as e:
            logger.error(f"[get_limit_up_pool] 获取 {date} 涨停池数据失败: {e}")
            return pd.DataFrame()

    def get_limit_down_pool(self, date: str) -> pd.DataFrame:
        """获取跌停池数据（使用Tushare limit_list_d接口）"""
        cache_file = self.market_dir / "limit_down" / f"{date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)

        if not self.ts_pro:
            logger.warning("[get_limit_down_pool] Tushare未初始化")
            return pd.DataFrame()

        try:
            df = self.ts_pro.limit_list_d(trade_date=date)
            if df.empty:
                logger.warning(f"[get_limit_down_pool] {date} 未获取到数据")
                return pd.DataFrame()

            df = df[df['limit'] == 'D'].copy()
            if df.empty:
                logger.info(f"[get_limit_down_pool] {date} 无跌停股票")
                return pd.DataFrame()

            df.to_csv(cache_file, index=False)
            logger.info(f"[get_limit_down_pool] 获取 {date} 跌停池数据: {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"[get_limit_down_pool] 获取 {date} 跌停池数据失败: {e}")
            return pd.DataFrame()

    def get_all_rt_k_data(self, trade_date: str) -> pd.DataFrame:
        """批量获取所有股票的实时日线数据（rt_k接口）"""
        cache_file = self.market_dir / "rt_k" / f"{trade_date}.csv"

        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                if not df.empty:
                    return df
            except Exception as e:
                logger.warning(f"[get_all_rt_k_data] 读取缓存失败: {e}")

        if not self.ts_pro:
            logger.warning("[get_all_rt_k_data] Tushare未初始化")
            return pd.DataFrame()

        try:
            df = self.ts_pro.rt_k(ts_code='3*.SZ,6*.SH,0*.SZ,9*.BJ')
            if df is None or df.empty:
                logger.warning("[get_all_rt_k_data] rt_k返回空数据")
                return pd.DataFrame()

            df.to_csv(cache_file, index=False)
            logger.info(f"[get_all_rt_k_data] 成功获取并缓存所有股票实时数据: {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"[get_all_rt_k_data] 获取所有股票实时数据失败: {e}")
            return pd.DataFrame()

    def get_limit_cpt_list(self, trade_date: str) -> pd.DataFrame:
        """获取最强板块统计数据（limit_cpt_list接口）"""
        if not self.ts_pro:
            logger.warning("[get_limit_cpt_list] Tushare未初始化")
            return pd.DataFrame()

        cache_file = self.market_dir / "limit_cpt" / f"{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            df = self.ts_pro.limit_cpt_list(trade_date=trade_date)
            if df is not None and not df.empty:
                if 'ts_code' in df.columns:
                    df = df[df['ts_code'] != '885699.TI'].copy()
                elif 'code' in df.columns:
                    df = df[df['code'] != '885699.TI'].copy()
                logger.info(f"[get_limit_cpt_list] 过滤ST板块后剩余: {len(df)}条")

                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_cpt_list] {trade_date} 返回空数据")

        except Exception as e:
            logger.error(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据异常: {e}")

        return pd.DataFrame()

    def get_limit_step(self, trade_date: str, ts_code: str = None) -> pd.DataFrame:
        """获取连板天梯数据（limit_step接口）"""
        if not self.ts_pro:
            logger.warning("[get_limit_step] Tushare未初始化")
            return pd.DataFrame()

        cache_file = self.market_dir / "limit_step" / f"{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            params = {'trade_date': trade_date}
            if ts_code:
                params['ts_code'] = ts_code

            df = self.ts_pro.limit_step(**params)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_step] 获取 {trade_date} 连板天梯数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_step] {trade_date} 返回空数据")

        except Exception as e:
            logger.error(f"[get_limit_step] 获取 {trade_date} 连板天梯数据异常: {e}")

        return pd.DataFrame()

    def _normalize_limit_up_format(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """标准化涨停数据格式，统一列名以兼容现有代码"""
        if df.empty:
            return df

        result_df = df.copy()

        if '代码' in result_df.columns:
            return result_df

        column_mapping = {
            'ts_code': '代码',
            'name': '名称',
            'close': '最新价',
            'pct_chg': '涨跌幅',
            'industry': '所属行业',
            'first_time': '首次封板时间',
            'last_time': '最后封板时间',
            'open_times': '炸板次数',
            'limit_times': '连板数',
            'up_stat': '涨停统计',
            'amount': '成交额',
            'turnover_ratio': '换手率',
            'float_mv': '流通市值',
            'total_mv': '总市值'
        }

        for old_col, new_col in column_mapping.items():
            if old_col in result_df.columns:
                result_df[new_col] = result_df[old_col]

        if 'limit_amount' in result_df.columns:
            result_df['板上成交金额'] = result_df['limit_amount']
        if 'fd_amount' in result_df.columns:
            result_df['封单金额'] = result_df['fd_amount']

        if '连板数' in result_df.columns:
            result_df['连板数'] = result_df['连板数'].fillna(1)
        if '涨停统计' in result_df.columns:
            result_df['涨停统计'] = result_df['涨停统计'].fillna('1/1')

        return result_df

    def _append_to_limit_up_summary(self, df: pd.DataFrame, trade_date: str):
        """将涨停数据追加到汇总文件"""
        try:
            if not self.date_utils.is_trade_date(trade_date):
                return

            summary_file = self.summary_dir / "limit_up_stocks.csv"
            df_copy = df.copy()
            df_copy['trade_date'] = trade_date

            key_columns = ['trade_date', '代码', '名称', '涨跌幅', '最新价',
                          '成交额', '流通市值', '所属行业', '涨停封单量',
                          '涨停封单额', '首次封板时间', '最后封板时间', '炸板次数', '连板数']

            existing_columns = [col for col in key_columns if col in df_copy.columns]
            df_summary = df_copy[existing_columns].copy()

            if summary_file.exists():
                try:
                    existing_df = pd.read_csv(summary_file)
                    if trade_date in existing_df['trade_date'].astype(str).values:
                        return

                    if '代码' in df.columns and not existing_df.empty:
                        latest_date = existing_df['trade_date'].astype(str).max()
                        latest_df = existing_df[existing_df['trade_date'].astype(str) == latest_date]
                        if len(latest_df) == len(df):
                            current_codes = set(df['代码'].astype(str))
                            latest_codes = set(latest_df['代码'].astype(str))
                            if current_codes == latest_codes:
                                return
                except Exception:
                    pass

            header = not summary_file.exists()
            df_summary.to_csv(summary_file, mode='a', header=header, index=False)
            action = "创建汇总文件并写入" if header else "追加"
            logger.info(f"{action}{len(df_summary)}条涨停数据: {summary_file}")

        except Exception as e:
            logger.warning(f"追加涨停数据到汇总文件失败: {e}")