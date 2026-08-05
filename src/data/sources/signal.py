"""
信号层 (Layer 3) — 同花顺热点归因 + 北向资金 + 龙虎榜 + 限售解禁 + 行业排名

┌──────────────┬──────┬────────────────────────────────────────────┐
│ 同花顺热点    │ HTTP │ 当日强势股 + 题材归因 reason tags (73ms)     │
│ 同花顺北向    │ HTTP │ hgt/sgt 分钟资金流向 + 本地自缓存            │
│ 百度概念      │ HTTP │ 概念板块归属(行业/概念/地域)                │
│ 龙虎榜席位    │ HTTP │ 上榜记录 + 买卖席位TOP5 + 机构动向           │
│ 全市场龙虎榜  │ HTTP │ 每日全市场上榜股票 + 净买额排名              │
│ 限售解禁      │ HTTP │ 历史解禁 + 未来90天待解禁                   │
│ 行业排名      │ HTTP │ 东财行业涨跌/上涨下跌家数                    │
└──────────────┴──────┴────────────────────────────────────────────┘
"""
import os
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from ..em_client import em_get, eastmoney_datacenter
from .quote import UA, normalize_code


# ==================== 3.1 同花顺热点 — 当日强势股 + 题材归因 ====================

def ths_hot_reason(date: Optional[str] = None) -> pd.DataFrame:
    """
    同花顺当日强势股归因。
    date: 'YYYY-MM-DD' 格式, None=今天
    返回 DataFrame, 含每只股票的题材标签(reason)。

    核心字段: 代码, 名称, 题材归因(reason), 涨幅%, 换手率%, 成交额, 大单净量
    实测: 73ms 拿到 ~125 只 + 完整字段, 零鉴权
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return pd.DataFrame()
        rows = data.get("data") or []
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        rename_map = {
            "name": "名称", "code": "代码", "reason": "题材归因",
            "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
            "huanshou": "换手率%", "chengjiaoe": "成交额",
            "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
            "market": "市场",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        return df
    except Exception:
        return pd.DataFrame()


def ths_hot_topic_ranking(date: Optional[str] = None) -> List[tuple]:
    """
    当日题材热度排名。
    返回: [(topic, count), ...] 按热度降序
    """
    df = ths_hot_reason(date)
    if df.empty or "题材归因" not in df.columns:
        return []

    all_tags = []
    for r in df["题材归因"].dropna():
        tags = [t.strip() for t in str(r).split("+") if t.strip()]
        all_tags.extend(tags)

    return Counter(all_tags).most_common(30)


# ==================== 3.2 同花顺北向资金 — 分钟级 + 自缓存 ====================

_HSGT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def _northbound_cache_path() -> Path:
    p = Path.home() / ".tradingagents" / "cache" / "northbound_daily.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def hsgt_realtime() -> pd.DataFrame:
    """
    沪深股通当日实时分钟流向(含集合竞价 09:10-15:00, 262个时间点)。
    返回: time, hgt_yi(沪股通累计净买入亿), sgt_yi(深股通累计净买入亿)
    """
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    try:
        r = requests.get(url, headers=_HSGT_HEADERS, timeout=10)
        d = r.json()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])
        n = len(times)
        return pd.DataFrame({
            "time": times,
            "hgt_yi": hgt[:n] + [None] * max(0, n - len(hgt)),
            "sgt_yi": sgt[:n] + [None] * max(0, n - len(sgt)),
        })
    except Exception:
        return pd.DataFrame()


def save_northbound_snapshot(date: str, hgt: float, sgt: float):
    """写入/更新当天北向收盘数据到 CSV"""
    path = _northbound_cache_path()
    rows = {}
    if path.exists():
        for line in path.read_text().strip().split("\n")[1:]:
            parts = line.split(",")
            if len(parts) == 3:
                rows[parts[0]] = line
    rows[date] = f"{date},{hgt},{sgt}"
    with open(path, "w") as f:
        f.write("date,hgt,sgt\n")
        for d in sorted(rows.keys()):
            f.write(rows[d] + "\n")


def load_northbound_history(n: int = 20) -> pd.DataFrame:
    """读取最近 N 天北向历史"""
    path = _northbound_cache_path()
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path).tail(n)


def northbound_today_summary() -> Dict:
    """今日北向资金摘要"""
    df = hsgt_realtime()
    if df.empty:
        return {}
    last = df.dropna().iloc[-1] if not df.dropna().empty else None
    if last is None:
        return {}
    date_str = datetime.now().strftime("%Y-%m-%d")
    hgt_v = float(last["hgt_yi"])
    sgt_v = float(last["sgt_yi"])
    total = hgt_v + sgt_v
    # 自动缓存
    save_northbound_snapshot(date_str, hgt_v, sgt_v)
    return {
        "date": date_str,
        "hgt_yi": hgt_v,
        "sgt_yi": sgt_v,
        "total_yi": total,
        "direction": "净流入" if total > 0 else "净流出",
        "points": len(df),
    }


# ==================== 3.3 龙虎榜 ====================

def dragon_tiger_board(code: str, trade_date: str, look_back: int = 30) -> Dict:
    """
    龙虎榜数据聚合。
    返回: {records: [...]  seats: {buy: [...], sell: [...]}  institution: {...}}
    """
    code = normalize_code(code)
    start = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)
    start_str = start.strftime("%Y-%m-%d")

    # 上榜记录
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{start_str}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
        page_size=50, sort_columns="TRADE_DATE", sort_types="-1",
    )
    records = []
    for row in data:
        records.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "reason": row.get("EXPLANATION", ""),
            "net_buy": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
        })

    seats = {"buy": [], "sell": []}
    if not records:
        return {"records": records, "seats": seats, "institution": {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}}

    latest_date = records[0]["date"]

    # 买入席位
    buy_data = eastmoney_datacenter(
        "RPT_BILLBOARD_DAILYDETAILSBUY",
        filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
        page_size=10, sort_columns="BUY", sort_types="-1",
    )
    for row in buy_data[:5]:
        seats["buy"].append({
            "name": row.get("OPERATEDEPT_NAME", ""),
            "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
            "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
            "net": round((row.get("NET") or 0) / 10000, 1),
        })

    # 卖出席位
    sell_data = eastmoney_datacenter(
        "RPT_BILLBOARD_DAILYDETAILSSELL",
        filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
        page_size=10, sort_columns="SELL", sort_types="-1",
    )
    for row in sell_data[:5]:
        seats["sell"].append({
            "name": row.get("OPERATEDEPT_NAME", ""),
            "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
            "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
            "net": round((row.get("NET") or 0) / 10000, 1),
        })

    # 机构买卖统计
    institution = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0}
    for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail_data:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                if side == "buy":
                    institution["buy_amt"] += amt
                else:
                    institution["sell_amt"] += amt
    institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
    institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
    institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)

    return {"records": records, "seats": seats, "institution": institution}


def daily_dragon_tiger(trade_date: Optional[str] = None, min_net_buy: Optional[float] = None) -> Dict:
    """
    全市场龙虎榜。
    min_net_buy: 净买入下限(万元), None=不过滤
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
        page_size=500, sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
    )
    if not data:
        return {"date": trade_date, "total_records": 0, "stocks": []}

    actual_date = str(data[0].get("TRADE_DATE", ""))[:10] if data else trade_date
    stocks = []
    for row in data:
        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if min_net_buy is not None and net_buy < min_net_buy:
            continue
        stocks.append({
            "code": row.get("SECURITY_CODE", ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", ""),
            "close": row.get("CLOSE_PRICE") or 0,
            "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
            "net_buy_wan": round(net_buy, 1),
            "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
            "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
            "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
        })
    return {"date": actual_date, "total_records": len(stocks), "stocks": stocks}


# ==================== 3.4 限售解禁日历 ====================

def lockup_expiry(code: str, trade_date: str, forward_days: int = 90) -> Dict:
    """
    限售解禁日历。
    返回: {history: [...], upcoming: [...]}
    """
    code = normalize_code(code)
    history_data = eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=15, sort_columns="FREE_DATE", sort_types="-1",
    )
    history = []
    for row in history_data:
        history.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": row.get("FREE_RATIO", 0),
        })

    end_date = datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=forward_days)
    end_str = end_date.strftime("%Y-%m-%d")
    upcoming_data = eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")(FREE_DATE>="{(datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")}")(FREE_DATE<="{end_str}")',
        page_size=20, sort_columns="FREE_DATE", sort_types="1",
    )
    upcoming = []
    for row in upcoming_data:
        upcoming.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": row.get("FREE_RATIO", 0),
        })

    return {"history": history, "upcoming": upcoming}


# ==================== 3.5 行业板块排名 ====================

def industry_comparison(top_n: int = 20) -> Dict:
    """
    全行业涨跌幅排名(东财行业板块, ~100个行业)。
    返回: {top: [...], bottom: [...], total: int}
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": UA}
    try:
        r = em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return {"top": [], "bottom": [], "total": 0}

        rows = []
        for i, item in enumerate(items):
            rows.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": item.get("f3", 0),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
                "leader_change": item.get("f136", 0),
            })

        return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}
    except Exception:
        return {"top": [], "bottom": [], "total": 0}


# ==================== 信号层组合用法 ====================

def market_breadth(trade_date: Optional[str] = None) -> Dict:
    """
    市场宽度/涨跌分布统计 — 东财 getTopicZDFenBu。
    返回: 涨/跌/平家数, 涨停/跌停家数, 分布表(按涨幅区间), 赚钱效应。
    """
    date_str = (trade_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    url = "https://push2ex.eastmoney.com/getTopicZDFenBu"
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 100,
        "sort": "fbt:asc",
        "date": date_str,
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        fb = data.get("data", {}).get("fenbu", [])
        if not fb:
            return {"available": False, "reason": "empty response"}

        dist = {}
        up = down = flat = limit_up = limit_down = 0
        for item in fb:
            for pct_str, cnt in item.items():
                pct = int(pct_str)
                cnt = int(cnt)
                dist[pct] = cnt
                if pct > 0:
                    up += cnt
                elif pct < 0:
                    down += cnt
                else:
                    flat += cnt
                if pct >= 10:
                    limit_up += cnt
                if pct <= -10:
                    limit_down += cnt

        total = up + down + flat
        return {
            "available": True,
            "date": trade_date or datetime.now().strftime("%Y-%m-%d"),
            "up": up, "down": down, "flat": flat,
            "total": total,
            "limit_up": limit_up, "limit_down": limit_down,
            "up_ratio_pct": round(up / total * 100, 2) if total else 0,
            "breadth": round((up - down) / max(total, 1) * 100, 2),
            "distribution": dist,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def market_overview_signal(trade_date: Optional[str] = None) -> Dict:
    """
    市场全景快照: 题材热度 + 北向流向 + 行业对比 + 涨跌家数, 一站式信号摘要。
    返回可序列化为 JSON 的 dict。
    """
    topics = ths_hot_topic_ranking(trade_date)
    north = northbound_today_summary()
    industry = industry_comparison(5)
    dt = daily_dragon_tiger(trade_date)
    breadth = market_breadth(trade_date)

    return {
        "date": trade_date or datetime.now().strftime("%Y-%m-%d"),
        "hot_topics": [{"topic": t, "stock_count": n} for t, n in topics[:10]],
        "northbound": north,
        "breadth": {
            "up": breadth.get("up", 0), "down": breadth.get("down", 0),
            "limit_up": breadth.get("limit_up", 0),
            "limit_down": breadth.get("limit_down", 0),
            "up_ratio_pct": breadth.get("up_ratio_pct", 0),
            "breadth": breadth.get("breadth", 0),
            "available": breadth.get("available", False),
        },
        "top_industries": industry.get("top", [])[:5],
        "bottom_industries": industry.get("bottom", [])[-5:],
        "dragon_tiger_total": dt.get("total_records", 0),
        "dragon_tiger_top5": [
            {"code": s["code"], "name": s["name"], "net_buy_wan": s["net_buy_wan"]}
            for s in dt.get("stocks", [])[:5]
        ],
    }
