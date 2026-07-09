"""
风控经理 - 多智能体系统的第四层(抄袭 TradingAgents 的 Risk Management / Portfolio Manager)

复审交易员的提案:对照当前回撤、总仓位、日内亏损等风险上下文,决定
批准 / 否决 / 下调置信度。这是**决策层软风控**;引擎执行前还会再过一道
硬风控 RiskManager.check_order(双保险)。

离线时用规则:高回撤/高仓位时压制买入。
"""
from typing import Dict, Optional


class RiskManagerAgent:
    def __init__(self, llm=None, deep_model: Optional[str] = None,
                 max_drawdown: float = 0.20, max_total_position: float = 0.95):
        self.llm = llm
        self.deep_model = deep_model
        self.max_drawdown = max_drawdown
        self.max_total_position = max_total_position

    def review(self, symbol: str, trader_decision: Dict, risk_context: Dict) -> Dict:
        """返回最终决策 {action, confidence, reason, approved, source}"""
        drawdown = risk_context.get('drawdown', 0.0)
        total_pos_pct = risk_context.get('total_position_pct', 0.0)
        daily_pnl = risk_context.get('daily_pnl', 0.0)

        online = self.llm is not None and self.llm.is_available()
        if online:
            out = self._review_via_llm(symbol, trader_decision, risk_context)
            if out:
                return out
        return self._rule_review(symbol, trader_decision, drawdown, total_pos_pct, daily_pnl)

    def _review_via_llm(self, symbol, decision, ctx) -> Optional[Dict]:
        sys = ('你是风控经理,需复审交易员提案。只输出JSON: '
               '{"approved":true/false,"action":"buy/sell/hold","confidence":0到1小数,'
               '"reason":"不超过80字中文"}。原则:回撤大、仓位高、日内亏损大时,'
               '应否决或下调买入;卖出降风险的请求可放行。')
        user = (f"股票: {symbol}\n交易员提案: {decision}\n"
                f"当前回撤={ctx.get('drawdown',0):.2%}, 总仓位={ctx.get('total_position_pct',0):.2%}, "
                f"今日盈亏={ctx.get('daily_pnl',0):.0f}\n"
                f"回撤上限={self.max_drawdown:.0%}, 仓位上限={self.max_total_position:.0%}\n"
                f"请复审并输出JSON。")
        parsed = self.llm.chat_json(sys, user, model=self.deep_model)
        if not parsed:
            return None
        approved = bool(parsed.get('approved', True))
        action = str(parsed.get('action', decision.get('action', 'hold'))).lower()
        if action not in ('buy', 'sell', 'hold'):
            action = 'hold'
        if not approved and action == 'buy':
            action = 'hold'
        try:
            conf = max(0.0, min(1.0, float(parsed.get('confidence', decision.get('confidence', 0)))))
        except (ValueError, TypeError):
            conf = decision.get('confidence', 0.0)
        return {'action': action, 'confidence': round(conf, 3),
                'reason': f"[风控经理] {str(parsed.get('reason',''))[:150]}",
                'approved': approved, 'source': 'llm'}

    def _rule_review(self, symbol, decision, drawdown, total_pos_pct, daily_pnl) -> Dict:
        action = decision.get('action', 'hold')
        conf = decision.get('confidence', 0.0)
        approved = True
        notes = []

        # 硬性风险线:买入时若回撤或仓位逼近上限,否决买入
        if action == 'buy':
            if drawdown >= self.max_drawdown:
                approved = False
                notes.append(f"回撤{drawdown:.1%}达上限,否决买入")
            elif total_pos_pct >= self.max_total_position:
                approved = False
                notes.append(f"仓位{total_pos_pct:.1%}达上限,否决买入")
            elif drawdown >= self.max_drawdown * 0.7:
                conf *= 0.6
                notes.append(f"回撤偏高,下调置信度")

        final_action = action if approved else 'hold'
        reason = f"[风控经理·规则] " + ("; ".join(notes) if notes else "提案通过")
        return {'action': final_action, 'confidence': round(conf, 3),
                'reason': reason, 'approved': approved, 'source': 'fallback'}
