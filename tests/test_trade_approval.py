from datetime import datetime, timedelta

from src.execution.trade_approval import ApprovalPolicy, TradeApprovalGate


def make_gate():
    return TradeApprovalGate(ApprovalPolicy(
        max_signal_age_seconds=1800,
        max_price_drift_pct=1.5,
        min_buy_confidence=0.58,
        min_risk_reward=1.35,
        min_data_quality_score=70,
    ))


def test_approval_caps_quantity_and_allows_fresh_signal():
    now = datetime(2026, 7, 10, 10, 0)
    result = make_gate().review(
        source="ai_pick",
        symbol="sh600000",
        side="buy",
        signal_action="buy",
        requested_quantity=500,
        recommended_quantity=300,
        reference_price=10.0,
        market_price=10.05,
        generated_at=(now - timedelta(minutes=5)).isoformat(),
        confidence=0.72,
        risk_reward=1.8,
        max_buy_price=10.2,
        stop_loss=9.2,
        data_quality={"allowed": True, "score": 92},
        now=now,
    )

    assert result.allowed is True
    assert result.approved_quantity == 300
    assert result.warnings


def test_approval_rejects_stale_or_chased_signal():
    now = datetime(2026, 7, 10, 10, 0)
    result = make_gate().review(
        source="ai_pick",
        symbol="sh600000",
        side="buy",
        signal_action="buy",
        requested_quantity=300,
        recommended_quantity=300,
        reference_price=10.0,
        market_price=10.3,
        generated_at=(now - timedelta(minutes=45)).isoformat(),
        confidence=0.72,
        risk_reward=1.8,
        max_buy_price=10.2,
        stop_loss=9.2,
        data_quality={"allowed": True, "score": 92},
        now=now,
    )

    assert result.allowed is False
    assert any("过期" in reason for reason in result.reasons)
    assert any("漂移" in reason or "最高买入价" in reason for reason in result.reasons)


def test_approval_rejects_bad_data_and_low_confidence():
    result = make_gate().review(
        source="ai_trade",
        symbol="sh600000",
        side="buy",
        signal_action="buy",
        requested_quantity=100,
        recommended_quantity=100,
        reference_price=10.0,
        market_price=10.0,
        confidence=0.4,
        risk_reward=1.0,
        data_quality={"allowed": False, "score": 45},
    )

    assert result.allowed is False
    assert any("数据质量" in reason for reason in result.reasons)
    assert any("置信度" in reason for reason in result.reasons)
