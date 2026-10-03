"""诊疗接力后端行为测试：覆盖责任链的每一项要求。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.relay import (
    ConsentError,
    ContractViolation,
    InvariantViolation,
    RelayService,
    ResourceConflict,
    RoleViolation,
)
from src.validator import validate_event

TZ = timezone(timedelta(hours=12))  # 斐济 UTC+12
BATCH = "fiji-2026-09"
DAYS = ["2026-09-21", "2026-09-22", "2026-09-23"]
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=TZ)
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
)


def at(day: str, hhmm: str) -> str:
    return f"{day}T{hhmm}:00+12:00"


def build_mission() -> RelayService:
    """一支带有限手术日、一台超声乳化设备、有限耗材与班次的医疗队。"""
    svc = RelayService(clock=lambda: NOW)
    svc.register_batch(
        idempotency_key="batch-1",
        batch_id=BATCH,
        title="斐济光明行",
        location="苏瓦",
        surgery_days=DAYS,
        occurred_at="2026-09-15T09:00:00+12:00",
    )
    staff = [
        ("dr-chen", "陈医生", ["OPHTHALMOLOGIST", "MEDICAL_LEAD"]),
        ("co-wang", "王协调", ["SCHEDULING_COORDINATOR"]),
        ("acc-lin", "林无障碍", ["ACCESSIBILITY_COORDINATOR"]),
        ("gov-zhao", "赵治理", ["PROGRAM_GOVERNANCE"]),
        ("nurse-a", "护士甲", ["NURSE"]),
        ("nurse-b", "护士乙", ["NURSE"]),
        ("local-dr", "当地医生", ["LOCAL_PROVIDER"]),
    ]
    for staff_id, name, roles in staff:
        svc.register_staff(
            idempotency_key=f"staff-{staff_id}",
            staff_id=staff_id,
            name=name,
            roles=roles,
            team="LOCAL" if staff_id == "local-dr" else "CN-TEAM",
        )
    svc.register_equipment(
        idempotency_key="eq-phaco",
        batch_id=BATCH,
        equipment_id="phaco-1",
        kind="PHACO_MACHINE",
        label="当地超声乳化设备",
        occurred_at="2026-09-15T09:30:00+12:00",
    )
    svc.add_consumable_stock(
        idempotency_key="stock-iol",
        batch_id=BATCH,
        kind="IOL",
        quantity=10,
        occurred_at="2026-09-15T09:40:00+12:00",
    )
    svc.add_consumable_stock(
        idempotency_key="stock-visco",
        batch_id=BATCH,
        kind="VISCO",
        quantity=10,
        occurred_at="2026-09-15T09:41:00+12:00",
    )
    for day in DAYS:
        for staff_id, role in (("dr-chen", "OPHTHALMOLOGIST"), ("nurse-a", "NURSE"), ("nurse-b", "NURSE")):
            svc.register_shift(
                idempotency_key=f"shift-{staff_id}-{day}",
                batch_id=BATCH,
                staff_id=staff_id,
                role=role,
                start=at(day, "08:00"),
                end=at(day, "16:00"),
                occurred_at="2026-09-15T10:00:00+12:00",
            )
    return svc


def add_patient(svc: RelayService, ref: str, local_id: str) -> None:
    svc.register_patient(
        idempotency_key=f"reg-{ref}",
        batch_id=BATCH,
        patient_ref=ref,
        local_identifier=local_id,
        occurred_at=at(DAYS[0], "08:10"),
        identity={"name": f"患者{ref}", "contact": "+679-0000", "family_narrative": "家属代述病史"},
        clinical={
            "birth_year": 1961,
            "sex": "F",
            "eye": "LEFT",
            "diagnosis": "年龄相关性白内障",
            "allergies": [],
        },
    )


def screen_with_ai_and_hospital(svc: RelayService, ref: str) -> tuple[str, str]:
    ai = svc.record_screening(
        idempotency_key=f"scr-ai-{ref}",
        patient_ref=ref,
        source_kind="AI_DEVICE",
        occurred_at=at(DAYS[0], "08:20"),
        findings={" cataract_grade": "IV", "eye": "LEFT"},
        recorded_by="village-device-07",
        confidence={"score": 0.86, "lower": 0.71, "upper": 0.94},
        model="eye-screen",
        model_version="2.3.1",
    )
    hospital = svc.record_screening(
        idempotency_key=f"scr-hosp-{ref}",
        patient_ref=ref,
        source_kind="HOSPITAL_EXAM",
        occurred_at=at(DAYS[0], "08:40"),
        findings={"bcva": "0.1", "iop": "15mmHg"},
        recorded_by="cwmh-oph-02",
    )
    return ai["evidence_id"], hospital["evidence_id"]


def review_ok(svc: RelayService, ref: str, evidence_ids) -> None:
    svc.record_clinical_review(
        idempotency_key=f"rev-{ref}",
        patient_ref=ref,
        reviewer_id="dr-chen",
        occurred_at=at(DAYS[0], "09:00"),
        indication=True,
        procedure="PHACO_IOL",
        rationale="晶状体混浊明显，视力 0.1，符合手术适应证",
        resource_needs={"equipment": ["PHACO_MACHINE"], "consumables": {"IOL": 1, "VISCO": 1}},
        based_on=evidence_ids,
        exam_note="裂隙灯检查：晶状体核硬化 IV 级",
    )


def prioritize_routine(svc: RelayService, ref: str) -> None:
    svc.set_priority(
        idempotency_key=f"pri-{ref}",
        patient_ref=ref,
        kind="ROUTINE",
        rationale="按登记顺序排期",
        confirmed_by="co-wang",
        occurred_at=at(DAYS[0], "09:10"),
    )


def consent_treatment(svc: RelayService, ref: str) -> None:
    svc.record_consent(
        idempotency_key=f"con-{ref}",
        patient_ref=ref,
        scope="TREATMENT",
        occurred_at=at(DAYS[0], "09:20"),
        note="患者本人签署，翻译在场",
    )


def schedule(svc: RelayService, ref: str, day: str, start: str, end: str, surgeon="dr-chen", nurse="nurse-a") -> str:
    result = svc.schedule_slot(
        idempotency_key=f"slot-{ref}",
        patient_ref=ref,
        start=at(day, start),
        end=at(day, end),
        equipment_ids=["phaco-1"],
        staff_ids=[surgeon, nurse],
        consumables={"IOL": 1, "VISCO": 1},
        occurred_at=at(day, "07:30"),
    )
    return result["slot_id"]


def complete(svc: RelayService, ref: str, slot_id: str, day: str) -> None:
    svc.complete_treatment(
        idempotency_key=f"done-{ref}",
        slot_id=slot_id,
        surgeon_id="dr-chen",
        occurred_at=at(day, "11:00"),
        outcome="超声乳化+人工晶状体植入顺利，术中无并发症",
    )


def handoff(svc: RelayService, ref: str, day: str) -> None:
    handoff = svc.register_handoff(
        idempotency_key=f"ho-{ref}",
        patient_ref=ref,
        local_provider="local-dr",
        follow_up_at="2026-09-30T09:00:00+12:00",
        escalation={"channel": "电话", "target": "CWMH 眼科门诊", "within_hours": 24},
        occurred_at=at(day, "11:30"),
    )
    svc.accept_handoff(
        idempotency_key=f"ho-acc-{ref}",
        handoff_id=handoff["handoff_id"],
        accepted_by="local-dr",
        occurred_at=at(day, "11:40"),
    )


def treat_end_to_end(svc: RelayService, ref: str, day: str = DAYS[0], start="09:30", end="10:30") -> str:
    add_patient(svc, ref, f"CWMH-2026-{ref}")
    evidence_ids = screen_with_ai_and_hospital(svc, ref)
    review_ok(svc, ref, evidence_ids)
    prioritize_routine(svc, ref)
    consent_treatment(svc, ref)
    slot_id = schedule(svc, ref, day, start, end)
    complete(svc, ref, slot_id, day)
    handoff(svc, ref, day)
    return slot_id


class FullChainTest(unittest.TestCase):
    def test_full_chain_emits_contract_events_in_order(self) -> None:
        svc = build_mission()
        treat_end_to_end(svc, "p-001")

        events = svc.event_store.all()
        self.assertEqual(
            [e["event_type"] for e in events],
            [
                "SCREENING_RECEIVED",
                "SCREENING_RECEIVED",
                "CLINICAL_REVIEWED",
                "TREATMENT_COMPLETED",
                "HANDOFF_ACCEPTED",
            ],
        )
        for event in events:
            self.assertEqual(validate_event(event), [])
            self.assertIn(event["event_type"], SCHEMA["properties"]["event_type"]["enum"])
            self.assertIn(event["aggregate_type"], SCHEMA["properties"]["aggregate_type"]["enum"])
        # 每个聚合内版本从 1 开始严格递增
        versions = {}
        for event in events:
            key = (event["aggregate_type"], event["aggregate_id"])
            versions.setdefault(key, []).append(event["version"])
        for seq in versions.values():
            self.assertEqual(seq, list(range(1, len(seq) + 1)))
        # 患者时间线可串起完整链路，供下一支医疗队复盘
        timeline = svc.patient_timeline("p-001")
        self.assertEqual(len(timeline), 5)
        self.assertEqual(timeline[0]["event_type"], "SCREENING_RECEIVED")
        self.assertEqual(timeline[-1]["event_type"], "HANDOFF_ACCEPTED")


class ScreeningAndReviewTest(unittest.TestCase):
    def test_ai_screening_requires_confidence_bounds_and_model(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-002", "CWMH-2026-p-002")
        with self.assertRaises(InvariantViolation):
            svc.record_screening(
                idempotency_key="scr-no-conf",
                patient_ref="p-002",
                source_kind="AI_DEVICE",
                occurred_at=at(DAYS[0], "08:20"),
                findings={},
                model="eye-screen",
                model_version="2.3.1",
            )
        with self.assertRaises(InvariantViolation):
            svc.record_screening(
                idempotency_key="scr-bad-conf",
                patient_ref="p-002",
                source_kind="AI_DEVICE",
                occurred_at=at(DAYS[0], "08:20"),
                findings={},
                confidence={"score": 0.3, "lower": 0.5, "upper": 0.9},
                model="eye-screen",
                model_version="2.3.1",
            )

    def test_ai_result_is_advisory_and_cannot_solely_decide_surgery(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-003", "CWMH-2026-p-003")
        ai = svc.record_screening(
            idempotency_key="scr-ai-only",
            patient_ref="p-003",
            source_kind="AI_DEVICE",
            occurred_at=at(DAYS[0], "08:20"),
            findings={"cataract_grade": "IV"},
            confidence={"score": 0.9, "lower": 0.8, "upper": 0.97},
            model="eye-screen",
            model_version="2.3.1",
        )
        self.assertTrue(ai["advisory_only"])
        # 只有 AI 证据、没有医生检查或医院检查：不能确认适应证
        with self.assertRaises(InvariantViolation):
            svc.record_clinical_review(
                idempotency_key="rev-ai-only",
                patient_ref="p-003",
                reviewer_id="dr-chen",
                occurred_at=at(DAYS[0], "09:00"),
                indication=True,
                procedure="PHACO_IOL",
                rationale="仅凭 AI 结果",
                based_on=[ai["evidence_id"]],
            )
        # 补上医生现场检查记录后可以确认，且事件标注参考了 AI 辅助
        result = svc.record_clinical_review(
            idempotency_key="rev-with-exam",
            patient_ref="p-003",
            reviewer_id="dr-chen",
            occurred_at=at(DAYS[0], "09:05"),
            indication=True,
            procedure="PHACO_IOL",
            rationale="医生现场检查确认",
            based_on=[ai["evidence_id"]],
            exam_note="裂隙灯检查：晶状体核硬化 IV 级",
        )
        event = svc.event_store.get(result["event_id"])
        self.assertTrue(event["payload"]["uses_ai_advisory"])

    def test_family_referral_alone_cannot_support_indication(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-004", "CWMH-2026-p-004")
        referral = svc.record_screening(
            idempotency_key="scr-ref",
            patient_ref="p-004",
            source_kind="FAMILY_REFERRAL",
            occurred_at=at(DAYS[0], "08:20"),
            findings={"narrative": "家属反映视物模糊两年"},
        )
        with self.assertRaises(InvariantViolation):
            svc.record_clinical_review(
                idempotency_key="rev-ref-only",
                patient_ref="p-004",
                reviewer_id="dr-chen",
                occurred_at=at(DAYS[0], "09:00"),
                indication=True,
                procedure="PHACO_IOL",
                rationale="仅凭家属转介",
                based_on=[referral["evidence_id"]],
            )

    def test_review_requires_ophthalmologist(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-005", "CWMH-2026-p-005")
        _, hospital = screen_with_ai_and_hospital(svc, "p-005")
        with self.assertRaises(RoleViolation):
            svc.record_clinical_review(
                idempotency_key="rev-nurse",
                patient_ref="p-005",
                reviewer_id="nurse-a",
                occurred_at=at(DAYS[0], "09:00"),
                indication=True,
                procedure="PHACO_IOL",
                rationale="护士越权复核",
                based_on=[hospital],
            )


class PriorityRoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_mission()
        add_patient(self.svc, "p-010", "CWMH-2026-p-010")
        evidence = screen_with_ai_and_hospital(self.svc, "p-010")
        review_ok(self.svc, "p-010", evidence)

    def test_each_exception_kind_confirmed_by_its_own_role(self) -> None:
        cases = [
            ("ROUTINE", "co-wang", "gov-zhao"),
            ("MEDICAL_URGENCY", "dr-chen", "co-wang"),
            ("ACCESSIBILITY", "acc-lin", "co-wang"),
            ("HUMANITARIAN_EXCEPTION", "gov-zhao", "dr-chen"),
        ]
        for index, (kind, good, bad) in enumerate(cases):
            ref = f"p-01{index}"
            add_patient(self.svc, ref, f"CWMH-2026-{ref}")
            review_ok(self.svc, ref, screen_with_ai_and_hospital(self.svc, ref))
            with self.assertRaises(RoleViolation, msg=f"{kind} 不应由 {bad} 确认"):
                self.svc.set_priority(
                    idempotency_key=f"pri-bad-{kind}",
                    patient_ref=ref,
                    kind=kind,
                    rationale="越权确认",
                    confirmed_by=bad,
                    occurred_at=at(DAYS[0], "09:10"),
                )
            self.svc.set_priority(
                idempotency_key=f"pri-ok-{kind}",
                patient_ref=ref,
                kind=kind,
                rationale=f"{kind} 依据记录",
                confirmed_by=good,
                occurred_at=at(DAYS[0], "09:11"),
            )

    def test_non_routine_kinds_emit_exception_event_with_snapshot(self) -> None:
        result = self.svc.set_priority(
            idempotency_key="pri-urgent",
            patient_ref="p-010",
            kind="MEDICAL_URGENCY",
            rationale="晶状体源性青光眼风险，需尽快手术",
            confirmed_by="dr-chen",
            occurred_at=at(DAYS[0], "09:10"),
        )
        event = self.svc.event_store.get(result["event_id"])
        self.assertEqual(event["event_type"], "EXCEPTION_APPROVED")
        self.assertEqual(event["payload"]["kind"], "MEDICAL_URGENCY")
        self.assertEqual(event["payload"]["confirmed_by"], "dr-chen")
        self.assertIn("resource_snapshot", event["payload"])
        # 常规排期只留决定记录，不产生例外事件
        add_patient(self.svc, "p-011", "CWMH-2026-p-011")
        review_ok(self.svc, "p-011", screen_with_ai_and_hospital(self.svc, "p-011"))
        routine = self.svc.set_priority(
            idempotency_key="pri-routine",
            patient_ref="p-011",
            kind="ROUTINE",
            rationale="按登记顺序",
            confirmed_by="co-wang",
            occurred_at=at(DAYS[0], "09:12"),
        )
        self.assertIsNone(routine["event_id"])

    def test_priority_requires_rationale_and_prior_review(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.svc.set_priority(
                idempotency_key="pri-empty",
                patient_ref="p-010",
                kind="ROUTINE",
                rationale="  ",
                confirmed_by="co-wang",
                occurred_at=at(DAYS[0], "09:10"),
            )
        add_patient(self.svc, "p-012", "CWMH-2026-p-012")
        with self.assertRaises(InvariantViolation):
            self.svc.set_priority(
                idempotency_key="pri-no-review",
                patient_ref="p-012",
                kind="ROUTINE",
                rationale="未复核先定优先级",
                confirmed_by="co-wang",
                occurred_at=at(DAYS[0], "09:10"),
            )


class ResourceConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_mission()
        for ref in ("p-020", "p-021"):
            add_patient(self.svc, ref, f"CWMH-2026-{ref}")
            review_ok(self.svc, ref, screen_with_ai_and_hospital(self.svc, ref))
            prioritize_routine(self.svc, ref)
            consent_treatment(self.svc, ref)

    def test_equipment_double_booking_rejected(self) -> None:
        schedule(self.svc, "p-020", DAYS[0], "09:30", "10:30")
        with self.assertRaises(ResourceConflict):
            self.svc.schedule_slot(
                idempotency_key="slot-clash",
                patient_ref="p-021",
                start=at(DAYS[0], "10:00"),
                end=at(DAYS[0], "11:00"),
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-b"],
                consumables={"IOL": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )

    def test_staff_double_booking_rejected(self) -> None:
        schedule(self.svc, "p-020", DAYS[0], "09:30", "10:30")
        with self.assertRaises(ResourceConflict):
            self.svc.schedule_slot(
                idempotency_key="slot-staff-clash",
                patient_ref="p-021",
                start=at(DAYS[0], "10:00"),
                end=at(DAYS[0], "11:00"),
                equipment_ids=[],
                staff_ids=["dr-chen", "nurse-b"],
                consumables={},
                occurred_at=at(DAYS[0], "07:35"),
            )

    def test_shift_coverage_required(self) -> None:
        with self.assertRaises(ResourceConflict):
            self.svc.schedule_slot(
                idempotency_key="slot-night",
                patient_ref="p-020",
                start=at(DAYS[0], "18:00"),
                end=at(DAYS[0], "19:00"),
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-a"],
                consumables={"IOL": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )

    def test_consumable_shortage_fails_atomically(self) -> None:
        before_iol = self.svc.stock_level(BATCH, "IOL")
        before_visco = self.svc.stock_level(BATCH, "VISCO")
        with self.assertRaises(ResourceConflict):
            self.svc.schedule_slot(
                idempotency_key="slot-shortage",
                patient_ref="p-020",
                start=at(DAYS[0], "09:30"),
                end=at(DAYS[0], "10:30"),
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-a"],
                consumables={"IOL": 99, "VISCO": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )
        # 失败不落任何占用：库存不变、没有台次、设备仍可用
        self.assertEqual(self.svc.stock_level(BATCH, "IOL"), before_iol)
        self.assertEqual(self.svc.stock_level(BATCH, "VISCO"), before_visco)
        slot_id = schedule(self.svc, "p-020", DAYS[0], "09:30", "10:30")
        self.assertTrue(slot_id)

    def test_adjacent_slots_share_equipment_and_staff(self) -> None:
        schedule(self.svc, "p-020", DAYS[0], "09:30", "10:30")
        slot_id = schedule(self.svc, "p-021", DAYS[0], "10:30", "11:30")
        self.assertTrue(slot_id)
        self.assertEqual(self.svc.stock_level(BATCH, "IOL"), 8)

    def test_surgery_day_enforced(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.svc.schedule_slot(
                idempotency_key="slot-offday",
                patient_ref="p-020",
                start="2026-09-24T09:00:00+12:00",
                end="2026-09-24T10:00:00+12:00",
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-a"],
                consumables={"IOL": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )

    def test_schedule_requires_indication_priority_and_consent(self) -> None:
        add_patient(self.svc, "p-022", "CWMH-2026-p-022")
        review_ok(self.svc, "p-022", screen_with_ai_and_hospital(self.svc, "p-022"))
        with self.assertRaises(InvariantViolation):  # 未记录优先级
            self.svc.schedule_slot(
                idempotency_key="slot-no-pri",
                patient_ref="p-022",
                start=at(DAYS[0], "09:30"),
                end=at(DAYS[0], "10:30"),
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-a"],
                consumables={"IOL": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )
        prioritize_routine(self.svc, "p-022")
        with self.assertRaises(ConsentError):  # 未签诊疗同意
            self.svc.schedule_slot(
                idempotency_key="slot-no-consent",
                patient_ref="p-022",
                start=at(DAYS[0], "09:30"),
                end=at(DAYS[0], "10:30"),
                equipment_ids=["phaco-1"],
                staff_ids=["dr-chen", "nurse-a"],
                consumables={"IOL": 1},
                occurred_at=at(DAYS[0], "07:35"),
            )


class OfflineBackfillTest(unittest.TestCase):
    def test_backfill_keeps_original_time_and_is_idempotent(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-030", "CWMH-2026-p-030")
        # 断网期间的筛查，两天后补录：occurred_at 保留原发生时间
        result = svc.record_screening(
            idempotency_key="offline-scr-1",
            patient_ref="p-030",
            source_kind="HOSPITAL_EXAM",
            occurred_at="2026-09-21T08:40:00+12:00",
            findings={"bcva": "0.1"},
        )
        event = svc.event_store.get(result["event_id"])
        self.assertEqual(event["occurred_at"], "2026-09-21T08:40:00+12:00")
        self.assertEqual(event["recorded_at"], NOW.isoformat())
        # 网络恢复后客户端重试同一命令：不重复建单
        replay = svc.record_screening(
            idempotency_key="offline-scr-1",
            patient_ref="p-030",
            source_kind="HOSPITAL_EXAM",
            occurred_at="2026-09-21T08:40:00+12:00",
            findings={"bcva": "0.1"},
        )
        self.assertEqual(replay["evidence_id"], result["evidence_id"])
        self.assertEqual(len(svc.event_store.all()), 1)

    def test_stock_and_schedule_replay_have_no_side_effects(self) -> None:
        svc = build_mission()
        svc.add_consumable_stock(
            idempotency_key="offline-stock",
            batch_id=BATCH,
            kind="IOL",
            quantity=5,
            occurred_at="2026-09-22T20:00:00+12:00",
        )
        svc.add_consumable_stock(
            idempotency_key="offline-stock",
            batch_id=BATCH,
            kind="IOL",
            quantity=5,
            occurred_at="2026-09-22T20:00:00+12:00",
        )
        self.assertEqual(svc.stock_level(BATCH, "IOL"), 15)

        add_patient(svc, "p-031", "CWMH-2026-p-031")
        review_ok(svc, "p-031", screen_with_ai_and_hospital(svc, "p-031"))
        prioritize_routine(svc, "p-031")
        consent_treatment(svc, "p-031")
        first = svc.schedule_slot(
            idempotency_key="offline-slot",
            patient_ref="p-031",
            start=at(DAYS[1], "09:00"),
            end=at(DAYS[1], "10:00"),
            equipment_ids=["phaco-1"],
            staff_ids=["dr-chen", "nurse-a"],
            consumables={"IOL": 1},
            occurred_at="2026-09-21T18:00:00+12:00",
        )
        replay = svc.schedule_slot(
            idempotency_key="offline-slot",
            patient_ref="p-031",
            start=at(DAYS[1], "09:00"),
            end=at(DAYS[1], "10:00"),
            equipment_ids=["phaco-1"],
            staff_ids=["dr-chen", "nurse-a"],
            consumables={"IOL": 1},
            occurred_at="2026-09-21T18:00:00+12:00",
        )
        self.assertEqual(replay["slot_id"], first["slot_id"])
        self.assertEqual(svc.stock_level(BATCH, "IOL"), 14)  # 只扣一次

    def test_event_store_rejects_conflicting_event_id(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-032", "CWMH-2026-p-032")
        result = svc.record_screening(
            idempotency_key="scr-conflict",
            patient_ref="p-032",
            source_kind="HOSPITAL_EXAM",
            occurred_at=at(DAYS[0], "08:40"),
            findings={"bcva": "0.1"},
        )
        event = dict(svc.event_store.get(result["event_id"]))
        event["summary"] = "被篡改的内容"
        with self.assertRaises(ContractViolation):
            svc.event_store.append(event)


class PrivacyTest(unittest.TestCase):
    def test_cross_border_projection_is_minimal(self) -> None:
        svc = build_mission()
        treat_end_to_end(svc, "p-040")
        projection = svc.minimal_treatment_projection("p-040")
        # 治疗所需字段齐全
        self.assertEqual(projection["local_identifier"], "CWMH-2026-p-040")
        self.assertEqual(projection["diagnosis"], "年龄相关性白内障")
        self.assertEqual(projection["procedure"], "PHACO_IOL")
        self.assertEqual(projection["age_band"], "60-69")
        self.assertTrue(projection["follow_up"]["escalation"]["target"])
        # 身份资料不出现在投影里
        serialized = json.dumps(projection, ensure_ascii=False)
        for leaked in ("患者p-040", "+679-0000", "家属代述病史", "identity"):
            self.assertNotIn(leaked, serialized)

    def test_withdraw_non_treatment_use_keeps_safety_records(self) -> None:
        svc = build_mission()
        treat_end_to_end(svc, "p-041")
        svc.record_consent(
            idempotency_key="con-research",
            patient_ref="p-041",
            scope="RESEARCH",
            occurred_at=at(DAYS[0], "09:25"),
        )
        self.assertEqual(len(svc.secondary_use_registry("RESEARCH")), 1)
        svc.withdraw_consent(
            idempotency_key="wd-research",
            patient_ref="p-041",
            scope="RESEARCH",
            occurred_at=at(DAYS[1], "10:00"),
            note="患者撤回研究用途授权",
        )
        # 二次利用登记中消失
        self.assertEqual(svc.secondary_use_registry("RESEARCH"), [])
        # 安全记录与诊疗投影完整保留
        projection = svc.minimal_treatment_projection("p-041")
        self.assertEqual(projection["treatment"]["outcome"], "超声乳化+人工晶状体植入顺利，术中无并发症")
        event_types = [e["event_type"] for e in svc.patient_timeline("p-041")]
        self.assertIn("TREATMENT_COMPLETED", event_types)
        self.assertIn("HANDOFF_ACCEPTED", event_types)

    def test_withdraw_treatment_cancels_slot_but_keeps_records(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-042", "CWMH-2026-p-042")
        review_ok(svc, "p-042", screen_with_ai_and_hospital(svc, "p-042"))
        prioritize_routine(svc, "p-042")
        consent_treatment(svc, "p-042")
        slot_id = schedule(svc, "p-042", DAYS[0], "09:30", "10:30")
        stock_before = svc.stock_level(BATCH, "IOL")
        svc.withdraw_consent(
            idempotency_key="wd-treatment",
            patient_ref="p-042",
            scope="TREATMENT",
            occurred_at=at(DAYS[0], "09:50"),
        )
        # 在排台次被取消、耗材释放，且不能再登记治疗
        self.assertEqual(svc.stock_level(BATCH, "IOL"), stock_before + 1)
        with self.assertRaises(ConsentError):
            svc.complete_treatment(
                idempotency_key="done-after-wd",
                slot_id=slot_id,
                surgeon_id="dr-chen",
                occurred_at=at(DAYS[0], "10:30"),
                outcome="不应发生",
            )
        # 已形成的筛查与复核记录仍在
        event_types = [e["event_type"] for e in svc.patient_timeline("p-042")]
        self.assertIn("SCREENING_RECEIVED", event_types)
        self.assertIn("CLINICAL_REVIEWED", event_types)


class DepartureAndHandoffTest(unittest.TestCase):
    def test_departure_requires_accepted_handoff_for_every_treated_patient(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-050", "CWMH-2026-p-050")
        review_ok(svc, "p-050", screen_with_ai_and_hospital(svc, "p-050"))
        prioritize_routine(svc, "p-050")
        consent_treatment(svc, "p-050")
        slot_id = schedule(svc, "p-050", DAYS[0], "09:30", "10:30")
        complete(svc, "p-050", slot_id, DAYS[0])

        report = svc.departure_report(BATCH)
        self.assertFalse(report["ready"])
        self.assertEqual(report["gaps"], [{"patient_ref": "p-050", "missing": "HANDOFF_REGISTERED"}])

        handoff_info = svc.register_handoff(
            idempotency_key="ho-p-050",
            patient_ref="p-050",
            local_provider="local-dr",
            follow_up_at="2026-09-30T09:00:00+12:00",
            escalation={"channel": "电话", "target": "CWMH 眼科门诊", "within_hours": 24},
            occurred_at=at(DAYS[0], "11:30"),
        )
        report = svc.departure_report(BATCH)
        self.assertFalse(report["ready"])
        self.assertEqual(report["gaps"][0]["missing"], "HANDOFF_ACCEPTED")

        svc.accept_handoff(
            idempotency_key="ho-acc-p-050",
            handoff_id=handoff_info["handoff_id"],
            accepted_by="local-dr",
            occurred_at=at(DAYS[0], "11:40"),
        )
        report = svc.departure_report(BATCH)
        self.assertTrue(report["ready"])
        entry = report["handoffs"][0]
        self.assertEqual(entry["local_provider"], "local-dr")
        self.assertEqual(entry["follow_up_at"], "2026-09-30T09:00:00+12:00")
        self.assertEqual(entry["escalation"]["target"], "CWMH 眼科门诊")

    def test_untreated_patients_listed_for_referral(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-051", "CWMH-2026-p-051")
        _, hospital = screen_with_ai_and_hospital(svc, "p-051")
        svc.record_clinical_review(
            idempotency_key="rev-p-051",
            patient_ref="p-051",
            reviewer_id="dr-chen",
            occurred_at=at(DAYS[0], "09:00"),
            indication=False,
            rationale="眼底病变需专科处理，建议转诊",
            based_on=[hospital],
            exam_note="眼底照相提示糖尿病视网膜病变",
        )
        report = svc.departure_report(BATCH)
        self.assertTrue(report["ready"])
        self.assertEqual(report["untreated_referrals"][0]["patient_ref"], "p-051")
        self.assertFalse(report["untreated_referrals"][0]["indication"])


class ExceptionTraceTest(unittest.TestCase):
    def test_humanitarian_exception_trace_reconstructs_full_context(self) -> None:
        """临近返程为特殊残障患者重开物资：例外决定可回看资源、医学意见与后续结果。"""
        svc = build_mission()
        treat_end_to_end(svc, "p-060")  # 已有一台手术占用过资源
        # 特殊残障患者：AI 初筛 + 医院检查，医生确认适应证
        add_patient(svc, "p-061", "CWMH-2026-p-061")
        evidence = screen_with_ai_and_hospital(svc, "p-061")
        review_ok(svc, "p-061", evidence)
        consent_treatment(svc, "p-061")
        # 人道例外：项目治理代表确认，记录重开物资的依据
        decision = svc.set_priority(
            idempotency_key="pri-p-061",
            patient_ref="p-061",
            kind="HUMANITARIAN_EXCEPTION",
            rationale="患者为重度残障，往返筛查点极其困难；返程前重开已封存物资完成手术",
            confirmed_by="gov-zhao",
            occurred_at=at(DAYS[2], "08:00"),
        )
        slot_id = schedule(svc, "p-061", DAYS[2], "09:00", "10:00")
        complete(svc, "p-061", slot_id, DAYS[2])
        handoff(svc, "p-061", DAYS[2])

        trace = svc.trace_exception(decision["event_id"])
        # 当时资源：快照记录了决定时刻的耗材与手术日占用
        snapshot = trace["resource_snapshot"]
        self.assertEqual(snapshot["consumables"]["IOL"], 9)  # p-060 用了一片
        self.assertEqual(snapshot["equipment"], ["phaco-1"])
        self.assertIn("dr-chen", snapshot["staff_on_shift"])
        # 医学意见：复核人、依据、AI 辅助标记与置信边界
        review = trace["clinical_review"]
        self.assertEqual(review["reviewer_id"], "dr-chen")
        self.assertTrue(review["uses_ai_advisory"])
        ai_evidence = [e for e in review["evidence"] if e["source_kind"] == "AI_DEVICE"][0]
        self.assertTrue(ai_evidence["advisory_only"])
        self.assertEqual(ai_evidence["confidence"]["lower"], 0.71)
        # 后续结果：治疗与复查责任
        self.assertEqual(trace["treatment"]["outcome"], "超声乳化+人工晶状体植入顺利，术中无并发症")
        self.assertEqual(trace["followup"]["local_provider"], "local-dr")
        self.assertEqual(trace["followup"]["status"], "ACCEPTED")
        # 例外决定本身的依据与确认人
        self.assertEqual(trace["exception"]["payload"]["confirmed_by"], "gov-zhao")
        self.assertIn("重开", trace["exception"]["payload"]["rationale"])

    def test_trace_rejects_non_exception_event(self) -> None:
        svc = build_mission()
        add_patient(svc, "p-062", "CWMH-2026-p-062")
        result = svc.record_screening(
            idempotency_key="scr-p-062",
            patient_ref="p-062",
            source_kind="HOSPITAL_EXAM",
            occurred_at=at(DAYS[0], "08:40"),
            findings={},
        )
        with self.assertRaises(Exception):
            svc.trace_exception(result["event_id"])


class CandidateQueueTest(unittest.TestCase):
    def test_queue_orders_by_priority_then_review_time(self) -> None:
        svc = build_mission()
        refs = []
        for index, ref in enumerate(("p-070", "p-071", "p-072")):
            add_patient(svc, ref, f"CWMH-2026-{ref}")
            review_ok(svc, ref, screen_with_ai_and_hospital(svc, ref))
            consent_treatment(svc, ref)
            refs.append(ref)
        svc.set_priority(
            idempotency_key="pri-p-070",
            patient_ref="p-070",
            kind="ROUTINE",
            rationale="常规",
            confirmed_by="co-wang",
            occurred_at=at(DAYS[0], "09:10"),
        )
        svc.set_priority(
            idempotency_key="pri-p-071",
            patient_ref="p-071",
            kind="MEDICAL_URGENCY",
            rationale="医学紧急",
            confirmed_by="dr-chen",
            occurred_at=at(DAYS[0], "09:11"),
        )
        svc.set_priority(
            idempotency_key="pri-p-072",
            patient_ref="p-072",
            kind="ACCESSIBILITY",
            rationale="无障碍需求",
            confirmed_by="acc-lin",
            occurred_at=at(DAYS[0], "09:12"),
        )
        queue = svc.candidate_queue(BATCH)
        self.assertEqual([row["patient_ref"] for row in queue], ["p-071", "p-072", "p-070"])
        # 排期后离开候选队列
        schedule(svc, "p-071", DAYS[0], "09:30", "10:30")
        self.assertEqual([row["patient_ref"] for row in svc.candidate_queue(BATCH)], ["p-072", "p-070"])


if __name__ == "__main__":
    unittest.main()
