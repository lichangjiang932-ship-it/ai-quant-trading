# 量化交易平台操作手册

适用于：`D:\destok\量化` 个人量化交易系统

---

## 一、快速开始

### 1.1 安装依赖

```bash
cd D:\destok\量化
pip install -r requirements.txt
```

> 可选：如需A股数据源增强，额外安装 `pip install akshare`

### 1.2 启动交易

**方式一：菜单启动（推荐新手）**

```bash
python start.py
```

菜单界面：
```
【新版引擎 - WebSocket实时推送】
  1. 新版引擎 - 均线交叉策略
  2. 新版引擎 - 动量策略
  3. 新版引擎 - 均值回归策略
【旧版引擎 - HTTP轮询】
  4. 旧版引擎 - 均线交叉策略
  5. 旧版引擎 - 动量策略
  6. 旧版引擎 - 均值回归策略
【其他】
  7. 使用QMT券商
  8. 启动Web监控面板
  9. 新闻驱动交易
  0. 查看配置
  q. 退出
```

**建议选 1/2/3（新版引擎）**，优先使用WebSocket实时推送。

**方式二：直接启动（熟悉后）**

```bash
# 新版引擎（WebSocket + 异步）
python engine.py

# 旧版引擎（HTTP轮询）
python main.py

# 新闻驱动交易
python main_news.py
```

**方式三：Web监控面板**

```bash
streamlit run dashboard.py
# 或通过菜单选 8
```

浏览器打开 `http://localhost:8501`

---

## 二、策略详解

### 2.1 均线交叉策略（cross_ma）

**原理**：短期均线上穿长期均线时买入（金叉），下穿时卖出（死叉）

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `short_window` | 5 | 短期均线周期（日） |
| `long_window` | 20 | 长期均线周期（日） |

**适用场景**：趋势行情，震荡市中容易反复假信号

**配置示例**：
```yaml
strategy:
  type: cross_ma
  short_window: 5
  long_window: 20
```

### 2.2 动量策略（momentum）

**原理**：N日涨幅超过阈值买入（追涨），涨幅回落至阈值以下卖出

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `lookback_period` | 20 | 动量计算周期（N日涨幅） |
| `entry_threshold` | 0.03 | 买入触发阈值（3%涨幅） |
| `exit_threshold` | -0.01 | 卖出触发阈值（-1%涨幅） |

**适用场景**：强趋势行情，抓主升浪

**配置示例**：
```yaml
strategy:
  type: momentum
  lookback_period: 20
  entry_threshold: 0.03
```

### 2.3 均值回归策略（mean_reversion）

**原理**：价格偏离均值超过N个标准差时买入（超卖），回归均值时卖出

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `lookback_period` | 20 | 均值计算周期 |
| `entry_threshold` | 2.0 | 入场阈值（Z分数 <-2 买入） |
| `exit_threshold` | 0.5 | 出场阈值（Z分数回归 <0.5 卖出） |

**适用场景**：震荡行情，高抛低吸

**配置示例**：
```yaml
strategy:
  type: mean_reversion
  lookback_period: 20
  entry_threshold: 2.0
  exit_threshold: 0.5
```

### 2.4 新闻驱动策略（NewsStrategy）

**原理**：自动抓取多源财经新闻 → 情感分析评分 → 正面信号买入/负面信号卖出

**启动**：`python main_news.py` 或菜单选 9

---

## 三、配置指南

配置文件：`config/config.yaml`（从 `config.example.yaml` 复制）

### 3.1 监控的股票

```yaml
trading:
  symbols:
    - sz000001    # 平安银行
    - sz000002    # 万科A
    - sh600000    # 浦发银行
    - sh600104    # 上汽集团
```

代码格式说明：

| 格式 | 示例 | 含义 |
|------|------|------|
| `sh` + 6位代码 | `sh600000` | 上海主板 |
| `sz` + 6位代码 | `sz000001` | 深圳主板/创业板 |
| 纯数字（自动识别） | `600000` | 系统自动加前缀 |

### 3.2 自动交易开关

```yaml
trading:
  auto_trade: false   # true=自动交易, false=仅显示信号不交易
```

> **首次使用强烈建议设为 `false`**，观察信号是否合理后再开启自动交易。

### 3.3 风险管理

```yaml
risk:
  max_position_size: 0.1    # 单只股票最大仓位 10%
  max_drawdown: 0.2         # 最大回撤 20%（超过停止交易）
  stop_loss: 0.05           # 止损 -5%
  take_profit: 0.1          # 止盈 +10%
  max_risk_per_trade: 0.01  # 单笔最大风险 1%
```

### 3.4 数据源

```yaml
data_source:
  realtime: eastmoney    # 实时行情：eastmoney / sina / tencent
  history: auto          # 历史数据：auto / akshare / eastmoney / yfinance
```

---

## 四、交易规则说明

### 4.1 A股交易规则

| 规则 | 说明 |
|------|------|
| 交易时间 | 9:30-11:30, 13:00-15:00（周一到周五） |
| 最小单位 | 100股（1手） |
| T+1 | 当日买入，次日才能卖出 |
| 涨跌停 | 主板±10%，科创/创业±20% |

### 4.2 费用计算

| 费用 | 费率 | 说明 |
|------|------|------|
| 佣金 | ≤0.03%（最低5元） | 买卖都收 |
| 印花税 | 0.1% | 仅卖出收取 |
| 过户费 | 0.001% | 买卖都收 |

---

## 五、回测系统

### 5.1 单个股票回测

```python
from src.backtest.backtester import Backtester
from src.strategies.momentum_strategy import MomentumStrategy
from src.data.market_data import MarketData

# 1. 获取数据
data = MarketData().get_stock_data("AAPL", period="2y")

# 2. 创建策略
strategy = MomentumStrategy(lookback_period=20, threshold=0.03)

# 3. 运行回测
bt = Backtester(initial_capital=100000, commission=0.001)
results = bt.run_backtest(strategy, data, "AAPL")

# 4. 查看结果
print(f"总收益率: {results['total_return']:.2%}")
print(f"夏普比率: {results['sharpe_ratio']:.2f}")
print(f"最大回撤: {results['max_drawdown']:.2%}")
print(f"索提诺比率: {results['sortino_ratio']:.2f}")
print(f"胜率: {results['win_rate']:.2%}")
print(f"盈亏比: {results['profit_factor']:.2f}")
```

### 5.2 参数优化

```python
from src.backtest.optimizer import StrategyOptimizer

opt = StrategyOptimizer(MomentumStrategy, data, "AAPL", metric="sharpe_ratio")
results = opt.optimize({
    'lookback_period': [10, 20, 30, 40, 50],
    'threshold': [0.01, 0.02, 0.03, 0.04, 0.05],
})
print(results.head(5))  # 显示最优的5组参数
```

### 5.3 回测指标说明

| 指标 | 含义 | 理想值 |
|------|------|--------|
| **总收益率** | 整个回测期的总盈亏比例 | 越高越好 |
| **年化收益率** | 折算成年化的收益率 | > 10% |
| **夏普比率** | 单位风险获得的超额收益 | > 1.0 |
| **索提诺比率** | 只考虑下行风险的夏普比率 | > 1.5 |
| **卡玛比率** | 年化收益/最大回撤 | > 2.0 |
| **最大回撤** | 最高点到最低点的跌幅 | < 20% |
| **胜率** | 盈利交易占比 | > 40% |
| **盈亏比** | 平均盈利/平均亏损 | > 2.0 |

---

## 六、Web监控面板

启动后访问 `http://localhost:8501`

### 6.1 页面功能

| 页面 | 功能 |
|------|------|
| **实时行情** | 多股行情看板 + 技术指标K线图 |
| **策略回测** | 交互式回测 + 权益曲线/回撤图 |
| **参数优化** | 多参数搜索 + 按指标排序 |
| **投资组合** | 持仓查看 + 手动买入/卖出 |
| **新闻情感** | 热门股票 + 行业情感排名 |
| **系统状态** | 账户信息 + 风控状态 |

### 6.2 K线图操作

- 鼠标悬停查看OHLCV数据
- 滚轮缩放时间范围
- 双击缩放重置
- 拖动平移

---

## 七、引擎架构说明

### 7.1 新版引擎（推荐）

```
engine.py (主入口)
  ├── AsyncEngine        # 异步事件循环引擎
  ├── WSQuoteClient      # WebSocket行情（东方财富）
  ├── RealtimeData       # HTTP行情（备用）
  ├── FastBroker         # 高性能交易执行 (0.008ms)
  ├── StateManager       # SQLite持久化
  ├── RealtimeStrategy   # 策略实例
  ├── PriorityNewsPipeline # 新闻流水线
  └── TradingScheduler   # 交易时段调度
```

**数据流**：
```
WebSocket推送 → 策略 on_tick() → TradingSignal → FastBroker → SQLite
                                             ↓
                                      PriorityNewsPipeline → 新闻信号
```

### 7.2 旧版引擎

```
main.py (主入口)
  ├── RealtimeData       # HTTP轮询(3秒/次)
  ├── SimulatedBroker    # 同步Broker
  ├── TradeLogger        # JSON文件日志
  └── MarketMonitor      # 涨跌停监控
```

---

## 八、常见问题

### Q: 安装依赖报错怎么办？

```bash
# 逐个安装核心依赖
pip install pandas numpy requests pyyaml websockets aiohttp

# 如遇网络问题，使用国内镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q: 行情数据不更新？

1. 检查网络连接
2. 新版引擎会尝试 WebSocket → HTTP轮询自动切换
3. 运行 `python start.py` 选 0 查看配置
4. 手动测试：`python -c "from src.data.realtime.realtime_data import RealtimeData; print(RealtimeData().get_stock_quote('sh600000'))"`

### Q: 如何切换回旧版引擎？

新版引擎完全兼容旧版，新增了 `engine.py`。旧版 `main.py` 仍然可用。
通过 `start.py` 菜单选 4/5/6 使用旧版引擎。

### Q: 如何查看交易日志？

```bash
# 新版引擎（SQLite）
python -c "from src.utils.state_manager import StateManager; sm = StateManager(); print(sm.get_recent_trades(10))"

# 旧版引擎（JSON）
cat logs/trades.json | python -m json.tool
```

### Q: 实盘交易怎么用？

1. 安装QMT客户端并启动miniQMT
2. 设置 `config.yaml`：
   ```yaml
   broker:
     type: qmt
     account_id: "你的资金账号"
     mini_qmt_path: "C:/QMT/miniqmt"
   ```
3. 启动 `python main.py`

---

## 八点五、新增模块 (v2.1)

### 8.5.1 投资组合分析

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

支持的指标:
- 风险调整收益: 夏普、索提诺、卡玛、信息比率
- 风险: 最大回撤及持续期、年化波动率、下行波动率、VaR(95%)、CVaR(95%)、偏度、峰度
- 交易统计: 胜率、盈亏比、利润因子、期望值、最大连盈/连亏、最大单笔盈亏、平均持仓时长
- 归因: 月度收益表、按标的盈亏、按策略盈亏

### 8.5.2 风险控制

#### 交易前风控
```python
from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide

rm = RiskManager(
    max_position_size=0.10,   # 单股最大 10%
    max_drawdown=0.20,        # 最大回撤 20%
    max_daily_loss=0.02,      # 单日最大亏损 2%
    max_total_position=0.95,  # 总仓位上限 95%
    max_orders_per_day=100,   # 单日最多 100 笔
)
result = rm.check_order(OrderRequest(
    symbol="sh600000", side=OrderSide.BUY,
    quantity=1000, price=10.0, portfolio_value=1_000_000
))
if not result.allowed:
    print(f"拦截: {result.reason}")
```

#### 止损止盈 (TP/SL) 监控器
```python
from src.execution.tpsl_monitor import TPSLMonitor, TPSLConfig

tpsl = TPSLMonitor(default_config=TPSLConfig(
    stop_loss=0.05, take_profit=0.10, trailing_stop=0.03,
    partial_tp_levels=[0.05, 0.10],
))
tpsl.register_position("sh600000", entry_price=10.0, quantity=1000)
events = tpsl.on_quote("sh600000", 11.0)
for ev in events:
    print(f"{ev.reason.value}: 盈亏 {ev.pnl_pct:.2%} 数量 {ev.suggested_quantity}")
```

支持: 固定 SL / 固定 TP / 追踪止损 / 多档分批止盈

引擎已自动集成: 每个 tick 检查所有持仓 → 触发时通过 FastBroker 平仓 → 写入 StateManager + 推送通知。

### 8.5.3 通知系统

支持的渠道: 控制台 / 日志文件 / Telegram Bot / 钉钉群机器人 / 微信企业号 / 通用 Webhook。

config.yaml 示例:
```yaml
notification:
  enabled: true
  min_level: info
  console:
    enabled: true
    use_color: true
  file:
    enabled: true
    log_dir: logs
    filename: notifications.log
  telegram:
    enabled: true
    bot_token: "123456:ABC..."
    chat_id: "-1001234567890"
  dingtalk:
    enabled: true
    webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=..."
    secret: "SEC..."
  wechat_work:
    enabled: true
    webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
```

事件类型: TRADE / SIGNAL / RISK / TPSL / SYSTEM / DAILY_REPORT / ERROR / PRICE_ALERT。
级别: INFO / SUCCESS / WARNING / ERROR / CRITICAL。

### 8.5.4 ML 机器学习策略

特征 (20 维): RSI / MACD / 布林带 / 收益率 / 波动率 / 量比 / 均线交叉 / 价格位置 / 量价相关性。
标签: 未来 5 日涨跌 {-1, 0, 1}。
模型: RandomForest (基线) / GradientBoosting (可选)。

config.yaml:
```yaml
ml:
  enabled: true
  model_type: random_forest  # or gradient_boosting
  train_window: 300
  prediction_horizon: 5
  confidence_threshold: 0.55
  retrain_interval: 60
  lookback_period: 60
```

引擎中通过 `strategy.type=ml` 自动加载。

### 8.5.5 WebSocket 健康监控

`WSQuoteClient` 内置:
- **指数退避 + 抖动** 重连 (1s → 60s 上限)
- **数据校验** `validate_quote` 过滤价格/涨跌幅异常
- **状态机** `WSStatus` (5 个状态)
- **健康检查** `is_healthy()` (30s 无消息视为不健康)
- **统计指标** `get_stats()`

订阅状态变化:
```python
from src.data.realtime.ws_client import WSQuoteClient
c = WSQuoteClient()
c.register_status_callback(lambda s: print(f"WS: {s.value}"))
```

### 8.5.6 多因子 / 网格示例策略

```bash
python examples/multi_factor.py    # 多因子综合策略
python examples/grid_strategy.py   # 网格交易策略
```

### 8.5.7 测试

```bash
python -m pytest tests/ -v
```

86 个单元测试,覆盖: 策略 (5) / 风控 (11) / TP/SL (10) / 组合分析 (20) / 通知 (8) / 引擎 (7) / WebSocket (21)。

---

## 九、文件清单

| 文件 | 用途 |
|------|------|
| `engine.py` | **新版引擎主程序**（推荐） |
| `main.py` | 旧版引擎主程序 |
| `main_news.py` | 新闻驱动交易平台 |
| `start.py` | 快速启动菜单 |
| `dashboard.py` | Web监控面板（Streamlit） |
| `config/config.yaml` | 配置文件 |
| `src/utils/async_engine.py` | 异步引擎 |
| `src/utils/state_manager.py` | SQLite持久化 |
| `src/data/realtime/ws_client.py` | WebSocket行情客户端 |
| `src/execution/fast_broker.py` | 高性能交易执行 |
| `src/news/priority_news.py` | 新闻优先级流水线 |
| `src/backtest/optimizer.py` | 参数优化器 |

---

## 十、快速参考

```bash
# 日常使用
cd D:\destok\量化
python start.py                    # 菜单启动
python engine.py                   # 新版引擎直接启动
streamlit run dashboard.py         # Web监控面板

# 回测
python -c "
from src.backtest.backtester import Backtester
from src.strategies.momentum_strategy import MomentumStrategy
from src.data.market_data import MarketData
data = MarketData().get_stock_data('AAPL', period='2y')
r = Backtester().run_backtest(MomentumStrategy(lookback_period=20, threshold=0.03), data)
print(f'收益: {r[\"total_return\"]:.2%}, 夏普: {r[\"sharpe_ratio\"]:.2f}, 回撤: {r[\"max_drawdown\"]:.2%}')
"

# 测试行情
python -c "from src.data.realtime.realtime_data import RealtimeData; q=RealtimeData().get_realtime_quote_eastmoney(['sh600000']); print(q)"

# 实时监控延迟
python -c "
from src.execution.fast_broker import FastBroker
b=FastBroker(); b.update_price('test',10)
import time
t0=time.perf_counter_ns()
b.buy('test',100,10)
print(f'延迟: {(time.perf_counter_ns()-t0)/1e6:.4f}ms')
"
```
