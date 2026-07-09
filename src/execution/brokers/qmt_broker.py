"""
QMT券商API对接
QMT（迅投）是国内常用的量化交易平台
"""
from typing import Dict, List, Optional
from datetime import datetime
import pandas as pd
from .base_broker import BaseBroker, Order, Position, OrderDirection, OrderType, OrderStatus


class QMTBroker(BaseBroker):
    """QMT券商API类"""
    
    def __init__(
        self,
        account_id: str = "",
        account_type: str = "STOCK",
        mini_qmt_path: str = ""
    ):
        """
        初始化QMT券商API
        
        Args:
            account_id: 资金账号
            account_type: 账户类型（STOCK-股票, CREDIT-信用）
            mini_qmt_path: miniQMT路径
        """
        super().__init__("QMTBroker")
        self.account_id = account_id
        self.account_type = account_type
        self.mini_qmt_path = mini_qmt_path
        self.xt_trader = None
        self.connected = False
    
    def connect(self, **kwargs) -> bool:
        """
        连接QMT
        
        Returns:
            bool: 是否连接成功
        """
        try:
            # 导入xtquant（QMT的Python API）
            from xtquant import xttrader, xtdata
            from xtquant.xttype import StockAccount
            
            # 创建交易对象
            session_id = int(datetime.now().strftime('%H%M%S'))
            self.xt_trader = xttrader.XtQuantTrader(self.mini_qmt_path, session_id)
            self.xt_trader.start()
            
            # 连接
            connect_result = self.xt_trader.connect()
            
            if connect_result == 0:
                # 创建账户对象
                self.account = StockAccount(self.account_id, self.account_type)
                
                # 订阅账户信息
                self.xt_trader.subscribe(self.account)
                
                self.connected = True
                print(f"QMT连接成功，账号: {self.account_id}")
                return True
            else:
                print(f"QMT连接失败，错误码: {connect_result}")
                return False
                
        except ImportError:
            print("请安装xtquant库：pip install xtquant")
            return False
        except Exception as e:
            print(f"QMT连接出错: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.xt_trader:
            self.xt_trader.stop()
        self.connected = False
    
    def get_account_info(self) -> Dict:
        """获取账户信息"""
        if not self.connected or not self.xt_trader:
            return {}
        
        try:
            # 获取资产信息
            asset = self.xt_trader.query_stock_asset(self.account)
            
            return {
                'total_asset': asset.total_asset,
                'cash': asset.cash,
                'market_value': asset.market_value,
                'frozen_cash': asset.frozen_cash,
                'position_pnl': asset.position_pnl,
                'close_pnl': asset.close_pnl
            }
        except Exception as e:
            print(f"获取账户信息失败: {e}")
            return {}
    
    def get_positions(self) -> List[Position]:
        """获取持仓"""
        if not self.connected or not self.xt_trader:
            return []
        
        try:
            # 获取持仓
            positions = self.xt_trader.query_stock_positions(self.account)
            
            result = []
            for pos in positions:
                if pos.volume > 0:
                    result.append(Position(
                        symbol=pos.stock_code,
                        quantity=pos.volume,
                        avg_cost=pos.open_price,
                        market_value=pos.market_value,
                        unrealized_pnl=pos.stock_profit
                    ))
            
            return result
        except Exception as e:
            print(f"获取持仓失败: {e}")
            return []
    
    def place_order(self, order: Order) -> str:
        """
        下单
        
        Args:
            order: 订单对象
        
        Returns:
            str: 订单ID
        """
        if not self.connected or not self.xt_trader:
            return ""
        
        try:
            # 转换订单方向
            from xtquant.xtconstant import STOCK_BUY, STOCK_SELL
            
            if order.direction == OrderDirection.BUY:
                direction = STOCK_BUY
            else:
                direction = STOCK_SELL
            
            # 转换订单类型
            from xtquant.xtconstant import FIX_PRICE, LATEST_PRICE
            
            if order.order_type == OrderType.LIMIT:
                price_type = FIX_PRICE
                price = order.price
            else:
                price_type = LATEST_PRICE
                price = 0
            
            # 下单
            order_id = self.xt_trader.order_stock(
                self.account,
                order.symbol,
                direction,
                order.quantity,
                price_type,
                price
            )
            
            order.order_id = str(order_id)
            order.status = OrderStatus.SUBMITTED
            order.created_at = datetime.now()
            
            return order.order_id
            
        except Exception as e:
            print(f"下单失败: {e}")
            order.status = OrderStatus.REJECTED
            return ""
    
    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        if not self.connected or not self.xt_trader:
            return False
        
        try:
            result = self.xt_trader.cancel_order_stock(self.account, int(order_id))
            return result == 0
        except Exception as e:
            print(f"撤单失败: {e}")
            return False
    
    def get_order_status(self, order_id: str) -> Dict:
        """获取订单状态"""
        if not self.connected or not self.xt_trader:
            return {}
        
        try:
            # 查询当日委托
            orders = self.xt_trader.query_stock_orders(self.account)
            
            for order in orders:
                if str(order.order_id) == order_id:
                    return {
                        'order_id': order.order_id,
                        'symbol': order.stock_code,
                        'direction': 'buy' if order.order_type == 23 else 'sell',
                        'quantity': order.order_volume,
                        'filled_quantity': order.traded_volume,
                        'price': order.price,
                        'status': self._convert_order_status(order.order_status),
                        'created_at': datetime.fromtimestamp(order.order_time)
                    }
            
            return {}
        except Exception as e:
            print(f"获取订单状态失败: {e}")
            return {}
    
    def _convert_order_status(self, xt_status: int) -> str:
        """转换QMT订单状态"""
        status_map = {
            0: 'pending',
            1: 'submitted',
            2: 'partial_filled',
            3: 'filled',
            4: 'cancelled',
            5: 'rejected'
        }
        return status_map.get(xt_status, 'unknown')
    
    def get_order_history(self) -> pd.DataFrame:
        """获取订单历史"""
        if not self.connected or not self.xt_trader:
            return pd.DataFrame()
        
        try:
            orders = self.xt_trader.query_stock_orders(self.account)
            
            if not orders:
                return pd.DataFrame()
            
            data = []
            for order in orders:
                data.append({
                    'order_id': order.order_id,
                    'symbol': order.stock_code,
                    'direction': 'buy' if order.order_type == 23 else 'sell',
                    'quantity': order.order_volume,
                    'filled_quantity': order.traded_volume,
                    'price': order.price,
                    'status': self._convert_order_status(order.order_status),
                    'created_at': datetime.fromtimestamp(order.order_time)
                })
            
            return pd.DataFrame(data)
        except Exception as e:
            print(f"获取订单历史失败: {e}")
            return pd.DataFrame()
    
    def subscribe_quote(self, symbols: List[str], callback=None):
        """
        订阅实时行情
        
        Args:
            symbols: 股票代码列表
            callback: 行情回调函数
        """
        if not self.connected or not self.xt_trader:
            return
        
        try:
            from xtquant import xtdata
            
            for symbol in symbols:
                xtdata.subscribe_quote(symbol, period='tick', callback=callback)
        except Exception as e:
            print(f"订阅行情失败: {e}")
    
    def get_stock_info(self, symbol: str) -> Dict:
        """
        获取股票基本信息
        
        Args:
            symbol: 股票代码
        
        Returns:
            Dict: 股票信息
        """
        if not self.connected or not self.xt_trader:
            return {}
        
        try:
            from xtquant import xtdata
            
            # 获取股票详情
            detail = xtdata.get_instrument_detail(symbol)
            
            return {
                'code': detail.get('InstrumentID', ''),
                'name': detail.get('InstrumentName', ''),
                'exchange': detail.get('ExchangeID', ''),
                'lot_size': detail.get('VolumeMultiple', 100),
                'price_tick': detail.get('PriceTick', 0.01),
                'list_date': detail.get('OpenDate', ''),
                'expire_date': detail.get('ExpireDate', '')
            }
        except Exception as e:
            print(f"获取股票信息失败: {e}")
            return {}