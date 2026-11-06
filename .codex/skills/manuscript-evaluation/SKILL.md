---
name: manuscript-evaluation
description: 评估论文的核心科学问题与贡献，逐条分类、给出重要性评分、证据强度和强调顺序。用于论文评估、创新点排序、审稿说服力分析及改稿前定位；指定稿件诊断报告沿用 manuscript-diagnosis，第三方引用评价使用 paper-impact。
---

# 论文评估

在 WikiGraph 仓库内使用。遵循 playbook、路由和项目上下文恢复约定，在当前 `research` 工作上下文加载：

```bash
python3 .scripts/route.py --capability manuscript-evaluation
```

按返回的[论文评估规范](../../../operations/research/manuscript-evaluation.md)执行；它是分类、评分、证据强度与改稿前主线讨论的单一维护源。

宿主 Agent 阅读指定论文及必要补充材料，先提出核心科学问题，再交付逐项贡献分类、重要性评分与理由、证据定位、主要缺口及强调建议。重要性和证据强度分开判断；分数用于安排重点，不是录用概率。缺失的比较证据如实标注，不用高分替代文献核验。

只要求评估时不改稿；用户要求保存时才写入授权项目目录。摘要和全文修改复用评估结论，依照规范处理主线确认并沿用已接受定位，不重复评分、不新增 API 或独立评审。
