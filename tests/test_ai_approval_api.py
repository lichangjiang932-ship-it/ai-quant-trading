import json
from types import SimpleNamespace

from fastapi.responses import JSONResponse

from frontend import api_server


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def setup_dependencies(monkeypatch, signal):
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {
            "sh600000": {
                "price": 10.0,
                "change_pct": 0,
                "data_source": "tencent",
            }
        },
    )
    monkeypatch.setattr(api_server, "_load_daily_frame", lambda *args: None)
    monkeypatch.setattr(
        api_server,
        "_portfolio_snapshot",
        lambda: {
            "total_asset": 100_000,
            "cash": 80_000,
            "pos_map": {},
        },
    )
    monkeypatch.setattr(
        api_server.professional_decision,
        "market_regime",
        lambda data: SimpleNamespace(to_dict=lambda: {"allow_new_positions": True}),
    )
    monkeypatch.setattr(
        api_server,
        "_deterministic_trade_signal",
        lambda *args, **kwargs: dict(signal),
    )
    monkeypatch.setattr(api_server, "_record_trade_approval", lambda *args: None)


def test_ai_trade_rejects_when_server_recheck_is_hold(monkeypatch):
    setup_dependencies(monkeypatch, {
        "action": "hold",
        "generated_at": "2026-07-10T10:00:00",
        "price": 10,
        "suggested_qty": 0,
        "confidence": 0.4,
        "risk_reward": 1.0,
        "data_quality": {"allowed": True, "score": 90},
    })

    payload = response_json(api_server.ai_trade({
        "symbol": "sh600000",
        "action": "buy",
        "price": 10,
    }))

    assert payload["success"] is False
    assert payload["approval"]["status"] == "rejected"


def test_ai_trade_executes_only_approved_quantity(monkeypatch):
    setup_dependencies(monkeypatch, {
        "action": "buy",
        "generated_at": "",
        "price": 10,
        "suggested_qty": 200,
        "confidence": 0.75,
        "risk_reward": 1.8,
        "buy_high": 10.2,
        "stop_loss": 9.2,
        "data_quality": {"allowed": True, "score": 90},
    })
    placed = {}

    def fake_place_order(body):
        placed.update(body)
        return JSONResponse({"success": True, "order_id": "paper-approved"})

    monkeypatch.setattr(api_server, "place_order", fake_place_order)

    payload = response_json(api_server.ai_trade({
        "symbol": "sh600000",
        "action": "buy",
        "quantity": 500,
        "price": 10,
    }))

    assert payload["success"] is True
    assert placed["quantity"] == 200
    assert placed["reason"] == "ai_trade_approved"
    assert payload["approval"]["status"] == "approved"


def test_dashboard_signal_endpoint_returns_placeholder_without_waiting(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            started.append(True)

    monkeypatch.setattr(api_server, "_dashboard_signal_cache", {})
    monkeypatch.setattr(api_server, "_dashboard_signal_refreshing", False)
    monkeypatch.setattr(api_server.threading, "Thread", FakeThread)

    signals = api_server._dashboard_signals_snapshot(["sh600000"])

    assert started == [True]
    assert signals[0]["action"] == "hold"
    assert "后台更新" in signals[0]["reason"]
