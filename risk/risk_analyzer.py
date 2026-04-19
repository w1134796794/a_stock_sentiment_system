"""
风险分析器
生成风险热力图、集中度分析、流动性风险预警
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import loguru

logger = loguru.logger


class RiskAnalyzer:
    """
    风险分析器
    识别和可视化投资组合风险
    """

    def __init__(self):
        self.risk_thresholds = {
            'sector_concentration': 0.40,  # 板块集中度警戒线
            'single_stock': 0.20,          # 单票集中度
            'liquidity': 0.10,             # 流动性风险
            'correlation': 0.70            # 相关性阈值
        }

    def analyze_sector_concentration(self, positions: Dict) -> Dict:
        """
        分析板块集中度风险

        Args:
            positions: 当前持仓 {stock_code: position_info}

        Returns:
            板块集中度分析结果
        """
        if not positions:
            return {'risk_level': 'low', 'concentration': 0, 'details': {}}

        # 按板块汇总
        sector_values = {}
        total_value = 0

        for code, pos in positions.items():
            sector = pos.get('sector', '未知')
            value = pos.get('market_value', 0)
            sector_values[sector] = sector_values.get(sector, 0) + value
            total_value += value

        if total_value == 0:
            return {'risk_level': 'low', 'concentration': 0, 'details': {}}

        # 计算集中度
        sector_ratios = {s: v/total_value for s, v in sector_values.items()}
        max_concentration = max(sector_ratios.values()) if sector_ratios else 0

        # 风险评级
        if max_concentration > self.risk_thresholds['sector_concentration']:
            risk_level = 'high'
        elif max_concentration > self.risk_thresholds['sector_concentration'] * 0.7:
            risk_level = 'medium'
        else:
            risk_level = 'low'

        # 排序
        sorted_sectors = sorted(sector_ratios.items(), key=lambda x: x[1], reverse=True)

        return {
            'risk_level': risk_level,
            'concentration': max_concentration,
            'sector_ratios': dict(sorted_sectors[:5]),
            'details': {
                'total_sectors': len(sector_ratios),
                'top_sector': sorted_sectors[0] if sorted_sectors else ('无', 0),
                'threshold': self.risk_thresholds['sector_concentration']
            }
        }

    def analyze_liquidity_risk(self, positions: Dict, market_data: Dict) -> Dict:
        """
        分析流动性风险

        Args:
            positions: 当前持仓
            market_data: 市场数据 {stock_code: {'avg_volume': x, 'avg_amount': y}}

        Returns:
            流动性风险分析
        """
        liquidity_risks = []

        for code, pos in positions.items():
            position_value = pos.get('market_value', 0)
            avg_daily_amount = market_data.get(code, {}).get('avg_amount', 1e8)

            # 计算持仓占日均成交额比例
            ratio = position_value / avg_daily_amount if avg_daily_amount > 0 else 1

            if ratio > self.risk_thresholds['liquidity']:
                liquidity_risks.append({
                    'stock_code': code,
                    'stock_name': pos.get('stock_name', ''),
                    'position_value': position_value,
                    'avg_daily_amount': avg_daily_amount,
                    'ratio': ratio,
                    'risk_level': 'high' if ratio > 0.2 else 'medium'
                })

        return {
            'risk_count': len(liquidity_risks),
            'high_risk_stocks': [r for r in liquidity_risks if r['risk_level'] == 'high'],
            'medium_risk_stocks': [r for r in liquidity_risks if r['risk_level'] == 'medium'],
            'max_ratio': max([r['ratio'] for r in liquidity_risks]) if liquidity_risks else 0
        }

    def analyze_correlation_risk(self,
                                 positions: Dict,
                                 price_history: Dict) -> Dict:
        """
        分析持仓相关性风险

        Args:
            positions: 当前持仓
            price_history: 价格历史 {stock_code: price_series}

        Returns:
            相关性风险分析
        """
        if len(positions) < 2:
            return {'risk_level': 'low', 'high_correlation_pairs': []}

        # 计算相关性矩阵
        codes = list(positions.keys())
        returns_data = {}

        for code in codes:
            if code in price_history:
                prices = price_history[code]
                returns = prices.pct_change().dropna()
                if len(returns) > 10:
                    returns_data[code] = returns

        if len(returns_data) < 2:
            return {'risk_level': 'low', 'high_correlation_pairs': [], 'reason': '数据不足'}

        # 构建DataFrame计算相关性
        returns_df = pd.DataFrame(returns_data)
        corr_matrix = returns_df.corr()

        # 找出高相关性股票对
        high_corr_pairs = []
        for i in range(len(codes)):
            for j in range(i+1, len(codes)):
                code1, code2 = codes[i], codes[j]
                if code1 in corr_matrix.index and code2 in corr_matrix.columns:
                    corr = corr_matrix.loc[code1, code2]
                    if abs(corr) > self.risk_thresholds['correlation']:
                        high_corr_pairs.append({
                            'stock1': code1,
                            'stock2': code2,
                            'correlation': corr,
                            'risk_level': 'high' if abs(corr) > 0.85 else 'medium'
                        })

        # 风险评级
        if len(high_corr_pairs) >= 3:
            risk_level = 'high'
        elif len(high_corr_pairs) >= 1:
            risk_level = 'medium'
        else:
            risk_level = 'low'

        return {
            'risk_level': risk_level,
            'high_correlation_pairs': high_corr_pairs,
            'correlation_matrix': corr_matrix,
            'diversification_score': max(0, 1 - len(high_corr_pairs) / max(len(positions) - 1, 1))
        }

    def generate_risk_heatmap(self, positions: Dict) -> Dict:
        """
        生成风险热力图数据

        Returns:
            风险热力图数据
        """
        if not positions:
            return {'sectors': [], 'stocks': [], 'risk_scores': []}

        # 板块风险
        sector_risk = self.analyze_sector_concentration(positions)

        # 个股风险评分
        stock_risks = []
        total_value = sum(p.get('market_value', 0) for p in positions.values())

        for code, pos in positions.items():
            risk_score = 0
            risk_factors = []

            # 仓位大小风险
            position_ratio = pos.get('market_value', 0) / total_value if total_value > 0 else 0
            if position_ratio > self.risk_thresholds['single_stock']:
                risk_score += 30
                risk_factors.append('仓位过大')

            # 亏损风险
            pnl_pct = pos.get('pnl_pct', 0)
            if pnl_pct < -0.05:
                risk_score += 25
                risk_factors.append('亏损较大')

            # 无热点共振
            if not pos.get('hot_resonance', False):
                risk_score += 15
                risk_factors.append('无热点支撑')

            stock_risks.append({
                'stock_code': code,
                'stock_name': pos.get('stock_name', ''),
                'sector': pos.get('sector', '未知'),
                'risk_score': min(risk_score, 100),
                'risk_factors': risk_factors,
                'position_ratio': position_ratio
            })

        # 排序
        stock_risks.sort(key=lambda x: x['risk_score'], reverse=True)

        return {
            'sector_concentration': sector_risk,
            'stock_risks': stock_risks,
            'high_risk_count': len([s for s in stock_risks if s['risk_score'] > 50]),
            'medium_risk_count': len([s for s in stock_risks if 30 < s['risk_score'] <= 50]),
            'low_risk_count': len([s for s in stock_risks if s['risk_score'] <= 30])
        }

    def generate_risk_report(self,
                            positions: Dict,
                            market_data: Dict = None,
                            price_history: Dict = None) -> Dict:
        """
        生成综合风险报告

        Returns:
            完整风险分析报告
        """
        report = {
            'timestamp': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': {},
            'sector_risk': {},
            'liquidity_risk': {},
            'correlation_risk': {},
            'risk_heatmap': {},
            'recommendations': []
        }

        # 1. 板块集中度风险
        sector_risk = self.analyze_sector_concentration(positions)
        report['sector_risk'] = sector_risk

        # 2. 流动性风险
        if market_data:
            liquidity_risk = self.analyze_liquidity_risk(positions, market_data)
            report['liquidity_risk'] = liquidity_risk

        # 3. 相关性风险
        if price_history:
            corr_risk = self.analyze_correlation_risk(positions, price_history)
            report['correlation_risk'] = corr_risk

        # 4. 风险热力图
        report['risk_heatmap'] = self.generate_risk_heatmap(positions)

        # 5. 汇总
        high_risk_count = (
            (1 if sector_risk['risk_level'] == 'high' else 0) +
            report.get('liquidity_risk', {}).get('risk_count', 0) +
            len(report.get('correlation_risk', {}).get('high_correlation_pairs', []))
        )

        report['summary'] = {
            'total_positions': len(positions),
            'high_risk_count': high_risk_count,
            'overall_risk_level': 'high' if high_risk_count >= 3 else ('medium' if high_risk_count >= 1 else 'low'),
            'total_exposure': sum(p.get('market_value', 0) for p in positions.values())
        }

        # 6. 建议
        recommendations = []

        if sector_risk['risk_level'] == 'high':
            top_sector = sector_risk['details']['top_sector'][0]
            recommendations.append(f"⚠️ 板块集中度过高: {top_sector}占比{sector_risk['concentration']:.1%}，建议减仓分散")

        if report.get('liquidity_risk', {}).get('high_risk_stocks'):
            stocks = [s['stock_name'] for s in report['liquidity_risk']['high_risk_stocks']]
            recommendations.append(f"⚠️ 流动性风险: {', '.join(stocks)}持仓占比过高，注意退出难度")

        if report.get('correlation_risk', {}).get('high_correlation_pairs'):
            pairs = report['correlation_risk']['high_correlation_pairs']
            recommendations.append(f"⚠️ 相关性风险: 发现{len(pairs)}对高相关股票，降低同向波动风险")

        if not recommendations:
            recommendations.append("✅ 当前风险水平可控，继续保持")

        report['recommendations'] = recommendations

        return report