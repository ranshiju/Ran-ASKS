# ADR-001：工程元图与可重放金样的分工

## 背景

LLM 需要低 token 地理解工程意图，但脚本、配置与真实写入行为可能漂移。单靠长文档会过时；单靠静态代码图又无法表达 raw 红线、任务规则和数据真理源。

## 决策

- `operations/engineering/graph.yaml` 是**目标工程图**：声明任务能力包、关键节点、I/O 契约、影响面和最小验证映射。
- `.scripts/engineering_graph.py validate` 做低成本漂移检查；`impact <node> --verify` 输出最小回归集；`contract <node>` 输出单脚本短契约。
- `test_ingest_pipeline.py` 是**实际行为金样**：在临时仓库重放骨架、语义、写图和图校验，不触碰真实 raw 或 `graph.db`。

## 不采用

不以 Tree-sitter 全仓代码图、Structurizr、Import Linter 或 Dagster 作为当前依赖：它们不能直接表达本项目的操作规范，且维护/上下文成本超过当前规模收益。

## 验证

运行 `engineering_graph.py validate`、目标节点的 `impact --verify`，以及真实摄入页的 `ingest_check --graph`。工程元图是最小起点，未覆盖变更仍须显式补查。
