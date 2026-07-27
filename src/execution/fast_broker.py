import time
import uuid
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

from .a_share_rules import instrument_type


class ExecSpeed(Enum):
    INSTANT = "instant"
    FAST = "fast"
    NORMAL = "normal"


@dataclass
class ExecOrder:
    order_id: str = ""
    symbol: str = ""
    direction: str = ""
    quantity: int = 0
    price: float = 0.0
    order_type: str = "market"
    status: str = "pending"
    filled_quantity: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    latency_ms: float = 0.0
    created_at: float = 0.0
    filled_at: float = 0.0
    reason: str = ""


class FastBroker:
    def __init__(
        self,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.0005,
        min_commission: float = 5.0,
        exec_speed: ExecSpeed = ExecSpeed.INSTANT
    ):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission
        self.exec_speed = exec_speed

        self.positions: Dict[str, Dict] = {}
        self.orders: List[ExecOrder] = []
        self._price_cache: Dict[str, float] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="broker")
        self._trade_count = 0
        self._total_latency = 0.0

    def update_price(self, symbol: str, price: float):
        self._price_cache[symbol] = price
        if symbol in self.positions:
            self.positions[symbol]['current_price'] = price

    def buy(self, symbol: str, quantity: int, price: float = None,
            reason: str = "") -> Tuple[bool, str, ExecOrder]:
        t0 = time.perf_counter_ns()

        exec_price = price or self._price_cache.get(symbol, 0)
        if exec_price <= 0:
            return False, "无效价格", ExecOrder()

        total_cost = quantity * exec_price
        commission = max(total_cost * self.commission_rate, self.min_commission)
        total_needed = total_cost + commission

        if self.cash < total_needed:
            max_qty = int((self.cash - self.min_commission) / exec_price / 100) * 100
            if max_qty <= 0:
                return False, "资金不足", ExecOrder()
            quantity = max_qty
            total_cost = quantity * exec_price
            commission = max(total_cost * self.commission_rate, self.min_commission)
            total_needed = total_cost + commission

        order = ExecOrder(
            order_id=self._gen_id("B"),
            symbol=symbol,
            direction="buy",
            quantity=quantity,
            price=exec_price,
            status="filled",
            filled_quantity=quantity,
            filled_price=exec_price,
            commission=round(commission, 2),
            created_at=time.time(),
            filled_at=time.time(),
            reason=reason,
        )

        self._apply_buy(order)
        self.orders.append(order)
        self._trade_count += 1

        t1 = time.perf_counter_ns()
        order.latency_ms = (t1 - t0) / 1_000_000
        self._total_latency += order.latency_ms

        return True, order.order_id, order

    def sell(self, symbol: str, quantity: int = 0, price: float = None,
             reason: str = "") -> Tuple[bool, str, ExecOrder]:
        t0 = time.perf_counter_ns()

        if symbol not in self.positions or self.positions[symbol]['quantity'] <= 0:
            return False, "无持仓", ExecOrder()

        pos = self.positions[symbol]
        sell_qty = quantity if quantity > 0 else pos['quantity']
        sell_qty = min(sell_qty, pos['quantity'])

        exec_price = price or self._price_cache.get(symbol, pos.get('avg_cost', 0))
        if exec_price <= 0:
            return False, "无效价格", ExecOrder()

        total_revenue = sell_qty * exec_price
        commission = max(total_revenue * self.commission_rate, self.min_commission)
        stamp_tax = 0 if instrument_type(symbol) == 'etf' else total_revenue * self.stamp_tax_rate
        entry_cost = sell_qty * pos['avg_cost']
        pnl = total_revenue - entry_cost - commission - stamp_tax

        order = ExecOrder(
            order_id=self._gen_id("S"),
            symbol=symbol,
            direction="sell",
            quantity=sell_qty,
            price=exec_price,
            status="filled",
            filled_quantity=sell_qty,
            filled_price=exec_price,
            commission=round(commission, 2),
            stamp_tax=round(stamp_tax, 2),
            created_at=time.time(),
            filled_at=time.time(),
            reason=reason,
        )

        self._apply_sell(order, pnl)
        self.orders.append(order)
        self._trade_count += 1

        t1 = time.perf_counter_ns()
        order.latency_ms = (t1 - t0) / 1_000_000
        self._total_latency += order.latency_ms

        return True, order.order_id, order

    def _apply_buy(self, order: ExecOrder):
        total_cost = order.filled_quantity * order.filled_price
        commission = max(total_cost * self.commission_rate, self.min_commission)
        self.cash -= (total_cost + commission)

        if order.symbol in self.positions:
            pos = self.positions[order.symbol]
            total_qty = pos['quantity'] + order.filled_quantity
            pos['avg_cost'] = (pos['avg_cost'] * pos['quantity'] + order.filled_price * order.filled_quantity) / total_qty
            pos['quantity'] = total_qty
        else:
            self.positions[order.symbol] = {
                'quantity': order.filled_quantity,
                'avg_cost': order.filled_price,
                'current_price': order.filled_price,
                'entry_time': datetime.now(),
            }

    def _apply_sell(self, order: ExecOrder, pnl: float):
        total_revenue = order.filled_quantity * order.filled_price
        commission = max(total_revenue * self.commission_rate, self.min_commission)
        stamp_tax = 0 if instrument_type(order.symbol) == 'etf' else total_revenue * self.stamp_tax_rate
        self.cash += (total_revenue - commission - stamp_tax)

        if order.symbol in self.positions:
            self.positions[order.symbol]['quantity'] -= order.filled_quantity
            if self.positions[order.symbol]['quantity'] <= 0:
                del self.positions[order.symbol]

    def get_account_info(self) -> Dict:
        market_value = 0
        for sym, pos in self.positions.items():
            price = pos.get('current_price', pos['avg_cost'])
            market_value += pos['quantity'] * price

        total = self.cash + market_value
        profit = total - self.initial_capital
        return {
            'total_asset': round(total, 2),
            'cash': round(self.cash, 2),
            'market_value': round(market_value, 2),
            'profit': round(profit, 2),
            'profit_pct': round(profit / self.initial_capital * 100, 4) if self.initial_capital > 0 else 0,
            'initial_capital': self.initial_capital,
            'trade_count': self._trade_count,
            'avg_latency_ms': round(self._total_latency / max(self._trade_count, 1), 3),
        }

    def get_positions(self) -> List[Dict]:
        return [
            {
                'symbol': sym,
                'quantity': pos['quantity'],
                'avg_cost': pos['avg_cost'],
                'market_value': round(pos['quantity'] * pos.get('current_price', pos['avg_cost']), 2),
                'current_price': pos.get('current_price', pos['avg_cost']),
                'pnl': round(pos['quantity'] * (pos.get('current_price', pos['avg_cost']) - pos['avg_cost']), 2),
            }
            for sym, pos in self.positions.items() if pos['quantity'] > 0
        ]

    def _gen_id(self, prefix: str) -> str:
        ts = int(time.time() * 1000) % 1000000
        return f"{prefix}{ts:06d}{uuid.uuid4().hex[:6].upper()}"

    def get_latency_report(self) -> Dict:
        return {
            'avg_latency_ms': round(self._total_latency / max(self._trade_count, 1), 3),
            'total_trades': self._trade_count,
            'orders_count': len(self.orders),
        }

    def sync_to_db(self, state_manager):
        state_manager.save_account_state('cash', self.cash)
        for sym, pos in self.positions.items():
            state_manager.save_position(sym, pos['quantity'], pos['avg_cost'])
