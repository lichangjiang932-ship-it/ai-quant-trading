"""交易层测试: src/trading 模块 + trade.py CLI + api_server 实盘端点。"""
import sys
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestFundTraderErrors:
    """基金交易错误处理 (用 monkeypatch 模拟凭证缺失, 不依赖本机凭证状态)。"""

    def test_not_initialized_message(self, monkeypatch):
        from src.trading.fund_trader import FundTrader, FundNotInitializedError

        def _fake_get_work_token(self):
            raise FundNotInitializedError(
                "爱基金凭证未初始化: 请先执行 ... init('你的INIT_TOKEN')"
            )

        monkeypatch.setattr(FundTrader, "_get_work_token", _fake_get_work_token)
        trader = FundTrader()
        with pytest.raises(FundNotInitializedError):
            trader._get_work_token()

    def test_holding_initialized_error_propagates(self, monkeypatch):
        from src.trading.fund_trader import FundTrader, FundNotInitializedError

        def _fake_get_work_token(self):
            raise FundNotInitializedError("爱基金凭证未初始化")

        monkeypatch.setattr(FundTrader, "_get_work_token", _fake_get_work_token)
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

    def test_account_default_paper(self):
        r = self._client().get("/api/account")
        assert r.json().get("mode") == "paper"

    def test_account_mode_live(self):
        r = self._client().get("/api/account?mode=live")
        body = r.json()
        assert body.get("mode") == "live"
        assert "fund" in body and "stock" in body

    def test_accounts_dual(self):
        r = self._client().get("/api/accounts")
        d = r.json()
        assert "paper" in d and "live" in d
        assert d["paper"].get("mode") == "paper"
        assert d["live"].get("mode") == "live"

    def test_system_status_dual_modes(self):
        r = self._client().get("/api/system/status")
        d = r.json()
        assert "paper" in d.get("modes", [])
        assert "live" in d.get("modes", [])
        assert "live" in d and "fund_ready" in d["live"]

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

    def test_fund_holdings_not_initialized_message(self, monkeypatch):
        """凭证缺失时 API 应返回友好 INIT_TOKEN 指引 (monkeypatch 模拟, 不依赖本机凭证)。"""
        from src.trading import fund_trader
        from src.trading.fund_trader import FundTrader, FundNotInitializedError

        def _fake(self):
            raise FundNotInitializedError(
                "爱基金凭证未初始化: 请先执行 ... init('你的INIT_TOKEN')"
            )

        monkeypatch.setattr(FundTrader, "_get_work_token", _fake)
        r = self._client().get("/api/fund/holdings")
        body = r.json()
        assert body.get("success") is False
        assert "INIT_TOKEN" in str(body.get("error", ""))


class TestFundOrderStatus:
    """订单状态判定 (官方 confirmFlag/checkFlag 组合规则)。"""

    def _judge(self, **kw):
        from src.trading.fund_trader import FundTrader
        return FundTrader.judge_order_status(kw)

    def test_success_confirm_3(self):
        s = self._judge(confirmFlag="3", checkFlag="1")
        assert s["status"] == "success"

    def test_success_check_0_confirm_0(self):
        s = self._judge(confirmFlag="0", checkFlag="0")
        assert s["status"] == "success"

    def test_processing(self):
        s = self._judge(confirmFlag="0", checkFlag="1")
        assert s["status"] == "processing"

    def test_failed_with_reason(self):
        s = self._judge(confirmFlag="6", checkFlag="1",
                        failMsg={"thsMessage": "余额不足", "message": "fallback"})
        assert s["status"] == "failed"
        assert s["reason"] == "余额不足"  # 优先 thsMessage

    def test_failed_fallback_message(self):
        s = self._judge(confirmFlag="6", checkFlag="1",
                        failMsg={"thsMessage": "", "message": "fallback"})
        assert s["status"] == "failed"
        assert s["reason"] == "fallback"

    def test_partial(self):
        s = self._judge(confirmFlag="2", checkFlag="1")
        assert s["status"] == "partial"

    def test_revoked(self):
        s = self._judge(confirmFlag="1", checkFlag="1")
        assert s["status"] == "revoked"

    def test_unknown(self):
        s = self._judge()
        assert s["status"] == "unknown"


class TestFundInfoEndpoint:
    """/api/fund/info 端点。"""

    def _client(self):
        from fastapi.testclient import TestClient
        import frontend.api_server as api
        return TestClient(api.app)

    def test_info_endpoint_registered(self):
        """端点存在即可 (真实数据依赖网络, 不在此断言内容)。"""
        r = self._client().get("/api/fund/info/000001")
        assert r.status_code == 200
        body = r.json()
        assert "success" in body
