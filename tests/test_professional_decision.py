import numpy as np
import pandas as pd

from src.analysis.professional import ProfessionalDecisionLayer


def make_frame(trend=0.001, periods=160):
    returns = np.full(periods, trend)
    close = 100 * np.cumprod(1 + returns)
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    return pd.DataFrame({
        "Open": close * 0.999,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": np.full(periods, 1_000_000),
    }, index=index)


def test_data_quality_blocks_stale_history():
    frame = make_frame()
    target = (frame.index[-1] + pd.offsets.BDay(5)).date()

    result = ProfessionalDecisionLayer(max_stale_sessions=2).data_quality(frame, target)

    assert result.allowed is False
    assert result.status == "blocked"
    assert result.stale_sessions >= 4


def test_data_quality_does_not_count_configured_market_holidays():
    frame = make_frame()
    latest = frame.index[-1]
    target = (latest + pd.offsets.BDay(4)).date()
    holidays = [
        (latest + pd.offsets.BDay(offset)).date().isoformat()
        for offset in (1, 2, 3)
    ]

    result = ProfessionalDecisionLayer(max_stale_sessions=0).data_quality(
        frame,
        target,
        holidays=holidays,
    )

    assert result.stale_sessions == 0
    assert result.allowed is True


def test_market_regime_distinguishes_risk_on_and_risk_off():
    layer = ProfessionalDecisionLayer()

    risk_on = layer.market_regime(make_frame(0.002))
    risk_off = layer.market_regime(make_frame(-0.002))

    assert risk_on.code == "risk_on"
    assert risk_on.position_multiplier == 1
    assert risk_off.code == "risk_off"
    assert risk_off.allow_new_positions is False


def test_adjusted_quantity_obeys_regime_and_shared_capital():
    assert ProfessionalDecisionLayer.adjusted_quantity(1000, 10, 0.5, 100_000) == 500
    assert ProfessionalDecisionLayer.adjusted_quantity(1000, 10, 1.0, 3_600) == 300
    assert ProfessionalDecisionLayer.adjusted_quantity(1000, 10, 0.0, 100_000) == 0


def test_data_quality_blocks_imputed_volume_and_exposes_source():
    frame = make_frame()
    frame.attrs.update({
        "data_source": "mootdx",
        "retrieved_at": "2026-07-10T08:20:00",
        "volume_imputed": True,
    })

    result = ProfessionalDecisionLayer().data_quality(frame)

    assert result.allowed is False
    assert result.source == "mootdx"
    assert result.retrieved_at == "2026-07-10T08:20:00"
    assert any("成交量" in reason for reason in result.reasons)
