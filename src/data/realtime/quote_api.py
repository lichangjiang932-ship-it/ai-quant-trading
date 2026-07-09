"""
行情API封装
"""
import requests
import json
from typing import Dict, List, Optional
from datetime import datetime


class QuoteAPI:
    """行情API封装类"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化行情API
        
        Args:
            api_key: API密钥（如果需要）
        """
        self.api_key = api_key
        self.base_urls = {
            'sina': 'http://hq.sinajs.cn',
            'tencent': 'http://qt.gtimg.cn',
            'eastmoney': 'https://push2.eastmoney.com/api/qt'
        }
    
    def get_quote(self, symbol: str, source: str = 'eastmoney') -> Dict:
        """
        获取单只股票行情
        
        Args:
            symbol: 股票代码
            source: 数据源
        
        Returns:
            Dict: 行情数据
        """
        from .realtime_data import RealtimeData
        realtime = RealtimeData()
        return realtime.get_stock_quote(symbol, source)
    
    def get_batch_quotes(self, symbols: List[str], source: str = 'eastmoney') -> Dict:
        """
        批量获取股票行情
        
        Args:
            symbols: 股票代码列表
            source: 数据源
        
        Returns:
            Dict: 行情数据
        """
        from .realtime_data import RealtimeData
        realtime = RealtimeData()
        
        # 标准化代码格式
        normalized = []
        for s in symbols:
            if not s.startswith(('sh', 'sz')):
                if s.startswith('6'):
                    normalized.append(f'sh{s}')
                else:
                    normalized.append(f'sz{s}')
            else:
                normalized.append(s)
        
        if source == 'sina':
            return realtime.get_realtime_quote_sina(normalized)
        elif source == 'tencent':
            return realtime.get_realtime_quote_tencent(normalized)
        else:
            return realtime.get_realtime_quote_eastmoney(normalized)
    
    def get_index_quotes(self) -> Dict:
        """
        获取主要指数行情
        
        Returns:
            Dict: 指数行情数据
        """
        from .realtime_data import RealtimeData
        realtime = RealtimeData()
        return realtime.get_market_overview()
    
    def get_stock_info(self, symbol: str) -> Dict:
        """
        获取股票基本信息
        
        Args:
            symbol: 股票代码
        
        Returns:
            Dict: 股票信息
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
            
            url = "https://push2.eastmoney.com/api/qt/stock/get"
            params = {
                'secid': f"{market}.{code}",
                'fields': 'f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f55,f57,f58,f60,f71,f92,f105,f116,f117,f162,f167,f168,f169,f170,f171,f177,f193'
            }
            
            response = requests.get(url, timeout=5)
            data = response.json()
            
            if data and data.get('data'):
                item = data['data']
                return {
                    'code': item.get('f57', ''),
                    'name': item.get('f58', ''),
                    'price': item.get('f43', 0) / 100 if item.get('f43') else 0,
                    'change_pct': item.get('f170', 0) / 100 if item.get('f170') else 0,
                    'volume': item.get('f47', 0),
                    'amount': item.get('f48', 0),
                    'high': item.get('f44', 0) / 100 if item.get('f44') else 0,
                    'low': item.get('f45', 0) / 100 if item.get('f45') else 0,
                    'open': item.get('f46', 0) / 100 if item.get('f46') else 0,
                    'pre_close': item.get('f60', 0) / 100 if item.get('f60') else 0,
                    'market_cap': item.get('f116', 0),
                    'circulating_cap': item.get('f117', 0),
                    'pe_ratio': item.get('f162', 0) / 100 if item.get('f162') else 0,
                    'pb_ratio': item.get('f167', 0) / 100 if item.get('f167') else 0,
                    'turnover': item.get('f168', 0) / 100 if item.get('f168') else 0
                }
            
            return {}
            
        except Exception as e:
            print(f"获取股票信息失败: {e}")
            return {}