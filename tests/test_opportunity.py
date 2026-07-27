import numpy as np
import pandas as pd

from src.analysis.opportunity import OpportunityConfig, OpportunityScorer
from src.strategies.potential_strategy import PotentialStrategy


def make_recovery_data():
    first = np.linspace(18, 20, 40)
    decline = np.linspace(20, 12, 60)
    recovery = np.linspace(12, 16, 60)
    close = np.concatenate([first, decline, recovery])
    wave = np.sin(np.arange(len(close)) / 3) * 0.08
    close = close + wave
    volume = np.full(len(close), 1_000_000.0)
    volume[-20:] = np.linspace(1_050_000, 1_500_000, 20)
    index = pd.date_range("2025-01-01", periods=len(close), freq="B")
    return pd.DataFrame({
        "Open": close * 0.998,
        "High": close * 1.015,
        "Low": close * 0.985,
        "Close": close,
        "Volume": volume,
        "Amount": close * volume,
    }, index=index)


def make_falling_data():
    close = np.linspace(25, 10, 160) + np.sin(np.arange(160)) * 0.05
    index = pd.date_range("2025-01-01", periods=len(close), freq="B")
    return pd.DataFrame({
        "Open": close * 1.002,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": np.full(len(close), 800_000.0),
    }, index=index)


def test_opportunity_recommends_capital_constrained_plan():
    scorer = OpportunityScorer(OpportunityConfig(
        risk_per_trade=0.01,
        max_position_pct=0.12,
    ))
    data = make_recovery_data()
    price = float(data["Close"].iloc[-1])
    result = scorer.analyze(
        "sh600000",
        data,
        equity=100_000,
        cash=80_000,
        quote={"price": price, "volume": 1_500_000, "amount": 500_000_000},
    )

    assert result.action == "buy"
    assert result.score >= 68
    assert result.suggested_qty > 0
    assert result.suggested_qty % 100 == 0
    assert result.suggested_amount <= 12_000
    assert result.stop_loss < result.price < result.target_price
    assert result.risk_reward >= 1.5
    assert result.expected_profit > 0
    assert result.max_loss <= 1_100


def test_opportunity_rejects_persistent_downtrend():
    result = OpportunityScorer().analyze(
        "sh600000",
        make_falling_data(),
        equity=100_000,
        cash=100_000,
    )

    assert result.action == "hold"
    assert result.suggested_qty == 0
    assert result.score < 68


def test_potential_strategy_generates_score_and_trade_signal():
    frame = PotentialStrategy().generate_signals(make_recovery_data())

    assert "opportunity_score" in frame.columns
    assert "position_fraction" in frame.columns
    assert frame["opportunity_score"].max() >= 68
    assert (frame["signal"] == 1).any()
    entry = frame[frame["signal"] == 1].iloc[0]
    assert entry["entry_confirmed"]
    assert 0 < entry["position_fraction"] <= 0.12


def test_historical_validation_has_stable_schema():
    result = OpportunityScorer().validate_history(
        "sh600000", make_recovery_data(), horizon=5, step=5
    )

    assert set(result) >= {"samples", "win_rate", "avg_return", "max_drawdown"}
    assert 0 <= result["win_rate"] <= 100
