# 量化交易平台

一个面向个人使用的 A 股量化交易平台，支持实时行情、策略回测、模拟盘、风险控制，并保留 QMT 实盘接入能力。

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
- **多券商**: 模拟券商 / QMT 实盘 / 同花顺实盘 (guling-trader) / FastBroker (0.05ms 延迟)
- **异步引擎**: WebSocket 推送 + 异步非阻塞 + SQLite 持久化
- **通知系统**: 控制台 / 日志 / Telegram / 钉钉 / 微信企业号 / Webhook

### 可视化与工具
- **Web 监控面板**: Streamlit + Plotly，K线 / 回测 / 优化 / 持仓 / 风险一站式
- **回测系统**: 多指标 + 多股组合 + 参数优化 + ML 策略回测
- **新闻情感**: 多源 + 情感评分 + 行业排名 + 龙虎榜
- **市场监控**: 涨跌停告警、异常波动监控

### 策略研究增强 (2026-08)
- **新闻涨幅因子引擎** (`src/news/news_factor.py`): 每日自动抓取热点新闻（新浪7x24 / 华尔街见闻 / 东财 / 新浪财经）→ A股事件词典情绪打分 → 0-100 因子分 (bull/bear/neutral)。已接入自托管买入筛选器：强利好加分、重大利空否决、消息兑现日(涨幅≥5%)防追高
- **巨型 IPO 上市日避险守卫** (`src/news/ipo_guard.py`): 13 个历史巨型 IPO 事件研究证实上市日沪深300平均 -0.98%（69% 下跌）；上市日 T 及 T+1 自动禁止新开仓
- **消息反应速度事件研究** (`news_anticipation_backtest.py`): 11736 个交易日样本证实"价格提前反应"——大涨次日开盘买入持有 1 日 -0.34% 胜率仅 40%，追消息接盘
- **收盘复盘** (`/api/review`): 收盘后自动统计当日成交与盈亏，按盈亏提炼策略建议供实盘参考

---

## 个人落地建议

当前默认运行在 **A 股模拟盘**，并已实现：

- 买入 100 股整数手、0.01 元报价单位和主板/创业板/科创板/北交所涨跌停校验。
- T+1 可卖数量控制；持仓表会同时显示总持仓和当前可卖数量。
- 市价单滑点、佣金、最低佣金、股票卖出万分之五印花税（ETF 免收）和含买入费用的持仓成本。
- 模拟账户、订单、持仓、日内盈亏、峰值净值和开仓锁跨重启恢复。
- 顶部“允许开仓/开仓已锁”按钮；锁定后只允许卖出减仓。
- 回测按“收盘产生信号、下一交易日开盘成交”，并计入交易成本。
- AI 选股以确定性“潜力评分”为主，综合 120 日价格位置、趋势、动量、量能、RSI 与波动率，AI 负责复核和解释。
- 每条推荐给出买入区、目标价、止损价、风险收益比，并按当前本金计算建议股数、目标情景收益和止损情景亏损。
- 服务保持运行时会在交易日 08:20 自动生成盘前计划；模型只使用目标交易日前已完成的日线，并用历史相似行情校准上涨概率和三日预期收益。
- 09:30 后必须按真实价格二次复核；高开超过限制、突破最高买价、跌破止损或硬风控不通过时自动取消，不追高。
- 持仓退出计划会给出继续持有、减仓、卖出或 T+1 待卖，并显示动态保护位、目标价与当前可卖数量。
- 策略页会结合真实持仓解释“观望”：空仓时表示暂不买入，持仓时表示继续持有，并明确保护位、目标位和可卖数量。
- 回测股票可直接从自选股下拉选择；交易页支持按股数或按金额买入，金额会自动向下换算为不超预算的 100 股整手并计入费用。
- 专业决策门禁会检查日线完整性、最新日期、异常价格关系、零成交量和异常复权跳变；未通过时禁止自动买卖。
- 实时行情改为按股票逐源补全：单个行情源只返回部分代码时继续回退，并为每条报价记录实际来源、接收时间和源健康状态。
- 潜力策略要求连续两日确认后才入场，过滤低流动性与异常 K 线，并按信号强度和近期波动动态降低单笔仓位。
- AI 推荐不能直接绕过订单系统：执行前会重新取价，检查信号时效、价格漂移、历史样本、数据质量、置信度和盈亏比，审批结果持久化留痕。
- 盘前计划先判断上证指数市场环境：进攻环境使用正常仓位，中性环境自动降仓，防守环境暂停新开仓。
- 所有候选共享“当日新增资金预算”，避免每只股票分别合规但合计超配；开盘后指数快速下跌还会触发市场冲击保护。
- 交易页搜索任意股票后自动显示即时潜力分析；策略页和回测页默认使用同一套潜力模型。

建议先连续运行模拟盘至少 20 个交易日，确认行情稳定、策略样本外有效、最大回撤可接受后，再配置 QMT。任何策略都不能保证盈利；实盘前应把单笔风险限制在总资产的 0.5%–1%，并从小资金开始。

个人使用默认只监听 `127.0.0.1`，不会向局域网开放。若确需远程访问，请先增加身份认证和 HTTPS，不要直接将交易接口暴露到公网。

---

## 部署指南（新机器从零跑起来）

主界面是 **AI 量化交易驾驶舱**（FastAPI + 单页前端），提供实时行情、分时/日 K 图、AI 自动选股、自选股、模拟下单。以下是在一台干净的电脑上把它跑起来的完整步骤。

### 1. 前置要求

- **Python 3.11**（建议 3.10–3.12）。确认：`python --version`
- **Git**（用于克隆）。
- **国内网络直连**：行情走东方财富 / 腾讯 / 通达信(mootdx TCP)，需要能直连境内接口，**不要挂代理/VPN**（否则东财接口会连不上）。

### 2. 克隆代码

```bash
git clone https://github.com/lichangjiang932-ship-it/ai-quant-trading.git
cd ai-quant-trading
```

### 3. 创建虚拟环境并安装依赖

```bash
# Windows (PowerShell)
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

> `curl_cffi` 是**关键依赖**（以 Chrome TLS 指纹绕过东财对 Python 的屏蔽），已在 `requirements.txt` 中，务必装上，否则资金流/涨停池/北向等接口会超时返回空。

### 4. 生成配置文件

配置文件不在仓库里（含个人设置，已被 `.gitignore` 排除），需从模板复制一份：

```bash
# Windows
copy config\config.example.yaml config\config.yaml
# macOS / Linux
cp config/config.example.yaml config/config.yaml
```

行情、自选股、模拟盘**无需密钥即可运行**。仅当你要用 **AI 自动选股 / 多智能体分析**（调用大模型）时，才需要下一步配置 API Key。

### 5. （可选）配置 AI 大模型密钥

AI 选股默认走 **DeepSeek**。在项目根目录新建 `.env` 文件（同样已被 gitignore，不会上传）：

```bash
# .env
DEEPSEEK_API_KEY=你的_deepseek_api_key
```

> Key 在 https://platform.deepseek.com 注册充值后创建。不配也能用平台的其它全部功能，只是 AI 选股会分析失败并转为观望提示。

### 6. 启动驾驶舱

```bash
python frontend/api_server.py
```

启动后浏览器打开 **http://localhost:8080** 即可使用：

- 顶部搜索任意 A 股（代码或名称，如 `600519` / `贵州茅台`）
- 「交易」页标题右侧 **☆ 加自选**，收藏的股票会出现在首页「自选行情」并持久化到 `config.yaml`
- K 线图右上角切换 **分时 / 日K**，分时图盘中每 20 秒实时刷新
- 「AI 选股」页顶部显示 **今日盘前计划**；默认 08:20 分析自选股，也可手动后台生成。开盘后按钮才会启用，并再次校验真实价格
- 「AI 选股」页点 **开始AI选股**，AI 从全市场热门股漏斗筛选 → 多智能体深度分析 → 给出买入计划，可一键确认下到模拟盘（经硬风控）

> 端口可在 `config/config.yaml` 的 `server.port` 修改（默认 8080）。
> `premarket.auto_execute` 默认是 `false`。只有明确改为 `true` 后，系统才会在 09:31 对排名第一的计划执行模拟买入；当前版本不会连接真实券商自动下单。

### 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 行情/资金流查询转圈或返回空 | 检查 `curl_cffi` 是否装上；关闭代理/VPN 直连境内 |
| AI 选股全部“分析失败/观望” | `.env` 未配 `DEEPSEEK_API_KEY` 或余额不足 |
| 端口 8080 被占用 | 改 `config/config.yaml` 的 `server.port` |
| 克隆后没有 `config.yaml` / `.env` | 正常，按第 4、5 步从模板生成（敏感文件不入库） |

---

## 项目结构

```
量化/
├── main.py                    # 旧版 HTTP 轮询引擎
├── engine.py                  # 新版 WebSocket 异步引擎 (推荐)
├── main_news.py               # 新闻驱动交易平台
├── start.py                   # 快速启动脚本
├── dashboard.py               # Web 监控面板 (Streamlit)
├── ipo_drain_backtest.py       # 巨型IPO上市日事件研究回测 (13个历史事件)
├── news_anticipation_backtest.py # 消息反应速度事件研究回测 (11736交易日样本)
├── config/                    # 配置文件
│   ├── config.example.yaml   # 配置模板
│   └── config.yaml           # 默认配置
├── src/
│   ├── news/                  # 新闻模块
│   │   ├── news_fetcher.py   # 多源新闻抓取 (新浪7x24/华尔街见闻/东财/新浪/财联社)
│   │   ├── news_analyzer.py  # 新闻分析器
│   │   ├── news_factor.py    # ★ 新闻涨幅因子引擎 (事件词典→0-100因子分)
│   │   ├── ipo_guard.py      # ★ 巨型IPO上市日避险守卫
│   │   └── sentiment/        # 情感分析
│   ├── research/              # ★ 股票研究 (速览卡/可比估值/事件情景)
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

## 实盘交易（基金 & 股票，项目内完成，不依赖 WorkBuddy）

项目内置完整交易层 `src/trading/`，通过 CLI 和 HTTP API 直接完成真实交易。

### 双账户体系

系统支持**模拟盘 + 实盘**双账户并行：

| 账户 | 交易对象 | 资金 | 入口 |
|------|---------|------|------|
| 模拟盘 (paper) | A 股虚拟交易 | 虚拟资金 (默认 100 万) | 交易页下单 / `/api/order` |
| 实盘 (live) | 基金(爱基金) + 股票(同花顺) | 真实资金 | `trade.py` / `/api/fund/*` `/api/live/*` |

- 模拟盘：FastBroker 虚拟撮合，可随意练习，风控规则与实盘一致
- 实盘：真实扣款/真实持仓，受 `trading.auto_trade` 门禁与交互确认保护
- 前端设置页「当前账户」可切换查看两个账户；`/api/accounts` 返回双账户对比
- `GET /api/account?mode=paper|live` 指定模式；默认 paper 兼容旧版

### 前置准备

```bash
# 1. 基金: 初始化爱基金凭证 (INIT_TOKEN 在同花顺 App → 理财 → 基金 Skill 页面)
D:/py/python.exe -c "from aijijin_sdk import init; init('你的INIT_TOKEN')"

# 2. 股票: 运行 guling-trader.exe 并配对, 将 agent_token 填入 config.yaml
#    broker.guling_agent_token = "你的agent_token"
```

### CLI 交易 (`python trade.py`)

```bash
# ---- 基金 (爱基金) ----
python trade.py fund holdings                 # 基金 + 钱包持仓
python trade.py fund info 000001              # 基金详情 (费率/风险/购买规则)
python trade.py fund buy 000001 1000          # 申购 1000 元 (申购前展示费率+风险提示)
python trade.py fund buy 000001 1000 --bank   # 银行卡支付
python trade.py fund redeem 000001 500        # 赎回 500 份 (账户自动获取 + 到账预估)
python trade.py fund redeem 000001 500 账户ID  # 指定账户赎回
python trade.py fund orders                   # 最近交易记录 (中文状态)
python trade.py fund order <单号>              # 订单详情 (中文状态判定)
python trade.py fund revoke <单号>             # 撤单
python trade.py fund init                      # 凭证初始化指引
python trade.py fund status                   # 凭证/设备授权/Work Token 检查

# ---- 股票 (guling-trader/同花顺实盘) ----
python trade.py stock status                  # 账户 + 持仓 + 在飞委托
python trade.py stock buy 600519 100          # 市价买入
python trade.py stock buy 600519 100 --price 1700.0   # 限价买入
python trade.py stock sell 600519 100         # 卖出
python trade.py stock cancel <委托号>          # 撤单
python trade.py stock connect                 # 测试 guling-trader 连接
```

所有真实下单命令默认需要输入 `y` 确认；加 `--yes` 跳过。
股票实盘受 `config.yaml` 的 `trading.auto_trade` 门禁，未开启时只记录不执行。

### HTTP API (FastAPI, `frontend/api_server.py`)

| 端点 | 说明 |
|------|------|
| `GET /api/fund/holdings` | 基金 + 钱包持仓 |
| `GET /api/fund/info/{code}` | 基金详情 (费率/风险/购买规则) |
| `POST /api/fund/buy` | 基金申购 `{fund_code, amount, pay_type}` |
| `POST /api/fund/redeem` | 基金赎回 `{fund_code, share_vol, trans_account_id}` |
| `GET /api/fund/orders?cust_id=` | 基金交易记录 (含中文状态) |
| `GET /api/fund/order/{serial}` | 基金订单详情 (含中文状态) |
| `POST /api/fund/revoke` | 基金撤单 `{serial}` |
| `GET /api/live/status` | 股票账户 + 持仓 + 委托 |
| `POST /api/live/order` | 股票下单 `{symbol, side, quantity, price?}` |
| `POST /api/live/cancel` | 股票撤单 `{entrust_no}` |
| `GET /api/news/factors` | 当日新闻涨幅因子（热点新闻→涉及个股 0-100 因子分） |
| `POST /api/news/factors/refresh` | 强制刷新当日新闻因子 |
| `GET /api/ipo/guard` | 巨型 IPO 上市日避险守卫状态（今日是否上市/未来 14 天预警） |
| `GET /api/review?date=` | 收盘复盘报告（当日成交/盈亏/策略建议） |
| `POST /api/review/generate` | 手动生成收盘复盘 |

### 安全机制

| 层级 | 说明 |
|------|------|
| 凭证隔离 | 基金 token 在 `~/.aijijin/credentials.json`；股票密码不离开同花顺 |
| auto_trade 门禁 | `trading.auto_trade: false` 时股票只记录不执行 |
| 交互确认 | CLI 真实下单需输入 `y`；API 需显式传参 |
| 幂等键 | 股票下单带 `client_order_id`，重复调用不重复下单 |

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
| mootdx (通达信 TCP) | 实时/历史 | A 股 (不封 IP, 首选) |
| 东方财富 | 历史/实时 | A 股 (公告/研报/龙虎榜/两融/资金流) |
| 新浪财经 | 实时 | A 股 + 7x24 直播快讯 |
| 腾讯财经 | 实时 | A 股 (PE/PB/市值/换手率) |
| 同花顺 | 信号/研报 | 热点题材/一致预期/机构评级 |
| 华尔街见闻 | 实时新闻 | 7x24 财经快讯 |
| 第一财经 | 实时新闻 | 财经资讯 |
| 财联社 | 实时新闻 | 电报快讯 |
| 巨潮资讯 | 公告 | 法定信息披露 (回退源) |
| yfinance | 历史 | 美股 / 港股 |

新闻聚合共 7 个渠道: 新浪滚动 / 新浪直播 / 东方财富 / 财联社 / 同花顺 / 华尔街见闻 / 第一财经，自动去重排序。
市场宽度信号: 涨跌家数 / 涨停跌停 / 赚钱效应 (东财全市场统计)。

## 运行测试

```bash
python -m pytest tests/ -v
```

测试覆盖: 策略 / 风控 / TP/SL / 组合分析 / 通知 / 引擎 / WebSocket / 研究模块 / 新闻因子。共 200 用例。

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
- [x] 单元测试 200 用例
- [x] 新闻涨幅因子引擎 + 巨型IPO上市日避险守卫
- [x] 收盘复盘 (成交/盈亏/策略建议自动生成)
- [x] 消息反应速度事件研究 (价格提前反应实证)
- [ ] 更多券商 (恒生 / 华泰)
- [ ] 移动端推送
- [ ] 多账户管理
- [ ] 策略组合 (多策略协同)

## 许可证

MIT License
