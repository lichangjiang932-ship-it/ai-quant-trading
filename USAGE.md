# 使用说明

本文档面向使用者,按场景组织,先讲安装与启动,再分别介绍各功能模块的用法。

## 目录

1. [环境要求](#1-环境要求)
2. [安装](#2-安装)
3. [配置](#3-配置)
4. [快速启动](#4-快速启动)
5. [运行回测](#5-运行回测)
6. [实时交易](#6-实时交易)
7. [风险控制](#7-风险控制)
8. [止损止盈](#8-止损止盈)
9. [投资组合分析](#9-投资组合分析)
10. [通知系统](#10-通知系统)
11. [Web 监控面板](#11-web-监控面板)
12. [运行测试](#12-运行测试)
13. [常见问题](#13-常见问题)

---

## 1. 环境要求

- Python 3.9+(推荐 3.11)
- Windows / Linux / macOS
- 内存 2GB+,磁盘 1GB+
- 可选:实盘需要 QMT mini 客户端

## 2. 安装

```bash
# 克隆或下载项目
cd D:\destok\money   # 或你的项目路径

# 安装核心依赖
pip install -r requirements.txt

# ML 策略需要(可选)
pip install scikit-learn

# Web 监控面板需要(可选)
pip install streamlit plotly streamlit-autorefresh

# 测试需要(可选)
pip install pytest pytest-asyncio
```

验证安装:
```bash
python -c "import src.analysis.portfolio, src.execution.risk_manager, src.notification.notifier; print('OK')"
```

## 3. 配置

复制模板:
```bash
cp config/config.example.yaml config/config.yaml
```

`config/config.yaml` 关键段说明:

```yaml
trading:
  initial_capital: 1000000      # 初始资金
  auto_trade: false              # 自动下单开关(实盘前确认)
  symbols: [sh600000, sz000001]  # 监控股票

broker:
  type: simulated                # simulated=模拟, qmt=实盘
  account_id: ""                 # QMT 账号
  mini_qmt_path: ""              # QMT 路径

strategy:
  type: cross_ma                 # cross_ma / momentum / mean_reversion / ml
  short_window: 5
  long_window: 20

risk:
  max_position_size: 0.10        # 单股最大 10%
  max_drawdown: 0.20             # 最大回撤 20%
  stop_loss: 0.05                # 止损 5%
  take_profit: 0.10              # 止盈 10%
  max_daily_loss: 0.02           # 单日最大亏损 2%

notification:
  enabled: true
  console: {enabled: true}
  file: {enabled: true, log_dir: logs}
  telegram: {enabled: false, bot_token: "", chat_id: ""}
  dingtalk: {enabled: false, webhook_url: ""}
```

> ⚠️ 实盘前请把 `auto_trade` 设为 `false` 先用模拟盘观察。

## 4. 快速启动

### 方式一:交互菜单(推荐)

```bash
python start.py
```

菜单提供 16 项功能:
- 新版引擎 / 旧版引擎 / ML 策略
- QMT 实盘
- Web 监控面板
- 新闻驱动
- 多因子 / 网格示例
- 投资组合分析
- 风险监控压测
- 运行全部测试
- 查看/重置配置

### 方式二:命令行直接启动

```bash
# 新版引擎(WebSocket + 异步)
python engine.py config/config.yaml

# 旧版引擎(HTTP 轮询)
python main.py

# 新闻驱动
python main_news.py

# Web 面板
streamlit run dashboard.py
```

## 5. 运行回测

### 5.1 跑示例

```bash
# 多因子策略
python examples/multi_factor.py

# 网格策略
python examples/grid_strategy.py
```

输出包含:总收益 / 年化 / 最大回撤 / 夏普 / 索提诺 / 卡玛 / 胜率 / 交易数。

### 5.2 编写自己的策略

继承 `BaseStrategy` 实现三个方法:

```python
from src.strategies.base_strategy import BaseStrategy, Signal
import pandas as pd
import numpy as np

class MyStrategy(BaseStrategy):
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        df["signal"] = 0
        df["raw_signal"] = 0
        for i in range(20, len(df)):
            if df["Close"].iloc[i] > df["Close"].iloc[i - 20:].mean():
                df.iat[i, df.columns.get_loc("raw_signal")] = 1
            else:
                df.iat[i, df.columns.get_loc("raw_signal")] = -1
        df["signal"] = df["raw_signal"].shift(1).fillna(0).astype(int)
        return df

    def calculate_position_size(self, signal, current_price, portfolio_value):
        if signal == Signal.BUY:
            return int(portfolio_value * 0.1 / current_price / 100) * 100
        return 0
```

### 5.3 执行回测

```python
from src.backtest.backtester import Backtester
import numpy as np
import pandas as pd

# 准备数据(实际中用 market_data 获取)
dates = pd.date_range("2020-01-01", periods=500, freq="D")
price = 100 * np.exp(np.cumsum(np.random.normal(0.001, 0.015, 500)))
data = pd.DataFrame({
    "Open": price * 0.999, "High": price * 1.01, "Low": price * 0.99,
    "Close": price, "Volume": np.random.randint(1_000_000, 10_000_000, 500),
}, index=dates)

strategy = MyStrategy()
bt = Backtester(initial_capital=1_000_000, commission=0.0003, slippage=0.001, stamp_tax=0.001)
results = bt.run_backtest(strategy, data, "TEST")

print(f"总收益 {results['total_return']:.2%}")
print(f"夏普 {results['sharpe_ratio']:.3f}")
print(f"最大回撤 {results['max_drawdown']:.2%}")
```

回测结果字段:
- `total_return` / `annualized_return`
- `sharpe_ratio` / `sortino_ratio` / `calmar_ratio`
- `max_drawdown` / `max_drawdown_pct`
- `win_rate` / `profit_factor` / `avg_win` / `avg_loss`
- `total_commission` / `total_stamp_tax`
- `trades` / `equity_curve` / `drawdown_series`

### 5.4 参数优化

```python
from src.backtest.optimizer import StrategyOptimizer
from src.strategies.momentum_strategy import MomentumStrategy

opt = StrategyOptimizer(MomentumStrategy, data, "TEST", metric="sharpe_ratio")
results = opt.optimize({
    "lookback_period": [10, 20, 30, 40],
    "threshold": [0.02, 0.03, 0.04, 0.05],
})
print(results.head())
```

## 6. 实时交易

### 6.1 模拟盘

```yaml
# config/config.yaml
trading:
  auto_trade: true       # 开启自动下单
broker:
  type: simulated        # 模拟券商
```

```bash
python start.py
# 选 1-4 启动新版引擎
```

### 6.2 实盘(QMT)

```yaml
broker:
  type: qmt
  account_id: "你的资金账号"
  mini_qmt_path: "D:/qmt/mini"
```

```bash
python start.py
# 选 8,按提示输入
```

或手动:
```bash
python main.py
```

## 7. 风险控制

### 7.1 交易前风控

引擎已自动调用 `RiskManager.check_order`。手动调用:

```python
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide

rm = RiskManager(
    max_position_size=0.10,    # 单股最大 10%
    max_drawdown=0.20,         # 最大回撤 20%
    max_daily_loss=0.02,       # 单日最大亏损 2%
    max_total_position=0.95,   # 总仓位上限
    max_orders_per_day=100,    # 单日笔数
    stop_loss=0.05,            # 止损 5%
    take_profit=0.10,          # 止盈 10%
)

req = OrderRequest(
    symbol="sh600000", side=OrderSide.BUY,
    quantity=1000, price=10.0, portfolio_value=1_000_000,
)
r = rm.check_order(req)
if not r.allowed:
    print(f"拦截: {r.reason}")
    # violations: ["单股仓位 10.00% > 10%", "回撤 25.00% > 20%"]
else:
    print(f"通过,建议数量 {r.suggested_quantity}")
```

### 7.2 触发条件与拦截原因

| 维度 | 触发条件 | 配置项 |
|------|----------|--------|
| 单股仓位 | 新建仓位 > N% 总资产 | `max_position_size` |
| 总仓位 | 持仓市值 > N% 总资产 | `max_total_position` |
| 回撤 | (峰值-当前)/峰值 > N% | `max_drawdown` |
| 日亏 | 当日亏损 > N% 总资产 | `max_daily_loss` |
| 笔数 | 今日成交 ≥ N 笔 | `max_orders_per_day` |
| 锁定 | 手动调用 `rm.lock()` | - |

### 7.3 锁定与解锁

```python
rm.lock(minutes=10)      # 锁 10 分钟
rm.lock(hours=1)         # 锁 1 小时
rm.unlock()              # 立即解锁
```

## 8. 止损止盈

引擎已自动接入。每个行情 tick 都会检查所有持仓。

### 8.1 配置(config.yaml)

```yaml
risk:
  stop_loss: 0.05            # 固定止损 5%
  take_profit: 0.10          # 固定止盈 10%
  trailing_stop: 0.03        # 追踪止损 3%(可空)
  partial_tp_levels: [0.05, 0.10]  # 分批止盈 5%, 10%
```

### 8.2 手动使用

```python
from src.execution.tpsl_monitor import TPSLMonitor, TPSLConfig

tpsl = TPSLMonitor(default_config=TPSLConfig(
    stop_loss=0.05,
    take_profit=0.10,
    trailing_stop=0.03,
    partial_tp_levels=[0.05, 0.10],
))

# 建仓时注册
tpsl.register_position("sh600000", entry_price=10.0, quantity=1000)

# 行情到来时
events = tpsl.on_quote("sh600000", 11.0)
for ev in events:
    print(f"{ev.reason.value} | 盈亏 {ev.pnl_pct:.2%} | 数量 {ev.suggested_quantity}")

# 查看状态
print(tpsl.get_positions())
print(tpsl.get_stats())
```

### 8.3 四种止盈方式

| 名称 | 触发 | 行为 |
|------|------|------|
| 固定止损 | 亏损 ≥ stop_loss | 全部卖出 |
| 固定止盈 | 盈利 ≥ take_profit | 全部卖出 |
| 追踪止损 | 从最高价回落 ≥ trailing_stop | 全部卖出 |
| 分批止盈 | 达到 level 阈值 | 卖出 50% 当前仓位 |

## 9. 投资组合分析

### 9.1 一键分析(从 StateManager 读交易历史)

```python
from src.analysis.portfolio import (
    load_trades_from_state, generate_report, format_text_report
)
from src.utils.state_manager import StateManager

sm = StateManager(db_path="data/trading_state.db")
trades = load_trades_from_state(sm, limit=5000)
rpt = generate_report(trades, initial_capital=1_000_000)
print(format_text_report(rpt, initial_capital=1_000_000))
```

输出示例:
```
============================================================
  投资组合业绩报告
============================================================
周期:       2024-01-02 ~ 2024-02-20  (40 个交易日)
初始资金:   ¥1,000,000
总收益:     5.23%
年化收益:   47.50%
年化波动:   12.30%
下行波动:   8.10%
夏普比率:   2.456
索提诺:     3.789
卡玛:       4.358
最大回撤:   1.09% (持续 5 天)
当前回撤:   0.00%
VaR 95%:    -0.85%
CVaR 95%:   -1.23%
偏度:       0.234
峰度:       -0.456
------------------------------------------------------------
  交易统计
------------------------------------------------------------
总笔数:     40 (买 14 / 卖 26)
胜率:       53.85%
盈亏笔数:   14 胜 / 12 负
平均盈利:   ¥378.02
平均亏损:   ¥-446.08
利润因子:   0.99
盈亏比:     0.85
期望值:     ¥45.23
最大连盈:   4
最大连亏:   3
最大单笔盈: ¥1,234.56
最大单笔亏: ¥-987.65
总佣金:     ¥200.00
总印花税:   ¥254.98
交易标的:   4 只
最佳标的:   sh600000
最差标的:   sz000001
============================================================
```

### 9.2 自定义数据源

```python
from src.analysis.portfolio import (
    TradeRecord, generate_report, format_text_report
)
from datetime import datetime

trades = [
    TradeRecord("sh600000", "buy", 1000, 10.0, datetime(2024, 1, 2),
                10000, 5, 0, 0, "manual"),
    TradeRecord("sh600000", "sell", 1000, 11.0, datetime(2024, 1, 10),
                11000, 5, 11, 1000, "manual"),
]
rpt = generate_report(trades, initial_capital=100_000)
print(format_text_report(rpt, initial_capital=100_000))
```

### 9.3 访问明细

```python
rpt = generate_report(trades, 1_000_000)
rpt["metrics"]            # dict: 所有业绩指标
rpt["trade_statistics"]   # dict: 交易统计
rpt["monthly_returns"]    # DataFrame: 月度收益表
rpt["by_symbol"]          # DataFrame: 按标的盈亏
rpt["by_strategy"]        # DataFrame: 按策略盈亏
rpt["equity_curve"]       # DataFrame: 权益曲线(可画图)
```

## 10. 通知系统

### 10.1 启动菜单启用

`start.py` → 选 9(投资组合业绩分析)会显示当前通知配置统计。
`start.py` → 选 14(风险监控)会显示通知器的发送/失败计数。

### 10.2 配置文件启用

```yaml
notification:
  enabled: true
  min_level: info          # info/success/warning/error/critical

  console:
    enabled: true
    use_color: true

  file:
    enabled: true
    log_dir: logs
    filename: notifications.log

  telegram:
    enabled: true
    bot_token: "123456:ABC-DEF..."   # @BotFather 获取
    chat_id: "-1001234567890"        # 群组或私聊 ID

  dingtalk:
    enabled: true
    webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    secret: "SEC..."                 # 加签密钥

  wechat_work:
    enabled: true
    webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    mentioned_list: ["@all"]
```

### 10.3 编程使用

```python
import asyncio
from src.notification.notifier import (
    NotificationManager, Notification, NotificationType, NotificationLevel,
    ConsoleNotifier, TelegramNotifier, DingTalkNotifier, build_manager_from_config,
)

# 方式一:从 config 构建
mgr = build_manager_from_config({
    "console": {"enabled": True},
    "telegram": {"enabled": True, "bot_token": "...", "chat_id": "..."},
})

# 方式二:手动添加
mgr = NotificationManager()
mgr.add_notifier(ConsoleNotifier())
mgr.add_notifier(TelegramNotifier(bot_token="...", chat_id="..."))

# 启动 worker
asyncio.run(mgr.start())

# 异步发送(进入队列)
await mgr.notify(Notification(
    type=NotificationType.TRADE,
    level=NotificationLevel.SUCCESS,
    title="买入 sh600000",
    message="¥10.00 x 1000股",
))

# 同步发送(立即返回)
sent = await mgr.notify_sync(Notification(
    type=NotificationType.RISK,
    level=NotificationLevel.WARNING,
    title="风控拦截",
    message="单股仓位超限",
))

# 停止
await mgr.stop()
```

### 10.4 事件类型与触发场景

| 类型 | 触发场景 |
|------|----------|
| `TRADE` | 买入/卖出成交 |
| `RISK` | 风控拦截订单 |
| `TPSL` | 止损止盈触发 |
| `SIGNAL` | 策略产生信号 |
| `SYSTEM` | 引擎启停、状态切换 |
| `ERROR` | 异常报错 |

## 11. Web 监控面板

```bash
streamlit run dashboard.py
# 浏览器访问 http://localhost:8501
```

页面功能:
- **实时行情**: 多股看板 + K 线 + MA20/MA60 + 成交量
- **策略回测**: 交互式回测(含 ML 策略) + 绩效图表
- **参数优化**: 参数搜索 + 结果排序
- **投资组合**: 持仓管理 + 手动买卖
- **新闻情感**: 市场情感 + 热门股票 + 行业排名
- **风险监控**: 风控状态 + SL/TP 测试 + 投资组合分析图表
- **系统状态**: 账户 + 风控 + 日志

> 勾选左侧"自动刷新"可每 N 秒自动更新(需 `streamlit-autorefresh`)。

## 12. 运行测试

```bash
# 全部测试
python -m pytest tests/ -v

# 单独模块
python -m pytest tests/test_risk.py -v
python -m pytest tests/test_portfolio.py -v
python -m pytest tests/test_notifier.py -v
python -m pytest tests/test_engine.py -v
python -m pytest tests/test_ws.py -v

# 通过启动菜单
python start.py
# 选 15
```

当前 86 个测试,覆盖:
- 策略与回测器 (5)
- 风控管理 (11)
- 止损止盈 (10)
- 投资组合分析 (20)
- 通知系统 (8)
- 引擎集成 (7)
- WebSocket (21)
- 原有测试 (5)

## 13. 常见问题

### Q: 启动时报 ModuleNotFoundError?

```bash
# 重新安装
pip install -r requirements.txt
# 或单独装
pip install scikit-learn streamlit websockets aiohttp pyyaml
```

### Q: WebSocket 连不上东方财富?

- 检查网络:浏览器访问 push2.eastmoney.com
- 换数据源:在 `data_source.realtime` 改 `sina`
- 引擎会自动降级到 HTTP 轮询

### Q: ML 策略运行时提示 sklearn 未安装?

```bash
pip install scikit-learn
# 1.0 以上版本均可,本项目测试用 1.8.0
```

### Q: 通知发不出去?

- Telegram:确认 `bot_token` 和 `chat_id` 正确,先在 Telegram 里 `/start` 机器人
- 钉钉:Webhook 需在群机器人设置中开启"加签",secret 才有效
- 微信企业号:Webhook 需在群机器人设置中获取

### Q: 回测的资金曲线异常?

- 检查 `initial_capital` 不要太小(建议 10 万+)
- 检查 `commission` 和 `slippage` 是否合理(A 股:commission=0.0003, stamp_tax=0.001)
- 数据量不足 60 条回测结果不可靠

### Q: 想添加新策略?

1. 继承 `BaseStrategy` 或 `RealtimeStrategy`
2. 实现 `generate_signals` / `on_tick` / `calculate_position_size` / `should_enter_position` / `should_exit_position`
3. 在 `engine.py` 的 `setup_strategies` 添加分支
4. 写测试在 `tests/test_strategies.py`

### Q: 想添加新券商?

1. 继承 `BaseBroker` 在 `src/execution/brokers/`
2. 实现 `connect` / `place_order` / `cancel_order` / `get_positions` / `get_account_info`
3. 在 `engine.py` 的 broker 初始化处添加分支

### Q: 实盘风险?

- 默认 `auto_trade: false`,只观察信号
- 第一次实盘:用小资金 1-3 万先跑 1-2 周
- 强烈建议开启所有通知渠道,实时监控
- 任何修改都应先在模拟盘验证

### Q: 数据从哪里来?

- 实时:东方财富 WebSocket(主)+ 新浪(备)+ HTTP 轮询(兜底)
- 历史:akShare(推荐) / 东方财富 / yfinance
- 自动回退,无需手动切换

### Q: 如何备份?

```bash
# 1. 配置
cp config/config.yaml config/config.yaml.bak

# 2. 数据库(状态/订单/交易)
cp data/trading_state.db backup/

# 3. 日志
tar -czf logs_$(date +%Y%m%d).tar.gz logs/
```

---

## 附:模块速查表

| 需求 | 模块 |
|------|------|
| 写回测策略 | `src/strategies/base_strategy.py` |
| 跑回测 | `src/backtest/backtester.py` |
| 优化参数 | `src/backtest/optimizer.py` |
| 接入 ML | `src/strategies/ml_strategy.py` |
| 接入实时数据 | `src/data/realtime/ws_client.py` |
| 接入券商 | `src/execution/brokers/` |
| 交易前拦截 | `src/execution/risk_manager.py` |
| 止损止盈 | `src/execution/tpsl_monitor.py` |
| 业绩分析 | `src/analysis/portfolio.py` |
| 推送通知 | `src/notification/notifier.py` |
| Web UI | `dashboard.py` |
| 启动入口 | `start.py` / `engine.py` / `main.py` |
| 状态持久化 | `src/utils/state_manager.py` |
| 异步任务 | `src/utils/async_engine.py` |
