# Paper-Summary 内容模板

> Ingest 论文时使用此模板创建 `wiki/papers/` 页面。
> 也可由 `.scripts/wiki_skeleton.py` 程序生成骨架(推荐,防格式手写错误)。

```markdown
---
title: "论文标题"
type: paper-summary
sources:
  - raw/references/<paper-id>/paper.md
source_type: official-doc
date: YYYY-MM-DD
venue: "arXiv:XXXX.XXXXX / Journal Vol, Page (Year)"
authors: ["第一作者", "通讯作者"]
confidence: high
status: current
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[papers/相关论文]]"
---

# 论文标题

> **作者**：XXX et al. | **发表**：Journal/Conference, Year
> **核心贡献**：一句话概括

## Navigation

2-4 句导航概述(80-200 tokens)。注意:本段文本末尾不得接 ## Content 标题,必须分行。

## Content

### 一、问题与动机
- 解决了什么问题
- 现有方法的不足

### 二、方法/框架
- 核心思路
- 关键技术点

### 三、主要贡献
1. 贡献一
2. 贡献二

### 四、实验/结果
- 主要实验或理论结果

### 五、局限与展望
- 已知局限
- 未来方向

```

**变更说明**:
- 删 `tags` 字段(v4 已删,功能由 graph.db 的 Hub 语义导航边和正文关键词列表覆盖)
- 删 `keywords` 字段(同上,功能被图邻接节点覆盖)
- section 从中文编号(一、二、三)改为标准三段:Navigation / Content / Core Triples
- Content 内子段保留中文编号降为 ### 三级
- 加"Navigation 不得接 ## Content"分行约束(防手写格式 bug)
- v5(2026-07-27): 删 Core Triples 段(v4 已废弃,边只在 graph.db,巩固阶段 graph_ingest 产临时 JSON 入库)
