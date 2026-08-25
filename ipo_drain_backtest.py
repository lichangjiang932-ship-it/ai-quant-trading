# -*- coding: utf-8 -*-
"""
巨型 IPO 上市日避险策略 — 事件研究回测
========================================
假设: 超大市值/超大规模 IPO 上市当日会虹吸市场资金, 其他股票普遍承压。
验证: 13 个历史巨型 IPO 上市日, 沪深300/上证/创业板指数的当日表现。

数据来源: 项目 mootdx/腾讯 指数日线 (2018-05 ~ 2026-08)
事件日期来源: 交易所公告 / 证券时报 / 同花顺 (WebSearch 逐条核实)
"""
import json
import os
import sys
from datetime import datetime

import pandas as pd

# 项目数据管道
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frontend.api_server import _load_daily_frame  # noqa: E402

# ═══════════════════════════════════════════════════════════════
# 1. 事件清单 (来源: 交易所公告/证券时报/同花顺)
# ═══════════════════════════════════════════════════════════════
EVENTS = [
    {"date": "2018-06-08", "name": "工业富联", "symbol": "sh601138", "note": "募资271亿, 工业互联网龙头"},
    {"date": "2019-12-10", "name": "邮储银行", "symbol": "sh601658", "note": "募资327亿, 国有大行"},
    {"date": "2020-01-16", "name": "京沪高铁", "symbol": "sh601816", "note": "募资306亿"},
    {"date": "2020-07-16", "name": "中芯国际", "symbol": "sh688981", "note": "募资532亿, 科创板最大IPO"},
    {"date": "2020-10-15", "name": "金龙鱼", "symbol": "sz300999", "note": "募资139亿, 创业板最大IPO"},
    {"date": "2020-11-02", "name": "中金公司", "symbol": "sh601995", "note": "募资132亿"},
    {"date": "2021-06-10", "name": "三峡能源", "symbol": "sh600905", "note": "募资227亿"},
    {"date": "2021-08-20", "name": "中国电信", "symbol": "sh601728", "note": "募资541亿, 回A"},
    {"date": "2021-12-15", "name": "百济神州", "symbol": "sh688235", "note": "募资222亿"},
    {"date": "2022-01-05", "name": "中国移动", "symbol": "sh600941", "note": "募资560亿, 回A"},
    {"date": "2022-04-21", "name": "中国海油", "symbol": "sh600938", "note": "募资323亿, 回A"},
    {"date": "2026-07-27", "name": "长鑫科技", "symbol": "sh688825", "note": "首日成交1411亿, 市值3.28万亿登顶"},
    {"date": "2026-08-19", "name": "宇树科技", "symbol": "sh688836", "note": "人形机器人第一股"},
]

INDEXES = [
    {"symbol": "sh000300", "name": "沪深300"},
    {"symbol": "sz399006", "name": "创业板指"},
    # 注: sh000001 上证指数数据源仅返回近800条(2023起), 覆盖不足已剔除
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "ipo_drain")


def _load_index(symbol: str) -> pd.DataFrame:
    df = _load_daily_frame(symbol, 2000)
    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": df.index.strftime("%Y-%m-%d"),
        "close": df["Close"].astype(float),
    })
    out["ret"] = out["close"].pct_change() * 100
    return out


def _ret_on(df: pd.DataFrame, d: str) -> float | None:
    """T 日涨跌幅 (%)."""
    import numpy as np
    try:
        i = int(np.where(df["date"].values == d)[0][0])
        return float(df["ret"].iloc[i])
    except (IndexError, KeyError, ValueError):
        return None


def _ret_from(df: pd.DataFrame, d: str, hold: int) -> float | None:
    """自 T+1 开盘起持有 hold 个交易日的累计收益 (%) 近似: close[T+hold] vs close[T]."""
    import numpy as np
    try:
        i = int(np.where(df["date"].values == d)[0][0])
        if i + hold >= len(df):
            return None
        base = float(df["close"].iloc[i])
        end = float(df["close"].iloc[i + hold])
        return (end / base - 1) * 100 if base > 0 else None
    except (IndexError, KeyError, ValueError):
        return None


def _baseline_avg(df: pd.DataFrame, d: str) -> float | None:
    """事件前 20 个交易日平均涨跌幅 (对照基准)."""
    import numpy as np
    try:
        i = int(np.where(df["date"].values == d)[0][0])
        if i < 20:
            return None
        return float(df["ret"].iloc[i - 20:i].mean())
    except (IndexError, KeyError, ValueError):
        return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=== 巨型 IPO 上市日事件研究 ===")
    print(f"事件数: {len(EVENTS)} | 指数: {', '.join(x['name'] for x in INDEXES)}")

    trades = []          # 事件级 (以沪深300为主口径)
    per_index = {x["symbol"]: [] for x in INDEXES}
    missing = []

    for ev in EVENTS:
        row = {
            "entry_date": ev["date"],
            "exit_date": ev["date"],
            "side": "long",
            "size": 1,
            "symbol": ev["symbol"],
            "symbol_name": ev["name"],
            "label": f"{ev['name']} 上市日 ({ev['date']})",
        }
        for ix in INDEXES:
            df = _load_index(ix["symbol"])
            if df.empty:
                missing.append(f"{ix['name']}")
                continue
            ret_t = _ret_on(df, ev["date"])
            base = _baseline_avg(df, ev["date"])
            ret_1d = _ret_from(df, ev["date"], 1)
            ret_3d = _ret_from(df, ev["date"], 3)
            ret_5d = _ret_from(df, ev["date"], 5)
            per_index[ix["symbol"]].append({
                "date": ev["date"], "name": ev["name"],
                "ret_T": ret_t, "ret_T1": ret_1d,
                "ret_3d": ret_3d, "ret_5d": ret_5d, "baseline": base,
            })
            if ix["symbol"] == "sh000300":
                row["entry_price"] = round(ret_t, 3) if ret_t is not None else None
                row["pnl_pct"] = ret_t            # 事件日沪深300涨跌幅
                row["pnl"] = ret_t
                row["holding_bars"] = 1
                row["note"] = ev["note"]
        trades.append(row)

    # ── 沪深300 事件级统计 ──
    hs300 = [t for t in trades if t.get("pnl_pct") is not None]
    rets = [t["pnl_pct"] for t in hs300]
    n = len(rets)
    avg = sum(rets) / n if n else 0
    med = sorted(rets)[n // 2] if n else 0
    down = sum(1 for r in rets if r < 0) / n * 100 if n else 0
    worst = min(rets) if rets else 0
    best = max(rets) if rets else 0

    summary = {
        "meta": {
            "strategy_name": "巨型IPO上市日避险策略 (事件研究)",
            "symbol": "沪深300/上证/创业板",
            "start": EVENTS[0]["date"], "end": EVENTS[-1]["date"],
            "initial_cash": 1, "generated_at": datetime.now().isoformat(),
            "market": "china_a", "source": "mootdx/腾讯 指数日线; 事件日期经公开渠道核实",
        },
        "summary": {
            "total_events": n,
            "avg_ret_T": round(avg, 2),
            "median_ret_T": round(med, 2),
            "down_pct": round(down, 1),
            "best_event": round(best, 2),
            "worst_event": round(worst, 2),
            "avg_baseline": None,
        },
    }

    # 对照: 事件前 20 日均涨跌
    base_all = []
    for rec in per_index["sh000300"]:
        if rec["baseline"] is not None:
            base_all.append(rec["baseline"])
    if base_all:
        summary["summary"]["avg_baseline"] = round(sum(base_all) / len(base_all), 2)

    # 明细输出
    print("\n沪深300 各事件当日涨跌:")
    for t in sorted(hs300, key=lambda x: x["pnl_pct"]):
        print(f"  {t['entry_date']} {t['label']:<24s} T日 {t['pnl_pct']:+.2f}%")
    print(f"\n沪深300 汇总: {n} 个事件 | 平均 {avg:+.2f}% | 中位 {med:+.2f}% | 下跌占比 {down:.0f}%")
    print(f"  最好 {best:+.2f}% | 最差 {worst:+.2f}% | 事件前20日均值 {summary['summary']['avg_baseline']}%")

    # 各指数维度
    print("\n各指数维度 (平均 T 日 / T+1 / 3日 / 5日):")
    for ix in INDEXES:
        recs = [r for r in per_index[ix["symbol"]] if r["ret_T"] is not None]
        if not recs:
            continue
        def avg_k(k):
            vals = [r[k] for r in recs if r[k] is not None]
            return sum(vals) / len(vals) if vals else None
        print(f"  {ix['name']}: T {avg_k('ret_T'):+.2f}% | T+1 {avg_k('ret_T1'):+.2f}% | "
              f"3日 {avg_k('ret_3d'):+.2f}% | 5日 {avg_k('ret_5d'):+.2f}%")

    # ── 输出文件 ──
    with open(os.path.join(OUT_DIR, "ipo_drain_trades.csv"), "w", encoding="utf-8", newline="") as f:
        import csv
        cols = ["entry_date", "exit_date", "side", "size", "symbol", "symbol_name",
                "label", "entry_price", "pnl_pct", "pnl", "holding_bars", "note"]
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow(t)
    with open(os.path.join(OUT_DIR, "ipo_drain_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    # 明细表 (供仪表盘)
    rows = []
    for ix in INDEXES:
        for r in per_index[ix["symbol"]]:
            rows.append({"index": ix["name"], **r})
    with open(os.path.join(OUT_DIR, "ipo_drain_detail.csv"), "w", encoding="utf-8", newline="") as f:
        import csv
        writer = csv.DictWriter(f, fieldnames=["index", "date", "name", "ret_T", "ret_T1", "ret_3d", "ret_5d", "baseline"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"\n输出: {OUT_DIR}")
    if missing:
        print(f"[注意] 部分指数数据缺失: {set(missing)}")


if __name__ == "__main__":
    main()
