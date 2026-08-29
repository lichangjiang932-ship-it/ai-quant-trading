"""
新闻获取模块 - 增强版
支持多个数据源：东方财富、新浪财经、财联社、同花顺、研报、公告、龙虎榜
"""
import requests
import json
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from threading import Thread, Event
import pandas as pd

from ..data.em_client import em_get, em_post  # 东财请求统一走节流器,防封 IP (em_post 用于 JSON-body 接口)


def _clean_stock_code(symbol: str) -> str:
    """把 sh600000 / 600000.SH 等格式统一为 6 位代码。"""
    value = str(symbol or "").strip().lower()
    value = re.sub(r"^(sh|sz|bj)", "", value)
    value = re.sub(r"\.(sh|sz|bj)$", "", value)
    match = re.search(r"(\d{6})", value)
    return match.group(1) if match else value


def _normalized_symbol(code: str) -> str:
    code = _clean_stock_code(code)
    if not (code.isdigit() and len(code) == 6):
        return ""
    # 北交所须先判: 43/83/87/88 开头 + 920xxx 新代码段 (否则 920 → 误判沪市)
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


class NewsItem:
    """新闻条目类"""
    
    def __init__(
        self,
        title: str,
        content: str = "",
        source: str = "",
        url: str = "",
        publish_time: Optional[datetime] = None,
        symbols: List[str] = None,
        category: str = "",
        importance: int = 0
    ):
        self.title = title
        self.content = content
        self.source = source
        self.url = url
        self.publish_time = publish_time or datetime.now()
        self.symbols = symbols or []
        self.category = category
        self.sentiment_score = None
        self.importance = importance
    
    def to_dict(self) -> Dict:
        return {
            'title': self.title,
            'content': self.content,
            'source': self.source,
            'url': self.url,
            'publish_time': self.publish_time.isoformat(),
            'symbols': self.symbols,
            'category': self.category,
            'sentiment_score': self.sentiment_score,
            'importance': self.importance
        }


class NewsFetcher:
    """新闻获取类 - 增强版"""
    
    def __init__(self):
        """初始化新闻获取器"""
        self.news_cache = []
        self.callbacks = []
        self.running = False
        self._thread = None
        self._stop_event = Event()
        self.source_health: Dict[str, Dict] = {}
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://www.eastmoney.com/'
        }

    def _record_source(self, source: str, ok: bool, count: int = 0, error: str = ""):
        self.source_health[source] = {
            "available": bool(ok),
            "count": max(int(count or 0), 0),
            "error": str(error or "")[:160],
            "checked_at": datetime.now().isoformat(),
        }
    
    def register_callback(self, callback):
        """注册新闻回调"""
        self.callbacks.append(callback)
    
    # ==================== 基础新闻源 ====================
    
    def fetch_sina_finance_news(self, count: int = 20) -> List[NewsItem]:
        """获取新浪财经7x24快讯"""
        news_list = []
        try:
            url = "https://feed.mix.sina.com.cn/api/roll/get"
            params = {'pageid': '153', 'lid': '2516', 'num': count, 'page': 1}
            
            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()
            
            if data.get('result') and data['result'].get('data'):
                for item in data['result']['data']:
                    title = item.get('title', '')
                    content = item.get('intro', '') or item.get('summary', '')
                    symbols = self._extract_symbols_from_text(title + content)
                    
                    news = NewsItem(
                        title=title,
                        content=content,
                        source='新浪财经',
                        url=item.get('url', ''),
                        publish_time=datetime.fromtimestamp(int(item.get('ctime', 0))) if item.get('ctime') else None,
                        symbols=symbols,
                        category='财经快讯',
                        importance=self._calc_importance(title, 'sina')
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取新浪财经新闻失败: {e}")
        return news_list
    
    def fetch_eastmoney_news(self, count: int = 20) -> List[NewsItem]:
        """获取东方财富新闻"""
        news_list = []
        try:
            # 尝试多个API
            urls = [
                f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size={count}&page_index=1&ann_type=A&client_source=web&f_node=0",
                f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&columns=102&pageSize={count}&page=1"
            ]
            
            for url in urls:
                try:
                    response = em_get(url, headers=self.headers, timeout=10)
                    data = response.json()
                    
                    items = []
                    if 'data' in data and 'list' in data.get('data', {}):
                        items = data['data']['list']
                    elif 'data' in data and 'diff' in data.get('data', {}):
                        items = data['data']['diff']
                    
                    for item in items:
                        title = item.get('title', '') or item.get('art_title', '')
                        content = item.get('digest', '') or item.get('content', '') or item.get('desc', '')
                        
                        if title:
                            symbols = self._extract_symbols_from_text(title + content)
                            news = NewsItem(
                                title=title,
                                content=content[:500],
                                source='东方财富',
                                url=item.get('url', '') or item.get('url_unique', ''),
                                publish_time=self._parse_time(item.get('showtime') or item.get('display_time', '')),
                                symbols=symbols,
                                category='财经新闻',
                                importance=self._calc_importance(title, 'eastmoney')
                            )
                            news_list.append(news)
                    
                    if news_list:
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"获取东方财富新闻失败: {e}")
        return news_list[:count]
    
    def fetch_cls_news(self, count: int = 20) -> List[NewsItem]:
        """获取财联社电报"""
        news_list = []
        try:
            url = "https://www.cls.cn/nodeapi/updateTelegraph"
            params = {'app': 'CailianpressWeb', 'os': 'web', 'sv': '8.4.6', 'rn': count}
            
            response = requests.get(url, headers=self.headers, timeout=10)
            data = response.json()
            
            if data.get('data') and data['data'].get('roll_data'):
                for item in data['data']['roll_data']:
                    content = item.get('content', '')
                    content = re.sub(r'<[^>]+>', '', content)
                    title = content[:50] + '...' if len(content) > 50 else content
                    symbols = self._extract_symbols_from_text(content)
                    
                    news = NewsItem(
                        title=title,
                        content=content,
                        source='财联社',
                        url=f"https://www.cls.cn/detail/{item.get('id', '')}",
                        publish_time=datetime.fromtimestamp(item.get('ctime', 0)) if item.get('ctime') else None,
                        symbols=symbols,
                        category='财经快讯',
                        importance=max(item.get('level', 5), self._calc_importance(title, 'cls'))
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取财联社新闻失败: {e}")
        return news_list
    
    def fetch_ths_news(self, count: int = 20) -> List[NewsItem]:
        """获取同花顺新闻"""
        news_list = []
        try:
            url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
            params = {'page': 1, 'tag': '', 'track': 'website', 'pagesize': count}

            # 同花顺接口需带同花顺 Referer + Chrome 指纹(em_get 已处理), 普通 requests 会被屏蔽
            response = em_get(url, params=params,
                              headers={'Referer': 'https://news.10jqka.com.cn/'},
                              timeout=10)
            data = response.json()
            
            if data.get('data') and data['data'].get('list'):
                for item in data['data']['list']:
                    title = item.get('title', '')
                    content = item.get('digest', '') or item.get('content', '')
                    symbols = self._extract_symbols_from_text(title + content)
                    
                    news = NewsItem(
                        title=title,
                        content=content[:500],
                        source='同花顺',
                        url=item.get('url', ''),
                        publish_time=self._parse_time(item.get('ctime', '')),
                        symbols=symbols,
                        category='财经新闻',
                        importance=self._calc_importance(title, 'ths')
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取同花顺新闻失败: {e}")
        return news_list

    def fetch_wallstreetcn_news(self, count: int = 20) -> List[NewsItem]:
        """获取华尔街见闻 7x24 快讯"""
        news_list = []
        try:
            url = "https://api-one.wallstcn.com/apiv1/content/lives"
            params = {'channel': 'global-channel', 'limit': count}

            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()

            items = data.get('data', {}).get('items', [])
            for item in items:
                content = item.get('content_text', '') or ''
                content = re.sub(r'<[^>]+>', '', content)
                title = item.get('title', '') or (content[:50] + '...' if len(content) > 50 else content)
                symbols = self._extract_symbols_from_text(content)

                news = NewsItem(
                    title=title,
                    content=content[:500],
                    source='华尔街见闻',
                    url=item.get('uri', '') or f"https://wallstreetcn.com/livenews/{item.get('id', '')}",
                    publish_time=datetime.fromtimestamp(int(item['display_time'])) if item.get('display_time') else None,
                    symbols=symbols,
                    category='财经快讯',
                    importance=max(self._calc_importance(title, 'wscn'), 5)
                )
                news_list.append(news)
        except Exception as e:
            print(f"获取华尔街见闻新闻失败: {e}")
        return news_list

    def fetch_yicai_news(self, count: int = 20) -> List[NewsItem]:
        """获取第一财经新闻"""
        news_list = []
        try:
            url = "https://www.yicai.com/api/ajax/getlatest"
            params = {'page': 1, 'pagesize': count}

            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()

            if isinstance(data, list):
                for item in data:
                    title = item.get('NewsTitle', '')
                    content = item.get('NewsNotes', '') or item.get('NewsSummary', '') or ''
                    content = re.sub(r'<[^>]+>', '', content)
                    symbols = self._extract_symbols_from_text(title + content)

                    news = NewsItem(
                        title=title,
                        content=content[:500],
                        source='第一财经',
                        url=item.get('NewsUrl', '') or item.get('url', ''),
                        publish_time=self._parse_time(item.get('CreateDate', '') or item.get('pubDate', '')),
                        symbols=symbols,
                        category='财经新闻',
                        importance=self._calc_importance(title, 'yicai')
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取第一财经新闻失败: {e}")
        return news_list

    def fetch_sina_live_news(self, count: int = 20) -> List[NewsItem]:
        """获取新浪财经 7x24 全球直播快讯 (JSON 接口, 比滚动新闻更新更及时)"""
        news_list = []
        try:
            url = "https://zhibo.sina.com.cn/api/zhibo/feed"
            params = {
                'page': 1, 'page_size': count, 'zhibo_id': 152,
                'tag_id': 0, 'dire': 'f', 'dpc': 1,
            }

            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()

            items = data.get('result', {}).get('data', {}).get('feed', {}).get('list', [])
            for item in items:
                content = item.get('rich_text', '') or ''
                content = re.sub(r'<[^>]+>', '', content)
                title = content[:50] + '...' if len(content) > 50 else content
                symbols = self._extract_symbols_from_text(content)

                news = NewsItem(
                    title=title,
                    content=content[:500],
                    source='新浪财经直播',
                    url=f"https://zhibo.sina.com.cn/live/152/1",
                    publish_time=self._parse_time(item.get('create_time', '')),
                    symbols=symbols,
                    category='财经快讯',
                    importance=max(self._calc_importance(title, 'sina'), 5)
                )
                news_list.append(news)
        except Exception as e:
            print(f"获取新浪直播新闻失败: {e}")
        return news_list
    
    # ==================== 专业数据源 ====================
    
    def fetch_stock_announcements(self, symbol: str = None, count: int = 20) -> List[NewsItem]:
        """获取上市公司公告"""
        news_list = []
        try:
            url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
            params = {
                'sr': '-1', 'page_size': count, 'page_index': '1',
                'ann_type': 'A', 'client_source': 'web', 'f_node': '0'
            }
            if symbol:
                # 东财 stock_list 只接受裸 6 位代码，传 sh/sz 前缀会正常返回空列表。
                params['stock_list'] = _clean_stock_code(symbol)

            response = em_get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()
            
            if data.get('data') and data['data'].get('list'):
                for item in data['data']['list']:
                    title = item.get('title_ch', '') or item.get('title', '')
                    codes = item.get('codes', [])
                    stock_code = codes[0].get('stock_code', '') if codes else ''
                    columns = item.get('columns', [])
                    column_name = columns[0].get('column_name', '') if columns else ''
                    
                    content = f"公告类型: {column_name}"
                    
                    news = NewsItem(
                        title=f"[公告] {title}",
                        content=content,
                        source='东方财富公告',
                        url=f"https://data.eastmoney.com/notices/detail/{stock_code}/{item.get('art_code', '')}.html",
                        publish_time=self._parse_time(item.get('notice_date', '') or item.get('display_time', '')),
                        symbols=[_normalized_symbol(stock_code)] if _normalized_symbol(stock_code) else [],
                        category='公司公告',
                        importance=7
                    )
                    news_list.append(news)
            self._record_source('eastmoney_announcements', True, len(news_list))
        except Exception as e:
            self._record_source('eastmoney_announcements', False, 0, str(e))
            print(f"获取公告失败: {e}")
        return news_list
    
    def fetch_research_reports(self, count: int = 20, keyword: str = None) -> List[NewsItem]:
        """获取研报数据。

        count 是第一个位置参数, 与其余 fetch_* 保持一致 —— 日度管线按
        fn(max_per_source) 位置调用, 若第一个参数是 keyword 会把条数误当个股代码。
        """
        news_list = []
        try:
            # 东方财富研报
            url = "https://reportapi.eastmoney.com/report/list"
            params = {
                'industryCode': '*', 'pageSize': count, 'industry': '*',
                'rating': '*', 'ratingChange': '*', 'beginTime': '',
                'endTime': '', 'pageNo': '1', 'fields': '',
                'qType': '0', 'orgCode': '', 'rcode': '', 'p': '1',
                'pageNum': '1', 'pageNumber': '1'
            }
            if keyword:
                # report/list 的 code 参数支持按个股定向查询，避免先拉市场研报再错误过滤。
                params['code'] = _clean_stock_code(keyword)

            response = em_get(url, params=params,
                              headers={'Referer': 'https://data.eastmoney.com/'},
                              timeout=10)
            data = response.json()

            if data.get('data'):
                for item in data['data']:
                    title = item.get('title', '')
                    stock_name = item.get('stockName', '')
                    org_name = item.get('orgSName', '')
                    em_rating = item.get('emRatingName', '')
                    
                    content = f"机构: {org_name}, 评级: {em_rating}, 目标价: {item.get('predictThisYearPe', '')}"
                    
                    news = NewsItem(
                        title=f"[研报] {stock_name} - {title}",
                        content=content,
                        source='东方财富研报',
                        url=f"https://data.eastmoney.com/report/info/{item.get('infoCode', '')}.html",
                        publish_time=self._parse_time(item.get('publishDate', '')),
                        symbols=[_normalized_symbol(item.get('stockCode', ''))] if _normalized_symbol(item.get('stockCode', '')) else [],
                        category='研究报告',
                        importance=6
                    )
                    news_list.append(news)
            self._record_source('eastmoney_reports', True, len(news_list))
        except Exception as e:
            self._record_source('eastmoney_reports', False, 0, str(e))
            print(f"获取研报失败: {e}")
        return news_list
    
    def fetch_dragon_tiger(self, count: int = 20) -> List[NewsItem]:
        """获取龙虎榜数据"""
        news_list = []
        try:
            url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
            params = {
                'sortColumns': 'TURNOVERVALUE',
                'sortTypes': '-1',
                'pageSize': count,
                'pageNumber': '1',
                'reportName': 'RPT_DAILYBILLBOARD_DETAILSNEW',
                'columns': 'ALL',
                'source': 'WEB',
                'client': 'WEB'
            }

            response = em_get(url, params=params, headers=self.headers, timeout=10)
            data = response.json()
            
            if data.get('result') and data['result'].get('data'):
                for item in data['result']['data']:
                    stock_code = item.get('SECURITY_CODE', '')
                    stock_name = item.get('SECURITY_NAME_ABBR', '')
                    reason = item.get('EXPLAIN', '')
                    turnover = item.get('TURNOVERVALUE', 0)
                    net_buy = item.get('BILLBOARD_NET_AMT', 0)
                    
                    content = f"上榜原因: {reason}, 成交额: {turnover/10000:.0f}万, 净买入: {net_buy/10000:.0f}万"
                    
                    news = NewsItem(
                        title=f"[龙虎榜] {stock_name}({stock_code}) {reason}",
                        content=content,
                        source='东方财富龙虎榜',
                        url=f"https://data.eastmoney.com/stock/{stock_code}.html",
                        publish_time=datetime.now(),
                        symbols=[f'sh{stock_code}'] if stock_code.startswith('6') else [f'sz{stock_code}'],
                        category='龙虎榜',
                        importance=7
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取龙虎榜失败: {e}")
        return news_list
    
    def fetch_hot_stocks_by_media(self, count: int = 20) -> List[NewsItem]:
        """获取财经媒体热议股票 (东财热门排行, 需 POST + JSON body)"""
        news_list = []
        try:
            url = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
            body = {
                'appId': 'appId01',
                'globalId': '786e4c21-70dc-435a-93bb-38',
                'marketType': '',
                'pageNo': 1,
                'pageSize': count
            }

            # 该接口只收 JSON body 且需 POST, em_get 是 GET 会丢 body; 用 em_post
            response = em_post(url, body=body,
                               headers={'Referer': 'https://emappdata.eastmoney.com/'},
                               timeout=10)
            data = response.json()
            
            if data.get('data'):
                for item in data['data']:
                    stock_code = item.get('sc', '')
                    stock_name = item.get('sn', '')
                    rank = item.get('rk', 0)
                    hot_value = item.get('hot', 0)
                    
                    news = NewsItem(
                        title=f"[热议] {stock_name}({stock_code}) 热度排名{rank}",
                        content=f"热度值: {hot_value}",
                        source='东方财富热议',
                        url=f"https://quote.eastmoney.com/{stock_code}.html",
                        publish_time=datetime.now(),
                        symbols=[stock_code] if stock_code else [],
                        category='热门股票',
                        importance=min(5 + rank // 10, 9)
                    )
                    news_list.append(news)
        except Exception as e:
            print(f"获取热议股票失败: {e}")
        return news_list
    
    # ==================== 辅助方法 ====================
    
    def _extract_symbols_from_text(self, text: str) -> List[str]:
        """从文本中提取股票代码"""
        symbols = []
        
        patterns = [
            r'[（\(](\d{6})[）\)]',
            r'(sh\d{6})',
            r'(sz\d{6})',
            r'(\d{6})\.(SH|SZ|sh|sz)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                if isinstance(match, tuple):
                    code = match[0]
                else:
                    code = match
                
                code = re.sub(r'[^\d]', '', code)
                if len(code) == 6:
                    if code.startswith('6'):
                        symbols.append(f'sh{code}')
                    else:
                        symbols.append(f'sz{code}')
        
        return list(set(symbols))
    
    def _parse_time(self, time_str: str) -> Optional[datetime]:
        """解析时间字符串"""
        if not time_str:
            return None
        
        formats = [
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d',
            '%Y/%m/%d %H:%M:%S',
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue
        
        # 尝试时间戳
        try:
            ts = int(time_str)
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            pass
        
        return None
    
    def _calc_importance(self, title: str, source: str) -> int:
        """计算新闻重要性"""
        importance = 5
        
        # 来源权重
        source_weights = {
            'cls': 2, '财联社': 2,
            'wscn': 2, '华尔街见闻': 2,
            'eastmoney': 1, '东方财富': 1,
            'sina': 1, '新浪财经': 1, '新浪财经直播': 1,
            'ths': 1, '同花顺': 1,
            'yicai': 1, '第一财经': 1,
        }
        importance += source_weights.get(source, 0)
        
        # 关键词
        keywords = ['重大', '紧急', '突发', '重磅', '央行', '国务院', '证监会', '暴雷']
        for kw in keywords:
            if kw in title:
                importance += 1
                break
        
        return min(importance, 10)
    
    # ==================== 聚合方法 ====================
    
    def fetch_all_news(self) -> List[NewsItem]:
        """获取所有新闻源 (7 个渠道聚合)"""
        all_news = []
        
        all_news.extend(self.fetch_sina_finance_news(20))
        all_news.extend(self.fetch_sina_live_news(20))
        all_news.extend(self.fetch_eastmoney_news(20))
        all_news.extend(self.fetch_cls_news(20))
        all_news.extend(self.fetch_ths_news(20))
        all_news.extend(self.fetch_wallstreetcn_news(20))
        all_news.extend(self.fetch_yicai_news(20))
        
        # 去重
        seen = set()
        unique = []
        for news in all_news:
            key = news.title[:30]
            if key not in seen:
                seen.add(key)
                unique.append(news)
        
        unique.sort(key=lambda x: x.publish_time or datetime.min, reverse=True)
        self.news_cache = unique
        return unique
    
    def fetch_all_professional(self) -> List[NewsItem]:
        """获取所有专业数据源"""
        all_news = []
        
        all_news.extend(self.fetch_stock_announcements(count=20))
        all_news.extend(self.fetch_research_reports(count=20))
        all_news.extend(self.fetch_dragon_tiger(count=10))
        all_news.extend(self.fetch_hot_stocks_by_media(count=10))
        
        seen = set()
        unique = []
        for news in all_news:
            key = news.title[:30]
            if key not in seen:
                seen.add(key)
                unique.append(news)
        
        unique.sort(key=lambda x: x.publish_time or datetime.min, reverse=True)
        return unique
    
    def fetch_stock_news(self, symbol: str, count: int = 10) -> List[NewsItem]:
        """按个股定向获取公告和研报，并在东财公告不足时回退巨潮。"""
        return self.fetch_stock_news_with_meta(symbol, count)['items']

    def fetch_stock_news_with_meta(self, symbol: str, count: int = 10) -> Dict:
        """获取个股研究材料，同时返回实际使用的数据源和覆盖状态。"""
        all_news = []
        code = _clean_stock_code(symbol)
        normalized = _normalized_symbol(code)
        
        # 个股公告
        all_news.extend(self.fetch_stock_announcements(symbol, count))
        
        # 个股研报必须使用 code 参数定向查询；旧逻辑把裸代码和 sh/sz 代码比较，永远匹配不到。
        all_news.extend(self.fetch_research_reports(keyword=code, count=count))

        # 巨潮是法定信息披露源；当东财公告为空时按需回退，而不是无差别增加爬取量。
        cninfo_count = 0
        if not any(item.category == '公司公告' for item in all_news):
            try:
                from ..data.sources.fundamental import cninfo_announcements

                for row in cninfo_announcements(code, page_size=count):
                    all_news.append(NewsItem(
                        title=f"[公告] {row.get('title', '')}",
                        content=f"公告类型: {row.get('type', '')}",
                        source='巨潮资讯',
                        url=row.get('url', ''),
                        publish_time=self._parse_time(row.get('date', '')),
                        symbols=[normalized] if normalized else [],
                        category='公司公告',
                        importance=7,
                    ))
                    cninfo_count += 1
                self._record_source('cninfo_announcements', True, cninfo_count)
            except Exception as exc:
                self._record_source('cninfo_announcements', False, 0, str(exc))

        seen = set()
        unique = []
        for item in all_news:
            key = (item.title.strip(), item.url.strip())
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        unique.sort(key=lambda item: item.publish_time or datetime.min, reverse=True)

        source_names = sorted({item.source for item in unique if item.source})
        checked = [
            dict({'source': key}, **value)
            for key, value in self.source_health.items()
            if key in {'eastmoney_announcements', 'eastmoney_reports', 'cninfo_announcements'}
        ]
        covered = any(item.get('available') for item in checked)
        return {
            'items': unique[:count],
            'status': {
                'available': covered,
                'records': len(unique),
                'sources': source_names,
                'checks': checked,
                'missing_reason': '' if covered else '公告与研报数据源均不可用',
            },
        }
    
    # ==================== 监控方法 ====================
    
    def start_monitor(self, interval: int = 60):
        """启动新闻监控"""
        self.running = True
        self._stop_event.clear()
        self._thread = Thread(target=self._monitor_loop, args=(interval,), daemon=True)
        self._thread.start()
        print("新闻监控已启动")
    
    def _monitor_loop(self, interval: int):
        """监控循环"""
        while self.running and not self._stop_event.is_set():
            try:
                news_list = self.fetch_all_news()
                for callback in self.callbacks:
                    try:
                        callback(news_list)
                    except Exception as e:
                        print(f"新闻回调出错: {e}")
            except Exception as e:
                print(f"新闻监控出错: {e}")
            self._stop_event.wait(interval)
    
    def stop_monitor(self):
        """停止新闻监控"""
        self.running = False
        self._stop_event.set()
    
    def get_recent_news(self, minutes: int = 30) -> List[NewsItem]:
        """获取最近N分钟的新闻"""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        return [n for n in self.news_cache if n.publish_time and n.publish_time >= cutoff]
