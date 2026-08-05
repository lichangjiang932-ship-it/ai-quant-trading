"""
信息渠道统一管理 & 健康监控
============================

提供全项目统一的信息渠道注册、优先级管理、健康检测和自动降级机制。

设计原则:
  - 每个数据源有明确的优先级和 fallback 链
  - 自动检测渠道可用性，不可用时静默降级
  - 支持运行时动态注册/移除渠道
  - 统一的渠道状态仪表盘数据

用法:
    from src.data.info_channels import InfoChannelManager, ChannelCategory

    mgr = InfoChannelManager()
    mgr.register("mootdx", ChannelCategory.QUOTE, priority=1)
    mgr.register("eastmoney", ChannelCategory.QUOTE, priority=2)

    # 按优先级获取第一个可用渠道的数据
    result = mgr.fetch_best("quote", fallback_fn=lambda ch: ch.get_data())

    # 获取所有渠道状态
    status = mgr.health_report()
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ChannelCategory(Enum):
    """信息渠道分类"""
    QUOTE = "quote"               # 行情数据 (K线/分时/实时价)
    FUNDAMENTAL = "fundamental"   # 基本面 (财报/估值/行业)
    NEWS = "news"                 # 新闻资讯 (快讯/公告/研报)
    SIGNAL = "signal"             # 信号数据 (龙虎榜/北向/热点)
    CAPITAL = "capital"           # 资金流向 (主力/北向/融资融券)
    SENTIMENT = "sentiment"       # 市场情绪 (涨跌比/恐慌指数)
    REFERENCE = "reference"       # 参考数据 (交易日历/除权除息)


class ChannelStatus(Enum):
    HEALTHY = "healthy"           # 正常
    DEGRADED = "degraded"        # 降级 (偶尔超时/错误)
    UNSTABLE = "unstable"        # 不稳定 (频繁失败)
    DOWN = "down"                 # 不可用


@dataclass
class ChannelInfo:
    """渠道信息"""
    name: str
    category: ChannelCategory
    priority: int = 100
    description: str = ""
    base_url: str = ""
    rate_limit_rps: float = 5.0     # 每秒请求上限
    rate_limit_per_min: int = 200   # 每分钟请求上限
    requires_auth: bool = False
    supports_realtime: bool = False
    supports_history: bool = True
    data_freshness_seconds: int = 60  # 数据新鲜度 (秒)


@dataclass
class ChannelHealth:
    """渠道健康状态"""
    status: ChannelStatus = ChannelStatus.HEALTHY
    total_calls: int = 0
    success_calls: int = 0
    fail_calls: int = 0
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    last_error: str = ""
    avg_latency_ms: float = 0.0
    consecutive_failures: int = 0
    throttled_until: float = 0.0
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_calls / self.total_calls

    @property
    def is_available(self) -> bool:
        if self.status == ChannelStatus.DOWN:
            return False
        if self.throttled_until > time.time():
            return False
        return True


class InfoChannelManager:
    """信息渠道统一管理器"""

    # 单例
    _instance: Optional["InfoChannelManager"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "InfoChannelManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._channels: Dict[str, ChannelInfo] = {}
        self._health: Dict[str, ChannelHealth] = {}
        self._fetch_fns: Dict[str, Dict[str, Callable]] = {}
        self._mu = threading.Lock()

    # ── 渠道注册 ──

    def register(
        self,
        name: str,
        category: ChannelCategory,
        priority: int = 100,
        description: str = "",
        base_url: str = "",
        rate_limit_rps: float = 5.0,
        rate_limit_per_min: int = 200,
        requires_auth: bool = False,
        supports_realtime: bool = False,
        supports_history: bool = True,
        data_freshness_seconds: int = 60,
    ) -> "InfoChannelManager":
        """注册信息渠道。"""
        with self._mu:
            self._channels[name] = ChannelInfo(
                name=name, category=category, priority=priority,
                description=description, base_url=base_url,
                rate_limit_rps=rate_limit_rps,
                rate_limit_per_min=rate_limit_per_min,
                requires_auth=requires_auth,
                supports_realtime=supports_realtime,
                supports_history=supports_history,
                data_freshness_seconds=data_freshness_seconds,
            )
            if name not in self._health:
                self._health[name] = ChannelHealth()
        return self

    def register_fetch_fn(
        self, channel_name: str, fn_name: str, fn: Callable
    ) -> "InfoChannelManager":
        """注册渠道的具体数据获取函数。"""
        with self._mu:
            if channel_name not in self._fetch_fns:
                self._fetch_fns[channel_name] = {}
            self._fetch_fns[channel_name][fn_name] = fn
        return self

    def unregister(self, name: str):
        """移除渠道。"""
        with self._mu:
            self._channels.pop(name, None)
            self._health.pop(name, None)
            self._fetch_fns.pop(name, None)

    # ── 渠道查询 ──

    def get_channels_by_category(
        self, category: ChannelCategory
    ) -> List[ChannelInfo]:
        """按分类获取渠道列表（按优先级排序）。"""
        with self._mu:
            channels = [
                c for c in self._channels.values()
                if c.category == category
            ]
            channels.sort(key=lambda c: c.priority)
            return channels

    def get_best_channel(
        self, category: ChannelCategory
    ) -> Optional[ChannelInfo]:
        """获取优先级最高且可用的渠道。"""
        for ch in self.get_channels_by_category(category):
            h = self._health.get(ch.name)
            if h and h.is_available:
                return ch
        return None

    def get_all_available(
        self, category: ChannelCategory
    ) -> List[ChannelInfo]:
        """获取所有可用渠道。"""
        return [
            ch for ch in self.get_channels_by_category(category)
            if self._health.get(ch.name) and self._health[ch.name].is_available
        ]

    # ── 健康管理 ──

    def record_success(self, channel_name: str, latency_ms: float = 0):
        """记录一次成功调用。"""
        with self._mu:
            h = self._health.setdefault(channel_name, ChannelHealth())
            h.total_calls += 1
            h.success_calls += 1
            h.last_success = time.time()
            h.consecutive_failures = 0
            if latency_ms > 0:
                h.avg_latency_ms = (
                    h.avg_latency_ms * (h.total_calls - 1) + latency_ms
                ) / h.total_calls
            self._update_status(h)

    def record_failure(self, channel_name: str, error: str = ""):
        """记录一次失败调用。"""
        with self._mu:
            h = self._health.setdefault(channel_name, ChannelHealth())
            h.total_calls += 1
            h.fail_calls += 1
            h.last_failure = time.time()
            h.last_error = error[:200]
            h.consecutive_failures += 1
            self._update_status(h)
            # 连续失败超阈值则节流
            if h.consecutive_failures >= 10:
                h.throttled_until = time.time() + 300  # 5分钟冷却
                h.status = ChannelStatus.DOWN

    def _update_status(self, h: ChannelHealth):
        """根据统计数据更新渠道状态。"""
        if h.consecutive_failures >= 10:
            h.status = ChannelStatus.DOWN
        elif h.consecutive_failures >= 5:
            h.status = ChannelStatus.UNSTABLE
        elif h.success_rate < 0.8 and h.total_calls > 10:
            h.status = ChannelStatus.DEGRADED
        else:
            h.status = ChannelStatus.HEALTHY
        h.checked_at = datetime.now().isoformat()

    def is_available(self, channel_name: str) -> bool:
        h = self._health.get(channel_name)
        return h is not None and h.is_available

    def get_health(self, channel_name: str) -> Optional[ChannelHealth]:
        return self._health.get(channel_name)

    # ── 带降级的数据获取 ──

    def fetch_with_fallback(
        self,
        category: ChannelCategory,
        fn_name: str,
        *args,
        timeout: float = 10.0,
        **kwargs,
    ) -> tuple[Any, Optional[str]]:
        """
        按优先级尝试各渠道获取数据，自动降级。
        返回 (数据, 使用的渠道名)。全部失败返回 (None, None)。
        """
        channels = self.get_channels_by_category(category)
        last_error = ""
        for ch in channels:
            h = self._health.get(ch.name)
            if h and not h.is_available:
                continue
            fn = (self._fetch_fns.get(ch.name) or {}).get(fn_name)
            if fn is None:
                continue
            try:
                t0 = time.time()
                result = fn(*args, **kwargs)
                elapsed = (time.time() - t0) * 1000
                if result is not None and (not hasattr(result, 'empty') or not result.empty):
                    self.record_success(ch.name, elapsed)
                    return result, ch.name
                self.record_failure(ch.name, "返回空数据")
            except Exception as e:
                last_error = str(e)
                self.record_failure(ch.name, last_error)
                continue
        return None, None

    # ── 报告 ──

    def health_report(self) -> Dict:
        """生成渠道健康报告。"""
        report = {}
        with self._mu:
            for name, ch in self._channels.items():
                h = self._health.get(name, ChannelHealth())
                report[name] = {
                    "category": ch.category.value,
                    "priority": ch.priority,
                    "status": h.status.value,
                    "available": h.is_available,
                    "success_rate": round(h.success_rate, 3),
                    "total_calls": h.total_calls,
                    "consecutive_failures": h.consecutive_failures,
                    "avg_latency_ms": round(h.avg_latency_ms, 1),
                    "last_error": h.last_error[:100],
                    "checked_at": h.checked_at,
                }
        return report

    def category_summary(self) -> Dict:
        """各分类渠道摘要。"""
        summary = {}
        for cat in ChannelCategory:
            channels = self.get_channels_by_category(cat)
            available = self.get_all_available(cat)
            summary[cat.value] = {
                "total": len(channels),
                "available": len(available),
                "best": available[0].name if available else None,
                "all": [c.name for c in channels],
            }
        return summary


# ── 预配置: 项目默认渠道 ──

def init_default_channels():
    """初始化项目默认的信息渠道。"""
    mgr = InfoChannelManager()

    # 行情数据
    mgr.register("mootdx", ChannelCategory.QUOTE, priority=1,
                 description="通达信TCP行情，不封IP", supports_realtime=True)
    mgr.register("baostock", ChannelCategory.QUOTE, priority=2,
                 description="免费证券数据，无需Token", supports_history=True)
    mgr.register("eastmoney", ChannelCategory.QUOTE, priority=3,
                 description="东方财富HTTP行情，数据最全", supports_realtime=True,
                 rate_limit_rps=3.0)
    mgr.register("akshare", ChannelCategory.QUOTE, priority=4,
                 description="AKShare开源数据接口", supports_history=True)
    mgr.register("yfinance", ChannelCategory.QUOTE, priority=5,
                 description="Yahoo Finance (海外市场)", supports_history=True)

    # 新闻资讯
    mgr.register("sina", ChannelCategory.NEWS, priority=1,
                 description="新浪财经7x24快讯", rate_limit_rps=10.0)
    mgr.register("cls", ChannelCategory.NEWS, priority=2,
                 description="财联社电报（最快）", rate_limit_rps=5.0)
    mgr.register("eastmoney_news", ChannelCategory.NEWS, priority=3,
                 description="东方财富新闻+公告", rate_limit_rps=3.0)
    mgr.register("ths", ChannelCategory.NEWS, priority=4,
                 description="同花顺财经新闻")
    mgr.register("cninfo", ChannelCategory.NEWS, priority=5,
                 description="巨潮资讯（法定信披）")

    # 信号数据
    mgr.register("eastmoney_signal", ChannelCategory.SIGNAL, priority=1,
                 description="东财龙虎榜/北向/热点", rate_limit_rps=3.0)
    mgr.register("ths_signal", ChannelCategory.SIGNAL, priority=2,
                 description="同花顺热点/题材归因")

    # 资金流向
    mgr.register("eastmoney_capital", ChannelCategory.CAPITAL, priority=1,
                 description="东方财富资金流向", rate_limit_rps=3.0)

    return mgr
