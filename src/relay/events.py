"""领域事件存储：契约校验、按聚合版本递增、按 event_id 幂等。

事件信封沿用 contracts/domain.schema.json 的标识规则：
event_id、event_type、aggregate_type、aggregate_id、occurred_at、version、summary。
信封之外允许附加字段（如 recorded_at、payload），与契约的 additionalProperties 兼容。

断网补录约定：
- occurred_at 永远保留业务发生时间，补录不改写；
- recorded_at 是系统入账时间；
- version 按入账顺序在聚合内严格递增，反映补录顺序而非发生顺序；
- 同一 event_id 重复入账且内容一致时视为幂等重放，直接返回原事件。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.relay.errors import ContractViolation
from src.validator import validate_event

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def _load_enums(path: Path) -> tuple[set, set]:
    """从契约文件读取已登记的事件类型与聚合类型，保持单一事实来源。"""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return set(), set()
    props = schema.get("properties", {})
    return (
        set(props.get("event_type", {}).get("enum", [])),
        set(props.get("aggregate_type", {}).get("enum", [])),
    )


class EventStore:
    """进程内事件存储；接口与持久化实现保持一致，便于替换。"""

    def __init__(self, schema_path: Path | None = None) -> None:
        self._events: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._event_types, self._aggregate_types = _load_enums(schema_path or SCHEMA_PATH)

    @property
    def registered_event_types(self) -> set:
        return set(self._event_types)

    @property
    def registered_aggregate_types(self) -> set:
        return set(self._aggregate_types)

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0) + 1

    def append(self, event: dict) -> dict:
        errors = validate_event(event)
        if errors:
            raise ContractViolation("；".join(errors))
        if self._event_types and event["event_type"] not in self._event_types:
            raise ContractViolation(f"未登记的事件类型：{event['event_type']}")
        if self._aggregate_types and event["aggregate_type"] not in self._aggregate_types:
            raise ContractViolation(f"未登记的聚合类型：{event['aggregate_type']}")
        try:
            datetime.fromisoformat(event["occurred_at"])
        except (TypeError, ValueError) as exc:
            raise ContractViolation(f"occurred_at 不是合法时间：{exc}") from exc
        existing = self._by_id.get(event["event_id"])
        if existing is not None:
            if existing != event:
                raise ContractViolation(f"event_id 冲突且内容不一致：{event['event_id']}")
            return existing  # 幂等重放：不产生第二条事件
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions.get(key, 0) + 1
        if event["version"] != expected:
            raise ContractViolation(
                f"聚合 {key} 的版本应为 {expected}，实际为 {event['version']}"
            )
        self._events.append(event)
        self._by_id[event["event_id"]] = event
        self._versions[key] = event["version"]
        return event

    def get(self, event_id: str) -> dict | None:
        return self._by_id.get(event_id)

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        return [
            event
            for event in self._events
            if event["aggregate_type"] == aggregate_type and event["aggregate_id"] == aggregate_id
        ]

    def all(self) -> list[dict]:
        return list(self._events)
