"""
guling-trader 券商适配器

将 guling-trader (同花顺实盘交易) 接入引擎的 FastBroker 接口。
通过 MCP 协议 (HTTP JSON-RPC) 与 guling.pro 云端通信,
由 guling-trader.exe (Windows 端) 操控同花顺 xiadan.exe 执行实盘交易。

依赖:
  - guling-trader.exe 在 Windows 上运行且已配对
  - agent_token 已配置在 config.yaml 的 broker.guling_agent_token 中

使用方式:
  config.yaml:
    broker:
      type: guling
      guling_agent_token: "<your-agent-token>"
      guling_mcp_url: "https://mcp.guling.pro"  # 可选,默认值
"""

import json
import time
import logging
import uuid
from typing import Dict, List, Optional, Tuple, Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP JSON-RPC 轻量客户端
# ---------------------------------------------------------------------------

class McpHttpClient:
    """MCP Streamable HTTP 轻量客户端。

    仅实现本项目需要的 MCP 子集: initialize / tools/call。
    使用 HTTP POST + JSON-RPC 2.0, 支持 session 管理。
    """

    def __init__(self, base_url: str, agent_token: str):
        self.base_url = base_url.rstrip("/")
        self.agent_token = agent_token
        self.session_id: Optional[str] = None
        self._request_id = 0
        self._initialized = False
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.agent_token}",
        })

    def _next_id(self) -> str:
        self._request_id += 1
        return str(self._request_id)

    def _rpc_call(self, method: str, params: dict = None) -> dict:
        """发送 JSON-RPC 请求, 返回 result 或抛出异常。"""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        headers = {}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        try:
            resp = self._session.post(
                self.base_url,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"guling-trader MCP 连接失败: {e}")

        # 提取 session ID
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid

        if resp.status_code != 200:
            raise RuntimeError(
                f"guling-trader HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"guling-trader 返回非 JSON: {resp.text[:300]}")

        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"guling-trader JSON-RPC error [{err.get('code')}]: {err.get('message', str(err))}"
            )

        return data.get("result", {})

    def initialize(self) -> bool:
        """初始化 MCP 会话。"""
        if self._initialized:
            return True

        result = self._rpc_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "money-engine",
                "version": "1.0.0",
            },
        })

        # 发送 initialized 通知
        try:
            self._session.post(
                self.base_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"Mcp-Session-Id": self.session_id} if self.session_id else {},
                timeout=10,
            )
        except Exception:
            pass

        self._initialized = True
        server_name = result.get("serverInfo", {}).get("name", "unknown")
        logger.info(f"guling-trader MCP 已初始化 (server={server_name})")
        return True

    def call_tool(self, name: str, arguments: dict = None) -> Any:
        """调用 MCP 工具, 返回解析后的结果。"""
        if not self._initialized:
            self.initialize()

        result = self._rpc_call("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

        # MCP CallToolResult: content[0].text 包含 JSON 信封
        content = result.get("content", [])
        if not content:
            return None

        text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

        return envelope

    def close(self):
        self._session.close()
        self._initialized = False


# ---------------------------------------------------------------------------
# ExecOrder 兼容 (复用 FastBroker 的数据类)
# ---------------------------------------------------------------------------

class ExecOrder:
    """订单执行结果 (与 FastBroker.ExecOrder 兼容)。"""
    __slots__ = ("order_id", "symbol", "side", "quantity", "price",
                 "filled_quantity", "filled_price", "status", "reason",
                 "latency_ns", "commission", "stamp_tax", "detail")

    def __init__(
        self,
        order_id: str = "",
        symbol: str = "",
        side: str = "buy",
        quantity: int = 0,
        price: float = 0.0,
        filled_quantity: int = 0,
        filled_price: float = 0.0,
        status: str = "unknown",
        reason: str = "",
        latency_ns: int = 0,
        commission: float = 0.0,
        stamp_tax: float = 0.0,
        detail: dict = None,
    ):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.price = price
        self.filled_quantity = filled_quantity
        self.filled_price = filled_price
        self.status = status
        self.reason = reason
        self.latency_ns = latency_ns
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.detail = detail or {}

    def __repr__(self):
        return (
            f"ExecOrder({self.side} {self.symbol} qty={self.quantity} "
            f"filled={self.filled_quantity}@{self.filled_price} "
            f"status={self.status})"
        )


# ---------------------------------------------------------------------------
# GulingBroker — FastBroker 风格接口
# ---------------------------------------------------------------------------

class GulingBroker:
    """同花顺实盘券商 (通过 guling-trader MCP)。

    提供 FastBroker 兼容接口:
      - buy(symbol, quantity, price, reason) -> (bool, str, ExecOrder)
      - sell(symbol, quantity, price, reason) -> (bool, str, ExecOrder)
      - update_price(symbol, price)
      - get_account_info() -> Dict
      - get_positions() -> List[Dict]
      - sync_to_db(state_manager)

    实盘安全说明:
      - auto_trade 必须显式设为 true 才会执行真实下单
      - 所有操作通过同花顺 xiadan.exe 执行
      - 密码始终不离开同花顺 (guling-trader 只模拟键鼠操作)
    """

    def __init__(
        self,
        agent_token: str,
        mcp_url: str = "https://mcp.guling.pro",
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.0005,
        min_commission: float = 5.0,
        auto_trade: bool = False,
    ):
        self.agent_token = agent_token
        self.mcp_url = mcp_url
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission
        self.auto_trade = auto_trade
        self._client: Optional[McpHttpClient] = None
        self._prices: Dict[str, float] = {}
        self._connected = False

    # ---- 连接管理 ----

    def connect(self) -> bool:
        """连接 guling-trader MCP 端点。"""
        if not self.agent_token:
            logger.error("guling_broker: agent_token 未配置, 无法连接")
            return False

        try:
            self._client = McpHttpClient(self.mcp_url, self.agent_token)
            self._client.initialize()
            self._connected = True
            logger.info("guling_broker: 已连接到 guling-trader MCP")
            return True
        except Exception as e:
            logger.error(f"guling_broker: 连接失败: {e}")
            self._connected = False
            return False

    def disconnect(self):
        if self._client:
            self._client.close()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ---- 行情更新 ----

    def update_price(self, symbol: str, price: float):
        """更新内存中的行情价 (用于利润计算)。"""
        self._prices[symbol] = price

    # ---- 下单 ----

    def buy(
        self, symbol: str, quantity: int = 0, price: float = None, reason: str = ""
    ) -> Tuple[bool, str, Optional[ExecOrder]]:
        """买入 (实盘)。

        Args:
            symbol: 股票代码 (如 '600000')
            quantity: 买入股数 (必须 100 的整数倍)
            price: 限价 (None=市价五档即成剩撤)
            reason: 交易原因 (日志用)
        """
        return self._place_order("buy", symbol, quantity, price, reason)

    def sell(
        self, symbol: str, quantity: int = 0, price: float = None, reason: str = ""
    ) -> Tuple[bool, str, Optional[ExecOrder]]:
        """卖出 (实盘)。"""
        return self._place_order("sell", symbol, quantity, price, reason)

    def _place_order(
        self, side: str, symbol: str, quantity: int,
        price: Optional[float], reason: str,
    ) -> Tuple[bool, str, Optional[ExecOrder]]:
        """统一下单逻辑。"""
        t0 = time.perf_counter_ns()

        if quantity <= 0:
            return False, "数量必须大于 0", None

        if not self.auto_trade:
            return (
                False,
                "auto_trade=false, 未执行实盘下单 (仅记录)",
                ExecOrder(
                    order_id=f"blocked_{side}_{symbol}",
                    symbol=symbol, side=side, quantity=quantity,
                    price=price or 0, status="blocked",
                    reason=f"auto_trade disabled: {reason}" if reason else "auto_trade disabled",
                ),
            )

        if not self._client or not self._connected:
            return False, "guling-trader 未连接", None

        # 构造 client_order_id (幂等键)
        client_order_id = f"money_{side}_{symbol}_{int(t0)}_{uuid.uuid4().hex[:8]}"

        args = {
            "stock_no": _normalize_symbol(symbol),
            "amount": quantity,
            "client_order_id": client_order_id,
        }
        if price is not None:
            args["price"] = price

        method = "buy" if side == "buy" else "sell"
        logger.info(
            f"guling_broker: {method.upper()} {args['stock_no']} "
            f"x{quantity}" + (f" @{price}" if price else " 市价") +
            (f" ({reason})" if reason else "")
        )

        try:
            envelope = self._client.call_tool(method, args)
        except Exception as e:
            latency_ns = time.perf_counter_ns() - t0
            return (
                False, str(e),
                ExecOrder(
                    order_id=client_order_id,
                    symbol=symbol, side=side, quantity=quantity,
                    price=price or 0, status="rejected",
                    reason=str(e), latency_ns=latency_ns,
                ),
            )

        latency_ns = time.perf_counter_ns() - t0

        # 解析 guling-trader 回执信封 (契约 v2)
        status = envelope.get("status", "failed")
        code = envelope.get("code", "unknown")
        data = envelope.get("data", {}) or {}
        error = envelope.get("error") or {}

        if status == "succeed":
            entrust_no = data.get("entrust_no", "")
            filled_amount = data.get("filled_amount", 0) or 0
            avg_price = data.get("avg_price", 0) or price or 0

            return (
                True,
                f"委托成功 entrust_no={entrust_no}",
                ExecOrder(
                    order_id=client_order_id,
                    symbol=symbol, side=side, quantity=quantity,
                    price=price or avg_price,
                    filled_quantity=filled_amount,
                    filled_price=avg_price,
                    status="filled" if filled_amount >= quantity else "submitted",
                    reason=reason,
                    latency_ns=latency_ns,
                    detail={"entrust_no": entrust_no, "envelope": envelope},
                ),
            )

        elif status == "busy":
            retry_after = data.get("retry_after_secs", 3)
            return (
                False,
                f"同花顺窗口忙, {retry_after}s 后可重试",
                ExecOrder(
                    order_id=client_order_id,
                    symbol=symbol, side=side, quantity=quantity,
                    price=price or 0, status="busy",
                    reason=f"busy, retry_after={retry_after}s",
                    latency_ns=latency_ns,
                ),
            )

        else:
            err_msg = error.get("message", code)
            return (
                False,
                f"委托失败: {err_msg}",
                ExecOrder(
                    order_id=client_order_id,
                    symbol=symbol, side=side, quantity=quantity,
                    price=price or 0, status="rejected",
                    reason=f"{code}: {err_msg}",
                    latency_ns=latency_ns,
                    detail={"code": code, "envelope": envelope},
                ),
            )

    # ---- 查询 ----

    def get_account_info(self) -> Dict:
        """查询账户信息 (资金余额、持仓市值等)。"""
        if not self._client or not self._connected:
            return self._empty_account()

        try:
            envelope = self._client.call_tool("balance")
        except Exception as e:
            logger.error(f"guling_broker: 查询余额失败: {e}")
            return self._empty_account()

        if envelope.get("status") != "succeed":
            return self._empty_account()

        data = envelope.get("data", {}) or {}
        return {
            "total_assets": data.get("总资产") or 0,
            "available_cash": data.get("可用金额") or 0,
            "frozen_cash": data.get("冻结金额") or 0,
            "market_value": data.get("股票市值") or 0,
            "total_profit": data.get("持仓盈亏") or 0,
            "daily_profit": data.get("当日盈亏") or 0,
            "daily_profit_pct": data.get("当日盈亏比_pct"),
            "balance": data.get("资金余额") or 0,
            "withdrawable": data.get("可取金额") or data.get("可用金额") or 0,
            "source": "guling-trader (同花顺实盘)",
        }

    def get_positions(self) -> List[Dict]:
        """查询持仓列表。"""
        if not self._client or not self._connected:
            return []

        try:
            envelope = self._client.call_tool("position")
        except Exception as e:
            logger.error(f"guling_broker: 查询持仓失败: {e}")
            return []

        if envelope.get("status") != "succeed":
            return []

        data = envelope.get("data", {})
        rows = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        positions = []
        for row in rows:
            symbol = str(row.get("证券代码", "")).strip()
            name = row.get("证券名称", "")
            qty = row.get("股票余额", 0) or 0
            available = row.get("可用余额", 0) or 0
            cost = row.get("参考成本价", 0) or 0
            market_price = row.get("市价", 0) or 0
            market_value = row.get("market_value", 0) or 0
            profit = row.get("浮动盈亏", 0) or 0
            profit_pct = row.get("盈亏比例_pct")

            positions.append({
                "symbol": symbol,
                "name": name,
                "quantity": int(qty),
                "available": int(available),
                "avg_cost": float(cost),
                "market_price": float(market_price),
                "market_value": float(market_value),
                "unrealized_pnl": float(profit),
                "unrealized_pnl_pct": float(profit_pct) if profit_pct is not None else None,
            })

        return positions

    def get_active_orders(self) -> List[Dict]:
        """查询在飞委托 (未成交/部分成交)。"""
        if not self._client or not self._connected:
            return []

        try:
            envelope = self._client.call_tool("orders_active")
        except Exception:
            return []

        if envelope.get("status") != "succeed":
            return []

        data = envelope.get("data", {})
        rows = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return rows

    def cancel_order(self, entrust_no: str) -> Tuple[bool, str]:
        """撤单。"""
        if not self._client or not self._connected:
            return False, "guling-trader 未连接"

        try:
            envelope = self._client.call_tool("cancel", {"entrust_no": entrust_no})
        except Exception as e:
            return False, str(e)

        if envelope.get("status") == "succeed":
            return True, "撤单成功"
        return False, envelope.get("error", {}).get("message", "撤单失败")

    def sync_to_db(self, state_manager) -> bool:
        """同步账户状态到数据库 (兼容 FastBroker 接口)。"""
        try:
            account = self.get_account_info()
            positions = self.get_positions()

            state = {
                "total_assets": account.get("total_assets", 0),
                "available_cash": account.get("available_cash", 0),
                "market_value": account.get("market_value", 0),
                "total_profit": account.get("total_profit", 0),
                "daily_profit": account.get("daily_profit", 0),
                "positions": positions,
                "updated_at": time.time(),
            }

            state_manager.save_account_state("guling_broker_state", state)
            return True
        except Exception as e:
            logger.error(f"guling_broker: sync_to_db 失败: {e}")
            return False

    # ---- 辅助 ----

    def _empty_account(self) -> Dict:
        return {
            "total_assets": 0, "available_cash": 0,
            "frozen_cash": 0, "market_value": 0,
            "total_profit": 0, "daily_profit": 0,
            "daily_profit_pct": None,
            "balance": 0, "withdrawable": 0,
            "source": "guling-trader (未连接)",
        }


# ---------------------------------------------------------------------------
# 适配器 — 将 GulingBroker 包装成 QMTFastAdapter 风格 (engine.py 兼容)
# ---------------------------------------------------------------------------

class GulingFastAdapter:
    """GulingBroker → FastBroker 风格适配器。

    与 QMTFastAdapter 接口一致, 可直接插入 engine.py 的 _create_broker()。
    """

    def __init__(
        self,
        agent_token: str = "",
        mcp_url: str = "https://mcp.guling.pro",
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.0005,
        min_commission: float = 5.0,
        auto_trade: bool = False,
    ):
        self._broker = GulingBroker(
            agent_token=agent_token,
            mcp_url=mcp_url,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate,
            min_commission=min_commission,
            auto_trade=auto_trade,
        )

    def connect(self) -> bool:
        return self._broker.connect()

    def disconnect(self):
        self._broker.disconnect()

    def update_price(self, symbol: str, price: float):
        self._broker.update_price(symbol, price)

    def buy(self, symbol: str, quantity: int = 0, price: float = None,
            reason: str = "") -> Tuple[bool, str, Optional[ExecOrder]]:
        return self._broker.buy(symbol, quantity, price, reason)

    def sell(self, symbol: str, quantity: int = 0, price: float = None,
             reason: str = "") -> Tuple[bool, str, Optional[ExecOrder]]:
        return self._broker.sell(symbol, quantity, price, reason)

    def get_account_info(self) -> Dict:
        return self._broker.get_account_info()

    def get_positions(self) -> List[Dict]:
        return self._broker.get_positions()

    def sync_to_db(self, state_manager) -> bool:
        return self._broker.sync_to_db(state_manager)

    @property
    def is_connected(self) -> bool:
        return self._broker.is_connected


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _normalize_symbol(symbol: str) -> str:
    """标准化股票代码: 去掉 sh/sz 前缀, 返回 6 位数字。"""
    s = symbol.strip().lower()
    if s.startswith("sh") or s.startswith("sz"):
        s = s[2:]
    # 去掉可能的前导零以外的点号
    return s.split(".")[0].zfill(6)
