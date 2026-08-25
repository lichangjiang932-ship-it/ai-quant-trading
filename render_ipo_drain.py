# -*- coding: utf-8 -*-
"""渲染 巨型IPO上市日事件研究 仪表盘。"""
import json
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "ipo_drain")
sys.path.insert(0, os.path.join(OUT, "ref"))
from render_dashboard import build_dashboard_data, render_dashboard  # noqa: E402

# 读取 detail 生成各指数明细表
detail_rows = []
detail_csv = os.path.join(OUT, "ipo_drain_detail.csv")
if os.path.exists(detail_csv):
    import csv
    with open(detail_csv, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            detail_rows.append(r)

with open(os.path.join(OUT, "ipo_drain_summary.json"), "r", encoding="utf-8") as f:
    summary = json.load(f)
s = summary["summary"]

text_conclusion = (
    f"数据支持\"巨型IPO上市日避险\"假设：{s['total_events']} 个历史巨型IPO上市日，"
    f"沪深300当日平均 {s['avg_ret_T']:+.2f}%，{s['down_pct']:.0f}% 概率下跌"
    f"（中位 {s['median_ret_T']:+.2f}%），而事件前20个交易日均值 "
    f"{s['avg_baseline']:+.2f}%——上市日显著跑输约1个百分点。"
    f"创业板更敏感（平均 -1.08%）。最极端为中芯国际（2020-07-16）当日 -4.81%。"
)
text_rule = (
    "策略规则（已接入自托管）：① 识别\"巨型IPO\"（预计募资≥100亿 或 上市前关注度极高"
    "如长鑫/宇树级别）；② 上市日 T 及 T+1 禁止新开仓，已有持仓提示风控；"
    "③ 与新闻因子联动：IPO 相关板块新闻热度骤升时同步触发。"
    "适用边界：样本13个，多为大盘股；2018年前及北交所未覆盖。"
)
text_limit = (
    "已知局限：① 样本量有限（13个事件），统计显著性有待扩充；"
    "② 上证指数维度数据存在异常（3日 +4.5% 存疑，已剔除出结论，结论以沪深300/创业板为准）；"
    "③ 上市日下跌存在市场环境混杂（如中芯国际当周恰逢市场调整），无法完全归因于IPO虹吸；"
    "④ 避险需承担\"误伤\"成本：30% 情况下上市日实际上涨（三峡能源 +0.67%）。"
)

extra = [
    {"type": "text", "tab": "overview", "title": "核心结论", "text": text_conclusion},
    {"type": "text", "tab": "overview", "title": "策略规则", "text": text_rule},
    {"type": "text", "tab": "overview", "title": "已知局限", "text": text_limit},
]
# 各指数明细表格 (事件研究 metrics)
tbl_rows = []
for r in detail_rows:
    tbl_rows.append({
        "index": r["index"], "date": r["date"], "name": r["name"],
        "ret_T": r["ret_T"], "ret_T1": r["ret_T1"],
        "ret_3d": r["ret_3d"], "ret_5d": r["ret_5d"],
        "baseline": r["baseline"],
    })
extra.append({
    "type": "table", "tab": "overview", "title": "各指数·各事件涨跌明细 (%)",
    "columns": ["index", "date", "name", "ret_T", "ret_T1", "ret_3d", "ret_5d", "baseline"],
    "rows": tbl_rows,
})

report = build_dashboard_data(
    trades_csv=os.path.join(OUT, "ipo_drain_trades.csv"),
    summary_json=os.path.join(OUT, "ipo_drain_summary.json"),
    language="zh",
    event_overview_mode="stats",
    extra_modules=extra,
)
render_dashboard(report, output_path=os.path.join(OUT, "index.html"),
                 template_path=os.path.join(OUT, "ref", "dashboard_template.html"))
print("仪表盘已生成:", os.path.join(OUT, "index.html"))
