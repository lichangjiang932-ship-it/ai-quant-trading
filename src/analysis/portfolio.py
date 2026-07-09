"""
投资组合分析模块
================

提供:
  - 风险调整收益指标: Sharpe, Sortino, Calmar, Information Ratio
  - 风险指标: 最大回撤, 回撤持续期, 年化波动率, 偏度, 峰度
  - 交易统计: 胜率, 盈亏比, 利润因子, 期望值, 最大单笔盈亏
  - 业绩归因: 按月/按周/按策略/按标的分解
  - 相关性: 持仓股收益率相关性矩阵
  - 报告生成: 文本报告 + 字典报告

数据源:
  - 外部传入 trade list / equity curve
  - StateManager (自动加载)
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Iterable

import numpy as np
import pandas as pd


TRADING_DAYS_PER_YEAR = 252


@dataclass
class TradeRecord:
    """统一交易记录"""
    symbol: str
    direction: str
    quantity: int
    price: float
    timestamp: Optional[datetime] = None
    amount: float = 0
    commission: float = 0
    stamp_tax: float = 0
    pnl: float = 0
    strategy: str = ""

    @classmethod
    def from_dict(cls, d: Dict) -> "TradeRecord":
        ts = d.get("timestamp") or d.get("created_at")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except Exception:
                ts = None
        pnl = float(d.get("pnl", 0) or 0)
        return cls(
            symbol=d.get("symbol", ""),
            direction=d.get("direction", ""),
            quantity=int(d.get("quantity", 0) or 0),
            price=float(d.get("price", 0) or 0),
            timestamp=ts,
            amount=float(d.get("amount", 0) or 0),
            commission=float(d.get("commission", 0) or 0),
            stamp_tax=float(d.get("stamp_tax", 0) or 0),
            pnl=pnl,
            strategy=d.get("strategy", ""),
        )


def _safe_float(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


@dataclass
class PerformanceMetrics:
    """核心业绩指标"""
    total_return: float = 0.0
    annualized_return: float = 0.0
    volatility: float = 0.0
    downside_volatility: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    information_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration_days: int = 0
    current_drawdown: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0
    n_periods: int = 0
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class TradeStatistics:
    """交易统计"""
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    payoff_ratio: float = 0.0
    expectancy: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    max_single_win: float = 0.0
    max_single_loss: float = 0.0
    total_commission: float = 0.0
    total_stamp_tax: float = 0.0
    avg_holding_period_hours: float = 0.0
    symbols_traded: int = 0
    best_symbol: str = ""
    worst_symbol: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def compute_metrics_from_returns(
    returns: Iterable[float],
    equity_values: Optional[Iterable[float]] = None,
    risk_free_rate: float = 0.025,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> PerformanceMetrics:
    """从日收益序列计算风险调整收益指标"""
    r = np.asarray(list(returns), dtype=float)
    r = r[~np.isnan(r)]
    m = PerformanceMetrics()
    m.n_periods = len(r)

    if equity_values is None:
        eq = np.cumprod(1.0 + r) if len(r) > 0 else np.array([])
    else:
        eq = np.asarray(list(equity_values), dtype=float)

    if len(r) == 0 and len(eq) == 0:
        return m

    if len(r) > 0:
        rf_per_period = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
        m.total_return = float(np.prod(1.0 + r) - 1.0)
        years = len(r) / periods_per_year
        m.annualized_return = float((1.0 + m.total_return) ** (1.0 / max(years, 1e-9)) - 1.0) if years > 0 else 0.0
        m.volatility = float(np.std(r, ddof=1) * np.sqrt(periods_per_year)) if len(r) > 1 else 0.0
        downside = r[r < 0]
        m.downside_volatility = float(np.std(downside, ddof=1) * np.sqrt(periods_per_year)) if len(downside) > 1 else 0.0
        mean = float(np.mean(r))
        m.sharpe_ratio = float(mean / (np.std(r, ddof=1) / np.sqrt(periods_per_year))) if len(r) > 1 and np.std(r) > 0 else 0.0
        m.sortino_ratio = float((mean - rf_per_period) / (m.downside_volatility / np.sqrt(periods_per_year))) if m.downside_volatility > 0 else 0.0
        m.var_95 = float(np.percentile(r, 5))
        m.cvar_95 = float(np.mean(r[r <= m.var_95])) if (r <= m.var_95).any() else 0.0
        if len(r) > 2:
            m.skewness = float(pd.Series(r).skew())
            m.kurtosis = float(pd.Series(r).kurt())

    if len(eq) > 0:
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        m.max_drawdown = float(-np.min(dd)) if len(dd) > 0 else 0.0
        m.current_drawdown = float(-dd[-1]) if len(dd) > 0 else 0.0
        trough_idx = int(np.argmin(dd))
        peak_idx = int(np.argmax(eq[:trough_idx + 1])) if trough_idx > 0 else 0
        m.max_drawdown_duration_days = max(0, trough_idx - peak_idx)
        m.calmar_ratio = float(m.annualized_return / m.max_drawdown) if m.max_drawdown > 0 else 0.0
    return m


def build_equity_curve_from_trades(
    trades: List[TradeRecord],
    initial_capital: float = 1_000_000,
) -> pd.DataFrame:
    """从交易列表重建权益曲线 (按时间排序)"""
    if not trades:
        return pd.DataFrame(columns=["timestamp", "equity", "cash", "position_value"])

    df = pd.DataFrame([asdict(t) for t in trades])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    cash = initial_capital
    positions: Dict[str, Tuple[int, float]] = {}
    rows = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        qty = int(r["quantity"])
        price = float(r["price"])
        commission = float(r["commission"])
        stamp_tax = float(r["stamp_tax"])
        if r["direction"] == "buy":
            cost = qty * price + commission + stamp_tax
            cash -= cost
            if sym in positions and positions[sym][0] > 0:
                prev_qty, prev_avg = positions[sym]
                total = prev_qty + qty
                avg = (prev_qty * prev_avg + qty * price) / total
                positions[sym] = (total, avg)
            else:
                positions[sym] = (qty, price)
        elif r["direction"] == "sell":
            proceeds = qty * price - commission - stamp_tax
            cash += proceeds
            prev_qty, prev_avg = positions.get(sym, (0, 0))
            realized = (price - prev_avg) * qty
            if prev_qty - qty > 0:
                positions[sym] = (prev_qty - qty, prev_avg)
            else:
                positions.pop(sym, None)
        position_value = sum(q * p for q, p in positions.values())
        rows.append({
            "timestamp": r["timestamp"],
            "equity": cash + position_value,
            "cash": cash,
            "position_value": position_value,
            "realized_pnl": realized if r["direction"] == "sell" else 0.0,
        })
    eq = pd.DataFrame(rows).set_index("timestamp")
    return eq


def compute_trade_statistics(trades: List[TradeRecord]) -> TradeStatistics:
    """基于已实现盈亏计算交易统计"""
    s = TradeStatistics()
    s.total_trades = len(trades)
    s.buy_trades = sum(1 for t in trades if t.direction == "buy")
    s.sell_trades = sum(1 for t in trades if t.direction == "sell")
    pnl_per_trade = [t.pnl for t in trades if t.direction == "sell"]
    pnl_arr = np.asarray(pnl_per_trade, dtype=float) if pnl_per_trade else np.array([0.0])
    s.total_commission = sum(t.commission for t in trades)
    s.total_stamp_tax = sum(t.stamp_tax for t in trades)
    if len(pnl_per_trade) == 0:
        return s
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr < 0]
    s.winning_trades = int(len(wins))
    s.losing_trades = int(len(losses))
    s.win_rate = float(s.winning_trades / len(pnl_arr)) if len(pnl_arr) else 0.0
    s.avg_win = float(np.mean(wins)) if len(wins) else 0.0
    s.avg_loss = float(np.mean(losses)) if len(losses) else 0.0
    s.max_single_win = float(np.max(pnl_arr)) if len(pnl_arr) else 0.0
    s.max_single_loss = float(np.min(pnl_arr)) if len(pnl_arr) else 0.0
    total_win = float(np.sum(wins)) if len(wins) else 0.0
    total_loss = float(abs(np.sum(losses))) if len(losses) else 0.0
    s.profit_factor = float(total_win / total_loss) if total_loss > 0 else float("inf") if total_win > 0 else 0.0
    s.payoff_ratio = float(s.avg_win / abs(s.avg_loss)) if s.avg_loss < 0 else 0.0
    s.expectancy = float(np.mean(pnl_arr)) if len(pnl_arr) else 0.0
    consec_w = consec_l = max_w = max_l = 0
    for p in pnl_per_trade:
        if p > 0:
            consec_w += 1
            consec_l = 0
            max_w = max(max_w, consec_w)
        else:
            consec_l += 1
            consec_w = 0
            max_l = max(max_l, consec_l)
    s.max_consecutive_wins = max_w
    s.max_consecutive_losses = max_l
    sym_pnl: Dict[str, float] = defaultdict(float)
    sym_count: Dict[str, int] = defaultdict(int)
    for t in trades:
        if t.direction == "sell":
            sym_pnl[t.symbol] += t.pnl
            sym_count[t.symbol] += 1
    s.symbols_traded = len(sym_count)
    if sym_pnl:
        s.best_symbol = max(sym_pnl, key=lambda k: sym_pnl[k])
        s.worst_symbol = min(sym_pnl, key=lambda k: sym_pnl[k])
    return s


def monthly_returns_table(equity_curve: pd.DataFrame) -> pd.DataFrame:
    """生成月度收益热力表 (年 x 月)"""
    if equity_curve.empty:
        return pd.DataFrame()
    monthly = equity_curve["equity"].resample("ME").last()
    if len(monthly) < 2:
        return pd.DataFrame()
    pct = monthly.pct_change().dropna()
    table = pd.DataFrame({
        "year": pct.index.year,
        "month": pct.index.month,
        "return": pct.values,
    })
    return table.pivot_table(index="year", columns="month", values="return", aggfunc="sum").fillna(0.0)


def by_symbol_breakdown(trades: List[TradeRecord]) -> pd.DataFrame:
    """按标的的盈亏/笔数分解"""
    rows = []
    by_sym: Dict[str, List[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)
    for sym, ts in by_sym.items():
        pnl_total = sum(t.pnl for t in ts if t.direction == "sell")
        buys = sum(t.amount for t in ts if t.direction == "buy")
        sells = sum(t.amount for t in ts if t.direction == "sell")
        rows.append({
            "symbol": sym,
            "trades": len(ts),
            "buys_amount": round(buys, 2),
            "sells_amount": round(sells, 2),
            "net_pnl": round(pnl_total, 2),
            "return_pct": round(pnl_total / buys * 100, 2) if buys > 0 else 0.0,
        })
    return pd.DataFrame(rows).sort_values("net_pnl", ascending=False) if rows else pd.DataFrame()


def by_strategy_breakdown(trades: List[TradeRecord]) -> pd.DataFrame:
    """按策略的盈亏分解"""
    rows = []
    by_strat: Dict[str, List[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_strat[t.strategy or "default"].append(t)
    for strat, ts in by_strat.items():
        pnl = sum(t.pnl for t in ts if t.direction == "sell")
        rows.append({
            "strategy": strat,
            "trades": len(ts),
            "net_pnl": round(pnl, 2),
            "pnl_per_trade": round(pnl / len(ts), 2) if ts else 0.0,
        })
    return pd.DataFrame(rows).sort_values("net_pnl", ascending=False) if rows else pd.DataFrame()


def generate_report(
    trades: List[TradeRecord],
    initial_capital: float = 1_000_000,
    risk_free_rate: float = 0.025,
) -> Dict:
    """生成完整组合分析报告"""
    eq = build_equity_curve_from_trades(trades, initial_capital)
    if not eq.empty:
        returns = eq["equity"].pct_change().dropna().tolist()
        equity_values = eq["equity"].tolist()
    else:
        returns = []
        equity_values = []
    metrics = compute_metrics_from_returns(returns, equity_values, risk_free_rate=risk_free_rate)
    if not eq.empty:
        metrics.start_date = str(eq.index[0])
        metrics.end_date = str(eq.index[-1])
    stats = compute_trade_statistics(trades)
    return {
        "metrics": metrics.to_dict(),
        "trade_statistics": stats.to_dict(),
        "monthly_returns": monthly_returns_table(eq),
        "by_symbol": by_symbol_breakdown(trades),
        "by_strategy": by_strategy_breakdown(trades),
        "equity_curve": eq,
    }


def format_text_report(report: Dict, initial_capital: float = 1_000_000) -> str:
    """生成可读文本报告"""
    m = report["metrics"]
    s = report["trade_statistics"]
    out = []
    out.append("=" * 60)
    out.append("  投资组合业绩报告")
    out.append("=" * 60)
    period = f"{m.get('start_date', 'N/A')[:10]} ~ {m.get('end_date', 'N/A')[:10]}"
    out.append(f"周期:       {period}  ({m['n_periods']} 个交易日)")
    out.append(f"初始资金:   ¥{initial_capital:,.0f}")
    out.append(f"总收益:     {m['total_return']:.2%}")
    out.append(f"年化收益:   {m['annualized_return']:.2%}")
    out.append(f"年化波动:   {m['volatility']:.2%}")
    out.append(f"下行波动:   {m['downside_volatility']:.2%}")
    out.append(f"夏普比率:   {m['sharpe_ratio']:.3f}")
    out.append(f"索提诺:     {m['sortino_ratio']:.3f}")
    out.append(f"卡玛:       {m['calmar_ratio']:.3f}")
    out.append(f"最大回撤:   {m['max_drawdown']:.2%} (持续 {m['max_drawdown_duration_days']} 天)")
    out.append(f"当前回撤:   {m['current_drawdown']:.2%}")
    out.append(f"VaR 95%:    {m['var_95']:.2%}")
    out.append(f"CVaR 95%:   {m['cvar_95']:.2%}")
    out.append(f"偏度:       {m['skewness']:.3f}")
    out.append(f"峰度:       {m['kurtosis']:.3f}")
    out.append("")
    out.append("-" * 60)
    out.append("  交易统计")
    out.append("-" * 60)
    out.append(f"总笔数:     {s['total_trades']} (买 {s['buy_trades']} / 卖 {s['sell_trades']})")
    out.append(f"胜率:       {s['win_rate']:.2%}")
    out.append(f"盈亏笔数:   {s['winning_trades']} 胜 / {s['losing_trades']} 负")
    out.append(f"平均盈利:   ¥{s['avg_win']:,.2f}")
    out.append(f"平均亏损:   ¥{s['avg_loss']:,.2f}")
    out.append(f"利润因子:   {s['profit_factor']:.2f}")
    out.append(f"盈亏比:     {s['payoff_ratio']:.2f}")
    out.append(f"期望值:     ¥{s['expectancy']:,.2f}")
    out.append(f"最大连盈:   {s['max_consecutive_wins']}")
    out.append(f"最大连亏:   {s['max_consecutive_losses']}")
    out.append(f"最大单笔盈: ¥{s['max_single_win']:,.2f}")
    out.append(f"最大单笔亏: ¥{s['max_single_loss']:,.2f}")
    out.append(f"总佣金:     ¥{s['total_commission']:,.2f}")
    out.append(f"总印花税:   ¥{s['total_stamp_tax']:,.2f}")
    out.append(f"交易标的:   {s['symbols_traded']} 只")
    if s["best_symbol"]:
        out.append(f"最佳标的:   {s['best_symbol']}")
    if s["worst_symbol"]:
        out.append(f"最差标的:   {s['worst_symbol']}")
    out.append("=" * 60)
    return "\n".join(out)


def load_trades_from_state(state_manager, limit: int = 10000) -> List[TradeRecord]:
    """从 StateManager 加载交易记录并转为 TradeRecord"""
    raw = state_manager.get_recent_trades(limit=limit)
    return [TradeRecord.from_dict(r) for r in raw]
