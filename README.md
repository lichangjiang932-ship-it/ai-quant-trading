# 量化交易平台

一个支持 A 股实时交易的量化交易平台，支持实时行情监控、策略执行、自动交易、风险控制、组合分析和通知推送。

## 功能特点

### 行情与策略
- **实时行情**: 新浪、腾讯、东方财富、akshare 等多源自动切换
- **策略库**:
  - 均线交叉策略 (CrossMA)
  - 动量策略 (Momentum)
  - 均值回归策略 (MeanReversion)
  - 多因子综合策略 (MultiFactor)
  - 网格交易策略 (Grid)
  - ML 机器学习策略 (基于 scikit-learn)
  - 新闻驱动策略 (NewsStrategy)

### 风险控制
- **交易前风控**: 单股仓位 / 总仓位 / 单日亏损 / 回撤 / 单日笔数 全面拦截
- **止损止盈 (TP/SL)**: 固定 SL/TP、追踪止损 (Trailing Stop)、分批止盈
- **风险报告**: 实时回撤、日亏、限额、锁定状态

### 投资组合分析
- **风险调整收益**: 夏普、索提诺、卡玛、信息比率
- **风险指标**: 最大回撤、回撤持续期、VaR、CVaR、波动率、偏度、峰度
- **业绩归因**: 按月 / 按周 / 按策略 / 按标的分解
- **交易统计**: 胜率、盈亏比、利润因子、期望值、最大连盈/连亏

### 执行与通知
- **多券商**: 模拟券商 / QMT 实盘 / FastBroker (0.05ms 延迟)
- **异步引擎**: WebSocket 推送 + 异步非阻塞 + SQLite 持久化
- **通知系统**: 控制台 / 日志 / Telegram / 钉钉 / 微信企业号 / Webhook

### 可视化与工具
- **Web 监控面板**: Streamlit + Plotly，K线 / 回测 / 优化 / 持仓 / 风险一站式
- **回测系统**: 多指标 + 多股组合 + 参数优化 + ML 策略回测
- **新闻情感**: 多源 + 情感评分 + 行业排名 + 龙虎榜
- **市场监控**: 涨跌停告警、异常波动监控

## 项目结构

```
量化/
├── main.py                    # 旧版 HTTP 轮询引擎
├── engine.py                  # 新版 WebSocket 异步引擎 (推荐)
├── main_news.py               # 新闻驱动交易平台
├── start.py                   # 快速启动脚本
├── dashboard.py               # Web 监控面板 (Streamlit)
├── config/                    # 配置文件
│   ├── config.example.yaml   # 配置模板
│   └── config.yaml           # 默认配置
├── src/
│   ├── data/                  # 数据模块
│   │   ├── market_data.py    # 历史数据
│   │   ├── data_loader.py    # 技术指标
│   │   └── realtime/         # 实时数据
│   │       ├── realtime_data.py
│   │       └── ws_client.py  # WebSocket 客户端 (含重连/校验/状态)
│   ├── strategies/            # 策略模块
│   │   ├── base_strategy.py
│   │   ├── realtime_strategy.py
│   │   ├── cross_ma_strategy.py
│   │   ├── momentum_strategy.py / mean_reversion_strategy.py
│   │   ├── realtime_*.py    # 实时版本
│   │   ├── ml_strategy.py    # ML 机器学习策略
│   │   └── news_strategy.py
│   ├── backtest/              # 回测模块
│   │   ├── backtester.py     # 回测引擎 (增强版)
│   │   ├── optimizer.py
│   │   └── portfolio.py
│   ├── execution/             # 执行模块
│   │   ├── brokers/          # 券商接口
│   │   ├── fast_broker.py    # 0.05ms 延迟快速券商
│   │   ├── risk_manager.py   # 交易前风控
│   │   └── tpsl_monitor.py   # 止损止盈监控
│   ├── analysis/              # 投资组合分析
│   │   └── portfolio.py
│   ├── notification/          # 通知系统
│   │   └── notifier.py
│   ├── scheduler/             # 调度器
│   ├── news/                  # 新闻情感
│   └── utils/                 # 工具模块
├── examples/                  # 示例策略
│   ├── simple_strategy.py
│   ├── multi_factor.py
│   └── grid_strategy.py
├── tests/                     # 单元测试
│   ├── test_strategies.py
│   ├── test_fast_broker.py
│   ├── test_risk.py
│   ├── test_portfolio.py
│   ├── test_notifier.py
│   ├── test_engine.py
│   └── test_ws.py
└── requirements.txt
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
pip install scikit-learn   # ML 策略需要
pip install streamlit plotly streamlit-autorefresh  # 监控面板
```

### 2. 配置

```bash
cp config/config.example.yaml config/config.yaml
# 按需编辑 config/config.yaml
```

### 3. 启动平台

**方式一：交互菜单 (推荐)**

```bash
python start.py
```

菜单提供: 引擎选择 / 策略选择 / QMT 实盘 / 监控面板 / 投资组合分析 / 风险监控 / 单元测试 / 配置查看重置等 16 项功能。

**方式二：直接启动新版引擎 (WebSocket + 异步)**

```bash
python engine.py config/config.yaml
```

**方式三：旧版引擎 (HTTP 轮询)**

```bash
python main.py
```

**方式四：新闻驱动**

```bash
python main_news.py
```

**方式五：Web 监控面板**

```bash
streamlit run dashboard.py
```

## 策略说明

### 1. 均线交叉策略
- 金叉 (短均上穿长均) → 买入
- 死叉 (短均下穿长均) → 卖出
- 参数: `short_window`, `long_window`

### 2. 动量策略
- 过去 N 日涨幅 > 阈值 → 买入
- 涨幅回落 → 卖出
- 参数: `lookback_period`, `entry_threshold`, `exit_threshold`

### 3. 均值回归策略
- Z 分低于阈值 (超卖) → 买入
- Z 分回归均值 → 卖出
- 参数: `lookback_period`, `entry_threshold`, `exit_threshold`

### 4. 多因子策略 (MultiFactor)
- 综合 动量 / 均值回归 / 波动率 / 成交量 四个因子
- 每个因子 Z-score 标准化后加权求和
- 得分高 → 买入,得分低 → 卖出
- 示例: `python examples/multi_factor.py`

### 5. 网格策略 (Grid)
- 在 [lower, upper] 区间布置 N 条等距网格
- 每跌一格 → 买入 1 单位,每涨一格 → 卖出 1 单位
- 整体止损 + 上轨强制平仓
- 示例: `python examples/grid_strategy.py`

### 6. ML 机器学习策略
- 特征: RSI / MACD / 布林带 / 收益率 / 波动率 / 量比 / 均线交叉 (20 维)
- 模型: RandomForest (基线) / GradientBoosting (可选)
- 标签: 未来 5 日涨跌 {-1, 0, 1}
- 配置项: `train_window`, `prediction_horizon`, `confidence_threshold`, `retrain_interval`

### 7. 新闻驱动策略
- 多源财经新闻 + 情感评分
- 正面新闻 → 买入 / 负面新闻 → 卖出

## 风险控制

```python
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide
from src.execution.tpsl_monitor import TPSLMonitor, TPSLConfig

# 交易前风控
rm = RiskManager(
    max_position_size=0.10,    # 单股最大 10%
    max_drawdown=0.20,         # 最大回撤 20%
    max_daily_loss=0.02,       # 单日最大亏损 2%
    max_total_position=0.95,   # 总仓位上限 95%
    stop_loss=0.05,            # 默认止损 5%
    take_profit=0.10,          # 默认止盈 10%
)
result = rm.check_order(OrderRequest("x", OrderSide.BUY, 1000, 10.0, 1_000_000))
if not result.allowed:
    print(f"拦截: {result.reason}")

# 止损止盈监控 (支持固定 / 追踪 / 分批)
tpsl = TPSLMonitor(default_config=TPSLConfig(
    stop_loss=0.05, take_profit=0.10, trailing_stop=0.03,
    partial_tp_levels=[0.05, 0.10],
))
tpsl.register_position("sh600000", entry_price=10.0, quantity=1000)
events = tpsl.on_quote("sh600000", 11.0)   # 触发止盈
```

引擎 (`engine.py`) 已自动集成:
- 每次报价 → 调用 TP/SL 监控器
- 每次买入前 → 调用 `RiskManager.check_order`
- 任何拦截 / 触发 → 推送通知 + 记录日志

## 投资组合分析

```python
from src.analysis.portfolio import (
    load_trades_from_state, generate_report, format_text_report
)
from src.utils.state_manager import StateManager

sm = StateManager(db_path="data/trading_state.db")
trades = load_trades_from_state(sm, limit=5000)
rpt = generate_report(trades, initial_capital=1_000_000)
print(format_text_report(rpt))
```

输出包含: 夏普 / 索提诺 / 卡玛 / 最大回撤 / VaR / 胜率 / 利润因子 / 月度收益表 / 标的表现 / 策略表现。

## 通知系统

```python
from src.notification.notifier import build_manager_from_config, Notification

mgr = build_manager_from_config({
    "console": {"enabled": True, "use_color": True},
    "file": {"enabled": True, "log_dir": "logs"},
    "telegram": {"enabled": True, "bot_token": "...", "chat_id": "..."},
    "dingtalk": {"enabled": True, "webhook_url": "...", "secret": "..."},
})
await mgr.start()
await mgr.notify(Notification(
    type=NotificationType.TRADE,
    level=NotificationLevel.SUCCESS,
    title="买入 sh600000",
    message="¥10.00 x 1000股",
))
```

## 回测系统

```python
from src.backtest.backtester import Backtester
from src.strategies.momentum_strategy import MomentumStrategy

strategy = MomentumStrategy(lookback_period=20, threshold=0.03)
bt = Backtester(initial_capital=1_000_000, commission=0.0003, slippage=0.001,
                stamp_tax=0.001)
results = bt.run_backtest(strategy, data, "AAPL")

print(f"总收益:     {results['total_return']:.2%}")
print(f"年化收益:   {results['annualized_return']:.2%}")
print(f"最大回撤:   {results['max_drawdown']:.2%}")
print(f"夏普:       {results['sharpe_ratio']:.3f}")
print(f"索提诺:     {results['sortino_ratio']:.3f}")
print(f"卡玛:       {results['calmar_ratio']:.3f}")
print(f"胜率:       {results['win_rate']:.2%}")
print(f"利润因子:   {results['profit_factor']:.2f}")
```

### 参数优化

```python
from src.backtest.optimizer import StrategyOptimizer

opt = StrategyOptimizer(MomentumStrategy, data, "AAPL", metric="sharpe_ratio")
results = opt.optimize({
    'lookback_period': [10, 20, 30, 40],
    'threshold': [0.02, 0.03, 0.04, 0.05]
})
print(results.head())
```

## Web 监控面板

启动 `streamlit run dashboard.py` 后访问 http://localhost:8501:

- **实时行情**: 多股行情看板 + K 线图
- **策略回测**: 含 ML 策略,交互式回测 + 绩效图表
- **参数优化**: 参数搜索 + 结果排序
- **投资组合**: 持仓管理 + 手动交易
- **新闻情感**: 市场情感 + 热门股票 + 行业排名
- **风险监控**: 风控状态 + 实时 SL/TP 测试 + 组合分析图表
- **系统状态**: 账户信息 + 引擎状态

## WebSocket 健康监控

`WSQuoteClient` 内置:
- **指数退避 + 抖动** 重连 (`compute_backoff`)
- **数据校验** `validate_quote` (过滤价格 / 涨跌幅异常)
- **状态机** `WSStatus` (DISCONNECTED → CONNECTING → CONNECTED → RECONNECTING → FAILED)
- **健康检查** `is_healthy()` (超时 30s 无消息视为不健康)
- **统计** `get_stats()` (消息数 / 失效数 / 重连次数 / 延迟)

```python
from src.data.realtime.ws_client import WSQuoteClient
c = WSQuoteClient()
c.register_status_callback(lambda s: print(f"WS status: {s.value}"))
print(c.get_stats())
```

## 数据源

| 数据源 | 类型 | 适用 |
|--------|------|------|
| akShare | 历史/实时 | A 股 (推荐) |
| 东方财富 | 历史/实时 | A 股 |
| 新浪财经 | 实时 | A 股 |
| 腾讯财经 | 实时 | A 股 |
| yfinance | 历史 | 美股 / 港股 |

## 运行测试

```bash
python -m pytest tests/ -v
```

测试覆盖: 策略 (5) / 风控 (11) / TP/SL (10) / 组合分析 (20) / 通知 (8) / 引擎 (7) / WebSocket (21)。共 86 用例。

## 开发计划

- [x] 实时动量策略
- [x] 实时均值回归策略
- [x] 增强回测指标
- [x] 参数优化器
- [x] Web 监控面板
- [x] 多数据源自动回退
- [x] ML 机器学习策略
- [x] 止损止盈 (固定 / 追踪 / 分批)
- [x] 交易前风控
- [x] 投资组合分析 (夏普 / Sortino / Calmar / VaR)
- [x] 通知系统 (Telegram / 钉钉 / 微信 / Webhook)
- [x] WebSocket 重连 + 数据校验
- [x] 单元测试 86 用例
- [ ] 更多券商 (恒生 / 华泰)
- [ ] 移动端推送
- [ ] 多账户管理
- [ ] 策略组合 (多策略协同)

## 许可证

MIT License
