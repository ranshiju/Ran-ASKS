# Capability Experiences（轻量经验层）

## 定位与优先级

Experience 是跨能力的轻量策略提示层，服务于 `query / ingest / write / build` 的决策点。它不是事实源、不是 playbook 替代品，也不能覆盖操作规范、Schema、raw 红线、graph 边界或回归要求。

优先级固定为：用户与 AGENTS → playbook → 操作规范 → experience。playbook 命中时默认不调用 experience；仅当 playbook 明确允许补充策略，或 playbook/固定路由未覆盖当前歧义时才可调用。

## 调用契约

统一入口：

```bash
python3 .scripts/experience_recall.py recall \
  --capability query|ingest|write|build \
  --context '<当前决策点摘要>' \
  --event start|deadend|ambiguity|retry|failure|planning|implementation|evidence|impact|revision
```

约束：

- 只读取 `memory/experiences/<capability>.md`，不扫描 raw、wiki、graph.db。
- 不调用 LLM、embedding、网络或另一个 experience 工具。
- 每次最多返回 3 条 pattern；无命中直接返回空。
- playbook 命中且未显式允许经验补充时，返回 `skipped_reason=playbook_priority`。
- 经验只用于缩小决策空间；采纳与否由当前能力规范和 agent 判断决定。

## 触发点

- **query**：start 制定轻量检索策略、常规结构化检索全 miss、跨语言/别名/同名消歧。
- **ingest**：解析失败或重试、Schema/来源类型判断不确定、疑似重复摄入。
- **write**：起草/修改前的文体与结构选择、常见修订策略。
- **build**：impact 分析、最小回归选择、历史故障模式判断。

禁止在常规步骤已明确、证据已充分或仅为了“多找一点”时调用，避免拖慢任务。

## Pattern 格式与预算

每个能力文件使用一个 YAML fenced block，字段固定：

```yaml
patterns:
  - id: stable-slug
    triggers: [泛化触发词]
    events: [start|deadend|ambiguity|retry|failure|planning|implementation|evidence|impact|revision]
    advice: 一条可执行策略
    boundaries: 不适用条件与硬边界
    failure_cases: 何时该放弃该经验
    source_trace: 蒸馏来源标识
    status: experimental|stable|deprecated
```

每个能力文件预算 4–6KB，超过预算必须淘汰。不得存完整案例、个人事实、合同/财务信息、raw 原文或可稳定写入 playbook 的确定性规则。

## 审计与生命周期

- 调用方把返回 pattern id 记入当前任务日志的 `experience_used` 字段；无命中记 `no_match`，playbook 优先跳过记 `playbook_priority`。
- LINT 周期从成功且可复用的任务记录蒸馏新 pattern。
- 淘汰条件：已被 playbook/Schema/graph 覆盖、低复用、过细难泛化、与现行规范冲突或来源失效。
- 重复有效的 pattern 可升格为 playbook 或操作规范；升格后原 pattern 标记 deprecated 并保留一个 LINT 周期。

## 与既有联想经验的关系

`memory/association-experiences.md` 是 query 联想层的历史专用形态。新任务统一使用 `memory/experiences/query.md`；旧文件仅在迁移审计完成前保留兼容。
