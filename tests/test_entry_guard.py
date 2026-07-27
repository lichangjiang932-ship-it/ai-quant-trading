import json
from datetime import datetime
from types import SimpleNamespace

from frontend import api_server
from src.analysis.entry_guard import EntryGuard


def positive_research(guard: EntryGuard):
    return guard.build_research_snapshot(
        news_items=[{
            "news": {"title": "订单增长且机构上调评级"},
            "sentiment": {"score": 0.65, "importance": 8},
        }],
        fundamentals={
            "roe": 16,
            "profit_yoy": 22,
            "revenue_yoy": 12,
            "pe_ttm": 24,
        },
        capital_flow={
            "total_main_net": 5_000_000,
            "last_main_net": 600_000,
        },
        market_regime={
            "code": "risk_on",
            "label": "进攻环境",
            "score": 80,
            "allow_new_positions": True,
        },
        average_amount=20_000_000,
    )


def plan():
    return {
        "action": "buy",
        "buy_low": 9.8,
        "buy_high": 10.4,
        "stop_loss": 9.5,
        "target_price": 11.2,
        "suggested_qty": 500,
        "average_amount": 20_000_000,
    }


def validation():
    return {"samples": 10, "win_rate": 62, "avg_return": 1.8}


def moderate_quote(price=10.2):
    return {
        "name": "测试股票",
        "price": price,
        "pre_close": 10.0,
        "open": 10.05,
        "high": 10.3,
        "low": 9.95,
        "change_pct": (price / 10.0 - 1) * 100,
        "data_source": "tencent",
    }


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_entry_guard_allows_moderate_confirmed_move():
    guard = EntryGuard()
    result = guard.evaluate(
        "sh600000",
        moderate_quote(),
        plan(),
        validation(),
        research=positive_research(guard),
        reference_price=10.2,
        market_open=True,
    )

    assert result["allowed"] is True
    assert result["action"] == "buy"
    assert result["profit_guaranteed"] is False
    assert result["target_scenario"]["net_profit"] > 0
    assert result["break_even_price"] > 10.2


def test_entry_guard_rejects_large_same_day_gain():
    guard = EntryGuard()
    quote = {
        "price": 10.8,
        "pre_close": 10.0,
        "open": 10.2,
        "high": 10.9,
        "low": 10.1,
        "change_pct": 8.0,
    }
    wide_plan = {**plan(), "buy_high": 11.0, "target_price": 12.0}

    result = guard.evaluate(
        "sh600000",
        quote,
        wide_plan,
        validation(),
        research=positive_research(guard),
        reference_price=10.8,
        market_open=True,
    )

    assert result["allowed"] is False
    assert any("防追高" in reason for reason in result["reasons"])


def test_entry_guard_rejects_intraday_fade():
    guard = EntryGuard()
    quote = {
        "price": 10.3,
        "pre_close": 10.0,
        "open": 10.5,
        "high": 10.8,
        "low": 10.0,
        "change_pct": 3.0,
    }
    wide_plan = {**plan(), "buy_low": 9.8, "buy_high": 10.8}

    result = guard.evaluate(
        "sh600000",
        quote,
        wide_plan,
        validation(),
        research=positive_research(guard),
        reference_price=10.3,
        market_open=True,
    )

    assert result["allowed"] is False
    assert result["intraday"]["fading"] is True
    assert any("冲高回落" in reason for reason in result["reasons"])


def test_entry_guard_replays_fee_adjusted_profit_from_recommendation_price():
    guard = EntryGuard()
    result = guard.evaluate(
        "sh600000",
        moderate_quote(price=10.1),
        plan(),
        validation(),
        research=positive_research(guard),
        reference_price=10.0,
        market_open=True,
    )

    replay = result["if_bought_at_analysis"]
    assert replay["available"] is True
    assert replay["profitable_now"] is True
    assert 0 < replay["net_pnl"] < 50
    assert replay["net_return_pct"] < 1.0


def test_prescreen_drops_chasing_candidate(monkeypatch):
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {
            "sh600001": {
                "price": 10.8, "pre_close": 10, "open": 10.2,
                "high": 10.9, "low": 10.1, "change_pct": 8,
                "amount": 800_000_000,
            },
            "sh600002": {
                "price": 10.2, "pre_close": 10, "open": 10.05,
                "high": 10.3, "low": 9.95, "change_pct": 2,
                "amount": 800_000_000,
            },
        },
    )
    candidates = [
        {"symbol": "sh600001", "amount": 800_000_000, "candidate_source": "momentum"},
        {"symbol": "sh600002", "amount": 800_000_000, "candidate_source": "liquidity"},
    ]

    result = api_server._prescreen(candidates, top=5)

    assert [item["symbol"] for item in result] == ["sh600002"]


def test_pick_status_revokes_buy_when_price_has_weakened(monkeypatch):
    guard = api_server.entry_guard
    research = positive_research(guard)
    stored = {
        "symbol": "sh600000",
        "name": "测试股票",
        "action": "buy",
        "price": 10.0,
        "analysis_price": 10.0,
        "generated_at": datetime.now().isoformat(),
        "planned_qty": 500,
        "suggested_qty": 500,
        "entry_plan": plan(),
        "validation": validation(),
        "research": research,
    }
    original = dict(api_server._scan_job)
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {
            "sh600000": {
                "price": 9.7,
                "pre_close": 10.0,
                "open": 10.1,
                "high": 10.4,
                "low": 9.65,
                "change_pct": -3.0,
            }
        },
    )
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda now: SimpleNamespace(is_open=True),
    )
    api_server._scan_job.update({
        "status": "done",
        "pool": "market",
        "total": 1,
        "done": 1,
        "current": "",
        "candidates": 20,
        "picks": [stored],
        "started_at": stored["generated_at"],
        "finished_at": stored["generated_at"],
        "error": "",
    })
    try:
        payload = response_json(api_server.ai_pick_status())
    finally:
        api_server._scan_job.clear()
        api_server._scan_job.update(original)

    assert payload["buy_count"] == 0
    assert payload["picks"][0]["action"] == "hold"
    assert payload["picks"][0]["entry_guard"]["allowed"] is False
    assert payload["profit_guaranteed"] is False


def test_pick_execute_cannot_bypass_live_chasing_guard(monkeypatch):
    guard = api_server.entry_guard
    generated_at = datetime.now().isoformat()
    recommendation = {
        "symbol": "sh600000",
        "action": "buy",
        "price": 10.8,
        "analysis_price": 10.8,
        "generated_at": generated_at,
        "suggested_qty": 500,
        "entry_plan": {**plan(), "buy_high": 11.0, "target_price": 12.0},
        "validation": validation(),
        "research": positive_research(guard),
    }
    original = dict(api_server._scan_job)
    monkeypatch.setattr(
        api_server.realtime,
        "get_quotes",
        lambda *args, **kwargs: {
            "sh600000": {
                "price": 10.8,
                "pre_close": 10.0,
                "open": 10.2,
                "high": 10.9,
                "low": 10.1,
                "change_pct": 8.0,
            }
        },
    )
    monkeypatch.setattr(
        api_server,
        "market_session",
        lambda now: SimpleNamespace(is_open=True),
    )
    api_server._scan_job.update({
        "status": "done",
        "picks": [recommendation],
        "started_at": generated_at,
        "finished_at": generated_at,
    })
    try:
        payload = response_json(api_server.ai_pick_execute({
            "symbol": "sh600000",
            "quantity": 500,
        }))
    finally:
        api_server._scan_job.clear()
        api_server._scan_job.update(original)

    assert payload["success"] is False
    assert payload["entry_guard"]["allowed"] is False
    assert any("防追高" in reason for reason in payload["entry_guard"]["reasons"])
