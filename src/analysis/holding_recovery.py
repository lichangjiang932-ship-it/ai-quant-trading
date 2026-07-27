"""亏损持仓的多维修复评估与 A 股操作建议。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class HoldingRecoveryConfig:
    sell_score: float = 38.0
    reduce_score: float = 52.0
    add_score: float = 72.0
    max_add_loss_pct: float = 8.0


class HoldingRecoveryAnalyzer:
    def __init__(self, config: Optional[HoldingRecoveryConfig] = None):
        self.config = config or HoldingRecoveryConfig()

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
        return max(min(float(value), high), low)

    @staticmethod
    def _normalize(data: pd.DataFrame) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()
        frame = data.copy()
        frame.rename(columns={column: str(column).title() for column in frame.columns}, inplace=True)
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(column not in frame.columns for column in required):
            return pd.DataFrame()
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.replace([np.inf, -np.inf], np.nan, inplace=True)
        frame.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
        return frame[frame["Close"] > 0]

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> float:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        relative = gain / loss.replace(0, np.nan)
        value = (100 - 100 / (1 + relative)).fillna(50).iloc[-1]
        return float(value)

    @staticmethod
    def _factor_label(score: float) -> str:
        if score >= 70:
            return "偏强"
        if score >= 55:
            return "改善"
        if score >= 40:
            return "偏弱"
        return "恶化"

    def _factor(self, score: float, summary: str, available: bool = True) -> Dict:
        score = self._clamp(score)
        return {
            "score": round(score, 2),
            "label": self._factor_label(score),
            "summary": summary,
            "available": bool(available),
        }

    def _technical_factor(self, data: pd.DataFrame, current_price: float) -> Tuple[Dict, Dict]:
        frame = self._normalize(data)
        if len(frame) < 30:
            return self._factor(50, "历史日线不足，技术面按中性处理", False), {
                "trend_broken": False,
                "rsi": 50.0,
                "ma20": 0.0,
                "ma60": 0.0,
            }

        close = frame["Close"]
        price = float(current_price or close.iloc[-1])
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else ma20
        rsi = self._rsi(close)
        ret5 = price / float(close.iloc[-6]) - 1 if len(close) >= 6 else 0.0
        ret20 = price / float(close.iloc[-21]) - 1 if len(close) >= 21 else 0.0
        low20 = float(frame["Low"].tail(20).min())
        score = 50.0
        score += 14 if price >= ma20 else -16
        score += 12 if ma20 >= ma60 else -14
        score += 9 if ma5 >= ma20 else -8
        score += 7 if ret5 > 0 else -6
        score += 6 if ret20 > -0.03 else -8
        if 42 <= rsi <= 68:
            score += 8
        elif rsi >= 75:
            score -= 7
        elif rsi < 30:
            score -= 4
        if price <= low20 * 1.02:
            score -= 7
        trend_broken = price < ma20 and ma5 < ma20 and ma20 <= ma60
        direction = "站上" if price >= ma20 else "跌破"
        structure = "中期结构向上" if ma20 >= ma60 else "中期结构仍弱"
        summary = f"现价{direction}20日线，{structure}，RSI {rsi:.0f}，5日动量 {ret5 * 100:+.1f}%"
        return self._factor(score, summary), {
            "trend_broken": trend_broken,
            "rsi": round(rsi, 2),
            "ma20": round(ma20, 4),
            "ma60": round(ma60, 4),
        }

    def _market_factor(self, market_regime: Optional[Dict]) -> Dict:
        regime = market_regime or {}
        code = str(regime.get("code", "unknown"))
        available = code != "unknown"
        score = float(regime.get("score", 50) or 50)
        if code == "risk_off":
            score = min(score, 30)
        elif code == "risk_on":
            score = max(score, 70)
        label = str(regime.get("label") or "市场环境未知")
        ret20 = float(regime.get("ret20_pct", 0) or 0)
        drawdown = float(regime.get("drawdown60_pct", 0) or 0)
        return self._factor(
            score,
            f"{label}，指数20日 {ret20:+.1f}%，60日回撤 {drawdown:.1f}%",
            available,
        )

    def _news_factor(self, news_items: Optional[List[Dict]]) -> Tuple[Dict, List[str], bool]:
        weighted_score = 0.0
        total_weight = 0.0
        headlines = []
        severe_negative = False
        for item in news_items or []:
            sentiment = item.get("sentiment") or {}
            try:
                score = float(sentiment.get("score", 0) or 0)
                importance = max(float(sentiment.get("importance", 5) or 5), 1.0)
            except (TypeError, ValueError):
                continue
            if abs(score) > 1:
                score /= 100
            score = max(min(score, 1), -1)
            weighted_score += score * importance
            total_weight += importance
            title = str((item.get("news") or {}).get("title") or item.get("title") or "").strip()
            if title and len(headlines) < 3:
                headlines.append(title)
            if score <= -0.6 and importance >= 7:
                severe_negative = True
        if total_weight <= 0:
            return self._factor(50, "未取得有效个股消息，消息面不作加分", False), headlines, False
        average = weighted_score / total_weight
        score = 50 + average * 42
        tone = "偏正面" if average > 0.12 else "偏负面" if average < -0.12 else "中性"
        return self._factor(score, f"近期开源消息情绪{tone}，样本 {len(news_items or [])} 条"), headlines, severe_negative

    def _fundamental_factor(self, fundamentals: Optional[Dict]) -> Tuple[Dict, bool]:
        values = fundamentals or {}
        score = 50.0
        observed = 0
        deteriorating = False

        def number(key: str):
            try:
                value = values.get(key)
                return None if value in (None, "", "-") else float(value)
            except (TypeError, ValueError):
                return None

        roe = number("roe")
        profit_yoy = number("profit_yoy")
        revenue_yoy = number("revenue_yoy")
        pe = number("pe_ttm") if number("pe_ttm") is not None else number("pe")
        pb = number("pb")
        if roe is not None:
            observed += 1
            score += 15 if roe >= 15 else 8 if roe >= 8 else -20 if roe < 0 else -4
            deteriorating = deteriorating or roe < 0
        if profit_yoy is not None:
            observed += 1
            score += 13 if profit_yoy >= 20 else 6 if profit_yoy >= 0 else -18 if profit_yoy <= -30 else -8
            deteriorating = deteriorating or profit_yoy <= -30
        if revenue_yoy is not None:
            observed += 1
            score += 9 if revenue_yoy >= 10 else 4 if revenue_yoy >= 0 else -11 if revenue_yoy <= -20 else -5
        if pe is not None:
            observed += 1
            score += -12 if pe <= 0 else -8 if pe >= 80 else 4 if pe <= 35 else 0
        if pb is not None:
            observed += 1
            score += -6 if pb >= 10 else 3 if 0 < pb <= 3 else 0
        if observed == 0:
            return self._factor(50, "未取得有效财务指标，基本面不作加分", False), False
        details = []
        if roe is not None:
            details.append(f"ROE {roe:.1f}%")
        if profit_yoy is not None:
            details.append(f"利润同比 {profit_yoy:+.1f}%")
        if revenue_yoy is not None:
            details.append(f"营收同比 {revenue_yoy:+.1f}%")
        if pe is not None:
            details.append(f"PE {pe:.1f}")
        return self._factor(score, "，".join(details[:3]) or "财务指标有限"), deteriorating

    def _capital_factor(self, capital_flow: Optional[Dict], average_amount: float) -> Dict:
        flow = capital_flow or {}
        if not flow:
            return self._factor(50, "未取得当日资金流，资金面不作加分", False)
        total = float(flow.get("total_main_net", 0) or 0)
        latest = float(flow.get("last_main_net", 0) or 0)
        denominator = max(float(average_amount or 0), 1.0)
        ratio = total / denominator
        score = 50 + np.tanh(ratio * 4) * 25
        score += 7 if latest > 0 else -7 if latest < 0 else 0
        direction = "净流入" if total > 0 else "净流出" if total < 0 else "持平"
        return self._factor(score, f"主力{direction} {abs(total) / 10000:.0f} 万元，尾盘增量 {latest / 10000:+.0f} 万元")

    @staticmethod
    def _reduce_quantity(available_quantity: int) -> int:
        available = max(int(available_quantity), 0)
        if available <= 100:
            return available
        quantity = (available // 2) // 100 * 100
        return max(min(quantity, available), 100)

    def analyze(
        self,
        symbol: str,
        data: pd.DataFrame,
        quantity: int,
        available_quantity: int,
        avg_cost: float,
        current_price: float,
        opportunity: Optional[Dict] = None,
        exit_plan: Optional[Dict] = None,
        market_regime: Optional[Dict] = None,
        news_items: Optional[List[Dict]] = None,
        fundamentals: Optional[Dict] = None,
        capital_flow: Optional[Dict] = None,
    ) -> Dict:
        quantity = max(int(quantity), 0)
        available = max(min(int(available_quantity), quantity), 0)
        price = float(current_price or 0)
        cost = float(avg_cost or 0)
        pnl_pct = (price / cost - 1) * 100 if price > 0 and cost > 0 else 0.0
        opportunity = opportunity or {}
        exit_plan = exit_plan or {}

        technical, technical_state = self._technical_factor(data, price)
        market = self._market_factor(market_regime)
        news, headlines, severe_negative_news = self._news_factor(news_items)
        fundamental, fundamental_deteriorating = self._fundamental_factor(fundamentals)
        capital = self._capital_factor(capital_flow, float(opportunity.get("average_amount", 0) or 0))
        factors = {
            "technical": technical,
            "market": market,
            "news": news,
            "fundamental": fundamental,
            "capital": capital,
        }
        weights = {
            "technical": 0.35,
            "market": 0.20,
            "news": 0.15,
            "fundamental": 0.15,
            "capital": 0.15,
        }
        recovery_score = sum(factors[name]["score"] * weight for name, weight in weights.items())
        completeness = sum(1 for factor in factors.values() if factor["available"]) / len(factors)
        protective_stop = float(exit_plan.get("protective_stop", 0) or opportunity.get("stop_loss", 0) or 0)
        pending_exit = str(exit_plan.get("pending_action", exit_plan.get("action", "hold")))
        stop_triggered = protective_stop > 0 and price <= protective_stop
        hard_exit = (
            stop_triggered
            or pending_exit == "sell"
            or technical_state["trend_broken"] and technical["score"] < 35
            or severe_negative_news
            or fundamental_deteriorating and fundamental["score"] < 35
        )

        market_code = str((market_regime or {}).get("code", "unknown"))
        allow_new = bool((market_regime or {}).get("allow_new_positions", market_code != "risk_off"))
        buy_low = float(opportunity.get("buy_low", 0) or 0)
        buy_high = float(opportunity.get("buy_high", 0) or 0)
        in_buy_zone = buy_low > 0 and buy_high > 0 and buy_low * 0.98 <= price <= buy_high * 1.01
        base_add_quantity = max(int(opportunity.get("suggested_qty", 0) or 0), 0)
        can_add = (
            pnl_pct < 0
            and pnl_pct >= -self.config.max_add_loss_pct
            and recovery_score >= self.config.add_score
            and technical["score"] >= 65
            and market_code != "risk_off"
            and allow_new
            and news["score"] >= 45
            and fundamental["score"] >= 45
            and capital["score"] >= 50
            and completeness >= 1.0
            and in_buy_zone
            and base_add_quantity >= 100
            and not hard_exit
        )

        pending_action = "hold"
        if hard_exit or recovery_score < self.config.sell_score:
            pending_action = "sell"
        elif recovery_score < self.config.reduce_score:
            pending_action = "reduce"
        elif can_add:
            pending_action = "add"

        suggested_buy = 0
        suggested_sell = 0
        decision = pending_action
        if pending_action == "add":
            multiplier = float((market_regime or {}).get("position_multiplier", 0.5) or 0.5)
            suggested_buy = int(base_add_quantity * min(max(multiplier, 0), 1)) // 100 * 100
            if suggested_buy <= 0:
                decision = "hold"
                pending_action = "hold"
        elif pending_action == "sell":
            suggested_sell = available
        elif pending_action == "reduce":
            suggested_sell = self._reduce_quantity(available)
        if pending_action in ("sell", "reduce") and available <= 0:
            decision = "wait_t1"

        if recovery_score >= 70:
            outlook = "回涨条件较强"
        elif recovery_score >= 55:
            outlook = "具备部分修复条件"
        elif recovery_score >= 40:
            outlook = "回涨条件偏弱"
        else:
            outlook = "回涨条件很弱"

        if decision == "add":
            label = f"亏损持仓：确认修复，可加仓 {suggested_buy} 股"
            meaning = "多维条件同时改善且价格仍在计划买入区，只允许小仓位确认式加仓。"
            operation = f"继续持有原 {quantity} 股，可买入 {suggested_buy} 股；加仓后仍以保护位 {protective_stop:.2f} 管理。"
        elif decision == "sell":
            label = f"亏损持仓：建议卖出 {suggested_sell} 股"
            meaning = "保护位、趋势、消息或基本面风险已压过等待回涨的理由。"
            operation = f"当前可卖 {available} 股，建议卖出 {suggested_sell} 股；不要继续补仓摊低成本。"
        elif decision == "reduce":
            label = f"亏损持仓：建议减仓 {suggested_sell} 股"
            meaning = "仍有修复可能，但证据不足以承担全部仓位，先降低风险敞口。"
            operation = f"当前可卖 {available} 股，建议先卖出 {suggested_sell} 股，剩余仓位等待趋势重新站稳。"
        elif decision == "wait_t1":
            label = "亏损持仓：退出条件成立，等待 T+1"
            meaning = "当前判断偏向减仓或卖出，但当日买入部分依法不能当天卖出。"
            operation = f"持有 {quantity} 股、当前可卖 0 股；下一交易日开盘后复核，条件未改善则执行{('清仓' if pending_action == 'sell' else '减仓')}。"
        else:
            label = "亏损持仓：继续持有，暂不补仓"
            meaning = "目前尚未触发硬性退出，但回涨证据不足以支持继续加仓。"
            operation = f"持有 {quantity} 股、可卖 {available} 股；暂不买卖，跌破 {protective_stop:.2f} 转为卖出，重新站稳20日线再评估。"

        strongest = sorted(factors.items(), key=lambda item: item[1]["score"], reverse=True)[0]
        weakest = sorted(factors.items(), key=lambda item: item[1]["score"])[0]
        factor_names = {
            "technical": "技术面",
            "market": "市场环境",
            "news": "消息面",
            "fundamental": "基本面",
            "capital": "资金面",
        }
        warnings = ["修复评分用于比较证据强弱，不是回涨概率，也不保证解套或盈利。"]
        if completeness < 1.0:
            warnings.append(f"五维数据完整度仅 {completeness * 100:.0f}%，缺失维度按中性处理且禁止据此激进加仓。")
        if pnl_pct < -self.config.max_add_loss_pct:
            warnings.append("亏损幅度已超过允许加仓阈值，系统禁止补仓摊低成本。")
        if severe_negative_news:
            warnings.append("检测到高重要度负面消息，优先按风险事件处理。")

        return {
            "symbol": symbol,
            "decision": decision,
            "pending_action": pending_action,
            "label": label,
            "meaning": meaning,
            "detail": operation,
            "recovery_score": round(self._clamp(recovery_score), 2),
            "outlook": outlook,
            "data_completeness": round(completeness, 4),
            "pnl_pct": round(pnl_pct, 4),
            "quantity": quantity,
            "available_quantity": available,
            "suggested_quantity": suggested_buy if decision == "add" else suggested_sell,
            "suggested_buy_quantity": suggested_buy,
            "suggested_sell_quantity": suggested_sell,
            "protective_stop": round(protective_stop, 4),
            "target_price": round(float(exit_plan.get("target_price", 0) or opportunity.get("target_price", 0) or 0), 4),
            "factors": factors,
            "headlines": headlines,
            "reasons": [
                f"最强证据：{factor_names[strongest[0]]} {strongest[1]['score']:.0f} 分，{strongest[1]['summary']}",
                f"主要拖累：{factor_names[weakest[0]]} {weakest[1]['score']:.0f} 分，{weakest[1]['summary']}",
            ],
            "warnings": warnings,
        }
