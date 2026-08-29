"""A 股交易规则与交易时段工具。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
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
    # 北交所需优先判定: 43/83/87/88 开头, 以及 2023 年启用的 920xxx 新代码段。
    # 若放到 6/9/5 之后, 920xxx 会被误判成沪市(9开头)。
    if value.startswith(("4", "8", "92")):
        return "bj" + value
    if value.startswith(("6", "9", "5")):
        return "sh" + value
    if value.startswith(("0", "3", "1")):
        return "sz" + value
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
    # 节假日判断（简单版：只排除周末；精确版需要交易日历）
    if current.weekday() >= 5:
        return MarketSession("closed", "周末休市", False)
    current_time = current.time()
    if time(9, 15) <= current_time < time(9, 25):
        return MarketSession("auction", "集合竞价(9:15-9:25)", False)
    if time(9, 25) <= current_time < time(9, 30):
        return MarketSession("auction_end", "竞价结束(9:25-9:30)", False)
    if time(9, 30) <= current_time <= time(11, 30):
        return MarketSession("morning", "上午连续竞价", True)
    if time(11, 30) < current_time < time(13, 0):
        return MarketSession("break", "午间休市", False)
    if time(13, 0) <= current_time <= time(15, 0):
        return MarketSession("afternoon", "下午连续竞价", True)
    # 深市尾盘集合竞价 (14:57-15:00)
    if time(14, 57) <= current_time <= time(15, 0):
        return MarketSession("closing_auction", "尾盘集合竞价", True)
    if current_time < time(9, 15):
        return MarketSession("pre_market", "盘前", False)
    return MarketSession("closed", "已收盘", False)


# ── 新增: 交易日历与特殊规则 ──

# 2024-2025 中国法定节假日（A股休市日）
_CN_HOLIDAYS_2024_2025 = {
    "2024-01-01",  # 元旦
    "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",  # 春节
    "2024-04-04", "2024-04-05",  # 清明节
    "2024-05-01", "2024-05-02", "2024-05-03",  # 劳动节
    "2024-06-10",  # 端午节
    "2024-09-16", "2024-09-17",  # 中秋节
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",  # 国庆节
    "2025-01-01",  # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-04-04",  # 清明节
    "2025-05-01", "2025-05-02",  # 劳动节
    "2025-06-02",  # 端午节
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07",  # 国庆节
}


def is_trading_day(date_obj: Optional[datetime] = None) -> bool:
    """判断是否为A股交易日（考虑周末和法定节假日）。"""
    d = date_obj or datetime.now()
    if d.weekday() >= 5:
        return False
    date_str = d.strftime("%Y-%m-%d")
    return date_str not in _CN_HOLIDAYS_2024_2025


def is_call_auction(now: Optional[datetime] = None) -> bool:
    """判断是否处于集合竞价时段（9:15-9:25）。"""
    current = now or datetime.now()
    return time(9, 15) <= current.time() < time(9, 25)


def is_closing_auction(now: Optional[datetime] = None) -> bool:
    """判断是否处于深市尾盘集合竞价（14:57-15:00）。"""
    current = now or datetime.now()
    return time(14, 57) <= current.time() <= time(15, 0)


def can_trade_now(symbol: str = "", now: Optional[datetime] = None) -> tuple[bool, str]:
    """检查当前是否可以交易。返回 (可交易, 原因)。"""
    current = now or datetime.now()
    if not is_trading_day(current):
        return False, "非交易日"
    session = market_session(current)
    if not session.is_open:
        return False, session.label
    # 集合竞价期间部分交易规则不同
    if is_call_auction(current):
        return False, "集合竞价期间仅接受限价委托，不支持市价单"
    if is_closing_auction(current) and symbol.startswith("sz"):
        return False, "深市尾盘集合竞价（14:57-15:00）"
    return True, "可交易"


def can_sell_today(buy_date: datetime, now: Optional[datetime] = None) -> tuple[bool, str]:
    """判断T+1规则：买入后下一交易日才可卖出。"""
    current = now or datetime.now()
    if not is_trading_day(current):
        return False, "非交易日"
    # 找到买入日后的下一个交易日
    check_date = buy_date + timedelta(days=1)
    while not is_trading_day(check_date):
        check_date += timedelta(days=1)
    if current.date() < check_date.date():
        return False, f"T+1 限制：最早可卖日为 {check_date.strftime('%Y-%m-%d')}"
    return True, "可卖出"


def stamp_duty(side: str, amount: float) -> float:
    """A股印花税：卖出时征收成交金额的0.05%（2024年减半后）。"""
    if str(side or "").lower() == "sell":
        return max(amount * 0.0005, 0.0)
    return 0.0


def commission(amount: float, rate: float = 0.0003, min_fee: float = 5.0) -> float:
    """A股佣金：买卖双向，最低5元。"""
    return max(amount * max(rate, 0), max(min_fee, 0))


def estimate_total_cost(
    side: str, quantity: int, price: float,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    slippage: float = 0.0,
) -> float:
    """估算含所有费用的总成本（佣金+印花税+滑点）。"""
    execution_price = price * (1 + max(slippage, 0)) if side == "buy" else price * (1 - max(slippage, 0))
    amount = quantity * execution_price
    comm = commission(amount, commission_rate, min_commission)
    stamp = stamp_duty(side, amount)
    return amount + comm + stamp


# ── 可转债/ETF 特殊规则 ──

def is_convertible_bond(symbol: str, name: str = "") -> bool:
    """判断是否为可转债（代码11/12开头）。"""
    code = normalize_symbol(symbol)[-6:]
    return code.startswith(("11", "12"))


def is_etf(symbol: str) -> bool:
    """判断是否为ETF（代码5/1/51/58/159开头）。"""
    code = normalize_symbol(symbol)[-6:]
    return code.startswith(("5", "1")) or code.startswith(("159", "510", "511", "512", "513", "515", "516", "517", "518", "588", "589"))


def bond_lot_size(symbol: str) -> int:
    """可转债最小交易单位：10张（深市）/ 10张（沪市）。"""
    return 10


def etf_lot_size(symbol: str) -> int:
    """ETF最小交易单位：100份。"""
    return 100


def get_lot_size(symbol: str) -> int:
    """获取品种最小交易单位。"""
    if is_convertible_bond(symbol):
        return bond_lot_size(symbol)
    if is_etf(symbol):
        return etf_lot_size(symbol)
    return buy_lot_size(symbol)


def validate_quantity_extended(symbol: str, side: str, quantity: int) -> Optional[str]:
    """扩展的数量校验（支持可转债/ETF）。"""
    if quantity <= 0:
        return "数量必须大于 0"
    if side == "buy":
        lot = get_lot_size(symbol)
        if quantity % lot != 0:
            return f"买入数量必须是 {lot} 的整数倍"
    return None
