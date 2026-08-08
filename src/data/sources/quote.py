"""
行情层 (Layer 1) — mootdx(TCP) + 腾讯财经(HTTP) + 百度股市通(HTTP)

数据源优先级: mootdx(不封IP) > 腾讯(不封IP) > 百度 > 东财

┌──────────┬────────┬──────────────────────────────────────┐
│ mootdx   │ TCP    │ K线 + 五档盘口 + 逐笔成交(零鉴权)      │
│ 腾讯财经  │ HTTP   │ PE/PB/市值/换手率/涨跌停/指数/ETF     │
│ 百度股市通│ HTTP   │ K线带MA5/10/20 + 概念板块(零鉴权)     │
│ 东财push2 │ HTTP   │ 行情(走 em_get 限流,仅兜底)           │
└──────────┴────────┴──────────────────────────────────────┘
"""
import urllib.request
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests

from ..em_client import em_get

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ==================== 代码工具 ====================

def get_prefix(code: str) -> str:
    """6位代码 -> 市场前缀"""
    code = code.strip().lower().replace("sh", "").replace("sz", "").replace("bj", "")
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def normalize_code(raw: str) -> str:
    """统一归一化为纯6位数字代码"""
    code = raw.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[2:]
    code = code.split(".")[0]
    return code.zfill(6)


# ==================== 1.1 mootdx — K线 + 五档盘口 + 逐笔成交 ====================

class MootdxSource:
    """通达信 TCP 协议行情源(不封IP,无需注册)"""

    def __init__(self):
        self._client = None
        self._available = None  # None=未检测, True/False=已检测

    def _ensure_client(self) -> bool:
        if self._available is False:
            return False
        if self._client is not None:
            return True
        try:
            from mootdx.quotes import Quotes
            self._client = Quotes.factory(market='std')
            # 快速探测
            self._client.quotes(symbol=['000001'])
            self._available = True
        except Exception as e:
            self._available = False
            self._client = None
        return self._available

    def is_available(self) -> bool:
        if self._available is None:
            return self._ensure_client()
        return self._available

    def close(self):
        """关闭 mootdx 连接 (停止心跳线程)。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._available = None

    def kline(self, code: str, category: int = 4, offset: int = 100) -> pd.DataFrame:
        """
        K线数据。
        category: 4=日线 5=周线 6=月线 7=1分钟 8=5分钟 9=15分钟 10=30分钟 11=60分钟
        """
        if not self._ensure_client():
            return pd.DataFrame()
        try:
            data = self._client.bars(symbol=normalize_code(code), category=category, offset=offset)
            if data is None or len(data) == 0:
                return pd.DataFrame()
            df = pd.DataFrame(data)
            rename = {'open': 'open', 'close': 'close', 'high': 'high', 'low': 'low',
                      'vol': 'volume', 'amount': 'amount'}
            for old, new in rename.items():
                if old in df.columns:
                    df.rename(columns={old: new}, inplace=True)
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)
            return df
        except Exception:
            return pd.DataFrame()

    def quote(self, codes: List[str]) -> Dict[str, Dict]:
        """实时报价(含五档盘口)"""
        if not self._ensure_client():
            return {}
        try:
            raw = self._client.quotes(symbol=[normalize_code(c) for c in codes])
            result = {}
            for item in (raw or []):
                code = item.get('code', '')
                result[code] = {
                    'price': item.get('price', 0),
                    'open': item.get('open', 0),
                    'high': item.get('high', 0),
                    'low': item.get('low', 0),
                    'last_close': item.get('last_close', 0),
                    'vol': item.get('vol', 0),
                    'amount': item.get('amount', 0),
                    'bid1': item.get('bid1', 0), 'bid2': item.get('bid2', 0),
                    'bid3': item.get('bid3', 0), 'bid4': item.get('bid4', 0),
                    'bid5': item.get('bid5', 0),
                    'bid_vol1': item.get('bid_vol1', 0), 'bid_vol2': item.get('bid_vol2', 0),
                    'bid_vol3': item.get('bid_vol3', 0), 'bid_vol4': item.get('bid_vol4', 0),
                    'bid_vol5': item.get('bid_vol5', 0),
                    'ask1': item.get('ask1', 0), 'ask2': item.get('ask2', 0),
                    'ask3': item.get('ask3', 0), 'ask4': item.get('ask4', 0),
                    'ask5': item.get('ask5', 0),
                    'ask_vol1': item.get('ask_vol1', 0), 'ask_vol2': item.get('ask_vol2', 0),
                    'ask_vol3': item.get('ask_vol3', 0), 'ask_vol4': item.get('ask_vol4', 0),
                    'ask_vol5': item.get('ask_vol5', 0),
                }
            return result
        except Exception:
            return {}

    def transactions(self, code: str, date: str = "") -> List[Dict]:
        """逐笔成交(非交易时间返回空)"""
        if not self._ensure_client():
            return []
        try:
            rows = self._client.transaction(symbol=normalize_code(code), date=date) or []
            return rows
        except Exception:
            return []

    def finance(self, code: str) -> Dict:
        """季报快照(37字段: EPS/ROE/净利等)"""
        if not self._ensure_client():
            return {}
        try:
            return self._client.finance(symbol=normalize_code(code)) or {}
        except Exception:
            return {}

    def f10(self, code: str, category: str = "最新提示") -> str:
        """F10 公司资料(9大类文本)"""
        if not self._ensure_client():
            return ""
        try:
            return self._client.F10(symbol=normalize_code(code), name=category) or ""
        except Exception:
            return ""


# ==================== 1.2 腾讯财经 API ====================

def tencent_quote(codes: List[str]) -> Dict[str, Dict]:
    """
    腾讯财经实时行情 — PE/PB/市值/换手率/涨跌停/指数/ETF。
    批量拉取, GBK编码, ~分隔88字段, 不封IP。

    返回: {code: {name, price, pe_ttm, pb, mcap_yi, float_mcap_yi,
                  turnover_pct, limit_up, limit_down, vol_ratio, pe_static,
                  change_pct, change_amt, open, high, low, last_close, amount_wan}}
    也支持指数: ["000001"(上证), "000300"(沪深300), "399006"(创业板)]
    也支持ETF:   ["510050", "510300"]
    """
    prefixed = []
    for c in codes:
        c = normalize_code(c)
        if c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception:
        return {}

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]

        def _f(idx):
            try:
                return float(vals[idx]) if vals[idx] else 0.0
            except (ValueError, TypeError):
                return 0.0

        result[code] = {
            "name":         vals[1],
            "price":        _f(3),
            "last_close":   _f(4),
            "open":         _f(5),
            "change_amt":   _f(31),
            "change_pct":   _f(32),
            "high":         _f(33),
            "low":          _f(34),
            "amount_wan":   _f(37),       # 成交额(万元)
            "turnover_pct": _f(38),       # 换手率%
            "pe_ttm":       _f(39),       # PE(TTM)
            "amplitude_pct":_f(43),       # 振幅%(非PB!)
            "mcap_yi":      _f(44),       # 总市值(亿)
            "float_mcap_yi":_f(45),       # 流通市值(亿)
            "pb":           _f(46),       # PB(市净率)
            "limit_up":     _f(47),       # 涨停价
            "limit_down":   _f(48),       # 跌停价
            "vol_ratio":    _f(49),       # 量比
            "pe_static":    _f(52),       # PE(静)
        }
    return result


# ==================== 1.3 百度股市通 K线(带MA) ====================

def baidu_kline_with_ma(code: str, start_time: str = "") -> Dict:
    """百度股市通K线 — 独有能力: 返回时自带 ma5/ma10/ma20 均价, 零鉴权"""
    url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
    params = {
        "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
        "isFutures": "false", "isStock": "true", "newFormat": "1",
        "group": "quotation_kline_ab", "finClientType": "pc",
        "code": normalize_code(code), "start_time": start_time, "ktype": "1",
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        result = d.get("Result", {})
        md = result.get("newMarketData", {})
        return {"keys": md.get("keys", []), "rows": md.get("marketData", "").split(";")}
    except Exception:
        return {"keys": [], "rows": []}


def baidu_kline_df(code: str) -> pd.DataFrame:
    """将百度K线转为DataFrame, 含ma5/ma10/ma20列"""
    data = baidu_kline_with_ma(code)
    keys = data["keys"]
    rows = data["rows"]
    if not keys or not rows:
        return pd.DataFrame()

    records = []
    for row in rows:
        parts = row.split(",")
        if len(parts) >= len(keys):
            rec = {}
            for i, k in enumerate(keys):
                try:
                    rec[k] = float(parts[i])
                except (ValueError, TypeError):
                    rec[k] = parts[i]
            records.append(rec)

    df = pd.DataFrame(records)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
    return df


def baidu_concept_blocks(code: str) -> Dict:
    """
    百度股市通概念板块归属。
    返回: {industry: [...], concept: [...], region: [...], concept_tags: [...]}
    """
    url = (
        f"https://finance.pae.baidu.com/api/getrelatedblock"
        f"?code={normalize_code(code)}&market=ab"
        f"&typeCode=all&finClientType=pc"
    )
    headers = {
        "Host": "finance.pae.baidu.com",
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        d = r.json()
        if str(d.get("ResultCode", -1)) != "0":
            return {"industry": [], "concept": [], "region": [], "concept_tags": []}

        result = {"industry": [], "concept": [], "region": [], "concept_tags": []}
        for block in d.get("Result", []):
            block_type = block.get("type", "")
            for item in block.get("list", []):
                entry = {
                    "name": item.get("name", ""),
                    "change_pct": item.get("increase", ""),
                    "desc": item.get("desc", ""),
                }
                if "行业" in block_type:
                    result["industry"].append(entry)
                elif "概念" in block_type:
                    result["concept"].append(entry)
                    result["concept_tags"].append(entry["name"])
                elif "地域" in block_type:
                    result["region"].append(entry)
        return result
    except Exception:
        return {"industry": [], "concept": [], "region": [], "concept_tags": []}


# ==================== 东财个股信息 ====================

def eastmoney_stock_info(code: str) -> Dict:
    """东财个股基本面(行业/总股本/流通股/市值/上市日期)"""
    market_code = 1 if normalize_code(code).startswith("6") else 0
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2", "invt": "2",
        "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
        "secid": f"{market_code}.{normalize_code(code)}",
    }
    headers = {"User-Agent": UA}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        d = r.json().get("data", {})
        return {
            "code": d.get("f57", ""),
            "name": d.get("f58", ""),
            "industry": d.get("f127", ""),
            "total_shares": d.get("f84", 0),
            "float_shares": d.get("f85", 0),
            "mcap": d.get("f116", 0),
            "float_mcap": d.get("f117", 0),
            "list_date": str(d.get("f189", "")),
            "price": d.get("f43", 0),
        }
    except Exception:
        return {}
