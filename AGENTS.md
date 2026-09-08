# KR Wiki - 项目宪法

> 文件型综合知识库：Raw 是事实层，Wiki 是面向理解并桥接 Raw 的语义层，`graph.db` 只保存发现与到达所需的关键导航边。事实答案必须能回溯 Raw。

## 红线

- Agent 不直接修改 `raw/`；Raw 写入只经受管摄入事务。同源重提必须保留来源并记录 log。
- `cross-domain/graph.db` 是公共域边的唯一来源；private 使用物理隔离的 `private/graph.db`。不得用不存在的路径启动裸 `sqlite3`，图操作经 `.scripts/graph_lib.py` 及受管脚本完成。
- DSH session log、Agent 任务文件和其他 `temp/` 产物都不是事实源，不得替代 Raw、Wiki 或 `graph.db`，也不得绕过 schema、校验或摄入事务。
- 对用户目标负责，保持独立判断。依据事实、代码、契约与验证结果给出真实、专业的建议；当用户方案与目标、约束或可靠性冲突时，明确说明判断、依据和更合适的方案。

## 运行边界

- **Agent 模式（默认）**：当前宿主 Agent 持有控制循环，直接读取已定位输入并调用确定性脚本。程序负责机械工作、schema、validator、preflight、提交和回滚，不模拟或评价 Agent 的推理。
- Agent 需要生成暂存产物时，脚本返回 `agent-task-v1`：其中只包含输入、输出、协议、当前问题和后续命令。Agent 完成任务后继续调用原 validator/commit，不经过 DSH、`llm_structured` 或 `agent_required` 包装。
- **API 模式**：程序持有控制循环；DSH、`llm_structured`、模型重试、预算和受限 worker 只服务 API backend。
- 两种模式共享确定性内核：输入解析与定位、schema、validator、IR/Graph 计划、报告、事务提交与回滚。控制循环和语义执行 adapter 相互独立，Agent 路径不得隐式回落到 API。

## 指令路由

先运行：

```bash
python3 .scripts/playbook_dispatch.py '指令关键词'
```

命中 playbook 就按其执行；未命中再判断任务：

- 使用：摄入/加内容=`ingest`，查问=`query`，健康检查=`lint`，同步=`sync`，写作=`write`，待巩固扫描=`scan`，收件箱=`inbox`，聚合页=`hub`，项目研究=`research`，学术前沿=`frontier`。
- 建设：修改 `operations/`、`.scripts/`、`dsh/` 或项目基础设施=`build`。
- 其他：直接回答。

使用任务随后调用 `.scripts/route.py --task <task>`；建设任务先调用 `.scripts/route.py --task build` 加载轻量方法卡，再按“建设任务”执行 impact。路由卡提供当前任务边界，不代替工程文档。其中：

- `query` 从 `--query-stage start` 开始；候选定位后用 `evidence`，有明确缺口才用 `continue`，交付前用 `answer`。
- `ingest` 显式给出 `--subproject academic|admin|teaching|business`、`--mode create|update|batch`、`--content paper|other` 和 `--source-kind ordinary|meeting`；`create` 再给单个 `--stage 1|2|3`。`source-kind` 由内容语义决定，扩展名不构成会议来源判据。
- `research` 等 task 表示持续状态；实际落笔按需加载 `write` capability，不切换研究状态。`wg.py` 暴露可组合的结构化执行工具。

## 使用任务

- 接续项目任务先读对应的 `projects/*/notes/status.md`；其他使用任务完成参数判断后立即路由。
- 路由前只做补齐参数或定位目标所需的最小读取，不预读规范全文、SCHEMA 或脚本源码。
- 路由后以任务卡和派发规范为操作边界；只有任务卡要求、参数仍不确定或命令报错时才定向补读。
- 功能性任务调用已封装入口；Raw/Wiki locator 是事实证据地址，engineering locator 只服务建设定位。

## 建设任务

1. 运行 `python3 .scripts/engineering_graph.py impact <target> --verify` 建立影响卡。
2. 优先读取 impact 推荐的 graph contract、capability 和 code-guidance 精确 locator；推荐不足时用 `engineering_locator.py list <path> --prefix <locator-prefix>` 或 `rg` 定位符号，再精确读取。不要先枚举大型 YAML 全表。
3. 新增组件或改变边界时先更新 `operations/engineering/graph.yaml`，再实现；Agent/API/shared 归属必须明确。
4. 实现后运行影响卡的针对性回归与 `engineering_graph.py validate`，再同步受影响工程文档。工程文档提供长期契约，不复制进总提示词或 Agent task。

## 定向约定

- `.project/README.md`、`.project/META.md`、`.project/config.yaml` 不是默认上下文。仅分别在发布/人类说明、工程架构或论文写作、PDF 提取或 `synology://` 解析故障时定向读取。
- 讨论物理论文时，将 `operations/research/physics-manuscript-editing.md` 作为注意力清单；实际修改时组合 academic write capability 使用。
- 附带发现只在交付时提示，不静默压制 LINT，也不擅自扩面修复。
