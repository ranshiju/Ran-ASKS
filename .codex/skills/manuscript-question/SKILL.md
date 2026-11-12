---
name: manuscript-question
description: 凝练或核验论文核心科学问题，兼顾科学性、概念清晰与语言精炼，区分一般科学问题、本文切入点和已验证范围。适用于研究定位与问题措辞讨论；贡献分类和评分使用 manuscript-evaluation，实际改稿使用相应写作能力。
---

# 论文核心科学问题凝练

在 WikiGraph 仓库根目录运行：

```bash
python3 .scripts/route.py --capability manuscript-question
```

按派发的[核心科学问题凝练规范](../../../operations/research/manuscript-question.md)执行；它是唯一方法维护源。宿主 Agent 核对稿件与已认可定位，给一条首选问题和必要的本文切入点、证据边界说明。

其他智能体可使用同一 CLI，或直接读取上述相对路径的规范。只凝练问题时不自动评分、改稿或启动外部模型；用户同时要求贡献评估时复用问题定位并组合论文评估能力。
