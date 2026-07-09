"""
投资组合管理
"""
import pandas as pd
from typing import Dict, Optional
from datetime import datetime


class Portfolio:
    """投资组合类"""
    
    def __init__(self, initial_capital: float = 100000):
        """
        初始化投资组合
        
        Args:
            initial_capital: 初始资金
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.history = []
    
    @property
    def total_value(self) -> float:
        """
        计算总价值
        
        Returns:
            float: 总价值
        """
        positions_value = sum(
            pos['shares'] * pos.get('current_price', pos['entry_price'])
            for pos in self.positions.values()
        )
        return self.cash + positions_value
    
    def get_position_value(self, symbol: str, current_price: float) -> float:
        """
        获取持仓价值
        
        Args:
            symbol: 股票代码
            current_price: 当前价格
        
        Returns:
            float: 持仓价值
        """
        if symbol not in self.positions:
            return 0
        
        return self.positions[symbol]['shares'] * current_price
    
    def get_position_pnl(
        self,
        symbol: str,
        current_price: float
    ) -> Dict:
        """
        获取持仓盈亏
        
        Args:
            symbol: 股票代码
            current_price: 当前价格
        
        Returns:
            Dict: 盈亏信息
        """
        if symbol not in self.positions:
            return {'pnl': 0, 'return_pct': 0}
        
        position = self.positions[symbol]
        entry_value = position['shares'] * position['entry_price']
        current_value = position['shares'] * current_price
        
        pnl = current_value - entry_value
        return_pct = pnl / entry_value * 100 if entry_value > 0 else 0
        
        return {
            'pnl': pnl,
            'return_pct': return_pct,
            'entry_price': position['entry_price'],
            'current_price': current_price,
            'shares': position['shares']
        }
    
    def update_position(
        self,
        symbol: str,
        shares: int,
        entry_price: float,
        entry_date: datetime
    ):
        """
        更新持仓
        
        Args:
            symbol: 股票代码
            shares: 股数
            entry_price: 入场价格
            entry_date: 入场日期
        """
        self.positions[symbol] = {
            'shares': shares,
            'entry_price': entry_price,
            'entry_date': entry_date,
            'current_price': entry_price
        }
    
    def remove_position(self, symbol: str):
        """
        移除持仓
        
        Args:
            symbol: 股票代码
        """
        if symbol in self.positions:
            del self.positions[symbol]
    
    def get_holdings(self) -> pd.DataFrame:
        """
        获取持仓列表
        
        Returns:
            DataFrame: 持仓列表
        """
        if not self.positions:
            return pd.DataFrame()
        
        holdings = []
        for symbol, pos in self.positions.items():
            holdings.append({
                'symbol': symbol,
                'shares': pos['shares'],
                'entry_price': pos['entry_price'],
                'current_price': pos.get('current_price', pos['entry_price']),
                'entry_date': pos['entry_date']
            })
        
        return pd.DataFrame(holdings)
    
    def get_allocation(self, current_prices: Dict[str, float]) -> Dict[str, float]:
        """
        获取资产配置
        
        Args:
            current_prices: 当前价格字典
        
        Returns:
            Dict: 资产配置比例
        """
        total_value = self.total_value
        allocation = {'cash': self.cash / total_value}
        
        for symbol, pos in self.positions.items():
            if symbol in current_prices:
                pos_value = pos['shares'] * current_prices[symbol]
                allocation[symbol] = pos_value / total_value
        
        return allocation