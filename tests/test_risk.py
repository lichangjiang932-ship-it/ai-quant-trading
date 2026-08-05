"""
风险管理器与止损止盈监控器测试
"""
import os
import sys
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.risk_manager import (
    RiskManager, OrderRequest, OrderSide, RiskCheckResult
)
from src.execution.tpsl_monitor import (
    TPSLMonitor, TPSLConfig, TPSLReason, PositionTracker, TPSLEvent
)


class TestRiskManager:
    def test_init_defaults(self):
        rm = RiskManager()
        assert rm.max_position_size == 0.10
        assert rm.max_drawdown == 0.20
        assert rm.stop_loss == 0.05
        assert rm.take_profit == 0.10

    def test_check_position_size_within_limit(self):
        rm = RiskManager(max_position_size=0.10)
        assert rm.check_position_size("x", 1000, 10.0, 1_000_000) is True

    def test_check_position_size_exceeds_limit(self):
        rm = RiskManager(max_position_size=0.10)
        assert rm.check_position_size("x", 20000, 10.0, 1_000_000) is False

    def test_check_drawdown_ok(self):
        rm = RiskManager(max_drawdown=0.20)
        rm.current_equity = 950_000
        rm.peak_equity = 1_000_000
        assert rm.check_drawdown(950_000) is True

    def test_check_drawdown_breach(self):
        rm = RiskManager(max_drawdown=0.20)
        rm.current_equity = 700_000
        rm.peak_equity = 1_000_000
        assert rm.check_drawdown(700_000) is False

    def test_check_order_buy_ok(self):
        rm = RiskManager()
        rm.current_equity = 1_000_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()
        req = OrderRequest(symbol="x", side=OrderSide.BUY, quantity=1000, price=10.0,
                           portfolio_value=1_000_000)
        r = rm.check_order(req)
        assert r.allowed is True

    def test_check_order_blocked_by_position_size(self):
        rm = RiskManager(max_position_size=0.10)
        rm.current_equity = 1_000_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()
        req = OrderRequest(symbol="x", side=OrderSide.BUY, quantity=20000, price=10.0,
                           portfolio_value=1_000_000)
        r = rm.check_order(req)
        assert r.allowed is False
        assert any("仓位" in v for v in r.violations)

    def test_check_order_blocked_by_drawdown(self):
        rm = RiskManager(max_drawdown=0.20)
        rm.current_equity = 700_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()
        req = OrderRequest(symbol="x", side=OrderSide.BUY, quantity=100, price=10.0,
                           portfolio_value=700_000)
        r = rm.check_order(req)
        assert r.allowed is False
        assert any("回撤" in v for v in r.violations)

    def test_check_order_blocked_by_daily_loss(self):
        rm = RiskManager(max_daily_loss=0.02)
        rm.current_equity = 1_000_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()
        rm.update_daily_pnl(-30_000)
        req = OrderRequest(symbol="x", side=OrderSide.BUY, quantity=100, price=10.0,
                           portfolio_value=1_000_000)
        r = rm.check_order(req)
        assert r.allowed is False
        assert any("日亏" in v for v in r.violations)

    def test_calculate_position_size_with_risk(self):
        rm = RiskManager(max_position_size=0.10, stop_loss=0.05)
        qty = rm.calculate_position_size_with_risk(10.0, 1_000_000, risk_per_trade=0.01)
        assert 0 < qty <= 10_000

    def test_reset_daily_pnl(self):
        rm = RiskManager()
        rm.reset_daily_pnl()
        rm.update_daily_pnl(-100)
        rm.reset_daily_pnl()
        assert rm.daily_pnl == -100

    def test_lock_unlock(self):
        rm = RiskManager()
        rm.current_equity = 1_000_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()
        rm.lock(minutes=10)
        req = OrderRequest(symbol="x", side=OrderSide.BUY, quantity=100, price=10.0,
                           portfolio_value=1_000_000)
        r = rm.check_order(req)
        assert r.allowed is False
        rm.unlock()
        r = rm.check_order(req)
        assert r.allowed is True

    def test_risk_report(self):
        rm = RiskManager()
        rm.current_equity = 800_000
        rm.peak_equity = 1_000_000
        report = rm.get_risk_report()
        assert "drawdown" in report
        assert "limits" in report
        assert report["limits"]["stop_loss"] == "5.0%"
        assert report["limits"]["take_profit"] == "10.0%"


class TestTPSLMonitor:
    def test_register_position(self):
        m = TPSLMonitor(default_config=TPSLConfig(stop_loss=0.05))
        m.register_position("x", 10.0, 1000)
        pos = m.get_positions()
        assert "x" in pos
        assert pos["x"]["remaining_quantity"] == 1000

    def test_stop_loss_trigger(self):
        cfg = TPSLConfig(stop_loss=0.05)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        events = m.on_quote("x", 9.4)
        assert len(events) == 1
        assert events[0].reason == TPSLReason.STOP_LOSS
        assert events[0].suggested_quantity == 1000

    def test_take_profit_trigger(self):
        cfg = TPSLConfig(take_profit=0.10)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        events = m.on_quote("x", 11.1)
        assert len(events) == 1
        assert events[0].reason == TPSLReason.TAKE_PROFIT

    def test_trailing_stop_trigger(self):
        cfg = TPSLConfig(trailing_stop=0.03)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        m.on_quote("x", 11.0)
        events = m.on_quote("x", 10.65)
        assert len(events) == 1
        assert events[0].reason == TPSLReason.TRAILING_STOP

    def test_partial_tp_levels(self):
        cfg = TPSLConfig(partial_tp_levels=[0.05, 0.10])
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        ev1 = m.on_quote("x", 10.6)
        assert len(ev1) == 1
        assert ev1[0].reason == TPSLReason.PARTIAL_TP
        assert ev1[0].suggested_quantity == 500

    def test_no_trigger_above_threshold(self):
        cfg = TPSLConfig(stop_loss=0.05, take_profit=0.10)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        events = m.on_quote("x", 10.5)
        assert len(events) == 0

    def test_position_removed_after_close(self):
        cfg = TPSLConfig(stop_loss=0.05)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        m.on_quote("x", 9.0)
        assert "x" not in m.get_positions()

    def test_update_position_qty(self):
        cfg = TPSLConfig(partial_tp_levels=[0.05])
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        m.update_position_qty("x", 500)
        assert m.get_positions()["x"]["remaining_quantity"] == 500

    def test_stats(self):
        cfg = TPSLConfig(stop_loss=0.05)
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        m.on_quote("x", 9.0)
        stats = m.get_stats()
        assert stats["total_triggered"] == 1
        assert stats["by_reason"]["stop_loss"] == 1

    def test_unregister_position(self):
        cfg = TPSLConfig()
        m = TPSLMonitor(default_config=cfg)
        m.register_position("x", 10.0, 1000)
        m.unregister_position("x")
        assert "x" not in m.get_positions()
