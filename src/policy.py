"""角色与共享策略常量。"""

# 系统内角色（actor 以 (staff_id, role) 出现）
TRIAGE_NURSE = "triage_nurse"
OPHTHALMOLOGIST = "ophthalmologist"
MEDICAL_LEAD = "medical_lead"
COORDINATOR = "coordinator"
ACCESSIBILITY_OFFICER = "accessibility_officer"
PROGRAM_REPRESENTATIVE = "program_representative"
SURGEON = "surgeon"
LOCAL_CLINICIAN = "local_clinician"

CLINICAL_ROLES = {TRIAGE_NURSE, OPHTHALMOLOGIST, MEDICAL_LEAD}

# 三种例外通道各自的唯一有权批准角色
EXCEPTION_APPROVER = {
    "medical_urgency": MEDICAL_LEAD,
    "accessibility": ACCESSIBILITY_OFFICER,
    "humanitarian": PROGRAM_REPRESENTATIVE,
}

SCREENING_SOURCES = {"grassroots_ai", "hospital_exam", "family_referral"}
CONFIDENCE_BANDS = {"high", "medium", "low"}

# 跨境治疗目的资料白名单（最少必要）
TREATMENT_EXPORT_WHITELIST = (
    "patient_local_id",
    "age_band",
    "sex",
    "relevant_diagnosis",
    "laterality",
    "procedure",
    "allergies",
    "key_medications",
    "operative_findings",
    "postop_medication",
    "followup_plan",
    "red_flags",
)

# 非治疗目的（宣传/研究等）即便取得同意也只给去标识化的极少量字段
NONCLINICAL_EXPORT_WHITELIST = (
    "patient_local_id",
    "age_band",
    "sex",
    "relevant_diagnosis",
    "laterality",
)
