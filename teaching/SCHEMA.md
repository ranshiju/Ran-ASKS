# teaching/ — 页面类型与摄入规范


---

## 页面类型

| 页面类型 | type 值 | 存储位置 | 说明 |
|----------|---------|----------|------|
| 课程总览 | `course` | `wiki/courses/` | 课程大纲、教学目标 |
| 知识点 | `topic` | `wiki/topics/` | 单个知识点详解 |
| 课时摘要 | `lecture` | `wiki/lectures/` | 每节课内容摘要 |
| 考核方案 | `assessment` | `wiki/assessments/` | 考试/作业方案 |
| 教学反思 | `pedagogy` | `wiki/pedagogy/` | 教学法总结与改进 |
| 网页资料 | `web-reference` | `wiki/web-references/` | 网页来源的教学参考资料（raw 存 `raw/web-references/YYYY/`，规范见 `academic/SCHEMA.md` 与 `operations/shared-conventions.md`） |

## raw 存储规范

每门课在 `raw/courses/<course-slug>/` 下建独立子文件夹管理该课程全部原始材料（大纲/日历/考试大纲/开课通知/课程必读等）。course-slug 用课程英文名 kebab-case（如 `electrodynamics`、`ai-introduction`、`ai-general`）。

## Frontmatter 模板

```yaml
---
title: "课程/知识点名称"
type: course | topic | lecture | assessment | pedagogy
sources:
  - raw/courses/filename.md
date: YYYY-MM-DD            # 来源日期；无可证日期时写 null
# date_status: unknown      # 仅 date: null 时必填；不得用 created 代替来源日期
course: "所属课程名称"
semester: "2025-2026-1"
# (v4,2026-07-25) tags/keywords/aliases/abbreviations 字段已删,并入 graph.db(aliases 表)/邻接覆盖
prerequisites:
  - "[[topics/前置知识点]]"
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

---

## 标准 section 结构

> wiki 内容页正文统一 section 结构,支持 section-level retrieval 省 token。详细规则见 `academic/SCHEMA.md`「标准 section 结构」节;工具 `.scripts/read_section.sh`;查询规则见 `operations/QUERY.md`「section 读取」。

### 本子项目标准 section

`## Navigation`（导航概述 80-200 tokens）/ `## Content`（正文，子标题降三级）。结构校验规则见 `academic/SCHEMA.md`。

### 关系存储

语义执行单元只提交受限关系提案；程序经 knowledge IR、证据与 Schema 校验后，由 `.scripts/graph_ingest.py` 写入 graph.db。Wiki 正文用 Raw 脚注承载事实证据，不保存关系段副本。
