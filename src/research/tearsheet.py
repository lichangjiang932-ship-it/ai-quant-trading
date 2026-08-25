"""公司速览卡 (Tearsheet) — 一页纸看懂一只股票。

聚合: 实时行情 / 估值 / 技术面 / 买卖区间 / 综合评级。
所有计算为纯函数, 数据由调用方注入, 便于单元测试。

输出示例:
{
  "symbol": "sz300750", "name": "宁德时代", "price": 180.5,
  "market_cap_yi": 7940.2, "pe_ttm": 28.6, "pb": 5.1,
  "turnover_pct": 2.1, "vol_ratio": 1.3,
  "technicals": {"ma5": ..., "ma20": ..., "rsi14": ..., "price_percentile": 0.62, "volatility_pct": ...},
  "valuation": {"pe_band": "中性", "pb_band": "偏高", "pe_percentile": ...},
  "trade_zone": {"buy_low": ..., "buy_high": ..., "stop_loss": ..., "target_price": ..., "risk_reward": ...},
  "rating": {"action": "buy|hold|sell", "score": 72, "confidence": 0.65, "reasons": [...]},
  "capital_flow": {"main_net": 123.4, "direction": "inflow|outflow|unknown"},
}
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_technicals(closes: List[float], volumes: Optional[List[float]] = None) -> Dict:
    """技术面: 均线 / RSI14 / 价格分位 / 波动率 / 量比。

    Args:
        closes: 收盘价序列 (新→旧或旧→新均可, 内部统一)
        volumes: 成交量序列 (与 closes 同序)
    """
    closes = [_safe_float(c) for c in closes]
    closes = [c for c in closes if c > 0]
    if len(closes) < 20:
        return {"ma5": None, "ma10": None, "ma20": None, "rsi14": None,
                "price_percentile": None, "volatility_pct": None, "vol_ratio": None}

    def ma(n: int) -> Optional[float]:
        if len(closes) < n:
            return None
        return round(float(np.mean(closes[-n:])), 2)

    # RSI14 (标准 Wilder 简化版)
    rsi = None
    if len(closes) >= 15:
        diffs = np.diff(closes[-15:])
        gains = np.where(diffs > 0, diffs, 0.0).mean()
        losses = np.where(diffs < 0, -diffs, 0.0).mean()
        if losses == 0:
            rsi = 100.0
        elif gains == 0:
            rsi = 0.0
        else:
            rsi = round(100 - 100 / (1 + gains / losses), 1)

    current = closes[-1]
    percentile = round(float((np.array(closes) <= current).mean()), 3)
    returns = np.diff(np.log(np.array(closes[-60:]) + 1e-12))
    volatility = round(float(returns.std()) * np.sqrt(252) * 100, 1) if len(returns) > 2 else None

    vol_ratio = None
    if volumes and len(volumes) >= 6:
        vols = [_safe_float(v) for v in volumes]
        avg5 = float(np.mean(vols[-6:-1]))
        if avg5 > 0:
            vol_ratio = round(float(vols[-1]) / avg5, 2)

    return {
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20),
        "rsi14": rsi, "price_percentile": percentile,
        "volatility_pct": volatility, "vol_ratio": vol_ratio,
    }


def valuation_band(pe: Optional[float], pb: Optional[float]) -> Dict:
    """估值带评估 (A 股粗粒度参考, 非投资建议)。"""
    bands = {"pe_band": "数据缺失", "pb_band": "数据缺失", "pe_percentile": None, "notes": []}
    if pe is not None:
        if pe <= 0:
            bands["pe_band"] = "亏损"
            bands["notes"].append("PE 为负: 公司当前亏损, 需关注盈利拐点")
        elif pe <= 15:
            bands["pe_band"] = "偏低"
            bands["notes"].append("PE(TTM) ≤15: 相对低估, 注意是否基本面恶化")
        elif pe <= 30:
            bands["pe_band"] = "中性"
        elif pe <= 60:
            bands["pe_band"] = "偏高"
            bands["notes"].append("PE(TTM) 30-60: 成长溢价, 需要增速支撑")
        else:
            bands["pe_band"] = "高估"
            bands["notes"].append("PE(TTM) >60: 估值高企, 波动风险大")
    if pb is not None:
        if pb <= 1:
            bands["pb_band"] = "破净"
        elif pb <= 3:
            bands["pb_band"] = "中性"
        elif pb <= 6:
            bands["pb_band"] = "偏高"
        else:
            bands["pb_band"] = "高估"
    return bands


def build_rating(signal: Dict, technicals: Dict, valuation: Dict,
                 capital: Optional[Dict] = None) -> Dict:
    """综合评级: 以机会模型信号为主, 技术面/估值/资金流微调。

    Args:
        signal: _deterministic_trade_signal 输出 (action/confidence/score/buy区间/reason)
        technicals: compute_technicals 输出
        valuation: valuation_band 输出
        capital: 资金流 {main_net, direction}
    """
    action = str(signal.get("action", "hold"))
    score = float(signal.get("potential_score", 0) or 0)
    confidence = float(signal.get("confidence", 0) or 0)
    reasons: List[str] = []
    adj = 0.0  # 微调分

    if action == "buy":
        base = 72.0
        reasons.append("机会模型进入买入区: " + (signal.get("reason", "") or "")[:80])
    elif action == "sell":
        base = 32.0
        reasons.append("持仓出现卖出信号: " + (signal.get("reason", "") or "")[:80])
    else:
        base = 50.0
        reasons.append("多因子未形成一致结论, 观望等待")

    # 技术面微调
    rsi = technicals.get("rsi14")
    if rsi is not None:
        if rsi >= 80:
            adj -= 6
            reasons.append(f"RSI={rsi:.0f} 超买, 追高风险大")
        elif rsi <= 20:
            adj += 3 if action != "sell" else 0
            reasons.append(f"RSI={rsi:.0f} 超卖, 或现反弹机会")
    percentile = technicals.get("price_percentile")
    if percentile is not None:
        if percentile >= 0.9 and action == "buy":
            adj -= 4
            reasons.append("价格处历史高位区间, 回调风险")
        elif percentile <= 0.2 and action != "sell":
            adj += 2
            reasons.append("价格处历史低位区间")

    # 估值微调
    if action == "buy" and valuation.get("pe_band") in ("高估", "偏高"):
        adj -= 3
        reasons.append(f"估值{valuation.get('pe_band')}, 安全边际不足")

    # 资金流微调
    if capital:
        main_net = _safe_float(capital.get("main_net"))
        direction = str(capital.get("direction", ""))
        if main_net > 0 and action == "buy":
            adj += 3
            reasons.append("主力资金净流入, 资金面配合")
        elif main_net < 0 and action == "buy":
            adj -= 3
            reasons.append("主力资金净流出, 需谨慎")

    final_score = round(max(0, min(100, base + adj)), 0)
    # 评级判定
    if final_score >= 65 and action == "buy":
        final_action = "buy"
    elif final_score <= 40 or action == "sell":
        final_action = "sell"
    elif final_score >= 55:
        final_action = "watch"
    else:
        final_action = "hold"

    return {
        "action": final_action,
        "score": int(final_score),
        "confidence": round(min(confidence + 0.05, 0.95), 2),
        "reasons": reasons[:6],
        "base_action": action,
    }


def build_tearsheet(*, symbol: str, name: str, quote: Dict, closes: List[float],
                    volumes: Optional[List[float]] = None, signal: Dict,
                    capital: Optional[Dict] = None, market_session: str = "") -> Dict:
    """组装完整速览卡。所有入参由调用方提供。

    Args:
        symbol: 规范化代码 (sh/sz/bj 前缀)
        name: 中文名称
        quote: 实时行情 {price, change_pct, pe_ttm, pb, mcap_yi, turnover_pct, vol_ratio, amount}
        closes: 日K收盘序列 (旧→新)
        volumes: 日K成交量序列
        signal: _deterministic_trade_signal 输出
        capital: 资金流 {main_net, direction}
        market_session: 市场状态标签
    """
    price = _safe_float(quote.get("price"))
    technicals = compute_technicals(closes, volumes)
    valuation = valuation_band(
        _safe_float(quote.get("pe_ttm")) or None,
        _safe_float(quote.get("pb")) or None,
    )
    rating = build_rating(signal, technicals, valuation, capital)

    return {
        "symbol": symbol,
        "name": name,
        "price": round(price, 2) if price else None,
        "change_pct": round(_safe_float(quote.get("change_pct")), 2),
        "market_cap_yi": round(_safe_float(quote.get("mcap_yi")), 1),
        "pe_ttm": round(_safe_float(quote.get("pe_ttm")), 1) if quote.get("pe_ttm") else None,
        "pb": round(_safe_float(quote.get("pb")), 2) if quote.get("pb") else None,
        "turnover_pct": _safe_float(quote.get("turnover_pct")),
        "vol_ratio": _safe_float(quote.get("vol_ratio")),
        "amount_yi": round(_safe_float(quote.get("amount")) / 1e8, 2) if quote.get("amount") else None,
        "technicals": technicals,
        "valuation": valuation,
        "trade_zone": {
            "buy_low": round(_safe_float(signal.get("buy_low")), 2) or None,
            "buy_high": round(_safe_float(signal.get("buy_high")), 2) or None,
            "stop_loss": round(_safe_float(signal.get("stop_loss")), 2) or None,
            "target_price": round(_safe_float(signal.get("target_price")), 2) or None,
            "risk_reward": round(_safe_float(signal.get("risk_reward")), 2) or None,
            "suggested_qty": int(signal.get("suggested_qty", 0) or 0),
        },
        "rating": rating,
        "capital_flow": {
            "main_net": round(_safe_float((capital or {}).get("main_net")), 2),
            "direction": (capital or {}).get("direction", "unknown"),
        },
        "market_session": market_session,
        "generated_at": __import__("datetime").datetime.now().isoformat(),
    }
