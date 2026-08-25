# -*- coding: utf-8 -*-
"""组合绩效分析 (借鉴 QuantStats / Qlib risk_analysis, 年化指标 + 月度收益表)。

输入: 净值序列 [{"date": "YYYY-MM-DD", "value": 12345.6}, ...] (每日收盘后记录)
输出: 累计/年化收益、波动率、Sharpe、Sortino、Calmar、最大回撤及区间、
      月度收益表、胜率统计。纯函数, 无外部依赖 (仅 numpy/pandas)。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

TRADING_DAYS = 244  # A股年交易日
RISK_FREE = 0.015   # 年化无风险利率近似


def _series(points: List[dict]):
    import pandas as pd
    if not points:
        return pd.Series(dtype=float)
    pairs = []
    for p in points:
        try:
            v = float(p.get('value', 0) or 0)
            d = str(p.get('date', ''))[:10]
            if v > 0 and d:
                pairs.append((d, v))
        except (TypeError, ValueError):
            continue
    # 去重按日期取最后一条
    dedup: Dict[str, float] = {}
    for d, v in pairs:
        dedup[d] = v
    if not dedup:
        return pd.Series(dtype=float)
    s = pd.Series(dedup).sort_index()
    return s


def max_drawdown(s) -> Dict:
    """返回 {'max_dd': 比例, 'peak_date': ..., 'trough_date': ..., 'recover_date': ...|None}"""
    if s is None or len(s) < 2:
        return {"max_dd": 0.0, "peak_date": "", "trough_date": "", "recover_date": None}
    cummax = s.cummax()
    dd = s / cummax - 1.0
    trough_idx = dd.idxmin()
    peak_idx = s.loc[:trough_idx].idxmax()
    max_dd = float(dd.loc[trough_idx])
    after = dd.loc[trough_idx:]
    recover = after[after >= -1e-9]
    recover_date = recover.index[0] if len(recover) else None
    return {
        "max_dd": max_dd,
        "peak_date": str(peak_idx),
        "trough_date": str(trough_idx),
        "recover_date": str(recover_date) if recover_date else None,
    }


def performance_report(points: List[dict], trades: Optional[List[dict]] = None,
                       benchmark_points: Optional[List[dict]] = None) -> dict:
    """生成完整绩效报告。数据点 < 2 时返回带提示的骨架报告。"""
    s = _series(points)
    if len(s) < 2:
        return {
            "available": False,
            "message": f"净值记录不足 (当前 {len(s)} 天, 需≥2天)。每个交易日收盘后自动累计。",
            "days": len(s),
        }

    rets = s.pct_change().dropna()
    total_ret = float(s.iloc[-1] / s.iloc[0] - 1)
    days = len(s)
    years = days / TRADING_DAYS
    annual_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
    vol_annual = float(rets.std() * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0

    rf_daily = RISK_FREE / TRADING_DAYS
    excess = rets - rf_daily
    downside = rets[rets < rf_daily]
    sharpe = (
        float(excess.mean() / excess.std() * math.sqrt(TRADING_DAYS))
        if len(excess) > 1 and excess.std() > 0 else None
    )
    sortino = (
        float(excess.mean() / downside.std() * math.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std() > 0 else None
    )
    mdd_info = max_drawdown(s)
    calmar = annual_ret / abs(mdd_info["max_dd"]) if mdd_info["max_dd"] < 0 else None

    win_days = int((rets > 0).sum())
    best_day = float(rets.max()) if len(rets) else 0.0
    worst_day = float(rets.min()) if len(rets) else 0.0

    report = {
        "available": True,
        "days": days,
        "start_date": str(s.index[0]),
        "end_date": str(s.index[-1]),
        "start_value": round(float(s.iloc[0]), 2),
        "end_value": round(float(s.iloc[-1]), 2),
        "total_return": round(total_ret, 4),
        "annual_return": round(annual_ret, 4),
        "annual_volatility": round(vol_annual, 4),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "max_drawdown": mdd_info,
        "win_days_pct": round(win_days / len(rets), 4) if len(rets) else 0,
        "best_day": round(best_day, 4),
        "worst_day": round(worst_day, 4),
        "monthly_returns": monthly_returns(s),
        "benchmark_return": None,
        "excess_return": None,
    }

    if benchmark_points:
        b = _series(benchmark_points)
        # 对齐到组合净值区间内
        b = b[(b.index >= s.index[0]) & (b.index <= s.index[-1])]
        if len(b) >= 2:
            b_ret = float(b.iloc[-1] / b.iloc[0] - 1)
            report["benchmark_return"] = round(b_ret, 4)
            report["excess_return"] = round(total_ret - b_ret, 4)

    return report


def monthly_returns(s) -> List[dict]:
    """按月聚合收益: [{'month': '2026-08', 'return': 0.032}, ...]。"""
    out = []
    try:
        idx = s.index.astype(str)
        grp: Dict[str, tuple] = {}
        for d, v in zip(idx, s.values):
            key = d[:7]
            if key not in grp:
                grp[key] = (v, v)
            grp[key] = (grp[key][0], v)  # (first, last)
        for month in sorted(grp):
            first, last = grp[month]
            if first > 0:
                out.append({"month": month, "return": round(last / first - 1, 4)})
    except Exception:
        pass
    return out
