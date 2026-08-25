"""可比公司估值 (Comps) — 同业 PE/PB 横向对比。

内置常用行业成分股映射 (可扩展)。数据由调用方注入 quotes/财务数据,
模块只做对比计算, 保持可单测。

行业分类: 银行 / 白酒 / 新能源车 / 半导体 / 医药 / 券商 / 保险 / 消费电子 / 地产 / 光伏
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 行业 → 成分股 (symbol, name) 常用池, 可被外部覆盖
INDUSTRY_PEERS: Dict[str, List[str]] = {
    "银行": ["sh600000", "sz000001", "sh601398", "sh601288", "sh600036", "sh601166"],
    "白酒": ["sh600519", "sz000858", "sh600809", "sz000568", "sh603369"],
    "新能源车": ["sz300750", "sz002594", "sh601633", "sz002460", "sh603799"],
    "半导体": ["sz002371", "sh688981", "sz300661", "sh688012", "sz002049"],
    "医药": ["sh600276", "sz300760", "sh603259", "sz300347", "sh600196"],
    "券商": ["sh600030", "sz300059", "sh601688", "sh600837", "sh601211"],
    "保险": ["sh601318", "sh601628", "sh601601", "sh601336"],
    "消费电子": ["sz002475", "sz002241", "sz300433", "sh603501", "sz002600"],
    "地产": ["sz000002", "sh600048", "sh600606", "sz001979"],
    "光伏": ["sz300274", "sh601012", "sz002459", "sh688223", "sz300316"],
    "AI算力": ["sz000977", "sh688041", "sz002230", "sh600845", "sz300308"],
}

# 代码 → 中文名 (缺失时前端可用行情 name 兜底)
SYMBOL_CN: Dict[str, str] = {
    "sh600000": "浦发银行", "sz000001": "平安银行", "sh601398": "工商银行",
    "sh601288": "农业银行", "sh600036": "招商银行", "sh601166": "兴业银行",
    "sh600519": "贵州茅台", "sz000858": "五粮液", "sh600809": "山西汾酒",
    "sz000568": "泸州老窖", "sh603369": "今世缘", "sz300750": "宁德时代",
    "sz002594": "比亚迪", "sh601633": "长城汽车", "sz002460": "赣锋锂业",
    "sh603799": "华友钴业", "sz002371": "北方华创", "sh688981": "中芯国际",
    "sz300661": "圣邦股份", "sh688012": "中微公司", "sz002049": "紫光国微",
    "sh600276": "恒瑞医药", "sz300760": "迈瑞医疗", "sh603259": "药明康德",
    "sz300347": "泰格医药", "sh600196": "复星医药", "sh600030": "中信证券",
    "sz300059": "东方财富", "sh601688": "华泰证券", "sh600837": "海通证券",
    "sh601211": "国泰君安", "sh601318": "中国平安", "sh601628": "中国人寿",
    "sh601601": "中国太保", "sh601336": "新华保险", "sz002475": "立讯精密",
    "sz002241": "歌尔股份", "sz300433": "蓝思科技", "sh603501": "韦尔股份",
    "sz002600": "领益智造", "sz000002": "万科A", "sh600048": "保利发展",
    "sh600606": "绿地控股", "sz001979": "招商蛇口", "sz300274": "阳光电源",
    "sh601012": "隆基绿能", "sz002459": "晶澳科技", "sh688223": "晶科能源",
    "sz300316": "晶盛机电", "sz000977": "浪潮信息", "sh688041": "海光信息",
    "sz002230": "科大讯飞", "sh600845": "宝信软件", "sz300308": "中际旭创",
}


def classify_industry(symbol: str) -> Optional[str]:
    """根据成分表反查行业 (找不到返回 None)。"""
    for industry, peers in INDUSTRY_PEERS.items():
        if symbol in peers:
            return industry
    return None


def _median(values: List[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 2)


def _pct_rank(value: Optional[float], peers: List[float]) -> Optional[float]:
    """value 在 peers 中的百分位 (越小越低估)。"""
    if value is None:
        return None
    vals = sorted(v for v in peers if v is not None and v > 0)
    if not vals:
        return None
    below = sum(1 for v in vals if v <= value)
    return round(below / len(vals), 2)


def compute_comps(symbol: str, industry: Optional[str], quotes: Dict,
                  financials: Optional[Dict] = None) -> Dict:
    """可比估值对比。

    Args:
        symbol: 目标股票代码
        industry: 行业名 (classify_industry 输出)
        quotes: {code: {name, price, pe_ttm, pb, mcap_yi, change_pct}}
        financials: {code: {pe_ttm, pb, roe, revenue_yoy}} (可选, 覆盖 quote 估值)
    """
    financials = financials or {}
    target_quote = quotes.get(symbol) or {}
    target_fin = financials.get(symbol) or {}

    if not industry or industry not in INDUSTRY_PEERS:
        peers = []
    else:
        peers = [s for s in INDUSTRY_PEERS[industry] if s != symbol]

    rows = []
    for s in [symbol] + peers:
        q = quotes.get(s) or {}
        fin = financials.get(s) or {}
        pe = fin.get("pe_ttm") or q.get("pe_ttm")
        pb = fin.get("pb") or q.get("pb")
        rows.append({
            "symbol": s,
            "name": fin.get("name") or q.get("name") or SYMBOL_CN.get(s, s),
            "price": round(float(q.get("price") or 0), 2),
            "change_pct": round(float(q.get("change_pct") or 0), 2),
            "pe_ttm": round(float(pe), 1) if pe and float(pe) > 0 else None,
            "pb": round(float(pb), 2) if pb and float(pb) > 0 else None,
            "mcap_yi": round(float(q.get("mcap_yi") or 0), 1),
            "roe": round(float(fin.get("roe") or 0), 1) if fin.get("roe") else None,
            "revenue_yoy": round(float(fin.get("revenue_yoy") or 0), 1) if fin.get("revenue_yoy") is not None else None,
            "is_target": s == symbol,
        })

    # 同业中位数 (不含目标)
    peer_rows = [r for r in rows if not r["is_target"]]
    valued_peers = [r for r in peer_rows if r["pe_ttm"] is not None or r["pb"] is not None]
    med_pe = _median([r["pe_ttm"] for r in peer_rows])
    med_pb = _median([r["pb"] for r in peer_rows])
    target = next((r for r in rows if r["is_target"]), None) or {}
    target_pe = target.get("pe_ttm")
    target_pb = target.get("pb")

    return {
        "symbol": symbol,
        "industry": industry or "未知",
        "peer_count": len(valued_peers),
        "peer_total": len(peer_rows),
        "median_pe": med_pe,
        "median_pb": med_pb,
        "target_pe": target_pe,
        "target_pb": target_pb,
        "pe_percentile": _pct_rank(target_pe, [r["pe_ttm"] for r in peer_rows]),
        "pb_percentile": _pct_rank(target_pb, [r["pb"] for r in peer_rows]),
        "conclusion": _comps_conclusion(target_pe, target_pb, med_pe, med_pb),
        "rows": rows,
    }


def _comps_conclusion(target_pe, target_pb, med_pe, med_pb) -> Dict:
    """估值结论: 相对同业是贵还是便宜。"""
    notes = []
    verdict = "中性"
    cheap, expensive = 0, 0
    if target_pe and med_pe:
        ratio = target_pe / med_pe
        if ratio <= 0.8:
            cheap += 1
            notes.append(f"PE 为同业中位数 {med_pe:.0f} 的 {ratio:.2f}x, 相对便宜")
        elif ratio >= 1.3:
            expensive += 1
            notes.append(f"PE 为同业中位数 {med_pe:.0f} 的 {ratio:.2f}x, 相对偏贵")
        else:
            notes.append(f"PE 与同业中位数 {med_pe:.0f} 相当")
    if target_pb and med_pb:
        ratio = target_pb / med_pb
        if ratio <= 0.8:
            cheap += 1
            notes.append(f"PB 为同业中位数 {med_pb:.1f} 的 {ratio:.2f}x")
        elif ratio >= 1.3:
            expensive += 1
            notes.append(f"PB 为同业中位数 {med_pb:.1f} 的 {ratio:.2f}x")
    if expensive > cheap:
        verdict = "偏贵"
    elif cheap > expensive:
        verdict = "偏便宜"
    if not notes:
        notes.append("同业估值数据不足, 无法对比")
    return {"verdict": verdict, "notes": notes}
