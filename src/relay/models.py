"""诊疗接力的核心对象与稳定枚举。

对象身份沿用 contracts/domain.schema.json 的聚合划分：
mission_patient、screening_evidence、treatment_slot、followup_handoff。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SourceKind(str, Enum):
    """筛查来源。"""

    AI_DEVICE = "AI_DEVICE"  # 基层 AI 设备初筛，仅用于辅助分流
    HOSPITAL_EXAM = "HOSPITAL_EXAM"  # 医院检查
    FAMILY_REFERRAL = "FAMILY_REFERRAL"  # 家属转介


class PriorityKind(str, Enum):
    """优先级类别；非常规类别即例外决定，需要留下事件。"""

    ROUTINE = "ROUTINE"  # 常规排期
    MEDICAL_URGENCY = "MEDICAL_URGENCY"  # 医学紧急
    ACCESSIBILITY = "ACCESSIBILITY"  # 无障碍
    HUMANITARIAN_EXCEPTION = "HUMANITARIAN_EXCEPTION"  # 人道例外


class Role(str, Enum):
    """参与角色；例外决定的确认权按角色分离。"""

    OPHTHALMOLOGIST = "OPHTHALMOLOGIST"  # 眼科医生
    MEDICAL_LEAD = "MEDICAL_LEAD"  # 医疗负责人
    SCHEDULING_COORDINATOR = "SCHEDULING_COORDINATOR"  # 排期协调员
    ACCESSIBILITY_COORDINATOR = "ACCESSIBILITY_COORDINATOR"  # 无障碍协调员
    PROGRAM_GOVERNANCE = "PROGRAM_GOVERNANCE"  # 项目治理代表
    NURSE = "NURSE"  # 护士
    LOCAL_PROVIDER = "LOCAL_PROVIDER"  # 当地接手医护


#: 每类优先级只能由对应角色确认，保证不同例外由不同角色负责、可相互制约。
PRIORITY_CONFIRM_ROLES: dict[PriorityKind, frozenset[Role]] = {
    PriorityKind.ROUTINE: frozenset({Role.SCHEDULING_COORDINATOR}),
    PriorityKind.MEDICAL_URGENCY: frozenset({Role.MEDICAL_LEAD}),
    PriorityKind.ACCESSIBILITY: frozenset({Role.ACCESSIBILITY_COORDINATOR}),
    PriorityKind.HUMANITARIAN_EXCEPTION: frozenset({Role.PROGRAM_GOVERNANCE}),
}

#: 候选队列默认排序权重（数值小者靠前），可在服务构造时覆盖。
DEFAULT_PRIORITY_WEIGHTS: dict[PriorityKind, int] = {
    PriorityKind.MEDICAL_URGENCY: 0,
    PriorityKind.HUMANITARIAN_EXCEPTION: 1,
    PriorityKind.ACCESSIBILITY: 2,
    PriorityKind.ROUTINE: 3,
}


class ConsentScope(str, Enum):
    """知情同意范围。"""

    TREATMENT = "TREATMENT"  # 诊疗必需，形成安全记录
    RESEARCH = "RESEARCH"  # 研究用途
    PUBLICITY = "PUBLICITY"  # 宣传用途


#: 非诊疗用途；撤回这些范围不影响已经形成的安全记录。
NON_TREATMENT_SCOPES = (ConsentScope.RESEARCH, ConsentScope.PUBLICITY)


class SlotStatus(str, Enum):
    SCHEDULED = "SCHEDULED"  # 已排期，资源已占用
    COMPLETED = "COMPLETED"  # 已完成治疗
    CANCELLED = "CANCELLED"  # 已取消，资源已释放


class HandoffStatus(str, Enum):
    PENDING = "PENDING"  # 已登记，待当地医护接手
    ACCEPTED = "ACCEPTED"  # 当地医护已接手


@dataclass
class Confidence:
    """AI 筛查的置信边界：score 落在 [lower, upper] 内。"""

    score: float
    lower: float
    upper: float


@dataclass
class Batch:
    """任务批次：一次海外行动，含有限手术日。"""

    batch_id: str
    title: str
    location: str
    surgery_days: frozenset  # set[date]，冻结便于比较


@dataclass
class Patient:
    """患者：系统内引用 + 患者本地标识；身份资料与临床资料分离存放。"""

    patient_ref: str
    batch_id: str
    local_identifier: str  # 当地编号（筛查点编号 / 医院病历号），对接当地记录所需
    registered_at: datetime
    identity: dict = field(default_factory=dict)  # 姓名、联系方式、家庭转述等受限身份资料
    clinical: dict = field(default_factory=dict)  # 出生年、眼别、诊断、过敏史等临床资料


@dataclass
class ScreeningEvidence:
    """筛查证据：来源、置信边界与"仅辅助分流"标记。"""

    evidence_id: str
    patient_ref: str
    source_kind: SourceKind
    occurred_at: datetime
    findings: dict
    recorded_by: str | None = None
    confidence: Confidence | None = None
    model: str | None = None
    model_version: str | None = None
    advisory_only: bool = False  # AI 结果只能用于辅助分流


@dataclass
class ClinicalReview:
    """医生复核：手术适应证、拟行术式与资源需求。"""

    patient_ref: str
    reviewer_id: str
    occurred_at: datetime
    indication: bool  # 手术适应证是否成立
    rationale: str
    procedure: str | None  # 拟行术式
    resource_needs: dict  # 设备、耗材、特殊照护等资源需求
    based_on: list  # 引用的筛查证据 evidence_id 列表
    exam_note: str | None  # 医生现场检查记录
    uses_ai_advisory: bool  # 复核是否参考了 AI 辅助结果


@dataclass
class PriorityDecision:
    """优先级决定：类别、依据与确认人。"""

    patient_ref: str
    kind: PriorityKind
    rationale: str
    confirmed_by: str
    occurred_at: datetime


@dataclass
class ConsentRecord:
    """某一同意范围的最新状态。"""

    patient_ref: str
    scope: ConsentScope
    granted: bool
    occurred_at: datetime
    note: str | None = None


@dataclass
class StaffMember:
    staff_id: str
    name: str
    roles: frozenset  # set[Role]
    team: str  # 所属团队（如 CN-TEAM / LOCAL）


@dataclass
class Equipment:
    equipment_id: str
    batch_id: str
    kind: str  # 如 PHACO_MACHINE（超声乳化设备）
    label: str = ""


@dataclass
class Shift:
    """人员班次：某人在本批次内可上岗的时间窗。"""

    batch_id: str
    staff_id: str
    role: Role
    start: datetime
    end: datetime


@dataclass
class Slot:
    """手术台次：设备、耗材、人员在同一时段的占用。"""

    slot_id: str
    batch_id: str
    patient_ref: str
    start: datetime
    end: datetime
    equipment_ids: tuple
    staff_ids: tuple
    consumables: dict  # 排期时预留的耗材 {kind: qty}
    status: SlotStatus = SlotStatus.SCHEDULED
    cancel_reason: str | None = None


@dataclass
class TreatmentRecord:
    """实际治疗记录（安全记录，不因非诊疗用途撤回而删除）。"""

    slot_id: str
    patient_ref: str
    surgeon_id: str
    occurred_at: datetime
    outcome: str
    consumables_used: dict
    stock_variance: dict  # 实际消耗超出账面库存的部分，留痕供审计
    notes: str | None = None


@dataclass
class Handoff:
    """复查责任交接：当地接手医护、复查时间与异常升级路径。"""

    handoff_id: str
    patient_ref: str
    local_provider: str  # 当地接手医护
    follow_up_at: datetime  # 复查时间
    escalation: dict  # 异常升级路径 {channel, target, within_hours}
    registered_at: datetime
    status: HandoffStatus = HandoffStatus.PENDING
    accepted_by: str | None = None
    accepted_at: datetime | None = None
