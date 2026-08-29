# -*- coding: utf-8 -*-
"""自选股(收藏)存储 — 分组 + 元数据 + 黑名单
=================================================

原来自选只是 config.yaml 里的一个**扁平代码列表**, 存在几个实际问题:

1. 只能表达"我关注它", 表达不了"我在观察它 / 我拉黑它" —— 没有黑名单,
   想排除某只股票只能靠策略参数硬编码;
2. 没有元数据: 为什么加的、目标价多少、什么标签, 全部丢失,
   过几周再看只剩一串代码, 无法复盘当时的判断;
3. 每次增删都 `config.save_config()` 整体重写 config.yaml —— 既会冲掉
   注释与格式, 并发写也有风险;
4. 空列表会静默回退到 `trading.symbols`, 导致"清空自选"这个操作不生效
   (清完又冒出 4 只默认股)。

改为独立 JSON 存储 (`data/watchlist.json`), 每条记录带分组与元数据:

    core      核心池  —— 主动关注, 选股时给予加权
    watch     观察   —— 仅看行情, 不参与选股加权
    blacklist 黑名单 —— 永不选入(任何人为主观排除/踩过坑的股票)

首次加载时自动把旧的扁平列表迁移为 core 分组, 不丢数据。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_STORE_PATH = os.path.join(_REPO_ROOT, "data", "watchlist.json")

_lock = threading.Lock()

# 分组: 核心池 / 观察 / 黑名单
GROUPS = ("core", "watch", "blacklist")
DEFAULT_GROUP = "core"

# 选股时各分组的含义
#   core      → 加权(人为研究过的标的, 值得优先)
#   watch     → 中性(只看不选优)
#   blacklist → 直接排除
GROUP_BUY_ADJUST = {"core": 4.0, "watch": 0.0}
BLACKLIST_GROUP = "blacklist"


def _load_raw() -> Dict:
    if not os.path.exists(_STORE_PATH):
        return {}
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_raw(data: Dict):
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    # 原子写: 先写 tmp 再替换, 避免写一半被读到 (Windows 下也稳)
    tmp = _STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _STORE_PATH)


def _normalize(symbol: str) -> str:
    """统一为 sh/sz/bj + 6 位代码。"""
    v = str(symbol or "").strip().lower()
    v = re.sub(r"^(sh|sz|bj)", "", v)
    v = re.sub(r"\.(sh|sz|bj)$", "", v)
    m = re.search(r"(\d{6})", v)
    if not m:
        return ""
    code = m.group(1)
    # 北交所: 43/83/87/88 开头, 以及 2023 年启用的 920xxx 新代码段
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    # 沪市: 60/68 主板科创板, 5 开头基金/权证, 9 开头 B 股(900xxx)
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def load_entries() -> Dict[str, Dict]:
    """返回 {symbol: entry}, 只读快照。"""
    with _lock:
        data = _load_raw()
    entries = data.get("entries") or {}
    out: Dict[str, Dict] = {}
    for sym, e in entries.items():
        norm = _normalize(sym)
        if not norm or not isinstance(e, dict):
            continue
        rec = dict(e)
        rec["symbol"] = norm
        rec["group"] = rec.get("group") if rec.get("group") in GROUPS else DEFAULT_GROUP
        rec.setdefault("note", "")
        rec.setdefault("tags", [])
        rec.setdefault("added_at", "")
        out[norm] = rec
    return out


def migrate_from_flat(symbols: List[str], group: str = DEFAULT_GROUP) -> int:
    """把旧的扁平代码列表迁移进来 (已存在的不覆盖)。返回新增条数。"""
    if not symbols:
        return 0
    with _lock:
        data = _load_raw()
        entries = dict(data.get("entries") or {})
        added = 0
        now = datetime.now().isoformat(timespec="seconds")
        for s in symbols:
            norm = _normalize(str(s or ""))
            if not norm or norm in entries:
                continue
            entries[norm] = {
                "group": group if group in GROUPS else DEFAULT_GROUP,
                "note": "",
                "tags": [],
                "target_price": None,
                "added_at": now,
                "source": "migrate",
            }
            added += 1
        data["version"] = 1
        data["entries"] = entries
        _save_raw(data)
    return added


def add(symbol: str, group: str = DEFAULT_GROUP, note: str = "",
        tags: Optional[List[str]] = None, target_price: Optional[float] = None,
        name: str = "", source: str = "manual") -> Optional[Dict]:
    """新增或更新一条自选记录。返回更新后的 entry, 无法识别代码返回 None。"""
    norm = _normalize(symbol)
    if not norm:
        return None
    if group not in GROUPS:
        group = DEFAULT_GROUP
    with _lock:
        data = _load_raw()
        entries = dict(data.get("entries") or {})
        now = datetime.now().isoformat(timespec="seconds")
        old = dict(entries.get(norm) or {})
        entry = {
            "group": group,
            "note": str(note or "")[:200],
            "tags": [str(t)[:20] for t in (tags or [])][:8],
            "target_price": float(target_price) if target_price else None,
            "name": str(name or old.get("name", ""))[:40],
            "added_at": old.get("added_at") or now,
            "updated_at": now,
            "source": str(source or "manual")[:20],
        }
        entries[norm] = entry
        data["version"] = 1
        data["entries"] = entries
        _save_raw(data)
    entry["symbol"] = norm
    return entry


def remove(symbol: str) -> bool:
    norm = _normalize(symbol)
    if not norm:
        return False
    with _lock:
        data = _load_raw()
        entries = dict(data.get("entries") or {})
        if norm not in entries:
            return False
        entries.pop(norm)
        data["entries"] = entries
        _save_raw(data)
    return True


def set_group(symbol: str, group: str) -> Optional[Dict]:
    """调整分组 (核心池/观察/黑名单)。返回更新后的 entry。"""
    if group not in GROUPS:
        return None
    norm = _normalize(symbol)
    if not norm:
        return None
    with _lock:
        data = _load_raw()
        entries = dict(data.get("entries") or {})
        if norm not in entries:
            return None
        entries[norm]["group"] = group
        entries[norm]["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["entries"] = entries
        _save_raw(data)
        out = dict(entries[norm])
    out["symbol"] = norm
    return out


def get(symbol: str) -> Optional[Dict]:
    norm = _normalize(symbol)
    if not norm:
        return None
    e = load_entries().get(norm)
    return e


def list_group(group: Optional[str] = None) -> List[str]:
    """列出某分组(或全部)的代码, 保持插入顺序。"""
    entries = load_entries()
    out = []
    for sym, e in entries.items():
        if group is None or e.get("group") == group:
            out.append(sym)
    return out


def blacklist() -> List[str]:
    return list_group(BLACKLIST_GROUP)


def is_blacklisted(symbol: str) -> bool:
    norm = _normalize(symbol)
    return bool(norm) and norm in set(blacklist())


def watch_symbols() -> List[str]:
    """参与行情/选股的代码 = 全部非黑名单 (core + watch)。"""
    return [s for s in load_entries().keys() if not is_blacklisted(s)]


def buy_adjust(symbol: str) -> float:
    """选股加权: 核心池加分, 观察组不加权, 黑名单由调用方直接排除。

    只给人为主观研究过的标的加分 —— 数量级刻意做小(±4), 不能盖过
    客观因子, 否则自选就变成了 bypass 风控的后门。
    """
    e = get(symbol)
    if not e:
        return 0.0
    return float(GROUP_BUY_ADJUST.get(e.get("group"), 0.0))


def stats() -> Dict:
    entries = load_entries()
    by_group = {g: 0 for g in GROUPS}
    for e in entries.values():
        g = e.get("group", DEFAULT_GROUP)
        by_group[g] = by_group.get(g, 0) + 1
    return {"total": len(entries), "by_group": by_group}
