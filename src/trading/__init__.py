"""
交易层 — 项目内完成基金 & 股票实盘交易, 不依赖 WorkBuddy。

子模块:
  fund_trader.py   爱基金交易客户端 (同花顺爱基金 HTTP API)
  stock_trader.py  股票实盘交易 (guling-trader MCP → 同花顺 xiadan.exe)

命令行入口:  python trade.py ...   (项目根目录)

API 入口:    frontend/api_server.py 的 /api/fund/* 与 /api/live/* 端点

凭证要求:
  基金: 先执行 `python -c "from aijijin_sdk import init; init('INIT_TOKEN')"` 完成初始化,
        token 持久化在 ~/.aijijin/credentials.json, 交易时自动换取 Work Token。
  股票: 需运行 guling-trader.exe 并配对, agent_token 填在 config.yaml broker.guling_agent_token。
"""
from .fund_trader import FundTrader, FundTraderError
from .stock_trader import StockTrader, StockTraderError

__all__ = [
    "FundTrader",
    "FundTraderError",
    "StockTrader",
    "StockTraderError",
]
