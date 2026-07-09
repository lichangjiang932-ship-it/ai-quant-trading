import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import time


class MarketData:
    def __init__(self):
        self.cache = {}
        self.cache_timeout = 60
        self.sources = {
            'mootdx': self._fetch_mootdx_kline,
            'akshare': self._fetch_akshare,
            'baostock': self._fetch_baostock,
            'yfinance': self._fetch_yfinance,
            'eastmoney': self._fetch_eastmoney_kline,
        }

    def _check_cache(self, cache_key: str, timeout: int = None) -> Optional[pd.DataFrame]:
        if timeout is None:
            timeout = self.cache_timeout
        if cache_key in self.cache:
            data, ts = self.cache[cache_key]
            if (datetime.now() - ts).seconds < timeout:
                return data
        return None

    def _set_cache(self, cache_key: str, data: pd.DataFrame):
        self.cache[cache_key] = (data, datetime.now())

    def get_stock_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "1y",
        source: str = "auto"
    ) -> pd.DataFrame:
        cache_key = f"{symbol}_{start_date}_{end_date}_{period}_{source}"
        cached = self._check_cache(cache_key, 300)
        if cached is not None:
            return cached

        if source == "auto":
            data = self._fetch_with_fallback(symbol, start_date, end_date, period)
        elif source in self.sources:
            try:
                data = self.sources[source](symbol, start_date, end_date, period)
            except Exception:
                data = self._fetch_with_fallback(symbol, start_date, end_date, period)
        else:
            data = self._fetch_with_fallback(symbol, start_date, end_date, period)

        if data is not None and not data.empty:
            self._set_cache(cache_key, data)
        return data if data is not None else pd.DataFrame()

    def _fetch_with_fallback(self, symbol, start_date, end_date, period) -> pd.DataFrame:
        errors = []
        # A股优先 mootdx(TCP不封IP) > baostock > eastmoney > akshare
        if symbol.lower().startswith(('sh', 'sz', '6', '0', '3', 'bj', '8', '4')):
            source_names = ['mootdx', 'baostock', 'eastmoney', 'akshare', 'yfinance']
        else:
            source_names = ['yfinance', 'eastmoney', 'akshare']

        for src in source_names:
            if src in self.sources:
                try:
                    data = self.sources[src](symbol, start_date, end_date, period)
                    if data is not None and not data.empty and len(data) > 5:
                        return data
                except Exception as e:
                    errors.append(f"{src}: {e}")
                    continue

        if errors:
            print(f"所有数据源均失败: {'; '.join(errors)}")
        return pd.DataFrame()

    def _fetch_mootdx_kline(
        self, symbol: str, start_date: Optional[str], end_date: Optional[str], period: str
    ) -> pd.DataFrame:
        """
        用 mootdx(TCP通达信) 获取A股日线, 不封IP, 比HTTP更稳定。
        未安装 mootdx 时抛 ImportError, 由 _fetch_with_fallback 跳过。
        """
        from .sources.quote import MootdxSource
        m = MootdxSource()
        if not m.is_available():
            return pd.DataFrame()

        period_count_map = {'1d': 1, '5d': 5, '1mo': 21, '3mo': 63,
                            '6mo': 126, '1y': 250, '2y': 500, '5y': 1250,
                            'ytd': 130, 'max': 2000}
        count = period_count_map.get(period, 250)

        try:
            return m.kline(symbol, category=4, offset=count)
        except Exception as e:
            print(f"mootdx K线获取失败: {e}")
            return pd.DataFrame()

    def _fetch_akshare(
        self, symbol: str, start_date: Optional[str], end_date: Optional[str], period: str
    ) -> pd.DataFrame:
        try:
            import akshare as ak

            code = symbol.replace('sh', '').replace('sz', '').replace('SH', '').replace('SZ', '')
            if not code.endswith(('SH', 'SZ')):
                if symbol.startswith(('sh', 'sh')):
                    pass
                if code.startswith(('6', '5')):
                    code = f"{code}"
                else:
                    code = f"{code}"

            if not start_date:
                period_map = {'1d': '1', '5d': '5', '1mo': '21', '3mo': '63',
                              '6mo': '126', '1y': '252', '2y': '504', '5y': '1260'}
                days = int(period_map.get(period, '252'))
                start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
            else:
                start_date = start_date.replace('-', '')
            if not end_date:
                end_date = datetime.now().strftime('%Y%m%d')
            else:
                end_date = end_date.replace('-', '')

            try:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq"
                )
                if not df.empty:
                    df = df.rename(columns={
                        '日期': 'date', '开盘': 'Open', '收盘': 'Close',
                        '最高': 'High', '最低': 'Low', '成交量': 'Volume',
                        '成交额': 'Amount', '振幅': 'Amplitude',
                        '涨跌幅': 'ChangePct', '涨跌额': 'Change',
                        '换手率': 'Turnover'
                    })
                    df['date'] = pd.to_datetime(df['date'])
                    df.set_index('date', inplace=True)
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume', 'Amount']]
                    df = df.astype(float)
                    return df
            except Exception:
                pass

            try:
                df = ak.stock_zh_a_daily(
                    symbol=f"sh{code}" if code.startswith('6') else f"sz{code}",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq"
                )
                if not df.empty:
                    df = df.rename(columns={
                        'date': 'date', 'open': 'Open', 'close': 'Close',
                        'high': 'High', 'low': 'Low', 'volume': 'Volume'
                    })
                    df['date'] = pd.to_datetime(df['date'])
                    df.set_index('date', inplace=True)
                    return df[['Open', 'High', 'Low', 'Close', 'Volume']]
            except Exception:
                pass

        except ImportError:
            pass
        except Exception as e:
            print(f"akshare获取数据失败: {e}")
        return pd.DataFrame()

    def _fetch_baostock(
        self, symbol: str, start_date: Optional[str], end_date: Optional[str], period: str
    ) -> pd.DataFrame:
        """
        用 baostock(免费、无需 token)获取 A 股日线，作为 akshare 的稳定回退源。
        未安装 baostock 时抛 ImportError，由 _fetch_with_fallback 跳过。
        输出列名与 akshare 分支一致: [Open, High, Low, Close, Volume, Amount]，date 为索引。
        """
        import baostock as bs  # 未装则 ImportError -> 上层跳过

        code = symbol.lower().replace('sh', '').replace('sz', '')
        # baostock 代码格式: sh.600000 / sz.000001
        if symbol.lower().startswith('sh') or code[:1] in ('6', '5', '9'):
            bs_code = f"sh.{code}"
        else:
            bs_code = f"sz.{code}"

        if not start_date:
            period_map = {'1mo': 30, '3mo': 90, '6mo': 180, '1y': 365,
                          '2y': 730, '5y': 1825}
            days = period_map.get(period, 365)
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        else:
            start_date = start_date if '-' in start_date else \
                f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')
        else:
            end_date = end_date if '-' in end_date else \
                f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

        lg = None
        try:
            lg = bs.login()
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2",  # 2=前复权
            )
            rows = []
            while rs is not None and rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=['date', 'Open', 'High', 'Low',
                                             'Close', 'Volume', 'Amount'])
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            for c in ['Open', 'High', 'Low', 'Close', 'Volume', 'Amount']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.dropna(subset=['Close'])
            return df[['Open', 'High', 'Low', 'Close', 'Volume', 'Amount']]
        finally:
            if lg is not None:
                try:
                    bs.logout()
                except Exception:
                    pass

    def _fetch_eastmoney_kline(
        self, symbol: str, start_date: Optional[str], end_date: Optional[str], period: str
    ) -> pd.DataFrame:
        try:
            from ..data.realtime.realtime_data import RealtimeData
            rt = RealtimeData()

            if not symbol.startswith(('sh', 'sz')):
                if symbol.startswith('6'):
                    symbol = f'sh{symbol}'
                else:
                    symbol = f'sz{symbol}'

            period_map = {
                '1d': 'day', '5d': 'day', '1mo': 'day', '3mo': 'day',
                '6mo': 'day', '1y': 'day', '2y': 'day', '5y': 'day',
                'ytd': 'day', 'max': 'day'
            }
            kline_period = period_map.get(period, 'day')

            period_count_map = {'1d': 1, '5d': 5, '1mo': 21, '3mo': 63,
                                '6mo': 126, '1y': 252, '2y': 504, '5y': 1260}
            count = period_count_map.get(period, 252)

            df = rt.get_kline_data(symbol, period=kline_period, count=count)
            if not df.empty:
                return df
        except Exception as e:
            print(f"东方财富K线获取失败: {e}")
        return pd.DataFrame()

    def _fetch_yfinance(
        self, symbol: str, start_date: Optional[str], end_date: Optional[str], period: str
    ) -> pd.DataFrame:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            if start_date and end_date:
                data = ticker.history(start=start_date, end=end_date)
            else:
                data = ticker.history(period=period)
            return data
        except Exception as e:
            print(f"yfinance获取{symbol}数据失败: {e}")
            return pd.DataFrame()

    def get_multiple_stocks(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "1y"
    ) -> Dict[str, pd.DataFrame]:
        data_dict = {}
        for symbol in symbols:
            data_dict[symbol] = self.get_stock_data(
                symbol, start_date, end_date, period
            )
        return data_dict

    def get_stock_info(self, symbol: str) -> Dict:
        try:
            import akshare as ak
            code = symbol.replace('sh', '').replace('sz', '')
            info = ak.stock_zh_a_spot_em()
            row = info[info['代码'] == code]
            if not row.empty:
                return {
                    'code': code,
                    'name': row.iloc[0]['名称'],
                    'price': float(row.iloc[0]['最新价']),
                    'change_pct': float(row.iloc[0]['涨跌幅']),
                    'volume': float(row.iloc[0]['成交量']),
                    'amount': float(row.iloc[0]['成交额']),
                    'pe': float(row.iloc[0]['市盈率-动态']) if pd.notna(row.iloc[0]['市盈率-动态']) else 0,
                    'market_cap': float(row.iloc[0]['总市值']),
                    'circulating_cap': float(row.iloc[0]['流通市值'])
                }
        except ImportError:
            pass
        except Exception as e:
            print(f"获取股票信息失败: {e}")
        return {}

    def get_crypto_data(
        self,
        symbol: str,
        exchange: str = "binance",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> pd.DataFrame:
        try:
            import ccxt
            exchange_class = getattr(ccxt, exchange)
            exchange_obj = exchange_class()
            since = None
            if start_date:
                since = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
            ohlcv = exchange_obj.fetch_ohlcv(
                symbol, timeframe='1d', since=since, limit=1000
            )
            df = pd.DataFrame(
                ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            print(f"获取加密货币数据失败: {e}")
            return pd.DataFrame()

    def clear_cache(self):
        self.cache.clear()
