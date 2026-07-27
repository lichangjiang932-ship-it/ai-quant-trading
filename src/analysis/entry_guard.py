"""AI 选股的盘中入场门禁、费用后情景与推荐价复盘。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np


@dataclass(frozen=True)
class EntryGuardConfig:
    max_day_gain_pct: float = 4.5
    min_day_change_pct: float = -2.5
    max_open_gap_pct: float = 3.0
    max_pullback_from_high_pct: float = 2.2
    max_positive_drift_pct: float = 1.5
    max_negative_drift_pct: float = 2.5
    min_net_target_return_pct: float = 2.0
    min_validation_samples: int = 3
    min_validation_win_rate: float = 45.0
    min_validation_avg_return: float = 0.0
    max_signal_age_seconds: int = 1800
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    min_commission: float = 5.0
    slippage: float = 0.0001


class EntryGuard:
    def __init__(self, config: Optional[EntryGuardConfig] = None):
        self.config = config or EntryGuardConfig()

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
        return max(min(float(value), high), low)

    @staticmethod
    def _number(value, default: float = 0.0) -> float:
        try:
            return float(value) if value not in (None, "", "-") else default
        except (TypeError, ValueError):
            return default

    def intraday_snapshot(self, quote: Optional[Dict]) -> Dict:
        quote = quote or {}
        price = self._number(quote.get("price"))
        pre_close = self._number(quote.get("pre_close"))
        change_pct = self._number(quote.get("change_pct"))
        if pre_close <= 0 and price > 0 and change_pct > -99:
            pre_close = price / max(1 + change_pct / 100, 0.01)
        open_price = self._number(quote.get("open"))
        high = self._number(quote.get("high"))
        low = self._number(quote.get("low"))
        if change_pct == 0 and price > 0 and pre_close > 0:
            change_pct = (price / pre_close - 1) * 100
        open_gap_pct = (open_price / pre_close - 1) * 100 if open_price > 0 and pre_close > 0 else 0.0
        from_open_pct = (price / open_price - 1) * 100 if price > 0 and open_price > 0 else 0.0
        pullback_pct = (high - price) / high * 100 if high > 0 and price > 0 else 0.0
        range_position_pct = (
            (price - low) / (high - low) * 100
            if high > low > 0 and price > 0
            else 50.0
        )
        complete = all(value > 0 for value in (price, pre_close, open_price, high, low))
        fading = (
            pullback_pct >= self.config.max_pullback_from_high_pct
            and (price < open_price or range_position_pct < 45)
        )
        return {
            "complete": complete,
            "price": round(price, 4),
            "pre_close": round(pre_close, 4),
            "open": round(open_price, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "day_change_pct": round(change_pct, 4),
            "open_gap_pct": round(open_gap_pct, 4),
            "from_open_pct": round(from_open_pct, 4),
            "pullback_from_high_pct": round(pullback_pct, 4),
            "range_position_pct": round(range_position_pct, 2),
            "fading": fading,
            "quote_time": str(quote.get("time") or quote.get("datetime") or ""),
            "quote_source": str(quote.get("data_source") or ""),
        }

    def prescreen_allowed(self, quote: Optional[Dict]) -> bool:
        snapshot = self.intraday_snapshot(quote)
        return bool(
            snapshot["complete"]
            and self.config.min_day_change_pct <= snapshot["day_change_pct"] <= self.config.max_day_gain_pct
            and snapshot["open_gap_pct"] <= self.config.max_open_gap_pct
            and not snapshot["fading"]
        )

    def build_research_snapshot(
        self,
        news_items: Optional[List[Dict]] = None,
        fundamentals: Optional[Dict] = None,
        capital_flow: Optional[Dict] = None,
        market_regime: Optional[Dict] = None,
        average_amount: float = 0.0,
        source_status: Optional[Dict] = None,
    ) -> Dict:
        source_status = source_status or {}
        news_score = 50.0
        news_weight = 0.0
        news_total = 0.0
        severe_negative_news = False
        headlines = []
        for item in news_items or []:
            sentiment = item.get("sentiment") or {}
            score = self._number(sentiment.get("score"))
            if abs(score) > 1:
                score /= 100
            score = max(min(score, 1), -1)
            importance = max(self._number(sentiment.get("importance"), 5), 1)
            news_total += score * importance
            news_weight += importance
            title = str((item.get("news") or {}).get("title") or item.get("title") or "").strip()
            if title and len(headlines) < 3:
                headlines.append(title)
            severe_negative_news = severe_negative_news or (score <= -0.6 and importance >= 7)
        news_coverage = (source_status.get("news") or {}).get("available")
        news_available = bool(news_coverage) if news_coverage is not None else news_weight > 0
        if news_weight > 0:
            news_score = self._clamp(50 + news_total / news_weight * 42)

        fundamentals = fundamentals or {}
        fundamental_score = 50.0
        fundamental_observed = 0
        fundamental_deteriorating = False
        roe = self._number(fundamentals.get("roe"), np.nan)
        profit_yoy = self._number(fundamentals.get("profit_yoy"), np.nan)
        revenue_yoy = self._number(fundamentals.get("revenue_yoy"), np.nan)
        pe = self._number(fundamentals.get("pe_ttm"), np.nan)
        if not np.isfinite(pe):
            pe = self._number(fundamentals.get("pe"), np.nan)
        if np.isfinite(roe):
            fundamental_observed += 1
            fundamental_score += 14 if roe >= 15 else 7 if roe >= 8 else -20 if roe < 0 else -4
            fundamental_deteriorating = fundamental_deteriorating or roe < 0
        if np.isfinite(profit_yoy):
            fundamental_observed += 1
            fundamental_score += 13 if profit_yoy >= 20 else 5 if profit_yoy >= 0 else -18 if profit_yoy <= -30 else -8
            fundamental_deteriorating = fundamental_deteriorating or profit_yoy <= -30
        if np.isfinite(revenue_yoy):
            fundamental_observed += 1
            fundamental_score += 8 if revenue_yoy >= 10 else 3 if revenue_yoy >= 0 else -10 if revenue_yoy <= -20 else -5
        if np.isfinite(pe):
            fundamental_observed += 1
            fundamental_score += -12 if pe <= 0 else -8 if pe >= 80 else 4 if pe <= 35 else 0
        fundamental_score = self._clamp(fundamental_score)
        fundamental_available = fundamental_observed > 0

        capital_flow = capital_flow or {}
        capital_available = bool(capital_flow)
        total_main = self._number(capital_flow.get("total_main_net"))
        latest_main = self._number(capital_flow.get("last_main_net"))
        capital_score = 50.0
        if capital_available:
            ratio = total_main / max(float(average_amount or 0), 1.0)
            capital_score += float(np.tanh(ratio * 4) * 25)
            capital_score += 7 if latest_main > 0 else -7 if latest_main < 0 else 0
        capital_score = self._clamp(capital_score)

        market_regime = market_regime or {}
        market_code = str(market_regime.get("code") or "unknown")
        market_available = market_code != "unknown"
        market_score = self._number(market_regime.get("score"), 50)
        if market_code == "risk_off":
            market_score = min(market_score, 25)
        elif market_code == "risk_on":
            market_score = max(market_score, 70)
        market_score = self._clamp(market_score)

        available_map = {
            "news": news_available,
            "fundamental": fundamental_available,
            "capital": capital_available,
            "market": market_available,
        }
        scores_map = {
            "news": news_score,
            "fundamental": fundamental_score,
            "capital": capital_score,
            "market": market_score,
        }
        weights = {"news": 0.25, "fundamental": 0.25, "capital": 0.20, "market": 0.30}
        completeness = sum(1 for value in available_map.values() if value) / len(available_map)
        available_weight = sum(weights[key] for key, value in available_map.items() if value)
        evidence_score = (
            sum(scores_map[key] * weights[key] for key, value in available_map.items() if value)
            / available_weight
            if available_weight > 0
            else 0.0
        )
        research_skipped = bool(source_status) and all(
            bool((source_status.get(key) or {}).get("skipped"))
            for key in ("news", "fundamental", "capital")
        )
        label_map = {"news": "公告/研报", "fundamental": "财务指标", "capital": "资金流", "market": "大盘环境"}
        missing = [] if research_skipped else [label_map[key] for key, value in available_map.items() if not value]
        news_tone = "偏正面" if news_score >= 58 else "偏负面" if news_score < 42 else "中性"
        capital_tone = "净流入" if total_main > 0 else "净流出" if total_main < 0 else "无明显方向"
        return {
            "scores": {
                "news": round(news_score, 2),
                "fundamental": round(fundamental_score, 2),
                "capital": round(capital_score, 2),
                "market": round(market_score, 2),
            },
            "available": {
                "news": news_available,
                "fundamental": fundamental_available,
                "capital": capital_available,
                "market": market_available,
            },
            "summaries": {
                "news": (
                    f"消息情绪{news_tone}，有效样本 {len(news_items or [])} 条"
                    if news_items else
                    "公告/研报源覆盖正常，近期未检出关联材料"
                    if news_available else
                    "公告/研报源暂不可用"
                ),
                "fundamental": (
                    "，".join(
                        part for part in (
                            f"ROE {roe:.1f}%" if np.isfinite(roe) else "",
                            f"利润同比 {profit_yoy:+.1f}%" if np.isfinite(profit_yoy) else "",
                            f"营收同比 {revenue_yoy:+.1f}%" if np.isfinite(revenue_yoy) else "",
                        ) if part
                    ) or "有效财务指标不足"
                ),
                "capital": (
                    f"主力{capital_tone} {abs(total_main) / 10000:.0f} 万元"
                    + (f"（{capital_flow.get('source')}）" if capital_flow.get('source') else "")
                    if capital_available else "资金流数据暂不可用"
                ),
                "market": str(market_regime.get("label") or "市场环境未知"),
            },
            "headlines": headlines,
            "completeness": round(completeness, 4),
            "evidence_score": round(self._clamp(evidence_score), 2),
            "missing": missing,
            "skipped": research_skipped,
            "source_status": source_status,
            "severe_negative_news": severe_negative_news,
            "fundamental_deteriorating": fundamental_deteriorating,
            "allow_new_positions": bool(market_regime.get("allow_new_positions", market_code != "risk_off")),
            "market_code": market_code,
        }

    def _buy_cost(self, quantity: int, price: float) -> float:
        gross = max(int(quantity), 0) * max(float(price), 0)
        if gross <= 0:
            return 0.0
        commission = max(gross * self.config.commission_rate, self.config.min_commission)
        return gross + commission + gross * self.config.slippage

    def _sell_proceeds(self, quantity: int, price: float) -> float:
        gross = max(int(quantity), 0) * max(float(price), 0)
        if gross <= 0:
            return 0.0
        commission = max(gross * self.config.commission_rate, self.config.min_commission)
        taxes = gross * (self.config.stamp_tax_rate + self.config.slippage)
        return max(gross - commission - taxes, 0.0)

    def _break_even_price(self, quantity: int, entry_price: float) -> float:
        if quantity <= 0 or entry_price <= 0:
            return 0.0
        cost = self._buy_cost(quantity, entry_price)
        low = entry_price
        high = entry_price * 1.20
        for _ in range(40):
            middle = (low + high) / 2
            if self._sell_proceeds(quantity, middle) >= cost:
                high = middle
            else:
                low = middle
        return high

    @staticmethod
    def _signal_age_seconds(generated_at: Optional[str]) -> Optional[float]:
        if not generated_at:
            return None
        try:
            parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            return max((now - parsed).total_seconds(), 0)
        except (TypeError, ValueError):
            return None

    def evaluate(
        self,
        symbol: str,
        quote: Optional[Dict],
        opportunity: Optional[Dict],
        validation: Optional[Dict],
        research: Optional[Dict] = None,
        reference_price: float = 0.0,
        generated_at: Optional[str] = None,
        market_open: bool = True,
    ) -> Dict:
        opportunity = opportunity or {}
        validation = validation or {}
        research = research or {}
        intraday = self.intraday_snapshot(quote)
        price = float(intraday["price"] or 0)
        reference = float(reference_price or price or 0)
        quantity = max(int(opportunity.get("suggested_qty", 0) or 0), 0)
        buy_low = self._number(opportunity.get("buy_low"))
        buy_high = self._number(opportunity.get("buy_high"))
        stop_loss = self._number(opportunity.get("stop_loss"))
        target_price = self._number(opportunity.get("target_price"))
        price_drift_pct = (price / reference - 1) * 100 if price > 0 and reference > 0 else 0.0
        signal_age = self._signal_age_seconds(generated_at)
        live_price_available = price > 0
        target_profit = (
            self._sell_proceeds(quantity, target_price) - self._buy_cost(quantity, price)
            if live_price_available else 0.0
        )
        stop_loss_amount = (
            self._buy_cost(quantity, price) - self._sell_proceeds(quantity, stop_loss)
            if live_price_available else 0.0
        )
        invested = self._buy_cost(quantity, price) if live_price_available else 0.0
        target_net_return_pct = target_profit / invested * 100 if invested > 0 else 0.0
        break_even_price = self._break_even_price(quantity, price)
        retrospective_available = live_price_available and reference > 0 and quantity > 0
        retrospective_pnl = (
            self._sell_proceeds(quantity, price) - self._buy_cost(quantity, reference)
            if retrospective_available else 0.0
        )
        retrospective_cost = self._buy_cost(quantity, reference)
        retrospective_return = retrospective_pnl / retrospective_cost * 100 if retrospective_cost > 0 else 0.0

        blockers = []
        status = "eligible"
        if not intraday["complete"]:
            blockers.append("盘中开高低和昨收数据不完整，无法判断是否冲高回落")
            status = "quote_incomplete"
        elif intraday["day_change_pct"] > self.config.max_day_gain_pct:
            blockers.append(f"当日已上涨 {intraday['day_change_pct']:.2f}%，超过防追高上限 {self.config.max_day_gain_pct:.1f}%")
            status = "chasing"
        elif intraday["day_change_pct"] < self.config.min_day_change_pct:
            blockers.append(f"当日下跌 {abs(intraday['day_change_pct']):.2f}%，尚未出现止跌确认")
            status = "falling"
        if intraday["open_gap_pct"] > self.config.max_open_gap_pct:
            blockers.append(f"高开 {intraday['open_gap_pct']:.2f}%，开盘溢价过高")
            status = "gap_risk"
        if intraday["fading"]:
            blockers.append(f"股价较日内高点回落 {intraday['pullback_from_high_pct']:.2f}%，属于冲高回落")
            status = "fading"
        if buy_high > 0 and price > buy_high:
            blockers.append(f"现价 {price:.2f} 已高于计划买入上限 {buy_high:.2f}")
            status = "above_buy_zone"
        elif buy_low > 0 and price < buy_low:
            blockers.append(f"现价 {price:.2f} 跌破计划买入区下沿 {buy_low:.2f}，需等待重新企稳")
            status = "below_buy_zone"
        if stop_loss > 0 and price <= stop_loss:
            blockers.append(f"现价已触及止损位 {stop_loss:.2f}，原买入逻辑失效")
            status = "invalidated"
        if reference > 0 and price_drift_pct > self.config.max_positive_drift_pct:
            blockers.append(f"相对推荐价已上涨 {price_drift_pct:.2f}%，不再追价")
            status = "price_moved"
        elif reference > 0 and price_drift_pct < -self.config.max_negative_drift_pct:
            blockers.append(f"相对推荐价已下跌 {abs(price_drift_pct):.2f}%，先确认下跌原因而不是直接抄底")
            status = "thesis_weakened"
        if signal_age is not None and signal_age > self.config.max_signal_age_seconds:
            blockers.append(f"推荐已超过 {self.config.max_signal_age_seconds // 60} 分钟，必须重新分析")
            status = "stale"
        if not market_open:
            blockers.append("当前不是连续竞价时段，只能形成观察计划，开盘后必须重新取价")
            status = "market_closed"

        samples = int(validation.get("samples", 0) or 0)
        win_rate = self._number(validation.get("win_rate"))
        average_return = self._number(validation.get("avg_return"))
        validation_ok = (
            samples >= self.config.min_validation_samples
            and win_rate >= self.config.min_validation_win_rate
            and average_return > self.config.min_validation_avg_return
        )
        if not validation_ok:
            blockers.append("历史相似样本数量、胜率或平均收益不足以支持买入")
            status = "weak_history"
        if opportunity.get("action") != "buy" or quantity <= 0:
            blockers.append("潜力模型没有给出可执行买入数量")
            status = "no_plan"
        if target_price <= price or target_net_return_pct < self.config.min_net_target_return_pct:
            blockers.append(f"扣除费用后目标情景收益仅 {target_net_return_pct:.2f}%，安全边际不足")
            status = "insufficient_edge"

        scores = research.get("scores") or {}
        completeness = self._number(research.get("completeness"))
        executable_plan = opportunity.get("action") == "buy" and quantity > 0
        if executable_plan and completeness < 0.5:
            missing_text = "、".join(research.get("missing") or []) or "多方研究数据"
            blockers.append(f"研究覆盖不足一半，当前缺少：{missing_text}")
            status = "research_incomplete"
        if research.get("severe_negative_news"):
            blockers.append("存在高重要度负面消息，事件风险否决买入")
            status = "news_risk"
        if research.get("fundamental_deteriorating") and self._number(scores.get("fundamental"), 50) < 35:
            blockers.append("基本面出现明显恶化，不能只依据技术反弹买入")
            status = "fundamental_risk"
        if not bool(research.get("allow_new_positions", True)):
            blockers.append("当前大盘处于防守环境，暂停新开仓")
            status = "market_risk"
        if self._number(scores.get("capital"), 50) < 30 and intraday["fading"]:
            blockers.append("主力资金明显流出且价格冲高回落")
            status = "capital_outflow"

        blockers = list(dict.fromkeys(blockers))
        allowed = not blockers
        label_map = {
            "eligible": "当前仍可按计划买入",
            "quote_incomplete": "实时行情字段缺失，暂不下单",
            "chasing": "涨幅过大，取消追高",
            "falling": "尚未止跌，继续等待",
            "gap_risk": "高开溢价过高，取消追价",
            "fading": "冲高回落，推荐失效",
            "above_buy_zone": "现价超过买入上限",
            "below_buy_zone": "跌破计划区，等待企稳",
            "invalidated": "已触及止损位，逻辑失效",
            "price_moved": "已偏离推荐价，不追价",
            "thesis_weakened": "推荐后转弱，重新分析",
            "market_closed": "非交易时段，等待开盘复核",
            "stale": "推荐过期，重新选股",
            "weak_history": "历史样本验证未通过",
            "no_plan": "潜力评分未形成买入计划",
            "insufficient_edge": "费用后收益空间不足",
            "research_incomplete": "研究数据未取全，暂不下单",
            "news_risk": "负面事件否决",
            "fundamental_risk": "基本面恶化，否决买入",
            "market_risk": "大盘防守，暂停开仓",
            "capital_outflow": "资金流出且冲高回落",
        }
        warnings = ["任何 AI 选股都不能确保盈利；历史胜率和目标价只用于情景评估。"]
        if completeness < 1:
            warnings.append(f"多方证据完整度 {completeness * 100:.0f}%，缺失信息不会被当作利好。")
        return {
            "symbol": symbol,
            "allowed": allowed,
            "action": "buy" if allowed else "hold",
            "status": status,
            "label": label_map.get(status, "条件不足，继续观察" if not allowed else "当前仍可按计划买入"),
            "reasons": blockers,
            "warnings": warnings,
            "profit_guaranteed": False,
            "analysis_price": round(reference, 4),
            "current_price": round(price, 4),
            "price_drift_pct": round(price_drift_pct, 4),
            "signal_age_seconds": round(signal_age, 1) if signal_age is not None else None,
            "quantity": quantity,
            "buy_low": round(buy_low, 4),
            "buy_high": round(buy_high, 4),
            "break_even_price": round(break_even_price, 4),
            "target_scenario": {
                "price": round(target_price, 4),
                "net_profit": round(target_profit, 2),
                "net_return_pct": round(target_net_return_pct, 4),
            },
            "stop_scenario": {
                "price": round(stop_loss, 4),
                "net_loss": round(max(stop_loss_amount, 0), 2),
            },
            "if_bought_at_analysis": {
                "available": retrospective_available,
                "net_pnl": round(retrospective_pnl, 2),
                "net_return_pct": round(retrospective_return, 4),
                "profitable_now": retrospective_pnl > 0 if retrospective_available else None,
            },
            "historical": {
                "samples": samples,
                "win_rate": round(win_rate, 2),
                "average_return_pct": round(average_return, 4),
            },
            "intraday": intraday,
            "research": research,
            "missing_data": list(research.get("missing") or []),
        }
