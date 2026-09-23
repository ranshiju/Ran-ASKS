# ADR-007：统一运行时功能注册与多执行适配器

> 状态：已批准（2026-09-21）
> 影响：`function-registry.yaml`、`function_registry.py`、`route.py`、`wg.py`、`dsh/function_catalog.py`、DSH ToolDefinition

## 背景

系统的用户功能、持续状态和执行工具此前分散在 `route.py`、`wg.py`、六组 DSH factory、操作规范和独立 CLI 中。`route.py --list` 还把 task 与 state 混列；DSH 工具只能在各 factory 内发现；固定管线、Host Agent 工具和 API worker 缺少统一身份。调用仍可工作，但无法从一个入口回答“该功能面向谁、由谁控制、允许谁调用、在哪个后端执行、会写哪些层”。

DSH 只服务 API backend 的程序控制循环。Agent backend 由当前宿主 Agent 持有控制循环，并用 `agent-task-v1` 与确定性代码交接。把全部功能、sub-agent、worker 和固定管线迁入 DSH 会混淆控制权，并把已有事务状态机降格为动态工具编排。

## 决策

1. `operations/config/function-registry.yaml` 是运行时功能、状态、调用者策略和入口绑定的唯一管理目录；它不是工作状态，也不替代工程元图。
2. canonical function ID 跨 Route、`wg.py`、DSH、CLI、固定管线和 worker 保持稳定。一个用户功能可以绑定多个执行工具；工具数量不再等于用户功能数量。
3. `route.py --list` 与 `wg.py functions list/show/resolve/validate` 从注册表发现功能。Route 的执行 section 和 handler 仍在代码中，由 runtime validator 检查与注册表完全一致；工程元图不再另行解析 Route 清单，只消费注册表的受管表面。
4. 注册表统一列出 DSH provider；所有生产 DSH 工具通过 `dsh/function_catalog.py` 绑定 canonical function ID、audience、caller、effect 和 maturity。未登记工具、重复 provider 或跨 factory 重名均失败；DSH session log 记录 function ID。
5. 固定管线继续由代码持有步骤、状态、恢复、事务和回滚；注册表只登记入口、控制者、协议和副作用。管线不得通过通用动态 invoke 绕过专用 validator/commit。
6. 主 Agent 可解析用户功能并启动管线；sub-agent 只能调用注册表允许的有界功能，不能直接启动事实写入管线；worker 仅由 API controller 或 pipeline 调用，不向用户暴露，也不拥有工具发现和委派权。
7. Agent backend 解析结果不包含 DSH binding；API backend 才投影 DSH 工具。共享 schema、validator、IR、报告、提交与回滚不因适配器变化而分叉。
8. `engineering_graph.py validate` 必须执行 runtime registry coverage，并将注册表中的 Route task/capability 与工程能力包对账，阻止 Route、WG、DSH 或工程能力形成第二套清单。
9. 注册表使用拒绝重复键的严格 YAML 解析。provider locator 仅用于受限 `dsh.*` factory 的运行时盘点，不是通用 import/shell handler；可执行 handler 仍由代码显式定义。

## 不采用

- **DSH 作为全系统运行时**：会让 Host Agent 与 API controller 形成重叠控制循环，并弱化固定管线事务。
- **在工程元图中兼任运行时注册表**：工程图表达组件责任和影响关系，运行时目录表达调用策略；合并会让两个变化节奏不同的契约互相污染。
- **YAML 配置任意 import 或 shell handler**：注册表只保存受校验的元数据和路径；可执行 handler 仍由代码显式映射。
- **通用 invoke 取代专用入口**：摄入、PPT、Hub 生命周期等写操作必须继续经过各自的参数校验、授权和事务入口。
- **复制每条管线步骤到 YAML**：代码和测试仍是固定控制流的真理源，注册表不建立第二套工作流定义。

## 验证

```bash
python3 .scripts/function_registry.py validate --runtime
python3 .scripts/test_function_registry.py
python3 .scripts/test_prompt_audit.py
python3 .scripts/test_wg.py
python3 dsh/test_dsh_harness.py
python3 .scripts/engineering_graph.py validate
```

当前注册表覆盖 24 个功能、3 个持续状态、15 个 `wg.py` 顶层命令和 38 个 DSH 工具。新增或删除 Route、WG、DSH 入口时，必须在同一改动中更新注册表和针对性回归。
