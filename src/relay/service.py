"""海外光明行诊疗接力后端。

把任务批次、患者本地标识、筛查来源与置信边界、医生复核、手术适应证、
资源需求、优先级理由、知情同意、排期、实际治疗与复查责任串成一条
可审计的链路。跨方里程碑落成 contracts/domain.schema.json 规定的领域事件。

核心不变量：
- AI 筛查只用于辅助分流；手术适应证必须由登记的眼科医生在医生现场检查
  或医院检查的基础上确认，AI 结果与家属转介不能单独决定手术。
- 常规排期、医学紧急、无障碍、人道例外分别记录依据，并由不同角色确认；
  非常规决定留下 EXCEPTION_APPROVED 事件，载荷含当时资源快照。
- 设备、耗材、人员班次的占用在排期时一次性校验并提交：要么全部落位，
  要么整体失败，不留半成品占用。
- 每个命令携带幂等键；断网补录保留原发生时间（occurred_at），重放不产生
  副作用（不重复建单、不重复扣库存）。
- 跨国团队只交换治疗所需的最少资料；患者撤回非诊疗用途不影响已经形成的
  安全记录。
- 返程前每名已治疗患者必须有当地接手医护、复查时间与异常升级路径。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Callable, Iterable

from src.relay.errors import (
    ConsentError,
    InvariantViolation,
    NotFound,
    ResourceConflict,
    RoleViolation,
)
from src.relay.events import EventStore
from src.relay.models import (
    PRIORITY_CONFIRM_ROLES,
    DEFAULT_PRIORITY_WEIGHTS,
    NON_TREATMENT_SCOPES,
    Batch,
    ClinicalReview,
    Confidence,
    ConsentRecord,
    ConsentScope,
    Equipment,
    Handoff,
    HandoffStatus,
    Patient,
    PriorityDecision,
    PriorityKind,
    Role,
    ScreeningEvidence,
    Shift,
    Slot,
    SlotStatus,
    SourceKind,
    StaffMember,
    TreatmentRecord,
)

_SOURCE_LABELS = {
    SourceKind.AI_DEVICE: "AI 设备",
    SourceKind.HOSPITAL_EXAM: "医院检查",
    SourceKind.FAMILY_REFERRAL: "家属转介",
}

_KIND_LABELS = {
    PriorityKind.MEDICAL_URGENCY: "医学紧急",
    PriorityKind.ACCESSIBILITY: "无障碍",
    PriorityKind.HUMANITARIAN_EXCEPTION: "人道例外",
}


def _ts(value: datetime | str) -> datetime:
    """归一化时间输入；跨国协作要求所有时间携带时区。"""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        raise InvariantViolation("时间必须携带时区信息")
    return moment


def _day(value: date | str) -> date:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class RelayService:
    """诊疗接力后端。进程内实现；事件存储与幂等表可整体替换为持久化实现。"""

    def __init__(
        self,
        *,
        schema_path=None,
        clock: Callable[[], datetime] | None = None,
        priority_weights: dict[PriorityKind, int] | None = None,
    ) -> None:
        self._events = EventStore(schema_path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._priority_weights = priority_weights or DEFAULT_PRIORITY_WEIGHTS
        self._idem: dict[str, dict] = {}
        self._batches: dict[str, Batch] = {}
        self._patients: dict[str, Patient] = {}
        self._evidence: dict[str, ScreeningEvidence] = {}
        self._reviews: dict[str, ClinicalReview] = {}
        self._priorities: dict[str, PriorityDecision] = {}
        self._consents: dict[tuple[str, ConsentScope], ConsentRecord] = {}
        self._staff: dict[str, StaffMember] = {}
        self._equipment: dict[str, Equipment] = {}
        self._stock: dict[tuple[str, str], int] = {}
        self._shifts: list[Shift] = []
        self._slots: dict[str, Slot] = {}
        self._treatments: dict[str, TreatmentRecord] = {}
        self._handoffs: dict[str, Handoff] = {}
        self._evidence_seq = 0
        self._slot_seq = 0
        self._handoff_seq = 0

    # ------------------------------------------------------------------
    # 基础设施：幂等执行与事件入账
    # ------------------------------------------------------------------

    def _once(self, idempotency_key: str, producer: Callable[[], dict]) -> dict:
        """按幂等键执行命令：重放返回首次结果，不产生副作用。"""
        if not idempotency_key:
            raise InvariantViolation("命令必须携带幂等键")
        if idempotency_key in self._idem:
            return deepcopy(self._idem[idempotency_key])
        result = producer()
        self._idem[idempotency_key] = deepcopy(result)
        return result

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: datetime | str,
        summary: str,
        payload: dict,
    ) -> dict:
        occurred = _ts(occurred_at)
        version = self._events.next_version(aggregate_type, aggregate_id)
        event = {
            "event_id": f"evt-{aggregate_type}-{aggregate_id}-{version:04d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred.isoformat(),
            "version": version,
            "summary": summary,
            "recorded_at": self._clock().isoformat(),
            "payload": payload,
        }
        return self._events.append(event)

    @property
    def event_store(self) -> EventStore:
        return self._events

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        return deepcopy(self._events.events_for(aggregate_type, aggregate_id))

    # ------------------------------------------------------------------
    # 登记：批次、人员、患者、资源
    # ------------------------------------------------------------------

    def register_batch(
        self,
        *,
        idempotency_key: str,
        batch_id: str,
        title: str,
        location: str,
        surgery_days: Iterable[date | str],
        occurred_at: datetime | str,
    ) -> dict:
        def _run() -> dict:
            if batch_id in self._batches:
                raise InvariantViolation(f"任务批次已存在：{batch_id}")
            days = frozenset(_day(d) for d in surgery_days)
            if not days:
                raise InvariantViolation("任务批次必须至少有一个手术日")
            _ts(occurred_at)
            self._batches[batch_id] = Batch(batch_id, title, location, days)
            return {"batch_id": batch_id, "surgery_days": sorted(d.isoformat() for d in days)}

        return self._once(idempotency_key, _run)

    def register_staff(
        self,
        *,
        idempotency_key: str,
        staff_id: str,
        name: str,
        roles: Iterable[str | Role],
        team: str,
    ) -> dict:
        def _run() -> dict:
            if staff_id in self._staff:
                raise InvariantViolation(f"人员已登记：{staff_id}")
            parsed = frozenset(Role(r) for r in roles)
            if not parsed:
                raise InvariantViolation("人员至少需要一个角色")
            self._staff[staff_id] = StaffMember(staff_id, name, parsed, team)
            return {"staff_id": staff_id, "roles": sorted(r.value for r in parsed)}

        return self._once(idempotency_key, _run)

    def register_patient(
        self,
        *,
        idempotency_key: str,
        batch_id: str,
        patient_ref: str,
        local_identifier: str,
        occurred_at: datetime | str,
        identity: dict | None = None,
        clinical: dict | None = None,
    ) -> dict:
        """登记患者。identity 为受限身份资料，不进入跨国交换；clinical 为临床资料。"""

        def _run() -> dict:
            self._batch(batch_id)
            if patient_ref in self._patients:
                raise InvariantViolation(f"患者引用已存在：{patient_ref}")
            if not local_identifier:
                raise InvariantViolation("患者本地标识不能为空")
            self._patients[patient_ref] = Patient(
                patient_ref=patient_ref,
                batch_id=batch_id,
                local_identifier=local_identifier,
                registered_at=_ts(occurred_at),
                identity=deepcopy(identity) if identity else {},
                clinical=deepcopy(clinical) if clinical else {},
            )
            return {"patient_ref": patient_ref, "local_identifier": local_identifier}

        return self._once(idempotency_key, _run)

    def register_equipment(
        self,
        *,
        idempotency_key: str,
        batch_id: str,
        equipment_id: str,
        kind: str,
        label: str = "",
        occurred_at: datetime | str,
    ) -> dict:
        def _run() -> dict:
            self._batch(batch_id)
            if equipment_id in self._equipment:
                raise InvariantViolation(f"设备已登记：{equipment_id}")
            _ts(occurred_at)
            self._equipment[equipment_id] = Equipment(equipment_id, batch_id, kind, label)
            return {"equipment_id": equipment_id, "kind": kind}

        return self._once(idempotency_key, _run)

    def add_consumable_stock(
        self,
        *,
        idempotency_key: str,
        batch_id: str,
        kind: str,
        quantity: int,
        occurred_at: datetime | str,
    ) -> dict:
        def _run() -> dict:
            self._batch(batch_id)
            if quantity <= 0:
                raise InvariantViolation("入库数量必须为正数")
            _ts(occurred_at)
            key = (batch_id, kind)
            self._stock[key] = self._stock.get(key, 0) + quantity
            return {"batch_id": batch_id, "kind": kind, "available": self._stock[key]}

        return self._once(idempotency_key, _run)

    def register_shift(
        self,
        *,
        idempotency_key: str,
        batch_id: str,
        staff_id: str,
        role: str | Role,
        start: datetime | str,
        end: datetime | str,
        occurred_at: datetime | str,
    ) -> dict:
        def _run() -> dict:
            self._batch(batch_id)
            self._staff_member(staff_id)
            start_dt, end_dt = _ts(start), _ts(end)
            if not end_dt > start_dt:
                raise InvariantViolation("班次结束时间必须晚于开始时间")
            _ts(occurred_at)
            self._shifts.append(Shift(batch_id, staff_id, Role(role), start_dt, end_dt))
            return {"staff_id": staff_id, "role": Role(role).value}

        return self._once(idempotency_key, _run)

    # ------------------------------------------------------------------
    # 筛查 → 复核 → 优先级 → 同意
    # ------------------------------------------------------------------

    def record_screening(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        source_kind: str | SourceKind,
        occurred_at: datetime | str,
        findings: dict,
        recorded_by: str | None = None,
        confidence: dict | None = None,
        model: str | None = None,
        model_version: str | None = None,
    ) -> dict:
        """登记筛查证据。AI 来源必须给出置信边界与模型信息，且只作辅助分流。"""

        def _run() -> dict:
            self._patient(patient_ref)
            kind = SourceKind(source_kind)
            conf = None
            if kind is SourceKind.AI_DEVICE:
                if not confidence:
                    raise InvariantViolation(
                        "AI 筛查必须给出置信边界 confidence={score, lower, upper}"
                    )
                conf = Confidence(
                    float(confidence["score"]),
                    float(confidence["lower"]),
                    float(confidence["upper"]),
                )
                if not (0.0 <= conf.lower <= conf.score <= conf.upper <= 1.0):
                    raise InvariantViolation("AI 置信边界必须满足 0 ≤ lower ≤ score ≤ upper ≤ 1")
                if not model or not model_version:
                    raise InvariantViolation("AI 筛查必须登记模型名称与版本")
            self._evidence_seq += 1
            evidence_id = f"ev-{self._evidence_seq:04d}"
            evidence = ScreeningEvidence(
                evidence_id=evidence_id,
                patient_ref=patient_ref,
                source_kind=kind,
                occurred_at=_ts(occurred_at),
                findings=deepcopy(findings),
                recorded_by=recorded_by,
                confidence=conf,
                model=model,
                model_version=model_version,
                advisory_only=kind is SourceKind.AI_DEVICE,
            )
            self._evidence[evidence_id] = evidence
            event = self._emit(
                "SCREENING_RECEIVED",
                "screening_evidence",
                evidence_id,
                occurred_at,
                summary=f"登记{_SOURCE_LABELS[kind]}筛查（{patient_ref}）",
                payload=self._evidence_payload(evidence),
            )
            return {
                "evidence_id": evidence_id,
                "event_id": event["event_id"],
                "advisory_only": evidence.advisory_only,
            }

        return self._once(idempotency_key, _run)

    def record_clinical_review(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        reviewer_id: str,
        occurred_at: datetime | str,
        indication: bool,
        rationale: str,
        procedure: str | None = None,
        resource_needs: dict | None = None,
        based_on: Iterable[str] = (),
        exam_note: str | None = None,
    ) -> dict:
        """医生复核并确认手术适应证。AI 结果只能辅助分流，不能单独决定手术。"""

        def _run() -> dict:
            self._patient(patient_ref)
            reviewer = self._staff.get(reviewer_id)
            if reviewer is None or Role.OPHTHALMOLOGIST not in reviewer.roles:
                raise RoleViolation("手术适应证只能由登记的眼科医生复核确认")
            evidence_ids = list(based_on)
            if not evidence_ids:
                raise InvariantViolation("复核必须引用至少一条筛查证据")
            evidence = []
            for evidence_id in evidence_ids:
                item = self._evidence.get(evidence_id)
                if item is None or item.patient_ref != patient_ref:
                    raise NotFound(f"筛查证据不存在或不属于该患者：{evidence_id}")
                evidence.append(item)
            has_clinical_basis = any(
                e.source_kind is SourceKind.HOSPITAL_EXAM for e in evidence
            ) or bool(exam_note)
            if indication and not has_clinical_basis:
                raise InvariantViolation(
                    "AI 结果与家属转介仅用于辅助分流，"
                    "确认手术适应证必须基于医生现场检查或医院检查"
                )
            if indication and not procedure:
                raise InvariantViolation("确认适应证时必须登记拟行术式")
            if not rationale or not rationale.strip():
                raise InvariantViolation("必须记录复核意见")
            uses_ai = any(e.source_kind is SourceKind.AI_DEVICE for e in evidence)
            review = ClinicalReview(
                patient_ref=patient_ref,
                reviewer_id=reviewer_id,
                occurred_at=_ts(occurred_at),
                indication=bool(indication),
                rationale=rationale.strip(),
                procedure=procedure,
                resource_needs=deepcopy(resource_needs) if resource_needs else {},
                based_on=evidence_ids,
                exam_note=exam_note,
                uses_ai_advisory=uses_ai,
            )
            self._reviews[patient_ref] = review
            event = self._emit(
                "CLINICAL_REVIEWED",
                "mission_patient",
                patient_ref,
                occurred_at,
                summary=f"医生复核完成，适应证{'成立' if indication else '不成立'}（{patient_ref}）",
                payload={
                    "reviewer_id": reviewer_id,
                    "indication": bool(indication),
                    "procedure": procedure,
                    "rationale": review.rationale,
                    "resource_needs": deepcopy(review.resource_needs),
                    "based_on": evidence_ids,
                    "exam_note": exam_note,
                    "uses_ai_advisory": uses_ai,
                },
            )
            return {
                "patient_ref": patient_ref,
                "indication": bool(indication),
                "event_id": event["event_id"],
            }

        return self._once(idempotency_key, _run)

    def set_priority(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        kind: str | PriorityKind,
        rationale: str,
        confirmed_by: str,
        occurred_at: datetime | str,
    ) -> dict:
        """记录优先级类别、依据与确认人。非常规类别留下 EXCEPTION_APPROVED 事件。"""

        def _run() -> dict:
            patient = self._patient(patient_ref)
            if patient_ref not in self._reviews:
                raise InvariantViolation("记录优先级前必须先完成医生复核")
            priority_kind = PriorityKind(kind)
            if not rationale or not rationale.strip():
                raise InvariantViolation("必须记录优先级依据")
            confirmer = self._staff.get(confirmed_by)
            allowed = PRIORITY_CONFIRM_ROLES[priority_kind]
            if confirmer is None or not (set(confirmer.roles) & allowed):
                roles = "、".join(sorted(r.value for r in allowed))
                raise RoleViolation(f"{priority_kind.value} 只能由 {roles} 确认")
            decision = PriorityDecision(
                patient_ref, priority_kind, rationale.strip(), confirmed_by, _ts(occurred_at)
            )
            self._priorities[patient_ref] = decision
            event_id = None
            if priority_kind is not PriorityKind.ROUTINE:
                event = self._emit(
                    "EXCEPTION_APPROVED",
                    "mission_patient",
                    patient_ref,
                    occurred_at,
                    summary=f"{_KIND_LABELS[priority_kind]}例外已批准（{patient_ref}）",
                    payload={
                        "kind": priority_kind.value,
                        "rationale": decision.rationale,
                        "confirmed_by": confirmed_by,
                        "resource_snapshot": self._resource_snapshot(patient.batch_id),
                    },
                )
                event_id = event["event_id"]
            return {"patient_ref": patient_ref, "kind": priority_kind.value, "event_id": event_id}

        return self._once(idempotency_key, _run)

    def record_consent(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        scope: str | ConsentScope,
        occurred_at: datetime | str,
        note: str | None = None,
    ) -> dict:
        def _run() -> dict:
            self._patient(patient_ref)
            consent_scope = ConsentScope(scope)
            self._consents[(patient_ref, consent_scope)] = ConsentRecord(
                patient_ref, consent_scope, True, _ts(occurred_at), note
            )
            return {"patient_ref": patient_ref, "scope": consent_scope.value, "granted": True}

        return self._once(idempotency_key, _run)

    def withdraw_consent(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        scope: str | ConsentScope,
        occurred_at: datetime | str,
        note: str | None = None,
    ) -> dict:
        """撤回同意。非诊疗用途撤回只影响二次利用；诊疗同意撤回会取消在排台次、
        阻断后续治疗，但已经形成的安全记录全部保留。"""

        def _run() -> dict:
            self._patient(patient_ref)
            consent_scope = ConsentScope(scope)
            self._consents[(patient_ref, consent_scope)] = ConsentRecord(
                patient_ref, consent_scope, False, _ts(occurred_at), note
            )
            cancelled = []
            if consent_scope is ConsentScope.TREATMENT:
                for slot in self._slots_of(patient_ref):
                    if slot.status is SlotStatus.SCHEDULED:
                        self._release_slot_resources(slot)
                        slot.status = SlotStatus.CANCELLED
                        slot.cancel_reason = "诊疗知情同意撤回"
                        cancelled.append(slot.slot_id)
            return {
                "patient_ref": patient_ref,
                "scope": consent_scope.value,
                "granted": False,
                "cancelled_slots": cancelled,
            }

        return self._once(idempotency_key, _run)

    # ------------------------------------------------------------------
    # 排期与治疗
    # ------------------------------------------------------------------

    def schedule_slot(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        start: datetime | str,
        end: datetime | str,
        equipment_ids: Iterable[str],
        staff_ids: Iterable[str],
        consumables: dict | None = None,
        occurred_at: datetime | str,
    ) -> dict:
        """排期。设备、耗材、人员班次一次性校验并提交，保证并发占用一致。"""

        def _run() -> dict:
            patient = self._patient(patient_ref)
            review = self._reviews.get(patient_ref)
            if review is None or not review.indication:
                raise InvariantViolation("排期前必须由医生确认手术适应证")
            if patient_ref not in self._priorities:
                raise InvariantViolation("排期前必须记录优先级类别、依据与确认人")
            if not self._consent_active(patient_ref, ConsentScope.TREATMENT):
                raise ConsentError("缺少有效的诊疗知情同意，不能排期")
            existing = self._slots_of(patient_ref)
            if any(s.status is SlotStatus.SCHEDULED for s in existing):
                raise InvariantViolation("该患者已有在排台次，不能重复排期")
            if any(s.status is SlotStatus.COMPLETED for s in existing):
                raise InvariantViolation("该患者已完成治疗")
            start_dt, end_dt = _ts(start), _ts(end)
            if not end_dt > start_dt:
                raise InvariantViolation("排期结束时间必须晚于开始时间")
            batch = self._batch(patient.batch_id)
            if start_dt.date() not in batch.surgery_days:
                raise InvariantViolation("排期日期不在任务批次的手术日内")
            wanted_equipment = tuple(equipment_ids)
            wanted_staff = tuple(staff_ids)
            wanted_consumables = dict(consumables or {})
            _ts(occurred_at)

            # 先收集全部冲突，再统一提交：任何失败都不留下半成品占用。
            problems: list[str] = []
            if not wanted_equipment:
                problems.append("手术台次必须登记占用设备")
            for equipment_id in wanted_equipment:
                equipment = self._equipment.get(equipment_id)
                if equipment is None or equipment.batch_id != batch.batch_id:
                    problems.append(f"设备不存在或不属于本批次：{equipment_id}")
                    continue
                for slot in self._active_slots():
                    if equipment_id in slot.equipment_ids and _overlap(
                        start_dt, end_dt, slot.start, slot.end
                    ):
                        problems.append(
                            f"设备 {equipment_id} 在该时段已被台次 {slot.slot_id} 占用"
                        )
            if not wanted_staff:
                problems.append("手术台次必须登记参与人员")
            surgeon_present = False
            for staff_id in wanted_staff:
                member = self._staff.get(staff_id)
                if member is None:
                    problems.append(f"人员未登记：{staff_id}")
                    continue
                if Role.OPHTHALMOLOGIST in member.roles:
                    surgeon_present = True
                on_shift = any(
                    sh.batch_id == batch.batch_id
                    and sh.staff_id == staff_id
                    and sh.start <= start_dt
                    and end_dt <= sh.end
                    for sh in self._shifts
                )
                if not on_shift:
                    problems.append(f"人员 {staff_id} 在该时段没有班次")
                for slot in self._active_slots():
                    if staff_id in slot.staff_ids and _overlap(
                        start_dt, end_dt, slot.start, slot.end
                    ):
                        problems.append(
                            f"人员 {staff_id} 在该时段已被台次 {slot.slot_id} 占用"
                        )
            if wanted_staff and not surgeon_present:
                problems.append("手术台次至少需要一名眼科医生")
            for kind, qty in wanted_consumables.items():
                if qty <= 0:
                    problems.append(f"耗材 {kind} 的预留数量必须为正数")
                    continue
                available = self._stock.get((batch.batch_id, kind), 0)
                if available < qty:
                    problems.append(f"耗材 {kind} 库存不足：需要 {qty}，剩余 {available}")
            if problems:
                raise ResourceConflict("；".join(problems))

            self._slot_seq += 1
            slot_id = f"slot-{batch.batch_id}-{self._slot_seq:03d}"
            for kind, qty in wanted_consumables.items():
                self._stock[(batch.batch_id, kind)] -= qty
            slot = Slot(
                slot_id=slot_id,
                batch_id=batch.batch_id,
                patient_ref=patient_ref,
                start=start_dt,
                end=end_dt,
                equipment_ids=wanted_equipment,
                staff_ids=wanted_staff,
                consumables=wanted_consumables,
            )
            self._slots[slot_id] = slot
            return {
                "slot_id": slot_id,
                "patient_ref": patient_ref,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "reserved_consumables": deepcopy(wanted_consumables),
            }

        return self._once(idempotency_key, _run)

    def cancel_slot(
        self,
        *,
        idempotency_key: str,
        slot_id: str,
        reason: str,
        occurred_at: datetime | str,
    ) -> dict:
        def _run() -> dict:
            slot = self._slot(slot_id)
            if slot.status is not SlotStatus.SCHEDULED:
                raise InvariantViolation(f"台次状态为 {slot.status.value}，不能取消")
            if not reason or not reason.strip():
                raise InvariantViolation("取消台次必须记录原因")
            _ts(occurred_at)
            self._release_slot_resources(slot)
            slot.status = SlotStatus.CANCELLED
            slot.cancel_reason = reason.strip()
            return {"slot_id": slot_id, "released": deepcopy(slot.consumables)}

        return self._once(idempotency_key, _run)

    def complete_treatment(
        self,
        *,
        idempotency_key: str,
        slot_id: str,
        surgeon_id: str,
        occurred_at: datetime | str,
        outcome: str,
        consumables_used: dict | None = None,
        notes: str | None = None,
    ) -> dict:
        """登记实际治疗。耗材按实际消耗结算，差额留痕；发出 TREATMENT_COMPLETED。"""

        def _run() -> dict:
            slot = self._slot(slot_id)
            if not self._consent_active(slot.patient_ref, ConsentScope.TREATMENT):
                raise ConsentError("诊疗知情同意已撤回或缺失，不能继续治疗")
            if slot.status is not SlotStatus.SCHEDULED:
                raise InvariantViolation(f"台次状态为 {slot.status.value}，不能登记治疗")
            member = self._staff.get(surgeon_id)
            if (
                member is None
                or Role.OPHTHALMOLOGIST not in member.roles
                or surgeon_id not in slot.staff_ids
            ):
                raise RoleViolation("主刀必须是该台次已排班的眼科医生")
            if not outcome or not outcome.strip():
                raise InvariantViolation("必须记录治疗结果")
            occurred = _ts(occurred_at)
            used = dict(consumables_used) if consumables_used is not None else dict(slot.consumables)
            variance: dict[str, int] = {}
            for kind in set(slot.consumables) | set(used):
                reserved = slot.consumables.get(kind, 0)
                actual = used.get(kind, 0)
                key = (slot.batch_id, kind)
                delta = reserved - actual
                if delta >= 0:
                    self._stock[key] = self._stock.get(key, 0) + delta
                else:
                    shortage = -delta
                    available = self._stock.get(key, 0)
                    taken = min(available, shortage)
                    self._stock[key] = available - taken
                    if shortage > taken:
                        # 实际消耗超出账面库存：如实记录差额，不阻断安全记录。
                        variance[kind] = shortage - taken
            slot.status = SlotStatus.COMPLETED
            record = TreatmentRecord(
                slot_id=slot_id,
                patient_ref=slot.patient_ref,
                surgeon_id=surgeon_id,
                occurred_at=occurred,
                outcome=outcome.strip(),
                consumables_used=used,
                stock_variance=variance,
                notes=notes,
            )
            self._treatments[slot_id] = record
            event = self._emit(
                "TREATMENT_COMPLETED",
                "treatment_slot",
                slot_id,
                occurred_at,
                summary=f"手术完成（{slot.patient_ref} / {slot_id}）",
                payload={
                    "patient_ref": slot.patient_ref,
                    "slot_id": slot_id,
                    "surgeon_id": surgeon_id,
                    "outcome": record.outcome,
                    "consumables_used": deepcopy(used),
                    "stock_variance": dict(variance),
                    "notes": notes,
                },
            )
            return {
                "slot_id": slot_id,
                "patient_ref": slot.patient_ref,
                "event_id": event["event_id"],
                "stock_variance": dict(variance),
            }

        return self._once(idempotency_key, _run)

    # ------------------------------------------------------------------
    # 复查责任交接
    # ------------------------------------------------------------------

    def register_handoff(
        self,
        *,
        idempotency_key: str,
        patient_ref: str,
        local_provider: str,
        follow_up_at: datetime | str,
        escalation: dict,
        occurred_at: datetime | str,
    ) -> dict:
        """登记复查责任：当地接手医护、复查时间与异常升级路径。"""

        def _run() -> dict:
            self._patient(patient_ref)
            if patient_ref not in self._reviews:
                raise InvariantViolation("登记交接前必须先完成医生复核")
            if not local_provider or not local_provider.strip():
                raise InvariantViolation("必须明确当地接手医护")
            if not escalation or not escalation.get("target"):
                raise InvariantViolation("必须登记异常升级路径（至少包含 target）")
            self._handoff_seq += 1
            handoff_id = f"ho-{self._handoff_seq:04d}"
            handoff = Handoff(
                handoff_id=handoff_id,
                patient_ref=patient_ref,
                local_provider=local_provider.strip(),
                follow_up_at=_ts(follow_up_at),
                escalation=deepcopy(escalation),
                registered_at=_ts(occurred_at),
            )
            self._handoffs[handoff_id] = handoff
            return {"handoff_id": handoff_id, "patient_ref": patient_ref}

        return self._once(idempotency_key, _run)

    def accept_handoff(
        self,
        *,
        idempotency_key: str,
        handoff_id: str,
        accepted_by: str,
        occurred_at: datetime | str,
    ) -> dict:
        """当地医护确认接手，发出 HANDOFF_ACCEPTED。"""

        def _run() -> dict:
            handoff = self._handoffs.get(handoff_id)
            if handoff is None:
                raise NotFound(f"交接单不存在：{handoff_id}")
            if handoff.status is HandoffStatus.ACCEPTED:
                raise InvariantViolation("交接单已被接手")
            if not accepted_by or not accepted_by.strip():
                raise InvariantViolation("必须记录接手人")
            handoff.status = HandoffStatus.ACCEPTED
            handoff.accepted_by = accepted_by.strip()
            handoff.accepted_at = _ts(occurred_at)
            event = self._emit(
                "HANDOFF_ACCEPTED",
                "followup_handoff",
                handoff_id,
                occurred_at,
                summary=f"当地医护已接手复查（{handoff.patient_ref}）",
                payload={
                    "patient_ref": handoff.patient_ref,
                    "handoff_id": handoff_id,
                    "local_provider": handoff.local_provider,
                    "follow_up_at": handoff.follow_up_at.isoformat(),
                    "escalation": deepcopy(handoff.escalation),
                    "accepted_by": handoff.accepted_by,
                },
            )
            return {"handoff_id": handoff_id, "event_id": event["event_id"]}

        return self._once(idempotency_key, _run)

    # ------------------------------------------------------------------
    # 查询：队列、返程报告、最少资料投影、二次利用登记、例外追溯
    # ------------------------------------------------------------------

    def candidate_queue(self, batch_id: str) -> list[dict]:
        """待排期候选：适应证成立、诊疗同意有效、尚未排期，按优先级排序。"""
        self._batch(batch_id)
        queue = []
        for patient in self._patients.values():
            if patient.batch_id != batch_id:
                continue
            review = self._reviews.get(patient.patient_ref)
            if review is None or not review.indication:
                continue
            if not self._consent_active(patient.patient_ref, ConsentScope.TREATMENT):
                continue
            slots = self._slots_of(patient.patient_ref)
            if any(s.status in (SlotStatus.SCHEDULED, SlotStatus.COMPLETED) for s in slots):
                continue
            decision = self._priorities.get(patient.patient_ref)
            kind = decision.kind if decision else PriorityKind.ROUTINE
            queue.append(
                {
                    "patient_ref": patient.patient_ref,
                    "priority_kind": kind.value,
                    "priority_rationale": decision.rationale if decision else None,
                    "reviewed_at": review.occurred_at.isoformat(),
                }
            )
        queue.sort(
            key=lambda row: (self._priority_weights[PriorityKind(row["priority_kind"])], row["reviewed_at"])
        )
        return queue

    def departure_report(self, batch_id: str) -> dict:
        """返程前报告：每名患者由当地哪位医护接手、何时复查、异常如何升级。"""
        self._batch(batch_id)
        handoffs, gaps, pending, referrals = [], [], [], []
        for patient in self._patients.values():
            if patient.batch_id != batch_id:
                continue
            ref = patient.patient_ref
            slots = self._slots_of(ref)
            treated = any(s.status is SlotStatus.COMPLETED for s in slots)
            scheduled = any(s.status is SlotStatus.SCHEDULED for s in slots)
            handoff = self._latest_handoff(ref)
            if treated:
                if handoff is not None and handoff.status is HandoffStatus.ACCEPTED:
                    handoffs.append(
                        {
                            "patient_ref": ref,
                            "local_provider": handoff.local_provider,
                            "follow_up_at": handoff.follow_up_at.isoformat(),
                            "escalation": deepcopy(handoff.escalation),
                            "accepted_by": handoff.accepted_by,
                        }
                    )
                else:
                    gaps.append(
                        {
                            "patient_ref": ref,
                            "missing": "HANDOFF_ACCEPTED" if handoff else "HANDOFF_REGISTERED",
                        }
                    )
            elif scheduled:
                pending.append({"patient_ref": ref, "reason": "仍有在排台次，返程前必须了结"})
            else:
                review = self._reviews.get(ref)
                if review is not None:
                    referrals.append(
                        {
                            "patient_ref": ref,
                            "indication": review.indication,
                            "reason": review.rationale,
                            "handoff_status": handoff.status.value if handoff else None,
                        }
                    )
        return {
            "batch_id": batch_id,
            "ready": not gaps and not pending,
            "handoffs": handoffs,
            "gaps": gaps,
            "scheduled_pending": pending,
            "untreated_referrals": referrals,
        }

    def minimal_treatment_projection(self, patient_ref: str) -> dict:
        """跨国团队交换用的最少资料投影：只含治疗所需字段，不含身份资料。"""
        patient = self._patient(patient_ref)
        review = self._reviews.get(patient_ref)
        priority = self._priorities.get(patient_ref)
        treatment = self._latest_treatment(patient_ref)
        handoff = self._latest_handoff(patient_ref)
        projection = {
            "patient_ref": patient.patient_ref,
            "local_identifier": patient.local_identifier,
            "age_band": self._age_band(patient),
            "sex": patient.clinical.get("sex"),
            "eye": patient.clinical.get("eye"),
            "diagnosis": patient.clinical.get("diagnosis"),
            "allergies": list(patient.clinical.get("allergies", [])),
            "indication": review.indication if review else None,
            "procedure": review.procedure if review else None,
            "resource_needs": deepcopy(review.resource_needs) if review else {},
            "priority_kind": priority.kind.value if priority else None,
            "consent_treatment": self._consent_active(patient_ref, ConsentScope.TREATMENT),
            "screenings": [
                self._evidence_payload(e)
                for e in self._evidence.values()
                if e.patient_ref == patient_ref
            ],
            "treatment": (
                {
                    "slot_id": treatment.slot_id,
                    "surgeon_id": treatment.surgeon_id,
                    "occurred_at": treatment.occurred_at.isoformat(),
                    "outcome": treatment.outcome,
                }
                if treatment
                else None
            ),
            "follow_up": (
                {
                    "local_provider": handoff.local_provider,
                    "follow_up_at": handoff.follow_up_at.isoformat(),
                    "escalation": deepcopy(handoff.escalation),
                    "status": handoff.status.value,
                }
                if handoff
                else None
            ),
        }
        return projection

    def treatment_handover_package(self, batch_id: str) -> list[dict]:
        """返程交接包：本批次已治疗患者的最少资料投影。"""
        self._batch(batch_id)
        return [
            self.minimal_treatment_projection(p.patient_ref)
            for p in self._patients.values()
            if p.batch_id == batch_id and self._latest_treatment(p.patient_ref) is not None
        ]

    def secondary_use_registry(self, purpose: str | ConsentScope) -> list[dict]:
        """二次利用（研究/宣传）登记：只含已授权且未撤回的患者，去标识化。"""
        scope = ConsentScope(purpose)
        if scope is ConsentScope.TREATMENT:
            raise InvariantViolation("诊疗安全记录不属于二次利用登记范围")
        rows = []
        for patient in self._patients.values():
            if not self._consent_active(patient.patient_ref, scope):
                continue
            review = self._reviews.get(patient.patient_ref)
            treatment = self._latest_treatment(patient.patient_ref)
            rows.append(
                {
                    "patient_ref": patient.patient_ref,
                    "age_band": self._age_band(patient),
                    "eye": patient.clinical.get("eye"),
                    "diagnosis": patient.clinical.get("diagnosis"),
                    "procedure": review.procedure if review else None,
                    "outcome": treatment.outcome if treatment else None,
                }
            )
        return rows

    def trace_exception(self, event_id: str) -> dict:
        """从一条 EXCEPTION_APPROVED 回看：当时资源、医学意见与后续结果。"""
        event = self._events.get(event_id)
        if event is None or event["event_type"] != "EXCEPTION_APPROVED":
            raise NotFound(f"例外决定事件不存在：{event_id}")
        patient_ref = event["aggregate_id"]
        review = self._reviews.get(patient_ref)
        treatment = self._latest_treatment(patient_ref)
        handoff = self._latest_handoff(patient_ref)
        return {
            "exception": deepcopy(event),
            "resource_snapshot": deepcopy(event["payload"]["resource_snapshot"]),
            "clinical_review": (
                {
                    "reviewer_id": review.reviewer_id,
                    "occurred_at": review.occurred_at.isoformat(),
                    "indication": review.indication,
                    "procedure": review.procedure,
                    "rationale": review.rationale,
                    "exam_note": review.exam_note,
                    "uses_ai_advisory": review.uses_ai_advisory,
                    "evidence": [
                        self._evidence_payload(self._evidence[eid])
                        for eid in review.based_on
                        if eid in self._evidence
                    ],
                }
                if review
                else None
            ),
            "treatment": (
                {
                    "slot_id": treatment.slot_id,
                    "surgeon_id": treatment.surgeon_id,
                    "occurred_at": treatment.occurred_at.isoformat(),
                    "outcome": treatment.outcome,
                    "consumables_used": deepcopy(treatment.consumables_used),
                    "stock_variance": dict(treatment.stock_variance),
                }
                if treatment
                else None
            ),
            "followup": (
                {
                    "handoff_id": handoff.handoff_id,
                    "status": handoff.status.value,
                    "local_provider": handoff.local_provider,
                    "follow_up_at": handoff.follow_up_at.isoformat(),
                    "escalation": deepcopy(handoff.escalation),
                    "accepted_by": handoff.accepted_by,
                }
                if handoff
                else None
            ),
        }

    def patient_timeline(self, patient_ref: str) -> list[dict]:
        """患者全链路事件时间线（跨聚合），供下一支医疗队复盘。"""
        self._patient(patient_ref)
        aggregates = [("mission_patient", patient_ref)]
        aggregates += [
            ("screening_evidence", e.evidence_id)
            for e in self._evidence.values()
            if e.patient_ref == patient_ref
        ]
        aggregates += [
            ("treatment_slot", s.slot_id) for s in self._slots_of(patient_ref)
        ]
        aggregates += [
            ("followup_handoff", h.handoff_id) for h in self._handoffs_of(patient_ref)
        ]
        events = [
            event
            for aggregate_type, aggregate_id in aggregates
            for event in self._events.events_for(aggregate_type, aggregate_id)
        ]
        events.sort(key=lambda e: (e["occurred_at"], e["recorded_at"], e["event_id"]))
        return deepcopy(events)

    def stock_level(self, batch_id: str, kind: str) -> int:
        return self._stock.get((batch_id, kind), 0)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _batch(self, batch_id: str) -> Batch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFound(f"任务批次不存在：{batch_id}")
        return batch

    def _patient(self, patient_ref: str) -> Patient:
        patient = self._patients.get(patient_ref)
        if patient is None:
            raise NotFound(f"患者不存在：{patient_ref}")
        return patient

    def _staff_member(self, staff_id: str) -> StaffMember:
        member = self._staff.get(staff_id)
        if member is None:
            raise NotFound(f"人员未登记：{staff_id}")
        return member

    def _slot(self, slot_id: str) -> Slot:
        slot = self._slots.get(slot_id)
        if slot is None:
            raise NotFound(f"台次不存在：{slot_id}")
        return slot

    def _slots_of(self, patient_ref: str) -> list[Slot]:
        return [s for s in self._slots.values() if s.patient_ref == patient_ref]

    def _handoffs_of(self, patient_ref: str) -> list[Handoff]:
        return [h for h in self._handoffs.values() if h.patient_ref == patient_ref]

    def _active_slots(self) -> list[Slot]:
        return [s for s in self._slots.values() if s.status is SlotStatus.SCHEDULED]

    def _consent_active(self, patient_ref: str, scope: ConsentScope) -> bool:
        record = self._consents.get((patient_ref, scope))
        return record is not None and record.granted

    def _release_slot_resources(self, slot: Slot) -> None:
        for kind, qty in slot.consumables.items():
            key = (slot.batch_id, kind)
            self._stock[key] = self._stock.get(key, 0) + qty

    def _latest_handoff(self, patient_ref: str) -> Handoff | None:
        handoffs = self._handoffs_of(patient_ref)
        if not handoffs:
            return None
        return max(handoffs, key=lambda h: h.registered_at)

    def _latest_treatment(self, patient_ref: str) -> TreatmentRecord | None:
        records = [t for t in self._treatments.values() if t.patient_ref == patient_ref]
        if not records:
            return None
        return max(records, key=lambda t: t.occurred_at)

    def _age_band(self, patient: Patient) -> str | None:
        birth_year = patient.clinical.get("birth_year")
        if not birth_year:
            return None
        age = self._clock().year - int(birth_year)
        if age < 0:
            return None
        lower = age // 10 * 10
        return f"{lower}-{lower + 9}"

    def _resource_snapshot(self, batch_id: str) -> dict:
        """例外决定当时的资源快照：手术日占用、设备、耗材库存、在岗人员。"""
        batch = self._batch(batch_id)
        per_day = {day.isoformat(): 0 for day in sorted(batch.surgery_days)}
        for slot in self._active_slots():
            if slot.batch_id != batch_id:
                continue
            key = slot.start.date().isoformat()
            per_day[key] = per_day.get(key, 0) + 1
        return {
            "surgery_day_load": per_day,
            "equipment": sorted(
                e.equipment_id for e in self._equipment.values() if e.batch_id == batch_id
            ),
            "consumables": {
                kind: qty
                for (bid, kind), qty in sorted(self._stock.items())
                if bid == batch_id
            },
            "staff_on_shift": sorted(
                {sh.staff_id for sh in self._shifts if sh.batch_id == batch_id}
            ),
        }

    @staticmethod
    def _evidence_payload(evidence: ScreeningEvidence) -> dict:
        return {
            "evidence_id": evidence.evidence_id,
            "patient_ref": evidence.patient_ref,
            "source_kind": evidence.source_kind.value,
            "occurred_at": evidence.occurred_at.isoformat(),
            "findings": deepcopy(evidence.findings),
            "recorded_by": evidence.recorded_by,
            "advisory_only": evidence.advisory_only,
            "confidence": (
                {
                    "score": evidence.confidence.score,
                    "lower": evidence.confidence.lower,
                    "upper": evidence.confidence.upper,
                }
                if evidence.confidence
                else None
            ),
            "model": evidence.model,
            "model_version": evidence.model_version,
        }
