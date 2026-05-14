"""
龙二波策略 - 正确的历史连板判断
核心：从每日涨停池取近期连板，非日线涨幅计算
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class PatternType(Enum):
    DRAGON_SECOND_WAVE = "龙二波"


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
    description: str = ""  # 描述字段，用于报告展示


class DragonSecondWaveStrategyV2:
    def __init__(self, data_manager, sentiment_engine):
        self.dm = data_manager
        self.se = sentiment_engine
        
        # 时间参数（近期记忆）- 优化后放宽条件
        self.params = {
            "recent_days": 20,           # 放宽：15→20天，给更多蓄势时间
            "max_adjust_days": 15,       # 放宽：10→15天，允许更久调整
            "max_break_days": 2,         # 新增：允许断板2天（原为1天）
            "min_adjust_depth": 0.05,    # 新增：最小调整深度5%（强势调整）
            "max_adjust_depth": 0.30,    # 放宽：25%→30%，容忍更深回调
            "ma10_tolerance": 0.10,      # 放宽：MA10容忍度5%→10%
            "use_ma20_fallback": True,   # 新增：MA10不行时用MA20备选
            
            # 第一波判断 - 双轨制（满足任一即认可）
            "min_first_wave": 3,         # 轨道1：连板至少3板
            "max_first_wave": 15,        # 连板上限15板
            "min_rise_5d": 0.25,         # 轨道2：5日累计涨幅>25%
            "min_rise_10d": 0.40,        # 轨道2：10日累计涨幅>40%
            "min_limit_up_count": 4,     # 轨道3：10天内至少4次涨停
        }
    
    def detect_second_wave(self,
                          stock_code: str,
                          stock_name: str,
                          today_str: str,
                          recent_zt_pools: Dict[str, pd.DataFrame],  # 近20日每日涨停池
                          today_data: pd.Series,
                          sector_hot: bool,
                          hist_data: pd.DataFrame = None) -> Optional[TradeSignal]:
        """
        检测龙二波机会 - 双轨制判断第一波
        recent_zt_pools: {日期: 当日涨停池DataFrame}
        hist_data: 历史日线数据（用于涨幅统计，双轨制判断）
        """
        # 标准化代码格式：去除后缀，保留6位数字
        stock_code_padded = str(stock_code).split('.')[0].zfill(6)
        logger.debug(f"[{stock_code_padded}] 开始检测龙二波 - 名称:{stock_name}, 日期:{today_str}, 板块热度:{sector_hot}")

        # ========== 步骤1：双轨制判断第一波（连板 或 涨幅）==========
        # 使用 stock_code_padded (6位数字) 确保代码格式一致
        consecutive_record = self._rebuild_consecutive_from_pools(
            stock_code_padded, recent_zt_pools, hist_data
        )

        if not consecutive_record['is_valid']:
            logger.debug(f"[{stock_code_padded}] 过滤: 第一波记录无效 - {consecutive_record.get('reason', '未知原因')}")
            return None

        first_wave_info = consecutive_record['first_wave']
        wave_type = first_wave_info.get('wave_type', 'consecutive')
        
        logger.debug(f"[{stock_code_padded}] 第一波记录: 类型={wave_type}, 强度={first_wave_info['max_boards']}板, "
                    f"5日涨幅={first_wave_info.get('rise_5d', 0)*100:.1f}%, "
                    f"10日涨幅={first_wave_info.get('rise_10d', 0)*100:.1f}%, "
                    f"涨停次数={first_wave_info.get('limit_up_count', 0)}, "
                    f"起涨日={first_wave_info['start_date']}, 见顶日={first_wave_info['peak_date']}")

        # 检查是否是近期这一波（非历史久远）
        days_since_peak = self._calculate_days_since_peak(
            first_wave_info['peak_date'], today_str
        )

        if days_since_peak > self.params["max_adjust_days"] + 5:
            logger.debug(f"[{stock_code_padded}] 过滤: 第一波距今太久 ({days_since_peak}天 > {self.params['max_adjust_days'] + 5}天)")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 第一波距今{days_since_peak}天")

        # ========== 步骤2：判断第一波高度（真龙标准）==========
        # 双轨制：连板3-15板 或 5日涨幅>25% 或 10日涨幅>40% 或 10天4次涨停
        max_boards = first_wave_info['max_boards']
        rise_5d = first_wave_info.get('rise_5d', 0)
        rise_10d = first_wave_info.get('rise_10d', 0)
        limit_up_count = first_wave_info.get('limit_up_count', 0)
        
        is_consecutive_valid = self.params["min_first_wave"] <= max_boards <= self.params["max_first_wave"]
        is_rise_5d_valid = rise_5d >= self.params["min_rise_5d"]
        is_rise_10d_valid = rise_10d >= self.params["min_rise_10d"]
        is_limit_up_count_valid = limit_up_count >= self.params["min_limit_up_count"]
        
        if not (is_consecutive_valid or is_rise_5d_valid or is_rise_10d_valid or is_limit_up_count_valid):
            logger.debug(f"[{stock_code_padded}] 过滤: 第一波强度不符合 ("
                        f"连板{max_boards}板, 5日涨幅{rise_5d*100:.1f}%, 10日涨幅{rise_10d*100:.1f}%, 涨停{limit_up_count}次)")
            return None
        
        # 记录通过的具体标准
        passed_criteria = []
        if is_consecutive_valid:
            passed_criteria.append(f"连板{max_boards}板")
        if is_rise_5d_valid:
            passed_criteria.append(f"5日涨幅{rise_5d*100:.1f}%")
        if is_rise_10d_valid:
            passed_criteria.append(f"10日涨幅{rise_10d*100:.1f}%")
        if is_limit_up_count_valid:
            passed_criteria.append(f"{limit_up_count}次涨停")
        
        logger.debug(f"[{stock_code_padded}] 通过: 第一波强度达标 ({', '.join(passed_criteria)})")

        # ========== 步骤3：检查调整期形态 ==========
        adjust_period = self._get_adjust_period(
            stock_code, first_wave_info['peak_date'], today_str
        )

        if not adjust_period:
            logger.debug(f"[{stock_code_padded}] 过滤: 无法获取调整期数据")
            return None

        logger.debug(f"[{stock_code_padded}] 调整期数据: 深度{adjust_period.get('depth', 0)*100:.1f}%, MA10:{adjust_period.get('ma10', 0):.2f}, 天数{adjust_period.get('days', 0)}")

        if not self._check_adjust_quality(adjust_period):
            logger.debug(f"[{stock_code_padded}] 过滤: 调整期质量不符合要求")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 调整期质量检查")

        # ========== 步骤4：今日启动确认（必须是首板）==========
        today_change = today_data.get('涨跌幅', 0)

        if today_change < 9.5:  # 今日未涨停
            logger.debug(f"[{stock_code_padded}] 过滤: 今日未涨停 ({today_change:.2f}% < 9.5%)")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 今日涨停 {today_change:.2f}%")

        today_pool = recent_zt_pools.get(today_str, pd.DataFrame())
        if today_pool.empty:
            logger.debug(f"[{stock_code_padded}] 过滤: 今日涨停池为空")
            return None

        # 兼容不同的列名
        code_col = None
        if '代码' in today_pool.columns:
            code_col = '代码'
        elif 'Code' in today_pool.columns:
            code_col = 'Code'
        elif 'ts_code' in today_pool.columns:
            code_col = 'ts_code'

        if code_col is None:
            logger.debug(f"[{stock_code_padded}] 过滤: 涨停池缺少代码列")
            return None

        # 确保代码格式一致（都是字符串）
        today_pool[code_col] = today_pool[code_col].astype(str).str.zfill(6)
        
        # 查找该股票在今日涨停池中的数据
        today_stock_row = today_pool[today_pool[code_col] == stock_code_padded]
        if today_stock_row.empty:
            logger.debug(f"[{stock_code_padded}] 过滤: 不在今日涨停池中")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 在今日涨停池中")
        
        # 检查今日是否是首板（连板数=1）
        today_consecutive = 1  # 默认为1（首板）
        for col in ['连板数', '连板', 'consecutive', 'limit_up_days', '连板天数']:
            if col in today_stock_row.columns:
                today_consecutive = int(today_stock_row[col].iloc[0])
                break
        
        if today_consecutive > 1:
            logger.debug(f"[{stock_code_padded}] 过滤: 今日不是首板（是{today_consecutive}连板），龙二波要求今日首板")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 今日是首板")
        
        # ========== 构建信号 ==========
        # 使用真正的调整天数（从见顶到二波涨停前一天）
        actual_adjust_days = adjust_period.get('days', days_since_peak)
        decline_days = adjust_period.get('decline_days', actual_adjust_days // 2)
        consolidation_days = adjust_period.get('consolidation_days', actual_adjust_days - decline_days)
        
        logger.debug(f"[{stock_code_padded}] 生成龙二波信号: {first_wave_info['max_boards']}板龙头, "
                    f"调整{actual_adjust_days}天(下跌{decline_days}天+震荡{consolidation_days}天), 板块热度:{sector_hot}")
        
        # 构建描述 - 区分连板龙和趋势龙
        wave_type = first_wave_info.get('wave_type', 'consecutive')
        max_boards = first_wave_info['max_boards']
        limit_up_count = first_wave_info.get('limit_up_count', 0)
        
        if wave_type == 'consecutive' and max_boards >= 3:
            # 真正的连板龙：连续涨停3板以上
            wave_type_desc = "连板"
            wave_detail = f"{max_boards}连板"
        else:
            # 趋势龙：10天内多次涨停但不是连续
            wave_type_desc = "趋势"
            wave_detail = f"{limit_up_count}次涨停"
        
        # 构建调整天数描述：总天数(下跌X天+震荡Y天)
        adjust_desc = f"调整{actual_adjust_days}天(下跌{decline_days}天+震荡{consolidation_days}天)"
        description = f"龙二波+{wave_detail}{wave_type_desc}龙+{adjust_desc}+深度{adjust_period['depth']*100:.1f}%"
        
        return TradeSignal(
            pattern_type=PatternType.DRAGON_SECOND_WAVE,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=today_data.get('首次封板时间', ''),
            confidence=0.82,
            entry_price=today_data.get('涨停价', 0),
            stop_loss=adjust_period['ma10'] * 0.97,
            take_profit=today_data.get('涨停价', 0) * 1.15,
            position_size="medium",
            reason=f"近期{wave_detail}{wave_type_desc}龙，{adjust_desc}后二波启动",
            key_metrics={
                "第一波强度": f"{first_wave_info['max_boards']}板",
                "第一波类型": wave_type_desc,
                "第一波日期": f"{first_wave_info['start_date']}至{first_wave_info['peak_date']}",
                "调整天数": actual_adjust_days,
                "下跌天数": decline_days,
                "震荡天数": consolidation_days,
                "调整深度": f"{adjust_period['depth']*100:.1f}%",
                "5日涨幅": f"{first_wave_info.get('rise_5d', 0)*100:.1f}%",
                "10日涨幅": f"{first_wave_info.get('rise_10d', 0)*100:.1f}%",
                "支撑均线": f"MA10:{adjust_period['ma10']:.2f}"
            },
            validation_rules=[
                f"近15日内{first_wave_info['max_boards']}连板（真龙）",
                f"{adjust_desc}（记忆未散）",
                "回踩MA10获支撑",
                "地量后放量首板",
                "板块热度未退" if sector_hot else "板块已冷（风险）"
            ],
            description=description
        )
    
    # ==================== 核心方法：双轨制第一波判断 ====================

    def _rebuild_consecutive_from_pools(self,
                                       stock_code: str,
                                       recent_pools: Dict[str, pd.DataFrame],
                                       hist_data: pd.DataFrame = None) -> Dict:
        """
        双轨制判断第一波：
        轨道1：连板高度（3板以上）
        轨道2：累计涨幅（5日25%或10日40%）
        轨道3：涨停次数（10天内4次以上）
        
        简化逻辑：直接从涨停池的连板数字段获取最大连板数
        """
        dates = sorted(recent_pools.keys())
        logger.debug(f"[{stock_code}] 检查近{len(dates)}天涨停池")

        # 收集该股票在所有涨停池中的连板数
        max_boards = 0
        peak_date = None
        first_date = None
        zt_dates = []
        
        for date in dates:
            pool = recent_pools[date]
            if pool.empty:
                continue

            # 兼容不同的列名
            code_col = None
            if '代码' in pool.columns:
                code_col = '代码'
            elif 'Code' in pool.columns:
                code_col = 'Code'
            elif 'ts_code' in pool.columns:
                code_col = 'ts_code'

            if code_col is None:
                continue

            # 标准化代码格式
            pool[code_col] = pool[code_col].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
            
            # 查找该股票
            stock_row = pool[pool[code_col] == stock_code]
            if not stock_row.empty:
                zt_dates.append(date)
                
                # 获取连板数（支持不同字段名）
                consecutive_col = None
                for col in ['连板数', '连板', 'consecutive', 'limit_up_days', '连板天数']:
                    if col in stock_row.columns:
                        consecutive_col = col
                        break
                
                if consecutive_col:
                    boards = int(stock_row[consecutive_col].iloc[0])
                    if boards > max_boards:
                        max_boards = boards
                        peak_date = date
                        # 计算起涨日（假设连板N天，则起涨日是N-1天前）
                        try:
                            peak_dt = datetime.strptime(date, "%Y%m%d")
                            first_dt = peak_dt - timedelta(days=(boards - 1))
                            first_date = first_dt.strftime("%Y%m%d")
                        except:
                            first_date = date

        logger.debug(f"[{stock_code}] 涨停日期: {zt_dates}, 最大连板: {max_boards}")

        # ========== 双轨制判断 ==========
        
        # 轨道1：连板高度检查
        consecutive_valid = self.params["min_first_wave"] <= max_boards <= self.params["max_first_wave"]
        
        # 轨道2&3：涨幅和涨停次数检查（需要历史数据）
        rise_valid = False
        limit_up_count_valid = False
        rise_stats = {}
        
        logger.debug(f"[{stock_code}] 双轨制判断: hist_data={'有' if hist_data is not None and not hist_data.empty else '无'}, "
                    f"连板有效={consecutive_valid}")
        
        if hist_data is not None and not hist_data.empty:
            logger.debug(f"[{stock_code}] 开始计算涨幅统计, 历史数据行数={len(hist_data)}, 列={list(hist_data.columns)}")
            rise_stats = self._get_rise_stats(stock_code, hist_data)
            logger.debug(f"[{stock_code}] 涨幅统计结果: {rise_stats}")
            if rise_stats.get('is_valid'):
                rise_valid = True
                limit_up_count_valid = rise_stats.get('limit_up_count', 0) >= self.params["min_limit_up_count"]
        
        # 综合判断：满足任一轨道即有效
        is_valid_wave = consecutive_valid or rise_valid or limit_up_count_valid
        
        if not is_valid_wave:
            reasons = []
            if not consecutive_valid:
                reasons.append(f"连板{max_boards}板(要求{self.params['min_first_wave']}-{self.params['max_first_wave']}板)")
            if hist_data is not None and not rise_valid:
                reasons.append(f"5日涨幅{rise_stats.get('rise_5d', 0)*100:.1f}%(要求>25%)")
                reasons.append(f"10日涨幅{rise_stats.get('rise_10d', 0)*100:.1f}%(要求>40%)")
            if hist_data is not None and not limit_up_count_valid:
                reasons.append(f"涨停次数{rise_stats.get('limit_up_count', 0)}次(要求>={self.params['min_limit_up_count']}次)")
            
            return {'is_valid': False, 'reason': '不满足任何第一波标准: ' + '; '.join(reasons)}

        # 确定第一波类型和关键日期
        if consecutive_valid:
            # 优先使用连板数据
            wave_type = 'consecutive'
            # peak_date 和 first_date 已在前面计算
            if peak_date is None:
                peak_date = dates[-1] if dates else ''
            if first_date is None:
                first_date = dates[0] if dates else ''
        elif rise_valid or limit_up_count_valid:
            # 使用涨幅数据
            wave_type = 'rise'
            peak_date = rise_stats.get('peak_date', dates[-1] if dates else '')
            first_date = rise_stats.get('start_date', dates[0] if dates else '')
            max_boards = max(max_boards, 3)  # 非连板但至少算3板强度
        else:
            return {'is_valid': False, 'reason': '无法确定第一波类型'}

        # 检查是否是近期这一波（非开头几天）
        if dates:
            today = datetime.strptime(dates[-1], "%Y%m%d")
            try:
                peak = datetime.strptime(peak_date, "%Y%m%d")
                days_since_peak = (today - peak).days
            except:
                days_since_peak = 0

            if days_since_peak > self.params["max_adjust_days"] + 5:
                return {'is_valid': False, 'reason': f'第一波距今太久({days_since_peak}天)'}

        logger.debug(f"[{stock_code}] 第一波类型: {wave_type}, 连板: {max_boards}板, 起涨日: {first_date}, 见顶日: {peak_date}")

        return {
            'is_valid': True,
            'first_wave': {
                'max_boards': max_boards,
                'start_date': first_date,
                'peak_date': peak_date,
                'zt_dates': zt_dates if wave_type == 'consecutive' else [],
                'wave_type': wave_type,
                'rise_5d': rise_stats.get('rise_5d', 0),
                'rise_10d': rise_stats.get('rise_10d', 0),
                'limit_up_count': rise_stats.get('limit_up_count', len(zt_dates))
            },
            'all_zt_dates': zt_dates
        }

    def _get_rise_stats(self, stock_code: str, hist_data: pd.DataFrame) -> Dict:
        """
        计算近期涨幅统计 - 用于双轨制判断
        
        核心逻辑：在历史数据中找出任意连续5日/10日的最大涨幅，
        以及该时间段内的涨停次数
        
        返回：
            is_valid: 是否满足涨幅标准
            rise_5d: 任意连续5日最大累计涨幅
            rise_10d: 任意连续10日最大累计涨幅
            limit_up_count: 最佳连续期间内的涨停次数
            start_date: 最佳连续期间的起涨日
            peak_date: 最佳连续期间的见顶日
        """
        if hist_data.empty or len(hist_data) < 5:
            return {'is_valid': False, 'reason': '历史数据不足'}
        
        # 确保按日期排序（升序）
        hist = hist_data.copy()
        if 'trade_date' in hist.columns:
            hist = hist.sort_values('trade_date').reset_index(drop=True)
        
        # 获取涨跌幅列名
        pct_col = None
        if 'pct_chg' in hist.columns:
            pct_col = 'pct_chg'
        elif 'pct_change' in hist.columns:
            pct_col = 'pct_change'
        elif 'change_pct' in hist.columns:
            pct_col = 'change_pct'
        
        # ========== 计算任意连续5日最大涨幅 ==========
        max_rise_5d = 0
        best_5d_start_idx = 0
        best_5d_end_idx = 0
        
        if len(hist) >= 5:
            for i in range(len(hist) - 4):
                start_close = hist.iloc[i]['close']
                end_close = hist.iloc[i + 4]['close']
                rise = (end_close - start_close) / start_close
                if rise > max_rise_5d:
                    max_rise_5d = rise
                    best_5d_start_idx = i
                    best_5d_end_idx = i + 4
        
        # ========== 计算任意连续10日最大涨幅 ==========
        max_rise_10d = 0
        best_10d_start_idx = 0
        best_10d_end_idx = 0
        
        if len(hist) >= 10:
            for i in range(len(hist) - 9):
                start_close = hist.iloc[i]['close']
                end_close = hist.iloc[i + 9]['close']
                rise = (end_close - start_close) / start_close
                if rise > max_rise_10d:
                    max_rise_10d = rise
                    best_10d_start_idx = i
                    best_10d_end_idx = i + 9
        
        # ========== 确定最佳连续期间（5日或10日中涨幅更大的）==========
        min_rise_5d = self.params.get("min_rise_5d", 0.25)
        min_rise_10d = self.params.get("min_rise_10d", 0.40)
        
        # 选择满足条件且涨幅更大的期间
        best_start_idx = 0
        best_end_idx = 0
        best_period = ''
        
        if max_rise_10d >= min_rise_10d and max_rise_10d > max_rise_5d:
            # 10日涨幅更大且满足条件
            best_start_idx = best_10d_start_idx
            best_end_idx = best_10d_end_idx
            best_period = '10日'
        elif max_rise_5d >= min_rise_5d:
            # 5日涨幅满足条件
            best_start_idx = best_5d_start_idx
            best_end_idx = best_5d_end_idx
            best_period = '5日'
        elif max_rise_10d > 0:
            # 都不满足，但选10日作为参考
            best_start_idx = best_10d_start_idx
            best_end_idx = best_10d_end_idx
            best_period = '10日'
        else:
            # 选5日作为参考
            best_start_idx = best_5d_start_idx
            best_end_idx = best_5d_end_idx
            best_period = '5日'
        
        # ========== 统计最佳期间内的涨停次数 ==========
        best_period_data = hist.iloc[best_start_idx:best_end_idx + 1]
        limit_up_count = 0
        
        if pct_col:
            limit_up_count = len(best_period_data[best_period_data[pct_col] > 9.5])
        else:
            # 手动计算涨停次数
            for i in range(1, len(best_period_data)):
                pct_change = (best_period_data.iloc[i]['close'] - best_period_data.iloc[i-1]['close']) / best_period_data.iloc[i-1]['close'] * 100
                if pct_change > 9.5:
                    limit_up_count += 1
        
        # ========== 获取起涨日和见顶日 ==========
        start_date = ''
        peak_date = ''
        if 'trade_date' in hist.columns:
            # 处理日期格式，去除时间部分
            start_date_raw = str(hist.iloc[best_start_idx]['trade_date'])
            peak_date_raw = str(hist.iloc[best_end_idx]['trade_date'])
            
            # 如果包含时间部分，只取日期部分
            if ' ' in start_date_raw:
                start_date_raw = start_date_raw.split(' ')[0]
            if ' ' in peak_date_raw:
                peak_date_raw = peak_date_raw.split(' ')[0]
            
            # 统一格式为 YYYYMMDD
            start_date = start_date_raw.replace('-', '').replace('/', '')
            peak_date = peak_date_raw.replace('-', '').replace('/', '')
        
        # ========== 判断是否满足任一标准 ==========
        min_limit_up_count = self.params.get("min_limit_up_count", 4)
        
        is_valid = (
            max_rise_5d >= min_rise_5d or
            max_rise_10d >= min_rise_10d or
            limit_up_count >= min_limit_up_count
        )
        
        logger.debug(f"[{stock_code}] 涨幅统计(双轨制): 任意5日最大{max_rise_5d*100:.1f}%, "
                    f"任意10日最大{max_rise_10d*100:.1f}%, 最佳期间({best_period})涨停{limit_up_count}次, "
                    f"起涨日={start_date}, 见顶日={peak_date}, 有效:{is_valid}")
        
        return {
            'is_valid': is_valid,
            'rise_5d': max_rise_5d,
            'rise_10d': max_rise_10d,
            'limit_up_count': limit_up_count,
            'start_date': start_date,
            'peak_date': peak_date,
            'best_period': best_period
        }

    def _count_trading_days_between(self, start_date: str, end_date: str) -> int:
        """
        计算两个日期之间有多少个交易日（不包括start_date，包括end_date）

        例如：
        - 周五(20260320)到下周一(20260323)：中间没有交易日，返回0
        - 周一(20260323)到周三(20260325)：中间有1个交易日(周二)，返回1
        - 周一(20260323)到周四(20260326)：中间有2个交易日(周二、周三)，返回2

        Args:
            start_date: 开始日期，格式YYYYMMDD
            end_date: 结束日期，格式YYYYMMDD

        Returns:
            两个日期之间的交易日数量
        """
        try:
            # 尝试使用交易日管理器
            if self.dm and hasattr(self.dm, 'trade_date_mgr'):
                trade_dates = self.dm.trade_date_mgr.get_trade_dates_between(start_date, end_date)
                # 过滤掉start_date当天
                trade_dates = [d for d in trade_dates if d > start_date]
                return len(trade_dates)
        except Exception as e:
            logger.debug(f"使用交易日管理器失败，使用简化计算: {e}")

        # 简化计算：使用日历天数减去周末
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        # 计算总天数差
        total_days = (end - start).days

        # 计算中间有多少个周末
        # 从start的下一天开始算
        weekend_days = 0
        current = start + timedelta(days=1)
        while current <= end:
            if current.weekday() >= 5:  # 周六或周日
                weekend_days += 1
            current += timedelta(days=1)

        trading_days = total_days - weekend_days
        return max(0, trading_days)

    def _get_adjust_period(self, stock_code: str,
                          peak_date: str, today: str) -> Dict:
        """
        获取调整期数据（peak_date到today之间）
        调整期定义：从第一波见顶到二波启动（今日涨停）前的整个阶段
        包含：下跌阶段 + 震荡整理阶段
        
        关键：调整天数 = 从见顶到今日涨停前一天的交易日数
        """
        # 计算需要提前获取的天数（至少10天数据用于计算MA10）
        peak_dt = datetime.strptime(peak_date, "%Y%m%d")
        extended_start_dt = peak_dt - timedelta(days=20)
        extended_start = extended_start_dt.strftime("%Y%m%d")

        # 从data_manager获取日线数据
        hist = self.dm.get_stock_daily(stock_code, extended_start, today)
        if not hist.empty:
            if 'trade_date' in hist.columns:
                hist = hist.sort_values('trade_date').reset_index(drop=True)

        if hist.empty:
            return {}

        # 筛选出peak_date之后的数据
        if 'trade_date' in hist.columns:
            peak_dt_ts = pd.Timestamp(peak_date)
            adjust_hist = hist[hist['trade_date'] >= peak_dt_ts].copy()
        else:
            adjust_hist = hist.copy()

        if len(adjust_hist) < 3:
            return {}

        # 计算调整深度（从peak到最低点的跌幅）
        peak_price = adjust_hist.iloc[0]['high']
        lowest = adjust_hist['low'].min()
        depth = (peak_price - lowest) / peak_price

        # 计算均线
        total_days = len(hist)
        if total_days < 10:
            return {}

        hist['MA10'] = hist['close'].rolling(10).mean()
        if total_days >= 20:
            hist['MA20'] = hist['close'].rolling(20).mean()
        else:
            hist['MA20'] = np.nan

        # 获取today对应的均线值
        ma10 = None
        ma20 = None
        if 'trade_date' in hist.columns:
            today_dt_ts = pd.Timestamp(today)
            today_row = hist[hist['trade_date'] == today_dt_ts]
            if not today_row.empty:
                ma10 = today_row.iloc[-1]['MA10']
                ma20 = today_row.iloc[-1]['MA20']
            else:
                ma10 = hist.iloc[-1]['MA10']
                ma20 = hist.iloc[-1]['MA20']
        else:
            ma10 = hist.iloc[-1]['MA10']
            ma20 = hist.iloc[-1]['MA20']

        if pd.isna(ma10):
            return {}

        # 计算调整天数：从见顶到今日涨停前一天
        # 这才是真正的"调整期"，包含下跌+震荡
        if 'trade_date' in hist.columns:
            today_dt_ts = pd.Timestamp(today)
            # 排除today这一天（因为是二波启动日）
            adjust_days_data = adjust_hist[adjust_hist['trade_date'] < today_dt_ts]
            adjust_days = len(adjust_days_data)
            
            # 找到最低点的日期，计算下跌阶段天数
            lowest_idx = adjust_hist['low'].idxmin()
            lowest_date = adjust_hist.loc[lowest_idx, 'trade_date']
            decline_days_data = adjust_hist[adjust_hist['trade_date'] <= lowest_date]
            decline_days = len(decline_days_data)
            
            # 震荡天数 = 总调整天数 - 下跌天数
            consolidation_days = adjust_days - decline_days
        else:
            adjust_days = len(adjust_hist) - 1 if len(adjust_hist) > 1 else len(adjust_hist)
            decline_days = adjust_days // 2  # 估算
            consolidation_days = adjust_days - decline_days

        return {
            'depth': depth,
            'ma10': ma10,
            'ma20': ma20 if not pd.isna(ma20) else None,
            'lowest_price': lowest,
            'days': adjust_days,
            'decline_days': decline_days,  # 下跌阶段天数
            'consolidation_days': consolidation_days  # 震荡阶段天数
        }
    
    def _check_adjust_quality(self, adjust: Dict) -> bool:
        """
        检查调整质量 - 优化后放宽条件
        """
        if not adjust:
            return False

        depth = adjust.get('depth', 0)
        min_depth = self.params.get("min_adjust_depth", 0.05)   # 最小5%
        max_depth = self.params.get("max_adjust_depth", 0.30)   # 最大30%
        
        # 调整深度检查：5%-30%（放宽后）
        if not (min_depth <= depth <= max_depth):
            logger.debug(f"调整深度不符合: {depth*100:.1f}% (要求{min_depth*100:.0f}%-{max_depth*100:.0f}%)")
            return False

        # 均线支撑检查 - 优化后增加MA20备选
        lowest_price = adjust.get('lowest_price')
        ma10 = adjust.get('ma10')
        ma20 = adjust.get('ma20')
        
        if lowest_price and ma10:
            ma10_tolerance = self.params.get("ma10_tolerance", 0.10)  # 容忍10%
            
            # 检查MA10支撑
            ma10_support = lowest_price >= ma10 * (1 - ma10_tolerance)
            
            if ma10_support:
                logger.debug(f"MA10支撑有效: 最低价{lowest_price:.2f} >= MA10*{1-ma10_tolerance:.0%}({ma10*(1-ma10_tolerance):.2f})")
                return True
            
            # MA10未支撑，尝试MA20备选
            if self.params.get("use_ma20_fallback", True) and ma20:
                ma20_tolerance = 0.05  # MA20更严格一些
                ma20_support = lowest_price >= ma20 * (1 - ma20_tolerance)
                
                if ma20_support:
                    logger.debug(f"MA20支撑有效(备选): 最低价{lowest_price:.2f} >= MA20*{1-ma20_tolerance:.0%}({ma20*(1-ma20_tolerance):.2f})")
                    return True
                else:
                    logger.debug(f"均线支撑无效: 最低价{lowest_price:.2f}, MA10={ma10:.2f}, MA20={ma20:.2f}")
                    return False
            else:
                logger.debug(f"MA10支撑无效: 最低价{lowest_price:.2f} < MA10*{1-ma10_tolerance:.0%}({ma10*(1-ma10_tolerance):.2f})")
                return False
        
        # 没有均线数据时，仅通过深度检查
        return True
    
    def _calculate_days_since_peak(self, peak_date: str, today: str) -> int:
        """计算从第一波见顶到今天的交易日天数"""
        try:
            # 尝试使用交易日管理器计算交易日天数
            if self.dm and hasattr(self.dm, 'trade_date_mgr'):
                trade_dates = self.dm.trade_date_mgr.get_trade_dates_between(peak_date, today)
                # 过滤掉peak_date当天
                trade_dates = [d for d in trade_dates if d > peak_date]
                return len(trade_dates)
        except Exception as e:
            logger.debug(f"使用交易日管理器失败，使用简化计算: {e}")
        
        # 简化计算：使用日历天数减去周末
        peak = datetime.strptime(peak_date, "%Y%m%d")
        today_dt = datetime.strptime(today, "%Y%m%d")
        
        # 计算总天数差
        total_days = (today_dt - peak).days
        
        # 计算中间有多少个周末
        weekend_days = 0
        current = peak + timedelta(days=1)
        while current <= today_dt:
            if current.weekday() >= 5:  # 周六或周日
                weekend_days += 1
            current += timedelta(days=1)
        
        trading_days = total_days - weekend_days
        return max(0, trading_days)

# ==================== 数据准备示例 ====================

def prepare_recent_pools(data_manager, today: str, days: int = 15) -> Dict[str, pd.DataFrame]:
    """
    准备近15日每日涨停池
    """
    pools = {}
    
    for i in range(days):
        date = data_manager.get_date_offset(today, -i)
        pool = data_manager.get_limit_up_pool(date)
        if not pool.empty:
            pools[date] = pool
    
    return pools

# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("龙二波策略V2 - 正确的历史连板判断")
    print("核心：从每日涨停池取近期连板，非日线涨幅计算")
    print("时间范围：近15日内，记忆未散")
    print("")
    print("正确做法：")
    print("1. 取近15日每日涨停池")
    print("2. 检查目标股在哪些日期出现在涨停池")
    print("3. 计算连续出现次数（允许断板1天）")
    print("4. 确认是近期这一波（非3个月前的行情）")
    print("5. 今日再次出现在涨停池=二波启动确认")