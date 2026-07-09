"""
均值回归策略
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from .base_strategy import BaseStrategy, Signal


class MeanReversionStrategy(BaseStrategy):
    """均值回归策略"""

    def __init__(
        self,
        lookback_period: int = 20,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.0,
        parameters: Optional[Dict] = None
    ):
        """
        初始化均值回归策略

        Args:
            lookback_period: 回看周期
            entry_threshold: 入场阈值（标准差倍数）
            exit_threshold: 出场阈值
            parameters: 其他参数
        """
        super().__init__("MeanReversionStrategy", parameters)
        self.lookback_period = lookback_period
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()

        df['MA'] = df['Close'].rolling(window=self.lookback_period).mean()
        df['std'] = df['Close'].rolling(window=self.lookback_period).std()

        df['upper_band'] = df['MA'] + (self.entry_threshold * df['std'])
        df['lower_band'] = df['MA'] - (self.entry_threshold * df['std'])

        df['z_score'] = (df['Close'] - df['MA']) / df['std']
        df['z_score'] = df['z_score'].replace([np.inf, -np.inf], np.nan)

        df['raw_signal'] = Signal.HOLD.value
        df.loc[df['Close'] < df['lower_band'], 'raw_signal'] = Signal.BUY.value
        df.loc[df['Close'] > df['upper_band'], 'raw_signal'] = Signal.SELL.value
        df.loc[
            (df['raw_signal'] == Signal.HOLD.value) &
            (df['z_score'].abs() < self.exit_threshold),
            'raw_signal'
        ] = Signal.SELL.value

        df['signal'] = df['raw_signal']
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

    def get_z_score_interpretation(self, z_score: float) -> str:
        if z_score > 2:
            return "严重超买"
        elif z_score > 1:
            return "超买"
        elif z_score > 0:
            return "略高于均值"
        elif z_score > -1:
            return "略低于均值"
        elif z_score > -2:
            return "超卖"
        else:
            return "严重超卖"
