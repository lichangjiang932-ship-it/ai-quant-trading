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


# ═══════════════════════════════════════════════════════════════
# 因子级反馈闭环 (借鉴 Vibe-Trading 因子监控 / AgentQuant 信号记忆)
# ═══════════════════════════════════════════════════════════════
# 原 signal_type 只有"普通信号/高评分/新闻驱动"三种粗粒度标签, 且只用于
# 前端展示、从不参与决策 —— 等于记了但不学。改为: 买入时把当次真正触发的
# 每个因子逐条落库(待定), 复盘打标后回填胜负, 于是每个因子都有自己的
# 历史胜率; 下次买入时据此对因子加减分, 让失效的因子自动失去话语权。
#
# 表: factor_evidence (date, symbol, side, factor, result_label, pnl)

def _ensure_factor_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, symbol TEXT, name TEXT, side TEXT,
            factor TEXT, result_label TEXT, pnl REAL,
            reason TEXT, created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fe_factor ON factor_evidence(factor)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fe_date ON factor_evidence(date)")


def record_factor_evidence(date_str: str, symbol: str, side: str,
                          factors: List[str], name: str = "", reason: str = ""):
    """买入/卖出时, 把当次触发的每个因子逐条落库, result_label 先置 'pending'。

    同一 (date, symbol, side, factor) 只记一次, 重复调用安全。
    """
    if not factors or not symbol:
        return
    try:
        conn = _conn()
        _ensure_factor_table(conn)
        cur = conn.cursor()
        for f in factors:
            f = str(f or "").strip()
            if not f:
                continue
            cur.execute(
                "SELECT 1 FROM factor_evidence WHERE date=? AND symbol=? AND side=? AND factor=?",
                (date_str, symbol, side, f))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO factor_evidence (date,symbol,name,side,factor,result_label,pnl,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (date_str, symbol, name, side, f, "pending", 0.0,
                 (reason or "")[:120], date.today().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def resolve_factor_evidence(date_str: str, symbol: str, side: str,
                           result_label: str, pnl: float = 0.0):
    """复盘打标后, 把该笔交易所有 pending 因子证据回填为实际结果。"""
    if not result_label or not symbol:
        return
    try:
        conn = _conn()
        _ensure_factor_table(conn)
        conn.execute(
            "UPDATE factor_evidence SET result_label=?, pnl=? "
            "WHERE date=? AND symbol=? AND side=? AND result_label='pending'",
            (result_label, float(pnl or 0), date_str, symbol, side))
        conn.commit()
        conn.close()
    except Exception:
        pass


def factor_stats(days: Optional[int] = None) -> Dict[str, Dict]:
    """统计每个因子的历史表现 (样本数/胜数/胜率/累计盈亏)。"""
    out: Dict[str, Dict] = {}
    try:
        conn = _conn()
        _ensure_factor_table(conn)
        sql = ("SELECT factor, result_label, COUNT(*), SUM(pnl) FROM factor_evidence "
               "WHERE result_label != 'pending'")
        args: List = []
        if days:
            sql += " AND date >= date('now', ?)"
            args.append(f"-{int(days)} days")
        sql += " GROUP BY factor, result_label"
        for factor, label, cnt, pnl in conn.execute(sql, args).fetchall():
            g = out.setdefault(factor, {"samples": 0, "wins": 0, "losses": 0, "pnl": 0.0})
            g["samples"] += cnt
            g["pnl"] += float(pnl or 0)
            if label in ("买对", "卖对"):
                g["wins"] += cnt
            elif label in ("买错", "卖错"):
                g["losses"] += cnt
        conn.close()
    except Exception:
        return {}
    for g in out.values():
        g["win_rate"] = round(g["wins"] / g["samples"] * 100, 1) if g["samples"] else 0.0
        g["pnl"] = round(g["pnl"], 2)
    return out


def factor_adjustment(factors: List[str], days: Optional[int] = None,
                      min_samples: int = 3, cap: float = 10.0) -> Dict:
    """根据各因子的历史胜率, 给出本次买入的综合分调整。

    借鉴思路: 因子会失效。只有样本足够(>= min_samples)时才让历史说话,
    避免小样本噪声把好因子误杀。

    返回 {"adjust": float, "notes": [str], "detail": {factor: {...}}}
    """
    stats = factor_stats(days) if factors else {}
    adjust = 0.0
    notes: List[str] = []
    detail: Dict[str, Dict] = {}
    for f in factors or []:
        g = stats.get(str(f))
        if not g or g.get("samples", 0) < max(int(min_samples), 1):
            continue
        wr = float(g.get("win_rate", 0) or 0)
        n = g["samples"]
        detail[f] = {"samples": n, "win_rate": wr, "pnl": g.get("pnl", 0)}
        if wr < 40.0:
            adjust -= 5
            notes.append(f"因子[{f}]历史胜率{wr:.0f}%({n}样本)偏低, -5")
        elif wr < 50.0:
            adjust -= 2
            notes.append(f"因子[{f}]历史胜率{wr:.0f}%({n}样本)偏弱, -2")
        elif wr >= 65.0:
            adjust += 3
            notes.append(f"因子[{f}]历史胜率{wr:.0f}%({n}样本)优良, +3")
    adjust = max(-abs(cap), min(adjust, abs(cap)))
    return {"adjust": round(adjust, 1), "notes": notes, "detail": detail}


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
