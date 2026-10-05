"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域规则异常的基类。"""


class ValidationError(DomainError):
    """请求数据不满足前置条件（400）。"""


class NotFoundError(DomainError):
    """引用的领域对象不存在（404）。"""


class ConflictError(DomainError):
    """与当前状态或确定性规则冲突（409），如重复确认、资源不足。"""
