"""
新闻驱动策略
根据最新新闻和情感分析进行交易决策
"""
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Callable
from .realtime_strategy import RealtimeStrategy, TradingSignal, SignalType
from ..news.news_analyzer import NewsAnalyzer


class NewsStrategy(RealtimeStrategy):
    """新闻驱动策略类"""
    
    def __init__(
        self,
        symbols: List[str],
        sentiment_threshold: float = 0.3,
        min_importance: int = 5,
        parameters: Optional[Dict] = None
    ):
        """
        初始化新闻驱动策略
        
        Args:
            symbols: 监控的股票代码列表
            sentiment_threshold: 情感阈值
            min_importance: 最小重要性
            parameters: 其他参数
        """
        super().__init__("NewsStrategy", symbols, parameters)
        
        self.sentiment_threshold = sentiment_threshold
        self.min_importance = min_importance
        
        # 新闻分析器
        self.news_analyzer = NewsAnalyzer()
        self.news_analyzer.register_signal_callback(self._on_news_signal)
        
        # 信号缓存
        self.news_signals = {}
        
        # 统计
        self.stats = {
            'total_news': 0,
            'signals_generated': 0,
            'buy_signals': 0,
            'sell_signals': 0
        }
    
    def _on_news_signal(self, signal: Dict):
        """处理新闻信号"""
        symbol = signal.get('symbol')
        if symbol:
            self.news_signals[symbol] = signal
    
    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        """
        处理实时行情
        
        Args:
            symbol: 股票代码
            quote: 实时行情
        
        Returns:
            Optional[TradingSignal]: 交易信号
        """
        # 检查是否有新闻信号
        if symbol in self.news_signals:
            signal_data = self.news_signals.pop(symbol)
            sentiment = signal_data.get('sentiment', {})
            news = signal_data.get('news', {})
            
            score = sentiment.get('score', 0)
            importance = sentiment.get('importance', 0)
            
            # 判断是否生成交易信号
            if importance >= self.min_importance:
                if score > self.sentiment_threshold:
                    # 正面新闻，买入信号
                    price = quote.get('price', 0)
                    quantity = self.calculate_position_size(price, 1000000, 0.1)
                    
                    if quantity > 0:
                        self.stats['buy_signals'] += 1
                        return TradingSignal(
                            symbol=symbol,
                            signal_type=SignalType.BUY,
                            price=price,
                            quantity=quantity,
                            reason=f"正面新闻: {news.get('title', '')[:30]}... (情感分数: {score:.2f})",
                            confidence=min(abs(score) * importance / 10, 1.0)
                        )
                
                elif score < -self.sentiment_threshold:
                    # 负面新闻，卖出信号
                    price = quote.get('price', 0)
                    return TradingSignal(
                        symbol=symbol,
                        signal_type=SignalType.SELL,
                        price=price,
                        quantity=0,  # 卖出全部
                        reason=f"负面新闻: {news.get('title', '')[:30]}... (情感分数: {score:.2f})",
                        confidence=min(abs(score) * importance / 10, 1.0)
                    )
        
        return None
    
    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[TradingSignal]:
        """处理K线数据"""
        # 新闻策略主要依赖实时新闻，不依赖K线
        return None
    
    def update_news(self, news_list: List[Dict]):
        """
        更新新闻
        
        Args:
            news_list: 新闻列表
        """
        self.stats['total_news'] += len(news_list)
        self.news_analyzer.analyze_realtime(news_list)
    
    def get_hot_stocks(self) -> List[Dict]:
        """获取热门股票"""
        return self.news_analyzer.get_hot_stocks(min_mentions=2)
    
    def get_market_sentiment(self) -> Dict:
        """获取市场情感"""
        hot_stocks = self.get_hot_stocks()
        
        if not hot_stocks:
            return {'label': 'neutral', 'score': 0}
        
        avg_score = sum(s['avg_sentiment'] for s in hot_stocks) / len(hot_stocks)
        
        return {
            'label': 'positive' if avg_score > 0.1 else ('negative' if avg_score < -0.1 else 'neutral'),
            'score': round(avg_score, 3),
            'hot_stocks_count': len(hot_stocks)
        }
    
    def get_symbol_analysis(self, symbol: str) -> Dict:
        """获取个股分析"""
        news_list = self.news_analyzer.get_symbol_news(symbol, count=5)
        
        return {
            'symbol': symbol,
            'recent_news': news_list,
            'news_count': len(news_list),
            'has_positive_news': any(n['sentiment']['score'] > 0.2 for n in news_list),
            'has_negative_news': any(n['sentiment']['score'] < -0.2 for n in news_list)
        }
    
    def get_status(self) -> Dict:
        """获取策略状态"""
        return {
            'name': self.name,
            'symbols': self.symbols,
            'sentiment_threshold': self.sentiment_threshold,
            'min_importance': self.min_importance,
            'stats': self.stats,
            'market_sentiment': self.get_market_sentiment(),
            'hot_stocks': self.get_hot_stocks()[:5]
        }