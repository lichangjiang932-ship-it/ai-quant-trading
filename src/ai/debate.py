# -*- coding: utf-8 -*-
"""
多空辩论决策 (Bull-Bear Debate) — 轻量版
===========================================
借鉴 TradingAgents-AShare 的 14-Agent 辩论框架, 简化为三方 LLM 对抗:
  1. 多头研究员 (Bull): 找看多理由, 打分 0-100
  2. 空头研究员 (Bear): 找看空理由, 打分 0-100
  3. 风控官 (Risk): 综合评估风险, 给出 verdict (allow/caution/block) 与仓位建议

输出汇入自托管综合分:
  debate_adj = (bull_score - bear_score) / 100 * 12  (−12 ~ +12)
  risk block → 否决
  caution → 综合分 -4

设计约束:
  - 只在通过初筛的候选上调用 (每轮最多 3 只), 控制 LLM 成本
  - 同股票同交易日缓存
  - LLM 不可用/解析失败 → 返回 None, 不影响主流程
"""
import time
from datetime import date
from typing import Dict, Optional

_BULL_SYSTEM = (
    "你是A股多头研究员。给定标的数据, 从趋势、量价、资金、消息、基本面中找"
    "看多依据并给出0-100的多头强度分。只输出JSON: {\"score\": int, \"reasons\": [\"...\"], \"summary\": \"...\"}"
)
_BEAR_SYSTEM = (
    "你是A股空头研究员。给定标的数据, 从趋势、量价、资金、消息、基本面中找"
    "看空依据并给出0-100的空头强度分。只输出JSON: {\"score\": int, \"reasons\": [\"...\"], \"summary\": \"...\"}"
)
_RISK_SYSTEM = (
    "你是交易风控官。综合多空观点与市场环境, 判断是否允许开仓。"
    "只输出JSON: {\"verdict\": \"allow|caut ion|block\", \"risk_note\": \"...\", \"suggest_position_pct\": 0-1}"
).replace("caut ion", "caution")

_cache: Dict[str, Dict] = {}
_cache_date = ""


def _get_client():
    try:
        from .llm_client import LLMClient
        return LLMClient()
    except Exception:
        return None


def _build_prompt(symbol: str, name: str, ctx: Dict) -> str:
    """构造辩论输入上下文 (量化数据, 不包含 K 线原始序列)。"""
    lines = [
        f"标的: {name}({symbol})",
        f"现价 {ctx.get('price')} 涨跌 {ctx.get('change_pct')}%",
        f"机会评分 {ctx.get('score')} 置信度 {ctx.get('confidence')} 风险回报比 {ctx.get('risk_reward')}",
        f"趋势: {ctx.get('trend', '未知')}",
        f"资金: {ctx.get('capital', '未知')}",
    ]
    if ctx.get('news'):
        lines.append(f"新闻因子: {ctx.get('news')}")
    if ctx.get('wyckoff'):
        lines.append(f"量价阶段: {ctx.get('wyckoff')}")
    if ctx.get('win_rate') is not None:
        lines.append(f"历史相似机会胜率: {ctx.get('win_rate')}% (样本{ctx.get('samples', 0)})")
    return "\n".join(lines)


def run_debate(symbol: str, name: str, ctx: Dict, force: bool = False) -> Optional[Dict]:
    """三方辩论 → {adj, verdict, bull, bear, risk}。失败/无key返回 None。"""
    global _cache_date
    today = date.today().isoformat()
    if _cache_date != today:
        _cache.clear()
        _cache_date = today
    if not force and symbol in _cache:
        return _cache[symbol]

    client = _get_client()
    if client is None or not client.is_available():
        return None
    prompt = _build_prompt(symbol, name, ctx)
    try:
        bull = client.chat_json(_BULL_SYSTEM, prompt, temperature=0.4) or {}
        bear = client.chat_json(_BEAR_SYSTEM, prompt, temperature=0.4) or {}
        risk = client.chat_json(_RISK_SYSTEM, prompt, temperature=0.3) or {}
        bull_score = float(bull.get('score', 50) or 50)
        bear_score = float(bear.get('score', 50) or 50)
        verdict = str(risk.get('verdict', 'caution')).strip().lower()
        adj = round((bull_score - bear_score) / 100 * 12, 1)
        result = {
            "adj": adj,
            "verdict": verdict if verdict in ('allow', 'caution', 'block') else 'caution',
            "bull": {"score": round(bull_score, 0), "summary": str(bull.get('summary', ''))[:120]},
            "bear": {"score": round(bear_score, 0), "summary": str(bear.get('summary', ''))[:120]},
            "risk": {"note": str(risk.get('risk_note', ''))[:120],
                     "position_pct": float(risk.get('suggest_position_pct', 0.5) or 0.5)},
        }
        _cache[symbol] = result
        return result
    except Exception:
        return None
