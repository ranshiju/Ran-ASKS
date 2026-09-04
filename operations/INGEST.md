# Ingest（摄入）操作规范


---

## 会话级总计划（条件触发）

总计划仅协调一次摄入会话的阶段，不替代下文创建/更新/批量模式、三阶段任务卡或其工具约束。满足任一条件时，在首次路由后、开始执行前建立 3–5 项抽象计划：多个文件、混合文件类型、跨子项目归类、批量摄入，或可能进入更新/冲突分支。单篇且归属、模式明确时直接进入当前任务卡。

- 允许的抽象阶段：分流与范围确认 → 编码 → 巩固 → 校验、回执与清理；按实际范围省略阶段，不得细化为命令、字段或工具清单。
- 每个 create stage 仍须完成、落盘并重新路由后才能推进；总计划不能合并或跳过 stage，也不能自动扩大到计划外文件或修复。
- Agent 可在会话中维护计划和完成状态；`INGEST_BACKEND=api` 时，API 只处理当前任务卡规定的证据卡/受控语义槽。计划由编排层提供，不作为 API 输出、证据或落盘产物。

---

## 两种模式

| 模式 | 触发条件 | 核心行为 |
|------|----------|----------|
| 创建 (create) | raw/ 中新文件，log.md 无记录 | 新建 wiki 页面，级联创建关联页面 |
| 更新 (update) | raw/ 中文件被新版替代，log.md 有旧记录 | 读取已有页面，增量覆盖，保留人工标注 |

---

> **使用约束**:先读规则再操作;wiki 声明可溯 raw;绝不修改 `raw/`(例外:同源重提需 log 记录)。


## 创建模式 — 执行步骤

> **两阶段设计**:写入拆为**编码(Encoding)**与**巩固(Consolidation)**两个显式阶段,对应人类记忆的编码/巩固机制(Wu 2025)。编码产出忠实摘要,巩固产出抽象知识与关系网络。两阶段可分离执行——巩固允许延迟批量,解决跨多篇累积压缩缺口。

### 阶段一:编码(Encoding)— 忠实提取,不加工

产出:来源摘要页(paper-summary / 政策摘要等),忠于 raw,不做跨文档抽象。

### 长文动态颗粒度（综述、书籍、长政策）

默认仍是“一份 raw → 一张来源页”。长文先运行只读规划器，不因字数直接拆页：

```bash
python3 .scripts/long_document_plan.py <已归档 raw 路径>
```

- **候选触发**：原文达到 `8000` 字符，或至少有 `4` 个二级标题；这只触发评估，不等于允许拆分。
- **确认条件**：至少两个候选章节各自足以回答独立问题、拥有可定位证据且拆分能降低查询成本。LLM 仅做一次语义确认，不能以长度或词频替代该判断。
- **产物形态**：确认拆分时，创建一张总览来源页（完整 `sources`、文档定位、章节地图、跨文档主题）和最多一层章节来源页；章节页同样指向原始 raw，并以标题/行号/锚点定位。未确认则保留单一来源页。
- **关键词预算**：总览 `3–8` 个、章节各 `2–5` 个；密度、词频只用于产生候选，优先标题、定义/结论、跨章节覆盖、现有 Hub 匹配和导航价值。高频文件套话、机构泛称及无复用价值词不得仅因密度入图。
- **写入边界**：规划器不写 raw/wiki/graph；确认后的页面仍走骨架、`graph_ingest.py`、`ingest_check.py`。不自动递归拆分到节级，除非用户明确要求。

## 会议纪要预处理

> **代码驱动**（2026-09-03）：inbox 下的会议纪要 `.txt` 由 `ingest_meeting.py` 驱动。去重、候选召回、校验、落位和落图均为确定性步骤；一个 Meeting Compiler specialist 在一次语义调用中完成转写纠错决策、Wiki 编译和语义槽抽取。`.docx/.doc/.pptx/.md` 仍由 `ingest_document.py` 提取，但文件名或正文同时满足强会议速记标记时，inbox 必须显式传入 `source_kind=meeting`，保留 `speech-recognition` 来源语义。

若 sources 是 `.txt` 会议纪要（`conferences/`、`discussions/` 或会议目录，`source_type: speech-recognition`），执行以下闭环：

1. **Raw 不可修改**：原始 ASR 文本是事实源。`corrected.txt`、`entity-resolution.json`、Wiki 和 semantic 都是可重建派生物，先写事务临时目录；任何纠错不得覆盖 Raw。
2. **程序只准备候选**：`speech_entity_resolver.py <raw.txt> --output <entity-candidates.json>` 从 `graph.db` 的 people、aliases 和人物关系生成 exact/review 候选，不使用 `--apply` 提前形成最终纠错文本。可用 `--candidate-file` 缩小本次局部召回；索引按人物知识指纹增量刷新。
3. **一个 specialist 一次理解全文**：`dsh/meeting_compiler_agent.py` 接收只读原文、人物候选、程序生成的 meeting ID/目标 sources 和 validator 错误，在同一上下文中返回 `meeting-compiler-v1`：
   - `<<<PREPROCESS>>>`：仅包含原文精确片段替换和人物 resolved/unchanged/unresolved 决策；证据不足必须 unresolved。
   - `<<<WIKI>>>`：conference-summary Wiki 草稿，参与者、术语和正文与同轮纠错决策一致。
   - `<<<SLOTS>>>`：参会者、汇报者、决策、待办和受限三元组，与同轮 Wiki 使用同一实体判断。
4. **程序编译派生物**：所有 replacement 必须命中原文，按一次非级联替换生成 staged `corrected.txt`；候选目录与 specialist 决策合并为 `entity-resolution.json`。重叠、重复、无命中或协议不完整均拒绝，不静默降级。
5. **产物分别校验**：Wiki 与 semantic 仍分别经过 3.4/3.6 validator。任一硬错误只允许带精确错误回到同一个 Meeting Compiler 做一次定向修订；warning 继续走局部确定性修复。禁止另起 Wiki-only 或 slots-only Worker。
6. **后端与恢复**：`INGEST_BACKEND=api` 时脚本把完整文本交给一次 Meeting Compiler 调用；`agent` 时 handoff 只携带只读源文件路径、`prompt + write_to + transaction_id`，宿主只派一个 sub-agent 用读取工具完整读取一次并写回同一协议文件，再调用 `ingest_meeting.py --resume <txn>`。API 返回 rejected 或协议解析失败时也保留失败 trace，并把同一协议转为可恢复 Agent handoff；DSH 使用 `ingest_meeting_resume` 恢复，不创建新事务，也不把长原文复制进 handoff JSON。文件名中的 `YYYYMMDD` 是权威年份；仅 `MMDD` 或无日期时才允许用摄入年推断，并在事务与 Wiki 中写 `date_inferred`。Compiler 提供非空 title 后，程序在落位前重算最终 meeting ID、Raw/Wiki 路径和 sources。
7. **统一落图**：校验后的 semantic 经 `knowledge-ir-v1` → 绑定 IR SHA-256 的 `graph-plan-v1` → 唯一 writer `graph_ingest.py`。Meeting Compiler 不写 Raw、Wiki 或 `graph.db`，也不自报 IR 的确定性字段。
8. **置信与回溯**：会议人物关系继续按 speech-recognition 来源处理；Wiki `sources` 始终指向原始 Raw，不指向 corrected。事实回答必须回溯 Raw，不能把纠错审计或 DSH session log 当事实源。

已有历史 `corrected.md` 保留不动、不回填、不删除，也不作为新流程输入。人物页/别名变化只影响下一次候选召回；历史会议仅在明确重摄入时更新。

1. 读取 `raw/` 中的新文档，理解核心内容
2. **读取对应子项目的 `SCHEMA.md`**（页面类型 + Frontmatter）
3. 在对应 `wiki/` 子目录创建来源摘要页
   - **用骨架脚本生成**:`.scripts/wiki_skeleton.py --page <wiki路径>` 生成 frontmatter+section 骨架(论文类从 paper.md 自动提取 title/authors/sources;确定性字段程序填,语义槽标 `<-- LLM 填 -->`)。LLM 复制骨架后只填语义内容(venue/related/Navigation正文/Content正文),不手写 frontmatter 结构和 section 标题(防格式手写错误)
   - **行政 Navigation 生成约束**：行政页的 Navigation 面向 query，不是正文摘要；用 2–4 句、约 80–150 tokens 说明文档/事项类型、核心主题/问题类型、用户可能使用的同义词/简称；来源明确时补关键人物/部门、时间/状态和关联文档。只能依据 raw，不得新增事实或复制正文。LLM 可先按结构化槽位生成(kind/topics/query_terms/actors/status/related)，再合成为自然语言；`ingest_check.py` 只读检查长度和主题/查询表达提示，不合格时仅定向返修 Navigation。
   - **标准 section 结构**(见各 SCHEMA「标准 section 结构」,2026-07-19):正文按 `## Navigation`(导航概述 2-4 句,80-200 tokens)/ `## Content`(正文,原 `## 中文编号` section 降为 `###`)组织
   - **section 标题必须独立成行**:Navigation 段文本末尾不得接 `## Content` 标题——二者必须分行,否则 ingest_check 漏检 Content 段(heredoc/手写易犯);`sources` 留 frontmatter。编码阶段先写 Navigation+Content,语义槽(`<<<SLOTS>>>` 分隔符)留巩固阶段产出
   - **重要断言加锚点**：对页面中每个关键声明（方法定义、实验结果、政策条款、决策结论等），在其后添加 `{:#<slug>}` 锚点，使后续引用可精确到段落级别
   - 锚点 slug 命名：简短英文或拼音，如 `{:#mpe-definition}` `{:#budget-2026}`
   - **来源标记（必填）**：根据 sources 字段判定并填写 `source_type`，并按来源类型设默认 `confidence`（取值规则见 `academic/SCHEMA.md`「source_type 与 confidence 取值规则」）：
     - sources 含 `.txt` 且位于 `conferences/` 或 `discussions/` → `source_type: speech-recognition`，`confidence: medium`
    - 正式文档（.pdf/.docx/.doc）→ 默认 `source_type: official-doc`，`confidence: high`；强会议速记标记命中的文档例外为 `speech-recognition`、`confidence: medium`
    - 其余（ocr/web/discussion）→ `confidence: medium`
   - **作者提取（论文类必填,2026-07-23 新增）**：`paper-summary` 页从 raw 论文标题下方作者行**忠实提取全部作者**，填入 frontmatter `authors` 字段：
     - 逐个提取，不省略、不跳过；去掉上标/脚注符号（*,†,‡,§）与机构编号
     - 保留姓名原始写法（如 `Z. Y. Xie`、`Guifré Vidal`），不臆测不补全
     - **禁止跨论文串号**：只读本篇 raw 的作者行，不从 related 指向的其他论文复制作者列表（批量摄入时尤其注意）
     - review/group/专利/综述类页面无标准作者行时可省略 `authors` 字段
   - 正文 `> **作者**: ...` 行与 frontmatter `authors` 必须一致
   - **论文书目近端证据**：PDF 第一页同时出现 `received` 与 `published` 时，frontmatter `date` 取 published year；APS DOI `10.1103/<journal>.<volume>.<article>` 由程序确定性补齐 venue。`ingest_check.py` 会对 source.yaml 的 published/DOI 证据与 wiki date/venue 做一致性校验，冲突为 ERROR。

3.5 **冲突检测（增量触发,2026-07-19 新增）**：编码后、巩固前,判定新内容是否与已有 wiki 冲突(更新策略三级分级入口)
   - 读取新页面 frontmatter 的 `related` 字段指向的已有 wiki(通常 2-5 个),与本次编码提取的实体/数字/结论做增量比对
   - **不做全量扫描**(成本 O(n),仅读 related 指向页面,成本可控)
   - 判定 5 信号:① 数字矛盾(指标/数据不一致) ② 时间标记(新旧文件日期) ③ 实体相同结论不同 + 有 raw 证据 ④ 实体相同结论不同 + 无 raw 证据 ⑤ 新增视角无矛盾
   - 无冲突(信号 ⑤ 或无信号) → 继续步骤 4 正常巩固
   - 有冲突(信号 ①②③④) → 转入「冲突处理分支」,按三级分级处理后再继续巩固

### 阶段二:巩固(Consolidation)— 提炼抽象,跨文档

产出:必要的概念/作者/对比页、index 与 graph.db 导航边；仅在有清晰稳定 Scope 时维护 Topic Hub。

**Source Wiki / Synthesis Wiki（逻辑角色）**：papers、会议和单文档摘要是 source-local 页面，回答“该来源说了什么”；reviews、comparisons、concepts 等跨来源页面是 synthesis 页面，回答“知识库如何综合理解”。两者不另建物理数据库，也不复制 Graph 关系。`graph_metrics.py consolidation --json` 只根据多来源复用与 Hub 累积产生 `PROMOTE_CONCEPT`、`PROMOTE_PROPOSITION`、`CREATE_REVIEW` 候选，必须由 Agent 审核后另行创建或更新。

3.5 **语义原型路由 + 拒绝判断（v5,2026-07-27）**：判断本篇文档的信息行为，选 Schema 包（见 `cross-domain/SCHEMA.md` 语义原型段）
   - **多标签**：一篇文档可同时命中多个原型（会议纪要=事件+规范+任务）
   - **拒绝判断**：无法归类时输出 `type: unknown`，建立 Raw 文档包节点；若已有对应 Wiki 页，则建立 `Wiki → 来源 → Raw` 直连边。不强行提取关系或 Hub 归属
   - **query 侧**：命中 unknown 文档时仍返回图里已有的最相关连通信息（拒绝判断 ≠ 拒绝提供已有信息）

4. **识别派生节点 + 查图核对 + 两轴分类**（v3，2026-07-25 图模型）— 先识别判定，再分类落地，不混识别与创建
   - **A1 识别候选派生节点**（只列清单，不判断）：从本篇 wiki 内容中列出所有候选东西（人名/方法名/概念/项目/机构等），不做"值不值页"判断
   - **A2 查图核对**（只读，新增）：对每个候选跑 `.scripts/query_graph.py search <name>` + `neighbors <name>`，核对图里是否已有规范名/对应页
     - 查到规范名 → 后续语义槽裸名用规范名（对齐页 title/aliases，提高 graph_ingest resolve 命中率）
     - 查不到 → 起新名（概念惯用名，带语义）
     - **实体校验**（纪要 author）：首次出现人名先查图核对，避免为同一人建重复页或误判身份（研究生 vs 合作者），判断不准时标 `confidence: low` 或留空待用户确认
   - **A3 两轴矩阵分类**（替原单三档）：
     - **轴1（页面维度）**：值单独成页→建/更页（page 节点）；不值单独成页→entity（若值节点，命名且会复现的概念）或丢弃（描述性短语）
     - **轴2（聚合维度）**：只在已有 Hub Scope 路由明确时连 Hub；新 Hub 由独立维护任务中的主 Agent 决定
     - 两轴独立：页面/概念节点可不属于任何 Hub
     - **裸名须带语义**：命名实体（人/物/概念/方法名），禁描述性短语当 object（如"赵宇科研态度(...)"不当节点）；裸名禁用 `/ \ [ ] { } | < >` 转义字符
5. **级联创建**（轴1=值页的候选落地）：根据内容类型触发关联页面（见下方"级联创建策略"）
   - **people 页两种深度**（定位见 `academic/SCHEMA.md`「people 页定位」）：
     - **纪要 people 页**（深挖）：如该人物在纪要中使用了昵称/英文 ID（如 qiaotete），填入 `nickname` 字段支持昵称反查；识别 `师生`、`合作者` 关系（谁指导谁、谁与谁合作），填入 `role`/`collaboration`/`advisor` 字段
     - **论文 people 页**（极简指针）：与张明远共同署名过至少一篇论文的作者全部建页；信息从论文作者行直接提取（name/institution/papers），不深挖师生/职称/同名核实，末尾标注"检索辅助定位，未做独立核实"
6. 更新 `wiki/index.md`，添加新条目（确保描述行包含核心关键词，信息密度足够搜索命中）
7. **（已移除，2026-07-25）**：原关键词索引维护步骤已删除——`keywords` 字段已去(功能被图邻接节点覆盖),`tags` 字段已去(功能被 hub↔page 边覆盖)。检索入口改走图 search(title/aliases),主题归属改走步骤 9 hub↔page 边。原 `keyword-index*.md` 已删除（融进 graph.db）。
8. **建边（graph.db 主数据化，2026-07-25 v4）**：边只在 graph.db（不再写 md `## Core Triples` 段）。LLM 产临时结构化片段 → 程序入库
   - **统一 IR/plan（2026-09-03）**：paper、meeting、document 可继续使用不同 prompt 与语义槽，但入图前一律编译为 `knowledge-ir-v1`，并生成绑定该 IR SHA-256 的 `graph-plan-v1`。常规摄入把两者写入 `temp/inbox-state/<txn>-knowledge-ir.json` 和 `<txn>-graph-plan.json`。语义 sub-agent 也可直接提交 `--knowledge-ir` 提案，但 page/profile、确定性 metadata、canonical 标记、locator 与 Raw 结构关系均由程序依据已落盘 Wiki 重编译；未通过校验时在打开数据库前停止。
   - **Wiki 桥接 Raw（v13,2026-08-25）**：弱 LLM 输出短格式 `主体|谓词|客体`，并为 keyword 提供 `概念名|一句文档局部说明`；不负责猜 locator、作者、期刊或日期。Wiki 页的事实段用脚注引用程序提供的 Raw handles；Graph 建立 `Wiki → 来源 → Raw 文档包` 直连边，程序再从 Wiki section 脚注为局部概念说明选择 Raw locator。其他人物、概念、Hub 等节点主要与 Wiki 相连。知识边可在 `edges.source` 写 Wiki section 或 Raw locator，也可留空；Wiki 脚注才是事实回溯 Raw 的主契约。`edge_evidence` 仅为历史兼容，不再由新摄入写入或作为校验要求；`edge_origins` 只记页面贡献 lineage。
   - **论文方向标签自动丢弃（v4,2026-07-25）**：论文语义槽中的方向谓词（`研究关键词`/`主要研究`/`涉及` 等）被 `graph_ingest` 自动过滤（`retired_semantic_tags_ignored`），不建边。论文方向只由 Hub Scope 路由（`hub_semantics.route_paper`）决定：论文 `## 研究方向定位` 句 vs active Hub `## Scope`，cosine ≥ 0.5（`ROUTE_FLOOR`）即写 `论文→主要研究→Hub`。
   - **通用文档 graph 巩固（v8,2026-09-01）**：通用文档页的语义槽统一三元组格式（`主体|谓词|客体`），keywords 由代码从三元组提取（kw 谓词的 object），不单独列主题段。谓词按域配置（`DOMAIN_CONFIG`）：行政 kw 谓词 `涉及/讨论/形成决策/推动/申请事项/适用对象`；教学 kw 谓词 `涉及/讨论/涵盖/考核`；商业 kw 谓词 `涉及/讨论/分析/规划`。程序校验并补来源、解析节点、去重；keyword 上限 15，导航关系上限 8。代码驱动摄入见 `ingest_document.py`（`--subproject academic|admin|teaching|business`）；其中 academic 必须显式分类为 `editorial|academic-reference`，不确定时返回 `classification_required`，agent 零监控。
   - **上下文复用（v6,2026-07-27）**：即时巩固时 raw/wiki 已在会话上下文（编码阶段已读），产 triples 直接引用证据段，**不重读 raw**；仅延迟巩固（跨会话，raw 已淘汰）需重读。复用仅限 in-context；若会话过长早期 raw 被淘汰，重读合法（非 bug）
   - **文档内共指**：LLM 读全文后统一指代（"张教授"和"张明远"是否同人），再产 triples；不逐段独立处理名字
   - **subject/object 裸名遵循步骤 4 A3 两轴判据**：值页的用页 title/aliases 对齐（提高 resolve 命中）；不值页的用带语义概念惯用名；描述性短语禁当 object
   - **程序入库**（`.scripts/graph_ingest.py --page <path> --triples <临时文件>`）：resolve 裸名→页path + 去双向冗余 + 去重复边 + alias 自动识别（层1机械抽取 title括注/wikilink显示名）+ INSERT 知识边
   - **resolve 归一化匹配（`graph_lib.resolve_bare_name`，2026-08-03 新增第5层）**：精确匹配（title→alias→suffix→suffix-prefix）miss 后，按缩写+去标点中英文做归一化比对。解决同一概念不同写法（如 `矩阵乘积态（MPS）`/`矩阵乘积态(MPS / matrix product state)`/裸缩写 `MPS`）碎片化为多节点的问题。仅对唯一节点匹配，多候选返回 None 避免歧义。派生概念（如 `正定MPS`）不误匹配。
   - **keyword 别名主动同步（`sync_keyword_aliases.py`，2026-08-03）**：每次摄入前扫描 entity 节点，按缩写+去标点名分组，同组多节点→取边数最多的为规范名，其余写入 `aliases` 表（幂等）。别名表是 `resolve_bare_name` 第 2 步的数据源——主动维护使 LLM 写不同写法时 resolve 直接命中规范名，不建碎片节点。独立入口 `python3 .scripts/sync_keyword_aliases.py --apply` 可手动全库同步。
   - **多形态 alias 即时拆解（`graph_lib.decompose_name_to_aliases`，2026-08-10）**：建实体节点时即时把拼接概念名（如「矩阵乘积态matrix product state(MPS)」）拆为缩写、中文、英文全称多形态 alias 并注册到 `aliases` 表，使 resolver 输入任一形态（缩写/英文全称/中文）都能命中同一节点。与 `sync_keyword_aliases`（事后全库补建）互补——前者摄入时即时覆盖，后者定期扫漏。
   - **命题裸缩写消解（`resolve_abbreviations.py`，2026-08-06）**：命题 path/title 中的概念须用 keyword id 表示。裸缩写双层校验（均限本知识库，不上网）：①图层 `resolve_bare_name` 查 alias 表 + keyword 节点，命中则已建立关联；②raw 层 `extract_abbreviations` 提取命题源页 raw 关键段的缩写定义。两层均 miss → warning（知识库内无全称可溯，需人工判断是否新建概念）。`--list` 列出待消解项，`--apply` 批量消解 raw 有定义的缩写（建 keyword 节点 + 更新命题 path + 建包含边）。非阻断后置：摄入照常跑完，agent 看 warning 后定期批量处理。
   - **可选边定位**：知识边的 `source`/locator 可使用 `wiki/page#heading-slug` 或 Raw locator，也可为空；不得因缺失而报错或告警。事实下钻优先读相邻 Wiki section，再沿该节脚注精确读取 Raw。Markdown/TXT Raw 使用标题或不可变行范围，文本型 PDF 可使用页码范围；论文一律使用 MinerU `paper.md`。`#全篇` 仅是旧式文件级 provenance，不是可向 LLM 返回全文的读取指令。结构性 `包含/相似` 属程序派生，confidence=`推断`。
   - **稀疏导航连通性**：Graph 只保存检索导航关键边。命题中被确定性匹配的概念不得因“保证可达”被自动提升为 `论文→研究关键词→概念`；论文经 proposition/`包含` 已可导航。未直连概念只进摄入报告 `navigation_connectivity_candidates`，不写事实边。
   - **无 page 人物**：程序仅将作者/通讯作者/参会/指导/师从/所属/任职于等人物关系产生的无页 entity 标记 `entity_subtype: person`；数量由 `.scripts/person_entity_audit.py` 审计，软上限 2000、预警线 1600。高影响人物由 `ingest_common.detect_people_page_candidates` 在每次 ingest/query 后自动检测，达标者写入 `cross-domain/people-pending.jsonl`（标准：≥6 篇论文作者 / ≥4 篇通讯作者 / ≥3 次会议提及 / ≥2 种人物关系类别，排除所属/任职与占位符）。达标者由 `build_people_pages.py` 自动建极简 people page 并迁移 graph node path，无需人工干预；slug 冲突（同名不同人）跳过留待人工。
   - **Hub 页**：通过语义三元组建立 hub↔page 边（谓词由页面与 hub 名的语义关系定，如`涉及`/`主要研究`）
   - 关系类型见下方「通用图边约束」及按域派发的关系规则
   - **edge confidence**：每条带 `[可追溯]/[推断]/[存疑]`（默认 `[可追溯]`，推断显式标）；`[SR]` 标记从本页 `source_type` 派生
   - **师生关系**是直接语义边（`导师 → 指导 → 学生`）；合作关系走 junction node 模式（不存派生合作边，见下方关系类型表注）。边 locator 可选，事实回溯走相关 Wiki 脚注

8.5 **引文提取(论文类)**:从 raw paper.md 的 References 段提取引文,建 citation-only 节点(含标题) + 引用边,让引用网络不因被引端未摄入而断链
   - **提取策略:LLM 写模板,代码提取**(语义-确定性分离)。`extract_citations.py` 内置 venue 锚点表 + 姓氏边界正则,机械提取 author/year/venue/title,零 LLM 成本。期刊名是有限集合,作为确定性锚点;标题靠"venue 锚点定位 + 姓氏边界(2+字母词)分隔作者列表与标题"提取
   - **标题规范**:有标题的记录标题(PRL 短式引文源中无标题的留空占位,不硬猜)。标题存 nodes.title 字段(裸名仍是 path 主键)
   - **未命中分类**(失败显式化,记 `.scripts/citation-templates/unmatched.log`):
     - `no-title-in-source`:venue 命中且找到作者边界但无标题文本(PRL 短式,源本无标题,终态)
     - `template-gap`:venue 命中但作者边界未找到(有标题但提取失败,可回补)
     - `no-anchor`:无 venue 但 author/year 在(节点建好,title 占位)
     - `unparsable`:author/year 全缺(当场 LLM 兜底填属性,模板扩展留建设期)
   - **命令**:
     - 机械模式(推荐):`extract_citations.py mechanical <paper.md> > citations.json` → `graph_ingest.py ingest --page <page> --citations citations.json`(全自动建边+补title,零LLM)
     - prefill 模式(复杂格式):`extract_citations.py prefill <paper.md>` → LLM 填 JSON → `graph_ingest.py ingest --triples <JSON>`
     - 回补:`extract_citations.py backfill-titles`(全库扫描,补已有引文节点的空 title)
   - **跨论文补全**:摄入时若引文节点已存在且 title 为空,而本次提取有 title,自动补全(`--citations` 模式内置)。`backfill-titles` 全库扫描也能跨论文补全(论文A无标题、论文B有标题→取B的)
   - `graph_ingest.py ingest --triples <JSON>` 入库:引文节点自动建(entity),引用边+发表于边入库
   - **auto-merge**:若该论文此前作为引文节点存在,ingest 时自动吸收(步骤 8 的 graph_ingest 已内置)
   - **膨胀控制**:只建被本库论文引用的引文节点,不凭空建;引文节点 source 指向引文列表路径(非 raw)

9. **Topic Hub 维护**：普通 ingest 只向已有 canonical Scope 的 Hub 路由，不自动创建、改写 Scope、分裂或合并。代码可报告候选、代表成员与 route probes；主 Agent 在独立 Hub 任务中确认 title、Scope、parent 和写入意图。详见 `operations/HUB.md`。
   - **更外层导航视图(L3)提示**：若巩固时发现某主题 L2 review 已达 HUB.md「更外层导航视图(L3)触发与生成」的触发信号(导航成本超预算/候选集过大/单页过长/层内成稳定簇)，向用户建议建更外层导航视图(地图非综述，只存路由指针)

### 收尾

10. 追加 `wiki/log.md`，记录本次操作
11. **即时校验(确定性壳)**：对本次摄入/修改的每个 wiki 文件运行 `.scripts/ingest_check.py <file1> [file2 ...]`(只读,不修改文件)。只校验**确定性结构**(frontmatter 必填+枚举、路径/type 一致、标准 section、wikilink 悬空、来源存在/配对/locator、日期、status/superseded_by 一致),不查语义(语义正确性仍由 LLM 把关)
    - **ERROR 阻断提交**：有 ERROR 须按报告定向修复后复检,全过才继续步骤 12。典型 ERROR:缺 `source_type`(Q9 教训)、枚举非法、`status:deprecated` 无 `superseded_by`、`##` section 重名、新页(created ≥ 2026-07-19)缺 `## Navigation`/`## Content`
    - **WARN 不阻断**：旧页缺标准 section(渐进迁移)、语义槽缺失(可能延迟巩固)、悬空 wikilink、sources 路径不存在、覆盖度锚点缺失(v6,见下)、提取引擎非 mineru(v8,见下)——建议修,不卡流程
    - **提取引擎检查(v8,2026-07-30)**：ingest_check 对 paper-summary 类 source 的 `parse_meta.yaml` 检查 `preferred` 字段,非 `mineru`(extractor 默认引擎，MinerU 失败重试3次后不回落低优先级引擎)报 WARN,建议用 mineru 重提取。无 `parse_meta.yaml` 的 source(会议纪要/docx/web 等非 PDF 提取场景)不触发
    - **覆盖度锚点检查(v6,2026-07-27)**：ingest_check 对 paper-summary 类自动检查 raw 锚点(标题共同词/作者集合),缺失报 WARN。作者优先使用相邻 source.yaml 中已锁定的 bibliographic.authors，并报告实际缺失姓名；锁定作者不可用时才回退 paper.md 机械提取，避免把 affiliation 碎片算作作者。此检查是脚本检查(零 LLM token),非全读;语义级覆盖判断由 LLM 在上下文内复用 raw 完成(见 step 8 上下文复用),不二次读 raw
    - **来源/图联动检查**：默认检查来源实体、PDF/Markdown 配对、符号链接和已填写 locator；追加 `--graph` 后，检查 `sources` 对应的 Raw 文档包节点及 `Wiki → 来源 → Raw` 直连边，并继续检查论文作者集合/venue/方向 Hub 冲突与占位符元数据节点。知识边 locator 不要求填写；方向边必须指向唯一且自洽的 Hub path。
    - **附带发现上报**：执行任一指令时附带发现的 WARN(含本次校验及顺路看到的结构缺陷)，指令末向用户提示修复建议(只提示,不自动扩面修复)，不静默压到 LINT 周期。趁文件在上下文内修复成本最低；LINT 仍负责未被附带发现的全库积压
    - **分工边界**：即时 check 只保本次摄入**局部事务正确**;全库健康(孤立页/重复概念/长期未更新/过期政策/Hub 超预算)仍由离线 `LINT` 操作负责,勿每次摄入全库体检
12. **派生同步（v4，2026-07-25 主数据化）**：边已在步骤 8 增量入库，无需重建
    - **不再有图重建步骤**：graph.db 是边唯一源，步骤 8 增量加边即完成；原 `graph_build.py --build --apply` 全量重建已废弃
    - **不再有全局 triples / keyword-index**：`triples*.md`/`keyword-index*.md` 已删（融进 graph.db），`rebuild_triples.py` 废弃
    - page-catalog.md 仍由 `ingest_build.py --catalog` 派生（frontmatter 节点列表，人可读视图，不涉及边）
    - **待补清单更新**：跑 `.scripts/graph_ingest_status.py --write` 更新 `cross-domain/ingest-pending.md`（标记历史遗留未进图的 md；本次 ingest 的页自动从清单移除。新摄入文件走标准流程直接进图，不进此清单）
    - **统一后置维护（inbox/直接 resume）**：至少一个文件真正 `completed` 后运行 `ingest_inbox.run_post_ingest_maintenance()`；缩写待办按精确 token 原子去重，raw 内已有定义的自动消解，其余写类型化 Agent review；人物页继续代码生成；Hub 新建与稳定超限分裂写 Agent handoff。完整回执进 `temp/inbox-maintenance/`，紧凑结果分别保留 `file_status` 与 `maintenance.status`，维护失败不得伪装成文件摄入失败或被静默吞掉。

13. **自检**：检查本次创建的页面中所有 `[[wikilinks]]` 指向的页面是否存在，缺失的立即补充(步骤 11 已程序化覆盖悬空检测,此步作为语义复核：确认悬空是真缺失而非跨域别名)

14. **可疑确认（按风险排序，v5,2026-07-27 修订）**：摄入末尾，对可疑项按风险排序后找用户确认（非平均审查所有项）：
   - **高风险（立即确认）**：模型最不确定的（confidence=[存疑]）× 连接重要节点最多 × 一旦错误影响范围最大
   - **中风险（批量确认）**：待核实别名/人名（corrected.md aliases 块 `status: pending`）、关系待核实的（如"杨老师"指代不明）
   - **低风险（保留待观察）**：标了 `[存疑]` 但低频/低影响的术语
   - 对话内按风险分级列给用户确认/驳回，不引入新工具
   - 用户确认的别名 → 写入 `graph.db` aliases 表（status=confirmed,resolved_name/evidence/confirmed_date 记审计）;不再写 people-aliases.yaml(已删)
   - 回填：别名确认后扫描历史 wiki 参与者行，将裸名替换为 wikilink 并补建参会边

### 巩固模式:即时 vs 延迟

| 模式 | 触发 | 执行范围 | 适用场景 |
|------|------|---------|---------|
| **即时巩固**(默认,现状行为) | 高显著性文档(命中 ≥2 条显著性信号) | 阶段一+阶段二一次完成 | 引入新方法/新作者/新关系的核心文档 |
| **延迟巩固** | 低/中显著性,或需跨多篇累积 | 仅阶段一(编码);阶段二留给后续批量 | 边缘实体、待多篇积累后再抽象的概念 |

**延迟巩固机制**:
- Ingest 时若选延迟巩固,只做步骤 1-3(编码),跳过 4-9
- 在 `log.md` 记录 `consolidation: pending`,标注待巩固原因(如"需跨多篇累积""边缘实体")
- 后续可通过 **Scan 操作** 或手动触发批量巩固:对多个 `pending` 文档一次性执行阶段二,实现跨多篇累积压缩。累积压缩的层级生长(更外层导航视图 L3)触发规则见 `operations/HUB.md`「更外层导航视图(L3)触发与生成」
- 延迟巩固期间,该文档仅有 primary summary,可被检索但无级联关联——符合"遗忘是降级非删除"(低显著性不级联,但 raw+summary 保留)

**显著性判定信号**(决定即时/延迟,详见 `thesis.md` 写入策略):① 新方法/框架 ② 新作者/机构 ③ 新关系(三元组) ④ 新数据/实验 ⑤ 新数字/指标。命中 ≥2 → 即时巩固;命中 ≤1 → 延迟巩固。

---


## 批量摄入子流程

> 触发：一次性摄入 ≥3 篇同类文档（如批量补文献）。核心策略：默认延迟巩固 + 编码最小读取 + 分级详略，摊薄单篇开销。这是「巩固模式:即时 vs 延迟」的场景化落地，不替代单篇即时巩固。

### 编码阶段最小读取

**提取成功即质量保证**：MinerU/docling 返回 `✅ 写入 paper.md` 时输出质量已由引擎保证。检测只做三件事（~100 tok）：文件存在 + size 合理（>5KB）+ 标题行正确（`grep -m1 "^# "` 非乱码，如不是"References"/乱码碎片）。**不读全文验证质量**——raw 读取仅服务于 wiki 编码所需的定向段（abstract + method + results），不为"确认没乱码"额外读。

论文结构规整，编码时不读全文（60-90KB），只读关键段送入上下文：
- Abstract（核心贡献一句话）
- Method 关键段（方法定义/框架）
- Results 表（主要数字）

**必须按 section 定向截取，禁止头部切片**：

**用脚本截取**:`.scripts/read_paper.py <paper.md路径> [sections...]`
- 默认截 6 段(abstract/introduction/method/results/discussion/conclusions),模糊匹配标题写法(罗马数字/编号/中英文)
- 一次调用替代多次 grep+sed,省工具调用;报告命中/未命中(防漏读);Abstract 无 ## 时从 preamble 回退提取
- stderr 输出诊断(命中哪些/未命中哪些/合计 token),stdout 只输出 section 正文
- **禁止 `sed -n '1,Np'` 头部切片冒充定向读取**——头部切片靠"论文核心前装"运气,长综述(如 RMP/AIP 综述,正文数百节)会漏掉方法/结果段,产生无 raw 证据的论断(违反可追溯原则)

section retrieval 思想用于 raw 上——只把关键段计入上下文 token,非整文件读取。跳过 Related Work / References / Appendix。
未命中的 section(如无独立 Discussion 段)是正常的,不补读——不同论文结构不同,不必凑齐。

**缩写提取（v2,2026-07-23 新增）**：读关键段时同步提取缩写配对，写入 frontmatter `abbreviations` 字段（`abbr: TNR` + `full: "Tensor Network Renormalization"`）。
- **规则**：只提取 raw 关键段（Abstract/Method/Results）里出现的缩写，不扫全文——与 wiki summary 同源，可追溯到 LLM 实际读的内容
- **机械提取**：`.scripts/extract_abbreviations.py <page> --apply`（括号注解型 `Full Name (ABBR)` + 中文 `全称（ABBR）`，首字母验证+出现≥2次过滤噪音）
- **无缩写不强加**：raw 关键段无明确缩写定义（如纯中文综述/专利）则 `abbreviations: []`，不编造
- **原文拼写错误纠错**：全称从 raw 提取后,对照已知纠错表(`.scripts/extract_abbreviations.py` 的 `RAW_TYPO_FIXES`)纠正 full 字段为正确拼写,同时用 `raw_form` 字段标注原文错误形式(可追溯)。仅纠正确认的常见 typo,不做通用拼写检查(学术术语不在通用词典)
- **缩写并入 aliases（v4，2026-07-25 主数据化）**：缩写（abbreviation）是别名的一种，并入 graph.db `aliases` 表。ingest 时 `graph_ingest.py` 层1机械抽取 title 括注/wikilink 显示名自动识别 alias（含缩写），碰撞记冲突不覆盖。查询走 `query_graph.py search`（查 title + aliases 表）。不再有 frontmatter `abbreviations` 字段，不再有 `abbreviations-index.md` 派生索引

### 分级详略（按威胁度）

对标文献按与 KR Wiki 框架的重合度（威胁度，见 `projects/kr-wiki-paper/notes/status.md` 排序）分级：
- **高威胁**（维度重合）：详写「六、关联」对比表 + 完整差异化分析
- **低威胁**（维度正交）：只留 Core Triple 对比关系 + 一句差异化，不写完整对比表

非对标文献（用户研究方向参考）按常规三段，不强制对比表。

### 默认延迟巩固

批量场景默认走延迟巩固（见上「巩固模式」）：先只编码（Navigation + Content，语义槽留空待巩固），triples/索引/对比表留批量巩固。`log.md` 标 `consolidation: pending`，标注待巩固原因。

### 批量巩固

N 篇编码完成后一次性执行巩固阶段（步骤 4-10）：
- triples 批量归位（可跑 `rebuild --apply`，2026-07-22 已解阻）
- `index` 批量更新（keyword-index 已废，融进 graph.db）
- 对比表按分级详略补写
- `ingest_check` 批量校验（不省）

仍走标准校验（步骤 11-13 不省），确定性壳不因批量而降级。

## 更新模式 — 执行步骤

1. 读取新版文件，对比已有 wiki 页面，找出增量变化。**若新旧版存在结论冲突**(数字矛盾或结论不一致),按「冲突处理分支」三级分级判定——版本演进则旧页标 deprecated+superseded_by 新建页,事实错误则直接修正(旧版若仍有参考价值可保留 deprecated 副本)
2. 覆盖更新 wiki 页面的主体内容（摘要、数据、结论等）
3. **保留**：人工手动添加的 `related` 链接、`tags`、注释、Query 产生的分析段落
4. 更新 Frontmatter 中的 `updated` 日期和 `sources` 路径
   - 同时更新页面内锚点：新增声明的锚点，删除已移除声明对应的锚点
5. 如需删除新版已不存在的内容，在该段落后标注 `~~(旧版内容，新版已移除)~~`
6. **关键词索引维护**：同创建模式步骤 7（分层子索引），更新涉及页面的关键词映射
7. **三元组维护**：同创建模式步骤 8，更新涉及的三元组（新增关系、删除过时关系）；[SR] 标记随 source_type 自动应用，不可移除
8. **Topic Hub 维护**：同创建模式步骤 9，更新涉及主题的 Hub 页摘要和导航
9. 追加 `wiki/log.md`，记录 `update | base_name (旧版本 → 新版本)`
10. **收尾校验同创建模式步骤 11-13**：`ingest_check`（不省）→ `ingest_build` 派生同步 → 自检。更新同样改了 frontmatter/triples/section，确定性壳不因更新而降级

---

## 冲突处理分支

检测到冲突时(创建模式步骤 3.5 或更新模式),按三级分级处理。

| 冲突类型 | 触发信号 | 处理 |
|---------|---------|------|
| **事实错误** | 数字矛盾 / 实体相同结论不同 + 有 raw 证据 | 修 wiki 改正(基于 raw 证据),记 log.md 标 `conflict-fix`,updated 推进 |
| **版本演进** | 时间标记(新旧文件) + 新旧都合法(如政策更新、论文修订) | 旧 wiki 标 `status: deprecated` + 填 `superseded_by` 指向新版;新建 current 页;Hub 时间线串联新旧版 |
| **视角补充** | 实体相同结论不同 + 无 raw 证据 / 新增视角无矛盾 | 不改已有 wiki,建对比页(comparison)保留多视角,两边 `related` 互指;不强制合并 |

**判定流程**:
1. 先看是否有 raw 证据支撑新旧结论——有则事实错误,无则视角补充
2. 新旧若都合法(时间标记 + 内容不矛盾只演进)→ 版本演进
3. 数字/数据矛盾 → 事实错误(以 raw 为准)

**与遗忘策略协同**:版本演进中"旧 wiki 标 deprecated"= 遗忘策略的软遗忘(降级非删除,保留可追溯)。更新与遗忘在版本演进场景下是同一动作两面。

冲突更新时若文件重命名,id 机制(远期)保证关系不断;当前用 wikilink,重命名需手动同步引用。

## 级联创建策略

Ingest 时，除来源摘要页外，还应触发的级联页面。

### academic 论文摄入

一篇论文摄入时，除 `paper-summary` 页面外，还应触发：

| 级联页面 | type | 触发条件 |
|----------|------|----------|
| 概念页 | `concept` | 论文提出的新方法/框架 → `wiki/concepts/` |
| 人物页 | `people` | 与张明远共同署名至少一篇论文的作者 → `wiki/authors/` 极简检索指针；纪要中需记录个人属性/师生/合作关系的人物也建页；其他人物默认只建 entity 节点 |
| 对比分析 | `comparison` | 多篇论文使用同一方法时可合并 → `wiki/comparisons/` |

一篇论文平均产生 3-5 个 wiki 页面，形成知识网络。

---

## 通用图边约束

从摄入内容中提取结构化关系，使用以下关系类型：

| 关系类型 | 含义 | 示例 |
|----------|------|------|
| `任职于` | 作者任职于某机构 | 作者Z → 任职于 → 首都师范大学 |
| `就读`/`所属` | 人就读于/所属某机构（指向 entity 机构节点） | 方大智 → 就读 → 首都师范大学 |
| `来源` | Wiki page → Raw 文档包节点（原件与同 stem locator companion 共用一个节点） | cnu-ran-shiju → 来源 → 个人简历(raw) |
| `主讲` | 人 → 课程节点 | 张明远 → 主讲 → 电动力学 |
| `依据` | 政策/决策依据某文件 | 决策A → 依据 → 政策文件B |
| `前置知识` | 概念/知识点有前置依赖 | 量子纠缠 → 前置知识 → 量子态 |
| `应用于` | 某方法应用于某场景 | 张量网络 → 应用于 → 模型压缩 |

只建能脱离上下文独立成立的原子边；派生或间接关系由图遍历推出。graph.db 只保存导航级关联，数值、证明、步骤与背景叙述留在 wiki `Content`；每条边必须能回溯其 raw 或 wiki 证据。

## 学术图边关系

| 关系类型 | 含义 | 示例 |
|----------|------|------|
| `提出方法` | 论文/作者提出某方法/框架 | 论文X → 提出方法 → 矩阵乘积纠缠 |
| `对比方法` | 论文对比了某方法 | 论文A → 对比方法 → 矩阵乘积态 |
| `属于领域` | 概念/方法属于某研究领域 | 矩阵乘积纠缠 → 属于领域 → 量子多体 |
| `发表于` | 论文/专利发表于期刊或会议（指向 entity venue 节点） | 论文X → 发表于 → PRB |
| `作者` | 人是论文/专利的作者(含通讯) | 张明远 → 作者 → MixT专利 |
| `引用` | 论文引用某论文/概念 | 论文X → 引用 → 论文Y |
| `主要研究`/`基于`/`紧密相关于`/`应用于`/`贡献于`/`延伸至`/`涉及`/`探索`/`属于`(兜底) | 论文与研究方向的**关系性质**（指向 topic-hub 子类 research-direction）；谓词由 LLM 选（参考集 + 动态扩充），**紧密程度**由程序查 `predicate_tiers.yaml` 补 tier（不存 edges，查询时派生排序）。原 `属于(主)`/`属于(交叉)` 已废（2026-07-26 迁移） | 论文X → 主要研究 → 强关联电子系统 |
| `研究基础` | 论文建立在某研究点之上 | 论文X → 研究基础 → kagome反铁磁体 |
| `核心方法` | 论文用的关键方法 | 论文X → 核心方法 → iPEPS |
| `核心创新点` | 论文的新发现/贡献 | 论文X → 核心创新点 → 价键晶体VBC |
| `局限性` | 论文识别的局限/不足 | 论文X → 局限性 → 仅考虑最近邻相互作用 |
| `未来展望` | 论文提出的展望/扩展方向 | 论文X → 未来展望 → 可扩展至长程相互作用 |


> **论文→概念谓词**：仅写能准确表达关系且有 Raw 支持的谓词，如 `研究基础`/`核心方法`/`对比方法`。套不上时省略该边，不用通用「研究关键词」兜底。
>
> **谓词集动态扩充聚合裁剪机制**：集本身允许动态扩充、聚合、裁剪（不是封闭死集），但走受控闭环：
> - **日常 ingest**：LLM 按参考集归类，不擅自造新谓词
> - **扩充触发**：某个明确关系在多文档中反复出现 → 提议提升为正式谓词（标注理由+典型实例）→ 加入集
> - **裁剪**：同义/近义谓词合并（靠 `base_predicate()` 前缀匹配）、低频长期无用裁剪回退为兜底
> - **闭环**：不擅自改，走「提议→（开发期）用户确认→加入/回退」
>
> **数量上限（软上限，WARN 不阻断）**：
> - 研究方向：由程序比较可 locate 的研究方向定位句与 Hub Scope
> - 概念边数量只作信息密度提示，不决定 Hub 归属或分裂
> - 超出时 LINT/ingest_check 提示检查是否在堆砌
>
> **概念节点写法（统一格式）**：概念名尽量写为「中文英文(缩写)」，使节点本身语义明确：
> - ✅ `密度矩阵重整化群density matrix renormalization group(DMRG)`
> - ✅ `连续矩阵乘积态continuous matrix product state(CMPS)`
> - ✅ `检索增强生成retrieval-augmented generation(RAG)`
> - ✅ `量子计算quantum computing`  # 无公认缩写则不写括号
> - ✅ `纠缠熵`  # 无对应英文则只写中文
> - ✅ `Born machine`  # 无对应中文则只写英文
> - ❌ `DMRG` / `CMPS`  # 裸缩写 embedding 语义弱，易误匹配方向
> - 规则：有中文+英文+缩写三者时写「中文英文(缩写)」；无公认缩写则写「中文英文」不写括号；无对应中文则只写英文；无对应英文则只写中文
> - 缩写须按论文原文展开为全称，不得自行编造缩写或全称（2026-08-10）
> - 程序侧：裸缩写依次查图 alias 与 Raw 中的 `Full Name (ABBR)` 定义；只有两者都无法消解时保留非阻断 WARN。

> **junction node 模式**:合作关系**不存派生边**。两人合作 = 共享论文/专利 → 论文/专利本身是 junction node → 各存一条 `人 → 作者 → 论文` 原子边,合作在查询时从"共享论文的作者边"推出(图遍历 BFS 一步即得)。派生合作边(如 `A → 合作者 → B`)已弃用,不抽取。师生/指导关系是**直接语义关系**(非共享产物派生),保留直接边；边 locator 可选，事实依据保留在相关 Wiki section 的 Raw 脚注。

> **枢纽节点与原子边规则**：junction 模式从「合作」推广到一切「因某产物才搭上关系」的实体——**只建能独立成立、脱离上下文仍可独立指代的原子三元组边；派生/间接关系不建边，靠图遍历推出**。论文/专利是典型枢纽：作者、方法、概念都因这篇论文产生关系，各只和论文连，不彼此连。具体：
> - **论文枢纽**：论文节点先建且为中心。`人 → 作者 → 论文`、`论文 → 发表于 → 期刊`、`论文 → 提出方法 → 概念` 是原子边（各自独立成立）。
> - **单位只和人连，不连论文**：`人 → 就读/所属 → 单位` 是原子边（「方大智就读于首师大」独立成立）；`单位 → ? → 论文` 是派生（经人这座桥，两跳遍历即得），不建。
> - **人不连期刊**：`人 → ? → 期刊` 是 `人→论文→期刊` 的派生合并，不建边；查「某人发在哪些期刊」走两跳遍历。
> - **判断标准**：这条边脱离上下文还能不能独立指代、独立成句。能 → 建边；它的成立必须借助第三个实体才有意义 → 不建，让那个实体当枢纽。
> - 此规则与反哺体系正交：默认走遍历；仅当遍历路径过长（三跳以上才答得出）时，反哺物化一条 shortcut 边。
> 
> **学术方向 Hub（Scope 路由）**：论文 Wiki 是摄入子图的枢纽。建边分两类：
> - `论文 Wiki → 主要研究 → Hub`：程序只读可 locate 的 `## 研究方向定位` 一句，与 active Hub 的 canonical `## Scope` 比较。top-1 同时通过 threshold 与 margin 才写边，并把该句 locator 写到边上。
> - `论文 Wiki → 核心方法/研究基础/对比方法等 → 概念节点`：只在关系有明确 Raw 支持时保留。不再产生通用 `研究关键词` 谓词。
>
> **Hub 动力学（派生导航）**：Hub 同时是 keyword、proposition、People page 等普通节点的可重叠动态群落。图融合完成后，程序只对本页一跳内的有效类型化 profile 做局部刷新，结合 Scope、同类成员原型与图邻接 affinity，幂等维护 `普通节点 → 聚类于 → Hub`。People page 只有存在可 locate `## 人物画像` 时参与；研究人员、行政人员、学生等按各自角色写画像，无 page 的 person entity 不参与。embedding 不可用时保留旧 membership。
>
> Hub 身份只由稳定 path、简短 title 和一句 Scope 定义。arXiv 方向只初始化根 Scope。新摄入不写 seeds、Hub `## 关键词` 或 catch-all 队列，也不由概念数量自动分裂。未归类、歧义、旧 Hub 无 Scope 和 new/split/merge candidate 都是 soft state，不产生摄入 ERROR/WARN。普通 membership 可由程序自动更新；摄入末期达标候选簇（cohesion≥0.6 且 members≥4）自动触发 agent 生成定义并 create_hub，不达标候选静默进 backlog；split/merge 仍由主 Agent 确认。详见 `operations/HUB.md`。

## 会议图边关系

| 关系类型 | 含义 | 示例 |
|----------|------|------|
| `参会` | 会议纪要 → 参会者 people 节点（仅对有 people page 的建边；`is_sr=1`，`confidence=[可追溯]`；时间靠会议节点 date 字段派生，不进谓词括号） | 0723会议 → 参会 → cnu-wu-xi |

会议人物必须复用 `entity-resolution.json` 的 resolved 结果；会议边带 `speech-recognition` 来源所派生的 `[SR]`，不得重复抽取或臆测参与者。

**会议与通用文档的概念导航**：会议、行政、教学和商业文档可保留 `讨论`/`涉及`/`汇报`/`规划` 等明确语义边，但新摄入不将其 object 同步到任何 Hub，不调用论文方向 embedding，也不写未归类队列。是否建 Hub 由独立的 Hub 维护任务根据查询效用和清晰 Scope 判断，不从词数自动涌现。

## 简历摄入

简历信息密度高且已是结构化文本，**不建 wiki 摘要页**（raw 直接充当 wiki）：
>   - **高质量提取**：从 docx 提取为同目录 `个人简历.md`（类比论文 `paper.md`），忠实结构化不摘要
>   - **Raw 文档包节点**：简历原件与同 stem Markdown companion 合并为一个 `raw` 类型节点；原件路径和 companion 路径都作为该节点 alias
>   - **Wiki–Raw 直连**：people page 作为 Wiki 节点，通过 `来源` 边指向简历 Raw 文档包；其他人物/机构/课程关系主要连接 people page。关系边 locator 可选
>   - **提取内容**：①教育经历→`指导`/`就读` 边 ②学术职务→`所属` 边 ③课程→`主讲` 边 ④人才培养→`指导` 边 ⑤学术专著+学术论文→按引文方式（citation-only 节点 + `发表于` + `作者` 边）⑥邀请报告+基金项目→信息放 people page 正文，仅在有明确关系时建概念边
>   - **版本控制**：简历更新时重摄入，diff 新旧边（git diff 可读）；不建版本快照文件
> 
## 核心导航判据

graph 只提取核心导航，不存内容。判据是「回答定位问题还是内容问题」：
> - **导航关系**（进图）：回答谁/什么/在哪/属于什么/涉及什么——作者、期刊、单位、研究方向、keyword（方法/概念/局限/展望）
> - **事实内容**（留 wiki `## Content`）：回答怎么样/为什么/多少/什么逻辑——实验数值、方法技术细节、证明逻辑、对比数值、背景叙述
> - **关系进图、数值不进图**：对比关系有导航价值时只建导航边（如 `论文→对比方法→VMC`），数值留 wiki
> - 此判据是 graph「关键关联子集非全集」哲学（见 AGENTS.md 知识图谱层）在学术建边的落地

## 节点类型

ingest 确定派生节点时，按类型建节点，建边规则统一（不因类型改变「只建原子边」原则）：

| 节点类型 | type 值 | 是否有页 | 说明 |
|----------|---------|---------|------|
| 人物 | `people` | 有个人属性信息才建 page（page 即节点，不分两个）；否则为 `entity` + `entity_subtype: person` | 替代原 `author`。人≠作者角色，可为参会者/被提及者。有 people page 时 page 直接挂 people 节点；`citation-only` 不计入人物数量 |
| 会议纪要 | `conference-summary` | 有 page（page 即节点） | 会议 wiki 节点，参会人通过 `参会` 边连接 people 节点；无 people page 的参会人暂不入图（裸名保留在 wiki 参与者行，未来建 page 后补边） |
| Raw 文档包 | `raw` | 有节点（无页，是结构节点） | 原件与同 stem locator companion 共用一个节点，各文件路径作为 alias；对应 Wiki page 通过 `来源` 边直接指向它 |
| 课程 | `entity` | 无页 | 课程节点（如电动力学/数学物理方法），通过 `主讲` 边连接 people 节点 |
| 论文/专利 | `paper-summary` 等 | 有 page（page 即节点） | 摄入中心枢纽，所有因它产生的关系挂它 |
| 期刊/会议场所 | `entity` | 有节点，无独立正文 | 具体期刊（如 PRB）各为一个 entity 节点，靠 `发表于` 语义边逆遍历聚合 |
| 单位/机构 | `entity` | 有节点，无独立正文 | 具体单位（如首都师范大学）各为一个 entity 节点，靠 `就读`/`所属` 语义边逆遍历聚合 |
| 研究方向聚合 | `topic-hub` | 有 page | 稳定 path + 简短 title + 必填 `## Scope` 定义身份；普通节点以可重建 `聚类于` 边形成可重叠动态群落，论文方向关系仍用可 locate 定位句；create/split/merge 须由主 Agent 确认 |
| 命题(proposition) | `entity`（`entity_subtype: proposition`） | 无页 | 命题谓词（核心创新点/局限性/未来展望）的 object——论断性陈述，作为节点入图而非阻断修复。ID 用 `extract_descriptive_id`（把内嵌概念名替换为各自 keyword ID；图里已有 keyword 的裸缩写经 abbr_map 同样替换为 id，保留命题结构）；全名作 title+alias。通过「包含」边连它论述的概念 |
| 概念 | `entity`（历史存储子类仍为 `entity_subtype: keyword`） | 无页 | 明确关系（如研究基础/核心方法/对比方法）的名词性 object。节点 ID 是简短明确的词或短语；同名异义用稳定消歧 ID，不再另建「研究关键词」层 |
| 主题聚合 | `topic-hub` | 有 page | 通用导航层，用语义谓词边聚合派生页；研究方向是其子类（见上） |
| 时间线聚合 | `timeline-summary` | 有 page | 行政时间线等，用 hub↔page 边聚合 |
| 概念/方法 | `concept`/`entity` | concept 有页；无页时 entity | 知识节点，靠语义边连网络 |

> **venue / institution 聚合机制（2026-07-27 改）**：venue 和 institution 不再是 hub，统一为 entity 节点。成员关系靠语义边（`发表于`/`就读`/`所属`）逆遍历聚合。hub 的三原则（度数上限/分裂）不适用于 venue/institution——它们是有限集合，靠谓词区分角色，不需要 hub 机制。
> 
> **研究方向 Hub（`hub_subtype: research-direction`）**：详细契约以 `operations/HUB.md` 为准。根 Hub 可由 `arxiv-directions.yaml` 初始化，但页落位后 `## Scope` 是唯一语义真理源；seeds 和旧 `## 关键词` 不参与路由。
> **hub 分类独立原则**：期刊与单位是不同维度，各自独立 type，不混入同一 hub。一篇论文的 `发表于` 指向具体期刊节点（如 PRB），不是指向「期刊」这个总 hub。

> **entity_subtype 二分（2026-08-05 proposition 改革）**：`entity` 节点按 `entity_subtype` 分两类，判据是 trigger 词的有无（脚本只看形式，不调 LLM）：
> - `keyword`（存储兼容名，业务语义为「概念」）：指代一个可独立指代的实体/方法/概念/度量/数据集。ID = `extract_keyword_id`（取简短明确形式）。概念谓词（研究基础/核心方法/对比方法）的 object 归此类，但不同步进 Hub。
> - `proposition`（命题/论断性）：表达「关于某概念的一个论断」。ID = `extract_descriptive_id`，通过「包含」边连它论述的概念节点。
> - **判据三路（`_is_proposition_slot`）**：①命题谓词的 object 直接判 proposition（谓词本身是强信号，跳长度门槛）②结构性谓词（`包含`）的 subject 判 proposition（包含边 subject 按定义是命题）③其他回退 `is_descriptive_phrase`（trigger+长度门槛兜底，仅服务自由边）。这让 22 字短命题不被长度门槛误拦
> - **两类节点正交**：keyword 是图的端点（被引用），proposition 是 reification 节点（把"论文提出X"从边升级为节点，使论断可去重、可追溯）。proposition 可在没有唯一精确概念匹配时独立保留；这不是错误或 warning

## 描述性短语校验

LLM 产出的 concept 端点须是**裸名带语义**（可独立指代的实体名/概念名）；命题谓词的 object 则保留完整 proposition。两者由谓词角色区分，不能由 backend prompt 临时改变。

**四类客体校验（`graph_ingest.py add_knowledge_edges` + `ingest_paper.py step_validate_semantics`）**：
- `descriptive_phrase`：客体含谓语结构或中文标点且超长 → **非阻断型**（2026-08-05 proposition 改革）。命题谓词（核心创新点/局限性/未来展望）的 object 作为 proposition 节点入图，故不进 3.6b LLM 局部修复；融合期只由代码建立稀疏概念包含边。主体含描述性短语亦非阻断。标题型谓词（`TITLE_OBJECT_PREDICATES`，含`引用`）的 object 是论文/文献标题（合法长实体，天然含作者列表逗号/期刊句号），跳过 descriptive_phrase 检测（2026-08-13）。
- `bare_abbreviation`：含英文缩写（≥2 连续大写字母）但无括号释义 → **非阻断型**（2026-08-06 语义改革）。warning 语义从「含裸缩写字符串」改为「含无法 resolve 到 keyword 的缩写」：slot 校验期与融合期双层复查（公共判定 `bare_tokens_resolvable`，2026-08-20），缩写能 resolve 到图里已有 keyword 节点/alias → 移除 warning（已建立关联，slot 校验期即不产生，避免无谓 handoff）；resolve miss → 保留 warning，交后置 `resolve_abbreviations.py` 双层校验（图层 resolve + raw 层提取）。不再进 3.6b LLM 修复。proposition 节点的 alias 不参与 resolve（非概念端点，`build_name_index` 与 `_ensure_entity_node` 双重排除），避免其 decompose 拆出的句内片段（如`MPS`）与概念 alias 冲突造成歧义误报（2026-08-13）。翻译对照全称词豁免（`is_bare_abbreviation`，2026-08-13）：obj 含中文且某 token 重复出现≥2次（如「XX模型XX model」）时，该 token 是中英对照的全称词（模型符号）非缩写，不报——避免把符号化模型名误判为裸缩写
- `citation_fragment`：MinerU 把参考文献条目误建为实体（如 `2019;1:538–550.-2019`）。`is_citation_fragment` 检测「年份;」+「:页码」格式，跳过建节点不连边（2026-08-02 新增）
- `duplicate_line`：完全相同的三角组行。`normalize_slots` 机械去重（首条保留）；`step_validate_semantics` 兜底检出残留重复并再次机械清理。该路径固定为零 LLM 调用。

**判据**：候选客体「脱离本论文能否独立指代」——能独立指代（如「价键晶体(VBC)」「iPEPS」）是合法裸名；必须靠本论文上下文才说得通的是描述性短语（如「1/9平台为价键晶体」「iPEPS 收缩方法」）。

**机械近似（程序级 WARN，graph_ingest.py，2026-08-03 双门槛调优）**：降低误伤的三段判据——长度 ≤15 直接放过（原 8 太严，10-15 字概念名常见）；16-20 仅标点判定（谓语词在此区间多为概念名修饰，误伤率高）；长度 >20 且含触发词（为/是/导致/采用/基于/表明/说明/表示/揭示/发现/应用于/实现/利用/开发/证明/探索/提出/推广到/扩展至/结合/改进）才判描述性短语。实测本批 19 条旧误报降至 5 条真阳性，误伤率 74%→0%。此为近似，漏判由 LINT 兜底；`descriptive_phrase` 只报告，不触发 3.6b，proposition 由谓词角色保留，concept 端点由共享 prompt contract 约束。

**Paper semantic contract（`paper-semantic-v1`，pipeline v12）**：API 的独立 slots prompt 与 Agent 的 combined prompt 都由 `build_paper_semantic_contract()` 注入同一份规则。`研究基础/核心方法/对比方法` 指向可复用 concept；`核心创新点/局限性/未来展望` 指向完整 proposition；概念间关系两端均为独立 concept。旧式“叙述节点 + 两条拆分边 + 一条自由边”不再由 prompt 生成；proposition 到既有概念的稀疏包含边继续由程序确定性编译。

**局部修补 Worker（`semantic-patch-v1`，2026-09-02）**：`bare_abbreviation`/`descriptive_phrase` 非阻断，不调用 LLM；`duplicate_line` 由程序删除重复行并保留首条。只有仍存在其他非机械阻断 warning 时，程序才把整批问题编号为稳定 issue catalog，调用一次 `ingest_semantic_fill`（`retries=0`）。Worker 只能逐个 issue ID 返回最多 4 条完整三元组或 abstain，不能输出完整 Wiki/语义槽、source locator、路径、作者或 venue。程序按 `field` 局部应用并全量复验；同一协议版本、语义文本和 issue catalog 计算 `input_hash`，schema 合格的决策缓存到 `temp/inbox-state/<txn>-semantic-patch-decision.json`，resume 不重复调用。失败直接转 agent 修事务草稿，不走第二模型链。

**Bounded SemanticRecoveryAgent**：仅当确定性清理和上述单次 Worker 后仍有可定位的阻断语义问题时启动。它只读取事务内 staged semantic/Wiki/source 摘要，最多 3 turns、2 次工具调用，重复 `tool+arguments` 立即停止；只返回 `semantic-patch-v1` Proposal，不拥有 Raw/Wiki/Graph/commit 写权限。程序把 Proposal 应用于 staged semantic 后执行完整 validator，失败即恢复原文并进入 decision escalation。API timeout、429、403、schema/空输出和代码异常由程序分类处理，不进入 Agent reasoning retry。

**ghost hub 兜底**：hub 合并删 `.md` 后若 graph.db 节点残留（ghost hub），`cleanup_ghost_hubs` 在每次 `graph_ingest ingest` 前自动清扫（删节点 + 关联边）。独立入口 `graph_ingest.py cleanup-ghosts` 可手动全库清扫。

## 文档子图与主图融合

每次摄入先在内存建立一个 `GraphDelta`，再与主图融合。`GraphDelta` 不是新数据库、事实层或审计档案，进程结束即消失；Raw、Wiki、`graph.db` 仍是唯一三层结构。

**第一步：建立文档子图。** 程序收集 Wiki anchor、同词干 Raw 文档包、`Wiki → 来源 → Raw` 骨架，以及机械边、LLM 提供的少量短三元组和 keyword 局部说明。Worker 只写基于当前文档的一句说明；程序从已校验 Wiki section 脚注机械绑定最贴近的 Raw locator，无 locator 的说明不进入 GraphDelta。完全重复边机械去重。只有不能安全写入的结构问题属于硬错误：缺 Wiki anchor、空端点/空谓词、自环、或已有本地 Raw source 却无法形成 Raw 文档包。弱 LLM 不负责遍历主图、选择 merge 目标、生成 locator、计算图指标或生成检查报告。

**第二步：连接并融合主图。** 程序先按精确 path 和唯一 title/alias/suffix 生成 attach plan；普通知识 surface mention 只解析到 entity，纯引用标题保留 page/entity 双类型。完整双语名再分解为中文、英文、缩写：中文/英文完整名称精确命中的 node ID 并集唯一时复用，完整名称冲突直接 ambiguous；缩写只在完整名称均未命中时兜底。其余轻微名称变体只有在存在代码化名称信号，并同时通过 label embedding、当前局部说明与既有 `title+description` 的 semantic embedding、唯一 top 候选和分差门时才复用。精确名称出现多候选且无法消歧时 `abstain_ambiguous`，跳过该边且不创建同名碰撞节点；没有精确名称碰撞、只是 semantic gate 未过时用 `keep_local_ambiguous` 保留本地 keyword、原始边和 gloss。融合前在内存 overlay 上运行小型 query probes：Wiki anchor 能否定位、Raw 能否一跳到达、边界节点能否在两跳内回到 Wiki、名称候选负担多大。探针用于观察查询效果，未满分不阻断摄入。

**节点身份模型。** `nodes.path` 是稳定 canonical node ID；`title` 是首选显示名；`aliases(alias,node_path)` 是多对多确定性名称入口，同一 alias 可指向多个节点并在查询上下文中消歧；`node_glosses` 保存每个来源页的一句话局部说明及其 Raw locator；`nodes.description` 是由合格 gloss 初始化、用于区分近名异义和 semantic embedding 的主导航说明，不是独立事实证据。后续摄入追加局部 gloss，不盲目覆盖已有非空主描述。title/description 改进不自动改 path，merge 后旧 path 可作为 alias 指向保留节点。

**同名异义边界。** 程序已经持有的 node ID 与 Raw/LLM 抽取的 surface mention 必须显式区分：只有端点带 `subject_is_canonical` / `object_is_canonical` 时才按 path 直接复用；普通 mention 即使字符串恰等于既有 `nodes.path`，仍须同时检查同名 title/alias 候选并结合上下文与 description 消歧。同名异义节点使用 `名称（简短义项）` 作为不同 path、共享基础名称 alias，且 description 必填；上下文不足时软 abstain 并跳过该歧义边，不猜测、不创建同名碰撞节点、不报摄入 ERROR/WARN。

实际写入在 SQLite `SAVEPOINT` 中完成；写入器异常，或融合后缺 Wiki anchor、Raw 节点、`Wiki → 来源 → Raw` 骨架时，整段融合回滚。`graph_delta.validation_receipt` 使用 `graph-delta-v1` / `graph-delta-validator-v1`，内容寻址绑定 delta 与 attach plan，并记录前置检查、后置条件和 SAVEPOINT 状态。`savepoint=released` 只证明局部融合已经验证并释放保存点；`outer_commit_required=true` 表示调用方仍须完成外层 SQLite commit，不能把该回执解释为数据库已经持久提交。报告中的 `graph_delta` 只描述本次事务的 attach plan、query probes 和融合结果，不是答案证据。

子图和主图质量只看代码/LLM 执行 query 是否稳定：anchor 命中、Raw 到达成本、边界路径成功率、同名候选负担，以及弱 LLM 的歧义/放弃率。不得用图密度、平均度、全连通、聚类系数或 locator 覆盖率作为摄入质量门。

## 稀疏命题与节点编译（2026-08-25）

命题谓词（核心创新点/局限性/未来展望）的 object 是论断性陈述，改革后作为 **proposition 节点**入图，不再阻断。这里描述的是进入 `GraphDelta` 前的语义编译，以及 attach plan 确定后的节点写入细节：

**语义槽覆盖恢复（v9）**：先统一 SLOTS 外层与 `三元组:` section 契约；正文仅含完整裸三元组时，程序确定性移除协议尾标记、补入 `三元组:` 后再校验，不调用 Worker。覆盖率只统计该 section 中可解析的 Worker 关系，不计 Wiki frontmatter 确定性生成的作者、期刊等边；混合格式、缺 section 或不可解析行须返回结构化诊断，不得静默计为零。格式与结构校验通过后仍少于 4 条时最多定向重抽一次 semantic slots，重抽 prompt 同时给出准确计数和上一版槽内容，不回到 Wiki 生成阶段，且两次结果只保留三元组更多的一版。第二次仍少于 4 条时允许提交，但必须记录 `graph_semantic_coverage_sparse` 并把 `quality_status` 降为 degraded。

**子图构建（`step_extract_propositions`，纯数据变换，零 DB/零 LLM）**：3.6b 校验通过后、落位前，收集所有命题谓词的 object（经 `_split_on_comma` 拆分），只登记命题数量和 `execution_mode: deterministic`。不改写 semantic，不拆原子三元组，不产生谓词候选，也不因没有概念链接标记 degraded；真实调用数仍只从 ExecutionEvent 统计。

**稀疏包含边**：完整命题始终保留。`add_knowledge_edges` 只把 proposition 链到两类已存在或已确认概念：①本页 `研究基础/核心方法/对比方法` 等概念槽，并复用 `decompose_name_to_aliases` 的中文、英文、缩写；②主图中唯一精确 title/alias 命中的 keyword/concept。ASCII 匹配使用词边界。同一 surface 多目标、无匹配或 embedding 不可用都静默跳过；代码绝不从命题片段新建 concept 节点。

**节点编译（`add_knowledge_edges`）**：遵守 `GraphDelta` 已生成的 attach plan，两遍扫描——第一遍建 `concept_map`（概念全名→keyword_id，供 proposition path 替换内嵌概念）；第二遍复用确定目标或建本地节点：
- trigger 路径 → proposition 节点：path = `extract_descriptive_id`（替换内嵌已有概念为 ID；`abbr_map` 把图里已有 keyword 的纯缩写 alias→node_path 一并替换，使命题 path 引用规范 keyword 节点），全名作 title+alias，`subtype='proposition'`
- 否则 → keyword 节点：path = `extract_keyword_id`（取最短形式），`subtype='keyword'`
- **传递包含**（第二层，代码匹配）：建节点循环后扫描每个新建 keyword 全名是否含 `concept_map` 里其他概念全名（非自身），命中建 `concept | 包含 | subconcept`。确定性、零 LLM 成本，递归有界（长度单调下降）
- **命题包含**：按上一段的稀疏候选建立 `proposition | 包含 | concept`，结构边标为 `推断` 并记录页面 lineage；重复摄入幂等
- **proposition 不被概念 resolve 命中**：归一化匹配会因 abbr 相同误匹配（概念 `矩阵乘积态...(MPS)` abbr=MPS 误命中命题 title 含 (MPS)），故 resolve 命中后查 subtype，proposition 目标 discard 继续建 keyword
- **确定性元数据 subtype**：GraphDelta 锁定的 person/venue/institution endpoint 在新建或复用时必须写入对应 `entity_subtype`；不得让期刊、会议或人物节点以空/keyword subtype 进入普通 Hub membership。

**命题级去重**：描述性 ID 天然让"不同论文提出同一命题"合并到同一节点（两篇都证 ANTN 超越 MPS → 同一 `证明ANTN超越MPS`），措辞不同的命题保持分离。这是把"知识涌现"从概念层（一维）扩展到论断层（二维）。

**Claim Promotion Policy**：单篇摄入产生的 proposition 默认是 source-local 导航论断，不因长度、embedding 分数或单次 query 自动晋升。只有被至少两个来源页复用，或后续由 Agent 确认为比较轴、冲突对象、Hub 核心结构时，才进入 promotion 候选。现阶段保留 source-local proposition 以兼容精确查询，不批量删除历史节点；`graph_metrics.py consolidation` 只读报告生命周期候选。

## graph.db 边格式（v4，2026-07-25 主数据化）

> **架构切换**：边只在 graph.db（不再写 md `## Core Triples` 段）。md 存节点属性，graph.db 存边。LLM/语义 sub-agent 只产类型化语义提案；`knowledge_ir.py` 统一编译并校验 IR，`graph_ingest.py` 生成 hash-bound plan 后作为唯一核心 writer 入库。论文版本、补充材料和翻译等 Raw 结构边也走同一 IR/plan/外层 SQLite commit，不再由类型管线旁路直写。图是人读用 `graph_dump.py`，查询用 `query_graph.py`。

> **edge confidence**：每条边带 `[可追溯]/[推断]/[存疑]`（关系性质，默认 `[可追溯]`，推断显式标）。与页面级 `confidence`（来源可靠性）正交。`[SR]` 标记由 `source_type=speech-recognition` 派生。详见 `academic/SCHEMA.md`「关系级元数据」。
>
> **边与来源**：`edges` 只保留一条 `(subject, predicate, object)` 语义边。`edges.source` 是可选 locator，可指向 Wiki section 或 Raw 片段，也可为空；事实引用保存在 Wiki section 的 Raw 脚注中。`edge_evidence` 仅保留作历史数据兼容，不是新写入路径、必填字段或审计对象。
>
> **节点 lineage**：页面实际使用的 entity 写入 `node_origins`；只有当前摄入新建的 entity 才进入 `managed_nodes`。`node_glosses` 另存逐来源 description + Raw locator；主 gloss 用于初始化 `nodes.description`。re-ingest 精确撤销本页 gloss，必要时从剩余来源提升替代主 gloss。节点 UPSERT 必须使用 `ON CONFLICT DO UPDATE`，不得用 `INSERT OR REPLACE` 触发外键级联清空 lineage。

```json
// LLM 产临时片段示例（ingest 时产，不入 md）
[
  {"subject": "张明远", "predicate": "指导(硕士,2022-2025)", "object": "王晓晨",
   "confidence": "可追溯", "source": "cross-domain/raw/facts/user-assertions.md#fact-luying-edu", "is_sr": false},
  {"subject": "苏刚", "predicate": "作者", "object": "MixT 专利",
   "confidence": "可追溯", "source": "academic/raw/works/patents/2026-mixt-patent.pdf", "is_sr": false}
]
```

入库后 `graph_dump.py` 输出可读：
```
  张明远 --指导(硕士,2022-2025)--> 王晓晨 [可追溯]  src: facts#fact-luying-edu
  苏刚 --作者--> MixT 专利 [可追溯]  src: patents/2026-mixt-patent.pdf
```

## 引文节点(citation-only)

论文数量庞大,未摄入论文也建节点(信息极简),让引用网络不因被引端未摄入而断链:

- **citation-only entity 节点**:无 wiki 页,无 raw,仅引文列表信息(标题/作者/期刊/年份)
- **命名**:`<first-author>-<year>`(如 `Vidal-2007`),与 references 命名一致
- **标题**:有标题的存 nodes.title(PRL 短式源无标题的留空占位)。`backfill-titles` 可从 source 回溯重提取
- **放 entity venue 节点**:靠 `发表于` 边连 venue(如 PRB/PRL),与已摄入论文连法一致
- **引用边**:已摄入论文 → `引用` → citation-only 节点,引用网络完整
- **提取**:`.scripts/extract_citations.py` 机械提取(venue 锚点+姓氏边界,覆盖常见格式);复杂格式走 prefill(LLM 补)。未命中按四类分类记日志

## auto-merge:引文节点自动吸收

摄入一篇论文(已建 page 节点)时,若该论文此前作为 citation-only entity 节点存在(被其他论文引用过),自动吸收:

- `graph_ingest.py ingest` 在 `upsert_page_node` 后自动检测:entity 节点的 path/title 是否匹配本页 title/alias/paper-id
- 匹配则 `merge_nodes`:迁移引用边(指向新 page 节点)+ 加 alias + 去重 + 删引文节点
- **用户无感**:摄入了引文,引文节点自动被吸收,引用网络不断
- 检测维度:page title / registered aliases / paper-id(路径最后一段)


## 通用来源标记

- **source_type 必填**：所有新建页面必须按来源类型填写 `source_type`（取值见各子项目 SCHEMA.md），`confidence` 按来源类型取默认值，不再凭 LLM 主观判断
- **[SR] 标记不可丢**：来自 `speech-recognition` 来源的三元组，`[SR]` 标记在更新模式下也必须保留，不可移除
- **[SR] 与 edge confidence 共存**:格式 `主体 → 谓词 → 客体 [可追溯] [SR] {authority; temporal}（来源：raw 路径#段落）`。edge confidence 在前(关系性质),[SR] 在后(来源类型),来源指针精确到段落。两者正交不互斥

## 会议来源与 SR 约束

- **昵称反查**：people 页的 `nickname` 字段用于从纪要中的昵称/英文 ID 反查人物，创建/更新 people 页时务必检查并填写
- **关系链完整**：摄入涉及多人互动的纪要时，提取师生/合作者关系，保持知识图谱的人物关系链完整
- **人名校验**：纪要中首次出现的人名先与 `wiki/authors/` 权威实体表核对——避免重复建档或身份误判。详细同音字/变体校验见 `LINT.md` 的 SR 交叉验证检查项

## 通用 raw 路径约束

- raw 是事实层：摄入只从已归档 raw 读取，`sources` 必须记录相对 raw 路径；不得把 inbox 或派生文件写入正式 `sources`。
- 不修改 raw；解析、纠错和 wiki/图写入均为派生产物。外部引用的 raw 不得随意移动或改名。
- **Raw Locator 契约（2026-08-25）**：原始 raw 能稳定局部读取时直接使用原件，不创建副本。Markdown/TXT/YAML/JSON/CSV 支持标题或行范围 locator；有文本层 PDF 支持页码 locator。读取工具只把命中片段交给 LLM。
- **论文 PDF 例外（硬约束）**：学术论文 PDF 无论是否有文本层，都必须经 MinerU 生成高质量 `paper.md`；论文 locator、wiki `sources` 与事实读取统一指向 `paper.md`。PDF 原件同时保留在 raw，只作为原始凭据，不替代论文 Markdown 读取层。
- **Companion 兜底**：DOCX/DOC/PPTX、扫描 PDF 等没有稳定可读原生 locator 的文档，摄入时把忠实提取文本保存为 `<原文件 stem>.md`，与原文件一起原子落到同一 raw 目录。该 Markdown 是 raw 事实包的一部分，不是 wiki、审计记录或临时派生物；成功落位后 wiki `sources` 指向它，原文件继续保留。若原件原生可定位则不生成 companion。
- **Wiki Page Locator 契约**：新摄入的论文和通用文档页按自然主题分节；heading slug 是可重建导航地址，不用 Wiki 行号。事实段落/列表项以 `[^rN]` 引用程序提供的精确 Raw handles，页末 `## Sources` 定义为 `[^rN]: raw/path#Lx-Ly`。机械校验只做闭环必需的三项：heading slug 页内唯一、引用脚注均有定义、Raw locator 可精确读取且非空；不新增 claim card、覆盖率评分或全库迁移告警。
- `temp/inbox-extract/.../doc.md` 只是事务暂存输入；需要 companion 时，manifest 必须同时提交原文件与同 stem Markdown，不能只留下临时文件。
- 具体目录、命名和预处理工具只读本子项目 SCHEMA 与按域派发的补充规则。

## private 领域（物理隔离，v1，2026-08-04）

私人知识库（健康/玄学）物理隔离于主库，规则：

- **独立 graph.db**：`private/graph.db` 是 private 边唯一源；主库 `cross-domain/graph.db` 不含 private 边。
- **不进聚合**：private 不在 `graph_lib.SUBPROJECTS`（主聚合清单），`SYNC` 不扫描 private，`_sync-state.md` 不记录。
- **双向隔离**：
  - private 的 ingest/query 仅操作 `private/graph.db`：`graph_ingest.py ingest --page private/wiki/... --db private/graph.db`（不传 `--db` 时按 page 路径自动选 private 库）；`query_graph.py search <term> --db private/graph.db`。
  - 主库的 ingest/query 默认连 `cross-domain/graph.db`，不读 private。
- **路由**：`route.py --task ingest --subproject private --content other ...` 派发 `private/SCHEMA.md`（论文 PDF 仍走 `ingest_paper.py`，但 private 不含论文类型）。
- **raw 路径**：sources 写完整路径 `private/raw/{health,metaphysics}/<file>`（与主库「完整域前缀路径」一致）。
- **校验**：`ingest_check.py` 按 `private/` 前缀选 private 库做图一致性校验；private 页 type 用 `private/SCHEMA.md` 枚举（`health-record`/`health-knowledge`/`metaphysics-profile`/`metaphysics-knowledge` 等）。
- **resolve 不跨库**：裸名解析仅在 `private/graph.db` 内匹配，主库实体不会污染 private resolve，反之亦然。

## 学术 raw 目录路径(own/others 分离)

摄入论文前,先判断归属:

- **自己论文/专著/专利/软著** → `academic/raw/works/<type>/`(papers/books/patents/software/editorials/proceedings)
- **他人参考论文** → `academic/raw/references/`
- **学术非论文参考文档** → `academic/raw/reference-documents/`；自有专题导言 → `academic/raw/works/editorials/`

两者分离管理。PDF 与 md 同名同位存放(命名规则与完整目录结构见 `academic/SCHEMA.md`)。frontmatter 的 `sources` 字段必须记录完整相对路径,如:
- `raw/works/papers/2024-luying-entanglement-prl.md`
- `raw/references/pink-2025-episodic-memory.md`

**PDF 提取**:调用**内置** `.scripts/extractor.py`(不跳过、不在别处另写兜底——extractor 内部 MinerU 为默认引擎（失败重试3次，退避0/5/15s；认证错误不重试），MinerU 耗尽后不静默回落 docling/pymupdf（论文质量要求），需低优先级引擎须显式 `--engine docling`/`--engine pymupdf`;档位与覆盖规则见 `academic/SCHEMA.md`)。提取后产出的 `paper.md` 存于对应论文目录(`<paper-id>/paper.md`):自己论文 `academic/raw/works/papers/<paper-id>/`(默认),他人论文 `academic/raw/references/<paper-id>/`(传 `--papers-dir`)。sources 字段用相对路径引用。**职责分离**:inbox 来的 PDF 按 `INBOX.md` 新流程,先用 `extractor --external-pdf <inbox路径> --paper <tmp-id> --papers-dir temp/inbox-extract` 在临时区提取为 `paper.md`,单遍阅读撰写 wiki 后由 `inbox_finalize.py` 实体复制落位到最终 `*/raw/<id>/`(不再"先归档再提取")。`--external-pdf` 用于 inbox 摄入的临时区提取;仅 synology:// 远程源等特殊场景另议。

> **分工边界**:`.scripts/extractor.py` 专处理**学术论文 PDF**(MinerU 默认+重试,产出 `<papers-dir>/<paper-id>/`);会议纪要 `.txt` 由 `ingest_meeting.py` 代码驱动摄入;学术非论文及行政/教学/商业文档(`.docx`/`.doc`/`.pptx`/`.txt`/`.pdf`)由 `ingest_document.py` 代码驱动摄入(`--subproject academic|admin|teaching|business`),内部用 textutil/pandoc 提取文本。academic 必须带 `--document-type editorial|academic-reference`，缺失时在事务与预处理前停止。

### raw 被外部调用约束

本项目的 raw/ 文件(自己论文 PDF、会议纪要)可能被其他项目引用为共用产物。遵循"raw 默认对外可引用"原则(不单独建 shared/ 目录):

- **被外部引用的 raw 文件不随意移动/改名**——一旦外部项目引用,本项目的移动/改名会导致断链
- 重命名/迁移 raw 文件前,需检查是否有外部引用(可由 LINT 或全局搜索协议路径定位)
- 确需迁移时,在 log.md 标注迁移记录,保留旧路径别名或更新外部引用

### 来源路径协议(synology://)与 .project/config.yaml

- 论文 sources 用相对路径:自己论文 `academic/raw/works/papers/<paper-id>/paper.md`,他人论文 `academic/raw/references/<paper-id>/paper.md`
- 指向 SynologyDrive **跨项目**文件时才用 `synology://` 协议(如 `synology://其他项目/...`),由 `.project/config.yaml` 的 `synology_roots` 解析为绝对路径(换机器只改 config,不改 sources)
- 本项目内部文件用相对路径(如 `raw/works/papers/...`)
- sources 字段:本项目内部文件用完整相对路径;仅跨项目共用产物用 `synology://`

## 重要约束

- 每次摄入 1-2 个文件为宜，批量不宜超过 3 个
- 新文件首次摄入触发约 3-10 个页面更新
- 绝对不要修改 `raw/` 中的任何文件
- 所有 wiki 页面的声明必须可追溯到 `raw/` 中的来源
- raw/ 支持用户自建任意嵌套子目录，Scan 和 Ingest 均递归处理所有层级
- sources 字段:本项目内部文件用完整相对路径(含用户自建子目录),如 `raw/policies/2025年度/XX办法.pdf`;论文用 `academic/raw/works/papers/<paper-id>/paper.md`(自己)或 `academic/raw/references/<paper-id>/paper.md`(他人);仅跨项目共用产物用 `synology://` 协议路径
- 论文页面模板见 `operations/templates/paper-summary.md`

---

## Raw 文档包节点与 Wiki 直连（2026-08-25）

- **文件都有图中代表**：进入知识库的 Raw 文件和 Wiki page 都对应图节点；其他人物、概念、Hub 等对象可为页面节点或无页实体节点。
- **Raw 文档包**：原件与同目录同 stem 的 locator companion 共同对应一个 `raw` 节点；例如 `paper.pdf` 与 MinerU `paper.md` 是一个 Raw 文档包，两个文件路径都解析到该节点。
- **Wiki 直连 Raw**：每个 Wiki page 按 frontmatter `sources` 建 `Wiki → 来源 → Raw` 边。一个 Wiki 可有多个 Raw 来源，一个 Raw 也可被多个 Wiki 使用。
- **locator 分层**：Markdown/TXT 使用标题或不可变行范围；文本型 PDF 可使用页码范围；其余格式由同目录 Markdown companion 提供 locator。论文 PDF 必须使用 MinerU `paper.md`。外部 `synology://` 路径在当前机器不可访问时单独报告，不视为 Raw 缺失。
- **边 locator 可选**：`edges.source` 可记录 Wiki section 或 Raw locator，但不强制；只校验已填写 locator 是否可解析。Wiki section 的脚注负责事实回溯 Raw。
- **存量对齐**：`edge_evidence` 与旧 `事实支撑` 边只作为迁移兼容输入，不再新增，也不参与完整性审计；`graph_repair.py --raw-links-only --apply` 同时补齐所有 Wiki/Raw 文档文件节点、同 stem aliases 与 `Wiki → 来源 → Raw` 直连。
- **查询过滤**：`raw` 类型节点在一般语义候选中可默认过滤；需要来源时沿 Wiki 的 `来源` 边显式取得 Raw 文档包。

## 重新摄入已入库论文（re-ingest）

> 管道版本升级后，重新摄入已入库论文以对齐最新版本。raw 不可变（红线），只重生 wiki + 清旧图边 + 重建。

- **触发**：影响 wiki/图边输出的建设变更（skeleton 模板/建边逻辑/prompt 调整）后，bump `CURRENT_PIPELINE_VERSION`（`graph_lib.py`），再跑 re-ingest。纯改名/重构不 bump，不触发。
- **版本戳机制**：`graph_ingest.py` 的 `upsert_page_node` 写入 `ingest_version` 到 page 节点。`re_ingest.py --outdated` 查 `ingest_version < CURRENT_PIPELINE_VERSION` 的 page 节点，精确触发落后论文；版本升级只标记存量为 outdated，不自动执行 re-ingest。
- **清旧边与 lineage**：`graph_ingest.py ingest --clean` 删除本页直接边，并按 `edge_origins(origin_page, edge_id, source)` 撤销本页贡献的概念间/命题间派生边；同一语义边仍有其他页面 origin 时保留。节点侧先撤销本页 `node_origins` 与 `node_glosses`；若被撤销的是主 gloss，仅在主描述仍等于该 gloss 时从剩余来源提升替代项，无剩余项才清空。仅删除同时满足 managed、无剩余 origin、无关系边、无时态事实的 entity，并同步清 alias；历史未标 managed 的节点一律保留。历史 `edge_evidence`/Raw source 仅用于兼容回收旧边，不再由新摄入生成。raw 不变。
- **调用**：

```bash
python3 .scripts/re_ingest.py --outdated --dry-run # 预览落后清单
python3 .scripts/re_ingest.py --outdated            # 仅 re-ingest 落后版本
python3 .scripts/re_ingest.py --raw <path>          # 单篇
python3 .scripts/re_ingest.py --manifest            # 全量（忽略版本）
```

- **边界**：raw 全程不动；wiki 覆盖前备份旧版至 `temp/inbox-state/<txn>-wiki-old.md`；不重跑 MinerU（raw 已有 paper.md）。
- **报告变更**：`resolve_miss` 已改名为 `nodes_created`（新建节点计数，非缺陷）；`textually_unsupported_candidates` 属过滤器正常拦截、非缺陷、默认不报。两者均不报附带发现。

## index.md 格式

```markdown
# 子项目索引
> 最后更新：YYYY-MM-DD

## 概念 / 政策 / 课程
- [[wiki/concepts/xxx|XXX]] — 一句话摘要
- ...

## 论文 / 文件 / 课时
- ...
```

每条一行，控制在 1-2K token 以内。

## log.md 格式

```markdown
# 操作日志

## [YYYY-MM-DD] ingest | base_name
- 创建/更新了哪些页面
- 值得注意的发现
- **token 成本**:`.scripts/count_tokens.py` 实计(读取 raw 段 + 规范 + 生成 wiki),避免字节估算

## [YYYY-MM-DD] update | base_name (旧版本 → 新版本)
- 更新了哪些页面
- 变化的摘要
- 保留的人工标注
```

仅追加，不修改历史条目。ingest 表示首次创建，update 表示版本更新。

### 受限结构化输出契约（v9）
所有需要 LLM 结构化输出的任务统一通过 `.scripts/llm_structured.py` 调用。Query 由 `QUERY_BACKEND=agent|api` 控制，默认 `agent`。ingest 单独由 `INGEST_BACKEND=agent|api` 控制（`hybrid` 已删除），默认 `agent`。`agent`=程序在语义阶段生成带 `prompt + write_to + transaction_id` 的协议交接，由宿主 Agent 写回同一事务后 resume；`api`=外部主 `LLM_MODEL` 提案并由程序校验后提交。两种模式共用 schema、证据、状态机和提交边界，后端选择不能改变事实约束。

**API 受限任务模型分工**：claims 固定使用主 `LLM_MODEL`；候选关键词选择和可机械判定的格式修复可分别配置完整的 `INGEST_KEYWORD_*`、`INGEST_REPAIR_*` 专用 API（如 MiniMax-M3）。专用调用失败或 Schema 不合格时回退主 API；主 API 仍失败时写 pending，并输出 `agent_fallback_required` 的最小交接包，由当前 Agent 基于证据卡、既有候选词与失败草稿兜底，禁止重读全文 raw。Agent 将 `{claims, uncertain, selected, keyword_uncertain}` 写为仓库内 JSON，传给 `api_ingest.py --agent-draft`（仅证据卡受限场景），程序重新执行同一证据/Schema 校验；通过后才允许页面/图校验闭环，不能直接提交。专用配置缺任一 base/key/model 时完全忽略，保持主模型原行为。草稿只保留控制回执；真实 API 调用、恢复尝试、模型和 provider token 统一从 ExecutionEvent 审计。

**弱模型纯 API 路径**：`INGEST_BACKEND=api` 时，`ingest_paper.py` 内部通过 `call_text()` 调用 API LLM 撰写 wiki 与语义槽，程序校验通过后落盘入库（见 playbook「摄入学术论文 PDF」）。wiki 输入不再默认塞全文：统一走 `ingest_common.build_source_context(kind="paper", force_reduced=True)`，按 `CONTEXT_PROFILES["paper"]` 定向抽取 Abstract/Introduction/Method/Results/Discussion/Conclusions；普通文本阈值 40k chars，API 定向摘要预算 22k chars、单段上限 6k。`read_paper` 命中 `method` 父段后继续吸收同号相关子节（Problem Formulation/Training/Inference），避免只拿父段概览。要求保留定理/等式/性能结论的对象、条件和比较基准；证据不充分时删除而不以常识补全。`api_ingest.py` 是早期证据卡编排器，保留用于需证据卡约束的受限场景，非 inbox 论文摄入的主入口。任何模式下，模型产物均须通过 schema、证据和确定性检查才可进入 wiki/graph。

**论文确定性 META**：新论文的 Wiki prompt 不再要求 `<<<META>>>`。title/authors/date/venue/type、paper-id 及 Raw/Wiki 路径由 PDF 近端证据、书目候选门和程序骨架确定性编译；LLM 只写语义正文与受限语义槽。旧事务若仍携带 META，只进入 `legacy_meta_audit`，不得覆盖 locked bibliography 或迁移路径。会议纪要与通用文档的既有 META 交叉校验暂时保留。

**Frontier 后置触发（2026-08-26）**：论文 ingest 完成事实写入、图校验与常规收尾后，`finalize_tail` 非阻断调用 `frontier.py capture-paper`。程序只从 Raw 捕获作者明示的 open question/future work/未解决问题，先按句子与枚举项确定性拆为独立问题，单篇最多 3 条；每条幂等新建或精确复用 `academic/frontier/questions/` Question Page，再以紧凑 Graph→Wiki→Raw 证据包尝试一次本库回答，不让 LLM 发散问题。支持性结论必须引用包内 Raw locator；模型不可用或回答失败只保留 `answer_status: pending`，不得回滚、阻断事实摄入或额外产生摄入 warning。`scientific_state` 不随库内回答自动更新。

**PDF 确定性书目预提取（2026-08-24）**：3.1 在调用 MinerU/LLM 前用 PyMuPDF 一次读取 PDF metadata 与第一页文本。title/authors 来自 metadata；year/venue 优先取第一页 `Proceedings of`/`Published`/版权行，其次 metadata subject/源文件名/creationDate；APS DOI 可确定性补齐 venue。结果先进入事务 `bibliographic_meta`，作为后续书目预审候选。

**论文书目预审门（candidate-id-v2，2026-09-04）**：3.2 在 `paper.md` 已生成、`persist_bibliographic_metadata()` 之前，由程序把 PDF metadata、标题邻域与发表证据行编成稳定候选目录；书目证据在 Abstract/Introduction 前截止，避免把正文或参考文献混入本篇身份。Worker 通常只返回 title/authors/year/venue/doi/arxiv_id 的候选 ID；仅当作者候选不完整或多人合并时，可提交完整、有序、逐人拆分且逐项绑定标题邻域 locator 的 `authors.proposed`，程序拒绝 affiliation、`et al`、重复、合并姓名和窗口外证据。含 DOI/arXiv 强标识、各标量字段至多一个候选、title/year 完整，且首页集中作者块与候选一致、无机构信号时走 `deterministic_fast_path`，不调用 Worker；其余场景每篇最多一次，`retries=0`。候选目录与有限证据计算 `input_hash`，schema 合格的裁决原子缓存于 `temp/inbox-state/<txn>-bibliographic-decision.json`，resume/重入输入未变时直接复用。无法裁决时 `review_status=manual_required`，事务停在 temp 并禁止 Raw/Wiki/Graph 提交；agent 模式也只向事务内 `bibliographic-review.json` 写同一 v2 裁决。通过后锁定 `state.bibliographic_meta`；paper-id、skeleton/wiki frontmatter 与 graph 机械作者边只消费该结果。最终 JSON 的 `bibliographic_worker` 回执记录 `api_called/cache_hit/skipped/skip_reason/input_hash`，用于核对正常路径调用数只减不增。`--raw`/re-ingest 复用已锁定 `source.yaml.bibliographic`；旧 raw 或旧事务仅走只读兼容，不修改 raw。

**近标题关系复核（relation-id-v1，2026-09-02）**：标题高度相似但没有 DOI/arXiv 时只标候选，不得在 3.1 直接判重复。管线先完成 MinerU、normalized-text 与 locked bibliography 去重；仍未决时，title/authors/year 完全一致可由程序零调用判为 `version`，其余至多调用一次 Worker。Worker 只能从程序目录选择目标 ID 与 `version|unrelated|ambiguous`，不能返回 `duplicate`、自由路径或 locator，也不能删除 Raw、写 Wiki 或建边。裁决以输入哈希缓存到 `temp/inbox-state/<txn>-relationship-decision.json`；只有程序在后续事务阶段有提交权。

**Inbox 论文语义槽治理**：`ingest_paper.py` 的登记谓词为优先清单，而非封闭枚举。格式合格的未登记短谓词记录到 `cross-domain/predicate-candidates.jsonl`；`.scripts/predicate_governance.py` 依 `.scripts/predicate-governance.yaml` 自动归一别名、聚合页面/来源/主体一致性，并写出观察期或正式注册表。正式注册只扩展摄入提示与校验允许集；反向关系与研究方向 tier 不自动推断。语义槽仅可写 wiki 明确陈述且原文支持的关系，不得从“关联/构造/表示”推导“基于”等方向边；“局限性”只记录作者明示的限制或近似代价，研究对象、模型维度、实验设置和适用场景不得误标。图写入前的 keyword 去重以中英文/缩写规范化为先：任一语言精确相同只能触发同一性核验，另一语言冲突或候选不唯一时不得自动合并或再用 embedding 覆盖；仅无冲突的剩余项可用 embedding 后备匹配。结构性语义错误早停并要求修正后 `--resume`，恢复提交前必复验；非阻断 warning 不增加调用，但在正常校验和 resume 复验中都必须持久化到 `semantic_warnings`/`quality_warnings` 并派生 `quality_status`。机械重复零调用修复，其余阻断项只走一次缓存的 `semantic-patch-v1`。

**Typed RecoveryPolicy**：恢复动作按 `infrastructure`、`output_transport`、`wiki_revision`、`semantic_revision`、`deterministic_repair`、`subagent` 独立计数，限额表示首轮之后允许的恢复次数。`llm_structured` 只负责前两类；pipeline 不会因 API 失败再原样调用 write step，DSH 也不重跑整个摄入子进程。validator、commit 前复验、resume 复验、写图后校验和 LINT 仍完整执行，不计入恢复预算。旧事务的 `wiki_retry/slots_retry` 仅一次性迁移为阶段恢复次数，不能用来推算模型调用数。

**事务状态与失败处置**：`temp/inbox-state/<txn>.json` 采用同目录临时文件、文件 `fsync`、原子替换和目录同步持久化；未知状态拒绝写入。普通前向阶段仍由 pipeline 驱动，`failed`、`agent_required`、书目复核等非线性 resume 必须通过 guarded transition，禁止直接跳过校验或提交阶段。终态单篇、论文 batch item 与统一 inbox 报告共用 `failure_disposition`（category/domain/disposition/retryable/owner/next_action/fingerprints）；DSH 优先消费该结构化对象，只有旧输出缺失时才做兼容文本分类。`python3 .scripts/inbox_state.py --summary` 只读汇总有效事务的状态、降级、恢复次数及其关联的 canonical ExecutionEvent，不写事务，不把 SessionLog 当调用计数。

**模型目录**：provider 当前可选模型记录在 `operations/config/llm-models.yaml`，仅用于选择与审计，不是运行时白名单；模型上下线不得阻断未使用该模型的摄入。

**统一源类型上下文（v12）**：论文和通用文档继续使用 `ingest_common.CONTEXT_PROFILES` 选择全文或定向摘要；`paper` 用 `read_paper` section，`document` 用 Markdown 标题抽取。会议纪要不再把头尾缩减结果分别交给多个 Worker：Meeting Compiler 通过事务内完整 `.txt` 一次读取，在同一上下文中完成预处理、Wiki 和 slots，长文件读取由脚本直接传路径/文本，不用 shell 切片。API 通用文档正文不超过 30,000 字符时仍由一次受限 Worker 同时产出 wiki 与候选三元组，更长文档保留既有两阶段适配。行政文档的 `department` 仅在 Raw 逐字出现完整部门名称时保留；负责人关系还须同一证据片段明确出现“负责人/负责”等职责措辞，校长讲话、主讲或发言身份不得推断为负责人。所有产物分别经过程序校验，语义硬错误最多定向重写一次，失败后带精确错误转 `agent_required`。

**API Worker 成本轨迹**：`temp/llm-events/YYYY-MM-DD.jsonl` 是唯一调用计数源。每次真实 HTTP 请求立即写一条 `execution-event-v1` / `llm_api_call`，用 `call_id` 关联同一逻辑调用的恢复尝试，并记录 `transaction_id`、operation、model、provider usage、latency、恢复类别以及 input/output hash 与长度。Agent handoff 和配置错误也可留控制事件，但不计为 API 请求。事务中的 `bibliographic_worker`、`relationship_worker`、`semantic_repair_worker` 只说明 `api_called/cache_hit/skipped/skip_reason` 控制决策；日志不保存正文。

**不确定类型裁决**：`ingest_inbox.py` 先由程序输出类型、分数、阈值和命中标记。PDF 学术分数 1/2/3、TXT 会议分数 1/2 视为不确定，仅在 `--run` 时调用一次 fast 档 API 分类器；高置信度和 dry-run 不调用。API 的中/高置信度具体类型可覆盖程序的不确定初判；`ambiguous`、`low` 或调用失败均在建立摄入事务前返回 `classification_required`，由用户兜底。程序初判和 API 裁决均写入完整摄入报告。

**通用文档来源日期**：`ingest_document.py` 的 `date` 只消费文件名或正文中明确的完整日期，不使用摄入日兜底。无可证日期时输出 `date: null` + `date_status: unknown`，同时保留 `created/updated` 作为摄入时间，文档 ID 使用 `undated-<slug>`。这一显式未知值通过 Schema/`ingest_check` 校验，但不得据此生成时态事实。

**DSH 摄入执行层**：`ingest_inbox.py --run` 的实际分发经 `dsh/agent_loop.py:IngestAgentLoop` 执行；`IngestGuard` 在 pre-execute 只放行 inbox 文件、合法事务 ID 与 `academic/raw/` 内的 re-ingest 路径，拒绝路径穿越。每次执行先过 `ToolRegistry` 的 guard/session/tool seam，并写 `temp/inbox-dsh/<session_id>.jsonl` 供事后审计。DSH 只调用底层 `ingest_*` 子进程，不重写摄入状态机、不直接写 raw/wiki/graph.db。

**DSH 增强机制**（2026-08-22）：
- **结构化错误分类**：`_ingest_call` 优先解析 stdout/stderr 的终态 JSON（显式类别、顶层 status、结构化 errors），缺失时才用窄关键词兜底；普通 PDF 文件名不得把书目/语义校验失败误分为提取失败。最终类别嵌入 `[ERROR category=<类别> script=<脚本> code=<退出码>]`，供 DSH 层决策。
- **单次派发与错误交接**：`IngestAgentLoop._execute` 对每个已获准的 tool action 只派发一次。`api_timeout` 等类别继续进入结构化结果和 session log，供恢复/运维判断，但 DSH 不重跑整个摄入子进程；API 基础设施与输出传输恢复只在 `llm_structured` 的 typed budget 内发生。
- **控制流保持**：子进程非零退出码不等于业务失败；只要 stdout/stderr 含合法顶层 `agent_required`、`partial`、`graph_ready`、`classification_required` 等可继续终态，DSH 就原样返回，不添加 `[ERROR]`。只有缺少合法控制流终态时才进入错误分类。
- **多文档摄入计划**：`ingest_inbox.py --run` 对多个文件先调 `_plan_ingest_order`，含版本/补充关键词（盖章/扫描/补充/v2/版本/修订/签字/正式版）的文件排后，主文档优先。重排结果记入 DSH session log `ingest/plan`。
- **全 PDF 批量捷径**（2026-08-24）：`ingest_inbox.py --run` 检测到多个文件且全部为论文 PDF 时，自动改走 `dsh/ingest_tools.py:ingest_paper_inbox` 调用 `ingest_paper.py --inbox` 两阶段批量入口（prepare 全部到 `graph_ready` 屏障后批量 commit），减少逐篇建图/整图校验次数；完成后仍运行裸缩写消解、人物候选检测和摄入报告。
- **紧凑 stdout + 完整报告**：`ingest_inbox.py --run` 不回显底层完整 stdout/Hub candidates，只输出单个紧凑 JSON（状态、计数、逐文件终态、`report_path`）。`cross-domain/ingest-reports/YYYYMMDD-HHMMSS.json` 保存时间戳、session ID、程序分类/API 边界复核、摄入计划、完整底层输出、逐文件状态/引擎/graph_report 与完成/失败/跳过计数，供复盘，不替代 raw/wiki/graph.db 事实源。

**阶段依赖与成本边界**：阶段 1 的摘要、Navigation 和 Content 需要语义生成；阶段 2 的关系抽取、别名判断和 hub 归属需要语义生成，但只读取阶段 1 产物及增量相关页；阶段 3 的日志、索引、`ingest_check.py` 和图状态检查优先使用确定性脚本，不重复调用 LLM。`route.py` 会在每次 ingest 派发时显示当前后端；非默认 `api` 模式必须看到提示后才继续。普通 `api` 论文路径的目标是正常摄入 `Agent token=0`；不完整草稿降级进入 pending 队列，不阻塞已验证的确定性产物。任何模式下，模型产物均须通过 schema、证据和确定性检查才可进入 wiki/graph。

**API 推理档位**：`.scripts/llm_structured.py` 按操作采用内部 `fast` / `standard` / `deep` / `xdeep` 档位。类型复核、候选选择与格式修复默认 `fast`；普通文档和论文的 Wiki/语义槽首次生成默认 `standard`；确定性校验只有在发现证据、Raw locator、核心内容或关系语义错误时才把一次定向重试升到 `deep`，结构/格式错误不升档。输出 `max_tokens` 与重试预算由调用方独立控制，不与推理档位绑定。当前 GLM-5.3-Flash endpoint 已验证 `reasoning_effort` 接受 `low/high`、拒绝 `medium`，故 `fast/standard` 映射 `low`，`deep/xdeep` 映射 `high`；其他 provider 必须先探测再配置。事件日志记录选择原因、错误类别、实际 effort、reasoning token 和耗时。

### 增量与派生状态（v9）

摄入默认按影响范围处理，不因一个新文件重读或重写全库。`.scripts/ingest_plan.py` 只读 raw 指纹，输出非破坏性的 `changed_raw`、`removed_raw` 和候选 `affected_wiki_pages`；它不调用 LLM、不写 wiki/graph。确认正常摄入后，才可用 `--write-state` 保存 `cross-domain/ingest-state.json` 的指纹状态。指纹仅用于发现变化，不能替代 `ingest_check.py`。

API 语义草稿的 `provenance` 记录 source/schema/rule/prompt/model 版本。版本或来源变化只表示结果可能过期：先生成局部重放清单并比较，不得自动覆盖人工内容。确定性节点、机械边、alias 和索引可重建；摘要、关键词和语义边须经受限输出、证据校验和正常编译流程；raw 和人工维护内容不得被重建覆盖。

建议调用顺序：

```bash
python3 .scripts/ingest_plan.py --raw-root academic/raw --wiki-root academic/wiki
# 确认影响范围后执行正常 stage 1→2→3
python3 .scripts/ingest_plan.py --raw-root academic/raw --wiki-root academic/wiki --write-state
```

这里的“先结构、后语义”专指 ingest：程序先做指纹、骨架、元数据、机械边和候选范围，LLM 只填受限语义槽；query 中对应为“先用图/索引缩小候选，再读 Navigation/Content/raw”。会议纪要读取 raw 的开头结构化小结后，时间线仅在需印证术语/参会者时按需 `grep` 局部片段；以 raw 和 `entity-resolution.json` 直接产出 wiki，`sources` 指向 raw，并在 wiki 内完成简写、纠错和去口语化。人物解析的 resolved 结果同时供参与者行与建边复用，不重复提取；历史 `corrected.md` 保留供回溯，不回填、不删除，也不作为新流程输入。
