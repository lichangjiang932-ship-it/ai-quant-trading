# -*- coding: utf-8 -*-
"""
新闻涨幅因子引擎 (News Momentum Factor Engine)
================================================
每日抓取热点新闻 → 提取涉及个股 → 事件词典情绪打分 + 热度计算
→ 输出 0-100 因子分 (direction: bull/bear/neutral) → 供自托管策略与前端使用

设计要点:
  - 不依赖 SnowNLP 微博情感 (对财经文本会拉低正面分), 改用 A 股事件词典
  - 名称→代码用东财全市场列表 (每日缓存刷新)
  - 因子分 = 50 + 情绪*45 + 热度修正, 越靠近 100 越强看多
"""
import json
import os
import re
import time
import threading
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

from ..data.em_client import em_get

# ── A 股事件词典 (涨幅驱动因子) ──
EVENT_BULLISH: Dict[str, int] = {
    "业绩预增": 3, "超预期": 3, "扭亏为盈": 3, "预盈": 2, "大幅增长": 2, "翻倍": 2,
    "中标": 3, "签订合同": 3, "重大合同": 3, "大单": 3, "订单": 2, "新签订单": 2,
    "回购": 2, "增持": 2, "举牌": 2, "并购重组": 3, "重组": 2, "收购": 2,
    "涨价": 2, "提价": 2, "政策利好": 3, "政策支持": 2, "扶持": 2,
    "突破": 2, "创新高": 2, "涨停": 2,
    "买入评级": 2, "增持评级": 2, "机构买入": 2, "外资看好": 2, "强烈推荐": 2,
    "战略合作": 2, "合作": 1, "扩产": 1, "分红": 1, "送转": 1,
    "业绩快报": 1, "行业景气": 2, "供不应求": 2, "产能利用率": 1,
    "低估": 2, "价值重估": 2, "被低估": 2, "回购注销": 2, "要约收购": 3,
}
EVENT_BEARISH: Dict[str, int] = {
    "业绩预亏": -3, "预减": -2, "亏损": -2, "暴雷": -3, "大幅下降": -2, "下滑": -2,
    "减持": -2, "违规": -3, "立案": -3, "调查": -2, "处罚": -3, "罚款": -2,
    "诉讼": -2, "被告": -2, "ST": -3, "退市": -3, "风险警示": -3,
    "商誉减值": -2, "爆仓": -3, "质押": -2, "平仓": -2, "跌停": -3, "闪崩": -3,
    "解禁": -1, "下调": -2, "终止": -2, "取消": -2, "风险提示": -2,
    "问询函": -2, "监管": -2, "财务造假": -3, "操纵市场": -3, "业绩地雷": -3,
    "流拍": -2, "停产": -2, "事故": -2, "召回": -2,
}
# 中性/弱信号 (仅影响热度, 不算情绪)
EVENT_NEUTRAL: List[str] = ["公告", "调研", "股东大会", "董事会", "路演", "互动易"]

# 数据路径
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
_SYMBOL_MAP_PATH = os.path.join(_DATA_DIR, "news_symbol_map.json")
_FACTOR_DIR = os.path.join(os.path.dirname(_DATA_DIR), "output", "news_factors")
_VALIDATION_PATH = os.path.join(_DATA_DIR, "news_factor_validation.csv")

_lock = threading.RLock()
_cache: Dict = {"date": "", "factors": [], "news": [], "map_time": ""}


# ═══════════════════════════════════════════════════════════════
# 1. 全市场名称→代码映射
# ═══════════════════════════════════════════════════════════════
def _load_symbol_map(force: bool = False) -> Dict[str, Dict]:
    """拉取全市场 A 股列表构建 name->symbol 映射 (每日缓存一次)。"""
    today = date.today().isoformat()
    if not force and _cache["map_time"] == today:
        return _cache.get("name_map", {})
    # 尝试读磁盘缓存
    if not force and os.path.exists(_SYMBOL_MAP_PATH):
        try:
            with open(_SYMBOL_MAP_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today and data.get("name_map"):
                _cache["map_time"] = today
                _cache["name_map"] = data["name_map"]
                return data["name_map"]
        except Exception:
            pass
    name_map: Dict[str, Dict] = {}
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        for page in range(1, 60):  # 每页100条, 最多60页(6000只)
            params = {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f13,f14",
            }
            data = em_get(url, params=params, headers={
                "Referer": "https://quote.eastmoney.com/",
            })
            payload = data.json() if hasattr(data, "json") else data
            diff = (payload.get("data") or {}).get("diff") or []
            rows = diff.values() if isinstance(diff, dict) else diff
            rows = list(rows)
            if not rows:
                break
            for item in rows:
                code = str(item.get("f12", "")).strip()
                name = str(item.get("f14", "")).strip()
                if not (code.isdigit() and len(code) == 6) or not name:
                    continue
                market = "sh" if item.get("f13") == 1 else "sz"
                symbol = market + code
                # 短名 + 全名都映射 (去空格/去尾缀)
                for key in {name, name.replace(" ", "")}:
                    if key:
                        name_map[key] = {"symbol": symbol, "code": code}
            if len(rows) < 100:
                break
    except Exception:
        pass
    if name_map:
        _cache["name_map"] = name_map
        _cache["map_time"] = today
        try:
            os.makedirs(os.path.dirname(_SYMBOL_MAP_PATH), exist_ok=True)
            with open(_SYMBOL_MAP_PATH, "w", encoding="utf-8") as f:
                json.dump({"date": today, "name_map": name_map},
                          f, ensure_ascii=False)
        except Exception:
            pass
    return name_map


def _normalize_symbol(code: str) -> str:
    """6位代码 → 带市场前缀。"""
    code = str(code or "").strip().lower()
    code = re.sub(r"^(sh|sz|bj)", "", code)
    code = re.sub(r"\.(sh|sz|bj)$", "", code)
    m = re.search(r"(\d{6})", code)
    code = m.group(1) if m else ""
    if not (code.isdigit() and len(code) == 6):
        return ""
    # 北交所须先判: 43/83/87/88 开头 + 920xxx 新代码段 (否则 920 → 误判沪市)
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


# ═══════════════════════════════════════════════════════════════
# 2. 新闻抓取 (多源, 带重试与降级)
# ═══════════════════════════════════════════════════════════════
def _get(module, func_name, *args, **kwargs):
    """延迟导入 + 重试抓取。"""
    for attempt in range(2):
        try:
            fetcher = module.NewsFetcher()
            fn = getattr(fetcher, func_name)
            return fn(*args, **kwargs)
        except Exception:
            if attempt == 0:
                time.sleep(1.5)
    return []


def fetch_hot_news(max_per_source: int = 12) -> List[Dict]:
    """抓取多源热点新闻, 去重合并。"""
    from . import news_fetcher as nf
    results: List[Dict] = []
    seen_titles = set()

    def push(item):
        title = getattr(item, "title", "") or ""
        if not title or len(title) < 6:
            return
        key = title[:30]
        if key in seen_titles:
            return
        seen_titles.add(key)
        results.append({
            "title": title,
            "content": getattr(item, "content", "") or "",
            "source": getattr(item, "source", "") or "未知",
            "time": str(getattr(item, "publish_time", datetime.now()))[:16],
            "symbols": getattr(item, "symbols", []) or [],
        })

    # 多源并行抓取: 5 个常规快讯源 + 同花顺/一财 + 热议股/研报 信号源
    for fn_name in ("fetch_sina_live_news", "fetch_wallstreetcn_news",
                    "fetch_eastmoney_news", "fetch_sina_finance_news",
                    "fetch_yicai_news", "fetch_ths_news",
                    "fetch_hot_stocks_by_media", "fetch_research_reports"):
        try:
            items = _get(nf, fn_name, max_per_source)
            for it in items:
                push(it)
        except Exception:
            continue
    return results


# ═══════════════════════════════════════════════════════════════
# 3. 个股提取 + 事件情绪分析
# ═══════════════════════════════════════════════════════════════
def extract_symbols_from_text(text: str) -> List[str]:
    """从文本提取股票代码 (6位数字, 带可选前缀)。"""
    syms = []
    for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text or ""):
        sym = _normalize_symbol(m.group(1))
        if sym and sym not in syms:
            syms.append(sym)
    return syms


def analyze_news_item(item: Dict) -> Dict:
    """新闻条目 → {sentiment, events, score}。"""
    text = f"{item.get('title', '')} {item.get('content', '')}"
    sentiment = 0.0
    events = []
    weight_sum = 0
    for word, w in EVENT_BULLISH.items():
        if word in text:
            sentiment += w
            weight_sum += abs(w)
            events.append(word)
    for word, w in EVENT_BEARISH.items():
        if word in text:
            sentiment += w
            weight_sum += abs(w)
            events.append(word)
    # 归一化到 -1..1
    if weight_sum > 0:
        sentiment = max(-1.0, min(1.0, sentiment / (weight_sum + 1)))
    score = 50 + sentiment * 45
    return {
        "sentiment": round(sentiment, 3),
        "events": events[:5],
        "score": round(max(0, min(100, score)), 1),
    }


# ═══════════════════════════════════════════════════════════════
# 4. 因子汇总
# ═══════════════════════════════════════════════════════════════
def build_factors(news_items: List[Dict]) -> List[Dict]:
    """新闻列表 → 个股级新闻涨幅因子。"""
    name_map = _load_symbol_map()
    # symbol -> 聚合
    agg: Dict[str, Dict] = {}

    def get_agg(sym: str) -> Dict:
        if sym not in agg:
            agg[sym] = {
                "symbol": sym, "name": "", "news_count": 0,
                "sources": set(), "sentiment_sum": 0.0, "event_weights": 0,
                "events": [], "titles": [], "max_score": 50.0,
            }
        return agg[sym]

    for item in news_items:
        title = item.get("title", "")
        content = item.get("content", "")
        source = item.get("source", "")
        # 提取代码
        symbols = extract_symbols_from_text(title + " " + content)
        # 名称匹配 (最长优先, 从标题中找)
        if not symbols:
            for name in sorted(name_map.keys(), key=len, reverse=True):
                if name in title:
                    symbols.append(name_map[name]["symbol"])
                    break
        if not symbols:
            continue
        a = analyze_news_item(item)
        for sym in symbols:
            g = get_agg(sym)
            g["news_count"] += 1
            if source:
                g["sources"].add(source)
            g["sentiment_sum"] += a["sentiment"]
            g["event_weights"] += 1
            for ev in a["events"]:
                if ev not in g["events"]:
                    g["events"].append(ev)
            if len(g["titles"]) < 3:
                g["titles"].append(title[:40])
            g["max_score"] = max(g["max_score"], a["score"])

    factors = []
    for sym, g in agg.items():
        if g["news_count"] <= 0:
            continue
        # 平均情绪
        avg_sentiment = g["sentiment_sum"] / g["news_count"]
        source_count = len(g["sources"])
        # 热度分
        hot = min(100, g["news_count"] * 22 + source_count * 12)
        # 因子分 = 50 + 情绪*45 + 热度修正
        factor = 50 + avg_sentiment * 45 + (hot - 50) * 0.15
        factor = round(max(0, min(100, factor)), 1)
        # 方向判定: 必须有事件词才算 bull/bear, 纯提及无事件归 neutral
        has_events = bool(g["events"])
        if has_events and factor >= 60:
            direction = "bull"
        elif has_events and factor <= 40:
            direction = "bear"
        else:
            direction = "neutral"
            # 无事件词的新闻不参与情绪打分, 因子拉回中性
            if not has_events:
                factor = round(min(factor, 55.0), 1)
        factors.append({
            "symbol": sym,
            "name": g["name"] or "",
            "news_count": g["news_count"],
            "source_count": source_count,
            "sentiment": round(avg_sentiment, 3),
            "hot_score": round(hot, 1),
            "factor_score": factor,
            "direction": direction,
            "events": g["events"][:4],
            "titles": g["titles"],
        })
    # 按因子分降序
    factors.sort(key=lambda x: x["factor_score"], reverse=True)
    return factors


# ═══════════════════════════════════════════════════════════════
# 5. 对外入口: 当日新闻因子 (带缓存 + 持久化)
# ═══════════════════════════════════════════════════════════════
def get_daily_factors(force: bool = False) -> Dict:
    """当日新闻涨幅因子。返回 {date, news_count, factors, generated_at}。"""
    today = date.today().isoformat()
    with _lock:
        if not force and _cache["date"] == today and _cache["factors"]:
            return {"date": today, "news_count": len(_cache["news"]),
                    "factors": _cache["factors"],
                    "generated_at": _cache.get("generated_at", "")}
    try:
        news_items = fetch_hot_news()
        factors = build_factors(news_items)
        with _lock:
            _cache["date"] = today
            _cache["news"] = news_items
            _cache["factors"] = factors
            _cache["generated_at"] = datetime.now().strftime("%H:%M:%S")
        # 持久化
        try:
            os.makedirs(_FACTOR_DIR, exist_ok=True)
            path = os.path.join(_FACTOR_DIR, f"{today}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": today, "news_count": len(news_items),
                           "factors": factors,
                           "generated_at": _cache["generated_at"]},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return {"date": today, "news_count": len(news_items),
                "factors": factors, "generated_at": _cache["generated_at"]}
    except Exception as e:
        return {"date": today, "news_count": 0, "factors": [],
                "generated_at": "", "error": str(e)}


def factor_for_symbol(symbol: str) -> Optional[Dict]:
    """查询单只股票的新闻因子 (供自托管策略调用)。"""
    data = get_daily_factors()
    for f in data.get("factors", []):
        if f["symbol"] == symbol:
            return f
    return None


# ═══════════════════════════════════════════════════════════════
# 6. 前瞻验证记录 (新闻因子 → 未来收益, 用于回测验证因子有效性)
# ═══════════════════════════════════════════════════════════════
def record_validation(symbol: str, factor_score: float, direction: str):
    """记录当日因子快照, 供后续收益跟踪。"""
    try:
        os.makedirs(os.path.dirname(_VALIDATION_PATH), exist_ok=True)
        header = not os.path.exists(_VALIDATION_PATH)
        with open(_VALIDATION_PATH, "a", encoding="utf-8") as f:
            if header:
                f.write("date,symbol,factor_score,direction,ret_3d,ret_5d,ret_10d\n")
            f.write(f"{date.today().isoformat()},{symbol},{factor_score},{direction},,,\n")
    except Exception:
        pass


def update_validation_returns():
    """回填前瞻验证收益: 对 validation.csv 中无收益的记录, 用 K 线算 3/5/10 交易日收益。

    每日收盘后调用。累积样本后可统计: 新闻因子分 vs 未来收益的相关性,
    验证"新闻驱动涨幅"因子是否有效 (回测专家视角的因子有效性验证)。
    """
    if not os.path.exists(_VALIDATION_PATH):
        return
    try:
        import pandas as _pd
        df = _pd.read_csv(_VALIDATION_PATH, dtype={"symbol": str})
        today = date.today().isoformat()
        changed = False
        for idx, row in df.iterrows():
            if _pd.notna(row.get("ret_3d")):
                continue
            rec_date = str(row["date"])
            sym = str(row["symbol"]).strip()
            if not sym:
                continue
            try:
                kline = _load_kline(sym, 60)
                if kline is None or len(kline) < 12:
                    continue
                # 找记录日之后的首个交易日作为买入基准 (next-day open 防前视)
                dates = [str(d)[:10] for d in kline["date"]]
                if rec_date not in dates:
                    continue
                i0 = dates.index(rec_date)
                if i0 + 1 >= len(dates):
                    continue
                base = float(kline["open"].iloc[i0 + 1])
                if base <= 0:
                    continue
                rets = {}
                for n, col in ((3, "ret_3d"), (5, "ret_5d"), (10, "ret_10d")):
                    j = i0 + 1 + (n - 1)  # 持有 n 个交易日, 用第 n 日收盘
                    if j < len(dates):
                        px = float(kline["close"].iloc[j])
                        if px > 0:
                            rets[col] = round((px / base - 1) * 100, 2)
                if rets:
                    for col, v in rets.items():
                        df.at[idx, col] = v
                    changed = True
            except Exception:
                continue
        if changed:
            df.to_csv(_VALIDATION_PATH, index=False, encoding="utf-8")
    except Exception:
        pass


def _load_kline(symbol: str, days: int = 60):
    """延迟加载 K 线 (日线前复权)。"""
    try:
        import pandas as _pd
        from ..data.sources.quote import get_kline
        frame = get_kline(symbol, days=days, fq="qfq")
        if frame is not None and not frame.empty and "close" in frame.columns:
            return frame
        return None
    except Exception:
        return None


def factor_validation_stats() -> Dict:
    """新闻因子有效性统计: 按方向分组统计未来收益 (样本累积后调用)。"""
    if not os.path.exists(_VALIDATION_PATH):
        return {"samples": 0}
    try:
        import pandas as _pd
        df = _pd.read_csv(_VALIDATION_PATH, dtype={"symbol": str})
        df = df[_pd.to_numeric(df["ret_3d"], errors="coerce").notna()]
        if df.empty:
            return {"samples": 0}
        df["ret_3d"] = _pd.to_numeric(df["ret_3d"], errors="coerce")
        df["ret_5d"] = _pd.to_numeric(df["ret_5d"], errors="coerce")
        df["ret_10d"] = _pd.to_numeric(df["ret_10d"], errors="coerce")
        groups = {}
        for direction in ("bull", "bear", "neutral"):
            g = df[df["direction"] == direction]
            if g.empty:
                continue
            groups[direction] = {
                "samples": int(len(g)),
                "ret_3d_avg": round(float(g["ret_3d"].mean()), 2),
                "ret_5d_avg": round(float(g["ret_5d"].mean()), 2),
                "ret_10d_avg": round(float(g["ret_10d"].mean()), 2),
                "win_rate_3d": round(float((g["ret_3d"] > 0).mean() * 100), 1),
            }
        return {"samples": int(len(df)), "groups": groups}
    except Exception as e:
        return {"samples": 0, "error": str(e)}
