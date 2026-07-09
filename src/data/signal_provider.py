"""
信号层 Provider — 封装同花顺热点/北向/龙虎榜/解禁/行业排名等信号数据

供策略引擎、AI智能体和Web面板统一使用。
所有东财请求走 em_get 限流防封。

使用方式:
    from src.data.signal_provider import SignalProvider
    sp = SignalProvider()

    # 当日强势股 + 题材归因
    hot = sp.hot_reasons()                # DataFrame: 代码/名称/涨幅%/题材归因
    topics = sp.hot_topic_ranking()       # [(topic, count), ...]

    # 北向资金
    north = sp.northbound_summary()       # {total_yi, direction, hgt_yi, sgt_yi}

    # 龙虎榜
    dt = sp.daily_dragon_tiger(min_net_buy=5000)  # 全市场龙虎榜(净买>5000万)
    seats = sp.dragon_tiger_seats("002475")       # 个股席位详情

    # 解禁
    lockup = sp.lockup_expiry("688017")           # 未来90天解禁

    # 行业
    industry = sp.industry_ranking()              # 行业涨跌排名

    # 市场全景
    overview = sp.market_overview()               # 综合快照
"""
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

# 添加项目根路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .sources.signal import (
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
)


class SignalProvider:
    """信号层数据统一入口"""

    # ── 同花顺热点 ──

    def hot_reasons(self, date: Optional[str] = None) -> pd.DataFrame:
        """
        当日强势股 + 题材归因。
        DataFrame 列: 代码, 名称, 涨幅%, 题材归因(reason), 换手率%, 成交额, 大单净量
        """
        return ths_hot_reason(date)

    def hot_topic_ranking(self, date: Optional[str] = None) -> List[tuple]:
        """当日题材热度排名 [(topic, stock_count), ...]"""
        return ths_hot_topic_ranking(date)

    # ── 北向资金 ──

    def hsgt_realtime(self) -> pd.DataFrame:
        """北向资金分钟级实时流向"""
        return hsgt_realtime()

    def northbound_summary(self) -> Dict:
        """北向资金今日摘要(含自动缓存)"""
        return northbound_today_summary()

    def northbound_history(self, n: int = 20) -> pd.DataFrame:
        """北向资金历史(n天, 本地缓存)"""
        return load_northbound_history(n)

    # ── 龙虎榜 ──

    def dragon_tiger_seats(self, code: str, trade_date: Optional[str] = None) -> Dict:
        """
        个股龙虎榜席位详情。
        返回: {records: [{date, reason, net_buy, turnover}],
               seats: {buy: [...], sell: [...]},
               institution: {buy_amt, sell_amt, net_amt}}
        """
        td = trade_date or datetime.now().strftime("%Y-%m-%d")
        return dragon_tiger_board(code, td)

    def daily_dragon_tiger(self, trade_date: Optional[str] = None,
                           min_net_buy: Optional[float] = None) -> Dict:
        """
        全市场龙虎榜。
        min_net_buy: 净买入下限(万元), 如 5000 只看净买>5000万
        """
        return daily_dragon_tiger(trade_date, min_net_buy)

    # ── 限售解禁 ──

    def lockup_expiry(self, code: str, trade_date: Optional[str] = None,
                      forward_days: int = 90) -> Dict:
        """
        限售解禁日历。
        返回: {history: [...], upcoming: [...]}
        """
        td = trade_date or datetime.now().strftime("%Y-%m-%d")
        return lockup_expiry(code, td, forward_days)

    # ── 行业排名 ──

    def industry_ranking(self, top_n: int = 20) -> Dict:
        """
        行业板块涨跌幅排名。
        返回: {top: [...], bottom: [...], total: int}
        每条: {rank, name, change_pct, up_count, down_count, leader, leader_change}
        """
        return industry_comparison(top_n)

    # ── 市场全景 ──

    def market_overview(self, trade_date: Optional[str] = None) -> Dict:
        """
        市场全景快照: 题材热度TOP10 + 北向流向 + 行业涨跌TOP5 + 龙虎榜净买TOP5。
        返回可序列化的 dict, 适合Web面板和AI提示词。
        """
        return market_overview_signal(trade_date)
