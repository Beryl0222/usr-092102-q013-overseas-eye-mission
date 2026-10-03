"""诊疗接力后端规则测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from src.dashboard import DepartureReport
from src.errors import (
    ConflictError,
    MismatchedReplay,
    ResourceConflict,
    RuleViolation,
)
from src.events import parse_ts
from src.mission import MissionService
from src.projection import project
from src.resources import ResourceLedger
from src.store import EventStore
from src.transfer import DataTransferService

BATCH = "fiji2026-test"
T0 = "2026-09-21T18:00:00+12:00"

NURSE = {"staff_id": "N1", "role": "triage_nurse"}
EYE = {"staff_id": "D1", "role": "ophthalmologist"}
LEAD = {"staff_id": "L1", "role": "medical_lead"}
COORD = {"staff_id": "C1", "role": "coordinator"}
ACCESS = {"staff_id": "A1", "role": "accessibility_officer"}
PROG = {"staff_id": "P1", "role": "program_representative"}
SURG = {"staff_id": "D1", "role": "surgeon"}
LOCAL = {"staff_id": "LC1", "role": "local_clinician"}

EQUIP = [{"resource_id": "PHACO-01", "label": "超声乳化仪"}]
STAFF = [{"resource_id": "SH-D1", "staff_id": "D1",
          "window_start": "2026-09-22T08:00:00+12:00",
          "window_end": "2026-09-27T18:00:00+12:00"}]
SUPPLIES = {"iol": 2, "visco": 5}


class MissionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.svc = MissionService(self.store, BATCH)
        self.svc.register_batch(COORD, site="Suva", surgery_days=["2026-09-22"],
                                equipment=EQUIP, supplies=SUPPLIES, staffing=STAFF,
                                occurred_at=T0, event_id="b1")

    # ---------- 筛查 / AI ----------

    def test_grassroots_ai_requires_confidence_boundary(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "confidence_band"):
            self.svc.record_screening(NURSE, "P1", source="grassroots_ai",
                                      source_ref="dev#1", findings={},
                                      ai={"model": "m"},
                                      occurred_at=T0)
        with self.assertRaisesRegex(RuleViolation, "boundary_note"):
            self.svc.record_screening(NURSE, "P1", source="grassroots_ai",
                                      source_ref="dev#1", findings={},
                                      ai={"model": "m", "confidence_band": "high"},
                                      occurred_at=T0)
        self.svc.record_screening(NURSE, "P1", source="grassroots_ai",
                                  source_ref="dev#1", findings={},
                                  ai={"model": "m", "confidence_band": "low",
                                      "boundary_note": "b"},
                                  occurred_at=T0)

    def test_three_sources_coexist(self) -> None:
        for i, src in enumerate(("grassroots_ai", "hospital_exam", "family_referral")):
            ai = ({"model": "m", "confidence_band": "medium", "boundary_note": "x"}
                  if src == "grassroots_ai" else None)
            self.svc.record_screening(NURSE, "P1", source=src, source_ref=f"r{i}",
                                      findings={}, ai=ai, occurred_at=T0)
        pv = self.svc._view().patient("P1")
        self.assertEqual(len(pv.screenings), 3)

    # ---------- AI 不得决定手术 ----------

    def test_ai_triage_alone_cannot_schedule(self) -> None:
        self.svc.record_screening(NURSE, "P1", source="grassroots_ai",
                                  source_ref="r", findings={},
                                  ai={"model": "m", "confidence_band": "high",
                                      "boundary_note": "x"}, occurred_at=T0)
        self.svc.record_ai_triage(NURSE, "P1", model_version="m",
                                  suggested_priority="operate_now",
                                  confidence_band="high", boundary_note="仅建议",
                                  occurred_at=T0)
        self.svc.give_consent(NURSE, "P1", scope="treatment", explained_by="D1",
                              witness="N1", language="en", materials=[], occurred_at=T0)
        # 没有 CLINICAL_REVIEWED，任何排期都不允许
        with self.assertRaisesRegex(RuleViolation, "眼科医生"):
            self.svc.schedule_surgery(
                COORD, "P1", slot_id="S1",
                scheduled_start="2026-09-22T09:00:00+12:00",
                scheduled_end="2026-09-22T09:45:00+12:00",
                equipment_resource_ids=["PHACO-01"], staff_resource_ids=["SH-D1"],
                supply_demands={"iol": 1, "visco": 1}, basis="routine", occurred_at=T0)

    def test_only_ophthalmologist_can_review_and_set_indication(self) -> None:
        self.svc.record_screening(NURSE, "P1", source="hospital_exam",
                                  source_ref="r", findings={}, occurred_at=T0)
        with self.assertRaisesRegex(RuleViolation, "ophthalmologist"):
            self.svc.clinical_review(COORD, "P1", indication_for_surgery="白内障",
                                     fitness="fit", required_supplies=["iol"],
                                     equipment_needs=["PHACO-01"],
                                     estimated_minutes=30, rationale="x", occurred_at=T0)

    # ---------- 通道与角色 ----------

    def _make_fit(self, pid: str = "P1", fitness: str = "fit") -> None:
        self.svc.record_screening(NURSE, pid, source="hospital_exam",
                                  source_ref=f"r-{pid}", findings={}, occurred_at=T0)
        self.svc.clinical_review(EYE, pid, indication_for_surgery="白内障" if fitness == "fit" else "",
                                 fitness=fitness, required_supplies=["iol", "visco"],
                                 equipment_needs=["PHACO-01"], estimated_minutes=40,
                                 rationale="检查确认", occurred_at=T0, event_id=f"rev-{pid}")
        self.svc.give_consent(NURSE, pid, scope="treatment", explained_by="D1",
                              witness="N1", language="en", materials=[],
                              occurred_at=T0, event_id=f"con-{pid}")

    def test_routine_channel_requires_coordinator(self) -> None:
        self._make_fit()
        with self.assertRaisesRegex(RuleViolation, "coordinator"):
            self.svc.set_routine_priority(NURSE, "P1", priority=1, reason="x", occurred_at=T0)
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="常规排序",
                                      occurred_at=T0, event_id="pr1")

    def test_exception_role_matrix_and_separation_of_duties(self) -> None:
        self._make_fit("PX")
        # 错误角色
        with self.assertRaisesRegex(RuleViolation, "accessibility_officer"):
            self.svc.approve_exception(COORD, "PX", exception_type="accessibility",
                                       requested_by="N1", rationale="r",
                                       evidence_refs=["e"], occurred_at=T0)
        # 申请人=批准人
        with self.assertRaisesRegex(RuleViolation, "同一人"):
            self.svc.approve_exception(ACCESS, "PX", exception_type="accessibility",
                                       requested_by="A1", rationale="r",
                                       evidence_refs=["e"], occurred_at=T0)
        # 医学紧急必须先有医生意见
        self.svc.record_screening(NURSE, "PY", source="hospital_exam",
                                  source_ref="r-PY", findings={}, occurred_at=T0)
        with self.assertRaisesRegex(RuleViolation, "医学意见"):
            self.svc.approve_exception(LEAD, "PY", exception_type="medical_urgency",
                                       requested_by="N1", rationale="急性",
                                       evidence_refs=["e"], occurred_at=T0)
        # 合规：医疗负责人批准紧急
        self.svc.clinical_review(EYE, "PY", indication_for_surgery="过熟期白内障倾向",
                                 fitness="fit", required_supplies=["iol", "visco"],
                                 equipment_needs=["PHACO-01"], estimated_minutes=40,
                                 rationale="眼压升高风险", occurred_at=T0, event_id="rev-PY")
        self.svc.approve_exception(LEAD, "PY", exception_type="medical_urgency",
                                   requested_by="N1", rationale="48h 内恶化风险",
                                   evidence_refs=["r-PY"], occurred_at=T0, event_id="ex-PY")

    # ---------- 排期与资源 ----------

    def _schedule(self, pid: str, slot: str, start: str, end: str, **kw) -> list[dict]:
        return self.svc.schedule_surgery(
            kw.pop("actor", COORD), pid, slot_id=slot,
            scheduled_start=start, scheduled_end=end,
            equipment_resource_ids=["PHACO-01"], staff_resource_ids=["SH-D1"],
            supply_demands={"iol": 1, "visco": 1},
            basis=kw.pop("basis", "routine"),
            basis_event_id=kw.pop("basis_event_id", None),
            occurred_at=kw.pop("occurred_at", T0),
            event_ids=kw.pop("event_ids", None))

    def test_full_routine_flow_and_consumption_turnover(self) -> None:
        self._make_fit("P1")
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="r",
                                      occurred_at=T0, event_id="pr1")
        self._schedule("P1", "S1", "2026-09-22T09:00:00+12:00",
                       "2026-09-22T09:45:00+12:00", event_ids=["e10"])
        self.assertEqual(self.svc._ledger().supply_state("iol")["available"], 1)
        self.svc.complete_treatment(
            SURG, "P1", slot_id="S1", procedure="phaco+IOL",
            performed_at="2026-09-22T09:40:00+12:00",
            supplies_actually_used={"iol": 1, "visco": 1},
            outcome_at_discharge="平稳", event_id="tx1")
        state = self.svc._ledger().supply_state("iol")
        self.assertEqual((state["initial"], state["held"], state["consumed"], state["available"]),
                         (2, 0, 1, 1))

    def test_double_booking_rejected_atomically(self) -> None:
        self._make_fit("P1")
        self._make_fit("P2")
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="r",
                                      occurred_at=T0, event_id="pr1")
        self.svc.set_routine_priority(COORD, "P2", priority=2, reason="r",
                                      occurred_at=T0, event_id="pr2")
        self._schedule("P1", "S1", "2026-09-22T09:00:00+12:00",
                       "2026-09-22T09:45:00+12:00", event_ids=["e10"])
        with self.assertRaises(ResourceConflict):
            self._schedule("P2", "S2", "2026-09-22T09:30:00+12:00",
                           "2026-09-22T10:15:00+12:00", event_ids=["e20"])
        # 失败后无部分占用：P2 自己非重叠时段可成功，且耗材仍够
        self._schedule("P2", "S3", "2026-09-23T09:00:00+12:00",
                       "2026-09-23T09:45:00+12:00", event_ids=["e30"])

    def test_supply_shortfall_rejects_whole_reservation(self) -> None:
        self._make_fit("P1")
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="r",
                                      occurred_at=T0, event_id="pr1")
        with self.assertRaisesRegex(ResourceConflict, "iol"):
            self.svc.schedule_surgery(
                COORD, "P1", slot_id="S1",
                scheduled_start="2026-09-22T09:00:00+12:00",
                scheduled_end="2026-09-22T09:45:00+12:00",
                equipment_resource_ids=["PHACO-01"], staff_resource_ids=["SH-D1"],
                supply_demands={"iol": 5, "visco": 1}, basis="routine", occurred_at=T0)
        # 设备也不应被预占
        self.assertFalse(self.svc._ledger().is_busy(
            "PHACO-01",
            parse_ts("2026-09-22T09:00:00+12:00"),
            parse_ts("2026-09-22T09:45:00+12:00")))

    def test_outside_shift_window_rejected(self) -> None:
        self._make_fit("P1")
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="r",
                                      occurred_at=T0, event_id="pr1")
        with self.assertRaisesRegex(ResourceConflict, "班次窗口"):
            self._schedule("P1", "S1", "2026-09-21T09:00:00+12:00",
                           "2026-09-21T09:45:00+12:00", event_ids=["e10"])

    def test_exception_basis_event_must_exist_and_single_use(self) -> None:
        self._make_fit("PX")
        with self.assertRaisesRegex(RuleViolation, "EXCEPTION_APPROVED"):
            self._schedule("PX", "S1", "2026-09-22T09:00:00+12:00",
                           "2026-09-22T09:45:00+12:00", basis="accessibility",
                           basis_event_id="nope", event_ids=["e10"])
        self.svc.approve_exception(ACCESS, "PX", exception_type="accessibility",
                                   requested_by="N1", rationale="轮椅",
                                   evidence_refs=["r-PX"], occurred_at=T0, event_id="ex1")
        self._schedule("PX", "S1", "2026-09-22T09:00:00+12:00",
                       "2026-09-22T09:45:00+12:00", basis="accessibility",
                       basis_event_id="ex1", event_ids=["e10"])
        with self.assertRaisesRegex(RuleViolation, "只能支撑一次"):
            self._schedule("PX", "S2", "2026-09-23T09:00:00+12:00",
                           "2026-09-23T09:45:00+12:00", basis="accessibility",
                           basis_event_id="ex1", event_ids=["e20"])

    def test_withdrawn_treatment_consent_blocks_new_schedule(self) -> None:
        self._make_fit("P1")
        self.svc.set_routine_priority(COORD, "P1", priority=1, reason="r",
                                      occurred_at=T0, event_id="pr1")
        self.svc.withdraw_consent(NURSE, "P1", scope_withdrawn="all_future", occurred_at=T0)
        with self.assertRaisesRegex(RuleViolation, "treatment"):
            self._schedule("P1", "S1", "2026-09-22T09:00:00+12:00",
                           "2026-09-22T09:45:00+12:00")

    # ---------- 幂等与并发 ----------

    def test_idempotent_replay_keeps_occurred_at(self) -> None:
        self.svc.record_screening(NURSE, "P1", source="hospital_exam",
                                  source_ref="r", findings={"va": "0.1"},
                                  occurred_at="2026-09-21T10:00:00+12:00", event_id="eX")
        n = len(self.store.all_events())
        self.svc.record_screening(NURSE, "P1", source="hospital_exam",
                                  source_ref="r", findings={"va": "0.1"},
                                  occurred_at="2026-09-21T10:00:00+12:00", event_id="eX")
        self.assertEqual(len(self.store.all_events()), n)
        event = next(e for e in self.store.all_events() if e["event_id"] == "eX")
        self.assertEqual(event["occurred_at"], "2026-09-21T10:00:00+12:00")

    def test_mismatched_replay_rejected(self) -> None:
        self.svc.record_screening(NURSE, "P1", source="hospital_exam",
                                  source_ref="r", findings={"va": "0.1"},
                                  occurred_at=T0, event_id="eY")
        with self.assertRaises(MismatchedReplay):
            self.svc.record_screening(NURSE, "P1", source="family_referral",
                                      source_ref="r", findings={"va": "0.1"},
                                      occurred_at=T0, event_id="eY")

    def test_optimistic_version_conflict(self) -> None:
        self.store.append({
            "event_id": "v1", "event_type": "SCREENING_RECEIVED",
            "aggregate_type": "mission_patient", "aggregate_id": "PV",
            "batch_id": BATCH, "occurred_at": T0, "version": 1,
            "summary": "x", "payload": {}})
        with self.assertRaises(ConflictError):
            self.store.append({
                "event_id": "v2", "event_type": "SCREENING_RECEIVED",
                "aggregate_type": "mission_patient", "aggregate_id": "PV",
                "batch_id": BATCH, "occurred_at": T0, "version": 2,
                "summary": "x", "payload": {}}, expected_version=0)

    def test_recorded_at_cannot_predate_occurred_at(self) -> None:
        with self.assertRaisesRegex(ValueError, "recorded_at"):
            self.store.append({
                "event_id": "v9", "event_type": "SCREENING_RECEIVED",
                "aggregate_type": "mission_patient", "aggregate_id": "PZ",
                "batch_id": BATCH, "occurred_at": "2026-09-25T10:00:00+12:00",
                "recorded_at": "2026-09-24T08:00:00+12:00",
                "version": 1, "summary": "x", "payload": {}})

    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.log"
            s1 = EventStore(path)
            MissionService(s1, BATCH).register_batch(
                COORD, site="S", surgery_days=["2026-09-22"], equipment=EQUIP,
                supplies=SUPPLIES, staffing=STAFF, occurred_at=T0, event_id="b1")
            s2 = EventStore(path)
            self.assertEqual(len(s2.all_events()), 1)
            self.assertEqual(s2.version(BATCH), 1)

    # ---------- 跨境交换与撤回 ----------

    def _treated_patient_with_consent(self, scope: str = "treatment_plus_nonclinical") -> None:
        self._make_fit("P1")
        if scope == "treatment_plus_nonclinical":
            # 覆盖同意
            self.svc.give_consent(NURSE, "P1", scope="treatment_plus_nonclinical",
                                  explained_by="D1", witness="N1", language="en",
                                  materials=[], occurred_at=T0, event_id="con2")

    MINIMAL = {"patient_local_id": "P1", "age_band": "70-79", "sex": "F",
               "relevant_diagnosis": "cataract", "laterality": "OD",
               "procedure": "phaco+IOL", "allergies": "none",
               "key_medications": "none", "operative_findings": "ok",
               "postop_medication": "abx", "followup_plan": "d1/w4",
               "red_flags": "pain"}

    def test_export_whitelist_and_overreach(self) -> None:
        self._treated_patient_with_consent()
        xfer = DataTransferService(self.store, BATCH)
        r = xfer.export_minimal(COORD, "P1", purpose="treatment", recipient_org="NGO",
                                recipient_country="FJ", record=self.MINIMAL,
                                legal_basis="treatment", occurred_at=T0,
                                transfer_id="x1")
        self.assertEqual(len(r["dataset"]), 12)
        with self.assertRaisesRegex(RuleViolation, "passport"):
            xfer.export_minimal(COORD, "P1", purpose="treatment", recipient_org="HQ",
                                recipient_country="CN",
                                record={**self.MINIMAL, "passport_no": "X"},
                                legal_basis="t", occurred_at=T0, transfer_id="x2")

    def test_nonclinical_channel_closes_after_withdrawal_but_treatment_records_remain(self) -> None:
        self._treated_patient_with_consent()
        xfer = DataTransferService(self.store, BATCH)
        xfer.export_minimal(PROG, "P1", purpose="success_story", recipient_org="COMMS",
                            recipient_country="CN",
                            record={"age_band": "70-79"}, legal_basis="consent",
                            occurred_at=T0, transfer_id="x1")
        self.svc.withdraw_consent(NURSE, "P1", scope_withdrawn="nonclinical",
                                  occurred_at=T0, event_id="w1")
        with self.assertRaisesRegex(RuleViolation, "撤回"):
            xfer.export_minimal(PROG, "P1", purpose="research", recipient_org="UNI",
                                recipient_country="AU", record={"age_band": "70-79"},
                                legal_basis="consent", occurred_at=T0, transfer_id="x2")
        # 治疗导出仍允许
        xfer.export_minimal(COORD, "P1", purpose="treatment", recipient_org="NGO",
                            recipient_country="FJ", record=self.MINIMAL,
                            legal_basis="treatment", occurred_at=T0, transfer_id="x3")
        # 安全/诊疗事件无一删除
        types = [e["event_type"] for e in self.store.all_events()]
        self.assertIn("CLINICAL_REVIEWED", types)
        self.assertIn("CONSENT_WITHDRAWN", types)

    def test_treatment_export_requires_treatment_consent(self) -> None:
        xfer = DataTransferService(self.store, BATCH)
        self.svc.record_screening(NURSE, "P9", source="hospital_exam",
                                  source_ref="r", findings={}, occurred_at=T0)
        with self.assertRaises(RuleViolation):
            xfer.export_minimal(COORD, "P9", purpose="treatment", recipient_org="N",
                                recipient_country="FJ", record={"patient_local_id": "P9"},
                                legal_basis="t", occurred_at=T0, transfer_id="x")

    # ---------- 交接 / 复查 / 升级 / 闭环 ----------

    def _full_treated(self, pid: str) -> None:
        self._make_fit(pid)
        self.svc.set_routine_priority(COORD, pid, priority=1, reason="r",
                                      occurred_at=T0, event_id=f"pr-{pid}")
        self._schedule(pid, f"S-{pid}", "2026-09-22T09:00:00+12:00",
                       "2026-09-22T09:45:00+12:00", event_ids=[f"sch-{pid}"])
        self.svc.complete_treatment(SURG, pid, slot_id=f"S-{pid}", procedure="phaco",
                                    performed_at="2026-09-22T09:40:00+12:00",
                                    supplies_actually_used={"iol": 1, "visco": 1},
                                    outcome_at_discharge="ok", event_id=f"tx-{pid}")

    def test_handoff_must_be_accepted_by_named_clinician(self) -> None:
        self._full_treated("P1")
        self.svc.assign_handoff(COORD, "P1", local_clinician_id="LC1",
                                local_facility="Suva", contact="c",
                                responsibilities=["复查"], occurred_at=T0)
        with self.assertRaisesRegex(RuleViolation, "本人"):
            self.svc.accept_handoff(COORD, "P1", understood_red_flags=[], occurred_at=T0)
        self.svc.accept_handoff(LOCAL, "P1", understood_red_flags=["眼痛"], occurred_at=T0)

    def test_batch_cannot_close_until_loop_closed(self) -> None:
        self._full_treated("P1")
        gaps = self.svc.closure_gaps()
        self.assertEqual([g["patient_local_id"] for g in gaps], ["P1"])
        with self.assertRaisesRegex(RuleViolation, "未闭环"):
            self.svc.close_batch(COORD, occurred_at=T0)

        self.svc.assign_handoff(COORD, "P1", local_clinician_id="LC1",
                                local_facility="Suva", contact="c",
                                responsibilities=["复查"], occurred_at=T0,
                                event_id="ha1")
        self.svc.accept_handoff(LOCAL, "P1", understood_red_flags=["痛"],
                                occurred_at=T0, event_id="ha2")
        with self.assertRaisesRegex(RuleViolation, "未安排术后复查"):
            self.svc.close_batch(COORD, occurred_at=T0)
        self.svc.schedule_followup(
            COORD, "P1", followup_no=1, due_at="2026-09-23T09:00:00+12:00",
            modality="in-person", responsible_local_clinician_id="LC1",
            escalation_path={"level1": "LC1", "level2": "on-call",
                             "mission_contact": "m@x"}, occurred_at=T0, event_id="f1")
        self.svc.close_batch(COORD, occurred_at=T0, event_id="close1")

    def test_open_escalation_blocks_closure(self) -> None:
        self._full_treated("P1")
        self.svc.assign_handoff(COORD, "P1", local_clinician_id="LC1",
                                local_facility="Suva", contact="c",
                                responsibilities=["x"], occurred_at=T0, event_id="ha1")
        self.svc.accept_handoff(LOCAL, "P1", understood_red_flags=["x"],
                                occurred_at=T0, event_id="ha2")
        self.svc.schedule_followup(
            COORD, "P1", followup_no=1, due_at="2026-09-23T09:00:00+12:00",
            modality="in-person", responsible_local_clinician_id="LC1",
            escalation_path={"level1": "LC1", "level2": "oc", "mission_contact": "m"},
            occurred_at=T0, event_id="f1")
        self.svc.open_escalation(LOCAL, "P1", red_flag="视力骤降", target="on-call",
                                 occurred_at=T0, event_id="e1")
        with self.assertRaisesRegex(RuleViolation, "未闭环"):
            self.svc.close_batch(COORD, occurred_at=T0)
        self.svc.resolve_escalation(LEAD, "P1", resolution="已处置",
                                    residual_risk="低", occurred_at=T0, event_id="e2")
        self.svc.close_batch(COORD, occurred_at=T0, event_id="close1")

    # ---------- 追溯 ----------

    def test_exception_audit_chain(self) -> None:
        self._full_treated("P1")  # 常规治疗，不影响
        self._make_fit("PX")
        self.svc.approve_exception(ACCESS, "PX", exception_type="accessibility",
                                   requested_by="N1", rationale="轮椅且交通困难",
                                   evidence_refs=["r-PX"], occurred_at=T0, event_id="exPX")
        self.svc.schedule_surgery(
            COORD, "PX", slot_id="SX",
            scheduled_start="2026-09-22T11:00:00+12:00",
            scheduled_end="2026-09-22T11:45:00+12:00",
            equipment_resource_ids=["PHACO-01"], staff_resource_ids=["SH-D1"],
            supply_demands={"iol": 1, "visco": 1}, basis="accessibility",
            basis_event_id="exPX", occurred_at=T0, event_ids=["schPX"])
        self.svc.complete_treatment(SURG, "PX", slot_id="SX", procedure="phaco",
                                    performed_at="2026-09-22T11:40:00+12:00",
                                    supplies_actually_used={"iol": 1, "visco": 1},
                                    outcome_at_discharge="ok", event_id="txPX")
        audit = DepartureReport(self.store, BATCH).exception_audit("PX")
        self.assertEqual(len(audit), 1)
        a = audit[0]
        self.assertEqual(a["approved_by"], "A1")
        self.assertEqual(a["approver_role"], "accessibility_officer")
        self.assertIn("iol", a["resource_snapshot_at_approval"]["supplies"])
        self.assertEqual(a["clinical_opinion_at_decision"]["fitness"], "fit")
        self.assertEqual(a["linked_slots"][0]["treatment"]["outcome_at_discharge"], "ok")


class ContractEnvelopeTest(unittest.TestCase):
    def test_schema_enums_match_code(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "contracts" / "domain.schema.json")
            .read_text(encoding="utf-8"))
        import src.events as events
        self.assertEqual(set(schema["properties"]["event_type"]["enum"]), events.EVENT_TYPES)
        self.assertEqual(set(schema["properties"]["aggregate_type"]["enum"]),
                         events.AGGREGATE_TYPES)


if __name__ == "__main__":
    unittest.main()
