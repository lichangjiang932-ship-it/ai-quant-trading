# -*- coding: utf-8 -*-
"""自选股存储 (src/data/watchlist_store.py) 单元测试。

用 tmp_path 隔离, 不污染 data/watchlist.json。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.data import watchlist_store as ws


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_STORE_PATH", str(tmp_path / "watchlist.json"))


# ─────────────── 代码归一化 ───────────────
@pytest.mark.parametrize("raw,expected", [
    ("sh600000", "sh600000"),
    ("600000", "sh600000"),
    ("600000.SH", "sh600000"),
    ("sz000001", "sz000001"),
    ("000001", "sz000001"),
    ("920288", "bj920288"),   # 北交所 8/4 开头
    ("", ""),
    ("abc", ""),
])
def test_normalize(raw, expected):
    assert ws._normalize(raw) == expected


# ─────────────── 增删 ───────────────
def test_add_and_get():
    e = ws.add("sh600000", group="core", note="测试备注", tags=["银行"])
    assert e["symbol"] == "sh600000"
    assert e["group"] == "core"
    assert e["note"] == "测试备注"
    assert e["tags"] == ["银行"]
    assert e["added_at"]
    assert ws.get("sh600000")["group"] == "core"


def test_add_is_idempotent_by_symbol():
    ws.add("sh600000", group="core")
    ws.add("600000", group="watch")   # 同一只, 换分组
    assert len(ws.list_group()) == 1
    assert ws.get("sh600000")["group"] == "watch"


def test_add_preserves_added_at_on_update():
    ws.add("sh600000", group="core")
    first = ws.get("sh600000")["added_at"]
    ws.add("sh600000", group="watch")
    assert ws.get("sh600000")["added_at"] == first  # added_at 不因更新而变


def test_add_rejects_invalid_symbol():
    assert ws.add("not-a-code") is None
    assert ws.add("") is None


def test_add_invalid_group_falls_back_to_core():
    ws.add("sh600000", group="bogus")
    assert ws.get("sh600000")["group"] == "core"


def test_remove():
    ws.add("sh600000")
    assert ws.remove("sh600000") is True
    assert ws.get("sh600000") is None
    assert ws.remove("sh600000") is False  # 重复删返回 False


# ─────────────── 分组 ───────────────
def test_set_group():
    ws.add("sh600000", group="core")
    e = ws.set_group("sh600000", "blacklist")
    assert e["group"] == "blacklist"
    assert ws.get("sh600000")["group"] == "blacklist"


def test_set_group_invalid():
    ws.add("sh600000")
    assert ws.set_group("sh600000", "nope") is None


def test_set_group_missing_symbol():
    assert ws.set_group("sh699999", "core") is None


def test_list_group_filters():
    ws.add("sh600000", group="core")
    ws.add("sz000001", group="watch")
    ws.add("sz300750", group="blacklist")
    assert ws.list_group("core") == ["sh600000"]
    assert ws.list_group("watch") == ["sz000001"]
    assert ws.list_group("blacklist") == ["sz300750"]
    assert len(ws.list_group()) == 3


# ─────────────── 黑名单 ───────────────
def test_blacklist_excluded_from_watch_symbols():
    ws.add("sh600000", group="core")
    ws.add("sz300750", group="blacklist")
    assert "sz300750" not in ws.watch_symbols()
    assert "sh600000" in ws.watch_symbols()
    assert ws.is_blacklisted("sz300750") is True
    assert ws.is_blacklisted("sh600000") is False


def test_is_blacklisted_unknown_symbol():
    assert ws.is_blacklisted("sh600000") is False


# ─────────────── 选股加权 ───────────────
def test_buy_adjust_core_gets_small_bonus():
    """核心池加分, 但幅度要小 —— 自选不能成为绕过风控的后门。"""
    ws.add("sh600000", group="core")
    adj = ws.buy_adjust("sh600000")
    assert 0 < adj <= 5


def test_buy_adjust_watch_is_neutral():
    ws.add("sz000001", group="watch")
    assert ws.buy_adjust("sz000001") == 0.0


def test_buy_adjust_unknown_is_zero():
    assert ws.buy_adjust("sh600000") == 0.0


# ─────────────── 迁移 ───────────────
def test_migrate_from_flat():
    n = ws.migrate_from_flat(["sh600000", "600104", "sz000001.SZ", "垃圾"])
    assert n == 3  # 无效代码被丢弃
    assert set(ws.list_group("core")) == {"sh600000", "sh600104", "sz000001"}


def test_migrate_does_not_overwrite_existing():
    ws.add("sh600000", group="blacklist", note="原有标记")
    ws.migrate_from_flat(["sh600000", "sz000001"])
    assert ws.get("sh600000")["group"] == "blacklist"  # 不覆盖
    assert ws.get("sh600000")["note"] == "原有标记"
    assert ws.get("sz000001")["group"] == "core"


def test_migrate_empty_list():
    assert ws.migrate_from_flat([]) == 0
    assert ws.list_group() == []


# ─────────────── 统计与持久化 ───────────────
def test_stats_counts_by_group():
    ws.add("sh600000", group="core")
    ws.add("sz000001", group="core")
    ws.add("sz300750", group="blacklist")
    s = ws.stats()
    assert s["total"] == 3
    assert s["by_group"]["core"] == 2
    assert s["by_group"]["blacklist"] == 1
    assert s["by_group"]["watch"] == 0


def test_persists_across_load():
    ws.add("sh600000", group="core", note="持久化测试")
    # 重新从磁盘读取 (fixture 内路径不变)
    assert ws.load_entries()["sh600000"]["note"] == "持久化测试"


def test_load_entries_normalizes_keys_and_defaults():
    ws.add("600000")  # 不带前缀写入
    entries = ws.load_entries()
    assert "sh600000" in entries
    e = entries["sh600000"]
    for k in ("group", "note", "tags", "added_at", "symbol"):
        assert k in e


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
