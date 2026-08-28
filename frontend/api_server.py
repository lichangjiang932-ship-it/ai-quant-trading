"""
AI量化交易平台 — FastAPI 后端数据服务
为前端 dashboard 提供实时行情、K线、策略、AI信号等数据接口
"""
import sys
import os
import json
import asyncio
import warnings
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as datetime_time
from functools import wraps
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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
from src.execution.trade_approval import ApprovalPolicy, TradeApprovalGate
from src.execution.a_share_rules import (
    backtest_trade_rejection,
    buy_quantity_for_amount,
    estimate_buy_cost,
    instrument_type,
    market_session,
    validate_quantity,
)
from src.analysis.opportunity import OpportunityConfig, OpportunityScorer
from src.analysis.entry_guard import EntryGuard, EntryGuardConfig
from src.analysis.holding_recovery import HoldingRecoveryAnalyzer, HoldingRecoveryConfig
from src.analysis.premarket import (
    PremarketAnalyzer,
    PremarketConfig,
    is_trading_day,
    target_trading_date,
)
from src.analysis.professional import ProfessionalDecisionLayer
from src.backtest.backtester import Backtester
from src.strategies.potential_strategy import PotentialStrategy
from src.utils.config import Config
from src.utils.state_manager import StateManager

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.yaml")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    global _premarket_scheduler_task, _autotrade_task, _news_factor_task
    if bool(config.get('premarket.scheduler_enabled', True)) and _premarket_scheduler_task is None:
        _premarket_scheduler_task = asyncio.create_task(_premarket_scheduler_loop())
    if _autotrade_task is None:
        _autotrade_task = asyncio.create_task(_autotrade_loop())
    if _news_factor_task is None:
        _news_factor_task = asyncio.create_task(_news_factor_loop())
    _ensure_strategy_refresh('potential')
    try:
        yield
    finally:
        if _premarket_scheduler_task is not None:
            _premarket_scheduler_task.cancel()
            try:
                await _premarket_scheduler_task
            except asyncio.CancelledError:
                pass
            _premarket_scheduler_task = None
        if _autotrade_task is not None:
            _autotrade_task.cancel()
            try:
                await _autotrade_task
            except asyncio.CancelledError:
                pass
            _autotrade_task = None
        if _news_factor_task is not None:
            _news_factor_task.cancel()
            try:
                await _news_factor_task
            except asyncio.CancelledError:
                pass
            _news_factor_task = None


app = FastAPI(title="AI量化交易平台API", version="3.0", lifespan=_app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "ALLOWED_ORIGINS",
            "http://127.0.0.1:8080,http://localhost:8080",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

# 挂载前端静态文件
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# 全局数据组件
config = Config(CONFIG_PATH)
realtime = RealtimeData()


class _QuoteCache:
    """
    进程内极短 TTL 行情缓存。

    SSE 每 3 秒推一帧、/api/ai_pick/status 每 2 秒轮询、下单前校验、选股粗筛
    原先各自独立请求东财，同一秒内会对同一只股票重复打好几次外部接口，
    既慢又有被封风控的风险。这里按「单只股票」粒度缓存 TTL 内的结果，
    多个调用方共享同一份行情；TTL 很短(默认 1.5s)，不影响盘口时效性。
    """

    def __init__(self, source, ttl: float = 1.5):
        self._source = source
        self._ttl = float(ttl)
        self._data = {}          # symbol -> (expire_ts, quote)
        self._lock = threading.RLock()

    def __getattr__(self, item):
        # 未显式代理的方法(K线/分时等)直接透传给真实数据源
        return getattr(self._source, item)

    def invalidate(self, symbols=None):
        with self._lock:
            if symbols is None:
                self._data.clear()
            else:
                for sym in symbols:
                    self._data.pop(sym, None)

    def get_quotes(self, symbols, sources=None):
        symbols = [s for s in (symbols or []) if s]
        if not symbols:
            return {}
        now = time.time()
        result, missing = {}, []
        with self._lock:
            for sym in symbols:
                hit = self._data.get(sym)
                if hit and hit[0] > now:
                    result[sym] = hit[1]
                else:
                    missing.append(sym)
        if missing:
            fresh = self._source.get_quotes(missing, sources=sources) or {}
            if fresh:
                expire = time.time() + self._ttl
                with self._lock:
                    for sym, quote in fresh.items():
                        self._data[sym] = (expire, quote)
            result.update(fresh)
        return result


realtime = _QuoteCache(realtime, ttl=float(config.get('data_source.quote_cache_seconds', 1.5) or 0))
broker = SimulatedBroker(
    initial_capital=config.get('trading.initial_capital', 1000000),
    commission_rate=config.get('commission.rate', 0.0003),
    stamp_tax_rate=config.get('commission.stamp_tax', 0.0005),
    min_commission=config.get('commission.min', 5),
    slippage=config.get('trading.slippage', 0.0001),
    enforce_market_hours=config.get('trading.enforce_market_hours', False),
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
state_manager = StateManager(os.path.join(ROOT_DIR, "data", "trading_state.db"))
broker.restore_state(state_manager.load_account_state('paper_broker_state'))
risk_runtime = state_manager.load_account_state('risk_runtime', {}) or {}
if risk_runtime.get('date') == datetime.now().date().isoformat():
    risk_mgr.daily_pnl = float(risk_runtime.get('daily_pnl', 0) or 0)
    risk_mgr.daily_order_count = int(risk_runtime.get('daily_order_count', 0) or 0)
    risk_mgr._current_date = datetime.now()
risk_mgr.peak_equity = float(
    risk_runtime.get('peak_equity', broker.get_account_info().get('total_asset', 0)) or 0
)

# ── 交易保护机制 (借鉴 Freqtrade Protections) + 净值记录 ──
from src.risk.protections import (
    ProtectionConfig,
    TrailingState,
    atr_position_qty,
    cooldown_block_reason,
    drawdown_guard_pause,
    load_protection_config,
    stoploss_guard_pause,
    trailing_stop_hit,
    update_trailing_high,
    compute_atr,
)


def _load_protection_cfg() -> ProtectionConfig:
    """从 config.yaml autotrade.protections 读取保护参数, 缺省用内置默认值。"""
    try:
        raw = config.get('autotrade', {}) or {}
    except Exception:
        raw = {}
    return load_protection_config(raw if isinstance(raw, dict) else {})


_trailing_highs: Dict[str, float] = dict(
    state_manager.load_account_state('autotrade_trailing_highs', {}) or {}
)
_equity_history: List[dict] = list(state_manager.load_account_state('equity_history', []) or [])


def _persist_trailing_highs():
    try:
        state_manager.save_account_state('autotrade_trailing_highs', _trailing_highs)
    except Exception:
        pass


def _record_equity_point(value: float, now=None):
    """每个交易日收盘记录一条净值点 (当日重复调用只更新数值)。"""
    now = now or datetime.now()
    d = now.date().isoformat()
    v = round(float(value or 0), 2)
    for p in _equity_history:
        if p.get('date') == d:
            p['value'] = v
            break
    else:
        _equity_history.append({"date": d, "value": v})
        del _equity_history[:-750]  # 最多保留约3年
    try:
        state_manager.save_account_state('equity_history', _equity_history)
    except Exception:
        pass
opportunity_scorer = OpportunityScorer(OpportunityConfig(
    risk_per_trade=config.get('risk.max_risk_per_trade', 0.0075),
    max_position_pct=config.get('risk.max_position_size', 0.12),
    buy_score=config.get('opportunity.buy_score', 62.0),
    watch_score=config.get('opportunity.watch_score', 52.0),
    min_risk_reward=config.get('opportunity.min_risk_reward', 1.3),
    commission_rate=config.get('commission.rate', 0.0003),
    min_commission=config.get('commission.min', 5),
    stamp_tax_rate=config.get('commission.stamp_tax', 0.0005),
))
premarket_analyzer = PremarketAnalyzer(PremarketConfig(
    buy_probability=config.get('premarket.buy_probability', 0.58),
    min_expected_holding_pct=config.get('premarket.min_expected_holding_pct', 0.45),
    max_gap_up_pct=config.get('premarket.max_gap_up_pct', 2.5),
    stop_loss_pct=config.get('risk.stop_loss', 0.07),
    take_profit_pct=config.get('risk.take_profit', 0.10),
))
professional_decision = ProfessionalDecisionLayer(
    max_stale_sessions=config.get('professional.max_stale_sessions', 2),
    neutral_position_multiplier=config.get('professional.neutral_position_multiplier', 0.5),
    unknown_position_multiplier=config.get('professional.unknown_position_multiplier', 0.5),
    max_daily_new_exposure=config.get('professional.max_daily_new_exposure', 0.20),
)
approval_gate = TradeApprovalGate(ApprovalPolicy(
    max_signal_age_seconds=config.get('approval.max_signal_age_seconds', 1800),
    max_price_drift_pct=config.get('approval.max_price_drift_pct', 1.5),
    min_buy_confidence=config.get('approval.min_buy_confidence', 0.58),
    min_risk_reward=config.get('approval.min_risk_reward', 1.35),
    min_data_quality_score=config.get('approval.min_data_quality_score', 70),
))
entry_guard = EntryGuard(EntryGuardConfig(
    max_day_gain_pct=config.get('ai_pick.max_day_gain_pct', 4.5),
    min_day_change_pct=config.get('ai_pick.min_day_change_pct', -2.5),
    max_open_gap_pct=config.get('ai_pick.max_open_gap_pct', 3.0),
    max_pullback_from_high_pct=config.get('ai_pick.max_pullback_from_high_pct', 2.2),
    max_positive_drift_pct=config.get('approval.max_price_drift_pct', 1.5),
    max_negative_drift_pct=config.get('ai_pick.max_negative_drift_pct', 2.5),
    min_net_target_return_pct=config.get('ai_pick.min_net_target_return_pct', 2.0),
    min_validation_samples=config.get('approval.min_validation_samples', 3),
    min_validation_win_rate=config.get('approval.min_validation_win_rate', 45),
    min_validation_avg_return=config.get('approval.min_validation_avg_return', 0),
    max_signal_age_seconds=config.get('approval.max_signal_age_seconds', 1800),
    commission_rate=config.get('commission.rate', 0.0003),
    stamp_tax_rate=config.get('commission.stamp_tax', 0.0005),
    min_commission=config.get('commission.min', 5),
    slippage=config.get('trading.slippage', 0.0001),
))
holding_recovery_analyzer = HoldingRecoveryAnalyzer(HoldingRecoveryConfig(
    sell_score=config.get('strategy.recovery_sell_score', 38),
    reduce_score=config.get('strategy.recovery_reduce_score', 52),
    add_score=config.get('strategy.recovery_add_score', 72),
    max_add_loss_pct=config.get('strategy.recovery_max_add_loss_pct', 8),
))
_ai_graph_cache = None
_broker_lock = threading.RLock()
_order_idempotency: Dict[str, tuple] = {}
_ORDER_IDEMPOTENCY_TTL = 300
_trading_control = state_manager.load_account_state(
    'trading_control', {'opening_enabled': True}
) or {'opening_enabled': True}
_premarket_plan = state_manager.load_account_state(
    'premarket_plan',
    {'status': 'idle', 'entries': [], 'position_exits': []},
) or {'status': 'idle', 'entries': [], 'position_exits': []}
_premarket_lock = threading.RLock()
_premarket_scheduler_task = None
_premarket_scheduler_state = {'generation_date': '', 'execution_date': ''}
_approval_lock = threading.RLock()
_stored_approval_log = state_manager.load_account_state('trade_approval_log', []) or []
_approval_log = _stored_approval_log if isinstance(_stored_approval_log, list) else []
# ── 虚拟盘自托管 AI 自动交易 ──
_autotrade_lock = threading.RLock()
_autotrade_exec_lock = threading.Lock()  # 防重入: 一轮分析只能同时跑一次
_stored_autotrade_state = state_manager.load_account_state('autotrade_state', {}) or {}
_autotrade_state = {
    "enabled": bool((_stored_autotrade_state or {}).get('enabled', False)),  # 开关 (仅虚拟盘生效, 持久化)
    "started_at": str((_stored_autotrade_state or {}).get('started_at', '')),
    "last_cycle": str((_stored_autotrade_state or {}).get('last_cycle', '')),
    "cycles": int((_stored_autotrade_state or {}).get('cycles', 0) or 0),
    "trades": list((_stored_autotrade_state or {}).get('trades', []) or []),  # 最近成交 (持久化, 复盘依赖)
    "log": [],                 # 最近日志 (最多 80)
    "error": "",
    "mode": "paper",
}
_autotrade_task: Optional[asyncio.Task] = None
_news_factor_task: Optional[asyncio.Task] = None
_validation_lock = threading.RLock()
_trading_counters = {"buy": 0, "sell": 0}  # 可观测性: 模拟盘累计成交笔数
_trading_counter_lock = threading.Lock()
_validation_cache: Dict[tuple, tuple] = {}
_dashboard_signal_lock = threading.RLock()
_stored_dashboard_signals = state_manager.load_account_state('dashboard_signals', {}) or {}
_dashboard_signal_cache = (
    _stored_dashboard_signals
    if isinstance(_stored_dashboard_signals, dict)
    else {}
)
_dashboard_signal_refreshing = False
_strategy_cache_lock = threading.RLock()
_stored_strategy_cache = state_manager.load_account_state('strategy_cache_v1', {}) or {}
_strategy_cache = _stored_strategy_cache if isinstance(_stored_strategy_cache, dict) else {}
_strategy_refreshing = set()
_strategy_research_lock = threading.RLock()
_strategy_research_sources: Dict[str, object] = {}


def _locked_broker_operation(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _broker_lock:
            return func(*args, **kwargs)
    return wrapper


def _get_idempotent_order(client_order_id: str):
    if not client_order_id:
        return None
    cached = _order_idempotency.get(client_order_id)
    if not cached:
        return None
    response, created_at = cached
    if time.monotonic() - created_at > _ORDER_IDEMPOTENCY_TTL:
        _order_idempotency.pop(client_order_id, None)
        return None
    return response


def _persist_broker_state():
    state_manager.save_account_state('paper_broker_state', broker.export_state())


def _persist_trading_control():
    state_manager.save_account_state('trading_control', _trading_control)


def _persist_risk_runtime():
    state_manager.save_account_state('risk_runtime', {
        'date': datetime.now().date().isoformat(),
        'daily_pnl': risk_mgr.daily_pnl,
        'daily_order_count': risk_mgr.daily_order_count,
        'peak_equity': risk_mgr.peak_equity,
    })


def _record_trade_approval(decision, execution: Optional[Dict] = None):
    entry = decision.to_dict()
    if execution is not None:
        entry['execution'] = {
            'success': bool(execution.get('success')),
            'order_id': execution.get('order_id', ''),
            'error': execution.get('error', ''),
        }
    limit = max(int(config.get('approval.audit_log_limit', 200) or 200), 20)
    with _approval_lock:
        _approval_log.append(entry)
        del _approval_log[:-limit]
        state_manager.save_account_state('trade_approval_log', _approval_log)


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

    previous_peak = risk_mgr.peak_equity
    risk_mgr.check_drawdown(total_asset)
    if risk_mgr.peak_equity != previous_peak:
        _persist_risk_runtime()

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
    quantity_error = validate_quantity(symbol, side, quantity)
    if quantity_error:
        suggested = quantity // 100 * 100 if side == 'buy' else quantity
        return {"allowed": False, "reason": quantity_error, "suggested_quantity": suggested}
    if side == 'buy' and not bool(_trading_control.get('opening_enabled', True)):
        return {"allowed": False, "reason": "开仓已锁定，仅允许卖出减仓", "suggested_quantity": 0}

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    position = snap["pos_map"].get(symbol)
    current_qty = int(getattr(position, 'quantity', 0) or 0) if position else 0
    available_qty = int(getattr(position, 'available_quantity', current_qty) or 0) if position else 0
    if order_side == OrderSide.SELL and quantity > available_qty:
        return {"allowed": False, "reason": f"T+1 可卖数量不足，当前可卖 {available_qty} 股", "suggested_quantity": available_qty}

    result = risk_mgr.check_order(OrderRequest(
        symbol=symbol,
        side=order_side,
        quantity=int(quantity),
        price=float(price),
        portfolio_value=snap["total_asset"],
        current_position_value=snap["market_value"],
        current_symbol_value=float(getattr(position, 'market_value', 0) or 0) if position else 0,
        reason=reason,
    ))

    if result.allowed and order_side == OrderSide.BUY:
        estimated_cost = quantity * price * (1 + broker.commission_rate + max(getattr(broker, 'slippage', 0), 0))
        estimated_cost += broker.min_commission
        if estimated_cost > snap["cash"]:
            # 用与下单完全一致的成本模型反推可买量(含滑点与最低佣金),
            # 否则建议数量本身仍会被资金校验二次拒绝。
            suggested = buy_quantity_for_amount(
                symbol,
                snap["cash"],
                price,
                broker.commission_rate,
                broker.min_commission,
                max(getattr(broker, 'slippage', 0), 0),
            )
            hint = f"可用资金不足，当前现金 {snap['cash']:.2f} 元"
            if suggested > 0:
                hint += f"，最多可买 {suggested} 股"
            else:
                hint += "，不足以买入 100 股"
            return {"allowed": False, "reason": hint, "suggested_quantity": max(suggested, 0)}

    return {
        "allowed": result.allowed,
        "reason": result.reason or "风控通过",
        "suggested_quantity": result.suggested_quantity or int(quantity),
        "violations": result.violations,
    }


def _validation_is_approved(validation: Dict) -> bool:
    minimum_samples = int(config.get('approval.min_validation_samples', 3) or 3)
    minimum_win_rate = float(config.get('approval.min_validation_win_rate', 45) or 45)
    minimum_average_return = float(config.get('approval.min_validation_avg_return', 0) or 0)
    return (
        int(validation.get('samples', 0) or 0) >= minimum_samples
        and float(validation.get('win_rate', 0) or 0) >= minimum_win_rate
        and float(validation.get('avg_return', 0) or 0) > minimum_average_return
    )


def _validation_is_rejected(validation: Dict) -> bool:
    """明确的差样本才算否决(有足够样本但胜率很低)。样本不足=未知, 不否决,
    让 AI 判断主导(信息优先)。"""
    samples = int(validation.get('samples', 0) or 0)
    if samples < int(config.get('approval.min_validation_samples', 3) or 3):
        return False  # 样本不足属"未知", 不当作否决
    win = float(validation.get('win_rate', 0) or 0)
    avg = float(validation.get('avg_return', 0) or 0)
    # 有足够样本却胜率明显偏低或平均收益为负 -> 差样本, 否决 AI 促成的买入
    return win < 35 or avg < -0.5


def _cached_opportunity_validation(symbol: str, history: pd.DataFrame) -> Dict:
    if not isinstance(history, pd.DataFrame) or history.empty:
        return {"samples": 0, "win_rate": 0, "avg_return": 0, "max_drawdown": 0}
    latest_index = str(history.index[-1])
    latest_close = round(float(history['Close'].iloc[-1]), 6)
    key = (symbol, len(history), latest_index, latest_close)
    ttl = max(int(config.get('approval.validation_cache_seconds', 1800) or 1800), 60)
    with _validation_lock:
        cached = _validation_cache.get(key)
        if cached and time.monotonic() - cached[1] <= ttl:
            return dict(cached[0])
    result = opportunity_scorer.validate_history(symbol, history)
    with _validation_lock:
        _validation_cache[key] = (dict(result), time.monotonic())
        if len(_validation_cache) > 256:
            oldest = min(_validation_cache, key=lambda item: _validation_cache[item][1])
            _validation_cache.pop(oldest, None)
    return result


def _deterministic_trade_signal(
    symbol: str,
    quote: Dict,
    snap: Dict,
    history: Optional[pd.DataFrame] = None,
    market_regime: Optional[Dict] = None,
) -> Dict:
    history = history if isinstance(history, pd.DataFrame) else _load_daily_frame(symbol, 240)
    target_date = target_trading_date(datetime.now(), _premarket_holidays())
    data_quality = professional_decision.data_quality(
        history, target_date, _premarket_holidays()
    ).to_dict()
    position = snap['pos_map'].get(symbol)
    opportunity = opportunity_scorer.analyze(
        symbol,
        history,
        equity=snap['total_asset'],
        cash=snap['cash'],
        current_symbol_value=float(getattr(position, 'market_value', 0) or 0) if position else 0,
        quote=quote,
    ).to_dict()
    validation = _cached_opportunity_validation(symbol, history)
    validation_ok = _validation_is_approved(validation)
    regime = market_regime or {
        'code': 'unknown',
        'label': '未评估',
        'allow_new_positions': True,
        'position_multiplier': 0.5,
    }
    price = float(opportunity.get('price', quote.get('price', 0)) or 0)
    action = 'hold'
    suggested_quantity = 0
    reason_parts: List[str] = []
    exit_plan = None

    if position:
        if data_quality.get('allowed'):
            exit_plan = premarket_analyzer.position_exit(
                symbol,
                history,
                quantity=int(position.quantity),
                available_quantity=int(position.available_quantity),
                avg_cost=float(position.avg_cost),
                current_price=price or None,
                opportunity_score=float(opportunity.get('score', 0) or 0),
            )
            if exit_plan.get('action') in ('sell', 'reduce'):
                action = 'sell'
                suggested_quantity = int(exit_plan.get('suggested_quantity', 0) or 0)
            reason_parts.extend(exit_plan.get('reasons') or [])
            reason_parts.extend(exit_plan.get('warnings') or [])
        else:
            reason_parts.append('行情质量未通过，已有持仓暂不自动操作')
    else:
        buy_allowed = (
            opportunity.get('action') == 'buy'
            and data_quality.get('allowed')
            and validation_ok
            and bool(regime.get('allow_new_positions', True))
            and float(quote.get('price', 0) or 0) > 0
        )
        if buy_allowed:
            daily_room = professional_decision.daily_new_capital_limit(
                snap['total_asset'], snap['cash']
            )
            suggested_quantity = professional_decision.adjusted_quantity(
                int(opportunity.get('suggested_qty', 0) or 0),
                price,
                float(regime.get('position_multiplier', 0.5) or 0),
                daily_room,
            )
            if suggested_quantity > 0:
                action = 'buy'
        reason_parts.extend(opportunity.get('reasons') or [])
        if not data_quality.get('allowed'):
            reason_parts.extend(data_quality.get('reasons') or [])
        elif not validation_ok:
            reason_parts.append(
                f"历史验证不足：{validation.get('samples', 0)} 次，"
                f"胜率 {validation.get('win_rate', 0):.1f}%"
            )
        elif not regime.get('allow_new_positions', True):
            reason_parts.append(f"当前为{regime.get('label', '防守环境')}，暂停开仓")

    confidence = float(opportunity.get('confidence', 0) or 0)
    if validation.get('samples', 0):
        validation_confidence = min(
            float(validation.get('win_rate', 0) or 0) / 100 * 0.7
            + min(int(validation.get('samples', 0) or 0) / 12, 1) * 0.3,
            0.95,
        )
        confidence = min(confidence * 0.75 + validation_confidence * 0.25, 0.95)
    if action == 'hold':
        confidence = min(confidence, 0.60)
    return {
        'symbol': symbol,
        'action': action,
        'confidence': round(confidence, 4),
        'reason': '；'.join(dict.fromkeys(reason_parts)) or '多因子条件尚未形成一致结论',
        'source': 'professional_deterministic',
        'generated_at': datetime.now().isoformat(),
        'price': price,
        'suggested_qty': suggested_quantity,
        'buy_low': opportunity.get('buy_low', 0),
        'buy_high': opportunity.get('buy_high', 0),
        'stop_loss': opportunity.get('stop_loss', 0),
        'target_price': opportunity.get('target_price', 0),
        'risk_reward': opportunity.get('risk_reward', 0),
        'potential_score': opportunity.get('score', 0),
        'data_quality': data_quality,
        'validation': validation,
        'market_regime': regime,
        'exit_plan': exit_plan,
        'warnings': opportunity.get('warnings', []),
        'quote_source': quote.get('data_source', ''),
    }


def _build_rule_ai_signals(symbols: Optional[List[str]] = None, quotes: Optional[Dict] = None) -> List[Dict]:
    symbols = symbols or config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104'])
    symbols = list(dict.fromkeys(_normalize_symbol(symbol) for symbol in symbols))[:6]
    quotes = quotes or realtime.get_quotes(symbols, sources=['eastmoney', 'tencent', 'sina'])
    snap = _portfolio_snapshot()
    benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
    benchmark = _load_daily_frame(benchmark_symbol, 240)
    regime = professional_decision.market_regime(benchmark).to_dict()
    if not symbols:
        return []
    with _Pool(max_workers=min(4, len(symbols))) as executor:
        frames = list(executor.map(lambda item: _load_daily_frame(item, 240), symbols))
    return [
        _deterministic_trade_signal(symbol, quotes.get(symbol, {}), snap, frame, regime)
        for symbol, frame in zip(symbols, frames)
    ]


def _refresh_dashboard_signal_cache(symbols: List[str]):
    global _dashboard_signal_cache, _dashboard_signal_refreshing
    try:
        quotes = realtime.get_quotes(
            symbols,
            sources=['eastmoney', 'tencent', 'sina'],
        )
        signals = _build_rule_ai_signals(symbols, quotes)
        cache = {
            'symbols': list(symbols),
            'signals': signals,
            'generated_at': datetime.now().isoformat(),
            'error': '',
        }
        with _dashboard_signal_lock:
            _dashboard_signal_cache = cache
            state_manager.save_account_state('dashboard_signals', cache)
    except Exception as exc:
        with _dashboard_signal_lock:
            _dashboard_signal_cache = {
                **(_dashboard_signal_cache or {}),
                'error': str(exc),
            }
    finally:
        with _dashboard_signal_lock:
            _dashboard_signal_refreshing = False


def _dashboard_signals_snapshot(symbols: List[str]) -> List[Dict]:
    global _dashboard_signal_refreshing
    refresh_seconds = max(
        int(config.get('approval.dashboard_signal_refresh_seconds', 300) or 300),
        30,
    )
    with _dashboard_signal_lock:
        cache = dict(_dashboard_signal_cache or {})
        generated_at = cache.get('generated_at', '')
        generated_time = TradeApprovalGate._parse_time(generated_at)
        age = (
            max((datetime.now() - generated_time).total_seconds(), 0)
            if generated_time
            else float('inf')
        )
        should_refresh = (
            cache.get('symbols') != symbols
            or age > refresh_seconds
            or not cache.get('signals')
        )
        if should_refresh and not _dashboard_signal_refreshing:
            _dashboard_signal_refreshing = True
            threading.Thread(
                target=_refresh_dashboard_signal_cache,
                args=(list(symbols),),
                daemon=True,
            ).start()
        signals = cache.get('signals') if cache.get('symbols') == symbols else None
    if signals:
        return signals
    return [
        {
            'symbol': symbol,
            'action': 'hold',
            'confidence': 0,
            'reason': '专业信号正在后台更新；数据完成前不执行交易',
            'source': 'refreshing',
            'generated_at': '',
            'suggested_qty': 0,
        }
        for symbol in symbols
    ]


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
        # 信息优先: 拉全 新闻/基本面/资金 + 注入情感/动量, 避免分析师无据默认观望
        research_bundle = _strategy_research_bundle(sym)
        news_items = research_bundle.get('news') or []
        research = entry_guard.build_research_snapshot(
            news_items=news_items,
            fundamentals=research_bundle.get('fundamentals'),
            capital_flow=research_bundle.get('capital'),
            market_regime=None,
            average_amount=0,
            source_status=research_bundle.get('source_status'),
        )
        hist = _load_daily_frame(sym, 120)
        try:
            decision = graph.analyze(sym, {
                "symbol": sym,
                "price": quote.get('price', 0),
                "change_pct": quote.get('change_pct', 0),
                "position": int(getattr(position, 'quantity', 0) or 0) if position else 0,
                "research": research,
                "sentiment": _aggregate_news_sentiment(news_items),
                "momentum": _recent_momentum(hist),
                "breaking_news": _pick_breaking_news(news_items),
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

_AI_PICK_SCHEMA_VERSION = 2

# 后台选股任务状态(单例; 同一时刻只跑一轮)
_scan_job = {
    "schema_version": _AI_PICK_SCHEMA_VERSION,
    "legacy_snapshot": False,
    "status": "idle",      # idle | running | done | error
    "pool": "",            # market | watchlist
    "total": 0,            # 深度分析总数
    "done": 0,             # 已完成数
    "current": "",         # 正在分析的股票名
    "picks": [],           # 结果(全部深度分析, 前端再按 action 分组)
    "candidates": 0,       # 粗筛前候选数
    "requested": 5,        # 本轮计划深度分析数量
    "prescreen_passed": 0,
    "prescreen_fallback": 0,
    "prescreen_rejected": 0,
    "prescreen_reasons": {},
    "started_at": "",
    "finished_at": "",
    "error": "",
}
_scan_lock = _threading.Lock()

# /api/ai_pick/status 被前端每 2s 轮询，但它每次都要拉实时行情并对每只票重跑
# entry_guard 复核，开销远超"查状态"。这里把复核后的结果按极短 TTL 缓存，
# 轮询期间直接复用，行情时效性不受影响。
_scan_live_cache = {"key": "", "expire": 0.0, "picks": []}

# 选股结果落库，重启后仍能看到上一轮推荐（与盘前计划的持久化策略保持一致）。
# 只恢复已完成的结果；进程被杀时残留的 running 状态没有对应线程，恢复成 error 更诚实。
def _persist_scan_job():
    try:
        with _scan_lock:
            snapshot = dict(_scan_job)
        state_manager.save_account_state('ai_pick_job', snapshot)
    except Exception:
        pass


def _restore_scan_job():
    try:
        saved = state_manager.load_account_state('ai_pick_job', None)
        if not isinstance(saved, dict):
            return
        try:
            saved_version = int(saved.get('schema_version', 1) or 1)
        except (TypeError, ValueError):
            saved_version = 1
        saved['schema_version'] = saved_version
        saved['legacy_snapshot'] = bool(
            saved.get('status') == 'done'
            and saved_version < _AI_PICK_SCHEMA_VERSION
        )
        if saved.get('status') == 'running':
            saved['status'] = 'error'
            saved['error'] = '上次选股因服务重启中断，请重新开始'
            saved['current'] = ''
        _scan_job.update(saved)
    except Exception:
        pass


_restore_scan_job()


def _fetch_candidates(limit: int = 40) -> List[Dict]:
    """构建兼顾活跃度和流动性的候选池，避免只追逐涨幅榜。"""
    out, seen = [], set()
    try:
        from src.data.em_client import _cffi, _HAS_CFFI, UA
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = {"Referer": "https://quote.eastmoney.com/", "User-Agent": UA}
        # 每个榜单都读取足够深，避免成交额榜前 20 恰好集中在同一热点板块时
        # 第二个榜单又因重复而无法把候选池补足。
        per_list = max(limit, 40)
        for ranking in ("f6", "f3"):
            params = {
                "pn": 1, "pz": max(per_list * 3, 60), "po": 1,
                "np": 1, "fltt": 2, "invt": 2, "fid": ranking,
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f6,f12,f13,f14,f15,f16,f17,f18",
            }
            if _HAS_CFFI:
                response = _cffi.get(
                    url, params=params, impersonate="chrome",
                    headers=headers, timeout=8,
                )
            else:
                import requests as _rq
                response = _rq.get(url, params=params, headers=headers, timeout=8)
            diff = (response.json().get("data") or {}).get("diff") or []
            rows = diff.values() if isinstance(diff, dict) else diff
            added = 0
            for item in rows:
                code = str(item.get("f12", "")).strip()
                name = str(item.get("f14", "")).strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                market = "sh" if item.get("f13") == 1 else "sz"
                symbol = market + code
                price = float(item.get("f2", 0) or 0)
                change = float(item.get("f3", 0) or 0)
                amount = float(item.get("f6", 0) or 0)
                if "ST" in name.upper() or "退" in name:
                    continue
                if price < 2 or amount < 2e8 or change <= -9.5 or change >= 9.5:
                    continue
                if symbol in seen:
                    continue
                seen.add(symbol)
                out.append({
                    "symbol": symbol, "name": name, "price": price,
                    "change_pct": change, "amount": amount,
                    "candidate_source": "momentum" if ranking == "f3" else "liquidity",
                    "high": float(item.get("f15", 0) or 0),
                    "low": float(item.get("f16", 0) or 0),
                    "open": float(item.get("f17", 0) or 0),
                    "pre_close": float(item.get("f18", 0) or 0),
                })
                added += 1
                if added >= per_list or len(out) >= limit:
                    break
            if len(out) >= limit:
                break
    except Exception as exc:
        # 原先是裸 except: pass，网络故障会被静默成"空候选池"，
        # 前端只看到 candidates:0 却不知道是行情源挂了还是真的没票。
        _fetch_candidates.last_error = f"{type(exc).__name__}: {exc}"
        print(f"[warn] 候选池拉取失败: {exc}", file=sys.stderr)
        return out
    _fetch_candidates.last_error = ""
    return out


def _prescreen(
    candidates: List[Dict],
    top: int = 6,
    fill_shortfall: bool = False,
) -> List[Dict]:
    """规则粗筛并记录淘汰原因；可用低风险近似项补足深度分析数量。"""
    syms = [c["symbol"] for c in candidates]
    if not syms:
        _prescreen.last_stats = {
            "passed": 0, "rejected": 0, "fallback": 0, "reasons": {},
        }
        return []
    quotes = realtime.get_quotes(syms, sources=['tencent', 'sina', 'eastmoney']) or {}
    passed, rejected = [], []
    rejection_counts: Dict[str, int] = {}
    for c in candidates:
        q = dict(c)
        for key, value in (quotes.get(c["symbol"], {}) or {}).items():
            if key in {'price', 'pre_close', 'open', 'high', 'low'} and not value and q.get(key):
                continue
            q[key] = value
        intraday = entry_guard.intraday_snapshot(q)
        prescreen_reasons = entry_guard.prescreen_reasons(q)
        change_pct = float(intraday.get('day_change_pct', 0) or 0)
        vol_ratio = float(q.get('vol_ratio', 0) or 0)
        turnover = float(q.get('turnover_pct', q.get('turnover', 0)) or 0)
        moderate_change = max(0, 4 - abs(change_pct - 1.5)) * 0.25
        volume_score = min(max(vol_ratio, 0), 3) * 0.12
        turnover_score = min(max(turnover, 0), 20) * 0.015
        liquidity_score = min(max(float(c.get('amount', 0) or 0) / 1e9, 0), 3) * 0.12
        source_bonus = 0.25 if c.get('candidate_source') == 'liquidity' else 0
        range_position = float(intraday.get('range_position_pct', 50) or 50)
        range_score = max(0, 1 - abs(range_position - 70) / 70) * 0.30
        pullback_penalty = float(intraday.get('pullback_from_high_pct', 0) or 0) * 0.08
        score = moderate_change + volume_score + turnover_score + liquidity_score + source_bonus + range_score - pullback_penalty
        c = dict(c)
        c["score"] = round(score, 3)
        c["quote"] = q
        c["intraday"] = intraday
        c["prescreen_passed"] = not prescreen_reasons
        c["prescreen_reasons"] = prescreen_reasons
        c["prescreen_fallback"] = False
        if prescreen_reasons:
            for reason in prescreen_reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            rejected.append(c)
        else:
            passed.append(c)

    passed.sort(key=lambda item: item["score"], reverse=True)
    # 补位只决定“是否值得继续分析”，不会绕过 _analyze_one 的实时硬门禁。
    rejected.sort(key=lambda item: (len(item["prescreen_reasons"]), -item["score"]))
    selected = passed[:top]
    if fill_shortfall and len(selected) < top:
        fallback = rejected[:top - len(selected)]
        for item in fallback:
            item["prescreen_fallback"] = True
        selected.extend(fallback)

    _prescreen.last_stats = {
        "passed": len(passed),
        "rejected": len(rejected),
        "fallback": sum(1 for item in selected if item.get("prescreen_fallback")),
        "reasons": rejection_counts,
    }
    return selected


def _aggregate_news_sentiment(news_items) -> Optional[float]:
    """把个股新闻列表的情感分聚合成 -1~1(按重要性加权)。无新闻返回 None(分析师会自行判定)。"""
    if not news_items:
        return None
    tot_w, acc = 0.0, 0.0
    for it in news_items:
        if not isinstance(it, dict):
            continue
        senti = it.get('sentiment') or {}
        try:
            score = float(senti.get('score', 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            imp = float(senti.get('importance', 5) or 5)
        except (TypeError, ValueError):
            imp = 5.0
        w = max(imp, 1.0)
        acc += score * w
        tot_w += w
    if tot_w <= 0:
        return None
    return max(-1.0, min(1.0, acc / tot_w))


def _recent_momentum(history) -> float:
    """近 10 日收盘动量(涨跌比例), 供技术分析师在 K 线不足时的退化路径使用。"""
    try:
        if history is None or len(history) < 6:
            return 0.0
        col = 'Close' if 'Close' in history.columns else ('close' if 'close' in history.columns else None)
        if col is None:
            return 0.0
        closes = [float(v) for v in history[col].tolist() if v is not None]
        if len(closes) < 6:
            return 0.0
        look = min(10, len(closes) - 1)
        past = closes[-look - 1]
        if past <= 0:
            return 0.0
        return max(-1.0, min(1.0, (closes[-1] - past) / past * 3))  # 放大到 -1~1 量级
    except Exception:
        return 0.0


def _pick_breaking_news(news_items) -> str:
    """挑一条最重要的新闻标题作为"突发"提示喂给新闻分析师。"""
    if not news_items:
        return ""
    best, best_imp = "", -1.0
    for it in news_items:
        if not isinstance(it, dict):
            continue
        title = (it.get('news') or {}).get('title') if isinstance(it.get('news'), dict) else it.get('title')
        if not title:
            continue
        imp = 0.0
        try:
            imp = float((it.get('sentiment') or {}).get('importance', 0) or 0)
        except (TypeError, ValueError):
            imp = 0.0
        if imp > best_imp:
            best_imp, best = imp, str(title)
    return best if best_imp >= 6 else ""


def _analyze_one(
    sym: str,
    quote: Dict,
    snap: Dict,
    market_regime: Optional[Dict] = None,
) -> Dict:
    """历史潜力评分为主，多智能体负责复核和解释。"""
    analysis_time = datetime.now().isoformat()
    position = snap["pos_map"].get(sym)
    price = float(quote.get('price', 0) or 0)
    history = _load_daily_frame(sym, 240)
    current_symbol_value = float(getattr(position, 'market_value', 0) or 0) if position else 0
    opportunity = opportunity_scorer.analyze(
        sym,
        history,
        equity=snap["total_asset"],
        cash=snap["cash"],
        current_symbol_value=current_symbol_value,
        quote=quote,
    ).to_dict()
    price = float(opportunity.get('price', price) or price)
    validation = _cached_opportunity_validation(sym, history)
    data_quality = professional_decision.data_quality(
        history,
        target_trading_date(datetime.now(), _premarket_holidays()),
        _premarket_holidays(),
    ).to_dict()
    # 信息优先: 无论初筛是否形成买入计划, 都拉全 新闻/基本面/资金 喂给多智能体,
    # 避免"评分不够→不拉数据→分析师无据→一律观望"的信息饥饿。
    research_bundle = _strategy_research_bundle(sym)
    research = entry_guard.build_research_snapshot(
        news_items=research_bundle.get('news'),
        fundamentals=research_bundle.get('fundamentals'),
        capital_flow=research_bundle.get('capital'),
        market_regime=market_regime,
        average_amount=float(opportunity.get('average_amount', 0) or 0),
        source_status=research_bundle.get('source_status'),
    )
    entry_plan = {
        key: opportunity.get(key)
        for key in (
            'action', 'price', 'buy_low', 'buy_high', 'stop_loss',
            'target_price', 'suggested_qty', 'suggested_amount',
            'average_amount', 'risk_reward', 'upside_pct',
        )
    }
    entry_evaluation = entry_guard.evaluate(
        sym,
        quote,
        entry_plan,
        validation,
        research=research,
        reference_price=price,
        generated_at=analysis_time,
        market_open=market_session(datetime.now()).is_open,
    )

    decision = {
        "action": "hold",
        "confidence": 0,
        "reason": "AI 复核未启用，使用确定性多因子评分",
        "risk": {}, "analysts": [], "debate": {}, "trader": {},
    }
    # 从真实新闻聚合情感分(-1~1) + 近期动量, 注入给分析师(补齐原本恒空的情感面/技术退化路径)
    news_items = research_bundle.get('news') or []
    sentiment_score = _aggregate_news_sentiment(news_items)
    momentum_val = _recent_momentum(history)
    breaking = _pick_breaking_news(news_items)

    try:
        graph = _get_ai_graph()["graph"]
        decision = graph.analyze(sym, {
            "symbol": sym,
            "price": price,
            "change_pct": quote.get('change_pct', 0),
            "position": int(getattr(position, 'quantity', 0) or 0) if position else 0,
            "opportunity": opportunity,
            "entry_guard": entry_evaluation,
            "research": research,
            "market_regime": market_regime or {},
            "sentiment": sentiment_score,
            "momentum": momentum_val,
            "breaking_news": breaking,
            "risk": {
                "drawdown": risk_mgr.get_risk_report().get('drawdown', 0),
                "total_position_pct": snap["total_position_pct"],
                "daily_pnl": risk_mgr.daily_pnl,
            },
            "trade_date": datetime.now().strftime('%Y%m%d'),
        }).to_dict()
    except Exception as exc:
        decision["reason"] = f"AI 复核不可用: {exc}"

    ai_action = decision.get('action', 'hold')
    ai_confidence = float(decision.get('confidence', 0) or 0)
    validation_ok = _validation_is_approved(validation)
    hard_veto = ai_action == 'sell' and ai_confidence >= 0.70
    approval_failures: List[str] = []
    approval_label = entry_evaluation.get('label') or '综合审批未通过'
    if opportunity.get('action') != 'buy' or int(opportunity.get('suggested_qty', 0) or 0) <= 0:
        approval_failures.append('潜力评分未达到买入阈值或没有可执行仓位')
        approval_label = '潜力评分未达买入线'
    if not validation_ok:
        approval_failures.append(
            f"历史验证未通过：样本 {validation.get('samples', 0)} 次，"
            f"胜率 {float(validation.get('win_rate', 0) or 0):.1f}%，"
            f"平均收益 {float(validation.get('avg_return', 0) or 0):.2f}%"
        )
        approval_label = '历史样本验证未通过'
    if not data_quality.get('allowed'):
        approval_failures.extend(data_quality.get('reasons') or ['行情时效或完整性未通过'])
        approval_label = '行情质量门禁未通过'
    if not entry_evaluation.get('allowed'):
        approval_failures.extend(entry_evaluation.get('reasons') or [])
        approval_label = entry_evaluation.get('label') or approval_label
    if hard_veto:
        approval_failures.append('AI 风险复核给出高置信度卖出/回避意见')
        approval_label = 'AI 风险复核否决买入'
    approval_failures = list(dict.fromkeys(approval_failures))

    # 硬风控底线(任何情况都必须满足才可买入)
    risk_floor_ok = (
        data_quality.get('allowed')
        and entry_evaluation.get('allowed')
        and not hard_veto
    )
    # 路径一: 确定性潜力评分给出买入(原逻辑)
    opportunity_buy = (
        opportunity.get('action') == 'buy'
        and int(opportunity.get('suggested_qty', 0) or 0) > 0
        and validation_ok
        and risk_floor_ok
    )
    # 路径二(信息优先): 多智能体高置信买入 -> 也可促成买入(不再只能否决),
    # 但仍须过硬风控底线; 历史验证从"必须通过"降级为"不能是明确的差样本"。
    ai_buy = (
        ai_action == 'buy'
        and ai_confidence >= 0.62
        and risk_floor_ok
        and not _validation_is_rejected(validation)
    )
    final_action = "buy" if (opportunity_buy or ai_buy) else "hold"

    # 买入数量: 优先用潜力评分的计划数量; 若是 AI 促成的买入且评分没给量, 用风控口径估一个
    planned_from_opp = int(opportunity.get('suggested_qty', 0) or 0)
    if final_action == 'buy':
        if planned_from_opp > 0:
            suggested_qty = planned_from_opp
        else:
            try:
                raw = risk_mgr.calculate_position_size_with_risk(price, snap["total_asset"], 0.008)
                suggested_qty = max(0, int(raw) // 100 * 100)
            except Exception:
                suggested_qty = 0
            if suggested_qty <= 0 and price > 0:
                # 兜底: 用不超过单股上限的资金买最小可成交手数
                budget = snap["total_asset"] * 0.08
                suggested_qty = max(0, int(budget / price / 100) * 100)
    else:
        suggested_qty = 0
    opportunity_confidence = float(opportunity.get('confidence', 0) or 0)
    # AI 促成买入时提高 AI 置信度权重, 让"AI说了算"的成分更实
    if final_action == 'buy' and not opportunity_buy and ai_buy:
        confidence = min(0.55 + ai_confidence * 0.4, 0.95)
    else:
        confidence = min(
            opportunity_confidence * 0.7
            + (ai_confidence if ai_action == final_action else 0) * 0.3,
            0.95,
        )
    primary_reasons = opportunity.get('reasons') or []
    if final_action == 'buy':
        reason = "；".join(primary_reasons[:3]) or "综合审批通过，可按计划执行"
        if validation.get('samples', 0):
            reason += (
                f"；历史相似机会 {validation['samples']} 次，"
                f"胜率 {validation['win_rate']:.1f}%"
            )
    else:
        # 未通过时先解释否决原因；有利因子另行展示，避免正面描述掩盖最终结论。
        reason = "；".join(approval_failures[:4]) or "综合审批未形成可执行买入计划"
    target_scenario = entry_evaluation.get('target_scenario') or {}
    stop_scenario = entry_evaluation.get('stop_scenario') or {}
    return {
        "symbol": sym,
        "name": quote.get('name') or COMMON_SYMBOL_NAMES.get(sym, sym),
        "price": price,
        "change_pct": float(quote.get('change_pct', 0) or 0),
        "action": final_action,
        "confidence": confidence,
        "reason": reason,
        "positive_factors": primary_reasons[:4],
        "approval_label": approval_label if final_action != 'buy' else '综合审批通过',
        "approval_failures": approval_failures,
        "suggested_qty": suggested_qty,
        "planned_qty": int(opportunity.get('suggested_qty', 0) or 0),
        "source": "opportunity+agents",
        "generated_at": analysis_time,
        "analysis_price": price,
        "analysis_change_pct": float(quote.get('change_pct', 0) or 0),
        "potential_score": opportunity.get('score', 0),
        "buy_low": opportunity.get('buy_low', 0),
        "buy_high": opportunity.get('buy_high', 0),
        "stop_loss": opportunity.get('stop_loss', 0),
        "target_price": opportunity.get('target_price', 0),
        "upside_pct": opportunity.get('upside_pct', 0),
        "risk_reward": opportunity.get('risk_reward', 0),
        "suggested_amount": opportunity.get('suggested_amount', 0) if final_action == 'buy' else 0,
        "planned_amount": opportunity.get('suggested_amount', 0),
        "expected_profit": target_scenario.get('net_profit', 0),
        "max_loss": stop_scenario.get('net_loss', 0),
        "price_percentile": opportunity.get('price_percentile', 0),
        "factors": opportunity.get('factors', {}),
        "warnings": list(dict.fromkeys(
            list(opportunity.get('warnings', []))
            + list(entry_evaluation.get('warnings', []))
        )),
        "validation": validation,
        "data_quality": data_quality,
        "entry_plan": entry_plan,
        "entry_guard": entry_evaluation,
        "research": research,
        "market_regime": market_regime or {},
        "profit_guaranteed": False,
        "disclaimer": "AI 无法确保盈利；只有实时价格、历史验证、多方证据和费用后空间同时通过才保留买入建议。",
        "quote_source": quote.get('data_source', ''),
        # AI 多智能体的真实观点(独立于可执行门禁), 让用户看到 AI 到底怎么判断,
        # 即使因休市/费用空间不足等导致当前不可执行买入。
        "ai_view": ai_action,
        "ai_confidence": round(ai_confidence, 3),
        "ai_bulls": sum(1 for a in decision.get('analysts', []) if a.get('stance') == 'bull'),
        "ai_bears": sum(1 for a in decision.get('analysts', []) if a.get('stance') == 'bear'),
        "market_open": market_session(datetime.now()).is_open,
        "sentiment_score": sentiment_score,
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
            quotes = realtime.get_quotes(syms, sources=['tencent', 'sina', 'eastmoney']) or {}
            shortlist = [{"symbol": s, "name": quotes.get(s, {}).get('name', s),
                          "quote": quotes.get(s, {}), "prescreen_passed": True,
                          "prescreen_reasons": [], "prescreen_fallback": False}
                         for s in syms][:top]
            prescreen_stats = {
                "passed": len(shortlist), "rejected": 0,
                "fallback": 0, "reasons": {},
            }
        else:
            candidates = _fetch_candidates(limit=max(80, top * 16))
            with _scan_lock:
                _scan_job["candidates"] = len(candidates)
            shortlist = _prescreen(candidates, top=top, fill_shortfall=True)
            prescreen_stats = dict(getattr(_prescreen, 'last_stats', {}) or {})

        snap = _portfolio_snapshot()
        benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
        benchmark = _load_daily_frame(benchmark_symbol, 240)
        market_regime = professional_decision.market_regime(benchmark).to_dict()
        with _scan_lock:
            _scan_job["total"] = len(shortlist)
            _scan_job["done"] = 0
            _scan_job["prescreen_passed"] = int(prescreen_stats.get("passed", 0) or 0)
            _scan_job["prescreen_fallback"] = int(prescreen_stats.get("fallback", 0) or 0)
            _scan_job["prescreen_rejected"] = int(prescreen_stats.get("rejected", 0) or 0)
            _scan_job["prescreen_reasons"] = dict(prescreen_stats.get("reasons", {}) or {})
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
            q = item.get("quote") or realtime.get_quotes(
                [sym], sources=['tencent', 'sina', 'eastmoney']
            ).get(sym, {})
            res = _analyze_one(sym, q, snap, market_regime)
            res["prescreen"] = {
                "passed": bool(item.get("prescreen_passed", True)),
                "fallback": bool(item.get("prescreen_fallback", False)),
                "reasons": list(item.get("prescreen_reasons") or []),
            }
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
        _persist_scan_job()
    except Exception as exc:
        with _scan_lock:
            _scan_job["status"] = "error"
            _scan_job["error"] = str(exc)
            _scan_job["finished_at"] = datetime.now().isoformat()
        _persist_scan_job()


def _premarket_holidays() -> List[str]:
    return [str(item)[:10] for item in (config.get('premarket.market_holidays', []) or [])]


def _persist_premarket_plan():
    state_manager.save_account_state('premarket_plan', _premarket_plan)


def _premarket_plan_snapshot() -> Dict:
    with _premarket_lock:
        return json.loads(json.dumps(_premarket_plan, ensure_ascii=False, default=str))


def _premarket_quantity(price: float, stop_loss: float, symbol: str, snap: Dict) -> int:
    if price <= 0:
        return 0
    position = snap["pos_map"].get(symbol)
    current_symbol_value = float(getattr(position, 'market_value', 0) or 0) if position else 0
    risk_per_share = max(price - stop_loss, price * 0.02)
    risk_budget = snap["total_asset"] * float(config.get('risk.max_risk_per_trade', 0.0075) or 0.0075)
    risk_quantity = int(risk_budget / max(risk_per_share, 0.01))
    position_room = max(
        snap["total_asset"] * float(config.get('risk.max_position_size', 0.12) or 0.12)
        - current_symbol_value,
        0,
    )
    capital_quantity = int(min(snap["cash"], position_room) / price)
    return max(min(risk_quantity, capital_quantity) // 100 * 100, 0)


def _generate_premarket_plan(pool: str = 'watchlist', top: int = 5) -> Dict:
    """只使用目标交易日前的日线生成盘前计划，不调用大模型。"""
    global _premarket_plan
    pool = pool if pool in ('market', 'watchlist') else 'watchlist'
    top = max(1, min(int(top or 5), 8))
    generated_at = datetime.now()
    trade_date = target_trading_date(generated_at, _premarket_holidays())
    with _premarket_lock:
        _premarket_plan = {
            'status': 'generating',
            'pool': pool,
            'trade_date': trade_date.isoformat(),
            'generated_at': generated_at.isoformat(),
            'entries': [],
            'position_exits': [],
            'error': '',
            'disclaimer': '概率来自历史相似行情，仅用于模拟盘计划，不构成收益承诺。',
        }
        _persist_premarket_plan()

    try:
        candidate_names = {}
        if pool == 'market':
            candidates = _fetch_candidates(limit=max(top * 5, 30))
            shortlist = candidates[:max(top * 2, 8)]
            symbols = [item['symbol'] for item in shortlist]
            candidate_names = {item['symbol']: item.get('name', '') for item in shortlist}
        else:
            symbols = _get_watchlist() or [
                _normalize_symbol(item)
                for item in config.get('trading.symbols', ['sh600000', 'sz000001'])
            ]
            symbols = list(dict.fromkeys(symbols))[:12]

        # 盘前预测不请求实时价，既减少无效重试，也避免把目标交易日盘中数据混入计划。
        quotes = {}
        snap = _portfolio_snapshot()
        benchmark_symbol = _normalize_symbol(
            str(config.get('professional.benchmark_symbol', 'sh000001'))
        )
        benchmark_history = _load_daily_frame(benchmark_symbol, 240)
        benchmark_history = premarket_analyzer.before_trade_date(benchmark_history, trade_date)
        market_regime = professional_decision.market_regime(benchmark_history).to_dict()
        histories = {}
        if symbols:
            with _Pool(max_workers=min(4, len(symbols))) as executor:
                loaded = list(executor.map(lambda item: _load_daily_frame(item, 360), symbols))
            histories = dict(zip(symbols, loaded))
        entries = []
        for symbol in symbols:
            history = histories.get(symbol, pd.DataFrame())
            history = premarket_analyzer.before_trade_date(history, trade_date)
            data_quality = professional_decision.data_quality(
                history, trade_date, _premarket_holidays()
            ).to_dict()
            forecast = premarket_analyzer.forecast(symbol, history).to_dict()
            if history.empty:
                entries.append({
                    'symbol': symbol,
                    'name': candidate_names.get(symbol) or quotes.get(symbol, {}).get('name') or symbol,
                    'decision': 'watch',
                    'available': False,
                    'warnings': ['没有可用历史日线'],
                    'data_quality': data_quality,
                })
                continue

            latest = history.iloc[-1]
            previous_close = float(latest['Close'])
            historical_quote = {
                'price': previous_close,
                'volume': float(latest.get('Volume', 0) or 0),
                'amount': float(latest.get('Amount', 0) or 0),
            }
            position = snap['pos_map'].get(symbol)
            opportunity = opportunity_scorer.analyze(
                symbol,
                history,
                equity=snap['total_asset'],
                cash=snap['cash'],
                current_symbol_value=float(getattr(position, 'market_value', 0) or 0) if position else 0,
                quote=historical_quote,
            ).to_dict()
            stop_loss = float(opportunity.get('stop_loss', previous_close * 0.93) or previous_close * 0.93)
            suggested_qty = _premarket_quantity(previous_close, stop_loss, symbol, snap)
            probability = float(forecast.get('rise_probability', 0) or 0)
            expected_holding = float(forecast.get('expected_holding_pct', 0) or 0)
            potential_score = float(opportunity.get('score', 0) or 0)
            risk_reward = float(opportunity.get('risk_reward', 0) or 0)
            base_eligible = (
                bool(forecast.get('available'))
                and probability >= premarket_analyzer.config.buy_probability
                and expected_holding >= premarket_analyzer.config.min_expected_holding_pct
                and potential_score >= 60
                and risk_reward >= 1.35
                and suggested_qty > 0
            )
            eligible = (
                base_eligible
                and bool(data_quality.get('allowed'))
                and bool(market_regime.get('allow_new_positions'))
            )
            max_gap_price = previous_close * (1 + premarket_analyzer.config.max_gap_up_pct / 100)
            model_buy_high = float(opportunity.get('buy_high', 0) or 0)
            max_buy_price = min(model_buy_high, max_gap_price) if model_buy_high > 0 else max_gap_price
            priority_score = (
                probability * 45
                + potential_score * 0.35
                + max(min(expected_holding, 5), -5) * 4
            )
            amount = suggested_qty * previous_close
            reasons = list(forecast.get('reasons') or [])
            if potential_score >= 68:
                reasons.append(f"潜力模型评分 {potential_score:.0f}/100")
            warnings = list(dict.fromkeys(
                list(forecast.get('warnings') or []) + list(opportunity.get('warnings') or [])
            ))
            gate_reasons = []
            if not data_quality.get('allowed'):
                gate_reasons.extend(data_quality.get('reasons') or ['数据质量门禁未通过'])
                warnings.extend(data_quality.get('warnings') or [])
            if base_eligible and not market_regime.get('allow_new_positions'):
                gate_reasons.append(f"当前为{market_regime.get('label', '防守环境')}，暂停新开仓")
            decision = 'buy' if eligible else 'blocked' if gate_reasons else 'watch'
            entries.append({
                'symbol': symbol,
                'name': candidate_names.get(symbol) or quotes.get(symbol, {}).get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol),
                'available': bool(forecast.get('available')),
                'decision': decision,
                'priority_score': round(priority_score, 2),
                'potential_score': round(potential_score, 2),
                'rise_probability': probability,
                'confidence': float(forecast.get('confidence', 0) or 0),
                'expected_intraday_pct': float(forecast.get('expected_intraday_pct', 0) or 0),
                'expected_holding_pct': expected_holding,
                'downside_pct': float(forecast.get('downside_pct', 0) or 0),
                'as_of_date': forecast.get('as_of_date', ''),
                'previous_close': round(previous_close, 4),
                'buy_low': opportunity.get('buy_low', previous_close),
                'max_buy_price': round(max_buy_price, 4),
                'stop_loss': round(stop_loss, 4),
                'target_price': opportunity.get('target_price', 0),
                'risk_reward': risk_reward,
                'raw_suggested_qty': suggested_qty,
                'suggested_qty': suggested_qty if eligible else 0,
                'suggested_amount': round(amount, 2) if eligible else 0,
                'expected_profit': round(amount * max(expected_holding, 0) / 100, 2) if eligible else 0,
                'max_loss': round(max(previous_close - stop_loss, 0) * suggested_qty, 2) if eligible else 0,
                'sample_count': int(forecast.get('sample_count', 0) or 0),
                'neighbor_count': int(forecast.get('neighbor_count', 0) or 0),
                'reasons': reasons[:4],
                'warnings': warnings[:4],
                'gate_reasons': gate_reasons[:4],
                'data_quality': data_quality,
                'execution': None,
            })

        entries.sort(key=lambda item: item.get('priority_score', -999), reverse=True)
        max_entries = max(1, min(int(config.get('premarket.max_entries', 1) or 1), 3))
        daily_capital_limit = professional_decision.daily_new_capital_limit(
            snap['total_asset'], snap['cash']
        )
        remaining_capital = daily_capital_limit
        accepted = 0
        for entry in entries:
            if entry.get('decision') == 'buy':
                if accepted >= max_entries:
                    entry['decision'] = 'watch'
                    entry['suggested_qty'] = 0
                    entry['suggested_amount'] = 0
                    entry['warnings'] = (entry.get('warnings') or []) + ['已达到当日计划开仓数量上限']
                    continue
                adjusted_quantity = professional_decision.adjusted_quantity(
                    int(entry.get('raw_suggested_qty', 0) or 0),
                    float(entry.get('previous_close', 0) or 0),
                    float(market_regime.get('position_multiplier', 0) or 0),
                    remaining_capital,
                )
                if adjusted_quantity <= 0:
                    entry['decision'] = 'blocked'
                    entry['suggested_qty'] = 0
                    entry['suggested_amount'] = 0
                    entry['gate_reasons'] = (entry.get('gate_reasons') or []) + ['组合新增资金预算不足']
                    continue
                accepted += 1
                entry['suggested_qty'] = adjusted_quantity
                entry['suggested_amount'] = round(
                    adjusted_quantity * float(entry.get('previous_close', 0) or 0), 2
                )
                entry['expected_profit'] = round(
                    entry['suggested_amount'] * max(float(entry.get('expected_holding_pct', 0) or 0), 0) / 100,
                    2,
                )
                entry['max_loss'] = round(
                    max(
                        float(entry.get('previous_close', 0) or 0)
                        - float(entry.get('stop_loss', 0) or 0),
                        0,
                    ) * adjusted_quantity,
                    2,
                )
                remaining_capital = max(remaining_capital - entry['suggested_amount'], 0)

        position_exits = []
        positions = broker.get_positions()
        position_symbols = [position.symbol for position in positions]
        position_quotes = {}
        position_histories = {}
        if position_symbols:
            with _Pool(max_workers=min(4, len(position_symbols))) as executor:
                loaded = list(executor.map(lambda item: _load_daily_frame(item, 240), position_symbols))
            position_histories = dict(zip(position_symbols, loaded))
        for position in positions:
            history = position_histories.get(position.symbol, pd.DataFrame())
            quote = position_quotes.get(position.symbol, {}) or {}
            data_quality = professional_decision.data_quality(
                history, trade_date, _premarket_holidays()
            ).to_dict()
            opportunity = opportunity_scorer.analyze(
                position.symbol,
                history,
                equity=snap['total_asset'],
                cash=snap['cash'],
                current_symbol_value=float(position.market_value or 0),
                quote=quote,
            ).to_dict()
            if data_quality.get('allowed'):
                exit_plan = premarket_analyzer.position_exit(
                    position.symbol,
                    history,
                    quantity=int(position.quantity),
                    available_quantity=int(position.available_quantity),
                    avg_cost=float(position.avg_cost),
                    current_price=float(quote.get('price', 0) or 0) or None,
                    opportunity_score=float(opportunity.get('score', 0) or 0),
                )
            else:
                exit_plan = {
                    'symbol': position.symbol,
                    'available': False,
                    'action': 'review',
                    'pending_action': 'hold',
                    'quantity': int(position.quantity),
                    'available_quantity': int(position.available_quantity),
                    'suggested_quantity': 0,
                    't1_locked': int(position.available_quantity) <= 0,
                    'reasons': ['行情质量门禁未通过，暂停自动卖出并等待数据恢复'],
                    'warnings': data_quality.get('reasons', []),
                }
            exit_plan['data_quality'] = data_quality
            exit_plan['name'] = quote.get('name') or COMMON_SYMBOL_NAMES.get(position.symbol, position.symbol)
            position_exits.append(exit_plan)

        with _premarket_lock:
            _premarket_plan = {
                'status': 'ready',
                'pool': pool,
                'trade_date': trade_date.isoformat(),
                'generated_at': datetime.now().isoformat(),
                'entries': entries[:top],
                'position_exits': position_exits,
                'buy_count': len([entry for entry in entries[:top] if entry.get('decision') == 'buy']),
                'benchmark_symbol': benchmark_symbol,
                'market_regime': market_regime,
                'capital_budget': {
                    'daily_limit': round(daily_capital_limit, 2),
                    'allocated': round(daily_capital_limit - remaining_capital, 2),
                    'remaining': round(remaining_capital, 2),
                },
                'error': '',
                'data_policy': f'仅使用 {trade_date.isoformat()} 之前已完成的日线；09:30 后必须按真实开盘价复核。',
                'disclaimer': '历史相似概率和预期收益均非保证；自动执行默认关闭，当前仅连接模拟盘。',
            }
            _persist_premarket_plan()
        return _premarket_plan_snapshot()
    except Exception as exc:
        with _premarket_lock:
            _premarket_plan['status'] = 'error'
            _premarket_plan['error'] = str(exc)
            _premarket_plan['finished_at'] = datetime.now().isoformat()
            _persist_premarket_plan()
        return _premarket_plan_snapshot()


def _execute_premarket_entry(symbol: str, now: Optional[datetime] = None) -> Dict:
    current = now or datetime.now()
    session = market_session(current)
    if not session.is_open:
        return {'success': False, 'error': f'当前为{session.label}，盘前计划只能在 09:30 后连续竞价时段复核执行'}
    plan = _premarket_plan_snapshot()
    if plan.get('status') != 'ready':
        return {'success': False, 'error': '尚无可执行的盘前计划'}
    if plan.get('trade_date') != current.date().isoformat():
        return {'success': False, 'error': '计划不是今天生成的，请先重新生成'}
    symbol = _normalize_symbol(symbol)
    entry = next((item for item in plan.get('entries', []) if item.get('symbol') == symbol), None)
    if not entry or entry.get('decision') != 'buy':
        return {'success': False, 'error': '该股票没有有效的今日买入计划'}
    if (entry.get('execution') or {}).get('success'):
        return {'success': False, 'error': '该盘前计划已经执行过，不能重复买入'}

    benchmark_symbol = _normalize_symbol(str(plan.get('benchmark_symbol', 'sh000001')))
    live_quotes = realtime.get_quotes(
        list(dict.fromkeys([symbol, benchmark_symbol])),
        sources=['tencent', 'sina', 'eastmoney'],
    )
    quote = live_quotes.get(symbol, {})
    price = round(float(quote.get('price', 0) or 0), 2)
    if price <= 0:
        return {'success': False, 'error': '开盘后仍没有有效实时价格，取消执行'}
    previous_close = float(entry.get('previous_close', 0) or 0)
    gap_pct = (price / previous_close - 1) * 100 if previous_close > 0 else 0
    max_buy_price = float(entry.get('max_buy_price', 0) or 0)
    if max_buy_price > 0 and price > max_buy_price:
        return {'success': False, 'error': f'开盘价 {price:.2f} 高于最高可接受价 {max_buy_price:.2f}，取消追高'}
    if gap_pct > premarket_analyzer.config.max_gap_up_pct:
        return {'success': False, 'error': f'高开 {gap_pct:.2f}% 超过限制，取消追高'}
    benchmark_change = float(live_quotes.get(benchmark_symbol, {}).get('change_pct', 0) or 0)
    market_shock_limit = float(config.get('professional.market_shock_pct', -2.0) or -2.0)
    if benchmark_change <= market_shock_limit:
        return {
            'success': False,
            'error': f'基准指数盘中下跌 {benchmark_change:.2f}%，触发市场冲击保护，取消开仓',
        }
    if price <= float(entry.get('stop_loss', 0) or 0):
        return {'success': False, 'error': '开盘价已跌破计划止损位，原预测失效'}

    quantity = int(entry.get('suggested_qty', 0) or 0)
    affordable = int(_portfolio_snapshot()['cash'] / max(price * (1 + broker.commission_rate), 1)) // 100 * 100
    quantity = min(quantity, affordable)
    if quantity <= 0:
        return {'success': False, 'error': '当前资金不足或建议数量无效'}
    response = place_order({
        'symbol': symbol,
        'side': 'buy',
        'quantity': quantity,
        'price': price,
        'reason': 'premarket_open_confirmed',
        'client_order_id': f"premarket-{current.date().strftime('%Y%m%d')}-{symbol}",
    })
    payload = json.loads(response.body.decode('utf-8'))
    execution = {
        'success': bool(payload.get('success')),
        'executed_at': current.isoformat(),
        'price': price,
        'quantity': quantity,
        'gap_pct': round(gap_pct, 4),
        'order_id': payload.get('order_id', ''),
        'error': payload.get('error', ''),
    }
    with _premarket_lock:
        for stored_entry in _premarket_plan.get('entries', []):
            if stored_entry.get('symbol') == symbol:
                stored_entry['execution'] = execution
                break
        _persist_premarket_plan()
    payload['execution'] = execution
    return payload


@app.get('/api/premarket/plan')
def get_premarket_plan():
    return JSONResponse(_premarket_plan_snapshot())


@app.post('/api/premarket/generate')
def generate_premarket_plan(body: dict = None):
    body = body or {}
    pool = str(body.get('pool', config.get('premarket.pool', 'watchlist'))).lower()
    top = int(body.get('top', 5) or 5)
    with _premarket_lock:
        if _premarket_plan.get('status') == 'generating':
            return JSONResponse({
                'success': False,
                'busy': True,
                'status': 'generating',
                'message': '盘前计划正在生成中',
            })
        _premarket_plan.update({
            'status': 'generating',
            'pool': pool,
            'generated_at': datetime.now().isoformat(),
            'error': '',
        })
        _persist_premarket_plan()
    worker = threading.Thread(
        target=_generate_premarket_plan,
        args=(pool, top),
        daemon=True,
    )
    worker.start()
    return JSONResponse({'success': True, 'status': 'generating', 'pool': pool})


@app.post('/api/premarket/execute')
def execute_premarket_plan(body: dict):
    return JSONResponse(_execute_premarket_entry(str(body.get('symbol', ''))))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """返回前端 HTML 页面"""
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    with open(html_path, 'r', encoding='utf-8') as f:
        return HTMLResponse(
            content=f.read(),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )


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

    # 东财支持批量请求，优先保证首页低延迟；缺失代码再逐源补全。
    sources = ['eastmoney', 'tencent', 'sina']
    quotes = realtime.get_quotes(sym_list, sources=sources)

    # 更新券商价格
    for symbol, quote in (quotes or {}).items():
        price = quote.get('price')
        if price:
            broker.update_quote(symbol, quote)

    return JSONResponse(quotes or {})


@app.get("/api/watchlist")
def get_watchlist():
    """自选股(核心池) + 实时行情。前端行情列表/首页默认显示这些。"""
    syms = _get_watchlist()
    quotes = realtime.get_quotes(syms, sources=['eastmoney', 'tencent', 'sina']) if syms else {}
    for symbol, quote in (quotes or {}).items():
        price = quote.get('price')
        if price:
            broker.update_quote(symbol, quote)
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
    # 直接带回该股行情, 前端无需再发一次 /api/quotes(去掉加自选卡顿)
    quote = {}
    try:
        quote = realtime.get_quotes([sym], sources=['tencent', 'sina']).get(sym, {}) or {}
        if quote.get('price'):
            broker.update_market_price(sym, float(quote['price']))
    except Exception:
        pass
    return JSONResponse({"success": True, "symbol": sym, "name": name or quote.get('name', ''),
                         "symbols": saved, "quote": quote})


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
def get_account(mode: str = Query("paper")):
    """获取账户信息。

    mode: paper=模拟盘(默认, 兼容旧前端) | live=实盘(基金+股票)
    """
    mode = mode.lower()
    if mode == "live":
        return _live_account_view()
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
        "mode": "paper",
        "opening_enabled": bool(_trading_control.get('opening_enabled', True)),
        "max_position_size": float(getattr(risk_mgr, 'max_position_size', 1.0)),
        "max_total_position": float(getattr(risk_mgr, 'max_total_position', 1.0)),
    })


def _live_account_view():
    """实盘账户视图 (基金 + 股票聚合)。"""
    try:
        from src.trading import get_live_snapshot
        snap = get_live_snapshot(config_path=CONFIG_PATH)
        return JSONResponse({
            "total_asset": round(snap.get("total_assets", 0), 2),
            "cash": round(snap.get("cash", 0), 2),
            "market_value": round(snap.get("market_value", 0), 2),
            "profit": round(snap.get("profit", 0), 2),
            "profit_pct": round(snap.get("profit_pct", 0), 2),
            "positions": len(snap.get("positions", [])),
            "mode": "live",
            "fund": snap.get("fund"),
            "stock": snap.get("stock"),
            "warnings": snap.get("warnings", []),
        })
    except Exception as e:
        return JSONResponse({
            "mode": "live", "total_asset": 0, "cash": 0, "market_value": 0,
            "profit": 0, "profit_pct": 0, "positions": 0,
            "error": str(e), "warnings": [str(e)],
        })


def _live_positions() -> list:
    """实盘持仓列表, 兼容前端 positions 渲染格式。

    将基金持仓 + 股票持仓统一转换为前端 renderPositions() 能识别的格式。
    """
    from src.trading import get_live_snapshot

    snap = get_live_snapshot(config_path=CONFIG_PATH)
    positions = []

    # ---- 基金持仓 ----
    fund = snap.get("fund") or {}
    for r in (fund.get("holdings") or []):
        code = str(r.get("fundCode", "") or "")
        name = str(r.get("fundName", "") or "")
        shares = float(r.get("holdVol", 0) or 0)
        value = float(r.get("totalAmount", 0) or 0)
        income = float(r.get("holdIncome", 0) or 0)
        nav = float(r.get("netValue", 0) or 0)
        cost = (value - income) / shares if shares > 0 else 0
        pnl_pct = (income / (value - income) * 100) if (value - income) > 0 else 0
        positions.append({
            "symbol": code,
            "name": name,
            "quantity": shares,
            "available_quantity": shares,
            "avg_cost": round(cost, 4),
            "last_price": nav,
            "market_value": round(value, 2),
            "unrealized_pnl": round(income, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "position_type": "fund",
        })

    # ---- 股票持仓 ----
    stock = snap.get("stock") or {}
    for p in (stock.get("positions") or []):
        sym = str(p.get("symbol", "") or "")
        qty = int(p.get("quantity", 0) or 0)
        avg_cost = float(p.get("avg_cost", 0) or 0)
        last = float(p.get("last_price", 0) or 0)
        mv = float(p.get("market_value", 0) or (qty * last))
        pnl = float(p.get("unrealized_pnl", 0) or 0)
        pnl_pct = float(p.get("unrealized_pnl_pct", 0) or 0)
        positions.append({
            "symbol": sym,
            "name": str(p.get("name", "") or ""),
            "quantity": qty,
            "available_quantity": int(p.get("available_quantity", qty) or 0),
            "avg_cost": round(avg_cost, 3),
            "last_price": last,
            "market_value": round(mv, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "position_type": "stock",
        })

    return positions


def _live_orders() -> list:
    """实盘订单列表, 兼容前端 orders 渲染格式。

    将基金交易记录 + 股票委托记录统一转换为前端 renderOrders() 能识别的格式。
    """
    orders = []

    # ---- 基金交易记录 ----
    try:
        cust_id = str(config.get("fund.cust_id", "") or "")
        if cust_id:
            rows = _fund_trader().get_order_list(cust_id, limit=20)
            from src.trading.fund_trader import FundTrader
            for r in rows:
                st = FundTrader.judge_order_status(r)
                orders.append({
                    "symbol": str(r.get("fundCode", "") or ""),
                    "direction": str(r.get("tradeType", "") or "").lower(),
                    "quantity": float(r.get("confirmVol", r.get("applyVol", 0)) or 0),
                    "price": float(r.get("confirmNav", r.get("applyNav", 0)) or 0),
                    "filled_price": float(r.get("confirmNav", r.get("applyNav", 0)) or 0),
                    "filled_quantity": float(r.get("confirmVol", 0) or 0),
                    "status": st.get("label", str(r.get("orderStatus", "") or "")),
                    "created_at": str(r.get("applyDate", r.get("orderDate", "")) or ""),
                    "updated_at": str(r.get("confirmDate", r.get("applyDate", "")) or ""),
                    "order_type": "fund",
                })
    except Exception:
        pass

    # ---- 股票委托记录 ----
    try:
        trader = _stock_trader()
        snap = trader.snapshot()
        for e in (snap.get("entrusts") or []):
            orders.append({
                "symbol": str(e.get("symbol", "") or ""),
                "direction": "sell" if str(e.get("direction", "")).lower() in ("sell", "卖出") else "buy",
                "quantity": int(e.get("quantity", 0) or 0),
                "price": float(e.get("price", 0) or 0),
                "filled_price": float(e.get("filled_price", e.get("price", 0)) or 0),
                "filled_quantity": int(e.get("filled_quantity", 0) or 0),
                "status": str(e.get("status", "") or ""),
                "created_at": str(e.get("time", "") or ""),
                "updated_at": str(e.get("time", "") or ""),
                "order_type": "stock",
            })
    except Exception:
        pass

    return orders


def _live_risk() -> dict:
    """实盘风险摘要 (精简版, 实盘无模拟风控限制)。"""
    from src.trading import get_live_snapshot
    snap = get_live_snapshot(config_path=CONFIG_PATH)
    total = float(snap.get("total_assets", 0) or 0)
    mv = float(snap.get("market_value", 0) or 0)
    return {
        "total_position_pct": (mv / total) if total > 0 else 0,
        "drawdown": 0,
        "daily_order_count": 0,
        "limits": {"max_total_position": 1.0, "max_drawdown": 1.0, "max_orders_per_day": 999},
        "total_asset": total,
        "cash": float(snap.get("cash", 0) or 0),
        "market_value": mv,
        "cash_pct": (float(snap.get("cash", 0)) / total) if total > 0 else 0,
        "position_count": len(snap.get("positions", [])),
        "positions": _live_positions(),
        "mode": "live",
    }


@app.get("/api/accounts")
def get_accounts():
    """双账户对比: 模拟盘 + 实盘。"""
    paper_resp = get_account(mode="paper")
    live_resp = _live_account_view()
    paper = json.loads(paper_resp.body) if hasattr(paper_resp, "body") else paper_resp
    live = json.loads(live_resp.body) if hasattr(live_resp, "body") else live_resp
    return JSONResponse({
        "paper": paper,
        "live": live,
        "server_time": datetime.now().isoformat(),
    })


@app.get("/api/system/status")
def get_system_status():
    session = market_session()
    fund_ready = bool((config.get('fund.cust_id', '') or '').strip())
    guling_token = str(config.get('broker.guling_agent_token', '') or '').strip()
    return JSONResponse({
        "modes": ["paper", "live"],
        "mode": "paper",
        "mode_label": "A股模拟盘",
        "broker": "SimulatedBroker",
        "live": {
            "fund_ready": fund_ready,
            "stock_ready": bool(guling_token),
        },
        "market_session": session.code,
        "market_session_label": session.label,
        "market_open": session.is_open,
        "opening_enabled": bool(_trading_control.get('opening_enabled', True)),
        "enforce_market_hours": bool(broker.enforce_market_hours),
        "t_plus_one": True,
        "buy_lot_size": 100,
        "state_persistence": True,
        "approval_gate": True,
        "quote_sources": realtime.get_source_health(),
        "server_time": datetime.now().isoformat(),
    })


def _stream_snapshot(extra_symbols: Optional[List[str]] = None, mode: str = "paper") -> Dict:
    """把页面需要的轻量数据合成一帧，供 SSE 与降级轮询共用。

    mode: paper=模拟盘(默认) | live=实盘(基金+股票聚合)
    """
    session = market_session()
    quotes = {}
    watch = _get_watchlist()
    quote_error = None
    try:
        quote_symbols = list(dict.fromkeys(
            watch + [_normalize_symbol(symbol) for symbol in (extra_symbols or []) if symbol]
        ))
        if quote_symbols:
            quotes = realtime.get_quotes(
                quote_symbols,
                sources=['eastmoney', 'tencent', 'sina'],
            ) or {}
            for symbol, quote in quotes.items():
                if quote.get('price'):
                    broker.update_quote(symbol, quote)
    except Exception as exc:  # 行情源抖动不应中断整条流
        quotes = {}
        quote_error = str(exc)

    # ── 实盘模式: account/positions/orders/risk 走实盘聚合, quotes/system 不变 ──
    if mode.lower() == "live":
        try:
            live_acct_raw = _live_account_view()
            live_acct = json.loads(live_acct_raw.body) if hasattr(live_acct_raw, "body") else live_acct_raw
            live_positions = _live_positions()
            live_orders = _live_orders()
            total = float(live_acct.get("total_asset", 0) or 0)
            mv = float(live_acct.get("market_value", 0) or 0)
            live_risk = {
                "total_position_pct": (mv / total) if total > 0 else 0,
                "drawdown": 0,
                "daily_order_count": 0,
                "limits": {"max_total_position": 1.0, "max_drawdown": 1.0, "max_orders_per_day": 999},
                "total_asset": total,
                "cash": float(live_acct.get("cash", 0) or 0),
                "market_value": mv,
                "cash_pct": (float(live_acct.get("cash", 0)) / total) if total > 0 else 0,
                "position_count": len(live_positions),
                "positions": live_positions,
                "mode": "live",
            }
            return {
                "quotes": quotes,
                "watchlist": watch,
                "account": {
                    "total_asset": total,
                    "initial_capital": 0,
                    "cash": float(live_acct.get("cash", 0) or 0),
                    "profit": float(live_acct.get("profit", 0) or 0),
                    "profit_pct": float(live_acct.get("profit_pct", 0) or 0),
                    "market_value": mv,
                    "positions": len(live_positions),
                    "mode": "live",
                    "fund": live_acct.get("fund"),
                    "stock": live_acct.get("stock"),
                    "warnings": live_acct.get("warnings", []),
                    "opening_enabled": True,
                },
                "positions": live_positions,
                "orders": live_orders,
                "risk": live_risk,
                "system": {
                    "mode": "live",
                    "mode_label": "实盘账户",
                    "market_session": session.code,
                    "market_session_label": session.label,
                    "market_open": session.is_open,
                    "opening_enabled": True,
                    "enforce_market_hours": False,
                    "t_plus_one": True,
                    "buy_lot_size": 100,
                    "state_persistence": False,
                    "approval_gate": True,
                    "server_time": datetime.now().isoformat(),
                },
                "server_time": datetime.now().isoformat(),
            }
        except Exception as exc:
            # 实盘聚合失败 → 返回空壳, 不阻塞行情
            return {
                "quotes": quotes, "watchlist": watch,
                "account": {"total_asset": 0, "cash": 0, "market_value": 0, "profit": 0, "mode": "live", "error": str(exc)},
                "positions": [], "orders": [],
                "risk": {"total_position_pct": 0, "mode": "live", "error": str(exc)},
                "system": {"mode": "live", "mode_label": "实盘账户", "market_open": session.is_open, "market_session_label": session.label, "opening_enabled": True, "enforce_market_hours": False, "server_time": datetime.now().isoformat()},
                "server_time": datetime.now().isoformat(),
            }

    # 行情先写入模拟券商，再计算资产、持仓和风控，避免快照出现一帧延迟。
    try:
        info = broker.get_account_info()
        snap = _portfolio_snapshot()
        report = risk_mgr.get_risk_report()
        report.update({
            "total_asset": snap["total_asset"],
            "cash": snap["cash"],
            "market_value": snap["market_value"],
            "total_position_pct": snap["total_position_pct"],
            "cash_pct": snap["cash"] / max(snap["total_asset"], 1),
            "position_count": len(snap["positions"]),
        })
    except Exception as exc:
        info = {"total_asset": 0, "initial_capital": broker.initial_capital, "cash": 0, "profit": 0, "profit_pct": 0, "market_value": 0}
        snap = {"positions": [], "total_asset": 0, "cash": 0, "market_value": 0, "total_position_pct": 0}
        report = {"error": str(exc), "total_position_pct": 0, "drawdown": 0, "daily_order_count": 0}
    if quote_error:
        report['quote_error'] = quote_error

    try:
        order_frame = broker.get_order_history()
        orders = (
            []
            if order_frame.empty
            else json.loads(order_frame.tail(20).to_json(orient="records", force_ascii=False))
        )
    except Exception as exc:
        orders = []
        report.setdefault('order_error', str(exc))

    return {
        "quotes": quotes,
        "watchlist": watch,
        "account": {
            "total_asset": float(info.get('total_asset', 0) or 0),
            "initial_capital": float(info.get('initial_capital', broker.initial_capital)),
            "cash": float(info.get('cash', 0) or 0),
            "profit": float(info.get('profit', 0) or 0),
            "profit_pct": float(info.get('profit_pct', 0) or 0),
            "market_value": float(info.get('market_value', 0) or 0),
            "positions": len(broker.get_positions()),
            "commission_rate": broker.commission_rate,
            "commission_rate_wan": round(broker.commission_rate * 10000, 1),
            "stamp_tax_rate": broker.stamp_tax_rate,
            "stamp_tax_qian": round(broker.stamp_tax_rate * 1000, 1),
            "min_commission": broker.min_commission,
            "slippage": getattr(broker, 'slippage', 0.0001),
            "slippage_wan": round(getattr(broker, 'slippage', 0.0001) * 10000, 1),
            "mode": "paper",
            "opening_enabled": bool(_trading_control.get('opening_enabled', True)),
        },
        "positions": _paper_positions_with_names(),
        "orders": orders,
        "risk": report,
        "system": {
            "mode": "paper",
            "mode_label": "A股模拟盘",
            "market_session": session.code,
            "market_session_label": session.label,
            "market_open": session.is_open,
            "opening_enabled": bool(_trading_control.get('opening_enabled', True)),
            "enforce_market_hours": bool(broker.enforce_market_hours),
            "t_plus_one": True,
            "buy_lot_size": 100,
            "state_persistence": True,
            "approval_gate": True,
            "server_time": datetime.now().isoformat(),
        },
        "server_time": datetime.now().isoformat(),
    }


@app.get("/api/stream")
async def stream_updates(symbol: Optional[str] = Query(None), mode: str = Query("paper")):
    """SSE 增量推送：交易时段 3s，休市 30s；内容无变化时只发心跳。

    取代前端过去的多组 setInterval 全量轮询，避免整页重建导致的闪烁。
    mode: paper=模拟盘 | live=实盘 (live 模式降低推送频率, 实盘数据不需 3s 刷新)
    """
    async def event_source():
        last_digest = None
        event_id = 0
        # 建连立刻推一帧完整快照，前端无需再等第一个轮询周期
        while True:
            try:
                extras = [symbol] if symbol else []
                payload = await asyncio.to_thread(_stream_snapshot, extras, mode)
                body = json.dumps(payload, ensure_ascii=False, default=str)
                # server_time 每帧都会变化，不应因此触发整帧重绘。
                comparable = dict(payload)
                comparable.pop("server_time", None)
                if isinstance(comparable.get("system"), dict):
                    comparable["system"] = dict(comparable["system"])
                    comparable["system"].pop("server_time", None)
                digest = hash(json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str))
                if digest != last_digest:
                    last_digest = digest
                    event_id += 1
                    yield f"id: {event_id}\nretry: 5000\nevent: snapshot\ndata: {body}\n\n"
                else:
                    yield ": keep-alive\n\n"
                open_now = bool(payload.get('system', {}).get('market_open'))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"event: stream-error\ndata: {err}\n\n"
                open_now = False
            # 实盘模式降低频率: 交易时段 10s, 休市 60s (实盘数据无需 3s 刷新)
            is_live = mode.lower() == "live"
            await asyncio.sleep((10 if is_live else 3) if open_now else (60 if is_live else 30))

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/snapshot")
async def get_dashboard_snapshot(symbol: Optional[str] = Query(None), mode: str = Query("paper")):
    """SSE 不可用时的单请求快照，也供用户手动刷新使用。

    mode: paper=模拟盘(默认) | live=实盘
    """
    extras = [symbol] if symbol else []
    try:
        payload = await asyncio.to_thread(_stream_snapshot, extras, mode)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": str(exc), "quotes": {}, "account": {}, "positions": [], "orders": [], "system": {}},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/trade/approvals")
def get_trade_approvals(limit: int = Query(50, ge=1, le=200)):
    with _approval_lock:
        return JSONResponse(list(reversed(_approval_log[-limit:])))


@app.post("/api/risk/limits")
@_locked_broker_operation
def update_risk_limits(body: dict):
    """只改仓位上限, 不重置账户。UI「设置」页用它调整单股/总仓位上限。"""
    try:
        changed = {}
        if 'max_position_size' in body:
            v = float(body.get('max_position_size'))
            if not 0.01 <= v <= 1.0:
                return JSONResponse({"success": False, "error": "单股仓位上限必须在 1% 到 100% 之间"})
            risk_mgr.max_position_size = v
            config.set('risk.max_position_size', v)
            changed['max_position_size'] = v
        if 'max_total_position' in body:
            v = float(body.get('max_total_position'))
            if not 0.01 <= v <= 1.0:
                return JSONResponse({"success": False, "error": "总仓位上限必须在 1% 到 100% 之间"})
            risk_mgr.max_total_position = v
            config.set('risk.max_total_position', v)
            changed['max_total_position'] = v
        if not changed:
            return JSONResponse({"success": False, "error": "没有需要更新的字段"})
        config.save_config(CONFIG_PATH)
        return JSONResponse({
            "success": True,
            "max_position_size": float(risk_mgr.max_position_size),
            "max_total_position": float(risk_mgr.max_total_position),
            "changed": changed,
        })
    except (TypeError, ValueError) as exc:
        return JSONResponse({"success": False, "error": f"参数无效: {exc}"})


@app.post("/api/trading/control")
@_locked_broker_operation
def update_trading_control(body: dict):
    enabled = body.get('opening_enabled')
    if not isinstance(enabled, bool):
        return JSONResponse({"success": False, "error": "opening_enabled 必须是布尔值"})
    _trading_control['opening_enabled'] = enabled
    _trading_control['updated_at'] = datetime.now().isoformat()
    _persist_trading_control()
    return JSONResponse({
        "success": True,
        "opening_enabled": enabled,
        "message": "已允许开仓" if enabled else "已锁定开仓，仅允许卖出",
    })


@app.post("/api/account/update")
@_locked_broker_operation
def update_account(body: dict):
    """
    更新佣金、印花税和滑点等账户参数，不改变资金、持仓或订单。
    """
    try:
        commission_rate = float(body.get('commission_rate', broker.commission_rate))
        stamp_tax_rate = float(body.get('stamp_tax_rate', broker.stamp_tax_rate))
        min_commission = float(body.get('min_commission', broker.min_commission))
        slippage = float(body.get('slippage', broker.slippage))

        if not 0 <= commission_rate <= 0.05:
            return JSONResponse({"success": False, "error": "佣金费率必须在 0 到 5% 之间"})
        if not 0 <= stamp_tax_rate <= 0.05:
            return JSONResponse({"success": False, "error": "印花税率必须在 0 到 5% 之间"})
        if not 0 <= min_commission <= 10000:
            return JSONResponse({"success": False, "error": "最低佣金超出有效范围"})
        if not 0 <= slippage <= 0.10:
            return JSONResponse({"success": False, "error": "滑点必须在 0 到 10% 之间"})

        broker.commission_rate = commission_rate
        broker.stamp_tax_rate = stamp_tax_rate
        broker.min_commission = min_commission
        broker.slippage = slippage

        # 同步写入 config.yaml（持久化）
        config.set('commission.rate', commission_rate)
        config.set('commission.min', int(min_commission))
        config.set('commission.stamp_tax', stamp_tax_rate)
        config.set('trading.slippage', slippage)
        config.save_config(CONFIG_PATH)
        _persist_broker_state()

        info = broker.get_account_info()
        return JSONResponse({
            "success": True,
            "message": "账户参数已保存，持仓和订单未受影响",
            "account": {
                "total_asset": float(info.get('total_asset', 0) or 0),
                "initial_capital": float(info.get('initial_capital', broker.initial_capital)),
                "cash": float(info.get('cash', 0) or 0),
                "profit": float(info.get('profit', 0) or 0),
                "profit_pct": float(info.get('profit_pct', 0) or 0),
                "positions": len(broker.get_positions()),
                "commission_rate": commission_rate,
                "commission_rate_wan": round(commission_rate * 10000, 1),
                "stamp_tax_rate": stamp_tax_rate,
                "stamp_tax_qian": round(stamp_tax_rate * 1000, 1),
                "min_commission": min_commission,
                "slippage": slippage,
                "slippage_wan": round(slippage * 10000, 1),
                "market_value": float(info.get('market_value', 0) or 0),
            }
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/account/deposit")
@_locked_broker_operation
def deposit_account(body: dict):
    """向模拟账户添加资金，同时完整保留现有持仓、订单和交易记录。"""
    try:
        amount = float(body.get('amount', 0))
        info = broker.add_funds(amount)

        # 同步抬高资金基准和回撤高水位，避免把入金误计为盈利。
        risk_mgr.peak_equity = max(
            float(risk_mgr.peak_equity) + amount,
            float(info.get('total_asset', 0) or 0),
        )
        config.set('trading.initial_capital', broker.initial_capital)
        config.save_config(CONFIG_PATH)
        _persist_broker_state()
        _persist_risk_runtime()

        return JSONResponse({
            "success": True,
            "message": f"已添加资金 {amount:,.2f} 元，持仓和订单未受影响",
            "amount": amount,
            "account": {
                "total_asset": float(info.get('total_asset', 0) or 0),
                "initial_capital": float(info.get('initial_capital', broker.initial_capital)),
                "cash": float(info.get('cash', 0) or 0),
                "profit": float(info.get('profit', 0) or 0),
                "profit_pct": float(info.get('profit_pct', 0) or 0),
                "positions": len(broker.get_positions()),
                "market_value": float(info.get('market_value', 0) or 0),
            },
        })
    except (TypeError, ValueError) as exc:
        return JSONResponse({"success": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse({"success": False, "error": f"添加资金失败: {exc}"})


def _strategy_universe(watchlist: List[str], held_symbols: List[str], limit: int = 12) -> List[str]:
    """持仓优先，随后补充自选；持仓永远不能被策略页过滤掉。"""
    combined = [
        _normalize_symbol(symbol)
        for symbol in list(held_symbols) + list(watchlist)
        if symbol
    ]
    return list(dict.fromkeys(combined))[:max(int(limit), 1)]


def _strategy_position_advice(
    last_signal: Optional[str],
    position,
    exit_plan: Optional[Dict],
    opportunity: Dict,
    current_price: float,
    recovery_analysis: Optional[Dict] = None,
) -> Dict:
    stop_price = float((exit_plan or {}).get('protective_stop', 0) or opportunity.get('stop_loss', 0) or 0)
    target_price = float((exit_plan or {}).get('target_price', 0) or opportunity.get('target_price', 0) or 0)
    if position is None:
        if last_signal == 'buy':
            suggested = int(opportunity.get('suggested_qty', 0) or 0)
            return {
                'decision': 'buy',
                'label': '空仓：出现买点',
                'meaning': '当前没有持仓；买入信号表示可以进入交易计划，但仍需通过价格和资金风控。',
                'detail': (
                    f"建议先确认价格位于买入区 {float(opportunity.get('buy_low', 0) or 0):.2f}–"
                    f"{float(opportunity.get('buy_high', 0) or 0):.2f}；"
                    + (f"按当前本金最多参考 {suggested} 股。" if suggested else "当前潜力评分未给出可执行数量，暂不下单。")
                ),
                'suggested_quantity': suggested,
            }
        if last_signal == 'sell':
            return {
                'decision': 'avoid',
                'label': '空仓：继续回避',
                'meaning': '当前没有持仓，因此卖出信号不需要执行卖单。',
                'detail': '保持空仓，不抄底；等待新的买入条件形成。',
                'suggested_quantity': 0,
            }
        return {
            'decision': 'wait',
            'label': '空仓观望：暂不买入',
            'meaning': '观望对空仓账户的含义是“不买、不卖”，不是遗漏建议。',
            'detail': '当前没有形成新的买入条件，继续等待；不要因为价格波动临时追涨。',
            'suggested_quantity': 0,
        }

    if recovery_analysis:
        return dict(recovery_analysis)

    quantity = int(getattr(position, 'quantity', 0) or 0)
    available_quantity = int(getattr(position, 'available_quantity', quantity) or 0)
    avg_cost = float(getattr(position, 'avg_cost', 0) or 0)
    pnl_pct = (current_price / avg_cost - 1) * 100 if current_price > 0 and avg_cost > 0 else 0
    exit_action = str((exit_plan or {}).get('action', 'hold'))
    pending_action = str((exit_plan or {}).get('pending_action', 'hold'))
    if last_signal == 'sell' and exit_action == 'hold':
        exit_action = 'sell' if available_quantity > 0 else 'wait_t1'
        pending_action = 'sell'

    if exit_action == 'wait_t1':
        return {
            'decision': 'wait_t1',
            'label': '持仓：T+1 锁定，等待可卖',
            'meaning': '退出条件已经出现，但这部分股票是当日买入，今天依法不能卖出。',
            'detail': f"持有 {quantity} 股、当前可卖 0 股；下一交易日开盘后重新复核，若条件仍成立则卖出。",
            'suggested_quantity': 0,
            'pending_action': pending_action,
        }
    if exit_action == 'sell':
        suggested = int((exit_plan or {}).get('suggested_quantity', available_quantity) or available_quantity)
        suggested = min(suggested, available_quantity)
        return {
            'decision': 'sell',
            'label': f'持仓：建议卖出 {suggested} 股',
            'meaning': '趋势或保护位已触发，当前建议优先降低风险，不再加仓。',
            'detail': f"持有 {quantity} 股、可卖 {available_quantity} 股，成本 {avg_cost:.2f}，当前盈亏 {pnl_pct:+.2f}%。",
            'suggested_quantity': suggested,
        }
    if exit_action == 'reduce':
        suggested = int((exit_plan or {}).get('suggested_quantity', 0) or 0)
        return {
            'decision': 'reduce',
            'label': f'持仓：建议减仓 {suggested} 股',
            'meaning': '达到阶段止盈或短线过热，建议分批落袋，不必一次清仓。',
            'detail': f"减仓后保留底仓；剩余仓位以保护位 {stop_price:.2f} 管理。",
            'suggested_quantity': suggested,
        }

    signal_note = '策略仍偏多，但已有持仓不自动重复加仓。' if last_signal == 'buy' else '没有新的买卖触发，观望对持仓的含义是继续持有。'
    return {
        'decision': 'hold',
        'label': '持仓观望：继续持有',
        'meaning': signal_note,
        'detail': (
            f"持有 {quantity} 股、可卖 {available_quantity} 股，成本 {avg_cost:.2f}，当前盈亏 {pnl_pct:+.2f}%；"
            f"跌破保护位 {stop_price:.2f} 考虑卖出，接近目标位 {target_price:.2f} 分批止盈。"
        ),
        'suggested_quantity': 0,
    }


def _get_strategy_research_sources() -> Dict[str, object]:
    with _strategy_research_lock:
        if not _strategy_research_sources:
            from src.data.capital_flow import CapitalFlowFetcher
            from src.data.fundamentals import FundamentalsFetcher
            from src.news.news_analyzer import NewsAnalyzer

            _strategy_research_sources.update({
                'news': NewsAnalyzer(),
                'fundamentals': FundamentalsFetcher(),
                'capital': CapitalFlowFetcher(),
            })
        return dict(_strategy_research_sources)


def _strategy_research_bundle(symbol: str) -> Dict:
    try:
        sources = _get_strategy_research_sources()
    except Exception:
        return {
            'news': [], 'fundamentals': {}, 'capital': {},
            'source_status': {
                'news': {'available': False, 'error': '研究数据源初始化失败'},
                'fundamental': {'available': False, 'error': '研究数据源初始化失败'},
                'capital': {'available': False, 'error': '研究数据源初始化失败'},
            },
        }
    result = {
        'news': [], 'fundamentals': {}, 'capital': {},
        'source_status': {
            'news': {'available': False, 'sources': [], 'records': 0},
            'fundamental': {'available': False, 'sources': [], 'fields': 0},
            'capital': {'available': False, 'sources': [], 'records': 0},
        },
    }

    def _fetch_news():
        status = {'available': False, 'sources': [], 'records': 0}
        try:
            if hasattr(sources['news'], 'get_symbol_news_with_meta'):
                response = sources['news'].get_symbol_news_with_meta(symbol, count=8)
                items = response.get('items', [])
                status.update(response.get('status', {}))
            else:
                items = sources['news'].get_symbol_news(symbol, count=8)
                status.update({'available': bool(items), 'records': len(items)})
            return 'news', items, status
        except Exception as exc:
            status['error'] = str(exc)[:160]
            return 'news', [], status

    def _fetch_fundamentals():
        status = {'available': False, 'sources': [], 'fields': 0}
        try:
            fundamentals = sources['fundamentals'].get(symbol) or {}
            observed = int(fundamentals.get('_field_count', 0) or 0)
            if observed <= 0:
                observed = sum(
                    fundamentals.get(key) is not None
                    for key in ('pe_ttm', 'pe', 'pb', 'roe', 'revenue_yoy', 'profit_yoy')
                )
            status.update({
                'available': observed > 0,
                'fields': observed,
                'sources': list(fundamentals.get('_sources', [])),
            })
            return 'fundamentals', fundamentals, status
        except Exception as exc:
            status['error'] = str(exc)[:160]
            return 'fundamentals', {}, status

    def _fetch_capital():
        status = {'available': False, 'sources': [], 'records': 0}
        try:
            capital = sources['capital'].get_main_net_summary(symbol) or {}
            if capital:
                capital['source'] = '东方财富当日分钟资金流'
                status.update({
                    'available': True,
                    'sources': ['东方财富当日分钟资金流'],
                    'records': int(capital.get('points', 0) or 0),
                })
                return 'capital', capital, status
        except Exception as exc:
            status['error'] = str(exc)[:160]

        # 非交易时段没有分钟流属于正常现象，按缺口回退近 20 日资金流。
        try:
            from src.data.sources.fundamental import fund_flow_summary

            historical = fund_flow_summary(symbol, recent_days=20) or {}
            if historical:
                latest = (historical.get('recent_main_net') or [{}])[-1]
                historical['last_main_net'] = float(latest.get('main_net', 0) or 0)
                historical['source'] = '东方财富近20日资金流'
                status.update({
                    'available': True,
                    'sources': ['东方财富近20日资金流'],
                    'records': int(historical.get('days', 0) or 0),
                    'fallback': True,
                })
                return 'capital', historical, status
        except Exception as exc:
            if not status.get('error'):
                status['error'] = str(exc)[:160]
        return 'capital', {}, status

    # 三类证据互不依赖，并行获取；东财客户端仍会自行节流，避免提高封禁风险。
    with _Pool(max_workers=3) as executor:
        fetched = list(executor.map(
            lambda function: function(),
            (_fetch_news, _fetch_fundamentals, _fetch_capital),
        ))
    for key, payload, status in fetched:
        result[key] = payload
        status_key = 'fundamental' if key == 'fundamentals' else key
        result['source_status'][status_key].update(status)
    return result


def _strategy_context_snapshot() -> tuple:
    snap = _portfolio_snapshot()
    held_symbols = [position.symbol for position in snap['positions']]
    symbols = _strategy_universe(_get_watchlist(), held_symbols)
    positions = []
    for position in sorted(snap['positions'], key=lambda item: item.symbol):
        positions.append({
            'symbol': position.symbol,
            'quantity': int(getattr(position, 'quantity', 0) or 0),
            'available_quantity': int(getattr(position, 'available_quantity', 0) or 0),
            'avg_cost': round(float(getattr(position, 'avg_cost', 0) or 0), 4),
        })
    context = {
        'symbols': symbols,
        'positions': positions,
        'cash': round(float(snap.get('cash', 0) or 0), 2),
        'total_asset': round(float(snap.get('total_asset', 0) or 0), 2),
        'target_date': target_trading_date(datetime.now(), _premarket_holidays()).isoformat(),
    }
    signature = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return snap, symbols, signature


def _compute_strategies(strategy: str) -> Dict:
    if strategy not in STRATEGY_LABELS:
        strategy = 'cross_ma'
    snap, symbols, context_signature = _strategy_context_snapshot()
    params, label = _default_params(strategy, 5, 20)
    quotes = realtime.get_quotes(symbols, sources=['tencent', 'sina', 'eastmoney']) or {}
    strategy_target_date = target_trading_date(datetime.now(), _premarket_holidays())
    benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
    benchmark = _load_daily_frame(benchmark_symbol, 240)
    market_regime = professional_decision.market_regime(benchmark).to_dict()

    def _one(symbol):
        frame = _load_daily_frame(symbol, 180)
        data_quality = professional_decision.data_quality(
            frame, strategy_target_date, _premarket_holidays()
        ).to_dict()
        closes = [float(value) for value in frame['Close'].tolist()] if not frame.empty else []
        opens = [float(value) for value in frame['Open'].tolist()] if not frame.empty else []
        dates = [str(index).split(' ')[0] for index in frame.index] if not frame.empty else []
        position = snap['pos_map'].get(symbol)
        if len(closes) < 25:
            return {
                'name': label,
                'symbol': symbol,
                'stock_name': quotes.get(symbol, {}).get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol),
                'pnl_pct': 0,
                'win_rate': 0,
                'max_dd': 0,
                'trades': 0,
                'buy_hold': 0,
                'signal': '无数据',
                'action': 'hold',
                'available': False,
                'advice': {
                    'decision': 'hold',
                    'label': '持仓：数据不足，暂不自动操作' if position else '空仓：数据不足，暂不买入',
                    'meaning': '历史日线不足，系统不会用不完整数据给出买卖结论。',
                    'detail': '请等待行情数据恢复后由后台重新分析。',
                    'suggested_quantity': 0,
                    'suggested_buy_quantity': 0,
                    'suggested_sell_quantity': 0,
                },
                'position': position.to_dict() if position else None,
                'data_quality': data_quality,
                'market_regime': market_regime,
                'recovery_analysis': None,
            }
        if strategy == 'potential':
            result = _run_potential_backtest(frame, symbol)
            signal_value = int(PotentialStrategy().generate_signals(frame)['signal'].iloc[-1])
            last_signal = 'buy' if signal_value == 1 else 'sell' if signal_value == -1 else None
        else:
            result = _run_strategy_backtest(
                closes,
                opens,
                dates,
                strategy,
                params,
                symbol=symbol,
                frame=frame,
            )
            last_signal = _signal_at(closes, len(closes) - 1, strategy, params)
        signal_label = {'buy': '买入', 'sell': '卖出'}.get(last_signal, '观望')
        quote = quotes.get(symbol, {}) or {}
        current_price = float(quote.get('price', 0) or closes[-1])
        opportunity = opportunity_scorer.analyze(
            symbol,
            frame,
            equity=snap['total_asset'],
            cash=snap['cash'],
            current_symbol_value=float(getattr(position, 'market_value', 0) or 0) if position else 0,
            quote={**quote, 'price': current_price},
        ).to_dict()
        exit_plan = None
        recovery_analysis = None
        if position:
            exit_plan = premarket_analyzer.position_exit(
                symbol,
                frame,
                quantity=int(position.quantity),
                available_quantity=int(position.available_quantity),
                avg_cost=float(position.avg_cost),
                current_price=current_price,
                opportunity_score=float(opportunity.get('score', 0) or 0),
            )
            if current_price < float(position.avg_cost):
                research = _strategy_research_bundle(symbol)
                recovery_analysis = holding_recovery_analyzer.analyze(
                    symbol,
                    frame,
                    quantity=int(position.quantity),
                    available_quantity=int(position.available_quantity),
                    avg_cost=float(position.avg_cost),
                    current_price=current_price,
                    opportunity=opportunity,
                    exit_plan=exit_plan,
                    market_regime=market_regime,
                    news_items=research['news'],
                    fundamentals=research['fundamentals'],
                    capital_flow=research['capital'],
                )
        advice = _strategy_position_advice(
            last_signal,
            position,
            exit_plan,
            opportunity,
            current_price,
            recovery_analysis,
        )
        if not data_quality.get('allowed'):
            advice = {
                'decision': 'review',
                'label': '数据异常：暂停自动操作',
                'meaning': '行情时效或完整性未通过专业门禁，当前策略信号不具备执行资格。',
                'detail': '已有持仓先保持不动并人工核对行情；空仓不要新开仓。数据恢复后后台会重新刷新。',
                'suggested_quantity': 0,
                'suggested_buy_quantity': 0,
                'suggested_sell_quantity': 0,
            }
        position_payload = position.to_dict() if position else None
        if position_payload:
            position_payload['current_price'] = round(current_price, 4)
            position_payload['market_value'] = round(position.quantity * current_price, 2)
            position_payload['unrealized_pnl'] = round(
                (current_price - position.avg_cost) * position.quantity,
                2,
            )
        return {
            'name': label,
            'symbol': symbol,
            'stock_name': quotes.get(symbol, {}).get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol),
            'pnl_pct': result['return_pct'],
            'win_rate': round(result['win_rate'] / 100, 3),
            'max_dd': -abs(result['max_drawdown']),
            'trades': len([trade for trade in result['trades'] if trade.get('side') == 'sell']),
            'buy_hold': result['buy_hold_return'],
            'signal': signal_label,
            'raw_action': last_signal or 'hold',
            'action': advice['decision'],
            'advice': advice,
            'position': position_payload,
            'exit_plan': exit_plan,
            'recovery_analysis': recovery_analysis,
            'market_regime': market_regime,
            'opportunity': {
                'score': opportunity.get('score', 0),
                'buy_low': opportunity.get('buy_low', 0),
                'buy_high': opportunity.get('buy_high', 0),
                'stop_loss': opportunity.get('stop_loss', 0),
                'target_price': opportunity.get('target_price', 0),
            },
            'data_quality': data_quality,
            'available': True,
        }

    items = []
    if symbols:
        with _Pool(max_workers=min(6, len(symbols))) as executor:
            items = list(executor.map(_one, symbols))
    return {
        'strategy': strategy,
        'label': label,
        'items': items,
        'market_regime': market_regime,
        '_context_signature': context_signature,
    }


def _strategy_cache_is_stale(entry: Dict, context_signature: str) -> bool:
    if not entry or not entry.get('generated_at'):
        return True
    if entry.get('_context_signature') != context_signature:
        return True
    try:
        generated_at = datetime.fromisoformat(str(entry['generated_at']))
        age = max((datetime.now() - generated_at).total_seconds(), 0)
    except (TypeError, ValueError):
        return True
    ttl = max(int(config.get('strategy.cache_seconds', 300) or 300), 30)
    return age > ttl


def _strategy_refresh_backoff_active(entry: Dict) -> bool:
    if not entry.get('failed_at'):
        return False
    try:
        failed_at = datetime.fromisoformat(str(entry['failed_at']))
        age = max((datetime.now() - failed_at).total_seconds(), 0)
    except (TypeError, ValueError):
        return False
    retry_seconds = max(int(config.get('strategy.retry_seconds', 30) or 30), 5)
    return age < retry_seconds


def _refresh_strategy_cache(strategy: str):
    try:
        payload = _compute_strategies(strategy)
        payload['generated_at'] = datetime.now().isoformat(timespec='seconds')
        payload['last_error'] = ''
        with _strategy_cache_lock:
            _strategy_cache[strategy] = payload
    except Exception as exc:
        with _strategy_cache_lock:
            previous = dict(_strategy_cache.get(strategy) or {})
            previous['last_error'] = str(exc)
            previous['failed_at'] = datetime.now().isoformat(timespec='seconds')
            _strategy_cache[strategy] = previous
    finally:
        with _strategy_cache_lock:
            _strategy_refreshing.discard(strategy)
            persisted = dict(_strategy_cache)
        try:
            state_manager.save_account_state('strategy_cache_v1', persisted)
        except Exception:
            pass


def _ensure_strategy_refresh(
    strategy: str,
    force: bool = False,
    context_signature: Optional[str] = None,
) -> bool:
    if strategy not in STRATEGY_LABELS:
        strategy = 'cross_ma'
    if context_signature is None:
        _, _, context_signature = _strategy_context_snapshot()
    with _strategy_cache_lock:
        entry = dict(_strategy_cache.get(strategy) or {})
        stale = _strategy_cache_is_stale(entry, context_signature)
        if strategy in _strategy_refreshing:
            return False
        if not force and (not stale or _strategy_refresh_backoff_active(entry)):
            return False
        _strategy_refreshing.add(strategy)
    threading.Thread(
        target=_refresh_strategy_cache,
        args=(strategy,),
        daemon=True,
        name=f'strategy-refresh-{strategy}',
    ).start()
    return True


def _strategy_cache_response(strategy: str, force: bool = False) -> Dict:
    if strategy not in STRATEGY_LABELS:
        strategy = 'cross_ma'
    _, _, context_signature = _strategy_context_snapshot()
    _ensure_strategy_refresh(strategy, force=force, context_signature=context_signature)
    with _strategy_cache_lock:
        entry = dict(_strategy_cache.get(strategy) or {})
        refreshing = strategy in _strategy_refreshing
    stale = _strategy_cache_is_stale(entry, context_signature)
    entry.pop('_context_signature', None)
    _, label = _default_params(strategy, 5, 20)
    entry.setdefault('strategy', strategy)
    entry.setdefault('label', label)
    entry.setdefault('items', [])
    entry['refreshing'] = refreshing
    entry['stale'] = stale
    if refreshing and entry.get('items'):
        entry['message'] = '后台正在更新行情、回测和持仓判断，当前展示上次结果。'
    elif refreshing:
        entry['message'] = '首次策略分析正在后台运行，完成后会自动显示，不影响浏览其他页面。'
    elif entry.get('last_error'):
        entry['message'] = f"后台刷新失败，保留上次结果：{entry['last_error']}"
    else:
        entry['message'] = '策略结果已由后台缓存，可直接查看。'
    return entry


@app.get('/api/strategies')
def get_strategies(
    strategy: str = Query('cross_ma'),
    refresh: bool = Query(False),
):
    """立即返回缓存结果，并在过期或手动刷新时启动后台分析。"""
    return JSONResponse(_strategy_cache_response(strategy, force=refresh))



@app.get("/api/ai_signals")
def get_ai_signals():
    """立即返回专业信号缓存，过期分析在后台刷新，不阻塞首页。"""
    symbols = [_normalize_symbol(s) for s in config.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002'])]
    return JSONResponse(_dashboard_signals_snapshot(symbols))


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
            "schema_version": _AI_PICK_SCHEMA_VERSION,
            "legacy_snapshot": False,
            "status": "running", "pool": pool, "total": 0, "done": 0,
            "current": "", "picks": [], "candidates": 0,
            "requested": top, "prescreen_passed": 0, "prescreen_fallback": 0,
            "prescreen_rejected": 0, "prescreen_reasons": {},
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
    picks = [dict(item) for item in job.get("picks", [])]
    _live_ttl = float(config.get('data_source.pick_status_cache_seconds', 2.0) or 0)
    _cache_key = f"{job.get('finished_at')}|{len(picks)}"
    _now_ts = time.time()
    with _scan_lock:
        _cached = (
            _scan_live_cache["picks"]
            if _scan_live_cache["key"] == _cache_key and _scan_live_cache["expire"] > _now_ts
            else None
        )
    if _cached is not None:
        picks = [dict(item) for item in _cached]
    elif job.get('status') == 'done' and picks:
        symbols = [item.get('symbol') for item in picks if item.get('symbol')]
        quotes = realtime.get_quotes(
            symbols,
            sources=['tencent', 'sina', 'eastmoney'],
        ) if symbols else {}
        market_open = market_session(datetime.now()).is_open
        checked_at = datetime.now().isoformat()
        for item in picks:
            original_action = item.get('action', 'hold')
            quote = quotes.get(item.get('symbol'), {}) or {}
            live_guard = entry_guard.evaluate(
                item.get('symbol', ''),
                quote,
                item.get('entry_plan') or {},
                item.get('validation') or {},
                research=item.get('research') or {},
                reference_price=float(item.get('analysis_price', item.get('price', 0)) or 0),
                generated_at=item.get('generated_at') or job.get('finished_at'),
                market_open=market_open,
            )
            if original_action != 'buy' and live_guard.get('allowed'):
                live_guard = dict(live_guard)
                live_guard['allowed'] = False
                live_guard['action'] = 'hold'
                live_guard['status'] = 'final_veto'
                live_guard['label'] = item.get('approval_label') or '综合审批未通过'
                live_guard['reasons'] = (
                    list(item.get('approval_failures') or [])
                    or ['原始潜力、数据质量或 AI 风险审批未通过']
                )
            live_action = 'buy' if original_action == 'buy' and live_guard.get('allowed') else 'hold'
            item['original_action'] = original_action
            item['action'] = live_action
            item['entry_guard'] = live_guard
            item['current_price'] = float(live_guard.get('current_price', 0) or 0)
            item['current_change_pct'] = float(
                (live_guard.get('intraday') or {}).get('day_change_pct', 0) or 0
            )
            item['live_checked_at'] = checked_at
            item['suggested_qty'] = int(item.get('planned_qty', 0) or 0) if live_action == 'buy' else 0
            item['suggested_amount'] = (
                item['suggested_qty'] * item['current_price'] if live_action == 'buy' else 0
            )
            item['expected_profit'] = float(
                (live_guard.get('target_scenario') or {}).get('net_profit', 0) or 0
            )
            item['max_loss'] = float(
                (live_guard.get('stop_scenario') or {}).get('net_loss', 0) or 0
            )
        with _scan_lock:
            _scan_live_cache["key"] = _cache_key
            _scan_live_cache["expire"] = time.time() + _live_ttl
            _scan_live_cache["picks"] = [dict(item) for item in picks]
    buys = [item for item in picks if item.get("action") == "buy"]
    try:
        snapshot_version = int(job.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        snapshot_version = 1
    legacy_snapshot = bool(
        job.get("status") == "done"
        and (
            job.get("legacy_snapshot")
            or snapshot_version < _AI_PICK_SCHEMA_VERSION
        )
    )
    return JSONResponse({
        "schema_version": snapshot_version,
        "current_schema_version": _AI_PICK_SCHEMA_VERSION,
        "legacy_snapshot": legacy_snapshot,
        "snapshot_notice": (
            "这是升级前保存的选股结果，可能只分析了 1 只股票；"
            "点击“开始AI选股”后，新一轮会从扩展候选池中完成 5 只深度分析。"
            if legacy_snapshot else ""
        ),
        "status": job["status"],
        "pool": job["pool"],
        "total": job["total"],
        "done": job["done"],
        "current": job["current"],
        "candidates": job["candidates"],
        "requested": int(job.get("requested", 5) or 5),
        "analyzed_count": len(picks),
        "recommended_count": len(buys),
        "prescreen_passed": int(job.get("prescreen_passed", 0) or 0),
        "prescreen_fallback": int(job.get("prescreen_fallback", 0) or 0),
        "prescreen_rejected": int(job.get("prescreen_rejected", 0) or 0),
        "prescreen_reasons": dict(job.get("prescreen_reasons", {}) or {}),
        "candidate_error": getattr(_fetch_candidates, 'last_error', '') or '',
        "picks": picks,
        "buy_count": len(buys),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "profit_guaranteed": False,
        "disclaimer": (
            "【旧版选股快照】当前是升级前保存的单股分析；点击“开始AI选股”后，"
            "新一轮会从扩展候选池中完成 5 只深度分析。AI 选股不能确保盈利。"
            if legacy_snapshot else
            "AI 选股不能确保盈利；页面会按最新价复核，并展示若在推荐价买入后的费用后净盈亏。"
        ),
    })


@app.post("/api/ai_pick/execute")
def ai_pick_execute(body: dict):
    """一键确认买入某个 AI 推荐。复用硬风控 + 模拟盘 place_order。"""
    symbol = _normalize_symbol(str(body.get('symbol', '')))
    if not symbol:
        return JSONResponse({"success": False, "error": "缺少 symbol"})
    with _scan_lock:
        recommendation = next(
            (item for item in _scan_job.get('picks', []) if item.get('symbol') == symbol),
            None,
        )
        generated_at = _scan_job.get('finished_at') or _scan_job.get('started_at')
    if not recommendation or recommendation.get('action') != 'buy':
        return JSONResponse({"success": False, "error": "该股票当前没有有效买入推荐，请重新运行选股"})
    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {})
    price = float(quote.get('price') or 0)
    if price <= 0:
        return JSONResponse({"success": False, "error": "没有可用行情价格，无法下单"})
    live_guard = entry_guard.evaluate(
        symbol,
        quote,
        recommendation.get('entry_plan') or {},
        recommendation.get('validation') or {},
        research=recommendation.get('research') or {},
        reference_price=float(recommendation.get('analysis_price', recommendation.get('price', 0)) or 0),
        generated_at=recommendation.get('generated_at') or generated_at,
        market_open=market_session(datetime.now()).is_open,
    )
    if not live_guard.get('allowed'):
        return JSONResponse({
            "success": False,
            "error": "；".join(live_guard.get('reasons', [])[:3]) or "实时入场条件已失效，请重新选股",
            "entry_guard": live_guard,
        })
    buy_high = float(recommendation.get('buy_high', 0) or 0)
    quantity = int(body.get('quantity') or 0)
    recommended_quantity = int(recommendation.get('suggested_qty', 0) or 0)
    quantity = recommended_quantity if quantity <= 0 else min(quantity, recommended_quantity)
    if quantity <= 0:
        return JSONResponse({"success": False, "error": "未能得到有效买入数量"})
    approval = approval_gate.review(
        source='ai_pick',
        symbol=symbol,
        side='buy',
        signal_action=recommendation.get('action', 'hold'),
        requested_quantity=quantity,
        recommended_quantity=recommended_quantity,
        reference_price=float(recommendation.get('price', body.get('price', 0)) or 0),
        market_price=price,
        generated_at=generated_at,
        confidence=float(recommendation.get('confidence', 0) or 0),
        risk_reward=float(recommendation.get('risk_reward', 0) or 0),
        max_buy_price=buy_high,
        stop_loss=float(recommendation.get('stop_loss', 0) or 0),
        data_quality=recommendation.get('data_quality') or {},
    )
    if not approval.allowed:
        _record_trade_approval(approval)
        return JSONResponse({
            "success": False,
            "error": "；".join(approval.reasons),
            "approval": approval.to_dict(),
            "entry_guard": live_guard,
        })
    scan_key = ''.join(character for character in str(generated_at) if character.isdigit())[:14]
    response = place_order({
        "symbol": symbol,
        "side": "buy",
        "quantity": approval.approved_quantity,
        "price": price,
        "reason": "ai_pick_approved",
        "client_order_id": f"aipick-{scan_key or datetime.now().strftime('%Y%m%d')}-{symbol}",
    })
    payload = json.loads(response.body.decode('utf-8'))
    payload['approval'] = approval.to_dict()
    payload['entry_guard'] = live_guard
    _record_trade_approval(approval, payload)
    return JSONResponse(payload)


def _paper_positions_with_names() -> List[Dict]:
    """模拟盘持仓 + 中文名称 (行情 → 常用表)。"""
    positions = [p.to_dict() for p in broker.get_positions()]
    try:
        syms = [p.get("symbol") for p in positions if p.get("symbol")]
        if syms:
            qmap = realtime.get_quotes(syms, sources=['tencent', 'sina', 'eastmoney']) or {}
            for p in positions:
                sym = p.get("symbol", "")
                p["name"] = (
                    (qmap.get(sym) or {}).get("name")
                    or COMMON_SYMBOL_NAMES.get(sym, "")
                    or p.get("name", "")
                )
    except Exception:
        pass
    return positions


@app.get("/api/positions")
def get_positions(mode: str = Query("paper")):
    """获取持仓列表。

    mode: paper=模拟盘(默认) | live=实盘(基金+股票)
    """
    if mode.lower() == "live":
        try:
            return JSONResponse(_live_positions())
        except Exception as e:
            return JSONResponse([])
    return JSONResponse(_paper_positions_with_names())


@app.get("/api/orders")
def get_orders(mode: str = Query("paper")):
    """获取订单历史。

    mode: paper=模拟盘(默认) | live=实盘(基金+股票)
    """
    if mode.lower() == "live":
        try:
            return JSONResponse(_live_orders())
        except Exception as e:
            return JSONResponse([])
    df = broker.get_order_history()
    if df.empty:
        return JSONResponse([])
    return JSONResponse(json.loads(df.to_json(orient="records", force_ascii=False)))


@app.post("/api/order")
@_locked_broker_operation
def place_order(body: dict):
    """模拟下单入口。所有请求先经过硬风控，不连接真实券商。"""
    try:
        symbol = _normalize_symbol(str(body.get('symbol', '')))
        side = str(body.get('side', body.get('direction', 'buy'))).lower()
        if side not in ('buy', 'sell'):
            return JSONResponse({"success": False, "error": "side 必须是 buy 或 sell"})
        price = float(body.get('price', 0))
        quantity = int(body.get('quantity', 0) or 0)
        requested_amount = float(
            body.get('amount', body.get('budget', body.get('order_amount', 0))) or 0
        )
        input_mode = str(body.get('input_mode', body.get('inputMode', ''))).lower()
        if side == 'buy' and requested_amount > 0:
            input_mode = 'amount'
        elif not input_mode:
            input_mode = 'quantity'
        reason = str(body.get('reason', 'api_order'))[:500]
        client_order_id = str(body.get('client_order_id', '')).strip()
        order_type_value = str(body.get('order_type', 'market')).lower()
        if price <= 0 or price > 1_000_000:
            return JSONResponse({"success": False, "error": "price 超出有效范围"})
        if client_order_id and (len(client_order_id) < 8 or len(client_order_id) > 64):
            return JSONResponse({"success": False, "error": "client_order_id 长度必须在 8 到 64 之间"})
        if order_type_value not in ('market', 'limit'):
            return JSONResponse({"success": False, "error": "order_type 必须是 market 或 limit"})
        if input_mode not in ('quantity', 'amount'):
            return JSONResponse({"success": False, "error": "input_mode 必须是 quantity 或 amount"})
        if input_mode == 'amount':
            if side != 'buy':
                return JSONResponse({"success": False, "error": "按金额下单仅适用于买入；卖出请填写可卖股数"})
            if requested_amount <= 0 or requested_amount > 100_000_000:
                return JSONResponse({"success": False, "error": "买入金额必须在 0 到 1 亿元之间"})
            applied_slippage = broker.slippage if order_type_value == 'market' else 0
            quantity = buy_quantity_for_amount(
                symbol,
                requested_amount,
                price,
                broker.commission_rate,
                broker.min_commission,
                applied_slippage,
            )
            if quantity <= 0:
                minimum = estimate_buy_cost(
                    100,
                    price,
                    broker.commission_rate,
                    broker.min_commission,
                    applied_slippage,
                )
                return JSONResponse({
                    "success": False,
                    "error": f"该金额不足以买入 100 股并支付费用，至少需要约 {minimum:.2f} 元",
                    "calculated_quantity": 0,
                })
        if quantity <= 0 or quantity > 10_000_000:
            return JSONResponse({"success": False, "error": "有效股数必须在 1 到 10,000,000 之间"})
        cached_response = _get_idempotent_order(client_order_id)
        if cached_response is not None:
            return JSONResponse(cached_response)

        check = _pre_trade_check(symbol, side, quantity, price, reason)
        if not check.get('allowed'):
            return JSONResponse({"success": False, "error": check.get('reason'), "risk": check})

        # 写入完整报价（含 pre_close/name）才能让券商侧的涨跌停校验生效；
        # 只调 update_market_price 会让 pre_close 缺失 → price_limits 返回 (0,0) → 校验被跳过。
        _quote_for_check = {}
        try:
            _quote_for_check = (realtime.get_quotes([symbol]) or {}).get(symbol) or {}
        except Exception:
            _quote_for_check = {}
        if _quote_for_check and hasattr(broker, 'update_quote'):
            _merged = dict(_quote_for_check)
            _merged['price'] = price
            broker.update_quote(symbol, _merged)
        elif hasattr(broker, 'update_market_price'):
            broker.update_market_price(symbol, price)
        order = Order(
            symbol=symbol,
            direction=OrderDirection.BUY if side == 'buy' else OrderDirection.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET if order_type_value == 'market' else OrderType.LIMIT,
            price=price,
        )
        order_id = broker.place_order(order)
        status = broker.get_order_status(order_id)
        if status.get('status') == 'rejected':
            _persist_broker_state()
            return JSONResponse({
                "success": False,
                "error": status.get('reject_reason') or "券商拒单",
                "order_id": order_id,
                "status": status,
            })
        risk_mgr.record_order({"symbol": symbol, "side": side, "quantity": quantity, "price": price})
        if side == 'sell':
            risk_mgr.update_daily_pnl(float(status.get('realized_pnl', 0) or 0))
        _persist_broker_state()
        _persist_risk_runtime()
        with _trading_counter_lock:
            _trading_counters[side] = _trading_counters.get(side, 0) + 1
        response = {
            "success": True,
            "order_id": order_id,
            "status": status,
            "account": broker.get_account_info(),
            "input_mode": input_mode,
            "requested_amount": requested_amount if input_mode == 'amount' else 0,
            "calculated_quantity": quantity,
        }
        if client_order_id:
            _order_idempotency[client_order_id] = (response, time.monotonic())
        return JSONResponse(response)
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
            broker.update_quote(sym, quote)
        results.append({
            "symbol": sym,
            "name": quote.get('name') or name_hint.get(sym) or COMMON_SYMBOL_NAMES.get(sym, sym),
            "price": price,
            "change_pct": float(quote.get('change_pct', 0) or 0),
            "quote": quote,
        })
    return JSONResponse({"query": query, "results": results, "quotes": quotes or {}})


@app.get("/api/risk")
def get_risk(mode: str = Query("paper")):
    """返回风险摘要。

    mode: paper=模拟盘(默认) | live=实盘(精简版)
    """
    if mode.lower() == "live":
        try:
            return JSONResponse(_live_risk())
        except Exception:
            return JSONResponse({"total_position_pct": 0, "drawdown": 0, "daily_order_count": 0, "limits": {}, "mode": "live"})
    snap = _portfolio_snapshot()
    report = risk_mgr.get_risk_report()
    report.update({
        "total_asset": snap["total_asset"],
        "cash": snap["cash"],
        "market_value": snap["market_value"],
        "total_position_pct": snap["total_position_pct"],
        "cash_pct": snap["cash"] / max(snap["total_asset"], 1),
        "position_count": len(snap["positions"]),
        "positions": _paper_positions_with_names(),
    })
    return JSONResponse(report)


@app.get("/api/opportunity")
def get_opportunity(symbol: str = Query("sh600000")):
    """当前股票的确定性潜力评分与本金匹配交易计划。"""
    symbol = _normalize_symbol(symbol)
    quote = realtime.get_quotes(
        [symbol], sources=['tencent', 'sina', 'eastmoney']
    ).get(symbol, {}) or {}
    history = _load_daily_frame(symbol, 240)
    snap = _portfolio_snapshot()
    position = snap['pos_map'].get(symbol)
    current_symbol_value = float(getattr(position, 'market_value', 0) or 0) if position else 0
    result = opportunity_scorer.analyze(
        symbol,
        history,
        equity=snap['total_asset'],
        cash=snap['cash'],
        current_symbol_value=current_symbol_value,
        quote=quote,
    ).to_dict()
    result.update({
        'name': quote.get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol),
        'validation': _cached_opportunity_validation(symbol, history),
        'capital': snap['total_asset'],
        'cash': snap['cash'],
        'model': 'potential-v1',
    })
    return JSONResponse(result)


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


# ===========================================================================
# 股票研究端点 — 速览卡 / 可比估值 / 事件情景 (src.research)
# ===========================================================================

@app.get("/api/research/tearsheet")
def research_tearsheet(symbol: str = Query("sh600000")):
    """公司速览卡: 行情/估值/技术面/买卖区间/综合评级。"""
    symbol = _normalize_symbol(symbol)
    try:
        from src.research import build_tearsheet
    except ImportError:
        return JSONResponse({"success": False, "error": "研究模块未安装"})
    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {}) or {}
    history = _load_daily_frame(symbol, 240)
    if history.empty or 'Close' not in history.columns:
        return JSONResponse({"success": False, "error": "暂无K线数据，无法生成速览卡"})
    closes = [float(v) for v in history['Close'].tolist() if float(v) > 0]
    volumes = [float(v) for v in history.get('Volume', []).tolist()] if 'Volume' in history.columns else None
    snap = _portfolio_snapshot()
    benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
    regime = professional_decision.market_regime(_load_daily_frame(benchmark_symbol, 240)).to_dict()
    signal = _deterministic_trade_signal(symbol, quote, snap, history, regime)
    capital = None
    try:
        summary = _get_capital_fetcher().get_main_net_summary(symbol)
        if summary:
            capital = {"main_net": summary.get("total_main_net", 0), "direction": summary.get("direction", "")}
    except Exception:
        pass
    session = market_session()
    tearsheet = build_tearsheet(
        symbol=symbol,
        name=quote.get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol),
        quote=quote,
        closes=closes,
        volumes=volumes,
        signal=signal,
        capital=capital,
        market_session=session.label,
    )
    return JSONResponse({"success": True, "tearsheet": tearsheet})


@app.get("/api/research/comps")
def research_comps(symbol: str = Query("sh600000")):
    """可比公司估值: 同业 PE/PB 对比 + 相对贵贱结论。"""
    symbol = _normalize_symbol(symbol)
    try:
        from src.research import compute_comps, classify_industry
    except ImportError:
        return JSONResponse({"success": False, "error": "研究模块未安装"})
    industry = classify_industry(symbol)
    peers = [symbol]
    if industry:
        from src.research.comps import INDUSTRY_PEERS
        peers = list(dict.fromkeys([symbol] + INDUSTRY_PEERS.get(industry, [])))
    quotes = realtime.get_quotes(peers, sources=['tencent', 'sina', 'eastmoney']) or {}
    result = compute_comps(symbol, industry, quotes)
    if not result["peer_count"]:
        result["conclusion"]["notes"] = ["该股未纳入内置行业池，可手动补充同业代码对比"]
    return JSONResponse({"success": True, "comps": result})


@app.post("/api/research/scenario")
def research_scenario(body: dict):
    """事件情景分析: 业绩/放量/破位/政策等事件的 what-if 价格区间。"""
    symbol = _normalize_symbol(str(body.get('symbol', 'sh600000')))
    event = str(body.get('event', 'custom'))
    custom_impact = None
    try:
        custom_impact = float(body.get('custom_impact')) if body.get('custom_impact') is not None else None
    except (TypeError, ValueError):
        custom_impact = None
    try:
        from src.research import build_scenario
    except ImportError:
        return JSONResponse({"success": False, "error": "研究模块未安装"})
    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {}) or {}
    price = float(quote.get('price', 0) or 0)
    if price <= 0:
        return JSONResponse({"success": False, "error": "暂无行情价格"})
    history = _load_daily_frame(symbol, 120)
    volatility = None
    if not history.empty and 'Close' in history.columns:
        closes = [float(v) for v in history['Close'].tolist() if float(v) > 0]
        if len(closes) > 5:
            import numpy as _np
            returns = _np.diff(_np.log(_np.array(closes[-60:]) + 1e-12))
            volatility = round(float(returns.std()) * _np.sqrt(252) * 100, 1)
    name = quote.get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol)
    result = build_scenario(
        symbol=symbol, name=name, price=price, event=event,
        volatility_pct=volatility, custom_impact=custom_impact,
        daily_range={"high": quote.get('high'), "low": quote.get('low'),
                     "pre_close": quote.get('pre_close')},
    )
    return JSONResponse({"success": True, "scenario": result})


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


STRATEGY_LABELS = {
    "potential": "潜力评分",
    "cross_ma": "双均线",
    "momentum": "动量",
    "mean_reversion": "均值回归",
}


def _load_daily_frame(symbol: str, count: int):
    """拉取并标准化真实日 K 数据。"""
    cache_key = f'daily_{int(count)}'
    cached = state_manager.get_cached_market_data(
        symbol,
        timeframe=cache_key,
        max_age_seconds=int(config.get('data_source.daily_cache_seconds', 1800) or 1800),
    )
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        cached_frame = cached.copy()
        cached_frame.attrs.update(getattr(cached, 'attrs', {}) or {})
        cached_frame.attrs['cache_hit'] = True
        return cached_frame
    data_source = 'mootdx'
    df = realtime.get_kline_mootdx(symbol, category=4, offset=count)
    if df.empty:
        data_source = 'eastmoney_kline'
        df = realtime.get_kline_data(symbol, period='day', count=count)
    if df.empty or 'close' not in [c.lower() for c in df.columns]:
        return pd.DataFrame()
    df = df.loc[:, ~df.columns.duplicated()].copy()
    df.rename(columns={column: str(column).title() for column in df.columns}, inplace=True)
    if "Open" not in df.columns:
        df["Open"] = df["Close"]
    if "High" not in df.columns:
        df["High"] = df[["Open", "Close"]].max(axis=1)
    if "Low" not in df.columns:
        df["Low"] = df[["Open", "Close"]].min(axis=1)
    volume_imputed = "Volume" not in df.columns
    if volume_imputed:
        df["Volume"] = 0
    for column in ("Open", "High", "Low", "Close", "Volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if not df.empty:
        df = df.loc[~df.index.duplicated(keep='last')].sort_index()
        df.attrs.update({
            'data_source': data_source,
            'retrieved_at': datetime.now().isoformat(),
            'cache_hit': False,
            'volume_imputed': volume_imputed,
        })
    if not df.empty:
        state_manager.cache_market_data(symbol, df, timeframe=cache_key)
    return df


def _load_daily_closes(symbol: str, count: int):
    """拉真实日K, 返回收盘价、开盘价和日期。"""
    df = _load_daily_frame(symbol, count)
    if df.empty:
        return [], [], []
    closes = [float(v or 0) for v in df["Close"].tolist()]
    opens = [float(v or 0) for v in df["Open"].tolist()]
    dates = []
    for idx in df.index:
        if hasattr(idx, 'strftime'):
            dates.append(idx.strftime('%Y-%m-%d'))
        else:
            dates.append(str(idx).split(' ')[0][:10])
    return closes, opens, dates


def _ma(values, end, window):
    if end + 1 < window:
        return None
    part = values[end - window + 1:end + 1]
    return sum(part) / len(part)


def _std(values, end, window):
    if end + 1 < window:
        return None
    part = values[end - window + 1:end + 1]
    m = sum(part) / len(part)
    var = sum((x - m) ** 2 for x in part) / len(part)
    return var ** 0.5


def _signal_at(closes, i, strategy, p):
    """第 i 根K线上, 某策略给出的目标动作: 'buy'|'sell'|None(不动作)。
    全部基于真实历史收盘价计算。"""
    if strategy == "cross_ma":
        fast, slow = p["fast"], p["slow"]
        fn, sn = _ma(closes, i, fast), _ma(closes, i, slow)
        fp, sp = _ma(closes, i - 1, fast), _ma(closes, i - 1, slow)
        if None in (fn, sn, fp, sp):
            return None
        if fp <= sp and fn > sn:
            return "buy"      # 金叉
        if fp >= sp and fn < sn:
            return "sell"     # 死叉
        return None
    if strategy == "momentum":
        look, thr = p["look"], p["thr"]
        if i < look:
            return None
        past = closes[i - look]
        if past <= 0:
            return None
        chg = (closes[i] - past) / past
        if chg >= thr:
            return "buy"
        if chg <= -thr:
            return "sell"
        return None
    if strategy == "mean_reversion":
        look, k = p["look"], p["k"]
        m, sd = _ma(closes, i, look), _std(closes, i, look)
        if m is None or sd is None or sd == 0:
            return None
        price = closes[i]
        if price < m - k * sd:
            return "buy"      # 超跌
        if price > m + k * sd:
            return "sell"     # 超涨
        return None
    return None


def _run_strategy_backtest(
    closes,
    opens,
    dates,
    strategy,
    params,
    initial=100000.0,
    symbol="",
    frame=None,
):
    """统一真实回测引擎。收盘信号在下一交易日开盘成交。
    返回真实的收益/回撤/胜率/交易/资金曲线 + 买入持有基准。

    自适应本金: 高价股(如茅台¥1400)10万买不起1手会导致0成交, 故把本金放大到
    至少能买 1 手(100股)最高价, 收益率口径不变(始终按 initial 归一)。"""
    max_price = max(closes) if closes else 0
    lot_cost = max_price * 100
    capital = max(initial, lot_cost * 1.05)  # 保证任意时点都买得起至少1手
    cash = capital
    position = 0
    entry_price = 0.0
    trades, equity, rejected_orders = [], [], []
    peak = capital
    max_dd = 0.0
    commission_rate = float(config.get('commission.rate', 0.0003) or 0)
    min_commission = float(config.get('commission.min', 5) or 0)
    stamp_tax_rate = (
        0
        if instrument_type(symbol) == 'etf'
        else float(config.get('commission.stamp_tax', 0.0005) or 0)
    )
    slippage = float(config.get('trading.slippage', 0.0001) or 0)

    for i in range(1, len(closes)):
        mark_price = closes[i]
        sig = _signal_at(closes, i - 1, strategy, params)
        if sig == "buy" and position == 0 and opens[i] > 0:
            bar = frame.iloc[i] if isinstance(frame, pd.DataFrame) and i < len(frame) else None
            high = float(bar.get('High', opens[i])) if bar is not None else float(opens[i])
            low = float(bar.get('Low', opens[i])) if bar is not None else float(opens[i])
            volume = bar.get('Volume') if bar is not None else None
            rejection = backtest_trade_rejection(
                symbol,
                "buy",
                float(closes[i - 1]),
                float(opens[i]),
                high,
                low,
                volume,
            )
            if rejection:
                rejected_orders.append({
                    "date": dates[i],
                    "symbol": symbol,
                    "side": "buy",
                    "price": round(float(opens[i]), 2),
                    "reason": rejection,
                })
            else:
                price = opens[i] * (1 + slippage)
                quantity = int((cash - min_commission) / max(price * (1 + commission_rate), 1) / 100) * 100
                if quantity > 0:
                    amount = quantity * price
                    commission = max(amount * commission_rate, min_commission)
                    entry_price = (amount + commission) / quantity
                    cash -= amount + commission
                    position = quantity
                    trades.append({"date": dates[i], "side": "buy", "price": round(price, 2), "quantity": position})
        elif sig == "sell" and position > 0:
            bar = frame.iloc[i] if isinstance(frame, pd.DataFrame) and i < len(frame) else None
            high = float(bar.get('High', opens[i])) if bar is not None else float(opens[i])
            low = float(bar.get('Low', opens[i])) if bar is not None else float(opens[i])
            volume = bar.get('Volume') if bar is not None else None
            rejection = backtest_trade_rejection(
                symbol,
                "sell",
                float(closes[i - 1]),
                float(opens[i]),
                high,
                low,
                volume,
            )
            if rejection:
                rejected_orders.append({
                    "date": dates[i],
                    "symbol": symbol,
                    "side": "sell",
                    "price": round(float(opens[i]), 2),
                    "reason": rejection,
                })
            else:
                price = opens[i] * (1 - slippage)
                amount = position * price
                commission = max(amount * commission_rate, min_commission)
                stamp_tax = amount * stamp_tax_rate
                cash += amount - commission - stamp_tax
                pnl_pct = (amount - commission - stamp_tax - entry_price * position) / max(entry_price * position, 1) * 100
                trades.append({"date": dates[i], "side": "sell", "price": round(price, 2), "quantity": position, "pnl_pct": round(pnl_pct, 2)})
                position = 0
                entry_price = 0.0
        value = cash + position * mark_price
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / max(peak, 1))
        # 资金曲线按 initial(10万) 归一, 便于展示; 收益率口径不受自适应本金影响
        equity.append({"date": dates[i], "value": round(value / capital * initial, 2), "price": round(mark_price, 2)})

    # 收尾: 未平仓的按最后价结算(仅计入收益, 不记为一笔已实现交易的胜负)
    final_value = cash + position * (closes[-1] if closes else 0)

    sell_trades = [t for t in trades if t.get('side') == 'sell']
    wins = len([t for t in sell_trades if t.get('pnl_pct', 0) > 0])
    win_rate = wins / len(sell_trades) if sell_trades else 0
    # 买入持有基准: 首日买满、末日不卖
    if len(closes) >= 2 and closes[0] > 0:
        buy_hold = (closes[-1] - closes[0]) / closes[0] * 100
    else:
        buy_hold = 0.0
    return {
        "return_pct": round((final_value - capital) / capital * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "win_rate": round(win_rate * 100, 1),
        "trades": trades,
        "equity": equity,
        "buy_hold_return": round(buy_hold, 2),
        "final_value": round(final_value, 2),
        "last_price": round(closes[-1], 2) if closes else 0,
        "rejected_order_count": len(rejected_orders),
        "rejected_orders": rejected_orders,
    }


def _run_potential_backtest(frame, symbol, initial=100000.0):
    backtester = Backtester(
        initial_capital=initial,
        commission=float(config.get('commission.rate', 0.0003) or 0),
        slippage=float(config.get('trading.slippage', 0.0001) or 0),
        stamp_tax=float(config.get('commission.stamp_tax', 0.0005) or 0),
        min_commission=float(config.get('commission.min', 5) or 0),
    )
    result = backtester.run_backtest(PotentialStrategy(), frame, symbol)
    if not result:
        return {
            "return_pct": 0, "max_drawdown": 0, "win_rate": 0,
            "trades": [], "equity": [], "buy_hold_return": 0,
            "last_price": 0, "rejected_order_count": 0, "rejected_orders": [],
        }
    equity_frame = result.get('equity_curve', pd.DataFrame())
    equity = [
        {"date": str(index).split(' ')[0], "value": round(float(row['total_equity']), 2)}
        for index, row in equity_frame.iterrows()
    ] if not equity_frame.empty else []
    trades = []
    for trade in result.get('trades', []):
        item = {
            "date": str(trade.get('date', '')).split(' ')[0],
            "side": str(trade.get('action', '')).lower(),
            "price": round(float(trade.get('price', 0) or 0), 2),
            "quantity": int(trade.get('shares', 0) or 0),
        }
        if trade.get('action') == 'SELL':
            item['pnl_pct'] = round(float(trade.get('return_pct', 0) or 0), 2)
        trades.append(item)
    rejected_orders = [
        {
            "date": str(order.get('date', '')).split(' ')[0],
            "symbol": str(order.get('symbol', symbol)),
            "side": str(order.get('side', '')).lower(),
            "price": round(float(order.get('price', 0) or 0), 2),
            "reason": str(order.get('reason', '无法成交')),
        }
        for order in result.get('rejected_orders', [])
    ]
    close = frame['Close']
    buy_hold = (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100 if len(close) > 1 else 0
    return {
        "return_pct": round(float(result.get('total_return', 0) or 0) * 100, 2),
        "max_drawdown": round(abs(float(result.get('max_drawdown', 0) or 0)) * 100, 2),
        "win_rate": round(float(result.get('win_rate', 0) or 0) * 100, 1),
        "trades": trades,
        "equity": equity,
        "buy_hold_return": round(buy_hold, 2),
        "last_price": round(float(close.iloc[-1]), 2),
        "sharpe_ratio": round(float(result.get('sharpe_ratio', 0) or 0), 2),
        "profit_factor": round(float(result.get('profit_factor', 0) or 0), 2),
        "rejected_order_count": int(result.get('rejected_order_count', len(rejected_orders)) or 0),
        "rejected_orders": rejected_orders,
    }


def _default_params(strategy, fast, slow):
    if strategy == "potential":
        return {}, "潜力评分 多因子/下一交易日成交"
    if strategy == "momentum":
        return {"look": max(3, fast * 2), "thr": 0.05}, f"动量 {max(3, fast*2)}日/±5%"
    if strategy == "mean_reversion":
        return {"look": slow, "k": 2.0}, f"均值回归 {slow}日±2σ"
    return {"fast": fast, "slow": slow}, f"双均线 MA{fast}/MA{slow}"


@app.get("/api/backtest")
def run_backtest(
    symbol: str = Query("sh600000"),
    strategy: str = Query("potential"),
    fast: int = Query(5, ge=2, le=60),
    slow: int = Query(20, ge=3, le=120),
    count: int = Query(180, ge=40, le=360),
):
    """真实历史回测，默认验证潜力评分策略。"""
    symbol = _normalize_symbol(symbol)
    if strategy not in STRATEGY_LABELS:
        strategy = "potential"
    if fast >= slow:
        fast = max(2, slow // 2)

    frame = _load_daily_frame(symbol, count)
    closes = [float(value) for value in frame['Close'].tolist()] if not frame.empty else []
    opens = [float(value) for value in frame['Open'].tolist()] if not frame.empty else []
    dates = [str(index).split(' ')[0] for index in frame.index] if not frame.empty else []
    if not closes:
        return JSONResponse({
            "symbol": symbol, "strategy": STRATEGY_LABELS[strategy], "strategy_key": strategy,
            "return_pct": 0, "max_drawdown": 0, "win_rate": 0, "buy_hold_return": 0,
            "trades": [], "equity": [], "rejected_order_count": 0,
            "rejected_orders": [], "message": "暂无可回测的K线数据",
        })

    params, label = _default_params(strategy, fast, slow)
    res = (
        _run_potential_backtest(frame, symbol)
        if strategy == 'potential'
        else _run_strategy_backtest(
            closes,
            opens,
            dates,
            strategy,
            params,
            symbol=symbol,
            frame=frame,
        )
    )
    return JSONResponse({
        "symbol": symbol,
        "strategy": label,
        "strategy_key": strategy,
        "return_pct": res["return_pct"],
        "max_drawdown": res["max_drawdown"],
        "win_rate": res["win_rate"],
        "buy_hold_return": res["buy_hold_return"],
        "trades": res["trades"][-20:],
        "equity": res["equity"][-120:],
        "last_price": res["last_price"],
        "sharpe_ratio": res.get("sharpe_ratio", 0),
        "profit_factor": res.get("profit_factor", 0),
        "rejected_order_count": res.get("rejected_order_count", 0),
        "rejected_orders": res.get("rejected_orders", [])[-20:],
    })



@app.post("/api/ai_trade")
def ai_trade(body: dict):
    """重新计算确定性信号并通过审批后，才转换为模拟订单。"""
    symbol = _normalize_symbol(str(body.get('symbol', '')))
    side = str(body.get('side', body.get('action', 'hold'))).lower()
    if side not in ('buy', 'sell'):
        return JSONResponse({"success": False, "error": "AI 信号不是买入或卖出"})

    quote = realtime.get_quotes([symbol], sources=['tencent', 'sina', 'eastmoney']).get(symbol, {}) if symbol else {}
    price = float(quote.get('price') or 0)
    if price <= 0:
        return JSONResponse({"success": False, "error": "没有可用行情价格，无法下单"})

    snap = _portfolio_snapshot()
    history = _load_daily_frame(symbol, 240)
    benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
    regime = professional_decision.market_regime(
        _load_daily_frame(benchmark_symbol, 240)
    ).to_dict()
    signal = _deterministic_trade_signal(symbol, quote, snap, history, regime)
    recommended_quantity = int(signal.get('suggested_qty', 0) or 0)
    requested_quantity = int(body.get('quantity', 0) or recommended_quantity)
    approval = approval_gate.review(
        source='ai_trade',
        symbol=symbol,
        side=side,
        signal_action=signal.get('action', 'hold'),
        requested_quantity=requested_quantity,
        recommended_quantity=recommended_quantity,
        reference_price=float(body.get('price') or signal.get('price') or 0),
        market_price=price,
        generated_at=signal.get('generated_at'),
        confidence=float(signal.get('confidence', 0) or 0),
        risk_reward=float(signal.get('risk_reward', 0) or 0),
        max_buy_price=float(signal.get('buy_high', 0) or 0),
        stop_loss=float(signal.get('stop_loss', 0) or 0),
        data_quality=signal.get('data_quality') or {},
    )
    if not approval.allowed:
        _record_trade_approval(approval)
        return JSONResponse({
            "success": False,
            "error": "；".join(approval.reasons),
            "approval": approval.to_dict(),
            "signal": signal,
        })
    response = place_order({
        "symbol": symbol,
        "side": side,
        "quantity": approval.approved_quantity,
        "price": price,
        "reason": "ai_trade_approved",
        "client_order_id": f"aitrade-{datetime.now().strftime('%Y%m%d')}-{symbol}-{side}",
    })
    payload = json.loads(response.body.decode('utf-8'))
    payload['approval'] = approval.to_dict()
    payload['signal'] = signal
    _record_trade_approval(approval, payload)
    return JSONResponse(payload)


async def _premarket_scheduler_loop():
    """服务运行期间在 08:20 生成计划；自动模拟买入必须显式开启。"""
    while True:
        try:
            now = datetime.now()
            today = now.date().isoformat()
            if is_trading_day(now.date(), _premarket_holidays()):
                if (
                    datetime_time(8, 20) <= now.time() < datetime_time(9, 20)
                    and _premarket_scheduler_state['generation_date'] != today
                ):
                    _premarket_scheduler_state['generation_date'] = today
                    if _premarket_plan_snapshot().get('status') != 'generating':
                        await asyncio.to_thread(
                            _generate_premarket_plan,
                            str(config.get('premarket.pool', 'watchlist')).lower(),
                            int(config.get('premarket.top', 5) or 5),
                        )
                if (
                    bool(config.get('premarket.auto_execute', False))
                    and datetime_time(9, 31) <= now.time() < datetime_time(10, 0)
                    and _premarket_scheduler_state['execution_date'] != today
                ):
                    _premarket_scheduler_state['execution_date'] = today
                    plan = _premarket_plan_snapshot()
                    entry = next(
                        (item for item in plan.get('entries', []) if item.get('decision') == 'buy'),
                        None,
                    )
                    if entry:
                        await asyncio.to_thread(_execute_premarket_entry, entry['symbol'], now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with _premarket_lock:
                _premarket_plan['scheduler_error'] = str(exc)
                _persist_premarket_plan()
        await asyncio.sleep(30)


# ===========================================================================
# 虚拟盘自托管 AI 自动交易 (仅模拟盘, 赚取收益为第一目标, 数据供实盘参考)
# ===========================================================================

# 内置活跃候选池: 覆盖主流行业龙头, 保证自托管每轮都有"未持仓"的买入标的
AUTOTRADE_CANDIDATES: List[str] = [
    "sh600519",  # 贵州茅台  白酒
    "sz000858",  # 五粮液    白酒
    "sh600809",  # 山西汾酒  白酒
    "sz300750",  # 宁德时代  新能源
    "sz002594",  # 比亚迪    新能源车
    "sz002460",  # 赣锋锂业  锂电
    "sz300274",  # 阳光电源  光伏
    "sh601012",  # 隆基绿能  光伏
    "sh688981",  # 中芯国际  半导体
    "sz002371",  # 北方华创  半导体
    "sz300661",  # 圣邦股份  半导体
    "sh600276",  # 恒瑞医药  医药
    "sz300760",  # 迈瑞医疗  医疗器械
    "sh603259",  # 药明康德  CXO
    "sz300059",  # 东方财富  券商
    "sh600030",  # 中信证券  券商
    "sh601318",  # 中国平安  保险
    "sh600036",  # 招商银行  银行
    "sz002475",  # 立讯精密  消费电子
    "sz002241",  # 歌尔股份  消费电子
    "sh603501",  # 韦尔股份  半导体设计
    "sz000977",  # 浪潮信息  AI算力
    "sh688041",  # 海光信息  AI芯片
    "sz002230",  # 科大讯飞  AI应用
    "sz300308",  # 中际旭创  CPO
    "sh601899",  # 紫金矿业  有色
    "sh600000",  # 浦发银行  银行
    "sz000001",  # 平安银行  银行
    "sh600104",  # 上汽集团  整车
    "sz000002",  # 万科A     地产
]


def _autotrade_held_days(symbol: str) -> int:
    """该持仓已持有多少自然日 (按最近一次买入日期计算)。

    返回 -1 表示在近期记录里找不到该股的买入记录 (如持仓来自更早的会话),
    此时调用方应放行卖出, 避免误锁仓。
    """
    trades = _autotrade_state.get("trades", []) or []
    for t in trades:  # trades 按时间倒序插入(最新在前)
        if t.get("symbol") == symbol and t.get("side") == "buy":
            try:
                buy_date = datetime.strptime(t.get("date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return -1
            return (datetime.now().date() - buy_date).days
    return -1


def _autotrade_candidate_pool() -> List[str]:
    """候选池 = 当前持仓 + 全市场粗筛漏斗 + 内置活跃池 + 自选 + AI选股。

    关键改造 (原实现硬截断 16 只, 是选股路径的最大瓶颈):
    原候选只来自"内置活跃池(约16~30只) + 自选 + AI选股", 在 5000+ 只 A 股里
    只看十几只 —— 导致反复在同一批票里进出(某白酒龙头3次全亏), 以及行情好时
    "一天选不到股票"。现改为两级漏斗:
      ① 全市场粗筛 (纯 HTTP 快照, 便宜): 5000+ → 按流动性/市值/换手/涨幅
         硬过滤 → 约 60 只, 涨跌都覆盖(按成交额排序, 保留回调中的买点)
      ② 精筛 (现有 _autotrade_buy_screen, 昂贵: 拉K线+多因子+LLM)
    持仓永远优先保留, 保证卖出逻辑不被漏斗挤掉。
    """
    syms: List[str] = []
    held: set = set()
    try:
        for p in broker.get_positions():
            s = getattr(p, 'symbol', '')
            if s:
                held.add(s)
                if s not in syms:
                    syms.append(s)
    except Exception:
        pass

    extra: List[str] = []
    # ① 全市场粗筛漏斗 (主要来源)
    try:
        from src.analysis.market_scanner import scan_candidates, load_prefilter_config
        _pf_cfg = load_prefilter_config(config.get('autotrade', {}) or {})
        for row in scan_candidates(_pf_cfg):
            s = _normalize_symbol(str(row.get('symbol', '')))
            if s:
                extra.append(s)
        _autotrade_log(f"全市场粗筛: 得到 {len(extra)} 只候选", "muted")
    except Exception as e:
        _autotrade_log(f"全市场粗筛不可用({type(e).__name__}), 回退内置池", "warn")

    # ② 内置活跃池 + 自选股 + 最近 AI 选股 (兜底与人工偏好)
    extra.extend(list(AUTOTRADE_CANDIDATES) + list(_get_watchlist() or []))
    try:
        with _scan_lock:
            for pick in (_scan_job.get("picks") or []):
                s = _normalize_symbol(str(pick.get("symbol", "")))
                if s:
                    extra.append(s)
    except Exception:
        pass

    for s in extra:
        s = _normalize_symbol(s)
        if s and s not in held and s not in syms:
            syms.append(s)
    # 持仓 + 全市场候选 + 兜底池; 上限放宽到 80 (漏斗已按成交额排序, 前列最活跃)
    return syms[:80]


def _autotrade_log(msg: str, kind: str = "info"):
    """写入自托管日志 (带时间戳, 最多保留 80 条)。"""
    with _autotrade_lock:
        _autotrade_state["log"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "msg": msg,
            "kind": kind,
        })
        _autotrade_state["log"] = _autotrade_state["log"][-80:]


def _autotrade_record_trade(entry: Dict):
    """记录一笔自动交易 (最多保留 50 条, 持久化供复盘)。"""
    with _autotrade_lock:
        _autotrade_state["trades"].insert(0, {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "symbol": entry.get("symbol", ""),
            "name": entry.get("name", ""),
            "side": entry.get("side", "buy"),
            "quantity": entry.get("quantity", 0),
            "price": round(float(entry.get("price", 0) or 0), 2),
            "reason": entry.get("reason", "")[:120],
            "order_id": entry.get("order_id", ""),
        })
        _autotrade_state["trades"] = _autotrade_state["trades"][:50]
        try:
            state_manager.save_account_state('autotrade_state', {
                "enabled": _autotrade_state.get('enabled', False),
                "started_at": _autotrade_state.get('started_at', ''),
                "last_cycle": _autotrade_state.get('last_cycle', ''),
                "cycles": _autotrade_state.get('cycles', 0),
                "trades": _autotrade_state.get('trades', []),
            })
        except Exception:
            pass


def _autotrade_batch_signals(symbols: List[str], quotes: Dict, snap: Dict, regime: Dict) -> Dict:
    """对候选池全量跑确定性信号 (不走 _build_rule_ai_signals 的 6 只截断)。"""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    with _Pool(max_workers=min(4, len(symbols))) as executor:
        frames = list(executor.map(lambda s: _load_daily_frame(s, 240), symbols))
    return {
        s: _deterministic_trade_signal(s, quotes.get(s, {}), snap, f, regime)
        for s, f in zip(symbols, frames)
    }


def _autotrade_buy_screen(symbol: str, opp: Dict, dq: Dict, regime: Dict,
                          quote: Dict, history: pd.DataFrame,
                          validation: Dict, capital: Optional[Dict],
                          cfg: Dict, news_factor: Optional[Dict] = None,
                          wyckoff: Optional[Dict] = None) -> Dict:
    """买入分析器 — 硬门槛 + 分层评分 (严格但不死板, 营利为第一目标)。

    硬门槛 (全过才买, 防止乱买):
      1. 市场环境允许开仓 + 数据质量合格
      2. 机会评分 ≥ buy_score (60) + 置信度 ≥ 0.55 + 风险回报比 ≥ 1.5
      3. 至少弱趋势 (价格接近或站上 MA20, 或 MA5 接近/超过 MA20)
      4. 主力资金未巨量净流出 (> 成交额 20% 才否决)

    新闻涨幅因子 (加分/否决):
      - bull 且因子分 ≥ 70 → 综合分 +10 (强催化)
      - bull 且 55~70 → 综合分 +4
      - bear 且因子分 ≤ 35 → 否决 (重大利空不碰)
      - bear 35~45 → 综合分 -6
      - neutral / 无新闻 → 不影响

    威科夫量价阶段 (借鉴 Wyckoff, 加分/否决):
      - spring 弹簧 → 综合分 +8 (洗盘买点)
      - markup 拉升 → +4 | accumulation 吸筹 → +2
      - markdown 下跌 → -8 | distribution 派发 → 否决

    分层加分 (决定买谁):
      - 强趋势 (+8) / 弱趋势 (+2)
      - 资金净流入 (+6) / 小幅流出 (-4)
      - 历史胜率: 样本≥10 且 ≥50% (+6); 样本≥10 且 25~40% (-8);
        样本<10 不否决 (统计意义不足, 不再一票否决)
    """
    price = float(quote.get('price', 0) or 0)
    score = float(opp.get('score', 0) or 0)
    conf = float(opp.get('confidence', 0) or 0)
    rr = float(opp.get('risk_reward', 0) or 0)
    factors: Dict = {}
    reject: List[str] = []

    # 1. 市场环境
    allow_new = bool(regime.get('allow_new_positions', True))
    factors['regime'] = regime.get('code', 'unknown')
    if not allow_new:
        reject.append(f"市场环境防守 ({regime.get('label', '')})，暂停开仓")

    # 2. 数据质量
    dq_ok = bool(dq.get('allowed'))
    factors['data_quality'] = dq.get('status', '')
    if not dq_ok:
        reject.append(f"数据质量未通过 ({dq.get('status', '')})")

    # 3. 评分/置信度/风险回报比 (硬门槛)
    buy_score = float(cfg.get('buy_score', 60) or 60)
    min_conf = float(cfg.get('min_confidence', 0.55) or 0.55)
    min_rr = float(cfg.get('min_risk_reward', 1.5) or 1.5)
    factors['score'] = round(score, 1)
    factors['confidence'] = round(conf, 2)
    factors['risk_reward'] = round(rr, 2)
    if score < buy_score:
        reject.append(f"机会评分 {score:.0f} < {buy_score:.0f}")
    if conf < min_conf:
        reject.append(f"置信度 {conf:.2f} < {min_conf:.2f}")
    if rr < min_rr:
        reject.append(f"风险回报比 {rr:.1f} < {min_rr:.1f}")

    # 4. 趋势确认 (分层: 强趋势/弱趋势可买, 无趋势否决)
    trend_strength = 0  # 0=无 1=弱 2=强
    if not history.empty and 'Close' in history.columns and len(history) >= 25:
        closes = [float(v) for v in history['Close'].tolist() if float(v) > 0]
        if len(closes) >= 25:
            import numpy as _np
            ma5 = float(_np.mean(closes[-5:]))
            ma20 = float(_np.mean(closes[-20:]))
            factors['ma5'] = round(ma5, 2)
            factors['ma20'] = round(ma20, 2)
            if price > ma20 and ma5 >= ma20:
                trend_strength = 2
            elif price > ma20 * 0.985 or ma5 > ma20 * 0.985:
                trend_strength = 1  # 接近站上/接近金叉, 允许参与
            else:
                reject.append(f"趋势未确认 (价 {price:.2f} vs MA20 {ma20:.2f}, MA5 {ma5:.2f} vs MA20 {ma20:.2f})")
    else:
        reject.append("K线数据不足，无法确认趋势")

    # 5. 资金流 (按成交额比例判断 — 大盘股绝对流出额大但不代表资金出逃)
    flow_ok = True
    if capital:
        main_net = float(capital.get('main_net', 0) or 0)
        factors['main_net'] = round(main_net, 0)
        factors['flow_direction'] = capital.get('direction', '')
        # 用当日成交额比例: 主力净流出 > 成交额 20% 视为巨量出逃 (否决)
        amount = float(quote.get('amount', 0) or 0)
        if amount > 0:
            flow_ratio = main_net / amount
            factors['flow_ratio'] = round(flow_ratio, 3)
            if main_net < -0.20 * amount:
                flow_ok = False
                reject.append(f"主力净流出占成交额 {abs(flow_ratio)*100:.0f}% (出逃)")
        else:
            # 无成交额数据: 兜底用总资产 3% (3.3万), 避免绝对额误杀大盘股
            if main_net < -0.03 * float(cfg.get('total_asset', 1e6) or 1e6):
                flow_ok = False
                reject.append(f"主力资金大幅净流出 ({abs(main_net)/1e4:.0f}万)")
    else:
        factors['flow_direction'] = 'unknown'

    # 6. 历史验证 (软约束: 样本充足且极差才否决, 样本少不否决)
    win_rate = float((validation or {}).get('win_rate', 0) or 0)
    samples = int((validation or {}).get('samples', 0) or 0)
    factors['win_rate'] = round(win_rate, 1)
    factors['samples'] = samples
    if samples >= 10 and win_rate < 25:
        reject.append(f"历史相似机会胜率 {win_rate:.0f}% (样本{samples})，模式明显不利")

    # 7. 新闻涨幅因子 (加分/否决)
    if news_factor:
        nf_dir = str(news_factor.get('direction', ''))
        nf_score = float(news_factor.get('factor_score', 0) or 0)
        factors['news_direction'] = nf_dir
        factors['news_factor'] = round(nf_score, 1)
        factors['news_events'] = news_factor.get('events', [])[:3]
        if nf_dir == 'bear' and nf_score <= 35:
            reject.append(f"新闻重大利空 (因子{nf_score:.0f}: {'/'.join(factors['news_events'])})")

    # 8. 威科夫量价阶段 (借鉴 Wyckoff: 派发否决 / 弹簧加分)
    if wyckoff:
        w_phase = str(wyckoff.get('phase', 'unknown'))
        w_conf = float(wyckoff.get('confidence', 0) or 0)
        factors['wyckoff_phase'] = w_phase
        factors['wyckoff_confidence'] = round(w_conf, 2)
        factors['wyckoff_note'] = wyckoff.get('note', '')
        if w_phase == 'distribution' and w_conf >= 0.5:
            reject.append(f"威科夫派发阶段 (高位放量滞涨, 置信度{w_conf:.0f})")

    # 9. 技术因子 (借鉴 Qlib Alpha158: RSI/MACD/BOLL/量价/动量/波动率)
    try:
        from src.analysis.alpha_factors import evaluate_alpha
        _alpha = evaluate_alpha(history, price)
    except Exception:
        _alpha = {}
    if _alpha:
        for k, v in (_alpha.get("detail") or {}).items():
            factors[f"alpha_{k}"] = v
        if _alpha.get("veto"):
            reject.append(_alpha["veto"])
        factors["alpha_notes"] = "、".join(_alpha.get("notes", [])[:3])

    # 综合分 (排序用)
    comp = score * conf
    if news_factor:
        nf_dir = news_factor.get('direction', '')
        nf_score = float(news_factor.get('factor_score', 0) or 0)
        # 防追高: 消息兑现日往往已大涨(价格提前反应), 追买接盘(回测: 大涨次日开盘买胜率仅40%)
        chg = float(quote.get('change_pct', 0) or 0)
        if nf_dir == 'bull':
            if chg >= 3.0:
                factors['news_chase'] = True
                if chg >= 5.0:
                    reject.append(f"新闻利好但当日已大涨 {chg:.1f}% (消息已兑现), 追高风险")
                # 3~5% 不加分不否决, 等回调
            elif nf_score >= 70:
                comp += 10
            else:
                comp += 4
        elif nf_dir == 'bear' and nf_score > 35:
            comp -= 6
    if wyckoff:
        from src.analysis.wyckoff_phase import PHASE_SCORE
        w_phase = str(wyckoff.get('phase', 'unknown'))
        if w_phase in PHASE_SCORE and w_phase != 'distribution':
            comp += PHASE_SCORE[w_phase]
    if trend_strength == 2:
        comp += 8
    elif trend_strength == 1:
        comp += 2
    # 技术因子综合调整 (Qlib Alpha158 精简版)
    if _alpha:
        comp += float(_alpha.get("adjust", 0) or 0)
    if capital and float(capital.get('main_net', 0) or 0) > 0:
        comp += 6
    elif capital and float(capital.get('main_net', 0) or 0) < 0:
        comp -= 4
    if samples >= 10 and win_rate >= 50:
        comp += 6
    elif samples >= 10 and win_rate < 40:
        comp -= 8
    if rr >= 2.0:
        comp += 4

    passed = not reject and price > 0
    return {
        "pass": passed,
        "score": round(comp, 1),
        "factors": factors,
        "reject": reject[:5],
    }


def _autotrade_cycle():
    """一轮自动交易: AI 选股 → 按资金买入 → 持仓止损/止盈卖出。全部模拟盘。

    买入规则 (自托管宽松版, 以赚取收益为第一目标):
      - 未持仓 + 机会评分 ≥ buy_score(默认60) + 置信度 ≥ min_confidence(默认0.5)
      - 数据质量合格 + 市场环境允许开仓 + 风险回报比 ≥ 1.2
      - 不卡历史相似机会验证 (样本少/胜率低不再一票否决)
    卖出规则: 复用确定性信号的止损/止盈/退出计划。
    """
    # 防重入: toggle 立即触发 + 循环周期可能并发, 同一轮只能跑一次
    if not _autotrade_exec_lock.acquire(blocking=False):
        _autotrade_log("上一轮分析仍在进行，跳过本轮", "muted")
        return
    try:
        return _autotrade_cycle_impl()
    finally:
        _autotrade_exec_lock.release()


def _autotrade_cycle_impl():
    now = datetime.now()
    session = market_session()
    # 非交易时段不自动交易 (模拟盘可练习, 但自动交易遵守市场时段避免误导)
    if not session.is_open:
        _autotrade_log(f"非交易时段 ({session.label})，跳过本轮", "muted")
        return
    if not bool(_trading_control.get('opening_enabled', True)):
        _autotrade_log("开仓已锁定，跳过本轮", "warn")
        return

    symbols = _autotrade_candidate_pool()
    if not symbols:
        _autotrade_log("候选池为空，跳过本轮", "muted")
        return

    try:
        quotes = realtime.get_quotes(symbols, sources=['eastmoney', 'tencent', 'sina']) or {}
        snap = _portfolio_snapshot()
        benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
        regime = professional_decision.market_regime(_load_daily_frame(benchmark_symbol, 240)).to_dict()

        # 自托管配置 (config.yaml autotrade 段, 均有默认值)
        max_positions = int(config.get('autotrade.max_positions', 6) or 6)
        buy_score = float(config.get('autotrade.buy_score', 60) or 60)
        min_conf = float(config.get('autotrade.min_confidence', 0.5) or 0.5)
        # 仓位不做上限 (营利第一): 目标仓位 = 100%, 现金充裕就持续买入优质标的
        target_pos_pct = 1.0
        min_keep = int(config.get('autotrade.min_keep_positions', 2) or 2)
        min_rr = float(config.get('autotrade.min_risk_reward', 1.2) or 1.2)

        # ── 环境自适应参数 (借鉴 AgentQuant: regime-adaptive) ──
        # 进攻环境: 放宽门槛放大预算; 中性: 默认; 防守: 收紧门槛 (已有 allow_new 门禁叠加)
        _regime_code = str(regime.get('code', 'neutral')).lower()
        if _regime_code in ('risk_on', 'bull', 'attack'):
            buy_score = min(buy_score, 55)
            min_conf = min(min_conf, 0.50)
            min_rr = min(min_rr, 1.2)
            _regime_budget = 0.40
            _autotrade_log(f"市场进攻环境: 门槛放宽 (评分≥{buy_score:.0f}) 预算上限 {_regime_budget:.0%}", "muted")
        elif _regime_code in ('risk_off', 'bear', 'defensive'):
            buy_score = max(buy_score, 70)
            min_conf = max(min_conf, 0.60)
            min_rr = max(min_rr, 1.8)
            _regime_budget = 0.25
            _autotrade_log(f"市场防守环境: 门槛收紧 (评分≥{buy_score:.0f}) 预算上限 {_regime_budget:.0%}", "warn")
        else:
            _regime_budget = 0.35

        positions = broker.get_positions()
        held = {getattr(p, 'symbol', '') for p in positions}
        total_asset = float(snap['total_asset'] or 0)
        cash = float(snap['cash'] or 0)
        current_pos_pct = float(snap['market_value']) / max(total_asset, 1)
        # 每日净值点 (供 /api/performance/report 绩效分析)
        _record_equity_point(total_asset, now)
        # 剩余可用空间 = 现金比例 (仓位不设上限)
        room = max(cash / max(total_asset, 1), 0.0)

        # 单笔预算: 按总资产比例, 现金越充足单笔上限越高 (分散风险, 不做总仓位限制)
        budget_pct = min(max(0.2, room * 0.45), _regime_budget)
        if room < 0.02:
            _autotrade_log(f"可用现金仅 {money_cn(cash)}，暂缓买入，仅做持仓管理", "muted")

        bought, sold = [], []
        prot_cfg = _load_protection_cfg()

        # === ①⁺ 追踪止损 (借鉴 Freqtrade): 浮盈达激活线后, 自持仓最高点回撤超阈值即离场 ===
        for p in list(positions):
            sym_t = getattr(p, 'symbol', '')
            price_t = float((quotes.get(sym_t, {}) or {}).get('price', 0) or 0)
            if price_t <= 0:
                continue
            high_t = max(float(_trailing_highs.get(sym_t, 0.0) or 0), price_t)
            _trailing_highs[sym_t] = high_t
            hit_t = trailing_stop_hit(float(getattr(p, 'avg_cost', 0) or 0), high_t, price_t, prot_cfg)
            if not hit_t:
                continue
            pos_obj = snap['pos_map'].get(sym_t) or p
            avail_t = int(getattr(pos_obj, 'available_quantity', 0) or 0)
            if avail_t <= 0:
                continue
            name_t = (quotes.get(sym_t, {}) or {}).get('name') or COMMON_SYMBOL_NAMES.get(sym_t, sym_t)
            try:
                resp = place_order({
                    "symbol": sym_t, "side": "sell", "quantity": avail_t, "price": price_t,
                    "reason": "autotrade_trail_stop",
                    "client_order_id": f"auto-{now.strftime('%Y%m%d%H%M%S')}-{sym_t}-trail",
                })
                payload = json.loads(resp.body.decode('utf-8'))
                if payload.get('success'):
                    _autotrade_record_trade({
                        "symbol": sym_t, "name": name_t, "side": "sell",
                        "quantity": avail_t, "price": price_t,
                        "reason": hit_t,
                        "order_id": payload.get('order_id', ''),
                    })
                    _trailing_highs.pop(sym_t, None)
                    _persist_trailing_highs()
                    sold.append(sym_t)
                    _autotrade_log(f"追踪止盈 {name_t}({sym_t}) {avail_t}股 @{price_t:.2f} — {hit_t}", "sell")
                else:
                    _autotrade_log(f"追踪止盈 {sym_t} 被拒: {payload.get('error', '')[:60]}", "warn")
            except Exception as e:
                _autotrade_log(f"追踪止盈 {sym_t} 异常: {e}", "err")


        # === ① 卖出处理 (持仓): 复用确定性信号 (止损/止盈/退出计划) ===
        held_syms = [s for s in symbols if s in held]
        if held_syms:
            sigs_map = _autotrade_batch_signals(held_syms, quotes, snap, regime)
            for symbol, sig in sigs_map.items():
                if sig.get('action') != 'sell':
                    continue
                pos = snap['pos_map'].get(symbol)
                if not pos:
                    continue
                price = float(sig.get('price', 0) or 0)
                quote = quotes.get(symbol, {})
                name = quote.get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol)
                confidence = float(sig.get('confidence', 0) or 0)
                reason = str(sig.get('reason', ''))
                # 持仓数保护: 持仓太少时只执行止损, 暂缓止盈, 保住底仓
                if len(positions) <= min_keep and not any(k in reason for k in ('止损', '回撤', '破位')):
                    _autotrade_log(f"持仓仅 {len(positions)} 只(保护线 {min_keep})，暂缓止盈 {name}({symbol})", "muted")
                    continue

                # 最小持有期 (借鉴 Freqtrade minimum_roi 的时间维度):
                # 亏损归因显示多笔"买入次日即卖"全部亏损, 手续费+噪音吃掉收益。
                # 硬风险理由(止损/回撤/破位)可穿越, 其余信号须持满 min_hold_days。
                is_hard_risk = any(k in reason for k in ('止损', '回撤', '破位'))
                if not is_hard_risk and prot_cfg.min_hold_days > 0:
                    held_days = _autotrade_held_days(symbol)
                    if 0 <= held_days < prot_cfg.min_hold_days:
                        _autotrade_log(
                            f"最短持有期未满足 {name}({symbol}) 仅持有{held_days}天 "
                            f"(要求{prot_cfg.min_hold_days}天), 暂缓卖出", "muted")
                        continue

                avail = int(getattr(pos, 'available_quantity', 0) or 0)
                suggested = int(sig.get('suggested_qty', 0) or 0) or avail
                suggested = min(suggested, avail)
                if suggested <= 0:
                    continue
                try:
                    resp = place_order({
                        "symbol": symbol, "side": "sell", "quantity": suggested, "price": price,
                        "reason": "autotrade_ai_sell",
                        "client_order_id": f"auto-{now.strftime('%Y%m%d%H%M%S')}-{symbol}-sell",
                    })
                    payload = json.loads(resp.body.decode('utf-8'))
                    if payload.get('success'):
                        _autotrade_record_trade({
                            "symbol": symbol, "name": name, "side": "sell",
                            "quantity": suggested, "price": price,
                            "reason": f"AI信号 {confidence:.0%} | {reason[:60]}",
                            "order_id": payload.get('order_id', ''),
                        })
                        sold.append(symbol)
                        _autotrade_log(f"AI自动卖出 {name}({symbol}) {suggested}股 @{price:.2f}", "sell")
                    else:
                        _autotrade_log(f"卖出 {symbol} 被拒: {payload.get('error', '')[:60]}", "warn")
                except Exception as e:
                    _autotrade_log(f"卖出 {symbol} 异常: {e}", "err")

        # === ② 买入处理 (未持仓): 严格多因子分析, 评分排序优先买最优的 ===
        # 巨型 IPO 上市日避险: 上市日(T)及次日(T+1)禁止新开仓 (资金虹吸回测依据)
        ipo_guard_hit = None
        try:
            from src.news.ipo_guard import is_giant_ipo_day
            ipo_guard_hit = is_giant_ipo_day(now.date())
            if ipo_guard_hit is None:
                ipo_guard_hit = is_giant_ipo_day(now.date() - timedelta(days=1))
                if ipo_guard_hit is not None:
                    ipo_guard_hit["_t_plus_1"] = True
        except Exception:
            ipo_guard_hit = None
        if ipo_guard_hit is not None:
            _autotrade_log(
                f"巨型IPO {ipo_guard_hit.get('name','')} 上市日避险: "
                f"回测沪深300当日平均-0.98%, 本轮禁止新开仓 "
                f"({ipo_guard_hit.get('date')}, {ipo_guard_hit.get('note','')})",
                "warn",
            )
        # ── 保护机制全局检查 (借鉴 Freqtrade Protections) ──
        protection_pause: List[str] = []
        _sg = stoploss_guard_pause(_autotrade_state.get('trades', []), now.date(), prot_cfg)
        if _sg:
            protection_pause.append(_sg)
        _dg = drawdown_guard_pause(total_asset, float(risk_mgr.peak_equity or 0), prot_cfg)
        if _dg:
            protection_pause.append(_dg)
        for _pz in protection_pause:
            _autotrade_log(_pz, "warn")

        buy_pool = []
        for s in symbols:
            if s in held:
                continue
            cd = cooldown_block_reason(s, _autotrade_state.get('trades', []), now.date(), prot_cfg)
            if cd:
                _autotrade_log(f"冷却跳过 {s} — {cd}", "muted")
                continue
            buy_pool.append(s)
        if room > 0.05 and len(positions) < max_positions and ipo_guard_hit is None and not protection_pause:
            candidates = []
            target_date = target_trading_date(now, _premarket_holidays())
            screen_cfg = {
                "buy_score": buy_score, "min_confidence": min_conf,
                "min_risk_reward": min_rr, "total_asset": total_asset,
            }
            # 当日新闻涨幅因子 (单次拉取, 供所有候选使用)
            try:
                from src.news.news_factor import get_daily_factors
                news_map = {}
                for nf in get_daily_factors().get("factors", []):
                    news_map[nf["symbol"]] = nf
            except Exception:
                news_map = {}
            # 威科夫量价阶段 (对候选池预计算, 供所有候选使用)
            wyckoff_map = {}
            try:
                from src.analysis.wyckoff_phase import detect_phase
                for s in buy_pool:
                    try:
                        h = _load_daily_frame(s, 120)
                        if h is not None and not h.empty:
                            wp = detect_phase(h, float(quotes.get(s, {}).get('price', 0) or 0))
                            if wp.get('phase') != 'unknown':
                                wyckoff_map[s] = wp
                    except Exception:
                        continue
            except Exception:
                wyckoff_map = {}
            for symbol in buy_pool:
                try:
                    history = _load_daily_frame(symbol, 240)
                    if history.empty or 'Close' not in history.columns:
                        continue
                    opp = opportunity_scorer.analyze(
                        symbol, history,
                        equity=total_asset, cash=snap['cash'],
                        current_symbol_value=0,
                        quote=quotes.get(symbol, {}),
                    ).to_dict()
                    dq = professional_decision.data_quality(history, target_date, _premarket_holidays()).to_dict()
                    validation = _cached_opportunity_validation(symbol, history)
                    capital = None
                    try:
                        cap_summary = _get_capital_fetcher().get_main_net_summary(symbol)
                        if cap_summary:
                            capital = {"main_net": cap_summary.get("total_main_net", 0),
                                       "direction": cap_summary.get("direction", "")}
                    except Exception:
                        pass
                    screen = _autotrade_buy_screen(
                        symbol, opp, dq, regime, quotes.get(symbol, {}),
                        history, validation, capital, screen_cfg,
                        news_factor=news_map.get(symbol),
                        wyckoff=wyckoff_map.get(symbol),
                    )
                    # ATR 波动率 (供风险预算仓位 sizing, 借鉴 Freqtrade custom_stake_amount)
                    try:
                        screen['atr'] = round(compute_atr(history), 3)
                    except Exception:
                        screen['atr'] = 0.0
                    if screen["pass"]:
                        candidates.append((symbol, opp, screen))
                    else:
                        name = (quotes.get(symbol, {}) or {}).get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol)
                        _autotrade_log(
                            f"分析 {name}({symbol}): 未通过 — {'; '.join(screen['reject'][:3]) or '条件不足'}",
                            "muted",
                        )
                except Exception:
                    continue
            # 按综合分排序, 优先买分析最强的
            candidates.sort(key=lambda x: x[2]["score"], reverse=True)
            # 多空辩论 (借鉴 TradingAgents): 对通过初筛的前 3 只候选做三方 LLM 对抗
            try:
                from src.ai.debate import run_debate
                for di, (symbol, opp, screen) in enumerate(candidates[:3]):
                    f = screen.get("factors", {})
                    ctx = {
                        "price": float(opp.get('price', 0) or 0),
                        "change_pct": float(quotes.get(symbol, {}).get('change_pct', 0) or 0),
                        "score": f.get('score'), "confidence": f.get('confidence'),
                        "risk_reward": f.get('risk_reward'),
                        "trend": f"MA5 {f.get('ma5')} vs MA20 {f.get('ma20')}",
                        "capital": f"净流入{int(f.get('main_net', 0))}" if f.get('main_net') else "未知",
                        "news": f"{f.get('news_factor')}({f.get('news_direction')})" if f.get('news_direction') else "",
                        "wyckoff": f.get('wyckoff_note', ''), "win_rate": f.get('win_rate'),
                        "samples": f.get('samples'),
                    }
                    db = run_debate(symbol, quotes.get(symbol, {}).get('name', symbol), ctx)
                    if db:
                        screen["debate"] = db
                        if db.get("verdict") == "block":
                            screen["score"] -= 999
                            _autotrade_log(
                                f"辩论否决 {symbol}: 风控 {db.get('risk', {}).get('note', '')[:50]}", "warn")
                        else:
                            screen["score"] += db.get("adj", 0)
                            _autotrade_log(
                                f"辩论 {symbol}: 多{db['bull']['score']:.0f} 空{db['bear']['score']:.0f} "
                                f"调整{db['adj']:+.1f} 风控{db.get('verdict')}", "muted")
            except Exception:
                pass
            # 重新排序 (辩论调整后)
            candidates.sort(key=lambda x: x[2]["score"], reverse=True)
            # 剩余可加仓金额: 每笔买入后实时扣减, 累计不超过目标仓位
            room_amount = room * total_asset
            for symbol, opp, screen in candidates:
                if len(positions) + len(bought) >= max_positions:
                    break
                if room_amount <= 500:
                    break
                price = float(opp.get('price', 0) or 0)
                quote = quotes.get(symbol, {})
                name = quote.get('name') or COMMON_SYMBOL_NAMES.get(symbol, symbol)
                suggested = int(opp.get('suggested_qty', 0) or 0)
                budget = min(budget_pct * total_asset, room_amount)
                max_by_budget = int(budget / price / 100) * 100 if price > 0 else 0
                # ATR 风险预算: 单笔亏损上限 = 总资产×atr_risk_pct, 波动大→股数少 (借鉴 Freqtrade/vnpy)
                risk_qty = atr_position_qty(
                    price, float(screen.get('atr', 0) or 0), total_asset, prot_cfg
                )
                if risk_qty > 0:
                    if risk_qty < max_by_budget:
                        _autotrade_log(
                            f"{symbol} ATR仓位: {risk_qty}股 (预算上限 {max_by_budget}股, "
                            f"单笔风险 {prot_cfg.atr_risk_pct:.1%})", "muted")
                    max_by_budget = min(max_by_budget, risk_qty)
                qty = min(suggested, max_by_budget) if suggested > 0 else max_by_budget
                qty = max(qty // 100 * 100, 0)
                if qty <= 0:
                    _autotrade_log(f"{symbol} 预算不足 (可用 ¥{budget:.0f})，跳过", "muted")
                    continue
                reason = str(opp.get('reasons', []) or [])
                f = screen.get("factors", {})
                news_part = ""
                if f.get('news_direction'):
                    news_part = (f" 新闻{f.get('news_factor')}({f.get('news_direction')})"
                                 f"{('/'+'/'.join(f.get('news_events',[]))) if f.get('news_events') else ''}")
                try:
                    resp = place_order({
                        "symbol": symbol, "side": "buy", "quantity": qty, "price": price,
                        "reason": "autotrade_ai_buy",
                        "client_order_id": f"auto-{now.strftime('%Y%m%d%H%M%S')}-{symbol}-buy",
                    })
                    payload = json.loads(resp.body.decode('utf-8'))
                    if payload.get('success'):
                        _autotrade_record_trade({
                            "symbol": symbol, "name": name, "side": "buy",
                            "quantity": qty, "price": price,
                            "reason": (f"评分{f.get('score')} 置信度{f.get('confidence')} "
                                       f"胜率{f.get('win_rate')}% 资金{'净流入' if f.get('flow_direction')=='inflow' else f.get('flow_direction','')}"
                                       + (f" 技术[{f.get('alpha_notes','')[:30]}]" if f.get('alpha_notes') else '')
                                       + news_part + f" | {reason[:35]}"),
                            "order_id": payload.get('order_id', ''),
                        })
                        bought.append(symbol)
                        room_amount -= qty * price
                        _trailing_highs[symbol] = price  # 追踪止损从买价起算
                        _persist_trailing_highs()
                        _autotrade_log(
                            f"AI买入 {name}({symbol}) {qty}股 @{price:.2f} | "
                            f"评分{f.get('score')} 置信度{f.get('confidence')} 胜率{f.get('win_rate')}%{news_part}",
                            "buy",
                        )
                    else:
                        _autotrade_log(f"买入 {symbol} 被拒: {payload.get('error', '')[:60]}", "warn")
                except Exception as e:
                    _autotrade_log(f"买入 {symbol} 异常: {e}", "err")

        with _autotrade_lock:
            _autotrade_state['cycles'] += 1
            _autotrade_state['last_cycle'] = now.strftime("%H:%M:%S")
        summary = f"第 {_autotrade_state['cycles']} 轮完成 · 仓位 {current_pos_pct * 100:.0f}%"
        if bought:
            summary += f" · 买入 {len(bought)} 只"
        if sold:
            summary += f" · 卖出 {len(sold)} 只"
        _autotrade_log(summary, "ok")
    except Exception as exc:
        with _autotrade_lock:
            _autotrade_state['error'] = str(exc)
        _autotrade_log(f"本轮异常: {exc}", "err")


async def _autotrade_loop():
    """自托管循环: 开启时按周期执行, 未开启时低频巡检开关。"""
    _last_review_date = ""
    while True:
        try:
            enabled = False
            with _autotrade_lock:
                enabled = bool(_autotrade_state.get('enabled'))
            if enabled:
                await asyncio.to_thread(_autotrade_cycle)
            # 收盘后自动生成当日复盘 (15:05 之后, 每天一次)
            try:
                now_dt = datetime.now()
                today = now_dt.date().isoformat()
                session = market_session()
                if (
                    datetime_time(15, 5) <= now_dt.time() <= datetime_time(23, 59)
                    and _last_review_date != today
                    and not session.is_open
                ):
                    _last_review_date = today
                    await asyncio.to_thread(_build_daily_review, today)
            except Exception:
                pass
            # 交易时段 3 分钟一轮, 非交易时段 60 秒巡检开关即可
            await asyncio.sleep(180 if enabled else 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)


# ===========================================================================
# 收盘复盘 — 记录模拟盘当日成交, 按盈亏提炼策略供实盘参考
# ===========================================================================

REVIEW_DIR = os.path.join(ROOT_DIR, "output", "review")
_review_save_lock = threading.Lock()  # 复盘文件写入互斥 (防并发线程互相锁文件)


def _save_review_report(path: str, payload: str) -> bool:
    """健壮保存复盘报告: 加锁 + 临时文件原子替换 + 多重降级。

    Windows 下目标文件被读取/扫描时 os.replace 会 WinError 5, 逐级降级:
      1. 写 .tmp → os.replace
      2. replace 失败 → 删除目标再 replace
      3. 仍失败 → 直接写目标文件 (放弃原子性)
      4. 全部失败 → 重试 5 次后放弃
    """
    with _review_save_lock:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        tmp_path = path + ".tmp"
        for attempt in range(5):
            # 1. 写临时文件
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(payload)
            except OSError:
                time.sleep(0.4)
                continue
            # 2. 原子替换 (目标被占用时降级)
            try:
                os.replace(tmp_path, path)
                return True
            except OSError:
                try:
                    os.remove(path)
                    os.replace(tmp_path, path)
                    return True
                except OSError:
                    pass
            # 3. 兜底: 直接写目标
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(payload)
                return True
            except OSError:
                time.sleep(0.4)
        return False


def _cleanup_review_tmp():
    """清理复盘目录残留的 .tmp 临时文件。"""
    try:
        if os.path.isdir(REVIEW_DIR):
            for name in os.listdir(REVIEW_DIR):
                if name.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(REVIEW_DIR, name))
                    except OSError:
                        pass
    except Exception:
        pass


def _review_pnl_of_trades(symbol: str, trades: List[Dict]) -> Dict:
    """对单只股票当日成交做盈亏统计。

    返回: {buy_qty, buy_amount, sell_qty, sell_amount, realized_pnl}
    已实现盈亏优先用订单自带 realized_pnl (broker 按真实持仓成本计算);
    缺省时按 (卖出价 - 当日加权买入成本) 兜底配对。
    """
    # 关键修复: 必须按 symbol 过滤, 否则会把别的股票订单混入配对
    mine = [t for t in trades if t.get('symbol') == symbol]
    buys = [t for t in mine if t.get('side') == 'buy' and t.get('status') in ('filled', 'FILLED')]
    sells = [t for t in mine if t.get('side') == 'sell' and t.get('status') in ('filled', 'FILLED')]
    buy_qty = sum(int(t.get('filled_quantity') or t.get('quantity') or 0) for t in buys)
    buy_amount = sum(float(t.get('filled_price') or t.get('price') or 0) * int(t.get('filled_quantity') or t.get('quantity') or 0) for t in buys)
    avg_cost = buy_amount / buy_qty if buy_qty > 0 else 0.0
    sell_qty = 0
    sell_amount = 0.0
    realized = 0.0
    for t in sells:
        qty = int(t.get('filled_quantity') or t.get('quantity') or 0)
        px = float(t.get('filled_price') or t.get('price') or 0)
        sell_qty += qty
        sell_amount += px * qty
        # 优先用 broker 已算好的 realized_pnl (真实持仓成本)
        if t.get('realized_pnl') is not None:
            realized += float(t.get('realized_pnl') or 0)
        elif avg_cost > 0:
            realized += (px - avg_cost) * qty
    return {
        "buy_qty": buy_qty, "buy_amount": round(buy_amount, 2),
        "sell_qty": sell_qty, "sell_amount": round(sell_amount, 2),
        "avg_cost": round(avg_cost, 3),
        "realized_pnl": round(realized, 2),
    }


def _extract_signal_info(reason: str) -> Dict:
    """从自托管 reason 提取 评分/置信度/胜率 (用于信号质量分析)。"""
    info = {}
    try:
        import re as _re
        m = _re.search(r'评分(\d+)', reason or '')
        if m:
            info['score'] = int(m.group(1))
        m = _re.search(r'置信度(\d+)%', reason or '')
        if m:
            info['confidence'] = int(m.group(1)) / 100.0
        m = _re.search(r'胜率(\d+)%', reason or '')
        if m:
            info['win_rate'] = int(m.group(1))
        m = _re.search(r'止盈|止损|破位|回撤', reason or '')
        if m:
            info['exit_type'] = m.group(0)
    except Exception:
        pass
    return info


def _build_daily_review(target_date: Optional[str] = None):
    """生成模拟盘当日复盘: 成交记录 + 盈亏统计 + 策略提炼。

    数据源: broker 订单历史(模拟盘) + 持仓浮盈 + 自托管成交记录。
    报告持久化到 output/review/YYYY-MM-DD.json。
    """
    today = (target_date or datetime.now().date().isoformat())
    try:
        td = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        td = datetime.now().date()
        today = td.isoformat()

    # 1. 当日订单 (模拟盘)
    trades: List[Dict] = []
    try:
        df = broker.get_order_history()
        if not df.empty:
            for _, row in df.iterrows():
                created = row.get('created_at')
                if created is None:
                    continue
                cdate = created.date() if hasattr(created, 'date') else created
                if str(cdate)[:10] != today:
                    continue
                status = str(row.get('status', ''))
                if status.lower() not in ('filled', 'success'):
                    continue
                trades.append({
                    "symbol": str(row.get('symbol', '')),
                    "side": str(row.get('direction', row.get('side', ''))).lower(),
                    "quantity": int(row.get('quantity', 0) or 0),
                    "filled_quantity": int(row.get('filled_quantity', 0) or 0) or int(row.get('quantity', 0) or 0),
                    "price": float(row.get('price', 0) or 0),
                    "filled_price": float(row.get('filled_price', 0) or 0) or float(row.get('price', 0) or 0),
                    "status": status,
                    "time": str(created)[11:19] if hasattr(created, 'strftime') else str(created)[11:19],
                    "realized_pnl": row.get('realized_pnl'),
                })
    except Exception as e:
        _autotrade_log(f"复盘: 读取订单失败 {e}", "warn")

    # 2. 自托管成交记录 (带信号 reason)
    autotrade_trades = list(_autotrade_state.get('trades') or [])
    auto_by_symbol = {}
    for t in autotrade_trades:
        sym = t.get('symbol', '')
        if sym:
            auto_by_symbol.setdefault(sym, []).append(t)

    # 3. 当前持仓浮盈
    positions = broker.get_positions()
    pos_map = {getattr(p, 'symbol', ''): p for p in positions}
    names = {}
    for p in positions:
        names[getattr(p, 'symbol', '')] = getattr(p, 'name', '') or COMMON_SYMBOL_NAMES.get(getattr(p, 'symbol', ''), getattr(p, 'symbol', ''))

    # 4. 按股票汇总
    by_symbol = {}
    for t in trades:
        sym = t['symbol']
        if sym not in by_symbol:
            by_symbol[sym] = _review_pnl_of_trades(sym, trades)
            by_symbol[sym]['symbol'] = sym
            by_symbol[sym]['name'] = names.get(sym, sym)
            by_symbol[sym]['autotrade'] = auto_by_symbol.get(sym, [])
    syms = sorted(by_symbol.keys())

    # 5. 盈亏汇总
    realized_total = sum(float(v.get('realized_pnl', 0) or 0) for v in by_symbol.values())
    unrealized_total = 0.0
    for sym in syms:
        pos = pos_map.get(sym)
        if pos:
            unrealized_total += float(getattr(pos, 'unrealized_pnl', 0) or 0)
    account = broker.get_account_info()
    total_asset = float(account.get('total_asset', 0) or 0)
    market_value = float(account.get('market_value', 0) or 0)

    # 6. 策略提炼 (核心: 根据当天盈亏给出相对策略)
    lessons: List[str] = []
    strategies: List[Dict] = []
    day_result = realized_total + unrealized_total
    buy_count = sum(1 for t in trades if t['side'] == 'buy')
    sell_count = sum(1 for t in trades if t['side'] == 'sell')

    # 6.1 整体盈亏
    if day_result >= 0:
        lessons.append(f"当日盈亏 {money_cn(day_result)} (已实现 {money_cn(realized_total)} + 浮动 {money_cn(unrealized_total)})，盈利状态")
    else:
        lessons.append(f"当日盈亏 {money_cn(day_result)} (已实现 {money_cn(realized_total)} + 浮动 {money_cn(unrealized_total)})，亏损状态")
    lessons.append(f"成交 {buy_count} 笔买入 / {sell_count} 笔卖出, 当前持仓 {len(positions)} 只, 仓位 {market_value / max(total_asset, 1) * 100:.0f}%")

    # 6.2 买入信号质量 (浮盈比例)
    buy_quality = []
    for sym in syms:
        auto = by_symbol[sym]['autotrade']
        if not auto:
            continue
        pos = pos_map.get(sym)
        if not pos:
            continue
        avg_cost = float(getattr(pos, 'avg_cost', 0) or 0)
        last = float(getattr(pos, 'last_price', 0) or getattr(pos, 'market_value', 0) / max(getattr(pos, 'quantity', 1), 1))
        ret = (last - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
        info = _extract_signal_info(auto[0].get('reason', '')) if auto else {}
        buy_quality.append({"symbol": sym, "name": by_symbol[sym]['name'], "return_pct": round(ret, 1), **info})
    win_buys = [b for b in buy_quality if b['return_pct'] > 0]
    if buy_quality and len(win_buys) / len(buy_quality) < 0.5:
        strategies.append({
            "title": "买入信号过松",
            "priority": "高",
            "content": f"当日买入 {len(buy_quality)} 只, 仅 {len(win_buys)} 只浮盈 ({len(win_buys) / len(buy_quality) * 100:.0f}%)。建议提高买入门槛: 评分+5 或 只买历史胜率≥45% 的标的。",
        })
    elif buy_quality and len(win_buys) / len(buy_quality) >= 0.7:
        strategies.append({
            "title": "买入信号有效",
            "priority": "中",
            "content": f"当日买入 {len(buy_quality)} 只, {len(win_buys)} 只浮盈 ({len(win_buys) / len(buy_quality) * 100:.0f}%)。可保持当前买入标准, 实盘参考该信号组合。",
        })

    # 6.3 卖出质量 (止损纪律 / 止盈落袋)
    exit_types = {}
    for sym in syms:
        for t in by_symbol[sym]['autotrade']:
            if t.get('side') == 'sell':
                info = _extract_signal_info(t.get('reason', ''))
                et = info.get('exit_type', '信号')
                exit_types[et] = exit_types.get(et, 0) + 1
    if exit_types.get('止损'):
        strategies.append({
            "title": "止损纪律",
            "priority": "中",
            "content": f"当日止损 {exit_types['止损']} 次。止损果断执行, 实盘务必保持同一纪律; 若止损频繁, 需检查买入时机是否追高。",
        })
    if exit_types.get('止盈'):
        strategies.append({
            "title": "止盈保护",
            "priority": "中",
            "content": f"当日止盈 {exit_types['止盈']} 次。若止盈后继续上涨, 实盘可改为分批止盈(如 50%+剩余跟踪)。",
        })

    # 6.4 仓位管理 (仓位不做上限, 只看现金充裕度提示建仓机会)
    target_pct = 1.0
    current_pct = market_value / max(total_asset, 1)
    cash_pct = 1.0 - current_pct
    if cash_pct > 0.5:
        strategies.append({
            "title": "现金充裕",
            "priority": "低",
            "content": f"当前仓位仅 {current_pct * 100:.0f}%，现金 {cash_pct * 100:.0f}%。以营利为第一目标, 实盘可关注通过严格分析(评分/趋势/资金)确认的标的适度建仓。",
        })
    elif current_pct > 0.95:
        strategies.append({
            "title": "近乎满仓",
            "priority": "中",
            "content": f"当前仓位 {current_pct * 100:.0f}% 已接近满仓。持仓集中度高, 实盘注意单票风险, 卖出后资金可再投入优质标的(仓位不做上限)。",
        })

    # 6.5 当日亏损复盘 (亏损必须有行动建议, 供实盘参考)
    if day_result < 0:
        strategies.append({
            "title": "当日亏损复盘",
            "priority": "高",
            "content": (f"当日亏损 {money_cn(abs(day_result))} (已实现 {money_cn(abs(realized_total))})。"
                       f"建议: ① 检查卖出时点 — 止损是否果断、止盈是否过晚; "
                       f"② 若亏损来自买入后浮亏, 复盘买入信号是否追高; "
                       f"③ 实盘对照当日策略, 避免重复同样错误。"),
        })
    elif day_result > 0 and realized_total > 0:
        strategies.append({
            "title": "盈利锁定",
            "priority": "中",
            "content": f"当日盈利 {money_cn(day_result)} 且已实现 {money_cn(realized_total)}。止盈纪律执行良好, 实盘保持; 注意防止回吐, 可分批止盈。",
        })

    # 6.6 默认兜底策略
    if not strategies:
        strategies.append({
            "title": "维持当前策略",
            "priority": "低",
            "content": "当日信号与盈亏未显示明显偏差, 建议维持当前买入标准与仓位纪律。",
        })

    # 6.7 信号反馈闭环 (借鉴 Wyckoff): 每笔成交打结果标签 + 按信号类型统计胜率
    signal_stats = {"buy": {"对": 0, "错": 0}, "sell": {"对": 0, "错": 0},
                    "by_signal": {}}
    last_prices = {}
    for p in positions:
        last_prices[getattr(p, 'symbol', '')] = float(getattr(p, 'last_price', 0) or 0)
    for t in trades:
        sym = t.get('symbol', '')
        px = float(t.get('filled_price', 0) or t.get('price', 0) or 0)
        side = t.get('side', '')
        ref = last_prices.get(sym, px) or px  # 用当前价判断结果 (收盘复盘近似)
        if side == 'buy' and px > 0:
            label = "买对" if ref >= px else "买错"
        elif side == 'sell' and px > 0:
            label = "卖对" if ref <= px else "卖飞"  # 卖出后继续跌=对, 反弹=飞
        else:
            label = ""
        t['result_label'] = label
        if label:
            key = "对" if label in ("买对", "卖对") else "错"
            signal_stats[side][key] = signal_stats[side].get(key, 0) + 1
        # 按信号类型归类 (从自托管 reason 提取)
        auto = auto_by_symbol.get(sym, [])
        reason = (auto[0].get('reason', '') if auto else '') or ''
        sig_key = "新闻驱动" if '新闻' in reason else ("高评分" if '评分6' in reason or '评分7' in reason or '评分8' in reason or '评分9' in reason else "普通信号")
        bucket = signal_stats["by_signal"].setdefault(sig_key, {"对": 0, "错": 0})
        if label:
            bucket[key] = bucket.get(key, 0) + 1

    report = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_asset": round(total_asset, 2),
            "market_value": round(market_value, 2),
            "position_ratio": round(current_pct, 4),
            "realized_pnl": round(realized_total, 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "day_pnl": round(day_result, 2),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "position_count": len(positions),
            "target_position_pct": target_pct,
            "signal_stats": signal_stats,
        },
        "trades": trades,
        "by_symbol": list(by_symbol.values()),
        "lessons": lessons,
        "strategies": strategies,
    }
    # 持久化 (加锁 + 原子写入 + 多重降级, 兼容 Windows 文件锁)
    try:
        path = os.path.join(REVIEW_DIR, f"{today}.json")
        payload = json.dumps(report, ensure_ascii=False, indent=1)
        if _save_review_report(path, payload):
            with _autotrade_lock:
                _autotrade_state['last_review'] = today
        else:
            _autotrade_log(f"复盘: 保存失败 (文件被占用), 报告仍在内存中可查询", "warn")
        # 策略记忆库 (借鉴 AgentQuant): 写入信号证据供跨日检索
        try:
            from src.analysis.strategy_memory import record_evidence
            evidence = []
            auto_map = {}
            for t in trades:
                sym = t.get('symbol', '')
                if sym not in auto_map:
                    auto = auto_by_symbol.get(sym, [])
                    auto_map[sym] = (auto[0].get('reason', '') if auto else '') or ''
                reason = auto_map[sym]
                sig_type = "新闻驱动" if '新闻' in reason else (
                    "高评分" if any(k in reason for k in ('评分6', '评分7', '评分8', '评分9')) else "普通信号")
                evidence.append({
                    "date": today, "symbol": sym, "name": names.get(sym, sym),
                    "side": t.get('side', ''), "signal_type": sig_type,
                    "result_label": t.get('result_label', ''), "pnl": 0.0,
                    "reason": reason,
                })
            record_evidence(evidence)
        except Exception:
            pass
    except Exception as e:
        _autotrade_log(f"复盘: 保存失败 {e}", "warn")
    _autotrade_log(f"收盘复盘已生成: {today} 当日盈亏 {money_cn(day_result)}", "ok")
    return report


def money_cn(v: float) -> str:
    """金额中文显示 (万元)。"""
    v = float(v or 0)
    if abs(v) >= 1e8:
        return f"{v/1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v/1e4:.1f}万"
    return f"{v:.0f}元"


@app.get("/api/market/candidates")
def market_candidates_api():
    """全市场选股漏斗结果 (第一级粗筛), 供前端观察选股路径覆盖面。

    返回全市场快照经流动性/市值/换手/涨幅/价格硬过滤后的候选,
    并给出"扫描了多少只 → 剩多少只"的漏斗统计。
    """
    try:
        from src.analysis.market_scanner import (
            fetch_market_snapshot, load_prefilter_config, prefilter_market,
        )
        cfg = load_prefilter_config(config.get('autotrade', {}) or {})
        rows = fetch_market_snapshot(cfg)
        cands = prefilter_market(rows, cfg)
        return JSONResponse({
            "success": True,
            "scanned": len(rows),
            "candidates": len(cands),
            "config": {
                "min_amount": cfg.min_amount,
                "max_change_pct": cfg.max_change_pct,
                "max_candidates": cfg.max_candidates,
            },
            "items": [{
                "symbol": r.get("symbol", ""),
                "name": r.get("name", ""),
                "price": r.get("price"),
                "change_pct": r.get("change_pct"),
                "amount": r.get("amount"),
                "turnover": r.get("turnover"),
                "float_mktcap_yi": r.get("float_mktcap_yi"),
            } for r in cands],
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)[:200],
                             "scanned": 0, "candidates": 0, "items": []})


@app.get("/api/performance/report")
def performance_report_api():
    """组合绩效报告 (借鉴 QuantStats/Qlib risk_analysis)。

    基于每日净值点 (自托管循环自动记录) 计算 年化收益/Sharpe/Sortino/
    Calmar/最大回撤/月度收益表, 并对比基准 (沪深300) 区间收益。
    """
    try:
        from src.research.performance import performance_report, _series
        points = list(_equity_history or [])
        trades = _autotrade_state.get('trades', [])
        # 基准: 沪深300 与净值区间对齐的收盘序列
        bench_points = []
        try:
            bmk = _load_daily_frame('sh000300', 500)
            if bmk is not None and not bmk.empty:
                for d, row in bmk.iterrows():
                    try:
                        bench_points.append({
                            "date": str(d)[:10],
                            "value": round(float(row['Close']), 2),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
        except Exception:
            pass
        report = performance_report(points, trades=trades, benchmark_points=bench_points)
        report["equity_curve"] = points[-120:]  # 最近120个点供前端画图
        return JSONResponse(content={"success": True, "data": report})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.get("/api/metrics")
def metrics():
    """可观测性指标 (Prometheus 文本格式兼容, 借鉴 QuantDinger)。"""
    try:
        from src.news.news_factor import get_daily_factors, _cache as _nf_cache
        from src.analysis.strategy_memory import query_stats
        snap = _portfolio_snapshot()
        account = broker.get_account_info()
        total = float(account.get('total_asset', 0) or 0)
        mv = float(account.get('market_value', 0) or 0)
        with _autotrade_lock:
            cycles = _autotrade_state.get('cycles', 0)
            autotrade_on = bool(_autotrade_state.get('enabled', False))
        nf = get_daily_factors()
        mem = query_stats()
        lines = [
            "# HELP aiq_account_total_asset 总资产",
            "# TYPE aiq_account_total_asset gauge",
            f"aiq_account_total_asset {total:.2f}",
            "# HELP aiq_account_position_ratio 仓位占比",
            "# TYPE aiq_account_position_ratio gauge",
            f"aiq_account_position_ratio {mv / max(total, 1):.4f}",
            "# HELP aiq_autotrade_cycles 自托管运行轮次",
            "# TYPE aiq_autotrade_cycles counter",
            f"aiq_autotrade_cycles {cycles}",
            "# HELP aiq_autotrade_enabled 自托管开关",
            "# TYPE aiq_autotrade_enabled gauge",
            f"aiq_autotrade_enabled {1 if autotrade_on else 0}",
            "# HELP aiq_news_factors 今日新闻因子覆盖股票数",
            "# TYPE aiq_news_factors gauge",
            f"aiq_news_factors {len(nf.get('factors', []))}",
            "# HELP aiq_news_count 今日抓取新闻条数",
            "# TYPE aiq_news_count gauge",
            f"aiq_news_count {nf.get('news_count', 0)}",
            "# HELP aiq_memory_evidence 策略记忆库累计样本",
            "# TYPE aiq_memory_evidence gauge",
            f"aiq_memory_evidence {mem.get('total', 0)}",
            "# HELP aiq_trades_buy 模拟盘累计买入笔数",
            "# TYPE aiq_trades_buy counter",
            f"aiq_trades_buy {_trading_counters.get('buy', 0)}",
            "# HELP aiq_trades_sell 模拟盘累计卖出笔数",
            "# TYPE aiq_trades_sell counter",
            f"aiq_trades_sell {_trading_counters.get('sell', 0)}",
        ]
        return JSONResponse({"success": True, "prometheus": "\n".join(lines),
                             "json": {"total_asset": round(total, 2),
                                      "position_ratio": round(mv / max(total, 1), 4),
                                      "autotrade_cycles": cycles,
                                      "autotrade_enabled": autotrade_on,
                                      "news_factors": len(nf.get('factors', [])),
                                      "news_count": nf.get('news_count', 0),
                                      "memory_evidence": mem.get('total', 0),
                                      "trades": dict(_trading_counters)}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/memory/signals")
def memory_signals(signal_type: str = Query(""), days: int = Query(0)):
    """策略记忆库: 按信号类型统计历史胜率 (借鉴 AgentQuant)。"""
    try:
        from src.analysis.strategy_memory import query_stats
        return JSONResponse({"success": True,
                             **query_stats(signal_type or None, days or None)})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/news/factors")
def news_factors():
    """当日新闻涨幅因子: 热点新闻 → 涉及个股因子分 (供研究页/自托管参考)。"""
    try:
        from src.news.news_factor import get_daily_factors
        data = get_daily_factors()
        return JSONResponse({
            "success": True,
            "date": data.get("date", ""),
            "news_count": data.get("news_count", 0),
            "generated_at": data.get("generated_at", ""),
            "factors": data.get("factors", []),
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/ipo/guard")
def ipo_guard():
    """巨型 IPO 上市日避险守卫状态。"""
    try:
        from src.news.ipo_guard import ipo_guard_status
        return JSONResponse({"success": True, **ipo_guard_status()})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/news/factors/refresh")
def news_factors_refresh():
    """强制刷新当日新闻因子。"""
    try:
        from src.news.news_factor import get_daily_factors
        data = get_daily_factors(force=True)
        return JSONResponse({"success": True, "news_count": data.get("news_count", 0),
                             "factors": data.get("factors", [])})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


async def _news_factor_loop():
    """每日新闻因子循环: 交易时段每 30 分钟抓取刷新 + 收盘后记录前瞻验证。"""
    while True:
        try:
            from src.news.news_factor import get_daily_factors, factor_for_symbol
            now_dt = datetime.now()
            session = market_session()
            if session.is_open:
                # 盘中刷新热点因子
                await asyncio.to_thread(get_daily_factors, True)
            elif datetime_time(15, 5) <= now_dt.time() <= datetime_time(23, 59):
                # 收盘后: 为持仓与候选记录前瞻验证快照 + 回填历史因子收益
                try:
                    from src.news.news_factor import record_validation, update_validation_returns
                    factors = get_daily_factors().get("factors", [])
                    for nf in factors[:30]:
                        record_validation(nf["symbol"], nf["factor_score"], nf["direction"])
                    update_validation_returns()
                except Exception:
                    pass
            await asyncio.sleep(1800)  # 30 分钟
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(300)


@app.get("/api/review")
def get_review(date: str = Query("")):
    """获取收盘复盘报告 (默认今日, 可指定 YYYY-MM-DD)。"""
    target = (date or datetime.now().date().isoformat())
    path = os.path.join(REVIEW_DIR, f"{target}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return JSONResponse(json.load(f))
        except Exception as e:
            return JSONResponse({"success": False, "error": f"复盘文件损坏: {e}"})
    # 未生成则现场生成
    try:
        report = _build_daily_review(target)
        return JSONResponse(report)
    except Exception as e:
        return JSONResponse({"success": False, "error": f"复盘生成失败: {e}"})


@app.post("/api/review/generate")
def generate_review():
    """手动触发收盘复盘。"""
    try:
        report = _build_daily_review(datetime.now().date().isoformat())
        return JSONResponse({"success": True, "report": report})
    except Exception as e:
        return JSONResponse({"success": False, "error": f"复盘生成失败: {e}"})


@app.get("/api/autotrade/status")
def autotrade_status():
    """自托管 AI 自动交易状态。"""
    with _autotrade_lock:
        st = dict(_autotrade_state)
        st['log'] = list(_autotrade_state['log'])
        st['trades'] = list(_autotrade_state['trades'])
        st['config'] = {
            "max_positions": int(config.get('autotrade.max_positions', 5) or 5),
            "per_trade_budget_pct": float(config.get('autotrade.per_trade_budget_pct', 0.2) or 0.2),
            "min_confidence": float(config.get('autotrade.min_confidence', 0.55) or 0.55),
            "cycle_seconds": 180,
        }
    return JSONResponse(st)


@app.post("/api/autotrade/toggle")
def autotrade_toggle(body: dict):
    """启停自托管 AI 自动交易 (仅虚拟盘)。body: {enabled: bool}"""
    enabled = bool(body.get('enabled', False))
    with _autotrade_lock:
        _autotrade_state['enabled'] = enabled
        if enabled and not _autotrade_state.get('started_at'):
            _autotrade_state['started_at'] = datetime.now().isoformat()
        if not enabled:
            _autotrade_state['started_at'] = ''
    _autotrade_log("自托管模式已开启 — AI 将自动选股买卖 (虚拟资金)" if enabled else "自托管模式已关闭", "ok" if enabled else "warn")
    # 持久化开关状态 (重启后保持)
    try:
        state_manager.save_account_state('autotrade_state', {
            "enabled": enabled,
            "started_at": _autotrade_state.get('started_at', ''),
            "last_cycle": _autotrade_state.get('last_cycle', ''),
            "cycles": _autotrade_state.get('cycles', 0),
        })
    except Exception:
        pass
    # 开启后立即触发一轮, 不用等下一个巡检周期
    if enabled:
        try:
            _threading.Thread(target=_autotrade_cycle, daemon=True).start()
        except Exception:
            pass
    return JSONResponse({"success": True, "enabled": enabled})


# ===========================================================================
# 实盘交易端点 — 基金 (爱基金) & 股票 (guling-trader/同花顺)
# 项目内直接完成真实交易, 不依赖 WorkBuddy。
# ===========================================================================

def _fund_trader():
    from src.trading.fund_trader import FundTrader
    return FundTrader()


def _stock_trader():
    from src.trading.stock_trader import StockTrader
    return StockTrader.from_config()


@app.get("/api/fund/holdings")
def fund_holdings():
    """基金 + 钱包持仓。"""
    try:
        holdings = _fund_trader().get_all_holdings()
        return JSONResponse({"success": True, "data": holdings})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/fund/buy")
def fund_buy(body: dict):
    """基金申购 (金额)。

    body: {fund_code, amount, pay_type: 'wallet'|'bank' (默认 wallet),
           trans_account_id?, cust_id?}
    """
    try:
        fund_code = str(body.get('fund_code', '')).strip()
        amount = float(body.get('amount', 0))
        if not fund_code or amount <= 0:
            return JSONResponse({"success": False, "error": "fund_code 和 amount(>0) 必填"})
        pay_type = str(body.get('pay_type', 'wallet')).lower()
        if pay_type not in ('wallet', 'bank'):
            return JSONResponse({"success": False, "error": "pay_type 必须是 wallet 或 bank"})
        result = _fund_trader().buy(
            fund_code, amount,
            pay_type=pay_type,
            trans_account_id=str(body.get('trans_account_id', '') or ''),
            cust_id=str(body.get('cust_id', '') or ''),
        )
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/fund/redeem")
def fund_redeem(body: dict):
    """基金赎回 (份额)。

    body: {fund_code, share_vol, trans_account_id, redemption_type: '1'|'0'}
    """
    try:
        fund_code = str(body.get('fund_code', '')).strip()
        share_vol = float(body.get('share_vol', 0))
        trans_account_id = str(body.get('trans_account_id', '') or '')
        if not fund_code or share_vol <= 0 or not trans_account_id:
            return JSONResponse({"success": False, "error": "fund_code/share_vol/trans_account_id 必填"})
        redemption_type = str(body.get('redemption_type', '1'))
        result = _fund_trader().redeem(
            fund_code, share_vol, trans_account_id,
            redemption_type=redemption_type,
        )
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/fund/orders")
def fund_orders(cust_id: Optional[str] = None, days: int = Query(30, ge=1, le=180),
                limit: int = Query(20, ge=1, le=200)):
    """基金交易记录 (含中文状态)。"""
    try:
        if not cust_id:
            cust_id = str(config.get('fund.cust_id', '') or '')
        if not cust_id:
            return JSONResponse({"success": False, "error": "缺少 cust_id (body 传参或配置 fund.cust_id)"})
        rows = _fund_trader().get_order_list(cust_id, limit=limit)
        from src.trading.fund_trader import FundTrader
        for r in rows:
            st = FundTrader.judge_order_status(r)
            r["_status"] = st
        return JSONResponse({"success": True, "data": rows})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/fund/info/{code}")
def fund_info(code: str):
    """基金完整详情: 基本信息 + 费率 + 风险等级 + 购买规则。"""
    try:
        info = _fund_trader().get_fund_info(code)
        return JSONResponse({"success": True, "data": info})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/fund/order/{serial}")
def fund_order_detail(serial: str):
    """基金订单详情 (含中文状态判定)。"""
    try:
        from src.trading.fund_trader import FundTrader
        detail = _fund_trader().get_order_detail(serial)
        status = FundTrader.judge_order_status(detail)
        detail["_status"] = status
        return JSONResponse({"success": True, "data": detail})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/fund/revoke")
def fund_revoke(body: dict):
    """基金撤单。body: {serial}"""
    try:
        serial = str(body.get('serial', '') or '').strip()
        if not serial:
            return JSONResponse({"success": False, "error": "serial 必填"})
        result = _fund_trader().revoke(serial)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/live/status")
def live_status():
    """股票实盘连接状态 + 账户 + 持仓 + 在飞委托。"""
    try:
        trader = _stock_trader()
        snap = trader.snapshot()
        return JSONResponse({"success": True, "data": snap})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/live/order")
def live_order(body: dict):
    """股票实盘下单。

    body: {symbol, side: 'buy'|'sell', quantity, price?, reason?, auto_trade?}
    注意: 受 config.yaml trading.auto_trade 门禁, 未开启时返回 blocked。
    """
    try:
        symbol = str(body.get('symbol', '')).strip()
        side = str(body.get('side', 'buy')).lower()
        quantity = int(body.get('quantity', 0) or 0)
        if not symbol or quantity <= 0 or side not in ('buy', 'sell'):
            return JSONResponse({"success": False, "error": "symbol/side(buy|sell)/quantity(>0) 必填"})
        price = float(body.get('price') or 0) or None
        reason = str(body.get('reason', 'api_live'))[:500]
        trader = _stock_trader()
        if side == 'buy':
            result = trader.buy(symbol, quantity, price, reason)
        else:
            result = trader.sell(symbol, quantity, price, reason)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/live/cancel")
def live_cancel(body: dict):
    """股票实盘撤单。body: {entrust_no}"""
    try:
        entrust_no = str(body.get('entrust_no', '') or '').strip()
        if not entrust_no:
            return JSONResponse({"success": False, "error": "entrust_no 必填"})
        trader = _stock_trader()
        ok, msg = trader.cancel_order(entrust_no)
        return JSONResponse({"success": ok, "message": msg})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


def main():
    _cleanup_review_tmp()  # 启动时清理复盘残留 .tmp
    port = config.get('server.port', 8080)
    host = config.get('server.host', '127.0.0.1')
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
