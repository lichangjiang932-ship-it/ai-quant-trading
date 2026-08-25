# -*- coding: utf-8 -*-
"""
AI 量化平台 MCP Server (原生 stdio 实现, 零依赖)
===================================================
借鉴 QuantDinger 的 Agent Gateway: 把平台能力暴露给外部 AI 助手 (Claude/其他 MCP 客户端)。

工具:
  - research_summary(symbol)   研究速览卡 (评级/买卖区间/可比估值)
  - news_factors()             今日新闻涨幅因子
  - ipo_guard()                巨型 IPO 上市日避险状态
  - review_today()             今日收盘复盘
  - memory_signals(signal_type) 策略记忆库胜率统计

用法:
  python mcp_server.py            # stdio 模式, 供 MCP 客户端 (Claude/WorkBuddy) 调用
配置示例 (mcp.json):
  {"mcpServers": {"ai-quant": {"command": "D:/py/python.exe", "args": ["D:/destok/money/mcp_server.py"]}}}
"""
import json
import os
import sys
import traceback

# 可执行目录 (允许从任意 cwd 启动)
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ai-quant"
SERVER_VERSION = "1.0.0"


# ─────────────────────────────────────────────
# 工具实现 (延迟导入项目模块, 失败返回错误信息)
# ─────────────────────────────────────────────
def _research_summary(args: dict):
    symbol = str(args.get("symbol", "") or "").strip()
    if not symbol:
        return "参数 symbol 必填 (如 sh600519 / 600519 / 贵州茅台)"
    try:
        from src.research import build_tearsheet, compute_comps
        from frontend.api_server import _normalize_symbol, _load_daily_frame
        sym = _normalize_symbol(symbol)
        hist = _load_daily_frame(sym, 240)
        ts = build_tearsheet(sym, hist)
        comps = compute_comps(sym, hist, limit=4)
        r = ts.get("rating", {})
        return json.dumps({
            "名称": ts.get("name"), "现价": ts.get("price"),
            "评级": r.get("action"), "评分": r.get("score"),
            "买入区间": ts.get("buy_low"), "止损": ts.get("stop_price"),
            "目标": ts.get("target_price"), "风险回报比": ts.get("risk_reward"),
            "可比估值": comps.get("verdict"),
        }, ensure_ascii=False)
    except Exception as e:
        return f"研究失败: {e}"


def _news_factors(args: dict):
    try:
        from src.news.news_factor import get_daily_factors
        data = get_daily_factors()
        top = data.get("factors", [])[:10]
        return json.dumps({
            "日期": data.get("date"), "新闻数": data.get("news_count"),
            "因子": [{"symbol": f["symbol"], "factor": f["factor_score"],
                      "direction": f["direction"], "events": f["events"]} for f in top],
        }, ensure_ascii=False)
    except Exception as e:
        return f"新闻因子失败: {e}"


def _ipo_guard(args: dict):
    try:
        from src.news.ipo_guard import ipo_guard_status
        st = ipo_guard_status()
        return json.dumps({
            "今日是否巨型IPO上市日": st["today_is_giant_ipo"],
            "今日事件": st["today_event"],
            "未来14天预警": st["upcoming"],
            "规则": st["rule"],
        }, ensure_ascii=False)
    except Exception as e:
        return f"IPO守卫失败: {e}"


def _review_today(args: dict):
    try:
        from frontend.api_server import _build_daily_review
        r = _build_daily_review()
        return json.dumps({
            "日期": r.get("date"),
            "当日盈亏": r.get("summary", {}).get("day_pnl"),
            "已实现": r.get("summary", {}).get("realized_pnl"),
            "成交": f"{r.get('summary',{}).get('buy_count')}买/{r.get('summary',{}).get('sell_count')}卖",
            "策略建议": [s["title"] + ": " + s["content"][:60] for s in r.get("strategies", [])],
        }, ensure_ascii=False)
    except Exception as e:
        return f"复盘失败: {e}"


def _memory_signals(args: dict):
    try:
        from src.analysis.strategy_memory import query_stats
        st = query_stats(args.get("signal_type") or None,
                         int(args.get("days") or 0) or None)
        return json.dumps({"样本": st["total"], "按信号类型": st["signals"]},
                          ensure_ascii=False)
    except Exception as e:
        return f"策略记忆失败: {e}"


TOOLS = [
    {"name": "research_summary", "description": "A股个股研究速览卡: 评级/买入区间/止损/目标价/可比估值",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string", "description": "股票代码或名称, 如 sh600519"}}, "required": ["symbol"]}},
    {"name": "news_factors", "description": "当日新闻涨幅因子: 热点新闻涉及个股的 0-100 因子分",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ipo_guard", "description": "巨型 IPO 上市日避险守卫状态与未来14天预警",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "review_today", "description": "今日收盘复盘: 盈亏/成交/策略建议",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_signals", "description": "策略记忆库: 按信号类型统计历史胜率",
     "inputSchema": {"type": "object", "properties": {
         "signal_type": {"type": "string", "description": "可选: 高评分/新闻驱动/普通信号"},
         "days": {"type": "integer", "description": "可选: 最近N天"}}}},
]

_HANDLERS = {
    "research_summary": _research_summary,
    "news_factors": _news_factors,
    "ipo_guard": _ipo_guard,
    "review_today": _review_today,
    "memory_signals": _memory_signals,
}


# ─────────────────────────────────────────────
# JSON-RPC 2.0 / MCP stdio 协议
# ─────────────────────────────────────────────
def _handle(msg: dict) -> dict:
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "notifications/initialized":
        return None  # 通知无响应
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"未知工具: {name}"}}
        try:
            text = handler(args)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(text)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(e) + "\n" + traceback.format_exc()[-300:]}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未支持的方法: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
