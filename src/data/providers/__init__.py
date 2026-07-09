"""数据提供者模块: 可选的外部研究数据源"""
from .openbb_provider import OpenBBProvider, HAS_OPENBB

__all__ = ['OpenBBProvider', 'HAS_OPENBB']
