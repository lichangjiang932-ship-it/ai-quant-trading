"""
资金面/筹码层 (Layer 4) + 基础数据层 (Layer 6) + 研报层 (Layer 2) + 估值公式

┌──────────────────┬──────┬──────────────────────────────────────────┐
│ 融资融券明细      │ HTTP │ 日级融资余额/买入/偿还 + 融券               │
│ 大宗交易          │ HTTP │ 成交价/量 + 买卖方营业部                    │
│ 股东户数变化      │ HTTP │ 季度股东户数 + 环比变化                     │
│ 分红送转          │ HTTP │ 历史每股派息/送股/转增                      │
│ 个股资金流120日   │ HTTP │ 主力/大单/中单/小单 日级净流入              │
│ 东财研报          │ HTTP │ 研报列表 + PDF下载 + 评级 + 三年EPS         │
│ 同花顺一致预期    │ HTTP │ 机构一致预期EPS                             │
│ 新浪财报三表      │ HTTP │ 资产负债表/利润表/现金流量表                 │
│ 估值公式          │      │ forward_pe / pe_digestion / PEG / full_val  │
└──────────────────┴──────┴──────────────────────────────────────────┘
"""
import json
import math
import re
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from ..em_client import em_get, eastmoney_datacenter
from .quote import UA, normalize_code


# ==================== 4.1 融资融券 ====================

def margin_trading(code: str, page_size: int = 30) -> List[Dict]:
    """融资融券明细(日级)"""
    data = eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{normalize_code(code)}")',
        page_size=page_size, sort_columns="DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": row.get("RZYE", 0),        # 融资余额(元)
            "rzmre": row.get("RZMRE", 0),       # 融资买入额
            "rzche": row.get("RZCHE", 0),       # 融资偿还额
            "rqye": row.get("RQYE", 0),         # 融券余额(元)
            "rqmcl": row.get("RQMCL", 0),       # 融券卖出量
            "rqchl": row.get("RQCHL", 0),       # 融券偿还量
            "rzrqye": row.get("RZRQYE", 0),     # 融资融券余额合计
        })
    return rows


# ==================== 4.2 大宗交易 ====================

def block_trade(code: str, page_size: int = 20) -> List[Dict]:
    """大宗交易记录"""
    data = eastmoney_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{normalize_code(code)}")',
        page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        close = row.get("CLOSE_PRICE") or 0
        deal_price = row.get("DEAL_PRICE") or 0
        premium = ((deal_price / close - 1) * 100) if close else 0
        rows.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "price": deal_price,
            "close": close,
            "premium_pct": round(premium, 2),
            "vol": row.get("DEAL_VOLUME", 0),
            "amount": row.get("DEAL_AMT", 0),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    return rows


# ==================== 4.3 股东户数变化 ====================

def holder_num_change(code: str, page_size: int = 10) -> List[Dict]:
    """股东户数变化(季度级)"""
    data = eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{normalize_code(code)}")',
        page_size=page_size, sort_columns="END_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10],
            "holder_num": row.get("HOLDER_NUM", 0),
            "change_num": row.get("HOLDER_NUM_CHANGE", 0),
            "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": row.get("AVG_FREE_SHARES", 0),
        })
    return rows


# ==================== 4.4 分红送转 ====================

def dividend_history(code: str, page_size: int = 20) -> List[Dict]:
    """分红送转历史"""
    data = eastmoney_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{normalize_code(code)}")',
        page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
            "bonus_rmb": row.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": row.get("TRANSFER_RATIO", 0),
            "bonus_ratio": row.get("BONUS_RATIO", 0),
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return rows


# ==================== 4.5 个股资金流 120日 ====================

def stock_fund_flow_120d(code: str) -> List[Dict]:
    """个股资金流(日级, 最近120个交易日)。单位=元"""
    market_code = 1 if normalize_code(code).startswith("6") else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": f"{market_code}.{normalize_code(code)}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "120",
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    }
    try:
        r = em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        klines = d.get("data", {}).get("klines", [])
        rows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 7:
                rows.append({
                    "date": parts[0],
                    "main_net": float(parts[1]) if parts[1] != "-" else 0,
                    "small_net": float(parts[2]) if parts[2] != "-" else 0,
                    "mid_net": float(parts[3]) if parts[3] != "-" else 0,
                    "large_net": float(parts[4]) if parts[4] != "-" else 0,
                    "super_net": float(parts[5]) if parts[5] != "-" else 0,
                })
        return rows
    except Exception:
        return []


def fund_flow_summary(code: str, recent_days: int = 20) -> Dict:
    """资金流摘要: 近N日主力累计 + 方向"""
    data = stock_fund_flow_120d(code)
    if not data:
        return {}
    recent = data[-recent_days:]
    total_main = sum(d["main_net"] for d in recent)
    total_super = sum(d["super_net"] for d in recent)
    return {
        "total_main_net": total_main,
        "total_super_net": total_super,
        "direction": "净流入" if total_main > 0 else "净流出",
        "days": len(recent),
        "recent_main_net": [{"date": d["date"], "main_net": d["main_net"]} for d in recent[-5:]],
    }


# ==================== Layer 2: 研报层 ====================

def eastmoney_reports(code: str, max_pages: int = 3) -> List[Dict]:
    """东财研报列表"""
    REPORT_API = "https://reportapi.eastmoney.com/report/list"
    all_records = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": normalize_code(code), "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        try:
            r = em_get(REPORT_API, params=params,
                       headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        except Exception:
            break
    return all_records


def ths_eps_forecast(code: str) -> pd.DataFrame:
    """
    同花顺机构一致预期EPS。
    直连 basic.10jqka.com.cn, 解析HTML表格。
    "均值" = 机构一致预期EPS
    """
    url = f"https://basic.10jqka.com.cn/new/{normalize_code(code)}/worth.html"
    headers = {
        "User-Agent": UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        dfs = pd.read_html(r.text)
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                return df
        return dfs[0] if dfs else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# ==================== Layer 6: 新浪财报三表 ====================

def sina_financial_report(code: str, report_type: str = "lrb", num: int = 8) -> List[Dict]:
    """
    新浪财报三表。
    report_type: "fzb"(资产负债表) / "lrb"(利润表) / "llb"(现金流量表)
    num: 取最近 N 期
    """
    prefix = "sh" if normalize_code(code).startswith("6") else "sz"
    paper_code = f"{prefix}{normalize_code(code)}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": report_type,
        "type": "0",
        "page": "1",
        "num": str(num),
    }
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        report_list = r.json().get("result", {}).get("data", {}).get("report_list", {}) or {}
        rows = []
        for period in sorted(report_list.keys(), reverse=True)[:num]:
            obj = report_list[period]
            rec = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
            for it in obj.get("data", []) or []:
                title = it.get("item_title", "")
                if not title or it.get("item_value") is None:
                    continue
                rec[title] = it.get("item_value")
                tongbi = it.get("item_tongbi")
                if tongbi not in (None, ""):
                    rec[title + "_同比"] = tongbi
            rows.append(rec)
        return rows
    except Exception:
        return []


# ==================== 巨潮公告 ====================

def cninfo_announcements(code: str, page_size: int = 30) -> List[Dict]:
    """巨潮公告全文检索"""
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    code_clean = normalize_code(code)
    if code_clean.startswith("6"):
        org_id = f"gssh0{code_clean}"
    elif code_clean.startswith("8") or code_clean.startswith("4"):
        org_id = f"gsbj0{code_clean}"
    else:
        org_id = f"gssz0{code_clean}"

    payload = {
        "stock": f"{code_clean},{org_id}",
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "", "category": "", "plate": "",
        "seDate": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.cninfo.com.cn/new/disclosure",
        "Origin": "https://www.cninfo.com.cn",
    }
    try:
        r = requests.post(url, data=payload, headers=headers, timeout=15)
        d = r.json()
        rows = []
        for item in d.get("announcements", []) or []:
            ts = item.get("announcementTime")
            if isinstance(ts, (int, float)):
                date_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            else:
                date_str = str(ts)[:10] if ts else ""
            rows.append({
                "title": item.get("announcementTitle", ""),
                "type": item.get("announcementTypeName", ""),
                "date": date_str,
                "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}",
            })
        return rows
    except Exception:
        return []


# ==================== 估值计算公式 ====================

def forward_pe(price: float, eps_forecast: float) -> float:
    """前向PE = 当前股价 / 未来年度一致预期EPS"""
    if eps_forecast <= 0:
        return float("inf")
    return price / eps_forecast


def pe_digestion(current_pe: float, cagr: float, target_pe: float = 30) -> float:
    """当前PE消化到目标PE需要多少年。target_pe 固定30x(A股成长股合理估值锚点)"""
    if current_pe <= target_pe:
        return 0.0
    if cagr <= 0:
        return float("inf")
    return math.log(current_pe / target_pe) / math.log(1 + cagr)


def calc_peg(pe: float, cagr: float) -> float:
    """
    PEG = PE / (CAGR * 100)
    PEG < 1   → 便宜
    PEG 1-1.5 → 合理
    PEG > 1.5 → 贵
    """
    if cagr <= 0:
        return float("inf")
    return pe / (cagr * 100)


# ==================== 完整估值分析 ====================

def full_valuation(code: str) -> Dict:
    """
    单票完整估值分析: 实时行情 + 机构一致预期 + PE/PEG/消化时间。
    返回:
    {name, price, mcap_yi, pe_ttm, pb, eps_cur, eps_next,
     pe_fwd, cagr_pct, peg, digest_years, analyst_count}
    """
    from .quote import tencent_quote

    # 腾讯实时行情
    quotes = tencent_quote([code])
    if not quotes:
        return {}
    c = normalize_code(code)
    q = quotes.get(c)
    if not q:
        return {}

    price = q["price"]
    mcap = q["mcap_yi"]
    pe_ttm = q["pe_ttm"]
    pb = q["pb"]

    # 机构一致预期
    df = ths_eps_forecast(code)
    eps_cur = eps_next = None
    analyst_count = 0
    if not df.empty and len(df.columns) >= 3:
        try:
            for i, row in df.iterrows():
                if i == 0:
                    eps_cur = float(row.iloc[2]) if pd.notna(row.iloc[2]) else None
                    analyst_count = int(row.iloc[1]) if pd.notna(row.iloc[1]) else 0
                elif i == 1:
                    eps_next = float(row.iloc[2]) if pd.notna(row.iloc[2]) else None
        except (ValueError, IndexError):
            pass

    # 估值指标
    pe_fwd = price / eps_cur if eps_cur else float("inf")
    cagr = (eps_next / eps_cur - 1) if (eps_cur and eps_next and eps_cur > 0) else 0
    peg = pe_fwd / (cagr * 100) if cagr > 0 else float("inf")
    digest = (
        math.log(pe_fwd / 30) / math.log(1 + cagr)
        if pe_fwd > 30 and cagr > 0 else 0
    )

    return {
        "name": q["name"],
        "price": price,
        "mcap_yi": mcap,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "eps_cur": eps_cur,
        "eps_next": eps_next,
        "pe_fwd": round(pe_fwd, 1) if eps_cur else None,
        "cagr_pct": round(cagr * 100, 0) if cagr else None,
        "peg": round(peg, 2) if peg != float("inf") else None,
        "digest_years": round(digest, 1),
        "analyst_count": analyst_count,
    }
