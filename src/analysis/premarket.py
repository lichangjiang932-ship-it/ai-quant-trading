"""盘前概率预测、下一交易日计算与持仓退出决策。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PremarketConfig:
    min_history: int = 90
    neighbors: int = 30
    min_samples: int = 18
    buy_probability: float = 0.58
    min_expected_holding_pct: float = 0.45
    max_gap_up_pct: float = 2.5
    trading_cost_rate: float = 0.0016
    stop_loss_pct: float = 0.07
    take_profit_pct: float = 0.10


@dataclass
class PremarketForecast:
    symbol: str
    available: bool
    as_of_date: str = ""
    rise_probability: float = 0.0
    expected_intraday_pct: float = 0.0
    expected_holding_pct: float = 0.0
    downside_pct: float = 0.0
    confidence: float = 0.0
    sample_count: int = 0
    neighbor_count: int = 0
    previous_close: float = 0.0
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float):
                payload[key] = round(value, 4)
        return payload


def is_trading_day(value: date, holidays: Optional[Iterable[str]] = None) -> bool:
    holiday_set = {str(item)[:10] for item in (holidays or [])}
    return value.weekday() < 5 and value.isoformat() not in holiday_set


def target_trading_date(
    now: Optional[datetime] = None,
    holidays: Optional[Iterable[str]] = None,
) -> date:
    """盘前和盘中指向当天，收盘后指向下一交易日。"""
    current = now or datetime.now()
    candidate = current.date()
    if current.time() > time(15, 0) or not is_trading_day(candidate, holidays):
        candidate += timedelta(days=1)
    while not is_trading_day(candidate, holidays):
        candidate += timedelta(days=1)
    return candidate


class PremarketAnalyzer:
    FEATURE_COLUMNS = [
        "ret_1",
        "ret_5",
        "ret_20",
        "ma_gap",
        "ma_trend",
        "rsi",
        "volume_ratio",
        "volatility",
        "range_position",
    ]

    def __init__(self, config: Optional[PremarketConfig] = None):
        self.config = config or PremarketConfig()

    @staticmethod
    def normalize_frame(data: pd.DataFrame) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()
        frame = data.copy()
        frame.rename(columns={column: str(column).title() for column in frame.columns}, inplace=True)
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(column not in frame.columns for column in required):
            return pd.DataFrame()
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.replace([np.inf, -np.inf], np.nan, inplace=True)
        frame.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
        frame = frame[frame["Close"] > 0]
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.sort_index()
        return frame

    @staticmethod
    def before_trade_date(data: pd.DataFrame, trade_date: date) -> pd.DataFrame:
        """剔除目标交易日当日及之后数据，防止盘前预测偷看未来。"""
        frame = data.copy()
        if isinstance(frame.index, pd.DatetimeIndex):
            return frame[frame.index.date < trade_date]
        return frame

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        relative = gain / loss.replace(0, np.nan)
        return (100 - 100 / (1 + relative)).fillna(50)

    def _feature_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        close = frame["Close"]
        daily_return = close.pct_change()
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        volume_mean = frame["Volume"].rolling(20).mean().replace(0, np.nan)
        low60 = frame["Low"].rolling(60).min()
        high60 = frame["High"].rolling(60).max()
        width60 = (high60 - low60).replace(0, np.nan)
        features = pd.DataFrame(index=frame.index)
        features["ret_1"] = close.pct_change(1)
        features["ret_5"] = close.pct_change(5)
        features["ret_20"] = close.pct_change(20)
        features["ma_gap"] = close / ma20 - 1
        features["ma_trend"] = ma5 / ma20 - 1
        features["rsi"] = (self._rsi(close) - 50) / 50
        features["volume_ratio"] = frame["Volume"] / volume_mean - 1
        features["volatility"] = daily_return.rolling(20).std(ddof=0)
        features["range_position"] = (close - low60) / width60
        features["target_intraday"] = frame["Close"].shift(-1) / frame["Open"].shift(-1) - 1
        features["target_holding"] = frame["Close"].shift(-3) / frame["Open"].shift(-1) - 1
        return features.replace([np.inf, -np.inf], np.nan)

    @staticmethod
    def _weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
        order = np.argsort(values)
        sorted_values = values[order]
        sorted_weights = weights[order]
        cumulative = np.cumsum(sorted_weights)
        cutoff = quantile * cumulative[-1]
        return float(sorted_values[min(np.searchsorted(cumulative, cutoff), len(sorted_values) - 1)])

    def forecast(self, symbol: str, data: pd.DataFrame) -> PremarketForecast:
        frame = self.normalize_frame(data)
        if len(frame) < self.config.min_history:
            return PremarketForecast(
                symbol=symbol,
                available=False,
                warnings=[f"历史数据不足 {self.config.min_history} 个交易日"],
            )

        features = self._feature_frame(frame)
        current = features.iloc[-1][self.FEATURE_COLUMNS]
        historical = features.iloc[:-3].dropna(
            subset=self.FEATURE_COLUMNS + ["target_intraday", "target_holding"]
        )
        if current.isna().any() or len(historical) < self.config.min_samples:
            return PremarketForecast(
                symbol=symbol,
                available=False,
                sample_count=int(len(historical)),
                previous_close=float(frame["Close"].iloc[-1]),
                warnings=["有效历史相似样本不足，暂不生成买入预测"],
            )

        matrix = historical[self.FEATURE_COLUMNS].astype(float)
        center = matrix.median()
        scale = matrix.std(ddof=0).replace(0, 1).fillna(1)
        distances = np.sqrt((((matrix - center) / scale - (current - center) / scale) ** 2).mean(axis=1))
        neighbor_count = min(self.config.neighbors, len(historical))
        nearest_index = distances.nsmallest(neighbor_count).index
        nearest = historical.loc[nearest_index]
        nearest_distances = distances.loc[nearest_index].to_numpy(dtype=float)
        weights = 1 / np.maximum(nearest_distances, 0.08)
        weights /= weights.sum()
        holding = nearest["target_holding"].to_numpy(dtype=float)
        intraday = nearest["target_intraday"].to_numpy(dtype=float)
        positive = holding > self.config.trading_cost_rate
        rise_probability = float(np.dot(weights, positive.astype(float)))
        expected_intraday = float(np.dot(weights, intraday))
        expected_holding = float(np.dot(weights, holding) - self.config.trading_cost_rate)
        downside = self._weighted_quantile(holding, weights, 0.20)
        effective_neighbors = 1 / float(np.square(weights).sum())
        dispersion = float(np.sqrt(np.dot(weights, (holding - np.dot(weights, holding)) ** 2)))
        sample_confidence = min(effective_neighbors / max(self.config.neighbors * 0.75, 1), 1)
        stability = max(0.0, 1 - dispersion / 0.08)
        confidence = min(0.95, 0.35 + 0.40 * sample_confidence + 0.20 * stability)

        latest = features.iloc[-1]
        reasons = []
        warnings = []
        if latest["ma_trend"] > 0:
            reasons.append("5 日均线位于 20 日均线上方，短期趋势偏强")
        if latest["ret_20"] > 0:
            reasons.append("近 20 日动量为正")
        if 0.25 <= latest["range_position"] <= 0.75:
            reasons.append("价格处于近 60 日区间中部，未处在极端高位")
        reasons.append(f"历史相似行情 {neighbor_count} 次，扣除成本后上涨概率 {rise_probability * 100:.1f}%")
        if latest["range_position"] > 0.85:
            warnings.append("价格接近近 60 日高位，开盘追高风险较大")
        if latest["volatility"] > 0.035:
            warnings.append("近期日波动偏高，建议降低仓位")
        if expected_holding <= 0:
            warnings.append("相似行情的三日预期收益未覆盖交易成本")

        index_value = frame.index[-1]
        as_of_date = index_value.strftime("%Y-%m-%d") if hasattr(index_value, "strftime") else str(index_value)
        return PremarketForecast(
            symbol=symbol,
            available=True,
            as_of_date=as_of_date,
            rise_probability=rise_probability,
            expected_intraday_pct=expected_intraday * 100,
            expected_holding_pct=expected_holding * 100,
            downside_pct=downside * 100,
            confidence=confidence,
            sample_count=int(len(historical)),
            neighbor_count=int(neighbor_count),
            previous_close=float(frame["Close"].iloc[-1]),
            reasons=reasons[:4],
            warnings=warnings[:4],
        )

    def position_exit(
        self,
        symbol: str,
        data: pd.DataFrame,
        quantity: int,
        available_quantity: int,
        avg_cost: float,
        current_price: Optional[float] = None,
        opportunity_score: Optional[float] = None,
    ) -> Dict:
        frame = self.normalize_frame(data)
        if len(frame) < 30 or quantity <= 0 or avg_cost <= 0:
            return {
                "symbol": symbol,
                "action": "hold",
                "available": False,
                "reasons": ["持仓或历史数据不足，暂不生成卖出计划"],
            }

        close = frame["Close"]
        price = float(current_price or close.iloc[-1])
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else ma20
        rsi = float(self._rsi(close).iloc[-1])
        previous_close = close.shift(1)
        true_range = pd.concat([
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(true_range.tail(14).mean())
        if not np.isfinite(atr) or atr <= 0:
            atr = price * 0.025
        high20 = float(frame["High"].tail(20).max())
        high60 = float(frame["High"].tail(60).max())
        protective_stop = max(
            avg_cost * (1 - self.config.stop_loss_pct),
            high20 - 2.5 * atr,
            ma20 - atr,
        )
        target_price = max(avg_cost * (1 + self.config.take_profit_pct), high60)
        pnl_pct = (price / avg_cost - 1) * 100
        score = float(opportunity_score) if opportunity_score is not None else 50.0

        stop_trigger = price <= protective_stop
        trend_break = price < ma20 and ma5 < ma20 and ma20 <= ma60
        weak_score = score < 45
        take_profit = pnl_pct >= self.config.take_profit_pct * 100 and rsi >= 70
        pending_action = "hold"
        reasons = []
        warnings = []
        if stop_trigger:
            pending_action = "sell"
            reasons.append(f"现价已触及动态保护位 {protective_stop:.2f}")
        elif trend_break or weak_score:
            pending_action = "sell"
            reasons.append("短中期趋势转弱，优先控制回撤")
        elif take_profit:
            pending_action = "reduce"
            reasons.append("达到止盈区且 RSI 偏高，建议分批落袋")
        else:
            reasons.append(f"趋势未破坏，保护位上移至 {protective_stop:.2f}")
            reasons.append(f"接近目标价 {target_price:.2f} 时重新评估或分批止盈")

        available = max(min(int(available_quantity), int(quantity)), 0)
        t1_locked = available <= 0
        action = pending_action
        suggested_quantity = 0
        if pending_action == "sell":
            suggested_quantity = available
        elif pending_action == "reduce":
            suggested_quantity = min(max((available // 2) // 100 * 100, 100), available) if available else 0
        if pending_action in ("sell", "reduce") and t1_locked:
            action = "wait_t1"
            warnings.append("该持仓当日买入，T+1 锁定；下一交易日才能执行卖出")
        elif pending_action == "hold":
            action = "hold"
        if pnl_pct < -self.config.stop_loss_pct * 100:
            warnings.append("持仓亏损已超过基础止损比例，请勿继续加仓摊低成本")

        return {
            "symbol": symbol,
            "available": True,
            "action": action,
            "pending_action": pending_action,
            "price": round(price, 4),
            "avg_cost": round(avg_cost, 4),
            "pnl_pct": round(pnl_pct, 4),
            "quantity": int(quantity),
            "available_quantity": available,
            "suggested_quantity": int(suggested_quantity),
            "t1_locked": t1_locked,
            "protective_stop": round(protective_stop, 4),
            "target_price": round(target_price, 4),
            "rsi": round(rsi, 2),
            "reasons": reasons[:3],
            "warnings": warnings[:3],
        }
