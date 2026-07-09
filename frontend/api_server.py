"""
AI量化交易平台 — FastAPI 后端数据服务
为前端 dashboard 提供实时行情、K线、策略、AI信号等数据接口
"""
import sys
import os
import json
import asyncio
import warnings
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

warnings.filterwarnings("ignore", message=r".*doesn\'t match a supported version.*", category=Warning)
warnings.filterwarnings("ignore", message=r".*does not match a supported version.*", category=Warning)
warnings.filterwarnings("ignore", category=Warning, module=r"requests(\\..*)?")

from src.data.realtime.realtime_data import RealtimeData
from src.data.data_loader import DataLoader
from src.execution.brokers.simulated_broker import SimulatedBroker
from src.execution.brokers.base_broker import Order, OrderDirection, OrderType
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide
from src.utils.config import Config

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.yaml")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

app = FastAPI(title="AI量化交易平台API", version="3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载前端静态文件
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# 全局数据组件
config = Config(CONFIG_PATH)
realtime = RealtimeData()
broker = SimulatedBroker(
    initial_capital=config.get('trading.initial_capital', 1000000),
    commission_rate=config.get('commission.rate', 0.0003),
    stamp_tax_rate=config.get('commission.stamp_tax', 0.001),
    min_commission=config.get('commission.min', 5),
    slippage=config.get('trading.slippage', 0.0001),
)
risk_mgr = RiskManager(
    max_position_size=config.get('risk.max_position_size', 0.10),
    max_drawdown=config.get('risk.max_drawdown', 0.20),
    stop_loss=config.get('risk.stop_loss', 0.05),
    take_profit=config.get('risk.take_profit', 0.10),
    max_daily_loss=config.get('risk.max_daily_loss', 0.02),
    max_total_position=config.get('risk.max_total_position', 0.95),
    max_orders_per_day=config.get('risk.max_orders_per_day', 100),
)
broker.connect()
_ai_graph_cache = None


COMMON_SYMBOL_NAMES = {
    "sh600000": "浦发银行",
    "sz000001": "平安银行",
    "sh600104": "上汽集团",
    "sz000002": "万科A",
    "sh601318": "中国平安",
    "sh600519": "贵州茅台",
    "sz300750": "宁德时代",
    "sh000001": "上证指数",
}


def _normalize_symbol(s: str) -> str:
    s = s.strip()
    if s.startswith(('sh', 'sz', 'bj')):
        return s
    if s.startswith(('6', '9')):
        return 'sh' + s
    if s.startswith('0') or s.startswith('3'):
        return 'sz' + s
    return s


DEFAULT_POOL = ['sh600000', 'sh600104', 'sz000001', 'sz000002']


def _get_watchlist() -> List[str]:
    """自选股列表(核心池)。空则回退 trading.symbols, 再回退内置默认。"""
    wl = config.get('trading.watchlist', None)
    if not wl:
        wl = config.get('trading.symbols', DEFAULT_POOL)
    seen, out = set(), []
    for s in (wl or []):
        sym = _normalize_symbol(str(s))
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out or list(DEFAULT_POOL)


def _resolve_symbol_name(query: str):
    """把用户输入(代码/名称/拼音)解析成 (symbol, name)。解析不到返回 (normalized, '')。"""
    q = (query or '').strip()
    if not q:
        return None, ''
    # 纯代码直接规范化
    if any(ch.isdigit() for ch in q) and len(q.replace('sh', '').replace('sz', '').replace('bj', '')) >= 6:
        sym = _normalize_symbol(q.lower())
        return sym, COMMON_SYMBOL_NAMES.get(sym, '')
    # 名称/拼音走东财 suggest
    try:
        hits = _suggest_symbols(q, limit=1)
        if hits:
            return _normalize_symbol(hits[0]['symbol']), hits[0].get('name', '')
    except Exception:
        pass
    sym = _normalize_symbol(q.lower())
    return sym, COMMON_SYMBOL_NAMES.get(sym, '')


def _save_watchlist(symbols: List[str]):
    """持久化自选股到 config.yaml。"""
    clean, seen = [], set()
    for s in symbols:
        sym = _normalize_symbol(str(s))
        if sym and sym not in seen:
            seen.add(sym)
            clean.append(sym)
    config.set('trading.watchlist', clean)
    config.save_config(CONFIG_PATH)
    return clean


def _portfolio_snapshot() -> Dict:
    account = broker.get_account_info()
    positions = broker.get_positions()
    total_asset = float(account.get('total_asset', 0) or 0)
    market_value = float(account.get('market_value', 0) or 0)
    pos_map = {p.symbol: p for p in positions}

    risk_mgr.current_equity = total_asset
    if risk_mgr.peak_equity <= 0:
        risk_mgr.peak_equity = max(total_asset, float(account.get('initial_capital', 0) or 0))

    return {
        "account": account,
        "positions": positions,
        "pos_map": pos_map,
        "total_asset": total_asset,
        "cash": float(account.get('cash', 0) or 0),
        "market_value": market_value,
        "total_position_pct": market_value / max(total_asset, 1),
    }


def _pre_trade_check(symbol: str, side: str, quantity: int, price: float, reason: str = "") -> Dict:
    symbol = _normalize_symbol(symbol)
    snap = _portfolio_snapshot()
    if not symbol:
        return {"allowed": False, "reason": "股票代码不能为空", "suggested_quantity": 0}
    if price <= 0:
        return {"allowed": False, "reason": "价格必须大于 0", "suggested_quantity": 0}
    if quantity <= 0:
        return {"allowed": False, "reason": "数量必须大于 0", "suggested_quantity": 0}

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    position = snap["pos_map"].get(symbol)
    current_qty = int(getattr(position, 'quantity', 0) or 0) if position else 0
    if order_side == OrderSide.SELL and quantity > current_qty:
        return {"allowed": False, "reason": f"可卖数量不足，当前持仓 {current_qty} 股", "suggested_quantity": current_qty}

    result = risk_mgr.check_order(OrderRequest(
        symbol=symbol,
        side=order_side,
        quantity=int(quantity),
        price=float(price),
        portfolio_value=snap["total_asset"],
        current_position_value=snap["market_value"],
        reason=reason,
    ))

    if result.allowed and order_side == OrderSide.BUY:
        estimated_cost = quantity * price * (1 + broker.commission_rate + max(getattr(broker, 'slippage', 0), 0))
        estimated_cost += broker.min_commission
        if estimated_cost > snap["cash"]:
            suggested = int(snap["cash"] / max(price * (1 + broker.commission_rate), 1)) // 100 * 100
            return {"allowed": False, "reason": "可用资金不足", "suggested_quantity": max(suggested, 0)}

    return {
        "allowed": result.allowed,
        "reason": result.reason or "风控通过",
        "suggested_quantity": result.suggested_quantity or int(quantity),
        "violations": result.violations,
    }


def _build_rule_ai_signals(symbols: Optional[List[str]] = None, quotes: Optional[Dict] = None) -> List[Dict]:
    symbols = symbols or config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104'])
    symbols = [_normalize_symbol(s) for s in symbols]
    quotes = quotes or realtime.get_quotes(symbols, sources=['tencent', 'sina', 'eastmoney'])

    signals = []
    for sym in symbols[:6]:
        q = quotes.get(sym, {})
        change_pct = float(q.get('change_pct', 0) or 0)
        vol_ratio = float(q.get('vol_ratio', 0) or 0)
        turnover = float(q.get('turnover_pct', q.get('turnover', 0)) or 0)
        score = change_pct * 0.18 + max(vol_ratio - 1, 0) * 0.12 + turnover * 0.01
        if score > 0.35:
            action = "buy"
            confidence = min(0.55 + abs(score) * 0.12, 0.90)
            reason = "量价同步偏强，短线资金承接较好"
        elif score < -0.25:
            action = "sell"
            confidence = min(0.55 + abs(score) * 0.12, 0.88)
            reason = "价格动量转弱，建议降低风险敞口"
        else:
            action = "hold"
            confidence = min(0.35 + abs(score) * 0.08, 0.60)
            reason = "信号强度不足，等待更明确确认"
        signals.append({
            "symbol": sym,
            "action": action,
            "confidence": round(confidence, 2),
            "reason": reason,
            "source": "rule",
        })
    return signals


def _get_ai_graph():
    global _ai_graph_cache
    if _ai_graph_cache is not None:
        return _ai_graph_cache
    from src.ai.llm_client import LLMClient
    from src.ai.agents.orchestrator import TradingAgentsGraph
    from src.data.fundamentals import FundamentalsFetcher
    from src.data.capital_flow import CapitalFlowFetcher
    from src.news.news_analyzer import NewsAnalyzer

    ag = config.get('agents', {}) or {}
    ai_cfg = config.get('ai', {}) or {}
    llm = LLMClient(
        provider=ag.get('provider') or ai_cfg.get('provider', 'deepseek'),
        model=ag.get('deep_think_model') or ai_cfg.get('model'),
        api_key_env=ag.get('api_key_env') or ai_cfg.get('api_key_env'),
        base_url=ag.get('base_url') or ai_cfg.get('base_url'),
        temperature=ag.get('temperature', ai_cfg.get('temperature', 0.3)),
    )
    graph = TradingAgentsGraph(
        llm=llm,
        config={
            "analysts": ag.get('analysts', ['technical', 'sentiment', 'news', 'fundamentals', 'capital']),
            "max_debate_rounds": ag.get('max_debate_rounds', 1),
            "quick_think_model": ag.get('quick_think_model'),
            "deep_think_model": ag.get('deep_think_model'),
            "use_memory": False,
            "max_drawdown": risk_mgr.max_drawdown,
            "max_total_position": risk_mgr.max_total_position,
        },
        kline_provider=realtime,
        news_analyzer=NewsAnalyzer(),
        fundamentals=FundamentalsFetcher(),
        capital_provider=CapitalFlowFetcher(),
        memory=None,
    )
    _ai_graph_cache = {"graph": graph, "llm": llm}
    return _ai_graph_cache


def _build_agent_ai_signals(symbols: List[str], quotes: Dict) -> List[Dict]:
    bundle = _get_ai_graph()
    graph = bundle["graph"]
    snap = _portfolio_snapshot()
    signals = []
    for sym in symbols[:4]:
        quote = quotes.get(sym, {})
        position = snap["pos_map"].get(sym)
        try:
            decision = graph.analyze(sym, {
                "symbol": sym,
                "price": quote.get('price', 0),
                "change_pct": quote.get('change_pct', 0),
                "position": int(getattr(position, 'quantity', 0) or 0) if position else 0,
                "risk": {
                    "drawdown": risk_mgr.get_risk_report().get('drawdown', 0),
                    "total_position_pct": snap["total_position_pct"],
                    "daily_pnl": risk_mgr.daily_pnl,
                },
                "trade_date": datetime.now().strftime('%Y%m%d'),
            }).to_dict()
            signals.append({
                "symbol": sym,
                "action": decision.get('action', 'hold'),
                "confidence": decision.get('confidence', 0),
                "reason": decision.get('reason', ''),
                "source": "agents",
                "risk": decision.get('risk', {}),
                # 透传完整多智能体推理链, 供前端展开显示
                "analysts": decision.get('analysts', []),
                "debate": decision.get('debate', {}),
                "trader": decision.get('trader', {}),
            })
        except Exception as exc:
            signals.append({
                "symbol": sym,
                "action": "hold",
                "confidence": 0,
                "reason": f"AI分析失败，已转观望: {exc}",
                "source": "error",
            })
    return signals


# ============================================================
#  AI 自动选股 — 全市场候选池 → 规则粗筛 → 多智能体深度分析(后台任务)
# ============================================================
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _Pool

# 后台选股任务状态(单例; 同一时刻只跑一轮)
_scan_job = {
    "status": "idle",      # idle | running | done | error
    "pool": "",            # market | watchlist
    "total": 0,            # 深度分析总数
    "done": 0,             # 已完成数
    "current": "",         # 正在分析的股票名
    "picks": [],           # 结果(全部深度分析, 前端再按 action 分组)
    "candidates": 0,       # 粗筛前候选数
    "started_at": "",
    "finished_at": "",
    "error": "",
}
_scan_lock = _threading.Lock()


def _fetch_candidates(limit: int = 40) -> List[Dict]:
    """全市场候选池: 东财涨幅榜 top(主源) + 涨停池(加权)。
    过滤 ST/退市/北交/低价/低成交额。返回 [{symbol,name,price,change_pct,amount}]。"""
    out, seen = [], set()
    # 1) 东财涨幅榜(全市场按涨幅倒序), 走 curl_cffi 指纹
    try:
        from src.data.em_client import _cffi, _HAS_CFFI, UA
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1, "pz": max(limit * 2, 60), "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 沪深主板/创业
            "fields": "f2,f3,f6,f12,f13,f14",
        }
        headers = {"Referer": "https://quote.eastmoney.com/", "User-Agent": UA}
        if _HAS_CFFI:
            r = _cffi.get(url, params=params, impersonate="chrome", headers=headers, timeout=8)
        else:
            import requests as _rq
            r = _rq.get(url, params=params, headers=headers, timeout=8)
        diff = (r.json().get("data") or {}).get("diff") or []
        rows = diff.values() if isinstance(diff, dict) else diff
        for it in rows:
            code = str(it.get("f12", "")).strip()
            name = str(it.get("f14", "")).strip()
            if not (code.isdigit() and len(code) == 6):
                continue
            mkt = "sh" if it.get("f13") == 1 else "sz"
            sym = mkt + code
            price = float(it.get("f2", 0) or 0)
            change = float(it.get("f3", 0) or 0)   # fltt=2 已是真实百分比
            amount = float(it.get("f6", 0) or 0)
            # 过滤: ST/退市名、价格过低、成交额<2亿(流动性)
            if "ST" in name.upper() or "退" in name:
                continue
            if price < 2 or amount < 2e8:
                continue
            if sym in seen:
                continue
            seen.add(sym)
            out.append({"symbol": sym, "name": name, "price": price,
                        "change_pct": change, "amount": amount})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _prescreen(candidates: List[Dict], top: int = 6) -> List[Dict]:
    """规则粗筛: 用实时行情的量价指标给候选打分, 取头部(省 LLM 调用)。"""
    syms = [c["symbol"] for c in candidates]
    if not syms:
        return []
    quotes = realtime.get_quotes(syms, sources=['tencent', 'sina']) or {}
    scored = []
    for c in candidates:
        q = quotes.get(c["symbol"], {}) or {}
        change_pct = float(q.get('change_pct', c.get('change_pct', 0)) or 0)
        vol_ratio = float(q.get('vol_ratio', 0) or 0)
        turnover = float(q.get('turnover_pct', q.get('turnover', 0)) or 0)
        # 偏好: 涨幅温和(不追天板)、量比放大、换手活跃
        momentum = change_pct if change_pct <= 7 else 7 - (change_pct - 7) * 0.5
        score = momentum * 0.16 + max(vol_ratio - 1, 0) * 0.18 + turnover * 0.02
        c = dict(c)
        c["score"] = round(score, 3)
        c["quote"] = q
        scored.append(c)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


def _analyze_one(sym: str, quote: Dict, snap: Dict) -> Dict:
    """对单只候选跑完整多智能体深度分析, 返回结构化 pick(含建议数量)。"""
    bundle = _get_ai_graph()
    graph = bundle["graph"]
    position = snap["pos_map"].get(sym)
    price = float(quote.get('price', 0) or 0)
    try:
        decision = graph.analyze(sym, {
            "symbol": sym,
            "price": price,
            "change_pct": quote.get('change_pct', 0),
            "position": int(getattr(position, 'quantity', 0) or 0) if position else 0,
            "risk": {
                "drawdown": risk_mgr.get_risk_report().get('drawdown', 0),
                "total_position_pct": snap["total_position_pct"],
                "daily_pnl": risk_mgr.daily_pnl,
            },
            "trade_date": datetime.now().strftime('%Y%m%d'),
        }).to_dict()
    except Exception as exc:
        return {"symbol": sym, "name": quote.get('name') or COMMON_SYMBOL_NAMES.get(sym, sym),
                "action": "hold", "confidence": 0, "reason": f"AI分析失败: {exc}",
                "source": "error", "price": price, "suggested_qty": 0}

    # 建议买入数量(风控口径, 1% 单笔风险), 只对 buy 计算
    suggested_qty = 0
    if decision.get('action') == 'buy' and price > 0:
        try:
            raw = risk_mgr.calculate_position_size_with_risk(price, snap["total_asset"], 0.01)
            suggested_qty = max(0, int(raw) // 100 * 100)
        except Exception:
            suggested_qty = 0
    return {
        "symbol": sym,
        "name": quote.get('name') or COMMON_SYMBOL_NAMES.get(sym, sym),
        "price": price,
        "change_pct": float(quote.get('change_pct', 0) or 0),
        "action": decision.get('action', 'hold'),
        "confidence": decision.get('confidence', 0),
        "reason": decision.get('reason', ''),
        "suggested_qty": suggested_qty,
        "source": "agents",
        "risk": decision.get('risk', {}),
        "analysts": decision.get('analysts', []),
        "debate": decision.get('debate', {}),
        "trader": decision.get('trader', {}),
    }


def _run_scan(pool: str, top: int):
    """后台线程: 候选→粗筛→并发深度分析→写 _scan_job。"""
    try:
        if pool == "watchlist":
            syms = _get_watchlist()
            quotes = realtime.get_quotes(syms, sources=['tencent', 'sina']) or {}
            shortlist = [{"symbol": s, "name": quotes.get(s, {}).get('name', s),
                          "quote": quotes.get(s, {})} for s in syms][:max(top, 6)]
        else:
            candidates = _fetch_candidates(limit=40)
            with _scan_lock:
                _scan_job["candidates"] = len(candidates)
            shortlist = _prescreen(candidates, top=top)

        snap = _portfolio_snapshot()
        with _scan_lock:
            _scan_job["total"] = len(shortlist)
            _scan_job["done"] = 0
        if not shortlist:
            with _scan_lock:
                _scan_job["status"] = "done"
                _scan_job["finished_at"] = datetime.now().isoformat()
            return

        # 并发深度分析(每只 ~25s, 4 路并发把总时长压到 ~top/4 * 25s)
        picks = [None] * len(shortlist)

        def _work(idx_item):
            idx, item = idx_item
            sym = item["symbol"]
            with _scan_lock:
                _scan_job["current"] = item.get("name") or sym
            q = item.get("quote") or realtime.get_quotes([sym], sources=['tencent']).get(sym, {})
            res = _analyze_one(sym, q, snap)
            with _scan_lock:
                _scan_job["done"] += 1
            picks[idx] = res

        with _Pool(max_workers=4) as ex:
            list(ex.map(_work, list(enumerate(shortlist))))

        picks = [p for p in picks if p]
        # buy 优先并按置信度排序, 其余(hold/sell)置后
        picks.sort(key=lambda p: (p.get('action') == 'buy', p.get('confidence', 0)), reverse=True)
        with _scan_lock:
            _scan_job["picks"] = picks
            _scan_job["current"] = ""
            _scan_job["status"] = "done"
            _scan_job["finished_at"] = datetime.now().isoformat()
    except Exception as exc:
        with _scan_lock:
            _scan_job["status"] = "error"
            _scan_job["error"] = str(exc)
            _scan_job["finished_at"] = datetime.now().isoformat()


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """返回前端 HTML 页面"""
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    with open(html_path, 'r', encoding='utf-8') as f:
        return f.read()


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/hybridaction/zybTrackerStatisticsAction")
async def zyb_tracker_statistics(__callback__: Optional[str] = Query(None)):
    payload = {"ok": True}
    if __callback__:
        return Response(
            content=f"{__callback__}({json.dumps(payload, ensure_ascii=False)})",
            media_type="application/javascript; charset=utf-8",
        )
    return JSONResponse(payload)


@app.get("/api/quotes")
def get_quotes(symbols: Optional[str] = Query(None)):
    """获取实时行情数据。无参时默认返回自选股(核心池)。"""
    if symbols:
        sym_list = [_normalize_symbol(s) for s in symbols.split(',') if s.strip()]
    else:
        sym_list = _get_watchlist()

    # 腾讯源优先（带完整字段含 name/PE/PB），pytdx name 有编码问题
    sources = ['tencent', 'sina', 'eastmoney']
    quotes = realtime.get_quotes(sym_list, sources=sources)

    # 更新券商价格
    for symbol, quote in (quotes or {}).items():
        price = quote.get('price')
        if price:
            broker.update_market_price(symbol, float(price))

    return JSONResponse(quotes or {})


@app.get("/api/watchlist")
def get_watchlist():
    """自选股(核心池) + 实时行情。前端行情列表/首页默认显示这些。"""
    syms = _get_watchlist()
    quotes = realtime.get_quotes(syms, sources=['tencent', 'sina', 'eastmoney']) if syms else {}
    for symbol, quote in (quotes or {}).items():
        price = quote.get('price')
        if price:
            broker.update_market_price(symbol, float(price))
    items = []
    for sym in syms:
        q = quotes.get(sym, {}) or {}
        items.append({
            "symbol": sym,
            "name": q.get('name') or COMMON_SYMBOL_NAMES.get(sym, sym),
            "price": float(q.get('price', 0) or 0),
            "change_pct": float(q.get('change_pct', 0) or 0),
            "amount": float(q.get('amount', 0) or 0),
        })
    return JSONResponse({"symbols": syms, "items": items, "quotes": quotes or {}})


@app.post("/api/watchlist/add")
def add_watchlist(body: dict):
    """加入自选。body: {symbol} 或 {query}(名称/拼音/代码)。"""
    raw = str(body.get('symbol') or body.get('query') or '').strip()
    if not raw:
        return JSONResponse({"success": False, "error": "请输入股票代码或名称"})
    sym, name = _resolve_symbol_name(raw)
    if not sym:
        return JSONResponse({"success": False, "error": f"无法识别「{raw}」"})
    wl = _get_watchlist()
    # 若当前自选为空回退池, 首次添加时以回退池为基础再追加
    if sym in wl:
        return JSONResponse({"success": True, "already": True, "symbol": sym, "name": name, "symbols": wl})
    wl = wl + [sym]
    saved = _save_watchlist(wl)
    return JSONResponse({"success": True, "symbol": sym, "name": name, "symbols": saved})


@app.post("/api/watchlist/remove")
def remove_watchlist(body: dict):
    """移出自选。body: {symbol}。"""
    sym = _normalize_symbol(str(body.get('symbol', '')).strip())
    if not sym:
        return JSONResponse({"success": False, "error": "缺少 symbol"})
    wl = [s for s in _get_watchlist() if s != sym]
    saved = _save_watchlist(wl)
    return JSONResponse({"success": True, "symbol": sym, "symbols": saved})


@app.get("/api/kline")
def get_kline(
    symbol: str = Query("sh600000"),
    period: str = Query("day"),
    count: int = Query(60),
):
    """获取K线数据"""
    symbol = _normalize_symbol(symbol)

    # 先尝试 mootdx (TCP, 最稳定)
    cat_map = {'day': 4, 'week': 5, 'month': 6, '60min': 11, '30min': 10, '5min': 8, '15min': 9}
    df = realtime.get_kline_mootdx(symbol, category=cat_map.get(period, 4), offset=count)

    if not df.empty:
        # mootdx 返回可能有重复列和多余列，清理
        df = df.loc[:, ~df.columns.duplicated()]
        keep = ['open', 'close', 'high', 'low', 'volume', 'amount']
        df = df[[c for c in keep if c in df.columns]]

    if df.empty:
        # 回退到东财 HTTP
        df = realtime.get_kline_data(symbol, period=period, count=count)

    if df.empty:
        return JSONResponse([])

    # 标准化列名（mootdx 和 eastmoney 列名不同）
    col_map = {}
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ('open', 'close', 'high', 'low', 'volume', 'amount'):
            col_map[col] = cl

    result = []
    for idx, row in df.iterrows():
        date_str = ""
        if hasattr(idx, 'strftime'):
            date_str = idx.strftime('%Y-%m-%d')
        elif hasattr(idx, 'split'):
            date_str = str(idx).split(' ')[0][:10]
        else:
            date_str = str(idx)[:10]

        def _get(key, default=0):
            mapped = col_map.get(key, key)
            val = row.get(mapped, row.get(key, default))
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        result.append({
            "date": date_str,
            "open": round(_get('open'), 2),
            "close": round(_get('close'), 2),
            "high": round(_get('high'), 2),
            "low": round(_get('low'), 2),
            "volume": int(_get('volume') or 0),
            "amount": round(_get('amount'), 2),
        })
    return JSONResponse(result)


@app.get("/api/timeshare")
def get_timeshare(symbol: str = Query("sh600000")):
    """当日分时数据(实时)。腾讯 minute 接口, 每点=分钟。
    返回 pre_close(昨收, 涨跌基准) + points[{time,price,avg,volume}]。
    price=当时价, avg=均价(累计额/累计量), volume=当分钟增量成交量(手)。
    非交易日/盘前返回 available=False。"""
    symbol = _normalize_symbol(symbol)
    code = symbol  # 腾讯 minute 接口用 sh600000 / sz000001 原样
    try:
        from src.data.em_client import _cffi, _HAS_CFFI, UA
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
        if _HAS_CFFI:
            r = _cffi.get(url, params={"code": code}, impersonate="chrome",
                          headers={"Referer": "https://gu.qq.com/"}, timeout=8)
        else:
            import requests as _rq
            r = _rq.get(url, params={"code": code},
                        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=8)
        d = r.json()
    except Exception as exc:
        return JSONResponse({"symbol": symbol, "available": False, "error": str(exc)})

    node = (d.get("data") or {}).get(code) or {}
    qt = ((node.get("qt") or {}).get(code)) or []
    raw = ((node.get("data") or {}).get("data")) or []
    if not raw:
        return JSONResponse({"symbol": symbol, "available": False})

    def _f(v, dft=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return dft

    pre_close = _f(qt[4]) if len(qt) > 4 else 0.0
    name = qt[1] if len(qt) > 1 else ""
    points = []
    prev_vol = 0.0
    for line in raw:
        parts = str(line).split(" ")
        if len(parts) < 4:
            continue
        t, price, cum_vol, cum_amt = parts[0], _f(parts[1]), _f(parts[2]), _f(parts[3])
        avg = (cum_amt / (cum_vol * 100)) if cum_vol > 0 else price
        minute_vol = max(cum_vol - prev_vol, 0.0)
        prev_vol = cum_vol
        hhmm = t.zfill(4)
        points.append({
            "time": f"{hhmm[:2]}:{hhmm[2:]}",
            "price": round(price, 3),
            "avg": round(avg, 3),
            "volume": int(minute_vol),
        })
    if not points:
        return JSONResponse({"symbol": symbol, "available": False})
    return JSONResponse({
        "symbol": symbol,
        "name": name,
        "available": True,
        "pre_close": round(pre_close, 3),
        "points": points,
    })


@app.get("/api/account")
def get_account():
    """获取账户信息"""
    info = broker.get_account_info()
    return JSONResponse({
        "total_asset": float(info.get('total_asset', 0) or 0),
        "initial_capital": float(info.get('initial_capital', broker.initial_capital)),
        "cash": float(info.get('cash', 0) or 0),
        "profit": float(info.get('profit', 0) or 0),
        "profit_pct": float(info.get('profit_pct', 0) or 0),
        "positions": len(broker.get_positions()),
        "commission_rate": broker.commission_rate,
        "commission_rate_wan": round(broker.commission_rate * 10000, 1),
        "stamp_tax_rate": broker.stamp_tax_rate,
        "stamp_tax_qian": round(broker.stamp_tax_rate * 1000, 1),
        "min_commission": broker.min_commission,
        "slippage": broker.slippage if hasattr(broker, 'slippage') else 0.0001,
        "slippage_wan": round((broker.slippage if hasattr(broker, 'slippage') else 0.0001) * 10000, 1),
        "market_value": float(info.get('market_value', 0) or 0),
    })


@app.post("/api/account/update")
def update_account(body: dict):
    """
    更新账户设置（初始资金、佣金、印花税、滑点等）
    重置券商状态，所有持仓和订单清零，按新参数重新初始化
    """
    try:
        initial_capital = float(body.get('initial_capital', broker.initial_capital))
        commission_rate = float(body.get('commission_rate', broker.commission_rate))
        stamp_tax_rate = float(body.get('stamp_tax_rate', broker.stamp_tax_rate))
        min_commission = float(body.get('min_commission', broker.min_commission))
        slippage = float(body.get('slippage', 0.0001))

        # 校验
        if initial_capital < 10000:
            return JSONResponse({"success": False, "error": "初始资金不能低于 10,000 元"})
        if commission_rate < 0:
            return JSONResponse({"success": False, "error": "佣金费率不能为负"})
        if stamp_tax_rate < 0:
            return JSONResponse({"success": False, "error": "印花税率不能为负"})
        if min_commission < 0:
            return JSONResponse({"success": False, "error": "最低佣金不能为负"})

        # 重建 broker（重置所有持仓和订单）
        broker.initial_capital = initial_capital
        broker.cash = initial_capital
        broker.commission_rate = commission_rate
        broker.stamp_tax_rate = stamp_tax_rate
        broker.min_commission = min_commission
        broker.slippage = slippage
        broker.positions = {}
        broker.orders = []
        broker.order_history = []
        broker.trade_history = []

        # 同步写入 config.yaml（持久化）
        config.set('trading.initial_capital', int(initial_capital))
        config.set('commission.rate', commission_rate)
        config.set('commission.min', int(min_commission))
        config.set('commission.stamp_tax', stamp_tax_rate)
        config.set('trading.slippage', slippage)
        config.save_config(CONFIG_PATH)

        info = broker.get_account_info()
        return JSONResponse({
            "success": True,
            "account": {
                "total_asset": float(info.get('total_asset', 0) or 0),
                "initial_capital": float(info.get('initial_capital', initial_capital)),
                "cash": float(info.get('cash', 0) or 0),
                "profit": 0.0,
                "profit_pct": 0.0,
                "positions": 0,
                "commission_rate": commission_rate,
                "commission_rate_wan": round(commission_rate * 10000, 1),
                "stamp_tax_rate": stamp_tax_rate,
                "stamp_tax_qian": round(stamp_tax_rate * 1000, 1),
                "min_commission": min_commission,
                "slippage": slippage,
                "slippage_wan": round(slippage * 10000, 1),
                "market_value": 0.0,
            }
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/strategies")
def get_strategies():
    """获取策略运行数据"""
    symbols = config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002'])
    # 腾讯源优先（带完整字段含 name/PE/PB），pytdx name 有编码问题
    sources = ['tencent', 'sina', 'eastmoney']
    quotes = realtime.get_quotes([_normalize_symbol(s) for s in symbols], sources=sources)

    strategies = []
    strategy_names = {
        'sh600000': 'CrossMA',
        'sz000001': 'Momentum',
        'sh600104': 'MeanReversion',
        'sz000002': 'MultiFactor',
    }

    for i, sym in enumerate(symbols[:4]):
        sym = _normalize_symbol(sym)
        q = quotes.get(sym, {})
        change_pct = float(q.get('change_pct', 0) or 0)
        name = strategy_names.get(sym, f'Strategy_{i+1}')
        strategies.append({
            "name": name,
            "symbol": sym,
            "pnl_pct": round(change_pct, 2),
            "win_rate": round(0.5 + change_pct * 0.01, 3) if change_pct else 0.5,
            "max_dd": round(-abs(change_pct) * 0.5, 2) if change_pct else -0.5,
            "run_hours": 24 + i * 24,
            "signal": "买入" if change_pct > 0 else ("卖出" if change_pct < -1 else "观望"),
            "action": "buy" if change_pct > 0 else ("sell" if change_pct < -1 else "hold"),
        })

    return JSONResponse(strategies)


@app.get("/api/ai_signals")
def get_ai_signals():
    """获取轻量 AI 信号。默认不触发大模型，保证首页刷新快速稳定。"""
    symbols = [_normalize_symbol(s) for s in config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002'])]
    quotes = realtime.get_quotes(symbols, sources=['tencent', 'sina', 'eastmoney'])
    return JSONResponse(_build_rule_ai_signals(symbols, quotes))


@app.post("/api/ai_analyze")
def ai_analyze():
    """触发多智能体分析。失败时返回规则兜底信号，前端始终可展示。"""
    symbols = [_normalize_symbol(s) for s in config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002'])]
    quotes = realtime.get_quotes(symbols, sources=['tencent', 'sina', 'eastmoney'])
    try:
        signals = _build_agent_ai_signals(symbols, quotes)
        source = "agents"
    except Exception as exc:
        signals = _build_rule_ai_signals(symbols, quotes)
        source = "rule_fallback"
        return JSONResponse({"signals": signals, "source": source, "error": str(exc)})
    return JSONResponse({"signals": signals, "source": source, "generated_at": datetime.now().isoformat()})


@app.post("/api/ai_pick/start")
def ai_pick_start(body: dict = None):
    """启动 AI 自动选股(后台漏斗任务)。body: {pool:'market'|'watchlist', top:int}。
    已在运行则返回 busy。前端随后轮询 /api/ai_pick/status。"""
    body = body or {}
    pool = str(body.get('pool', 'market')).lower()
    if pool not in ('market', 'watchlist'):
        pool = 'market'
    try:
        top = int(body.get('top', 5))
    except (TypeError, ValueError):
        top = 5
    top = max(1, min(top, 8))

    with _scan_lock:
        if _scan_job["status"] == "running":
            return JSONResponse({"success": False, "busy": True,
                                 "message": "已有一轮选股在进行中",
                                 "done": _scan_job["done"], "total": _scan_job["total"]})
        _scan_job.update({
            "status": "running", "pool": pool, "total": 0, "done": 0,
            "current": "", "picks": [], "candidates": 0,
            "started_at": datetime.now().isoformat(), "finished_at": "", "error": "",
        })

    t = _threading.Thread(target=_run_scan, args=(pool, top), daemon=True)
    t.start()
    return JSONResponse({"success": True, "pool": pool, "top": top})


@app.get("/api/ai_pick/status")
def ai_pick_status():
    """AI 选股进度 + 结果快照。前端每 ~2s 轮询。"""
    with _scan_lock:
        job = dict(_scan_job)
    buys = [p for p in job.get("picks", []) if p.get("action") == "buy"]
    return JSONResponse({
        "status": job["status"],
        "pool": job["pool"],
        "total": job["total"],
        "done": job["done"],
        "current": job["current"],
        "candidates": job["candidates"],
        "picks": job["picks"],
        "buy_count": len(buys),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
    })


@app.post("/api/ai_pick/execute")
def ai_pick_execute(body: dict):
    """一键确认买入某个 AI 推荐。复用硬风控 + 模拟盘 place_order。"""
    symbol = _normalize_symbol(str(body.get('symbol', '')))
    if not symbol:
        return JSONResponse({"success": False, "error": "缺少 symbol"})
    # 优先用推荐里的建议数量; 未传则取实时行情价
    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {})
    price = float(body.get('price') or quote.get('price') or 0)
    if price <= 0:
        return JSONResponse({"success": False, "error": "没有可用行情价格，无法下单"})
    quantity = int(body.get('quantity') or 0)
    if quantity <= 0:
        # 回退到风控口径的建议数量
        snap = _portfolio_snapshot()
        try:
            raw = risk_mgr.calculate_position_size_with_risk(price, snap["total_asset"], 0.01)
            quantity = max(0, int(raw) // 100 * 100)
        except Exception:
            quantity = 0
    if quantity <= 0:
        return JSONResponse({"success": False, "error": "未能得到有效买入数量"})
    return place_order({
        "symbol": symbol, "side": "buy", "quantity": quantity,
        "price": price, "reason": "ai_pick",
    })


@app.get("/api/positions")
def get_positions():
    """获取模拟账户持仓。"""
    return JSONResponse([p.to_dict() for p in broker.get_positions()])


@app.get("/api/orders")
def get_orders():
    """获取模拟账户订单历史。"""
    df = broker.get_order_history()
    if df.empty:
        return JSONResponse([])
    return JSONResponse(json.loads(df.to_json(orient="records", force_ascii=False)))


@app.post("/api/order")
def place_order(body: dict):
    """模拟下单入口。所有请求先经过硬风控，不连接真实券商。"""
    try:
        symbol = _normalize_symbol(str(body.get('symbol', '')))
        side = str(body.get('side', body.get('direction', 'buy'))).lower()
        if side not in ('buy', 'sell'):
            return JSONResponse({"success": False, "error": "side 必须是 buy 或 sell"})
        quantity = int(body.get('quantity', 0))
        price = float(body.get('price', 0))
        reason = str(body.get('reason', 'api_order'))

        check = _pre_trade_check(symbol, side, quantity, price, reason)
        if not check.get('allowed'):
            return JSONResponse({"success": False, "error": check.get('reason'), "risk": check})

        if hasattr(broker, 'update_market_price'):
            broker.update_market_price(symbol, price)
        order = Order(
            symbol=symbol,
            direction=OrderDirection.BUY if side == 'buy' else OrderDirection.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            price=price,
        )
        order_id = broker.place_order(order)
        status = broker.get_order_status(order_id)
        if status.get('status') == 'rejected':
            return JSONResponse({"success": False, "error": "券商拒单", "order_id": order_id, "status": status})
        risk_mgr.record_order({"symbol": symbol, "side": side, "quantity": quantity, "price": price})
        return JSONResponse({"success": True, "order_id": order_id, "status": status, "account": broker.get_account_info()})
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)})


_SUGGEST_CACHE: Dict[str, tuple] = {}


def _suggest_symbols(query: str, limit: int = 10) -> List[Dict]:
    """用东财 suggest 接口做「名称/拼音/代码 -> A股代码」的全库解析。

    覆盖全部 A 股(不再局限于硬编码 8 只)。返回 [{symbol, name}]。
    走 em_get(curl_cffi Chrome 指纹)避免被封;60s 缓存;失败返回 []。
    """
    key = query.strip().lower()
    if not key:
        return []
    hit = _SUGGEST_CACHE.get(key)
    if hit and (datetime.now().timestamp() - hit[1]) < 60:
        return hit[0]
    try:
        from src.data.em_client import em_get
        r = em_get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={"input": query, "type": "14",
                    "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": str(limit)},
            headers={"Referer": "https://www.eastmoney.com/"},
            timeout=6,
        )
        data = (r.json().get("QuotationCodeTable") or {}).get("Data") or []
    except Exception:
        return []
    out = []
    for it in data:
        code = str(it.get("Code", "")).strip()
        name = str(it.get("Name", "")).strip()
        quote_id = str(it.get("QuoteID", "")).strip()   # 形如 "1.600519" / "0.002594"
        if not (code.isdigit() and len(code) == 6):
            continue
        # 只收 A 股主板/创业/科创/北交(6/0/3/8/4 开头), 用 QuoteID 前缀定市场
        if quote_id.startswith("1."):
            out.append({"symbol": "sh" + code, "name": name})
        elif quote_id.startswith("0."):
            out.append({"symbol": "sz" + code, "name": name})
        elif code[0] in ("6", "9"):
            out.append({"symbol": "sh" + code, "name": name})
        elif code[0] in ("0", "3"):
            out.append({"symbol": "sz" + code, "name": name})
        elif code[0] in ("4", "8"):
            out.append({"symbol": "bj" + code, "name": name})
    out = out[:limit]
    if out:
        _SUGGEST_CACHE[key] = (out, datetime.now().timestamp())
    return out


@app.get("/api/search")
def search_symbols(q: str = Query("")):
    """按代码或名称搜索股票，并返回可直接填入交易单的行情。

    支持全部 A 股: 先用东财 suggest 做名称/拼音/代码全库解析,
    再回退到本地配置池 + 常用股, 最后按纯代码直连。
    """
    query = q.strip()
    configured = [_normalize_symbol(s) for s in config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002'])]
    universe = list(dict.fromkeys(configured + list(COMMON_SYMBOL_NAMES.keys())))

    name_hint: Dict[str, str] = {}
    if query:
        # 1) 东财 suggest 全库解析(名称/拼音/代码都能命中)
        suggested = _suggest_symbols(query, limit=10)
        candidates = []
        for item in suggested:
            sym = _normalize_symbol(item["symbol"])
            candidates.append(sym)
            if item.get("name"):
                name_hint[sym] = item["name"]

        # 2) 本地池按名称/代码兜底
        query_lower = query.lower()
        for sym in universe:
            if query_lower in sym.lower() or query in COMMON_SYMBOL_NAMES.get(sym, ""):
                candidates.append(sym)

        # 3) 纯代码直连兜底
        if any(ch.isdigit() for ch in query):
            candidates.append(_normalize_symbol(query.lower()))

        if not candidates:
            candidates = [_normalize_symbol(query.lower())]
    else:
        candidates = universe[:8]

    candidates = list(dict.fromkeys([_normalize_symbol(sym) for sym in candidates if sym]))[:10]
    quotes = realtime.get_quotes(candidates, sources=['tencent', 'sina', 'eastmoney']) if candidates else {}
    results = []
    for sym in candidates:
        quote = quotes.get(sym, {}) or {}
        price = float(quote.get('price', 0) or 0)
        if price:
            broker.update_market_price(sym, price)
        results.append({
            "symbol": sym,
            "name": quote.get('name') or name_hint.get(sym) or COMMON_SYMBOL_NAMES.get(sym, sym),
            "price": price,
            "change_pct": float(quote.get('change_pct', 0) or 0),
            "quote": quote,
        })
    return JSONResponse({"query": query, "results": results, "quotes": quotes or {}})


@app.get("/api/risk")
def get_risk():
    """返回当前模拟账户的风险摘要。"""
    snap = _portfolio_snapshot()
    report = risk_mgr.get_risk_report()
    report.update({
        "total_asset": snap["total_asset"],
        "cash": snap["cash"],
        "market_value": snap["market_value"],
        "total_position_pct": snap["total_position_pct"],
        "cash_pct": snap["cash"] / max(snap["total_asset"], 1),
        "position_count": len(snap["positions"]),
        "positions": [p.to_dict() for p in snap["positions"]],
    })
    return JSONResponse(report)


_capital_fetcher = None


def _get_capital_fetcher():
    global _capital_fetcher
    if _capital_fetcher is None:
        from src.data.capital_flow import CapitalFlowFetcher
        _capital_fetcher = CapitalFlowFetcher()
    return _capital_fetcher


@app.get("/api/capital")
def get_capital(symbol: str = Query("sh600000")):
    """个股资金面: 主力/超大单净流入(元)。非交易时段可能返回空。
    只做一次 fund-flow 分钟线请求(curl_cffi ~0.3s), 不再串联涨停池/北向, 保证接口快。"""
    symbol = _normalize_symbol(symbol)
    fetcher = _get_capital_fetcher()
    try:
        summary = fetcher.get_main_net_summary(symbol)
    except Exception as exc:
        return JSONResponse({"symbol": symbol, "available": False, "error": str(exc)})
    if not summary:
        return JSONResponse({"symbol": symbol, "available": False, "summary": "(非交易时段或暂无资金流数据)"})
    main_net = summary.get("total_main_net", 0)
    super_net = summary.get("total_super_net", 0)
    text = f"主力今日净{'流入' if main_net >= 0 else '流出'}{abs(main_net) / 1e4:.0f}万 | 超大单净{super_net / 1e4:+.0f}万"
    return JSONResponse({
        "symbol": symbol,
        "available": True,
        "main_net": main_net,
        "super_net": super_net,
        "last_main_net": summary.get("last_main_net", 0),
        "direction": summary.get("direction", ""),
        "summary": text,
    })


@app.get("/api/market_sentiment")
def get_market_sentiment():
    """市场情绪: 涨停数/炸板率/最高连板 + 北向资金。非交易时段返回空占位。"""
    fetcher = _get_capital_fetcher()
    date = datetime.now().strftime('%Y%m%d')
    try:
        stats = fetcher.get_sentiment_stats(date)
        north = fetcher.get_north_flow()
    except Exception as exc:
        return JSONResponse({"available": False, "error": str(exc)})
    if not stats and not north:
        return JSONResponse({"available": False, "date": date})
    payload = {"available": True, "date": date}
    if stats:
        payload.update({
            "limit_up_count": stats.get("limit_up_count", 0),
            "broken_count": stats.get("broken_count", 0),
            "break_rate": stats.get("break_rate", 0),
            "max_limit_days": stats.get("max_limit_days", 0),
        })
    if north:
        payload["north_net"] = north.get("north_net", 0)
        payload["north_direction"] = north.get("direction", "")
    return JSONResponse(payload)


@app.get("/api/backtest")
def run_backtest(
    symbol: str = Query("sh600000"),
    fast: int = Query(5, ge=2, le=60),
    slow: int = Query(20, ge=3, le=120),
    count: int = Query(180, ge=40, le=360),
):
    """简易双均线回测，给前端回测页提供可操作结果。"""
    symbol = _normalize_symbol(symbol)
    if fast >= slow:
        fast = max(2, slow // 2)

    df = realtime.get_kline_mootdx(symbol, category=4, offset=count)
    if df.empty:
        df = realtime.get_kline_data(symbol, period='day', count=count)
    if df.empty or 'close' not in [c.lower() for c in df.columns]:
        return JSONResponse({
            "symbol": symbol,
            "strategy": "双均线",
            "return_pct": 0,
            "max_drawdown": 0,
            "win_rate": 0,
            "trades": [],
            "equity": [],
            "message": "暂无可回测的K线数据",
        })

    df = df.loc[:, ~df.columns.duplicated()].copy()
    lower_cols = {c.lower(): c for c in df.columns}
    close_col = lower_cols.get('close')
    open_col = lower_cols.get('open', close_col)
    closes = [float(v or 0) for v in df[close_col].tolist()]
    opens = [float(v or 0) for v in df[open_col].tolist()]
    dates = []
    for idx in df.index:
        if hasattr(idx, 'strftime'):
            dates.append(idx.strftime('%Y-%m-%d'))
        else:
            dates.append(str(idx).split(' ')[0][:10])

    cash = 100000.0
    position = 0
    entry_price = 0.0
    trades = []
    equity = []
    peak = cash
    max_dd = 0.0

    def ma(values, end, window):
        if end + 1 < window:
            return None
        part = values[end - window + 1:end + 1]
        return sum(part) / len(part)

    for i in range(1, len(closes)):
        fast_now = ma(closes, i, fast)
        slow_now = ma(closes, i, slow)
        fast_prev = ma(closes, i - 1, fast)
        slow_prev = ma(closes, i - 1, slow)
        price = closes[i]
        if None not in (fast_now, slow_now, fast_prev, slow_prev):
            crossed_up = fast_prev <= slow_prev and fast_now > slow_now
            crossed_down = fast_prev >= slow_prev and fast_now < slow_now
            if crossed_up and position == 0 and price > 0:
                position = int(cash / price / 100) * 100
                if position > 0:
                    entry_price = price
                    cash -= position * price
                    trades.append({"date": dates[i], "side": "buy", "price": round(price, 2), "quantity": position})
            elif crossed_down and position > 0:
                cash += position * price
                pnl_pct = (price - entry_price) / max(entry_price, 1) * 100
                trades.append({"date": dates[i], "side": "sell", "price": round(price, 2), "quantity": position, "pnl_pct": round(pnl_pct, 2)})
                position = 0
                entry_price = 0.0
        value = cash + position * price
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / max(peak, 1))
        equity.append({"date": dates[i], "value": round(value, 2), "price": round(price, 2)})

    if position > 0 and closes:
        price = closes[-1]
        cash += position * price
        pnl_pct = (price - entry_price) / max(entry_price, 1) * 100
        trades.append({"date": dates[-1], "side": "sell", "price": round(price, 2), "quantity": position, "pnl_pct": round(pnl_pct, 2)})

    final_value = cash
    sell_trades = [t for t in trades if t.get('side') == 'sell']
    wins = len([t for t in sell_trades if t.get('pnl_pct', 0) > 0])
    win_rate = wins / len(sell_trades) if sell_trades else 0
    return JSONResponse({
        "symbol": symbol,
        "strategy": f"双均线 MA{fast}/MA{slow}",
        "return_pct": round((final_value - 100000) / 100000 * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "win_rate": round(win_rate * 100, 1),
        "trades": trades[-20:],
        "equity": equity[-120:],
        "last_price": round(closes[-1], 2) if closes else 0,
    })


@app.post("/api/ai_trade")
def ai_trade(body: dict):
    """把一条 AI 信号转换成模拟订单。仍然只进入模拟券商和硬风控。"""
    symbol = _normalize_symbol(str(body.get('symbol', '')))
    side = str(body.get('side', body.get('action', 'hold'))).lower()
    if side not in ('buy', 'sell'):
        return JSONResponse({"success": False, "error": "AI 信号不是买入或卖出"})

    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {}) if symbol else {}
    price = float(body.get('price') or quote.get('price') or 0)
    if price <= 0:
        return JSONResponse({"success": False, "error": "没有可用行情价格，无法下单"})

    snap = _portfolio_snapshot()
    quantity = int(body.get('quantity') or 0)
    if quantity <= 0:
        if side == 'buy':
            quantity = risk_mgr.calculate_position_size_with_risk(price, snap["total_asset"], 0.01)
            quantity = max(0, quantity // 100 * 100)
        else:
            position = snap["pos_map"].get(symbol)
            quantity = int(getattr(position, 'quantity', 0) or 0) if position else 0
            quantity = max(0, quantity // 100 * 100)
    if quantity <= 0:
        return JSONResponse({"success": False, "error": "AI 未能得到有效交易数量"})

    return place_order({
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "reason": "ai_trade",
    })


def main():
    port = config.get('server.port', 8080)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
