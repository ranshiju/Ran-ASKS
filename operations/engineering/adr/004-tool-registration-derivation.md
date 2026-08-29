# ADR-004：工具注册派生与 DSH 命名对齐

> 状态：已批准（2026-08-20）
> 影响：query_orchestrate.py / query_actions.py / dsh/tools.py / dsh/test_dsh_harness.py / code-guidance.md / graph.yaml

## 背景

查询流水线的 8 个工具（graph_search/graph_neighbors/graph_relations/graph_hub_of/read_section/read_raw/admin_recall/wiki_recall）此前散落在 `query_orchestrate.py` 的 10 处硬编码（ALLOWED set、去重 set、record_visit set、allowed_next_actions 列表、_build_step if/elif 链、--action choices、_DISCOVERY set、STAGE_ACTIONS 等）。

DSH 适配层 `dsh/tools.py` 另有独立注册表 `build_tools()`，使用不同命名（lookup/neighbors/relations/hub_of/abbr）。两层完全互不引用，`grep_abbreviations` 和 `abbr` 两个死工具的清理暴露了跨层漂移问题：前序模型声称删了 `grep_abbreviations`，实际漏了 5 处残留，还留了个孤立 `def` 破坏语法；`abbr` 死工具在 DSH 层未被任何测试捕获，运行时必崩但 35 个测试全 PASS。

## 决策

- **`_ACTION_SIGS` 签名派生**：从 `query_actions` 的 8 个函数签名自动派生 `action→[(param, default)]`，驱动 `_build_step`（输入组装）、`ALLOWED`（合法性白名单）、`allowed_next_actions`（下一步候选）、去重 set、`--action choices`。新增工具只需加函数 + `DISPATCH` 注册，5 处硬编码自动适配。
- **DSH 命名对齐**：`dsh/tools.py` 的工具命名从旧的 wg.py CLI 风格（lookup/neighbors/relations/hub_of）改为与 `query_actions.DISPATCH` 完全对齐（graph_search/graph_neighbors/graph_relations/graph_hub_of）。
- **双路调用**：查询类工具经 `_qa_call` 直接调 `query_actions` 函数（避免子进程开销），CLI 类工具（recall/remember）仍经 `_wg_call` 调 `wg.py` 子进程。
- **死工具清除**：删除 `grep_abbreviations`（frontmatter `abbreviations` 字段已 v4 主数据化到 graph.db aliases 表）和 DSH 层 `abbr`（调用已删的 wg.py abbr 子命令）。

## 不采用

- **集中式注册表装饰器**（`@tool(stages=[...], is_discovery=True)`）：当前 8 个工具稳定已久，增删频率极低，装饰器引入的间接层复杂度超过收益。`_ACTION_SIGS` 签名派生已消除最易漂移的硬编码点。
- **DSH 真实运行时接入**（TypeScript/Cordis）：Python 适配层已覆盖核心概念（ToolRegistry/Hook 瀑布/Session log/Guard），接入真实 DSH 的桥接成本在当前工具生态复杂度下不划算。
- **两套 API 模式合并**（query_orchestrate._api_query_loop + dsh.AgentLoop）：两套 guard 互补不冲突，合并需将 STAGE_ACTIONS/预算/回环改写为 pre-execute hook，属中长期工作。

## 验证

- 10 套回归测试全 PASS（9 套 ingest/query + DSH 35 项）
- `rg "grep_abbreviations|cmd_abbr" --type py` 返回零结果
- `dsh/tools.py:build_tools()` 返回 10 个工具，命名与 `query_actions.DISPATCH` 对齐
- `_ACTION_SIGS` 烟雾测试验证 `_build_step` 参数组装与旧行为等价
