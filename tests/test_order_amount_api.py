import json

from frontend import api_server


class FakeBroker:
    commission_rate = 0.0003
    min_commission = 5.0
    slippage = 0.001

    def __init__(self):
        self.order = None

    def update_market_price(self, symbol, price):
        return None

    def place_order(self, order):
        self.order = order
        order.order_id = "amount-1"
        return order.order_id

    def get_order_status(self, order_id):
        return {
            "status": "filled",
            "filled_quantity": self.order.quantity,
            "filled_price": self.order.price,
            "realized_pnl": 0,
        }

    def get_account_info(self):
        return {"cash": 90_000, "total_asset": 100_000, "market_value": 10_000}


class FakeRiskManager:
    def record_order(self, order):
        return None

    def update_daily_pnl(self, pnl):
        return None


def setup_order_dependencies(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(api_server, "broker", broker)
    monkeypatch.setattr(api_server, "risk_mgr", FakeRiskManager())
    monkeypatch.setattr(
        api_server,
        "_pre_trade_check",
        lambda *args, **kwargs: {"allowed": True, "reason": "风控通过"},
    )
    monkeypatch.setattr(api_server, "_persist_broker_state", lambda: None)
    monkeypatch.setattr(api_server, "_persist_risk_runtime", lambda: None)
    api_server._order_idempotency.clear()
    return broker


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_amount_order_is_converted_again_on_server(monkeypatch):
    broker = setup_order_dependencies(monkeypatch)

    response = api_server.place_order({
        "symbol": "sh600000",
        "side": "buy",
        "price": 10,
        "quantity": 0,
        "amount": 10_050,
        "input_mode": "amount",
        "order_type": "market",
    })
    payload = response_json(response)

    assert payload["success"] is True
    assert payload["calculated_quantity"] == 1000
    assert broker.order.quantity == 1000


def test_amount_order_explains_minimum_lot_budget(monkeypatch):
    setup_order_dependencies(monkeypatch)

    response = api_server.place_order({
        "symbol": "sh600000",
        "side": "buy",
        "price": 10,
        "amount": 1_000,
        "input_mode": "amount",
        "order_type": "market",
    })
    payload = response_json(response)

    assert payload["success"] is False
    assert "100 股" in payload["error"]


def test_amount_order_is_auto_detected_without_mode_field(monkeypatch):
    broker = setup_order_dependencies(monkeypatch)

    response = api_server.place_order({
        "symbol": "sz159326",
        "side": "buy",
        "price": 1.79,
        "quantity": 0,
        "amount": 500,
        "order_type": "market",
    })
    payload = response_json(response)

    assert payload["success"] is True
    assert payload["input_mode"] == "amount"
    assert payload["calculated_quantity"] == 200
    assert broker.order.quantity == 200
