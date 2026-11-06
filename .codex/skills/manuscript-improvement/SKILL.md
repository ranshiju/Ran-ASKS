---
name: manuscript-improvement
description: 系统改进学术论文，由 Agent 通读主文、图表和补充材料，再依次改进摘要、标题、引言和全文，并回查贡献与证据的一致性。适用于整篇论文改进、围绕创新点重组论证和接续多轮改稿；单独摘要修改使用 academic-abstract，指定稿件诊断报告使用 manuscript-diagnosis。
---

# 学术论文系统改进

在 WikiGraph 仓库内使用，流程的单一维护源是[论文改进规范](../../../operations/research/manuscript-improvement.md)。遵循仓库 playbook、路由与项目上下文恢复约定，保持 `research` 工作上下文。

- 讨论或评估：读取上述规范与论文，给出有依据的建议，不自动改文件。
- 实际改稿：加载下列 profile 并按返回规范完成交付；同一连续回合已加载则复用。

```bash
python3 .scripts/route.py --capability write --capability-profile academic-manuscript
```

宿主 Agent 自行通读全文后，按规范复用 `manuscript-evaluation`，先展示核心科学问题、贡献分类与重要性评分、证据强度和推荐主线，再按“摘要 → 标题 → 引言 → 全文”推进，最后回查一致性。定位确认按规范执行，准备阅读不能转交给用户，也不增加逐阶段批准；接续任务复用已完成阅读，保留用户已接受的贡献与标题选择。

摘要阶段按规范复用现有摘要功能，学科规则按需组合。流程不调用额外模型、不自动启动独立评审；局部修改不自动扩大范围。交付修改文件、关键变化、适用的 diff 与构建检查结果，并如实报告无法通过写作补齐的证据缺口。
