# ADR-007：统一运行时功能注册与多执行适配器

> 状态：已批准（2026-09-21）
> 影响：`function-registry.yaml`、`function_registry.py`、`route.py`、`wg.py`、`dsh/function_catalog.py`、DSH ToolDefinition

## 背景

系统的用户功能、工作状态和执行工具此前分散在 `route.py`、`wg.py`、六组 DSH factory、操作规范和独立 CLI 中。`route.py --list` 还把 task 与 state 混列；DSH 工具只能在各 factory 内发现；固定管线、Host Agent 工具和 API worker 缺少统一身份。调用仍可工作，但无法从一个入口回答“该功能面向谁、由谁控制、允许谁调用、在哪个后端执行、会写哪些层”。

DSH 只服务 API backend 的程序控制循环。Agent backend 由当前宿主 Agent 持有控制循环，并用 `agent-task-v1` 与确定性代码交接。把全部功能、sub-agent、worker 和固定管线迁入 DSH 会混淆控制权，并把已有事务状态机降格为动态工具编排。

## 决策

1. `operations/config/function-registry.yaml` 是运行时功能、工作状态、调用者策略和入口绑定的唯一管理目录；它不是工作状态，也不替代工程元图。
2. canonical function ID 跨 Route、`wg.py`、DSH、CLI、固定管线和 worker 保持稳定。一个用户功能可以绑定多个执行工具；工具数量不再等于用户功能数量。
3. `route.py --list` 与 `wg.py functions list/show/resolve/validate` 从注册表发现功能。Route 的执行 section 和 handler 仍在代码中，由 runtime validator 检查与注册表完全一致；工程元图不再另行解析 Route 清单，只消费注册表的受管表面。
4. 注册表统一列出 DSH provider；所有生产 DSH 工具通过 `dsh/function_catalog.py` 绑定 canonical function ID、audience、caller、effect 和 maturity。未登记工具、重复 provider 或跨 factory 重名均失败；DSH session log 记录 function ID。
5. 固定管线继续由代码持有步骤、状态、恢复、事务和回滚；注册表只登记入口、控制者、协议和副作用。管线不得通过通用动态 invoke 绕过专用 validator/commit。
6. 主 Agent 可解析用户功能并启动管线；sub-agent 只能调用注册表允许的有界功能，不能直接启动事实写入管线；worker 仅由 API controller 或 pipeline 调用，不向用户暴露，也不拥有工具发现和委派权。
7. Agent backend 解析结果不包含 DSH binding；API backend 才投影 DSH 工具。共享 schema、validator、IR、报告、提交与回滚不因适配器变化而分叉。
8. `engineering_graph.py validate` 必须执行 runtime registry coverage，并将注册表中的 Route task/capability 与工程能力包对账，阻止 Route、WG、DSH 或工程能力形成第二套清单。
9. 注册表使用拒绝重复键的严格 YAML 解析。provider locator 仅用于受限 `dsh.*` factory 的运行时盘点，不是通用 import/shell handler；可执行 handler 仍由代码显式定义。

## 功能与工作状态的定义

2026-09-29 补充语义说明，作为本系统分类与后续设计的判断依据。2026-09-30 统一术语为“工作状态”（state），用于指称可跨操作和会话恢复的工作上下文。程序字段 `state` / `states` 及现有类型 ID 沿用原标识；具体执行进度称“执行状态”或“进度”。

**功能（function）**：可被调用、具有明确执行边界的一项操作或一组相关操作。每次执行应明确输入、目标、输出、允许的副作用和完成条件。功能可以包含多个步骤、人机交互、中断恢复及事务提交；“一次执行”按目标与完成条件划分，可以跨越多次底层命令或调用。功能既可以读取状态，也可以产生受管状态更新。

**工作状态（state）**：围绕用户目标、跨多次功能执行和会话持续存在、能够恢复推进的工作上下文。它保存工作身份、目标、约束、已作决定、当前成果、进度、未完成事项及下一步。一项功能完成后，这个上下文仍有独立价值，后续可以组合其他功能继续推进。具体字段和生命周期由所属工作状态协议定义。

| 区分维度 | 功能 | 工作状态 |
|---|---|---|
| 回答的问题 | 这次完成什么操作？ | 哪项工作正在推进，做到哪里？ |
| 核心内容 | 输入、操作、输出、副作用、完成条件 | 目标、约束、决定、成果、进度、待办 |
| 生命周期 | 一次有边界的执行，允许暂停和恢复 | 跨越多次执行，按工作目标完成或归档 |
| 相互关系 | 读取或更新工作状态 | 提供上下文，承接功能执行结果 |
| 例子 | 修改第3页PPT；查询某结论来源 | 一份报告的持续创作；一个课题的研究推进 |

### 分类时需要区分的层次

- **执行状态**：如摄入阶段、等待API、校验失败、待提交，用于恢复某次功能执行及保证事务正确。持久化、耗时长、多步骤或有状态机，都不是登记工作状态的充分理由。
- **工作状态类型与实例**：`research` 是一种已登记类型；某个具体课题是实例。注册表保存类型、入口和管理信息，实例的内容与进度写入各自权威存储。`active`、`waiting` 等是事项的生命周期取值。
- **profile 与专门工作上下文**：`generic`、`research`、`role_work`、`writing` 是工作区的语义配置。它们配置事项和记忆类型，不按 profile 数量另算顶层工作状态数。`research` 同时是一个工作区 profile 和一个已登记的工作状态类型，属于不同层次。
- **功能登记与执行动作**：一个 canonical function ID 可以汇集一组相关操作；CLI 子命令、Route task、按需 capability profile 和 DSH 工具是发现或执行绑定，不各自增加一个功能计数。`kind=function|pipeline|tool|state_operation|worker` 是注册表中功能记录的种类。
- **状态管理功能**：`state.workspace.manage` 等执行有界的初始化、更新或恢复动作，属于功能；被管理的 `workspace` 上下文才是状态。
- **谁持有控制循环**：Agent backend 由宿主 Agent 推进工作；API backend 由程序推进。工作状态是被读取、更新的上下文，工作状态本身不充当执行器；功能/工作状态的分类不改变两种 backend 的边界。
- **事实与工作记录**：工作状态记录进度、判断和证据地址；知识事实仍回溯 Raw，工作区笔记、执行日志和暂存产物保持各自原有证据地位。

### 判断与登记规则

1. 先明确对象：讨论的是一个可完成的操作，还是在多次操作之间持续承接成果的用户工作。
2. 操作有明确输入、结果和完成条件，按功能定义；目标、决策和待办需要跨操作恢复，按持续工作上下文设计。
3. 同一业务可以同时具有两者。例如研究状态调用查询与写作功能，报告创作上下文调用配图、改单页与汇总操作。
4. 新的持续工作上下文优先复用现有工作区机制；确有独立语义与生命周期、权威存储、恢复入口和状态操作契约时，再评估登记新的工作状态类型。登记数量由注册表决定，不由目录名或对话中的称呼决定。
5. 功能和工作状态之间的适用关系、调用者、backend、效果与入口仍以 `function-registry.yaml` 为准。文档中的实例或设计建议不自动产生新的运行时条目。

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

新增或删除 Route、WG、DSH 入口时，必须在同一改动中更新注册表和针对性回归。


## 当前功能与工作状态清单（2026-09-29）

以下是从[运行时注册表](../../config/function-registry.yaml)整理的说明性快照；注册表仍是唯一管理目录。统计 canonical 条目，不把多个入口或一个功能的子操作重复计数。现有 **3种工作状态、26项功能记录**；功能记录包含 **20项非内部功能（16项stable、4项preview）和6项内部API worker**。20项中19项面向用户及Agent，系统建设面向Agent。成熟度是现有登记值，本次盘点不等于重新验收所有功能。

### 已登记的工作状态

| ID | 工作状态名称 | 持续上下文 | 注册表声明的权威存储 |
|---|---|---|---|
| `workspace` | 持续工作区 | 项目事项、提案、记忆和可重建投影的工作状态。 | `projects/<path>/workspace.yaml + .workspace/items + .workspace/memories` |
| `research` | 研究工作 | 研究项目中的持续判断、实验、笔记和论文推进状态。 | `projects/<path>/workspace.yaml + research profile` |
| `frontier` | 研究前沿 | 开放问题、部分答案、残余缺口、思路和验证轨迹的工作状态。 | `academic/frontier/questions + academic/frontier/trajectories` |

`workspace` 提供通用承载，`research` 有专门研究语义并复用工作区内核，`frontier` 使用问题与轨迹记录。三种登记类型不意味着必须创建三套互不相干的数据。工作区的四种 profile 为通用工作（generic）、科研（research）、行政管理（role_work）、持续写作（writing），见 [WORKSPACE.md](../../WORKSPACE.md)。研究旧项目的`.research-memory`由兼容入口维护；工作区`notes/status.md`等为可重建投影，权威记录按各自协议维护。

### 非内部功能（20项）

“关联工作状态”列列出注册表显式的 `states` 字段；“—”表示未声明工作状态筛选，不表示该功能没有执行状态或持久数据。`states` 的实际解析规则由 resolver 和专用入口共同约束。除标注preview的四项外，表内成熟度均为stable。

| canonical ID | 名称与操作范围 | 登记种类 | 关联工作状态 |
|---|---|---|---|
| `system.function_catalog` | **功能目录**：列出、查看和按调用者、后端、工作状态解析 canonical 功能。 | tool | — |
| `knowledge.ingest` | **内容摄入**：受管摄入附件、正文、Inbox、论文、会议和普通文档，并提交 Raw、Wiki、Graph 与回执。 | pipeline | — |
| `knowledge.query` | **知识查询与溯源**：结构化召回、图导航、Wiki section 读取和 Raw locator 核验。 | function | — |
| `knowledge.lint` | **知识库健康检查**：发现并报告结构、冲突、版本、来源、提示词和图质量问题，不自动扩面修复。 | function | — |
| `knowledge.sync` | **跨域同步与对账**：按增量、补漏和一致性核对跨域内容，并报告或提交受管变更。 | function | — |
| `knowledge.scan` | **存量扫描**：发现新文件、更新版本、Inbox 项和待巩固候选；未授权时只报告。 | function | — |
| `knowledge.hub` | **Hub 查询与生命周期**：检查 Scope 和成员动力学，并在 Agent 确认后执行创建、分裂、合并或重分配。 | function | — |
| `artifact.write` | **文稿写作与修改**：基于可回溯材料生成或修改 Markdown、Word、PDF 和专业文稿。 | function | workspace、research |
| `artifact.slide_library` | **PPT模板管理与复用（preview）**：检索、查看、收藏直接使用/套用填充组件，保留来源版本并生成独立单页实例。 | function | workspace、research |
| `knowledge.visualize` | **知识图可视化（preview）**：按主题、节点及关系筛选，只读生成有向图与来源导航清单，显式报告截断。 | function | — |
| `artifact.cv` | **简历制作与维护（preview）**：在持续工作区中使用匿名模板包组织模块，校验证据化双语履历，生成日期 DOCX 版本并比较版本差异。 | function | workspace |
| `artifact.presentation` | **演示文稿制作与辅助检查（preview）**：制作、预览和导出原生 PPTX/PDF，并按需进行形式检查、证据核查和引用脱敏。 | pipeline | workspace、research |
| `artifact.image_ocr` | **图片 OCR**：对图片执行源绑定转写、复核和回执生成；远程调用需要显式授权。 | tool | — |
| `artifact.visual_qa` | **视觉产物 QA**：检查图片、PDF 和 PPT 页面中的可见缺陷，不修改输入产物。 | tool | — |
| `artifact.visual_reconstruction` | **图片或 PDF 转可编辑 PPT**：将视觉输入重建为可编辑 PPTX，并报告 fallback 和视觉差异。 | pipeline | — |
| `artifact.comic` | **漫画与配图生成**：查询模型并生成单图或 storyboard 批量图片；真实远程调用需要显式授权。 | tool | — |
| `state.workspace.manage` | **工作区状态管理**：初始化、接续和管理事项、提案、记忆与可重建投影。 | state_operation | workspace、research |
| `state.research.manage` | **研究项目与记忆管理**：初始化或校验研究项目，恢复上下文并记录稳定研究判断。 | state_operation | research |
| `state.frontier.manage` | **Frontier 问题与轨迹管理**：提问、回答、检索、审查和追加开放问题、思路与验证记录。 | state_operation | frontier、research |
| `engineering.build` | **系统建设**：分析工程影响面、精确读取工程 locator、修改、验证和同步工程文档。 | function | — |

### 内部API worker（6项）

这些条目登记为受限内部执行单元，由API controller或pipeline调用，按既有schema返回候选；不作为独立用户功能入口，也不持有持续工作的控制循环。全部为API backend、internal成熟度。

| canonical ID | 名称与执行职责 |
|---|---|
| `worker.ingest.paper_workspace` | **论文 workspace 语义 Worker**：API 模式下生成受候选、证据包和 schema 约束的论文 workspace 产物。 |
| `worker.ingest.meeting_compiler` | **会议编译 Worker**：API 模式下按单一受限协议完成会议纠错、Wiki 和语义槽候选。 |
| `worker.ingest.document_semantic` | **通用文档语义 Worker**：API 模式下生成受 schema 约束的 Wiki 与语义槽候选。 |
| `worker.semantic_recovery` | **语义修复 Worker**：API 模式下对既有受限候选进行一次结构化局部修复，不持有事务控制循环。 |
| `worker.frontier.answer` | **Frontier 回答 Worker**：API 模式下只消费有界 Graph-Wiki-Raw 证据包并返回 schema 化候选答案。 |
| `worker.workspace.profile` | **工作区画像 Worker**：API 模式下从有界工作区快照生成画像候选，由共享 schema 校验后提交。 |

### 现有业务的状态与功能关系

| 业务场景 | 持续上下文 | 被调用的功能或操作 |
|---|---|---|
| 课题研究 | `research`及具体课题工作区 | 查询、研究记忆管理、论文写作 |
| 开放问题探索 | `frontier`问题与验证轨迹 | 检索证据、回答、审查及追加轨迹 |
| 行政与协调工作 | `workspace`的`role_work` profile | 事项管理、资料查询、文稿写作 |
| 长期写作 | `workspace`的`writing` profile | 起草、改写、导出及版本维护 |
| 简历维护 | 工作区中的履历材料与版本记录 | `artifact.cv`提供的校验、生成和比较 |
| 学术报告制作 | 项目工作区及该报告的创作记录、页序、版本、确认和待办 | `artifact.presentation`及配图、改单页、预览、汇总和按需检查 |
| 一次资料摄入 | 摄入事务自身的阶段、校验及恢复记录 | `knowledge.ingest`；如服务研究，结果由研究上下文承接 |

**PPT定位**：整份报告的持续创作具有工作状态属性；改单页、配图、预览、汇总等是有边界的操作。目前这些操作归入既有功能及入口，`presentation`尚未登记为独立工作状态类型。现有PPT创作状态机和工作区已保存相关记录；将其升级为独立工作状态类型属于后续架构决策。本说明记录当前实现与概念关系，不将设计建议写成已实现事实。

### 获取最新清单

```bash
python3 .scripts/route.py --list
python3 .scripts/function_registry.py list --include-internal --format json
python3 .scripts/function_registry.py show artifact.presentation --format json
python3 .scripts/function_registry.py validate --runtime
```

更新注册条目时同步本快照的分类、计数和对应行；执行参数、调用者权限、backend及最新成熟度直接查询注册表。Route task、按需profile、底层脚本与工具的数量另行统计。

## 登记覆盖审计（2026-09-29）

本轮按上述定义检查实际实现，结论是：**注册表的已管理表面一致，但独立功能和具体入口仍有登记缺口；未发现必须增加的顶层工作状态。** 以下保留初次审计发现；审计当时计数为3种工作状态、24项功能记录。随后经用户授权已将模板管理与知识图可视化登记并补齐统一入口，当前计数以上节为准。上节清单是“已登记清单”，不应理解为所有实际实现的完备清单。

### 检查范围与方法

- 对照注册表、Route、WG、DSH生产工具factory、操作规范、playbook、项目内skill，以及`.scripts/`、`computing/`、`scripts/`的入口。通过源代码、AST和选定入口的`--help`检查职责；未执行远程计算、发布、摄入或生成任务。
- 静态盘点`.scripts/`的122个非测试Python文件，其中102个有主入口；25个主入口直接出现在注册表的`cli/pipelines/implementations`绑定中，77个未直接绑定。该差集只是检查线索，其中包含管线内核、旧版兼容、迁移、实验和辅助脚本，不能将77个全部计作漏登功能。
- 同时盘点DSH的22个非测试Python文件、项目内skill的1个预检脚本、computing的1个共享工具和scripts的3个专用采集脚本；阅读各研究项目的相关入口约定。第三方依赖、temp试验和全部项目科研代码不作为系统级功能逐项登记范围。
- 检查10份`workspace.yaml`（generic 2、research 1、role_work 5、writing 2）及10处旧式`.research-memory`目录。目录存在只说明承载方式，本轮不验证其中每个业务事项或研究结论。

### 待明确归属的功能入口

| 已实现能力 | 证据入口 | 判断与建议 |
|---|---|---|
| PPT模板与组件库 | `.scripts/slide_library.py:main`；`operations/PRESENTATION.md`“直接使用型与套用填充型” | **已解决**：独立登记为 `artifact.slide_library`，补齐 WG/capability 入口及 show/use；现有收藏、槽位和原生对象操作统一归属。 |
| 知识图可视化 | `.scripts/graph_visualize.py:main` | **已解决**：独立登记为 `knowledge.visualize`，补齐 WG/capability 入口、只读访问、有界筛选、多重有向边及来源导航。 |
| 开源构建、版本准备与发布 | `.scripts/open_source_release.py:main`；`.scripts/open_source_publish.py:main`；`operations/engineering/open-source-release.md` | **入口及功能归属缺口**。已有明确操作契约，当前`engineering.build`只描述工程分析/修改/验证，未绑定发布入口或体现远程发布副作用。建议独立登记发布功能；论文产物打包验证可归入该功能的子操作。 |
| 轻量经验召回 | `.scripts/experience_recall.py:recall`；`operations/EXPERIENCES.md` | **共享工具归属缺口**。有独立输入、输出、预算和跨query/ingest/write/build用途，但未有功能身份或执行绑定。可登记共享工具，或在既有系统支持功能下明确归属；不是新的工作状态。 |

初次审计的四项中，前两项已完成独立功能落地；开源发布与经验召回仍待单独处理。跨项目远程计算及受限领域诊断按用户决定维持辅助工具/专门技能归属。此次新增功能沿用已有工作状态。

### 已有功能的具体绑定不完整

| 已有功能范围 | 未直接登记的代表入口 | 处理判断 |
|---|---|---|
| PPT图形创作 | `.scripts/presentation_artwork.py` | 已由PPT规范调用，可先补到`artifact.presentation`；仅因API参与创作不把整个宿主流程改称API backend。 |
| PPT结构和视觉解析 | `.scripts/pptx_structure.py`、`.scripts/pptx_visual.py` | 服务摄入、设计与检查；应明确共享实现归属及模式入口，避免重复算作几套相同功能。 |
| private摄入及重摄 | `.scripts/private_ingest.py`、`.scripts/private_reingest.py` | 仍属`knowledge.ingest`的领域入口；补绑定时保留private物理隔离和本地处理约束。private会话开关是域选择，不是独立用户工作状态。 |
| 常规摄入辅助与旧入口 | `.scripts/ingest_admin.py`、`.scripts/re_ingest.py`、`.scripts/ingest_user_assertions.py`等 | 部分已有WG/DSH间接绑定，缺少直接CLI清单不代表缺少整个功能；可按主要支持入口补齐。 |
| 查询/路由辅助 | `.scripts/query_orchestrate.py`、`.scripts/read_paper.py`、`.scripts/playbook_dispatch.py`等 | 作为已登记功能的适配器或辅助入口看待；不逐脚本膨胀功能数量。 |

### 状态检查与排除项

- **受限领域诊断保留为专用 skill**：按用户 2026-09-29 明确意见，该领域技能目录及其预检脚本按现有方式使用，不纳入系统功能目录，也不计为漏登功能。本项修正此前将其列为独立功能遗漏的判断。
- **远程计算归为研究辅助工具**：按用户2026-09-29明确意见，`computing/ssh_run.py`保留为研究过程中按需使用的SSH执行与传输工具，不纳入系统功能目录，也不计为漏登功能。本项修正此前将其列为独立功能遗漏的判断。
- 教学、行政、系会汇报、简历维护及本次PRL报告已有工作区承载，profile均已定义。`presentation`未登记为独立类型，但其跨页创作记录由工作区与PPT状态协议承接；这是专门工作状态类型的可选设计，不是运行中的上下文丢失。
- 学生指导、项目申请、受限领域诊断等目录可具有持续工作上下文，已有研究记忆或项目记录按各自约定维护。目录名或业务主题不会自动产生新的顶层工作状态类型。
- `projects/asks-ai-agent/AGENTS.md`中的文章pipeline属于该研究项目内的专门创作流程；文章状态目前由项目记录承载。若以后提升为全系统复用入口，再决定其功能登记和工作区profile。
- 摄入事务、PPT页内批准、实验checkpoint和发布回执是相应操作或产物的状态；不能仅因保存JSON就升级为顶层state。
- `dsh/semantic_recovery_agent.py`中的`inspect_issue_context`是已登记`worker.semantic_recovery`的任务局部受限工具，不是漏掉的第七组生产DSH provider。
- `baseline_*`、E1评测、一次性迁移/修复以及`scripts/`中的专用采集工具，按工程/研究内部工具处理。外部安装的个人skill、宿主插件和应用能力不纳入本仓库功能总数。

### 为什么现有校验没有发现

`.scripts/function_registry.py:validate_runtime`只做Route task/profile、WG顶层命令和已声明DSH provider工具的精确对账。`validate_registry`检查已写入的CLI/实现路径存在，但不反向发现所有独立CLI、skill或共享工具。因此本轮`validate --runtime`通过，仅证明受管表面一致，不能证明整个仓库不存在漏登。

后续宜先为上述独立功能确定身份，为共享入口明确归属；再增加有明确范围的入口覆盖清单或审计机制，并给内部、legacy和实验入口标明类别。注册结果继续以单一运行时注册表为准。
