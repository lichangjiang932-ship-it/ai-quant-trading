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
    global _premarket_scheduler_task
    if bool(config.get('premarket.scheduler_enabled', True)) and _premarket_scheduler_task is None:
        _premarket_scheduler_task = asyncio.create_task(_premarket_scheduler_loop())
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
_validation_lock = threading.RLock()
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
        per_list = max(limit // 2, 10)
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


def _prescreen(candidates: List[Dict], top: int = 6) -> List[Dict]:
    """规则粗筛: 用实时行情的量价指标给候选打分, 取头部(省 LLM 调用)。"""
    syms = [c["symbol"] for c in candidates]
    if not syms:
        return []
    quotes = realtime.get_quotes(syms, sources=['tencent', 'sina', 'eastmoney']) or {}
    scored = []
    for c in candidates:
        q = dict(c)
        for key, value in (quotes.get(c["symbol"], {}) or {}).items():
            if key in {'price', 'pre_close', 'open', 'high', 'low'} and not value and q.get(key):
                continue
            q[key] = value
        intraday = entry_guard.intraday_snapshot(q)
        if not entry_guard.prescreen_allowed(q):
            continue
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
        scored.append(c)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


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
    reason = "；".join(primary_reasons[:3]) or "多因子评分暂未达到买入标准"
    if validation.get('samples', 0):
        reason += (
            f"；历史相似机会 {validation['samples']} 次，"
            f"胜率 {validation['win_rate']:.1f}%"
        )
    if not data_quality.get('allowed'):
        reason += "；行情质量门禁未通过"
    elif not validation_ok:
        reason += "；历史样本胜率或平均收益未通过审批"
    if hard_veto:
        reason += "；AI 风险复核给出高置信度否决"
    if entry_evaluation.get('reasons'):
        reason += "；" + "；".join(entry_evaluation['reasons'][:3])
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
                          "quote": quotes.get(s, {})} for s in syms][:max(top, 6)]
        else:
            candidates = _fetch_candidates(limit=40)
            with _scan_lock:
                _scan_job["candidates"] = len(candidates)
            shortlist = _prescreen(candidates, top=top)

        snap = _portfolio_snapshot()
        benchmark_symbol = _normalize_symbol(str(config.get('professional.benchmark_symbol', 'sh000001')))
        benchmark = _load_daily_frame(benchmark_symbol, 240)
        market_regime = professional_decision.market_regime(benchmark).to_dict()
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
            q = item.get("quote") or realtime.get_quotes(
                [sym], sources=['tencent', 'sina', 'eastmoney']
            ).get(sym, {})
            res = _analyze_one(sym, q, snap, market_regime)
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
        "mode": "paper",
        "opening_enabled": bool(_trading_control.get('opening_enabled', True)),
        "max_position_size": float(getattr(risk_mgr, 'max_position_size', 1.0)),
        "max_total_position": float(getattr(risk_mgr, 'max_total_position', 1.0)),
    })


@app.get("/api/system/status")
def get_system_status():
    session = market_session()
    return JSONResponse({
        "mode": "paper",
        "mode_label": "A股模拟盘",
        "broker": "SimulatedBroker",
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


def _stream_snapshot(extra_symbols: Optional[List[str]] = None) -> Dict:
    """把页面需要的轻量数据合成一帧，供 SSE 与降级轮询共用。"""
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

    # 行情先写入模拟券商，再计算资产、持仓和风控，避免快照出现一帧延迟。
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
        "positions": [p.to_dict() for p in snap["positions"]],
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
async def stream_updates(symbol: Optional[str] = Query(None)):
    """SSE 增量推送：交易时段 3s，休市 30s；内容无变化时只发心跳。

    取代前端过去的多组 setInterval 全量轮询，避免整页重建导致的闪烁。
    """
    async def event_source():
        last_digest = None
        event_id = 0
        # 建连立刻推一帧完整快照，前端无需再等第一个轮询周期
        while True:
            try:
                extras = [symbol] if symbol else []
                payload = await asyncio.to_thread(_stream_snapshot, extras)
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
            await asyncio.sleep(3 if open_now else 30)

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
async def get_dashboard_snapshot(symbol: Optional[str] = Query(None)):
    """SSE 不可用时的单请求快照，也供用户手动刷新使用。"""
    extras = [symbol] if symbol else []
    payload = await asyncio.to_thread(_stream_snapshot, extras)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


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
        if not 0 <= commission_rate <= 0.05:
            return JSONResponse({"success": False, "error": "佣金费率必须在 0 到 5% 之间"})
        if not 0 <= stamp_tax_rate <= 0.05:
            return JSONResponse({"success": False, "error": "印花税率必须在 0 到 5% 之间"})
        if not 0 <= min_commission <= 10000:
            return JSONResponse({"success": False, "error": "最低佣金超出有效范围"})
        if not 0 <= slippage <= 0.10:
            return JSONResponse({"success": False, "error": "滑点必须在 0 到 10% 之间"})

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
        broker._session_date = datetime.now().date()
        risk_mgr.daily_pnl = 0.0
        risk_mgr.daily_order_count = 0
        risk_mgr.peak_equity = initial_capital

        # 同步写入 config.yaml（持久化）
        config.set('trading.initial_capital', int(initial_capital))
        config.set('commission.rate', commission_rate)
        config.set('commission.min', int(min_commission))
        config.set('commission.stamp_tax', stamp_tax_rate)
        config.set('trading.slippage', slippage)
        config.save_config(CONFIG_PATH)
        _persist_broker_state()
        _persist_risk_runtime()

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
    return JSONResponse({
        "status": job["status"],
        "pool": job["pool"],
        "total": job["total"],
        "done": job["done"],
        "current": job["current"],
        "candidates": job["candidates"],
        "candidate_error": getattr(_fetch_candidates, 'last_error', '') or '',
        "picks": picks,
        "buy_count": len(buys),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "profit_guaranteed": False,
        "disclaimer": "AI 选股不能确保盈利；页面会按最新价复核，并展示若在推荐价买入后的费用后净盈亏。",
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


def main():
    port = config.get('server.port', 8080)
    host = config.get('server.host', '127.0.0.1')
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
