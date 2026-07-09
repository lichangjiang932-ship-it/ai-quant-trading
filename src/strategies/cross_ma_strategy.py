"""
均线交叉策略（实时版）
当短期均线上穿长期均线时买入，下穿时卖出
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from .realtime_strategy import RealtimeStrategy, TradingSignal, SignalType


class CrossMAStrategy(RealtimeStrategy):
    """均线交叉策略"""
    
    def __init__(
        self,
        symbols,
        short_window: int = 5,
        long_window: int = 20,
        parameters: Optional[Dict] = None
    ):
        """
        初始化均线交叉策略
        
        Args:
            symbols: 股票代码列表
            short_window: 短期均线周期
            long_window: 长期均线周期
            parameters: 其他参数
        """
        super().__init__("CrossMA", symbols, parameters)
        self.short_window = short_window
        self.long_window = long_window
        
        # 存储历史数据
        self.price_history = {s: [] for s in symbols}
        self.ma_short = {s: None for s in symbols}
        self.ma_long = {s: None for s in symbols}
        self.prev_ma_short = {s: None for s in symbols}
        self.prev_ma_long = {s: None for s in symbols}
    
    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        """
        处理实时行情
        
        Args:
            symbol: 股票代码
            quote: 实时行情
        
        Returns:
            Optional[TradingSignal]: 交易信号
        """
        price = quote.get('price', 0)
        if price <= 0:
            return None
        
        # 更新价格历史
        self.price_history[symbol].append(price)
        
        # 保持足够的历史数据
        if len(self.price_history[symbol]) > self.long_window + 10:
            self.price_history[symbol] = self.price_history[symbol][-(self.long_window + 10):]
        
        # 计算均线
        if len(self.price_history[symbol]) >= self.long_window:
            prices = pd.Series(self.price_history[symbol])
            
            # 保存上一次的均线值
            self.prev_ma_short[symbol] = self.ma_short[symbol]
            self.prev_ma_long[symbol] = self.ma_long[symbol]
            
            # 计算当前均线
            self.ma_short[symbol] = prices.rolling(window=self.short_window).mean().iloc[-1]
            self.ma_long[symbol] = prices.rolling(window=self.long_window).mean().iloc[-1]
            
            # 检查交叉信号
            return self._check_cross_signal(symbol, price)
        
        return None
    
    def _check_cross_signal(self, symbol: str, price: float) -> Optional[TradingSignal]:
        """检查交叉信号"""
        if self.prev_ma_short[symbol] is None or self.prev_ma_long[symbol] is None:
            return None
        
        curr_short = self.ma_short[symbol]
        curr_long = self.ma_long[symbol]
        prev_short = self.prev_ma_short[symbol]
        prev_long = self.prev_ma_long[symbol]
        
        # 金叉：短均线上穿长均线
        if prev_short <= prev_long and curr_short > curr_long:
            quantity = self.calculate_position_size(price, 1000000, 0.1)
            if quantity > 0:
                return TradingSignal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    price=price,
                    quantity=quantity,
                    reason=f"金叉信号：MA{self.short_window}({curr_short:.2f})上穿MA{self.long_window}({curr_long:.2f})",
                    confidence=0.8
                )
        
        # 死叉：短均线下穿长均线
        elif prev_short >= prev_long and curr_short < curr_long:
            return TradingSignal(
                symbol=symbol,
                signal_type=SignalType.SELL,
                price=price,
                quantity=0,  # 卖出全部持仓
                reason=f"死叉信号：MA{self.short_window}({curr_short:.2f})下穿MA{self.long_window}({curr_long:.2f})",
                confidence=0.8
            )
        
        return None
    
    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[TradingSignal]:
        """处理K线数据"""
        close_price = bar_data.get('close', 0)
        if close_price <= 0:
            return None
        
        return self.on_tick(symbol, {'price': close_price})
    
    def get_status(self, symbol: str) -> Dict:
        """获取策略状态"""
        return {
            'symbol': symbol,
            'short_window': self.short_window,
            'long_window': self.long_window,
            'ma_short': self.ma_short.get(symbol),
            'ma_long': self.ma_long.get(symbol),
            'price_count': len(self.price_history.get(symbol, []))
        }