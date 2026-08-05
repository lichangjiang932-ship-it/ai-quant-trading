"""
A股全栈数据统一入口 — AStockDataProvider

整合全部七层数据架构, 统一接口, 零 akshare 依赖。
所有东财请求走 em_get 内置限流防封。

使用方式:
    from src.data.sources import AStockDataProvider
    p = AStockDataProvider()

    # 行情
    quote = p.tencent_quote(["688017"])          # PE/PB/市值/涨跌停
    kline = p.mootdx_kline("688017", 4)          # 日线K线
    baidu = p.baidu_kline_with_ma("600519")      # K线带MA5/10/20

    # 信号
    hot = p.ths_hot_reason()                     # 当日强势股+题材归因
    north = p.northbound_summary()               # 北向资金摘要
    dt = p.daily_dragon_tiger()                  # 全市场龙虎榜
    industry = p.industry_comparison(10)          # 行业排名

    # 资金面
    margin = p.margin_trading("600519")          # 融资融券
    flow = p.fund_flow_summary("600519")         # 120日资金流摘要

    # 基础数据 + 估值
    info = p.stock_info("688017")                # 个股基本信息
    val = p.full_valuation("688017")             # 完整估值分析
    concepts = p.concept_blocks("688017")        # 概念板块

    # 市场全景
    overview = p.market_overview()               # 市场快照(题材+北向+行业)
"""

from typing import Dict, List, Optional

import pandas as pd

from .quote import (
    MootdxSource,
    tencent_quote,
    baidu_kline_with_ma,
    baidu_kline_df,
    baidu_concept_blocks,
    eastmoney_stock_info,
)
from .signal import (
    ths_hot_reason,
    ths_hot_topic_ranking,
    hsgt_realtime,
    northbound_today_summary,
    load_northbound_history,
    dragon_tiger_board,
    daily_dragon_tiger,
    lockup_expiry,
    industry_comparison,
    market_overview_signal,
    market_breadth,
)
from .fundamental import (
    margin_trading,
    block_trade,
    holder_num_change,
    dividend_history,
    stock_fund_flow_120d,
    fund_flow_summary,
    eastmoney_reports,
    ths_eps_forecast,
    sina_financial_report,
    cninfo_announcements,
    forward_pe,
    pe_digestion,
    calc_peg,
    full_valuation,
)


class AStockDataProvider:
    """
    A股全栈数据统一入口。

    整合 skill 全部七层数据架构, 通过统一接口暴露:
    - 行情层: mootdx(TCP)/腾讯(HTTP)/百度(HTTP)
    - 研报层: 东财研报 + 同花顺一致预期
    - 信号层: 同花顺热点 + 北向 + 龙虎榜 + 解禁 + 行业排名
    - 资金面层: 两融/大宗/股东/分红/资金流
    - 新闻层: 东财个股新闻/全球资讯
    - 基础数据: 财务报表/F10
    - 公告层: 巨潮公告
    - 估值公式: forward_pe/pe_digestion/PEG/full_valuation

    东财请求自动走 em_get 限流(≥1s间隔+随机抖动), 防止封IP。
    """

    def __init__(self):
        self._mootdx = MootdxSource()

    # ── 行情层 ──

    @property
    def mootdx(self) -> MootdxSource:
        """mootdx 源: K线/五档盘口/逐笔成交/财务快照/F10"""
        return self._mootdx

    def tencent_quote(self, codes: List[str]) -> Dict[str, Dict]:
        """腾讯财经实时行情: PE/PB/市值/换手率/涨跌停/指数/ETF"""
        return tencent_quote(codes)

    def baidu_kline_with_ma(self, code: str) -> Dict:
        """百度K线(带MA5/MA10/MA20)"""
        return baidu_kline_with_ma(code)

    def baidu_kline_df(self, code: str) -> pd.DataFrame:
        """百度K线 DataFrame 含 MA 列"""
        return baidu_kline_df(code)

    def concept_blocks(self, code: str) -> Dict:
        """概念板块归属(行业/概念/地域)"""
        return baidu_concept_blocks(code)

    def stock_info(self, code: str) -> Dict:
        """东财个股基本面(行业/股本/市值/上市日期)"""
        return eastmoney_stock_info(code)

    # ── 信号层 ──

    def ths_hot_reason(self, date: Optional[str] = None) -> pd.DataFrame:
        """同花顺当日强势股+题材归因"""
        return ths_hot_reason(date)

    def ths_hot_topic_ranking(self, date: Optional[str] = None) -> List[tuple]:
        """当日题材热度排名 [(topic, count), ...]"""
        return ths_hot_topic_ranking(date)

    def hsgt_realtime(self) -> pd.DataFrame:
        """北向资金分钟级实时流向"""
        return hsgt_realtime()

    def northbound_summary(self) -> Dict:
        """北向资金今日摘要(含自动缓存)"""
        return northbound_today_summary()

    def northbound_history(self, n: int = 20) -> pd.DataFrame:
        """北向资金历史(本地缓存)"""
        return load_northbound_history(n)

    def dragon_tiger_board(self, code: str, trade_date: Optional[str] = None) -> Dict:
        """个股龙虎榜(席位+机构动向)"""
        from datetime import datetime as dt
        td = trade_date or dt.now().strftime("%Y-%m-%d")
        return dragon_tiger_board(code, td)

    def daily_dragon_tiger(self, trade_date: Optional[str] = None, min_net_buy: Optional[float] = None) -> Dict:
        """全市场龙虎榜"""
        return daily_dragon_tiger(trade_date, min_net_buy)

    def lockup_expiry(self, code: str, trade_date: Optional[str] = None) -> Dict:
        """限售解禁日历"""
        from datetime import datetime as dt
        td = trade_date or dt.now().strftime("%Y-%m-%d")
        return lockup_expiry(code, td)

    def industry_comparison(self, top_n: int = 20) -> Dict:
        """行业板块排名"""
        return industry_comparison(top_n)

    def market_overview(self, trade_date: Optional[str] = None) -> Dict:
        """市场全景快照"""
        return market_overview_signal(trade_date)

    def market_breadth(self, trade_date: Optional[str] = None) -> Dict:
        """市场宽度: 涨跌家数/涨停跌停/赚钱效应"""
        return market_breadth(trade_date)

    # ── 资金面层 ──

    def margin_trading(self, code: str, page_size: int = 30) -> List[Dict]:
        """融资融券明细"""
        return margin_trading(code, page_size)

    def block_trade(self, code: str, page_size: int = 20) -> List[Dict]:
        """大宗交易"""
        return block_trade(code, page_size)

    def holder_num_change(self, code: str, page_size: int = 10) -> List[Dict]:
        """股东户数变化"""
        return holder_num_change(code, page_size)

    def dividend_history(self, code: str, page_size: int = 20) -> List[Dict]:
        """分红送转"""
        return dividend_history(code, page_size)

    def fund_flow_120d(self, code: str) -> List[Dict]:
        """个股资金流120日"""
        return stock_fund_flow_120d(code)

    def fund_flow_summary(self, code: str, recent_days: int = 20) -> Dict:
        """资金流摘要"""
        return fund_flow_summary(code, recent_days)

    # ── 研报层 ──

    def research_reports(self, code: str, max_pages: int = 3) -> List[Dict]:
        """东财研报列表"""
        return eastmoney_reports(code, max_pages)

    def consensus_eps(self, code: str) -> pd.DataFrame:
        """同花顺一致预期EPS"""
        return ths_eps_forecast(code)

    # ── 基础数据层 ──

    def financial_report(self, code: str, report_type: str = "lrb", num: int = 8) -> List[Dict]:
        """新浪财报三表"""
        return sina_financial_report(code, report_type, num)

    def announcements(self, code: str, page_size: int = 30) -> List[Dict]:
        """巨潮公告"""
        return cninfo_announcements(code, page_size)

    # ── 估值 ──

    def forward_pe(self, price: float, eps_forecast: float) -> float:
        """前向PE"""
        return forward_pe(price, eps_forecast)

    def pe_digestion(self, current_pe: float, cagr: float, target_pe: float = 30) -> float:
        """PE消化时间"""
        return pe_digestion(current_pe, cagr, target_pe)

    def calc_peg(self, pe: float, cagr: float) -> float:
        """PEG"""
        return calc_peg(pe, cagr)

    def full_valuation(self, code: str) -> Dict:
        """完整估值分析"""
        return full_valuation(code)

    # ── 批量估值对比 ──

    def batch_valuation(self, codes: List[str]) -> pd.DataFrame:
        """批量估值对比 DataFrame"""
        results = []
        for code in codes:
            try:
                r = full_valuation(code)
                if r:
                    results.append(r)
            except Exception:
                pass
        if not results:
            return pd.DataFrame()
        return pd.DataFrame(results)

    # ── 综合股票快照 ──

    def stock_snapshot(self, code: str) -> Dict:
        """
        单股综合快照: 行情 + 估值 + 资金流 + 概念。
        供策略分析师和AI智能体一站式使用。
        """
        result = {"code": code}

        # 行情+估值
        val = self.full_valuation(code)
        if val:
            result.update(val)

        # 概念
        concepts = self.concept_blocks(code)
        result["concept_tags"] = concepts.get("concept_tags", [])
        result["industry_name"] = (concepts.get("industry", [{}])[0] or {}).get("name", "")

        # 资金流(20日)
        flow = self.fund_flow_summary(code, 20)
        if flow:
            result["fund_flow"] = flow

        # 个股基本信息
        info = self.stock_info(code)
        if info:
            result["industry"] = info.get("industry", "")
            result["total_shares"] = info.get("total_shares", 0)
            result["float_shares"] = info.get("float_shares", 0)
            result["list_date"] = info.get("list_date", "")

        return result
