"""
QMT -> FastBroker 接口适配器

问题背景: 新版引擎 engine.py 的执行路径按 FastBroker 风格调用券商:
    update_price / buy / sell / get_account_info / get_position_quantity /
    get_positions / sync_to_db
    且 buy/sell 返回 (success, order_id, ExecOrder)。
而真实实盘券商 QMTBroker 是 place_order(Order) 风格。本适配器把 QMTBroker 包装成
FastBroker 同款接口,使 QMT 实盘可以直接接入新引擎,无需改动引擎执行逻辑。

安全说明: 只有当 config 中 broker.type=qmt 且 QMT 连接成功时才会用到本类;
连接失败由引擎回退到模拟 FastBroker(见 engine._create_broker)。
"""
import time
from typing import Dict, List, Optional, Tuple

from .qmt_broker import QMTBroker
from .base_broker import Order, OrderDirection, OrderType
from ..fast_broker import ExecOrder


class QMTFastAdapter:
    """把 QMTBroker 适配成 FastBroker 风格接口"""

    def __init__(
        self,
        account_id: str = "",
        mini_qmt_path: str = "",
        account_type: str = "STOCK",
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        min_commission: float = 5.0,
    ):
        self.qmt = QMTBroker(account_id=account_id, account_type=account_type,
                             mini_qmt_path=mini_qmt_path)
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission

        self._price_cache: Dict[str, float] = {}
        self._trade_count = 0
        self._total_latency = 0.0

    def connect(self) -> bool:
        return self.qmt.connect()

    def disconnect(self):
        self.qmt.disconnect()

    # ---- 行情 ----
    def update_price(self, symbol: str, price: float):
        self._price_cache[symbol] = price

    # ---- 下单 ----
    def buy(self, symbol: str, quantity: int, price: float = None,
            reason: str = "") -> Tuple[bool, str, ExecOrder]:
        return self._place(symbol, OrderDirection.BUY, quantity, price, reason, "B")

    def sell(self, symbol: str, quantity: int = 0, price: float = None,
             reason: str = "") -> Tuple[bool, str, ExecOrder]:
        # quantity<=0 表示全平: 用当前持仓数量
        if quantity is None or quantity <= 0:
            quantity = self.get_position_quantity(symbol)
        if quantity <= 0:
            return False, "无持仓", ExecOrder()
        return self._place(symbol, OrderDirection.SELL, quantity, price, reason, "S")

    def _place(self, symbol: str, direction: OrderDirection, quantity: int,
               price: Optional[float], reason: str, prefix: str) -> Tuple[bool, str, ExecOrder]:
        t0 = time.perf_counter_ns()
        exec_price = price or self._price_cache.get(symbol, 0)
        if not exec_price or exec_price <= 0:
            return False, "无效价格", ExecOrder()
        if quantity <= 0:
            return False, "无效数量", ExecOrder()

        # 有指定价用限价单,否则市价单(交给 QMT 用最新价成交)
        order_type = OrderType.LIMIT if price else OrderType.MARKET
        order = Order(symbol=symbol, direction=direction, quantity=quantity,
                      order_type=order_type, price=exec_price)

        order_id = self.qmt.place_order(order)
        latency_ms = (time.perf_counter_ns() - t0) / 1_000_000

        if not order_id:
            return False, "QMT下单失败", ExecOrder(latency_ms=latency_ms)

        amount = exec_price * quantity
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate if direction == OrderDirection.SELL else 0.0

        exec_order = ExecOrder(
            order_id=str(order_id),
            symbol=symbol,
            direction=direction.value,
            quantity=quantity,
            price=exec_price,
            status="submitted",
            filled_quantity=quantity,
            filled_price=exec_price,
            commission=round(commission, 2),
            stamp_tax=round(stamp_tax, 2),
            latency_ms=latency_ms,
            created_at=time.time(),
            reason=reason,
        )
        self._trade_count += 1
        self._total_latency += latency_ms
        return True, str(order_id), exec_order

    # ---- 查询 ----
    def get_position_quantity(self, symbol: str) -> int:
        for pos in self.qmt.get_positions():
            if pos.symbol == symbol:
                return int(pos.quantity)
        return 0

    def get_positions(self) -> List[Dict]:
        result = []
        for pos in self.qmt.get_positions():
            price = self._price_cache.get(pos.symbol, pos.avg_cost)
            result.append({
                'symbol': pos.symbol,
                'quantity': pos.quantity,
                'avg_cost': pos.avg_cost,
                'market_value': pos.market_value or round(pos.quantity * price, 2),
                'current_price': price,
                'pnl': pos.unrealized_pnl,
            })
        return result

    def get_account_info(self) -> Dict:
        info = self.qmt.get_account_info()
        total = info.get('total_asset', 0) or 0
        cash = info.get('cash', 0) or 0
        market_value = info.get('market_value', 0) or 0
        profit = total - self.initial_capital
        return {
            'total_asset': round(total, 2),
            'cash': round(cash, 2),
            'market_value': round(market_value, 2),
            'profit': round(profit, 2),
            'profit_pct': round(profit / self.initial_capital * 100, 4) if self.initial_capital > 0 else 0,
            'initial_capital': self.initial_capital,
            'trade_count': self._trade_count,
            'avg_latency_ms': round(self._total_latency / max(self._trade_count, 1), 3),
        }

    def sync_to_db(self, state_manager):
        info = self.qmt.get_account_info()
        state_manager.save_account_state('cash', info.get('cash', 0) or 0)
        for pos in self.qmt.get_positions():
            state_manager.save_position(pos.symbol, pos.quantity, pos.avg_cost)
