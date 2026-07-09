"""
止损止盈监控器
==============

在实时行情驱动下,对所有持仓进行:
  - 固定比例止损 / 止盈
  - 移动止损(Trailing Stop)
  - 多档分级止盈(分批止盈)

触发后向策略信号队列推送强制平仓信号,由 engine 执行。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Callable, Awaitable


class TPSLReason(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    PARTIAL_TP = "partial_take_profit"


@dataclass
class TPSLConfig:
    """单笔持仓的 TP/SL 配置"""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None
    partial_tp_levels: List[float] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[Dict]) -> "TPSLConfig":
        d = d or {}
        return cls(
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            trailing_stop=d.get("trailing_stop"),
            partial_tp_levels=list(d.get("partial_tp_levels", [])),
        )


@dataclass
class TPSLEvent:
    symbol: str
    reason: TPSLReason
    entry_price: float
    current_price: float
    pnl_pct: float
    suggested_quantity: int
    triggered_at: float = field(default_factory=time.time)


class PositionTracker:
    """跟踪每笔持仓的最高价、已触发分档、是否已平仓"""

    def __init__(self, symbol: str, entry_price: float, quantity: int,
                 entry_time: float, config: TPSLConfig):
        self.symbol = symbol
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_time = entry_time
        self.config = config
        self.highest_price = entry_price
        self.lowest_price = entry_price
        self.partial_tp_triggered: List[float] = list(config.partial_tp_levels)
        self.triggered_partial: Dict[float, bool] = {lv: False for lv in config.partial_tp_levels}
        self.triggered_tp = False
        self.triggered_sl = False
        self.remaining_quantity = quantity

    def update_price(self, price: float):
        if price > self.highest_price:
            self.highest_price = price
        if price < self.lowest_price:
            self.lowest_price = price

    def get_trailing_stop_price(self) -> Optional[float]:
        if self.config.trailing_stop is None:
            return None
        return self.highest_price * (1.0 - self.config.trailing_stop)

    def evaluate(self, current_price: float) -> Optional[TPSLEvent]:
        """返回首个触发的事件;若已无剩余仓位则返回 None"""
        if self.remaining_quantity <= 0:
            return None

        pnl_pct = (current_price - self.entry_price) / self.entry_price

        if self.config.stop_loss is not None and pnl_pct <= -self.config.stop_loss:
            if not self.triggered_sl:
                self.triggered_sl = True
                return TPSLEvent(
                    symbol=self.symbol, reason=TPSLReason.STOP_LOSS,
                    entry_price=self.entry_price, current_price=current_price,
                    pnl_pct=pnl_pct, suggested_quantity=self.remaining_quantity,
                )

        if self.config.take_profit is not None and pnl_pct >= self.config.take_profit:
            if not self.triggered_tp:
                self.triggered_tp = True
                return TPSLEvent(
                    symbol=self.symbol, reason=TPSLReason.TAKE_PROFIT,
                    entry_price=self.entry_price, current_price=current_price,
                    pnl_pct=pnl_pct, suggested_quantity=self.remaining_quantity,
                )

        if self.config.partial_tp_levels:
            for level in sorted(self.config.partial_tp_levels):
                if pnl_pct >= level and not self.triggered_partial[level]:
                    self.triggered_partial[level] = True
                    sell_qty = max(int(self.remaining_quantity * 0.5), 0)
                    if sell_qty > 0:
                        return TPSLEvent(
                            symbol=self.symbol, reason=TPSLReason.PARTIAL_TP,
                            entry_price=self.entry_price, current_price=current_price,
                            pnl_pct=pnl_pct, suggested_quantity=sell_qty,
                        )

        if self.config.trailing_stop is not None:
            ts_price = self.highest_price * (1.0 - self.config.trailing_stop)
            if current_price <= ts_price and current_price > self.entry_price:
                return TPSLEvent(
                    symbol=self.symbol, reason=TPSLReason.TRAILING_STOP,
                    entry_price=self.entry_price, current_price=current_price,
                    pnl_pct=pnl_pct, suggested_quantity=self.remaining_quantity,
                )

        return None


class TPSLMonitor:
    """
    止损止盈监控器

    用法:
        monitor = TPSLMonitor(default_config=TPSLConfig(stop_loss=0.05))
        engine.async_engine.submit(monitor.run(), priority=TaskPriority.HIGH)

        # 持仓建立时
        monitor.register_position("sh600000", entry_price=10.0, quantity=1000)

        # 行情到达时
        events = monitor.on_quote("sh600000", price=10.5)
        for ev in events:
            await signal_queue.put(ev)
    """

    def __init__(
        self,
        default_config: Optional[TPSLConfig] = None,
        on_event: Optional[Callable[[TPSLEvent], Awaitable[None]]] = None,
    ):
        self.default_config = default_config or TPSLConfig()
        self._positions: Dict[str, PositionTracker] = {}
        self._on_event = on_event
        self._event_log: List[TPSLEvent] = []
        self._max_log = 10000
        self._total_triggered = 0
        self._trigger_breakdown: Dict[str, int] = {r.value: 0 for r in TPSLReason}

    def register_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        config: Optional[TPSLConfig] = None,
        entry_time: Optional[float] = None,
    ):
        cfg = config or self.default_config
        self._positions[symbol] = PositionTracker(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=entry_time or time.time(),
            config=cfg,
        )

    def update_position_qty(self, symbol: str, quantity: int):
        if symbol in self._positions:
            self._positions[symbol].remaining_quantity = quantity

    def unregister_position(self, symbol: str):
        self._positions.pop(symbol, None)

    def on_quote(self, symbol: str, price: float) -> List[TPSLEvent]:
        """行情到达时调用,返回触发的事件列表(可能多个分档)"""
        tracker = self._positions.get(symbol)
        if not tracker or price <= 0:
            return []
        tracker.update_price(price)
        events: List[TPSLEvent] = []
        while True:
            ev = tracker.evaluate(price)
            if ev is None:
                break
            events.append(ev)
            if ev.reason == TPSLReason.PARTIAL_TP:
                tracker.remaining_quantity = max(0, tracker.remaining_quantity - ev.suggested_quantity)
            else:
                tracker.remaining_quantity = 0
            self._record_event(ev)
        if tracker.remaining_quantity <= 0:
            self._positions.pop(symbol, None)
        return events

    def _record_event(self, ev: TPSLEvent):
        self._event_log.append(ev)
        if len(self._event_log) > self._max_log:
            self._event_log = self._event_log[-self._max_log:]
        self._total_triggered += 1
        self._trigger_breakdown[ev.reason.value] = self._trigger_breakdown.get(ev.reason.value, 0) + 1

    def get_positions(self) -> Dict[str, Dict]:
        return {
            sym: {
                "entry_price": p.entry_price,
                "remaining_quantity": p.remaining_quantity,
                "highest_price": p.highest_price,
                "lowest_price": p.lowest_price,
                "pnl_at_high": (p.highest_price - p.entry_price) / p.entry_price,
                "pnl_at_low": (p.lowest_price - p.entry_price) / p.entry_price,
                "config": p.config.__dict__,
                "triggered_tp": p.triggered_tp,
                "triggered_sl": p.triggered_sl,
            }
            for sym, p in self._positions.items()
        }

    def get_stats(self) -> Dict:
        return {
            "active_positions": len(self._positions),
            "total_triggered": self._total_triggered,
            "by_reason": dict(self._trigger_breakdown),
            "recent_events": [
                {
                    "symbol": e.symbol, "reason": e.reason.value,
                    "pnl_pct": round(e.pnl_pct, 4),
                    "qty": e.suggested_quantity, "at": e.triggered_at,
                }
                for e in self._event_log[-10:]
            ],
        }

    def reset(self):
        self._positions.clear()
        self._event_log.clear()
        self._total_triggered = 0
        self._trigger_breakdown = {r.value: 0 for r in TPSLReason}
