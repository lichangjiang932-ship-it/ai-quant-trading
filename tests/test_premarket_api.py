from datetime import datetime

from fastapi.responses import JSONResponse

from frontend import api_server
from src.execution.a_share_rules import MarketSession


def make_plan():
    return {
        "status": "ready",
        "trade_date": "2026-07-10",
        "benchmark_symbol": "sh000001",
        "entries": [{
            "symbol": "sh600000",
            "decision": "buy",
            "previous_close": 10.0,
            "max_buy_price": 10.2,
            "stop_loss": 9.2,
            "suggested_qty": 100,
            "execution": None,
        }],
        "position_exits": [],
    }


def test_premarket_execution_rejects_chasing_gap(monkeypatch):
    monkeypatch.setattr(api_server, "_premarket_plan", make_plan())
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda now=None: MarketSession("morning", "上午交易", True),
    )
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {"sh600000": {"price": 10.3}},
    )

    result = api_server._execute_premarket_entry(
        "sh600000", datetime(2026, 7, 10, 9, 31)
    )

    assert result["success"] is False
    assert "取消追高" in result["error"]


def test_premarket_execution_records_confirmed_paper_order(monkeypatch):
    monkeypatch.setattr(api_server, "_premarket_plan", make_plan())
    monkeypatch.setattr(api_server, "_persist_premarket_plan", lambda: None)
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda now=None: MarketSession("morning", "上午交易", True),
    )
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {"sh600000": {"price": 10.05}},
    )
    monkeypatch.setattr(api_server, "_portfolio_snapshot", lambda: {"cash": 100_000})
    monkeypatch.setattr(
        api_server,
        "place_order",
        lambda body: JSONResponse({"success": True, "order_id": "paper-1"}),
    )

    result = api_server._execute_premarket_entry(
        "sh600000", datetime(2026, 7, 10, 9, 31)
    )

    assert result["success"] is True
    assert result["execution"]["quantity"] == 100
    assert api_server._premarket_plan["entries"][0]["execution"]["success"] is True


def test_premarket_execution_stops_during_market_shock(monkeypatch):
    monkeypatch.setattr(api_server, "_premarket_plan", make_plan())
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda now=None: MarketSession("morning", "上午交易", True),
    )
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {
            "sh600000": {"price": 10.05},
            "sh000001": {"change_pct": -2.5},
        },
    )

    result = api_server._execute_premarket_entry(
        "sh600000", datetime(2026, 7, 10, 9, 31)
    )

    assert result["success"] is False
    assert "市场冲击保护" in result["error"]
