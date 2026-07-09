import asyncio
import json
import time
import struct
import zlib
import random
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime
from enum import Enum


class QuoteSource(Enum):
    EASTMONEY = "eastmoney"
    SINA = "sina"
    TENCENT = "tencent"


class MarketLevel(Enum):
    LEVEL1 = 1
    LEVEL2 = 2


class WSStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


def validate_quote(symbol: str, quote: Dict, strict: bool = False) -> bool:
    """行情数据校验

    Args:
        strict: 严格模式下,缺失关键字段直接拒绝
    """
    if not quote or not isinstance(quote, dict):
        return False
    price = quote.get("price", 0)
    if price is None or price <= 0:
        return False
    if price > 10000:
        return False
    change_pct = quote.get("change_pct", 0)
    if change_pct is not None and abs(change_pct) > 30:
        return False
    if strict:
        required = ("price", "volume", "amount", "pre_close")
        if not all(k in quote for k in required):
            return False
        if quote.get("pre_close", 0) <= 0:
            return False
    return True


class WSSymbol:
    def __init__(self, code: str, market: int = None):
        raw = code.replace('sh', '').replace('sz', '').replace('SH', '').replace('SZ', '')
        if market is not None:
            self.market = market
        elif code.startswith(('sh', 'SH')) or raw.startswith(('6', '5', '9')):
            self.market = 1
        else:
            self.market = 0
        self.code = raw
        self.full = f"{'sh' if self.market == 1 else 'sz'}{self.code}"

    @property
    def secid(self) -> str:
        return f"{self.market}.{self.code}"

    def __repr__(self):
        return self.full


class WSQuoteClient:
    def __init__(self, source: QuoteSource = QuoteSource.EASTMONEY):
        self.source = source
        self._ws = None
        self._running = False
        self._callbacks: List[Callable] = []
        self._status_callbacks: List[Callable] = []
        self._symbols: List[WSSymbol] = []
        self._last_quotes: Dict[str, Dict] = {}
        self._reconnect_interval = 3
        self._max_reconnect_interval = 60
        self._max_reconnect_attempts = 0
        self._reconnect_count = 0
        self._latency_ms = 0
        self._pong_received = asyncio.Event()
        self._last_msg_time: float = 0
        self._status: WSStatus = WSStatus.DISCONNECTED
        self._total_messages = 0
        self._invalid_messages = 0
        self._stale_threshold = 30.0

        self._ws_urls = {
            QuoteSource.EASTMONEY: "wss://push2.eastmoney.com/api/qt/stock/utfmt?token=&_=",
            QuoteSource.SINA: "wss://ws.hq.sinajs.cn/",
        }

    def register_callback(self, callback: Callable):
        self._callbacks.append(callback)

    def register_status_callback(self, callback: Callable):
        """状态变化回调: on_status(status: WSStatus)"""
        self._status_callbacks.append(callback)

    def subscribe(self, symbols: List[str]):
        self._symbols = [WSSymbol(s) for s in symbols]
        self._last_quotes = {s.full: {} for s in self._symbols}

    def _set_status(self, status: WSStatus):
        if self._status == status:
            return
        self._status = status
        for cb in self._status_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(status))
                else:
                    cb(status)
            except Exception:
                pass

    def is_healthy(self) -> bool:
        if self._status != WSStatus.CONNECTED:
            return False
        if self._last_msg_time == 0:
            return True
        return (time.time() - self._last_msg_time) < self._stale_threshold

    def get_stats(self) -> Dict:
        return {
            "status": self._status.value,
            "reconnect_count": self._reconnect_count,
            "total_messages": self._total_messages,
            "invalid_messages": self._invalid_messages,
            "symbols_count": len(self._symbols),
            "last_msg_age_sec": (time.time() - self._last_msg_time) if self._last_msg_time else None,
            "healthy": self.is_healthy(),
            "latency_ms": self._latency_ms,
        }

    def _compute_backoff(self, attempt: int) -> float:
        base = self._reconnect_interval * (1.5 ** min(attempt, 6))
        capped = min(base, self._max_reconnect_interval)
        jitter = random.uniform(0, capped * 0.2)
        return capped + jitter

    async def connect(self):
        if self.source == QuoteSource.EASTMONEY:
            await self._connect_eastmoney()
        elif self.source == QuoteSource.SINA:
            await self._connect_sina()

    async def _connect_eastmoney(self):
        import websockets
        uri = self._ws_urls[QuoteSource.EASTMONEY] + str(int(time.time() * 1000))

        while self._running:
            try:
                self._set_status(WSStatus.CONNECTING)
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024
                ) as ws:
                    self._ws = ws
                    self._reconnect_count = 0
                    self._set_status(WSStatus.CONNECTED)
                    print(f"[WSClient] 东方财富WebSocket已连接")

                    await self._send_subscribe_eastmoney(ws)
                    await self._read_loop(ws)

            except Exception as e:
                self._reconnect_count += 1
                if self._max_reconnect_attempts > 0 and self._reconnect_count > self._max_reconnect_attempts:
                    self._set_status(WSStatus.FAILED)
                    print(f"[WSClient] 重连次数已达上限 ({self._max_reconnect_attempts})")
                    break
                self._set_status(WSStatus.RECONNECTING)
                wait = self._compute_backoff(self._reconnect_count)
                print(f"[WSClient] 连接断开 ({e}), {wait:.0f}秒后重连 (第{self._reconnect_count}次)")
                await asyncio.sleep(wait)
        self._set_status(WSStatus.DISCONNECTED)

    async def _send_subscribe_eastmoney(self, ws):
        secids = [s.secid for s in self._symbols]
        if not secids:
            return

        for i in range(0, len(secids), 100):
            batch = secids[i:i + 100]
            package = self._build_eastmoney_package(batch)
            await ws.send(package)

    def _build_eastmoney_package(self, secids: List[str]) -> bytes:
        market_str = ",".join(secids)
        body = f"market={market_str}&type=quote&client=web"
        header = struct.pack('>I', len(body))
        return header + body.encode('utf-8')

    async def _read_loop(self, ws):
        async for message in ws:
            try:
                self._total_messages += 1
                self._last_msg_time = time.time()
                if isinstance(message, bytes) and len(message) > 4:
                    quotes = self._parse_eastmoney_push(message)
                    if quotes:
                        valid = {k: v for k, v in quotes.items() if validate_quote(k, v)}
                        invalid_count = len(quotes) - len(valid)
                        self._invalid_messages += invalid_count
                        if valid:
                            self._last_quotes.update(valid)
                            await self._notify_callbacks(valid)
                elif isinstance(message, str):
                    if 'pong' in message.lower():
                        self._pong_received.set()
            except Exception as e:
                self._invalid_messages += 1
                print(f"[WSClient] 解析消息失败: {e}")

    def _parse_eastmoney_push(self, data: bytes) -> Dict[str, Dict]:
        try:
            if len(data) <= 4:
                return {}

            body_len = struct.unpack('>I', data[:4])[0]
            body = data[4:4 + body_len]

            try:
                text = body.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    decompressed = zlib.decompress(body)
                    text = decompressed.decode('utf-8')
                except Exception:
                    return {}

            if not text.startswith('{'):
                return {}

            data_json = json.loads(text)
            result = {}

            items = data_json.get('data', {}).get('diff', []) if 'data' in data_json else data_json.get('data', []) if isinstance(data_json.get('data'), list) else []

            if not items and isinstance(data_json, list):
                items = data_json

            for item in items:
                if not isinstance(item, dict):
                    continue

                code = item.get('f12', '') or item.get('code', '')
                market = item.get('f13', 0) or item.get('market', 0)
                if not code:
                    continue

                symbol = f"{'sh' if market == 1 else 'sz'}{code}"

                self._latency_ms = item.get('f186', 0)

                result[symbol] = {
                    'name': item.get('f14', ''),
                    'price': self._safe_float(item.get('f2')),
                    'change_pct': self._safe_float(item.get('f3')),
                    'change': self._safe_float(item.get('f4')),
                    'volume': item.get('f5', 0) or item.get('f47', 0),
                    'amount': self._safe_float(item.get('f6')) or self._safe_float(item.get('f48')),
                    'high': self._safe_float(item.get('f15')) or self._safe_float(item.get('f44')),
                    'low': self._safe_float(item.get('f16')) or self._safe_float(item.get('f45')),
                    'open': self._safe_float(item.get('f17')) or self._safe_float(item.get('f46')),
                    'pre_close': self._safe_float(item.get('f18')) or self._safe_float(item.get('f60')),
                    'turnover': self._safe_float(item.get('f8')),
                    'pe': self._safe_float(item.get('f9')),
                    'amplitude': self._safe_float(item.get('f7')),
                    'bid_prices': [
                        self._safe_float(item.get(f'b1_p')),
                        self._safe_float(item.get(f'b2_p')),
                        self._safe_float(item.get(f'b3_p')),
                    ],
                    'ask_prices': [
                        self._safe_float(item.get(f'a1_p')),
                        self._safe_float(item.get(f'a2_p')),
                        self._safe_float(item.get(f'a3_p')),
                    ],
                    'bid_volumes': [
                        item.get('b1_v', 0),
                        item.get('b2_v', 0),
                        item.get('b3_v', 0),
                    ],
                    'ask_volumes': [
                        item.get('a1_v', 0),
                        item.get('a2_v', 0),
                        item.get('a3_v', 0),
                    ],
                    'time': datetime.now(),
                    'timestamp': int(time.time() * 1000),
                }

            return result

        except Exception as e:
            return {}

    def _safe_float(self, val) -> float:
        if val is None:
            return 0.0
        try:
            v = float(val)
            return v / 100 if abs(v) > 10000 else v
        except (ValueError, TypeError):
            return 0.0

    async def _connect_sina(self):
        import websockets
        uri = self._ws_urls[QuoteSource.SINA]

        while self._running:
            try:
                self._set_status(WSStatus.CONNECTING)
                async with websockets.connect(uri, ping_interval=20) as ws:
                    self._ws = ws
                    self._reconnect_count = 0
                    self._set_status(WSStatus.CONNECTED)
                    print(f"[WSClient] 新浪WebSocket已连接")

                    subscribe_msg = " ".join(s.full for s in self._symbols)
                    await ws.send(subscribe_msg)
                    await self._read_loop_sina(ws)

            except Exception as e:
                self._reconnect_count += 1
                if self._max_reconnect_attempts > 0 and self._reconnect_count > self._max_reconnect_attempts:
                    self._set_status(WSStatus.FAILED)
                    break
                self._set_status(WSStatus.RECONNECTING)
                wait = self._compute_backoff(self._reconnect_count)
                print(f"[WSClient] 新浪WS断开 ({e}), {wait:.0f}秒后重连 (第{self._reconnect_count}次)")
                await asyncio.sleep(wait)
        self._set_status(WSStatus.DISCONNECTED)

    async def _read_loop_sina(self, ws):
        async for message in ws:
            try:
                self._total_messages += 1
                self._last_msg_time = time.time()
                quotes = self._parse_sina_push(message)
                if quotes:
                    valid = {k: v for k, v in quotes.items() if validate_quote(k, v)}
                    invalid_count = len(quotes) - len(valid)
                    self._invalid_messages += invalid_count
                    if valid:
                        self._last_quotes.update(valid)
                        await self._notify_callbacks(valid)
            except Exception:
                self._invalid_messages += 1

    def _parse_sina_push(self, data: str) -> Dict[str, Dict]:
        import re
        result = {}
        lines = data.strip().split('\n')

        for line in lines:
            match = re.search(r'var hq_str_(\w+)="(.+)"', line)
            if not match:
                continue

            symbol = match.group(1)
            fields = match.group(2).split(',')

            if len(fields) >= 32:
                try:
                    price = float(fields[3])
                    pre_close = float(fields[2])
                    result[symbol] = {
                        'name': fields[0],
                        'open': float(fields[1]),
                        'pre_close': pre_close,
                        'price': price,
                        'high': float(fields[4]),
                        'low': float(fields[5]),
                        'volume': int(float(fields[8])),
                        'amount': float(fields[9]),
                        'change': price - pre_close,
                        'change_pct': (price - pre_close) / pre_close * 100 if pre_close else 0,
                        'time': fields[30],
                        'date': fields[31],
                        'timestamp': int(time.time() * 1000),
                    }
                except (ValueError, IndexError):
                    continue

        return result

    async def _notify_callbacks(self, quotes: Dict):
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(quotes)
                else:
                    cb(quotes)
            except Exception as e:
                print(f"[WSClient] 回调异常: {e}")

    def get_latest_quote(self, symbol: str) -> Dict:
        symbol = symbol.replace('sh', '').replace('sz', '')
        for key, val in self._last_quotes.items():
            if symbol in key:
                return val
        return self._last_quotes.get(symbol, {})

    def get_all_quotes(self) -> Dict[str, Dict]:
        return dict(self._last_quotes)

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    async def start(self):
        self._running = True
        await self.connect()

    async def stop(self):
        self._running = False
        self._set_status(WSStatus.DISCONNECTED)
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
