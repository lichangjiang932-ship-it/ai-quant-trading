import json

import pandas as pd

from frontend import api_server


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_backtest_api_returns_unfilled_order_reasons(monkeypatch):
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    frame = pd.DataFrame({
        "Open": [10.0, 11.0, 10.8],
        "High": [10.2, 11.0, 11.0],
        "Low": [9.8, 11.0, 10.6],
        "Close": [10.0, 11.0, 10.9],
        "Volume": [1000, 1000, 1000],
    }, index=dates)
    monkeypatch.setattr(api_server, "_load_daily_frame", lambda *_: frame)
    monkeypatch.setattr(
        api_server,
        "_signal_at",
        lambda closes, index, strategy, params: "buy" if index == 0 else None,
    )

    response = api_server.run_backtest(
        symbol="sh600000",
        strategy="cross_ma",
        fast=5,
        slow=20,
        count=180,
    )
    payload = response_json(response)

    assert payload["trades"] == []
    assert payload["rejected_order_count"] == 1
    assert payload["rejected_orders"][0]["side"] == "buy"
    assert "涨停" in payload["rejected_orders"][0]["reason"]
