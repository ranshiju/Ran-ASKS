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

`## Navigation`(导航概述 80-200 tokens)/ `## Core Triples`(路由级关系 3-8 条)/ `## Content`(正文,子标题降三级)。防退化五规则与正文迁移见 `academic/SCHEMA.md`。

### 关系级元数据

每条 Core Triples 可带行内方括号 edge confidence [可追溯|推断|存疑] + 花括号来源元数据 {authority; temporal}(confidence 移至方括号,独立判断不继承页面级)。详细规则见 `academic/SCHEMA.md`「关系级元数据」。图由 `.scripts/graph_build.py` 从各页 Core Triples 段派生重建。
