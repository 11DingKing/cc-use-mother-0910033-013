"""活动取消补办管理后端。

模块划分：

- ``models``：领域对象与枚举。
- ``store``：带全局锁与快照回滚的内存存储，保证“确认补办”原子性。
- ``services``：取消登记、补办确认、退出/改期/恢复、通知与去重报表。
- ``api``：基于标准库 ``http.server`` 的 JSON 接口。
"""
from __future__ import annotations

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .services import Services

__all__ = ["Services", "DomainError", "NotFoundError", "ValidationError", "ConflictError"]
