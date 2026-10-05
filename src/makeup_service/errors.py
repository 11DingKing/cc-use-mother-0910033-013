"""领域错误类型。"""


class DomainError(Exception):
    """所有领域错误的基类。"""


class NotFoundError(DomainError):
    """实体不存在。"""


class InvalidStateError(DomainError):
    """当前状态不允许执行该操作。"""


class ResourceConflictError(DomainError):
    """有限资源在目标时段余量不足，原子转移整体失败。"""


class AlreadyConfirmedError(DomainError):
    """补办已确认；重复确认应走幂等返回，变更应走改期接口。"""


class RestorationClosedError(DomainError):
    """补办场次已开始，恢复原场次的窗口已关闭。"""
