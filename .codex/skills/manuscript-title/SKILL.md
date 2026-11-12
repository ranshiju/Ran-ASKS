---
name: manuscript-title
description: 根据核心科学问题、科学贡献及全文内容拟定、优化或比较学术论文标题，提供候选与首选理由并校准证据范围。适用于取标题、改标题和中英文题名讨论；单独凝练问题或评分贡献使用对应能力，整篇改稿使用 manuscript-improvement。
---

# 论文标题拟定

在 WikiGraph 仓库根目录运行：

```bash
python3 .scripts/route.py --capability manuscript-title
```

按派发的[标题规范](../../../operations/research/manuscript-title.md)与共享落笔约定执行。宿主 Agent 综合已认可的核心问题、贡献排序和全文证据，突出有证据支持的科学进展，给出候选及首选理由；英文稿以英文质量为准并提供中文对应，材料不足时明确暂定范围。

其他智能体可使用同一 CLI，或直接读取标题规范及其引用的共享写作章节。复用有效阅读和既有定位，按请求交付标题建议或落地修改，不自动扩展为整篇改稿。
