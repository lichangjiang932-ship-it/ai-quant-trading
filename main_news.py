"""
新闻驱动量化交易平台
根据最新新闻和情感分析进行自动交易
"""
import sys
import os
import time
import signal
from datetime import datetime
from typing import Dict, List

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.news.news_analyzer import NewsAnalyzer
from src.news.news_fetcher import NewsFetcher
from src.scheduler.news_monitor import NewsMonitor
from src.execution.brokers.simulated_broker import SimulatedBroker
from src.execution.brokers.base_broker import Order, OrderDirection, OrderType
from src.utils.trade_logger import TradeLogger
from src.utils.config import Config


class NewsTradingPlatform:
    """新闻驱动交易平台"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        """初始化平台"""
        # 加载配置
        self.config = Config(config_path)
        
        # 初始化组件
        self.news_monitor = NewsMonitor()
        self.broker = None
        self.logger = TradeLogger(log_dir="logs")
        
        # 状态
        self.running = False
        self.portfolio = {}  # 持仓
        self.watch_symbols = []
        self.portfolio_value = self.config.get('trading.initial_capital', 1000000)
        
        print("=" * 60)
        print("新闻驱动量化交易平台 v1.0")
        print("=" * 60)
    
    def setup(self):
        """设置平台"""
        # 初始化券商
        initial_capital = self.config.get('trading.initial_capital', 1000000)
        self.broker = SimulatedBroker(initial_capital=initial_capital)
        self.broker.connect()
        print(f"使用模拟券商，初始资金: {initial_capital:,.2f}")
        
        # 设置监控股票
        self.watch_symbols = self.config.get('trading.symbols', [])
        for symbol in self.watch_symbols:
            self.news_monitor.add_watch_symbol(symbol)
        
        # 注册信号回调
        self.news_monitor.register_signal_callback(self._on_signal)
        
        print(f"监控股票: {self.watch_symbols}")
    
    def start(self):
        """启动平台"""
        print("\n启动新闻驱动交易平台...")
        self.running = True
        
        # 启动新闻监控
        interval = self.config.get('trading.news_update_interval', 60)
        self.news_monitor.start(interval)
        
        print("平台已启动")
        print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("\n按 Ctrl+C 停止平台\n")
        
        # 定时打印状态
        status_thread = __import__('threading').Thread(target=self._status_loop, daemon=True)
        status_thread.start()
        
        # 等待停止信号
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
    
    def stop(self):
        """停止平台"""
        print("\n正在停止平台...")
        self.running = False
        self.news_monitor.stop()
        self._print_summary()
        print("平台已停止")
    
    def _on_signal(self, signal: Dict):
        """处理交易信号"""
        symbol = signal.get('symbol')
        action = signal.get('action')
        reason = signal.get('reason', '')
        confidence = signal.get('confidence', 0)
        
        print(f"\n{'='*40}")
        print(f"[信号] {action.upper()} {symbol}")
        print(f"原因: {reason}")
        print(f"置信度: {confidence:.2%}")
        print(f"{'='*40}")
        
        # 检查是否自动交易
        if not self.config.get('trading.auto_trade', False):
            print("自动交易未开启，跳过执行")
            return
        
        # 执行交易
        self._execute_trade(signal)
    
    def _execute_trade(self, signal: Dict):
        """执行交易"""
        symbol = signal.get('symbol')
        action = signal.get('action')
        
        try:
            # 获取当前价格
            from src.data.realtime.realtime_data import RealtimeData
            realtime = RealtimeData()
            quote = realtime.get_stock_quote(symbol)
            price = quote.get('price', 0)
            
            if price <= 0:
                print(f"无法获取{symbol}价格")
                return
            
            if action == 'buy':
                # 计算买入数量
                portfolio_value = self.broker.get_account_info().get('total_asset', 0)
                max_amount = portfolio_value * 0.1  # 最大10%仓位
                quantity = int(max_amount / price / 100) * 100  # 取整到100股
                
                if quantity <= 0:
                    print("资金不足")
                    return
                
                # 下单
                order = Order(
                    symbol=symbol,
                    direction=OrderDirection.BUY,
                    quantity=quantity,
                    order_type=OrderType.MARKET
                )
                
                order_id = self.broker.place_order(order)
                
                if order_id:
                    print(f"买入成功: {quantity}股 @ {price}")
                    
                    # 记录交易
                    self.logger.log_trade({
                        'order_id': order_id,
                        'symbol': symbol,
                        'direction': 'buy',
                        'price': price,
                        'quantity': quantity,
                        'amount': price * quantity,
                        'reason': signal.get('reason', ''),
                        'strategy': 'NewsStrategy'
                    })
                    
                    # 更新持仓
                    self.portfolio[symbol] = {
                        'quantity': quantity,
                        'price': price,
                        'time': datetime.now()
                    }
            
            elif action == 'sell':
                # 检查是否有持仓
                if symbol in self.portfolio:
                    quantity = self.portfolio[symbol]['quantity']
                    
                    order = Order(
                        symbol=symbol,
                        direction=OrderDirection.SELL,
                        quantity=quantity,
                        order_type=OrderType.MARKET
                    )
                    
                    order_id = self.broker.place_order(order)
                    
                    if order_id:
                        print(f"卖出成功: {quantity}股 @ {price}")
                        
                        # 记录交易
                        self.logger.log_trade({
                            'order_id': order_id,
                            'symbol': symbol,
                            'direction': 'sell',
                            'price': price,
                            'quantity': quantity,
                            'amount': price * quantity,
                            'reason': signal.get('reason', ''),
                            'strategy': 'NewsStrategy'
                        })
                        
                        # 更新持仓
                        del self.portfolio[symbol]
                else:
                    print(f"无{symbol}持仓，跳过卖出")
        
        except Exception as e:
            print(f"执行交易出错: {e}")
            self.logger.log_error(str(e), "执行交易")
    
    def _status_loop(self):
        """状态打印循环"""
        while self.running:
            time.sleep(60)  # 每分钟打印一次
            
            if self.running:
                self._print_status()
    
    def _print_status(self):
        """打印状态"""
        print(f"\n{'='*60}")
        print(f"[状态] {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        
        # 账户信息
        account_info = self.broker.get_account_info()
        print(f"总资产: {account_info.get('total_asset', 0):,.2f}")
        print(f"可用资金: {account_info.get('cash', 0):,.2f}")
        
        # 持仓
        if self.portfolio:
            print("\n持仓:")
            for symbol, pos in self.portfolio.items():
                print(f"  {symbol}: {pos['quantity']}股 @ {pos['price']:.2f}")
        
        # 市场情感
        sentiment = self.news_monitor.get_market_sentiment()
        print(f"\n市场情感: {sentiment.get('label', 'neutral')} (分数: {sentiment.get('score', 0):.3f})")
        
        # 热门股票
        hot_stocks = self.news_monitor.get_hot_stocks()
        if hot_stocks:
            print("\n热门股票:")
            for stock in hot_stocks[:3]:
                print(f"  {stock['symbol']}: 提及{stock['mention_count']}次, 情感{stock['sentiment_label']}")
        
        # 最新信号
        signals = self.news_monitor.get_latest_signals(3)
        if signals:
            print("\n最新信号:")
            for sig in signals:
                print(f"  [{sig['action'].upper()}] {sig['symbol']} - {sig['reason'][:30]}")
    
    def _print_summary(self):
        """打印总结"""
        print(f"\n{'='*60}")
        print("交易总结")
        print(f"{'='*60}")
        
        # 账户信息
        account_info = self.broker.get_account_info()
        print(f"初始资金: {self.portfolio_value:,.2f}")
        print(f"当前总资产: {account_info.get('total_asset', 0):,.2f}")
        print(f"盈亏: {account_info.get('profit', 0):,.2f}")
        print(f"收益率: {account_info.get('profit_pct', 0):.2f}%")
        
        # 持仓
        if self.portfolio:
            print("\n当前持仓:")
            for symbol, pos in self.portfolio.items():
                print(f"  {symbol}: {pos['quantity']}股 @ {pos['price']:.2f}")
        
        # 监控统计
        stats = self.news_monitor.stats
        print(f"\n监控统计:")
        print(f"  总新闻数: {stats.get('total_news', 0)}")
        print(f"  总信号数: {stats.get('total_signals', 0)}")
        print(f"  买入信号: {stats.get('buy_signals', 0)}")
        print(f"  卖出信号: {stats.get('sell_signals', 0)}")


def main():
    """主函数"""
    platform = NewsTradingPlatform()
    
    def signal_handler(sig, frame):
        print("\n收到停止信号...")
        platform.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    platform.setup()
    platform.start()


if __name__ == "__main__":
    main()