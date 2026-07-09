"""
多智能体决策策略 - 把 TradingAgentsGraph 接进实时引擎

继承 RealtimeStrategy,on_tick 内**重度节流**:
- 每股每 decision_interval 秒最多跑一次完整多智能体流水线(约 6-9 次 LLM 调用,慢且贵)
- 或当引擎标记该股有突发新闻时,立即触发一次分析
其余 tick 直接返回 None。

决策 -> TradingSignal(沿用 confidence 阈值 + position_open 模式)。
卖出/平仓时,调用 ReflectionMemory 记录本次交易的经验教训(供未来决策学习)。
"""
import time
from datetime import datetime
from typing import Dict, List, Optional

from .realtime_strategy import RealtimeStrategy, TradingSignal, SignalType
from ..ai.agents.orchestrator import TradingAgentsGraph


class AIAgentsStrategy(RealtimeStrategy):
    def __init__(
        self,
        symbols: List[str],
        graph: TradingAgentsGraph,
        decision_interval: float = 300.0,
        confidence_threshold: float = 0.6,
        max_position_pct: float = 0.1,
        memory=None,
        parameters: Optional[Dict] = None,
    ):
        super().__init__("AIAgentsStrategy", symbols, parameters)
        self.graph = graph
        self.decision_interval = decision_interval
        self.confidence_threshold = confidence_threshold
        self.max_position_pct = max_position_pct
        self.memory = memory

        self.position_open: Dict[str, bool] = {s: False for s in symbols}
        self._entry_price: Dict[str, float] = {}
        self._last_decision_ts: Dict[str, float] = {s: 0.0 for s in symbols}
        self._sentiment: Dict[str, float] = {}
        self._breaking: Dict[str, str] = {}
        self._last_decision: Dict[str, Dict] = {}
        self._risk_context: Dict = {}

    # ---- 引擎回灌接口 ----
    def set_sentiment(self, sentiment_map: Dict[str, float]):
        if isinstance(sentiment_map, dict):
            for sym, score in sentiment_map.items():
                try:
                    self._sentiment[sym] = float(score)
                except (ValueError, TypeError):
                    pass

    def set_breaking_news(self, symbol: str, text: str):
        """引擎发现某股突发新闻时调用,触发即时分析。"""
        if symbol:
            self._breaking[symbol] = text

    def set_risk_context(self, risk_context: Dict):
        """引擎回灌当前风险快照(回撤/仓位/日亏),供 AI 风控经理复审。"""
        if isinstance(risk_context, dict):
            self._risk_context = risk_context

    def _should_run(self, symbol: str, now: float) -> bool:
        if symbol in self._breaking:
            return True
        return (now - self._last_decision_ts.get(symbol, 0.0)) >= self.decision_interval

    def on_tick(self, symbol: str, quote: Dict) -> Optional[TradingSignal]:
        price = quote.get('price', 0)
        if not price or price <= 0:
            return None

        now = time.time()
        if not self._should_run(symbol, now):
            return None
        self._last_decision_ts[symbol] = now
        breaking = self._breaking.pop(symbol, None)

        context = {
            'price': price,
            'change_pct': quote.get('change_pct', 0),
            'momentum': quote.get('momentum'),
            'sentiment': self._sentiment.get(symbol),
            'position': 1 if self.position_open.get(symbol) else 0,
            'breaking_news': breaking,
            'risk': self._risk_context,
            'trade_date': datetime.now().strftime('%Y%m%d'),
        }

        try:
            decision = self.graph.analyze(symbol, context)
        except Exception as e:
            self._last_decision[symbol] = {'error': str(e)}
            return None

        self._last_decision[symbol] = decision.to_dict()
        # 打印完整推理链到控制台(让用户看到"每个 agent 说了什么")
        print(decision.pretty())

        if decision.confidence < self.confidence_threshold:
            return None

        if decision.action == 'buy' and not self.position_open.get(symbol):
            quantity = self.calculate_position_size(price, 1_000_000, self.max_position_pct)
            if quantity > 0:
                self.position_open[symbol] = True
                self._entry_price[symbol] = price
                return TradingSignal(
                    symbol=symbol, signal_type=SignalType.BUY, price=price,
                    quantity=quantity,
                    reason=f"多智能体买入: {decision.reason[:120]}",
                    confidence=decision.confidence,
                )
        elif decision.action == 'sell' and self.position_open.get(symbol):
            self.position_open[symbol] = False
            entry = self._entry_price.pop(symbol, price)
            # 反思记忆: 记录本次平仓的经验教训
            if self.memory is not None:
                try:
                    self.memory.record_trade_close(
                        symbol, entry_price=entry, exit_price=price, quantity=1,
                        decision_reason=decision.reason[:200],
                    )
                except Exception:
                    pass
            return TradingSignal(
                symbol=symbol, signal_type=SignalType.SELL, price=price, quantity=0,
                reason=f"多智能体卖出: {decision.reason[:120]}",
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
            'sentiment': self._sentiment.get(symbol),
            'last_decision': self._last_decision.get(symbol),
            'graph': self.graph.status(),
        }
