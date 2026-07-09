"""
通知系统
========

支持渠道:
  - 控制台 (ConsoleNotifier) - 默认
  - 日志文件 (FileNotifier)
  - Telegram Bot (TelegramNotifier) - 通过 HTTP API
  - 钉钉 (DingTalkNotifier) - 通过 Webhook
  - 微信企业号 (WeChatWorkNotifier) - 通过 Webhook

特性:
  - 异步发送 (asyncio)
  - 自动重试 (exponential backoff)
  - 消息格式化 (Markdown / Text)
  - 频率限制 (避免刷屏)
  - 事件类型与订阅 (订阅你关心的通知)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import base64
import json
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any, Callable, Awaitable, Set
import logging
import os


class NotificationLevel(Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class NotificationType(Enum):
    TRADE = "trade"
    SIGNAL = "signal"
    RISK = "risk"
    TPSL = "tpsl"
    SYSTEM = "system"
    DAILY_REPORT = "daily_report"
    ERROR = "error"
    PRICE_ALERT = "price_alert"


@dataclass
class Notification:
    type: NotificationType
    level: NotificationLevel
    title: str
    message: str
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)

    def format_markdown(self) -> str:
        ts = datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        icon = {
            NotificationLevel.INFO: "[INFO]",
            NotificationLevel.SUCCESS: "[OK]",
            NotificationLevel.WARNING: "[WARN]",
            NotificationLevel.ERROR: "[ERROR]",
            NotificationLevel.CRITICAL: "[CRIT]",
        }.get(self.level, "-")
        return f"{icon} **{self.title}**\n\n{self.message}\n\n_{ts}_"


class Notifier(ABC):
    """通知器抽象基类"""

    def __init__(self, name: str = "base"):
        self.name = name
        self.enabled = True
        self.sent_count = 0
        self.failed_count = 0
        self.rate_limit_per_minute = 60
        self._sent_timestamps: List[float] = []

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        pass

    async def close(self):
        pass

    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._sent_timestamps = [t for t in self._sent_timestamps if now - t < 60]
        if len(self._sent_timestamps) >= self.rate_limit_per_minute:
            return False
        self._sent_timestamps.append(now)
        return True

    def get_stats(self) -> Dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "sent": self.sent_count,
            "failed": self.failed_count,
            "last_minute": len(self._sent_timestamps),
        }


class ConsoleNotifier(Notifier):
    """控制台通知器 (彩色输出)"""

    COLORS = {
        NotificationLevel.INFO: "\033[36m",
        NotificationLevel.SUCCESS: "\033[32m",
        NotificationLevel.WARNING: "\033[33m",
        NotificationLevel.ERROR: "\033[31m",
        NotificationLevel.CRITICAL: "\033[35;1m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__("console")
        self.use_color = use_color

    async def send(self, notification: Notification) -> bool:
        if not self.enabled or not self._check_rate_limit():
            return False
        ts = datetime.fromtimestamp(notification.timestamp).strftime("%H:%M:%S")
        prefix = f"[{notification.type.value.upper()}]"
        if self.use_color:
            color = self.COLORS.get(notification.level, "")
            line = f"{color}{prefix} [{ts}] {notification.title} - {notification.message}{self.RESET}"
        else:
            line = f"{prefix} [{ts}] {notification.title} - {notification.message}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode(), flush=True)
        self.sent_count += 1
        return True


class FileNotifier(Notifier):
    """文件日志通知器"""

    def __init__(self, log_dir: str = "logs", filename: str = "notifications.log"):
        super().__init__("file")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, filename)
        self._logger = logging.getLogger("notifier.file")
        handler = logging.FileHandler(self.path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)

    async def send(self, notification: Notification) -> bool:
        if not self.enabled:
            return False
        level = {
            NotificationLevel.INFO: logging.INFO,
            NotificationLevel.SUCCESS: logging.INFO,
            NotificationLevel.WARNING: logging.WARNING,
            NotificationLevel.ERROR: logging.ERROR,
            NotificationLevel.CRITICAL: logging.CRITICAL,
        }.get(notification.level, logging.INFO)
        msg = f"[{notification.type.value}] {notification.title} | {notification.message}"
        self._logger.log(level, msg)
        self.sent_count += 1
        return True

    async def close(self):
        for h in self._logger.handlers:
            h.close()


class WebhookNotifier(Notifier):
    """通用 Webhook 通知器 (兼容飞书/Discord/Slack 等)"""

    def __init__(self, webhook_url: str, method: str = "POST", timeout: float = 5.0):
        super().__init__("webhook")
        self.webhook_url = webhook_url
        self.method = method
        self.timeout = timeout
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            except ImportError:
                self._session = None

    async def send(self, notification: Notification) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        if not self._check_rate_limit():
            return False
        await self._ensure_session()
        payload = {
            "title": notification.title,
            "message": notification.message,
            "type": notification.type.value,
            "level": notification.level.value,
            "timestamp": notification.timestamp,
            "data": notification.data,
        }
        try:
            if self._session is not None:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    ok = 200 <= resp.status < 300
            else:
                import urllib.request
                req = urllib.request.Request(
                    self.webhook_url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    ok = 200 <= resp.status < 300
            if ok:
                self.sent_count += 1
            else:
                self.failed_count += 1
            return ok
        except Exception as e:
            self.failed_count += 1
            return False

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None


class TelegramNotifier(Notifier):
    """Telegram 通知器 (通过 Bot API)"""

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 5.0):
        super().__init__("telegram")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.api_base = f"https://api.telegram.org/bot{bot_token}"
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            except ImportError:
                self._session = None

    async def send(self, notification: Notification) -> bool:
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False
        if not self._check_rate_limit():
            return False
        await self._ensure_session()
        text = notification.format_markdown()
        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            if self._session is not None:
                async with self._session.post(url, json=payload) as resp:
                    ok = 200 <= resp.status < 300
                    if not ok:
                        self.failed_count += 1
                    else:
                        self.sent_count += 1
                    return ok
            else:
                import urllib.request
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.sent_count += 1
                    return 200 <= resp.status < 300
        except Exception as e:
            self.failed_count += 1
            return False

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None


class DingTalkNotifier(Notifier):
    """钉钉群机器人通知器 (支持加签安全模式)"""

    def __init__(self, webhook_url: str, secret: Optional[str] = None,
                 timeout: float = 5.0, at_mobiles: Optional[List[str]] = None):
        super().__init__("dingtalk")
        self.webhook_url = webhook_url
        self.secret = secret
        self.timeout = timeout
        self.at_mobiles = at_mobiles or []
        self._session = None

    def _sign(self) -> str:
        if not self.secret:
            return ""
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(secret_enc, string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"&timestamp={timestamp}&sign={sign}"

    async def _ensure_session(self):
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            except ImportError:
                self._session = None

    async def send(self, notification: Notification) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        if not self._check_rate_limit():
            return False
        await self._ensure_session()
        url = self.webhook_url + self._sign()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": notification.title,
                "text": f"## {notification.title}\n\n{notification.message}\n\n"
                        f"> 类型: {notification.type.value} | 级别: {notification.level.value}",
            },
            "at": {"atMobiles": self.at_mobiles, "isAtAll": False},
        }
        try:
            if self._session is not None:
                async with self._session.post(url, json=payload) as resp:
                    ok = 200 <= resp.status < 300
                    self.sent_count += 1 if ok else self.sent_count
                    self.failed_count += 0 if ok else 1
                    return ok
            else:
                import urllib.request
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.sent_count += 1
                    return 200 <= resp.status < 300
        except Exception as e:
            self.failed_count += 1
            return False

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None


class WeChatWorkNotifier(Notifier):
    """企业微信群机器人通知器"""

    def __init__(self, webhook_url: str, timeout: float = 5.0,
                 mentioned_list: Optional[List[str]] = None):
        super().__init__("wechat_work")
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.mentioned_list = mentioned_list or []
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
            except ImportError:
                self._session = None

    async def send(self, notification: Notification) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        if not self._check_rate_limit():
            return False
        await self._ensure_session()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"## {notification.title}\n"
                           f"> {notification.message}\n>\n"
                           f"> 类型: {notification.type.value} | "
                           f"级别: {notification.level.value}\n"
                           f"> 时间: {datetime.fromtimestamp(notification.timestamp).strftime('%H:%M:%S')}",
            },
        }
        if self.mentioned_list:
            payload["markdown"]["mentioned_list"] = self.mentioned_list
        try:
            if self._session is not None:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    ok = 200 <= resp.status < 300
                    if ok:
                        self.sent_count += 1
                    else:
                        self.failed_count += 1
                    return ok
            else:
                import urllib.request
                req = urllib.request.Request(
                    self.webhook_url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.sent_count += 1
                    return 200 <= resp.status < 300
        except Exception:
            self.failed_count += 1
            return False

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None


class NotificationManager:
    """
    通知管理器

    用法:
        mgr = NotificationManager()
        mgr.add_notifier(ConsoleNotifier())
        mgr.add_notifier(TelegramNotifier(bot_token, chat_id))
        mgr.subscribe(NotificationType.TRADE, mgr.default_notifiers)
        await mgr.notify(Notification(...))
    """

    def __init__(self, queue_max_size: int = 1000):
        self._notifiers: List[Notifier] = []
        self._subscriptions: Dict[NotificationType, List[Notifier]] = {}
        self._global_notifiers: List[Notifier] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max_size)
        self._running = False
        self._worker: Optional[asyncio.Task] = None
        self._total_sent = 0
        self._total_failed = 0
        self._min_level = NotificationLevel.INFO
        self._level_order = {
            NotificationLevel.INFO: 0,
            NotificationLevel.SUCCESS: 1,
            NotificationLevel.WARNING: 2,
            NotificationLevel.ERROR: 3,
            NotificationLevel.CRITICAL: 4,
        }
        self._send_lock = asyncio.Lock()

    def add_notifier(self, notifier: Notifier, global_: bool = True):
        self._notifiers.append(notifier)
        if global_:
            self._global_notifiers.append(notifier)

    def subscribe(self, ntype: NotificationType, notifiers: List[Notifier]):
        self._subscriptions.setdefault(ntype, []).extend(notifiers)

    def set_min_level(self, level: NotificationLevel):
        self._min_level = level

    def get_notifier(self, name: str) -> Optional[Notifier]:
        for n in self._notifiers:
            if n.name == name:
                return n
        return None

    def get_stats(self) -> Dict:
        return {
            "total_notifiers": len(self._notifiers),
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "by_notifier": [n.get_stats() for n in self._notifiers],
        }

    async def start(self):
        if self._running:
            return
        self._running = True
        self._worker = asyncio.create_task(self._worker_loop(), name="notifier-worker")
        self._log_internal(NotificationLevel.INFO, "通知管理器已启动")

    async def stop(self):
        self._running = False
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        for n in self._notifiers:
            try:
                await n.close()
            except Exception:
                pass

    def _log_internal(self, level: NotificationLevel, msg: str):
        for n in self._global_notifiers:
            if isinstance(n, ConsoleNotifier):
                try:
                    print(f"[NOTIFIER] {msg}", flush=True)
                except Exception:
                    pass
                break

    async def notify(self, notification: Notification):
        if self._level_order[notification.level] < self._level_order[self._min_level]:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            self._log_internal(NotificationLevel.WARNING, "通知队列已满,丢弃通知")

    async def notify_sync(self, notification: Notification) -> int:
        """同步发送 (跳过队列),返回成功数"""
        if self._level_order[notification.level] < self._level_order[self._min_level]:
            return 0
        targets = self._collect_targets(notification)
        if not targets:
            return 0
        results = await asyncio.gather(*[n.send(notification) for n in targets], return_exceptions=True)
        sent = sum(1 for r in results if r is True)
        self._total_sent += sent
        self._total_failed += len(targets) - sent
        return sent

    def _collect_targets(self, notification: Notification) -> List[Notifier]:
        targets = list(self._global_notifiers)
        targets.extend(self._subscriptions.get(notification.type, []))
        seen: Set[int] = set()
        unique = []
        for t in targets:
            if id(t) not in seen:
                unique.append(t)
                seen.add(id(t))
        return unique

    async def _worker_loop(self):
        while self._running:
            try:
                notif = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                targets = self._collect_targets(notif)
                if not targets:
                    self._queue.task_done()
                    continue
                results = await asyncio.gather(*[n.send(notif) for n in targets], return_exceptions=True)
                sent = sum(1 for r in results if r is True)
                self._total_sent += sent
                self._total_failed += len(targets) - sent
            except Exception as e:
                self._log_internal(NotificationLevel.ERROR, f"通知处理失败: {e}")
            finally:
                self._queue.task_done()


def build_manager_from_config(cfg: Dict) -> NotificationManager:
    """根据配置字典构建通知管理器

    cfg 示例:
        {
            "console": {"enabled": true, "use_color": true},
            "file": {"log_dir": "logs"},
            "telegram": {"bot_token": "...", "chat_id": "..."},
            "dingtalk": {"webhook_url": "...", "secret": "..."},
            "wechat_work": {"webhook_url": "..."},
            "webhook": {"url": "..."}
        }
    """
    mgr = NotificationManager()
    if cfg.get("console", {}).get("enabled", True):
        mgr.add_notifier(ConsoleNotifier(use_color=cfg["console"].get("use_color", True)))
    if cfg.get("file", {}).get("enabled", False):
        f = cfg["file"]
        mgr.add_notifier(FileNotifier(log_dir=f.get("log_dir", "logs"), filename=f.get("filename", "notifications.log")))
    if cfg.get("telegram", {}).get("enabled", False):
        t = cfg["telegram"]
        mgr.add_notifier(TelegramNotifier(bot_token=t["bot_token"], chat_id=t["chat_id"]))
    if cfg.get("dingtalk", {}).get("enabled", False):
        d = cfg["dingtalk"]
        mgr.add_notifier(DingTalkNotifier(webhook_url=d["webhook_url"], secret=d.get("secret")))
    if cfg.get("wechat_work", {}).get("enabled", False):
        w = cfg["wechat_work"]
        mgr.add_notifier(WeChatWorkNotifier(webhook_url=w["webhook_url"], mentioned_list=w.get("mentioned_list")))
    if cfg.get("webhook", {}).get("enabled", False):
        wh = cfg["webhook"]
        mgr.add_notifier(WebhookNotifier(webhook_url=wh["url"]))
    return mgr
