# ADR-002：纯 API 摄入使用程序证据卡与局部修复

## 背景

弱 API 模型可节省 Agent token，但在开放式摄入中容易混淆原文定位、改写逐字引文、超出字段长度，或把多个证据合并为一条声明。要求 Agent 逐篇审查会抵消成本优势；允许模型直接写 wiki/图会破坏 raw 可回溯边界。

## 决策

- `INGEST_BACKEND=api` 的论文语义一律经 `.scripts/api_ingest.py`，而不是自由生成页面或三元组。
- 程序从 `paper.md` 的摘要和讨论段切分带编号证据卡；模型只能输出 `field + claim + evidence_id`，每条只选一张卡。
- 程序由卡片附回 `raw_locator` 与逐字 `evidence_quote`，并校验字段白名单、长度、条目上限、候选关键词和 JSON Schema。
- 对超长或多卡编号的失败声明，只执行一次局部修复；同字段超限但其余声明合规时裁剪超限项并带 WARN 继续。修复后仍不合格、或草稿为空/结构失败，则写入 `cross-domain/api-ingest-pending.jsonl`；后续受限 Agent 兜底成功时追加可审计的 `resolved` 记录。
- 只有 `complete=true` 的草稿可编译到新的占位 wiki 骨架，并生成受控 semantic 槽；图仍只由 `graph_ingest.py` 写入。
- 常规成功/失败路径都不要求 Agent 即时接管：成功路径的目标是 Agent token 为零，失败路径保守地缺少语义增强而不污染知识库。

## 后果

- 优点：证据定位由程序控制；模型差异主要表现为少产出或进入 pending；可通过字段级修复提高完成率；保持图写入单入口。
- 代价：每篇最多增加一次局部修复 API 调用；初始证据卡覆盖受摘要/讨论切片和卡片上限约束。
- 不自动处理：新 Hub、谓词、通讯作者、无 raw 支撑的事实、跨文献推断；这些仍需后续治理或更高权限路径。

## 验证

运行 `python3 .scripts/test_api_ingest.py`、`python3 .scripts/engineering_graph.py validate`。真实摄入仅在 `complete=true` 后运行 `ingest_check.py --graph <page>`。
