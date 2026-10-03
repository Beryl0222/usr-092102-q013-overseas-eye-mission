"""校验领域事件信封的基础字段。"""

from src.events import AGGREGATE_TYPES, EVENT_TYPES

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

# 服务端追加的事件还必须带批次号（见 src/store.py）
FULL_REQUIRED = REQUIRED + ("batch_id",)


def validate_event(record: dict) -> list[str]:
    """基础信封校验（保持与既有契约样例兼容）。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if record.get("event_type") and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知 event_type：{record['event_type']}")
    if record.get("aggregate_type") and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知 aggregate_type：{record['aggregate_type']}")
    return errors


def validate_full_event(record: dict) -> list[str]:
    """服务端提交事件的完整校验。"""
    errors = [f"缺少字段：{name}" for name in FULL_REQUIRED if name not in record]
    errors.extend(validate_event(record))
    return errors
