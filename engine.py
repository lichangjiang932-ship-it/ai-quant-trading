import asyncio
import time
import signal
import sys
from typing import Dict, List, Optional, Callable
from datetime import datetime, time as dtime
from pathlib import Path

from src.utils.async_engine import AsyncEngine, TaskPriority
from src.utils.state_manager import StateManager
from src.data.realtime.ws_client import WSQuoteClient, QuoteSource, WSSymbol
from src.data.realtime.realtime_data import RealtimeData
from src.execution.fast_broker import FastBroker, ExecOrder
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide, RiskCheckResult
from src.execution.tpsl_monitor import TPSLMonitor, TPSLConfig, TPSLReason, TPSLEvent
from src.strategies.realtime_strategy import RealtimeStrategy, TradingSignal, SignalType
from src.strategies.cross_ma_strategy import CrossMAStrategy
from src.strategies.realtime_momentum_strategy import RealtimeMomentumStrategy
from src.strategies.realtime_mean_reversion_strategy import RealtimeMeanReversionStrategy
from src.ai.llm_client import LLMClient
from src.data.providers.openbb_provider import OpenBBProvider
from src.news.priority_news import PriorityNewsPipeline, NewsPriority
from src.news.news_analyzer import NewsAnalyzer
from src.scheduler.trading_scheduler import TradingScheduler, TradingSession
from src.utils.config import Config
from src.notification.notifier import (
    NotificationManager, Notification, NotificationType, NotificationLevel,
    build_manager_from_config,
)


class TradingEngine:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config = Config(config_path)

        try:
            from src.data.em_client import set_min_interval
            set_min_interval(self.config.get('data_source.em_min_interval', 1.0))
        except Exception:
            pass

        self.async_engine = AsyncEngine("TradingEngine")
        self.state_manager = StateManager(db_path="data/trading_state.db")
        self.ws_client = WSQuoteClient(QuoteSource.EASTMONEY)
        self.http_client = RealtimeData()
        self.broker = self._create_broker()
        self.scheduler = TradingScheduler()
        self.news_analyzer = NewsAnalyzer()
        self.news_pipeline = PriorityNewsPipeline()
        self.strategies: List[RealtimeStrategy] = []

        self.risk_manager = RiskManager(
            max_position_size=self.config.get('risk.max_position_size', 0.10),
            max_drawdown=self.config.get('risk.max_drawdown', 0.20),
            stop_loss=self.config.get('risk.stop_loss', 0.05),
            take_profit=self.config.get('risk.take_profit', 0.10),
            max_daily_loss=self.config.get('risk.max_daily_loss', 0.02),
            max_total_position=self.config.get('risk.max_total_position', 0.95),
            max_orders_per_day=self.config.get('risk.max_orders_per_day', 100),
        )
        self.tpsl_monitor = TPSLMonitor(
            default_config=TPSLConfig(
                stop_loss=self.config.get('risk.stop_loss', 0.05),
                take_profit=self.config.get('risk.take_profit', 0.10),
                trailing_stop=self.config.get('risk.trailing_stop'),
                partial_tp_levels=self.config.get('risk.partial_tp_levels', []),
            )
        )
        self._tpsl_events: asyncio.Queue = asyncio.Queue(maxsize=500)

        self.notifier: Optional[NotificationManager] = None
        notif_cfg = self.config.get('notification', {})
        if notif_cfg.get('enabled', True):
            self.notifier = build_manager_from_config(notif_cfg)
            for t in (NotificationType.TRADE, NotificationType.SIGNAL, NotificationType.RISK,
                      NotificationType.TPSL, NotificationType.SYSTEM, NotificationType.ERROR):
                pass
        if self.config.get('notification.min_level', 'info') == 'warning':
            self.notifier and self.notifier.set_min_level(NotificationLevel.WARNING)

        self._running = False
        self._shutdown_requested = False
        self._strategy_signals: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._last_quotes: Dict[str, Dict] = {}
        self._quote_count = 0
        self._signal_count = 0
        self._start_time: Optional[datetime] = None

        self._latency_stats = {
            'quote_to_signal_ms': [],
            'signal_to_order_ms': [],
            'total_pipeline_ms': [],
        }
        self._max_latency_samples = 10000

    def _create_broker(self):
        """按 config 的 broker.type 创建券商。

        simulated -> FastBroker(模拟,默认)
        qmt       -> QMTFastAdapter(实盘);连接失败自动回退到 FastBroker,
                     仿照旧版 main.py 的容错逻辑,保证引擎永不因券商缺失而崩溃。
        """
        initial_capital = self.config.get('trading.initial_capital', 1_000_000)
        commission_rate = self.config.get('commission.rate', 0.0003)
        stamp_tax_rate = self.config.get('commission.stamp_tax', 0.0005)
        min_commission = self.config.get('commission.min', 5.0)

        def _make_fast():
            return FastBroker(
                initial_capital=initial_capital,
                commission_rate=commission_rate,
                stamp_tax_rate=stamp_tax_rate,
                min_commission=min_commission,
            )

        broker_type = str(self.config.get('broker.type', 'simulated')).lower()

        if broker_type == 'qmt':
            try:
                from src.execution.brokers.qmt_fast_adapter import QMTFastAdapter
                adapter = QMTFastAdapter(
                    account_id=self.config.get('broker.account_id', ''),
                    mini_qmt_path=self.config.get('broker.mini_qmt_path', ''),
                    initial_capital=initial_capital,
                    commission_rate=commission_rate,
                    stamp_tax_rate=stamp_tax_rate,
                    min_commission=min_commission,
                )
                if adapter.connect():
                    print("[Engine] 券商: QMT 实盘已连接")
                    return adapter
                print("[Engine] QMT 连接失败, 回退到模拟券商 FastBroker")
            except Exception as e:
                print(f"[Engine] QMT 初始化异常, 回退到模拟券商: {e}")
            return _make_fast()

        if broker_type not in ('simulated', 'sim', 'fast'):
            print(f"[Engine] 未知券商类型 '{broker_type}', 使用模拟券商 FastBroker")
        else:
            print("[Engine] 券商: 模拟盘 FastBroker")
        return _make_fast()

    def _build_agents_strategy(self, symbols):
        """装配多智能体策略(TradingAgents 架构): 分析师->辩论->交易员->风控->反思。

        注入项目已有的真实数据源:
        - RealtimeData 提供日K,技术分析师用 DataLoader 算真实指标
        - NewsAnalyzer 提供个股新闻,新闻分析师读取
        - FundamentalsFetcher(akshare) 提供 PE/PB/ROE,基本面分析师读取
        - ReflectionMemory(SQLite) 提供历史经验回灌
        """
        from src.strategies.ai_agents_strategy import AIAgentsStrategy
        from src.ai.agents.orchestrator import TradingAgentsGraph
        from src.ai.agents.memory import ReflectionMemory
        from src.data.fundamentals import FundamentalsFetcher
        from src.data.capital_flow import CapitalFlowFetcher

        ag = self.config.get('agents', {})
        llm = LLMClient(
            provider=ag.get('provider', 'deepseek'),
            model=ag.get('deep_think_model') or ag.get('quick_think_model'),
            api_key_env=ag.get('api_key_env'),
            base_url=ag.get('base_url'),
            temperature=ag.get('temperature', 0.3),
        )
        memory = None
        if ag.get('use_memory', True):
            memory = ReflectionMemory(db_path="data/trading_state.db", llm=llm,
                                      deep_model=ag.get('deep_think_model'))
        graph = TradingAgentsGraph(
            llm=llm,
            config={
                'analysts': ag.get('analysts',
                                   ['technical', 'sentiment', 'news', 'fundamentals', 'capital']),
                'max_debate_rounds': ag.get('max_debate_rounds', 1),
                'quick_think_model': ag.get('quick_think_model'),
                'deep_think_model': ag.get('deep_think_model'),
                'use_memory': ag.get('use_memory', True),
                'max_drawdown': self.config.get('risk.max_drawdown', 0.20),
                'max_total_position': self.config.get('risk.max_total_position', 0.95),
            },
            kline_provider=self.http_client,
            news_analyzer=self.news_analyzer,
            fundamentals=FundamentalsFetcher(),
            capital_provider=CapitalFlowFetcher(),
            memory=memory,
        )
        strategy = AIAgentsStrategy(
            symbols=symbols,
            graph=graph,
            decision_interval=ag.get('decision_interval', 300),
            confidence_threshold=ag.get('confidence_threshold', 0.6),
            max_position_pct=self.config.get('risk.max_position_size', 0.1),
            memory=memory,
        )
        print(f"[Engine] 多智能体策略就绪: {graph.status()}")
        return strategy

    def setup_strategies(self):
        symbols = self.config.get('trading.symbols', ['sh600000', 'sz000001'])
        strategy_type = self.config.get('strategy.type', 'cross_ma')

        if strategy_type == 'cross_ma':
            strategy = CrossMAStrategy(
                symbols=symbols,
                short_window=self.config.get('strategy.short_window', 5),
                long_window=self.config.get('strategy.long_window', 20),
            )
        elif strategy_type == 'momentum':
            strategy = RealtimeMomentumStrategy(
                symbols=symbols,
                lookback_period=self.config.get('strategy.lookback_period', 20),
                entry_threshold=self.config.get('strategy.entry_threshold', 0.03),
            )
        elif strategy_type == 'mean_reversion':
            strategy = RealtimeMeanReversionStrategy(
                symbols=symbols,
                lookback_period=self.config.get('strategy.lookback_period', 20),
                entry_threshold=self.config.get('strategy.entry_threshold', 2.0),
            )
        elif strategy_type in ('agents', 'multi_agent', 'tradingagents'):
            strategy = self._build_agents_strategy(symbols)
        elif strategy_type in ('ai', 'llm'):
            from src.strategies.ai_strategy import AIStrategy
            ai_cfg = self.config.get('ai', {})
            llm = LLMClient(
                provider=ai_cfg.get('provider', 'deepseek'),
                model=ai_cfg.get('model'),
                api_key_env=ai_cfg.get('api_key_env'),
                base_url=ai_cfg.get('base_url'),
                temperature=ai_cfg.get('temperature', 0.2),
            )
            research = None
            if ai_cfg.get('use_openbb', False):
                research = OpenBBProvider(enabled=True)
            strategy = AIStrategy(
                symbols=symbols,
                llm_client=llm,
                lookback_period=self.config.get('strategy.lookback_period', 20),
                decision_interval=ai_cfg.get('decision_interval', 30),
                confidence_threshold=ai_cfg.get('confidence_threshold', 0.55),
                max_position_pct=self.config.get('risk.max_position_size', 0.1),
                research_provider=research,
            )
            print(f"[Engine] AI 策略就绪: LLM {llm.status()}")
        elif strategy_type in ('ml', 'ml_strategy'):
            try:
                from src.strategies.ml_strategy import MLStrategy
                ml_cfg = self.config.get('ml', {})
                strategy = MLStrategy(
                    symbols=symbols,
                    lookback_period=self.config.get('strategy.lookback_period',
                                                    ml_cfg.get('lookback_period', 60)),
                    train_window=ml_cfg.get('train_window', 300),
                    prediction_horizon=ml_cfg.get('prediction_horizon', 5),
                    confidence_threshold=ml_cfg.get('confidence_threshold', 0.55),
                    retrain_interval=ml_cfg.get('retrain_interval', 60),
                    model_type=ml_cfg.get('model_type', 'random_forest'),
                )
            except ImportError as e:
                print(f"[Engine] ML 策略加载失败, 回退到均线策略: {e}")
                strategy = CrossMAStrategy(symbols=symbols)
        else:
            strategy = CrossMAStrategy(symbols=symbols)

        strategy.register_signal_callback(self._on_strategy_signal)
        self.strategies.append(strategy)
        print(f"[Engine] 策略已注册: {strategy.name}, 监控 {len(symbols)} 只股票")

    async def _on_ws_quote(self, quotes: Dict):
        now = time.perf_counter()
        self._last_quotes.update(quotes)
        self._quote_count += 1

        for symbol, quote in quotes.items():
            price = quote.get('price', 0) or quote.get('f2', 0)
            if price > 0:
                price = float(price)
                self.broker.update_price(symbol, price)
                self._eval_tpsl(symbol, price)

        for strategy in self.strategies:
            for symbol in strategy.symbols:
                if symbol in quotes:
                    quote = quotes[symbol]
                    t0 = time.perf_counter()
                    signal = strategy.on_tick(symbol, quote)
                    if signal:
                        elapsed = (time.perf_counter() - t0) * 1000
                        self._record_latency('quote_to_signal', elapsed)
                        await self._strategy_signals.put((signal, now))
                        self._signal_count += 1

    def _eval_tpsl(self, symbol: str, price: float):
        events = self.tpsl_monitor.on_quote(symbol, price)
        for ev in events:
            try:
                self._tpsl_events.put_nowait(ev)
            except asyncio.QueueFull:
                pass
            self.risk_manager.record_order({
                'symbol': ev.symbol, 'side': 'sell', 'quantity': ev.suggested_quantity,
                'price': ev.current_price, 'reason': f'tpsl:{ev.reason.value}',
            })
            print(f"[TPSL] {ev.symbol} {ev.reason.value} | pnl={ev.pnl_pct:.2%} | qty={ev.suggested_quantity}")
            if self.notifier:
                level = NotificationLevel.WARNING if ev.reason != TPSLReason.STOP_LOSS else NotificationLevel.ERROR
                self._notify(NotificationType.TPSL, level,
                             f"{ev.symbol} {ev.reason.value}",
                             f"{ev.reason.value} | 盈亏 {ev.pnl_pct:.2%} | 数量 {ev.suggested_quantity}",
                             symbol=ev.symbol, pnl_pct=ev.pnl_pct)

    async def _on_http_quote(self, quotes: Dict):
        for symbol, quote in quotes.items():
            price = quote.get('price', 0)
            if price > 0:
                self._last_quotes[symbol] = quote
                self.broker.update_price(symbol, price)
                self._eval_tpsl(symbol, price)

                for strategy in self.strategies:
                    if symbol in strategy.symbols:
                        signal = strategy.on_tick(symbol, quote)
                        if signal:
                            self._signal_count += 1
                            await self._strategy_signals.put((signal, time.perf_counter()))

    def _on_strategy_signal(self, signal: TradingSignal):
        pass

    async def _process_signals(self):
        while self._running:
            try:
                signal, quote_time = await asyncio.wait_for(
                    self._strategy_signals.get(), timeout=0.5
                )
                t0 = time.perf_counter()

                if not self.config.get('trading.auto_trade', False):
                    self._print_signal(signal)
                    continue

                await self._execute_signal(signal)

                elapsed = (time.perf_counter() - t0) * 1000
                total_ms = (time.perf_counter() - quote_time) * 1000
                self._record_latency('signal_to_order', elapsed)
                self._record_latency('total_pipeline', total_ms)

                self._strategy_signals.task_done()

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[Engine] 信号处理异常: {e}")

    async def _process_tpsl(self):
        while self._running:
            try:
                ev: TPSLEvent = await asyncio.wait_for(
                    self._tpsl_events.get(), timeout=0.5
                )
                if not self.config.get('trading.auto_trade', False):
                    continue
                await self._execute_tpsl(ev)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[Engine] TPSL处理异常: {e}")

    async def _execute_tpsl(self, ev: TPSLEvent):
        if ev.suggested_quantity <= 0:
            return
        success, order_id, order = self.broker.sell(
            ev.symbol, ev.suggested_quantity, ev.current_price,
            f"TPSL:{ev.reason.value} pnl={ev.pnl_pct:.2%}",
        )
        if success:
            print(f"[TPSL-EXEC] 卖出 {ev.symbol} {order.filled_quantity}股 @ {ev.current_price:.2f} | {ev.reason.value}")
            self.state_manager.save_trade({
                'order_id': order_id,
                'symbol': ev.symbol,
                'direction': 'sell',
                'quantity': order.filled_quantity,
                'price': ev.current_price,
                'amount': ev.current_price * order.filled_quantity,
                'commission': order.commission,
                'stamp_tax': order.stamp_tax,
                'pnl': (ev.current_price - ev.entry_price) * order.filled_quantity,
                'strategy': 'TPSLMonitor',
                'reason': f'TPSL:{ev.reason.value}',
            })
            remaining = self.broker.get_position_quantity(ev.symbol)
            if remaining and remaining > 0:
                self.tpsl_monitor.update_position_qty(ev.symbol, remaining)
            else:
                self.tpsl_monitor.unregister_position(ev.symbol)

    async def _execute_signal(self, signal: TradingSignal):
        symbol = signal.symbol
        price = signal.price
        reason = signal.reason

        if signal.signal_type == SignalType.BUY:
            quantity = signal.quantity or self._calc_buy_qty(symbol, price)
            account = self.broker.get_account_info()
            positions = self.broker.get_positions()
            current_symbol_value = 0.0
            for position in positions:
                position_symbol = position.get('symbol') if isinstance(position, dict) else getattr(position, 'symbol', '')
                if position_symbol == symbol:
                    current_symbol_value = float(
                        position.get('market_value', 0) if isinstance(position, dict)
                        else getattr(position, 'market_value', 0)
                    )
                    break
            self.risk_manager.check_drawdown(float(account.get('total_asset', 0) or 0))
            order_req = OrderRequest(
                symbol=symbol, side=OrderSide.BUY, quantity=quantity, price=price,
                portfolio_value=account.get('total_asset', 0),
                current_position_value=account.get('market_value', 0),
                current_symbol_value=current_symbol_value,
                reason=reason,
            )
            risk = self.risk_manager.check_order(order_req)
            if not risk.allowed:
                print(f"[RISK-BLOCK] 买入 {symbol} 被风控拒绝: {risk.reason}")
                self._notify(NotificationType.RISK, NotificationLevel.WARNING,
                             f"风控拦截: {symbol}", risk.reason, symbol=symbol)
                return
            if risk.suggested_quantity and risk.suggested_quantity < quantity:
                quantity = risk.suggested_quantity
                print(f"[RISK-ADJ] 买入 {symbol} 数量调整为 {quantity} (风控建议)")

            success, order_id, order = self.broker.buy(
                symbol, quantity, price, reason
            )
            if success:
                print(f"[EXEC] 买入 {symbol} {quantity}股 @ {price:.2f} | {order.latency_ms:.2f}ms | {order_id}")
                self.state_manager.save_trade({
                    'order_id': order_id,
                    'symbol': symbol,
                    'direction': 'buy',
                    'quantity': quantity,
                    'price': price,
                    'amount': price * quantity,
                    'commission': order.commission,
                    'strategy': self.strategies[0].name if self.strategies else '',
                    'reason': reason,
                })
                self.risk_manager.record_order({
                    'symbol': symbol, 'side': 'buy', 'quantity': quantity,
                    'price': price, 'reason': reason,
                })
                self.tpsl_monitor.register_position(symbol, price, quantity)
                self._notify(NotificationType.TRADE, NotificationLevel.SUCCESS,
                             f"买入 {symbol}", f"¥{price:.2f} x {quantity}股 | {reason}",
                             symbol=symbol, price=price, quantity=quantity)
            else:
                print(f"[EXEC] 买入失败 {symbol}: {order_id}")
                self._notify(NotificationType.ERROR, NotificationLevel.ERROR,
                             f"买入失败 {symbol}", str(order_id), symbol=symbol)

        elif signal.signal_type == SignalType.SELL:
            success, order_id, order = self.broker.sell(
                symbol, 0, price, reason
            )
            if success:
                print(f"[EXEC] 卖出 {symbol} {order.filled_quantity}股 @ {price:.2f} | {order.latency_ms:.2f}ms | {order_id}")
                self.state_manager.save_trade({
                    'order_id': order_id,
                    'symbol': symbol,
                    'direction': 'sell',
                    'quantity': order.filled_quantity,
                    'price': price,
                    'amount': price * order.filled_quantity,
                    'commission': order.commission,
                    'stamp_tax': order.stamp_tax,
                    'pnl': 0,
                    'strategy': self.strategies[0].name if self.strategies else '',
                    'reason': reason,
                })
                self._notify(NotificationType.TRADE, NotificationLevel.SUCCESS,
                             f"卖出 {symbol}", f"¥{price:.2f} x {order.filled_quantity}股 | {reason}",
                             symbol=symbol, price=price, quantity=order.filled_quantity)
            else:
                print(f"[EXEC] 卖出失败 {symbol}: {order_id}")
                self._notify(NotificationType.ERROR, NotificationLevel.ERROR,
                             f"卖出失败 {symbol}", str(order_id), symbol=symbol)

        self.state_manager.save_signal(signal.to_dict())

    def _calc_buy_qty(self, symbol: str, price: float) -> int:
        account = self.broker.get_account_info()
        max_pos_pct = self.config.get('risk.max_position_size', 0.1)
        max_amount = account['total_asset'] * max_pos_pct
        qty = int(max_amount / price / 100) * 100
        return max(qty, 100)

    def _notify(self, ntype: NotificationType, level: NotificationLevel, title: str, message: str, **data):
        if self.notifier is None:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.notifier.notify(Notification(
                    type=ntype, level=level, title=title, message=message, data=data
                )))
            else:
                loop.run_until_complete(self.notifier.notify_sync(Notification(
                    type=ntype, level=level, title=title, message=message, data=data
                )))
        except RuntimeError:
            pass

    def _print_signal(self, signal: TradingSignal):
        print(f"\n[SIGNAL] {signal.signal_type.value.upper()} {signal.symbol} @ {signal.price:.2f}")
        print(f"  原因: {signal.reason}  置信度: {signal.confidence:.2%}")

    def _record_latency(self, stage: str, ms: float):
        if stage in self._latency_stats:
            self._latency_stats[stage].append(ms)
            if len(self._latency_stats[stage]) > self._max_latency_samples:
                self._latency_stats[stage] = self._latency_stats[stage][-self._max_latency_samples:]

    async def _http_polling_loop(self):
        symbols = self.config.get('trading.symbols', ['sh600000', 'sz000001'])
        interval = self.config.get('trading.update_interval', 3)
        sources = self.config.get('data_source.realtime_order',
                                  ['pytdx', 'eastmoney', 'sina', 'tencent'])

        while self._running:
            try:
                quotes = self.http_client.get_quotes(symbols, sources=sources)
                if quotes:
                    await self._on_http_quote(quotes)
            except Exception as e:
                pass
            await asyncio.sleep(interval)

    async def _news_loop(self):
        interval = self.config.get('trading.news_update_interval', 60)
        while self._running:
            try:
                news_list = self.news_analyzer.get_market_news(count=20)
                prof_news = self.news_analyzer.get_professional_news(count=10)
                all_news = news_list + prof_news
                if all_news:
                    await self.news_pipeline.push(all_news)
                self._feed_sentiment_to_ai()
            except Exception as e:
                pass
            await asyncio.sleep(interval)

    def _feed_sentiment_to_ai(self):
        """把每股新闻情感回灌给 AI 策略。best-effort,任何异常都吞掉。

        AIStrategy.set_sentiment 接收 {symbol: score(-1~1)}。情感来自
        NewsAnalyzer.analyze_realtime 产出的 symbol_sentiments(avg_score)。
        """
        ai_strategies = [s for s in self.strategies if hasattr(s, 'set_sentiment')]
        if not ai_strategies:
            return
        try:
            raw_items = self.news_analyzer.news_fetcher.fetch_all_news()
        except Exception:
            return
        if not raw_items:
            return
        try:
            result = self.news_analyzer.analyze_realtime(raw_items)
        except Exception:
            return
        sym_sent = result.get('symbol_sentiments', {}) if isinstance(result, dict) else {}
        sentiment_map = {
            sym: info.get('avg_score', 0)
            for sym, info in sym_sent.items()
            if isinstance(info, dict)
        }
        if sentiment_map:
            for strat in ai_strategies:
                strat.set_sentiment(sentiment_map)

    def _feed_risk_to_agents(self, account: Dict, risk_report: Dict):
        """把当前风险快照回灌给多智能体策略,供 AI 风控经理复审。best-effort。"""
        agent_strategies = [s for s in self.strategies if hasattr(s, 'set_risk_context')]
        if not agent_strategies:
            return
        total = account.get('total_asset', 0) or 1
        market_value = account.get('market_value', 0) or 0
        ctx = {
            'drawdown': risk_report.get('drawdown', 0.0),
            'daily_pnl': risk_report.get('daily_pnl', 0.0),
            'total_position_pct': (market_value / total) if total else 0.0,
        }
        for strat in agent_strategies:
            try:
                strat.set_risk_context(ctx)
            except Exception:
                pass

    def _on_breaking_news(self, news_data: Dict):
        """突发新闻回调: 通知多智能体策略对相关股票立即触发一次分析。best-effort。"""
        agent_strategies = [s for s in self.strategies if hasattr(s, 'set_breaking_news')]
        if not agent_strategies:
            return
        try:
            title = news_data.get('title', '') or news_data.get('news', {}).get('title', '')
            symbols = news_data.get('symbols', []) or news_data.get('news', {}).get('symbols', [])
        except Exception:
            return
        if not symbols:
            return
        for strat in agent_strategies:
            for sym in symbols:
                if sym in getattr(strat, 'symbols', []):
                    try:
                        strat.set_breaking_news(sym, title[:100])
                    except Exception:
                        pass

    async def _status_loop(self):
        await asyncio.sleep(5)
        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                break

            account = self.broker.get_account_info()
            latency = self.get_latency_summary()
            ws_latency = getattr(self.ws_client, 'latency_ms', 0)
            tpsl_stats = self.tpsl_monitor.get_stats()
            risk_report = self.risk_manager.get_risk_report()

            self._feed_risk_to_agents(account, risk_report)

            print(f"\n[STATUS] {datetime.now().strftime('%H:%M:%S')}")
            print(f"  总资产: ¥{account['total_asset']:,.2f} | 可用: ¥{account['cash']:,.2f}")
            print(f"  盈亏: ¥{account['profit']:,.2f} ({account['profit_pct']:.2f}%)")
            print(f"  WS延迟: {ws_latency:.1f}ms | 信号->订单: {latency.get('signal_to_order', 0):.1f}ms")
            print(f"  行情: {self._quote_count}条 | 信号: {self._signal_count}个")
            print(f"  风控: 回撤 {risk_report['drawdown']:.2%} | 日亏 ¥{risk_report['daily_pnl']:,.0f} | 今日单数 {risk_report['daily_order_count']}")
            print(f"  止盈止损: 监控 {tpsl_stats['active_positions']} 笔 | 累计触发 {tpsl_stats['total_triggered']} 次")

    def get_latency_summary(self) -> Dict:
        summary = {}
        for stage, samples in self._latency_stats.items():
            if samples:
                import numpy as np
                arr = np.array(samples)
                summary[stage] = round(float(np.mean(arr)), 2)
                summary[f'{stage}_p99'] = round(float(np.percentile(arr, 99)), 2)
        return summary

    async def start(self):
        self._running = True
        self._start_time = datetime.now()
        print(f"\n{'='*50}")
        print(f"  量化交易引擎 v2.0")
        print(f"  启动时间: {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*50}")

        self.setup_strategies()
        self.async_engine.start()
        if self.notifier:
            await self.notifier.start()

        symbols = self.config.get('trading.symbols', ['sh600000', 'sz000001'])
        self.ws_client.subscribe(symbols)
        self.ws_client.register_callback(self._on_ws_quote)

        self.news_pipeline.register_callback(self._on_breaking_news, NewsPriority.BREAKING)
        await self.news_pipeline.start()

        tasks = [
            asyncio.create_task(self._connect_ws(), name="ws-connect"),
            asyncio.create_task(self._process_signals(), name="signal-processor"),
            asyncio.create_task(self._process_tpsl(), name="tpsl-processor"),
            asyncio.create_task(self._http_polling_loop(), name="http-polling"),
            asyncio.create_task(self._news_loop(), name="news-poller"),
            asyncio.create_task(self._status_loop(), name="status-display"),
        ]

        print(f"[Engine] 核心组件已启动")
        print(f"[Engine] 策略: {self.strategies[0].name if self.strategies else 'None'}")
        print(f"[Engine] 行情: WebSocket(东方财富) + HTTP轮询(备用)")
        print(f"[Engine] 按 Ctrl+C 停止\n")

        await self._wait_for_shutdown()

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self.news_pipeline.stop()
        if self.notifier:
            await self.notifier.stop()
        self._cleanup()

    async def _connect_ws(self):
        try:
            await self.ws_client.start()
        except ImportError:
            print("[Engine] websockets库未安装，使用HTTP轮询模式")
            print("[Engine] 安装: pip install websockets")
        except Exception as e:
            print(f"[Engine] WebSocket连接失败: {e}")

    async def _wait_for_shutdown(self):
        while self._running and not self._shutdown_requested:
            await asyncio.sleep(1)

    def stop(self):
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self._running = False

    def _cleanup(self):
        elapsed = (datetime.now() - self._start_time).total_seconds() if self._start_time else 0
        account = self.broker.get_account_info()
        latency = self.get_latency_summary()

        self.broker.sync_to_db(self.state_manager)
        self.state_manager.update_daily_summary()

        print(f"\n{'='*50}")
        print(f"  引擎停止报告")
        print(f"{'='*50}")
        print(f"  运行时间: {elapsed:.0f}秒")
        print(f"  处理行情: {self._quote_count}条")
        print(f"  生成信号: {self._signal_count}个")
        print(f"  执行交易: {account['trade_count']}笔")
        print(f"  总资产: ¥{account['total_asset']:,.2f} -> ¥{account['total_asset']:,.2f}")
        print(f"  盈亏: ¥{account['profit']:,.2f} ({account['profit_pct']:.2f}%)")
        print(f"  信号处理延迟: {latency.get('signal_to_order', 0):.1f}ms (平均)")
        print(f"  总流水线延迟: {latency.get('total_pipeline', 0):.1f}ms (平均)")
        print(f"{'='*50}")

        self.async_engine.stop()
        self.state_manager.close()


def run_engine(config_path: str = "config/config.yaml"):
    engine = TradingEngine(config_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler():
        print("\n\n正在停止引擎...")
        engine.stop()

    try:
        if sys.platform == 'win32':
            loop.run_until_complete(engine.start())
        else:
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)
            loop.run_until_complete(engine.start())
    except KeyboardInterrupt:
        engine.stop()
    finally:
        try:
            loop.run_until_complete(asyncio.sleep(0.5))
        except Exception:
            pass
        loop.close()


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config/config.yaml"
    run_engine(config_file)
