"""全市场选股漏斗 (借鉴 Qlib / Vibe-Trading 的多级筛选架构)。

背景与问题
----------
原选股路径的候选池被硬截断到 16 只, 来源只有"内置活跃池 + 自选 + AI选股",
导致系统在 5000+ 只 A 股里只看十几只 —— 结果就是:
  * 反复在同一批股票(白酒/电池龙头)里进出, 3 次全亏;
  * 行情好时"一天选不到股票"。

本模块把选股改造成**两级漏斗**, 先便宜后昂贵:

  第一级 粗筛 (本模块, 纯 HTTP 快照, 无 LLM)
      5000+ 只 → 按流动性/市值/换手/涨幅/价格 硬过滤 → 60~80 只
  第二级 精筛 (现有 _autotrade_buy_screen, 含 K 线拉取 + 多因子 + LLM)
      60~80 只 → 严格多因子 → 实际买入的少数几只

这样昂贵的第二级只用在真正有希望的一批票上, 而不是被 16 只的先验池锁死。

粗筛不追求"选中最优", 只追求"不漏掉有希望的、先剔掉明显不能碰的":
  - 剔除流动性不足 (成交额过小 → 买卖滑点大、易被操纵)
  - 剔除涨停/接近涨停 (防追高 —— 亏损归因里最大单笔就买在阶段高点)
  - 剔除暴跌股 (下跌趋势不接刀)
  - 剔除微盘股与超大盘股 (前者波动异常, 后者短期弹性不足)
  - 剔除ST/退市/次新股异常波动
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

SINA_MARKET_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://vip.stock.finance.sina.com.cn/",
}


@dataclass
class PrefilterConfig:
    """粗筛参数。可在 config.yaml autotrade.prefilter 段覆盖。"""
    # 流动性: 当日成交额下限 (元)。低于此值滑点大、易被操纵
    min_amount: float = 1.0e8          # 1 亿
    # 换手率区间 (%)。过低=无人问津, 过高=情绪过热(往往是出货)
    min_turnover: float = 1.0
    max_turnover: float = 20.0
    # 流通市值区间 (亿元)。剔除微盘(异常波动)与超大盘(短期弹性不足)
    min_float_mktcap_yi: float = 50.0
    max_float_mktcap_yi: float = 5000.0
    # 当日涨跌幅区间 (%)。上限防追高(涨停/接近涨停不追), 下限防接飞刀
    min_change_pct: float = -5.0
    max_change_pct: float = 9.0
    # 股价区间 (元)。剔除低价仙股与超高价股(单笔资金买不满一手弹性差)
    min_price: float = 3.0
    max_price: float = 300.0
    # 单页条数与最大翻页数 (控制请求量, 3×100=300 条足够粗筛出候选)
    page_size: int = 100
    max_pages: int = 3
    # 粗筛后保留的最大候选数, 交给昂贵的第二级精筛
    max_candidates: int = 60


DEFAULT_PREFILTER = PrefilterConfig()


def load_prefilter_config(autotrade_cfg: dict) -> PrefilterConfig:
    """从 config.yaml 的 autotrade.prefilter 段加载, 缺省用默认值。"""
    raw = dict((autotrade_cfg or {}).get("prefilter") or {})
    known = {f for f in PrefilterConfig().__dataclass_fields__}
    clean = {k: v for k, v in raw.items() if k in known}
    return PrefilterConfig(**clean) if clean else PrefilterConfig()


def fetch_market_snapshot(cfg: PrefilterConfig = DEFAULT_PREFILTER,
                          sort_by: str = "amount") -> List[Dict]:
    """分页拉取全市场 A 股快照 (新浪行情中心, 免鉴权)。

    sort_by 默认按成交额降序 —— 取"全市场最活跃"的一批票, 涨跌都包含。
    若按涨幅排序则只会拿到领涨股, 把正在回调的买点全漏掉 (亏损归因显示
    最佳买点是上升趋势中的健康回踩, 而非追涨)。


    返回原始行列表, 字段已归一化为:
      symbol(带前缀) code name price change_pct amount turnover
      float_mktcap_yi total_mktcap_yi pe pb

    网络异常时返回已成功拉取的部分 (不抛异常, 保证上层链路可用)。
    """
    rows: List[Dict] = []
    for page in range(1, max(int(cfg.max_pages), 1) + 1):
        try:
            resp = requests.get(
                SINA_MARKET_URL,
                params={"page": page, "num": int(cfg.page_size), "sort": sort_by,
                        "asc": "0", "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                headers=_HEADERS,
                timeout=12,
            )
            data = resp.json()
        except Exception:
            break  # 网络问题: 用已拿到的部分继续, 不让整个选股链路断掉
        if not isinstance(data, list) or not data:
            break
        for item in data:
            row = _normalize_row(item)
            if row:
                rows.append(row)
        time.sleep(0.12)  # 轻微节流, 避免被临时限流
    return rows


def _normalize_row(item: Dict) -> Optional[Dict]:
    """把新浪原始字段归一化为统一结构; 解析失败返回 None。"""
    try:
        symbol = str(item.get("symbol", "") or "").strip()
        if not symbol:
            return None
        price = float(item.get("trade") or 0)
        if price <= 0:
            return None
        # mktcap/nmc 单位是万元 → 换算成亿元
        total_yi = float(item.get("mktcap") or 0) / 1e4
        float_yi = float(item.get("nmc") or 0) / 1e4
        return {
            "symbol": symbol,
            "code": str(item.get("code", "") or ""),
            "name": str(item.get("name", "") or ""),
            "price": price,
            "change_pct": float(item.get("changepercent") or 0),
            "amount": float(item.get("amount") or 0),
            "turnover": float(item.get("turnoverratio") or 0),
            "total_mktcap_yi": total_yi,
            "float_mktcap_yi": float_yi,
            "pe": float(item.get("per") or 0),
            "pb": float(item.get("pb") or 0),
        }
    except (TypeError, ValueError):
        return None


# ST / *ST / 退市 / 次新异常波动 的名称特征
_BAD_NAME_RE = re.compile(r"(ST|退|PT|\*ST)", re.IGNORECASE)


def _is_bad_name(name: str) -> bool:
    return bool(_BAD_NAME_RE.search(name or ""))


def prefilter_market(rows: List[Dict],
                     cfg: PrefilterConfig = DEFAULT_PREFILTER) -> List[Dict]:
    """第一级粗筛: 便宜的硬过滤, 把全市场缩到可控的候选集。

    返回按"成交额"降序的候选列表 (成交额=市场关注度, 优先精筛更活跃的票)。
    """
    out: List[Dict] = []
    for r in rows or []:
        if _is_bad_name(r.get("name", "")):
            continue
        price = r.get("price", 0)
        if not (cfg.min_price <= price <= cfg.max_price):
            continue
        if r.get("amount", 0) < cfg.min_amount:
            continue
        to = r.get("turnover", 0)
        if not (cfg.min_turnover <= to <= cfg.max_turnover):
            continue
        fmc = r.get("float_mktcap_yi", 0)
        if fmc > 0 and not (cfg.min_float_mktcap_yi <= fmc <= cfg.max_float_mktcap_yi):
            continue
        chg = r.get("change_pct", 0)
        if not (cfg.min_change_pct <= chg <= cfg.max_change_pct):
            continue
        out.append(r)

    out.sort(key=lambda x: x.get("amount", 0), reverse=True)
    return out[:max(int(cfg.max_candidates), 1)]


def scan_candidates(cfg: PrefilterConfig = DEFAULT_PREFILTER) -> List[Dict]:
    """一步到位: 拉全市场快照 → 粗筛 → 返回候选列表。"""
    rows = fetch_market_snapshot(cfg)
    return prefilter_market(rows, cfg)
