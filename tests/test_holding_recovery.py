import numpy as np
import pandas as pd

from src.analysis.holding_recovery import HoldingRecoveryAnalyzer


def make_frame(up: bool = True, periods: int = 100) -> pd.DataFrame:
    close = np.linspace(8.0, 10.0, periods) if up else np.linspace(12.0, 8.0, periods)
    return pd.DataFrame({
        "Open": close * 0.998,
        "High": close * 1.015,
        "Low": close * 0.985,
        "Close": close,
        "Volume": np.linspace(1_000_000, 1_400_000, periods),
    })


def positive_context():
    return {
        "opportunity": {
            "suggested_qty": 400,
            "buy_low": 9.8,
            "buy_high": 10.2,
            "stop_loss": 9.2,
            "target_price": 11.5,
            "average_amount": 20_000_000,
        },
        "exit_plan": {
            "action": "hold",
            "pending_action": "hold",
            "protective_stop": 9.2,
            "target_price": 11.5,
        },
        "market_regime": {
            "code": "risk_on",
            "label": "进攻环境",
            "score": 82,
            "position_multiplier": 1.0,
            "allow_new_positions": True,
            "ret20_pct": 4.2,
            "drawdown60_pct": -1.5,
        },
        "news_items": [
            {
                "news": {"title": "公司订单增长，机构上调评级"},
                "sentiment": {"score": 0.7, "importance": 8},
            }
        ],
        "fundamentals": {
            "roe": 16,
            "profit_yoy": 24,
            "revenue_yoy": 15,
            "pe_ttm": 22,
            "pb": 2.1,
        },
        "capital_flow": {
            "total_main_net": 5_000_000,
            "last_main_net": 800_000,
        },
    }


def test_recovery_allows_only_confirmed_small_addition():
    result = HoldingRecoveryAnalyzer().analyze(
        "sh600000",
        make_frame(up=True),
        quantity=500,
        available_quantity=500,
        avg_cost=10.5,
        current_price=10.0,
        **positive_context(),
    )

    assert result["decision"] == "add"
    assert result["suggested_buy_quantity"] == 400
    assert result["suggested_sell_quantity"] == 0
    assert result["recovery_score"] >= 72
    assert set(result["factors"]) == {"technical", "market", "news", "fundamental", "capital"}


def test_recovery_sells_when_stop_and_trend_are_broken():
    context = positive_context()
    context["exit_plan"] = {
        "action": "sell",
        "pending_action": "sell",
        "protective_stop": 8.5,
        "target_price": 10.8,
    }
    context["market_regime"] = {
        "code": "risk_off",
        "label": "防守环境",
        "score": 20,
        "position_multiplier": 0,
        "allow_new_positions": False,
        "ret20_pct": -8,
        "drawdown60_pct": -14,
    }

    result = HoldingRecoveryAnalyzer().analyze(
        "sh600000",
        make_frame(up=False),
        quantity=600,
        available_quantity=600,
        avg_cost=10.0,
        current_price=8.0,
        **context,
    )

    assert result["decision"] == "sell"
    assert result["suggested_sell_quantity"] == 600
    assert result["suggested_buy_quantity"] == 0


def test_recovery_exit_respects_t_plus_one_lock():
    context = positive_context()
    context["exit_plan"] = {
        "action": "wait_t1",
        "pending_action": "sell",
        "protective_stop": 8.5,
        "target_price": 10.8,
    }

    result = HoldingRecoveryAnalyzer().analyze(
        "sh600000",
        make_frame(up=False),
        quantity=300,
        available_quantity=0,
        avg_cost=10.0,
        current_price=8.0,
        **context,
    )

    assert result["decision"] == "wait_t1"
    assert result["pending_action"] == "sell"
    assert result["suggested_sell_quantity"] == 0
    assert "下一交易日" in result["detail"]


def test_missing_research_data_never_triggers_averaging_down():
    context = positive_context()
    context["news_items"] = []
    context["fundamentals"] = {}
    context["capital_flow"] = {}

    result = HoldingRecoveryAnalyzer().analyze(
        "sh600000",
        make_frame(up=True),
        quantity=500,
        available_quantity=500,
        avg_cost=10.5,
        current_price=10.0,
        **context,
    )

    assert result["decision"] == "hold"
    assert result["suggested_buy_quantity"] == 0
    assert result["data_completeness"] < 0.8
