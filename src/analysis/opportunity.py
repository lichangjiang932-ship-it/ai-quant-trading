"""A 股机会评分、资金规划与历史相似机会验证。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.execution.a_share_rules import instrument_type


@dataclass(frozen=True)
class OpportunityConfig:
    min_history: int = 80
    lookback: int = 120
    risk_per_trade: float = 0.0075
    max_position_pct: float = 0.12
    buy_score: float = 68.0
    watch_score: float = 55.0
    min_risk_reward: float = 1.5
    min_average_amount: float = 10_000_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005


@dataclass
class OpportunityResult:
    symbol: str
    action: str
    score: float
    confidence: float
    price: float
    buy_low: float
    buy_high: float
    stop_loss: float
    target_price: float
    upside_pct: float
    risk_reward: float
    suggested_qty: int
    suggested_amount: float
    expected_profit: float
    max_loss: float
    price_percentile: float
    rsi: float
    volatility: float
    volume_ratio: float
    average_amount: float
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    factors: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float):
                payload[key] = round(value, 4)
        return payload


class OpportunityScorer:
    def __init__(self, config: Optional[OpportunityConfig] = None):
        self.config = config or OpportunityConfig()

    @staticmethod
    def normalize_frame(data: pd.DataFrame) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()
        frame = data.copy()
        rename = {column: str(column).title() for column in frame.columns}
        frame.rename(columns=rename, inplace=True)
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(column not in frame.columns for column in required):
            return pd.DataFrame()
        for column in required + (["Amount"] if "Amount" in frame.columns else []):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.replace([np.inf, -np.inf], np.nan, inplace=True)
        frame.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
        return frame[frame["Close"] > 0]

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        relative = gain / loss.replace(0, np.nan)
        return (100 - 100 / (1 + relative)).fillna(50)

    @staticmethod
    def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
        previous_close = frame["Close"].shift(1)
        true_range = pd.concat([
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        return true_range.rolling(period).mean()

    def analyze(
        self,
        symbol: str,
        data: pd.DataFrame,
        equity: float,
        cash: float,
        current_symbol_value: float = 0.0,
        quote: Optional[Dict] = None,
    ) -> OpportunityResult:
        frame = self.normalize_frame(data)
        if len(frame) < self.config.min_history:
            return self._empty(symbol, quote, f"历史数据不足 {self.config.min_history} 个交易日")

        window = frame.tail(self.config.lookback).copy()
        close = window["Close"]
        current = float((quote or {}).get("price") or close.iloc[-1])
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        rsi = float(self._rsi(close).iloc[-1])
        atr = float(self._atr(window).iloc[-1] or current * 0.025)
        if not np.isfinite(atr) or atr <= 0:
            atr = current * 0.025
        ret20 = current / float(close.iloc[-21]) - 1 if len(close) > 20 else 0
        ret60 = current / float(close.iloc[-61]) - 1 if len(close) > 60 else ret20
        low120 = float(window["Low"].min())
        high120 = float(window["High"].max())
        price_percentile = (current - low120) / max(high120 - low120, current * 0.01)
        daily_return = close.pct_change()
        volatility = float(daily_return.tail(20).std(ddof=0) * np.sqrt(252))
        avg_volume = float(window["Volume"].tail(20).mean() or 0)
        amount_series = (
            window["Amount"]
            if "Amount" in window.columns
            else window["Close"] * window["Volume"]
        )
        average_amount = float(amount_series.tail(20).mean() or 0)
        latest_volume = float((quote or {}).get("volume") or window["Volume"].iloc[-1] or 0)
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0
        support = float(window["Low"].tail(20).min())
        resistance = float(window["High"].tail(60).max())

        trend_score = 0.0
        trend_score += 8 if current > ma20.iloc[-1] else 1
        trend_score += 10 if ma20.iloc[-1] > ma60.iloc[-1] else 0
        trend_score += 7 if ma20.iloc[-1] > ma20.iloc[-6] else 0

        if 0.20 <= price_percentile <= 0.60:
            location_score = 20.0
        elif 0.08 <= price_percentile < 0.20:
            location_score = 14.0
        elif 0.60 < price_percentile <= 0.78:
            location_score = 11.0
        else:
            location_score = 4.0

        if 0.02 <= ret20 <= 0.15 and ret60 > -0.05:
            momentum_score = 20.0
        elif -0.03 <= ret20 < 0.02 and ret60 > 0:
            momentum_score = 13.0
        elif 0.15 < ret20 <= 0.25:
            momentum_score = 9.0
        else:
            momentum_score = 3.0

        if 1.0 <= volume_ratio <= 2.5:
            volume_score = 10.0
        elif 0.7 <= volume_ratio < 1.0 or 2.5 < volume_ratio <= 4.0:
            volume_score = 6.0
        else:
            volume_score = 2.0

        if 45 <= rsi <= 65:
            rsi_score = 10.0
        elif 35 <= rsi < 45 or 65 < rsi <= 72:
            rsi_score = 6.0
        elif rsi < 30:
            rsi_score = 4.0
        else:
            rsi_score = 1.0

        risk_score = 15.0 if volatility <= 0.30 else 10.0 if volatility <= 0.45 else 4.0
        score = trend_score + location_score + momentum_score + volume_score + rsi_score + risk_score

        stop_loss = max(current - 2.0 * atr, support * 0.985)
        if stop_loss >= current:
            stop_loss = current - 2.0 * atr
        stop_loss = max(stop_loss, current * 0.88)
        risk_per_share = max(current - stop_loss, current * 0.02)
        target_price = max(resistance, current + risk_per_share * 1.8)
        if price_percentile > 0.85:
            target_price = max(current + 1.5 * atr, current + risk_per_share * 1.5)
        risk_reward = (target_price - current) / max(risk_per_share, 0.01)
        upside_pct = (target_price / current - 1) * 100
        buy_low = max(current - 0.5 * atr, stop_loss + 0.5 * risk_per_share)
        buy_high = current + 0.20 * atr

        factors = {
            "trend": trend_score,
            "price_location": location_score,
            "momentum": momentum_score,
            "volume": volume_score,
            "rsi": rsi_score,
            "risk": risk_score,
        }
        reasons = []
        warnings = []
        if trend_score >= 18:
            reasons.append("中期趋势向上，20 日均线强于 60 日均线")
        if 0.20 <= price_percentile <= 0.60:
            reasons.append("价格位于近 120 日区间中低部，仅表示价格位置因子相对有利")
        if 0.02 <= ret20 <= 0.15:
            reasons.append("20 日动量温和转强，暂未出现明显追高")
        if 1.0 <= volume_ratio <= 2.5:
            reasons.append("成交量温和放大，资金参与度改善")
        if rsi > 75:
            warnings.append("RSI 偏高，短线追涨风险较大")
        if price_percentile > 0.85:
            warnings.append("股价接近 120 日高位，潜力空间依赖突破")
        if volatility > 0.45:
            warnings.append("年化波动较高，建议降低仓位")
        if ret20 > 0.25:
            warnings.append("20 日涨幅过大，不建议追高")
        minimum_amount = (
            self.config.min_average_amount * 0.5
            if instrument_type(symbol) == "etf"
            else self.config.min_average_amount
        )
        liquid_enough = average_amount >= minimum_amount
        if not liquid_enough:
            warnings.append(
                f"近 20 日平均成交额仅 {average_amount / 10_000:.0f} 万元，流动性不足"
            )

        action = "buy" if (
            score >= self.config.buy_score
            and risk_reward >= self.config.min_risk_reward
            and current > ma20.iloc[-1]
            and ret20 <= 0.25
            and liquid_enough
        ) else "hold"
        if score < self.config.watch_score:
            warnings.append("多因子得分不足，暂不进入候选买入区")

        risk_budget = max(equity, 0) * self.config.risk_per_trade
        risk_quantity = int(risk_budget / max(risk_per_share, 0.01))
        symbol_room = max(equity * self.config.max_position_pct - current_symbol_value, 0)
        capital_quantity = int(min(max(cash, 0), symbol_room) / current)
        liquidity_quantity = capital_quantity
        amount = float((quote or {}).get("amount") or average_amount or 0)
        if amount > 0:
            liquidity_quantity = int(amount * 0.0005 / current)
        suggested_qty = max(
            min(risk_quantity, capital_quantity, liquidity_quantity) // 100 * 100,
            0,
        )
        if action != "buy":
            suggested_qty = 0
        suggested_amount = suggested_qty * current
        buy_fee = max(suggested_amount * self.config.commission_rate, self.config.min_commission) if suggested_qty else 0
        target_amount = suggested_qty * target_price
        sell_fee = (
            max(target_amount * self.config.commission_rate, self.config.min_commission)
            + (0 if instrument_type(symbol) == "etf" else target_amount * self.config.stamp_tax_rate)
        ) if suggested_qty else 0
        expected_profit = max(target_amount - suggested_amount - buy_fee - sell_fee, 0)
        max_loss = max((current - stop_loss) * suggested_qty + buy_fee, 0)
        confidence = min(max(
            score / 100 * 0.68
            + min(risk_reward / 3, 1) * 0.17
            + min(average_amount / max(minimum_amount * 5, 1), 1) * 0.10
            + max(0, 1 - volatility / 0.60) * 0.05,
            0,
        ), 0.95)

        return OpportunityResult(
            symbol=symbol,
            action=action,
            score=score,
            confidence=confidence,
            price=current,
            buy_low=buy_low,
            buy_high=buy_high,
            stop_loss=stop_loss,
            target_price=target_price,
            upside_pct=upside_pct,
            risk_reward=risk_reward,
            suggested_qty=suggested_qty,
            suggested_amount=suggested_amount,
            expected_profit=expected_profit,
            max_loss=max_loss,
            price_percentile=price_percentile * 100,
            rsi=rsi,
            volatility=volatility * 100,
            volume_ratio=volume_ratio,
            average_amount=average_amount,
            reasons=reasons[:4],
            warnings=warnings[:4],
            factors=factors,
        )

    def validate_history(
        self,
        symbol: str,
        data: pd.DataFrame,
        horizon: int = 10,
        step: int = 5,
    ) -> Dict:
        frame = self.normalize_frame(data)
        if len(frame) < self.config.min_history + horizon:
            return {"samples": 0, "win_rate": 0, "avg_return": 0, "max_drawdown": 0}
        returns = []
        for end in range(self.config.min_history, len(frame) - horizon, step):
            history = frame.iloc[:end]
            result = self.analyze(symbol, history, 100_000, 100_000)
            if result.action != "buy":
                continue
            future = frame.iloc[end:end + horizon]
            entry = float(future["Open"].iloc[0])
            exit_price = float(future["Close"].iloc[-1])
            for _, row in future.iterrows():
                if float(row["Low"]) <= result.stop_loss:
                    exit_price = result.stop_loss
                    break
                if float(row["High"]) >= result.target_price:
                    exit_price = result.target_price
                    break
            net_return = exit_price / entry - 1 - (
                self.config.commission_rate * 2 + self.config.stamp_tax_rate
            )
            returns.append(net_return)
        if not returns:
            return {"samples": 0, "win_rate": 0, "avg_return": 0, "max_drawdown": 0}
        series = pd.Series(returns, dtype=float)
        equity = (1 + series).cumprod()
        drawdown = equity / equity.cummax() - 1
        return {
            "samples": int(len(series)),
            "win_rate": round(float((series > 0).mean() * 100), 1),
            "avg_return": round(float(series.mean() * 100), 2),
            "median_return": round(float(series.median() * 100), 2),
            "max_drawdown": round(float(drawdown.min() * 100), 2),
        }

    def _empty(self, symbol: str, quote: Optional[Dict], warning: str) -> OpportunityResult:
        price = float((quote or {}).get("price") or 0)
        return OpportunityResult(
            symbol=symbol,
            action="hold",
            score=0,
            confidence=0,
            price=price,
            buy_low=0,
            buy_high=0,
            stop_loss=0,
            target_price=0,
            upside_pct=0,
            risk_reward=0,
            suggested_qty=0,
            suggested_amount=0,
            expected_profit=0,
            max_loss=0,
            price_percentile=0,
            rsi=0,
            volatility=0,
            volume_ratio=0,
            average_amount=0,
            warnings=[warning],
        )
