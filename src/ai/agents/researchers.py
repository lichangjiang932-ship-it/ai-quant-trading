"""
研究员辩论 - 多智能体系统的第二层(抄袭 TradingAgents 的 Researcher Team)

多头研究员 vs 空头研究员就分析师报告展开 max_debate_rounds 轮辩论,各自反驳对方;
研究经理综合辩论,给出「看多/看空/中性 + 依据」。

离线时用分析师评分的加权规则模拟辩论结论,保证不崩。
"""
from typing import Dict, List, Optional


class ResearchDebate:
    def __init__(self, llm=None, deep_model: Optional[str] = None, max_rounds: int = 1):
        self.llm = llm
        self.deep_model = deep_model
        self.max_rounds = max(0, int(max_rounds))

    def run(self, symbol: str, analyst_reports: List[Dict], memory_text: str = '') -> Dict:
        """返回 {stance, score, bull, bear, conclusion, transcript}"""
        avg_score = (sum(r.get('score', 0) for r in analyst_reports) / len(analyst_reports)
                     if analyst_reports else 0.0)

        summary = self._format_reports(analyst_reports)
        transcript: List[str] = []

        online = self.llm is not None and self.llm.is_available()

        if not online:
            return self._rule_conclusion(symbol, analyst_reports, avg_score, summary)

        bull_prompt_sys = ('你是多头研究员,任务是从给定分析中挖掘看多理由,并反驳看空观点。'
                           '务必基于数据,不超过150字。')
        bear_prompt_sys = ('你是空头研究员,任务是从给定分析中挖掘看空理由与风险,并反驳看多观点。'
                           '务必基于数据,不超过150字。')

        bull_text, bear_text = '', ''
        mem = f"\n历史经验教训(供参考):\n{memory_text}" if memory_text else ''
        for i in range(max(1, self.max_rounds)):
            bull_user = (f"股票{symbol}的分析师报告:\n{summary}{mem}\n\n"
                         f"对手(空头)上一轮观点: {bear_text or '(暂无)'}\n请给出你的多头论证。")
            bull_text = self.llm.chat(bull_prompt_sys, bull_user, model=self.deep_model,
                                      fallback='(多头:数据不足,暂持中性)')
            transcript.append(f"[多头·R{i+1}] {bull_text}")

            bear_user = (f"股票{symbol}的分析师报告:\n{summary}{mem}\n\n"
                         f"对手(多头)本轮观点: {bull_text}\n请给出你的空头论证。")
            bear_text = self.llm.chat(bear_prompt_sys, bear_user, model=self.deep_model,
                                      fallback='(空头:数据不足,暂持中性)')
            transcript.append(f"[空头·R{i+1}] {bear_text}")

        # 研究经理综合
        mgr_sys = ('你是研究经理,需在多空辩论后给出最终研究结论。只输出JSON: '
                   '{"stance":"bull/bear/neutral","score":-1到1的小数,"conclusion":"不超过100字中文"}。')
        mgr_user = (f"多头观点:\n{bull_text}\n\n空头观点:\n{bear_text}\n\n"
                    f"分析师平均评分={avg_score:+.2f}。请给出研究结论JSON。")
        parsed = self.llm.chat_json(mgr_sys, mgr_user, model=self.deep_model)

        if not parsed:
            return self._rule_conclusion(symbol, analyst_reports, avg_score, summary,
                                         bull=bull_text, bear=bear_text, transcript=transcript)

        score = parsed.get('score', avg_score)
        try:
            score = max(-1.0, min(1.0, float(score)))
        except (ValueError, TypeError):
            score = avg_score
        stance = str(parsed.get('stance', '')).lower()
        if stance not in ('bull', 'bear', 'neutral'):
            stance = 'bull' if score > 0.15 else ('bear' if score < -0.15 else 'neutral')

        return {
            'stance': stance,
            'score': round(score, 3),
            'bull': bull_text,
            'bear': bear_text,
            'conclusion': str(parsed.get('conclusion', ''))[:200],
            'transcript': transcript,
        }

    @staticmethod
    def _format_reports(reports: List[Dict]) -> str:
        lines = []
        for r in reports:
            lines.append(f"- {r.get('name')}: 立场={r.get('stance')} 评分={r.get('score'):+.2f} | {r.get('report','')[:120]}")
        return "\n".join(lines) if lines else "(无分析师报告)"

    def _rule_conclusion(self, symbol, reports, avg_score, summary,
                         bull='', bear='', transcript=None) -> Dict:
        stance = 'bull' if avg_score > 0.15 else ('bear' if avg_score < -0.15 else 'neutral')
        stance_cn = {'bull': '看多', 'bear': '看空', 'neutral': '中性'}[stance]
        bulls = [r['name'] for r in reports if r.get('stance') == 'bull']
        bears = [r['name'] for r in reports if r.get('stance') == 'bear']
        conclusion = (f"[研究经理·规则] 综合评分{avg_score:+.2f},结论{stance_cn}。"
                      f"看多方: {', '.join(bulls) or '无'}; 看空方: {', '.join(bears) or '无'}。")
        return {
            'stance': stance,
            'score': round(avg_score, 3),
            'bull': bull or f"多头依据: {', '.join(bulls) or '缺乏明显多头信号'}",
            'bear': bear or f"空头依据: {', '.join(bears) or '缺乏明显空头信号'}",
            'conclusion': conclusion,
            'transcript': transcript or [],
        }
