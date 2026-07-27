"""基于价格位置、趋势、动量、量能和风险的潜力评分策略。"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy, Signal


class PotentialStrategy(BaseStrategy):
    def __init__(
        self,
        buy_threshold: float = 68,
        sell_threshold: float = 48,
        max_position_pct: float = 0.12,
        target_volatility: float = 0.28,
        min_average_amount: float = 10_000_000,
        parameters: Optional[Dict] = None,
    ):
        super().__init__("PotentialStrategy", parameters)
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.max_position_pct = min(max(float(max_position_pct), 0.01), 0.30)
        self.target_volatility = max(float(target_volatility), 0.05)
        self.min_average_amount = max(float(min_average_amount), 0)

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        frame = data.copy()
        close = frame["Close"]
        volume = frame["Volume"]
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        low120 = frame["Low"].rolling(120, min_periods=60).min()
        high120 = frame["High"].rolling(120, min_periods=60).max()
        percentile = (close - low120) / (high120 - low120).replace(0, np.nan)
        momentum20 = close.pct_change(20)
        momentum60 = close.pct_change(60)
        daily_return = close.pct_change()
        volume_ratio = volume / volume.rolling(20).mean().replace(0, np.nan)
        amount = frame["Amount"] if "Amount" in frame.columns else close * volume
        average_amount = amount.rolling(20).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        volatility = daily_return.rolling(20).std(ddof=0) * np.sqrt(252)

        trend = (close > ma20).astype(float) * 8
        trend += (ma20 > ma60).astype(float) * 10
        trend += (ma20 > ma20.shift(5)).astype(float) * 7
        location = pd.Series(4.0, index=frame.index)
        location[(percentile >= .20) & (percentile <= .60)] = 20
        location[(percentile >= .08) & (percentile < .20)] = 14
        location[(percentile > .60) & (percentile <= .78)] = 11
        momentum = pd.Series(3.0, index=frame.index)
        momentum[(momentum20 >= .02) & (momentum20 <= .15) & (momentum60 > -.05)] = 20
        momentum[(momentum20 >= -.03) & (momentum20 < .02) & (momentum60 > 0)] = 13
        momentum[(momentum20 > .15) & (momentum20 <= .25)] = 9
        volume_score = pd.Series(2.0, index=frame.index)
        volume_score[(volume_ratio >= 1) & (volume_ratio <= 2.5)] = 10
        volume_score[((volume_ratio >= .7) & (volume_ratio < 1)) | ((volume_ratio > 2.5) & (volume_ratio <= 4))] = 6
        rsi_score = pd.Series(1.0, index=frame.index)
        rsi_score[(rsi >= 45) & (rsi <= 65)] = 10
        rsi_score[((rsi >= 35) & (rsi < 45)) | ((rsi > 65) & (rsi <= 72))] = 6
        rsi_score[rsi < 30] = 4
        risk = pd.Series(4.0, index=frame.index)
        risk[volatility <= .45] = 10
        risk[volatility <= .30] = 15

        frame["opportunity_score"] = (
            trend + location + momentum + volume_score + rsi_score + risk
        ).fillna(0)
        valid_bar = (
            (volume > 0)
            & (frame["High"] >= frame[["Open", "Close"]].max(axis=1))
            & (frame["Low"] <= frame[["Open", "Close"]].min(axis=1))
        )
        eligible = (
            (frame["opportunity_score"] >= self.buy_threshold)
            & (close > ma20)
            & (momentum20 <= .25)
            & daily_return.between(-0.08, 0.095)
            & volume_ratio.between(0.5, 4.0)
            & (average_amount >= self.min_average_amount)
            & valid_bar
        )
        confirmed_entry = eligible & eligible.shift(1, fill_value=False)
        trend_break = (close < ma20) & (close.shift(1) < ma20.shift(1))
        exit_signal = (
            (frame["opportunity_score"] < self.sell_threshold)
            | trend_break
            | (daily_return <= -0.08)
        )
        score_strength = (
            0.5
            + (frame["opportunity_score"] - self.buy_threshold)
            / max(100 - self.buy_threshold, 1)
            * 0.5
        ).clip(0.5, 1.0)
        volatility_scale = (self.target_volatility / volatility.replace(0, np.nan)).clip(0.35, 1.0)
        frame["position_fraction"] = (
            self.max_position_pct * score_strength * volatility_scale
        ).clip(0.02, self.max_position_pct).where(confirmed_entry, 0).fillna(0)
        frame["entry_confirmed"] = confirmed_entry
        frame["average_amount_20"] = average_amount
        frame["signal"] = Signal.HOLD.value
        frame.loc[
            confirmed_entry & ~confirmed_entry.shift(1, fill_value=False),
            "signal",
        ] = Signal.BUY.value
        frame.loc[exit_signal & ~exit_signal.shift(1, fill_value=False), "signal"] = Signal.SELL.value
        return frame

    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
        portfolio_value: float,
    ) -> int:
        if signal != Signal.BUY or current_price <= 0:
            return 0
        return max(
            int(portfolio_value * self.max_position_pct / current_price / 100) * 100,
            0,
        )
