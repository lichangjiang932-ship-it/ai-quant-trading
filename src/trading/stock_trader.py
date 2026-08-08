"""
股票实盘交易 — 项目内直接完成 A 股实盘交易, 不依赖 WorkBuddy。

基于 guling-trader (同花顺实盘):
  StockTrader → GulingBroker → McpHttpClient → mcp.guling.pro → guling-trader.exe → 同花顺 xiadan.exe

依赖:
  - guling-trader.exe 在 Windows 上运行且已配对
  - agent_token 配置在 config.yaml 的 broker.guling_agent_token

安全:
  - auto_trade=False 时只记录不执行 (默认)
  - 密码始终不离开同花顺
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StockTraderError(Exception):
    """股票交易异常基类。"""


class StockTrader:
    """股票实盘交易客户端 (FastBroker 风格接口的轻量包装)。

    用法:
        trader = StockTrader.from_config()          # 从 config.yaml 读取
        trader.connect()
        trader.buy("600519", 100, price=1500.0)
        trader.get_positions()
    """

    def __init__(
        self,
        agent_token: str = "",
        mcp_url: str = "https://mcp.guling.pro",
        auto_trade: bool = False,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.0005,
        min_commission: float = 5.0,
    ):
        self.agent_token = agent_token
        self.mcp_url = mcp_url
        self.auto_trade = auto_trade
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission
        self._broker = None  # 延迟导入 GulingBroker

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config_path: Optional[str] = None,
        auto_trade: Optional[bool] = None,
    ) -> "StockTrader":
        """从 config.yaml 读取 broker 配置创建实例。

        Args:
            config_path: config.yaml 路径, 默认 <项目根>/config/config.yaml
            auto_trade: 覆盖配置中的 auto_trade 开关
        """
        if config_path is None:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            config_path = os.path.join(root, "config", "config.yaml")
        try:
            from src.utils.config import Config
            cfg = Config(config_path)
        except Exception:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            class _C:
                def __init__(self, d):
                    self._d = d
                def get(self, k, default=None):
                    node = self._d
                    for part in k.split("."):
                        if isinstance(node, dict) and part in node:
                            node = node[part]
                        else:
                            return default
                    return node
            cfg = _C(cfg)

        broker_cfg = cfg.get("broker", {}) or {}
        if auto_trade is None:
            auto_trade = bool(cfg.get("trading.auto_trade", False))

        return cls(
            agent_token=str(broker_cfg.get("guling_agent_token", "") or ""),
            mcp_url=str(broker_cfg.get("guling_mcp_url", "https://mcp.guling.pro")),
            auto_trade=bool(auto_trade),
            commission_rate=float(cfg.get("commission.rate", 0.0003)),
            stamp_tax_rate=float(cfg.get("commission.stamp_tax", 0.0005)),
            min_commission=float(cfg.get("commission.min", 5.0)),
        )

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def _ensure_broker(self):
        if self._broker is None:
            from src.execution.brokers.guling_broker import GulingBroker
            self._broker = GulingBroker(
                agent_token=self.agent_token,
                mcp_url=self.mcp_url,
                auto_trade=self.auto_trade,
                commission_rate=self.commission_rate,
                stamp_tax_rate=self.stamp_tax_rate,
                min_commission=self.min_commission,
            )

    def connect(self) -> bool:
        self._ensure_broker()
        return self._broker.connect()

    def disconnect(self):
        if self._broker:
            self._broker.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._broker and self._broker.is_connected)

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def buy(
        self, symbol: str, quantity: int = 0, price: Optional[float] = None,
        reason: str = "",
    ) -> Dict:
        """买入。symbol 支持 '600519' / 'sh600519' 形式。"""
        return self._place("buy", symbol, quantity, price, reason)

    def sell(
        self, symbol: str, quantity: int = 0, price: Optional[float] = None,
        reason: str = "",
    ) -> Dict:
        """卖出。"""
        return self._place("sell", symbol, quantity, price, reason)

    def _place(self, side: str, symbol: str, quantity: int,
               price: Optional[float], reason: str) -> Dict:
        self._ensure_broker()
        try:
            ok, msg, order = self._broker.buy(symbol, quantity, price, reason) \
                if side == "buy" else self._broker.sell(symbol, quantity, price, reason)
        except Exception as e:
            raise StockTraderError(f"{side} 失败: {e}") from e
        return {
            "success": ok,
            "message": msg,
            "order_id": order.order_id if order else None,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": order.status if order else None,
            "filled_quantity": order.filled_quantity if order else 0,
            "filled_price": order.filled_price if order else None,
            "detail": order.detail if order else {},
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_account_info(self) -> Dict:
        """账户资金信息。"""
        self._ensure_broker()
        return self._broker.get_account_info()

    def get_positions(self) -> List[Dict]:
        """持仓列表。"""
        self._ensure_broker()
        return self._broker.get_positions()

    def get_active_orders(self) -> List[Dict]:
        """在飞委托。"""
        self._ensure_broker()
        return self._broker.get_active_orders()

    def cancel_order(self, entrust_no: str) -> Tuple[bool, str]:
        """撤单。"""
        self._ensure_broker()
        return self._broker.cancel_order(entrust_no)

    # ------------------------------------------------------------------
    # 便捷
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict:
        """账户 + 持仓 + 在飞委托 总览。"""
        try:
            account = self.get_account_info()
        except Exception as e:
            account = {"error": str(e)}
        try:
            positions = self.get_positions()
        except Exception as e:
            positions = [{"error": str(e)}]
        try:
            orders = self.get_active_orders()
        except Exception as e:
            orders = [{"error": str(e)}]
        return {
            "connected": self.is_connected,
            "account": account,
            "positions": positions,
            "active_orders": orders,
        }
