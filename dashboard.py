import sys
import os
import time
from datetime import datetime, timedelta
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import streamlit as st
    import pandas as pd
    import numpy as np
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
except ImportError:
    print("请安装依赖: pip install streamlit plotly pandas numpy")
    sys.exit(1)

from src.data.market_data import MarketData
from src.data.realtime.realtime_data import RealtimeData
from src.data.data_loader import DataLoader
from src.strategies.cross_ma_strategy import CrossMAStrategy
from src.strategies.realtime_momentum_strategy import RealtimeMomentumStrategy
from src.strategies.realtime_mean_reversion_strategy import RealtimeMeanReversionStrategy
from src.strategies.realtime_strategy import TradingSignal, SignalType
from src.execution.brokers.simulated_broker import SimulatedBroker
from src.execution.brokers.base_broker import Order, OrderDirection, OrderType
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide
from src.utils.trade_logger import TradeLogger
from src.utils.config import Config
from src.backtest.backtester import Backtester
from src.backtest.optimizer import StrategyOptimizer
from src.news.news_analyzer import NewsAnalyzer
from src.scheduler.market_monitor import MarketMonitor, MarketAlert


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.yaml")
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOG_DIR = os.path.join(ROOT_DIR, "logs")


st.set_page_config(
    page_title="AI量化交易平台",
    page_icon="A",
    layout="wide"
)

_DARK_CSS = """
<style>
/* 深色主题覆盖 */
.stApp {
    background: #0A0A0B;
    color: #E4E4E7;
    font-family: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif;
}
/* 侧边栏 */
[data-testid="stSidebar"] {
    background: #141417 !important;
    border-right: 1px solid #27272A !important;
}
[data-testid="stSidebar"] .stMarkdown h1,
[data-testid="stSidebar"] .stMarkdown h2,
[data-testid="stSidebar"] .stMarkdown h3 {
    color: #E4E4E7 !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label {
    color: #A1A1AA !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
    color: #E4E4E7 !important;
}
/* 主标题 */
h1, h2, h3 {
    color: #E4E4E7 !important;
    font-weight: 600 !important;
}
/* 卡片容器 */
.kpi-card {
    background: #141417;
    border: 1px solid #27272A;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 12px;
}
.kpi-label {
    font-size: 13px;
    color: #71717A;
    margin-bottom: 4px;
}
.kpi-value {
    font-size: 24px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    color: #E4E4E7;
}
.kpi-delta-up {
    color: #EF4444 !important;
}
.kpi-delta-down {
    color: #22C55E !important;
}
/* 按钮 */
.stButton > button {
    background: #1C1C20 !important;
    color: #E4E4E7 !important;
    border: 1px solid #27272A !important;
    border-radius: 8px !important;
}
.stButton > button[kind="primary"] {
    background: #0EA5E9 !important;
    color: #0A0A0B !important;
    border: none !important;
}
/* 输入框 */
.stTextInput input,
.stNumberInput input,
.stSelectbox select,
.stSlider {
    background: #1C1C20 !important;
    color: #E4E4E7 !important;
    border: 1px solid #27272A !important;
    border-radius: 8px !important;
}
/* 表格 */
[data-testid="stDataFrame"] table {
    background: #141417 !important;
    color: #E4E4E7 !important;
}
[data-testid="stDataFrame"] th {
    background: #1C1C20 !important;
    color: #A1A1AA !important;
}
[data-testid="stDataFrame"] td {
    border-bottom: 1px solid #27272A !important;
}
/* 信息提示 */
.stInfo, .stSuccess, .stWarning, .stError {
    background: #141417 !important;
    border: 1px solid #27272A !important;
    color: #E4E4E7 !important;
    border-radius: 8px !important;
}
/* 折叠面板 */
.streamlit-expanderHeader {
    background: #141417 !important;
    border: 1px solid #27272A !important;
    border-radius: 8px !important;
    color: #E4E4E7 !important;
}
/* 标签页 */
.stTabs [data-baseweb="tab-list"] {
    background: #141417 !important;
    border-radius: 8px !important;
}
.stTabs [data-baseweb="tab"] {
    color: #A1A1AA !important;
}
.stTabs [data-baseweb="tab--selected"] {
    color: #0EA5E9 !important;
    border-bottom-color: #0EA5E9 !important;
}
/* metric */
[data-testid="stMetric"] {
    background: #141417 !important;
    border: 1px solid #27272A !important;
    border-radius: 12px !important;
    padding: 12px 16px !important;
}
[data-testid="stMetric"] label {
    color: #71717A !important;
}
[data-testid="stMetricValue"] {
    color: #E4E4E7 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-weight: 700 !important;
}
</style>
"""
st.markdown(_DARK_CSS, unsafe_allow_html=True)



def init_session_state():
    if 'config' not in st.session_state:
        st.session_state.config = Config(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else Config()
    if 'market_data' not in st.session_state:
        st.session_state.market_data = MarketData()
    if 'realtime_data' not in st.session_state:
        st.session_state.realtime_data = RealtimeData()
    if 'data_loader' not in st.session_state:
        st.session_state.data_loader = DataLoader()
    if 'broker' not in st.session_state:
        st.session_state.broker = SimulatedBroker(initial_capital=1000000)
        st.session_state.broker.connect()
    if 'risk_manager' not in st.session_state:
        st.session_state.risk_manager = RiskManager()
    if 'trade_logger' not in st.session_state:
        st.session_state.trade_logger = TradeLogger(log_dir=LOG_DIR)
    if 'news_analyzer' not in st.session_state:
        st.session_state.news_analyzer = NewsAnalyzer()
    if 'last_quotes' not in st.session_state:
        st.session_state.last_quotes = {}
    if 'chart_data' not in st.session_state:
        st.session_state.chart_data = {}
    if 'ai_decisions' not in st.session_state:
        st.session_state.ai_decisions = {}
    if 'ai_graph_bundle' not in st.session_state:
        st.session_state.ai_graph_bundle = None



def normalize_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().lower().replace(".", "")
    if not symbol:
        return ""
    if symbol.startswith(("sh", "sz")):
        return symbol
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def parse_symbols(raw: str):
    return [s for s in (normalize_symbol(x) for x in raw.split(',')) if s]


def format_money(value) -> str:
    try:
        return f"¥{float(value):,.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


def format_pct(value) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "0.00%"


def round_lot(quantity: float) -> int:
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return 0
    return max(0, quantity // 100 * 100)


def action_label(action: str) -> str:
    return {"buy": "买入", "sell": "卖出", "hold": "观望"}.get(action, "观望")


def action_badge(action: str) -> str:
    color = {"buy": "#dc2626", "sell": "#059669", "hold": "#475569"}.get(action, "#475569")
    return f"<span style='color:{color};font-weight:700'>{action_label(action)}</span>"


def get_market_session_label(now=None) -> str:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "非交易日"
    current = now.time()
    if current < datetime.strptime("09:15", "%H:%M").time():
        return "盘前"
    if current < datetime.strptime("09:30", "%H:%M").time():
        return "集合竞价"
    if current <= datetime.strptime("11:30", "%H:%M").time():
        return "交易中"
    if current < datetime.strptime("13:00", "%H:%M").time():
        return "午间休市"
    if current <= datetime.strptime("15:00", "%H:%M").time():
        return "交易中"
    return "已收盘"


def portfolio_snapshot():
    account = st.session_state.broker.get_account_info()
    positions = st.session_state.broker.get_positions()
    total_asset = float(account.get('total_asset', 0) or 0)
    total_position_value = sum(float(getattr(p, 'market_value', 0) or 0) for p in positions)
    pos_map = {p.symbol: p for p in positions}

    rm = st.session_state.risk_manager
    rm.current_equity = total_asset
    if rm.peak_equity <= 0:
        rm.peak_equity = max(total_asset, float(account.get('initial_capital', 0) or 0))

    return {
        'account': account,
        'positions': positions,
        'pos_map': pos_map,
        'total_asset': total_asset,
        'cash': float(account.get('cash', 0) or 0),
        'total_position_value': total_position_value,
        'total_position_pct': total_position_value / max(total_asset, 1),
    }


def run_pre_trade_check(symbol: str, direction: str, quantity: int, price: float, reason: str = ""):
    symbol = normalize_symbol(symbol)
    snap = portfolio_snapshot()
    rm = st.session_state.risk_manager

    if not symbol:
        return {'allowed': False, 'reason': '股票代码不能为空', 'suggested_quantity': 0}
    if price <= 0:
        return {'allowed': False, 'reason': '价格必须大于 0', 'suggested_quantity': 0}
    if quantity <= 0:
        return {'allowed': False, 'reason': '数量必须大于 0', 'suggested_quantity': 0}

    side = OrderSide.BUY if direction == 'buy' else OrderSide.SELL
    position = snap['pos_map'].get(symbol)
    current_qty = int(getattr(position, 'quantity', 0) or 0) if position else 0
    current_symbol_value = float(getattr(position, 'market_value', 0) or 0) if position else 0

    if side == OrderSide.SELL and quantity > current_qty:
        return {
            'allowed': False,
            'reason': f'可卖数量不足，当前持仓 {current_qty} 股',
            'suggested_quantity': current_qty,
        }

    req = OrderRequest(
        symbol=symbol,
        side=side,
        quantity=int(quantity),
        price=float(price),
        portfolio_value=snap['total_asset'],
        current_position_value=snap['total_position_value'],
        reason=reason,
    )
    result = rm.check_order(req)

    if result.allowed and side == OrderSide.BUY:
        estimated_cost = quantity * price * 1.002
        if estimated_cost > snap['cash']:
            suggested = round_lot(snap['cash'] / max(price * 1.002, 1))
            return {
                'allowed': False,
                'reason': f'可用资金不足，预估需要 {format_money(estimated_cost)}',
                'suggested_quantity': suggested,
            }

    return {
        'allowed': result.allowed,
        'reason': result.reason or '风控通过',
        'suggested_quantity': result.suggested_quantity or int(quantity),
        'violations': result.violations,
        'current_qty': current_qty,
        'current_symbol_value': current_symbol_value,
    }


def execute_guarded_order(symbol: str, direction: str, quantity: int, price: float,
                          reason: str = "", source: str = "manual"):
    symbol = normalize_symbol(symbol)
    check = run_pre_trade_check(symbol, direction, quantity, price, reason)
    if not check.get('allowed'):
        return {'ok': False, 'message': check.get('reason', '风控未通过'), 'risk': check}

    order_direction = OrderDirection.BUY if direction == 'buy' else OrderDirection.SELL
    if hasattr(st.session_state.broker, 'update_market_price'):
        st.session_state.broker.update_market_price(symbol, price)

    order = Order(
        symbol=symbol,
        direction=order_direction,
        quantity=int(quantity),
        order_type=OrderType.LIMIT,
        price=float(price),
    )
    order_id = st.session_state.broker.place_order(order)
    status = st.session_state.broker.get_order_status(order_id) if order_id else {}
    if status.get('status') == 'rejected':
        return {'ok': False, 'message': '券商拒单：资金或持仓不足', 'risk': check, 'order_id': order_id}

    st.session_state.risk_manager.record_order({
        'symbol': symbol,
        'direction': direction,
        'quantity': int(quantity),
        'price': float(price),
        'source': source,
    })
    st.session_state.trade_logger.log_trade({
        'symbol': symbol,
        'direction': direction,
        'quantity': int(quantity),
        'price': float(price),
        'amount': float(price) * int(quantity),
        'commission': 0,
        'stamp_tax': 0,
        'source': source,
        'reason': reason[:300],
        'order_id': order_id,
    })
    return {'ok': True, 'message': f'{action_label(direction)}已成交，订单 {order_id}',
            'risk': check, 'order_id': order_id, 'status': status}


def render_order_result(result: dict):
    if result.get('ok'):
        st.success(result.get('message', '交易完成'))
    else:
        st.error(result.get('message', '交易失败'))
        suggested = (result.get('risk') or {}).get('suggested_quantity')
        if suggested:
            st.info(f"建议数量：{suggested} 股")


def get_ai_graph_bundle(force_rebuild: bool = False):
    if st.session_state.ai_graph_bundle is not None and not force_rebuild:
        return st.session_state.ai_graph_bundle

    try:
        from src.ai.llm_client import LLMClient
        from src.ai.agents.orchestrator import TradingAgentsGraph
        from src.ai.agents.memory import ReflectionMemory
        from src.data.fundamentals import FundamentalsFetcher
        from src.data.capital_flow import CapitalFlowFetcher
    except Exception as exc:
        bundle = {'error': f'AI模块加载失败: {exc}', 'graph': None, 'llm_status': {}}
        st.session_state.ai_graph_bundle = bundle
        return bundle

    cfg = st.session_state.config
    ag = cfg.get('agents', {}) or {}
    ai_cfg = cfg.get('ai', {}) or {}
    provider = ag.get('provider') or ai_cfg.get('provider', 'deepseek')
    model = ag.get('deep_think_model') or ai_cfg.get('model')
    api_key_env = ag.get('api_key_env') or ai_cfg.get('api_key_env')
    base_url = ag.get('base_url') or ai_cfg.get('base_url')

    llm = LLMClient(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        temperature=ag.get('temperature', ai_cfg.get('temperature', 0.3)),
    )

    memory = None
    if ag.get('use_memory', True):
        try:
            memory = ReflectionMemory(
                db_path=os.path.join(DATA_DIR, "trading_state.db"),
                llm=llm,
                deep_model=ag.get('deep_think_model'),
            )
        except Exception:
            memory = None

    graph = TradingAgentsGraph(
        llm=llm,
        config={
            'analysts': ag.get('analysts', ['technical', 'sentiment', 'news', 'fundamentals', 'capital']),
            'max_debate_rounds': ag.get('max_debate_rounds', 1),
            'quick_think_model': ag.get('quick_think_model'),
            'deep_think_model': ag.get('deep_think_model'),
            'use_memory': ag.get('use_memory', True),
            'max_drawdown': st.session_state.risk_manager.max_drawdown,
            'max_total_position': st.session_state.risk_manager.max_total_position,
        },
        kline_provider=st.session_state.realtime_data,
        news_analyzer=st.session_state.news_analyzer,
        fundamentals=FundamentalsFetcher(),
        capital_provider=CapitalFlowFetcher(),
        memory=memory,
    )
    bundle = {'graph': graph, 'llm_status': llm.status(), 'error': ''}
    st.session_state.ai_graph_bundle = bundle
    return bundle


def decision_quantity(symbol: str, action: str, price: float, decision: dict) -> int:
    snap = portfolio_snapshot()
    rm = st.session_state.risk_manager
    if action == 'sell':
        pos = snap['pos_map'].get(symbol)
        return int(getattr(pos, 'quantity', 0) or 0) if pos else 0
    if action != 'buy' or price <= 0:
        return 0

    trader = decision.get('trader', {}) or {}
    target_pct = trader.get('target_pct')
    try:
        target_pct = float(target_pct) / 100
    except (TypeError, ValueError):
        target_pct = rm.max_position_size
    target_pct = min(max(target_pct, 0), rm.max_position_size)
    by_target = snap['total_asset'] * target_pct / price
    by_risk = rm.calculate_position_size_with_risk(price, snap['total_asset'], risk_per_trade=0.01)
    by_cash = snap['cash'] / max(price * 1.002, 1)
    return round_lot(min(by_target, by_risk, by_cash))


def fetch_realtime_quotes(symbols):
    cfg = st.session_state.config
    sources = cfg.get('data_source.realtime_order', ['pytdx', 'eastmoney', 'sina', 'tencent'])
    quotes = st.session_state.realtime_data.get_quotes(symbols, sources=sources)
    for symbol, quote in (quotes or {}).items():
        price = quote.get('price')
        if price and hasattr(st.session_state.broker, 'update_market_price'):
            st.session_state.broker.update_market_price(symbol, float(price))
    return quotes or {}


def analyze_with_agents(symbols, quotes, sentiment_override=None):
    bundle = get_ai_graph_bundle()
    graph = bundle.get('graph')
    if graph is None:
        st.error(bundle.get('error') or 'AI模块不可用')
        return {}

    snap = portfolio_snapshot()
    sentiment_override = sentiment_override or {}
    results = {}
    progress = st.progress(0, text="AI 分析中")
    for i, symbol in enumerate(symbols, start=1):
        quote = quotes.get(symbol, {})
        price = quote.get('price', 0)
        position = snap['pos_map'].get(symbol)
        context = {
            'symbol': symbol,
            'price': price,
            'change_pct': quote.get('change_pct', 0),
            'volume': quote.get('volume', 0),
            'amount': quote.get('amount', 0),
            'sentiment': sentiment_override.get(symbol),
            'position': int(getattr(position, 'quantity', 0) or 0) if position else 0,
            'risk': {
                'drawdown': st.session_state.risk_manager.get_risk_report().get('drawdown', 0),
                'total_position_pct': snap['total_position_pct'],
                'daily_pnl': st.session_state.risk_manager.daily_pnl,
            },
            'trade_date': datetime.now().strftime('%Y%m%d'),
        }
        try:
            decision = graph.analyze(symbol, context).to_dict()
        except Exception as exc:
            decision = {
                'symbol': symbol,
                'action': 'hold',
                'confidence': 0,
                'reason': f'AI分析失败: {exc}',
                'analysts': [],
                'debate': {},
                'trader': {},
                'risk': {'approved': False, 'reason': str(exc)},
            }
        qty = decision_quantity(symbol, decision.get('action'), float(price or 0), decision)
        risk = {'allowed': False, 'reason': '无需交易', 'suggested_quantity': 0}
        if decision.get('action') in ('buy', 'sell') and qty > 0 and price:
            risk = run_pre_trade_check(symbol, decision.get('action'), qty, float(price), decision.get('reason', ''))
        decision['suggested_quantity'] = qty
        decision['pre_trade_risk'] = risk
        results[symbol] = decision
        progress.progress(i / max(len(symbols), 1), text=f"AI 分析中：{symbol}")
    progress.empty()
    st.session_state.ai_decisions = results
    return results


def render_ai_decision_detail(symbol: str, decision: dict):
    with st.expander(f"{symbol} 决策链", expanded=False):
        analysts = decision.get('analysts', [])
        if analysts:
            st.markdown("**分析师团队**")
            st.dataframe(pd.DataFrame(analysts), use_container_width=True)
        debate = decision.get('debate') or {}
        if debate:
            st.markdown("**多空辩论**")
            st.write(debate.get('conclusion', ''))
            cols = st.columns(2)
            cols[0].caption("多头")
            cols[0].write(debate.get('bull', ''))
            cols[1].caption("空头")
            cols[1].write(debate.get('bear', ''))
        st.markdown("**交易员与风控**")
        st.json({
            'trader': decision.get('trader', {}),
            'risk_agent': decision.get('risk', {}),
            'hard_risk': decision.get('pre_trade_risk', {}),
        })


def render_kpi_card(label, value, delta=None, delta_label=None, color="#E4E4E7"):
    delta_html = ""
    if delta is not None:
        cls = "kpi-delta-up" if delta >= 0 else "kpi-delta-down"
        delta_html = f"<div style='font-size:12px;margin-top:4px' class='{cls}'>{delta:+.2f}{delta_label or ''}</div>"
    st.markdown(
        f"<div class='kpi-card'>"
        f"<div class='kpi-label'>{label}</div>"
        f"<div class='kpi-value' style='color:{color}'>{value}</div>"
        f"{delta_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_quote_card(symbol, q):
    name = q.get('name', symbol)
    price = float(q.get('price') or 0)
    change_pct = float(q.get('change_pct') or 0)
    color = "#EF4444" if change_pct >= 0 else "#22C55E"
    arrow = "▲" if change_pct >= 0 else "▼"
    st.markdown(
        f"<div class='kpi-card' style='padding:12px 14px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='font-weight:600;color:#E4E4E7'>{name}</span>"
        f"<span style='font-size:11px;color:#71717A'>{symbol}</span>"
        f"</div>"
        f"<div style='display:flex;justify-content:space-between;align-items:baseline;margin-top:8px'>"
        f"<span style='font-size:20px;font-weight:700;font-family:JetBrains Mono,monospace;color:{color}'>{price:.2f}</span>"
        f"<span style='font-size:13px;font-weight:600;color:{color}'>{arrow} {abs(change_pct):.2f}%</span>"
        f"</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_ai_cockpit():
    st.title("AI 量化交易驾驶舱")

    cfg = st.session_state.config
    broker_type = cfg.get('broker.type', 'simulated')
    auto_trade = bool(cfg.get('trading.auto_trade', False))
    account = st.session_state.broker.get_account_info()
    bundle = get_ai_graph_bundle()
    llm_status = bundle.get('llm_status', {})
    snap = portfolio_snapshot()
    risk_report = st.session_state.risk_manager.get_risk_report()

    total_asset = float(account.get('total_asset', 0) or 0)
    initial_capital = float(account.get('initial_capital', 1000000) or 1000000)
    total_pnl = total_asset - initial_capital
    pnl_pct = total_pnl / max(initial_capital, 1)

    # KPI 行
    kpi_cols = st.columns(4)
    with kpi_cols[0]:
        render_kpi_card("总资产", format_money(total_asset))
    with kpi_cols[1]:
        render_kpi_card(
            "今日盈亏",
            format_money(total_pnl),
            delta=pnl_pct * 100,
            delta_label="%",
            color="#EF4444" if total_pnl >= 0 else "#22C55E",
        )
    with kpi_cols[2]:
        win_rate = 0.0
        trades = st.session_state.trade_logger.get_trades()
        if not trades.empty and 'pnl' in trades.columns:
            wins = (trades['pnl'] > 0).sum()
            total = len(trades)
            win_rate = wins / total if total > 0 else 0.0
        render_kpi_card("胜率", f"{win_rate:.1%}")
    with kpi_cols[3]:
        active_strategies = len(cfg.get('trading.symbols', []))
        render_kpi_card("活跃策略", str(active_strategies), color="#0EA5E9")

    # 状态栏
    status_cols = st.columns(5)
    status_cols[0].metric("市场", get_market_session_label())
    status_cols[1].metric("交易模式", "模拟盘" if broker_type == 'simulated' else str(broker_type).upper())
    status_cols[2].metric("自动交易", "关闭" if not auto_trade else "开启")
    status_cols[3].metric("AI", "在线" if llm_status.get('available') else "规则兜底")
    status_cols[4].metric("当前回撤", format_pct(risk_report.get('drawdown', 0)))

    if broker_type != 'simulated':
        st.warning("当前配置不是 simulated。此面板仍默认使用模拟券商，实盘建议通过 engine.py + QMT 风控链路执行。")
    if auto_trade:
        st.warning("配置中 auto_trade=true。界面内仍需单独勾选模拟执行，避免误触实盘。")

    with st.sidebar:
        if st.button("重载AI", use_container_width=True):
            st.session_state.ai_graph_bundle = None
            get_ai_graph_bundle(force_rebuild=True)
            st.success("AI 已重载")

    default_symbols = ",".join(cfg.get('trading.symbols', ['sh600000', 'sz000001', 'sh600104', 'sz000002']))
    control_cols = st.columns([3, 1, 1, 1])
    with control_cols[0]:
        raw_symbols = st.text_input("股票池", default_symbols)
    with control_cols[1]:
        max_analyze = st.number_input("分析数量", min_value=1, max_value=20, value=4, step=1)
    with control_cols[2]:
        simulate_execute = st.checkbox("通过后模拟执行", value=False)
    with control_cols[3]:
        show_chart = st.checkbox("显示趋势图", value=True)

    symbols = parse_symbols(raw_symbols)[:int(max_analyze)]
    if not symbols:
        st.info("请输入股票代码")
        return

    quote_cols = st.columns([1, 1, 4])
    refresh_quotes = quote_cols[0].button("刷新行情", type="secondary", use_container_width=True)
    run_ai = quote_cols[1].button("AI 分析", type="primary", use_container_width=True)

    if refresh_quotes or run_ai or 'cockpit_quotes' not in st.session_state:
        with st.spinner("获取实时行情..."):
            st.session_state.cockpit_quotes = fetch_realtime_quotes(symbols)

    quotes = st.session_state.get('cockpit_quotes', {})

    # 行情卡片
    if quotes:
        st.subheader("实时行情")
        card_cols = st.columns(min(len(symbols), 4))
        for i, symbol in enumerate(list(quotes.keys())[:4]):
            with card_cols[i]:
                render_quote_card(symbol, quotes.get(symbol, {}))

    # 行情表格
    quote_rows = []
    for symbol in symbols:
        q = quotes.get(symbol, {})
        quote_rows.append({
            '代码': symbol,
            '名称': q.get('name', ''),
            '最新价': q.get('price', 0),
            '涨跌幅%': q.get('change_pct', 0),
            '成交额': q.get('amount', 0),
            '换手率%': q.get('turnover', 0),
            '市盈率': q.get('pe_ratio', 0),
        })
    quote_df = pd.DataFrame(quote_rows)
    st.dataframe(quote_df, use_container_width=True, hide_index=True)

    # 趋势图（默认显示第一只股票的K线）
    if show_chart and symbols:
        with st.spinner("加载K线数据..."):
            chart_symbol = symbols[0]
            kline_data = st.session_state.realtime_data.get_kline_data(chart_symbol, period='day', count=60)
            if not kline_data.empty:
                fig = make_subplots(
                    rows=2, cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.05,
                    row_heights=[0.7, 0.3]
                )
                fig.add_trace(go.Candlestick(
                    x=kline_data.index,
                    open=kline_data['open'], high=kline_data['high'],
                    low=kline_data['low'], close=kline_data['close'],
                    name='K线'
                ), row=1, col=1)
                ma20 = kline_data['close'].rolling(20).mean()
                ma60 = kline_data['close'].rolling(60).mean()
                fig.add_trace(go.Scatter(x=kline_data.index, y=ma20, mode='lines',
                                         name='MA20', line=dict(color='orange', width=1)), row=1, col=1)
                fig.add_trace(go.Scatter(x=kline_data.index, y=ma60, mode='lines',
                                         name='MA60', line=dict(color='purple', width=1)), row=1, col=1)
                fig.add_trace(go.Bar(x=kline_data.index, y=kline_data['volume'],
                                     name='成交量', marker_color='rgba(14,165,233,0.5)'), row=2, col=1)
                fig.update_layout(
                    title=f'{chart_symbol} K线图',
                    xaxis_title='日期',
                    yaxis_title='价格',
                    hovermode='x unified',
                    height=500,
                    paper_bgcolor='#0A0A0B',
                    plot_bgcolor='#141417',
                    font=dict(color='#E4E4E7'),
                    legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1)
                )
                fig.update_xaxes(rangeslider_visible=False, gridcolor='#27272A')
                fig.update_yaxes(gridcolor='#27272A')
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无K线数据，请尝试其他股票代码")

    sentiment_map = {}
    if st.toggle("读取新闻情感", value=False):
        with st.spinner("聚合新闻情感..."):
            for symbol in symbols:
                try:
                    items = st.session_state.news_analyzer.get_symbol_news(symbol, count=8)
                    scores = [x.get('sentiment', {}).get('score') for x in items if isinstance(x, dict)]
                    scores = [float(s) for s in scores if isinstance(s, (int, float))]
                    sentiment_map[symbol] = sum(scores) / len(scores) if scores else None
                except Exception:
                    sentiment_map[symbol] = None

    if run_ai:
        decisions = analyze_with_agents(symbols, quotes, sentiment_override=sentiment_map)
        if simulate_execute:
            for symbol, decision in decisions.items():
                action = decision.get('action')
                qty = int(decision.get('suggested_quantity') or 0)
                price = float((quotes.get(symbol) or {}).get('price') or 0)
                risk = decision.get('pre_trade_risk') or {}
                if action in ('buy', 'sell') and qty > 0 and risk.get('allowed'):
                    result = execute_guarded_order(symbol, action, qty, price,
                                                   reason=decision.get('reason', ''),
                                                   source='ai_cockpit')
                    render_order_result(result)

    decisions = st.session_state.get('ai_decisions', {})
    if decisions:
        st.subheader("AI 决策")
        rows = []
        for symbol, decision in decisions.items():
            risk = decision.get('pre_trade_risk') or {}
            rows.append({
                '代码': symbol,
                '动作': action_label(decision.get('action')),
                '置信度': decision.get('confidence', 0),
                '建议数量': decision.get('suggested_quantity', 0),
                '硬风控': '通过' if risk.get('allowed') else risk.get('reason', '无需交易'),
                '理由': decision.get('reason', ''),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        for symbol, decision in decisions.items():
            quote = quotes.get(symbol, {})
            price = float(quote.get('price') or 0)
            action = decision.get('action')
            qty = int(decision.get('suggested_quantity') or 0)
            risk = decision.get('pre_trade_risk') or {}
            cols = st.columns([1, 1, 1, 3])
            cols[0].markdown(action_badge(action), unsafe_allow_html=True)
            cols[1].metric(symbol, format_money(price))
            cols[2].metric("置信度", format_pct(decision.get('confidence', 0)))
            if action in ('buy', 'sell') and qty > 0:
                disabled = not risk.get('allowed')
                if cols[3].button(f"模拟执行 {action_label(action)} {qty} 股", key=f"exec_{symbol}_{action}", disabled=disabled):
                    result = execute_guarded_order(symbol, action, qty, price,
                                                   reason=decision.get('reason', ''),
                                                   source='ai_cockpit_manual')
                    render_order_result(result)
            else:
                cols[3].info("当前建议观望")
            render_ai_decision_detail(symbol, decision)

    st.subheader("组合与风险")
    risk_cols = st.columns(4)
    risk_cols[0].metric("可用资金", format_money(snap['cash']))
    risk_cols[1].metric("总仓位", format_pct(snap['total_position_pct']))
    risk_cols[2].metric("当前回撤", format_pct(risk_report.get('drawdown', 0)))
    risk_cols[3].metric("今日下单", risk_report.get('daily_order_count', 0))

    positions = st.session_state.broker.get_positions()
    if positions:
        pos_df = pd.DataFrame([p.to_dict() for p in positions])
        st.dataframe(pos_df, use_container_width=True, hide_index=True)
    else:
        st.info("当前无持仓")



init_session_state()

st.sidebar.title("量化交易平台")
st.sidebar.markdown("---")

auto_refresh = st.sidebar.checkbox("自动刷新", value=False)
if auto_refresh:
    refresh_secs = st.sidebar.slider("刷新间隔(秒)", 2, 60, 5)
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=refresh_secs * 1000, key="auto-refresh")
    except ImportError:
        st.sidebar.warning("未安装 streamlit-autorefresh, 实时刷新不可用")

page = st.sidebar.radio(
    "导航",
    ["AI交易驾驶舱", "实时行情", "策略回测", "参数优化", "投资组合", "新闻情感", "风险监控", "系统状态"]
)

account_info = st.session_state.broker.get_account_info()
st.sidebar.markdown("---")
st.sidebar.subheader("账户概览")
st.sidebar.metric("总资产", f"¥{account_info.get('total_asset', 0):,.2f}")
st.sidebar.metric("可用资金", f"¥{account_info.get('cash', 0):,.2f}")
st.sidebar.metric("盈亏", f"¥{account_info.get('profit', 0):,.2f}",
                   delta=f"{account_info.get('profit_pct', 0):.2f}%")


if page == "AI交易驾驶舱":
    render_ai_cockpit()

elif page == "实时行情":
    st.title("实时行情监控")

    col1, col2 = st.columns([3, 1])
    with col2:
        default_symbols = "sh600000, sz000001, sh600104, sz000002"
        symbols_input = st.text_input("股票代码（逗号分隔）", default_symbols)
        interval = st.slider("刷新间隔(秒)", 1, 10, 3)
        refresh = st.button("刷新行情")

    symbols = [s.strip() for s in symbols_input.split(',') if s.strip()]
    symbols = [normalize_symbol(s) for s in symbols]

    quote_placeholder = st.empty()
    chart_placeholder = st.empty()

    if refresh or True:
        with st.spinner("获取行情中..."):
            # 优先腾讯/新浪，东财回退，避免东财限流导致长时间无数据
            quotes = st.session_state.realtime_data.get_quotes(
                symbols, sources=['tencent', 'sina', 'eastmoney']
            )
            if not quotes:
                quotes = st.session_state.realtime_data.get_quotes(
                    symbols, sources=['sina', 'eastmoney']
                )

            if quotes:
                st.session_state.last_quotes = quotes
                df_quotes = pd.DataFrame([
                    {
                        '代码': s,
                        '名称': q.get('name', ''),
                        '最新价': q.get('price', 0),
                        '涨跌幅(%)': q.get('change_pct', 0),
                        '涨跌额': q.get('change', 0),
                        '最高': q.get('high', 0),
                        '最低': q.get('low', 0),
                        '成交量': q.get('volume', 0),
                        '成交额': q.get('amount', 0),
                        '昨收': q.get('pre_close', 0),
                        '今开': q.get('open', 0),
                    }
                    for s, q in quotes.items()
                ])

                quote_placeholder.dataframe(
                    df_quotes.style.format({
                        '最新价': '¥{:.2f}',
                        '涨跌幅(%)': '{:.2f}%',
                        '涨跌额': '{:.3f}',
                        '最高': '¥{:.2f}',
                        '最低': '¥{:.2f}',
                        '成交量': '{:.0f}',
                        '成交额': '¥{:.2f}',
                        '昨收': '¥{:.2f}',
                        '今开': '¥{:.2f}',
                    }).map(
                        lambda x: 'color: #EF4444; font-weight: 600'
                        if isinstance(x, (int, float)) and x > 0
                        else ('color: #22C55E; font-weight: 600'
                              if isinstance(x, (int, float)) and x < 0 else ''),
                        subset=['涨跌幅(%)', '涨跌额']
                    ),
                    use_container_width=True
                )
            else:
                quote_placeholder.warning(
                    "未获取到行情数据，可能为非交易时间或网络受限。已尝试腾讯/新浪/东财多个数据源。"
                )

    st.subheader("K线图")
    chart_symbol = st.selectbox("选择股票", symbols if symbols else ['sh600000'])
    kline_period = st.selectbox("周期", ["day", "week", "month", "60min", "30min"], index=0)
    kline_count = st.slider("数据条数", 20, 200, 60)

    with st.spinner("加载K线数据..."):
        kline_data = st.session_state.realtime_data.get_kline_data(
            chart_symbol, period=kline_period, count=kline_count
        )
        if kline_data.empty:
            # 回退到 mootdx(TCP)
            cat_map = {'day': 4, 'week': 5, 'month': 6, '60min': 11, '30min': 10}
            kline_data = st.session_state.realtime_data.get_kline_mootdx(
                chart_symbol, category=cat_map.get(kline_period, 4), offset=kline_count
            )

        if not kline_data.empty:
            fig = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.05,
                row_heights=[0.7, 0.3]
            )

            fig.add_trace(go.Candlestick(
                x=kline_data.index,
                open=kline_data['open'], high=kline_data['high'],
                low=kline_data['low'], close=kline_data['close'],
                name='K线'
            ), row=1, col=1)

            ma20 = kline_data['close'].rolling(20).mean()
            ma60 = kline_data['close'].rolling(60).mean()
            fig.add_trace(go.Scatter(x=kline_data.index, y=ma20, mode='lines',
                                     name='MA20', line=dict(color='orange', width=1)), row=1, col=1)
            fig.add_trace(go.Scatter(x=kline_data.index, y=ma60, mode='lines',
                                     name='MA60', line=dict(color='purple', width=1)), row=1, col=1)

            fig.add_trace(go.Bar(x=kline_data.index, y=kline_data['volume'],
                                 name='成交量', marker_color='rgba(14,165,233,0.5)'), row=2, col=1)

            fig.update_layout(
                title=f'{chart_symbol} K线图',
                xaxis_title='日期',
                yaxis_title='价格',
                hovermode='x unified',
                height=600,
                paper_bgcolor='#0A0A0B',
                plot_bgcolor='#141417',
                font=dict(color='#E4E4E7'),
                legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1)
            )
            fig.update_xaxes(rangeslider_visible=False, gridcolor='#27272A')
            fig.update_yaxes(gridcolor='#27272A')
            chart_placeholder.plotly_chart(fig, use_container_width=True)
        else:
            st.info("暂无K线数据，请尝试其他股票代码或检查网络连接")


elif page == "策略回测":
    st.title("策略回测")

    strategy_type = st.selectbox(
        "选择策略",
        ["动量策略 (MomentumStrategy)", "均值回归策略 (MeanReversionStrategy)",
         "ML机器学习策略 (MLStrategy)"]
    )

    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input("股票代码", "AAPL")
        period = st.selectbox("数据周期", ["1y", "2y", "5y", "6mo", "3mo"], index=0)
    with col2:
        initial_capital = st.number_input("初始资金", 10000, 10000000, 100000, step=10000)
        commission = st.number_input("佣金率", 0.0, 0.01, 0.001, step=0.001, format="%.3f")
        slippage = st.number_input("滑点", 0.0, 0.01, 0.001, step=0.001, format="%.3f")

    st.subheader("策略参数")

    if "动量" in strategy_type:
        lookback = st.slider("回看周期", 5, 60, 20)
        threshold = st.slider("动量阈值", 0.01, 0.20, 0.03, step=0.01)

        from src.strategies.momentum_strategy import MomentumStrategy
        strategy = MomentumStrategy(lookback_period=lookback, threshold=threshold)
    elif "ML" in strategy_type:
        st.info("ML 策略基于 sklearn.RandomForest / GradientBoosting,首次运行需安装 scikit-learn")
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            lookback = st.slider("特征回看", 5, 120, 60)
        with col_b:
            train_window = st.slider("训练窗口", 100, 1000, 300)
        with col_c:
            conf = st.slider("置信度阈值", 0.30, 0.80, 0.55, step=0.05)
        from src.strategies.ml_strategy import MLStrategy
        try:
            strategy = MLStrategy(
                symbols=[symbol],
                lookback_period=lookback,
                train_window=train_window,
                prediction_horizon=5,
                confidence_threshold=conf,
            )
        except Exception as e:
            st.error(f"ML 策略初始化失败: {e}")
            st.stop()
    else:
        lookback = st.slider("回看周期", 5, 60, 20)
        entry_threshold = st.slider("入场阈值(标准差)", 1.0, 3.0, 2.0, step=0.1)
        exit_threshold = st.slider("出场阈值(标准差)", 0.0, 2.0, 0.5, step=0.1)

        from src.strategies.mean_reversion_strategy import MeanReversionStrategy
        strategy = MeanReversionStrategy(
            lookback_period=lookback,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold
        )

    bt = Backtester(
        initial_capital=initial_capital,
        commission=commission,
        slippage=slippage
    )

    if st.button("运行回测", type="primary"):
        with st.spinner("获取数据并运行回测..."):
            if symbol.upper() == symbol and not any(s in symbol for s in ['sh', 'sz']):
                source = 'yfinance'
            else:
                source = 'akshare'

            data = st.session_state.market_data.get_stock_data(
                symbol, period=period, source=source
            )

            if data.empty:
                st.error(f"无法获取 {symbol} 的数据，请检查股票代码")
            elif len(data) < 50:
                st.warning(f"数据不足 ({len(data)} 条)，回测结果可能不可靠")
                results = bt.run_backtest(strategy, data, symbol)
            else:
                results = bt.run_backtest(strategy, data, symbol)

            if results:
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("总收益率", f"{results['total_return']:.2%}")
                col2.metric("年化收益率", f"{results['annualized_return']:.2%}")
                col3.metric("最大回撤", f"{results['max_drawdown']:.2%}")
                col4.metric("夏普比率", f"{results['sharpe_ratio']:.2f}")

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("索提诺比率", f"{results.get('sortino_ratio', 0):.2f}")
                col2.metric("卡玛比率", f"{results.get('calmar_ratio', 0):.2f}")
                col3.metric("胜率", f"{results.get('win_rate', 0):.2%}")
                col4.metric("交易次数", f"{results.get('total_trades', 0)}")

                equity_df = results['equity_curve']
                drawdown_series = results.get('drawdown_series', pd.Series(dtype=float))

                fig = make_subplots(
                    rows=2, cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.08,
                    row_heights=[0.7, 0.3],
                    subplot_titles=('权益曲线', '回撤曲线')
                )

                fig.add_trace(
                    go.Scatter(x=equity_df.index, y=equity_df['total_equity'],
                               mode='lines', name='总权益',
                               line=dict(color='blue', width=2)),
                    row=1, col=1
                )
                fig.add_trace(
                    go.Scatter(x=equity_df.index, y=[initial_capital] * len(equity_df),
                               mode='lines', name='初始资金',
                               line=dict(color='gray', width=1, dash='dash')),
                    row=1, col=1
                )

                if not drawdown_series.empty:
                    fig.add_trace(
                        go.Scatter(x=drawdown_series.index, y=drawdown_series * 100,
                                   mode='lines', name='回撤',
                                   fill='tozeroy', line=dict(color='red', width=1)),
                        row=2, col=1
                    )

                fig.update_layout(height=500, hovermode='x unified')
                fig.update_yaxes(title_text="权益", row=1, col=1)
                fig.update_yaxes(title_text="回撤(%)", row=2, col=1)
                st.plotly_chart(fig, use_container_width=True)

                trades = results.get('trades', [])
                if trades:
                    st.subheader("交易记录")
                    trades_df = pd.DataFrame(trades)
                    st.dataframe(trades_df, use_container_width=True)


elif page == "参数优化":
    st.title("参数优化")

    strategy_type = st.selectbox(
        "策略类型",
        ["动量策略 (MomentumStrategy)", "均值回归策略 (MeanReversionStrategy)"]
    )

    symbol = st.text_input("股票代码", "AAPL")
    period = st.selectbox("数据周期", ["1y", "2y", "3y", "5y"], index=0)
    metric = st.selectbox("优化目标", ["sharpe_ratio", "total_return", "calmar_ratio", "profit_factor"])

    st.subheader("参数搜索范围")

    if "动量" in strategy_type:
        col1, col2 = st.columns(2)
        with col1:
            lookback_start = st.number_input("回看周期(起始)", 5, 30, 10)
            lookback_end = st.number_input("回看周期(结束)", 10, 60, 50)
            lookback_step = st.number_input("回看周期(步长)", 1, 20, 5)
        with col2:
            threshold_start = st.number_input("动量阈值(起始)", 0.01, 0.10, 0.02, step=0.01)
            threshold_end = st.number_input("动量阈值(结束)", 0.05, 0.30, 0.10, step=0.01)
            threshold_step = st.number_input("动量阈值(步长)", 0.01, 0.10, 0.02, step=0.01)

        from src.strategies.momentum_strategy import MomentumStrategy
        param_grid = {
            'lookback_period': list(range(int(lookback_start), int(lookback_end) + 1, int(lookback_step))),
            'threshold': [round(x, 2) for x in np.arange(threshold_start, threshold_end + threshold_step, threshold_step)]
        }
    else:
        col1, col2 = st.columns(2)
        with col1:
            lookback_start = st.number_input("回看周期(起始)", 5, 30, 10)
            lookback_end = st.number_input("回看周期(结束)", 10, 60, 50)
            lookback_step = st.number_input("回看周期(步长)", 1, 20, 5)
        with col2:
            entry_start = st.number_input("入场阈值(起始)", 1.0, 2.0, 1.5, step=0.1)
            entry_end = st.number_input("入场阈值(结束)", 2.0, 4.0, 3.0, step=0.1)
            entry_step = st.number_input("入场阈值(步长)", 0.1, 1.0, 0.5, step=0.1)

        from src.strategies.mean_reversion_strategy import MeanReversionStrategy
        param_grid = {
            'lookback_period': list(range(int(lookback_start), int(lookback_end) + 1, int(lookback_step))),
            'entry_threshold': [round(x, 1) for x in np.arange(entry_start, entry_end + entry_step, entry_step)]
        }

    if st.button("开始优化", type="primary"):
        with st.spinner("获取数据并运行参数优化..."):
            data = st.session_state.market_data.get_stock_data(symbol, period=period)

            if data.empty:
                st.error(f"无法获取 {symbol} 的数据")
                st.stop()

            if "动量" in strategy_type:
                opt = StrategyOptimizer(MomentumStrategy, data, symbol, metric=metric)
            else:
                opt = StrategyOptimizer(MeanReversionStrategy, data, symbol, metric=metric)

            results = opt.optimize(param_grid, parallel=False)

            st.subheader(f"优化结果（按{metric}排序）")
            st.dataframe(results, use_container_width=True)

            best_params = results.iloc[0].to_dict()
            st.subheader("最佳参数")
            col1, col2, col3, col4 = st.columns(4)
            i = 0
            for k, v in best_params.items():
                if k in ['total_return', 'annualized_return', 'max_drawdown']:
                    col1.metric(k, f"{v:.2%}" if isinstance(v, float) else v)
                elif k in ['sharpe_ratio', 'sortino_ratio', 'calmar_ratio']:
                    col2.metric(k, f"{v:.4f}")
                elif k in ['win_rate', 'profit_factor']:
                    col3.metric(k, f"{v:.2%}" if k == 'win_rate' else f"{v:.2f}")
                elif k not in ['error']:
                    col4.metric(k, v)


elif page == "投资组合":
    st.title("投资组合管理")

    col1, col2 = st.columns(2)
    with col1:
        symbol_input = st.text_input("添加股票代码", "")
        add_stock = st.button("添加")
        if add_stock and symbol_input:
            st.session_state.setdefault('portfolio_symbols', [])

    col2.metric("总资产", f"¥{account_info.get('total_asset', 0):,.2f}")

    st.subheader("当前持仓")
    positions = st.session_state.broker.get_positions()
    if positions:
        pos_data = []
        for p in positions:
            pos_data.append({
                '代码': p.symbol,
                '数量': p.quantity,
                '成本价': f"¥{p.avg_cost:.2f}",
                '市值': f"¥{p.market_value:,.2f}",
                '盈亏': f"¥{p.unrealized_pnl:,.2f}"
            })
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True)
    else:
        st.info("当前无持仓")

    st.subheader("执行交易")
    trade_symbol = st.text_input("股票代码", "sh600000")
    trade_price = st.number_input("价格", 0.0, 1000.0, 10.0, step=0.01)
    trade_quantity = st.number_input("数量(股)", 100, 1000000, 1000, step=100)

    trade_col1, trade_col2 = st.columns(2)
    with trade_col1:
        if st.button("买入", type="primary"):
            result = execute_guarded_order(
                trade_symbol, 'buy', int(trade_quantity), float(trade_price),
                reason='手动买入', source='portfolio_manual'
            )
            render_order_result(result)

    with trade_col2:
        if st.button("卖出"):
            result = execute_guarded_order(
                trade_symbol, 'sell', int(trade_quantity), float(trade_price),
                reason='手动卖出', source='portfolio_manual'
            )
            render_order_result(result)

    st.subheader("交易历史")
    trades_df = st.session_state.trade_logger.get_trades()
    if not trades_df.empty:
        st.dataframe(trades_df.tail(20), use_container_width=True)
    else:
        st.info("暂无交易记录")


elif page == "新闻情感":
    st.title("新闻情感分析")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("获取最新新闻"):
            with st.spinner("获取新闻中..."):
                news_list = st.session_state.news_analyzer.get_market_news(20)
                if news_list:
                    st.session_state['news_cache'] = news_list
                    st.success(f"获取到 {len(news_list)} 条新闻")
                else:
                    st.warning("暂无新闻数据")

    with col2:
        if st.button("获取专业数据"):
            with st.spinner("获取中..."):
                prof_news = st.session_state.news_analyzer.get_professional_news(20)
                if prof_news:
                    st.session_state['prof_news_cache'] = prof_news
                    st.success(f"获取到 {len(prof_news)} 条专业数据")

    st.subheader("市场情感概览")
    sentiment = st.session_state.news_analyzer.get_analysis_summary()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("缓存新闻数", sentiment.get('cached_news', 0))
    col2.metric("跟踪股票数", sentiment.get('tracked_symbols', 0))

    hot_stocks = sentiment.get('hot_stocks', [])
    if hot_stocks:
        st.subheader("热门股票")
        hot_df = pd.DataFrame(hot_stocks)
        st.dataframe(hot_df, use_container_width=True)

    industry_sentiment = sentiment.get('industry_sentiment', [])
    if industry_sentiment:
        st.subheader("行业情感排名")
        industry_df = pd.DataFrame(industry_sentiment)
        fig = px.bar(industry_df, x='industry', y='avg_score',
                     color='label', title='行业情感分数',
                     color_discrete_map={
                         'positive': 'green', 'negative': 'red', 'neutral': 'gray'
                     })
        st.plotly_chart(fig, use_container_width=True)

    news_cache = st.session_state.get('news_cache', [])
    if news_cache:
        st.subheader("最新新闻")
        for item in news_cache[:10]:
            news = item.get('news', {})
            sentiment_info = item.get('sentiment', {})
            with st.expander(f"{news.get('title', '')[:60]}..."):
                st.write(f"来源: {news.get('source', '')}")
                st.write(f"时间: {news.get('publish_time', '')}")
                st.write(f"情感: {sentiment_info.get('label', 'neutral')} "
                         f"(分数: {sentiment_info.get('score', 0):.3f})")


elif page == "风险监控":
    st.title("风险监控")

    col1, col2, col3, col4 = st.columns(4)
    rm = st.session_state.risk_manager
    col1.metric("最大仓位", f"{rm.max_position_size:.0%}")
    col2.metric("最大回撤", f"{rm.max_drawdown:.0%}")
    col3.metric("止损", f"{rm.stop_loss:.0%}")
    col4.metric("止盈", f"{rm.take_profit:.0%}")

    st.subheader("当前风险状态")
    account_info = st.session_state.broker.get_account_info()
    total_asset = account_info.get('total_asset', 0)
    if hasattr(rm, 'get_risk_report'):
        st.json(rm.get_risk_report())
    else:
        risk_state = {
            "total_asset": total_asset,
            "peak_equity": getattr(rm, 'peak_equity', total_asset),
            "drawdown": (getattr(rm, 'peak_equity', total_asset) - total_asset) /
                        max(getattr(rm, 'peak_equity', total_asset), 1),
            "daily_pnl": getattr(rm, 'daily_pnl', 0),
            "limits": {
                "max_position_size": rm.max_position_size,
                "max_drawdown": rm.max_drawdown,
                "stop_loss": rm.stop_loss,
                "take_profit": rm.take_profit,
            }
        }
        st.json(risk_state)

    st.subheader("手动止盈止损测试")
    col1, col2, col3 = st.columns(3)
    with col1:
        sl_symbol = st.text_input("股票代码", "sh600000", key="sl_symbol")
    with col2:
        sl_entry = st.number_input("入场价", 0.0, 1000.0, 10.0, step=0.01)
    with col3:
        sl_current = st.number_input("当前价", 0.0, 1000.0, 10.0, step=0.01)
    pnl_pct = (sl_current - sl_entry) / sl_entry if sl_entry > 0 else 0
    if pnl_pct <= -rm.stop_loss:
        st.error(f"触发止损! 亏损 {pnl_pct:.2%} 超过 {rm.stop_loss:.0%}")
    elif pnl_pct >= rm.take_profit:
        st.success(f"触发止盈! 盈利 {pnl_pct:.2%} 达到 {rm.take_profit:.0%}")
    else:
        st.info(f"当前盈亏 {pnl_pct:.2%} (止损线 -{rm.stop_loss:.0%}, 止盈线 +{rm.take_profit:.0%})")

    st.subheader("交易历史与盈亏分析")
    try:
        from src.analysis.portfolio import (
            load_trades_from_state, generate_report, format_text_report
        )
        from src.utils.state_manager import StateManager
        sm = StateManager(db_path="data/trading_state.db")
        trades = load_trades_from_state(sm, limit=5000)
        if trades:
            rpt = generate_report(trades, initial_capital=st.session_state.broker.initial_capital)
            st.text(format_text_report(rpt, initial_capital=st.session_state.broker.initial_capital))
            eq = rpt.get("equity_curve")
            if eq is not None and not eq.empty:
                st.line_chart(eq["equity"], height=300)
        else:
            st.info("暂无交易记录可分析")
    except Exception as e:
        st.warning(f"分析模块未就绪: {e}")


elif page == "系统状态":
    st.title("系统状态")

    st.subheader("账户信息")
    info_data = {
        '总资产': f"¥{account_info.get('total_asset', 0):,.2f}",
        '可用资金': f"¥{account_info.get('cash', 0):,.2f}",
        '持仓市值': f"¥{account_info.get('market_value', 0):,.2f}",
        '冻结资金': f"¥{account_info.get('frozen_cash', 0):,.2f}",
        '总盈亏': f"¥{account_info.get('profit', 0):,.2f}",
        '收益率': f"{account_info.get('profit_pct', 0):.2f}%"
    }
    st.json(info_data)

    st.subheader("风控状态")
    risk_info = {
        '最大仓位比例': st.session_state.risk_manager.max_position_size,
        '最大回撤限制': st.session_state.risk_manager.max_drawdown,
        '止损比例': st.session_state.risk_manager.stop_loss,
        '止盈比例': st.session_state.risk_manager.take_profit
    }
    st.json(risk_info)

    st.subheader("交易日志")
    log_files = [f for f in os.listdir(LOG_DIR) if os.path.isfile(os.path.join(LOG_DIR, f))] if os.path.exists(LOG_DIR) else []
    if log_files:
        st.write("日志文件:")
        for f in log_files[-10:]:
            st.text(f"  {f}")
    else:
        st.info("暂无日志文件")
