"""
券商API基类
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime
import pandas as pd


class OrderDirection(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"  # 市价单
    LIMIT = "limit"    # 限价单
    STOP = "stop"      # 止损单


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Order:
    """订单类"""
    
    def __init__(
        self,
        symbol: str,
        direction: OrderDirection,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ):
        """
        初始化订单
        
        Args:
            symbol: 股票代码
            direction: 订单方向
            quantity: 数量（股）
            order_type: 订单类型
            price: 价格（限价单）
            stop_price: 止损价格
        """
        self.order_id = None
        self.symbol = symbol
        self.direction = direction
        self.quantity = quantity
        self.order_type = order_type
        self.price = price
        self.stop_price = stop_price
        self.status = OrderStatus.PENDING
        self.filled_quantity = 0
        self.filled_price = None
        self.commission = 0.0
        self.stamp_tax = 0.0
        self.realized_pnl = 0.0
        self.reject_reason = ""
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'direction': self.direction.value,
            'quantity': self.quantity,
            'order_type': self.order_type.value,
            'price': self.price,
            'stop_price': self.stop_price,
            'status': self.status.value,
            'filled_quantity': self.filled_quantity,
            'filled_price': self.filled_price,
            'commission': self.commission,
            'stamp_tax': self.stamp_tax,
            'realized_pnl': self.realized_pnl,
            'reject_reason': self.reject_reason,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }


class Position:
    """持仓类"""
    
    def __init__(
        self,
        symbol: str,
        quantity: int,
        avg_cost: float,
        market_value: float = 0,
        unrealized_pnl: float = 0,
        available_quantity: Optional[int] = None,
        today_bought: int = 0,
    ):
        """
        初始化持仓
        
        Args:
            symbol: 股票代码
            quantity: 持仓数量
            avg_cost: 平均成本
            market_value: 市值
            unrealized_pnl: 未实现盈亏
        """
        self.symbol = symbol
        self.quantity = quantity
        self.avg_cost = avg_cost
        self.market_value = market_value
        self.unrealized_pnl = unrealized_pnl
        self.available_quantity = quantity if available_quantity is None else available_quantity
        self.today_bought = today_bought
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'quantity': self.quantity,
            'avg_cost': self.avg_cost,
            'market_value': self.market_value,
            'unrealized_pnl': self.unrealized_pnl,
            'available_quantity': self.available_quantity,
            'today_bought': self.today_bought,
        }


class BaseBroker(ABC):
    """券商API基类"""
    
    def __init__(self, name: str = "BaseBroker"):
        """
        初始化券商API
        
        Args:
            name: 券商名称
        """
        self.name = name
        self.connected = False
        self.account_info = {}
    
    @abstractmethod
    def connect(self, **kwargs) -> bool:
        """
        连接券商
        
        Returns:
            bool: 是否连接成功
        """
        pass
    
    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass
    
    @abstractmethod
    def get_account_info(self) -> Dict:
        """
        获取账户信息
        
        Returns:
            Dict: 账户信息
        """
        pass
    
    @abstractmethod
    def get_positions(self) -> List[Position]:
        """
        获取持仓
        
        Returns:
            List[Position]: 持仓列表
        """
        pass
    
    @abstractmethod
    def place_order(self, order: Order) -> str:
        """
        下单
        
        Args:
            order: 订单对象
        
        Returns:
            str: 订单ID
        """
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        撤单
        
        Args:
            order_id: 订单ID
        
        Returns:
            bool: 是否撤单成功
        """
        pass
    
    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict:
        """
        获取订单状态
        
        Args:
            order_id: 订单ID
        
        Returns:
            Dict: 订单状态
        """
        pass
    
    @abstractmethod
    def get_order_history(self) -> pd.DataFrame:
        """
        获取订单历史
        
        Returns:
            DataFrame: 订单历史
        """
        pass
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected
