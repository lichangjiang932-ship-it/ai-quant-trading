"""
投资组合分析模块测试
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.portfolio import (
    TradeRecord, PerformanceMetrics, TradeStatistics,
    compute_metrics_from_returns, build_equity_curve_from_trades,
    compute_trade_statistics, monthly_returns_table,
    by_symbol_breakdown, by_strategy_breakdown,
    generate_report, format_text_report,
)


def make_trades(n=20, seed=42):
    np.random.seed(seed)
    base = datetime(2024, 1, 2, 9, 30)
    pool = ["sh600000", "sz000001", "sh600519", "sz300750"]
    out = []
    for i in range(n):
        ts = base + timedelta(days=i // 2, hours=i % 2)
        sym = pool[i % len(pool)]
        price = 10.0 + np.random.randn() * 0.3
        qty = 1000
        commission = max(5, qty * price * 0.0003)
        if i % 3 == 0:
            stamp = 0
            pnl = 0
            direction = "buy"
        else:
            stamp = qty * price * 0.001
            pnl = (price - 10.0) * qty
            direction = "sell"
        out.append(TradeRecord(
            symbol=sym, direction=direction, quantity=qty, price=round(price, 2),
            timestamp=ts, amount=round(qty * price, 2),
            commission=round(commission, 2), stamp_tax=round(stamp, 2),
            pnl=round(pnl, 2), strategy="TestStrat",
        ))
    return out


class TestMetrics:
    def test_empty_returns(self):
        m = compute_metrics_from_returns([])
        assert m.n_periods == 0
        assert m.sharpe_ratio == 0.0
        assert m.max_drawdown == 0.0

    def test_positive_returns(self):
        np.random.seed(0)
        r = np.random.normal(0.001, 0.01, 252).tolist()
        m = compute_metrics_from_returns(r)
        assert m.n_periods == 252
        assert m.sharpe_ratio > 0
        assert 0 < m.volatility < 1.0
        assert 0 <= m.max_drawdown < 1.0

    def test_negative_returns(self):
        r = [-0.01] * 100
        m = compute_metrics_from_returns(r)
        assert m.total_return < 0
        assert m.sharpe_ratio < 0

    def test_var_and_cvar(self):
        np.random.seed(1)
        r = np.random.normal(0, 0.02, 1000).tolist()
        m = compute_metrics_from_returns(r)
        assert m.var_95 < 0
        assert m.cvar_95 <= m.var_95

    def test_drawdown_duration(self):
        r = [0.0, 0.0, -0.1, -0.1, 0.05, 0.05, 0.05]
        m = compute_metrics_from_returns(r)
        assert m.max_drawdown > 0
        assert m.max_drawdown_duration_days >= 0

    def test_skewness_kurtosis(self):
        np.random.seed(2)
        r = np.random.normal(0, 0.01, 500).tolist()
        m = compute_metrics_from_returns(r)
        assert -1 < m.skewness < 1
        assert -1 < m.kurtosis < 5

    def test_with_equity_values(self):
        eq = [100, 110, 121, 100, 90, 95]
        m = compute_metrics_from_returns([], equity_values=eq)
        assert m.max_drawdown > 0


class TestEquityCurve:
    def test_build_from_empty(self):
        eq = build_equity_curve_from_trades([])
        assert eq.empty

    def test_build_from_trades(self):
        trades = make_trades(20)
        eq = build_equity_curve_from_trades(trades, initial_capital=1_000_000)
        assert not eq.empty
        assert len(eq) == len(trades)
        assert (eq["cash"] + eq["position_value"]).iloc[-1] == eq["equity"].iloc[-1]

    def test_buy_increases_position_value(self):
        trades = [
            TradeRecord(symbol="x", direction="buy", quantity=1000, price=10.0,
                        timestamp=datetime(2024, 1, 2, 10, 0),
                        amount=10000, commission=5, stamp_tax=0, pnl=0),
        ]
        eq = build_equity_curve_from_trades(trades, initial_capital=100_000)
        assert eq["position_value"].iloc[0] == 10000


class TestTradeStatistics:
    def test_empty(self):
        s = compute_trade_statistics([])
        assert s.total_trades == 0
        assert s.win_rate == 0.0

    def test_basic_stats(self):
        trades = make_trades(20)
        s = compute_trade_statistics(trades)
        assert s.total_trades == 20
        assert s.buy_trades + s.sell_trades == 20
        assert s.symbols_traded == 4

    def test_win_loss_counting(self):
        trades = [
            TradeRecord("x", "sell", 100, 12.0, datetime(2024, 1, 2), 1200, 5, 1.2, pnl=200, strategy="s"),
            TradeRecord("x", "sell", 100, 9.0, datetime(2024, 1, 3), 900, 5, 0.9, pnl=-100, strategy="s"),
        ]
        s = compute_trade_statistics(trades)
        assert s.winning_trades == 1
        assert s.losing_trades == 1
        assert s.win_rate == 0.5

    def test_consecutive_wins_losses(self):
        trades = []
        ts = datetime(2024, 1, 1)
        pnls = [100, 200, 300, -50, -100, -150, 50]
        for p in pnls:
            trades.append(TradeRecord("x", "sell", 100, 10, ts, 1000, 5, 1, pnl=p, strategy="s"))
        s = compute_trade_statistics(trades)
        assert s.max_consecutive_wins == 3
        assert s.max_consecutive_losses == 3

    def test_best_worst_symbol(self):
        trades = [
            TradeRecord("A", "sell", 100, 11, datetime(2024, 1, 2), 1100, 5, 1.1, pnl=100, strategy="s"),
            TradeRecord("B", "sell", 100, 9, datetime(2024, 1, 3), 900, 5, 0.9, pnl=-100, strategy="s"),
        ]
        s = compute_trade_statistics(trades)
        assert s.best_symbol == "A"
        assert s.worst_symbol == "B"


class TestBreakdowns:
    def test_by_symbol(self):
        trades = make_trades(20)
        df = by_symbol_breakdown(trades)
        assert not df.empty
        assert "net_pnl" in df.columns
        assert set(df["symbol"]) == {"sh600000", "sz000001", "sh600519", "sz300750"}

    def test_by_strategy(self):
        trades = make_trades(20)
        df = by_strategy_breakdown(trades)
        assert not df.empty
        assert "TestStrat" in df["strategy"].values

    def test_monthly_returns(self):
        dates = pd.date_range("2024-01-31", periods=12, freq="ME")
        eq = pd.DataFrame({
            "equity": 1_000_000 * np.cumprod(1 + np.random.default_rng(0).normal(0.01, 0.02, 12))
        }, index=dates)
        tbl = monthly_returns_table(eq)
        assert not tbl.empty


class TestReport:
    def test_generate_report(self):
        trades = make_trades(30)
        rpt = generate_report(trades, initial_capital=1_000_000)
        assert "metrics" in rpt
        assert "trade_statistics" in rpt
        assert "monthly_returns" in rpt
        assert "by_symbol" in rpt
        assert "by_strategy" in rpt
        assert "equity_curve" in rpt
        assert rpt["metrics"]["n_periods"] > 0

    def test_text_report(self):
        trades = make_trades(10)
        rpt = generate_report(trades, initial_capital=1_000_000)
        txt = format_text_report(rpt, initial_capital=1_000_000)
        assert "投资组合业绩报告" in txt
        assert "夏普比率" in txt
        assert "最大回撤" in txt
        assert "胜率" in txt
