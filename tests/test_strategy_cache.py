import threading
import time
from datetime import datetime

from frontend import api_server


def test_strategy_refresh_failure_uses_retry_backoff():
    assert api_server._strategy_refresh_backoff_active({
        "failed_at": datetime.now().isoformat(timespec="seconds"),
    }) is True


def test_strategy_cache_returns_immediately_and_refreshes_in_background(monkeypatch):
    original_cache = dict(api_server._strategy_cache)
    original_refreshing = set(api_server._strategy_refreshing)
    started = threading.Event()
    release = threading.Event()
    saved = []

    def compute(strategy):
        started.set()
        release.wait(timeout=3)
        return {
            "strategy": strategy,
            "label": "潜力评分",
            "items": [{"symbol": "sh600000"}],
            "market_regime": {"code": "neutral"},
            "_context_signature": "context-v1",
        }

    monkeypatch.setattr(api_server, "_strategy_context_snapshot", lambda: ({}, ["sh600000"], "context-v1"))
    monkeypatch.setattr(api_server, "_compute_strategies", compute)
    monkeypatch.setattr(api_server.state_manager, "save_account_state", lambda key, value: saved.append((key, value)))
    api_server._strategy_cache.clear()
    api_server._strategy_refreshing.clear()
    try:
        before = time.monotonic()
        response = api_server._strategy_cache_response("potential")
        elapsed = time.monotonic() - before

        assert elapsed < 0.5
        assert response["items"] == []
        assert response["refreshing"] is True
        assert started.wait(timeout=1)

        release.set()
        deadline = time.monotonic() + 3
        while "potential" in api_server._strategy_refreshing and time.monotonic() < deadline:
            time.sleep(0.01)

        completed = api_server._strategy_cache_response("potential")
        assert completed["refreshing"] is False
        assert completed["items"] == [{"symbol": "sh600000"}]
        assert completed["stale"] is False
        assert saved
    finally:
        release.set()
        api_server._strategy_cache.clear()
        api_server._strategy_cache.update(original_cache)
        api_server._strategy_refreshing.clear()
        api_server._strategy_refreshing.update(original_refreshing)


def test_strategy_cache_keeps_previous_result_while_context_changes(monkeypatch):
    original_cache = dict(api_server._strategy_cache)
    original_refreshing = set(api_server._strategy_refreshing)
    release = threading.Event()

    def compute(strategy):
        release.wait(timeout=3)
        return {
            "strategy": strategy,
            "label": "潜力评分",
            "items": [{"symbol": "sz000001", "version": "new"}],
            "_context_signature": "new-context",
        }

    monkeypatch.setattr(api_server, "_strategy_context_snapshot", lambda: ({}, ["sz000001"], "new-context"))
    monkeypatch.setattr(api_server, "_compute_strategies", compute)
    monkeypatch.setattr(api_server.state_manager, "save_account_state", lambda key, value: None)
    api_server._strategy_refreshing.clear()
    api_server._strategy_cache.clear()
    api_server._strategy_cache["potential"] = {
        "strategy": "potential",
        "label": "潜力评分",
        "items": [{"symbol": "sh600000", "version": "old"}],
        "generated_at": "2099-01-01T00:00:00",
        "_context_signature": "old-context",
    }
    try:
        response = api_server._strategy_cache_response("potential")

        assert response["items"][0]["version"] == "old"
        assert response["stale"] is True
        assert response["refreshing"] is True
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while "potential" in api_server._strategy_refreshing and time.monotonic() < deadline:
            time.sleep(0.01)
        api_server._strategy_cache.clear()
        api_server._strategy_cache.update(original_cache)
        api_server._strategy_refreshing.clear()
        api_server._strategy_refreshing.update(original_refreshing)
