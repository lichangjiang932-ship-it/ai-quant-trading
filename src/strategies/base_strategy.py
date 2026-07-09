"""
策略基类
"""
from abc import ABC, abstractmethod
import pandas as pd
from typing import Dict, List, Optional
from enum import Enum


class Signal(Enum):
    """交易信号"""
    BUY = 1
    SELL = -1
    HOLD = 0


class BaseStrategy(ABC):
    """策略基类"""

    def __init__(self, name: str, parameters: Optional[Dict] = None):
        """
        初始化策略

        Args:
            name: 策略名称
            parameters: 策略参数
        """
        self.name = name
        self.parameters = parameters or {}
        self.positions: Dict[str, Dict] = {}
        self.signals: List[Dict] = []

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        生成交易信号

        Args:
            data: 市场数据

        Returns:
            DataFrame: 添加了信号列的DataFrame
        """
        pass

    @abstractmethod
    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
        portfolio_value: float
    ) -> float:
        """
        计算仓位大小

        Args:
            signal: 交易信号
            current_price: 当前价格
            portfolio_value: 组合价值

        Returns:
            float: 仓位大小（股数）
        """
        pass

    def should_enter_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float,
        portfolio_value: float
    ) -> bool:
        """
        是否应该进入仓位

        Args:
            symbol: 股票代码
            signal: 交易信号
            current_price: 当前价格
            portfolio_value: 组合价值

        Returns:
            bool: 是否应该进入仓位
        """
        if signal != Signal.BUY:
            return False

        if self.has_position(symbol):
            return False

        position_size = self.calculate_position_size(
            signal, current_price, portfolio_value
        )
        return position_size > 0

    def should_exit_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float
    ) -> bool:
        """
        是否应该退出仓位

        Args:
            symbol: 股票代码
            signal: 交易信号
            current_price: 当前价格

        Returns:
            bool: 是否应该退出仓位
        """
        if not self.has_position(symbol):
            return False

        if signal != Signal.SELL:
            return False

        pos = self.positions[symbol]
        entry_price = pos.get('entry_price', current_price)
        if self._should_stop_loss(entry_price, current_price):
            return True
        if self._should_take_profit(entry_price, current_price):
            return True

        return True

    def has_position(self, symbol: str) -> bool:
        """是否持有某股票"""
        pos = self.positions.get(symbol)
        return pos is not None and pos.get('shares', 0) > 0

    def _should_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """止损检查（默认不触发）"""
        sl = self.parameters.get('stop_loss')
        if sl is None:
            return False
        loss_pct = (entry_price - current_price) / entry_price
        return loss_pct >= sl

    def _should_take_profit(self, entry_price: float, current_price: float) -> bool:
        """止盈检查（默认不触发）"""
        tp = self.parameters.get('take_profit')
        if tp is None:
            return False
        profit_pct = (current_price - entry_price) / entry_price
        return profit_pct >= tp

    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        获取持仓信息

        Args:
            symbol: 股票代码

        Returns:
            Dict: 持仓信息
        """
        return self.positions.get(symbol)

    def update_position(
        self,
        symbol: str,
        shares: int,
        entry_price: float,
        entry_date=None
    ):
        """
        更新持仓（回测器在成交后调用）

        Args:
            symbol: 股票代码
            shares: 股数
            entry_price: 入场价格
            entry_date: 入场日期
        """
        prev = self.positions.get(symbol)
        if prev and prev.get('shares', 0) > 0:
            total_shares = prev['shares'] + shares
            avg_price = (
                prev['entry_price'] * prev['shares'] + entry_price * shares
            ) / total_shares
        else:
            total_shares = shares
            avg_price = entry_price

        self.positions[symbol] = {
            'shares': total_shares,
            'entry_price': avg_price,
            'entry_date': entry_date if entry_date is not None else pd.Timestamp.now()
        }

    def close_position(self, symbol: str):
        """
        关闭持仓（回测器在卖出后调用）

        Args:
            symbol: 股票代码
        """
        if symbol in self.positions:
            self.positions[symbol] = {
                'shares': 0,
                'entry_price': self.positions[symbol].get('entry_price', 0),
                'entry_date': self.positions[symbol].get('entry_date')
            }

    def reset(self):
        """重置策略状态（用于新一轮回测）"""
        self.positions = {}
        self.signals = []
