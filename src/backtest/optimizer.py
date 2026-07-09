import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Callable, Any
from itertools import product
from concurrent.futures import ProcessPoolExecutor, as_completed
from ..strategies.base_strategy import BaseStrategy
from .backtester import Backtester


class ParameterGrid:
    def __init__(self, param_grid: Dict[str, List]):
        self.param_grid = param_grid

    def __iter__(self):
        keys = self.param_grid.keys()
        values = self.param_grid.values()
        for combo in product(*values):
            yield dict(zip(keys, combo))

    def __len__(self):
        return len(list(product(*self.param_grid.values())))


class StrategyOptimizer:
    def __init__(
        self,
        strategy_class: type,
        data: pd.DataFrame,
        symbol: str = "STOCK",
        initial_capital: float = 100000,
        commission: float = 0.001,
        slippage: float = 0.001,
        metric: str = "sharpe_ratio"
    ):
        self.strategy_class = strategy_class
        self.data = data
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self.metric = metric
        self.results = []

    def optimize(self, param_grid: Dict[str, List], parallel: bool = True) -> pd.DataFrame:
        grid = ParameterGrid(param_grid)
        total = len(grid)
        print(f"参数优化开始，共 {total} 组参数")

        if parallel and total > 10:
            results = self._run_parallel(grid)
        else:
            results = self._run_sequential(grid)

        self.results = pd.DataFrame(results)
        self.results.sort_values(by=self.metric, ascending=False, inplace=True)
        self.results.reset_index(drop=True, inplace=True)

        print(f"参数优化完成")
        print(f"最佳{self.metric}: {self.results.iloc[0][self.metric]:.4f}")
        return self.results

    def _run_single(self, params: Dict) -> Dict:
        try:
            strategy = self.strategy_class(**params)
            bt = Backtester(
                initial_capital=self.initial_capital,
                commission=self.commission,
                slippage=self.slippage
            )
            result = bt.run_backtest(strategy, self.data, self.symbol)
            return {**params, **{
                k: result.get(k, 0) for k in [
                    'total_return', 'annualized_return', 'max_drawdown',
                    'sharpe_ratio', 'sortino_ratio', 'calmar_ratio',
                    'total_trades', 'win_rate', 'profit_factor'
                ]
            }}
        except Exception as e:
            return {**params, 'error': str(e)}

    def _run_sequential(self, grid) -> List[Dict]:
        return [self._run_single(params) for params in grid]

    def _run_parallel(self, grid) -> List[Dict]:
        results = []
        with ProcessPoolExecutor() as executor:
            futures = {executor.submit(self._run_single, params): params for params in grid}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    params = futures[future]
                    results.append({**params, 'error': str(e)})
        return results

    def get_best_params(self, n: int = 1) -> List[Dict]:
        if self.results.empty:
            return []
        return self.results.head(n).to_dict('records')

    def plot_optimization_heatmap(self, param_x: str, param_y: str, figsize=(10, 8)):
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns

            if self.results.empty:
                print("没有优化结果")
                return

            pivot = self.results.pivot_table(
                values=self.metric,
                index=param_y,
                columns=param_x,
                aggfunc='mean'
            )

            plt.figure(figsize=figsize)
            sns.heatmap(pivot, annot=True, fmt='.3f', cmap='RdYlGn')
            plt.title(f'参数优化热力图 - {self.metric}')
            plt.tight_layout()
            plt.show()

        except ImportError:
            print("请安装 matplotlib 和 seaborn: pip install matplotlib seaborn")
