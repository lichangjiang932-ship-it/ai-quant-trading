"""
模拟券商（用于测试和回测）
"""
from typing import Dict, List, Optional
from datetime import datetime
import pandas as pd
import uuid
from .base_broker import BaseBroker, Order, Position, OrderDirection, OrderType, OrderStatus
from ..a_share_rules import instrument_type, market_session, validate_order_price, validate_quantity


class SimulatedBroker(BaseBroker):
    """模拟券商类"""
    
    def __init__(
        self,
        initial_capital: float = 1000000,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.0005,
        min_commission: float = 5,
        slippage: float = 0.0001,
        enforce_market_hours: bool = False,
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
        self.enforce_market_hours = enforce_market_hours
        
        self.positions = {}
        self.orders = []
        self.order_history = []
        self.trade_history = []
        self._quote_meta = {}
        self._session_date = datetime.now().date()
    
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
        self._roll_trading_day()
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
        self._roll_trading_day()
        positions = []
        for symbol, pos in self.positions.items():
            if pos['quantity'] > 0:
                positions.append(Position(
                    symbol=symbol,
                    quantity=pos['quantity'],
                    avg_cost=pos['avg_cost'],
                    market_value=pos['quantity'] * pos.get('current_price', pos['avg_cost']),
                    unrealized_pnl=pos.get('current_price', pos['avg_cost']) * pos['quantity'] - pos['avg_cost'] * pos['quantity'],
                    available_quantity=max(pos['quantity'] - pos.get('today_bought', 0), 0),
                    today_bought=pos.get('today_bought', 0),
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
        self._roll_trading_day()
        # 生成订单ID
        order.order_id = str(uuid.uuid4())[:8]
        order.status = OrderStatus.SUBMITTED
        order.created_at = datetime.now()
        
        quantity_error = validate_quantity(
            order.symbol, order.direction.value, order.quantity
        )
        if quantity_error:
            return self._reject(order, quantity_error)

        session = market_session()
        if self.enforce_market_hours and not session.is_open:
            return self._reject(order, f"当前为{session.label}，已启用交易时段限制")

        quote = self._quote_meta.get(order.symbol, {})
        reference_price = order.price or quote.get('price') or self.positions.get(
            order.symbol, {}
        ).get('current_price', 0)
        price_error = validate_order_price(
            order.symbol,
            float(reference_price or 0),
            float(quote.get('pre_close', 0) or 0),
            str(quote.get('name', '') or ''),
        )
        if price_error:
            return self._reject(order, price_error)

        # 检查是否有足够资金（买入时）
        if order.direction == OrderDirection.BUY:
            estimated_price = self._execution_price(order, reference_price)
            
            total_cost = order.quantity * estimated_price
            commission = max(total_cost * self.commission_rate, self.min_commission)
            
            if self.cash < total_cost + commission:
                return self._reject(order, "可用资金不足")
        
        # 检查是否有足够持仓（卖出时）
        if order.direction == OrderDirection.SELL:
            position = self.positions.get(order.symbol)
            available = 0 if not position else max(
                position['quantity'] - position.get('today_bought', 0), 0
            )
            if order.quantity > available:
                return self._reject(
                    order,
                    f"T+1 可卖数量不足，当前可卖 {available} 股",
                )
        
        # 模拟成交
        self._fill_order(order)
        
        return order.order_id
    
    def _fill_order(self, order: Order):
        """模拟成交"""
        quote = self._quote_meta.get(order.symbol, {})
        reference_price = order.price or quote.get('price') or self.positions.get(
            order.symbol, {}
        ).get('current_price', 0)
        order.filled_price = self._execution_price(order, reference_price)
        
        order.filled_quantity = order.quantity
        order.status = OrderStatus.FILLED
        order.updated_at = datetime.now()
        
        # 计算费用
        trade_amount = order.filled_price * order.filled_quantity
        
        if order.direction == OrderDirection.BUY:
            # 买入：收取佣金
            commission = max(trade_amount * self.commission_rate, self.min_commission)
            total_cost = trade_amount + commission
            order.commission = commission
            
            self.cash -= total_cost
            
            # 更新持仓
            if order.symbol in self.positions:
                pos = self.positions[order.symbol]
                total_quantity = pos['quantity'] + order.filled_quantity
                pos['avg_cost'] = (
                    pos['avg_cost'] * pos['quantity'] + trade_amount + commission
                ) / total_quantity
                pos['quantity'] = total_quantity
                pos['today_bought'] = pos.get('today_bought', 0) + order.filled_quantity
                pos['current_price'] = order.filled_price
            else:
                self.positions[order.symbol] = {
                    'quantity': order.filled_quantity,
                    'avg_cost': total_cost / order.filled_quantity,
                    'current_price': order.filled_price,
                    'today_bought': order.filled_quantity,
                }
        
        else:  # SELL
            # 卖出：收取佣金和印花税
            commission = max(trade_amount * self.commission_rate, self.min_commission)
            stamp_tax = 0 if instrument_type(order.symbol) == 'etf' else trade_amount * self.stamp_tax_rate
            total_income = trade_amount - commission - stamp_tax
            order.commission = commission
            order.stamp_tax = stamp_tax
            
            self.cash += total_income
            
            # 更新持仓
            if order.symbol in self.positions:
                pos = self.positions[order.symbol]
                avg_cost = pos['avg_cost']
                pos['quantity'] -= order.filled_quantity
                # 卖出优先冲抵昨仓；今日买入的锁定量最多只能减到剩余持仓数，
                # 否则当日"买入后再卖昨仓"会把 today_bought 留在高位，
                # 导致 available_quantity = quantity - today_bought 被低估。
                pos['today_bought'] = min(pos.get('today_bought', 0), pos['quantity'])
                order.realized_pnl = (
                    trade_amount - avg_cost * order.filled_quantity - commission - stamp_tax
                )
        
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
            'pnl': order.realized_pnl,
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
        self._quote_meta.setdefault(symbol, {})['price'] = price

    def update_quote(self, symbol: str, quote: Dict):
        """更新价格以及涨跌停校验所需的昨收和名称。"""
        clean_quote = dict(quote or {})
        self._quote_meta[symbol] = clean_quote
        price = float(clean_quote.get('price', 0) or 0)
        if price > 0 and symbol in self.positions:
            self.positions[symbol]['current_price'] = price

    def export_state(self) -> Dict:
        """导出可持久化的模拟账户状态。"""
        self._roll_trading_day()
        return {
            'version': 1,
            'initial_capital': self.initial_capital,
            'cash': self.cash,
            'positions': self.positions,
            'orders': [order.to_dict() for order in self.order_history[-500:]],
            'trade_history': self.trade_history[-1000:],
            'session_date': self._session_date.isoformat(),
        }

    def restore_state(self, state: Optional[Dict]) -> bool:
        """恢复模拟账户，失败时保留新账户。"""
        if not state or not isinstance(state, dict):
            return False
        try:
            self.initial_capital = float(state.get('initial_capital', self.initial_capital))
            self.cash = float(state.get('cash', self.initial_capital))
            self.positions = dict(state.get('positions') or {})
            self.trade_history = list(state.get('trade_history') or [])
            session_date = str(state.get('session_date') or '')
            if session_date != datetime.now().date().isoformat():
                for position in self.positions.values():
                    position['today_bought'] = 0
            self._session_date = datetime.now().date()
            self.order_history = []
            for item in state.get('orders') or []:
                order = Order(
                    symbol=item['symbol'],
                    direction=OrderDirection(item['direction']),
                    quantity=int(item['quantity']),
                    order_type=OrderType(item.get('order_type', 'limit')),
                    price=item.get('price'),
                    stop_price=item.get('stop_price'),
                )
                order.order_id = item.get('order_id')
                order.status = OrderStatus(item.get('status', 'filled'))
                order.filled_quantity = int(item.get('filled_quantity', 0) or 0)
                order.filled_price = item.get('filled_price')
                order.commission = float(item.get('commission', 0) or 0)
                order.stamp_tax = float(item.get('stamp_tax', 0) or 0)
                order.realized_pnl = float(item.get('realized_pnl', 0) or 0)
                order.reject_reason = str(item.get('reject_reason', '') or '')
                if item.get('created_at'):
                    order.created_at = datetime.fromisoformat(item['created_at'])
                if item.get('updated_at'):
                    order.updated_at = datetime.fromisoformat(item['updated_at'])
                self.order_history.append(order)
            self.orders = [
                order for order in self.order_history
                if order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED)
            ]
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def _roll_trading_day(self):
        today = datetime.now().date()
        if today == self._session_date:
            return
        for position in self.positions.values():
            position['today_bought'] = 0
        self._session_date = today

    def _execution_price(self, order: Order, reference_price: float) -> float:
        price = float(reference_price or 0)
        if order.order_type != OrderType.MARKET:
            return price
        direction = 1 if order.direction == OrderDirection.BUY else -1
        return round(price * (1 + direction * max(self.slippage, 0)), 2)

    def _reject(self, order: Order, reason: str) -> str:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.updated_at = datetime.now()
        self.order_history.append(order)
        return order.order_id
