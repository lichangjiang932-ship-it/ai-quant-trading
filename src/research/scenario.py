"""事件与情景分析 (Scenario) — 关键事件的 what-if 价格影响评估。

基于历史波动率与事件类型模板, 给出基准/乐观/悲观三种情景。
数据由调用方注入 (当前价、波动率、历史区间), 模块只做计算。
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 事件类型 → 历史经验波动幅度 (±%), 模板可扩展
EVENT_TEMPLATES: Dict[str, Dict] = {
    "earnings_beat":    {"label": "业绩超预期",   "impact": 0.06, "note": "财报超预期通常带动 2-3 日行情"},
    "earnings_miss":    {"label": "业绩不及预期", "impact": -0.08, "note": "财报不及预期易引发恐慌性抛售"},
    "limit_up":         {"label": "涨停打开",     "impact": 0.05, "note": "涨停后次日波动加大, 注意承接"},
    "breakdown":        {"label": "放量破位",     "impact": -0.06, "note": "跌破关键支撑后趋势可能延续"},
    "breakout":         {"label": "放量突破",     "impact": 0.06, "note": "突破平台若量能配合可看高一线"},
    "policy":           {"label": "政策利好",     "impact": 0.05, "note": "行业政策催化, 持续性看落地节奏"},
    "policy_bad":       {"label": "政策利空",     "impact": -0.05, "note": "监管收紧短期承压"},
    "major_holder":     {"label": "大股东增减持", "impact": -0.04, "note": "减持公告短期压制情绪"},
    "buyback":          {"label": "回购/增持",    "impact": 0.03, "note": "回购彰显信心, 温和利好"},
    "custom":           {"label": "自定义事件",   "impact": 0.05, "note": "按自定义幅度评估"},
}

SCENARIO_STEPS = {
    "optimistic": ("乐观情景", 1.5),
    "base":       ("基准情景", 0.5),
    "pessimistic":("悲观情景", -0.5),
}


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_scenario(*, symbol: str, name: str, price: float, event: str,
                   volatility_pct: Optional[float] = None,
                   custom_impact: Optional[float] = None,
                   daily_range: Optional[Dict] = None) -> Dict:
    """构建事件情景分析。

    Args:
        price: 当前价
        event: EVENT_TEMPLATES 的 key, 或 'custom'
        volatility_pct: 年化波动率% (来自技术面)
        custom_impact: 自定义涨跌幅度 (如 0.10 = ±10%)
        daily_range: {high, low, pre_close} 当日区间
    """
    template = EVENT_TEMPLATES.get(event)
    if not template:
        template = EVENT_TEMPLATES["custom"]
    impact = custom_impact if custom_impact is not None else template["impact"]

    # 用年化波动率估算单日波动 (可选, 兜底 5%)
    vol = volatility_pct or 5.0
    day_vol = max(vol / 100 / 16, 0.01)  # 年化/√252 近似单日

    scenarios = []
    for key, (label, weight) in SCENARIO_STEPS.items():
        delta = impact * weight
        target = price * (1 + delta)
        prob_label = "45%" if key == "base" else ("25%" if key == "optimistic" else "30%")
        scenarios.append({
            "key": key,
            "label": label,
            "target_price": round(target, 2),
            "change_pct": round(delta * 100, 2),
            "prob": prob_label,
        })

    # 当日技术参考位
    ref = {}
    if daily_range:
        ref = {
            "today_high": round(_safe_float(daily_range.get("high")), 2),
            "today_low": round(_safe_float(daily_range.get("low")), 2),
            "pre_close": round(_safe_float(daily_range.get("pre_close")), 2),
        }

    advice = _scenario_advice(template["impact"], scenarios)
    return {
        "symbol": symbol,
        "name": name,
        "price": round(price, 2),
        "event_key": event,
        "event_label": template["label"],
        "event_note": template["note"],
        "day_volatility_est": round(day_vol * 100, 2),
        "scenarios": scenarios,
        "daily_ref": ref,
        "advice": advice,
    }


def _scenario_advice(impact: float, scenarios: List[Dict]) -> Dict:
    """按事件方向给操作倾向 (仅供研究参考)。"""
    base = next((s for s in scenarios if s["key"] == "base"), None)
    if impact > 0.04:
        action, color = "关注回踩买入", "buy"
    elif impact < -0.04:
        action, color = "回避或减仓", "sell"
    elif impact > 0:
        action, color = "中性偏多", "watch"
    else:
        action, color = "中性", "hold"
    return {"action": action, "tone": color,
            "note": "情景为统计参考, 非确定性预测; 实际走势受大盘与个股基本面影响"}
