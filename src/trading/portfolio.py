"""
实盘账户聚合视图 — 基金(爱基金) + 股票(guling-trader) 统一快照。

将实盘交易层 (FundTrader + StockTrader) 的数据聚合成与模拟盘
(/api/account) 结构兼容的账户视图, 供前端双账户展示。

容错策略:
  - 基金凭证未初始化 / 股票未连接 → 对应部分返回空并附 warning, 不阻塞整体
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EMPTY_LIVE = {
    "mode": "live",
    "mode_label": "实盘账户",
    "total_assets": 0.0,
    "cash": 0.0,
    "market_value": 0.0,
    "profit": 0.0,
    "profit_pct": 0.0,
    "fund": {"connected": False, "total_value": 0.0, "holdings": [], "error": ""},
    "stock": {"connected": False, "total_assets": 0.0, "positions": [], "error": ""},
    "positions": [],
    "warnings": [],
}


def get_live_snapshot(
    config_path: Optional[str] = None, config=None, max_fund_holdings: int = 50
) -> Dict:
    """实盘账户快照 (基金 + 股票)。容错聚合, 单项失败不影响整体。

    Args:
        config_path: config.yaml 路径 (StockTrader.from_config 用)
        config: 已加载的 Config 对象 (复用, 避免重复读文件)
    """
    result: Dict[str, Any] = {
        "mode": "live",
        "mode_label": "实盘账户",
        "total_assets": 0.0,
        "cash": 0.0,
        "market_value": 0.0,
        "profit": 0.0,
        "profit_pct": 0.0,
        "fund": {"connected": False, "total_value": 0.0, "holdings": [], "error": ""},
        "stock": {"connected": False, "total_assets": 0.0, "positions": [], "error": ""},
        "positions": [],
        "warnings": [],
    }

    # ---- 基金 (爱基金) ----
    try:
        from .fund_trader import FundTrader, FundNotInitializedError

        trader = FundTrader()
        holdings = trader.get_all_holdings()
        fund_rows = holdings.get("fundList") or []
        fund_value = sum(float(r.get("totalAmount") or 0) for r in fund_rows)
        wallet = holdings.get("wallet") or {}

        # 钱包持仓 (货币基金等, 如圆信永丰丰润货币B) — 不在 fundList 但属于持仓
        wallet_position = None
        if isinstance(wallet, dict) and wallet.get("fundCode"):
            # 总份额 = 可用份额 + 冻结份额 (freezeMoney 字段也是份额单位)
            w_avail = float(wallet.get("avaiableVol") or 0)
            w_freeze = float(wallet.get("freezeMoney") or 0)
            w_total_share = w_avail + w_freeze
            # 如果 bankAccountShareList 有值, 取它作为权威值 (避免重复)
            ba_list = wallet.get("bankAccountShareList") or []
            if ba_list:
                w_total_share = sum(float(ba.get("totalShare") or 0) for ba in ba_list)
            if w_total_share > 0:
                w_income = float(wallet.get("holdProfits") or 0)
                w_name = str(wallet.get("fundName", "") or "")
                w_code = str(wallet.get("fundCode", "") or "")
                wallet_position = {
                    "fundCode": w_code,
                    "fundName": w_name,
                    "holdVol": w_total_share,
                    "totalAmount": round(w_total_share, 4),  # 货币基金净值≈1
                    "holdIncome": w_income,
                    "avgCost": 0,
                    "netValue": 1.0,
                    "positionType": "wallet",
                }
                fund_rows.append(wallet_position)
                # 钱包持仓计入 fund 总额 (货币基金按净值 1 计算市值)
                fund_value += float(wallet_position["totalAmount"])

        wallet_value = 0.0
        if isinstance(wallet, dict):
            wallet_value = float(wallet.get("totalValue") or wallet.get("sumValue") or 0)
        result["fund"] = {
            "connected": True,
            "total_value": fund_value,
            "wallet_value": wallet_value,
            "holdings": fund_rows[:max_fund_holdings],
            "count": len(fund_rows),
            "wallet_position": wallet_position,
            "error": "",
        }
        # 全部基金市值计入 (fund_value 已含钱包货币基金)
        result["market_value"] += fund_value
        result["total_assets"] += fund_value
        # 钱包现金余额
        result["cash"] += wallet_value
        result["total_assets"] += wallet_value
    except FundNotInitializedError as e:
        result["fund"]["error"] = str(e)
        result["warnings"].append("基金: 凭证未初始化")
    except Exception as e:
        result["fund"]["error"] = str(e)
        result["warnings"].append(f"基金: {e}")

    # ---- 股票 (guling-trader) ----
    try:
        from .stock_trader import StockTrader

        trader = StockTrader.from_config(config_path=config_path)
        snap = trader.snapshot()
        account = snap.get("account") or {}
        positions = snap.get("positions") or []
        connected = bool(snap.get("connected"))
        result["stock"] = {
            "connected": connected,
            "total_assets": float(account.get("total_assets") or 0),
            "available_cash": float(account.get("available_cash") or 0),
            "market_value": float(account.get("market_value") or 0),
            "positions": positions,
            "error": "",
        }
        result["total_assets"] += float(account.get("total_assets") or 0)
        result["cash"] += float(account.get("available_cash") or 0)
        result["market_value"] += float(account.get("market_value") or 0)
    except Exception as e:
        result["stock"]["error"] = str(e)
        result["warnings"].append(f"股票: {e}")

    # 汇总
    result["positions"] = result["fund"]["holdings"] + result["stock"]["positions"]
    if result["total_assets"] > 0:
        result["profit_pct"] = (
            result.get("profit", 0.0) / result["total_assets"] * 100
        )
    return result


def format_live_account(snap: Dict) -> str:
    """把实盘账户快照格式化为可读文本。"""
    lines: List[str] = ["实盘账户总览"]
    lines.append(f"  总资产: {snap['total_assets']:.2f} 元")
    lines.append(f"  可用资金: {snap['cash']:.2f} 元 | 市值: {snap['market_value']:.2f} 元")

    fund = snap.get("fund") or {}
    if fund.get("connected"):
        lines.append(f"\n基金持仓 ({fund.get('count', 0)} 只, 市值 {fund.get('total_value', 0):.2f} 元):")
        for r in (fund.get("holdings") or [])[:10]:
            name = r.get("fundName", "")
            code = r.get("fundCode", "")
            vol = r.get("holdVol", r.get("totalAmount", 0))
            value = r.get("totalAmount", 0) or 0
            income = r.get("holdIncome", 0) or 0
            tag = " [钱包]" if r.get("positionType") == "wallet" else ""
            lines.append(f"  {name}({code}) 份额={vol} 市值={value} 收益={income}{tag}")
        if fund.get("wallet_value"):
            lines.append(f"  钱包资产: {fund.get('wallet_value', 0):.2f} 元")
    elif fund.get("error"):
        lines.append(f"\n基金: 不可用 ({fund['error'][:60]})")

    stock = snap.get("stock") or {}
    if stock.get("connected"):
        lines.append(f"\n股票账户 (guling-trader):")
        lines.append(f"  总资产: {stock.get('total_assets', 0):.2f} | 可用: {stock.get('available_cash', 0):.2f}")
        for p in (stock.get("positions") or [])[:10]:
            lines.append(
                f"  {p.get('name', '')}({p.get('symbol', '')}) "
                f"{p.get('quantity', 0)}股 成本{p.get('avg_cost', '')} 盈亏{p.get('unrealized_pnl', 0)}"
            )
    elif stock.get("error"):
        lines.append(f"\n股票: 不可用 ({stock['error'][:60]})")

    return "\n".join(lines)
