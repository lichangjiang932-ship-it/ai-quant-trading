"""
快速启动脚本 - 支持新旧两种引擎 + 多种策略 + 工具
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def quick_start():
    print("量化交易平台 - 快速启动")
    print("=" * 50)
    print("【新版引擎 - WebSocket 实时推送】")
    print("  1. 新版引擎 - 均线交叉策略")
    print("  2. 新版引擎 - 动量策略")
    print("  3. 新版引擎 - 均值回归策略")
    print("  4. 新版引擎 - ML 机器学习策略")
    print("【旧版引擎 - HTTP 轮询】")
    print("  5. 旧版引擎 - 均线交叉策略")
    print("  6. 旧版引擎 - 动量策略")
    print("  7. 旧版引擎 - 均值回归策略")
    print("【券商】")
    print("  8. 使用 QMT 券商")
    print("【工具与示例】")
    print("  9. 启动 Web 监控面板")
    print("  10. 新闻驱动交易")
    print("  11. 多因子策略回测示例")
    print("  12. 网格策略回测示例")
    print("  13. 投资组合业绩分析")
    print("  14. 风险监控与压力测试")
    print("  15. 运行全部单元测试")
    print("【配置】")
    print("  c. 查看/编辑当前配置")
    print("  r. 重置为默认配置")
    print("  q. 退出")
    print("=" * 50)

    choice = input("请选择: ").strip()

    actions = {
        '1': lambda: start_engine('cross_ma'),
        '2': lambda: start_engine('momentum'),
        '3': lambda: start_engine('mean_reversion'),
        '4': lambda: start_engine('ml'),
        '5': lambda: start_legacy('cross_ma'),
        '6': lambda: start_legacy('momentum'),
        '7': lambda: start_legacy('mean_reversion'),
        '8': lambda: start_qmt(),
        '9': lambda: start_dashboard(),
        '10': lambda: start_news_trading(),
        '11': lambda: run_example('examples/multi_factor.py'),
        '12': lambda: run_example('examples/grid_strategy.py'),
        '13': lambda: run_portfolio_analysis(),
        '14': lambda: run_risk_monitor(),
        '15': lambda: run_all_tests(),
    }

    if choice in actions:
        actions[choice]()
    elif choice == 'c':
        show_config()
    elif choice == 'r':
        reset_config()
    elif choice.lower() == 'q':
        return
    else:
        print("无效选择")


def start_engine(strategy_type='cross_ma'):
    """启动新版异步引擎"""
    print(f"\n启动新版引擎 - {strategy_type} 策略...")
    print("特点: WebSocket实时推送 + 异步非阻塞 + SQLite持久化\n")

    config = _make_config(strategy_type)
    _write_config(config)

    from engine import run_engine
    run_engine('config/config.yaml')


def start_legacy(strategy_type='cross_ma'):
    """启动旧版引擎"""
    print(f"\n启动旧版引擎 - {strategy_type} 策略...")
    print("特点: HTTP轮询 + 同步阻塞\n")

    config = _make_config(strategy_type)
    _write_config(config)

    from main import TradingPlatform
    platform = TradingPlatform('config/config.yaml')
    platform.setup()
    platform.start()


def start_news_trading():
    """启动新闻驱动交易"""
    print("\n启动新闻驱动交易平台...\n")
    try:
        from main_news import main as news_main
        news_main()
    except Exception as e:
        print(f"启动失败: {e}")


def start_qmt():
    print("\n启动 QMT 交易...")
    account_id = input("请输入资金账号: ").strip()
    mini_qmt_path = input("请输入 miniQMT 路径: ").strip()
    symbols_input = input("请输入监控股票（逗号分隔, 回车用默认）: ").strip()

    if not account_id or not mini_qmt_path:
        print("账号和路径不能为空, 已回退到模拟券商")
        account_id, mini_qmt_path = "", ""

    symbol_list = [s.strip() for s in symbols_input.split(',') if s.strip()] if symbols_input else ['sh600000', 'sz000001']

    config = _make_config('cross_ma')
    config['trading']['symbols'] = symbol_list
    config['broker'] = {
        'type': 'qmt' if account_id else 'simulated',
        'account_id': account_id,
        'mini_qmt_path': mini_qmt_path,
    }
    _write_config(config)

    from main import TradingPlatform
    platform = TradingPlatform('config/config.yaml')
    platform.setup()
    platform.start()


def start_dashboard():
    print("\n启动 Web 监控面板...")
    print("面板将在浏览器中打开, 请稍候...\n")
    try:
        import subprocess
        subprocess.run(["streamlit", "run", "dashboard.py"], check=False)
    except FileNotFoundError:
        print("未找到 streamlit, 请先安装: pip install streamlit plotly")
    except Exception as e:
        print(f"启动失败: {e}")


def run_example(path):
    print(f"\n运行示例: {path}\n")
    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        return
    import runpy
    runpy.run_path(path, run_name="__main__")


def run_portfolio_analysis():
    print("\n=== 投资组合业绩分析 ===\n")
    try:
        from src.utils.state_manager import StateManager
        from src.analysis.portfolio import load_trades_from_state, generate_report, format_text_report
        from src.execution.fast_broker import FastBroker

        sm = StateManager(db_path="data/trading_state.db")
        trades = load_trades_from_state(sm, limit=5000)
        if not trades:
            print("暂无交易记录, 请先运行回测或交易")
            return

        try:
            initial = FastBroker(initial_capital=1_000_000).initial_capital
        except Exception:
            initial = 1_000_000

        rpt = generate_report(trades, initial_capital=initial)
        print(format_text_report(rpt, initial_capital=initial))

        eq = rpt.get("equity_curve")
        if eq is not None and not eq.empty:
            print(f"\n权益曲线点数: {len(eq)}")
            print(f"起始权益: ¥{eq['equity'].iloc[0]:,.2f}")
            print(f"最终权益: ¥{eq['equity'].iloc[-1]:,.2f}")

        sym_df = rpt.get("by_symbol")
        if sym_df is not None and not sym_df.empty:
            print("\n按标的盈亏:")
            print(sym_df.to_string(index=False))
    except Exception as e:
        print(f"分析失败: {e}")


def run_risk_monitor():
    print("\n=== 风险监控压力测试 ===\n")
    try:
        from src.execution.risk_manager import RiskManager, OrderRequest, OrderSide
        from src.execution.tpsl_monitor import TPSLMonitor, TPSLConfig, TPSLReason

        rm = RiskManager(max_position_size=0.10, max_drawdown=0.20,
                         stop_loss=0.05, take_profit=0.10, max_daily_loss=0.02)
        rm.current_equity = 1_000_000
        rm.peak_equity = 1_000_000
        rm.reset_daily_pnl()

        print("风控配置:")
        print(f"  最大仓位: {rm.max_position_size:.0%}")
        print(f"  最大回撤: {rm.max_drawdown:.0%}")
        print(f"  止损: {rm.stop_loss:.0%}")
        print(f"  止盈: {rm.take_profit:.0%}")
        print(f"  单日最大亏损: {rm.max_daily_loss:.0%}")

        cases = [
            ("正常买入 1000股 @ 10", OrderRequest("x", OrderSide.BUY, 1000, 10.0, 1_000_000), True),
            ("超限买入 20000股 @ 10", OrderRequest("x", OrderSide.BUY, 20000, 10.0, 1_000_000), False),
            ("回撤过大时买入", None, False),
        ]
        for name, req, expected in cases[:2]:
            r = rm.check_order(req)
            status = "[OK] 通过" if r.allowed == expected else "[FAIL] 异常"
            print(f"\n  {name}: {status} | {r.reason[:60]}")

        rm.current_equity = 700_000
        r = rm.check_order(OrderRequest("x", OrderSide.BUY, 100, 10.0, 700_000))
        print(f"\n  回撤过大时买入: {'[OK] 拦截' if not r.allowed else '[FAIL] 漏检'} | {r.reason[:60]}")

        print("\n止损止盈监控器:")
        m = TPSLMonitor(default_config=TPSLConfig(stop_loss=0.05, take_profit=0.10))
        m.register_position("sh600000", 10.0, 1000)
        for price in [10.5, 11.0, 11.2, 9.3]:
            evs = m.on_quote("sh600000", price)
            for ev in evs:
                print(f"  价格 {price}: 触发 {ev.reason.value} | 盈亏 {ev.pnl_pct:.2%}")
    except Exception as e:
        print(f"压力测试失败: {e}")
        import traceback
        traceback.print_exc()


def run_all_tests():
    print("\n运行全部单元测试...\n")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    print(f"\n退出码: {result.returncode}")


def show_config():
    config_file = 'config/config.yaml'
    if not os.path.exists(config_file):
        print("配置文件不存在, 正在创建默认配置...")
        reset_config()
        return
    import yaml
    with open(config_file, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    print(yaml.dump(cfg, allow_unicode=True, default_flow_style=False))


def reset_config():
    """从 example 复制一份到 config.yaml"""
    import shutil
    src = 'config/config.example.yaml'
    dst = 'config/config.yaml'
    if not os.path.exists(src):
        print(f"示例配置不存在: {src}")
        return
    os.makedirs('config', exist_ok=True)
    shutil.copy2(src, dst)
    print(f"已重置 {dst}")


def _make_config(strategy_type):
    config = {
        'trading': {
            'initial_capital': 1000000,
            'auto_trade': False,
            'update_interval': 3,
            'symbols': ['sz000001', 'sz000002', 'sh600000', 'sh600104'],
        },
        'broker': {'type': 'simulated'},
        'strategy': {'type': strategy_type},
        'risk': {
            'max_position_size': 0.1,
            'max_drawdown': 0.2,
            'stop_loss': 0.05,
            'take_profit': 0.1,
            'max_daily_loss': 0.02,
        },
        'commission': {
            'rate': 0.0003,
            'min': 5,
            'stamp_tax': 0.0005,
        },
        'notification': {
            'enabled': True,
            'min_level': 'info',
            'console': {'enabled': True, 'use_color': True},
            'file': {'enabled': True, 'log_dir': 'logs', 'filename': 'notifications.log'},
        },
    }
    if strategy_type == 'cross_ma':
        config['strategy'].update({'short_window': 5, 'long_window': 20})
    elif strategy_type == 'momentum':
        config['strategy'].update({'lookback_period': 20, 'entry_threshold': 0.03})
    elif strategy_type == 'mean_reversion':
        config['strategy'].update({'lookback_period': 20, 'entry_threshold': 2.0, 'exit_threshold': 0.5})
    elif strategy_type == 'ml':
        config['strategy'].update({
            'type': 'ml',
            'lookback_period': 60,
            'train_window': 300,
            'prediction_horizon': 5,
            'confidence_threshold': 0.55,
        })
        config['ml'] = {
            'enabled': True, 'model_type': 'random_forest',
            'train_window': 300, 'prediction_horizon': 5,
            'confidence_threshold': 0.55, 'retrain_interval': 60, 'lookback_period': 60,
        }
    return config


def _write_config(config):
    import yaml
    os.makedirs('config', exist_ok=True)
    with open('config/config.yaml', 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


if __name__ == "__main__":
    try:
        quick_start()
    except KeyboardInterrupt:
        print("\n\n已退出")
    except EOFError:
        pass
