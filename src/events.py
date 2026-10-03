"""领域事件信封与载荷常量。"""

from datetime import datetime, timezone

# 与 contracts/domain.schema.json 保持一致的稳定枚举
EVENT_TYPES = {
    "BATCH_REGISTERED",
    "SCREENING_RECEIVED",
    "AI_TRIAGE_RECORDED",
    "CLINICAL_REVIEWED",
    "PRIORITY_SET",
    "EXCEPTION_APPROVED",
    "CONSENT_GIVEN",
    "CONSENT_WITHDRAWN",
    "SLOT_RESERVED",
    "SLOT_RELEASED",
    "SURGERY_SCHEDULED",
    "SUPPLIES_CONSUMED",
    "TREATMENT_COMPLETED",
    "HANDOFF_ASSIGNED",
    "HANDOFF_ACCEPTED",
    "FOLLOWUP_SCHEDULED",
    "FOLLOWUP_OUTCOME_RECORDED",
    "ESCALATION_OPENED",
    "ESCALATION_RESOLVED",
    "DATA_EXPORTED",
    "BATCH_CLOSED",
}

AGGREGATE_TYPES = {
    "task_batch",
    "mission_patient",
    "screening_evidence",
    "treatment_slot",
    "resource",
    "followup_handoff",
    "data_transfer",
}

ENVELOPE_REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
    "batch_id",
)

# 重放幂等比对时不参与的字段：recorded_at 是首次入系统时间，允许重试时缺省/不同
REPLAY_IGNORED_KEYS = {"recorded_at"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str) -> datetime:
    """解析 ISO8601；要求带时区，避免现场本地时间与 UTC 混比。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区偏移：{value}")
    return dt
