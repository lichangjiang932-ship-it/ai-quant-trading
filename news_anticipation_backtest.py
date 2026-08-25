# -*- coding: utf-8 -*-
"""
"消息反应速度"事件研究 — 价格是否提前反应? 追消息还能赚吗?
================================================================
用户论点: 股价是市场的提前预测; 公开消息时反应多已兑现, 反应快才能赚。

验证1 (主力): 大涨日(≥5%, 消息/资金强反应代理)次日买入 → 持有 1/3/5/10 日
    若平均收益≈0 或为负 → 证实"消息公开时反应已兑现, 追买接盘"
验证2 (补充): 业绩预增公告前 5 日 vs 后 5 日累计涨幅
    若前5日 > 后5日 → 证实"价格提前预测, 公告日兑现"

数据: 项目 mootdx/腾讯 日线 (2024-07 ~ 2026-08, 500条/股)
"""
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frontend.api_server import _load_daily_frame  # noqa: E402

# 标的池: 高流动性代表性个股 (沪深300/创业板/科创板)
UNIVERSE = [
    "sh600519", "sz000858", "sh600809", "sz000568",   # 白酒
    "sz300750", "sz002594", "sz002460",                # 新能源车链
    "sz300760", "sh600276", "sz002371", "sh688981",    # 医药/半导体
    "sz300059", "sh600030",                            # 券商
    "sh601012", "sz300274", "sh600438",                # 光伏
    "sh601318", "sh600036", "sh601398",                # 金融
    "sz000725", "sz002475", "sh603501",                # 电子
    "sh688036", "sz300782",                            # 科技
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "news_anticipation")


def load(symbol: str) -> pd.DataFrame:
    df = _load_daily_frame(symbol, 500)
    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": df.index.strftime("%Y-%m-%d"),
        "open": df["Open"].astype(float),
        "close": df["Close"].astype(float),
    })
    out["ret"] = out["close"].pct_change() * 100
    out["ret_open"] = out["open"].pct_change() * 100  # 前收→今开 (跳空+开盘走势)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"=== 消息反应速度事件研究 | 标的池 {len(UNIVERSE)} 只 | 覆盖 2024-07~2026-08 ===")

    frames = {}
    for sym in UNIVERSE:
        df = load(sym)
        if len(df) > 100:
            frames[sym] = df

    # ─────────────────────────────────────────────
    # 验证1: 大涨日(≥5%)次日买入, 持有 N 日
    # ─────────────────────────────────────────────
    events_buy = []   # 大涨日事件
    events_all = []   # 基准: 所有交易日
    for sym, df in frames.items():
        closes = df["close"].values
        for i in range(1, len(df) - 10):
            ret_t = float(df["ret"].iloc[i])
            base = closes[i]                       # 事件日收盘 (消息反应当日)
            if base <= 0:
                continue
            ret_1 = (closes[i+1] / base - 1) * 100
            ret_3 = (closes[i+3] / base - 1) * 100
            ret_5 = (closes[i+5] / base - 1) * 100
            ret_10 = (closes[i+10] / base - 1) * 100
            row = {"date": df["date"].iloc[i], "ret_T": round(ret_t, 2),
                   "ret_1": round(ret_1, 2), "ret_3": round(ret_3, 2),
                   "ret_5": round(ret_5, 2), "ret_10": round(ret_10, 2)}
            events_all.append(row)
            if ret_t >= 5.0:
                events_buy.append({**row, "symbol": sym})
            elif ret_t <= -5.0:
                events_buy.append({**row, "symbol": sym, "_crash": True})

    def stats(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        if not vals:
            return None
        return {"n": len(vals), "avg": round(sum(vals)/len(vals), 2),
                "med": round(sorted(vals)[len(vals)//2], 2),
                "win": round(sum(1 for v in vals if v > 0)/len(vals)*100, 1)}

    def show(label, rows):
        s1, s3, s5, s10 = (stats(rows, "ret_1"), stats(rows, "ret_3"),
                           stats(rows, "ret_5"), stats(rows, "ret_10"))
        if not s1:
            print(f"  {label}: 无样本")
            return
        print(f"  {label}: n={s1['n']}")
        print(f"    持有1日 均{s1['avg']:+.2f}% 胜率{s1['win']:.0f}% | "
              f"3日 均{s3['avg']:+.2f}% 胜率{s3['win']:.0f}% | "
              f"5日 均{s5['avg']:+.2f}% 胜率{s5['win']:.0f}% | "
              f"10日 均{s10['avg']:+.2f}% 胜率{s10['win']:.0f}%")

    print("\n┌─ 验证1: 事件日收盘买入 → 持有 N 日 (事件日当天 = 消息/资金强烈反应) ─┐")
    big_up = [e for e in events_buy if not e.get("_crash")]
    big_dn = [e for e in events_buy if e.get("_crash")]
    show("基准(全部交易日)", events_all)
    show("大涨日 ≥5% (消息兑现日)", big_up)
    show("大跌日 ≤-5% (利空兑现日)", big_dn)
    print("└──────────────────────────────────────────────────────────────────┘")

    # 控制变量: 大涨日次日开盘买入 (真正可执行时点, 更贴近现实)
    print("\n┌─ 验证1b: 大涨日次日开盘买入 → 持有 N 日 (现实中可执行) ─┐")
    rows_next_open = []
    for sym, df in frames.items():
        for i in range(2, len(df) - 10):
            if float(df["ret"].iloc[i-1]) >= 5.0:   # 昨日大涨
                buy_px = float(df["open"].iloc[i])   # 今日开盘买入
                if buy_px <= 0:
                    continue
                r1 = (df["close"].iloc[i] / buy_px - 1) * 100
                r3 = (df["close"].iloc[i+2] / buy_px - 1) * 100
                r5 = (df["close"].iloc[i+4] / buy_px - 1) * 100
                r10 = (df["close"].iloc[i+9] / buy_px - 1) * 100
                rows_next_open.append({"ret_1": r1, "ret_3": r3, "ret_5": r5, "ret_10": r10})
    show("大涨次日开盘买入", rows_next_open)
    print("└──────────────────────────────────────────────────────────────────┘")

    # ─────────────────────────────────────────────
    # 验证2: 业绩预增公告 前5日 vs 后5日
    # ─────────────────────────────────────────────
    print("\n┌─ 验证2: 业绩预增公告 提前反应程度 (公告日=0) ─┐")
    ann_rows = []
    try:
        from src.news.news_fetcher import NewsFetcher
        f = NewsFetcher()
        for sym in UNIVERSE:
            try:
                items = f.fetch_stock_announcements(symbol=sym, count=30)
                for it in items:
                    title = it.title or ""
                    if not any(k in title for k in ("预增", "业绩预告", "业绩快报", "预盈")):
                        continue
                    d = str(it.publish_time)[:10]
                    df = frames.get(sym)
                    if df is None:
                        continue
                    try:
                        i = int(np.where(df["date"].values == d)[0][0])
                    except (IndexError, ValueError):
                        continue
                    if i < 5 or i + 5 >= len(df):
                        continue
                    pre = (df["close"].iloc[i] / df["close"].iloc[i-5] - 1) * 100
                    post = (df["close"].iloc[i+5] / df["close"].iloc[i] - 1) * 100
                    ann_rows.append({"symbol": sym, "date": d, "title": title[:30],
                                     "pre_5d": round(float(pre), 2), "post_5d": round(float(post), 2)})
            except Exception:
                continue
    except Exception as e:
        print(f"  公告拉取失败: {str(e)[:60]}")
    if ann_rows:
        pres = [r["pre_5d"] for r in ann_rows]
        posts = [r["post_5d"] for r in ann_rows]
        print(f"  样本: {len(ann_rows)} 个预增类公告")
        print(f"  公告前5日累计 均{sum(pres)/len(pres):+.2f}% | 公告后5日累计 均{sum(posts)/len(posts):+.2f}%")
        for r in ann_rows[:10]:
            print(f"    {r['date']} {r['symbol']} 前5日{r['pre_5d']:+.1f}% / 后5日{r['post_5d']:+.1f}% | {r['title']}")
        pre_win = sum(1 for p in pres if p > 0) / len(pres) * 100
        post_win = sum(1 for p in posts if p > 0) / len(posts) * 100
        print(f"  前5日上涨占比 {pre_win:.0f}% | 后5日上涨占比 {post_win:.0f}%")
    else:
        print("  未获取到预增类公告样本 (公告接口仅返回近期数据, 历史覆盖有限)")
    print("└──────────────────────────────────────────────────────────────────┘")

    # ── 输出 ──
    trades = []
    for e in big_up[:200]:
        trades.append({"entry_date": e["date"], "exit_date": e["date"],
                       "side": "long", "size": 1, "symbol": e.get("symbol", ""),
                       "label": f"大涨日 {e.get('symbol','')} {e['date']} (+{e['ret_T']}%)",
                       "pnl_pct": e["ret_1"], "pnl": e["ret_1"], "holding_bars": 1})
    summary = {
        "meta": {"strategy_name": "消息反应速度事件研究", "start": "2024-07", "end": "2026-08",
                 "generated_at": datetime.now().isoformat(), "market": "china_a",
                 "source": "mootdx/腾讯 日线; 公告经东财接口"},
        "summary": {"big_up_events": len(big_up), "big_dn_events": len(big_dn),
                    "all_days": len(events_all), "announcement_samples": len(ann_rows)},
    }
    import csv
    with open(os.path.join(OUT_DIR, "news_anticipation_trades.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["entry_date", "exit_date", "side", "size", "symbol", "label", "pnl_pct", "pnl", "holding_bars"])
        w.writeheader()
        for t in trades:
            w.writerow(t)
    with open(os.path.join(OUT_DIR, "news_anticipation_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    if ann_rows:
        with open(os.path.join(OUT_DIR, "announcement_events.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["symbol", "date", "title", "pre_5d", "post_5d"])
            w.writeheader()
            for r in ann_rows:
                w.writerow(r)
    print(f"\n输出: {OUT_DIR}")


if __name__ == "__main__":
    main()
