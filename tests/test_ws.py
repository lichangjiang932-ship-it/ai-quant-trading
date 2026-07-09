"""
WebSocket 客户端测试
"""
import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.realtime.ws_client import (
    WSQuoteClient, WSStatus, WSSymbol, QuoteSource, validate_quote
)


class TestValidateQuote:
    def test_valid_quote(self):
        q = {"price": 10.5, "change_pct": 1.5, "volume": 1000}
        assert validate_quote("sh600000", q) is True

    def test_zero_price_rejected(self):
        q = {"price": 0, "change_pct": 0}
        assert validate_quote("x", q) is False

    def test_negative_price_rejected(self):
        q = {"price": -10}
        assert validate_quote("x", q) is False

    def test_extreme_price_rejected(self):
        q = {"price": 100000}
        assert validate_quote("x", q) is False

    def test_extreme_change_rejected(self):
        q = {"price": 10, "change_pct": 50}
        assert validate_quote("x", q) is False

    def test_none_price_rejected(self):
        q = {"price": None}
        assert validate_quote("x", q) is False

    def test_empty_dict_rejected(self):
        assert validate_quote("x", {}) is False

    def test_strict_mode(self):
        q = {"price": 10, "volume": 100}
        assert validate_quote("x", q, strict=True) is False
        q2 = {"price": 10, "volume": 100, "amount": 1000, "pre_close": 9.5}
        assert validate_quote("x", q2, strict=True) is True

    def test_strict_zero_pre_close(self):
        q = {"price": 10, "volume": 100, "amount": 1000, "pre_close": 0}
        assert validate_quote("x", q, strict=True) is False


class TestWSSymbol:
    def test_shanghai_market(self):
        s = WSSymbol("sh600000")
        assert s.market == 1
        assert s.code == "600000"
        assert s.full == "sh600000"
        assert s.secid == "1.600000"

    def test_shenzhen_market(self):
        s = WSSymbol("sz000001")
        assert s.market == 0
        assert s.code == "000001"
        assert s.full == "sz000001"
        assert s.secid == "0.000001"

    def test_bare_code(self):
        s = WSSymbol("600519")
        assert s.market == 1
        assert s.full == "sh600519"

    def test_market_5_codes(self):
        s = WSSymbol("510300")
        assert s.market == 1
        assert s.full == "sh510300"


class TestWSQuoteClient:
    def test_init(self):
        c = WSQuoteClient()
        assert c.source == QuoteSource.EASTMONEY
        assert c._status == WSStatus.DISCONNECTED
        assert c._reconnect_count == 0

    def test_register_callback(self):
        c = WSQuoteClient()
        cb = lambda x: None
        c.register_callback(cb)
        assert cb in c._callbacks

    def test_register_status_callback(self):
        c = WSQuoteClient()
        cb = lambda x: None
        c.register_status_callback(cb)
        assert cb in c._status_callbacks

    def test_subscribe(self):
        c = WSQuoteClient()
        c.subscribe(["sh600000", "sz000001"])
        assert len(c._symbols) == 2
        assert c._last_quotes["sh600000"] == {}

    def test_health_when_disconnected(self):
        c = WSQuoteClient()
        assert c.is_healthy() is False

    def test_set_status_triggers_callbacks(self):
        c = WSQuoteClient()
        called = []
        c.register_status_callback(lambda s: called.append(s))
        c._set_status(WSStatus.CONNECTED)
        c._set_status(WSStatus.CONNECTED)
        c._set_status(WSStatus.DISCONNECTED)
        assert called == [WSStatus.CONNECTED, WSStatus.DISCONNECTED]

    def test_backoff_progression(self):
        c = WSQuoteClient()
        c._reconnect_interval = 1
        c._max_reconnect_interval = 30
        b1 = c._compute_backoff(1)
        b5 = c._compute_backoff(5)
        b10 = c._compute_backoff(10)
        assert b1 < b5
        assert b5 <= 30 + 6
        assert b10 <= 30 + 6

    def test_stats(self):
        c = WSQuoteClient()
        c.subscribe(["sh600000"])
        stats = c.get_stats()
        assert stats["status"] == "disconnected"
        assert stats["symbols_count"] == 1
        assert stats["reconnect_count"] == 0
        assert stats["healthy"] is False
