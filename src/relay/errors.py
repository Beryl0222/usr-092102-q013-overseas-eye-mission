"""诊疗接力领域错误。"""


class RelayError(Exception):
    """领域错误基类。"""


class NotFound(RelayError):
    """引用的对象不存在。"""


class InvariantViolation(RelayError):
    """业务不变量被破坏（如 AI 结果单独决定手术、链路顺序颠倒）。"""


class RoleViolation(RelayError):
    """操作或确认由不具备对应角色的人员执行。"""


class ConsentError(RelayError):
    """知情同意状态不允许该操作。"""


class ResourceConflict(RelayError):
    """设备、耗材或人员班次的并发占用冲突。"""


class ContractViolation(RelayError):
    """事件不符合 contracts/domain.schema.json 的约定。"""
