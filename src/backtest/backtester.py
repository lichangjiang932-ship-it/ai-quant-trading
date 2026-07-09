import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from ..strategies.base_strategy import BaseStrategy, Signal
from .portfolio import Portfolio


class Backtester:
    def __init__(
        self,
        initial_capital: float = 100000,
        commission: float = 0.001,
        slippage: float = 0.001,
        risk_free_rate: float = 0.02,
        stamp_tax: float = 0.001,
        min_commission: float = 5.0
    ):
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self.stamp_tax = stamp_tax
        self.min_commission = min_commission
        self.risk_free_rate = risk_free_rate
        self.portfolio = Portfolio(initial_capital)
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
        self._strategy: Optional[BaseStrategy] = None

    def run_backtest(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        symbol: str = "STOCK"
    ) -> Dict:
        self._strategy = strategy
        strategy.reset()
        self.portfolio = Portfolio(self.initial_capital)
        self.trades = []
        self.equity_curve = []

        if data is None or data.empty:
            return {}

        data_with_signals = strategy.generate_signals(data)
        if 'signal' not in data_with_signals.columns:
            return {}

        for date, row in data_with_signals.iterrows():
            raw = row.get('signal', np.nan)
            if pd.isna(raw):
                current_price = row.get('Close', np.nan)
                if not pd.isna(current_price):
                    self._update_equity_curve(date, current_price)
                continue

            try:
                signal = Signal(int(raw))
            except (ValueError, TypeError):
                continue

            current_price = row['Close']
            if pd.isna(current_price) or current_price <= 0:
                continue

            if strategy.should_exit_position(symbol, signal, current_price):
                self._execute_exit(symbol, current_price, date)
            elif strategy.should_enter_position(
                symbol, signal, current_price, self.portfolio.total_value
            ):
                position_size = strategy.calculate_position_size(
                    signal, current_price, self.portfolio.total_value
                )
                self._execute_entry(symbol, position_size, current_price, date)

            self._update_equity_curve(date, current_price)

        return self._calculate_results()

    def run_multiple_symbols(
        self,
        strategy: BaseStrategy,
        data_dict: Dict[str, pd.DataFrame],
        allocation: Optional[Dict[str, float]] = None
    ) -> Dict:
        if allocation is None:
            equal_weight = 1.0 / max(len(data_dict), 1)
            allocation = {s: equal_weight for s in data_dict}

        all_trades = []
        all_equities: Dict[str, pd.DataFrame] = {}
        per_symbol_results: Dict[str, Dict] = {}

        for symbol, data in data_dict.items():
            weight = allocation.get(symbol, 0)
            if weight <= 0:
                continue

            sub_bt = Backtester(
                initial_capital=self.initial_capital * weight,
                commission=self.commission,
                slippage=self.slippage,
                risk_free_rate=self.risk_free_rate,
                stamp_tax=self.stamp_tax,
                min_commission=self.min_commission
            )
            result = sub_bt.run_backtest(strategy, data, symbol)
            all_trades.extend(sub_bt.trades)
            if result and 'equity_curve' in result:
                all_equities[symbol] = result['equity_curve']
            per_symbol_results[symbol] = {
                'total_return': result.get('total_return', 0),
                'max_drawdown': result.get('max_drawdown', 0),
                'sharpe_ratio': result.get('sharpe_ratio', 0),
                'total_trades': result.get('total_trades', 0),
            }

        combined_equity = self._combine_equity_curves(all_equities)
        combined = self._calculate_combined_results(combined_equity, all_trades)
        combined['symbol_results'] = per_symbol_results
        combined['trades'] = all_trades
        combined['equity_curve'] = combined_equity
        return combined

    def _combine_equity_curves(self, all_equities: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        combined = None
        for symbol, eq in all_equities.items():
            if eq is None or eq.empty:
                continue
            if combined is None:
                combined = eq[['total_equity']].copy()
                combined.columns = [symbol]
            else:
                sub = eq[['total_equity']].copy()
                sub.columns = [symbol]
                combined = combined.join(sub, how='outer')

        if combined is None or combined.empty:
            return pd.DataFrame()

        combined = combined.ffill().fillna(0)
        combined['total_equity'] = combined.sum(axis=1)
        combined['cash'] = 0
        combined['positions_value'] = combined['total_equity']
        return combined

    def _execute_entry(self, symbol: str, shares: int, price: float, date):
        if shares <= 0:
            return

        executed_price = price * (1 + self.slippage)
        total_cost = shares * executed_price
        commission_cost = max(total_cost * self.commission, self.min_commission)

        if total_cost + commission_cost > self.portfolio.cash:
            affordable = (self.portfolio.cash - self.min_commission) / (executed_price * (1 + self.commission))
            shares = max(int(affordable / 100) * 100, 0)
            if shares <= 0:
                return
            total_cost = shares * executed_price
            commission_cost = max(total_cost * self.commission, self.min_commission)

        self.portfolio.cash -= (total_cost + commission_cost)
        self.portfolio.update_position(symbol, shares, executed_price, date)
        if self._strategy is not None:
            self._strategy.update_position(symbol, shares, executed_price, date)

        self.trades.append({
            'date': date,
            'symbol': symbol,
            'action': 'BUY',
            'shares': shares,
            'price': executed_price,
            'commission': commission_cost,
            'stamp_tax': 0,
            'total_cost': total_cost + commission_cost,
            'pnl': 0
        })

    def _execute_exit(self, symbol: str, price: float, date):
        if symbol not in self.portfolio.positions:
            return
        position = self.portfolio.positions[symbol]
        shares = position['shares']
        if shares <= 0:
            return

        executed_price = price * (1 - self.slippage)
        total_revenue = shares * executed_price
        commission_cost = max(total_revenue * self.commission, self.min_commission)
        stamp_tax_cost = total_revenue * self.stamp_tax
        entry_cost = shares * position['entry_price']
        pnl = total_revenue - entry_cost - commission_cost - stamp_tax_cost

        self.portfolio.cash += (total_revenue - commission_cost - stamp_tax_cost)
        self.portfolio.remove_position(symbol)
        if self._strategy is not None:
            self._strategy.close_position(symbol)

        self.trades.append({
            'date': date,
            'symbol': symbol,
            'action': 'SELL',
            'shares': shares,
            'price': executed_price,
            'commission': commission_cost,
            'stamp_tax': stamp_tax_cost,
            'total_cost': commission_cost + stamp_tax_cost,
            'pnl': pnl,
            'return_pct': pnl / entry_cost * 100 if entry_cost > 0 else 0
        })

    def _update_equity_curve(self, date, current_price: float):
        positions_value = 0
        for sym, pos in self.portfolio.positions.items():
            if pos.get('shares', 0) > 0:
                pos['current_price'] = current_price
                positions_value += pos['shares'] * current_price

        total_equity = self.portfolio.cash + positions_value
        self.equity_curve.append({
            'date': date,
            'cash': self.portfolio.cash,
            'positions_value': positions_value,
            'total_equity': total_equity
        })

    def _calculate_results(self) -> Dict:
        if not self.equity_curve:
            return {}

        equity_df = pd.DataFrame(self.equity_curve)
        equity_df.set_index('date', inplace=True)

        final_equity = equity_df['total_equity'].iloc[-1]
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        days = (equity_df.index[-1] - equity_df.index[0]).days
        annualized_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0

        rolling_max = equity_df['total_equity'].expanding().max()
        drawdown = equity_df['total_equity'] / rolling_max - 1
        max_drawdown = drawdown.min()

        daily_returns = equity_df['total_equity'].pct_change().dropna()
        sharpe_ratio = self._safe_sharpe(daily_returns)
        sortino_ratio = self._safe_sortino(daily_returns)
        calmar_ratio = annualized_return / abs(max_drawdown) if max_drawdown != 0 else 0

        sell_trades = [t for t in self.trades if t['action'] == 'SELL']
        total_closed = len(sell_trades)
        winning_trades = [t for t in sell_trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in sell_trades if t.get('pnl', 0) <= 0]
        win_rate = len(winning_trades) / total_closed if total_closed > 0 else 0

        avg_win = np.mean([t['pnl'] for t in winning_trades]) if winning_trades else 0
        avg_loss = abs(np.mean([t['pnl'] for t in losing_trades])) if losing_trades else 0

        total_win = sum(t['pnl'] for t in winning_trades)
        total_loss = abs(sum(t['pnl'] for t in losing_trades))
        profit_factor = total_win / total_loss if total_loss > 0 else (float('inf') if total_win > 0 else 0)

        total_commission = sum(t.get('commission', 0) for t in self.trades)
        total_stamp_tax = sum(t.get('stamp_tax', 0) for t in self.trades)

        return {
            'initial_capital': self.initial_capital,
            'final_equity': float(final_equity),
            'total_return': float(total_return),
            'annualized_return': float(annualized_return),
            'max_drawdown': float(max_drawdown),
            'max_drawdown_pct': float(max_drawdown * 100),
            'sharpe_ratio': float(sharpe_ratio),
            'sortino_ratio': float(sortino_ratio),
            'calmar_ratio': float(calmar_ratio),
            'total_trades': total_closed,
            'buy_trades': len([t for t in self.trades if t['action'] == 'BUY']),
            'sell_trades': total_closed,
            'win_rate': float(win_rate),
            'avg_win': float(avg_win),
            'avg_loss': float(avg_loss),
            'profit_factor': float(profit_factor) if profit_factor != float('inf') else 999.0,
            'total_commission': float(total_commission),
            'total_stamp_tax': float(total_stamp_tax),
            'daily_returns_std': float(daily_returns.std()) if not daily_returns.empty else 0,
            'trades': self.trades,
            'equity_curve': equity_df,
            'drawdown_series': drawdown
        }

    def _calculate_combined_results(self, equity_df: pd.DataFrame, trades: List) -> Dict:
        if equity_df is None or equity_df.empty:
            return {}

        final_equity = equity_df['total_equity'].iloc[-1]
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        days = (equity_df.index[-1] - equity_df.index[0]).days
        annualized_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0

        rolling_max = equity_df['total_equity'].expanding().max()
        drawdown = equity_df['total_equity'] / rolling_max - 1
        max_drawdown = drawdown.min()

        daily_returns = equity_df['total_equity'].pct_change().dropna()
        sharpe_ratio = self._safe_sharpe(daily_returns)

        sell_trades = [t for t in trades if t.get('action') == 'SELL']
        win_rate = 0
        if sell_trades:
            win_rate = sum(1 for t in sell_trades if t.get('pnl', 0) > 0) / len(sell_trades)

        return {
            'initial_capital': self.initial_capital,
            'final_equity': float(final_equity),
            'total_return': float(total_return),
            'annualized_return': float(annualized_return),
            'max_drawdown': float(max_drawdown),
            'sharpe_ratio': float(sharpe_ratio),
            'total_trades': len(sell_trades),
            'win_rate': float(win_rate),
            'equity_curve': equity_df,
            'drawdown_series': drawdown
        }

    def _safe_sharpe(self, daily_returns: pd.Series) -> float:
        if daily_returns.empty or daily_returns.std() == 0:
            return 0
        excess = daily_returns.mean() - self.risk_free_rate / 252
        return float(excess / daily_returns.std() * np.sqrt(252))

    def _safe_sortino(self, daily_returns: pd.Series) -> float:
        if daily_returns.empty:
            return 0
        downside = daily_returns[daily_returns < 0]
        if downside.empty or downside.std() == 0:
            return 0
        excess = daily_returns.mean() - self.risk_free_rate / 252
        return float(excess / downside.std() * np.sqrt(252))

    def get_trade_summary(self) -> pd.DataFrame:
        return pd.DataFrame(self.trades)
