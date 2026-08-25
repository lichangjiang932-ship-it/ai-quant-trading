# -*- coding: utf-8 -*-
"""交易保护机制 (src/risk/protections.py) 单元测试。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from src.risk.protections import (
    ProtectionConfig,
    TrailingState,
    atr_position_qty,
    cooldown_block_reason,
    compute_atr,
    drawdown_guard_pause,
    load_protection_config,
    stoploss_guard_pause,
    trailing_stop_hit,
    update_trailing_high,
)

CFG = ProtectionConfig()
NOW = date(2026, 8, 25)


# ── 冷却期 ──

def test_cooldown_blocks_recent_sell():
    trades = [{"symbol": "sh600000", "side": "sell", "date": (NOW - timedelta(days=1)).isoformat(), "reason": "止盈"}]
    assert cooldown_block_reason("sh600000", trades, NOW, CFG) is not None


def test_cooldown_expires():
    trades = [{"symbol": "sh600000", "side": "sell", "date": (NOW - timedelta(days=5)).isoformat(), "reason": "止盈"}]
    assert cooldown_block_reason("sh600000", trades, NOW, CFG) is None


def test_cooldown_ignores_other_symbols_and_buys():
    trades = [
        {"symbol": "sz000001", "side": "sell", "date": NOW.isoformat()},
        {"symbol": "sh600000", "side": "buy", "date": NOW.isoformat()},
    ]
    assert cooldown_block_reason("sh600000", trades, NOW, CFG) is None


# ── 连亏熔断 ──

def test_stoploss_guard_triggers():
    trades = [
        {"symbol": f"sh6{i:05d}", "side": "sell", "date": NOW.isoformat(), "reason": "AI信号 | 跌破止损位"}
        for i in range(2)
    ]
    reason = stoploss_guard_pause(trades, NOW, CFG)
    assert reason is not None and "连亏熔断" in reason


def test_stoploss_guard_not_triggered_by_single_stop():
    trades = [{"symbol": "sh600000", "side": "sell", "date": NOW.isoformat(), "reason": "跌破止损位"}]
    assert stoploss_guard_pause(trades, NOW, CFG) is None


def test_stoploss_guard_ignores_old_stops():
    old = (NOW - timedelta(days=30)).isoformat()
    trades = [
        {"symbol": "sh600000", "side": "sell", "date": old, "reason": "止损"},
        {"symbol": "sz000001", "side": "sell", "date": old, "reason": "止损"},
    ]
    assert stoploss_guard_pause(trades, NOW, CFG) is None


# ── 回撤熔断 ──

def test_drawdown_guard_triggers():
    assert drawdown_guard_pause(90_000, 100_000, CFG) is not None


def test_drawdown_guard_ok_within_threshold():
    assert drawdown_guard_pause(95_000, 100_000, CFG) is None
    assert drawdown_guard_pause(0, 0, CFG) is None  # 无数据不误报


# ── 追踪止损 ──

def test_trailing_state_updates_high():
    st = TrailingState()
    update_trailing_high(st, "sh600000", 10.0)
    update_trailing_high(st, "sh600000", 12.0)
    update_trailing_high(st, "sh600000", 11.0)
    assert st.highs["sh600000"] == 12.0


def test_trailing_stop_hits_after_pullback():
    # 成本10, 最高12 (+20% 激活), 现价11.5 → 回撤 ~4.2% ≥ 3%
    hit = trailing_stop_hit(10.0, 12.0, 11.5, CFG)
    assert hit is not None and "追踪止盈" in hit


def test_trailing_stop_no_hit_without_activation():
    # 最高仅 +2%, 未达激活线 4%
    assert trailing_stop_hit(10.0, 10.2, 9.9, CFG) is None


def test_trailing_stop_no_hit_when_price_at_high():
    assert trailing_stop_hit(10.0, 12.0, 12.0, CFG) is None


# ── ATR sizing ──

def _frame(n=40, base=10.0):
    rows = []
    for i in range(n):
        rows.append({
            "High": base * 1.02, "Low": base * 0.98,
            "Close": base, "Open": base, "Volume": 1e6,
        })
    return pd.DataFrame(rows)


def test_compute_atr_positive_and_insufficient_data():
    atr = compute_atr(_frame())
    assert atr > 0
    assert compute_atr(_frame(3)) == 0.0
    assert compute_atr(pd.DataFrame()) == 0.0


def test_atr_position_qty_scales_with_volatility():
    qty_low_vol = atr_position_qty(10.0, 0.10, 100_000, CFG)
    qty_high_vol = atr_position_qty(10.0, 0.50, 100_000, CFG)
    assert qty_low_vol > qty_high_vol > 0
    # 100 股整数手
    assert qty_low_vol % 100 == 0
    assert atr_position_qty(10.0, 0.0, 100_000, CFG) == 0  # 数据不足回退


def test_load_protection_config_from_dict():
    cfg = load_protection_config({"protections": {"cooldown_days": 7, "bogus_key": 1}})
    assert cfg.cooldown_days == 7
    assert cfg.stoploss_max_count == ProtectionConfig().stoploss_max_count


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
