# 海外光明行诊疗接力

本仓库保存海外光明行诊疗接力的领域词汇、事件约定与基础校验代码，便于各参与方在后续开发中统一对象身份和版本语义。

## 目录

- `contracts/domain.schema.json`：领域事件信封及稳定枚举。
- `data/sample.json`：一条可用于联调的中文业务样例。
- `src/validator.py`：事件基础字段校验。
- `src/relay/`：诊疗接力后端（命令、查询与领域规则）。
- `tests/`：领域资料与后端行为的一致性检查。

当前核心对象为mission_patient、screening_evidence、treatment_slot、followup_handoff，已登记事件为SCREENING_RECEIVED、CLINICAL_REVIEWED、EXCEPTION_APPROVED、TREATMENT_COMPLETED、HANDOFF_ACCEPTED。这些资料只约束基础交换格式，具体业务服务需要在保持兼容的前提下继续建设。

## 诊疗接力后端（src/relay）

`RelayService` 把任务批次、患者本地标识、筛查来源与置信边界、医生复核、手术适应证、资源需求、优先级理由、知情同意、排期、实际治疗与复查责任串成一条可审计的链路。跨方里程碑落成契约规定的领域事件；事件存储按 `event_id` 幂等、按聚合严格递增 `version`，`occurred_at` 永远保留业务发生时间（断网补录不改写，`recorded_at` 为入账时间）。

### 链路命令

```
register_batch / register_staff / register_equipment / add_consumable_stock / register_shift
register_patient            # 患者本地标识；identity 与 clinical 分离存放
record_screening            # AI_DEVICE / HOSPITAL_EXAM / FAMILY_REFERRAL
record_clinical_review      # 医生复核 + 手术适应证 + 资源需求
set_priority                # ROUTINE / MEDICAL_URGENCY / ACCESSIBILITY / HUMANITARIAN_EXCEPTION
record_consent / withdraw_consent
schedule_slot / cancel_slot # 设备、耗材、人员班次一次性校验并提交
complete_treatment          # 实际治疗与耗材结算
register_handoff / accept_handoff
```

### 查询

- `candidate_queue(batch_id)`：待排期候选，按优先级排序。
- `departure_report(batch_id)`：返程前每名患者由当地哪位医护接手、何时复查、异常如何升级，以及未了结缺口。
- `minimal_treatment_projection(patient_ref)` / `treatment_handover_package(batch_id)`：跨国交换用的最少资料投影，不含身份资料。
- `secondary_use_registry(purpose)`：研究/宣传二次利用登记，排除已撤回者。
- `trace_exception(event_id)`：从任何例外决定回看当时资源快照、医学意见与后续治疗、复查结果。
- `patient_timeline(patient_ref)`：患者全链路事件时间线，供下一支医疗队复盘。

### 不变量

- AI 筛查必须给出置信边界与模型信息，只用于辅助分流；确认手术适应证必须基于医生现场检查或医院检查，且只能由登记的眼科医生复核。
- 常规排期由排期协调员确认，医学紧急由医疗负责人确认，无障碍由无障碍协调员确认，人道例外由项目治理代表确认；非常规决定留下 EXCEPTION_APPROVED 事件，载荷含当时资源快照。
- 排期先收集设备、耗材、人员班次的全部冲突再统一提交：要么全部落位，要么整体失败，不留半成品占用。
- 每个命令携带幂等键；断网补录重放不产生副作用（不重复建单、不重复扣库存）。
- 患者撤回非诊疗用途只影响二次利用登记，已经形成的安全记录完整保留；撤回诊疗同意会取消在排台次、阻断后续治疗，但记录同样保留。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
