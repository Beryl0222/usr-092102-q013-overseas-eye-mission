"""斐济眼科行动（2026-09，Suva）诊疗接力端到端示例。

运行：python3 -m examples.fiji_mission
演示：三来源筛查与 AI 置信边界 → 医生复核 → 四通道优先级 → 资源原子占用
→ 临近返程为残障患者重开物资的无障碍例外 → 双重预订被拒 → 断网补录幂等
→ 最小化跨境交换与撤回 → 当地交接/复查/升级 → 返程闭环看板与例外追溯。
"""

import json

from src.dashboard import DepartureReport
from src.errors import ResourceConflict, RuleViolation
from src.mission import MissionService
from src.resources import ResourceLedger
from src.store import EventStore
from src.transfer import DataTransferService

BATCH = "fiji2026-0921"

# 角色（staff_id, role）
NURSE = {"staff_id": "N-ANI", "role": "triage_nurse"}
CHEN = {"staff_id": "DR-CHEN", "role": "ophthalmologist"}
LEAD = {"staff_id": "DR-LEAD", "role": "medical_lead"}
COORD = {"staff_id": "CO-TALO", "role": "coordinator"}
ACCESS = {"staff_id": "AO-MERE", "role": "accessibility_officer"}
PROG = {"staff_id": "PR-JO", "role": "program_representative"}
SURGEON = {"staff_id": "DR-CHEN", "role": "surgeon"}
ANA = {"staff_id": "DR-ANA", "role": "local_clinician"}  # 苏瓦当地眼科医生


def line(title: str) -> None:
    print(f"\n{'='*20} {title} {'='*20}")


def main() -> None:
    store = EventStore()
    svc = MissionService(store, BATCH)
    xfer = DataTransferService(store, BATCH)

    line("1. 批次登记：只有一个手术周、一台超声乳化仪、两枚人工晶体")
    svc.register_batch(
        COORD, site="Suva Eye Department", surgery_days=["2026-09-22", "2026-09-25"],
        equipment=[{"resource_id": "PHACO-01", "label": "当地超声乳化仪（唯一一台）"}],
        supplies={"iol": 2, "viscoelastic": 10, "suture_kit": 6},
        staffing=[
            {"resource_id": "SHIFT-DRCHEN", "staff_id": "DR-CHEN",
             "window_start": "2026-09-22T08:00:00+12:00",
             "window_end": "2026-09-26T18:00:00+12:00"},
            {"resource_id": "SHIFT-NANI", "staff_id": "N-ANI",
             "window_start": "2026-09-22T08:00:00+12:00",
             "window_end": "2026-09-26T18:00:00+12:00"},
        ],
        occurred_at="2026-09-21T18:00:00+12:00", event_id="fiji2026-0921-0001")

    line("2. 三名患者从不同来源进入名单（基层AI/医院/家属），AI 只给带边界的建议")
    # FJ-0137：基层 AI 设备筛出（medium 置信），家属又转介了一次
    svc.record_screening(
        NURSE, "FJ-2026-0137", source="grassroots_ai",
        source_ref="village-device-Navua#run42",
        findings={"va_right": "0.2", "va_left": "0.7", "suspect": "cataract_od"},
        ai={"model": "fundus-triage-v3", "score": 0.81, "confidence_band": "medium",
            "boundary_note": "图像有眩光伪影，未散瞳，不能据此判断手术；建议医生复核"},
        raw_evidence_ref="s3://mission/raw/FJ-2026-0137/run42.zip",
        occurred_at="2026-09-21T10:05:00+12:00", event_id="fiji2026-0921-0002")
    svc.record_screening(
        NURSE, "FJ-2026-0137", source="family_referral",
        source_ref="family-daughter@village-health-worker",
        findings={"note": "女儿称母亲夜间已无法独自走动"},
        occurred_at="2026-09-21T11:20:00+12:00", event_id="fiji2026-0921-0003")
    svc.record_ai_triage(
        NURSE, "FJ-2026-0137", model_version="fundus-triage-v3",
        suggested_priority="urgent_review", confidence_band="high",
        boundary_note="高置信仅指“应优先复核”，不等于手术决定",
        occurred_at="2026-09-21T10:06:00+12:00", event_id="fiji2026-0921-0004")

    # FJ-0212：残障患者，医院检查来源；FJ-0305：医院检查，常规候诊
    svc.record_screening(
        NURSE, "FJ-2026-0212", source="hospital_exam",
        source_ref="Suva-OPD-20260918-118",
        findings={"va_right": "0.05", "va_left": "0.08", "suspect": "dense_cataract_both",
                  "accessibility": "轮椅、听障，需手语翻译与无障碍接送"},
        occurred_at="2026-09-21T14:00:00+12:00", event_id="fiji2026-0921-0005")
    svc.record_screening(
        NURSE, "FJ-2026-0305", source="hospital_exam",
        source_ref="Suva-OPD-20260919-077",
        findings={"va_right": "0.3", "suspect": "cataract_od_early"},
        occurred_at="2026-09-21T14:30:00+12:00", event_id="fiji2026-0921-0006")

    line("3. 只有眼科医生复核能确立适应证；AI 不能直接排期")
    for pid, ind, fit in [
        ("FJ-2026-0137", "右眼年龄相关性白内障，视力损害影响独立生活", "fit"),
        ("FJ-2026-0212", "双眼致密白内障，双眼视力 0.1 以下", "fit"),
        ("FJ-2026-0305", "右眼早期白内障，尚可等待下一支队伍", "defer"),
    ]:
        svc.clinical_review(
            CHEN, pid, indication_for_surgery=ind, fitness=fit,
            required_supplies=["iol", "viscoelastic", "suture_kit"],
            equipment_needs=["PHACO-01"], estimated_minutes=45,
            rationale="散瞳裂隙灯检查确认；AI 仅作分诊输入，诊断以人工检查为准",
            confidence="confirmed", occurred_at="2026-09-21T16:00:00+12:00",
            event_id=f"review-{pid}")

    # 反例：没有医生 fit 复核、或仅凭 AI，排期必须被拒（FJ-0305 为 defer）
    svc.give_consent(
        NURSE, "FJ-2026-0305", scope="treatment", explained_by="DR-CHEN",
        witness="N-ANI", language="en-FJ", materials=["iTaukei 口头解释卡"],
        occurred_at="2026-09-21T16:30:00+12:00", event_id="consent-0305")
    svc.set_routine_priority(
        COORD, "FJ-2026-0305", priority=3, reason="早期白内障，常规等候",
        occurred_at="2026-09-21T17:00:00+12:00", event_id="prio-0305")
    try:
        svc.schedule_surgery(
            COORD, "FJ-2026-0305", slot_id="SLOT-X",
            scheduled_start="2026-09-22T09:00:00+12:00",
            scheduled_end="2026-09-22T09:45:00+12:00",
            equipment_resource_ids=["PHACO-01"], staff_resource_ids=["SHIFT-DRCHEN"],
            supply_demands={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
            basis="routine", occurred_at="2026-09-21T17:05:00+12:00")
    except RuleViolation as e:
        print(f"[拒绝] defer 患者排期：{e}")

    line("4. FJ-0137 走常规通道：协调员确认优先级、同意、排期并治疗")
    svc.set_routine_priority(
        COORD, "FJ-2026-0137", priority=1,
        reason="视力 0.2 + 夜间无法独立行动；等待时间与损害程度综合排序",
        occurred_at="2026-09-21T17:10:00+12:00", event_id="prio-0137")
    svc.give_consent(
        NURSE, "FJ-2026-0137", scope="treatment", explained_by="DR-CHEN",
        witness="N-ANI", language="iTaukei", materials=["iTaukei 同意书", "图示说明"],
        occurred_at="2026-09-21T17:20:00+12:00", event_id="consent-0137")
    svc.schedule_surgery(
        COORD, "FJ-2026-0137", slot_id="SLOT-0922-01",
        scheduled_start="2026-09-22T09:00:00+12:00",
        scheduled_end="2026-09-22T09:45:00+12:00",
        equipment_resource_ids=["PHACO-01"],
        staff_resource_ids=["SHIFT-DRCHEN", "SHIFT-NANI"],
        supply_demands={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
        basis="routine", occurred_at="2026-09-21T17:30:00+12:00",
        event_ids=["fiji2026-0921-0010"])
    print("库存（FJ-0137 已预占）：",
          ResourceLedger(store.all_events()).supply_state("iol"))

    line("5. 并发占用一致：同一台 PHACO-01 的重叠排期整体失败（FJ-0137 已有资格再抢同一时段）")
    try:
        svc.schedule_surgery(
            COORD, "FJ-2026-0137", slot_id="SLOT-0922-DUP",
            scheduled_start="2026-09-22T09:15:00+12:00",
            scheduled_end="2026-09-22T10:00:00+12:00",
            equipment_resource_ids=["PHACO-01"],
            staff_resource_ids=["SHIFT-DRCHEN", "SHIFT-NANI"],
            supply_demands={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
            basis="routine", occurred_at="2026-09-21T17:35:00+12:00")
    except RuleViolation as e:
        print(f"[拒绝] {e}")
    except ResourceConflict as e:
        print(f"[拒绝] 双重预订：{e}")
    print("失败排期未产生任何部分占用，IOL 可用仍为：",
          ResourceLedger(store.all_events()).supply_state("iol")["available"])

    line("6. 断网补录：保留现场发生时间；同一 event_id 重放幂等、不产生第二条")
    before = len(store.all_events())
    svc.record_screening(
        NURSE, "FJ-2026-0137", source="grassroots_ai",
        source_ref="village-device-Navua#run42",
        findings={"va_right": "0.2", "va_left": "0.7", "suspect": "cataract_od"},
        ai={"model": "fundus-triage-v3", "score": 0.81, "confidence_band": "medium",
            "boundary_note": "图像有眩光伪影，未散瞳，不能据此判断手术；建议医生复核"},
        raw_evidence_ref="s3://mission/raw/FJ-2026-0137/run42.zip",
        occurred_at="2026-09-21T10:05:00+12:00",   # 现场时间，不是现在
        event_id="fiji2026-0921-0002")              # 重连后重放同一批
    after = len(store.all_events())
    print(f"重放前后事件数：{before} → {after}（相同，幂等）；"
          "occurred_at 仍为 2026-09-21T10:05+12:00")

    line("7. FJ-0137 完成手术：预占转实耗，设备班次释放")
    svc.complete_treatment(
        SURGEON, "FJ-2026-0137", slot_id="SLOT-0922-01",
        procedure="phacoemulsification + IOL OD",
        performed_at="2026-09-22T09:40:00+12:00",
        supplies_actually_used={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
        outcome_at_discharge="无并发症，次日当地复查",
        notes="手术顺利", event_id="tx-0137")
    print("库存（FJ-0137 术后）：",
          ResourceLedger(store.all_events()).supply_state("iol"))

    line("8. 临近返程为残障患者重开物资：无障碍例外，角色分立、申请人≠批准人")
    # 错误角色批准被拒
    try:
        svc.approve_exception(
            COORD, "FJ-2026-0212", exception_type="accessibility",
            requested_by="N-ANI", rationale="轮椅患者转运困难",
            evidence_refs=["Suva-OPD-20260918-118"],
            occurred_at="2026-09-25T19:00:00+12:00")
    except RuleViolation as e:
        print(f"[拒绝] 协调员无权批无障碍例外：{e}")
    # 自己申请自己批被拒
    try:
        svc.approve_exception(
            ACCESS, "FJ-2026-0212", exception_type="accessibility",
            requested_by="AO-MERE", rationale="同一人申请并批准",
            evidence_refs=["Suva-OPD-20260918-118"],
            occurred_at="2026-09-25T19:05:00+12:00")
    except RuleViolation as e:
        print(f"[拒绝] {e}")
    # 合规：护士申请，无障碍专员依据可达性评估批准，事件固化此刻资源快照
    svc.approve_exception(
        ACCESS, "FJ-2026-0212", exception_type="accessibility",
        requested_by="N-ANI",
        rationale="患者轮椅且听障，住所到苏瓦单程 4 小时；本次不手术将至少再等一年，"
                  "已落实无障碍车辆与手语翻译，9-26 加开一台",
        evidence_refs=["Suva-OPD-20260918-118", "accessibility-assessment-0212",
                       "transport-voucher-0212"],
        occurred_at="2026-09-25T19:30:00+12:00", event_id="ex-0212")

    line("9. FJ-0212 依据例外排期（最后一枚 IOL）并治疗")
    ex = next(e for e in store.all_events()
              if e["event_type"] == "EXCEPTION_APPROVED" and e["payload"]["patient_local_id"] == "FJ-2026-0212")
    svc.give_consent(
        NURSE, "FJ-2026-0212", scope="treatment_plus_nonclinical",
        explained_by="DR-CHEN", witness="SIGN-LANG-INTERP", language="sign+en-FJ",
        materials=["手语视频同意书", "大字版图示"],
        occurred_at="2026-09-25T20:00:00+12:00", event_id="consent-0212")
    svc.schedule_surgery(
        COORD, "FJ-2026-0212", slot_id="SLOT-0926-EXTRA",
        scheduled_start="2026-09-26T08:30:00+12:00",
        scheduled_end="2026-09-26T09:15:00+12:00",
        equipment_resource_ids=["PHACO-01"],
        staff_resource_ids=["SHIFT-DRCHEN", "SHIFT-NANI"],
        supply_demands={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
        basis="accessibility", basis_event_id=ex["event_id"],
        occurred_at="2026-09-25T20:10:00+12:00", event_ids=["fiji2026-0925-0020"])
    print("库存（例外排期后，IOL 见底）：",
          ResourceLedger(store.all_events()).supply_state("iol"))
    svc.complete_treatment(
        SURGEON, "FJ-2026-0212", slot_id="SLOT-0926-EXTRA",
        procedure="phacoemulsification + IOL OU (sequential, first eye OD)",
        performed_at="2026-09-26T09:10:00+12:00",
        supplies_actually_used={"iol": 1, "viscoelastic": 1, "suture_kit": 1},
        outcome_at_discharge="一般情况稳定；听障患者已用图文卡交代红旗症状",
        complications="", notes="手语翻译全程在场", event_id="tx-0212")

    line("10. 跨境只交换治疗所需最少资料；撤回非诊疗用途后该通道关闭、诊疗链不动")
    minimal = {
        "patient_local_id": "FJ-2026-0212", "age_band": "65-74", "sex": "F",
        "relevant_diagnosis": "dense cataract OU", "laterality": "OU",
        "procedure": "phaco+IOL OD 2026-09-26; OS deferred",
        "allergies": "none known", "key_medications": "none",
        "operative_findings": "uneventful", "postop_medication": "ofloxacin+pred acetate qid",
        "followup_plan": "day1 / week4 local review", "red_flags": "pain, vision loss, discharge",
    }
    r = xfer.export_minimal(
        COORD, "FJ-2026-0212", purpose="treatment",
        recipient_org="Fiji Eye Care NGO", recipient_country="FJ",
        record=minimal, legal_basis="treatment-continuity",
        occurred_at="2026-09-26T10:00:00+12:00", transfer_id="xfer-0212-treatment")
    print("治疗目的导出字段：", r["dataset"].keys() and len(r["dataset"]), "个（白名单内）")
    # 试图夹带护照号 → 边界拒绝
    try:
        xfer.export_minimal(
            COORD, "FJ-2026-0212", purpose="treatment",
            recipient_org="Overseas HQ", recipient_country="CN",
            record={**minimal, "passport_no": "FJ9***"}, legal_basis="treatment",
            occurred_at="2026-09-26T10:05:00+12:00", transfer_id="xfer-overreach")
    except RuleViolation as e:
        print(f"[拒绝] 超最小化字段：{e}")
    # 宣传用途在同意期内可以，撤回后被拒；治疗导出与安全记录不受影响
    xfer.export_minimal(
        PROG, "FJ-2026-0212", purpose="success_story",
        recipient_org="Foundation Comms", recipient_country="CN",
        record={"age_band": "65-74", "sex": "F",
                "relevant_diagnosis": "dense cataract OU", "laterality": "OU"},
        legal_basis="consent", occurred_at="2026-09-26T10:10:00+12:00",
        transfer_id="xfer-0212-story")
    print("宣传用途仅给出 4 个去标识化字段，不含本地号/姓名/联系方式")
    svc.withdraw_consent(
        NURSE, "FJ-2026-0212", scope_withdrawn="nonclinical",
        occurred_at="2026-09-26T12:00:00+12:00", event_id="withdraw-0212")
    try:
        xfer.export_minimal(
            PROG, "FJ-2026-0212", purpose="success_story_2",
            recipient_org="Foundation Comms", recipient_country="CN",
            record={"age_band": "65-74"}, legal_basis="consent",
            occurred_at="2026-09-26T12:30:00+12:00", transfer_id="xfer-after-withdraw")
    except RuleViolation as e:
        print(f"[拒绝] 撤回后非治疗导出：{e}")
    xfer.export_minimal(
        COORD, "FJ-2026-0212", purpose="treatment",
        recipient_org="Fiji Eye Care NGO", recipient_country="FJ",
        record=minimal, legal_basis="treatment-continuity",
        occurred_at="2026-09-26T13:00:00+12:00", transfer_id="xfer-0212-treatment-2")
    print("撤回后治疗目的导出仍可进行；适应证/同意/治疗/交接事件均保留")

    line("11. 返程前明确：谁接手、何时复查、异常如何升级")
    # FJ-0137：9-22 术毕当日完成交接与次日复查安排（结果 9-23 补录）
    # FJ-0212：9-26 加台术后当日交接，9-27 首次复查
    handoff_times = {
        "FJ-2026-0137": ("2026-09-22T15:00:00+12:00", "2026-09-22T15:30:00+12:00",
                         "2026-09-22T15:45:00+12:00", "2026-09-22T15:50:00+12:00",
                         "2026-10-20T09:00:00+12:00"),
        "FJ-2026-0212": ("2026-09-26T15:00:00+12:00", "2026-09-26T15:30:00+12:00",
                         "2026-09-26T15:45:00+12:00", "2026-09-26T15:50:00+12:00",
                         "2026-10-24T09:00:00+12:00"),
    }
    for pid, first_due in [("FJ-2026-0137", "2026-09-23T09:00:00+12:00"),
                           ("FJ-2026-0212", "2026-09-27T09:00:00+12:00")]:
        t_assign, t_accept, t_fu1, t_fu2, due_fu2 = handoff_times[pid]
        svc.assign_handoff(
            COORD, pid, local_clinician_id="DR-ANA",
            local_facility="Suva Eye Department", contact="+679-***",
            responsibilities=["术后用药督导", "眼压与切口检查", "异常升级"],
            occurred_at=t_assign, event_id=f"assign-{pid}")
        svc.accept_handoff(
            ANA, pid, understood_red_flags=["眼痛加重", "视力骤降", "分泌物", "恶心呕吐"],
            occurred_at=t_accept, event_id=f"accept-{pid}")
        svc.schedule_followup(
            COORD, pid, followup_no=1, due_at=first_due, modality="in-person",
            responsible_local_clinician_id="DR-ANA",
            escalation_path={"level1": "Suva Eye Department DR-ANA",
                             "level2": "on-call ophthalmologist +679-***",
                             "mission_contact": "mission2026@bright-vision.example"},
            occurred_at=t_fu1, event_id=f"fu1-{pid}")
        svc.schedule_followup(
            COORD, pid, followup_no=2,
            due_at=due_fu2, modality="in-person",
            responsible_local_clinician_id="DR-ANA",
            escalation_path={"level1": "Suva Eye Department DR-ANA",
                             "level2": "on-call ophthalmologist +679-***",
                             "mission_contact": "mission2026@bright-vision.example"},
            occurred_at=t_fu2, event_id=f"fu2-{pid}")

    # 次日复查：FJ-0212 出现异常 → 升级 → 处置闭环
    svc.record_followup_outcome(
        ANA, "FJ-2026-0137", followup_no=1,
        checked_at="2026-09-23T09:20:00+12:00", result="恢复良好，OD 0.6，切口清洁",
        va="0.6", findings="无异常", event_id="fuout-0137-1")
    svc.record_followup_outcome(
        ANA, "FJ-2026-0212", followup_no=1,
        checked_at="2026-09-27T09:30:00+12:00", result="轻度前房反应，眼压偏高 24mmHg",
        va="0.2", findings="Tyndall+，加用降眼压药并当日复测",
        event_id="fuout-0212-1")
    svc.open_escalation(
        ANA, "FJ-2026-0212", red_flag="眼压 24mmHg + 前房反应",
        target="on-call ophthalmologist +679-***", linked_followup_no=1,
        occurred_at="2026-09-27T09:45:00+12:00", event_id="esc-0212-open")
    svc.resolve_escalation(
        LEAD, "FJ-2026-0212",
        resolution="返程当日远程会诊：加 brimonidine bid，留观至下午复测，眼压降至 16；"
                   "由 DR-ANA 接续 48 小时复测与第 4 周复查",
        residual_risk="低；若复测 >21 继续升级并考虑抗青光眼评估",
        occurred_at="2026-09-27T16:30:00+12:00", event_id="esc-0212-resolved")
    # 第 4 周复查（10-24）在返程时尚未发生：只留安排与责任人，结果由 DR-ANA 届时补录——
    # 系统不接受“发生时间晚于录入时刻”的结果记录，防止预填随访。

    line("12. 返程闭环检查：有未闭环项时禁止关闭批次")
    print("当前缺口：", svc.closure_gaps())
    closed = svc.close_batch(
        COORD, occurred_at="2026-09-27T18:00:00+12:00", event_id="batch-close")
    print(closed["summary"])

    line("13. 返程看板（下一支医疗队的接手视图）")
    board = DepartureReport(store, BATCH).board()
    for row in board["patients"]:
        print(f"- {row['patient_local_id']}｜来源 "
              f"{[s['source'] for s in row['screening_sources']]}｜"
              f"通道：{row['priority_channel'] or '未排期'}｜"
              f"已治疗：{row['treated']}｜接手：{row['local_clinician']} "
              f"({row['handoff_state']})｜首次复查：{row['first_followup_due']}｜"
              f"缺口：{row['closure_gaps'] or '无'}")
    print(f"汇总：{board['treated_total']} 人治疗，{board['closed_loop_total']} 人闭环")

    line("14. 例外追溯：从一次开口子回看资源、医学意见与后续结果")
    audit = DepartureReport(store, BATCH).exception_audit("FJ-2026-0212")[0]
    print(json.dumps({
        "exception": audit["exception_type"],
        "requested_by": audit["requested_by"], "approved_by": audit["approved_by"],
        "approver_role": audit["approver_role"], "decision_at": audit["decision_at"],
        "clinical_opinion": audit["clinical_opinion_at_decision"]["indication_for_surgery"],
        "iol_at_that_moment": audit["resource_snapshot_at_approval"]["supplies"]["iol"],
        "linked_slot": audit["linked_slots"][0]["slot_id"],
        "treatment_outcome": audit["linked_slots"][0]["treatment"]["outcome_at_discharge"],
        "followups": audit["followup_outcomes"],
        "escalation": audit["escalations"][0],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
