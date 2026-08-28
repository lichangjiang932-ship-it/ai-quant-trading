"""
东财防封统一请求入口(抄袭 a-stock-data 的 em_get)

问题背景: 东财系 HTTP 接口(push2 / push2ex / datacenter / reportapi / search / np-*)有风控:
    每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 次  ->  临时封 IP。
    更棘手: push2/push2ex 会按 TLS/JA3 指纹识别客户端, 直接对 Python requests 的握手
    **掐断连接(RemoteDisconnected)**, 于是 requests 每次都触发 3 次重试白等 8s 后返回空,
    表现为「非交易时段查询无反应」。curl / 浏览器同一 URL 却 0.3s 正常返回。

解决: 优先用 curl_cffi 以 Chrome 指纹发起请求(impersonate), 绕过 JA3 屏蔽;
      未安装 curl_cffi 时自动降级回 requests(仍带限流+重试), 保证不硬依赖。

本模块提供全项目统一入口 em_get():
- 模块级 Keep-Alive Session 复用连接 (curl_cffi 或 requests)
- 串行最小间隔(默认 1s) + 随机抖动,批量调用自动降速
- 连接级自动重试: 瞬态连接错误 / 429 / 5xx 指数退避
- 403 不重试(东财风控信号,重试无益,靠降频应对)
任何东财请求都应走 em_get,避免高频封 IP。
"""
import time
import random
import threading
from typing import Dict, List, Optional

import requests

# ---- curl_cffi: 以真实浏览器 TLS 指纹发请求, 绕过东财 push2/push2ex 的 JA3 屏蔽 ----
try:
    from curl_cffi import requests as _cffi
    _HAS_CFFI = True
except Exception:  # 未安装则降级, 不影响主流程
    _cffi = None
    _HAS_CFFI = False

# curl_cffi 模拟的浏览器版本(新版本用 chrome 通用别名, 老版本回退到 chrome120)
_IMPERSONATE = "chrome"


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 两次东财请求最小间隔(秒);批量筛选可调大到 1.5~2
EM_MIN_INTERVAL = 1.0

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})

# 连接级自动重试(老版本 urllib3 缺参数时降级为无重试,不影响主流程)
try:
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except Exception:
        from requests.packages.urllib3.util.retry import Retry
    _adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    ))
    _EM_SESSION.mount("https://", _adapter)
    _EM_SESSION.mount("http://", _adapter)
except Exception:
    pass

# 默认忽略系统代理/VPN(HTTP_PROXY 等环境变量)。东财是境内源,不需要科学上网;
# 若用户开了代理软件但代理端口不通,会导致所有东财请求 ProxyError。设 trust_env=False
# 让 requests 直连境内接口。如确实需要走代理,调 set_use_proxy(True)。
_EM_SESSION.trust_env = False

# curl_cffi 独立 Session(复用连接), 同样直连不走系统代理
_CFFI_SESSION = None
if _HAS_CFFI:
    try:
        _CFFI_SESSION = _cffi.Session(impersonate=_IMPERSONATE, trust_env=False)
    except Exception:
        try:
            _CFFI_SESSION = _cffi.Session(impersonate=_IMPERSONATE)
        except Exception:
            _CFFI_SESSION = None

_lock = threading.Lock()
_last_call = [0.0]  # 上次请求时间戳(列表以便闭包内修改)
_use_proxy = [False]


def set_min_interval(seconds: float):
    """允许上层(如 config)调节节流间隔。"""
    global EM_MIN_INTERVAL
    try:
        EM_MIN_INTERVAL = max(0.0, float(seconds))
    except (ValueError, TypeError):
        pass


def set_use_proxy(enabled: bool):
    """是否让东财请求走系统代理/VPN。默认 False(直连境内源,避免代理端口不通导致失败)。"""
    _EM_SESSION.trust_env = bool(enabled)
    _use_proxy[0] = bool(enabled)


def _cffi_get(url, params, merged_headers, timeout, **kwargs):
    """用 curl_cffi(Chrome 指纹)发请求。失败抛异常由上层决定是否回退。"""
    sess = _CFFI_SESSION
    if sess is not None:
        return sess.get(url, params=params, headers=merged_headers,
                        timeout=timeout, **kwargs)
    # 没有持久 session 时用一次性请求
    return _cffi.get(url, params=params, headers=merged_headers,
                     timeout=timeout, impersonate=_IMPERSONATE, **kwargs)


def _cffi_post(url, body, merged_headers, timeout, **kwargs):
    """POST 版: 东财少数接口(如热议股排行)只收 JSON body, 需 POST。"""
    sess = _CFFI_SESSION
    if sess is not None:
        return sess.post(url, json=body, headers=merged_headers,
                         timeout=timeout, **kwargs)
    return _cffi.post(url, json=body, headers=merged_headers,
                     timeout=timeout, impersonate=_IMPERSONATE, **kwargs)


def em_get(url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None,
           timeout: int = 15, **kwargs):
    """东财统一请求入口: 自动节流 + 复用 session + 默认 UA + 浏览器 TLS 指纹。

    所有 eastmoney.com 接口都应通过它请求,避免高频被封 IP。
    优先 curl_cffi(绕过 JA3 屏蔽), 失败/未安装则回退 requests。
    返回 Response 对象(具备 .json()/.text/.status_code), 调用方自行 .json()。
    异常照常抛出,由调用方 catch。
    """
    with _lock:
        wait = EM_MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        try:
            merged_headers = {"User-Agent": UA}
            if headers:
                merged_headers.update(headers)

            # 1) 优先 curl_cffi(Chrome 指纹), 绕过 push2/push2ex 的 TLS 屏蔽
            if _HAS_CFFI:
                try:
                    return _cffi_get(url, params, merged_headers, timeout, **kwargs)
                except Exception:
                    # curl_cffi 偶发失败 -> 回退 requests, 不让整条链路断掉
                    pass

            # 2) 回退: 普通 requests(带限流 + 重试)
            return _EM_SESSION.get(url, params=params, headers=merged_headers,
                                   timeout=timeout, **kwargs)
        finally:
            _last_call[0] = time.time()


def em_post(url: str, body: Optional[Dict] = None, headers: Optional[Dict] = None,
           timeout: int = 15, **kwargs):
    """东财统一 POST 入口: 复用 em_get 的节流 + curl_cffi 指纹 + 回退逻辑。

    用于只接受 JSON body 的东财接口(如 emappdata 热议股排行)。
    返回 Response 对象(具备 .json()/.text/.status_code)。
    """
    with _lock:
        wait = EM_MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        try:
            merged_headers = {"User-Agent": UA,
                              "Content-Type": "application/json"}
            if headers:
                merged_headers.update(headers)

            if _HAS_CFFI:
                try:
                    return _cffi_post(url, body, merged_headers, timeout, **kwargs)
                except Exception:
                    pass
            return _EM_SESSION.post(url, json=body, headers=merged_headers,
                                    timeout=timeout, **kwargs)
        finally:
            _last_call[0] = time.time()


def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                         filter_str: str = "", page_size: int = 50,
                         sort_columns: str = "", sort_types: str = "-1") -> List[Dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁/融资融券/北向等共用(已内置限流)。

    失败/无数据返回 []。
    """
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = em_get(DATACENTER_URL, params=params, timeout=15)
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception:
        pass
    return []
