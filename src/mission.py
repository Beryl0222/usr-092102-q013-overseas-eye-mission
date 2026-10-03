"""诊疗接力命令服务：把筛查到术后复查的完整责任链落到不可变事件。

关键规则（见 docs/domain.md §3）：
- AI 结果只能记录为建议（AI_TRIAGE_RECORDED / grassroots_ai 筛查的置信边界），
  不产生排期权；只有眼科医生 CLINICAL_REVIEWED 确立手术适应证。
- 常规/医学紧急/无障碍/人道四通道分别记录依据，批准角色不同，申请人≠批准人。
- 排期对设备、班次、耗材的占用在一个事务里提交，任一冲突整体失败。
- 治疗完成才把耗材预占转为实耗；术后必须有当地接手、复查安排、升级路径，批次才能关闭。
"""

import uuid
from collections import defaultdict
from datetime import datetime

from src.errors import RuleViolation
from src.events import now_iso, parse_ts
from src.policy import (
    CONFIDENCE_BANDS,
    EXCEPTION_APPROVER,
    OPHTHALMOLOGIST,
    SCREENING_SOURCES,
    SURGEON,
)
from src.projection import project
from src.resources import ResourceLedger
from src.store import EventStore


def _actor(actor: dict) -> tuple[str, str]:
    return actor["staff_id"], actor["role"]


class MissionService:
    def __init__(self, store: EventStore, batch_id: str) -> None:
        self.store = store
        self.batch_id = batch_id

    # ================= 通用辅助 =================

    def _view(self) -> "object":
        return project(self.store.all_events()).get(self.batch_id)

    def _ledger(self, events=None) -> ResourceLedger:
        return ResourceLedger(events if events is not None else self.store.all_events())

    def _build(self, counts: dict, agg_type: str, agg_id: str, event_type: str,
               payload: dict, summary: str, *, occurred_at: str,
               correlation_id: str | None = None, causation_id: str | None = None,
               event_id: str | None = None) -> dict:
        eid = event_id or f"{self.batch_id}-{uuid.uuid4().hex[:10]}"
        # 断网重放：event_id 已存在时沿用原版本号，且不占用新的流位置
        existing = self.store.get_event(eid)
        if existing is not None:
            if existing["aggregate_id"] != agg_id:
                raise RuleViolation(
                    f"event_id={eid} 已属于其他聚合 {existing['aggregate_id']}，拒绝重放"
                )
            ver = existing["version"]
        else:
            ver = self.store.version(agg_id) + counts[agg_id] + 1
            counts[agg_id] += 1
        event = {
            "event_id": eid,
            "event_type": event_type,
            "aggregate_type": agg_type,
            "aggregate_id": agg_id,
            "batch_id": self.batch_id,
            "occurred_at": occurred_at,
            "version": ver,
            "summary": summary,
            "payload": payload,
        }
        if correlation_id:
            event["correlation_id"] = correlation_id
        if causation_id:
            event["causation_id"] = causation_id
        return event

    def _emit_patient(self, patient_id: str, event_type: str, payload: dict, summary: str,
                      *, occurred_at: str, event_id: str | None = None,
                      correlation_id: str | None = None, causation_id: str | None = None) -> dict:
        counts: dict[str, int] = defaultdict(int)
        event = self._build(counts, "mission_patient", patient_id, event_type, payload,
                            summary, occurred_at=occurred_at, event_id=event_id,
                            correlation_id=correlation_id, causation_id=causation_id)
        return self.store.commit([event])[0]

    @staticmethod
    def _require_role(actor: dict, role: str, action: str) -> None:
        if actor.get("role") != role:
            raise RuleViolation(f"{action} 需要角色 {role}，当前为 {actor.get('role')}")

    # ================= 批次 =================

    def register_batch(self, actor: dict, *, site: str, surgery_days: list[str],
                       equipment: list[dict], supplies: dict[str, int],
                       staffing: list[dict], occurred_at: str | None = None,
                       event_id: str | None = None) -> dict:
        """登记批次，声明有限资源：设备清单（仅一台超声乳化仪也照实登记）、
        耗材期初库存、人员班次窗口。"""
        payload = {
            "mission_code": self.batch_id,
            "site": site,
            "surgery_days": surgery_days,
            "registered_by": _actor(actor)[0],
            "equipment": equipment,
            "supplies": supplies,
            "staffing": staffing,
        }
        counts: dict[str, int] = defaultdict(int)
        event = self._build(counts, "task_batch", self.batch_id, "BATCH_REGISTERED",
                            payload, f"批次登记：{site}，{len(surgery_days)} 个手术日",
                            occurred_at=occurred_at or now_iso(), event_id=event_id)
        return self.store.commit([event])[0]

    # ================= 筛查与 AI 辅助分流 =================

    def record_screening(self, actor: dict, patient_id: str, *, source: str,
                         source_ref: str, findings: dict,
                         ai: dict | None = None, raw_evidence_ref: str | None = None,
                         occurred_at: str | None = None,
                         event_id: str | None = None) -> dict:
        """登记一条筛查来源（基层AI设备/医院检查/家属转介，可多条并存）。

        grassroots_ai 来源必须给 ai：{model, score, confidence_band, boundary_note}，
        明确置信边界；AI 数据只是证据之一，不构成手术决定。
        """
        if source not in SCREENING_SOURCES:
            raise RuleViolation(f"未知筛查来源：{source}")
        if source == "grassroots_ai":
            if not ai or ai.get("confidence_band") not in CONFIDENCE_BANDS:
                raise RuleViolation("基层AI筛查必须给出 confidence_band（high/medium/low）")
            if not ai.get("boundary_note"):
                raise RuleViolation("基层AI筛查必须给出 boundary_note 说明置信边界")

        # 证据序号按实际已留痕的证据聚合计数，保证断网重放时序号与 aggregate_id 不漂移
        seq = len(self.store.aggregates(f"{patient_id}#src")) + 1
        occurred_at = occurred_at or now_iso()
        payload = {
            "patient_local_id": patient_id,
            "source": source,
            "source_ref": source_ref,
            "recorded_by": _actor(actor)[0],
            "findings": findings,
        }
        if ai:
            payload["ai"] = ai

        counts: dict[str, int] = defaultdict(int)
        events = [self._build(counts, "mission_patient", patient_id, "SCREENING_RECEIVED",
                              payload, f"登记筛查来源：{source}", occurred_at=occurred_at,
                              event_id=event_id)]
        if raw_evidence_ref:
            evidence_eid = f"{event_id}-evidence" if event_id else None
            evidence_agg = f"{patient_id}#src{seq}"
            if evidence_eid:
                prior = self.store.get_event(evidence_eid)
                if prior is not None:  # 断网重放：沿用原证据聚合 id，避免序号漂移
                    evidence_agg = prior["aggregate_id"]
            ev_payload = {"patient_local_id": patient_id, "source": source,
                          "raw_evidence_ref": raw_evidence_ref}
            events.append(self._build(
                counts, "screening_evidence", evidence_agg,
                "SCREENING_RECEIVED", ev_payload,
                f"筛查原始证据留痕：{source}", occurred_at=occurred_at,
                event_id=evidence_eid))
        return self.store.commit(events)[0]

    def record_ai_triage(self, actor: dict, patient_id: str, *, model_version: str,
                         suggested_priority: str, confidence_band: str,
                         boundary_note: str, occurred_at: str | None = None,
                         event_id: str | None = None) -> dict:
        """记录 AI 辅助分流建议。任何置信带都只是建议，不产生排期权。"""
        if confidence_band not in CONFIDENCE_BANDS:
            raise RuleViolation("confidence_band 必须是 high/medium/low")
        payload = {
            "patient_local_id": patient_id,
            "model_version": model_version,
            "suggested_priority": suggested_priority,
            "confidence_band": confidence_band,
            "boundary_note": boundary_note,
            "reviewed_by_ai_gateway": _actor(actor)[0],
            "advisory_only": True,
        }
        return self._emit_patient(patient_id, "AI_TRIAGE_RECORDED", payload,
                                  f"AI分流建议（{confidence_band}）：{suggested_priority}，仅供医生参考",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    # ================= 医生复核 =================

    def clinical_review(self, actor: dict, patient_id: str, *, indication_for_surgery: str,
                        fitness: str, required_supplies: list[str],
                        equipment_needs: list[str], estimated_minutes: int,
                        rationale: str, contraindications: str = "",
                        confidence: str = "confirmed", occurred_at: str | None = None,
                        event_id: str | None = None) -> dict:
        """眼科医生人工复核——唯一能确立手术适应证的环节。"""
        self._require_role(actor, OPHTHALMOLOGIST, "临床复核")
        if fitness not in ("fit", "unfit", "defer"):
            raise RuleViolation("fitness 必须是 fit/unfit/defer")
        if fitness == "fit" and not indication_for_surgery:
            raise RuleViolation("fit 的复核必须写明手术适应证")
        if confidence not in ("confirmed", "uncertain"):
            raise RuleViolation("confidence 必须是 confirmed/uncertain")

        pv = self._view().patient(patient_id)
        ai_present = any(s["source"] == "grassroots_ai" for s in pv.screenings) or bool(pv.ai_triages)
        payload = {
            "patient_local_id": patient_id,
            "ophthalmologist_id": _actor(actor)[0],
            "indication_for_surgery": indication_for_surgery,
            "contraindications": contraindications,
            "fitness": fitness,
            "required_supplies": required_supplies,
            "equipment_needs": equipment_needs,
            "estimated_minutes": estimated_minutes,
            "confidence": confidence,
            "rationale": rationale,
            "ai_was_input": ai_present,
            "ai_overridden": False,  # 由调用方在理由中说明分歧；AI 无决定权故无所谓“推翻批准”
        }
        return self._emit_patient(patient_id, "CLINICAL_REVIEWED", payload,
                                  f"眼科医生复核：{fitness}；适应证：{indication_for_surgery[:40]}",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    # ================= 优先级与四类通道 =================

    def set_routine_priority(self, actor: dict, patient_id: str, *, priority: int,
                             reason: str, occurred_at: str | None = None,
                             event_id: str | None = None) -> dict:
        """常规通道：分诊护士提议、现场协调员确认（本方法要求 coordinator 角色）。"""
        self._require_role(actor, "coordinator", "常规优先级确认")
        payload = {"patient_local_id": patient_id, "channel": "routine",
                   "priority": priority, "reason": reason,
                   "set_by": _actor(actor)[0]}
        return self._emit_patient(patient_id, "PRIORITY_SET", payload,
                                  f"常规优先级 {priority}：{reason}",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def approve_exception(self, actor: dict, patient_id: str, *, exception_type: str,
                          requested_by: str, rationale: str, evidence_refs: list[str],
                          occurred_at: str | None = None,
                          event_id: str | None = None) -> dict:
        """批准医学紧急/无障碍/人道例外。批准角色按通道分立，请求人≠批准人；
        事件固化批准时刻的资源快照，供事后完整回看。"""
        approver_role = EXCEPTION_APPROVER.get(exception_type)
        if not approver_role:
            raise RuleViolation(f"未知例外类型：{exception_type}")
        self._require_role(actor, approver_role, f"{exception_type} 例外批准")
        approver_id = _actor(actor)[0]
        if requested_by == approver_id:
            raise RuleViolation("例外请求人与批准人不得为同一人")
        if not rationale or not evidence_refs:
            raise RuleViolation("例外必须记录理由与证据引用")

        pv = self._view().patient(patient_id)
        if exception_type == "medical_urgency" and not pv.clinical_reviews:
            raise RuleViolation("医学紧急例外必须先有眼科医生的医学意见（CLINICAL_REVIEWED）")
        if not pv.screenings:
            raise RuleViolation("例外必须基于至少一条已登记筛查证据")

        payload = {
            "patient_local_id": patient_id,
            "exception_type": exception_type,
            "rationale": rationale,
            "evidence_refs": evidence_refs,
            "requested_by": requested_by,
            "approved_by": approver_id,
            "approver_role": approver_role,
            "decision_at": occurred_at or now_iso(),
            "resource_snapshot": self._ledger().snapshot(),
        }
        return self._emit_patient(patient_id, "EXCEPTION_APPROVED", payload,
                                  f"{exception_type} 例外批准（{approver_id}）：{rationale[:40]}",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    # ================= 知情同意 =================

    def give_consent(self, actor: dict, patient_id: str, *, scope: str,
                     explained_by: str, witness: str, language: str,
                     materials: list[str], occurred_at: str | None = None,
                     event_id: str | None = None) -> dict:
        if scope not in ("treatment", "treatment_plus_nonclinical"):
            raise RuleViolation("同意范围必须是 treatment 或 treatment_plus_nonclinical")
        payload = {"patient_local_id": patient_id, "scope": scope,
                   "explained_by": explained_by, "witness": witness,
                   "language": language, "materials": materials,
                   "consent_at": occurred_at or now_iso(),
                   "received_by": _actor(actor)[0]}
        return self._emit_patient(patient_id, "CONSENT_GIVEN", payload,
                                  f"知情同意：{scope}（语言 {language}）",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def withdraw_consent(self, actor: dict, patient_id: str, *, scope_withdrawn: str,
                         occurred_at: str | None = None,
                         event_id: str | None = None) -> dict:
        """撤回非诊疗用途不影响已形成的诊疗/安全记录；撤回未来治疗则不得再排新手术。"""
        if scope_withdrawn not in ("nonclinical", "all_future"):
            raise RuleViolation("撤回范围必须是 nonclinical 或 all_future")
        payload = {"patient_local_id": patient_id,
                   "scope_withdrawn": scope_withdrawn,
                   "withdrawn_at": occurred_at or now_iso(),
                   "received_by": _actor(actor)[0]}
        return self._emit_patient(patient_id, "CONSENT_WITHDRAWN", payload,
                                  f"同意撤回：{scope_withdrawn}（诊疗安全记录保留）",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    # ================= 排期（资源原子占用） =================

    def schedule_surgery(self, actor: dict, patient_id: str, *, slot_id: str,
                         scheduled_start: str, scheduled_end: str,
                         equipment_resource_ids: list[str], staff_resource_ids: list[str],
                         supply_demands: dict[str, int], basis: str,
                         basis_event_id: str | None = None,
                         occurred_at: str | None = None,
                         event_ids: list[str] | None = None) -> list[dict]:
        """排期：同时预占设备、人员班次、耗材，全部通过才提交。

        basis=routine 需要 PRIORITY_SET；其余三种需要引用对应患者的 EXCEPTION_APPROVED。
        必须先有眼科医生 fit 复核与 treatment 同意。AI 建议一律不能作为排期依据。
        """
        pv = self._view().patient(patient_id)
        review = pv.latest_review
        if review is None or review.get("fitness") != "fit":
            raise RuleViolation("排期前必须有眼科医生 fitness=fit 的 CLINICAL_REVIEWED")
        if "treatment" not in pv.effective_scopes:
            raise RuleViolation("排期前必须取得 treatment 范围知情同意且未撤回")

        missing_supplies = sorted(set(review["required_supplies"]) - set(supply_demands))
        if missing_supplies:
            raise RuleViolation(f"排期耗材未覆盖医生复核要求：{missing_supplies}")
        missing_equip = sorted(set(review["equipment_needs"]) - set(equipment_resource_ids))
        if missing_equip:
            raise RuleViolation(f"排期设备未覆盖医生复核要求：{missing_equip}")

        if basis == "routine":
            if pv.priority is None:
                raise RuleViolation("常规排期需要协调员确认的 PRIORITY_SET")
        elif basis in EXCEPTION_APPROVER:
            ex = next((x for x in pv.exceptions if x["event_id"] == basis_event_id
                       and x["exception_type"] == basis), None)
            if ex is None:
                raise RuleViolation(f"{basis} 排期必须引用有效的 EXCEPTION_APPROVED 事件")
            if any(s.get("basis_event_id") == basis_event_id for s in pv.schedules):
                raise RuleViolation("同一例外批准只能支撑一次排期，防止一次开口被重复使用")
        else:
            raise RuleViolation(f"未知排期依据通道：{basis}")

        parse_ts(scheduled_start)
        parse_ts(scheduled_end)
        correlation_id = f"sch:{slot_id}"
        occurred_at = occurred_at or now_iso()

        demands: list[dict] = []
        for rid in equipment_resource_ids:
            demands.append({"kind": "equipment", "resource_id": rid,
                            "interval_start": scheduled_start, "interval_end": scheduled_end})
        for rid in staff_resource_ids:
            demands.append({"kind": "staff_shift", "resource_id": rid,
                            "interval_start": scheduled_start, "interval_end": scheduled_end})
        for code, qty in supply_demands.items():
            demands.append({"kind": "supply", "supply_code": code, "qty": qty})

        counts: dict[str, int] = defaultdict(int)
        events: list[dict] = []
        ids = event_ids or []
        id_iter = iter(ids)

        def next_id(suffix: str) -> str | None:
            try:
                return f"{next(id_iter)}-{suffix}"
            except StopIteration:
                return None

        for d in demands:
            if d["kind"] in ("equipment", "staff_shift"):
                rid = d["resource_id"]
                p = {"kind": d["kind"], "resource_id": rid,
                     "interval_start": scheduled_start, "interval_end": scheduled_end,
                     "correlation_id": correlation_id, "purpose_slot_id": slot_id,
                     "patient_local_id": patient_id}
                events.append(self._build(
                    counts, "resource", rid, "SLOT_RESERVED", p,
                    f"预占 {d['kind']} {rid} 用于 {slot_id}",
                    occurred_at=occurred_at, correlation_id=correlation_id,
                    event_id=next_id(f"hold-{rid}")))
            else:
                rid = f"supply:{d['supply_code']}"
                p = {"kind": "supply", "supply_code": d["supply_code"], "qty": d["qty"],
                     "correlation_id": correlation_id, "purpose_slot_id": slot_id,
                     "patient_local_id": patient_id}
                events.append(self._build(
                    counts, "resource", rid, "SLOT_RESERVED", p,
                    f"预占耗材 {d['supply_code']} x{d['qty']} 用于 {slot_id}",
                    occurred_at=occurred_at, correlation_id=correlation_id,
                    event_id=next_id(f"hold-{d['supply_code']}")))

        schedule_payload = {
            "patient_local_id": patient_id,
            "slot_id": slot_id,
            "scheduled_start": scheduled_start,
            "scheduled_end": scheduled_end,
            "equipment_resource_ids": equipment_resource_ids,
            "staff_resource_ids": staff_resource_ids,
            "supply_demands": supply_demands,
            "basis": basis,
            "basis_event_id": basis_event_id,
            "scheduled_by": _actor(actor)[0],
        }
        events.append(self._build(
            counts, "mission_patient", patient_id, "SURGERY_SCHEDULED", schedule_payload,
            f"排期 {slot_id}（{basis}）：{scheduled_start}",
            occurred_at=occurred_at, correlation_id=correlation_id,
            causation_id=basis_event_id, event_id=next_id("sched")))

        def precheck(projected: list[dict]) -> None:
            ResourceLedger(projected).assert_can_reserve(demands, correlation_id)

        return self.store.commit(events, precheck=precheck)

    def release_slot(self, actor: dict, patient_id: str, slot_id: str, *, reason: str,
                     occurred_at: str | None = None) -> list[dict]:
        """释放已排期占用（如患者撤回未来治疗、医学状况变化）。"""
        pv = self._view().patient(patient_id)
        sch = next((s for s in pv.schedules if s["slot_id"] == slot_id and s["state"] == "scheduled"), None)
        if sch is None:
            raise RuleViolation(f"没有处于 scheduled 状态的 {slot_id}")
        correlation_id = f"sch:{slot_id}"
        occurred_at = occurred_at or now_iso()
        counts: dict[str, int] = defaultdict(int)
        events: list[dict] = []
        for rid in sch["equipment_resource_ids"] + sch["staff_resource_ids"]:
            res = self._ledger().resources.get(rid)
            kind = res["kind"] if res else "equipment"
            p = {"kind": kind, "resource_id": rid, "correlation_id": correlation_id,
                 "purpose_slot_id": slot_id, "patient_local_id": patient_id, "reason": reason}
            events.append(self._build(counts, "resource", rid, "SLOT_RELEASED", p,
                                      f"释放 {rid}（{reason}）", occurred_at=occurred_at,
                                      correlation_id=correlation_id))
        for code in sch["supply_demands"]:
            rid = f"supply:{code}"
            p = {"kind": "supply", "supply_code": code, "correlation_id": correlation_id,
                 "purpose_slot_id": slot_id, "patient_local_id": patient_id, "reason": reason}
            events.append(self._build(counts, "resource", rid, "SLOT_RELEASED", p,
                                      f"释放耗材预占 {code}（{reason}）",
                                      occurred_at=occurred_at, correlation_id=correlation_id))
        return self.store.commit(events)

    # ================= 实际治疗 =================

    def complete_treatment(self, actor: dict, patient_id: str, *, slot_id: str,
                           procedure: str, performed_at: str,
                           supplies_actually_used: dict[str, int],
                           outcome_at_discharge: str,
                           complications: str = "", notes: str = "",
                           event_id: str | None = None) -> list[dict]:
        """记录实际治疗：预占转实耗、释放设备班次，与排期不符必须写明 deviation_reason。"""
        self._require_role(actor, SURGEON, "治疗完成记录")
        pv = self._view().patient(patient_id)
        sch = next((s for s in pv.schedules if s["slot_id"] == slot_id and s["state"] == "scheduled"), None)
        if sch is None:
            raise RuleViolation(f"没有处于 scheduled 状态的 {slot_id}，无法记录治疗")

        planned = sch["supply_demands"]
        deviation = {k: (planned.get(k, 0), supplies_actually_used.get(k, 0))
                     for k in set(planned) | set(supplies_actually_used)
                     if planned.get(k, 0) != supplies_actually_used.get(k, 0)}

        correlation_id = f"sch:{slot_id}"
        counts: dict[str, int] = defaultdict(int)
        events: list[dict] = []
        for rid in sch["equipment_resource_ids"] + sch["staff_resource_ids"]:
            res = self._ledger().resources.get(rid)
            kind = res["kind"] if res else "equipment"
            p = {"kind": kind, "resource_id": rid, "correlation_id": correlation_id,
                 "purpose_slot_id": slot_id, "patient_local_id": patient_id,
                 "reason": "treatment_completed"}
            events.append(self._build(counts, "resource", rid, "SLOT_RELEASED", p,
                                      f"治疗完成释放 {rid}", occurred_at=performed_at,
                                      correlation_id=correlation_id))
        for code, qty in supplies_actually_used.items():
            p = {"supply_code": code, "qty": qty, "correlation_id": correlation_id,
                 "used_for_slot_id": slot_id, "patient_local_id": patient_id}
            events.append(self._build(counts, "resource", f"supply:{code}",
                                      "SUPPLIES_CONSUMED", p,
                                      f"耗材实耗 {code} x{qty}", occurred_at=performed_at,
                                      correlation_id=correlation_id))
        treatment_payload = {
            "patient_local_id": patient_id,
            "slot_id": slot_id,
            "surgeon_id": _actor(actor)[0],
            "procedure": procedure,
            "performed_at": performed_at,
            "supplies_actually_used": supplies_actually_used,
            "complications": complications,
            "outcome_at_discharge": outcome_at_discharge,
            "notes": notes,
            "supply_deviation": deviation,
        }
        events.append(self._build(
            counts, "mission_patient", patient_id, "TREATMENT_COMPLETED", treatment_payload,
            f"治疗完成：{procedure}", occurred_at=performed_at,
            correlation_id=correlation_id, event_id=event_id))

        def precheck(projected: list[dict]) -> None:
            ResourceLedger(projected).assert_consumption(supplies_actually_used, correlation_id)

        return self.store.commit(events, precheck=precheck)

    # ================= 交接、复查、升级 =================

    def _handoff_id(self, patient_id: str) -> str:
        return f"{patient_id}#handoff"

    def assign_handoff(self, actor: dict, patient_id: str, *, local_clinician_id: str,
                       local_facility: str, contact: str, responsibilities: list[str],
                       occurred_at: str | None = None, event_id: str | None = None) -> dict:
        payload = {"patient_local_id": patient_id,
                   "local_clinician_id": local_clinician_id,
                   "local_facility": local_facility, "contact": contact,
                   "responsibilities": responsibilities,
                   "assigned_by": _actor(actor)[0]}
        return self._emit_handoff(patient_id, "HANDOFF_ASSIGNED", payload,
                                  f"指定当地接手：{local_clinician_id}（{local_facility}）",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def accept_handoff(self, actor: dict, patient_id: str, *,
                       understood_red_flags: list[str], occurred_at: str | None = None,
                       event_id: str | None = None) -> dict:
        """必须由被指定的当地医护本人接受，未接受不算闭环。"""
        pv = self._view().patient(patient_id)
        if pv.handoff is None:
            raise RuleViolation("尚未指定接手医护，无法接受")
        if actor["staff_id"] != pv.handoff["local_clinician_id"]:
            raise RuleViolation("只有被指定的当地医护本人可以接受交接")
        payload = {"patient_local_id": patient_id,
                   "accepted_by": actor["staff_id"],
                   "understood_red_flags": understood_red_flags}
        return self._emit_handoff(patient_id, "HANDOFF_ACCEPTED", payload,
                                  f"{actor['staff_id']} 接受术后交接",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def schedule_followup(self, actor: dict, patient_id: str, *, followup_no: int,
                          due_at: str, modality: str,
                          responsible_local_clinician_id: str,
                          escalation_path: dict, occurred_at: str | None = None,
                          event_id: str | None = None) -> dict:
        for key in ("level1", "level2", "mission_contact"):
            if not escalation_path.get(key):
                raise RuleViolation(f"复查安排必须给出升级路径 {key}")
        payload = {"patient_local_id": patient_id, "followup_no": followup_no,
                   "due_at": due_at, "modality": modality,
                   "responsible_local_clinician_id": responsible_local_clinician_id,
                   "escalation_path": escalation_path,
                   "scheduled_by": _actor(actor)[0]}
        return self._emit_handoff(patient_id, "FOLLOWUP_SCHEDULED", payload,
                                  f"复查 {followup_no} 安排于 {due_at}（{modality}）",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def record_followup_outcome(self, actor: dict, patient_id: str, *, followup_no: int,
                                checked_at: str, result: str, va: str | None = None,
                                findings: str = "", event_id: str | None = None) -> dict:
        self._require_role(actor, "local_clinician", "复查结果记录")
        payload = {"patient_local_id": patient_id, "followup_no": followup_no,
                   "checked_at": checked_at, "result": result, "va": va,
                   "findings": findings, "recorder": actor["staff_id"]}
        return self._emit_handoff(patient_id, "FOLLOWUP_OUTCOME_RECORDED", payload,
                                  f"复查 {followup_no} 结果：{result[:40]}",
                                  occurred_at=checked_at, event_id=event_id)

    def open_escalation(self, actor: dict, patient_id: str, *, red_flag: str,
                        target: str, linked_followup_no: int | None = None,
                        occurred_at: str | None = None, event_id: str | None = None) -> dict:
        payload = {"patient_local_id": patient_id, "red_flag": red_flag,
                   "opened_at": occurred_at or now_iso(), "opened_by": _actor(actor)[0],
                   "target": target, "linked_followup_no": linked_followup_no}
        return self._emit_handoff(patient_id, "ESCALATION_OPENED", payload,
                                  f"异常升级：{red_flag[:40]} → {target}",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def resolve_escalation(self, actor: dict, patient_id: str, *, resolution: str,
                           residual_risk: str, occurred_at: str | None = None,
                           event_id: str | None = None) -> dict:
        payload = {"patient_local_id": patient_id, "resolution": resolution,
                   "resolved_at": occurred_at or now_iso(),
                   "resolved_by": _actor(actor)[0], "residual_risk": residual_risk}
        return self._emit_handoff(patient_id, "ESCALATION_RESOLVED", payload,
                                  f"升级处置完成：{resolution[:40]}",
                                  occurred_at=occurred_at or now_iso(), event_id=event_id)

    def _emit_handoff(self, patient_id: str, event_type: str, payload: dict, summary: str,
                      *, occurred_at: str, event_id: str | None = None) -> dict:
        counts: dict[str, int] = defaultdict(int)
        event = self._build(counts, "followup_handoff", self._handoff_id(patient_id),
                            event_type, payload, summary, occurred_at=occurred_at,
                            event_id=event_id)
        return self.store.commit([event])[0]

    # ================= 批次关闭（返程闭环） =================

    def closure_gaps(self) -> list[dict]:
        """每名已治疗患者必须有：已接受的交接、至少一次复查安排、无未解决升级。"""
        bv = self._view()
        gaps = []
        if bv is None:
            return gaps
        for pid, pv in sorted(bv.patients.items()):
            if not pv.treatments:
                continue
            patient_gaps = []
            if pv.handoff is None:
                patient_gaps.append("未指定当地接手医护")
            elif pv.handoff.get("state") != "accepted":
                patient_gaps.append("当地接手医护尚未接受")
            if not pv.followups:
                patient_gaps.append("未安排术后复查")
            if any(e["state"] == "open" for e in pv.escalations):
                patient_gaps.append("存在未解决的异常升级")
            if patient_gaps:
                gaps.append({"patient_local_id": pid, "gaps": patient_gaps})
        return gaps

    def close_batch(self, actor: dict, *, occurred_at: str | None = None,
                    event_id: str | None = None) -> dict:
        gaps = self.closure_gaps()
        if gaps:
            raise RuleViolation(f"批次存在 {len(gaps)} 名未闭环患者，不能返程关闭：{gaps}")
        payload = {"closed_by": _actor(actor)[0],
                   "closed_at": occurred_at or now_iso(), "open_followups": []}
        counts: dict[str, int] = defaultdict(int)
        event = self._build(counts, "task_batch", self.batch_id, "BATCH_CLOSED", payload,
                            "批次关闭：全部术后患者已闭环",
                            occurred_at=occurred_at or now_iso(), event_id=event_id)
        return self.store.commit([event])[0]
