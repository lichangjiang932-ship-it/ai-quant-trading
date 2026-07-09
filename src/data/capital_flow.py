"""
资金层数据源(抄袭 a-stock-data 的资金面/打板层)

提供 A 股特有的「聪明钱/情绪」信号,全部走 em_get 防封:
- get_fund_flow_minute : 个股分钟级资金流(主力/超大单/大单/中单/小单净流入,单位=元)
- get_limit_up_pool    : 东财涨停池(连板梯队)
- get_broken_pool      : 东财炸板池(打板情绪:炸板率)
- get_north_flow       : 北向资金实时净流入
- summarize_for_symbol : 把该股资金面浓缩成一行中文,供分析师提示词

坑(来自参考项目实测):
- push2 资金流金额单位是【元】非万元
- push2ex 涨停池价格是 ×1000 整数,需 ÷1000;金额单位=元
- date 必须传交易日,非交易日 data 返回 null
无网络/接口异常一律返回空({}/[]),绝不抛出。
"""
import time
from typing import Dict, List, Optional

from .em_client import em_get


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _secid(code: str) -> str:
    """6位代码 -> 东财 secid。1=沪 0=深。"""
    code = code.lower().replace('sh', '').replace('sz', '')
    return f"1.{code}" if code[:1] in ('6', '5', '9') else f"0.{code}"


def _fmt_zt_time(t) -> str:
    """涨停时间整数 -> HH:MM:SS(92500 -> 09:25:00)。"""
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


class CapitalFlowFetcher:
    """资金层数据获取器(全部走 em_get 防封 + 60s 缓存 + 优雅降级)"""

    def __init__(self, cache_ttl: int = 60):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}

    def _cached(self, key: str, now_ts: Optional[float]):
        ts = now_ts if now_ts is not None else time.time()
        hit = self._cache.get(key)
        if hit and (ts - hit[1]) < self.cache_ttl:
            return hit[0]
        return None

    def _store(self, key: str, value, now_ts: Optional[float]):
        ts = now_ts if now_ts is not None else time.time()
        if value:
            self._cache[key] = (value, ts)
        return value

    # ---- 个股分钟资金流 ----
    def get_fund_flow_minute(self, symbol: str, now_ts: Optional[float] = None) -> List[Dict]:
        """个股当日分钟级资金流。返回 [{time, main_net, super_net, large_net, mid_net, small_net}]。
        金额单位=元。无数据返回 []。"""
        key = f"flow:{symbol}"
        c = self._cached(key, now_ts)
        if c is not None:
            return c

        secid = _secid(symbol)
        url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params = {"secid": secid, "klt": 1,
                  "fields1": "f1,f2,f3,f7",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57"}
        headers = {"Referer": "https://quote.eastmoney.com/",
                   "Origin": "https://quote.eastmoney.com"}
        rows: List[Dict] = []
        try:
            r = em_get(url, params=params, headers=headers, timeout=10)
            d = r.json()
            for line in d.get("data", {}).get("klines", []):
                parts = line.split(",")
                if len(parts) >= 6:
                    rows.append({
                        "time": parts[0],
                        "main_net": float(parts[1]),
                        "small_net": float(parts[2]),
                        "mid_net": float(parts[3]),
                        "large_net": float(parts[4]),
                        "super_net": float(parts[5]),
                    })
        except Exception:
            return []
        return self._store(key, rows, now_ts)

    def get_main_net_summary(self, symbol: str) -> Dict:
        """浓缩该股资金流: 全天主力累计净流入 + 最新分钟方向。无数据返回 {}。

        坑: 东财 fflow/kline 每根分钟线的 f52/f56 等字段【已是当日累计净流入】,
        不是当分钟增量。所以「全天累计」= 最后一根的值, 绝不能把所有分钟再 sum 一遍
        (那会把累计值累计 N 次, 虚高约百倍 -> 曾出现 ETF 成交2亿却"主力净流出27亿")。
        """
        rows = self.get_fund_flow_minute(symbol)
        if not rows:
            return {}
        last = rows[-1]
        total_main = last["main_net"]     # 最后一分钟 = 当日累计
        total_super = last["super_net"]
        # 最新一分钟的增量(与上一根做差), 用于展示"当前方向"
        prev = rows[-2] if len(rows) >= 2 else {"main_net": 0.0}
        last_delta = last["main_net"] - prev.get("main_net", 0.0)
        return {
            "total_main_net": total_main,     # 元(当日累计)
            "total_super_net": total_super,   # 元(当日累计)
            "last_main_net": last_delta,      # 元(最近一分钟增量)
            "direction": "inflow" if total_main > 0 else "outflow",
            "points": len(rows),
        }

    # ---- 涨停/炸板池 ----
    def _zt_api(self, endpoint: str, sort: str, date: str) -> List[Dict]:
        url = f"https://push2ex.eastmoney.com/{endpoint}"
        params = {"ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
                  "pagesize": 10000, "sort": sort, "date": date}
        headers = {"Referer": "https://quote.eastmoney.com/"}
        try:
            r = em_get(url, params=params, headers=headers, timeout=10)
            return (r.json().get("data") or {}).get("pool") or []
        except Exception:
            return []

    def get_limit_up_pool(self, date: str, now_ts: Optional[float] = None) -> List[Dict]:
        """涨停池。date=YYYYMMDD 交易日。返回 code/name/price/pct/limit_days(连板)/industry。"""
        key = f"zt:{date}"
        c = self._cached(key, now_ts)
        if c is not None:
            return c
        out = []
        for p in self._zt_api("getTopicZTPool", "fbt:asc", date):
            try:
                out.append({
                    "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "pct": round(p.get("zdp", 0), 2), "limit_days": p.get("lbc", 1),
                    "seal_fund": p.get("fund", 0), "break_times": p.get("zbc", 0),
                    "industry": p.get("hybk", ""),
                })
            except Exception:
                continue
        return self._store(key, out, now_ts)

    def get_broken_pool(self, date: str, now_ts: Optional[float] = None) -> List[Dict]:
        """炸板池(涨停后开板)。返回 code/name/price/pct/break_times/industry。"""
        key = f"zb:{date}"
        c = self._cached(key, now_ts)
        if c is not None:
            return c
        out = []
        for p in self._zt_api("getTopicZBPool", "fbt:asc", date):
            try:
                out.append({
                    "code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "pct": round(p.get("zdp", 0), 2), "break_times": p.get("zbc", 0),
                    "industry": p.get("hybk", ""),
                })
            except Exception:
                continue
        return self._store(key, out, now_ts)

    def get_sentiment_stats(self, date: str) -> Dict:
        """打板情绪速算: 涨停数/炸板数/炸板率/最高连板。无数据返回 {}。"""
        zt = self.get_limit_up_pool(date)
        zb = self.get_broken_pool(date)
        if not zt and not zb:
            return {}
        zt_n, zb_n = len(zt), len(zb)
        break_rate = zb_n / (zt_n + zb_n) if (zt_n + zb_n) else 0.0
        max_lb = max((s.get("limit_days", 1) for s in zt), default=0)
        return {
            "limit_up_count": zt_n,
            "broken_count": zb_n,
            "break_rate": round(break_rate, 3),
            "max_limit_days": max_lb,
        }

    # ---- 北向资金 ----
    def get_north_flow(self, now_ts: Optional[float] = None) -> Dict:
        """北向资金实时净流入(元)。无数据返回 {}。"""
        key = "north"
        c = self._cached(key, now_ts)
        if c is not None:
            return c
        url = "https://push2.eastmoney.com/api/qt/kamt/get"
        params = {"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55,f56"}
        headers = {"Referer": "https://quote.eastmoney.com/"}
        try:
            r = em_get(url, params=params, headers=headers, timeout=10)
            d = r.json().get("data") or {}
            # hk2sh + hk2sz 净流入(万元 -> 元)
            sh = (d.get("hk2sh") or {}).get("netBuyAmt")
            sz = (d.get("hk2sz") or {}).get("netBuyAmt")
            if sh is None and sz is None:
                return {}  # 非交易时段/接口无值,视为无数据
            total = 0.0
            for v in (sh, sz):
                if v is not None:
                    total += float(v) * 10000  # 万元 -> 元
            result = {"north_net": total, "direction": "inflow" if total > 0 else "outflow"}
        except Exception:
            return {}
        return self._store(key, result, now_ts)

    # ---- 浓缩文本(供分析师) ----
    def summarize_for_symbol(self, symbol: str, date: Optional[str] = None) -> str:
        """把该股资金面浓缩成一行中文。无任何数据返回提示串。"""
        parts = []
        flow = self.get_main_net_summary(symbol)
        if flow:
            tm = flow["total_main_net"] / 1e4  # 元 -> 万元(展示)
            parts.append(f"主力今日净{'流入' if tm >= 0 else '流出'}{abs(tm):.0f}万")
            ts = flow["total_super_net"] / 1e4
            parts.append(f"超大单净{ts:+.0f}万")

        code = symbol.lower().replace('sh', '').replace('sz', '')
        if date:
            in_zt = any(s["code"] == code for s in self.get_limit_up_pool(date))
            in_zb = any(s["code"] == code for s in self.get_broken_pool(date))
            if in_zt:
                parts.append("今日涨停")
            elif in_zb:
                parts.append("涨停后炸板")

        north = self.get_north_flow()
        if north:
            nn = north["north_net"] / 1e8  # 元 -> 亿元
            parts.append(f"北向净{nn:+.1f}亿")

        return " | ".join(parts) if parts else "(无资金流数据)"
