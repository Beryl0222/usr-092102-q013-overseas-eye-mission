# 诊疗接力领域事件目录

斐济眼科行动（海外光明行）诊疗接力后端的统一语言。所有跨角色、跨队伍交换的事实都以**不可变领域事件**表达；当前状态由事件流重放得到，任何例外决定都可以从事件回溯到当时的资源、医学意见与后续结果。

## 1. 标识规则（沿用 `contracts/domain.schema.json`）

每条事件必须携带信封字段：

| 字段 | 含义 |
|---|---|
| `event_id` | 全局唯一，**断网补录的幂等键**。现场离线生成（建议 `{batch_id}-{seq:04d}`），重连后重放：同 id 同载荷 → 返回原事件；同 id 异载荷 → 拒绝（`MismatchedReplay`）。 |
| `event_type` / `aggregate_type` | 取自 schema 稳定枚举，新增事件类型须先改契约。 |
| `aggregate_id` | 聚合标识，同一聚合的事件共享（如患者 `FJ-2026-0137`）。 |
| `occurred_at` | **实际发生时间**。断网补录必须保留现场时间，禁止改写为补录时刻。 |
| `recorded_at` | 进入系统时间；实时录入与 `occurred_at` 相同，补录时晚于它。 |
| `version` | 事件在所属聚合流中的版本号，从 1 连续递增；用于乐观并发控制。 |
| `batch_id` | 任务批次，如 `fiji2026-0921`。 |
| `correlation_id` | 一次业务动作跨多事件的关联号（如一次排期同时占用设备、班次、耗材）。 |
| `causation_id` | 上游事件 id（如例外批准 → 重开物资排期）。 |

聚合标识前缀：`task_batch` = 批次号；`mission_patient` = 现场本地号（如 `FJ-2026-0137`，非护照等跨境身份）；`screening_evidence` = `{patient}#src{n}`；`treatment_slot` = `{patient}#sched{n}`；`resource` = `{kind}:{code}`；`followup_handoff` = `{patient}#handoff`；`data_transfer` = 导出单号。

## 2. 聚合与事件

### task_batch（任务批次）
- **BATCH_REGISTERED**：批次登记。载荷：`mission_code, site, surgery_days, equipment(可用设备清单), supplies(耗材期初), staffing(班次窗口), coordinator_id`。
- **BATCH_CLOSED**：批次关闭（返程）。载荷：`closed_by, open_followups(未闭环清单)`；存在未闭环交接时拒绝关闭。

### mission_patient（患者）
- **SCREENING_RECEIVED**：登记一条筛查来源。载荷：`patient_local_id, source ∈ {grassroots_ai, hospital_exam, family_referral}, source_ref, recorded_by, findings{vision,…}, ai?:{model,score,confidence_band:high|medium|low,boundary_note, recommends_review}`。三来源可并存；AI 来源必须带置信带与边界说明。
- **AI_TRIAGE_RECORDED**：AI 辅助分流建议（独立于筛查证据）。载荷：`suggested_priority, confidence_band, boundary_note, model_version, reviewed_by_ai_gateway`。**仅为建议**，不产生排期权。
- **CLINICAL_REVIEWED**：眼科医生人工复核。载荷：`ophthalmologist_id, indication_for_surgery（手术适应证）, contraindications, fitness, required_supplies[], equipment_needs[], estimated_minutes, ai_overridden, rationale, confidence:confirmed|uncertain`。**只有该事件确立手术适应证**；AI 不能代替。
- **PRIORITY_SET**：常规通道优先级。载荷：`channel:routine, priority, reason, set_by:coordinator_role`。
- **EXCEPTION_APPROVED**：例外批准（医学紧急 / 无障碍 / 人道，三类分立）。载荷：`exception_type ∈ {medical_urgency, accessibility, humanitarian}, rationale, evidence_refs[], requested_by, approved_by, resource_snapshot（批准时刻设备/耗材/班次占用快照）, decision_at`。**请求人与批准人不得为同一人**，且批准人角色必须与通道匹配（见 §3）。
- **CONSENT_GIVEN**：知情同意。载荷：`scope ∈ {treatment, treatment_plus_nonclinical}, explained_by, witness, language, materials, consent_at`。
- **CONSENT_WITHDRAWN**：撤回。载荷：`scope_withdrawn:nonclinical|all_future, withdrawn_at, received_by`。撤回非诊疗用途**不删除、不影响**已形成的诊疗/安全记录；撤回后拒绝一切非治疗目的导出。撤回未来治疗则不得再排新手术。
- **SURGERY_SCHEDULED**：排期。载荷：`slot_id, scheduled_start, scheduled_end, equipment_resource_ids[], staff_resource_ids[], supply_demands{code:qty}, correlation_id, basis ∈ {routine, medical_urgency, accessibility, humanitarian}, basis_event_id`。排期成功以同一 correlation 下的 **SLOT_RESERVED**（设备/班次）成功为前提；单台超声乳化设备同一时间窗只能被一个排期占用。
- **TREATMENT_COMPLETED**：实际治疗。载荷：`slot_id, surgeon_id, procedure, performed_at, supplies_actually_used{code:qty}, complications?, outcome_at_discharge, deviation_reason?（与排期不符时必填）, notes`。完成时释放占用、按实消耗耗材（**SUPPLIES_CONSUMED** 同 correlation）。

### screening_evidence（筛查证据）
筛查来源登记（SCREENING_RECEIVED）写入患者事件流以保证排序一致；原始证据包（AI 图像、基层设备导出、医院报告）以 `aggregate_type=screening_evidence`（id `{patient}#src{n}`）独立留痕，只允许追加、不允许修改，医生判断只能通过 **CLINICAL_REVIEWED** 表达。

### treatment_slot（手术时段）
- **SLOT_RESERVED** / **SLOT_RELEASED**：资源账本事件（聚合也可记在 `resource`）。载荷：`resource_id, kind∈{equipment,staff_shift,supply}, interval_start/end（设备/班次）, qty（耗材）, correlation_id, purpose_slot_id`。双重预订、班次外时间、超库存均被拒绝。

### resource（设备/耗材/人员班次）
- **SUPPLIES_CONSUMED**：实际出库。载荷：`supply_code, qty, correlation_id, used_for_slot_id`。
- SLOT_RESERVED 对耗材表示**预占**，TREATMENT_COMPLETED 后转为实际消耗并核对差异。

### followup_handoff（术后交接与复查）
- **HANDOFF_ASSIGNED**：指定当地接手医护。载荷：`local_clinician_id, local_facility, contact, assigned_by, responsibilities[]`。
- **HANDOFF_ACCEPTED**：接手确认。载荷：`accepted_by, accepted_at, understood_red_flags[]`。必须由被指名人接受，未接受不算闭环。
- **FOLLOWUP_SCHEDULED**：复查安排。载荷：`followup_no, due_at, modality, responsible_local_clinician_id, escalation_path{level1, level2, mission_contact}`。
- **FOLLOWUP_OUTCOME_RECORDED**：复查结果。载荷：`followup_no, checked_at, result, va?, findings, recorder`。
- **ESCALATION_OPENED**：异常升级。载荷：`red_flag, opened_at, opened_by, target, linked_followup_no`。
- **ESCALATION_RESOLVED**：升级处置结果。载荷：`resolution, resolved_at, resolved_by, residual_risk`。

### data_transfer（跨境资料交换）
- **DATA_EXPORTED**：最小化导出留痕。载荷：`purpose ∈ {treatment, …其他}，recipient_org, recipient_country, fields[], legal_basis, patient_local_id, consent_scope_at_export, approved_by, exported_at`。仅治疗目的可在患者仅授予 treatment 范围同意时进行；导出字段按白名单（§4）裁剪，任何非治疗目的在撤回后一律拒绝。

## 3. 优先级通道与确认角色矩阵

| 通道 | 事件 | 依据记录 | 请求人 | 批准/确认人 |
|---|---|---|---|---|
| 常规排期 | PRIORITY_SET + SURGERY_SCHEDULED(basis=routine) | 评分/等待时间/视力损害 | 分诊护士 | **现场协调员**（coordinator） |
| 医学紧急 | EXCEPTION_APPROVED(medical_urgency) | 眼科医生意见 + 急性/恶化风险 | 任一临床人员 | **医疗负责人**（medical_lead；不得是申请人） |
| 无障碍 | EXCEPTION_APPROVED(accessibility) | 残障事实、陪同与可达性评估 | 分诊/家属 | **无障碍专员**（accessibility_officer） |
| 人道主义 | EXCEPTION_APPROVED(humanitarian) | 社会处境与不可再等待的理由 | 现场任一角色 | **项目方代表**（program_representative） |

硬规则：
1. AI_TRIAGE_RECORDED 任何置信带都不直接产生 SURGERY_SCHEDULED；高置信只意味着“优先请医生复核”。
2. 无 CLINICAL_REVIEWED（fitness=fit 且有明确适应证）不得排期。
3. 无对应 scope 的 CONSENT_GIVEN 不得排期/导出。
4. 任何排期必须在资源账本上同时取得设备、人员班次窗口、耗材三类预占；任一冲突整体失败（不允许部分占用）。
5. 例外排期须在 `basis_event_id` 引用有效的 EXCEPTION_APPROVED；批准事件固化 `resource_snapshot`，供事后回看“重开物资时还剩什么、谁同意的、医学意见是什么”。

## 4. 跨境最小资料白名单（treatment 目的）

`patient_local_id, age_band, sex, relevant_diagnosis, laterality, procedure, allergies, key_medications, operative_findings, postop_medication, followup_plan, red_flags`。

排除：姓名、护照号/身份证号、联系方式、家属转介叙述、AI 原始图像、族裔/宗教、定位轨迹。非治疗目的（宣传、效果故事、二次研究）须 `treatment_plus_nonclinical` 同意且未撤回；撤回后该通道关闭。**安全与诊疗记录（适应证、同意、排期、治疗、交接、异常）永不可因营销撤回而删除或改写。**

## 5. 断网补录与并发

- 现场以本地事件日志录入，`occurred_at` 用现场时钟；上线后按 `event_id` 幂等推送。
- 存储按聚合维护版本：提交时 `expected_version` 必须匹配当前流版本，否则 `ConflictError`，由调用方重读重放后重试。
- 资源占用的判定基于**全量已提交事件重放**，因此两个并发排期对同一设备时段只有一个能提交成功。
- 时钟约束：补录事件 `recorded_at >= occurred_at`；事件一经追加不可变（修正只能追加新事件）。

## 6. 返程闭环定义

批次可关闭当且仅当每名有 TREATMENT_COMPLETED 的患者都具备：HANDOFF_ASSIGNED + HANDOFF_ACCEPTED + 至少一条 FOLLOWUP_SCHEDULED，且所有打开的 ESCALATION_OPENED 已有 ESCALATION_RESOLVED。看板按患者展示：本地号、来源与置信边界、医生适应证、优先级通道与批准人、同意范围、实际治疗、接手人、首次复查日期、升级路径、缺口。例外台账支持从任一 EXCEPTION_APPROVED 联到 resource_snapshot、医生复核、排期、治疗与复查结果——完整责任链，而非一次性的成功故事。
