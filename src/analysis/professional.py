"""面向实盘决策的数据质量、市场环境与组合资金门禁。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


@dataclass
class DataQualityResult:
    allowed: bool
    score: float
    status: str
    latest_date: str = ""
    stale_sessions: int = 0
    rows: int = 0
    source: str = ""
    retrieved_at: str = ""
    invalid_rows: int = 0
    zero_volume_ratio: float = 0.0
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        payload = asdict(self)
        payload["score"] = round(float(self.score), 2)
        payload["zero_volume_ratio"] = round(float(self.zero_volume_ratio), 4)
        return payload


@dataclass
class MarketRegimeResult:
    code: str
    label: str
    position_multiplier: float
    allow_new_positions: bool
    score: float = 0.0
    latest_date: str = ""
    ret20_pct: float = 0.0
    drawdown60_pct: float = 0.0
    volatility_pct: float = 0.0
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float):
                payload[key] = round(value, 4)
        return payload


class ProfessionalDecisionLayer:
    def __init__(
        self,
        min_history: int = 80,
        max_stale_sessions: int = 2,
        neutral_position_multiplier: float = 0.5,
        unknown_position_multiplier: float = 0.5,
        max_daily_new_exposure: float = 0.20,
    ):
        self.min_history = max(int(min_history), 30)
        self.max_stale_sessions = max(int(max_stale_sessions), 0)
        self.neutral_position_multiplier = min(max(float(neutral_position_multiplier), 0), 1)
        self.unknown_position_multiplier = min(max(float(unknown_position_multiplier), 0), 1)
        self.max_daily_new_exposure = min(max(float(max_daily_new_exposure), 0), 1)

    @staticmethod
    def _normalize(data: pd.DataFrame) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()
        metadata = dict(getattr(data, "attrs", {}) or {})
        frame = data.copy()
        frame.rename(columns={column: str(column).title() for column in frame.columns}, inplace=True)
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(column not in frame.columns for column in required):
            return pd.DataFrame()
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.replace([np.inf, -np.inf], np.nan, inplace=True)
        frame.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.sort_index()
        frame.attrs.update(metadata)
        return frame

    @staticmethod
    def _latest_date(frame: pd.DataFrame) -> Optional[date]:
        if frame.empty:
            return None
        index_value = frame.index[-1]
        if isinstance(index_value, (int, np.integer)):
            return None
        if hasattr(index_value, "date"):
            return index_value.date()
        try:
            return pd.Timestamp(index_value).date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _business_sessions_between(
        latest: date,
        target: date,
        holidays: Optional[Iterable[str]] = None,
    ) -> int:
        if latest >= target:
            return 0
        sessions = pd.bdate_range(latest, target, inclusive="neither")
        holiday_set = {str(item)[:10] for item in (holidays or [])}
        return int(sum(timestamp.date().isoformat() not in holiday_set for timestamp in sessions))

    def data_quality(
        self,
        data: pd.DataFrame,
        target_date: Optional[date] = None,
        holidays: Optional[Iterable[str]] = None,
    ) -> DataQualityResult:
        frame = self._normalize(data)
        if frame.empty:
            return DataQualityResult(
                allowed=False,
                score=0,
                status="blocked",
                reasons=["缺少完整 OHLCV 日线，禁止自动决策"],
            )

        reasons = []
        warnings = []
        score = 100.0
        source = str(frame.attrs.get("data_source", "") or "")
        retrieved_at = str(frame.attrs.get("retrieved_at", "") or "")
        latest_date = self._latest_date(frame)
        stale_sessions = 0
        if latest_date and target_date:
            stale_sessions = self._business_sessions_between(latest_date, target_date, holidays)
            if stale_sessions > self.max_stale_sessions:
                score -= 50
                reasons.append(f"行情已落后 {stale_sessions} 个交易日")
            elif stale_sessions > 0:
                warnings.append(f"最新日线距目标交易日 {stale_sessions} 个交易日")

        if len(frame) < self.min_history:
            score -= 50
            reasons.append(f"历史数据不足 {self.min_history} 条")

        recent = frame.tail(60)
        invalid_mask = (
            recent[["Open", "High", "Low", "Close"]].isna().any(axis=1)
            | (recent["Low"] <= 0)
            | (recent["High"] < recent[["Open", "Close"]].max(axis=1))
            | (recent["Low"] > recent[["Open", "Close"]].min(axis=1))
        )
        invalid_rows = int(invalid_mask.sum())
        if bool(invalid_mask.iloc[-1]):
            score -= 70
            reasons.append("最新一根日线价格关系异常")
        elif invalid_rows:
            score -= min(invalid_rows * 8, 30)
            warnings.append(f"近 60 日发现 {invalid_rows} 条 OHLC 异常记录")

        duplicate_count = int(frame.index.duplicated().sum())
        if duplicate_count:
            score -= min(duplicate_count * 2, 20)
            warnings.append(f"发现 {duplicate_count} 条重复日期")

        zero_volume_ratio = float((frame["Volume"].tail(20) <= 0).mean())
        volume_imputed = bool(frame.attrs.get("volume_imputed", False))
        if volume_imputed or zero_volume_ratio >= 0.50:
            score -= 50
            reasons.append("成交量数据缺失或近 20 日多数为零")
        elif zero_volume_ratio >= 0.10:
            score -= 20
            warnings.append("近 20 日存在较多零成交量记录")

        extreme_return = frame["Close"].pct_change().abs().tail(60)
        if bool((extreme_return > 0.35).any()):
            score -= 15
            warnings.append("近期存在超过 35% 的异常跳变，请核对复权方式")

        allowed = not reasons and score >= 60
        status = "good" if allowed and score >= 85 else "warning" if allowed else "blocked"
        if allowed:
            reasons.append("历史长度、时效和最新 OHLC 校验通过")
        return DataQualityResult(
            allowed=allowed,
            score=max(score, 0),
            status=status,
            latest_date=latest_date.isoformat() if latest_date else "",
            stale_sessions=stale_sessions,
            rows=int(len(frame)),
            source=source,
            retrieved_at=retrieved_at,
            invalid_rows=invalid_rows,
            zero_volume_ratio=zero_volume_ratio,
            reasons=reasons[:3],
            warnings=warnings[:3],
        )

    def market_regime(self, index_data: pd.DataFrame) -> MarketRegimeResult:
        frame = self._normalize(index_data)
        if len(frame) < 60:
            return MarketRegimeResult(
                code="unknown",
                label="市场数据不足",
                position_multiplier=self.unknown_position_multiplier,
                allow_new_positions=True,
                warnings=["基准指数数据不足，仓位自动降档"],
            )

        close = frame["Close"]
        current = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        ret20 = current / float(close.iloc[-21]) - 1
        high60 = float(close.tail(60).max())
        drawdown60 = current / high60 - 1
        volatility = float(close.pct_change().tail(20).std(ddof=0) * np.sqrt(252))
        score = 50.0
        score += 18 if current > ma20 else -18
        score += 17 if ma20 > ma60 else -17
        score += max(min(ret20 * 100, 12), -12)
        score += max(min((drawdown60 + 0.08) * 100, 8), -12)

        severe_risk = drawdown60 <= -0.12 or (volatility >= 0.35 and ret20 < 0)
        weak_trend = current < ma20 and ma20 < ma60
        strong_trend = current > ma20 and ma20 > ma60 and ret20 > 0 and drawdown60 > -0.08
        if severe_risk or weak_trend:
            code = "risk_off"
            label = "防守环境"
            multiplier = 0.0
            allow_new = False
            reasons = ["基准指数趋势向下或回撤/波动达到防守阈值"]
            warnings = ["暂停新开仓，仅管理已有持仓"]
        elif strong_trend:
            code = "risk_on"
            label = "进攻环境"
            multiplier = 1.0
            allow_new = True
            reasons = ["基准指数站上 20/60 日均线且近 20 日动量为正"]
            warnings = []
        else:
            code = "neutral"
            label = "中性环境"
            multiplier = self.neutral_position_multiplier
            allow_new = True
            reasons = ["基准指数趋势尚未形成一致方向"]
            warnings = [f"单笔计划仓位降至正常值的 {multiplier * 100:.0f}%"]

        index_value = frame.index[-1]
        latest_date = index_value.strftime("%Y-%m-%d") if hasattr(index_value, "strftime") else str(index_value)
        return MarketRegimeResult(
            code=code,
            label=label,
            position_multiplier=multiplier,
            allow_new_positions=allow_new,
            score=max(min(score, 100), 0),
            latest_date=latest_date,
            ret20_pct=ret20 * 100,
            drawdown60_pct=drawdown60 * 100,
            volatility_pct=volatility * 100,
            reasons=reasons,
            warnings=warnings,
        )

    def daily_new_capital_limit(self, total_asset: float, cash: float) -> float:
        return max(min(float(total_asset) * self.max_daily_new_exposure, float(cash)), 0)

    @staticmethod
    def adjusted_quantity(
        raw_quantity: int,
        price: float,
        position_multiplier: float,
        remaining_capital: float,
    ) -> int:
        if raw_quantity <= 0 or price <= 0 or position_multiplier <= 0 or remaining_capital <= 0:
            return 0
        scaled = int(raw_quantity * min(position_multiplier, 1)) // 100 * 100
        capital_limited = int(remaining_capital / price) // 100 * 100
        return max(min(scaled, capital_limited), 0)
