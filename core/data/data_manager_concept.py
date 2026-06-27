"""
概念数据管理模块 - 个股概念、概念成分股

数据来源：Tushare
- kpl_concept_cons: 个股所属概念
- ths_index + ths_member: 概念板块成分股（替代原东财dc_index/dc_member）
"""
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
import loguru

from core.data.data_manager_base import DataManagerBase

logger = loguru.logger


class ConceptDataManager(DataManagerBase):
    """概念数据管理器（统一使用同花顺数据）"""

    def get_stock_concepts(self, ts_code: str) -> str:
        """获取个股所属概念（使用Tushare kpl_concept_cons接口）"""
        if not self.ts_pro:
            return ''

        code = ts_code
        cache_file = self.concept_dir / "members" / f"{code.replace('.', '_')}.csv"

        if cache_file.exists():
            df = pd.read_csv(cache_file)
            if 'name' in df.columns and not df.empty:
                concepts = df['name'].dropna().unique()
                return ','.join(concepts)
            return ''

        try:
            df = self.ts_pro.kpl_concept_cons(con_code=code)
            if 'name' in df.columns and not df.empty:
                df.to_csv(cache_file, index=False)
                concepts = df['name'].dropna().unique()
                return ','.join(concepts)
        except Exception as e:
            logger.warning(f"获取股票{code}概念数据失败: {e}")

        return ''

    def get_concept_members(self, trade_date: str) -> pd.DataFrame:
        """
        获取所有概念板块的成分股数据（使用同花顺ths_index + ths_member）

        替代原东财dc_index/dc_member方案

        Args:
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            概念成分股DataFrame，包含股票代码和所属概念
        """
        if not self.ts_pro:
            return pd.DataFrame()

        cache_file = self.concept_dir / "members" / f"all_{trade_date}.csv"

        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            from core.data.data_manager_sector import SectorDataManager
            sector_dm = SectorDataManager.__new__(SectorDataManager)
            sector_dm.__dict__.update(self.__dict__)

            concepts_df = sector_dm.get_ths_index(index_type='概念指数')
            if concepts_df.empty:
                logger.warning("无法获取同花顺概念板块数据")
                return pd.DataFrame()

            logger.info(f"获取到{len(concepts_df)}个同花顺概念板块，开始获取成分股...")

            all_members = []
            for _, concept_row in concepts_df.iterrows():
                concept_code = concept_row['ts_code']
                concept_name = concept_row['name']

                try:
                    members_df = sector_dm.get_ths_member(concept_code)
                    if not members_df.empty:
                        members_df['concept_name'] = concept_name
                        members_df['concept_code'] = concept_code
                        all_members.append(members_df)
                except Exception as e:
                    logger.debug(f"获取概念{concept_name}成分股失败: {e}")

            if all_members:
                result_df = pd.concat(all_members, ignore_index=True)
                result_df.to_csv(cache_file, index=False)
                logger.info(f"获取概念成分股数据完成: {len(result_df)}条记录")
                return result_df
            else:
                logger.warning("未获取到任何概念成分股数据")
                return pd.DataFrame()

        except Exception as e:
            logger.error(f"获取概念成分股数据失败: {e}")
            return pd.DataFrame()

    def cache_limit_up_stock_concepts(
        self, stock_codes: Iterable[str], trade_date: str
    ) -> pd.DataFrame:
        """预取当日涨停股概念归属，供涨停梯队页面离线聚合。

        只查询当天涨停池涉及的股票，避免为了几十只股票遍历全部概念板块。
        页面只读取这里生成的按日文件，不会在用户访问时调用 Tushare。
        """
        codes = list(dict.fromkeys(str(code or "").strip() for code in stock_codes if str(code or "").strip()))
        columns = ["con_code", "concept_code", "concept_name", "type"]
        if not codes or not hasattr(self, "get_stock_sectors_batch"):
            return pd.DataFrame(columns=columns)

        try:
            batches = self.get_stock_sectors_batch(codes) or {}
        except Exception as e:
            logger.warning(f"[概念梯队] 涨停股概念归属预取失败: {e}")
            return pd.DataFrame(columns=columns)

        rows = []
        for requested_code, frame in batches.items():
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            for _, sector in frame.iterrows():
                sector_type = str(sector.get("type") or "").strip()
                if sector_type not in {"N", "概念"}:
                    continue
                concept_name = str(sector.get("name") or "").strip()
                if not concept_name:
                    continue
                rows.append({
                    "con_code": str(requested_code or "").strip(),
                    "concept_code": str(sector.get("ts_code") or "").strip(),
                    "concept_name": concept_name,
                    "type": "N",
                })

        result = pd.DataFrame(rows, columns=columns).drop_duplicates()
        cache_file = self.concept_dir / "members" / f"limit_up_{trade_date}.csv"
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = cache_file.with_suffix(".csv.tmp")
            result.to_csv(temp_file, index=False, encoding="utf-8-sig")
            temp_file.replace(cache_file)
            logger.info(
                f"[概念梯队] {trade_date} 涨停股概念归属缓存完成: "
                f"{len(codes)} 只股票, {len(result)} 条概念关系"
            )
        except Exception as e:
            logger.warning(f"[概念梯队] 概念归属缓存写入失败 {cache_file}: {e}")
        return result

    def get_stock_concepts_from_members(self, ts_code: str, trade_date: str) -> str:
        """从概念成分股数据中获取个股所属概念"""
        code = ts_code
        members_df = self.get_concept_members(trade_date)
        if members_df.empty:
            return ''

        matched = members_df[members_df['con_code'] == code] if 'con_code' in members_df.columns else pd.DataFrame()
        if matched.empty and 'code' in members_df.columns:
            matched = members_df[members_df['code'] == code]

        if matched.empty:
            return ''

        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)

    def _get_concepts_from_preloaded_data(self, ts_code: str, members_df: pd.DataFrame) -> str:
        """从预加载的概念成分股数据中获取个股所属概念"""
        code = ts_code

        matched = members_df[members_df['con_code'] == code] if 'con_code' in members_df.columns else pd.DataFrame()
        if matched.empty and 'code' in members_df.columns:
            matched = members_df[members_df['code'] == code]

        if matched.empty:
            return ''

        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)
