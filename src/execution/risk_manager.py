"""
风险管理器
=========

职责:
  - 仓位大小限制 (max_position_size)
  - 总回撤限制 (max_drawdown)
  - 单日亏损限制 (max_daily_loss)
  - 单笔止损/止盈比例 (stop_loss / take_profit)
  - 行业 / 单股 / 总仓位集中度 (concentration limits)
  - 交易前风控 (check_order): 综合以上所有规则给出 go/no-go
"""
from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    portfolio_value: float = 0
    current_position_value: float = 0
    current_symbol_value: float = 0
    current_industry_value: float = 0
    reason: str = ""


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str = ""
    suggested_quantity: int = 0
    violations: List[str] = field(default_factory=list)


class RiskManager:
    """风险管理器类"""

    def __init__(
        self,
        max_position_size: float = 0.10,
        max_drawdown: float = 0.20,
        stop_loss: float = 0.05,
        take_profit: float = 0.10,
        max_daily_loss: float = 0.02,
        max_total_position: float = 0.95,
        max_single_industry_pct: float = 0.30,
        max_orders_per_day: int = 100,
    ):
        """
        Args:
            max_position_size: 单股最大仓位占总资产的比例
            max_drawdown: 最大回撤限制
            stop_loss: 单笔止损比例
            take_profit: 单笔止盈比例
            max_daily_loss: 单日最大亏损占总资产比例
            max_total_position: 总仓位上限
            max_single_industry_pct: 单行业最大集中度
            max_orders_per_day: 单日最大成交笔数
        """
        self.max_position_size = max_position_size
        self.max_drawdown = max_drawdown
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.max_daily_loss = max_daily_loss
        self.max_total_position = max_total_position
        self.max_single_industry_pct = max_single_industry_pct
        self.max_orders_per_day = max_orders_per_day

        self.daily_pnl = 0.0
        self.peak_equity = 0.0
        self.current_equity = 0.0
        self.daily_order_count = 0
        self._current_date: Optional[datetime] = None
        self._order_history: List[Dict] = []
        self._industry_map: Dict[str, str] = {}
        self._blocked_until: Optional[datetime] = None

    def bind_industry(self, symbol: str, industry: str):
        self._industry_map[symbol] = industry

    def check_position_size(
        self, symbol: str, shares: int, price: float, portfolio_value: float
    ) -> bool:
        position_value = shares * price
        position_pct = position_value / max(portfolio_value, 1)
        return position_pct <= self.max_position_size

    def check_drawdown(self, current_equity: float) -> bool:
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        if self.peak_equity <= 0:
            return True
        drawdown = (self.peak_equity - current_equity) / self.peak_equity
        return drawdown <= self.max_drawdown

    def check_stop_loss(self, entry_price: float, current_price: float) -> bool:
        loss = (entry_price - current_price) / entry_price
        return loss >= self.stop_loss

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        profit = (current_price - entry_price) / entry_price
        return profit >= self.take_profit

    def calculate_position_size_with_risk(
        self, price: float, portfolio_value: float, risk_per_trade: float = 0.01
    ) -> int:
        max_risk = portfolio_value * risk_per_trade
        stop_loss_distance = price * self.stop_loss
        if stop_loss_distance <= 0:
            return 0
        shares = int(max_risk / stop_loss_distance)
        max_shares = int(portfolio_value * self.max_position_size / price)
        return min(shares, max_shares)

    def update_daily_pnl(self, pnl: float):
        self.daily_pnl += pnl

    def check_daily_loss_limit(self, portfolio_value: float) -> bool:
        if portfolio_value <= 0:
            return False
        daily_loss_pct = abs(min(0, self.daily_pnl)) / portfolio_value
        return daily_loss_pct <= self.max_daily_loss

    def reset_daily_pnl(self, today: Optional[datetime] = None):
        today = today or datetime.now()
        if self._current_date is None or self._current_date.date() != today.date():
            self._current_date = today
            self.daily_pnl = 0.0
            self.daily_order_count = 0

    def check_order(self, order: OrderRequest) -> RiskCheckResult:
        """交易前风控综合检查"""
        violations: List[str] = []
        if self._blocked_until and datetime.now() < self._blocked_until:
            return RiskCheckResult(
                allowed=False,
                reason=f"风控锁定中,解锁时间 {self._blocked_until.isoformat()}",
                violations=["LOCKED"],
            )

        self.reset_daily_pnl()
        if self.daily_order_count >= self.max_orders_per_day:
            violations.append(f"单日下单数 {self.daily_order_count} >= {self.max_orders_per_day}")

        if order.side == OrderSide.BUY:
            if order.portfolio_value > 0 and self.current_equity > 0:
                if not self.check_drawdown(self.current_equity):
                    violations.append(
                        f"回撤 {(self.peak_equity - self.current_equity) / self.peak_equity:.2%} > {self.max_drawdown:.0%}"
                    )
                if not self.check_daily_loss_limit(self.current_equity):
                    violations.append(
                        f"日亏 {abs(self.daily_pnl) / self.current_equity:.2%} > {self.max_daily_loss:.0%}"
                    )

            new_position_value = order.quantity * order.price
            post_trade_symbol_value = order.current_symbol_value + new_position_value
            if order.portfolio_value > 0:
                if post_trade_symbol_value / order.portfolio_value > self.max_position_size:
                    violations.append(
                        f"单股仓位 {post_trade_symbol_value / order.portfolio_value:.2%} > {self.max_position_size:.0%}"
                    )
                total_exposure = order.current_position_value + new_position_value
                if total_exposure / order.portfolio_value > self.max_total_position:
                    violations.append(
                        f"总仓位 {total_exposure / order.portfolio_value:.2%} > {self.max_total_position:.0%}"
                    )
                if self._industry_map:
                    industry = self._industry_map.get(order.symbol)
                    if industry:
                        violations_check = self._check_industry(
                            industry,
                            order.current_industry_value + new_position_value,
                            order.portfolio_value,
                        )
                        if violations_check:
                            violations.append(violations_check)
            if not self.check_position_size(
                order.symbol,
                int(post_trade_symbol_value / order.price),
                order.price,
                order.portfolio_value,
            ):
                violations.append("单股仓位超限")

        if violations:
            return RiskCheckResult(
                allowed=False,
                reason="; ".join(violations),
                violations=violations,
                suggested_quantity=self._suggest_qty(order, violations),
            )
        return RiskCheckResult(allowed=True, suggested_quantity=order.quantity)

    def _check_industry(self, industry: str, new_value: float, portfolio_value: float) -> Optional[str]:
        if portfolio_value <= 0:
            return None
        same_industry_value = new_value
        if same_industry_value / portfolio_value > self.max_single_industry_pct:
            return f"行业 {industry} 集中度 {same_industry_value / portfolio_value:.2%} > {self.max_single_industry_pct:.0%}"
        return None

    def _suggest_qty(self, order: OrderRequest, violations: List[str]) -> int:
        if order.price <= 0 or order.portfolio_value <= 0:
            return 0
        symbol_room = max(
            order.portfolio_value * self.max_position_size - order.current_symbol_value,
            0,
        )
        total_room = max(
            order.portfolio_value * self.max_total_position - order.current_position_value,
            0,
        )
        max_qty_by_size = int(min(symbol_room, total_room) / order.price)
        if order.side == OrderSide.BUY:
            return max(0, max_qty_by_size // 100 * 100)
        return order.quantity

    def lock(self, minutes: int = 0, hours: int = 0):
        from datetime import timedelta
        delta = timedelta(minutes=minutes, hours=hours)
        self._blocked_until = datetime.now() + delta

    def unlock(self):
        self._blocked_until = None

    def record_order(self, order: Dict):
        self._order_history.append({"time": datetime.now().isoformat(), **order})
        self.daily_order_count += 1
        if len(self._order_history) > 1000:
            self._order_history = self._order_history[-1000:]

    def get_risk_report(self) -> Dict:
        return {
            "current_equity": self.current_equity,
            "peak_equity": self.peak_equity,
            "drawdown": (self.peak_equity - self.current_equity) / max(self.peak_equity, 1),
            "daily_pnl": self.daily_pnl,
            "daily_order_count": self.daily_order_count,
            "blocked": self._blocked_until is not None and datetime.now() < self._blocked_until,
            "limits": {
                "max_position_size": self.max_position_size,
                "max_drawdown": self.max_drawdown,
                "stop_loss": self.stop_loss,
                "take_profit": self.take_profit,
                "max_daily_loss": self.max_daily_loss,
                "max_total_position": self.max_total_position,
                "max_orders_per_day": self.max_orders_per_day,
            },
        }
