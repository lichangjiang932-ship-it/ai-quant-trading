"""
量化交易平台主程序
支持实时行情监控和自动交易
"""
import sys
import os
import time
import signal
from datetime import datetime
from typing import Dict, List, Optional

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data.realtime.realtime_data import RealtimeData
from src.execution.brokers.simulated_broker import SimulatedBroker
from src.execution.brokers.base_broker import Order, OrderDirection, OrderType
from src.strategies.cross_ma_strategy import CrossMAStrategy
from src.strategies.realtime_momentum_strategy import RealtimeMomentumStrategy
from src.strategies.realtime_mean_reversion_strategy import RealtimeMeanReversionStrategy
from src.strategies.realtime_strategy import TradingSignal, SignalType
from src.scheduler.trading_scheduler import TradingScheduler
from src.scheduler.market_monitor import MarketMonitor
from src.utils.trade_logger import TradeLogger
from src.utils.config import Config


class TradingPlatform:
    """量化交易平台"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        """
        初始化交易平台
        
        Args:
            config_path: 配置文件路径
        """
        # 加载配置
        self.config = Config(config_path)
        
        # 初始化组件
        self.realtime_data = RealtimeData()
        self.broker = None
        self.strategies = []
        self.scheduler = TradingScheduler()
        self.monitor = MarketMonitor()
        self.logger = TradeLogger(log_dir="logs")
        
        # 状态
        self.running = False
        self.portfolio_value = self.config.get('trading.initial_capital', 1000000)
        
        print("=" * 60)
        print("量化交易平台 v1.0")
        print("=" * 60)
    
    def setup(self):
        """设置平台"""
        # 初始化券商
        broker_type = self.config.get('broker.type', 'simulated')
        
        if broker_type == 'simulated':
            initial_capital = self.config.get('trading.initial_capital', 1000000)
            self.broker = SimulatedBroker(initial_capital=initial_capital)
            self.broker.connect()
            print(f"使用模拟券商，初始资金: {initial_capital:,.2f}")
        elif broker_type == 'qmt':
            from src.execution.brokers.qmt_broker import QMTBroker
            self.broker = QMTBroker(
                account_id=self.config.get('broker.account_id', ''),
                mini_qmt_path=self.config.get('broker.mini_qmt_path', '')
            )
            if self.broker.connect():
                print("QMT券商连接成功")
            else:
                print("QMT券商连接失败，使用模拟券商")
                self.broker = SimulatedBroker()
                self.broker.connect()
        else:
            print(f"未知券商类型: {broker_type}，使用模拟券商")
            self.broker = SimulatedBroker()
            self.broker.connect()
        
        # 注册策略
        symbols = self.config.get('trading.symbols', ['sh600000', 'sz000001'])
        strategy_type = self.config.get('strategy.type', 'cross_ma')
        
        if strategy_type == 'cross_ma':
            strategy = CrossMAStrategy(
                symbols=symbols,
                short_window=self.config.get('strategy.short_window', 5),
                long_window=self.config.get('strategy.long_window', 20)
            )
        elif strategy_type == 'momentum':
            strategy = RealtimeMomentumStrategy(
                symbols=symbols,
                lookback_period=self.config.get('strategy.lookback_period', 20),
                entry_threshold=self.config.get('strategy.entry_threshold', 0.03)
            )
        elif strategy_type == 'mean_reversion':
            strategy = RealtimeMeanReversionStrategy(
                symbols=symbols,
                lookback_period=self.config.get('strategy.lookback_period', 20),
                entry_threshold=self.config.get('strategy.entry_threshold', 2.0)
            )
        else:
            print(f"未知策略类型: {strategy_type}，使用默认均线交叉策略")
            strategy = CrossMAStrategy(symbols=symbols)
        
        # 注册信号回调
        strategy.register_signal_callback(self._on_signal)
        self.strategies.append(strategy)
        
        # 注册调度器策略
        for strat in self.strategies:
            self.scheduler.register_strategy(
                name=strat.name,
                strategy_func=lambda s=strat: self._execute_strategy(s)
            )
        
        # 注册监控告警回调
        self.monitor.register_alert_callback(
            __import__('src.scheduler.market_monitor', fromlist=['MarketAlert']).MarketAlert.LIMIT_UP,
            self._on_limit_up
        )
        self.monitor.register_alert_callback(
            __import__('src.scheduler.market_monitor', fromlist=['MarketAlert']).MarketAlert.LIMIT_DOWN,
            self._on_limit_down
        )
        
        # 注册调度器回调
        self.scheduler.register_callback(
            __import__('src.scheduler.trading_scheduler', fromlist=['TradingSession']).TradingSession.MORNING,
            self._on_market_open
        )
        self.scheduler.register_callback(
            __import__('src.scheduler.trading_scheduler', fromlist=['TradingSession']).TradingSession.AFTER_HOURS,
            self._on_market_close
        )
        
        print(f"已注册 {len(self.strategies)} 个策略")
        print(f"监控 {len(symbols)} 只股票")
    
    def start(self):
        """启动平台"""
        print("\n启动交易平台...")
        self.running = True
        
        # 启动调度器
        self.scheduler.start()
        
        # 启动市场监控
        self.monitor.start()
        
        # 启动实时行情监控
        symbols = self.config.get('trading.symbols', ['sh600000', 'sz000001'])
        self.realtime_data.start_realtime_monitor(
            symbols=symbols,
            callback=self._on_realtime_data,
            interval=self.config.get('trading.update_interval', 3)
        )
        
        print("交易平台已启动")
        print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"交易时段: {self.scheduler.get_current_session().value}")
        print("\n按 Ctrl+C 停止平台\n")
        
        # 等待停止信号
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
    
    def stop(self):
        """停止平台"""
        print("\n正在停止交易平台...")
        self.running = False
        
        # 停止组件
        self.realtime_data.stop_realtime_monitor()
        self.monitor.stop()
        self.scheduler.stop()
        
        # 生成日报
        self.logger.generate_daily_report()
        
        # 打印总结
        self._print_summary()
        
        print("交易平台已停止")
    
    def _on_realtime_data(self, quotes: Dict):
        """处理实时行情数据"""
        for symbol, quote in quotes.items():
            # 更新策略数据
            for strategy in self.strategies:
                if symbol in strategy.symbols:
                    signal = strategy.on_tick(symbol, quote)
                    if signal:
                        strategy.emit_signal(signal)
    
    def _on_signal(self, signal: TradingSignal):
        """处理交易信号"""
        # 记录信号
        self.logger.log_signal(signal.to_dict())
        
        # 打印信号
        print(f"\n[信号] {signal.signal_type.value.upper()} {signal.symbol}")
        print(f"  价格: {signal.price}")
        print(f"  数量: {signal.quantity}")
        print(f"  原因: {signal.reason}")
        print(f"  置信度: {signal.confidence:.2%}")
        
        # 自动交易
        if self.config.get('trading.auto_trade', False):
            self._execute_signal(signal)
    
    def _execute_signal(self, signal: TradingSignal):
        """执行交易信号"""
        try:
            if signal.signal_type == SignalType.BUY:
                order = Order(
                    symbol=signal.symbol,
                    direction=OrderDirection.BUY,
                    quantity=signal.quantity,
                    order_type=OrderType.MARKET
                )
            elif signal.signal_type == SignalType.SELL:
                # 获取持仓
                positions = self.broker.get_positions()
                sell_quantity = 0
                for pos in positions:
                    if pos.symbol == signal.symbol:
                        sell_quantity = pos.quantity
                        break
                
                if sell_quantity <= 0:
                    print(f"  无持仓，跳过卖出")
                    return
                
                order = Order(
                    symbol=signal.symbol,
                    direction=OrderDirection.SELL,
                    quantity=sell_quantity,
                    order_type=OrderType.MARKET
                )
            else:
                return
            
            # 下单
            order_id = self.broker.place_order(order)
            
            if order_id:
                print(f"  订单已提交: {order_id}")
                
                # 记录交易
                self.logger.log_trade({
                    'order_id': order_id,
                    'symbol': signal.symbol,
                    'direction': signal.signal_type.value,
                    'price': signal.price,
                    'quantity': signal.quantity,
                    'reason': signal.reason,
                    'amount': signal.price * signal.quantity
                })
            else:
                print(f"  订单提交失败")
                
        except Exception as e:
            print(f"  执行交易出错: {e}")
            self.logger.log_error(str(e), "执行交易")
    
    def _execute_strategy(self, strategy):
        """执行策略"""
        # 获取实时行情
        for symbol in strategy.symbols:
            quote = self.realtime_data.get_stock_quote(symbol)
            if quote:
                signal = strategy.on_tick(symbol, quote)
                if signal:
                    strategy.emit_signal(signal)
    
    def _on_market_open(self):
        """市场开盘回调"""
        print("\n" + "=" * 60)
        print("市场开盘")
        print("=" * 60)
        
        # 更新账户信息
        account_info = self.broker.get_account_info()
        print(f"账户总资产: {account_info.get('total_asset', 0):,.2f}")
        print(f"可用资金: {account_info.get('cash', 0):,.2f}")
    
    def _on_market_close(self):
        """市场收盘回调"""
        print("\n" + "=" * 60)
        print("市场收盘")
        print("=" * 60)
        
        # 生成日报
        report = self.logger.generate_daily_report()
        summary = report.get('summary', {})
        
        print(f"当日交易: {summary.get('total_trades', 0)} 笔")
        print(f"买入金额: {summary.get('total_buy_amount', 0):,.2f}")
        print(f"卖出金额: {summary.get('total_sell_amount', 0):,.2f}")
        print(f"交易费用: {summary.get('total_cost', 0):,.2f}")
    
    def _on_limit_up(self, alert: Dict):
        """涨停告警"""
        print(f"\n[涨停] {alert['name']}({alert['symbol']}) 涨停!")
    
    def _on_limit_down(self, alert: Dict):
        """跌停告警"""
        print(f"\n[跌停] {alert['name']}({alert['symbol']}) 跌停!")
    
    def _print_summary(self):
        """打印总结"""
        print("\n" + "=" * 60)
        print("交易总结")
        print("=" * 60)
        
        # 账户信息
        account_info = self.broker.get_account_info()
        print(f"初始资金: {self.portfolio_value:,.2f}")
        print(f"当前总资产: {account_info.get('total_asset', 0):,.2f}")
        print(f"盈亏: {account_info.get('profit', 0):,.2f}")
        print(f"收益率: {account_info.get('profit_pct', 0):.2f}%")
        
        # 持仓
        positions = self.broker.get_positions()
        if positions:
            print("\n当前持仓:")
            for pos in positions:
                print(f"  {pos.symbol}: {pos.quantity}股, 成本: {pos.avg_cost:.2f}")
        
        # 交易统计
        trades = self.logger.get_trades()
        if not trades.empty:
            print(f"\n总交易次数: {len(trades)}")


def main():
    """主函数"""
    # 创建平台
    platform = TradingPlatform()
    
    # 设置信号处理
    def signal_handler(sig, frame):
        print("\n收到停止信号...")
        platform.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 设置平台
    platform.setup()
    
    # 启动平台
    platform.start()


if __name__ == "__main__":
    main()