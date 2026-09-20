# 海外光明行诊疗接力

本仓库保存海外光明行诊疗接力的领域词汇、事件约定与基础校验代码，便于各参与方在后续开发中统一对象身份和版本语义。

## 目录

- `contracts/domain.schema.json`：领域事件信封及稳定枚举。
- `data/sample.json`：一条可用于联调的中文业务样例。
- `src/`：事件基础字段校验。
- `tests/`：领域资料的一致性检查。

当前核心对象为mission_patient、screening_evidence、treatment_slot、followup_handoff，已登记事件为SCREENING_RECEIVED、CLINICAL_REVIEWED、EXCEPTION_APPROVED、TREATMENT_COMPLETED、HANDOFF_ACCEPTED。这些资料只约束基础交换格式，具体业务服务需要在保持兼容的前提下继续建设。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
