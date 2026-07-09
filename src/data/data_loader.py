"""
数据加载和处理模块
"""
import pandas as pd
import numpy as np
from typing import Optional, Dict, List, Tuple


class DataLoader:
    """数据加载和预处理类"""
    
    def __init__(self):
        self.indicators = {}
    
    def calculate_returns(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        periods: List[int] = [1, 5, 20]
    ) -> pd.DataFrame:
        """
        计算收益率
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            periods: 收益率周期列表
        
        Returns:
            DataFrame: 添加了收益率列的DataFrame
        """
        df = data.copy()
        
        for period in periods:
            df[f"return_{period}d"] = df[column].pct_change(period)
        
        return df
    
    def calculate_moving_averages(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        windows: List[int] = [5, 10, 20, 50, 200]
    ) -> pd.DataFrame:
        """
        计算移动平均线
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            windows: 移动平均周期列表
        
        Returns:
            DataFrame: 添加了移动平均线的DataFrame
        """
        df = data.copy()
        
        for window in windows:
            df[f"MA_{window}"] = df[column].rolling(window=window).mean()
        
        return df
    
    def calculate_volatility(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        window: int = 20
    ) -> pd.DataFrame:
        """
        计算波动率
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            window: 波动率计算窗口
        
        Returns:
            DataFrame: 添加了波动率列的DataFrame
        """
        df = data.copy()
        
        # 计算日收益率
        df['daily_return'] = df[column].pct_change()
        
        # 计算滚动波动率（年化）
        df['volatility'] = df['daily_return'].rolling(window=window).std() * np.sqrt(252)
        
        return df
    
    def calculate_rsi(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        period: int = 14
    ) -> pd.DataFrame:
        """
        计算相对强弱指数 (RSI)
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            period: RSI周期
        
        Returns:
            DataFrame: 添加了RSI列的DataFrame
        """
        df = data.copy()
        
        # 计算价格变化
        delta = df[column].diff()
        
        # 分离涨跌
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        # 计算RSI
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        return df
    
    def calculate_macd(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> pd.DataFrame:
        """
        计算MACD指标
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期
        
        Returns:
            DataFrame: 添加了MACD列的DataFrame
        """
        df = data.copy()
        
        # 计算快速和慢速EMA
        exp1 = df[column].ewm(span=fast_period, adjust=False).mean()
        exp2 = df[column].ewm(span=slow_period, adjust=False).mean()
        
        # 计算MACD线
        df['MACD'] = exp1 - exp2
        
        # 计算信号线
        df['Signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()
        
        # 计算MACD柱状图
        df['MACD_Hist'] = df['MACD'] - df['Signal']
        
        return df
    
    def calculate_bollinger_bands(
        self,
        data: pd.DataFrame,
        column: str = "Close",
        window: int = 20,
        num_std: float = 2.0
    ) -> pd.DataFrame:
        """
        计算布林带
        
        Args:
            data: 包含价格数据的DataFrame
            column: 价格列名
            window: 移动平均窗口
            num_std: 标准差倍数
        
        Returns:
            DataFrame: 添加了布林带列的DataFrame
        """
        df = data.copy()
        
        # 计算中轨（移动平均线）
        df['BB_middle'] = df[column].rolling(window=window).mean()
        
        # 计算标准差
        std = df[column].rolling(window=window).std()
        
        # 计算上下轨
        df['BB_upper'] = df['BB_middle'] + (std * num_std)
        df['BB_lower'] = df['BB_middle'] - (std * num_std)
        
        return df
    
    def prepare_data(
        self,
        data: pd.DataFrame,
        indicators: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        准备数据，计算常用指标
        
        Args:
            data: 原始数据
            indicators: 要计算的指标列表
        
        Returns:
            DataFrame: 添加了指标的DataFrame
        """
        if indicators is None:
            indicators = ["ma", "rsi", "macd", "bollinger"]
        
        df = data.copy()
        
        if "ma" in indicators:
            df = self.calculate_moving_averages(df)
        
        if "rsi" in indicators:
            df = self.calculate_rsi(df)
        
        if "macd" in indicators:
            df = self.calculate_macd(df)
        
        if "bollinger" in indicators:
            df = self.calculate_bollinger_bands(df)
        
        if "volatility" in indicators:
            df = self.calculate_volatility(df)
        
        return df