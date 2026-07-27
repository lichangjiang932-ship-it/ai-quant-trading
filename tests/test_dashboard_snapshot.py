from types import SimpleNamespace

import pandas as pd

from frontend import api_server


def test_stream_snapshot_is_atomic_and_includes_selected_symbol(monkeypatch):
    calls = []
    updated = []
    position = SimpleNamespace(to_dict=lambda: {
        "symbol": "sh600000",
        "quantity": 100,
        "market_value": 1010.0,
    })

    monkeypatch.setattr(api_server, "_get_watchlist", lambda: ["sh600000"])
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda: SimpleNamespace(code="morning", label="上午交易", is_open=True),
    )

    def get_quotes(symbols, sources):
        calls.append(("quotes", list(symbols)))
        return {
            symbol: {"symbol": symbol, "price": 10.1, "change_pct": 1.0}
            for symbol in symbols
        }

    monkeypatch.setattr(api_server.realtime, "get_quotes", get_quotes)
    monkeypatch.setattr(
        api_server.broker,
        "update_quote",
        lambda symbol, quote: updated.append(symbol),
    )
    monkeypatch.setattr(
        api_server.broker,
        "get_account_info",
        lambda: {
            "total_asset": 100_000,
            "initial_capital": 100_000,
            "cash": 98_990,
            "market_value": 1010,
            "profit": 0,
            "profit_pct": 0,
        },
    )
    monkeypatch.setattr(api_server.broker, "get_positions", lambda: [position])
    monkeypatch.setattr(
        api_server.broker,
        "get_order_history",
        lambda: pd.DataFrame([{"symbol": "sh600000", "status": "filled"}]),
    )

    def portfolio_snapshot():
        calls.append(("portfolio", list(updated)))
        return {
            "total_asset": 100_000,
            "cash": 98_990,
            "market_value": 1010,
            "total_position_pct": 0.0101,
            "positions": [position],
        }

    monkeypatch.setattr(api_server, "_portfolio_snapshot", portfolio_snapshot)
    monkeypatch.setattr(api_server.risk_mgr, "get_risk_report", lambda: {})

    snapshot = api_server._stream_snapshot(["sz000001", "sh600000"])

    assert calls[0] == ("quotes", ["sh600000", "sz000001"])
    assert calls[1] == ("portfolio", ["sh600000", "sz000001"])
    assert snapshot["watchlist"] == ["sh600000"]
    assert set(snapshot["quotes"]) == {"sh600000", "sz000001"}
    assert snapshot["positions"][0]["quantity"] == 100
    assert snapshot["orders"][0]["status"] == "filled"


def test_frontend_uses_sse_with_snapshot_fallback():
    html = (api_server.FRONTEND_DIR + "/index.html")
    with open(html, "r", encoding="utf-8") as file:
        source = file.read()

    assert "new EventSource" in source
    assert "/api/snapshot?symbol=" in source
    assert "setInterval(refreshLight" not in source
    assert "visibilitychange" in source
