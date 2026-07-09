"""
AI 决策策略 - 由大模型(LLM)综合行情/动量/新闻情感给出买卖决策

设计要点:
- 继承实时策略基类 RealtimeStrategy,实现 on_tick / on_bar(与其它实时策略同构)
- 每标的维护价格缓冲并计算动量(仿 RealtimeMomentumStrategy)
- 节流: 每标的每 decision_interval 秒最多调用一次 LLM,其余 tick 直接返回 None,
  避免每笔行情都请求大模型导致慢/贵/限流
- 新闻情感由引擎通过 set_sentiment 回灌,策略自身不再抓新闻
- LLM 返回 buy/sell -> 构造 TradingSignal;hold 或置信度不足 -> None
- LLM 不可用时,LLMClient 内部已回退到确定性规则,策略照常工作
"""
import time
import pandas as pd
from typing import Dict, List, Optional

from .realtime_strategy import RealtimeStrategy, TradingSignal, SignalType
from ..ai.llm_client import LLMClient


class AIStrategy(RealtimeStrategy):
    """大模型驱动的实时决策策略"""

    def __init__(
        self,
        symbols: List[str],
        llm_client: Optional[LLMClient] = None,
        lookback_period: int = 20,
        decision_interval: float = 30.0,
        confidence_threshold: float = 0.55,
        max_position_pct: float = 0.1,
        research_provider=None,
        parameters: Optional[Dict] = None,
    ):
        super().__init__("AIStrategy", symbols, parameters)
        self.llm = llm_client or LLMClient()
        self.lookback_period = lookback_period
        self.decision_interval = decision_interval
        self.confidence_threshold = confidence_threshold
        self.max_position_pct = max_position_pct
        self.research_provider = research_provider

        self.price_history: Dict[str, List[float]] = {s: [] for s in symbols}
        self.position_open: Dict[str, bool] = {s: False for s in symbols}
        self._last_decision_ts: Dict[str, float] = {s: 0.0 for s in symbols}
        self._sentiment: Dict[str, float] = {}
        self._last_decision: Dict[str, Dict] = {}

    def set_sentiment(self, sentiment_map: Dict[str, float]):
        """引擎回灌每股新闻情感分(-1~1)。合并更新,容错。"""
        if not isinstance(sentiment_map, dict):
            return
        for sym, score in sentiment_map.items():
            try:
                self._sentiment[sym] = float(score)
            except (ValueError, TypeError):
                continue

    def _compute_momentum(self, symbol: str) -> Optional[float]:
        hist = self.price_history[symbol]
        if len(hist) < self.lookback_period + 1:
            return None
        prices = pd.Series(hist)
        return float(prices.iloc[-1] / prices.iloc[-self.lookback_period - 1] - 1)

    def _build_context(self, symbol: str, quote: Dict, price: float) -> Dict:
        momentum = self._compute_momentum(symbol)
        research = ''
        if self.research_provider is not None:
            try:
                research = self.research_provider.get_research_context([symbol])
            except Exception:
                research = ''
        return {
            'symbol': symbol,
            'price': price,
            'change_pct': quote.get('change_pct', 0),
            'momentum': momentum,
            'momentum_window': self.lookback_period,
            'sentiment': self._sentiment.get(symbol),
            'position': 1 if self.position_open.get(symbol) else 0,
            'research': research,
        }

    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        price = quote.get('price', 0)
        if not price or price <= 0:
            return None

        # 维护价格缓冲(仿 RealtimeMomentumStrategy)
        hist = self.price_history.setdefault(symbol, [])
        hist.append(price)
        if len(hist) > self.lookback_period + 5:
            self.price_history[symbol] = hist[-(self.lookback_period + 5):]

        # 节流: 距上次决策不足 decision_interval 秒则跳过
        now = time.time()
        if now - self._last_decision_ts.get(symbol, 0.0) < self.decision_interval:
            return None
        self._last_decision_ts[symbol] = now

        context = self._build_context(symbol, quote, price)
        decision = self.llm.decide(context)
        self._last_decision[symbol] = decision.to_dict()

        if decision.confidence < self.confidence_threshold:
            return None

        if decision.action == 'buy' and not self.position_open.get(symbol):
            quantity = self.calculate_position_size(price, 1_000_000, self.max_position_pct)
            if quantity > 0:
                self.position_open[symbol] = True
                return TradingSignal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    price=price,
                    quantity=quantity,
                    reason=f"AI({decision.source})买入: {decision.reason}",
                    confidence=decision.confidence,
                )
        elif decision.action == 'sell' and self.position_open.get(symbol):
            self.position_open[symbol] = False
            return TradingSignal(
                symbol=symbol,
                signal_type=SignalType.SELL,
                price=price,
                quantity=0,
                reason=f"AI({decision.source})卖出: {decision.reason}",
                confidence=decision.confidence,
            )
        return None

    def on_bar(self, symbol: str, bar_data: Dict) -> Optional[TradingSignal]:
        close_price = bar_data.get('close', 0)
        if close_price <= 0:
            return None
        return self.on_tick(symbol, {'price': close_price,
                                     'change_pct': bar_data.get('change_pct', 0)})

    def get_status(self, symbol: str) -> Dict:
        return {
            'symbol': symbol,
            'position_open': self.position_open.get(symbol, False),
            'price_count': len(self.price_history.get(symbol, [])),
            'sentiment': self._sentiment.get(symbol),
            'last_decision': self._last_decision.get(symbol),
            'llm': self.llm.status(),
        }
