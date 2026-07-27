from datetime import datetime

import numpy as np
import pandas as pd

from src.analysis.premarket import PremarketAnalyzer, target_trading_date


def make_market_data(periods=240, falling=False):
    rng = np.random.default_rng(7)
    returns = rng.normal(-0.003 if falling else 0.001, 0.012, periods)
    if not falling:
        returns[1:] += np.clip(returns[:-1], -0.01, 0.01) * 0.35
    close = 20 * np.cumprod(1 + returns)
    open_price = np.r_[close[0], close[:-1] * (1 + rng.normal(0, 0.004, periods - 1))]
    high = np.maximum(open_price, close) * 1.012
    low = np.minimum(open_price, close) * 0.988
    volume = 1_000_000 * (1 + rng.uniform(-0.25, 0.35, periods))
    return pd.DataFrame({
        "Open": open_price,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    }, index=pd.date_range("2025-01-01", periods=periods, freq="B"))


def test_forecast_uses_stable_probability_schema():
    result = PremarketAnalyzer().forecast("sh600000", make_market_data()).to_dict()

    assert result["available"] is True
    assert 0 <= result["rise_probability"] <= 1
    assert 0 <= result["confidence"] <= 0.95
    assert result["neighbor_count"] > 0
    assert result["previous_close"] > 0


def test_forecast_does_not_use_rows_after_prediction_date():
    data = make_market_data(260)
    prefix = data.iloc[:210]
    cutoff = data.index[210].date()

    from_prefix = PremarketAnalyzer().forecast("sh600000", prefix).to_dict()
    no_future = PremarketAnalyzer.before_trade_date(data, cutoff)
    from_same_slice = PremarketAnalyzer().forecast("sh600000", no_future).to_dict()

    assert from_prefix == from_same_slice


def test_target_trading_date_skips_weekends_and_configured_holidays():
    assert target_trading_date(datetime(2026, 7, 10, 16, 0)).isoformat() == "2026-07-13"
    assert target_trading_date(datetime(2026, 7, 13, 8, 20)).isoformat() == "2026-07-13"
    assert target_trading_date(
        datetime(2026, 7, 10, 16, 0), holidays=["2026-07-13"]
    ).isoformat() == "2026-07-14"


def test_exit_plan_respects_t_plus_one_lock():
    analyzer = PremarketAnalyzer()
    data = make_market_data(falling=True)
    price = float(data["Close"].iloc[-1])

    locked = analyzer.position_exit(
        "sh600000", data, quantity=100, available_quantity=0,
        avg_cost=price * 1.20, current_price=price, opportunity_score=30,
    )
    released = analyzer.position_exit(
        "sh600000", data, quantity=100, available_quantity=100,
        avg_cost=price * 1.20, current_price=price, opportunity_score=30,
    )

    assert locked["action"] == "wait_t1"
    assert locked["pending_action"] == "sell"
    assert locked["suggested_quantity"] == 0
    assert released["action"] == "sell"
    assert released["suggested_quantity"] == 100
