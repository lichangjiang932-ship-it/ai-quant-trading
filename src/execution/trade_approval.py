"""AI 交易信号进入订单系统前的统一审批门禁。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4


@dataclass(frozen=True)
class ApprovalPolicy:
    max_signal_age_seconds: int = 1800
    max_price_drift_pct: float = 1.5
    min_buy_confidence: float = 0.58
    min_risk_reward: float = 1.35
    min_data_quality_score: float = 70.0


@dataclass
class ApprovalDecision:
    allowed: bool
    status: str
    source: str
    symbol: str
    side: str
    approved_quantity: int
    reference_price: float
    market_price: float
    price_drift_pct: float
    signal_age_seconds: float
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    approval_id: str = field(default_factory=lambda: uuid4().hex[:16])
    reviewed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        payload = asdict(self)
        for key in (
            "reference_price",
            "market_price",
            "price_drift_pct",
            "signal_age_seconds",
        ):
            payload[key] = round(float(payload[key]), 4)
        return payload


class TradeApprovalGate:
    def __init__(self, policy: Optional[ApprovalPolicy] = None):
        self.policy = policy or ApprovalPolicy()

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            return None

    def review(
        self,
        *,
        source: str,
        symbol: str,
        side: str,
        signal_action: str,
        requested_quantity: int,
        recommended_quantity: int,
        reference_price: float,
        market_price: float,
        generated_at: Optional[str] = None,
        confidence: Optional[float] = None,
        risk_reward: Optional[float] = None,
        max_buy_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        data_quality: Optional[Dict] = None,
        now: Optional[datetime] = None,
        max_signal_age_seconds: Optional[int] = None,
    ) -> ApprovalDecision:
        reviewed_at = now or datetime.now()
        reasons: List[str] = []
        warnings: List[str] = []
        normalized_side = str(side or "").lower()
        normalized_action = str(signal_action or "").lower()
        requested = max(int(requested_quantity or 0), 0)
        recommended = max(int(recommended_quantity or 0), 0)
        quantity = min(requested, recommended) if requested and recommended else recommended or requested

        if normalized_side not in ("buy", "sell"):
            reasons.append("交易方向无效")
        valid_actions = ("buy",) if normalized_side == "buy" else ("sell", "reduce")
        if normalized_action not in valid_actions:
            reasons.append("当前有效信号与订单方向不一致")
        if quantity <= 0:
            reasons.append("审批数量必须大于 0")
        if normalized_side == "buy" and quantity % 100 != 0:
            reasons.append("A 股买入审批数量必须为 100 股整数倍")
        if requested > 0 and recommended > 0 and requested > recommended:
            warnings.append(f"请求数量已从 {requested} 股下调至模型上限 {recommended} 股")

        reference = float(reference_price or 0)
        market = float(market_price or 0)
        drift = (market / reference - 1) * 100 if reference > 0 and market > 0 else 0.0
        if reference <= 0 or market <= 0:
            reasons.append("缺少有效参考价或实时价格")

        signal_age = 0.0
        signal_time = self._parse_time(generated_at)
        age_limit = int(
            max_signal_age_seconds
            if max_signal_age_seconds is not None
            else self.policy.max_signal_age_seconds
        )
        if generated_at and signal_time is None:
            reasons.append("信号生成时间无效")
        elif signal_time is not None:
            signal_age = max((reviewed_at - signal_time).total_seconds(), 0.0)
            if signal_age > age_limit:
                reasons.append(f"信号已过期（{signal_age / 60:.1f} 分钟）")

        quality = data_quality or {}
        if quality:
            if not bool(quality.get("allowed")):
                reasons.append("行情数据质量门禁未通过")
            quality_score = float(quality.get("score", 100) or 0)
            if quality_score < self.policy.min_data_quality_score:
                reasons.append(f"行情质量评分仅 {quality_score:.0f} 分")

        if normalized_side == "buy":
            if confidence is None or float(confidence) < self.policy.min_buy_confidence:
                reasons.append("买入信号置信度不足")
            if risk_reward is None or float(risk_reward) < self.policy.min_risk_reward:
                reasons.append("预期盈亏比未达到审批标准")
            if reference > 0 and drift > self.policy.max_price_drift_pct:
                reasons.append(f"价格较信号价上移 {drift:.2f}%，超过允许漂移")
            if max_buy_price and market > float(max_buy_price):
                reasons.append(f"实时价格高于最高买入价 {float(max_buy_price):.2f}")
            if stop_loss and market <= float(stop_loss):
                reasons.append("实时价格已跌破计划止损位，原买入逻辑失效")

        allowed = not reasons
        if allowed:
            reasons.append("信号时效、价格、数据质量和交易数量审批通过")
        return ApprovalDecision(
            allowed=allowed,
            status="approved" if allowed else "rejected",
            source=str(source or "unknown"),
            symbol=str(symbol or ""),
            side=normalized_side,
            approved_quantity=quantity if allowed else 0,
            reference_price=reference,
            market_price=market,
            price_drift_pct=drift,
            signal_age_seconds=signal_age,
            reasons=reasons[:6],
            warnings=warnings[:4],
            reviewed_at=reviewed_at.isoformat(),
        )
