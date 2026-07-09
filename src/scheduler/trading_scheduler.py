"""
交易调度器
负责管理交易时间、定时任务和策略执行
"""
import time
import threading
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Callable
from enum import Enum
import schedule


class TradingSession(Enum):
    """交易时段"""
    PRE_MARKET = "pre_market"      # 盘前
    MORNING = "morning"            # 上午
    LUNCH_BREAK = "lunch_break"    # 午休
    AFTERNOON = "afternoon"        # 下午
    AFTER_HOURS = "after_hours"    # 盘后
    CLOSED = "closed"              # 休市


class TradingScheduler:
    """交易调度器类"""
    
    def __init__(self):
        """初始化交易调度器"""
        # A股交易时间
        self.trading_times = {
            'pre_market_start': dtime(9, 15),
            'morning_start': dtime(9, 30),
            'morning_end': dtime(11, 30),
            'afternoon_start': dtime(13, 0),
            'afternoon_end': dtime(15, 0),
            'after_hours_end': dtime(15, 30)
        }
        
        self.current_session = TradingSession.CLOSED
        self.running = False
        self._thread = None
        self.callbacks = {}
        self.strategy_callbacks = {}
    
    def get_current_session(self) -> TradingSession:
        """获取当前交易时段"""
        now = datetime.now().time()
        
        # 检查是否为交易日（周一到周五）
        if datetime.now().weekday() >= 5:
            return TradingSession.CLOSED
        
        # 判断时段
        if now < self.trading_times['pre_market_start']:
            return TradingSession.CLOSED
        elif now < self.trading_times['morning_start']:
            return TradingSession.PRE_MARKET
        elif now < self.trading_times['morning_end']:
            return TradingSession.MORNING
        elif now < self.trading_times['afternoon_start']:
            return TradingSession.LUNCH_BREAK
        elif now < self.trading_times['afternoon_end']:
            return TradingSession.AFTERNOON
        elif now < self.trading_times['after_hours_end']:
            return TradingSession.AFTER_HOURS
        else:
            return TradingSession.CLOSED
    
    def is_trading_time(self) -> bool:
        """检查是否为交易时间"""
        session = self.get_current_session()
        return session in [TradingSession.MORNING, TradingSession.AFTERNOON]
    
    def is_pre_market(self) -> bool:
        """检查是否为盘前"""
        return self.get_current_session() == TradingSession.PRE_MARKET
    
    def is_after_hours(self) -> bool:
        """检查是否为盘后"""
        return self.get_current_session() == TradingSession.AFTER_HOURS
    
    def register_callback(self, session: TradingSession, callback: Callable):
        """
        注册交易时段回调
        
        Args:
            session: 交易时段
            callback: 回调函数
        """
        if session not in self.callbacks:
            self.callbacks[session] = []
        self.callbacks[session].append(callback)
    
    def register_strategy(
        self,
        name: str,
        strategy_func: Callable,
        execution_times: List[TradingSession] = None
    ):
        """
        注册策略
        
        Args:
            name: 策略名称
            strategy_func: 策略函数
            execution_times: 执行时段列表
        """
        if execution_times is None:
            execution_times = [TradingSession.MORNING, TradingSession.AFTERNOON]
        
        self.strategy_callbacks[name] = {
            'func': strategy_func,
            'execution_times': execution_times,
            'last_execution': None
        }
    
    def start(self):
        """启动调度器"""
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print("交易调度器已启动")
    
    def stop(self):
        """停止调度器"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("交易调度器已停止")
    
    def _run_loop(self):
        """调度器主循环"""
        last_session = None
        
        while self.running:
            try:
                current_session = self.get_current_session()
                
                # 检测时段切换
                if current_session != last_session:
                    self._on_session_change(last_session, current_session)
                    last_session = current_session
                
                # 执行策略
                self._execute_strategies(current_session)
                
                # 等待1秒
                time.sleep(1)
                
            except Exception as e:
                print(f"调度器出错: {e}")
                time.sleep(5)
    
    def _on_session_change(self, old_session: TradingSession, new_session: TradingSession):
        """处理时段切换"""
        print(f"交易时段切换: {old_session.value} -> {new_session.value}")
        
        # 触发新时段的回调
        if new_session in self.callbacks:
            for callback in self.callbacks[new_session]:
                try:
                    callback()
                except Exception as e:
                    print(f"时段回调出错: {e}")
    
    def _execute_strategies(self, current_session: TradingSession):
        """执行策略"""
        for name, strategy_info in self.strategy_callbacks.items():
            # 检查是否在执行时段
            if current_session in strategy_info['execution_times']:
                # 检查是否需要执行（避免重复执行）
                now = datetime.now()
                last_exec = strategy_info['last_execution']
                
                # 如果是首次执行或距离上次执行超过1分钟
                if last_exec is None or (now - last_exec).seconds >= 60:
                    try:
                        strategy_info['func']()
                        strategy_info['last_execution'] = now
                    except Exception as e:
                        print(f"策略{name}执行出错: {e}")
    
    def get_status(self) -> Dict:
        """获取调度器状态"""
        return {
            'running': self.running,
            'current_session': self.get_current_session().value,
            'is_trading_time': self.is_trading_time(),
            'registered_strategies': list(self.strategy_callbacks.keys())
        }
    
    def wait_for_trading_time(self, timeout: int = 300):
        """
        等待交易时间
        
        Args:
            timeout: 超时时间（秒）
        
        Returns:
            bool: 是否等到交易时间
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if self.is_trading_time():
                return True
            time.sleep(1)
        
        return False