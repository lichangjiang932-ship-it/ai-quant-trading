import json
from types import SimpleNamespace

import pytest

from frontend import api_server
from src.execution.brokers.base_broker import Order, OrderDirection, OrderType
from src.execution.brokers.simulated_broker import SimulatedBroker


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def funded_broker():
    broker = SimulatedBroker(initial_capital=100_000, enforce_market_hours=False)
    broker.connect()
    broker.update_quote("sh600000", {
        "name": "浦发银行",
        "price": 10.0,
        "pre_close": 10.0,
    })
    broker.place_order(
        Order("sh600000", OrderDirection.BUY, 100, OrderType.LIMIT, price=10.0)
    )
    return broker


def isolate_persistence(monkeypatch, broker):
    monkeypatch.setattr(api_server, "broker", broker)
    risk_manager = SimpleNamespace(peak_equity=100_000.0)
    monkeypatch.setattr(api_server, "risk_mgr", risk_manager)
    monkeypatch.setattr(api_server.config, "set", lambda *args: None)
    monkeypatch.setattr(api_server.config, "save_config", lambda *args: None)
    monkeypatch.setattr(api_server, "_persist_broker_state", lambda: None)
    monkeypatch.setattr(api_server, "_persist_risk_runtime", lambda: None)
    return risk_manager


def test_deposit_api_adds_cash_without_resetting_holdings(monkeypatch):
    broker = funded_broker()
    risk_manager = isolate_persistence(monkeypatch, broker)
    before = broker.get_account_info()
    positions = broker.positions
    order_history = broker.order_history
    trade_history = broker.trade_history
    old_peak = risk_manager.peak_equity

    response = api_server.deposit_account({"amount": 20_000})
    payload = response_json(response)

    assert payload["success"] is True
    assert broker.positions is positions
    assert broker.order_history is order_history
    assert broker.trade_history is trade_history
    assert broker.get_positions()[0].quantity == 100
    assert broker.cash == pytest.approx(before["cash"] + 20_000)
    assert broker.initial_capital == pytest.approx(before["initial_capital"] + 20_000)
    assert broker.get_account_info()["profit"] == pytest.approx(before["profit"])
    assert risk_manager.peak_equity >= old_peak + 20_000


def test_fee_update_does_not_reset_account(monkeypatch):
    broker = funded_broker()
    isolate_persistence(monkeypatch, broker)
    cash = broker.cash
    positions = broker.positions
    order_history = broker.order_history

    response = api_server.update_account({
        "commission_rate": 0.0002,
        "stamp_tax_rate": 0.0005,
        "min_commission": 3,
        "slippage": 0.0002,
    })
    payload = response_json(response)

    assert payload["success"] is True
    assert broker.cash == cash
    assert broker.positions is positions
    assert broker.order_history is order_history
    assert broker.get_positions()[0].quantity == 100
    assert payload["account"]["positions"] == 1
