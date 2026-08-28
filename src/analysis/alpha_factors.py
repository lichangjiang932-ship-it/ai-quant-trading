# -*- coding: utf-8 -*-
"""Alpha158 精简版技术因子 (借鉴 Microsoft Qlib Alpha158 因子库, 37.5k stars)。

从日K历史计算经典技术因子, 供买入筛选器加分/否决:
  - RSI(14)          超买防追高 / 超卖反弹
  - MACD(12,26,9)    多头/空头趋势确认
  - BOLL(20,2)       突破上轨过热减分
  - 量价配合         放量上攻加分 / 缩量滞涨减分
  - 动量 (20/60日)   涨幅过大防回撤 / 中期动量确认
  - 波动率 (20日)    年化波动过高减分

纯函数, 输入 DataFrame(High/Low/Close/Volume), 不依赖网络。
"""
from __future__ import annotations

from typing import Dict, Optional


def _rsi(closes, period: int = 14) -> float:
    """Wilder RSI。数据不足返回 50 (中性)。"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(values, period: int):
    """标准 EMA 序列。"""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _macd_hist_series(closes):
    """MACD 柱 (hist = DIF - DEA) 全序列。"""
    if len(closes) < 35:
        return []
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    return [a - b for a, b in zip(dif, dea)]


def evaluate_alpha(history, price: float) -> Dict:
    """计算技术因子并给出综合调整分。

    Returns:
        {
          "adjust": float,        # 加到综合分的调整值 [-15, +12]
          "veto": str|None,       # 一票否决原因 (极端超买)
          "detail": {...},        # 各因子数值, 写入复盘/日志
          "notes": [...],         # 人话说明
        }
    """
    out: Dict = {"adjust": 0.0, "veto": None, "notes": []}
    detail: Dict[str, float] = {}
    try:
        closes = [float(v) for v in history["Close"].tolist() if float(v) > 0]
        highs = [float(v) for v in history["High"].tolist()] if "High" in history.columns else []
        lows = [float(v) for v in history["Low"].tolist()] if "Low" in history.columns else []
        vols = [float(v) for v in history["Volume"].tolist()] if "Volume" in history.columns else []
    except (KeyError, TypeError, ValueError):
        return out
    n = len(closes)
    if n < 30 or price <= 0:
        return out  # 数据不足不打分

    adj = 0.0
    notes = []

    # ── RSI(14): 超买防追高 / 超卖反弹 ──
    rsi = _rsi(closes, 14)
    detail["rsi14"] = round(rsi, 1)
    if rsi >= 78:
        out["veto"] = f"RSI {rsi:.0f} 极端超买, 追高风险"
        adj -= 8
    elif rsi >= 70:
        adj -= 4
        notes.append(f"RSI {rsi:.0f} 偏超买")
    elif rsi <= 32:
        adj += 3
        notes.append(f"RSI {rsi:.0f} 超卖区")

    # ── MACD: 趋势确认 ──
    hist = _macd_hist_series(closes)
    if len(hist) >= 2:
        detail["macd_hist"] = round(hist[-1], 4)
        if hist[-1] > 0 and hist[-1] > hist[-2]:
            adj += 3
            notes.append("MACD多头增强")
        elif hist[-1] < 0 and hist[-1] < hist[-2]:
            adj -= 3
            notes.append("MACD空头增强")

    # ── BOLL(20,2): 上轨过热 ──
    ma20 = sum(closes[-20:]) / 20.0
    std20 = (sum((c - ma20) ** 2 for c in closes[-20:]) / 20.0) ** 0.5
    upper = ma20 + 2 * std20
    detail["boll_upper"] = round(upper, 2)
    if price > upper * 1.01:
        adj -= 5
        notes.append(f"突破布林上轨 {upper:.2f}, 短线过热")

    # ── 量价配合: 放量上攻 / 缩量滞涨 ──
    if len(vols) >= 25:
        v5 = sum(vols[-5:]) / 5.0
        v20 = sum(vols[-20:]) / 20.0
        vr = v5 / v20 if v20 > 0 else 1.0
        detail["vol_ratio"] = round(vr, 2)
        mom5 = closes[-1] / closes[-6] - 1 if n >= 6 and closes[-6] > 0 else 0.0
        if vr >= 1.4 and mom5 > 0.01:
            adj += 4
            notes.append(f"放量上攻 (量比{vr:.1f})")
        elif vr <= 0.6 and mom5 < 0:
            adj -= 3
            notes.append(f"缩量阴跌 (量比{vr:.1f})")

    # ── 动量: 过热防回撤 / 中期趋势 ──
    mom20 = closes[-1] / closes[-21] - 1 if n >= 21 and closes[-21] > 0 else 0.0
    mom60 = closes[-1] / closes[-61] - 1 if n >= 61 and closes[-61] > 0 else 0.0
    detail["mom20"] = round(mom20 * 100, 1)
    detail["mom60"] = round(mom60 * 100, 1)
    if mom20 > 0.25:
        adj -= 4
        notes.append(f"20日已涨 {mom20*100:.0f}%, 回撤风险")
    elif mom60 > 0.05:
        adj += 2
        notes.append("中期动量向上")

    # ── 买入位置: 距 20 日高点的距离 (核心防追高) ──
    # 亏损归因: 多笔最大亏损都买在 20 日高点附近(追顶), 而盈利单多为回踩买入。
    # 规则: 贴顶(距高点<2%且非放量突破)重罚; 回踩5~12%加分; 真突破(放量)放行。
    hi20 = max(closes[-20:]) if n >= 20 else max(closes)
    lo20 = min(closes[-20:]) if n >= 20 else min(closes)
    if hi20 > 0 and hi20 > lo20:
        dist_hi = (hi20 - price) / hi20          # 距 20 日高点回撤幅度
        rng_pos = (price - lo20) / (hi20 - lo20)  # 在区间中的位置 0=低点 1=高点
        detail["dist_from_high20"] = round(dist_hi * 100, 1)
        detail["range_pos20"] = round(rng_pos, 2)

        vr_now = detail.get("vol_ratio", 0.0) or 0.0
        is_breakout = price >= hi20 * 0.995 and vr_now >= 1.4  # 放量突破, 不算追高

        if is_breakout:
            adj += 2
            notes.append(f"放量突破20日高点(量比{vr_now:.1f}), 视为有效突破")
        elif dist_hi <= 0.02:
            # 贴着 20 日高点买 = 典型追顶
            adj -= 9
            notes.append(f"现价距20日高点仅{dist_hi*100:.1f}%(追顶), 无突破量能")
        elif dist_hi <= 0.05:
            adj -= 4
            notes.append(f"距20日高点{dist_hi*100:.1f}%, 位置偏高")
        elif 0.05 < dist_hi <= 0.12:
            # 上升趋势中的健康回踩 — 最佳买点
            adj += 5
            notes.append(f"回踩20日高点{dist_hi*100:.1f}%, 位置健康")
        elif dist_hi > 0.25:
            adj -= 2
            notes.append(f"距20日高点{dist_hi*100:.0f}%, 走势偏弱")

    # ── 波动率: 年化过高减分 ──
    rets = [closes[i] / closes[i - 1] - 1 for i in range(n - 20, n)]
    mean_r = sum(rets) / len(rets)
    vol_d = (sum((r - mean_r) ** 2 for r in rets) / len(rets)) ** 0.5
    vol_ann = vol_d * (244 ** 0.5)
    detail["volat20_ann"] = round(vol_ann * 100, 1)
    if vol_ann > 0.85:
        adj -= 3
        notes.append(f"年化波动 {vol_ann*100:.0f}% 偏高")

    out["adjust"] = round(max(-15.0, min(adj, 12.0)), 1)
    out["notes"] = notes
    out["detail"] = detail
    return out
