# business/ — 页面类型与摄入规范


---

## 页面类型

| 页面类型 | type 值 | 存储位置 | 说明 |
|----------|---------|----------|------|
| 商业计划 | `plan` | `wiki/plans/` | 商业计划书、创业方案 |
| 市场调研 | `research` | `wiki/research/` | 市场分析、行业报告、趋势洞察 |
| 竞品分析 | `competitor` | `wiki/competitors/` | 竞争对手分析、对比研究 |
| 战略决策 | `strategy` | `wiki/strategies/` | 商业模式、增长策略、战略规划 |
| 项目管理 | `project` | `wiki/projects/` | 项目方案、里程碑、复盘总结 |
| 会议纪要 | `meeting-summary` | `wiki/conferences/` | 商业会议、商务洽谈记录 |
| 合同摘要 | `contract` | `wiki/contracts/` | 合同、协议要点摘要 |
| 财务记录 | `financial` | `wiki/financials/` | 财务报表、预算、收支分析 |
| 网页资料 | `web-reference` | `wiki/web-references/` | 网页来源的商业参考资料（raw 存 `raw/web-references/YYYY/`，规范见 `academic/SCHEMA.md` 与 `operations/shared-conventions.md`） |

## Frontmatter 模板

```yaml
---
title: "计划/报告/项目名称"
type: plan | research | competitor | strategy | project | meeting-summary | contract | financial
sources:
  - raw/plans/filename.md
source_type: official-doc | speech-recognition | ocr | web | discussion
date: YYYY-MM-DD
status: active | completed | archived | draft
domain: "行业/领域"
# (v4,2026-07-25) tags/keywords/aliases/abbreviations 字段已删,并入 graph.db(aliases 表)/邻接覆盖
confidence: high | medium | low
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[research/相关调研]]"
  - "[[competitors/相关竞品]]"
  - "[[strategies/相关策略]]"
---
```

## source_type 取值规则

详见 `academic/SCHEMA.md`。admin/business 域默认 `official-doc`（正式文档）；若摄入语音识别纪要则标 `speech-recognition`。

---

## 标准 section 结构

> wiki 内容页正文统一 section 结构,支持 section-level retrieval 省 token。详细规则见 `academic/SCHEMA.md`「标准 section 结构」节;工具 `.scripts/read_section.sh`;查询规则见 `operations/QUERY.md`「section 读取」。

### 本子项目标准 section

`## Navigation`(导航概述 80-200 tokens)/ `## Core Triples`(路由级关系 3-8 条)/ `## Content`(正文,子标题降三级)。防退化五规则与正文迁移见 `academic/SCHEMA.md`。

### 关系级元数据

每条 Core Triples 可带行内方括号 edge confidence [可追溯|推断|存疑] + 花括号来源元数据 {authority; temporal}(confidence 移至方括号,独立判断不继承页面级)。详细规则见 `academic/SCHEMA.md`「关系级元数据」。图由 `.scripts/graph_build.py` 从各页 Core Triples 段派生重建。
