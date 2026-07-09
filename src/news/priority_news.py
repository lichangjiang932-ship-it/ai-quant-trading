import asyncio
import time
import json
import re
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime, timedelta
from collections import defaultdict
from enum import Enum
from dataclasses import dataclass, field


class NewsPriority(Enum):
    BREAKING = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class NewsCategory(Enum):
    POLICY = "政策"
    MARKET = "市场"
    INDUSTRY = "行业"
    COMPANY = "公司"
    MACRO = "宏观"
    SENTIMENT = "情绪"


@dataclass(order=True)
class PriorityNewsItem:
    priority: int
    timestamp: float
    data: Dict = field(compare=False)


class PriorityNewsPipeline:
    def __init__(self, max_queue_size: int = 500):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._callbacks: Dict[NewsPriority, List[Callable]] = defaultdict(list)
        self._global_callbacks: List[Callable] = []
        self._processed_ids: set = set()
        self._stats = {
            'total_received': 0,
            'total_processed': 0,
            'total_breaking': 0,
            'avg_process_latency_ms': 0.0,
            'last_process_time': None,
        }
        self._total_latency = 0.0
        self._process_count = 0
        self._running = False
        self._worker_task = None

        self._breaking_keywords = [
            '突发', '重磅', '紧急', '央行', '降息', '加息', '降准',
            '国务院', '证监会', '政治局', '中央', '暴雷', '跌停',
            '涨停', '熔断', '退市', '暂停上市', 'st', '*st',
        ]
        self._high_keywords = [
            '政策', '监管', '反垄断', '制裁', '重大合同', '业绩预告',
            '收购', '重组', '增发', '分红', '回购', '减持', '增持',
        ]

    def register_callback(self, callback: Callable, priority: NewsPriority = None):
        if priority:
            self._callbacks[priority].append(callback)
        else:
            self._global_callbacks.append(callback)

    async def push(self, news_list: List[Dict]):
        for news in news_list:
            news_id = self._gen_news_id(news)
            if news_id in self._processed_ids:
                continue
            self._processed_ids.add(news_id)

            priority = self._classify_priority(news)
            item = PriorityNewsItem(
                priority=priority.value,
                timestamp=time.time(),
                data={**news, '_priority': priority.name, '_id': news_id}
            )

            try:
                await asyncio.wait_for(self._queue.put(item), timeout=0.1)
                self._stats['total_received'] += 1
                if priority == NewsPriority.BREAKING:
                    self._stats['total_breaking'] += 1
            except asyncio.TimeoutError:
                print(f"[PriorityNews] 队列满，丢弃新闻: {news.get('title', '')[:30]}")

        if len(self._processed_ids) > 10000:
            self._processed_ids = set(list(self._processed_ids)[-5000:])

    def _gen_news_id(self, news: Dict) -> str:
        title = news.get('title', '') or news.get('news', {}).get('title', '')
        pub_time = news.get('publish_time', '') or news.get('news', {}).get('publish_time', '')
        return f"{title[:50]}_{pub_time}"

    def _classify_priority(self, news: Dict) -> NewsPriority:
        title = news.get('title', '') or news.get('news', {}).get('title', '')
        content = news.get('content', '') or news.get('news', {}).get('content', '')
        text = (title + ' ' + content).lower()

        for kw in self._breaking_keywords:
            if kw.lower() in text:
                return NewsPriority.BREAKING

        for kw in self._high_keywords:
            if kw.lower() in text:
                return NewsPriority.HIGH

        importance = news.get('importance', 0) or news.get('news', {}).get('importance', 0)
        sentiment_score = abs(news.get('sentiment', {}).get('score', 0) or
                              news.get('news', {}).get('sentiment_score', 0))

        if importance >= 7 or sentiment_score >= 0.6:
            return NewsPriority.HIGH
        elif importance >= 5 or sentiment_score >= 0.3:
            return NewsPriority.NORMAL
        else:
            return NewsPriority.LOW

    async def start(self):
        self._running = True
        self._worker_task = asyncio.create_task(self._process_loop())

    async def stop(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _process_loop(self):
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                t0 = time.perf_counter()

                await self._dispatch(item)
                self._queue.task_done()

                elapsed = (time.perf_counter() - t0) * 1000
                self._total_latency += elapsed
                self._process_count += 1
                self._stats['total_processed'] += 1
                self._stats['last_process_time'] = datetime.now().isoformat()

                priority_name = NewsPriority(item.priority).name
                if priority_name == 'BREAKING':
                    print(f"[PriorityNews] 紧急处理: {item.data.get('title', '')[:50]} ({elapsed:.1f}ms)")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[PriorityNews] 处理异常: {e}")

    async def _dispatch(self, item: PriorityNewsItem):
        priority = NewsPriority(item.priority)

        for cb in self._callbacks.get(priority, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(item.data)
                else:
                    cb(item.data)
            except Exception as e:
                print(f"[PriorityNews] 优先级回调失败: {e}")

        if priority == NewsPriority.BREAKING:
            for cb in self._global_callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(item.data)
                    else:
                        cb(item.data)
                except Exception as e:
                    print(f"[PriorityNews] 全局回调失败: {e}")

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            'queue_size': self._queue.qsize(),
            'avg_process_latency_ms': round(
                self._total_latency / max(self._process_count, 1), 3
            ),
            'processed_ids_count': len(self._processed_ids),
        }
