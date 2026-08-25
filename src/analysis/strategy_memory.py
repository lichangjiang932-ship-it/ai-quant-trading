# -*- coding: utf-8 -*-
"""
策略记忆库 (Strategy Memory) — 借鉴 AgentQuant NLA memory 的简化版
=====================================================================
将每日复盘产生的"信号证据"（每笔成交 + 结果标签 + 信号类型）持久化到 SQLite,
支持按信号类型/日期/股票跨日检索统计胜率 —— 让策略能够"记住"历史并自我评估。

表: strategy_evidence (date, symbol, side, signal_type, result_label, pnl, reason)
"""
import os
import sqlite3
from datetime import date
from typing import Dict, List, Optional

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "strategy_memory.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, symbol TEXT, name TEXT, side TEXT,
            signal_type TEXT, result_label TEXT, pnl REAL,
            reason TEXT, created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_date ON strategy_evidence(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_signal ON strategy_evidence(signal_type)")
    return conn


def record_evidence(entries: List[Dict]):
    """批量写入信号证据 (幂等: 同 date+symbol+side+reason 跳过)。"""
    if not entries:
        return
    try:
        conn = _conn()
        cur = conn.cursor()
        for e in entries:
            cur.execute(
                "SELECT 1 FROM strategy_evidence WHERE date=? AND symbol=? AND side=? AND reason=?",
                (e.get("date", ""), e.get("symbol", ""), e.get("side", ""), e.get("reason", "")[:100]))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO strategy_evidence (date,symbol,name,side,signal_type,result_label,pnl,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (e.get("date", ""), e.get("symbol", ""), e.get("name", ""), e.get("side", ""),
                 e.get("signal_type", "普通信号"), e.get("result_label", ""),
                 float(e.get("pnl", 0) or 0), (e.get("reason", "") or "")[:100],
                 date.today().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def query_stats(signal_type: Optional[str] = None, days: Optional[int] = None) -> Dict:
    """按信号类型统计历史结果 (对/错 + 胜率)。"""
    try:
        conn = _conn()
        sql = "SELECT signal_type, side, result_label, COUNT(*) FROM strategy_evidence"
        conds, args = [], []
        if signal_type:
            conds.append("signal_type=?")
            args.append(signal_type)
        if days:
            conds.append("date >= date('now', ?)")
            args.append(f"-{days} days")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " GROUP BY signal_type, side, result_label"
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        groups = {}
        for st, side, label, cnt in rows:
            g = groups.setdefault(st, {"buy_对": 0, "buy_错": 0, "sell_对": 0, "sell_错": 0, "total": 0})
            # 归一化结果标签: 买对/卖对 → 对; 买错/卖错 → 错
            norm = "对" if label in ("买对", "卖对") else ("错" if label in ("买错", "卖错") else label)
            key = f"{side}_{norm}"
            if key in g:
                g[key] += cnt
            g["total"] += cnt
        for st, g in groups.items():
            right = g["buy_对"] + g["sell_对"]
            g["win_rate"] = round(right / g["total"] * 100, 1) if g["total"] else 0
        return {"signals": groups, "total": sum(g["total"] for g in groups.values())}
    except Exception:
        return {"signals": {}, "total": 0}
