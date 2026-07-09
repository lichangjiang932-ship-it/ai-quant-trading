"""
A股实时数据获取模块
支持多种数据源：新浪、腾讯、东方财富
"""
import requests
import json
import time
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from threading import Thread, Event
import pandas as pd


class RealtimeData:
    """A股实时数据获取类"""
    
    def __init__(self):
        """初始化实时数据获取"""
        self.callbacks = []
        self.running = False
        self._thread = None
        self._stop_event = Event()
        self._pytdx_client = None  # 惰性初始化(仅当用到 pytdx 源时)
        # 境内行情源(新浪/腾讯/东财)直连,忽略系统代理/VPN,避免代理不通导致请求失败
        self._session = requests.Session()
        self._session.trust_env = False
    
    def get_realtime_quote_sina(self, symbols: List[str]) -> Dict:
        """
        通过新浪财经API获取实时行情
        
        Args:
            symbols: 股票代码列表（如 ['sh600000', 'sz000001']）
        
        Returns:
            Dict: 实时行情数据
        """
        results = {}
        
        for symbol in symbols:
            try:
                url = f"http://hq.sinajs.cn/list={symbol}"
                headers = {
                    'Referer': 'http://finance.sina.com.cn',
                    'User-Agent': 'Mozilla/5.0'
                }
                
                response = self._session.get(url, headers=headers, timeout=5)
                response.encoding = 'gbk'
                
                # 解析数据
                data = response.text
                if '=""' not in data:
                    # 提取数据
                    match = re.search(r'"(.+)"', data)
                    if match:
                        fields = match.group(1).split(',')
                        if len(fields) >= 32:
                            results[symbol] = {
                                'name': fields[0],
                                'open': float(fields[1]),
                                'pre_close': float(fields[2]),
                                'price': float(fields[3]),
                                'high': float(fields[4]),
                                'low': float(fields[5]),
                                'volume': int(float(fields[8])),
                                'amount': float(fields[9]),
                                'time': fields[30],
                                'date': fields[31],
                                'change': float(fields[3]) - float(fields[2]),
                                'change_pct': (float(fields[3]) - float(fields[2])) / float(fields[2]) * 100
                            }
            except Exception as e:
                print(f"获取{symbol}行情失败: {e}")
        
        return results
    
    def get_realtime_quote_tencent(self, symbols: List[str]) -> Dict:
        """
        通过腾讯财经API获取实时行情(增强版, 包含PE/PB/市值等全字段)

        Args:
            symbols: 股票代码列表（如 ['sh600000', 'sz000001']）

        Returns:
            Dict: 实时行情数据, 字段:
                  name, price, pre_close, open, high, low,
                  volume, amount, change, change_pct, time,
                  pe_ttm, pb, mcap_yi(总市值亿), float_mcap_yi(流通市值亿),
                  turnover_pct(换手率%), limit_up(涨停价), limit_down(跌停价),
                  vol_ratio(量比), pe_static(PE静), amplitude_pct(振幅%)
        """
        results = {}

        for symbol in symbols:
            try:
                # 转换代码格式 (腾讯用 sh600000 格式)
                code = symbol[2:] if symbol.startswith(('sh', 'sz', 'bj')) else symbol
                market = 'sh' if (symbol.startswith('sh') or code.startswith(('6', '9'))) else 'sz'
                tc_symbol = f"{market}{code}"

                url = f"http://qt.gtimg.cn/q={tc_symbol}"
                headers = {
                    'Referer': 'http://finance.qq.com',
                    'User-Agent': 'Mozilla/5.0'
                }

                response = self._session.get(url, headers=headers, timeout=5)
                response.encoding = 'gbk'

                # 解析数据(腾讯88字段, ~分隔)
                data = response.text
                match = re.search(r'"(.+)"', data)
                if match:
                    fields = match.group(1).split('~')
                    if len(fields) >= 53:
                        def _f(idx):
                            try:
                                return float(fields[idx]) if fields[idx] else 0.0
                            except (ValueError, TypeError):
                                return 0.0

                        results[symbol] = {
                            # 基础行情(与旧版兼容)
                            'name': fields[1],
                            'code': fields[2],
                            'price': _f(3),
                            'pre_close': _f(4),
                            'open': _f(5),
                            'volume': int(_f(6) * 100),
                            'amount': _f(37) * 10000,
                            'high': _f(33),
                            'low': _f(34),
                            'change': _f(31),
                            'change_pct': _f(32),
                            'time': fields[30],
                            # 估值/基本面(增强字段)
                            'pe_ttm': _f(39),
                            'pb': _f(46),
                            'mcap_yi': _f(44),
                            'float_mcap_yi': _f(45),
                            'turnover_pct': _f(38),
                            'limit_up': _f(47),
                            'limit_down': _f(48),
                            'vol_ratio': _f(49),
                            'pe_static': _f(52),
                            'amplitude_pct': _f(43),
                        }
            except Exception as e:
                print(f"获取{symbol}行情失败: {e}")

        return results

    def get_realtime_quote_tencent_rich(self, symbols: List[str]) -> Dict:
        """腾讯行情增强版(与 get_realtime_quote_tencent 相同, 显式语义别名)"""
        return self.get_realtime_quote_tencent(symbols)
    
    def get_realtime_quote_eastmoney(self, symbols: List[str]) -> Dict:
        """
        通过东方财富API获取实时行情
        
        Args:
            symbols: 股票代码列表（如 ['sh600000', 'sz000001']）
        
        Returns:
            Dict: 实时行情数据
        """
        results = {}
        
        # 构建secids参数
        secids = []
        for symbol in symbols:
            code = symbol[2:]
            market = '1' if symbol.startswith('sh') else '0'
            secids.append(f"{market}.{code}")
        
        try:
            url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
            params = {
                'fltt': '2',
                'fields': 'f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18',
                'secids': ','.join(secids)
            }

            from ..em_client import em_get
            response = em_get(url, params=params, timeout=5)
            data = response.json()
            
            if data and data.get('data') and data['data'].get('diff'):
                for item in data['data']['diff']:
                    symbol_code = item.get('f12', '')
                    market = 'sh' if item.get('f13', 0) == 1 else 'sz'
                    symbol = f"{market}{symbol_code}"
                    
                    results[symbol] = {
                        'name': item.get('f14', ''),
                        'price': item.get('f2', 0),
                        'change_pct': item.get('f3', 0),
                        'change': item.get('f4', 0),
                        'volume': item.get('f5', 0),
                        'amount': item.get('f6', 0),
                        'high': item.get('f15', 0),
                        'low': item.get('f16', 0),
                        'open': item.get('f17', 0),
                        'pre_close': item.get('f18', 0),
                        'turnover': item.get('f8', 0),
                        'pe_ratio': item.get('f9', 0)
                    }
        except Exception as e:
            print(f"获取行情失败: {e}")
        
        return results

    def get_realtime_quote_pytdx(self, symbols: List[str]) -> Dict:
        """
        通过 pytdx(通达信 TCP 协议)获取实时行情。比 HTTP 抓取更稳、更少限流。
        未安装 pytdx 或连接失败时返回 {}，由 get_quotes 自动回退到其它源。
        """
        try:
            if self._pytdx_client is None:
                from .pytdx_client import PytdxQuoteClient
                self._pytdx_client = PytdxQuoteClient()
            if not self._pytdx_client.is_available():
                return {}
            return self._pytdx_client.get_quotes(symbols)
        except Exception:
            return {}

    def get_quotes(self, symbols: List[str], sources: Optional[List[str]] = None) -> Dict:
        """
        统一实时行情入口：按 sources 顺序逐源尝试，第一个返回非空即用。

        Args:
            symbols: 股票代码列表
            sources: 源优先级，默认 ['pytdx', 'eastmoney', 'sina', 'tencent']

        Returns:
            Dict: 实时行情（字段结构与各 get_realtime_quote_* 一致）
        """
        if not sources:
            sources = ['pytdx', 'eastmoney', 'sina', 'tencent']

        dispatch = {
            'pytdx': self.get_realtime_quote_pytdx,
            'eastmoney': self.get_realtime_quote_eastmoney,
            'sina': self.get_realtime_quote_sina,
            'tencent': self.get_realtime_quote_tencent,
        }

        for src in sources:
            fn = dispatch.get(str(src).lower())
            if fn is None:
                continue
            try:
                quotes = fn(symbols)
                if quotes:
                    return quotes
            except Exception:
                continue
        return {}

    def get_stock_quote(self, symbol: str, source: str = 'sina') -> Dict:
        """
        获取单只股票实时行情
        
        Args:
            symbol: 股票代码（如 'sh600000' 或 '600000'）
            source: 数据源（sina, tencent, eastmoney）
        
        Returns:
            Dict: 实时行情数据
        """
        # 标准化代码格式
        if not symbol.startswith(('sh', 'sz')):
            if symbol.startswith('6'):
                symbol = f'sh{symbol}'
            else:
                symbol = f'sz{symbol}'
        
        if source == 'sina':
            return self.get_realtime_quote_sina([symbol]).get(symbol, {})
        elif source == 'tencent':
            return self.get_realtime_quote_tencent([symbol]).get(symbol, {})
        elif source == 'eastmoney':
            return self.get_realtime_quote_eastmoney([symbol]).get(symbol, {})
        else:
            return self.get_realtime_quote_sina([symbol]).get(symbol, {})
    
    def get_kline_mootdx(self, symbol: str, category: int = 4, offset: int = 100) -> pd.DataFrame:
        """
        通过 mootdx(TCP通达信) 获取K线数据。不封IP, 比HTTP更稳定。

        Args:
            symbol: 股票代码(如 'sh600000' 或 '600000')
            category: 4=日线 5=周线 6=月线 7=1分钟 8=5分钟 9=15分钟 10=30分钟 11=60分钟
            offset: 数据条数

        Returns:
            DataFrame: K线数据 (open, close, high, low, volume, amount)
        """
        try:
            from ..sources.quote import MootdxSource
            m = MootdxSource()
            return m.kline(symbol, category, offset)
        except Exception:
            return pd.DataFrame()

    def get_market_overview(self) -> Dict:
        """
        获取市场概览（上证、深证、创业板指数）
        
        Returns:
            Dict: 市场指数数据
        """
        symbols = ['sh000001', 'sz399001', 'sz399006']
        quotes = self.get_realtime_quote_sina(symbols)
        
        return {
            '上证指数': quotes.get('sh000001', {}),
            '深证成指': quotes.get('sz399001', {}),
            '创业板指': quotes.get('sz399006', {})
        }
    
    def get_kline_data(
        self,
        symbol: str,
        period: str = 'day',
        count: int = 100
    ) -> pd.DataFrame:
        """
        获取K线数据
        
        Args:
            symbol: 股票代码
            period: 周期（day, week, month, 5min, 15min, 30min, 60min）
            count: 数据条数
        
        Returns:
            DataFrame: K线数据
        """
        try:
            # 标准化代码格式
            if not symbol.startswith(('sh', 'sz')):
                if symbol.startswith('6'):
                    symbol = f'sh{symbol}'
                else:
                    symbol = f'sz{symbol}'
            
            code = symbol[2:]
            market = '1' if symbol.startswith('sh') else '0'
            
            # 东方财富K线API
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                'secid': f"{market}.{code}",
                'fields1': 'f1,f2,f3,f4,f5,f6',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                'klt': self._get_klt(period),
                'fqt': '1',
                'end': '20500101',
                'lmt': count
            }

            from ..em_client import em_get
            response = em_get(url, params=params, timeout=5)
            data = response.json()
            
            if data and data.get('data') and data['data'].get('klines'):
                klines = data['data']['klines']
                
                df = pd.DataFrame([
                    {
                        'datetime': k.split(',')[0],
                        'open': float(k.split(',')[1]),
                        'close': float(k.split(',')[2]),
                        'high': float(k.split(',')[3]),
                        'low': float(k.split(',')[4]),
                        'volume': int(float(k.split(',')[5])),
                        'amount': float(k.split(',')[6]),
                        'turnover': float(k.split(',')[7])
                    }
                    for k in klines
                ])
                
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)
                
                return df
            
            return pd.DataFrame()
            
        except Exception as e:
            print(f"获取K线数据失败: {e}")
            return pd.DataFrame()
    
    def _get_klt(self, period: str) -> str:
        """获取K线周期参数"""
        period_map = {
            'day': '101',
            'week': '102',
            'month': '103',
            '5min': '5',
            '15min': '15',
            '30min': '30',
            '60min': '60'
        }
        return period_map.get(period, '101')
    
    def start_realtime_monitor(
        self,
        symbols: List[str],
        callback: Callable,
        interval: float = 3.0,
        source: str = 'sina'
    ):
        """
        启动实时行情监控
        
        Args:
            symbols: 股票代码列表
            callback: 回调函数
            interval: 刷新间隔（秒）
            source: 数据源
        """
        self.callbacks.append(callback)
        
        if not self.running:
            self.running = True
            self._stop_event.clear()
            self._thread = Thread(
                target=self._monitor_loop,
                args=(symbols, interval, source),
                daemon=True
            )
            self._thread.start()
    
    def _monitor_loop(self, symbols: List[str], interval: float, source: str):
        """监控循环"""
        while self.running and not self._stop_event.is_set():
            try:
                # 获取实时行情
                if source == 'sina':
                    quotes = self.get_realtime_quote_sina(symbols)
                elif source == 'tencent':
                    quotes = self.get_realtime_quote_tencent(symbols)
                else:
                    quotes = self.get_realtime_quote_eastmoney(symbols)
                
                # 调用回调函数
                for callback in self.callbacks:
                    callback(quotes)
                
            except Exception as e:
                print(f"监控出错: {e}")
            
            # 等待间隔
            self._stop_event.wait(interval)
    
    def stop_realtime_monitor(self):
        """停止实时行情监控"""
        self.running = False
        self._stop_event.set()
        self.callbacks = []