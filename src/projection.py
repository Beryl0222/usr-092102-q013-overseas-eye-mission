"""事件流读模型投影：批次与患者的当前状态。"""

from collections import defaultdict


class PatientView:
    def __init__(self, patient_local_id: str) -> None:
        self.patient_local_id = patient_local_id
        self.screenings: list[dict] = []
        self.ai_triages: list[dict] = []
        self.clinical_reviews: list[dict] = []
        self.exceptions: list[dict] = []
        self.priority: dict | None = None
        self.granted_scopes: set[str] = set()
        self.withdrawn_scopes: set[str] = set()
        self.consent_events: list[dict] = []
        self.schedules: list[dict] = []          # SURGERY_SCHEDULED，state 随治疗/释放更新
        self.treatments: list[dict] = []
        self.handoff: dict | None = None
        self.followups: list[dict] = []
        self.escalations: list[dict] = []

    @property
    def effective_scopes(self) -> set[str]:
        return self.granted_scopes - self.withdrawn_scopes

    @property
    def latest_review(self) -> dict | None:
        return self.clinical_reviews[-1] if self.clinical_reviews else None

    @property
    def active_schedule(self) -> dict | None:
        """最近一次未被治疗关闭、未取消的排期。"""
        for sch in reversed(self.schedules):
            if sch.get("state") == "scheduled":
                return sch
        return None


class BatchView:
    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self.registered: dict | None = None
        self.closed: dict | None = None
        self.patients: dict[str, PatientView] = {}

    def patient(self, patient_local_id: str) -> PatientView:
        return self.patients.setdefault(patient_local_id, PatientView(patient_local_id))


def project(events: list[dict], batch_id: str | None = None) -> dict[str, BatchView]:
    """按 batch_id 投影全部事件。"""
    batches: dict[str, BatchView] = defaultdict(lambda: BatchView(batch_id or "_"))
    for e in sorted(events, key=lambda x: (x["occurred_at"], x["event_id"])):
        bid = e.get("batch_id")
        bv = batches[bid]
        bv.batch_id = bid
        t, p = e["event_type"], e.get("payload", {})

        if t == "BATCH_REGISTERED":
            bv.registered = e
            continue
        if t == "BATCH_CLOSED":
            bv.closed = e
            continue

        pid = p.get("patient_local_id")
        # 资源/导出聚合事件可能通过 payload 带 patient
        pv = bv.patient(pid) if pid else None

        if t == "SCREENING_RECEIVED" and pv and e["aggregate_type"] == "mission_patient":
            pv.screenings.append({**p, "occurred_at": e["occurred_at"]})
        elif t == "AI_TRIAGE_RECORDED" and pv:
            pv.ai_triages.append({**p, "occurred_at": e["occurred_at"]})
        elif t == "CLINICAL_REVIEWED" and pv:
            pv.clinical_reviews.append({**p, "occurred_at": e["occurred_at"]})
        elif t == "PRIORITY_SET" and pv:
            pv.priority = {**p, "occurred_at": e["occurred_at"]}
        elif t == "EXCEPTION_APPROVED" and pv:
            pv.exceptions.append({**p, "event_id": e["event_id"],
                                  "occurred_at": e["occurred_at"]})
        elif t == "CONSENT_GIVEN" and pv:
            pv.consent_events.append(e)
            scope = p["scope"]
            pv.granted_scopes.add("treatment")
            if scope == "treatment_plus_nonclinical":
                pv.granted_scopes.add("nonclinical")
        elif t == "CONSENT_WITHDRAWN" and pv:
            pv.consent_events.append(e)
            if p["scope_withdrawn"] == "nonclinical":
                pv.withdrawn_scopes.add("nonclinical")
            elif p["scope_withdrawn"] == "all_future":
                pv.withdrawn_scopes.add("treatment")
                pv.withdrawn_scopes.add("nonclinical")
        elif t == "SURGERY_SCHEDULED" and pv:
            pv.schedules.append({**p, "state": "scheduled",
                                 "scheduled_event_at": e["occurred_at"]})
        elif t == "TREATMENT_COMPLETED" and pv:
            pv.treatments.append({**p, "occurred_at": e["occurred_at"]})
            for sch in reversed(pv.schedules):
                if sch.get("slot_id") == p.get("slot_id") and sch["state"] == "scheduled":
                    sch["state"] = "completed"
                    break
        elif t == "HANDOFF_ASSIGNED" and pv:
            pv.handoff = {**p, "state": "assigned"}
        elif t == "HANDOFF_ACCEPTED" and pv and pv.handoff is not None:
            pv.handoff.update(p, state="accepted", accepted_at=e["occurred_at"])
        elif t == "FOLLOWUP_SCHEDULED" and pv:
            # 结果按发生时间可能早于补录到达的安排：归并进同号条目而非新增
            prior = next((f for f in pv.followups if f.get("followup_no") == p["followup_no"]), None)
            if prior is not None:
                prior.update(p)
            else:
                pv.followups.append({**p, "state": "scheduled"})
        elif t == "FOLLOWUP_OUTCOME_RECORDED" and pv:
            matched = next((f for f in pv.followups if f.get("followup_no") == p.get("followup_no")), None)
            if matched is not None:
                matched.update(p, state="done")
            else:
                # 结果事件按发生时间可能早于补录的安排事件：先暂存，重放末尾归并
                pv.followups.append({**p, "state": "done"})
        elif t == "ESCALATION_OPENED" and pv:
            pv.escalations.append({**p, "state": "open", "opened_event": e["event_id"]})
        elif t == "ESCALATION_RESOLVED" and pv:
            for esc in reversed(pv.escalations):
                if esc.get("state") == "open":
                    esc.update(p, state="resolved", resolved_at=e["occurred_at"])
                    break
            else:
                pv.escalations.append({**p, "state": "resolved"})

    return dict(batches)
