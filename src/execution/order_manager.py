"""
订单管理器
"""
import pandas as pd
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum


class OrderType(Enum):
    """订单类型"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderSide(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Order:
    """订单类"""
    
    def __init__(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ):
        """
        初始化订单
        
        Args:
            symbol: 股票代码
            side: 订单方向
            quantity: 数量
            order_type: 订单类型
            price: 价格（限价单）
            stop_price: 止损价格
        """
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.order_type = order_type
        self.price = price
        self.stop_price = stop_price
        self.status = OrderStatus.PENDING
        self.filled_quantity = 0
        self.filled_price = None
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'quantity': self.quantity,
            'order_type': self.order_type.value,
            'price': self.price,
            'stop_price': self.stop_price,
            'status': self.status.value,
            'filled_quantity': self.filled_quantity,
            'filled_price': self.filled_price,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }


class OrderManager:
    """订单管理器类"""
    
    def __init__(self):
        """初始化订单管理器"""
        self.orders = []
        self.order_history = []
    
    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ) -> Order:
        """
        创建订单
        
        Args:
            symbol: 股票代码
            side: 订单方向
            quantity: 数量
            order_type: 订单类型
            price: 价格
            stop_price: 止损价格
        
        Returns:
            Order: 订单对象
        """
        order = Order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            stop_price=stop_price
        )
        
        self.orders.append(order)
        return order
    
    def cancel_order(self, order: Order) -> bool:
        """
        取消订单
        
        Args:
            order: 订单对象
        
        Returns:
            bool: 是否取消成功
        """
        if order.status == OrderStatus.PENDING:
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now()
            self.order_history.append(order)
            self.orders.remove(order)
            return True
        return False
    
    def fill_order(
        self,
        order: Order,
        filled_quantity: int,
        filled_price: float
    ):
        """
        成交订单
        
        Args:
            order: 订单对象
            filled_quantity: 成交数量
            filled_price: 成交价格
        """
        order.filled_quantity = filled_quantity
        order.filled_price = filled_price
        order.updated_at = datetime.now()
        
        if filled_quantity == order.quantity:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        
        self.order_history.append(order)
        if order in self.orders:
            self.orders.remove(order)
    
    def get_pending_orders(self) -> List[Order]:
        """
        获取待处理订单
        
        Returns:
            List[Order]: 待处理订单列表
        """
        return [o for o in self.orders if o.status == OrderStatus.PENDING]
    
    def get_order_history(self) -> pd.DataFrame:
        """
        获取订单历史
        
        Returns:
            DataFrame: 订单历史
        """
        if not self.order_history:
            return pd.DataFrame()
        
        return pd.DataFrame([o.to_dict() for o in self.order_history])

    # ── 新增: 订单过期与批量管理 ──

    def expire_stale_orders(self, max_age_seconds: int = 300) -> List[Order]:
        """将超过指定秒数的待处理订单标记为过期取消。"""
        now = datetime.now()
        expired = []
        for order in list(self.orders):
            if order.status == OrderStatus.PENDING:
                age = (now - order.created_at).total_seconds()
                if age > max_age_seconds:
                    order.status = OrderStatus.CANCELLED
                    order.updated_at = now
                    self.order_history.append(order)
                    self.orders.remove(order)
                    expired.append(order)
        return expired

    def cancel_all_pending(self) -> int:
        """取消所有待处理订单。返回取消数量。"""
        count = 0
        for order in list(self.orders):
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.now()
                self.order_history.append(order)
                self.orders.remove(order)
                count += 1
        return count

    def get_orders_by_symbol(self, symbol: str) -> List[Order]:
        """获取指定股票的所有订单。"""
        return [o for o in self.orders + [
            h for h in self.order_history
            if not hasattr(h, 'orders') or h not in self.orders
        ] if o.symbol == symbol]

    def get_orders_by_status(self, status: OrderStatus) -> List[Order]:
        """按状态筛选订单。"""
        return [o for o in self.orders if o.status == status]

    def get_order_summary(self) -> Dict:
        """订单摘要统计。"""
        pending = len(self.get_orders_by_status(OrderStatus.PENDING))
        filled = sum(1 for o in self.order_history if o.status == OrderStatus.FILLED)
        rejected = sum(1 for o in self.order_history if o.status == OrderStatus.REJECTED)
        cancelled = sum(1 for o in self.order_history if o.status == OrderStatus.CANCELLED)
        return {
            "pending": pending,
            "filled_today": filled,
            "rejected_today": rejected,
            "cancelled_today": cancelled,
            "total_history": len(self.order_history),
            "active_count": len(self.orders),
        }