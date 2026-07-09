"""
交易员 - 多智能体系统的第三层(抄袭 TradingAgents 的 Trader)

综合 4 份分析师报告 + 研究员辩论结论 + 反思记忆,组织成结构化交易决策:
{action(buy/sell/hold), confidence(0~1), target_pct(建议仓位%), reason(中文)}

离线时基于研究员评分 + 是否持仓,用确定性规则给决策。
"""
from typing import Dict, List, Optional


class Trader:
    def __init__(self, llm=None, deep_model: Optional[str] = None):
        self.llm = llm
        self.deep_model = deep_model

    def decide(self, symbol: str, analyst_reports: List[Dict], debate: Dict,
               context: Dict, memory_text: str = '') -> Dict:
        score = debate.get('score', 0.0)
        has_position = bool(context.get('position'))

        online = self.llm is not None and self.llm.is_available()
        if online:
            parsed = self._decide_via_llm(symbol, analyst_reports, debate, context, memory_text)
            if parsed:
                return parsed
        return self._rule_decide(symbol, score, has_position, debate)

    def _decide_via_llm(self, symbol, reports, debate, context, memory_text) -> Optional[Dict]:
        sys = ('你是交易员,需综合分析师报告、多空辩论结论与历史经验,给出最终交易决策。'
               '只输出JSON: {"action":"buy/sell/hold","confidence":0到1小数,'
               '"target_pct":建议仓位百分比0到100的数,"reason":"不超过100字中文理由"}。'
               '决策要稳健:信息矛盾或不足时倾向hold。')
        summary = "\n".join(
            f"- {r.get('name')}: {r.get('stance')} {r.get('score'):+.2f} | {r.get('report','')[:100]}"
            for r in reports
        )
        pos = context.get('position') or 0
        mem = f"\n历史经验:\n{memory_text}" if memory_text else ''
        user = (f"股票: {symbol}\n当前持仓: {pos} 股\n"
                f"分析师报告:\n{summary}\n\n"
                f"研究经理结论: {debate.get('conclusion','')} (评分{debate.get('score',0):+.2f})\n"
                f"多头: {debate.get('bull','')[:150]}\n空头: {debate.get('bear','')[:150]}{mem}\n\n"
                f"请输出交易决策JSON。")
        parsed = self.llm.chat_json(sys, user, model=self.deep_model)
        if not parsed:
            return None
        return self._normalize(parsed, source='llm')

    @staticmethod
    def _normalize(parsed: Dict, source: str) -> Dict:
        action = str(parsed.get('action', 'hold')).lower().strip()
        if action not in ('buy', 'sell', 'hold'):
            action = 'hold'
        try:
            conf = max(0.0, min(1.0, float(parsed.get('confidence', 0.0))))
        except (ValueError, TypeError):
            conf = 0.0
        try:
            target = max(0.0, min(100.0, float(parsed.get('target_pct', 0))))
        except (ValueError, TypeError):
            target = 0.0
        return {
            'action': action,
            'confidence': round(conf, 3),
            'target_pct': round(target, 1),
            'reason': str(parsed.get('reason', ''))[:200],
            'source': source,
        }

    def _rule_decide(self, symbol, score, has_position, debate) -> Dict:
        action, conf, target = 'hold', min(abs(score), 0.5), 0.0
        if score > 0.2 and not has_position:
            action, conf, target = 'buy', min(abs(score), 1.0), min(abs(score) * 100, 30)
        elif score < -0.15 and has_position:
            action, conf, target = 'sell', min(abs(score), 1.0), 0.0
        reason = f"[交易员·规则] 研究评分{score:+.2f} -> {action}。{debate.get('conclusion','')[:60]}"
        return {'action': action, 'confidence': round(conf, 3),
                'target_pct': round(target, 1), 'reason': reason, 'source': 'fallback'}
