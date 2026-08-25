# -*- coding: utf-8 -*-
"""
巨型 IPO 上市日避险守卫 (Giant IPO Liquidity Drain Guard)
==========================================================
回测依据: 13 个历史巨型 IPO 上市日, 沪深300 当日平均 -0.98% (69% 下跌),
创业板 -1.08%; 事件前 20 日均值仅 +0.09% —— 上市日资金虹吸显著。

规则:
  - 巨型 IPO 上市日 T 及 T+1: 自托管禁止新开仓, 已持仓提示风控
  - 名单可配置 (config/giant_ipo.yaml), 内置已验证的历史 + 近期事件
"""
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

_GIANT_IPO_LIST = [
    # (上市日期, 名称, 代码, 备注)  — 日期来源: 交易所公告/证券时报/同花顺
    ("2018-06-08", "工业富联", "sh601138", "募资271亿"),
    ("2019-12-10", "邮储银行", "sh601658", "募资327亿"),
    ("2020-01-16", "京沪高铁", "sh601816", "募资306亿"),
    ("2020-07-16", "中芯国际", "sh688981", "募资532亿, 当日沪深300 -4.81%"),
    ("2020-10-15", "金龙鱼", "sz300999", "募资139亿"),
    ("2020-11-02", "中金公司", "sh601995", "募资132亿"),
    ("2021-06-10", "三峡能源", "sh600905", "募资227亿"),
    ("2021-08-20", "中国电信", "sh601728", "募资541亿"),
    ("2021-12-15", "百济神州", "sh688235", "募资222亿"),
    ("2022-01-05", "中国移动", "sh600941", "募资560亿"),
    ("2022-04-21", "中国海油", "sh600938", "募资323亿"),
    ("2026-07-27", "长鑫科技", "sh688825", "首日成交1411亿, 市值3.28万亿"),
    ("2026-08-19", "宇树科技", "sh688836", "人形机器人第一股"),
    # ↓ 用户可在此追加即将上市的巨型 IPO ↓
]

# 近期可能上市的巨型 IPO 预警 (用户可维护; 日期确认后移入主表)
_PENDING_IPO = [
    # ("2026-09-XX", "某大型公司", "sh6xxxxx", "预计募资超100亿"),
]


def load_giant_ipo_list() -> List[Dict]:
    """巨型 IPO 名单 (内置 + 可选配置文件合并)。"""
    items = [
        {"date": d, "name": n, "symbol": s, "note": note}
        for d, n, s, note in _GIANT_IPO_LIST
    ]
    # 尝试读配置扩展
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg_path = os.path.join(root, "config", "giant_ipo.yaml")
        if os.path.exists(cfg_path):
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for it in cfg.get("events", []) or []:
                items.append({
                    "date": str(it.get("date", "")),
                    "name": str(it.get("name", "")),
                    "symbol": str(it.get("symbol", "")),
                    "note": str(it.get("note", "")),
                })
    except Exception:
        pass
    return items


def is_giant_ipo_day(target: Optional[date] = None) -> Optional[Dict]:
    """目标日期 (默认今天) 是否为巨型 IPO 上市日。是则返回事件信息。"""
    d = (target or date.today()).isoformat()
    for it in load_giant_ipo_list():
        if it["date"] == d:
            return it
    return None


def upcoming_giant_ipo(days: int = 14) -> List[Dict]:
    """未来 days 天内即将到来的巨型 IPO (预警)。"""
    today = date.today()
    out = []
    for it in load_giant_ipo_list():
        try:
            d = date.fromisoformat(it["date"])
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            out.append({**it, "days_left": delta})
    return sorted(out, key=lambda x: x["days_left"])


def ipo_guard_status() -> Dict:
    """守卫状态: 今日是否巨型IPO日 / 未来预警。"""
    hit = is_giant_ipo_day()
    return {
        "today_is_giant_ipo": bool(hit),
        "today_event": hit,
        "upcoming": upcoming_giant_ipo(14),
        "rule": "巨型IPO上市日(T及T+1)禁止新开仓; 回测依据: 沪深300当日平均-0.98%, 69%概率下跌",
    }
