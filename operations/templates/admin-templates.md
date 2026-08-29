# 行政文档 Wiki 模板（v5，2026-07-27）

> 按语义原型分流。ingest 步骤 3.5 判断原型后选对应模板。
> skeleton 脚本填确定性字段，LLM 只填语义槽。

## 规范型模板（policy/procedure/decision）

适用：制度、政策、通知、管理办法、操作流程、决策记录

```markdown
---
title: "文件标题"
type: policy | procedure | decision
sources:
  - raw/xxx
source_type: official-doc | ocr | web
date: YYYY-MM-DD
effective_from: YYYY-MM-DD        # 生效日期(无则留空待审)
effective_to: ""                   # 失效日期(无则留空)
applicable_to: "适用对象"           # 物理系/全校/某岗位
department: "发布部门"
status: active | draft | deprecated
superseded_by: ""                  # 版本演进指向
confidence: high | medium
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[policies/相关政策]]"
---

# 文件标题

> **发布者**：XX | **适用对象**：XX | **生效**：YYYY-MM-DD | **状态**：现行/废止

## Navigation

面向 query 的 2-4 句导航（约 80-150 tokens）：说明这是什么文件/事项、核心主题和问题类型、用户可能使用的同义表达或简称；来源明确时补充关键人物/部门、时间/状态和关联文件。只能依据 raw，不新增事实；不要复制正文。

## 适用对象与范围

- 适用对象：谁/什么场景
- 适用范围：什么事项

## 核心规则

### 权利/义务
- 可以/必须做什么

### 禁止/例外
- 不得做什么
- 例外情形

## 关键节点

- 生效时间
- 修订记录
- 衔接文件（superseded_by）
```

## 事件型模板（meeting-summary/conference-summary/activity）

适用：会议纪要、活动记录、访问记录

```markdown
---
title: "事件标题"
type: meeting-summary | conference-summary | activity
sources:
  - raw/xxx
source_type: official-doc | speech-recognition | ocr
date: YYYY-MM-DD
department: "主办部门"
status: active | confirmed | completed
confidence: high | medium
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[policies/相关政策]]"
---

# 事件标题

> **时间**：YYYY-MM-DD | **地点/形式**：XX | **主办**：XX

## Navigation

面向 query 的 2-4 句导航（约 80-150 tokens）：说明什么事件/事项、时间、参与者或部门、核心议题和可能的查询表达（如“申报”“评审”“后续任务”）；来源明确时补充决定、状态和关联文件。只能依据 raw，不新增事实；不要复制讨论正文。

## 参与者

- 张明远（角色）
- 张三（角色）

## 讨论记录

- 议题1：要点
- 议题2：要点

## 决策记录

- 决定1：内容（决定≠讨论）
- 决定2：内容

## 行动项

| 任务 | 负责人 | 截止时间 |
|------|--------|----------|
| XX | 张明远 | YYYY-MM-DD |

## 结果/产出

- 主要结果
```

## 实体型模板（profile/reference/speech）

适用：个人档案、简历、参考资料、发言稿

```markdown
---
title: "标题"
type: profile | reference | speech
sources:
  - raw/xxx
source_type: official-doc | speech-recognition | ocr
date: YYYY-MM-DD
department: ""           # 发言稿可有
speaker: ""              # speech 类填
status: active | final
confidence: high | medium
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[相关页面]]"
---

# 标题

> **核心信息**：一句话概括

## Navigation

面向 query 的 2-4 句导航（约 80-150 tokens）：说明这是什么资料、核心主题/属性、常见查询表达；来源明确时补充人物、部门、时间、状态和关联文件。只能依据 raw，不新增事实；不要复制正文。

## 属性

- 属性1
- 属性2

## 关键内容

### 段落1
- 内容
```

## 变更说明
- v5(2026-07-27): 新增行政模板，按语义原型分流（规范型/事件型/实体型）
- 规范型加 effective_from/effective_to/applicable_to 槽位
- 事件型拆"讨论记录/决策记录/行动项"三段
- 实体型保留单节点属性结构
- 删 paper 模板的 Core Triples 段（v4 废弃）
