"""
pytdx(通达信)实时行情客户端

相比 HTTP 抓取(新浪/腾讯/东财),pytdx 走通达信的 TCP 二进制协议,更稳、更少限流,
是 A 股最值得加的免费实时行情源。

设计要点:
- try import pytdx 优雅降级(HAS_PYTDX);未安装时 is_available()=False,调用方自动跳过
- 内置一组通达信公共行情服务器,连不上自动轮换下一个
- 维护长连接;断线时下次调用自动重连
- get_quotes(symbols) 输出与 RealtimeData.get_realtime_quote_* **完全一致的字段**,
  便于在多源回退里无缝替换
- 任何异常都不抛出,返回空 dict,保证不拖垮引擎

pytdx API 速览:
  api = TdxHq_API()
  api.connect(ip, port)
  data = api.get_security_quotes([(market, code), ...])  # market: 1=沪 0=深
  # 每条含: code, price, last_close, open, high, low, vol, amount, bid1.., ask1..
"""
from typing import Dict, List, Optional, Tuple

try:
    from pytdx.hq import TdxHq_API
    HAS_PYTDX = True
except Exception:
    TdxHq_API = None
    HAS_PYTDX = False


# 通达信公共行情服务器(连不上自动轮换)
DEFAULT_SERVERS = [
    ('119.147.212.81', 7709),
    ('222.161.29.170', 7709),
    ('123.125.108.14', 7709),
    ('115.238.90.165', 7709),
    ('218.108.98.244', 7709),
]


def _split_symbol(symbol: str) -> Optional[Tuple[int, str]]:
    """sh600000 / sz000001 / 600000 -> (market, code)。market: 1=沪 0=深"""
    s = symbol.lower().strip()
    if s.startswith('sh'):
        return 1, s[2:]
    if s.startswith('sz'):
        return 0, s[2:]
    code = s
    if code[:1] in ('6', '5', '9'):  # 6沪股 5沪基 9沪B
        return 1, code
    if code[:1] in ('0', '3', '1', '2'):  # 0/3深股 1深基 2深B
        return 0, code
    return None


class PytdxQuoteClient:
    """通达信实时行情客户端"""

    def __init__(self, servers: Optional[List[Tuple[str, int]]] = None):
        self.servers = servers or DEFAULT_SERVERS
        self._api = None
        self._connected = False

    def is_available(self) -> bool:
        """已安装 pytdx 即认为可用(连接在首次取数时惰性建立)"""
        return HAS_PYTDX

    def connect(self) -> bool:
        if not HAS_PYTDX:
            return False
        if self._connected and self._api is not None:
            return True
        for ip, port in self.servers:
            try:
                api = TdxHq_API(heartbeat=True)
                if api.connect(ip, port, time_out=3):
                    self._api = api
                    self._connected = True
                    return True
            except Exception:
                continue
        self._connected = False
        return False

    def disconnect(self):
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
        self._api = None
        self._connected = False

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """取实时行情。返回 {symbol: {字段...}}。失败/未装返回 {}。

        字段与 RealtimeData.get_realtime_quote_eastmoney 对齐:
        name, price, change, change_pct, open, high, low, pre_close, volume, amount
        """
        if not HAS_PYTDX:
            return {}
        if not self.connect():
            return {}

        # 构造 (market, code) 列表,并记录回填用的原始 symbol
        pairs: List[Tuple[int, str]] = []
        order: List[str] = []
        for sym in symbols:
            mc = _split_symbol(sym)
            if mc is None:
                continue
            pairs.append(mc)
            order.append(sym)

        if not pairs:
            return {}

        try:
            rows = self._api.get_security_quotes(pairs)
        except Exception:
            # 连接可能失效,标记断开,下次重连
            self._connected = False
            return {}

        if not rows:
            return {}

        results: Dict[str, Dict] = {}
        for sym, row in zip(order, rows):
            if not row:
                continue
            try:
                price = float(row.get('price', 0) or 0)
                pre_close = float(row.get('last_close', 0) or 0)
                if price <= 0:
                    continue
                change = price - pre_close if pre_close else 0.0
                change_pct = (change / pre_close * 100) if pre_close else 0.0
                results[sym] = {
                    'name': '',  # pytdx 行情不含名称,留空(不影响策略)
                    'price': price,
                    'change': round(change, 4),
                    'change_pct': round(change_pct, 4),
                    'open': float(row.get('open', 0) or 0),
                    'high': float(row.get('high', 0) or 0),
                    'low': float(row.get('low', 0) or 0),
                    'pre_close': pre_close,
                    'volume': int(row.get('vol', 0) or 0),
                    'amount': float(row.get('amount', 0) or 0),
                }
            except (ValueError, TypeError):
                continue

        return results
