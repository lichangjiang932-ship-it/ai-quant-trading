"""
实时策略基类
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Optional, Callable
from datetime import datetime
import pandas as pd


class SignalType(Enum):
    """信号类型"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class TradingSignal:
    """交易信号类"""
    
    def __init__(
        self,
        symbol: str,
        signal_type: SignalType,
        price: float,
        quantity: int,
        reason: str = "",
        confidence: float = 1.0
    ):
        """
        初始化交易信号
        
        Args:
            symbol: 股票代码
            signal_type: 信号类型
            price: 建议价格
            quantity: 建议数量
            reason: 原因
            confidence: 置信度 (0-1)
        """
        self.symbol = symbol
        self.signal_type = signal_type
        self.price = price
        self.quantity = quantity
        self.reason = reason
        self.confidence = confidence
        self.timestamp = datetime.now()
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'signal_type': self.signal_type.value,
            'price': self.price,
            'quantity': self.quantity,
            'reason': self.reason,
            'confidence': self.confidence,
            'timestamp': self.timestamp.isoformat()
        }


class RealtimeStrategy(ABC):
    """实时策略基类"""
    
    def __init__(
        self,
        name: str,
        symbols: List[str],
        parameters: Optional[Dict] = None
    ):
        """
        初始化实时策略
        
        Args:
            name: 策略名称
            symbols: 监控的股票代码列表
            parameters: 策略参数
        """
        self.name = name
        self.symbols = symbols
        self.parameters = parameters or {}
        self.signals = []
        self.signal_callbacks = []
        self.last_data = {}
        self.position_limits = {}
    
    def register_signal_callback(self, callback: Callable):
        """注册信号回调"""
        self.signal_callbacks.append(callback)
    
    def set_position_limit(self, symbol: str, max_shares: int):
        """设置仓位限制"""
        self.position_limits[symbol] = max_shares
    
    @abstractmethod
    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        """
        处理实时行情
        
        Args:
            symbol: 股票代码
            quote: 实时行情
        
        Returns:
            Optional[TradingSignal]: 交易信号
        """
        pass
    
    @abstractmethod
    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[TradingSignal]:
        """
        处理K线数据
        
        Args:
            symbol: 股票代码
            bar_data: K线数据
        
        Returns:
            Optional[TradingSignal]: 交易信号
        """
        pass
    
    def update_data(self, symbol: str, data: Dict):
        """更新数据缓存"""
        self.last_data[symbol] = data
    
    def emit_signal(self, signal: TradingSignal):
        """
        发送交易信号
        
        Args:
            signal: 交易信号
        """
        self.signals.append(signal)
        
        # 调用回调函数
        for callback in self.signal_callbacks:
            try:
                callback(signal)
            except Exception as e:
                print(f"信号回调出错: {e}")
    
    def get_signals(self, limit: int = 100) -> List[Dict]:
        """获取历史信号"""
        return [s.to_dict() for s in self.signals[-limit:]]
    
    def calculate_position_size(
        self,
        price: float,
        portfolio_value: float,
        max_position_pct: float = 0.1
    ) -> int:
        """
        计算仓位大小
        
        Args:
            price: 当前价格
            portfolio_value: 组合总价值
            max_position_pct: 最大仓位比例
        
        Returns:
            int: 建议股数（100的整数倍）
        """
        max_amount = portfolio_value * max_position_pct
        shares = int(max_amount / price / 100) * 100
        return max(shares, 0)