#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
交易命令行工具 — 项目内完成基金 & 股票实盘交易, 不依赖 WorkBuddy。

用法:
  python trade.py fund holdings                    # 基金+钱包持仓
  python trade.py fund buy <代码> <金额> [--wallet|--bank] [--yes]
  python trade.py fund redeem <代码> <份额> <账户ID> [--wallet|--bank] [--yes]
  python trade.py fund orders [--days 30] [--limit 20]
  python trade.py fund order <单号>                # 订单详情
  python trade.py fund revoke <单号>               # 撤单
  python trade.py fund init                        # 提示初始化凭证

  python trade.py stock status                     # 账户+持仓+在飞委托
  python trade.py stock buy <代码> <股数> [--price X] [--yes]
  python trade.py stock sell <代码> <股数> [--price X] [--yes]
  python trade.py stock cancel <委托号>
  python trade.py stock connect                    # 测试连接

示例:
  python trade.py fund buy 000001 1000 --wallet
  python trade.py stock buy 600519 100 --price 1700.0

安全:
  - 真实下单命令默认需输入 y 确认; 加 --yes 跳过
  - 股票实盘受 config.yaml trading.auto_trade 门禁
"""
from __future__ import annotations

import argparse
import sys


def _init_syspath():
    import os
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)


def _yes(confirm: bool, action: str) -> bool:
    if confirm:
        return True
    try:
        ans = input(f"\n⚠️  即将执行真实交易: {action}\n输入 y 确认: ").strip().lower()
        return ans == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def _load_cfg():
    from src.utils.config import Config
    import os
    root = os.path.dirname(os.path.abspath(__file__))
    return Config(os.path.join(root, "config", "config.yaml"))


# ---------------------------------------------------------------------------
# 基金命令
# ---------------------------------------------------------------------------

def _fund_trader():
    from src.trading.fund_trader import FundTrader
    return FundTrader()


def cmd_fund_status(_args):
    """检查爱基金凭证 / 设备授权 / Work Token 状态。"""
    import json
    import os

    print("=== 爱基金凭证状态检查 ===")
    cred_path = os.path.expanduser("~/.aijijin/credentials.json")
    if not os.path.exists(cred_path):
        print("❌ 凭证文件不存在:", cred_path)
        print("   请先执行:  D:/py/python.exe -c \"from aijijin_sdk import init; print('confirm_status=', init('你的INIT_TOKEN'))\"")
        return 1

    try:
        d = json.load(open(cred_path, encoding="utf-8"))
    except Exception as e:
        print(f"❌ 凭证文件损坏: {e}")
        return 1

    dev = d.get("device", {})
    rt = d.get("refresh_token") or {}
    print(f"✅ 凭证文件: {cred_path}")
    print(f"   版本: {d.get('version')}  更新时间: {d.get('updated_at')}")
    print(f"   设备名称: {dev.get('device_name')}  平台: {dev.get('platform')}")
    print(f"   设备哈希: {(dev.get('device_hash') or '')[:12]}...")
    print(f"   Refresh Token: {'✅ 已写入' if rt.get('token') else '❌ 缺失'}")

    print("\n=== Work Token 换取测试 ===")
    from aijijin_sdk import get_work_token
    try:
        token = get_work_token()
        print(f"✅ Work Token 获取成功 (长度 {len(token)}) — 设备已授权, 可以交易!")
        return 0
    except Exception as e:
        name = type(e).__name__
        code = getattr(e, "code", None)
        print(f"❌ Work Token 获取失败: {name}")
        if code:
            print(f"   错误码: {code}")
        print(f"   信息: {e}")
        if str(code) in ("1406", "4001", "4002", "4003"):
            print("\n👉 设备未授权: 请到【同花顺 App → 理财 tab → 基金 Skill 页面】")
            print("   找到设备授权/设备管理入口, 确认这台设备:")
            print(f"   设备名称: {dev.get('device_name')}  平台: {dev.get('platform')}")
            print("   确认完成后重新运行本命令验证。")
        elif "RefreshTokenExpired" in name or str(code) in ("1101", "1102", "1103"):
            print("\n👉 Refresh Token 已失效: 请到同花顺 App 重新获取 INIT_TOKEN")
            print("   然后执行: D:/py/python.exe -c \"from aijijin_sdk import init; init('新TOKEN')\"")
        return 1


def cmd_fund_holdings(_args):
    trader = _fund_trader()
    try:
        holdings = trader.get_all_holdings()
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    from src.trading.fund_trader import format_holdings
    print(format_holdings(holdings))
    return 0


def cmd_fund_buy(args):
    trader = _fund_trader()
    pay_type = "wallet" if args.pay == "wallet" else "bank"
    amount = float(args.amount)

    # 申购前展示基金详情 + 风险等级提示
    try:
        info = trader.get_fund_info(args.code)
        from src.trading.fund_trader import format_fund_info
        print("=== 基金信息 ===")
        print(format_fund_info(info))
        print()
        # 风险等级不匹配提示
        fr = info.get("fundRiskLevel")
        cr = info.get("clientRiskLevel")
        if fr and cr and int(fr) > int(cr):
            print(f"⚠️  风险提示: 产品风险 R{fr} 高于您的风险承受能力 C{cr}!")
            print("    如继续申购请自行确认风险承受能力。")
            print()
    except Exception as e:
        print(f"[提示] 无法获取基金详情: {e}")
        print()

    if not _yes(args.yes, f"基金申购 {args.code} 金额 {amount} 元 (支付: {pay_type})"):
        print("已取消")
        return 1
    try:
        result = trader.buy(
            args.code, amount, pay_type=pay_type,
        )
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    print(f"✅ 申购委托已提交: 单号 {result['appSheetSerialNo']}")
    detail = result.get("detail") or {}
    if detail:
        from src.trading.fund_trader import FundTrader, format_order_detail
        status = FundTrader.judge_order_status(detail)
        print()
        print(format_order_detail(detail, status))
    return 0


def cmd_fund_redeem(args):
    trader = _fund_trader()
    redemption_type = "1" if args.pay == "wallet" else "0"
    account = args.account

    # 未指定账户时: 从持仓自动查找
    if not account:
        try:
            holdings = trader.get_all_holdings()
            found = None
            for r in (holdings.get("fundList") or []):
                if r.get("fundCode") == args.code:
                    detail_list = r.get("fundPositonDetailList") or []
                    if detail_list:
                        acc0 = detail_list[0]
                        found = acc0.get("transactionAccountId") or acc0.get("transActionAccountId")
                    if found:
                        break
            if not found:
                # 兜底: 用钱包账户
                init = trader.subscribe_init(args.code)
                found = trader._pick_account(init, "wallet")
            account = str(found)
            print(f"自动选择账户: {account}")
        except Exception as e:
            print(f"[错误] 无法自动获取账户: {e}")
            return 1

    # 赎回预估展示
    try:
        est = trader.estimate_redeem(args.code, account, float(args.shares))
        print("=== 赎回预估 ===")
        print(f"  基金: {args.code}")
        print(f"  单位净值: {est['nav']}")
        print(f"  可用份额: {est['availableVol']}")
        print(f"  赎回份额: {est['targetVol']}")
        print(f"  预估金额: {est['estimatedAmount']:.2f} 元")
        print(f"  预估费率: {est['feeRatePct']:.4f}%")
        print(f"  预估手续费: {est['estimatedFee']:.2f} 元")
        print(f"  预估到账: {est['estimatedArrival']:.2f} 元")
        print()
    except Exception as e:
        print(f"[提示] 无法获取赎回预估: {e}")
        print()

    if not _yes(args.yes, f"基金赎回 {args.code} 份额 {args.shares} (方式: {args.pay})"):
        print("已取消")
        return 1
    try:
        result = trader.redeem(
            args.code, float(args.shares), account,
            redemption_type=redemption_type,
        )
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    print(f"✅ 赎回委托已提交: 单号 {result['appSheetSerialNo']}")
    detail = result.get("detail") or {}
    if detail:
        from src.trading.fund_trader import FundTrader, format_order_detail
        status = FundTrader.judge_order_status(detail)
        print()
        print(format_order_detail(detail, status))
    return 0


def cmd_fund_orders(args):
    trader = _fund_trader()
    cfg = _load_cfg()
    cust_id = (cfg.get("fund.cust_id", "") or "").strip()
    if not cust_id:
        print("[提示] 未配置 fund.cust_id (config.yaml)。先执行: python trade.py fund buy ... 获取")
        print("       或在 config.yaml 的 fund.cust_id 填入客户 ID 后再查询。")
        return 1
    try:
        rows = trader.get_order_list(
            cust_id, limit=args.limit, start_date=args.start, end_date=args.end,
        )
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    if not rows:
        print("(无交易记录)")
        return 0
    from src.trading.fund_trader import FundTrader
    print(f"交易记录 ({len(rows)} 条):")
    biz_map = {"022": "申购", "023": "赎回", "024": "分红", "aip": "定投", "buy": "申购", "sell": "赎回"}
    for r in rows:
        name = r.get("fundName", "")
        code = r.get("fundCode", "")
        biz = biz_map.get(str(r.get("businessCode", "")), str(r.get("businessCode", "")))
        amt = r.get("applicationAmount", r.get("applicationVol", ""))
        time_ = r.get("acceptTime", "") or "-"
        # 用状态判定显示中文状态
        st = FundTrader.judge_order_status(r)
        print(f"  [{time_}] {name}({code}) {biz} {amt} | {st['label']}")
    return 0


def cmd_fund_order(args):
    trader = _fund_trader()
    try:
        d = trader.get_order_detail(args.serial)
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    from src.trading.fund_trader import FundTrader, format_order_detail
    status = FundTrader.judge_order_status(d)
    print(format_order_detail(d, status))
    return 0


def cmd_fund_info(args):
    """查询基金完整详情 (费率/风险/购买规则)。"""
    trader = _fund_trader()
    try:
        info = trader.get_fund_info(args.code)
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    from src.trading.fund_trader import format_fund_info
    print(format_fund_info(info))
    return 0


def cmd_fund_revoke(args):
    trader = _fund_trader()
    if not _yes(args.yes, f"撤单 {args.serial}"):
        print("已取消")
        return 1
    try:
        trader.revoke(args.serial)
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    print(f"✅ 撤单成功: {args.serial}")
    return 0


def cmd_fund_init(_args):
    print(
        "初始化爱基金凭证:\n"
        "  1. 打开同花顺 App → 理财 → 基金 Skill 页面, 获取 INIT_TOKEN\n"
        "  2. 执行:  D:/py/python.exe -c \"from aijijin_sdk import init; init('你的INIT_TOKEN')\"\n"
        "  3. 若 confirm_status=0, 去同花顺 App 确认设备授权\n"
        "  4. 完成后执行 `python trade.py fund holdings` 验证"
    )
    return 0


# ---------------------------------------------------------------------------
# 股票命令
# ---------------------------------------------------------------------------

def _stock_trader(args=None):
    from src.trading.stock_trader import StockTrader
    trader = StockTrader.from_config(auto_trade=getattr(args, "auto_trade", None))
    return trader


def cmd_stock_status(_args):
    trader = _stock_trader()
    snap = trader.snapshot()
    print(f"连接状态: {'✅ 已连接' if snap['connected'] else '❌ 未连接'}")
    acc = snap.get("account") or {}
    print(f"总资产: {acc.get('total_assets', 'N/A')}  可用: {acc.get('available_cash', 'N/A')}")
    print(f"市值: {acc.get('market_value', 'N/A')}  持仓盈亏: {acc.get('total_profit', 'N/A')}")
    pos = snap.get("positions") or []
    if pos:
        print(f"\n持仓 ({len(pos)} 只):")
        for p in pos:
            print(
                f"  {p.get('name', '')}({p.get('symbol', '')}) "
                f"{p.get('quantity', 0)}股 成本{p.get('avg_cost', '')} "
                f"现价{p.get('market_price', '')} 盈亏{p.get('unrealized_pnl', 0)}"
            )
    orders = snap.get("active_orders") or []
    if orders:
        print(f"\n在飞委托 ({len(orders)}):")
        for o in orders:
            print(f"  {o}")
    return 0


def cmd_stock_buy(args):
    trader = _stock_trader(args)
    if not _yes(args.yes, f"股票买入 {args.symbol} {args.quantity}股" +
                (f" @{args.price}" if args.price else " 市价")):
        print("已取消")
        return 1
    try:
        result = trader.buy(args.symbol, args.quantity, args.price, reason="cli")
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    _print_stock_result(result)
    return 0


def cmd_stock_sell(args):
    trader = _stock_trader(args)
    if not _yes(args.yes, f"股票卖出 {args.symbol} {args.quantity}股" +
                (f" @{args.price}" if args.price else " 市价")):
        print("已取消")
        return 1
    try:
        result = trader.sell(args.symbol, args.quantity, args.price, reason="cli")
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    _print_stock_result(result)
    return 0


def _print_stock_result(result: dict):
    if result.get("success"):
        print(
            f"✅ 委托成功: {result['side']} {result['symbol']} "
            f"{result['filled_quantity'] or result['quantity']}股"
        )
        print(f"   状态: {result['status']} | {result['message']}")
    else:
        print(f"❌ 委托失败: {result.get('message')}")
        if result.get("detail"):
            print(f"   详情: {result['detail']}")


def cmd_stock_cancel(args):
    trader = _stock_trader(args)
    if not _yes(args.yes, f"撤单 {args.entrust_no}"):
        print("已取消")
        return 1
    try:
        ok, msg = trader.cancel_order(args.entrust_no)
    except Exception as e:
        print(f"[错误] {e}")
        return 1
    print(f"{'✅ 撤单成功' if ok else '❌ 撤单失败'}: {msg}")
    return 0


def cmd_stock_connect(_args):
    trader = _stock_trader()
    ok = trader.connect()
    print(f"{'✅ guling-trader 已连接' if ok else '❌ 连接失败 (请确认 guling-trader.exe 已运行且已配对)'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    _init_syspath()
    parser = argparse.ArgumentParser(
        prog="trade",
        description="量化交易平台 — 基金/股票实盘交易 CLI (不依赖 WorkBuddy)",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # fund
    fund = sub.add_parser("fund", help="基金交易 (爱基金)")
    fund_sub = fund.add_subparsers(dest="cmd", required=True)

    f_hold = fund_sub.add_parser("holdings", help="基金+钱包持仓")
    f_hold.set_defaults(func=cmd_fund_holdings)

    f_buy = fund_sub.add_parser("buy", help="基金申购 (金额)")
    f_buy.add_argument("code", help="基金代码")
    f_buy.add_argument("amount", help="申购金额 (元)")
    f_buy.add_argument("--wallet", dest="pay", action="store_const", const="wallet", default="wallet", help="钱包支付 (默认)")
    f_buy.add_argument("--bank", dest="pay", action="store_const", const="bank", help="银行卡支付")
    f_buy.add_argument("--yes", action="store_true", help="跳过确认")
    f_buy.set_defaults(func=cmd_fund_buy)

    f_red = fund_sub.add_parser("redeem", help="基金赎回 (份额, 账户可自动获取)")
    f_red.add_argument("code", help="基金代码")
    f_red.add_argument("shares", help="赎回份额")
    f_red.add_argument("account", nargs="?", default="", help="交易账户 ID (留空自动从持仓获取)")
    f_red.add_argument("--wallet", dest="pay", action="store_const", const="wallet", default="wallet", help="钱包赎回 (默认)")
    f_red.add_argument("--bank", dest="pay", action="store_const", const="bank", help="银行卡赎回")
    f_red.add_argument("--yes", action="store_true", help="跳过确认")
    f_red.set_defaults(func=cmd_fund_redeem)

    f_orders = fund_sub.add_parser("orders", help="交易记录")
    f_orders.add_argument("--days", type=int, default=30, help="查询天数 (默认30)")
    f_orders.add_argument("--limit", type=int, default=20, help="条数")
    f_orders.set_defaults(func=cmd_fund_orders, start=None, end=None)

    f_info = fund_sub.add_parser("info", help="基金详情 (费率/风险/购买规则)")
    f_info.add_argument("code", help="基金代码")
    f_info.set_defaults(func=cmd_fund_info)

    f_order = fund_sub.add_parser("order", help="订单详情")
    f_order.add_argument("serial", help="订单号 appSheetSerialNo")
    f_order.set_defaults(func=cmd_fund_order)

    f_revoke = fund_sub.add_parser("revoke", help="撤单")
    f_revoke.add_argument("serial", help="订单号")
    f_revoke.add_argument("--yes", action="store_true")
    f_revoke.set_defaults(func=cmd_fund_revoke)

    f_init = fund_sub.add_parser("init", help="初始化凭证指引")
    f_init.set_defaults(func=cmd_fund_init)

    f_status = fund_sub.add_parser("status", help="检查凭证/设备授权/Work Token 状态")
    f_status.set_defaults(func=cmd_fund_status)

    # stock
    stock = sub.add_parser("stock", help="股票实盘 (guling-trader/同花顺)")
    stock_sub = stock.add_subparsers(dest="cmd", required=True)

    s_status = stock_sub.add_parser("status", help="账户+持仓+委托总览")
    s_status.set_defaults(func=cmd_stock_status)

    s_buy = stock_sub.add_parser("buy", help="买入")
    s_buy.add_argument("symbol", help="股票代码 (如 600519 / sh600519)")
    s_buy.add_argument("quantity", type=int, help="股数 (100整数倍)")
    s_buy.add_argument("--price", type=float, default=None, help="限价 (默认市价)")
    s_buy.add_argument("--auto_trade", action="store_true", help="临时开启实盘 (覆盖配置)")
    s_buy.add_argument("--yes", action="store_true")
    s_buy.set_defaults(func=cmd_stock_buy)

    s_sell = stock_sub.add_parser("sell", help="卖出")
    s_sell.add_argument("symbol", help="股票代码")
    s_sell.add_argument("quantity", type=int, help="股数")
    s_sell.add_argument("--price", type=float, default=None, help="限价 (默认市价)")
    s_sell.add_argument("--auto_trade", action="store_true", help="临时开启实盘 (覆盖配置)")
    s_sell.add_argument("--yes", action="store_true")
    s_sell.set_defaults(func=cmd_stock_sell)

    s_cancel = stock_sub.add_parser("cancel", help="撤单")
    s_cancel.add_argument("entrust_no", help="委托号")
    s_cancel.add_argument("--auto_trade", action="store_true")
    s_cancel.add_argument("--yes", action="store_true")
    s_cancel.set_defaults(func=cmd_stock_cancel)

    s_conn = stock_sub.add_parser("connect", help="测试连接")
    s_conn.set_defaults(func=cmd_stock_connect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
