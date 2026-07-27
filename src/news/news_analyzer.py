"""
新闻分析器 - 增强版
综合分析新闻，生成交易信号
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from collections import defaultdict
from .news_fetcher import NewsFetcher, NewsItem
from .sentiment.sentiment_analyzer import SentimentAnalyzer


class NewsAnalyzer:
    """新闻分析器类"""
    
    def __init__(self):
        """初始化新闻分析器"""
        self.news_fetcher = NewsFetcher()
        self.sentiment_analyzer = SentimentAnalyzer()
        
        # 缓存
        self.news_cache = {}
        self.sentiment_cache = {}
        self.symbol_sentiment = defaultdict(list)
        
        # 回调
        self.signal_callbacks = []
        
        # 配置
        self.min_importance = 5
        self.sentiment_threshold = 0.3
        self.time_window = 60
    
    def register_signal_callback(self, callback: Callable):
        """注册信号回调"""
        self.signal_callbacks.append(callback)
    
    def analyze_realtime(self, news_list: List[NewsItem]) -> Dict:
        """实时分析新闻"""
        results = {
            'new_count': 0,
            'signals': [],
            'symbol_sentiments': {},
            'market_sentiment': None,
            'industry_sentiments': {}
        }
        
        for news in news_list:
            news_key = f"{news.title}_{news.publish_time}"
            if news_key in self.news_cache:
                continue
            
            self.news_cache[news_key] = news
            
            # 分析情感
            sentiment = self.sentiment_analyzer.analyze_news(news.to_dict())
            news.sentiment_score = sentiment['score']
            news.importance = sentiment['importance']
            
            self.sentiment_cache[news_key] = sentiment
            
            # 提取相关股票
            symbols = news.symbols or self.news_fetcher._extract_symbols_from_text(
                news.title + news.content
            )
            
            # 更新股票情感
            for symbol in symbols:
                self.symbol_sentiment[symbol].append({
                    'score': sentiment['score'],
                    'importance': sentiment['importance'],
                    'time': news.publish_time,
                    'title': news.title,
                    'industry': sentiment.get('industry', '')
                })
            
            # 更新行业情感
            industry = sentiment.get('industry', '')
            if industry:
                if industry not in results['industry_sentiments']:
                    results['industry_sentiments'][industry] = []
                results['industry_sentiments'][industry].append(sentiment['score'])
            
            results['new_count'] += 1
            
            # 生成信号
            if (sentiment['importance'] >= self.min_importance and
                abs(sentiment['score']) >= self.sentiment_threshold):
                
                signal = {
                    'symbol': symbols[0] if symbols else None,
                    'sentiment': sentiment,
                    'news': news.to_dict(),
                    'time': datetime.now()
                }
                results['signals'].append(signal)
        
        # 计算各股票情感
        for symbol in self.symbol_sentiment:
            recent = self._get_recent_sentiment(symbol)
            if recent:
                avg_score = sum(s['score'] for s in recent) / len(recent)
                results['symbol_sentiments'][symbol] = {
                    'avg_score': round(avg_score, 3),
                    'count': len(recent),
                    'label': 'positive' if avg_score > 0.1 else ('negative' if avg_score < -0.1 else 'neutral'),
                    'industries': list(set(s.get('industry', '') for s in recent if s.get('industry')))
                }
        
        # 计算市场整体情感
        all_recent = []
        for symbol in self.symbol_sentiment:
            all_recent.extend(self._get_recent_sentiment(symbol))
        
        if all_recent:
            avg_score = sum(s['score'] for s in all_recent) / len(all_recent)
            results['market_sentiment'] = {
                'avg_score': round(avg_score, 3),
                'label': 'positive' if avg_score > 0.1 else ('negative' if avg_score < -0.1 else 'neutral')
            }
        
        # 计算行业平均情感
        for industry, scores in results['industry_sentiments'].items():
            results['industry_sentiments'][industry] = {
                'avg_score': round(sum(scores) / len(scores), 3),
                'count': len(scores)
            }
        
        # 触发回调
        for signal in results['signals']:
            for callback in self.signal_callbacks:
                try:
                    callback(signal)
                except Exception as e:
                    print(f"信号回调出错: {e}")
        
        return results
    
    def _get_recent_sentiment(self, symbol: str, minutes: int = None) -> List[Dict]:
        """获取最近的情感数据"""
        if minutes is None:
            minutes = self.time_window
        
        cutoff_time = datetime.now() - timedelta(minutes=minutes)
        return [
            s for s in self.symbol_sentiment[symbol]
            if s['time'] and s['time'] >= cutoff_time
        ]
    
    def get_symbol_news(self, symbol: str, count: int = 10) -> List[Dict]:
        """获取个股新闻"""
        return self.get_symbol_news_with_meta(symbol, count)['items']

    def get_symbol_news_with_meta(self, symbol: str, count: int = 10) -> Dict:
        """获取个股公告/研报及数据源覆盖信息。"""
        result = self.news_fetcher.fetch_stock_news_with_meta(symbol, count)
        items = []
        for news in result.get('items', []):
            payload = news.to_dict()
            items.append({
                'news': payload,
                'sentiment': self.sentiment_analyzer.analyze_news(payload),
            })
        return {'items': items, 'status': result.get('status', {})}
    
    def get_market_news(self, count: int = 20) -> List[Dict]:
        """获取市场新闻"""
        news_list = self.news_fetcher.fetch_all_news()[:count]
        return [{'news': n.to_dict(), 'sentiment': self.sentiment_analyzer.analyze_news(n.to_dict())} for n in news_list]
    
    def get_professional_news(self, count: int = 20) -> List[Dict]:
        """获取专业数据（公告、研报、龙虎榜）"""
        news_list = self.news_fetcher.fetch_all_professional()[:count]
        return [{'news': n.to_dict(), 'sentiment': self.sentiment_analyzer.analyze_news(n.to_dict())} for n in news_list]
    
    def get_hot_stocks(self, min_mentions: int = 2) -> List[Dict]:
        """获取热门股票"""
        stock_mentions = defaultdict(int)
        stock_sentiments = defaultdict(list)
        
        for symbol, sentiments in self.symbol_sentiment.items():
            recent = self._get_recent_sentiment(symbol)
            if recent:
                stock_mentions[symbol] = len(recent)
                stock_sentiments[symbol] = [s['score'] for s in recent]
        
        hot_stocks = []
        for symbol, count in stock_mentions.items():
            if count >= min_mentions:
                avg_sentiment = sum(stock_sentiments[symbol]) / len(stock_sentiments[symbol])
                hot_stocks.append({
                    'symbol': symbol,
                    'mention_count': count,
                    'avg_sentiment': round(avg_sentiment, 3),
                    'sentiment_label': 'positive' if avg_sentiment > 0.1 else ('negative' if avg_sentiment < -0.1 else 'neutral')
                })
        
        hot_stocks.sort(key=lambda x: x['mention_count'], reverse=True)
        return hot_stocks
    
    def get_industry_sentiment(self) -> List[Dict]:
        """获取行业情感排名"""
        industry_data = defaultdict(list)
        
        for symbol, sentiments in self.symbol_sentiment.items():
            for s in sentiments:
                industry = s.get('industry', '')
                if industry:
                    industry_data[industry].append(s['score'])
        
        result = []
        for industry, scores in industry_data.items():
            avg_score = sum(scores) / len(scores)
            result.append({
                'industry': industry,
                'avg_score': round(avg_score, 3),
                'count': len(scores),
                'label': 'positive' if avg_score > 0.1 else ('negative' if avg_score < -0.1 else 'neutral')
            })
        
        result.sort(key=lambda x: x['avg_score'], reverse=True)
        return result
    
    def generate_trading_signals(self, portfolio_symbols: List[str] = None) -> List[Dict]:
        """生成交易信号"""
        signals = []
        
        hot_stocks = self.get_hot_stocks(min_mentions=2)
        
        for stock in hot_stocks:
            symbol = stock['symbol']
            sentiment = stock['avg_sentiment']
            
            if portfolio_symbols and symbol in portfolio_symbols:
                if sentiment < -0.3:
                    signals.append({
                        'symbol': symbol,
                        'action': 'sell',
                        'reason': f'负面新闻较多，情感分数: {sentiment:.2f}',
                        'confidence': abs(sentiment),
                        'importance': stock['mention_count']
                    })
            else:
                if sentiment > 0.3:
                    signals.append({
                        'symbol': symbol,
                        'action': 'buy',
                        'reason': f'正面新闻较多，情感分数: {sentiment:.2f}',
                        'confidence': sentiment,
                        'importance': stock['mention_count']
                    })
        
        signals.sort(key=lambda x: x['importance'] * x['confidence'], reverse=True)
        return signals
    
    def start_realtime_monitor(self, interval: int = 60):
        """启动实时新闻监控"""
        def on_news_update(news_list):
            self.analyze_realtime(news_list)
        
        self.news_fetcher.register_callback(on_news_update)
        self.news_fetcher.start_monitor(interval)
    
    def stop_realtime_monitor(self):
        """停止实时新闻监控"""
        self.news_fetcher.stop_monitor()
    
    def get_analysis_summary(self) -> Dict:
        """获取分析摘要"""
        return {
            'cached_news': len(self.news_cache),
            'cached_sentiments': len(self.sentiment_cache),
            'tracked_symbols': len(self.symbol_sentiment),
            'hot_stocks': self.get_hot_stocks(min_mentions=2)[:5],
            'industry_sentiment': self.get_industry_sentiment()[:5]
        }
