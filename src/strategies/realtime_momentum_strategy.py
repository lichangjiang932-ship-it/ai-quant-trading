import pandas as pd
import numpy as np
from typing import Dict, Optional
from .realtime_strategy import RealtimeStrategy, TradingSignal, SignalType


class RealtimeMomentumStrategy(RealtimeStrategy):
    def __init__(
        self,
        symbols,
        lookback_period: int = 20,
        entry_threshold: float = 0.03,
        exit_threshold: float = -0.01,
        parameters: Optional[Dict] = None
    ):
        super().__init__("RealtimeMomentum", symbols, parameters)
        self.lookback_period = lookback_period
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold

        self.price_history = {s: [] for s in symbols}
        self.position_open = {s: False for s in symbols}

    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        price = quote.get('price', 0)
        if price <= 0:
            return None

        self.price_history[symbol].append(price)
        if len(self.price_history[symbol]) > self.lookback_period + 5:
            self.price_history[symbol] = self.price_history[symbol][-(self.lookback_period + 5):]

        if len(self.price_history[symbol]) < self.lookback_period + 1:
            return None

        prices = pd.Series(self.price_history[symbol])
        momentum = prices.iloc[-1] / prices.iloc[-self.lookback_period - 1] - 1

        if not self.position_open[symbol] and momentum > self.entry_threshold:
            quantity = self.calculate_position_size(price, 1000000, 0.1)
            if quantity > 0:
                self.position_open[symbol] = True
                return TradingSignal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    price=price,
                    quantity=quantity,
                    reason=f"动量买入信号：{self.lookback_period}日涨幅 {momentum:.2%}",
                    confidence=min(abs(momentum) * 2, 1.0)
                )

        if self.position_open[symbol] and momentum < self.exit_threshold:
            self.position_open[symbol] = False
            return TradingSignal(
                symbol=symbol,
                signal_type=SignalType.SELL,
                price=price,
                quantity=0,
                reason=f"动量卖出信号：{self.lookback_period}日涨幅 {momentum:.2%}",
                confidence=min(abs(momentum) * 2, 1.0)
            )

        return None

    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[TradingSignal]:
        close_price = bar_data.get('close', 0)
        if close_price <= 0:
            return None
        return self.on_tick(symbol, {'price': close_price})

    def get_status(self, symbol: str) -> Dict:
        return {
            'symbol': symbol,
            'lookback_period': self.lookback_period,
            'entry_threshold': self.entry_threshold,
            'position_open': self.position_open.get(symbol, False),
            'price_count': len(self.price_history.get(symbol, []))
        }
