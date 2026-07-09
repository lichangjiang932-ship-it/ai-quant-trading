"""多智能体交易系统(抄袭 TradingAgents 架构,适配 A 股)

数据 -> 分析师团队 -> 多空研究员辩论 -> 交易员 -> 风控经理 -> 反思记忆
"""
from .orchestrator import TradingAgentsGraph, AgentDecision

__all__ = ['TradingAgentsGraph', 'AgentDecision']
