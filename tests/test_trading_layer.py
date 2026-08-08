"""交易层测试: src/trading 模块 + trade.py CLI + api_server 实盘端点。"""
import sys
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestFundTraderErrors:
    """基金交易错误处理 (凭证未初始化时给出友好指引)。"""

    def test_not_initialized_message(self):
        from src.trading.fund_trader import FundTrader, FundNotInitializedError
        trader = FundTrader()
        with pytest.raises(FundNotInitializedError):
            trader._get_work_token()

    def test_holding_initialized_error_propagates(self):
        from src.trading.fund_trader import FundTrader, FundNotInitializedError
        trader = FundTrader()
        with pytest.raises(FundNotInitializedError):
            trader.get_all_holdings()


class TestStockTraderConfig:
    """StockTrader 从 config 读取配置。"""

    def test_from_config_loads(self):
        from src.trading.stock_trader import StockTrader
        trader = StockTrader.from_config(
            config_path=os.path.join(ROOT, "config", "config.yaml")
        )
        assert trader.agent_token == ""
        assert trader.mcp_url == "https://mcp.guling.pro"
        assert trader.auto_trade is False

    def test_auto_trade_override(self):
        from src.trading.stock_trader import StockTrader
        trader = StockTrader.from_config(
            config_path=os.path.join(ROOT, "config", "config.yaml"),
            auto_trade=True,
        )
        assert trader.auto_trade is True


class TestTradeCli:
    """trade.py CLI 结构。"""

    def test_help_returns_zero(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "trade.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        assert "fund" in r.stdout and "stock" in r.stdout

    def test_fund_init_help(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "trade.py"), "fund", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        for cmd in ("holdings", "buy", "redeem", "orders", "revoke", "init"):
            assert cmd in r.stdout


class TestLiveApiEndpoints:
    """api_server 实盘端点注册与错误处理。"""

    def _client(self):
        from fastapi.testclient import TestClient
        import frontend.api_server as api
        return TestClient(api.app)

    def test_live_status_registered(self):
        r = self._client().get("/api/live/status")
        assert r.status_code == 200
        assert "connected" in r.json().get("data", {})

    def test_live_order_blocked_without_auto_trade(self):
        r = self._client().post(
            "/api/live/order",
            json={"symbol": "600519", "side": "buy", "quantity": 100},
        )
        body = r.json()
        assert body.get("success") is False
        assert "auto_trade" in str(body.get("message", ""))

    def test_live_order_validation(self):
        r = self._client().post(
            "/api/live/order",
            json={"symbol": "", "side": "buy", "quantity": 0},
        )
        assert r.json().get("success") is False

    def test_fund_buy_validation(self):
        r = self._client().post(
            "/api/fund/buy",
            json={"fund_code": "", "amount": 0},
        )
        assert r.json().get("success") is False

    def test_fund_holdings_not_initialized_message(self):
        r = self._client().get("/api/fund/holdings")
        body = r.json()
        assert body.get("success") is False
        assert "INIT_TOKEN" in str(body.get("error", ""))
