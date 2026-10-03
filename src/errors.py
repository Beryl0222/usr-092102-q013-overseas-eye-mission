"""领域错误类型。"""


class DomainError(Exception):
    """所有领域规则错误的基类。"""


class MismatchedReplay(DomainError):
    """同一 event_id 重放但载荷不一致——可能是现场与服务器版本分叉。"""


class ConflictError(DomainError):
    """聚合版本冲突（乐观锁）或事件版本号不连续。"""


class ResourceConflict(DomainError):
    """设备/班次/耗材并发占用冲突。"""


class RuleViolation(DomainError):
    """业务规则被违反（无适应证排期、角色不符、未闭环等）。"""
