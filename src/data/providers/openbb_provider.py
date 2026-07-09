"""
OpenBB 研究数据源(可选)

OpenBB 是数据/研究平台,擅长美股/宏观/研报,但不能对 A 股下单,A 股实时行情也弱。
因此这里只把 OpenBB 当作"研究背景"提供者,产出一小段参考文本喂给 LLM 决策,
绝不参与 A 股真实下单。

设计要点(参照 src/strategies/ml_strategy.py 的 HAS_SKLEARN 降级写法):
- 未安装 openbb 时优雅降级,get_research_context 返回空串,不报错
- 任何异常都吞掉并返回空,保证不拖垮引擎
"""
from typing import List, Optional

try:
    from openbb import obb
    HAS_OPENBB = True
except Exception:  # ImportError 或 openbb 自身初始化异常
    obb = None
    HAS_OPENBB = False


class OpenBBProvider:
    """OpenBB 研究背景提供者(可选依赖)"""

    def __init__(self, enabled: bool = False):
        # 只有当用户显式开启且成功导入 openbb 时才真正启用
        self.enabled = bool(enabled) and HAS_OPENBB
        self._cache = {}

    def is_available(self) -> bool:
        return self.enabled

    def status(self) -> dict:
        return {
            'openbb_installed': HAS_OPENBB,
            'enabled': self.enabled,
        }

    def get_research_context(self, symbols: Optional[List[str]] = None,
                             max_chars: int = 500) -> str:
        """
        返回一小段市场研究背景文本供 LLM 参考。
        未启用或出错时返回空串。此处刻意保持轻量,只取大盘概览级别信息,
        避免把 A 股逐票查询压到不支持的 provider 上。
        """
        if not self.enabled:
            return ''
        try:
            parts = []
            snp = self._safe_index_snapshot()
            if snp:
                parts.append(snp)
            text = ' | '.join(p for p in parts if p)
            return text[:max_chars]
        except Exception:
            return ''

    def _safe_index_snapshot(self) -> str:
        """尝试用 OpenBB 取一个宏观/大盘参考点。失败返回空串。"""
        try:
            # 用美股大盘 ETF 作为全球风险偏好的参考锚点(OpenBB 强项)
            data = obb.equity.price.quote('SPY')
            df = data.to_df() if hasattr(data, 'to_df') else None
            if df is not None and not df.empty:
                row = df.iloc[-1]
                price = row.get('last_price', row.get('close', ''))
                return f"SPY参考价 {price}"
        except Exception:
            return ''
        return ''
