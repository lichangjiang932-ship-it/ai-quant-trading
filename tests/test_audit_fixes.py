import pandas as pd

from src.backtest.backtester import Backtester
from src.execution.risk_manager import OrderRequest, OrderSide, RiskManager
from src.strategies.base_strategy import BaseStrategy, Signal


class FixedSignalStrategy(BaseStrategy):
    def __init__(self, signals=None):
        super().__init__("FixedSignalStrategy")
        self._signals = signals

    def generate_signals(self, data):
        frame = data.copy()
        frame["signal"] = self._signals or [
            Signal.BUY.value, Signal.HOLD.value,
            Signal.SELL.value, Signal.HOLD.value,
        ]
        return frame

    def calculate_position_size(self, signal, current_price, portfolio_value):
        return 100 if signal == Signal.BUY else 0


def test_existing_symbol_position_counts_toward_limit():
    risk = RiskManager(max_position_size=0.10)
    risk.check_drawdown(1_000_000)
    result = risk.check_order(OrderRequest(
        symbol="sh600000",
        side=OrderSide.BUY,
        quantity=2_000,
        price=10,
        portfolio_value=1_000_000,
        current_position_value=90_000,
        current_symbol_value=90_000,
    ))

    assert result.allowed is False
    assert result.suggested_quantity == 1_000
    assert any("单股仓位" in item for item in result.violations)


def test_suggested_quantity_respects_total_exposure_room():
    risk = RiskManager(max_position_size=0.50, max_total_position=0.95)
    risk.check_drawdown(1_000_000)
    result = risk.check_order(OrderRequest(
        symbol="sh600000",
        side=OrderSide.BUY,
        quantity=2_000,
        price=10,
        portfolio_value=1_000_000,
        current_position_value=940_000,
        current_symbol_value=0,
    ))

    assert result.allowed is False
    assert result.suggested_quantity == 1_000
    assert any("总仓位" in item for item in result.violations)


def test_backtest_executes_signal_on_next_bar_open_and_includes_entry_fee():
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    data = pd.DataFrame({
        "Open": [10.0, 20.0, 30.0, 40.0],
        "High": [11.0, 21.0, 31.0, 41.0],
        "Low": [9.0, 19.0, 29.0, 39.0],
        "Close": [10.5, 20.5, 30.5, 40.5],
        "Volume": [1000, 1000, 1000, 1000],
    }, index=dates)

    backtester = Backtester(
        initial_capital=100_000,
        commission=0,
        min_commission=5,
        slippage=0,
        stamp_tax=0,
    )
    result = backtester.run_backtest(FixedSignalStrategy(), data, "TEST")

    buy, sell = result["trades"]
    assert buy["date"] == dates[1]
    assert buy["price"] == 20.0
    assert sell["date"] == dates[3]
    assert sell["price"] == 40.0
    assert sell["pnl"] == 1990.0


def _run_tradeability_backtest(data, signals):
    backtester = Backtester(
        initial_capital=100_000,
        commission=0,
        min_commission=0,
        slippage=0,
        stamp_tax=0,
    )
    return backtester.run_backtest(
        FixedSignalStrategy(signals),
        data,
        "sh600000",
    )


def test_backtest_rejects_limit_up_entry():
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame({
        "Open": [10.0, 11.0, 10.8],
        "High": [10.2, 11.0, 11.0],
        "Low": [9.8, 11.0, 10.6],
        "Close": [10.0, 11.0, 10.9],
        "Volume": [1000, 1000, 1000],
    }, index=dates)

    result = _run_tradeability_backtest(
        data,
        [Signal.BUY.value, Signal.HOLD.value, Signal.HOLD.value],
    )

    assert result["trades"] == []
    assert result["rejected_order_count"] == 1
    assert "涨停" in result["rejected_orders"][0]["reason"]


def test_backtest_rejects_limit_down_exit():
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    data = pd.DataFrame({
        "Open": [10.0, 10.1, 9.0, 9.2],
        "High": [10.2, 10.2, 9.0, 9.4],
        "Low": [9.8, 9.9, 9.0, 9.1],
        "Close": [10.0, 10.0, 9.0, 9.3],
        "Volume": [1000, 1000, 1000, 1000],
    }, index=dates)

    result = _run_tradeability_backtest(
        data,
        [Signal.BUY.value, Signal.SELL.value, Signal.HOLD.value, Signal.HOLD.value],
    )

    assert [trade["action"] for trade in result["trades"]] == ["BUY"]
    assert result["rejected_order_count"] == 1
    assert result["rejected_orders"][0]["side"] == "sell"
    assert "跌停" in result["rejected_orders"][0]["reason"]


def test_backtest_rejects_suspended_entry():
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame({
        "Open": [10.0, 10.0, 10.1],
        "High": [10.2, 10.0, 10.2],
        "Low": [9.8, 10.0, 10.0],
        "Close": [10.0, 10.0, 10.1],
        "Volume": [1000, 0, 1000],
    }, index=dates)

    result = _run_tradeability_backtest(
        data,
        [Signal.BUY.value, Signal.HOLD.value, Signal.HOLD.value],
    )

    assert result["trades"] == []
    assert result["rejected_orders"][0]["reason"] == "停牌或零成交量"


def test_backtest_allows_normal_a_share_open():
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame({
        "Open": [10.0, 10.2, 10.3],
        "High": [10.2, 10.4, 10.5],
        "Low": [9.8, 10.1, 10.2],
        "Close": [10.0, 10.3, 10.4],
        "Volume": [1000, 1000, 1000],
    }, index=dates)

    result = _run_tradeability_backtest(
        data,
        [Signal.BUY.value, Signal.HOLD.value, Signal.HOLD.value],
    )

    assert result["rejected_order_count"] == 0
    assert result["trades"][0]["action"] == "BUY"
    assert result["trades"][0]["price"] == 10.2
