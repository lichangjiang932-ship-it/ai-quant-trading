"""
编排器 - 把多智能体串成一条决策流水线(抄袭 TradingAgents 的 graph.propagate)

流程: 数据 -> 分析师团队(4) -> 多空辩论 -> 交易员 -> 风控经理 -> 最终决策
两档模型(仿 TradingAgents): quick_think 给分析师, deep_think 给辩论/交易/风控。
全程结构化返回,便于日志与面板展示「每个 agent 说了什么」。
离线(无 Key)时每一层都走规则兜底,永不崩。
"""
from typing import Dict, List, Optional

from .analysts import build_analysts
from .researchers import ResearchDebate
from .trader import Trader
from .risk_manager_agent import RiskManagerAgent


class AgentDecision:
    """多智能体最终决策 + 全部中间产物(便于展示与调试)"""

    def __init__(self, symbol: str, action: str, confidence: float, reason: str,
                 analyst_reports: List[Dict], debate: Dict, trader_decision: Dict,
                 risk_review: Dict):
        self.symbol = symbol
        self.action = action if action in ('buy', 'sell', 'hold') else 'hold'
        self.confidence = max(0.0, min(1.0, float(confidence)))
        self.reason = reason
        self.analyst_reports = analyst_reports
        self.debate = debate
        self.trader_decision = trader_decision
        self.risk_review = risk_review

    def to_dict(self) -> Dict:
        return {
            'symbol': self.symbol,
            'action': self.action,
            'confidence': round(self.confidence, 3),
            'reason': self.reason,
            'analysts': [
                {'name': r.get('name'), 'stance': r.get('stance'),
                 'score': r.get('score'), 'report': r.get('report')}
                for r in self.analyst_reports
            ],
            'debate': {
                'stance': self.debate.get('stance'),
                'score': self.debate.get('score'),
                'conclusion': self.debate.get('conclusion'),
            },
            'trader': self.trader_decision,
            'risk': self.risk_review,
        }

    def pretty(self) -> str:
        """多行中文报告,给控制台/日志展示。"""
        lines = [f"===== 多智能体决策: {self.symbol} ====="]
        lines.append("-- 分析师团队 --")
        for r in self.analyst_reports:
            lines.append(f"  [{r.get('name')}] {r.get('stance')}({r.get('score'):+.2f}): "
                         f"{str(r.get('report',''))[:100]}")
        lines.append("-- 多空辩论 --")
        lines.append(f"  多头: {str(self.debate.get('bull',''))[:100]}")
        lines.append(f"  空头: {str(self.debate.get('bear',''))[:100]}")
        lines.append(f"  研究经理: {str(self.debate.get('conclusion',''))[:120]}")
        lines.append("-- 交易员 --")
        td = self.trader_decision
        lines.append(f"  {td.get('action')} conf={td.get('confidence')} "
                     f"目标仓位={td.get('target_pct')}% | {str(td.get('reason',''))[:80]}")
        lines.append("-- 风控经理 --")
        lines.append(f"  {self.risk_review.get('reason','')}")
        lines.append(f"===> 最终: {self.action.upper()} (置信度 {self.confidence:.0%})")
        return "\n".join(lines)


class TradingAgentsGraph:
    """多智能体编排图"""

    def __init__(self, llm=None, config: Optional[Dict] = None,
                 kline_provider=None, news_analyzer=None, fundamentals=None,
                 capital_provider=None, memory=None):
        cfg = config or {}
        self.llm = llm
        self.quick_model = cfg.get('quick_think_model')
        self.deep_model = cfg.get('deep_think_model')
        self.memory = memory
        self.use_memory = cfg.get('use_memory', True)

        analyst_names = cfg.get('analysts',
                                ['technical', 'sentiment', 'news', 'fundamentals', 'capital'])
        self.analysts = build_analysts(
            analyst_names, llm=llm, model=self.quick_model,
            kline_provider=kline_provider, news_analyzer=news_analyzer,
            fundamentals=fundamentals, capital=capital_provider,
        )
        self.debate = ResearchDebate(llm=llm, deep_model=self.deep_model,
                                     max_rounds=cfg.get('max_debate_rounds', 1))
        self.trader = Trader(llm=llm, deep_model=self.deep_model)
        self.risk_agent = RiskManagerAgent(
            llm=llm, deep_model=self.deep_model,
            max_drawdown=cfg.get('max_drawdown', 0.20),
            max_total_position=cfg.get('max_total_position', 0.95),
        )

    def analyze(self, symbol: str, context: Optional[Dict] = None) -> AgentDecision:
        """对单只股票跑完整多智能体流水线,返回结构化决策。"""
        context = context or {}

        # 反思记忆(该股历史经验)
        memory_text = ''
        if self.use_memory and self.memory is not None:
            try:
                memory_text = self.memory.get_recent(symbol, limit=3)
            except Exception:
                memory_text = ''

        # 1) 分析师团队
        reports = []
        for analyst in self.analysts:
            try:
                reports.append(analyst.analyze(symbol, context))
            except Exception as e:
                reports.append({'name': getattr(analyst, 'name', 'analyst'),
                                'stance': 'neutral', 'score': 0.0,
                                'report': f'(分析失败: {e})', 'facts': ''})

        # 2) 多空辩论 + 研究经理
        debate = self.debate.run(symbol, reports, memory_text=memory_text)

        # 3) 交易员
        trader_decision = self.trader.decide(symbol, reports, debate, context,
                                             memory_text=memory_text)

        # 4) 风控经理复审
        risk_context = context.get('risk', {})
        risk_review = self.risk_agent.review(symbol, trader_decision, risk_context)

        final_action = risk_review.get('action', 'hold')
        final_conf = risk_review.get('confidence', trader_decision.get('confidence', 0.0))
        final_reason = (f"{trader_decision.get('reason','')} || {risk_review.get('reason','')}")

        return AgentDecision(
            symbol=symbol,
            action=final_action,
            confidence=final_conf,
            reason=final_reason[:300],
            analyst_reports=reports,
            debate=debate,
            trader_decision=trader_decision,
            risk_review=risk_review,
        )

    def status(self) -> Dict:
        return {
            'analysts': [a.name for a in self.analysts],
            'llm_available': self.llm.is_available() if self.llm else False,
            'use_memory': self.use_memory,
            'quick_model': self.quick_model,
            'deep_model': self.deep_model,
        }
