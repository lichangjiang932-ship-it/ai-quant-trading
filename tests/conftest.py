"""pytest 会话级 fixture — 测试结束后释放全局连接, 避免 mootdx 心跳线程残留导致进程无法退出。"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def _cleanup_global_connections():
    """session 级自动清理: 测试全部结束后释放 realtime/mootdx/pytdx 连接。"""
    yield
    try:
        import gc
        # 找出所有存活的 RealtimeData 实例并关闭
        from src.data.realtime.realtime_data import RealtimeData
        for obj in gc.get_objects():
            if isinstance(obj, RealtimeData):
                try:
                    obj.close()
                except Exception:
                    pass
    except Exception:
        pass
    try:
        # 兜底: 直接停止 mootdx/pytdx 心跳线程
        import threading
        for t in threading.enumerate():
            name = type(t).__name__
            if "HeartBeat" in name or "Hq" in name:
                stop_event = getattr(t, "stop_event", None)
                if stop_event is not None:
                    try:
                        stop_event.set()
                    except Exception:
                        pass
    except Exception:
        pass
