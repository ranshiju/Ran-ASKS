# WikiRan 工程全景手册

> **目标**：只读本手册后，能建立系统的全局心智模型、掌握真理源裁决顺序与数据模型；实际执行时仍须按任务调用 `.scripts/route.py` 读取最新规范，运行脚本时按任务定向读取同目录的 `code-guidance.md`。
> **按需读取协议**：日常任务不应整读本手册。`route.py` 会从 `operations/engineering/graph.yaml` 派发短工程上下文包和不可跳过的任务卡；任务卡先于长规范执行。建设改动先用 `.scripts/engineering_graph.py impact <节点> --verify` 查询影响面和最小回归集；需要运行高风险脚本时再用 `contract <节点>` 获取短 I/O 契约。只有元图未覆盖的复杂问题才回到本手册或 `code-guidance.md`；未覆盖不等于无影响，必须显式补查。
> **本手册的定位**：本手册只承担其他文档无法替代的三件事——工程全景心智模型、文档矛盾时的真理源裁决、数据层 schema 与常见错误对照。流程细节（摄入/查询/Hub/Lint 的操作步骤）以 `operations/` 对应规范为准，脚本调用细节以 `code-guidance.md` 为准，机器可检查的工程结构以 `graph.yaml` 为准。本手册不重复这三者。

---

## 0. 一分钟心智模型

WikiRan 是一个**文件型、跨域、可回溯的知识库**。其主链路是：

```text
不可变 raw 原料
  → LLM 编码成 wiki 页面（带 sources）
  → LLM 只提取少量语义槽，程序建立内存文档子图
  → 确定性 attach plan + query probes 后原子融合 graph.db
  → 程序局部刷新普通节点的重叠 Hub membership，并产生生命周期候选
  → Query 按意图融合 Wiki 语义召回与 Graph 结构召回，再沿 semantic address 下钻 raw 核验
  → Graph 累积只产生 consolidation 候选，由 Agent 审核后更新 Synthesis Wiki
  → 校验、日志、同步与反哺使结构持续演化
```

不要把这个流程理解成“把 Markdown 做成向量库”。当前系统以可读 Markdown 页面保存节点属性、以 SQLite 图保存关系和别名；embedding 只用于局部匹配、关键词聚类或候选采样，不是当前的全文检索主路径。

最重要的事实边界：

- `raw/` 是事实锚点，**绝不修改**；
- wiki、图、Hub、索引、修正版、快照均为派生物；
- 图边和 Hub 负责定位，不是事实答案；
- 每一个事实性回答都要沿 `sources` 或边的 `source` 回到 raw。

摄入流程现在是**代码驱动编排**：一个端到端 pipeline 由纯代码掌控流程控制权（去重→提取→撰写→校验→落位→写图→收尾），LLM 只在最小语义点（撰写 wiki 与语义槽）被调用，其余步骤全由程序完成；agent 启动后不全程监控，只在结束读取最终结果。论文 API 路径以摘要、定理、结论构成的确定性证据包锚定生成，要求保留主张的对象、条件和比较基准；语义槽不得推断方向边或把研究场景误作局限。结构性语义错误早停并保留可修复产物，恢复前由程序复验；描述性对象走局部修复。详见 `operations/INGEST.md` 与 `code-guidance.md` §2。

写图采用一次性的内存 `GraphDelta`：先描述 Wiki、Raw 文档包、少量导航边和带 Raw locator 的 keyword 局部说明，再用确定性名称入口与受限双视图 identity gate 连接主图，并在 SAVEPOINT 中融合。精确名称多候选和单一 embedding 相似都不自动 merge；前者无法消歧时 abstain，后者在没有名称碰撞时保留本地节点、边和 gloss。query probes 只观察 anchor、Raw 到达和两跳导航效果，不因软指标未满分制造摄入错误。

节点以 `nodes.path` 作为稳定 ID，配首选 `title`、多对多 `aliases` 和主导航 `description`；`node_glosses` 逐来源保存局部说明与 Raw locator，后续摄入不盲目覆盖主描述。DSH 只暴露 `node_resolve` 与 `semantic_search` 两个语义工具：前者用名称信号和 label/semantic 双视图做受限身份解析，后者只做相关召回；裸 embedding、阈值选择和 merge 写操作不交给 LLM。

建设 Agent 读取工程文件时采用独立的按需 locator，不混用 Raw/Wiki 证据语义：Markdown heading path、YAML JSON Pointer、Python qualified symbol 和显式行段均由代码精确截取。读取失败、歧义或超预算时直接拒绝，不回退全文；不为此维护索引或 companion。功能性任务只调用封装函数，工程 locator 不进入通用 query/DSH 工具面。具体调用见 `code-guidance.md` 的 `engineering_locator.py` 段。

Agent 调用分成三层：`research`、`ingest` 等 task 是持续工作状态；`write` 是只在实际落笔时加载的能力；`wg.py` 与 DSH ToolRegistry 中的函数是带参数、结构化返回的执行工具。视觉能力沿用同一边界：`visual_check` 是只读检查，仅在用户明确要求，或修改指令依赖页面可见状态而需要先理解布局时调用；`visual_to_editable_ppt` 只响应明确的图片/PDF 对象化请求，并写入新的 PPTX。两者均不进入事实查询链，不把视觉判断升级为 Raw 证据，也不因常规编译或文字修改自动触发。

## 1. 权威顺序与真理源

### 1.1 文档矛盾时的裁决顺序

当文档或历史代码彼此矛盾时，按以下顺序判断：

1. 用户的本轮指令；
2. 根目录 `AGENTS.md` 的红线与任务识别；
3. `.scripts/route.py --task ...` 为当前任务输出的规范片段；
4. `operations/*.md`、各子项目 `SCHEMA.md` 和正在执行的脚本；
5. `.project/META.md`、`projects/kr-wiki-paper/notes/status.md` 的架构状态；
6. 项目讨论、论文、归档和历史说明。

最后一层经常保留已废弃方案，不能覆盖当前规范和脚本。

`memory/experiences/` 只存轻量、实验性的策略提示，优先级低于 playbook、操作规范和脚本；它不能作为事实、数据或回溯来源。仅当 playbook 未覆盖或执行歧义时，用 `.scripts/experience_recall.py` 取最多 3 条泛化 pattern。

任务类型识别（使用 vs 建设）、路由纪律、执行清单等启动流程见 `AGENTS.md`「指令识别」与「使用任务启动约束」，本手册不重复。

## 2. 仓库布局与数据层

### 2.1 仓库布局

```text
raw 事实域
├── academic/       学术论文、机构、人物、会议等
├── admin/          行政政策、流程、会议、活动等
├── teaching/       教学材料
└── business/       转化/商业材料

cross-domain/       跨域图、Hub、嵌入缓存与派生输出
inbox/              待分类入口
temp/               临时提取区（inbox-extract 等中间产物）
operations/         可执行规范（含 engineering/ 工程元图与本手册）
.scripts/           确定性工具与编排器
scripts/            爬虫与一次性数据采集脚本
projects/           用户个人工作区与研究文档；会用到知识库工程系统，但不属于知识库内容
agents/             agent 配置与提示资产
slide-library/      讲稿与幻灯资产
memory/ 用户偏好和局部 playbook
```

每个知识子项目原则上都有：

```text
<domain>/raw/       原始材料，只增不改
<domain>/wiki/      可读、可检索、可溯源的编译页面
<domain>/outputs/   日志、报告等运行产物
<domain>/SCHEMA.md  本域页面类型、frontmatter、section 契约
```

四个业务域独立管理，跨域的连接主要通过 `cross-domain/graph.db` 和 `cross-domain/topics/` 完成。`projects/` 是活跃文稿区；其中的文档只有在用户明确要求时才可被摄入相应 raw 域。在 `projects/<name>/` 下做研究时走 `route.py --task research`，研究过程产生结构化记忆（`projects/<name>/.research-memory/`），独立于 ingest，研究内容不入库。

### 2.2 Raw：不可变的事实层

- 原始文件放在各域的 `raw/`。
- 任何任务都不得编辑、重排或“纠正” raw。
- 如果原始材料本身存在语音识别错误、PDF 提取需求或版本演进，生成旁路的派生文件，而不是覆盖原件。
- wiki frontmatter 的 `sources` 必须指向可定位的 raw 或规范允许的派生输入；关键声明可带 section/锚点定位。

例外不是“可以改 raw”，而是同源重提、纠错和版本变化都必须记录日志，并保留旧原料。

### 2.3 Wiki：节点属性和人类可读内容

wiki 是 LLM 编译的知识页面。它承载：

- 标题、类型、日期、状态、来源、版本链等节点属性；
- `## Navigation`：约 80–200 token 的导航概述；
- `## Content`：内容主体；
- 域/类型所需的其他结构和关键声明锚点；
- 人工补充的 `related`、说明和局部分析。

`<wiki-page>#<heading-slug>` 是 Wiki 与 Graph 共享的 Semantic Address。Source Wiki（论文、会议、单文档摘要）保持 source-local；Synthesis Wiki（review、comparison、concept）综合多个来源。Graph context 在读取时动态 overlay，不复制进 Markdown。

新页面应以 `.scripts/wiki_skeleton.py` 生成骨架，避免手写确定性字段。论文骨架会从 `paper.md` 机械提取标题、来源和作者，兼容 MinerU 的 HTML 上标与姓名断词；这只是编码辅助，LLM 仍须以 raw 核对作者完整性。标准 section 读取是查询降成本的契约：候选先读 `Navigation`，确认相关才读 `Content` 或锚点段。旧页面可能还未迁移；读取脚本报错时，LLM 必须显式决定整读并说明“该页未迁移”，不能静默回退。

### 2.4 `graph.db`：边与别名的唯一主数据

当前架构中：

- Markdown 保存页面内容和节点属性；
- `cross-domain/graph.db` 保存节点索引、语义边、aliases 和独立的时态事实；
- 图边**不再**从 Markdown 的 `Core Triples` 段作为当前主路径派生；摄入通过 `graph_ingest.py` 增量写入。

SQLite 主要表：

| 表 | 作用 |
|---|---|
| `nodes` | `path` 主键，标题、类型、来源类型、日期、状态、是否直连 raw、`ingest_version`（管道版本戳，re-ingest 判断是否需重摄入） |
| `aliases` | 别名/缩写到节点路径的唯一映射 |
| `edges` | `subject`、语义 `predicate`、`object`、置信、来源定位、语音识别标记 |
| `edge_evidence` | 历史兼容证据表；新摄入不写，事实回溯走 Wiki section 脚注 |
| `edge_origins` / `node_origins` | 页面编译贡献 lineage；不是事实证据 |
| `node_glosses` | keyword 的逐来源局部说明与精确 Raw locator；`nodes.description` 是可重建主导航缓存 |
| `temporal_facts` | 独立于 `edges` 的轻量时态事实：`valid_from`/`valid_until`、`superseded_by`、`source`、`recorded_at`；默认不混入普通图导航 |
| `metadata` | 图级元数据 |

`temporal_facts` 当前由 `graph_ingest.py` 对 admin `policy`/`procedure`/`decision` 与 teaching `course` 页写入：
frontmatter `effective_from`/`effective_to` 被落成一条 `subject=page`、`predicate=生效`、`object=page`
的 canonical 时态事实；查询用 `query_graph.py temporal --at DATE`，普通 `neighbors`/`search` 不读取它。

节点常见类型：`page`、`entity`、`people`、`hub`、`timeline-summary` 和 Raw 文档包节点。`entity` 可有 keyword、proposition、person、citation-only、venue、institution 等子类型。图中的节点用相对路径/规范名定位，不能随意把同一实体写成多个裸字符串。Wiki 通过机械 `来源` 边直连 Raw 文档包；人物、概念和 Hub 主要与 Wiki 相连。

概念节点不再从 Hub `## 关键词` 取名称集。摄入先走 GraphDelta/node_semantics 的名称门：同名候选全部纳入消歧，唯一候选才可复用；再用当前文档局部说明与节点 title + 主 description 做语义门。精确同名多候选无法消歧时 abstain；无名称碰撞但语义门不足时保留本地节点和来源，不得以单一 embedding 分数强制合并或丢弃文档关系。

Hub 是 keyword、proposition、People page 等普通节点的可重叠动态群落。canonical 身份来自稳定 path、简短 title 与唯一 `## Scope`；arXiv 方向只作初始化模板。程序结合节点 profile、同类成员原型和图邻接 affinity，幂等维护派生 `聚类于` 边并生成 new/split/merge candidates。People page 以可定位 `## 人物画像` 参与：研究、行政、学生及其他角色按实际对象/职责/阶段描述；无 page 的 person entity 不参与。未归类、歧义和候选都不构成错误或告警，create/split/merge 仍由主 Agent 确认。

边的置信只能是：

- `可追溯`：可定位到单一或明确 raw 片段的提取；
- `推断`：跨来源综合、查询反哺等推断关系；
- `存疑`：低置信、待审关系。

没有“图中直接事实”这一档。即便 `可追溯` 也是派生编码，事实回答仍回 raw。

查询时 predicate family 由谓词和端点类型确定性派生，不写入 `edges`。`fact/relation/explanation/exploration/lineage` profile 只控制遍历平面；联合召回对 Wiki/Graph 排名使用 weighted RRF，section capsule 和 Graph neighbor 都只是候选信号。

### 2.5 派生物不是主数据

页面目录、图快照、统计、关键词索引、查询日志、审计 JSON、报告等均是派生或运行产物。可通过工具重建的东西不要手工维护第二份。

历史中可能仍出现 `Core Triples` 或 `triples*.md` 的说明、旧页和兼容脚本。它们可用于迁移/审阅，**不得**被当作当下写边和查询的唯一真理源；对当前数据关系以 `graph.db` 为准。

## 3. 常见错误与正确替代

| 不要做 | 应当做 |
|---|---|
| 修改 raw 来修正 OCR/ASR | 保留 raw，生成并审计派生修正版 |
| 从 Hub 或图边直接回答事实 | 用它们定位后回 raw 核验 |
| 把所有连通节点当一个新主题 | 以 keyword 共现和语义簇形成候选，再确认 |
| 让未匹配文档消失 | 保留无 Hub 状态；仍可经标题、Navigation、概念边和语义搜索召回 |
| 手工维护多份边索引 | 以 `graph.db` 为边主数据，按工具派生快照 |
| 让 LLM 自检格式、路径和重复 | 运行确定性脚本并按报错修复 |
| 因有证据缺口无限检索 | 提交具体可行动假设；无候选则限定性回答 |
| 默认读取整篇候选页面 | 先读 `Navigation`，按相关性再下钻 |
| 把旧 `deprecated` 页面当现行制度 | 默认过滤，历史问题才沿版本链读取 |
| 看到 WARN 后沉默 | 向用户提示，除非获授权不要擅自扩面修复 |
| 把 warning 当失败回 3.3 全量重写 | 走局部修补（最小成本修复）；只有硬错误才回退重写 |
| 把未登记谓词直接变为永久图谱契约 | 记录候选并自动归一/聚合；达到阈值才正式注册，反向关系与方向 tier 仍受控 |
| 结构错误反复调用同一生成提示词 | 早停，保留通过产物；修正后 `--resume` 并由程序在提交前复验 |
| 让 agent 全程监控正常摄入 | 默认 quiet；agent 只读最终结果，遇硬错误才介入 |

## 4. 进一步阅读地图

| 想掌握什么 | 首读文件 |
|---|---|
| 红线、任务识别、启动纪律 | `AGENTS.md` |
| 任务分发和建设原则 | `operations/shared-conventions.md`、`.scripts/route.py` |
| 摄入细节 | `operations/INGEST.md` + 对应 `<domain>/SCHEMA.md` |
| 图表结构与写边 | `.scripts/graph_lib.py`、`.scripts/graph_ingest.py` |
| Hub 细节 | `operations/HUB.md`、`hub_semantics.py`；`direction_matcher.py`/`cluster_keywords.py`/`hub_split.py` 仅 legacy 兼容 |
| 查询协议 | `operations/QUERY.md`、`query_orchestrate.py`、`query_actions.py`、`dsh/` |
| 项目研究状态与落笔能力 | `operations/RESEARCH.md`、`operations/WRITE.md` |
| 研究前沿问题与轨迹 | `operations/FRONTIER.md`、`.scripts/frontier.py` |
| 图片、PDF、PPT/PPTX 视觉检查 | `operations/VISUAL_QA.md`、`.scripts/visual_qa.py` |
| 图片/PDF 转可编辑 PPT | `operations/VISUAL_TO_EDITABLE_PPT.md`、`.scripts/visual_to_editable_ppt.py` |
| 健康/同步 | `operations/LINT.md`、`operations/SYNC.md`、`graph_metrics.py` |
| 当前架构/规模 | `.project/META.md` |
| 论文研究状态 | `projects/kr-wiki-paper/notes/status.md` |
| 设计理念 | `projects/知识结构涌现/idea-deltas.md` |
| 脚本何时、如何安全调用 | `operations/engineering/code-guidance.md`（按任务定向读取） |
| 工程影响面、当前入口与任务卡 | `operations/engineering/graph.yaml`、`.scripts/engineering_graph.py` |
| 工程元图与金样的分工决策 | `operations/engineering/adr/001-engineering-meta-graph-and-golden-pipeline.md` |
| 开源发布边界与构建 | `operations/engineering/open-source-release.md`、`open-source-manifest.yaml`、`.scripts/open_source_release.py` |
