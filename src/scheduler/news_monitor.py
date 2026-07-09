"""
新闻监控器
实时监控新闻并生成交易信号
"""
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Callable
from collections import defaultdict
from ..news.news_fetcher import NewsFetcher, NewsItem
from ..news.sentiment.sentiment_analyzer import SentimentAnalyzer
from ..news.news_analyzer import NewsAnalyzer


class NewsMonitor:
    """新闻监控器类"""
    
    def __init__(self):
        """初始化新闻监控器"""
        self.news_analyzer = NewsAnalyzer()
        self.running = False
        self._thread = None
        
        # 监控配置
        self.watch_symbols = []
        self.update_interval = 60  # 秒
        
        # 信号回调
        self.signal_callbacks = []
        
        # 历史记录
        self.news_history = []
        self.signal_history = []
        
        # 统计
        self.stats = {
            'total_news': 0,
            'total_signals': 0,
            'buy_signals': 0,
            'sell_signals': 0,
            'start_time': None
        }
    
    def add_watch_symbol(self, symbol: str):
        """添加监控股票"""
        if symbol not in self.watch_symbols:
            self.watch_symbols.append(symbol)
    
    def remove_watch_symbol(self, symbol: str):
        """移除监控股票"""
        if symbol in self.watch_symbols:
            self.watch_symbols.remove(symbol)
    
    def register_signal_callback(self, callback: Callable):
        """注册信号回调"""
        self.signal_callbacks.append(callback)
    
    def start(self, interval: int = 60):
        """
        启动新闻监控
        
        Args:
            interval: 更新间隔（秒）
        """
        self.running = True
        self.update_interval = interval
        self.stats['start_time'] = datetime.now()
        
        # 注册新闻回调
        self.news_analyzer.news_fetcher.register_callback(self._on_news_update)
        
        # 启动监控
        self.news_analyzer.start_realtime_monitor(interval)
        
        # 启动信号处理线程
        self._thread = threading.Thread(target=self._signal_loop, daemon=True)
        self._thread.start()
        
        print(f"新闻监控已启动，更新间隔: {interval}秒")
    
    def stop(self):
        """停止新闻监控"""
        self.running = False
        self.news_analyzer.stop_realtime_monitor()
        if self._thread:
            self._thread.join(timeout=5)
        print("新闻监控已停止")
    
    def _on_news_update(self, news_list: List[NewsItem]):
        """处理新闻更新"""
        self.stats['total_news'] += len(news_list)
        
        # 记录历史
        for news in news_list:
            self.news_history.append({
                'title': news.title,
                'source': news.source,
                'time': news.publish_time,
                'symbols': news.symbols
            })
        
        # 保持历史记录在合理范围
        if len(self.news_history) > 1000:
            self.news_history = self.news_history[-500:]
    
    def _signal_loop(self):
        """信号处理循环"""
        while self.running:
            try:
                # 生成交易信号
                signals = self.news_analyzer.generate_trading_signals(self.watch_symbols)
                
                for signal in signals:
                    self.stats['total_signals'] += 1
                    
                    if signal['action'] == 'buy':
                        self.stats['buy_signals'] += 1
                    else:
                        self.stats['sell_signals'] += 1
                    
                    # 记录历史
                    self.signal_history.append({
                        'time': datetime.now(),
                        'symbol': signal['symbol'],
                        'action': signal['action'],
                        'reason': signal['reason'],
                        'confidence': signal['confidence']
                    })
                    
                    # 触发回调
                    for callback in self.signal_callbacks:
                        try:
                            callback(signal)
                        except Exception as e:
                            print(f"信号回调出错: {e}")
                
                # 保持历史记录在合理范围
                if len(self.signal_history) > 1000:
                    self.signal_history = self.signal_history[-500:]
                
            except Exception as e:
                print(f"信号处理出错: {e}")
            
            time.sleep(10)  # 每10秒检查一次
    
    def get_latest_news(self, count: int = 10) -> List[Dict]:
        """获取最新新闻"""
        news_list = self.news_analyzer.get_market_news(count)
        return [n['news'] for n in news_list]
    
    def get_latest_signals(self, count: int = 10) -> List[Dict]:
        """获取最新信号"""
        return self.signal_history[-count:]
    
    def get_symbol_news(self, symbol: str, count: int = 5) -> List[Dict]:
        """获取个股新闻"""
        return self.news_analyzer.get_symbol_news(symbol, count)
    
    def get_hot_stocks(self) -> List[Dict]:
        """获取热门股票"""
        return self.news_analyzer.get_hot_stocks(min_mentions=2)
    
    def get_market_sentiment(self) -> Dict:
        """获取市场情感"""
        return self.news_analyzer.get_market_sentiment()
    
    def get_status(self) -> Dict:
        """获取监控状态"""
        return {
            'running': self.running,
            'watch_symbols': self.watch_symbols,
            'update_interval': self.update_interval,
            'stats': self.stats,
            'latest_signals': self.get_latest_signals(5),
            'market_sentiment': self.get_market_sentiment(),
            'hot_stocks': self.get_hot_stocks()[:5]
        }