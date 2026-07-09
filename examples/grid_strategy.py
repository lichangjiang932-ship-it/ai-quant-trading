"""
网格交易策略示例
================

原理:
  在价格区间 [lower, upper] 内布置 N 条等距网格线
  价格每跌一格 -> 买入 1 单位
  价格每涨一格 -> 卖出 1 单位
  突破上下轨 -> 强制平仓或不开仓

特点:
  - 震荡市表现极佳
  - 趋势市可能反复止损
  - 需要严格设置上下轨
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from src.strategies.base_strategy import BaseStrategy, Signal
from src.backtest.backtester import Backtester


class GridStrategy(BaseStrategy):
    """
    网格交易策略

    Args:
        lower: 网格下界
        upper: 网格上界
        grid_count: 网格数量
        position_per_grid: 每格交易股数
        stop_loss_pct: 整体止损 (相对均价)
    """

    def __init__(
        self,
        lower: float = 9.0,
        upper: float = 11.0,
        grid_count: int = 10,
        position_per_grid: int = 100,
        stop_loss_pct: float = 0.10,
        parameters: dict = None,
    ):
        super().__init__("GridStrategy", parameters or {})
        self.lower = lower
        self.upper = upper
        self.grid_count = grid_count
        self.position_per_grid = position_per_grid
        self.stop_loss_pct = stop_loss_pct
        self.grid_size = (upper - lower) / grid_count
        self.grid_lines = [lower + i * self.grid_size for i in range(grid_count + 1)]
        self._last_grid_idx: dict = {}

    def _grid_index(self, price: float) -> int:
        if price <= self.lower:
            return 0
        if price >= self.upper:
            return self.grid_count
        return int((price - self.lower) / self.grid_size)

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        df["signal"] = 0
        df["raw_signal"] = 0
        sym = "TEST"
        for i in range(len(df)):
            price = df["Close"].iloc[i]
            idx = self._grid_index(price)
            prev_idx = self._last_grid_idx.get(sym, idx)
            if idx > prev_idx:
                df.iat[i, df.columns.get_loc("raw_signal")] = -1
            elif idx < prev_idx:
                df.iat[i, df.columns.get_loc("raw_signal")] = 1
            self._last_grid_idx[sym] = idx
        df["signal"] = df["raw_signal"].shift(1).fillna(0).astype(int)
        return df

    def calculate_position_size(
        self, signal: Signal, current_price: float, portfolio_value: float
    ) -> float:
        if signal == Signal.BUY:
            return self.position_per_grid
        return 0

    def should_enter_position(
        self, symbol: str, signal: Signal, current_price: float, portfolio_value: float
    ) -> bool:
        if signal != Signal.BUY:
            return False
        if self.has_position(symbol):
            return False
        return self.calculate_position_size(signal, current_price, portfolio_value) > 0

    def should_exit_position(
        self, symbol: str, signal: Signal, current_price: float
    ) -> bool:
        if not self.has_position(symbol):
            return False
        pos = self.positions[symbol]
        entry = pos.get("entry_price", current_price)
        pnl_pct = (current_price - entry) / entry
        if pnl_pct <= -self.stop_loss_pct:
            return True
        if signal == Signal.SELL:
            return True
        if current_price >= self.upper:
            return True
        return False


def main():
    print("=" * 60)
    print("网格交易策略回测")
    print("=" * 60)

    np.random.seed(42)
    n = 500
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    returns = np.random.normal(0.0, 0.012, n)
    price = pd.Series(10.0 * np.exp(np.cumsum(returns)))

    data = pd.DataFrame({
        "Open": price * 0.999,
        "High": price * 1.01,
        "Low": price * 0.99,
        "Close": price.values,
        "Volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=dates)

    print(f"价格区间: {data['Close'].min():.2f} - {data['Close'].max():.2f}")

    strategy = GridStrategy(
        lower=round(data["Close"].min() * 1.02, 2),
        upper=round(data["Close"].max() * 0.98, 2),
        grid_count=10,
        position_per_grid=100,
        stop_loss_pct=0.10,
    )

    backtester = Backtester(
        initial_capital=1_000_000,
        commission=0.0003,
        slippage=0.001,
        stamp_tax=0.001,
    )
    results = backtester.run_backtest(strategy, data, "TEST")

    if not results:
        print("回测未产生结果")
        return

    print(f"\n回测结果:")
    print(f"  初始资金: ¥{results['initial_capital']:,.0f}")
    print(f"  最终权益: ¥{results['final_equity']:,.0f}")
    print(f"  总收益:   {results['total_return']:.2%}")
    print(f"  年化收益: {results['annualized_return']:.2%}")
    print(f"  最大回撤: {results['max_drawdown']:.2%}")
    print(f"  夏普:     {results['sharpe_ratio']:.3f}")
    print(f"  胜率:     {results.get('win_rate', 0):.2%}")
    print(f"  交易数:   {results.get('total_trades', 0)}")


if __name__ == "__main__":
    main()
