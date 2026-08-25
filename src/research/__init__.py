"""股票研究模块 — 把卖方/买方研究能力沉淀为项目内可复用组件。

能力矩阵:
  - tearsheet: 公司速览卡 (业务/估值/技术面/买卖区间/综合评级)
  - comps:     可比公司估值 (同业 PE/PB 对比)
  - scenario:  事件与情景分析 (业绩/放量/破位 what-if)

设计原则:
  - 纯计算函数, 数据由调用方 (api_server) 注入, 保持模块可单测
  - 输出结构统一为 dict, 前端直接渲染
"""
from __future__ import annotations

from src.research.tearsheet import build_tearsheet
from src.research.comps import compute_comps, classify_industry
from src.research.scenario import build_scenario, EVENT_TEMPLATES

__all__ = ["build_tearsheet", "compute_comps", "classify_industry",
           "build_scenario", "EVENT_TEMPLATES"]
