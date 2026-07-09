"""
机器学习策略 - 基于技术指标的涨跌预测
使用随机森林/梯度提升对次日方向进行分类,生成交易信号

支持回测和实时两种模式:
- 回测模式: 在 generate_signals 中先训练再预测
- 实时模式: 在 on_tick 中加载已训练模型直接预测
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from .base_strategy import BaseStrategy, Signal

try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


class MLStrategy(BaseStrategy):
    """机器学习策略"""

    def __init__(
        self,
        symbols: List[str],
        lookback_period: int = 60,
        train_window: int = 252,
        retrain_interval: int = 60,
        prediction_horizon: int = 5,
        confidence_threshold: float = 0.55,
        model_type: str = 'random_forest',
        parameters: Optional[Dict] = None
    ):
        """
        初始化ML策略

        Args:
            symbols: 监控的股票代码
            lookback_period: 特征计算回看周期
            train_window: 训练窗口大小
            retrain_interval: 重训练间隔(天数)
            prediction_horizon: 预测未来N日方向
            confidence_threshold: 信号置信度阈值
            model_type: 模型类型 (random_forest / gradient_boosting)
            parameters: 其他参数
        """
        super().__init__("MLStrategy", parameters or {})
        self.symbols = symbols
        self.lookback_period = lookback_period
        self.train_window = train_window
        self.retrain_interval = retrain_interval
        self.prediction_horizon = prediction_horizon
        self.confidence_threshold = confidence_threshold
        self.model_type = model_type

        self._models: Dict[str, object] = {}
        self._scalers: Dict[str, object] = {}
        self._last_train_bar: Dict[str, int] = {}
        self._last_signals: Dict[str, Signal] = {}
        self._feature_names: List[str] = []

    def _compute_features(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标特征"""
        df = data.copy()

        for period in [5, 10, 20, 60]:
            df[f'return_{period}d'] = df['Close'].pct_change(period)

        df['volatility_20d'] = df['Close'].pct_change().rolling(20).std()

        delta = df['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi_14'] = 100 - (100 / (1 + rs))

        ema12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema26 = df['Close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']

        df['bb_middle'] = df['Close'].rolling(20).mean()
        std20 = df['Close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + 2 * std20
        df['bb_lower'] = df['bb_middle'] - 2 * std20
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
        df['bb_position'] = (df['Close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)

        df['volume_ratio'] = df['Volume'] / df['Volume'].rolling(20).mean().replace(0, np.nan)

        df['high_low_range'] = (df['High'] - df['Low']) / df['Close']
        df['close_open'] = (df['Close'] - df['Open']) / df['Open'].replace(0, np.nan)

        for w in [5, 10, 20]:
            df[f'ma_cross_{w}'] = df['Close'] / df['Close'].rolling(w).mean() - 1

        return df

    def _build_labels(self, df: pd.DataFrame) -> pd.Series:
        """构造训练标签: 未来N日涨跌方向"""
        future_return = df['Close'].pct_change(self.prediction_horizon).shift(-self.prediction_horizon)
        labels = pd.Series(index=df.index, dtype=float)
        labels[future_return > 0.02] = 1
        labels[future_return < -0.02] = -1
        labels[(future_return >= -0.02) & (future_return <= 0.02)] = 0
        return labels

    def _get_feature_columns(self, df: pd.DataFrame) -> List[str]:
        exclude = {'Open', 'High', 'Low', 'Close', 'Volume', 'label', 'signal', 'confidence', 'raw_signal'}
        return [c for c in df.columns if c not in exclude and df[c].dtype != object]

    def _train_model(self, symbol: str, df: pd.DataFrame, end_idx: int):
        """在指定位置训练模型"""
        if not HAS_SKLEARN:
            return None, None

        start_idx = max(0, end_idx - self.train_window)
        train_df = df.iloc[start_idx:end_idx].copy()

        if 'label' not in train_df.columns:
            train_df['label'] = self._build_labels(train_df)

        feature_cols = self._get_feature_columns(train_df)
        if not feature_cols:
            return None, None
        needed_cols = list(set(feature_cols + ['label']))
        train_data = train_df[needed_cols].dropna()

        if len(train_data) < 50:
            return None, None

        X = train_data[feature_cols].values
        y = train_data['label'].values

        unique_classes = np.unique(y)
        if len(unique_classes) < 2:
            return None, None

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        if self.model_type == 'gradient_boosting':
            model = GradientBoostingClassifier(
                n_estimators=80, max_depth=4, learning_rate=0.05,
                random_state=42, subsample=0.8
            )
        else:
            model = RandomForestClassifier(
                n_estimators=100, max_depth=8, min_samples_leaf=20,
                random_state=42, n_jobs=-1
            )

        model.fit(X_scaled, y)
        self._feature_names = feature_cols
        return model, scaler

    def _predict(self, symbol: str, df: pd.DataFrame, idx: int) -> tuple:
        """
        在 idx 位置预测方向
        Returns: (signal_value, confidence)  signal_value in {-1, 0, 1}
        """
        if not HAS_SKLEARN:
            return 0, 0.0

        need_train = (
            symbol not in self._models or
            symbol not in self._last_train_bar or
            idx - self._last_train_bar[symbol] >= self.retrain_interval
        )

        if need_train:
            model, scaler = self._train_model(symbol, df, idx)
            if model is not None:
                self._models[symbol] = model
                self._scalers[symbol] = scaler
                self._last_train_bar[symbol] = idx

        if symbol not in self._models:
            return 0, 0.0

        feature_cols = self._feature_names
        if not feature_cols:
            return 0, 0.0

        row = df.iloc[idx]
        features = row[feature_cols].values.reshape(1, -1)

        if np.isnan(features).any():
            return 0, 0.0

        try:
            X_scaled = self._scalers[symbol].transform(features)
            proba = self._models[symbol].predict_proba(X_scaled)[0]
            classes = self._models[symbol].classes_

            best_idx = int(np.argmax(proba))
            confidence = float(proba[best_idx])
            pred_class = int(classes[best_idx])

            return pred_class, confidence
        except Exception:
            return 0, 0.0

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        df = self._compute_features(data)
        df['label'] = self._build_labels(df)
        df['signal'] = np.nan
        df['confidence'] = np.nan

        symbol = self.symbols[0] if self.symbols else "STOCK"
        start_idx = self.train_window + self.prediction_horizon

        for i in range(start_idx, len(df)):
            pred, conf = self._predict(symbol, df, i)
            if conf >= self.confidence_threshold:
                df.iloc[i, df.columns.get_loc('signal')] = float(pred)
                df.iloc[i, df.columns.get_loc('confidence')] = conf

        df['signal'] = df['signal'].fillna(0)
        return df

    def on_tick(self, symbol: str, quote: Dict) -> Optional[object]:
        """实时模式: 直接基于最近一次模型预测输出信号"""
        from .realtime_strategy import TradingSignal, SignalType

        if symbol not in self._last_signals:
            return None

        sig = self._last_signals[symbol]
        if sig == Signal.HOLD:
            return None

        price = quote.get('price', 0)
        if price <= 0:
            return None

        confidence = quote.get('confidence', 0.5)

        if sig == Signal.BUY:
            quantity = self.calculate_position_size(price, 1000000, 0.1)
            if quantity > 0:
                return TradingSignal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    price=price,
                    quantity=quantity,
                    reason=f"ML模型预测上涨 (置信度={confidence:.2%})",
                    confidence=confidence
                )
        else:
            return TradingSignal(
                symbol=symbol,
                signal_type=SignalType.SELL,
                price=price,
                quantity=0,
                reason=f"ML模型预测下跌 (置信度={confidence:.2%})",
                confidence=confidence
            )
        return None

    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[object]:
        return self.on_tick(symbol, bar_data)

    def update_prediction(self, symbol: str, signal: Signal, confidence: float = 0.5):
        """供外部训练循环更新模型预测结果"""
        self._last_signals[symbol] = signal
        self.parameters[f'{symbol}_confidence'] = confidence

    def get_model_info(self) -> Dict:
        return {
            'has_model': bool(self._models),
            'symbols_trained': list(self._models.keys()),
            'feature_count': len(self._feature_names),
            'sklearn_available': HAS_SKLEARN
        }

    def should_enter_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float,
        portfolio_value: float
    ) -> bool:
        if signal != Signal.BUY:
            return False
        if self.has_position(symbol):
            return False
        return self.calculate_position_size(signal, current_price, portfolio_value) > 0

    def should_exit_position(
        self,
        symbol: str,
        signal: Signal,
        current_price: float
    ) -> bool:
        if not self.has_position(symbol):
            return False
        if signal == Signal.SELL:
            return True
        pos = self.positions[symbol]
        return self._should_stop_loss(pos['entry_price'], current_price) or \
               self._should_take_profit(pos['entry_price'], current_price)

    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
        portfolio_value: float,
        max_position_pct: float = 0.1
    ) -> float:
        if signal != Signal.BUY:
            return 0
        max_investment = portfolio_value * max_position_pct
        shares = int(max_investment / current_price)
        return max(shares, 0)
