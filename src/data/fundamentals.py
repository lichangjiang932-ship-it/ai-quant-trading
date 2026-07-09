"""
A 股基本面数据获取(直连HTTP API, 零 akshare 依赖)

V4.0: 基于 a-stock-data V3.2.1 skill, 全部直连 HTTP API:
- 腾讯财经: PE/PB/总市值(不封IP)
- 东财push2: 个股信息(行业/股本)
- 新浪: 财报三表(ROE/营收/净利同比)
- mootdx: 财务快照(37字段, EPS/ROE/净利)

供多智能体的「基本面分析师」读取真实财务指标。
"""
import time
from typing import Dict, Optional


def _to_code(symbol: str) -> str:
    """sh600000 / sz000001 / 600000 -> 600000(6位纯代码)"""
    s = symbol.lower().replace('sh', '').replace('sz', '').strip()
    return s


def _safe_float(val) -> Optional[float]:
    try:
        if val is None or val == '' or val == '-':
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


class FundamentalsFetcher:
    """A 股基本面指标获取器(零 akshare 依赖)"""

    def __init__(self, cache_ttl: int = 3600):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # code -> (data, ts)

    def is_available(self) -> bool:
        return True  # HTTP直连,始终可用

    def get(self, symbol: str, now_ts: Optional[float] = None) -> Dict:
        """返回基本面字典。无数据字段用 None 占位。

        字段: name, pe, pe_ttm, pb, total_mv(亿), roe(%),
              revenue_yoy(%营收同比), profit_yoy(%净利同比), industry
        """
        code = _to_code(symbol)
        ts = now_ts if now_ts is not None else time.time()
        cached = self._cache.get(code)
        if cached and (ts - cached[1]) < self.cache_ttl:
            return cached[0]

        data: Dict = {}
        self._merge_tencent(code, data)
        self._merge_em_info(code, data)
        self._merge_mootdx_finance(code, data)

        if data:
            self._cache[code] = (data, ts)
        return data

    def _merge_tencent(self, code: str, data: Dict):
        """腾讯财经: PE/PB/总市值/名称(不封IP)"""
        try:
            from .sources.quote import tencent_quote
            quotes = tencent_quote([code])
            q = quotes.get(code)
            if q:
                data.setdefault('name', q.get('name'))
                data.setdefault('pe_ttm', q.get('pe_ttm') or None)
                data.setdefault('pe', q.get('pe_static') or None)
                data.setdefault('pb', q.get('pb') or None)
                data.setdefault('total_mv', q.get('mcap_yi') or None)
        except Exception:
            pass

    def _merge_em_info(self, code: str, data: Dict):
        """东财个股信息: 行业/名称/市值"""
        try:
            from .sources.quote import eastmoney_stock_info
            info = eastmoney_stock_info(code)
            if info:
                data.setdefault('name', info.get('name'))
                data.setdefault('industry', info.get('industry'))
                if 'total_mv' not in data and info.get('mcap'):
                    data['total_mv'] = round(info['mcap'] / 1e8, 2)
        except Exception:
            pass

    def _merge_mootdx_finance(self, code: str, data: Dict):
        """mootdx 财务快照: ROE/净利/营收等37字段"""
        try:
            from .sources.quote import MootdxSource
            m = MootdxSource()
            fin = m.finance(code)
            if fin:
                roe = _safe_float(fin.get('roe'))
                if roe is not None:
                    data.setdefault('roe', roe)
                profit = _safe_float(fin.get('profit'))
                if profit is not None:
                    data.setdefault('profit', profit)
                income = _safe_float(fin.get('income'))
                if income is not None:
                    data.setdefault('income', income)
        except Exception:
            pass

    def summarize(self, symbol: str) -> str:
        """把基本面浓缩成一行中文文本,供分析师提示词使用。"""
        d = self.get(symbol)
        if not d:
            return "(无基本面数据)"
        parts = []
        if d.get('name'):
            parts.append(str(d['name']))
        if d.get('industry'):
            parts.append(f"行业:{d['industry']}")
        if d.get('pe') is not None:
            parts.append(f"PE:{d['pe']:.1f}")
        if d.get('pb') is not None:
            parts.append(f"PB:{d['pb']:.2f}")
        if d.get('total_mv') is not None:
            parts.append(f"总市值:{d['total_mv']:.0f}亿")
        if d.get('roe') is not None:
            parts.append(f"ROE:{d['roe']:.1f}%")
        if d.get('revenue_yoy') is not None:
            parts.append(f"营收同比:{d['revenue_yoy']:.1f}%")
        if d.get('profit_yoy') is not None:
            parts.append(f"净利同比:{d['profit_yoy']:.1f}%")
        return " | ".join(parts) if parts else "(无基本面数据)"
