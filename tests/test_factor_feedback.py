# -*- coding: utf-8 -*-
"""因子级反馈闭环 (src/analysis/strategy_memory.py) 单元测试。

闭环链条: 买入落库(pending) → 复盘回填胜负 → 下次买入按胜率加减分。
测试用临时 DB, 不污染 data/strategy_memory.db。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import src.analysis.strategy_memory as sm


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """每个用例用独立临时库, 隔离真实数据。"""
    monkeypatch.setattr(sm, "_DB_PATH", str(tmp_path / "t.db"))


def _buy(day, sym, factors, name="X"):
    sm.record_factor_evidence(day, sym, "buy", factors, name=name, reason="test")


# ─────────────── 落库 ───────────────
def test_record_creates_one_row_per_factor():
    """买入时每个触发因子各落一条 pending 记录。"""
    import sqlite3
    _buy("2026-08-29", "sh600000", ["追顶", "资金净流入", "强趋势"])
    conn = sqlite3.connect(sm._DB_PATH)
    rows = conn.execute(
        "SELECT factor, result_label FROM factor_evidence ORDER BY factor").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["强趋势", "资金净流入", "追顶"]
    assert all(r[1] == "pending" for r in rows)
    # pending 行不进胜率统计 (还没结果)
    assert sm.factor_stats() == {}


def test_record_is_idempotent():
    """同 (date,symbol,side,factor) 重复调用只记一次。"""
    _buy("2026-08-29", "sh600000", ["追顶"])
    _buy("2026-08-29", "sh600000", ["追顶"])
    sm.resolve_factor_evidence("2026-08-29", "sh600000", "buy", "买错", -100)
    assert sm.factor_stats()["追顶"]["samples"] == 1


def test_record_ignores_empty():
    sm.record_factor_evidence("2026-08-29", "sh600000", "buy", [])
    sm.record_factor_evidence("2026-08-29", "", "buy", ["追顶"])
    assert sm.factor_stats() == {}


# ─────────────── 回填 ───────────────
def test_resolve_fills_pending_rows():
    _buy("2026-08-29", "sh600000", ["追顶", "资金净流入"])
    sm.resolve_factor_evidence("2026-08-29", "sh600000", "buy", "买错", -500)
    st = sm.factor_stats()
    assert st["追顶"] == {"samples": 1, "wins": 0, "losses": 1,
                          "pnl": -500.0, "win_rate": 0.0}
    assert st["资金净流入"]["losses"] == 1


def test_resolve_does_not_touch_other_symbols():
    _buy("2026-08-29", "sh600000", ["追顶"])
    _buy("2026-08-29", "sh600001", ["追顶"])
    sm.resolve_factor_evidence("2026-08-29", "sh600000", "buy", "买对", 300)
    st = sm.factor_stats()["追顶"]
    assert st["samples"] == 1  # 只回填了 600000


def test_stats_computes_win_rate_and_pnl():
    for i, (lbl, pnl) in enumerate([("买对", 100), ("买对", 200), ("买错", -50)]):
        _buy("2026-08-29", f"sh60000{i}", ["回踩"])
        sm.resolve_factor_evidence("2026-08-29", f"sh60000{i}", "buy", lbl, pnl)
    g = sm.factor_stats()["回踩"]
    assert g["samples"] == 3 and g["wins"] == 2 and g["losses"] == 1
    assert g["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert g["pnl"] == pytest.approx(250.0)


def test_stats_days_filter():
    _buy("2020-01-01", "sh600000", ["老因子"])
    sm.resolve_factor_evidence("2020-01-01", "sh600000", "buy", "买对", 10)
    assert "老因子" in sm.factor_stats()
    assert "老因子" not in sm.factor_stats(days=30)


# ─────────────── 决策加减分 ───────────────
def _seed(factor, n, wins):
    for i in range(n):
        sym = f"sh60{i:04d}"
        _buy("2026-08-29", sym, [factor])
        sm.resolve_factor_evidence("2026-08-29", sym, "buy",
                                   "买对" if i < wins else "买错", 10 if i < wins else -10)


def test_adjust_ignores_small_samples():
    """样本不足时不让历史说话, 避免小样本噪声误杀好因子。"""
    _seed("回踩", 2, 0)  # 2 样本全错
    assert sm.factor_adjustment(["回踩"], min_samples=3)["adjust"] == 0.0


def test_adjust_penalizes_bad_factor():
    _seed("追顶", 4, 0)  # 0% 胜率
    r = sm.factor_adjustment(["追顶"], min_samples=3)
    assert r["adjust"] == -5.0
    assert "追顶" in r["notes"][0]


def test_adjust_light_penalty_for_weak_factor():
    _seed("弱因子", 4, 1)  # 25% 胜率 → <40 应 -5; 用 10 样本 45% 测 -2
    _seed("中因子", 10, 4)  # 40% 胜率
    r = sm.factor_adjustment(["中因子"], min_samples=3)
    assert r["adjust"] == -2.0


def test_adjust_rewards_strong_factor():
    _seed("好因子", 10, 8)  # 80% 胜率
    r = sm.factor_adjustment(["好因子"], min_samples=3)
    assert r["adjust"] == 3.0


def test_adjust_is_capped():
    """多个坏因子叠加时, 调整幅度有上限, 避免一票打死。"""
    for f in ("a", "b", "c", "d"):
        _seed(f, 4, 0)
    r = sm.factor_adjustment(["a", "b", "c", "d"], min_samples=3, cap=10.0)
    assert r["adjust"] == -10.0  # 4×(-5) 被截到 -10


def test_adjust_no_history_returns_zero():
    assert sm.factor_adjustment(["从未出现过"])["adjust"] == 0.0
    assert sm.factor_adjustment([])["adjust"] == 0.0


def test_adjust_detail_reports_samples():
    _seed("回踩", 5, 4)
    r = sm.factor_adjustment(["回踩"], min_samples=3)
    assert r["detail"]["回踩"]["samples"] == 5
    assert r["detail"]["回踩"]["win_rate"] == 80.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
