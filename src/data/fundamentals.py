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
    s = str(symbol or '').lower().replace('sh', '').replace('sz', '').replace('bj', '').strip()
    return s


def _safe_float(val) -> Optional[float]:
    try:
        if val is None or val == '' or val == '-':
            return None
        if isinstance(val, str):
            val = val.strip().replace(',', '').replace('%', '')
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
        self._merge_em_finance(code, data)
        if data.get('revenue_yoy') is None or data.get('profit_yoy') is None:
            self._merge_sina_finance(code, data)
        self._merge_mootdx_finance(code, data)

        observed = sum(
            data.get(key) is not None
            for key in ('pe_ttm', 'pe', 'pb', 'roe', 'revenue_yoy', 'profit_yoy')
        )
        data['_field_count'] = observed
        data['_available'] = observed > 0

        if data:
            self._cache[code] = (data, ts)
        return data

    @staticmethod
    def _mark_source(data: Dict, source: str):
        sources = data.setdefault('_sources', [])
        if source not in sources:
            sources.append(source)

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
                self._mark_source(data, '腾讯行情')
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
                self._mark_source(data, '东方财富公司资料')
        except Exception:
            pass

    def _merge_em_finance(self, code: str, data: Dict):
        """东方财富主要财务指标：ROE、营收同比、归母净利同比。"""
        try:
            from .em_client import eastmoney_datacenter

            exchange = 'BJ' if code.startswith(('4', '8')) else 'SH' if code.startswith(('5', '6', '9')) else 'SZ'
            rows = eastmoney_datacenter(
                'RPT_F10_FINANCE_MAINFINADATA',
                filter_str=f'(SECUCODE="{code}.{exchange}")',
                page_size=4,
                sort_columns='REPORT_DATE',
                sort_types='-1',
            )
            if not rows:
                return
            row = rows[0]

            def first(*keys):
                for key in keys:
                    value = _safe_float(row.get(key))
                    if value is not None:
                        return value
                return None

            values = {
                'roe': first('ROEJQ', 'ROE_WEIGHT', 'ROE'),
                'revenue_yoy': first('TOTALOPERATEREVETZ', 'TOTAL_OPERATE_INCOME_YOY', 'YSTZ'),
                'profit_yoy': first('PARENTNETPROFITTZ', 'PARENT_NETPROFIT_YOY', 'SJLTZ'),
            }
            for key, value in values.items():
                if value is not None:
                    data.setdefault(key, value)
            self._mark_source(data, '东方财富财务指标')
        except Exception:
            pass

    def _merge_sina_finance(self, code: str, data: Dict):
        """仅在同比字段缺失时回退新浪利润表，避免无差别重复抓取。"""
        try:
            from .sources.fundamental import sina_financial_report

            rows = sina_financial_report(code, report_type='lrb', num=8)
            if not rows:
                return
            row = rows[0]

            def find_yoy(candidates):
                for candidate in candidates:
                    for key, raw in row.items():
                        if key.endswith('_同比') and candidate in key:
                            value = _safe_float(raw)
                            if value is not None:
                                return value
                return None

            revenue_yoy = find_yoy(('营业总收入', '营业收入'))
            profit_yoy = find_yoy(('归属于母公司股东的净利润', '归属于母公司所有者的净利润', '净利润'))
            if revenue_yoy is not None:
                data.setdefault('revenue_yoy', revenue_yoy)
            if profit_yoy is not None:
                data.setdefault('profit_yoy', profit_yoy)
            self._mark_source(data, '新浪财务报表')
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
                self._mark_source(data, '通达信财务快照')
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
