# admin/ — 页面类型与摄入规范

> 摄入前先读 `operations/INGEST.md`。共享命名惯例见 `operations/shared-conventions.md`。

---

## 子目录结构

| 子目录 | 用途 | 文件命名规范 |
|--------|------|-------------|
| `policies/` | 政策文件、规章制度 | 保持原文件名 |
| `meetings/` | 会议纪要 | `YYYY/MMDD.ext`（见 `operations/shared-conventions.md`） |
| `speeches/` | 发言稿、讲话稿 | `主题 -YYYYMMDD.ext` |
| `activities/` | 活动方案、项目征集 | 保持原文件名 |
| `applications/` | 申请书、请示报告 | `YYYY/主题 - 申请人.ext`（按年份分目录） |
| `profile/` | 个人档案、简历 | 保持原文件名 |
| `forms/` | 表单、模板、表格 | 保持原文件名 |
| `references/` | 参考资料、外部链接 | 保持原文件名 |
| `reports/` | 报告、总结 | 保持原文件名 |
| `量子信息科学专业/` | 专业申报相关材料 | 保持原文件名 |

---

## 申请书处理要点（`applications/`）

- wiki 页面只记录摘要，不逐字转录原文（敏感性）
- 标注处理状态（待处理/已批准/已驳回/已执行）
- 记录申请事由、申请人、日期、请求内容、状态

---

## 页面类型

| 页面类型 | type 值 | 存储位置 | 说明 |
|----------|---------|----------|------|
| 政策解读 | `policy` | `wiki/policies/` | 规章制度、政策文件解读 |
| 操作流程 | `procedure` | `wiki/procedures/` | 办事流程、操作指南 |
| 决策记录 | `decision` | `wiki/decisions/` | 重要决策及背景 |
| 会议摘要 | `meeting-summary` | `wiki/meetings/` | 会议纪要摘要 |
| 发言稿 | `speech` | `wiki/speeches/` | 发言稿、讲话稿摘要 |
| 活动方案 | `activity` | `wiki/activities/` | 活动方案、项目征集摘要 |
| 申请书 | `application` | `wiki/applications/` | 申请书摘要（仅摘要，不转录） |
| 个人档案 | `profile` | `wiki/profile/` | 个人简历、档案摘要（资料页，非人物图节点；人物统一归 `academic/wiki/authors/`，见 `academic/SCHEMA.md` people 段） |
| 参考资料 | `reference` | `wiki/references/` | 参考资料、外部信息摘要 |
| 时间线 | `timeline-entry` | `wiki/timeline.md` | 行政事务时间线条目 |
| 网页资料 | `web-reference` | `wiki/web-references/` | 网页来源的行政参考资料（raw 存 `raw/web-references/YYYY/`，规范见 `academic/SCHEMA.md` 与 `operations/shared-conventions.md`） |

## Frontmatter 模板

```yaml
---
title: "文件/事项标题"
type: policy | procedure | decision | meeting-summary | timeline-entry | timeline-summary | speech | activity | application | profile | reference  # v4 加 timeline-summary(派生聚合页)
sources:
  - raw/policies/filename.md
source_type: official-doc | speech-recognition | ocr | web | discussion
date: YYYY-MM-DD
url: "原始链接"            # 外部来源必填
status: active | deprecated | draft | completed | confirmed | final   # 按 type 取值;版本演进用 deprecated(配合 superseded_by);expired 已废弃改用 deprecated
superseded_by: "[[新版页面]]"   # 仅 status: deprecated 时填,指向替代页面
department: "部门名称"
# (v4,2026-07-25) tags/keywords/aliases/abbreviations 字段已删,并入 graph.db(aliases 表)/邻接覆盖
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[policies/相关政策]]"
  - "[[decisions/相关决策]]"
---
```

## status 取值规则(含版本演进)

`status` 按 type 分义,版本演进场景统一用 `deprecated`:

| status | 适用 type | 含义 |
|--------|----------|------|
| `active` | policy/procedure/decision/activity/speech/reference/profile | 现行 |
| `draft` | 各 type | 草稿未定稿 |
| `confirmed` | meeting-summary | 会议确认 |
| `completed` | activity | 活动已完成 |
| `final` | speech | 发言稿定稿 |
| `deprecated` | 各 type(版本演进) | 旧版已废止,配合 `superseded_by` 指向新版 |

**版本演进处理**:新旧内容都合法时(如政策更新:十四五→十五五),旧页标 `deprecated` + 填 `superseded_by`,新建 active/current 页,Hub 时间线串联。`deprecated` ≠ 删除,保留可追溯(与遗忘策略一致)。

## source_type 取值规则

详见 `academic/SCHEMA.md`。admin/business 域默认 `official-doc`（正式文档）；若摄入语音识别纪要则标 `speech-recognition`。

---

## 标准 section 结构

> wiki 内容页正文统一 section 结构,支持 section-level retrieval 省 token。详细规则见 `academic/SCHEMA.md`「标准 section 结构」节;工具 `.scripts/read_section.sh`;查询规则见 `operations/QUERY.md`「section 读取」。

### 本子项目标准 section

`## Navigation`(导航概述 80-200 tokens)/ `## Content`(正文,子标题降三级)。防退化五规则与正文迁移见 `academic/SCHEMA.md`。

### graph.db 边写作约束（v4，2026-07-25 主数据化）

- 只保留行政导航级关系与稳定主题；文件细节、数字、背景叙述和执行过程留在 `## Content`。
- 边只写入 `graph.db`：摄入时用 `graph_ingest.py ingest --page <wiki页> --semantic <语义槽>` 增量入图；不再写 Markdown `Core Triples`，也不使用 `graph_build.py` 重建。
- 行政页面的语义槽使用 `行政主题`（最多15个）和 `行政关系`（最多8条）；关系谓词限 `涉及`、`讨论`、`形成决策`、`依据`、`替代`、`汇报`、`发布者`、`负责人`、`承办部门`、`推动`、`申请事项`、`适用对象`。模板由 `graph_ingest.py prefill --page <wiki页>` 输出。
