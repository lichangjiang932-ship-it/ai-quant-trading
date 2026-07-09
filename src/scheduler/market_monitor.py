"""
市场监控器
实时监控市场状态和异常情况
"""
import time
import threading
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Callable
from enum import Enum
import json


class MarketAlert(Enum):
    """市场告警类型"""
    LIMIT_UP = "limit_up"           # 涨停
    LIMIT_DOWN = "limit_down"       # 跌停
    HIGH_VOLATILITY = "high_vol"    # 高波动
    LARGE_DROP = "large_drop"       # 大幅下跌
    LARGE_RISE = "large_rise"       # 大幅上涨
    VOLUME_SURGE = "volume_surge"   # 成交量激增


class MarketMonitor:
    """市场监控器类"""
    
    def __init__(self):
        """初始化市场监控器"""
        self.running = False
        self._thread = None
        self.watch_list = []
        self.alert_callbacks = {}
        self.market_data = {}
        self.alert_history = []
        
        # 告警阈值
        self.thresholds = {
            'limit_up_pct': 9.8,        # 涨停阈值（接近涨停）
            'limit_down_pct': -9.8,     # 跌停阈值
            'large_change_pct': 5,      # 大幅波动阈值
            'volume_surge_ratio': 3,    # 成交量激增倍数
            'volatility_threshold': 3   # 波动率阈值
        }
    
    def set_threshold(self, key: str, value: float):
        """
        设置告警阈值
        
        Args:
            key: 阈值键名
            value: 阈值
        """
        self.thresholds[key] = value
    
    def add_watch(self, symbol: str, name: str = ""):
        """
        添加监控标的
        
        Args:
            symbol: 股票代码
            name: 股票名称
        """
        self.watch_list.append({
            'symbol': symbol,
            'name': name,
            'added_at': datetime.now()
        })
    
    def remove_watch(self, symbol: str):
        """
        移除监控标的
        
        Args:
            symbol: 股票代码
        """
        self.watch_list = [w for w in self.watch_list if w['symbol'] != symbol]
    
    def register_alert_callback(self, alert_type: MarketAlert, callback: Callable):
        """
        注册告警回调
        
        Args:
            alert_type: 告警类型
            callback: 回调函数
        """
        if alert_type not in self.alert_callbacks:
            self.alert_callbacks[alert_type] = []
        self.alert_callbacks[alert_type].append(callback)
    
    def start(self):
        """启动监控"""
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print("市场监控已启动")
    
    def stop(self):
        """停止监控"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("市场监控已停止")
    
    def _monitor_loop(self):
        """监控主循环"""
        while self.running:
            try:
                if self.watch_list:
                    self._check_market_data()
                time.sleep(3)  # 每3秒检查一次
            except Exception as e:
                print(f"监控出错: {e}")
                time.sleep(5)
    
    def _check_market_data(self):
        """检查市场数据"""
        from ..data.realtime.realtime_data import RealtimeData
        
        realtime = RealtimeData()
        
        # 获取监控标的的实时行情
        symbols = [w['symbol'] for w in self.watch_list]
        quotes = realtime.get_realtime_quote_eastmoney(symbols)
        
        for symbol, quote in quotes.items():
            # 更新市场数据
            self.market_data[symbol] = {
                'quote': quote,
                'updated_at': datetime.now()
            }
            
            # 检查告警条件
            self._check_alerts(symbol, quote)
    
    def _check_alerts(self, symbol: str, quote: Dict):
        """检查告警条件"""
        change_pct = quote.get('change_pct', 0)
        volume = quote.get('volume', 0)
        
        # 检查涨跌停
        if change_pct >= self.thresholds['limit_up_pct']:
            self._trigger_alert(MarketAlert.LIMIT_UP, symbol, quote, f"接近涨停 ({change_pct:.2f}%)")
        
        if change_pct <= self.thresholds['limit_down_pct']:
            self._trigger_alert(MarketAlert.LIMIT_DOWN, symbol, quote, f"接近跌停 ({change_pct:.2f}%)")
        
        # 检查大幅波动
        if abs(change_pct) >= self.thresholds['large_change_pct']:
            alert_type = MarketAlert.LARGE_RISE if change_pct > 0 else MarketAlert.LARGE_DROP
            self._trigger_alert(alert_type, symbol, quote, f"大幅波动 ({change_pct:.2f}%)")
        
        # 检查成交量激增（需要历史数据对比，这里简化处理）
        # 实际应用中需要保存历史成交量数据进行对比
    
    def _trigger_alert(self, alert_type: MarketAlert, symbol: str, quote: Dict, message: str):
        """触发告警"""
        alert = {
            'type': alert_type.value,
            'symbol': symbol,
            'name': quote.get('name', ''),
            'price': quote.get('price', 0),
            'change_pct': quote.get('change_pct', 0),
            'message': message,
            'time': datetime.now()
        }
        
        # 记录告警历史
        self.alert_history.append(alert)
        
        # 打印告警
        print(f"[告警] {alert['name']}({symbol}): {message}")
        
        # 调用回调函数
        if alert_type in self.alert_callbacks:
            for callback in self.alert_callbacks[alert_type]:
                try:
                    callback(alert)
                except Exception as e:
                    print(f"告警回调出错: {e}")
    
    def get_alert_history(self, limit: int = 100) -> List[Dict]:
        """
        获取告警历史
        
        Args:
            limit: 返回数量限制
        
        Returns:
            List[Dict]: 告警历史
        """
        return self.alert_history[-limit:]
    
    def get_market_overview(self) -> Dict:
        """
        获取市场概览
        
        Returns:
            Dict: 市场概览
        """
        from ..data.realtime.realtime_data import RealtimeData
        
        realtime = RealtimeData()
        return realtime.get_market_overview()
    
    def get_stock_status(self, symbol: str) -> Dict:
        """
        获取股票状态
        
        Args:
            symbol: 股票代码
        
        Returns:
            Dict: 股票状态
        """
        if symbol in self.market_data:
            data = self.market_data[symbol]
            quote = data['quote']
            
            return {
                'symbol': symbol,
                'name': quote.get('name', ''),
                'price': quote.get('price', 0),
                'change_pct': quote.get('change_pct', 0),
                'volume': quote.get('volume', 0),
                'amount': quote.get('amount', 0),
                'is_limit_up': quote.get('change_pct', 0) >= 9.8,
                'is_limit_down': quote.get('change_pct', 0) <= -9.8,
                'updated_at': data['updated_at'].isoformat()
            }
        
        return {}