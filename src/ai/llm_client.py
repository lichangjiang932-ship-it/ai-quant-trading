"""
LLM 决策客户端 - OpenAI 兼容协议

支持国内大模型(DeepSeek / 通义千问)通过 OpenAI 兼容的 /chat/completions 接口
进行交易决策。设计要点:
- API Key 只从环境变量读取(配置里存变量名,不存明文)
- decide() 把行情 + 动量 + 新闻情感 + 可选研究背景组织成中文提示词,
  要求模型返回严格 JSON(buy/sell/hold + confidence + reason)
- 无 Key / 未联网 / 请求失败 时回退到确定性规则,保证调用方永不因 AI 崩溃

参考现有可选依赖降级写法: src/strategies/ml_strategy.py 的 HAS_SKLEARN
"""
import os
import json
import re
from typing import Dict, Optional

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# 各 provider 的默认 OpenAI 兼容端点与模型
PROVIDER_DEFAULTS = {
    'deepseek': {
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-chat',
        'api_key_env': 'DEEPSEEK_API_KEY',
    },
    'qwen': {
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'model': 'qwen-plus',
        'api_key_env': 'DASHSCOPE_API_KEY',
    },
}

VALID_ACTIONS = ('buy', 'sell', 'hold')


class LLMDecision:
    """LLM 决策结果"""

    def __init__(self, action: str = 'hold', confidence: float = 0.0,
                 reason: str = '', source: str = 'llm'):
        self.action = action if action in VALID_ACTIONS else 'hold'
        try:
            self.confidence = max(0.0, min(1.0, float(confidence)))
        except (ValueError, TypeError):
            self.confidence = 0.0
        self.reason = reason
        self.source = source  # 'llm' 或 'fallback'

    def to_dict(self) -> Dict:
        return {
            'action': self.action,
            'confidence': self.confidence,
            'reason': self.reason,
            'source': self.source,
        }

    def __repr__(self):
        return f"LLMDecision({self.action}, conf={self.confidence:.2f}, src={self.source})"


class LLMClient:
    """OpenAI 兼容协议的大模型客户端(用 requests 直连,无需 openai SDK)"""

    def __init__(self, provider: str = 'deepseek', model: Optional[str] = None,
                 api_key_env: Optional[str] = None, base_url: Optional[str] = None,
                 temperature: float = 0.2, timeout: int = 20):
        provider = (provider or 'deepseek').lower()
        defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS['deepseek'])

        self.provider = provider
        self.model = model or defaults['model']
        self.base_url = (base_url or defaults['base_url']).rstrip('/')
        self.api_key_env = api_key_env or defaults['api_key_env']
        self.api_key = os.environ.get(self.api_key_env, '').strip()
        self.temperature = temperature
        self.timeout = timeout

        self._call_count = 0
        self._fail_count = 0

    def is_available(self) -> bool:
        """是否可真正调用大模型(已安装 requests 且环境变量里有 Key)"""
        return HAS_REQUESTS and bool(self.api_key)

    def status(self) -> Dict:
        return {
            'provider': self.provider,
            'model': self.model,
            'available': self.is_available(),
            'has_key': bool(self.api_key),
            'key_env': self.api_key_env,
            'calls': self._call_count,
            'failures': self._fail_count,
        }

    def decide(self, context: Dict) -> LLMDecision:
        """根据上下文做决策。任何异常都回退到规则,绝不抛出。"""
        if not self.is_available():
            return self._fallback(context, reason_prefix='未配置API Key或缺少requests')
        try:
            self._call_count += 1
            return self._decide_via_llm(context)
        except Exception as e:
            self._fail_count += 1
            return self._fallback(context, reason_prefix=f'LLM调用失败({e})')

    def _decide_via_llm(self, context: Dict) -> LLMDecision:
        url = f"{self.base_url}/chat/completions"
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': self._build_system_prompt()},
                {'role': 'user', 'content': self._build_user_prompt(context)},
            ],
            'temperature': self.temperature,
            'response_format': {'type': 'json_object'},
            'stream': False,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        content = data['choices'][0]['message']['content']
        parsed = self._parse_json(content)
        if parsed is None:
            return self._fallback(context, reason_prefix='LLM返回无法解析为JSON')

        return LLMDecision(
            action=str(parsed.get('action', 'hold')).lower().strip(),
            confidence=parsed.get('confidence', 0.0),
            reason=str(parsed.get('reason', ''))[:200],
            source='llm',
        )

    def chat(self, system: str, user: str, model: Optional[str] = None,
             temperature: Optional[float] = None, fallback: str = '') -> str:
        """通用自由文本对话(供分析师/研究员/交易员/风控 agent 使用)。

        任何异常(无 Key、断网、超时、非法响应)都不抛出,返回 fallback 文本,
        保证多智能体流水线在离线时也能继续跑。

        Args:
            system: 系统提示词(角色设定)
            user: 用户提示词(数据与问题)
            model: 覆盖默认模型(用于 deep_think / quick_think 两档)
            temperature: 覆盖默认温度
            fallback: 离线/失败时返回的兜底文本
        """
        if not self.is_available():
            return fallback
        try:
            self._call_count += 1
            return self._chat_via_llm(system, user, model, temperature)
        except Exception:
            self._fail_count += 1
            return fallback

    def _chat_via_llm(self, system: str, user: str, model: Optional[str],
                      temperature: Optional[float]) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': model or self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'temperature': self.temperature if temperature is None else temperature,
            'stream': False,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return (data['choices'][0]['message']['content'] or '').strip()

    def chat_json(self, system: str, user: str, model: Optional[str] = None,
                  temperature: Optional[float] = None) -> Optional[Dict]:
        """要求模型返回 JSON 对象并解析。失败返回 None(调用方自行兜底)。"""
        if not self.is_available():
            return None
        try:
            self._call_count += 1
            url = f"{self.base_url}/chat/completions"
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            }
            payload = {
                'model': model or self.model,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                'temperature': self.temperature if temperature is None else temperature,
                'response_format': {'type': 'json_object'},
                'stream': False,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.json()['choices'][0]['message']['content']
            return self._parse_json(content)
        except Exception:
            self._fail_count += 1
            return None

    def _build_system_prompt(self) -> str:
        return (
            "你是一名严谨的A股量化交易助手。你会收到某只股票的实时行情、近期动量、"
            "新闻情感分数以及可选的市场研究背景,请据此判断当前应当买入(buy)、卖出(sell)"
            "还是持有(hold)。要求:\n"
            "1. 只输出一个JSON对象,不要输出任何多余文字或markdown代码块。\n"
            "2. JSON字段: action(取值 buy/sell/hold)、confidence(0到1之间的小数)、"
            "reason(不超过80字的中文理由)。\n"
            "3. 保持稳健:信息不足或风险不明时倾向 hold 并给较低 confidence。\n"
            "4. 你只给决策建议,真实下单数量与风控由外部系统负责,无需你计算股数。"
        )

    def _build_user_prompt(self, context: Dict) -> str:
        symbol = context.get('symbol', '')
        price = context.get('price', 0)
        change_pct = context.get('change_pct', 0)
        momentum = context.get('momentum')
        momentum_window = context.get('momentum_window', 'N')
        sentiment = context.get('sentiment')
        position = context.get('position')
        research = context.get('research')

        lines = [
            f"股票代码: {symbol}",
            f"最新价: {price}",
            f"当日涨跌幅: {change_pct}%",
        ]
        if isinstance(momentum, (int, float)):
            lines.append(f"近{momentum_window}个采样点动量: {momentum:.2%}")
        if isinstance(sentiment, (int, float)):
            lines.append(f"新闻情感分(-1到1,越大越正面): {sentiment}")
        lines.append(f"当前持仓: {position if position else '无'} 股")
        if research:
            lines.append(f"市场研究背景(参考): {research}")
        lines.append("请仅返回交易决策JSON。")
        return "\n".join(lines)

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict]:
        """容错解析: 先直接 json.loads,失败再从文本中抽取第一个 {...} 块。"""
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None

    def _fallback(self, context: Dict, reason_prefix: str = '') -> LLMDecision:
        """确定性规则兜底: 综合动量与新闻情感打分。无 AI 也能安全运行。"""
        momentum = context.get('momentum')
        sentiment = context.get('sentiment')
        has_position = bool(context.get('position'))

        m = momentum if isinstance(momentum, (int, float)) else 0.0
        s = sentiment if isinstance(sentiment, (int, float)) else 0.0

        score = m * 5.0 + s * 0.5  # 动量为主,情感为辅
        confidence = min(abs(score), 1.0)

        action = 'hold'
        if score > 0.15 and not has_position:
            action = 'buy'
        elif score < -0.10 and has_position:
            action = 'sell'
        else:
            confidence = min(confidence, 0.4)

        reason = (f"[规则兜底] {reason_prefix}; 动量={m:.4f}, 情感={s:.3f}, "
                  f"综合分={score:.3f}")
        return LLMDecision(action=action, confidence=confidence,
                           reason=reason, source='fallback')
