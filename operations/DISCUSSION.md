# Discussion（学术讨论存档）操作规范


---

## 触发方式

| 触发语 | Agent 行为 |
|--------|-----------|
| "开始学术讨论：[主题]" | 确认主题，后续对话视为学术讨论；不主动写入文件 |
| "归档讨论" / "总结讨论并摄入" | 总结讨论 → 创建 raw 文件 → 执行 Ingest |
| "退出讨论模式" | 停止学术讨论行为，不归档 |
| "把刚才的讨论总结并摄入" | 从对话历史提取讨论内容 → 创建 raw → 执行 Ingest |

---

## 讨论期间行为规范

- **主动搜索**：随时通过互联网检索相关学术论文、技术报告、开源实现等
- **信息整合**：将检索结果与当前讨论关联，补充背景、指出争议或研究空白
- **提出新想法**：基于检索结果主动提出新的研究方向和改进思路
- **来源透明**：引用时注明论文标题、作者、年份或链接
- **讨论过程中不写入文件**：结束后先创建 raw，用户确认后再 Ingest

---

## 归档执行流程

1. **提取讨论内容**：从对话历史中提取主题、关键论点、结论、纠正记录
2. **创建 raw 文件**：写入 `academic/raw/discussions/YYYY-MM-DD-主题slug.txt`
   - 记录完整思辨过程（含 AI 原始错误表述、用户纠正、逐轮修正轨迹）
   - 参与者标注为"与 AI 协作讨论"
   - 包含"讨论过程纠正记录"章节（原始表述 → 修正后表述）
3. **用户确认**：展示 raw 文件内容，等待确认
4. **执行 Ingest**：按标准 Ingest 流程创建/更新 wiki 页面
   - 同一主题始终只有一页 wiki，新讨论追加到 `sources`
   - wiki 仅保留结论，不含讨论过程纠正
5. **更新索引和日志**：更新 `academic/wiki/index.md` 和 `academic/wiki/log.md`

---

## Raw 文件命名

`raw/discussions/YYYY-MM-DD-主题slug.txt`，每次新讨论产生新 raw，不修改旧 raw。

## Raw 与 Wiki 的内容分工

- **Raw 保留过程**：完整思辨过程，含错误表述和纠正记录
- **Wiki 仅保留结论**：最终学术结论，不含迭代过程
- 用 `tags` 区分：`#paper-discussion`（单篇论文）/ `#topic-discussion`（主题）

## 文献引用规范

raw 文件中必须记录唯一标识符，定稿前核实：

- **arXiv**：记录 ID（如 `arXiv:2204.11428`）+ 链接 `https://arxiv.org/abs/XXXX.XXXXX`
- **已发表论文**：记录 DOI（如 `DOI: 10.1145/...`）+ 链接 `https://doi.org/XXXX`
- 两者兼有时均记录

---

## Discussion Wiki 页面模板

```markdown
---
title: "讨论主题"
type: discussion
sources:
  - raw/discussions/YYYY-MM-DD-主题slug.txt
date: YYYY-MM-DD
tags: [paper-discussion | topic-discussion, 方向标签]
confidence: medium
created: YYYY-MM-DD
updated: YYYY-MM-DD
related:
  - "[[papers/相关论文]]"
  - "[[concepts/相关概念]]"
---

# 讨论主题

> **讨论日期**：YYYY-MM-DD | **参与者**：张明远、AI
> **讨论类型**：论文讨论 / 主题讨论

## 背景
- 讨论的起因和上下文

## 讨论要点
### 要点一
- ...

### 要点二
- ...

## 结论与行动
- 达成的共识
- 待解决问题
- 后续行动
```
