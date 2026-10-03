"""只追加（append-only）的领域事件存储。

职责：
- 按聚合流维护单调 version，提交时做乐观并发检查；
- event_id 全局幂等：同 id 同载荷重放返回原事件，同 id 异载荷拒绝；
- 保留 occurred_at（现场发生时间），补录时刻写入 recorded_at；
- 事件一经追加不可变，修正只能追加新事件。
"""

import copy
import json
import threading
from collections import defaultdict
from pathlib import Path

from src.errors import ConflictError, MismatchedReplay
from src.events import REPLAY_IGNORED_KEYS, now_iso, parse_ts
from src.validator import validate_full_event


class EventStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._streams: dict[str, list[dict]] = defaultdict(list)
        self._by_id: dict[str, dict] = {}
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._index(json.loads(line))

    # ---------- 读取 ----------

    def stream(self, aggregate_id: str) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(e) for e in self._streams[aggregate_id]]

    def all_events(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(e) for e in self.all_events_unlocked()]

    def all_events_unlocked(self) -> list[dict]:
        """必须在持有 self._lock 时调用。"""
        out = [e for evs in self._streams.values() for e in evs]
        out.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
        return out

    def version(self, aggregate_id: str) -> int:
        with self._lock:
            return len(self._streams[aggregate_id])

    def get_event(self, event_id: str) -> dict | None:
        """按幂等键取回已存在事件（服务层据此在重放时沿用原版本号）。"""
        with self._lock:
            existing = self._by_id.get(event_id)
            return copy.deepcopy(existing) if existing else None

    def aggregates(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(a for a in self._streams if a.startswith(prefix))

    # ---------- 写入 ----------

    def commit(self, events: list[dict], precheck=None) -> list[dict]:
        """原子提交一批跨聚合事件（如一次排期同时占用设备/班次/耗材）。

        - 全部分支通过后才落盘，不允许部分占用；
        - precheck(all_events_with_staged) 在同一把锁内执行业务判定（资源冲突等），
          可见的事件流已包含本批暂存事件，因此并发排期只有一方能通过；
        - 整批幂等：已存在且载荷一致的 event_id 视为重放，跳过且不重复参与判定。
        """
        with self._lock:
            staged: list[dict] = []
            staged_counts: dict[str, int] = defaultdict(int)
            for event in events:
                errors = validate_full_event(event)
                if errors:
                    raise ValueError("；".join(errors))
                occurred = parse_ts(event["occurred_at"])
                recorded_at = event.get("recorded_at") or now_iso()
                if parse_ts(recorded_at) < occurred:
                    raise ValueError("recorded_at 不得早于 occurred_at")

                aggregate_id = event["aggregate_id"]
                existing = self._by_id.get(event["event_id"])
                if existing is not None:
                    a = {k: v for k, v in existing.items() if k not in REPLAY_IGNORED_KEYS}
                    b = {k: v for k, v in event.items() if k not in REPLAY_IGNORED_KEYS}
                    if a != b:
                        raise MismatchedReplay(
                            f"event_id={event['event_id']} 已存在但载荷不一致，拒绝覆盖"
                        )
                    continue

                current = len(self._streams[aggregate_id]) + staged_counts[aggregate_id]
                if event["version"] != current + 1:
                    raise ConflictError(
                        f"聚合 {aggregate_id} 版本不连续：期望 {current + 1}，收到 {event['version']}"
                    )
                stored = copy.deepcopy(event)
                stored["recorded_at"] = recorded_at
                staged.append(stored)
                staged_counts[aggregate_id] += 1

            if precheck is not None:
                projected = self.all_events_unlocked() + [copy.deepcopy(e) for e in staged]
                precheck(projected)

            for stored in staged:
                self._index(stored)
                if self._path:
                    with self._path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(stored, ensure_ascii=False) + "\n")
            return [copy.deepcopy(self._by_id[e["event_id"]]) for e in events]

    def append(self, event: dict, *, expected_version: int | None = None) -> dict:
        """提交一条事件。

        expected_version：调用方基于的流版本（None 表示不校验，但 version 仍须连续）。
        """
        errors = validate_full_event(event)
        if errors:
            raise ValueError("；".join(errors))

        occurred = parse_ts(event["occurred_at"])
        recorded_at = event.get("recorded_at") or now_iso()
        if parse_ts(recorded_at) < occurred:
            raise ValueError("recorded_at 不得早于 occurred_at（补录不能早于事件发生）")

        aggregate_id = event["aggregate_id"]

        with self._lock:
            existing = self._by_id.get(event["event_id"])
            if existing is not None:
                # 幂等重放：忽略 recorded_at 后逐字段比对
                a = {k: v for k, v in existing.items() if k not in REPLAY_IGNORED_KEYS}
                b = {k: v for k, v in event.items() if k not in REPLAY_IGNORED_KEYS}
                if a != b:
                    raise MismatchedReplay(
                        f"event_id={event['event_id']} 已存在但载荷不一致，拒绝覆盖"
                    )
                return copy.deepcopy(existing)

            current = len(self._streams[aggregate_id])
            if expected_version is not None and expected_version != current:
                raise ConflictError(
                    f"聚合 {aggregate_id} 版本冲突：期望 {expected_version}，实际 {current}"
                )
            if event["version"] != current + 1:
                raise ConflictError(
                    f"聚合 {aggregate_id} 版本不连续：期望 {current + 1}，收到 {event['version']}"
                )

            stored = copy.deepcopy(event)
            stored["recorded_at"] = recorded_at
            self._index(stored)
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(stored, ensure_ascii=False) + "\n")
            return copy.deepcopy(stored)

    # ---------- 内部 ----------

    def _index(self, event: dict) -> None:
        self._by_id[event["event_id"]] = event
        self._streams[event["aggregate_id"]].append(event)
