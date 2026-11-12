---
name: manuscript-introduction
description: 基于已有稿件或研究材料，围绕标题、核心科学问题与贡献撰写、重组或评估完整引言，结合背景动机及核验文献交代本文切入点与科学意义。单独背景动机或完整摘要使用对应能力。
---

# 学术论文引言撰写与修改

在 WikiGraph 仓库根目录运行：

```bash
python3 .scripts/route.py --capability manuscript-introduction
```

按派发的[引言规范](../../../operations/research/manuscript-introduction.md)及共享落笔约定执行。有稿件时以原稿为基础或参考，复用有效阅读和已认可定位；最后与原稿对比验收，修正确认的退步，交付带引用的完整引言及简短对比结论。讨论不自动改稿，应用默认保存候选，授权替换时按项目版本约定落实。

其他智能体可调用同一 CLI 或直接读取规范。方法在仓库内维护，稿件留在用户项目，宿主 Agent 完成语义工作，不依赖外部模型 API。
