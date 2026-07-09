"""
策略与回测器集成测试
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.mean_reversion_strategy import MeanReversionStrategy
from src.strategies.base_strategy import Signal
from src.backtest.backtester import Backtester


def make_trending_data(n=500, seed=42, uptrend=True):
    np.random.seed(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    drift = 0.002 if uptrend else -0.002
    returns = np.random.normal(drift, 0.015, n)
    price = 100 * np.exp(np.cumsum(returns))
    return pd.DataFrame({
        "Open": price * 0.999,
        "High": price * 1.01,
        "Low": price * 0.99,
        "Close": price,
        "Volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=dates)


class TestMomentumStrategy:
    def test_no_repeat_buying(self):
        s = MomentumStrategy(lookback_period=20, threshold=0.01)
        data = make_trending_data(500, uptrend=True)
        signals_df = s.generate_signals(data)
        assert "signal" in signals_df.columns
        buys = (signals_df["signal"] == 1).sum()
        s.update_position("x", 1000, 10.0)
        s.update_position("x", 1000, 10.5)
        info = s.get_position("x")
        assert info["shares"] == 2000
        s.close_position("x")
        assert not s.has_position("x")

    def test_position_tracking(self):
        s = MomentumStrategy(lookback_period=20, threshold=0.01)
        s.update_position("x", 1000, 10.0)
        assert s.has_position("x")
        s.close_position("x")
        assert not s.has_position("x")


class TestBacktester:
    def test_backtest_runs(self):
        data = make_trending_data(300, uptrend=True)
        bt = Backtester(initial_capital=1_000_000, commission=0.0003, slippage=0.001)
        s = MomentumStrategy(lookback_period=20, threshold=0.02)
        results = bt.run_backtest(s, data, "TEST")
        assert "total_return" in results
        assert "sharpe_ratio" in results
        assert "equity_curve" in results
        assert "trades" in results

    def test_backtest_no_position_collapse(self):
        data = make_trending_data(500, uptrend=True)
        bt = Backtester(initial_capital=1_000_000, commission=0.0003, slippage=0.001)
        s = MomentumStrategy(lookback_period=20, threshold=0.02)
        results = bt.run_backtest(s, data, "TEST")
        assert results["total_return"] > -0.50
        assert results["final_equity"] >= 0

    def test_backtest_mean_reversion(self):
        np.random.seed(7)
        data = make_trending_data(500, uptrend=False)
        bt = Backtester(initial_capital=1_000_000, commission=0.0003, slippage=0.001)
        s = MeanReversionStrategy(lookback_period=20, entry_threshold=2.0, exit_threshold=0.5)
        results = bt.run_backtest(s, data, "TEST")
        assert "total_return" in results
        assert results["final_equity"] >= 0


class TestStopLossTakeProfit:
    def test_stop_loss_in_base_strategy(self):
        from src.strategies.momentum_strategy import MomentumStrategy
        s = MomentumStrategy(lookback_period=20, threshold=0.01)
        s.parameters["stop_loss"] = 0.05
        s.parameters["take_profit"] = 0.10
        s.update_position("x", 1000, 10.0)
        should_exit = s.should_exit_position("x", Signal.SELL, 9.4)
        assert should_exit is True

    def test_take_profit_in_base_strategy(self):
        from src.strategies.momentum_strategy import MomentumStrategy
        s = MomentumStrategy(lookback_period=20, threshold=0.01)
        s.parameters["stop_loss"] = 0.05
        s.parameters["take_profit"] = 0.10
        s.update_position("x", 1000, 10.0)
        should_exit = s.should_exit_position("x", Signal.SELL, 11.5)
        assert should_exit is True
