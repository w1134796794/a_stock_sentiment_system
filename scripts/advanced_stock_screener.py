"""
高级标的筛选系统
实现多种筛选策略：弱转强、回踩5日线、卡位涨停等
"""
import pandas as pd
import akshare as ak
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional
import json

@dataclass
class StockSignal:
    """股票信号数据类"""
    stock_name: str
    stock_code: str
    signal_type: str  # 弱转强, 回踩5日线, 卡位涨停,  etc.
    confidence: float  # 置信度 0-1
    description: str
    key_metrics: Dict
    l1_industry: str
    l2_industry: str
    l3_industry: str
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None

class AdvancedStockScreener:
    """高级股票筛选器"""
    
    def __init__(self):
        self.data_dir = Path("data")
        self.cache_dir = Path("data/cache")
        self.output_dir = Path("output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载行业映射
        self.hierarchy_df = pd.read_excel(self.data_dir / "industry/industry_hierarchy.xlsx")
        
        # 加载20日涨停数据
        self.limit_up_detail = pd.read_excel(self.output_dir / "limit_up_detail_20d.xlsx")
        self.focus_stocks = pd.read_excel(self.output_dir / "focus_stocks.xlsx")
        
    def fetch_stock_daily(self, stock_code: str, days: int = 30) -> pd.DataFrame:
        """获取股票历史日线数据"""
        try:
            # 转换代码格式
            code_str = str(stock_code).zfill(6)
            if code_str.startswith('6'):
                symbol = f"sh{code_str}"
            else:
                symbol = f"sz{code_str}"
            
            df = ak.stock_zh_a_hist(symbol=code_str, period="daily", 
                                   start_date=(datetime.now() - timedelta(days=days)).strftime("%Y%m%d"),
                                   end_date=datetime.now().strftime("%Y%m%d"))
            return df
        except Exception as e:
            print(f"  ✗ 获取{stock_code}历史数据失败: {e}")
            return pd.DataFrame()
    
    def calculate_ma(self, df: pd.DataFrame, ma_list=[5, 10, 20, 60]) -> pd.DataFrame:
        """计算均线"""
        for ma in ma_list:
            df[f'MA{ma}'] = df['收盘'].rolling(window=ma).mean()
        return df
    
    def screen_weak_to_strong(self, stock_code: str, stock_name: str, 
                              l1: str, l2: str, l3: str) -> Optional[StockSignal]:
        """
        弱转强模式筛选
        条件：
        1. 昨日烂板（炸板次数>0 或 最后封板时间晚于10:00）
        2. 今日高开高走（开盘价>昨日收盘价，且快速涨停）
        3. 成交量放大（今日成交量 > 昨日成交量 * 1.2）
        """
        df = self.fetch_stock_daily(stock_code, days=5)
        if df.empty or len(df) < 2:
            return None
        
        df = self.calculate_ma(df, [5, 10])
        
        # 获取最近两天数据
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        
        # 判断条件
        # 1. 昨日涨停但烂板（简化：昨日涨停但盘中打开过）
        yesterday_limit_up = yesterday['涨跌幅'] >= 9.5
        yesterday_bad_board = yesterday['最高'] != yesterday['最低']  # 有上下影线说明打开过
        
        # 2. 今日高开
        today_gap_up = today['开盘'] > yesterday['收盘']
        today_strong = today['涨跌幅'] >= 9.5 and (today['最低'] - today['开盘']) / today['开盘'] < 0.02
        
        # 3. 成交量放大
        volume_increase = today['成交量'] > yesterday['成交量'] * 1.2
        
        if yesterday_limit_up and yesterday_bad_board and today_gap_up and today_strong and volume_increase:
            return StockSignal(
                stock_name=stock_name,
                stock_code=stock_code,
                signal_type="弱转强",
                confidence=0.85,
                description=f"昨日烂板后今日高开高走涨停，成交量放大{today['成交量']/yesterday['成交量']:.1f}倍",
                key_metrics={
                    "昨日涨幅": f"{yesterday['涨跌幅']:.2f}%",
                    "今日涨幅": f"{today['涨跌幅']:.2f}%",
                    "成交量比": f"{today['成交量']/yesterday['成交量']:.2f}",
                    "开盘跳空": f"{(today['开盘']-yesterday['收盘'])/yesterday['收盘']*100:.2f}%"
                },
                l1_industry=l1,
                l2_industry=l2,
                l3_industry=l3
            )
        return None
    
    def screen_pullback_to_ma5(self, stock_code: str, stock_name: str,
                               l1: str, l2: str, l3: str) -> Optional[StockSignal]:
        """
        回踩5日线模式筛选
        条件：
        1. 近期有涨停（5日内有涨停记录）
        2. 股价回踩MA5（收盘价在MA5 ±1%范围内）
        3. 缩量（成交量 < 5日均量）
        4. 出现企稳信号（下影线或阳线）
        """
        df = self.fetch_stock_daily(stock_code, days=20)
        if df.empty or len(df) < 10:
            return None
        
        df = self.calculate_ma(df, [5, 10, 20])
        
        today = df.iloc[-1]
        recent_5d = df.tail(5)
        
        # 1. 近期有涨停
        recent_limit_up = (recent_5d['涨跌幅'] >= 9.5).any()
        
        # 2. 回踩MA5
        ma5 = today['MA5']
        close_to_ma5 = abs(today['收盘'] - ma5) / ma5 < 0.01
        
        # 3. 缩量
        avg_volume_5d = df.tail(5)['成交量'].mean()
        volume_shrink = today['成交量'] < avg_volume_5d * 0.8
        
        # 4. 企稳信号（有下影线）
        lower_shadow = today['收盘'] > today['最低']  # 有下影线
        
        if recent_limit_up and close_to_ma5 and volume_shrink and lower_shadow:
            return StockSignal(
                stock_name=stock_name,
                stock_code=stock_code,
                signal_type="回踩5日线",
                confidence=0.75,
                description=f"近期涨停后回踩5日线，缩量企稳",
                key_metrics={
                    "当前价": today['收盘'],
                    "MA5": round(ma5, 2),
                    "偏离MA5": f"{(today['收盘']-ma5)/ma5*100:.2f}%",
                    "成交量比": f"{today['成交量']/avg_volume_5d:.2f}",
                    "近5日最高涨幅": f"{recent_5d['涨跌幅'].max():.2f}%"
                },
                l1_industry=l1,
                l2_industry=l2,
                l3_industry=l3,
                entry_price=today['收盘'],
                stop_loss=round(ma5 * 0.97, 2),
                target_price=round(today['收盘'] * 1.08, 2)
            )
        return None
    
    def screen_position_battle(self, stock_code: str, stock_name: str,
                               l1: str, l2: str, l3: str) -> Optional[StockSignal]:
        """
        卡位涨停模式筛选
        条件：
        1. 同板块昨日龙头断板（昨日涨停今日未涨停）
        2. 该股票今日首板涨停
        3. 涨停时间早于同板块其他股票
        """
        # 简化实现：检查该股票是否为首板且所属板块活跃
        df = self.fetch_stock_daily(stock_code, days=10)
        if df.empty or len(df) < 5:
            return None
        
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        
        # 今日首板
        today_first_limit = today['涨跌幅'] >= 9.5 and yesterday['涨跌幅'] < 9.5
        
        # 板块内有其他股票昨日涨停（简化判断）
        # 这里使用focus_stocks中同板块的其他股票
        sector_stocks = self.focus_stocks[
            (self.focus_stocks['L3'] == l3) & 
            (self.focus_stocks['Stock_Name'] != stock_name)
        ]
        sector_active = len(sector_stocks) >= 2
        
        if today_first_limit and sector_active:
            return StockSignal(
                stock_name=stock_name,
                stock_code=stock_code,
                signal_type="卡位涨停",
                confidence=0.70,
                description=f"板块内卡位涨停，成为新龙头",
                key_metrics={
                    "今日涨幅": f"{today['涨跌幅']:.2f}%",
                    "昨日涨幅": f"{yesterday['涨跌幅']:.2f}%",
                    "同板块活跃股数": len(sector_stocks),
                    "板块排名": self.get_sector_rank(stock_name, l3)
                },
                l1_industry=l1,
                l2_industry=l2,
                l3_industry=l3
            )
        return None
    
    def screen_breakout(self, stock_code: str, stock_name: str,
                        l1: str, l2: str, l3: str) -> Optional[StockSignal]:
        """
        突破模式筛选
        条件：
        1. 突破近期高点（创20日新高）
        2. 放量涨停
        3. 板块共振（同板块多股上涨）
        """
        df = self.fetch_stock_daily(stock_code, days=30)
        if df.empty or len(df) < 20:
            return None
        
        today = df.iloc[-1]
        
        # 1. 创20日新高
        high_20d = df.tail(20)['最高'].max()
        breakout = today['最高'] >= high_20d and today['涨跌幅'] >= 9.5
        
        # 2. 放量
        avg_volume_20d = df.tail(20)['成交量'].mean()
        volume_surge = today['成交量'] > avg_volume_20d * 1.5
        
        if breakout and volume_surge:
            return StockSignal(
                stock_name=stock_name,
                stock_code=stock_code,
                signal_type="突破新高",
                confidence=0.80,
                description=f"放量涨停突破20日新高",
                key_metrics={
                    "今日涨幅": f"{today['涨跌幅']:.2f}%",
                    "20日高点": high_20d,
                    "突破幅度": f"{(today['最高']-high_20d)/high_20d*100:.2f}%",
                    "成交量比": f"{today['成交量']/avg_volume_20d:.2f}"
                },
                l1_industry=l1,
                l2_industry=l2,
                l3_industry=l3
            )
        return None
    
    def get_sector_rank(self, stock_name: str, l3: str) -> int:
        """获取股票在板块内的排名"""
        sector_stocks = self.focus_stocks[self.focus_stocks['L3'] == l3]
        if sector_stocks.empty:
            return 0
        sector_stocks = sector_stocks.sort_values('Score', ascending=False)
        try:
            rank = sector_stocks[sector_stocks['Stock_Name'] == stock_name].index[0]
            return rank + 1
        except:
            return 0
    
    def run_all_screens(self, max_stocks: int = 20):
        """运行所有筛选策略"""
        print("\n" + "="*80)
        print("高级标的筛选 - 多策略扫描")
        print("="*80)
        
        all_signals = []
        
        # 从关注标的中选取前N只进行深度分析
        target_stocks = self.focus_stocks.head(max_stocks)
        
        print(f"\n对 {len(target_stocks)} 只重点关注标的进行多维度扫描...")
        print("-" * 80)
        
        for idx, row in target_stocks.iterrows():
            stock_name = row['Stock_Name']
            stock_code = self.get_stock_code(stock_name)
            l1, l2, l3 = row['L1'], row['L2'], row['L3']
            
            print(f"\n[{idx+1}/{len(target_stocks)}] 分析 {stock_name} ({stock_code})...")
            
            # 运行各种筛选策略
            strategies = [
                ("弱转强", self.screen_weak_to_strong),
                ("回踩5日线", self.screen_pullback_to_ma5),
                ("卡位涨停", self.screen_position_battle),
                ("突破新高", self.screen_breakout),
            ]
            
            for strategy_name, strategy_func in strategies:
                signal = strategy_func(stock_code, stock_name, l1, l2, l3)
                if signal:
                    all_signals.append(signal)
                    print(f"  ✓ 发现【{strategy_name}】信号 - 置信度{signal.confidence:.0%}")
        
        # 整理结果
        return self.compile_results(all_signals)
    
    def get_stock_code(self, stock_name: str) -> str:
        """通过股票名称查找代码"""
        # 从limit_up_detail中查找
        matches = self.limit_up_detail[self.limit_up_detail['name'] == stock_name]
        if not matches.empty:
            return str(matches.iloc[0]['code']).zfill(6)
        return "000000"
    
    def compile_results(self, signals: List[StockSignal]):
        """整理筛选结果"""
        if not signals:
            print("\n未发现符合条件的信号")
            return pd.DataFrame()
        
        print("\n" + "="*80)
        print(f"筛选完成！共发现 {len(signals)} 个交易信号")
        print("="*80)
        
        # 按信号类型分组
        signals_by_type = {}
        for signal in signals:
            if signal.signal_type not in signals_by_type:
                signals_by_type[signal.signal_type] = []
            signals_by_type[signal.signal_type].append(signal)
        
        # 打印结果
        for signal_type, type_signals in signals_by_type.items():
            print(f"\n【{signal_type}】 - {len(type_signals)} 只")
            print("-" * 80)
            
            # 按置信度排序
            type_signals.sort(key=lambda x: x.confidence, reverse=True)
            
            for signal in type_signals:
                print(f"\n  {signal.stock_name} ({signal.stock_code})")
                print(f"  所属板块: {signal.l1_industry} > {signal.l2_industry} > {signal.l3_industry}")
                print(f"  置信度: {signal.confidence:.0%}")
                print(f"  描述: {signal.description}")
                print(f"  关键指标:")
                for k, v in signal.key_metrics.items():
                    print(f"    - {k}: {v}")
                if signal.entry_price:
                    print(f"  建议入场: {signal.entry_price}")
                    print(f"  止损位: {signal.stop_loss}")
                    print(f"  目标位: {signal.target_price}")
        
        # 保存结果
        results = []
        for signal in signals:
            results.append({
                'Stock_Name': signal.stock_name,
                'Stock_Code': signal.stock_code,
                'Signal_Type': signal.signal_type,
                'Confidence': signal.confidence,
                'Description': signal.description,
                'L1_Industry': signal.l1_industry,
                'L2_Industry': signal.l2_industry,
                'L3_Industry': signal.l3_industry,
                'Entry_Price': signal.entry_price,
                'Stop_Loss': signal.stop_loss,
                'Target_Price': signal.target_price,
                'Key_Metrics': json.dumps(signal.key_metrics, ensure_ascii=False)
            })
        
        results_df = pd.DataFrame(results)
        results_df = results_df.sort_values(['Signal_Type', 'Confidence'], ascending=[True, False])
        results_df.to_excel(self.output_dir / "advanced_screening_results.xlsx", index=False)
        print(f"\n✓ 筛选结果已保存: {self.output_dir / 'advanced_screening_results.xlsx'}")
        
        return results_df

def main():
    screener = AdvancedStockScreener()
    results = screener.run_all_screens(max_stocks=20)

if __name__ == "__main__":
    main()
