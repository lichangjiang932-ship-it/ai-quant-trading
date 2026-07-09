"""
简单示例策略
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import matplotlib.pyplot as plt
from src.data.market_data import MarketData
from src.data.data_loader import DataLoader
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.mean_reversion_strategy import MeanReversionStrategy
from src.backtest.backtester import Backtester


def run_momentum_strategy():
    """运行动量策略"""
    print("=" * 50)
    print("动量策略回测")
    print("=" * 50)
    
    # 获取数据
    market_data = MarketData()
    data = market_data.get_stock_data("AAPL", period="2y")
    
    if data.empty:
        print("无法获取数据")
        return
    
    # 创建策略
    strategy = MomentumStrategy(
        lookback_period=20,
        threshold=0.02
    )
    
    # 运行回测
    backtester = Backtester(
        initial_capital=100000,
        commission=0.001,
        slippage=0.001
    )
    
    results = backtester.run_backtest(strategy, data, "AAPL")
    
    # 打印结果
    print(f"初始资金: ${results['initial_capital']:,.2f}")
    print(f"最终权益: ${results['final_equity']:,.2f}")
    print(f"总收益率: {results['total_return']:.2%}")
    print(f"年化收益率: {results['annualized_return']:.2%}")
    print(f"最大回撤: {results['max_drawdown']:.2%}")
    print(f"夏普比率: {results['sharpe_ratio']:.2f}")
    print(f"总交易次数: {results['total_trades']}")
    print(f"胜率: {results['win_rate']:.2%}")
    
    # 绘制权益曲线
    equity_df = results['equity_curve']
    plt.figure(figsize=(12, 6))
    plt.plot(equity_df.index, equity_df['total_equity'])
    plt.title('动量策略权益曲线')
    plt.xlabel('日期')
    plt.ylabel('权益')
    plt.grid(True)
    plt.savefig('momentum_equity_curve.png')
    plt.show()


def run_mean_reversion_strategy():
    """运行均值回归策略"""
    print("=" * 50)
    print("均值回归策略回测")
    print("=" * 50)
    
    # 获取数据
    market_data = MarketData()
    data = market_data.get_stock_data("AAPL", period="2y")
    
    if data.empty:
        print("无法获取数据")
        return
    
    # 创建策略
    strategy = MeanReversionStrategy(
        lookback_period=20,
        entry_threshold=2.0,
        exit_threshold=0.5
    )
    
    # 运行回测
    backtester = Backtester(
        initial_capital=100000,
        commission=0.001,
        slippage=0.001
    )
    
    results = backtester.run_backtest(strategy, data, "AAPL")
    
    # 打印结果
    print(f"初始资金: ${results['initial_capital']:,.2f}")
    print(f"最终权益: ${results['final_equity']:,.2f}")
    print(f"总收益率: {results['total_return']:.2%}")
    print(f"年化收益率: {results['annualized_return']:.2%}")
    print(f"最大回撤: {results['max_drawdown']:.2%}")
    print(f"夏普比率: {results['sharpe_ratio']:.2f}")
    print(f"总交易次数: {results['total_trades']}")
    print(f"胜率: {results['win_rate']:.2%}")
    
    # 绘制权益曲线
    equity_df = results['equity_curve']
    plt.figure(figsize=(12, 6))
    plt.plot(equity_df.index, equity_df['total_equity'])
    plt.title('均值回归策略权益曲线')
    plt.xlabel('日期')
    plt.ylabel('权益')
    plt.grid(True)
    plt.savefig('mean_reversion_equity_curve.png')
    plt.show()


if __name__ == "__main__":
    run_momentum_strategy()
    print("\n" + "=" * 50 + "\n")
    run_mean_reversion_strategy()