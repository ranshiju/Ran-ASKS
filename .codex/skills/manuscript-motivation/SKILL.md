---
name: manuscript-motivation
description: 围绕论文标题、核心科学问题与贡献，综合稿件和文献证据构建简练有力的背景动机，交付摘要用一至两句话及引言用两至三段。适用于背景动机撰写、优化与原稿比较；完整摘要或全文修改使用对应写作能力。
---

# 论文背景与动机构建

在 WikiGraph 仓库根目录运行：

```bash
python3 .scripts/route.py --capability manuscript-motivation
```

按派发的[背景与动机规范](../../../operations/research/manuscript-motivation.md)和共享落笔约定执行。宿主 Agent 围绕最新认可的标题、核心科学问题与贡献构建动机，核验相关权威文献和研究范围，交付两种长度的候选文字，附必要引用与真实缺口。

其他智能体可使用同一 CLI，或直接读取规范及其引用的共享写作章节。复用已有定位和有效证据，实际稿件留在用户项目中，不因应用功能自动覆盖主文或将其公开。
