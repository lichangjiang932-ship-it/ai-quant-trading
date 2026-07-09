"""
动量策略
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from .base_strategy import BaseStrategy, Signal


class MomentumStrategy(BaseStrategy):
    """动量策略"""

    def __init__(
        self,
        lookback_period: int = 20,
        threshold: float = 0.02,
        parameters: Optional[Dict] = None
    ):
        """
        初始化动量策略

        Args:
            lookback_period: 回看周期
            threshold: 动量阈值
            parameters: 其他参数
        """
        super().__init__("MomentumStrategy", parameters)
        self.lookback_period = lookback_period
        self.threshold = threshold

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        生成交易信号

        Args:
            data: 市场数据

        Returns:
            DataFrame: 添加了信号列的DataFrame
        """
        df = data.copy()

        df['momentum'] = df['Close'].pct_change(self.lookback_period)

        df['raw_signal'] = Signal.HOLD.value
        df.loc[df['momentum'] > self.threshold, 'raw_signal'] = Signal.BUY.value
        df.loc[df['momentum'] < -self.threshold, 'raw_signal'] = Signal.SELL.value

        df['signal'] = df['raw_signal']

        df['MA_20'] = df['Close'].rolling(window=20).mean()
        df['MA_50'] = df['Close'].rolling(window=50).mean()

        return df

    def should_enter_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float,
        portfolio_value: float
    ) -> bool:
        if signal != Signal.BUY:
            return False
        if self.has_position(symbol):
            return False
        size = self.calculate_position_size(signal, current_price, portfolio_value)
        return size > 0

    def should_exit_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float
    ) -> bool:
        if not self.has_position(symbol):
            return False
        if signal == Signal.SELL:
            return True
        pos = self.positions[symbol]
        return self._should_stop_loss(pos['entry_price'], current_price) or \
               self._should_take_profit(pos['entry_price'], current_price)

    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
        portfolio_value: float,
        max_position_pct: float = 0.1
    ) -> float:
        if signal != Signal.BUY:
            return 0
        max_investment = portfolio_value * max_position_pct
        shares = int(max_investment / current_price)
        return max(shares, 0)

    def get_momentum_strength(self, momentum: float) -> str:
        if momentum > 0.1:
            return "强"
        elif momentum > 0.05:
            return "中"
        elif momentum > 0:
            return "弱"
        elif momentum > -0.05:
            return "弱"
        elif momentum > -0.1:
            return "中"
        else:
            return "强"
