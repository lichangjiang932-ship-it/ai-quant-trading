"""
通知系统测试
"""
import os
import sys
import asyncio
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notification.notifier import (
    ConsoleNotifier, FileNotifier, WebhookNotifier,
    TelegramNotifier, DingTalkNotifier, WeChatWorkNotifier,
    NotificationManager, Notification, NotificationType, NotificationLevel,
    build_manager_from_config,
)


def make_notif(level=NotificationLevel.INFO):
    return Notification(
        type=NotificationType.SIGNAL, level=level,
        title="测试标题", message="测试内容",
    )


@pytest.mark.asyncio
async def test_console_sends():
    n = ConsoleNotifier(use_color=False)
    ok = await n.send(make_notif())
    assert ok is True
    assert n.sent_count == 1


@pytest.mark.asyncio
async def test_file_writes():
    with tempfile.TemporaryDirectory() as tmp:
        n = FileNotifier(log_dir=tmp, filename="test.log")
        ok = await n.send(Notification(
            type=NotificationType.TRADE, level=NotificationLevel.SUCCESS,
            title="买入", message="sh600000 1000股"
        ))
        assert ok is True
        with open(os.path.join(tmp, "test.log"), encoding="utf-8") as f:
            content = f.read()
        assert "买入" in content
        assert "sh600000" in content
        await n.close()


@pytest.mark.asyncio
async def test_manager_dispatches():
    mgr = NotificationManager()
    c = ConsoleNotifier(use_color=False)
    f = FileNotifier(log_dir=tempfile.mkdtemp(), filename="mgr.log")
    mgr.add_notifier(c)
    mgr.add_notifier(f)
    await mgr.start()
    for i in range(3):
        await mgr.notify(Notification(
            type=NotificationType.TRADE, level=NotificationLevel.INFO,
            title=f"trade{i}", message="msg"
        ))
    await asyncio.sleep(1.0)
    stats = mgr.get_stats()
    assert stats["total_sent"] >= 3
    await mgr.stop()


@pytest.mark.asyncio
async def test_rate_limit():
    n = ConsoleNotifier(use_color=False)
    n.rate_limit_per_minute = 3
    for i in range(5):
        await n.send(make_notif())
    assert n.sent_count == 3


@pytest.mark.asyncio
async def test_disabled_notifier():
    n = ConsoleNotifier(use_color=False)
    n.enabled = False
    ok = await n.send(make_notif())
    assert ok is False
    assert n.sent_count == 0


def test_markdown_format():
    n = Notification(
        type=NotificationType.TPSL, level=NotificationLevel.WARNING,
        title="止损", message="sh600000 -6%"
    )
    md = n.format_markdown()
    assert "止损" in md
    assert "sh600000" in md


def test_config_builder():
    cfg = {
        "console": {"enabled": True, "use_color": False},
        "file": {"enabled": True, "log_dir": tempfile.mkdtemp()},
        "telegram": {"enabled": False},
        "dingtalk": {"enabled": False},
    }
    mgr = build_manager_from_config(cfg)
    assert len(mgr._notifiers) == 2
    names = {n.name for n in mgr._notifiers}
    assert "console" in names
    assert "file" in names


@pytest.mark.asyncio
async def test_min_level_filter():
    mgr = NotificationManager()
    c = ConsoleNotifier(use_color=False)
    mgr.add_notifier(c)
    mgr.set_min_level(NotificationLevel.ERROR)
    await mgr.notify_sync(Notification(
        type=NotificationType.SYSTEM, level=NotificationLevel.INFO,
        title="info", message="should be filtered"
    ))
    assert c.sent_count == 0
    await mgr.notify_sync(Notification(
        type=NotificationType.SYSTEM, level=NotificationLevel.ERROR,
        title="error", message="should pass"
    ))
    assert c.sent_count == 1
