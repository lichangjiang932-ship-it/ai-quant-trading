"""
分析师团队 - 多智能体交易系统的第一层(抄袭 TradingAgents 的 Analyst Team)

四个专业分析师,每个 = 一次带专业提示词的 LLM 调用,但**都吃项目已有的真实数据**:
- TechnicalAnalyst : 用 DataLoader 算真实 RSI/MACD/布林/均线/波动率
- SentimentAnalyst : 用 NewsAnalyzer 的每股情感聚合
- NewsAnalyst      : 用 NewsAnalyzer 的个股+大盘新闻标题
- FundamentalsAnalyst: 用 FundamentalsFetcher(akshare) 的 PE/PB/ROE/营收净利同比

离线(无 API Key)时,每个分析师用确定性规则生成一段中文报告,保证流水线不崩。
每个分析师返回 dict: {name, stance(bull/bear/neutral), score(-1~1), report(中文文本)}
"""
import pandas as pd
from typing import Dict, List, Optional

from ...data.data_loader import DataLoader


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _stance_from_score(score: float) -> str:
    if score > 0.15:
        return 'bull'
    if score < -0.15:
        return 'bear'
    return 'neutral'


class BaseAnalyst:
    """分析师基类: 子类实现 _gather(收集真实数据) 与 _rule_report(离线兜底)"""
    name = 'analyst'
    role_prompt = '你是一名专业的A股分析师。'

    def __init__(self, llm=None, model: Optional[str] = None):
        self.llm = llm
        self.model = model

    def analyze(self, symbol: str, context: Dict) -> Dict:
        try:
            facts, score = self._gather(symbol, context)
        except Exception as e:
            facts, score = f"(数据获取失败: {e})", 0.0

        rule_report = self._rule_report(symbol, facts, score)

        report = rule_report
        if self.llm is not None and self.llm.is_available():
            user = (
                f"股票: {symbol}\n"
                f"以下是{self.name}维度的客观数据:\n{facts}\n\n"
                f"请用不超过120字中文,给出该维度的多空判断(看多/看空/中性)与简要依据。"
            )
            llm_text = self.llm.chat(self.role_prompt, user, model=self.model,
                                     fallback=rule_report)
            if llm_text:
                report = llm_text

        return {
            'name': self.name,
            'stance': _stance_from_score(score),
            'score': round(score, 3),
            'report': report,
            'facts': facts,
        }

    def _gather(self, symbol: str, context: Dict):
        raise NotImplementedError

    def _rule_report(self, symbol: str, facts: str, score: float) -> str:
        stance = {'bull': '看多', 'bear': '看空', 'neutral': '中性'}[_stance_from_score(score)]
        return f"[{self.name}·规则] {stance}(评分{score:+.2f})。依据: {facts}"


class TechnicalAnalyst(BaseAnalyst):
    name = '技术面分析师'
    role_prompt = ('你是一名A股技术分析专家,擅长用RSI、MACD、布林带、均线系统判断趋势与超买超卖。'
                   '基于给出的指标数值,判断短期多空。')

    def __init__(self, llm=None, model=None, kline_provider=None, lookback: int = 60):
        super().__init__(llm, model)
        self.kline_provider = kline_provider  # RealtimeData 实例
        self.loader = DataLoader()
        self.lookback = lookback

    def _gather(self, symbol: str, context: Dict):
        df = None
        if self.kline_provider is not None:
            try:
                df = self.kline_provider.get_kline_data(symbol, 'day', self.lookback)
            except Exception:
                df = None

        if df is None or df.empty or len(df) < 20:
            # 退而求其次: 用实时行情里的动量近似
            m = context.get('momentum')
            score = _clip((m or 0) * 5)
            return (f"日K数据不足,改用实时动量近似: 动量={m}", score)

        # 用项目已有 DataLoader 算真实指标(kline 列为小写 close)
        df = self.loader.calculate_rsi(df, column='close')
        df = self.loader.calculate_macd(df, column='close')
        df = self.loader.calculate_bollinger_bands(df, column='close')
        df = self.loader.calculate_moving_averages(df, column='close', windows=[5, 20, 60])
        last = df.iloc[-1]

        rsi = last.get('RSI')
        macd_hist = last.get('MACD_Hist')
        close = last.get('close')
        ma5 = last.get('MA_5')
        ma20 = last.get('MA_20')
        bb_up = last.get('BB_upper')
        bb_low = last.get('BB_lower')

        score = 0.0
        notes = []
        if pd.notna(rsi):
            if rsi < 30:
                score += 0.35; notes.append(f"RSI={rsi:.0f}超卖")
            elif rsi > 70:
                score -= 0.35; notes.append(f"RSI={rsi:.0f}超买")
            else:
                notes.append(f"RSI={rsi:.0f}")
        if pd.notna(macd_hist):
            if macd_hist > 0:
                score += 0.25; notes.append("MACD红柱")
            else:
                score -= 0.25; notes.append("MACD绿柱")
        if pd.notna(ma5) and pd.notna(ma20):
            if ma5 > ma20:
                score += 0.25; notes.append("MA5上穿MA20(多头排列)")
            else:
                score -= 0.25; notes.append("MA5下穿MA20(空头排列)")
        if pd.notna(close) and pd.notna(bb_up) and pd.notna(bb_low):
            if close >= bb_up:
                score -= 0.15; notes.append("触及布林上轨")
            elif close <= bb_low:
                score += 0.15; notes.append("触及布林下轨")

        facts = f"收盘={close:.2f}; " + "; ".join(notes)
        return facts, _clip(score)


class SentimentAnalyst(BaseAnalyst):
    name = '情感分析师'
    role_prompt = ('你是一名A股市场情绪分析专家,基于新闻情感聚合分数判断短期资金情绪偏向。')

    def __init__(self, llm=None, model=None):
        super().__init__(llm, model)

    def _gather(self, symbol: str, context: Dict):
        # 情感由引擎回灌到 context['sentiment'](-1~1)
        s = context.get('sentiment')
        if s is None:
            return ("暂无该股新闻情感聚合数据", 0.0)
        score = _clip(float(s))
        tag = '偏正面' if score > 0.1 else ('偏负面' if score < -0.1 else '中性')
        return (f"新闻情感聚合分={score:+.2f}({tag})", score)


class NewsAnalyst(BaseAnalyst):
    name = '新闻分析师'
    role_prompt = ('你是一名A股新闻事件分析专家,基于个股与大盘新闻标题,判断事件对股价的短期影响。')

    def __init__(self, llm=None, model=None, news_analyzer=None):
        super().__init__(llm, model)
        self.news_analyzer = news_analyzer

    def _gather(self, symbol: str, context: Dict):
        titles: List[str] = []
        breaking = context.get('breaking_news')  # 引擎标记的突发新闻文本
        if breaking:
            titles.append(f"[突发] {breaking}")
        if self.news_analyzer is not None:
            try:
                items = self.news_analyzer.get_symbol_news(symbol, count=5)
                for it in items:
                    t = it.get('news', {}).get('title') if isinstance(it, dict) else None
                    if t:
                        titles.append(t)
            except Exception:
                pass

        if not titles:
            return ("近期无相关新闻", 0.0)

        # 规则评分: 命中利好/利空关键词
        pos_kw = ['增长', '中标', '合作', '回购', '增持', '利好', '超预期', '涨停', '突破']
        neg_kw = ['下滑', '亏损', '减持', '违规', '处罚', '利空', '跌停', '退市', '暴雷', '诉讼']
        score = 0.0
        for t in titles:
            for k in pos_kw:
                if k in t:
                    score += 0.15
            for k in neg_kw:
                if k in t:
                    score -= 0.15
        facts = "近期新闻: " + " | ".join(titles[:5])
        return facts, _clip(score)


class FundamentalsAnalyst(BaseAnalyst):
    name = '基本面分析师'
    role_prompt = ('你是一名A股基本面分析专家,基于PE/PB/ROE与营收净利同比,判断估值高低与成长性。')

    def __init__(self, llm=None, model=None, fundamentals=None):
        super().__init__(llm, model)
        self.fundamentals = fundamentals  # FundamentalsFetcher 实例

    def _gather(self, symbol: str, context: Dict):
        if self.fundamentals is None:
            return ("未接入基本面数据源", 0.0)
        d = self.fundamentals.get(symbol)
        if not d:
            return ("(无基本面数据,可能离线)", 0.0)

        score = 0.0
        notes = []
        pe = d.get('pe')
        pb = d.get('pb')
        roe = d.get('roe')
        rev = d.get('revenue_yoy')
        prof = d.get('profit_yoy')

        if pe is not None:
            if 0 < pe < 20:
                score += 0.2; notes.append(f"PE={pe:.1f}偏低")
            elif pe > 60 or pe < 0:
                score -= 0.2; notes.append(f"PE={pe:.1f}偏高/亏损")
            else:
                notes.append(f"PE={pe:.1f}")
        if roe is not None:
            if roe > 15:
                score += 0.2; notes.append(f"ROE={roe:.1f}%优秀")
            elif roe < 5:
                score -= 0.15; notes.append(f"ROE={roe:.1f}%偏弱")
        if prof is not None:
            if prof > 20:
                score += 0.2; notes.append(f"净利同比+{prof:.0f}%")
            elif prof < -20:
                score -= 0.2; notes.append(f"净利同比{prof:.0f}%")
        if rev is not None and rev > 20:
            score += 0.1; notes.append(f"营收同比+{rev:.0f}%")

        facts = (self.fundamentals.summarize(symbol)
                 + (" || " + "; ".join(notes) if notes else ""))
        return facts, _clip(score)


class CapitalAnalyst(BaseAnalyst):
    name = '资金面分析师'
    role_prompt = ('你是一名A股资金面分析专家,擅长通过主力资金净流入、超大单动向、'
                   '涨停/炸板情绪与北向资金判断短期资金的进出方向。基于给出的资金数据判断多空。')

    def __init__(self, llm=None, model=None, capital=None):
        super().__init__(llm, model)
        self.capital = capital  # CapitalFlowFetcher 实例

    def _gather(self, symbol: str, context: Dict):
        if self.capital is None:
            return ("未接入资金层数据源", 0.0)

        # 交易日日期(YYYYMMDD),优先用 context 传入,否则不查涨停池
        date = context.get('trade_date')

        score = 0.0
        notes = []

        flow = self.capital.get_main_net_summary(symbol)
        if flow:
            tm = flow['total_main_net']  # 元
            ts = flow['total_super_net']
            if tm > 0:
                score += 0.3; notes.append(f"主力净流入{tm/1e4:.0f}万")
            elif tm < 0:
                score -= 0.3; notes.append(f"主力净流出{abs(tm)/1e4:.0f}万")
            if ts > 0:
                score += 0.15; notes.append("超大单净买")
            elif ts < 0:
                score -= 0.1; notes.append("超大单净卖")

        code = symbol.lower().replace('sh', '').replace('sz', '')
        if date:
            try:
                if any(s['code'] == code for s in self.capital.get_limit_up_pool(date)):
                    score += 0.2; notes.append("处涨停池")
                elif any(s['code'] == code for s in self.capital.get_broken_pool(date)):
                    score -= 0.2; notes.append("涨停后炸板")
            except Exception:
                pass

        north = self.capital.get_north_flow()
        if north:
            if north.get('north_net', 0) > 0:
                score += 0.1; notes.append("北向净流入")
            elif north.get('north_net', 0) < 0:
                score -= 0.05; notes.append("北向净流出")

        facts = self.capital.summarize_for_symbol(symbol, date)
        if notes:
            facts += " || " + "; ".join(notes)
        if not flow and not north:
            return ("(无资金流数据,可能非交易时段或离线)", _clip(score))
        return facts, _clip(score)


def build_analysts(names: List[str], llm=None, model=None,
                   kline_provider=None, news_analyzer=None, fundamentals=None,
                   capital=None) -> List[BaseAnalyst]:
    """按 config 的 analysts 列表构建分析师实例"""
    registry = {
        'technical': lambda: TechnicalAnalyst(llm, model, kline_provider=kline_provider),
        'sentiment': lambda: SentimentAnalyst(llm, model),
        'news': lambda: NewsAnalyst(llm, model, news_analyzer=news_analyzer),
        'fundamentals': lambda: FundamentalsAnalyst(llm, model, fundamentals=fundamentals),
        'capital': lambda: CapitalAnalyst(llm, model, capital=capital),
    }
    result = []
    for n in names:
        factory = registry.get(str(n).lower())
        if factory:
            result.append(factory())
    return result
