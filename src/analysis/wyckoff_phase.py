# -*- coding: utf-8 -*-
"""
威科夫量价阶段识别 (Wyckoff Phase Detector) — 轻量版
=======================================================
借鉴 WyckoffTradingAgent: 用日线 K 线近似识别经典量价结构阶段。

阶段:
  - accumulation 吸筹: 长期低位横盘 + 地量后温和放量 (潜在建仓)
  - spring 弹簧: 跌破前低快速收回 (假突破洗盘, 经典买点)
  - markup 拉升: 站上 MA20 + 成交量持续放大 (趋势主升)
  - distribution 派发: 高位放量滞涨 / 量价背离 (出货风险)
  - markdown 下跌: 跌破 MA20 且均线空头 (规避)

输出: {phase, confidence(0-1), note, detail}
"""
from typing import Dict

import numpy as np
import pandas as pd


def detect_phase(history: pd.DataFrame, price: float = 0.0) -> Dict:
    """用日线识别当前量价阶段。

    Args:
        history: DataFrame, 需含 Close/Volume/Open (索引为日期), 至少 60 根
        price: 当前价 (缺省用最后一根 Close)

    Returns:
        {"phase", "confidence", "note"}
    """
    empty = {"phase": "unknown", "confidence": 0.0, "note": "K线数据不足"}
    if history is None or history.empty or "Close" not in history.columns:
        return empty
    try:
        closes = [float(v) for v in history["Close"].tolist() if float(v) > 0]
        if len(closes) < 60:
            return empty
        volumes = [float(v) for v in history.get("Volume", pd.Series([0.0] * len(closes))).tolist()]
        prices = closes
        cur = float(price) if price > 0 else closes[-1]

        n = len(closes)
        ma20 = float(np.mean(closes[-20:]))
        ma60 = float(np.mean(closes[-60:])) if n >= 60 else ma20

        # 位置判断
        low_52 = min(closes[-120:]) if n >= 120 else min(closes[-60:])
        high_52 = max(closes[-120:]) if n >= 120 else max(closes[-60:])
        rng = max(high_52 - low_52, 1e-9)
        pos = (cur - low_52) / rng  # 0=低位 1=高位

        # 量能
        vol_avg20 = float(np.mean(volumes[-20:])) if volumes else 0
        vol_avg5 = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else vol_avg20
        vol_ratio = vol_avg5 / max(vol_avg20, 1e-9)  # >1 放量 <1 缩量
        vol_recent = float(volumes[-1]) / max(vol_avg20, 1e-9) if volumes else 0

        # 价格行为
        chg_20 = (closes[-1] / closes[-21] - 1) * 100 if n >= 21 else 0
        chg_60 = (closes[-1] / closes[-61] - 1) * 100 if n >= 61 else chg_20

        # 前低 / 突破检测 (Spring: 近10日跌破近60日最低点后快速收回)
        low_60 = min(closes[-60:])
        low_idx = closes[-60:].index(low_60) + (n - 60)
        spring = False
        if len(closes) >= 70 and n - low_idx <= 10:
            below_low = [c for c in closes[-15:-5] if c <= low_60 * 1.01]
            if below_low and closes[-1] > low_60 * 1.02:
                spring = True

        # 高位放量滞涨 (Distribution): 高位 + 放量 + 价格停滞
        stall = (len(closes) >= 15 and
                 abs(closes[-1] / closes[-6] - 1) < 0.03 and
                 closes[-1] > closes[-20] and vol_ratio > 1.15)

        # 决策
        if spring:
            phase = "spring"
            confidence = 0.8
            note = "跌破前低快速收回 (Spring), 经典洗盘买点"
        elif pos < 0.25 and abs(chg_60) < 8 and vol_ratio < 0.9:
            phase = "accumulation"
            confidence = 0.65
            note = "低位横盘缩量, 疑似吸筹"
        elif cur > ma20 and ma20 > ma60 and vol_ratio > 1.0 and chg_20 > 0:
            phase = "markup"
            confidence = 0.75 if vol_recent > 1.2 else 0.6
            note = "站上MA20且量能放大, 拉升阶段"
        elif pos > 0.6 and stall and vol_ratio > 1.15:
            phase = "distribution"
            confidence = 0.7
            note = "高位放量滞涨, 疑似派发"
        elif cur < ma20 and ma20 < ma60:
            phase = "markdown"
            confidence = 0.7
            note = "跌破MA20均线空头, 下跌阶段"
        else:
            phase = "unknown"
            confidence = 0.3
            note = "阶段特征不明显"

        return {
            "phase": phase,
            "confidence": round(confidence, 2),
            "note": note,
            "detail": {
                "pos": round(pos, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
                "vol_ratio": round(vol_ratio, 2), "chg_20": round(chg_20, 1),
                "chg_60": round(chg_60, 1), "spring": spring,
            },
        }
    except Exception:
        return empty


# 阶段 → 策略建议映射 (供买入筛选器使用)
PHASE_SCORE = {
    "spring": +8,          # 洗盘后买点
    "markup": +4,          # 拉升初/中段
    "accumulation": +2,    # 吸筹观察
    "unknown": 0,
    "markdown": -8,        # 下跌规避
    "distribution": -12,   # 派发否决级
}
