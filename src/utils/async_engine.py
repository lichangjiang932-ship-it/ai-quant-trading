import asyncio
import threading
import time
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
from datetime import datetime
import signal
import sys


class TaskPriority(Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class AsyncEngine:
    def __init__(self, name: str = "TradingEngine"):
        self.name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_queue: asyncio.PriorityQueue = None
        self._shutdown_event = asyncio.Event()
        self._latency_recorder: List[Dict] = []
        self._max_latency_samples = 10000

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name=f"{self.name}-EventLoop")
        self._thread.start()
        time.sleep(0.1)

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task_queue = asyncio.PriorityQueue()

        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as e:
            print(f"[AsyncEngine] 事件循环异常: {e}")
        finally:
            self._loop.close()

    async def _main_loop(self):
        main_task = asyncio.create_task(self._process_queue(), name=f"{self.name}-QueueProcessor")
        watch_task = asyncio.create_task(self._watchdog(), name=f"{self.name}-Watchdog")

        await self._shutdown_event.wait()

        main_task.cancel()
        watch_task.cancel()
        for name, task in self._tasks.items():
            task.cancel()

        pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _process_queue(self):
        while not self._shutdown_event.is_set():
            try:
                priority, coro, callback = await asyncio.wait_for(
                    self._task_queue.get(), timeout=0.5
                )
                try:
                    start = time.perf_counter()
                    result = await coro
                    elapsed = (time.perf_counter() - start) * 1000
                    self._record_latency(priority.name, elapsed)

                    if callback:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(result)
                        else:
                            callback(result)
                except Exception as e:
                    print(f"[AsyncEngine] 任务执行失败: {e}")
            except asyncio.TimeoutError:
                continue

    async def _watchdog(self):
        while not self._shutdown_event.is_set():
            await asyncio.sleep(5)
            total_tasks = len(self._tasks)
            queue_size = self._task_queue.qsize() if self._task_queue else 0
            if queue_size > 100:
                print(f"[AsyncEngine] 警告: 任务队列积压 {queue_size}")

    def _record_latency(self, priority: str, ms: float):
        self._latency_recorder.append({
            'time': datetime.now(),
            'priority': priority,
            'latency_ms': round(ms, 3)
        })
        if len(self._latency_recorder) > self._max_latency_samples:
            self._latency_recorder = self._latency_recorder[-self._max_latency_samples:]

    def submit(self, name: str, coro, priority: TaskPriority = TaskPriority.NORMAL,
               callback: Optional[Callable] = None):
        if not self._running or not self._task_queue:
            print(f"[AsyncEngine] 引擎未启动")
            return

        async def wrapped():
            try:
                return await coro
            except Exception as e:
                print(f"[AsyncEngine] 任务 {name} 异常: {e}")
                raise

        future = asyncio.run_coroutine_threadsafe(
            self._task_queue.put((priority.value, wrapped(), callback)),
            self._loop
        )

    def create_task(self, name: str, coro, cancel_previous: bool = True):
        if not self._loop or not self._running:
            return

        if cancel_previous and name in self._tasks:
            self._tasks[name].cancel()

        task = asyncio.run_coroutine_threadsafe(
            self._schedule_task(name, coro), self._loop
        )

    async def _schedule_task(self, name: str, coro):
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if name in self._tasks:
                del self._tasks[name]

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
        if self._thread:
            self._thread.join(timeout=5)
        print(f"[AsyncEngine] {self.name} 已停止")

    def get_latency_stats(self) -> Dict:
        if not self._latency_recorder:
            return {}
        df = pd.DataFrame(self._latency_recorder)
        stats = {}
        for priority in ['CRITICAL', 'HIGH', 'NORMAL', 'LOW']:
            subset = df[df['priority'] == priority]['latency_ms']
            if not subset.empty:
                stats[priority] = {
                    'avg_ms': round(subset.mean(), 3),
                    'p50_ms': round(subset.median(), 3),
                    'p95_ms': round(subset.quantile(0.95), 3),
                    'p99_ms': round(subset.quantile(0.99), 3),
                    'max_ms': round(subset.max(), 3),
                    'count': len(subset)
                }
        return stats

    def is_running(self) -> bool:
        return self._running and self._loop and not self._loop.is_closed()


try:
    import pandas as pd
except ImportError:
    pass
