# -*- coding: utf-8 -*-
"""渲染 消息反应速度事件研究 仪表盘。"""
import json
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "news_anticipation")
sys.path.insert(0, os.path.join(OUT, "ref"))
from render_dashboard import build_dashboard_data, render_dashboard  # noqa: E402

text_conclusion = (
    "用户观点（价格是市场的提前预测，公开消息时反应已兑现）被数据证实：\n"
    "① 现实可执行的\"大涨日次日开盘买入\"（普通投资者拿到消息后最可能的下单时点），"
    "持有1日平均 -0.34%（基准 +0.06%）、胜率仅 40%——追消息接盘，中短期均跑输基准；\n"
    "② 大涨日当天收盘买入（理想时点）看似 +0.75%，但实盘在拉升/涨停中买不进，"
    "该收益不可得；\n"
    "③ 典型案例：五粮液 2026-07-15 半年报业绩预告，公告前5日已上涨 +6.9%，"
    "公告后5日回落 -2.2%——价格在消息公开前已提前反应。"
)
text_rule = (
    "策略启示（已同步调整项目策略）：\n"
    "① 新闻因子用于\"确认催化+排除风险\"，不做\"追涨加分\"——利好新闻出现后禁止在高开/大涨日追入；\n"
    "② 真正领先的信号是量价（放量突破、主力资金流入），它们在新闻公开前已启动，"
    "新闻本身是滞后的；\n"
    "③ 新增\"防追高\"规则：bull 新闻因子仅在当日回调（≥-1%）时允许加分，否则提示等待。"
)
text_limit = (
    "已知局限：\n"
    "① 大涨日≥5% 是\"消息/资金强反应\"的代理，部分大涨与消息无关（纯资金博弈）；\n"
    "② 业绩预增公告样本仅 7 个（公告接口仅返回近期数据），验证2参考价值有限；\n"
    "③ 未区分消息类型（利好/利空对追买影响不同），大涨日含涨停板（次日买入不可成交）高估了可执行性；\n"
    "④ 2024-07~2026-08 区间，未覆盖完整牛熊周期。"
)

extra = [
    {"type": "text", "tab": "overview", "title": "核心结论", "text": text_conclusion},
    {"type": "text", "tab": "overview", "title": "策略启示", "text": text_rule},
    {"type": "text", "tab": "overview", "title": "已知局限", "text": text_limit},
]

report = build_dashboard_data(
    trades_csv=os.path.join(OUT, "news_anticipation_trades.csv"),
    summary_json=os.path.join(OUT, "news_anticipation_summary.json"),
    language="zh",
    event_overview_mode="stats",
    extra_modules=extra,
)
render_dashboard(report, output_path=os.path.join(OUT, "index.html"),
                 template_path=os.path.join(OUT, "ref", "dashboard_template.html"))
print("仪表盘已生成:", os.path.join(OUT, "index.html"))
