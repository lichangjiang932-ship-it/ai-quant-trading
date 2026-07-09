"""
策略测试
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import pytest
from src.data.market_data import MarketData
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.mean_reversion_strategy import MeanReversionStrategy
from src.strategies.base_strategy import Signal


def create_sample_data():
    """创建示例数据"""
    dates = pd.date_range(start='2020-01-01', end='2022-12-31', freq='D')
    np.random.seed(42)
    
    # 生成模拟价格数据
    returns = np.random.normal(0.0005, 0.02, len(dates))
    price = 100 * np.exp(np.cumsum(returns))
    
    data = pd.DataFrame({
        'Open': price * (1 + np.random.uniform(-0.01, 0.01, len(dates))),
        'High': price * (1 + np.random.uniform(0, 0.02, len(dates))),
        'Low': price * (1 - np.random.uniform(0, 0.02, len(dates))),
        'Close': price,
        'Volume': np.random.randint(1000000, 10000000, len(dates))
    }, index=dates)
    
    return data


class TestMomentumStrategy:
    """动量策略测试"""
    
    def test_strategy_initialization(self):
        """测试策略初始化"""
        strategy = MomentumStrategy(lookback_period=20, threshold=0.02)
        
        assert strategy.name == "MomentumStrategy"
        assert strategy.lookback_period == 20
        assert strategy.threshold == 0.02
    
    def test_generate_signals(self):
        """测试信号生成"""
        strategy = MomentumStrategy(lookback_period=20, threshold=0.02)
        data = create_sample_data()
        
        signals_data = strategy.generate_signals(data)
        
        assert 'signal' in signals_data.columns
        assert 'momentum' in signals_data.columns
        assert 'MA_20' in signals_data.columns
        assert 'MA_50' in signals_data.columns
        
        # 检查信号值
        assert all(signals_data['signal'].isin([
            Signal.BUY.value,
            Signal.SELL.value,
            Signal.HOLD.value
        ]))
    
    def test_position_size_calculation(self):
        """测试仓位计算"""
        strategy = MomentumStrategy()
        
        # 测试买入信号
        position_size = strategy.calculate_position_size(
            Signal.BUY, 100.0, 100000.0
        )
        assert position_size > 0
        assert position_size == 100  # 10% of 100000 / 100
        
        # 测试卖出信号
        position_size = strategy.calculate_position_size(
            Signal.SELL, 100.0, 100000.0
        )
        assert position_size == 0
        
        # 测试持有信号
        position_size = strategy.calculate_position_size(
            Signal.HOLD, 100.0, 100000.0
        )
        assert position_size == 0


class TestMeanReversionStrategy:
    """均值回归策略测试"""
    
    def test_strategy_initialization(self):
        """测试策略初始化"""
        strategy = MeanReversionStrategy(
            lookback_period=20,
            entry_threshold=2.0,
            exit_threshold=0.5
        )
        
        assert strategy.name == "MeanReversionStrategy"
        assert strategy.lookback_period == 20
        assert strategy.entry_threshold == 2.0
        assert strategy.exit_threshold == 0.5
    
    def test_generate_signals(self):
        """测试信号生成"""
        strategy = MeanReversionStrategy(
            lookback_period=20,
            entry_threshold=2.0
        )
        data = create_sample_data()
        
        signals_data = strategy.generate_signals(data)
        
        assert 'signal' in signals_data.columns
        assert 'MA' in signals_data.columns
        assert 'upper_band' in signals_data.columns
        assert 'lower_band' in signals_data.columns
        assert 'z_score' in signals_data.columns
    
    def test_position_size_calculation(self):
        """测试仓位计算"""
        strategy = MeanReversionStrategy()
        
        # 测试买入信号
        position_size = strategy.calculate_position_size(
            Signal.BUY, 100.0, 100000.0
        )
        assert position_size > 0
        
        # 测试卖出信号
        position_size = strategy.calculate_position_size(
            Signal.SELL, 100.0, 100000.0
        )
        assert position_size == 0


class TestSignal:
    """信号测试"""
    
    def test_signal_values(self):
        """测试信号值"""
        assert Signal.BUY.value == 1
        assert Signal.SELL.value == -1
        assert Signal.HOLD.value == 0


if __name__ == "__main__":
    pytest.main([__file__])