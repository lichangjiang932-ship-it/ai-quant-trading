# -*- coding: utf-8 -*-
"""全市场选股漏斗 (src/analysis/market_scanner.py) 单元测试。

只测纯逻辑(归一化/粗筛/配置加载), 不发起真实网络请求 —— 保证测试稳定快速。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.analysis.market_scanner import (
    PrefilterConfig,
    _is_bad_name,
    _normalize_row,
    load_prefilter_config,
    prefilter_market,
)


def _row(symbol="sh600000", name="测试股", price=10.0, change=1.0,
         amount=5e8, turnover=3.0, float_yi=500.0):
    return {
        "symbol": symbol, "code": symbol[2:], "name": name,
        "price": price, "change_pct": change, "amount": amount,
        "turnover": turnover, "float_mktcap_yi": float_yi,
        "total_mktcap_yi": float_yi * 1.2, "pe": 20.0, "pb": 2.0,
    }


# ─────────────── 归一化 ───────────────
def test_normalize_row_converts_units_and_fields():
    raw = {"symbol": "sh600000", "code": "600000", "name": "浦发银行",
           "trade": "10.50", "changepercent": "1.23", "amount": 1.2e9,
           "turnoverratio": "2.5", "mktcap": "30000000", "nmc": "20000000",
           "per": "6.5", "pb": "0.6"}
    r = _normalize_row(raw)
    assert r["symbol"] == "sh600000"
    assert r["price"] == 10.50
    assert r["change_pct"] == 1.23
    # mktcap 单位万元 → 亿元: 3000万万元 = 3000亿
    assert abs(r["total_mktcap_yi"] - 3000.0) < 1e-6
    assert abs(r["float_mktcap_yi"] - 2000.0) < 1e-6


def test_normalize_row_rejects_invalid_price():
    assert _normalize_row({"symbol": "sh600000", "trade": "0"}) is None
    assert _normalize_row({"symbol": "", "trade": "10"}) is None
    assert _normalize_row({"symbol": "sh600000", "trade": "-"}) is None


# ─────────────── 名称过滤 ───────────────
@pytest.mark.parametrize("name,expected", [
    ("ST早就该退", True),
    ("*ST某某", True),
    ("某某退", True),
    ("贵州茅台", False),
    ("宁德时代", False),
])
def test_is_bad_name(name, expected):
    assert _is_bad_name(name) is expected


# ─────────────── 粗筛规则 ───────────────
def test_prefilter_keeps_healthy_stock():
    rows = [_row()]
    assert len(prefilter_market(rows)) == 1


def test_prefilter_rejects_illiquid():
    """成交额不足 → 滑点大/易被操纵, 必须剔除。"""
    rows = [_row(amount=1e7)]  # 1000万, 远低于1亿
    assert prefilter_market(rows) == []


def test_prefilter_rejects_limit_up_chasing():
    """防追高核心: 涨停/接近涨停不追 (亏损归因里最大单笔买在阶段高点)。"""
    assert prefilter_market([_row(change=10.0)]) == []   # 涨停
    assert prefilter_market([_row(change=9.5)]) == []    # 接近涨停
    assert len(prefilter_market([_row(change=8.9)])) == 1  # 8.9% 仍可


def test_prefilter_rejects_falling_knife():
    """暴跌股不接刀。"""
    assert prefilter_market([_row(change=-9.0)]) == []
    assert len(prefilter_market([_row(change=-4.9)])) == 1


def test_prefilter_rejects_extreme_turnover():
    """换手率过低=无人问津; 过高=情绪过热(往往在出货)。"""
    assert prefilter_market([_row(turnover=0.2)]) == []   # 过低
    assert prefilter_market([_row(turnover=35.0)]) == []  # 过热
    assert len(prefilter_market([_row(turnover=5.0)])) == 1


def test_prefilter_rejects_micro_and_giant_cap():
    assert prefilter_market([_row(float_yi=8.0)]) == []     # 微盘
    assert prefilter_market([_row(float_yi=20000.0)]) == []  # 超大盘
    assert len(prefilter_market([_row(float_yi=800.0)])) == 1


def test_prefilter_rejects_penny_and_ultra_high_price():
    assert prefilter_market([_row(price=1.5)]) == []
    assert prefilter_market([_row(price=800.0)]) == []
    assert len(prefilter_market([_row(price=50.0)])) == 1


def test_prefilter_rejects_st_names():
    assert prefilter_market([_row(name="ST某某")]) == []


def test_prefilter_sorts_by_amount_and_caps():
    """结果按成交额降序(最活跃优先精筛), 且不超过 max_candidates。"""
    cfg = PrefilterConfig(max_candidates=3)
    rows = [_row(symbol=f"sh60000{i}", amount=1e9 * (i + 1)) for i in range(10)]
    out = prefilter_market(rows, cfg)
    assert len(out) == 3
    amounts = [r["amount"] for r in out]
    assert amounts == sorted(amounts, reverse=True)


def test_prefilter_empty_input():
    assert prefilter_market([]) == []
    assert prefilter_market(None) == []


# ─────────────── 配置加载 ───────────────
def test_load_prefilter_config_defaults():
    cfg = load_prefilter_config({})
    assert cfg.max_candidates == 60
    assert cfg.min_amount == 1.0e8


def test_load_prefilter_config_from_dict():
    cfg = load_prefilter_config({"prefilter": {"max_candidates": 25,
                                               "min_amount": 5e8,
                                               "bogus": 1}})
    assert cfg.max_candidates == 25
    assert cfg.min_amount == 5e8
    # 未指定的字段保持默认
    assert cfg.max_pages == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
