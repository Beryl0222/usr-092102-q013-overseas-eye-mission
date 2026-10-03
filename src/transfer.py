"""跨境资料交换：治疗目的最小化导出与撤回语义。

- 治疗目的只放行白名单字段（docs/domain.md §4），多给的字段在边界被拒绝；
- 非治疗目的必须有未撤回的 nonclinical 同意；撤回后该通道关闭，导出被拒；
- 撤回不删除任何已形成的诊疗/安全事件——本模块只有追加 DATA_EXPORTED 留痕，没有删除路径。
"""

import uuid
from collections import defaultdict

from src.errors import RuleViolation
from src.events import now_iso
from src.policy import NONCLINICAL_EXPORT_WHITELIST, TREATMENT_EXPORT_WHITELIST
from src.projection import project
from src.store import EventStore

TREATMENT_PURPOSE = "treatment"


class DataTransferService:
    def __init__(self, store: EventStore, batch_id: str) -> None:
        self.store = store
        self.batch_id = batch_id

    def export_minimal(self, actor: dict, patient_id: str, *, purpose: str,
                       recipient_org: str, recipient_country: str, record: dict,
                       legal_basis: str, occurred_at: str | None = None,
                       transfer_id: str | None = None) -> dict:
        """组装并留痕一次跨境导出。返回 {"event":..., "dataset":...}。"""
        bv = project(self.store.all_events()).get(self.batch_id)
        if bv is None or patient_id not in bv.patients:
            raise RuleViolation(f"系统中没有患者 {patient_id}")
        pv = bv.patients[patient_id]

        scopes = pv.effective_scopes
        if purpose == TREATMENT_PURPOSE:
            if "treatment" not in scopes:
                raise RuleViolation("治疗目的导出需要有效的 treatment 同意")
        else:
            if "nonclinical" not in scopes:
                raise RuleViolation(
                    f"非治疗目的（{purpose}）需要 treatment_plus_nonclinical 同意且未撤回；"
                    "撤回后不得导出"
                )

        whitelist = (TREATMENT_EXPORT_WHITELIST if purpose == TREATMENT_PURPOSE
                     else NONCLINICAL_EXPORT_WHITELIST)
        extra = sorted(set(record) - set(whitelist))
        if extra:
            raise RuleViolation(f"以下字段超出{purpose}目的的最小字段集，必须移除：{extra}")
        missing = sorted(set(whitelist) - set(record))
        dataset = {k: record[k] for k in whitelist if k in record}
        # 最小化：不强制填满；治疗目的必须带本地标识供接手方对接，宣传等目的则连本地号都不给
        if purpose == TREATMENT_PURPOSE and "patient_local_id" not in dataset:
            raise RuleViolation("治疗目的导出至少需要 patient_local_id 供接手方对接")

        transfer_id = transfer_id or f"xfer-{uuid.uuid4().hex[:10]}"
        payload = {
            "patient_local_id": patient_id,
            "purpose": purpose,
            "recipient_org": recipient_org,
            "recipient_country": recipient_country,
            "fields": sorted(dataset.keys()),
            "fields_omitted_as_unknown": missing,
            "legal_basis": legal_basis,
            "consent_scope_at_export": sorted(scopes),
            "approved_by": actor["staff_id"],
            "exported_at": occurred_at or now_iso(),
        }
        counts: dict[str, int] = defaultdict(int)
        ver = self.store.version(transfer_id) + 1
        event = {
            "event_id": transfer_id,
            "event_type": "DATA_EXPORTED",
            "aggregate_type": "data_transfer",
            "aggregate_id": transfer_id,
            "batch_id": self.batch_id,
            "occurred_at": payload["exported_at"],
            "version": ver,
            "summary": f"{purpose} 目的导出至 {recipient_org}（{recipient_country}），{len(dataset)} 个字段",
            "payload": payload,
        }
        stored = self.store.commit([event])[0]
        return {"event": stored, "dataset": dataset}
