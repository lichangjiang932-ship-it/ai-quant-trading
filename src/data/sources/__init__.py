"""
A股全栈数据源 — 基于 a-stock-data V3.2.1 skill 实现
七层数据架构, 27 个端点, 零 akshare 依赖

行情层: mootdx(TCP K线/盘口/逐笔) + 腾讯(PE/PB/市值) + 百度(MA K线)
研报层: 东财研报 + 同花顺一致预期EPS
信号层: 同花顺热点归因 + 北向资金 + 龙虎榜席位 + 限售解禁 + 行业排名
资金面层: 融资融券 + 大宗交易 + 股东户数 + 分红送转 + 资金流120日
新闻层: 东财个股新闻 + 全球资讯
基础数据: mootdx 财务快照/F10 + 新浪财报三表 + 东财个股信息
公告层: 巨潮公告
估值公式: 前向PE / PE消化 / PEG / 完整估值

使用方式:
    from src.data.sources import AStockDataProvider
    provider = AStockDataProvider()
    quote = provider.tencent_quote(["688017"])    # PE/PB/市值
    kline = provider.mootdx_kline("688017")        # K线(日/周/月/分钟)
    hot = provider.ths_hot_reason()                # 当日强势股+题材
"""

from .provider import AStockDataProvider

__all__ = ["AStockDataProvider"]
