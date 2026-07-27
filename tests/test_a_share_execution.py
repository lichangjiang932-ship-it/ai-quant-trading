from datetime import timedelta

from src.execution.a_share_rules import (
    backtest_trade_rejection,
    buy_quantity_for_amount,
    estimate_buy_cost,
    market_session,
    price_limits,
    validate_order_price,
    validate_quantity,
)
from src.execution.brokers.base_broker import Order, OrderDirection, OrderStatus, OrderType
from src.execution.brokers.simulated_broker import SimulatedBroker


def make_broker(**kwargs):
    broker = SimulatedBroker(
        initial_capital=100_000,
        commission_rate=0.0003,
        min_commission=5,
        stamp_tax_rate=0.001,
        slippage=0.001,
        **kwargs,
    )
    broker.connect()
    broker.update_quote("sh600000", {
        "name": "浦发银行",
        "price": 10.0,
        "pre_close": 10.0,
    })
    return broker


def test_a_share_board_price_limits():
    assert price_limits("sh600000", 10.0) == (9.0, 11.0)
    assert price_limits("sz300750", 100.0) == (80.0, 120.0)
    assert price_limits("bj830799", 10.0) == (7.0, 13.0)
    assert price_limits("sh600000", 10.0, "ST 测试") == (9.5, 10.5)


def test_quantity_and_tick_validation():
    assert validate_quantity("sh600000", "buy", 150) is not None
    assert validate_quantity("sh600000", "buy", 200) is None
    assert validate_quantity("sh600000", "sell", 37) is None
    assert validate_order_price("sh600000", 10.001, 10.0) is not None
    assert validate_order_price("sh600000", 11.01, 10.0) is not None


def test_amount_budget_converts_to_whole_lots_with_fees():
    quantity = buy_quantity_for_amount(
        "sh600000",
        amount=10_050,
        price=10.0,
        commission_rate=0.0003,
        min_commission=5,
        slippage=0.001,
    )

    assert quantity == 1000
    assert estimate_buy_cost(1000, 10.0, 0.0003, 5, 0.001) <= 10_050
    assert buy_quantity_for_amount("sh600000", 1000, 10.0, min_commission=5) == 0


def test_market_order_applies_slippage_and_entry_commission():
    broker = make_broker()
    order = Order(
        "sh600000", OrderDirection.BUY, 100, OrderType.MARKET, price=10.0
    )
    order_id = broker.place_order(order)
    status = broker.get_order_status(order_id)
    position = broker.get_positions()[0]

    assert status["status"] == "filled"
    assert status["filled_price"] == 10.01
    assert position.avg_cost == 10.06
    assert position.available_quantity == 0
    assert position.today_bought == 100


def test_etf_sell_is_exempt_from_stamp_tax():
    broker = make_broker()
    broker.update_quote("sz159272", {
        "name": "创业板ETF",
        "price": 10.0,
        "pre_close": 10.0,
    })
    broker.place_order(
        Order("sz159272", OrderDirection.BUY, 100, OrderType.LIMIT, price=10.0)
    )
    broker._session_date -= timedelta(days=1)
    order_id = broker.place_order(
        Order("sz159272", OrderDirection.SELL, 100, OrderType.LIMIT, price=10.0)
    )

    assert broker.get_order_status(order_id)["stamp_tax"] == 0


def test_backtest_tradeability_blocks_suspension_and_limit_boards():
    assert backtest_trade_rejection(
        "sh600000", "buy", 10, 11, 11, 11, 1_000_000
    ) is not None
    assert backtest_trade_rejection(
        "sh600000", "sell", 10, 9, 9, 9, 1_000_000
    ) is not None
    assert backtest_trade_rejection(
        "sh600000", "buy", 10, 10.2, 10.4, 10.1, 0
    ) == "停牌或零成交量"
    assert backtest_trade_rejection(
        "sh600000", "buy", 10, 10.2, 10.4, 10.1, 1_000_000
    ) is None


def test_t_plus_one_blocks_same_day_sell_then_releases_next_day():
    broker = make_broker()
    buy = Order("sh600000", OrderDirection.BUY, 100, OrderType.LIMIT, price=10.0)
    broker.place_order(buy)

    sell = Order("sh600000", OrderDirection.SELL, 100, OrderType.LIMIT, price=10.0)
    sell_id = broker.place_order(sell)
    rejected = broker.get_order_status(sell_id)
    assert rejected["status"] == "rejected"
    assert "T+1" in rejected["reject_reason"]

    broker._session_date -= timedelta(days=1)
    assert broker.get_positions()[0].available_quantity == 100
    next_day_sell = Order(
        "sh600000", OrderDirection.SELL, 100, OrderType.LIMIT, price=10.0
    )
    filled_id = broker.place_order(next_day_sell)
    assert broker.get_order_status(filled_id)["status"] == "filled"


def test_paper_account_state_round_trip():
    broker = make_broker()
    broker.place_order(
        Order("sh600000", OrderDirection.BUY, 100, OrderType.LIMIT, price=10.0)
    )
    state = broker.export_state()

    restored = make_broker()
    assert restored.restore_state(state) is True
    assert restored.cash == broker.cash
    assert restored.get_positions()[0].quantity == 100
    assert restored.get_order_history().iloc[-1]["status"] == OrderStatus.FILLED.value


def test_market_session_reports_known_state():
    assert market_session().code in {
        "pre_market", "auction", "morning", "break", "afternoon", "closed"
    }
