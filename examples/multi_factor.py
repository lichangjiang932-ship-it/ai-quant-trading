"""
多因子综合策略示例
==================

演示如何组合多个因子 (动量 + 均值回归 + 波动率 + 成交量)
为每只股票计算综合得分,得分高的买入,得分低的卖出。

因子说明:
  - 动量因子: 过去 N 日收益率 (越大越强)
  - 均值回归: 价格相对布林带下轨的距离 (越低越超卖)
  - 波动率: 过去 N 日收益率标准差 (越小越稳定)
  - 量比: 今日成交量 / 5 日均量 (越大资金越关注)
  - 趋势强度: ADX 或价格趋势斜率

每个因子标准化为 Z-score,然后加权求和得综合分。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from src.strategies.base_strategy import BaseStrategy, Signal
from src.backtest.backtester import Backtester


class MultiFactorStrategy(BaseStrategy):
    """
    多因子综合策略

    用法:
        strategy = MultiFactorStrategy(
            weights={'momentum': 0.35, 'mean_reversion': 0.25,
                     'volatility': 0.20, 'volume': 0.20},
            entry_threshold=0.5,
            exit_threshold=-0.3,
        )
        results = Backtester(...).run_backtest(strategy, data, symbol)
    """

    def __init__(
        self,
        momentum_window: int = 20,
        bb_window: int = 20,
        bb_std: float = 2.0,
        vol_window: int = 20,
        volume_ma: int = 5,
        weights: dict = None,
        entry_threshold: float = 0.5,
        exit_threshold: float = -0.3,
        parameters: dict = None,
    ):
        super().__init__("MultiFactor", parameters or {})
        self.momentum_window = momentum_window
        self.bb_window = bb_window
        self.bb_std = bb_std
        self.vol_window = vol_window
        self.volume_ma = volume_ma
        self.weights = weights or {
            "momentum": 0.35,
            "mean_reversion": 0.25,
            "volatility": 0.20,
            "volume": 0.20,
        }
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold

    def _compute_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["Close"]
        df["momentum"] = close.pct_change(self.momentum_window)
        sma = close.rolling(self.bb_window).mean()
        std = close.rolling(self.bb_window).std()
        lower = sma - self.bb_std * std
        df["bb_position"] = (close - lower) / (2 * self.bb_std * std)
        df["volatility"] = close.pct_change().rolling(self.vol_window).std()
        vol_ma = df["Volume"].rolling(self.volume_ma).mean()
        df["volume_ratio"] = df["Volume"] / vol_ma
        return df

    def _normalize(self, series: pd.Series) -> pd.Series:
        s = series.copy()
        std = s.std()
        if std == 0 or pd.isna(std):
            return pd.Series(np.zeros(len(s)), index=s.index)
        return (s - s.mean()) / std

    def _composite_score(self, df: pd.DataFrame) -> pd.Series:
        mom_norm = self._normalize(df["momentum"])
        mr_norm = self._normalize(-df["bb_position"])
        vol_norm = self._normalize(-df["volatility"])
        volr_norm = self._normalize(df["volume_ratio"])
        score = (
            self.weights["momentum"] * mom_norm
            + self.weights["mean_reversion"] * mr_norm
            + self.weights["volatility"] * vol_norm
            + self.weights["volume"] * volr_norm
        )
        return score

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        df = self._compute_factors(data)
        df["score"] = self._composite_score(df)
        df["signal"] = np.where(
            df["score"] > self.entry_threshold, 1,
            np.where(df["score"] < self.exit_threshold, -1, 0),
        )
        df["raw_signal"] = df["signal"].shift(1).fillna(0).astype(int)
        df["signal"] = df["raw_signal"]
        return df

    def calculate_position_size(
        self, signal: Signal, current_price: float, portfolio_value: float
    ) -> float:
        if signal != Signal.BUY or current_price <= 0:
            return 0
        target_value = portfolio_value * 0.10
        shares = int(target_value / current_price / 100) * 100
        return max(shares, 0)


def main():
    print("=" * 60)
    print("多因子综合策略回测")
    print("=" * 60)

    np.random.seed(42)
    n = 500
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    returns = np.random.normal(0.0008, 0.015, n)
    price = 100 * np.exp(np.cumsum(returns))
    volume = np.random.randint(1_000_000, 10_000_000, n)
    data = pd.DataFrame({
        "Open": price * 0.999,
        "High": price * 1.01,
        "Low": price * 0.99,
        "Close": price,
        "Volume": volume,
    }, index=dates)

    strategy = MultiFactorStrategy(
        momentum_window=20,
        bb_window=20,
        vol_window=20,
        weights={"momentum": 0.35, "mean_reversion": 0.25,
                 "volatility": 0.20, "volume": 0.20},
        entry_threshold=0.5,
        exit_threshold=-0.3,
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
    print(f"  索提诺:   {results.get('sortino_ratio', 0):.3f}")
    print(f"  卡玛:     {results.get('calmar_ratio', 0):.3f}")
    print(f"  胜率:     {results.get('win_rate', 0):.2%}")
    print(f"  交易数:   {results.get('total_trades', 0)}")


if __name__ == "__main__":
    main()
