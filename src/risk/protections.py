# -*- coding: utf-8 -*-
"""交易保护机制 (借鉴 Freqtrade Protections, 46.9k stars)。

四道防线:
  1. CooldownPeriod   冷却期: 卖出后 N 天内禁止买回同一只 (防反复接刀)
  2. StoplossGuard    连亏熔断: 滚动窗口内止损次数超限 → 全局暂停开仓 X 天
  3. MaxDrawdownGuard 回撤熔断: 组合净值从峰值回撤超阈值 → 暂停开仓
  4. TrailingStop     追踪止损: 浮盈达到激活线后, 从持仓最高点回撤超阈值即离场

另含 ATR 波动率仓位 sizing (借鉴 Freqtrade custom_stake_amount / vnpy 风控):
  qty = (总资产 × 单笔风险%) / (入场价 - 止损价), 止损距离 = ATR × 倍数

纯函数实现, 不依赖 broker / 网络, 方便单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd


# ─────────────────────────── 配置 ───────────────────────────

@dataclass
class ProtectionConfig:
    # CooldownPeriod: 卖出后冷却天数
    cooldown_days: int = 3
    # StoplossGuard: 滚动 lookback 天内止损 ≥ stoploss_max_count 次 → 暂停 pause_days 天
    stoploss_lookback_days: int = 5
    stoploss_max_count: int = 2
    stoploss_pause_days: int = 2
    # MaxDrawdownGuard: 净值从峰值回撤超过该比例 → 暂停开仓 (直到回到阈值内)
    max_drawdown_pct: float = 0.08
    # TrailingStop: 浮盈 ≥ activate 后启用; 从最高点回撤 ≥ drawdown 即离场
    trail_activate_pct: float = 0.04
    trail_drawdown_pct: float = 0.03
    # ATR sizing: 单笔风险占组合比例; 止损距离 = atr × atr_stop_mult
    atr_risk_pct: float = 0.01
    atr_stop_mult: float = 2.0
    atr_period: int = 14


DEFAULT_CONFIG = ProtectionConfig()


def load_protection_config(autotrade_cfg: dict) -> ProtectionConfig:
    """从 config.yaml autotrade.protections 段加载, 缺省用默认值。"""
    cfg = dict((autotrade_cfg or {}).get('protections') or {})
    known = {f for f in ProtectionConfig().__dataclass_fields__}
    clean = {}
    for k, v in cfg.items():
        if k in known:
            try:
                clean[k] = type(getattr(DEFAULT_CONFIG, k))(v)
            except (TypeError, ValueError):
                pass
    return ProtectionConfig(**clean)


# ─────────────────────────── 工具 ───────────────────────────

def parse_date(s) -> Optional[date]:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _recent_trades(trades: List[dict], now: date, lookback_days: int) -> List[dict]:
    """取最近 lookback 天的交易记录。trades 元素需有 side/date 字段。"""
    out = []
    for t in trades or []:
        d = parse_date(t.get('date'))
        if d and timedelta(0) <= now - d <= timedelta(days=lookback_days):
            out.append(t)
    return out


# ─────────────────────── 1+2: 买入前检查 ───────────────────────

def cooldown_block_reason(symbol: str, trades: List[dict], now: date,
                          cfg: ProtectionConfig = DEFAULT_CONFIG) -> Optional[str]:
    """该股最近卖出过且仍在冷却期内 → 返回原因, 否则 None。

    匹配规则: 最近一条针对该 symbol 的 sell 记录距今 < cooldown_days。
    """
    latest_sell = None
    for t in trades or []:
        if t.get('symbol') == symbol and str(t.get('side')) == 'sell':
            d = parse_date(t.get('date'))
            if d and (latest_sell is None or d > latest_sell):
                latest_sell = d
    if latest_sell is None:
        return None
    elapsed = (now - latest_sell).days
    if elapsed < max(cfg.cooldown_days, 0):
        return f"冷却期: {latest_sell.isoformat()} 刚卖出, 需等 {cfg.cooldown_days} 天 (已过 {elapsed} 天)"
    return None


def stoploss_guard_pause(trades: List[dict], now: date,
                         cfg: ProtectionConfig = DEFAULT_CONFIG) -> Optional[str]:
    """滚动窗口内止损次数超限 → 返回全局暂停原因, 否则 None。

    止损识别: sell 记录 reason 含 '止损'/'回撤'/'破位'。
    """
    stops = []
    for t in _recent_trades(trades, now, cfg.stoploss_lookback_days):
        reason = str(t.get('reason', ''))
        if str(t.get('side')) == 'sell' and any(k in reason for k in ('止损', '回撤', '破位')):
            d = parse_date(t.get('date'))
            if d:
                stops.append(d)
    if len(stops) >= max(cfg.stoploss_max_count, 1):
        last = max(stops)
        until = last + timedelta(days=cfg.stoploss_pause_days)
        if now <= until:
            return (
                f"连亏熔断: 近{cfg.stoploss_lookback_days}天止损{len(stops)}次"
                f"(≥{cfg.stoploss_max_count}), 暂停开仓至 {until.isoformat()}"
            )
    return None


def drawdown_guard_pause(total_asset: float, peak_equity: float,
                         cfg: ProtectionConfig = DEFAULT_CONFIG) -> Optional[str]:
    """组合回撤超阈值 → 返回暂停开仓原因, 否则 None。"""
    if peak_equity <= 0 or total_asset <= 0:
        return None
    dd = (peak_equity - total_asset) / peak_equity
    if dd >= max(cfg.max_drawdown_pct, 0.01):
        return f"回撤熔断: 组合自峰值回撤 {dd:.1%} (≥{cfg.max_drawdown_pct:.0%}), 暂停新开仓"
    return None


# ─────────────────────── 3: 追踪止损 ───────────────────────

@dataclass
class TrailingState:
    highs: Dict[str, float] = field(default_factory=dict)  # symbol → 持仓期最高价


def update_trailing_high(state: TrailingState, symbol: str, price: float) -> float:
    """更新持仓最高价, 返回当前 high。"""
    price = float(price or 0)
    if price <= 0:
        return state.highs.get(symbol, 0.0)
    old = state.highs.get(symbol, 0.0)
    new = max(old, price)
    state.highs[symbol] = new
    return new


def trailing_stop_hit(avg_cost: float, high: float, price: float,
                      cfg: ProtectionConfig = DEFAULT_CONFIG) -> Optional[str]:
    """追踪止损判定: 浮盈曾达激活线且回撤达标 → 返回离场原因, 否则 None。"""
    avg_cost = float(avg_cost or 0)
    high = float(high or 0)
    price = float(price or 0)
    if min(avg_cost, high, price) <= 0:
        return None
    gain_at_high = (high - avg_cost) / avg_cost
    pullback = (high - price) / high
    if gain_at_high >= cfg.trail_activate_pct and pullback >= cfg.trail_drawdown_pct:
        return (f"追踪止盈离场: 最高 {high:.2f}(浮盈{gain_at_high:.1%}) "
                f"现价 {price:.2f}, 自高点回落 {pullback:.1%}")
    return None


# ─────────────────────── 4: ATR 仓位 sizing ───────────────────────

def compute_atr(history, period: int = 14) -> float:
    """Average True Range (Wilder 平滑)。history 需含 High/Low/Close 列。

    数据不足 (< period+1 行) 或列缺失时返回 0, 调用方应跳过 sizing。
    """
    try:
        h = history['High'].astype(float)
        l = history['Low'].astype(float)
        c = history['Close'].astype(float)
    except (KeyError, AttributeError, TypeError):
        return 0.0
    n = len(c)
    if n < period + 1:
        return 0.0
    prev_close = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    return float(atr) if atr == atr else 0.0  # NaN guard


def atr_position_qty(price: float, atr: float, total_asset: float,
                     cfg: ProtectionConfig = DEFAULT_CONFIG) -> int:
    """按波动率定仓位: qty = 风险额 / 每股止损距离, 向下取整到 100 股。

    止损距离/股 = ATR × 停损倍数 → 波动大的股票自动拿更少股数。
    返回 0 表示数据不足或算不出合理手数, 调用方应回退固定预算逻辑。
    """
    price = float(price or 0)
    atr = float(atr or 0)
    if price <= 0 or atr <= 0 or total_asset <= 0:
        return 0
    risk_amount = total_asset * max(cfg.atr_risk_pct, 0.0005)
    stop_distance_per_share = atr * max(cfg.atr_stop_mult, 0.5)
    if stop_distance_per_share <= 0:
        return 0
    return max(int(risk_amount / stop_distance_per_share / 100) * 100, 0)
