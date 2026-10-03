"""返程闭环看板与例外追溯（只读报告）。

看板回答“每名患者由谁接手、何时复查、异常如何升级、还缺什么”；
例外台账回答“这次开口子当时还剩多少资源、医学意见是什么、后来结果如何”——
完整责任链，不用一次成功故事替代。
"""

from src.projection import project
from src.store import EventStore

_CHANNEL_LABEL = {
    "routine": "常规排期",
    "medical_urgency": "医学紧急例外",
    "accessibility": "无障碍例外",
    "humanitarian": "人道例外",
}


class DepartureReport:
    def __init__(self, store: EventStore, batch_id: str) -> None:
        bv = project(store.all_events()).get(batch_id)
        if bv is None:
            raise KeyError(f"批次不存在：{batch_id}")
        self.batch_id = batch_id
        self.bv = bv

    # ---------- 返程看板 ----------

    def board(self) -> dict:
        rows = []
        for pid, pv in sorted(self.bv.patients.items()):
            if not pv.screenings:
                continue  # 只出现于导出等事件的引用，跳过
            sources = [{"source": s["source"],
                        "confidence_band": (s.get("ai") or {}).get("confidence_band"),
                        "boundary_note": (s.get("ai") or {}).get("boundary_note")}
                       for s in pv.screenings]
            review = pv.latest_review
            channel = None
            approver = None
            basis_ex = None
            active = None
            for sch in reversed(pv.schedules):
                if sch["state"] in ("scheduled", "completed"):
                    active = sch
                    break
            if active:
                channel = _CHANNEL_LABEL.get(active["basis"], active["basis"])
                if active["basis"] != "routine":
                    ex = next((e for e in pv.exceptions if e["event_id"] == active["basis_event_id"]), None)
                    if ex:
                        approver = f"{ex['approved_by']}（{ex['approver_role']}）"
                        basis_ex = ex["event_id"]
                elif pv.priority:
                    approver = pv.priority["set_by"]

            handoff = pv.handoff
            first_fu = pv.followups[0] if pv.followups else None
            open_escalations = [e for e in pv.escalations if e["state"] == "open"]

            gaps = []
            if pv.treatments:
                if handoff is None:
                    gaps.append("未指定当地接手医护")
                elif handoff.get("state") != "accepted":
                    gaps.append("接手医护尚未接受")
                if not pv.followups:
                    gaps.append("未安排复查")
                if open_escalations:
                    gaps.append("有未解决的异常升级")

            rows.append({
                "patient_local_id": pid,
                "screening_sources": sources,
                "ai_triage_suggestions": [
                    {"suggested_priority": t["suggested_priority"],
                     "confidence_band": t["confidence_band"],
                     "boundary_note": t["boundary_note"], "advisory_only": True}
                    for t in pv.ai_triages],
                "indication_for_surgery": review["indication_for_surgery"] if review else None,
                "clinical_fitness": review["fitness"] if review else None,
                "reviewed_by": review["ophthalmologist_id"] if review else None,
                "priority_channel": channel,
                "priority_approver": approver,
                "exception_event_id": basis_ex,
                "consent_scopes_effective": sorted(pv.effective_scopes),
                "treated": bool(pv.treatments),
                "last_outcome": pv.treatments[-1]["outcome_at_discharge"] if pv.treatments else None,
                "local_clinician": (handoff or {}).get("local_clinician_id"),
                "local_facility": (handoff or {}).get("local_facility"),
                "handoff_state": (handoff or {}).get("state"),
                "first_followup_due": first_fu["due_at"] if first_fu else None,
                "first_followup_modality": first_fu["modality"] if first_fu else None,
                "escalation_path": first_fu["escalation_path"] if first_fu else None,
                "open_escalations": [{"red_flag": e["red_flag"], "target": e["target"]}
                                     for e in open_escalations],
                "closure_gaps": gaps,
            })

        treated = [r for r in rows if r["treated"]]
        return {
            "batch_id": self.batch_id,
            "batch_closed": self.bv.closed is not None,
            "patients_total": len(rows),
            "treated_total": len(treated),
            "closed_loop_total": sum(1 for r in treated if not r["closure_gaps"]),
            "patients": rows,
        }

    # ---------- 例外追溯 ----------

    def exception_audit(self, patient_id: str | None = None) -> list[dict]:
        """每个例外：决定本身 → 当时资源快照 → 医学意见 → 排期 → 实际治疗 → 复查结果。"""
        out = []
        for pid, pv in sorted(self.bv.patients.items()):
            if patient_id and pid != patient_id:
                continue
            for ex in pv.exceptions:
                review_at_decision = next(
                    (r for r in reversed(pv.clinical_reviews)
                     if r["occurred_at"] <= ex["occurred_at"]), None)
                linked_slots = [s for s in pv.schedules if s.get("basis_event_id") == ex["event_id"]]
                slot_summaries = []
                for s in linked_slots:
                    tx = next((t for t in pv.treatments if t["slot_id"] == s["slot_id"]), None)
                    slot_summaries.append({
                        "slot_id": s["slot_id"],
                        "scheduled_start": s["scheduled_start"],
                        "state": s["state"],
                        "scheduled_by": s["scheduled_by"],
                        "treatment": None if tx is None else {
                            "procedure": tx["procedure"],
                            "performed_at": tx["performed_at"],
                            "surgeon_id": tx["surgeon_id"],
                            "supplies_actually_used": tx["supplies_actually_used"],
                            "supply_deviation": tx.get("supply_deviation"),
                            "complications": tx["complications"],
                            "outcome_at_discharge": tx["outcome_at_discharge"],
                        },
                    })
                out.append({
                    "patient_local_id": pid,
                    "exception_event_id": ex["event_id"],
                    "exception_type": ex["exception_type"],
                    "rationale": ex["rationale"],
                    "evidence_refs": ex["evidence_refs"],
                    "requested_by": ex["requested_by"],
                    "approved_by": ex["approved_by"],
                    "approver_role": ex["approver_role"],
                    "decision_at": ex["occurred_at"],
                    "clinical_opinion_at_decision": None if review_at_decision is None else {
                        "ophthalmologist_id": review_at_decision["ophthalmologist_id"],
                        "indication_for_surgery": review_at_decision["indication_for_surgery"],
                        "fitness": review_at_decision["fitness"],
                        "confidence": review_at_decision["confidence"],
                        "rationale": review_at_decision["rationale"],
                    },
                    "resource_snapshot_at_approval": ex["resource_snapshot"],
                    "linked_slots": slot_summaries,
                    "followup_outcomes": [
                        {"followup_no": f["followup_no"], "checked_at": f.get("checked_at"),
                         "result": f.get("result"), "state": f["state"]}
                        for f in pv.followups],
                    "escalations": [{"red_flag": e["red_flag"], "state": e["state"],
                                     "resolution": e.get("resolution")}
                                    for e in pv.escalations],
                })
        return out
