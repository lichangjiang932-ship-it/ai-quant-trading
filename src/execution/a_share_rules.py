"""A 股交易规则与交易时段工具。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from typing import Optional


@dataclass(frozen=True)
class MarketSession:
    code: str
    label: str
    is_open: bool


def normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().lower()
    if value.startswith(("sh", "sz", "bj")):
        return value
    if value.startswith(("6", "9", "5")):
        return "sh" + value
    if value.startswith(("0", "3", "1")):
        return "sz" + value
    if value.startswith(("4", "8")):
        return "bj" + value
    return value


def instrument_type(symbol: str) -> str:
    code = normalize_symbol(symbol)[-6:]
    if code.startswith(("5", "1")):
        return "etf"
    return "stock"


def is_a_share_symbol(symbol: str) -> bool:
    value = normalize_symbol(symbol)
    return (
        value.startswith(("sh", "sz", "bj"))
        and len(value) == 8
        and value[-6:].isdigit()
    )


def buy_lot_size(symbol: str) -> int:
    return 100


def normalize_buy_quantity(symbol: str, quantity: int) -> int:
    lot = buy_lot_size(symbol)
    return max(int(quantity) // lot * lot, 0)


def estimate_buy_cost(
    quantity: int,
    price: float,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    slippage: float = 0.0,
) -> float:
    if quantity <= 0 or price <= 0:
        return 0.0
    execution_price = price * (1 + max(slippage, 0))
    trade_amount = quantity * execution_price
    commission = max(trade_amount * max(commission_rate, 0), max(min_commission, 0))
    return trade_amount + commission


def buy_quantity_for_amount(
    symbol: str,
    amount: float,
    price: float,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    slippage: float = 0.0,
) -> int:
    """把预算换算为不超预算的 A 股合法整手数量。"""
    if amount <= 0 or price <= 0:
        return 0
    lot = buy_lot_size(symbol)
    execution_price = price * (1 + max(slippage, 0))
    quantity = int(amount / execution_price) // lot * lot
    while quantity > 0 and estimate_buy_cost(
        quantity,
        price,
        commission_rate,
        min_commission,
        slippage,
    ) > amount:
        quantity -= lot
    return max(quantity, 0)


def validate_quantity(symbol: str, side: str, quantity: int) -> Optional[str]:
    if quantity <= 0:
        return "数量必须大于 0"
    if side == "buy" and quantity % buy_lot_size(symbol) != 0:
        return f"A 股买入数量必须是 {buy_lot_size(symbol)} 股的整数倍"
    return None


def price_limit_pct(symbol: str, name: str = "") -> float:
    code = normalize_symbol(symbol)[-6:]
    upper_name = str(name or "").upper()
    if "ST" in upper_name:
        return 0.05
    if normalize_symbol(symbol).startswith("bj"):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def round_price(price: float, tick: float = 0.01) -> float:
    if price <= 0:
        return 0.0
    tick_decimal = Decimal(str(tick))
    rounded = (Decimal(str(price)) / tick_decimal).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ) * tick_decimal
    return float(rounded)


def price_limits(symbol: str, pre_close: float, name: str = "") -> tuple[float, float]:
    if pre_close <= 0:
        return 0.0, 0.0
    limit = price_limit_pct(symbol, name)
    return (
        round_price(pre_close * (1 - limit)),
        round_price(pre_close * (1 + limit)),
    )


def validate_order_price(
    symbol: str,
    price: float,
    pre_close: float = 0,
    name: str = "",
) -> Optional[str]:
    if price <= 0:
        return "委托价格必须大于 0"
    if abs(price - round_price(price)) > 1e-8:
        return "A 股委托价格最小变动单位为 0.01 元"
    lower, upper = price_limits(symbol, pre_close, name)
    if lower and price < lower:
        return f"委托价格低于跌停价 {lower:.2f}"
    if upper and price > upper:
        return f"委托价格高于涨停价 {upper:.2f}"
    return None


def backtest_trade_rejection(
    symbol: str,
    side: str,
    pre_close: float,
    open_price: float,
    high: float,
    low: float,
    volume: Optional[float],
    name: str = "",
) -> Optional[str]:
    """返回历史开盘委托无法成交的原因；None 表示可成交。"""
    try:
        open_price = float(open_price)
        high = float(high)
        low = float(low)
        pre_close = float(pre_close)
    except (TypeError, ValueError):
        return "价格数据无效"
    if not all(isfinite(value) and value > 0 for value in (open_price, high, low)):
        return "价格数据无效"
    if volume is not None:
        try:
            if float(volume) <= 0:
                return "停牌或零成交量"
        except (TypeError, ValueError):
            pass
    if not is_a_share_symbol(symbol):
        return None
    if not isfinite(pre_close) or pre_close <= 0:
        return "昨收价数据无效"
    lower_limit, upper_limit = price_limits(symbol, pre_close, name)
    half_tick = 0.005
    if side == "buy" and upper_limit and open_price >= upper_limit - half_tick:
        return f"开盘封涨停 {upper_limit:.2f}，买入无法保证成交"
    if side == "sell" and lower_limit and open_price <= lower_limit + half_tick:
        return f"开盘封跌停 {lower_limit:.2f}，卖出无法保证成交"
    return None


def market_session(now: Optional[datetime] = None) -> MarketSession:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return MarketSession("closed", "周末休市", False)
    current_time = current.time()
    if time(9, 15) <= current_time < time(9, 30):
        return MarketSession("auction", "集合竞价", False)
    if time(9, 30) <= current_time <= time(11, 30):
        return MarketSession("morning", "上午交易", True)
    if time(11, 30) < current_time < time(13, 0):
        return MarketSession("break", "午间休市", False)
    if time(13, 0) <= current_time <= time(15, 0):
        return MarketSession("afternoon", "下午交易", True)
    if current_time < time(9, 15):
        return MarketSession("pre_market", "盘前", False)
    return MarketSession("closed", "已收盘", False)
