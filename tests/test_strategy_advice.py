from types import SimpleNamespace

from frontend.api_server import _strategy_position_advice, _strategy_universe


def opportunity():
    return {
        "buy_low": 9.8,
        "buy_high": 10.2,
        "stop_loss": 9.2,
        "target_price": 11.5,
        "suggested_qty": 500,
    }


def test_wait_has_different_meaning_for_empty_and_held_stock():
    empty = _strategy_position_advice(None, None, None, opportunity(), 10.0)
    position = SimpleNamespace(
        quantity=500,
        available_quantity=500,
        avg_cost=9.5,
    )
    held = _strategy_position_advice(
        None,
        position,
        {"action": "hold", "protective_stop": 9.3, "target_price": 11.2},
        opportunity(),
        10.0,
    )

    assert empty["decision"] == "wait"
    assert "暂不买入" in empty["label"]
    assert held["decision"] == "hold"
    assert "继续持有" in held["label"]
    assert "保护位" in held["detail"]


def test_held_sell_signal_respects_t_plus_one():
    position = SimpleNamespace(quantity=300, available_quantity=0, avg_cost=10.0)
    advice = _strategy_position_advice(
        "sell",
        position,
        {"action": "wait_t1", "pending_action": "sell"},
        opportunity(),
        9.2,
    )

    assert advice["decision"] == "wait_t1"
    assert advice["suggested_quantity"] == 0
    assert "下一交易日" in advice["detail"]


def test_strategy_universe_always_includes_positions_first():
    symbols = _strategy_universe(
        ["sh600000", "sz000001"],
        ["sz159326", "sh600000"],
    )

    assert symbols == ["sz159326", "sh600000", "sz000001"]
