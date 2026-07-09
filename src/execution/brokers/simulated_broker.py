"""
模拟券商（用于测试和回测）
"""
from typing import Dict, List, Optional
from datetime import datetime
import pandas as pd
import uuid
from .base_broker import BaseBroker, Order, Position, OrderDirection, OrderType, OrderStatus


class SimulatedBroker(BaseBroker):
    """模拟券商类"""
    
    def __init__(
        self,
        initial_capital: float = 1000000,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        min_commission: float = 5,
        slippage: float = 0.0001
    ):
        """
        初始化模拟券商
        
        Args:
            initial_capital: 初始资金
            commission_rate: 佣金费率
            stamp_tax_rate: 印花税率（卖出时收取）
            min_commission: 最低佣金
            slippage: 滑点（万分之）
        """
        super().__init__("SimulatedBroker")
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission
        self.slippage = slippage
        
        self.positions = {}
        self.orders = []
        self.order_history = []
        self.trade_history = []
    
    def connect(self, **kwargs) -> bool:
        """连接模拟券商"""
        self.connected = True
        self.account_info = {
            'total_asset': self.initial_capital,
            'cash': self.cash,
            'market_value': 0,
            'frozen_cash': 0
        }
        return True
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
    
    def get_account_info(self) -> Dict:
        """获取账户信息"""
        market_value = sum(
            pos['quantity'] * pos.get('current_price', pos['avg_cost'])
            for pos in self.positions.values()
        ) if self.positions else 0
        
        return {
            'total_asset': self.cash + market_value,
            'cash': self.cash,
            'market_value': market_value,
            'frozen_cash': 0,
            'initial_capital': self.initial_capital,
            'profit': self.cash + market_value - self.initial_capital,
            'profit_pct': (self.cash + market_value - self.initial_capital) / self.initial_capital * 100
        }
    
    def get_positions(self) -> List[Position]:
        """获取持仓"""
        positions = []
        for symbol, pos in self.positions.items():
            if pos['quantity'] > 0:
                positions.append(Position(
                    symbol=symbol,
                    quantity=pos['quantity'],
                    avg_cost=pos['avg_cost'],
                    market_value=pos['quantity'] * pos.get('current_price', pos['avg_cost']),
                    unrealized_pnl=pos.get('current_price', pos['avg_cost']) * pos['quantity'] - pos['avg_cost'] * pos['quantity']
                ))
        return positions
    
    def place_order(self, order: Order) -> str:
        """
        下单
        
        Args:
            order: 订单对象
        
        Returns:
            str: 订单ID
        """
        # 生成订单ID
        order.order_id = str(uuid.uuid4())[:8]
        order.status = OrderStatus.SUBMITTED
        order.created_at = datetime.now()
        
        # 检查是否有足够资金（买入时）
        if order.direction == OrderDirection.BUY:
            if order.order_type == OrderType.MARKET and order.price is None:
                # 市价单需要估算价格
                estimated_price = self.positions.get(order.symbol, {}).get('current_price', 10)
            else:
                estimated_price = order.price
            
            total_cost = order.quantity * estimated_price
            commission = max(total_cost * self.commission_rate, self.min_commission)
            
            if self.cash < total_cost + commission:
                order.status = OrderStatus.REJECTED
                self.order_history.append(order)
                return order.order_id
        
        # 检查是否有足够持仓（卖出时）
        if order.direction == OrderDirection.SELL:
            if order.symbol not in self.positions or self.positions[order.symbol]['quantity'] < order.quantity:
                order.status = OrderStatus.REJECTED
                self.order_history.append(order)
                return order.order_id
        
        # 模拟成交
        self._fill_order(order)
        
        return order.order_id
    
    def _fill_order(self, order: Order):
        """模拟成交"""
        # 设置成交价格
        if order.order_type == OrderType.MARKET:
            order.filled_price = self.positions.get(order.symbol, {}).get('current_price', 10)
        else:
            order.filled_price = order.price
        
        order.filled_quantity = order.quantity
        order.status = OrderStatus.FILLED
        order.updated_at = datetime.now()
        
        # 计算费用
        trade_amount = order.filled_price * order.filled_quantity
        
        if order.direction == OrderDirection.BUY:
            # 买入：收取佣金
            commission = max(trade_amount * self.commission_rate, self.min_commission)
            total_cost = trade_amount + commission
            
            self.cash -= total_cost
            
            # 更新持仓
            if order.symbol in self.positions:
                pos = self.positions[order.symbol]
                total_quantity = pos['quantity'] + order.filled_quantity
                pos['avg_cost'] = (pos['avg_cost'] * pos['quantity'] + order.filled_price * order.filled_quantity) / total_quantity
                pos['quantity'] = total_quantity
            else:
                self.positions[order.symbol] = {
                    'quantity': order.filled_quantity,
                    'avg_cost': order.filled_price,
                    'current_price': order.filled_price
                }
        
        else:  # SELL
            # 卖出：收取佣金和印花税
            commission = max(trade_amount * self.commission_rate, self.min_commission)
            stamp_tax = trade_amount * self.stamp_tax_rate
            total_income = trade_amount - commission - stamp_tax
            
            self.cash += total_income
            
            # 更新持仓
            if order.symbol in self.positions:
                self.positions[order.symbol]['quantity'] -= order.filled_quantity
        
        # 记录交易
        self.trade_history.append({
            'order_id': order.order_id,
            'symbol': order.symbol,
            'direction': order.direction.value,
            'quantity': order.filled_quantity,
            'price': order.filled_price,
            'amount': trade_amount,
            'commission': commission if 'commission' in locals() else 0,
            'stamp_tax': stamp_tax if 'stamp_tax' in locals() else 0,
            'datetime': datetime.now()
        })
        
        # 记录订单
        self.orders.append(order)
        self.order_history.append(order)
    
    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        for order in self.orders:
            if order.order_id == order_id and order.status == OrderStatus.SUBMITTED:
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.now()
                return True
        return False
    
    def get_order_status(self, order_id: str) -> Dict:
        for order in self.orders:
            if order.order_id == order_id:
                return order.to_dict()
        for order in self.order_history:
            if order.order_id == order_id:
                return order.to_dict()
        return {}
    
    def get_order_history(self) -> pd.DataFrame:
        """获取订单历史"""
        if not self.order_history:
            return pd.DataFrame()
        
        return pd.DataFrame([o.to_dict() for o in self.order_history])
    
    def get_trade_history(self) -> pd.DataFrame:
        """获取交易历史"""
        if not self.trade_history:
            return pd.DataFrame()
        
        return pd.DataFrame(self.trade_history)
    
    def update_market_price(self, symbol: str, price: float):
        """
        更新市场价格
        
        Args:
            symbol: 股票代码
            price: 当前价格
        """
        if symbol in self.positions:
            self.positions[symbol]['current_price'] = price