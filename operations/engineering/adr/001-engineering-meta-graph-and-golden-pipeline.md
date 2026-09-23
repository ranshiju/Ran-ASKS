# ADR-001：工程元图与可重放金样的分工

## 背景

LLM 需要低 token 地理解工程意图，但脚本、配置与真实写入行为可能漂移。单靠长文档会过时；单靠静态代码图又无法表达 raw 红线、任务规则和数据真理源。

## 决策

- `operations/engineering/graph.yaml` 是**目标工程图**：声明任务能力包、关键节点、I/O 契约、影响面和最小验证映射。
- `.scripts/engineering_graph.py validate` 做低成本漂移检查；`impact <node> --verify` 输出最小回归集；`contract <node>` 输出单脚本短契约。
- `test_ingest_pipeline.py` 是**实际行为金样**：在临时仓库重放骨架、语义、写图和图校验，不触碰真实 raw 或 `graph.db`。

## CRG 融合边界（2026-09）

工程管理继续以 [code-review-graph](https://github.com/tirth8205/code-review-graph) 的持久结构、最小上下文和影响半径思想为主要外部参考，并吸收 v2.3.9 的结果诚实性契约：

- `impact` 既接受单个 canonical 目标，也接受显式文件、工作树、暂存区或分支基线变更集；分支比较采用 merge-base。
- 变更发现有独立超时，Git 失败或超时返回 error，不得降级为“无变化”。
- 输出遵循 `engineering-impact-v1`，区分已映射工程路径、未注册工程路径和工程审查范围外的知识/项目状态路径，并报告总量、返回量、遗漏量、截断和分析基线；未注册工程路径使结果返回 `partial`，范围外路径只记录地址且不读取内容。
- `verification` 中 seed 节点声明的测试是 `direct`，由两跳影响节点带入的是 `related`；后者只提示回归相关性，不证明直接覆盖。
- `graph.yaml` SHA-256、当前 Git HEAD 与 manifest dirty 状态只描述本次分析基线，不虚构持久 freshness 状态。

本项目融合的是上述契约，不引入 CRG 的 Tree-sitter 代码图、SQLite AST 存储、MCP、watch daemon、embedding 或写工具。原因是 WikiGraph 工程图表达规范、实现、测试和责任关系，必须保留人工治理语义；工程影响分析仍是一次调用即出产物的只读工具。

## Open Code Review 评估（2026-09）

评估 Alibaba Open Code Review v1.12.7 后，当前不引入运行时或 Codex 插件。其 delegation mode 的文件选择、merge-base、排除逻辑、规则分组和覆盖清单与 `engineering-impact-v1` 大量重合；默认规则还会排除 `test_*.py`、`*_test.py` 等测试文件，必须通过项目 `include` 显式覆盖，而本项目把测试视为工程影响闭包的一等节点。让 OCR 再决定审查范围会形成第二个范围真理源。

OCR 的剩余独立价值是通用语言审查规则和结构化行级 finding 格式。只有在固定 WikiGraph 变更样本上证明它相对“工程元图 + 当前宿主 Agent”提高缺陷检出或定位准确率，且满足以下边界时，才重新考虑只读 delegation 试点：

- 审查文件列表由 `engineering-impact-v1.resolved_files` 提供，OCR 不重新扩大范围；
- Raw、Wiki、项目材料、状态数据库和 temp 始终排除；测试文件必须显式纳入；
- OCR 不调用自己的 LLM 控制循环、不自动修复、不写 session 状态进仓库；
- finding 必须回到工程元图 seed、direct/related 验证和实际 diff 复核后才能采纳。

## 不采用

不以 Tree-sitter 全仓代码图、Structurizr、Import Linter 或 Dagster 作为当前依赖：它们不能直接表达本项目的操作规范，且维护/上下文成本超过当前规模收益。Open Code Review 当前同样不作为依赖；外部代码审查器只能消费工程元图产出的有界上下文，不能成为工程责任关系或验证覆盖的真理源。

## 验证

运行 `engineering_graph.py validate`、`test_engineering_graph.py`、目标或变更集的 `impact --verify`，以及真实摄入页的 `ingest_check --graph`。工程元图是最小起点；`partial`、未注册路径和 `related` 验证必须显式处理。
