# WikiRan 代码调用指南

> **用途**：供 LLM 在需要运行 `.scripts/` 时按任务定向读取。本文件说明“何时调用、是否写入、最小正确调用、调用后必须做什么”，不替代 `operations/` 的业务规则。
> **先决条件**：先读 `AGENTS.md`，完成任务识别，并执行 `.scripts/route.py --task <task>`。不得因为本指南给出命令就跳过路由、Schema 或 raw 红线。
> **读取策略**：不要每次整读。按当前任务只读一个工作流 section；命令参数以脚本当前输出/源码为准，本指南给出安全调用模式。
> **工程元图优先**：先由 `route.py` 获得 task 的最小工程上下文和任务卡；任务卡是本轮不可跳过的程序/行为护栏。建设变更先运行 `.scripts/engineering_graph.py impact <节点> --verify`；高风险脚本再运行 `contract <节点>` 获取短 I/O 契约，再读取本指南中命中的调用段。元图未覆盖不等于无影响，必须显式补查。

---

## 0. 统一调用规则

### 0.1 先判断脚本类别

| 类别 | 典型脚本 | 是否可写 | 调用原则 |
|---|---|---:|---|
| 路由与读取 | `route.py`、`read_section.sh`、`read_paper.py` | 否 | 每项任务优先使用 |
| 编码骨架 | `wiki_skeleton.py`、`extractor.py` | 写派生 wiki/raw 旁路 | 必须先确认归档路径 |
| 图写入 | `graph_ingest.py` | 写 `graph.db`；仅按 arXiv 配置创建根 Hub | 只在巩固阶段、语义已审核后调用 |
| Hub 动力学 | `hub_semantics.py` | 可幂等写派生 `聚类于` 边；生命周期写入受 Agent 门控 | ingest 只做局部 membership 刷新；create/split/merge 先分析后确认 |
| 查询 | `query_orchestrate.py`、`query_actions.py`、`query_graph.py` | 前两者写会话/日志；图查询只读 | 图结果只用于定位，仍读 raw |
| 增量计划 | `ingest_plan.py` | 输出 raw 变化与候选影响页 | 只读计划；不调用 LLM、不写 wiki/graph |
| 派生状态 | `derivation_state.py` | 计算指纹、记录生成版本、判断过期 | 作为库模块调用；不负责重建或覆盖内容 |
| 校验与诊断 | `ingest_check.py`、`graph_metrics.py`、`graph_dump.py` | 通常只读；部分有状态/快照选项 | 先报告，勿把诊断当修复授权 |
| 可视化 | `graph_visualize.py` | 只读 `graph.db`，输出 PNG/HTML | 按需调用；布局在 Python 端预算，浏览器零物理模拟 |
| 维护/迁移 | `ingest_build.py`、`rebuild_triples.py`、迁移脚本 | 可生成派生物或批改文件 | 仅在相应规范明确要求时调用 |

### 0.2 通用安全约束

1. **绝不修改 `raw/`**。能写 raw 附近的脚本只能生成 `paper.md`、`corrected.md`、审计 JSON 等派生物。
2. 路径默认使用仓库相对路径；页面参数通常不带 `.md`，实际以脚本 help/规范为准。
3. 任何带 `--apply`、`--split`、`--force`、`init`、`merge`、输出文件参数的调用都是潜在写操作。先说明影响，确认本轮已获执行授权。
4. 普通 `--help` 不一定安全或可用：少数历史工具把第一个参数直接当作文件/模式。未知用法先看文件开头 docstring 或对应规范，不要探测性执行。
5. 脚本成功不等于语义正确。写入后的页面仍应按流程运行 `ingest_check.py`，事实关系仍由 LLM 回看证据。
6. 不擅自对全库运行重建、批量提取、`--force` 或会改状态的命令；优先限制到本次文件或先执行分析模式。

## 1. 所有任务的入口：路由与定向读取

### 1.1 `.scripts/route.py`

- **何时调用**：每个知识库使用任务开始时；建设任务也调用 `build`。
- **写入**：否。
- **作用**：按 task 输出本轮工作状态所需规范，或按 capability 输出状态内临时组合的能力规范，避免模型凭记忆操作。
- **三层语义**：task/state（如 `research`）提供持续工作上下文；capability（如 `write`）只在具体动作发生时加载提示与规则；tool（DSH ToolRegistry/`wg.py`）执行带参数、带结构化返回的读取、计算或写入。三者共享工程图作为发现与约束来源，但使用不同调用接口。
- **输出顺序**：工程上下文 → 任务卡 → 执行纪律 → 定向规范；ingest 另有模板复用补充。使用任务收到使用纪律；建设任务改为收到工程精确读取门，并只截取 shared-conventions 的下游同步、工程文档维护和系统设计原则，不再默认输出全文。任务卡把弱模型最易错、但不能靠上下文猜测的边界前置；业务细节仍以随后规范为准。
- **query 固定上下文去重**：query 的 `start` 卡承载工程上下文、执行纪律、任务边界与经验触发；`evidence`/`continue`/`answer` 仅派发当前阶段规范段，不重复注入这些固定上下文。
- **查询相似边多轮渐进式召回**：相似边召回按 query 回环轮次渐进式放开（编排层注入，`query_orchestrate.py`）：
  - **第一轮**（`loop_count=0`）：`similar_topk=0`，**完全排除相似边**——纯知识边优先扩散，确定性召回。
  - **第二轮**（`loop_count=1`）：`similar_topk=3`，动态 K 上限 3——证据不足时引入保守相似边。
  - **第三轮+**（`loop_count≥2`）：`similar_topk=5`，动态 K 上限 5——进一步扩大相似召回。
  - `--similar-topk` 语义：`0`=排除（第一轮），`-1`=全部（手动调试），`N>0`=动态 K 上限（按 score 截断 `SIMILAR_SCORE_MARGIN=0.03`，下限 1）。
  - 动态 K 由节点 score 分布涌现：高分赢家→少保留（iPEPS 7→1），score 聚集→多保留（上限）。
  - 知识边不受此限制、始终优先扩散；相似边排末尾。存储层不裁剪相似边，扩散完全由导航层控制。
- **最小调用**：

```bash
.scripts/route.py --task query
.scripts/route.py --task query --query "某人与谁有何关系" --query-stage start
.scripts/route.py --task query --query "某人与谁有何关系" --query-stage evidence
.scripts/route.py --task ingest --subproject academic --mode create --content paper --stage 1
.scripts/route.py --task ingest --subproject academic --mode create --content other --source-kind meeting --stage 1
.scripts/route.py --task build
.scripts/route.py --task research
.scripts/route.py --capability write --capability-profile academic
```

- **后续**：使用任务按派发的任务卡、执行纪律和规范推进：先执行明确的下一步，只有派发要求、参数无法判定或命令报错时才最小定向补读；达到验收条件即停止，不自动扩面。建设任务先运行 impact/contract，优先直接读取 impact 推荐的精确 locator；不足时才用其 filtered-list 入口。ingest 的 create 模式依次完成 stage 1 → 2 → 3，再调下一 stage。
- **research→write 组合**：进入 `research` 后保持该状态；只有实际起草、改写或润色论文标题、摘要、正文、图注、附录或补充材料时调用 academic write capability。同一连续写作回合加载一次即可；讨论、查询、数据核对、实验与状态汇报不调用。顶层 `--task write` 是 general write profile 的兼容入口。
- **query 分段**：先用默认 `start` 完成意图判别和低成本定位；候选已定位且需事实核验时才调用 `evidence`；只有存在具体 gap、candidate、action 与 expected gain 时才调用 `continue`；交付前调用 `answer`。`--profile auto` 返回可组合意图，避免“列出 + 关系 + 依据”被单一关键词覆盖。
- **程序护栏**：ingest 不再接受默认域；create 模式不再一次派发全部 stages。缺 `--subproject` 或 `--stage` 会直接失败。`--source-kind` 默认 `ordinary`；只有会议纪要传 `meeting`，才会派发实体纠错、SR 与会议图边规则。
- **去重边界**：论文巩固阶段的研究方向/keyword 执行规则只由 `INGEST.md` 派发；不再重复加载 `academic/SCHEMA.md` 的同义段。Schema 仍是字段和数据契约的权威来源。
- **不要**：用路由输出替代对指定 `SCHEMA.md` 或 raw 的读取。

### 1.1c `.scripts/ingest_meeting.py`

- **何时调用**：inbox 下的会议纪要 `.txt` 文件，代码驱动全流程摄入。支持 `--subproject academic|admin|business` 跨域存储（默认 academic）。
- **写入**：`temp/inbox-extract/<txn>/`（corrected.txt/entity-resolution.json/wiki.md/semantic）；最终 `raw/wiki` 按 subproject 落位（academic→`academic/raw/conferences/`、admin→`admin/raw/meetings/`、business→`business/raw/conferences/`），经 `inbox_finalize.py` 原子落位；`graph.db` 经 `graph_ingest.py`。
- **最小调用**：

```bash
python3 .scripts/ingest_meeting.py --txt inbox/<file>.txt --subproject academic
python3 .scripts/ingest_meeting.py --txt inbox/<file>.txt --subproject admin
python3 .scripts/ingest_meeting.py --resume <txn-id> --verbose
```

- **流程**：3.1 dedup → 3.2 preprocess（speech_entity_resolver）→ 3.3a write_wiki（LLM）→ 3.4 validate_wiki → 3.3b write_slots（LLM，续接对话）→ 3.5 fill_semantics → 3.6 validate_semantics → [3.6b 局部修复] → 落位 → 3.7 update_graph → 3.8 validate_graph → 3.9 finalize_tail（+清理 inbox 源至回收站）
- **驱动器**：9 步调度循环、状态机、修复循环、resume 安全网委托 `ingest_pipeline.py`（`run_pipeline(state, MEETING_SPEC, progress)`）；本脚本只声明 spec + provider step 函数。
- **后端模式**：`INGEST_BACKEND=agent` 时 3.3 合并 wiki+语义槽为单次 LLM 调用（`build_agent_meeting_wiki_slots_prompt`，省一轮往返）；`api` 模式分两次。`agent_required` 响应含 `pipeline_plan`（3 步：判断重复+预处理 / 撰写 wiki 与语义槽 / 更新 Graph+校验+收尾）。
- **子图健康性**（委托 `ingest_common.py`）：语义槽硬错误（谓词非法、解析失败）→ 早停交接（`stop_for_semantic_errors`，保留已通过的 wiki，不盲重试）；warning（描述性短语、裸缩写、重复行）→ 3.6b 两级降级修复（DeepSeek→GLM→agent 兜底）；落位前 `validate_before_commit` 全量复验（resume 安全网）。warning 分阻断/非阻断：`bare_abbreviation`/`descriptive_phrase` 非阻断（后置机制兜底），其余阻断。
- **META 交叉校验**（委托 `ingest_common.py`，2026-08-07）：LLM 撰写 wiki 时同步输出 `<<<META>>>` 块（`doc_date`/`title`/`doc_type`），程序解析后与 Python 推导值交叉校验。`doc_type` 不一致 → 返回 `type_mismatch` 状态，多文档循环跳过该文件，全部跑完后汇总报 agent 判断；年份不一致 → 自动修正 paper-id/meeting-id/admin-id（LLM 读全文比正则可靠，零额外 agent 调用）。`doc_date` 按文档实际内容提取（有什么提什么，如 `2024-03` 或 `2024-03-15`），不编造精度。
- **语义槽**：四个 section——参会者（人→参会→会议）、汇报者（人|议题→人→汇报→议题，修复人-议题断链）、决策（决策内容→本会议→决策→内容，内容作 proposition 节点，与论文核心创新点同构）、待办（任务|负责人→人→待办→任务，任务作 keyword 不建独立节点）+ 三元组（讨论/涉及/规划 + 关联 + 参会 + 指导/师从）。关键词由代码从三元组提取。学术性引导：议题用规范学术概念名（能与论文 keyword 对齐）、决策含学术判断、提取密度宜低。sources 路径由程序注入真实 raw 路径（杜绝 `memory://` 占位），`validate_wiki` 拒绝占位路径。
- **不要**：让 agent 手动拆步或全程监控；让 LLM 单独列关键词（关键词从三元组提取）。

### 1.1d `.scripts/ingest_document.py`（通用文档摄入：admin/teaching/business）

- **何时调用**：inbox 下的行政/教学/商业文档（`.docx`/`.doc`/`.pptx`/`.txt`/`.pdf`），代码驱动全流程摄入。`ingest_admin.py` 是向后兼容薄包装（转发到 `ingest_document.py --subproject admin`）。
- **写入**：`temp/inbox-extract/<txn>/`（doc.md/manifest.json/wiki.md/semantic）；最终 `raw/wiki` 经 `inbox_finalize.py` 原子落位；`graph.db` 经 `graph_ingest.py`。
- **时态提示**：admin 的 `policy`/`procedure`/`decision` 与 teaching 的 `course` 页，LLM 在原文有明确施行/生效/废止日期时写入 frontmatter `effective_from`/`effective_to`（`YYYY-MM-DD`；无截止留空）。后续由 `graph_ingest.py` 转为 `temporal_facts`。
- **最小调用**：

```bash
python3 .scripts/ingest_document.py --file inbox/<file> --subproject admin
python3 .scripts/ingest_document.py --file inbox/<file> --subproject teaching
python3 .scripts/ingest_document.py --file inbox/<file> --subproject business
python3 .scripts/ingest_document.py --resume <txn-id> --verbose
# 兼容入口（= --subproject admin）：
python3 .scripts/ingest_document.py --file inbox/<file> --subproject admin
```

- **流程**：3.1 dedup → 3.2 preprocess（textutil/pandoc 提取）→ 3.3a write_wiki（LLM，带全文）→ 3.4 validate_wiki → 3.3b write_slots（LLM，续接对话，只带 wiki）→ 3.5 fill_semantics → 3.6 validate_semantics → [3.6b 局部修复 warning] → 落位 → 3.7 update_graph → 3.8 validate_graph → 3.9 finalize_tail（+清理 inbox 源至回收站）
- **驱动器**：9 步调度循环、状态机、修复循环、resume 安全网委托 `ingest_pipeline.py`（`run_pipeline(state, DOCUMENT_SPEC, progress)`）；本脚本只声明 spec + provider step 函数。
- **后端模式**：`INGEST_BACKEND=agent` 时 3.3 合并 wiki+语义槽为单次 LLM 调用（`build_agent_doc_wiki_slots_prompt`）；`api` 模式分两次。`agent_required` 响应含 `pipeline_plan`。
- **子图健康性**（委托 `ingest_common.py`）：与论文/会议一致——硬错误早停保留 wiki，warning 两级降级修复，落位前 `validate_before_commit` 复验。
- **sources 回填**：文档 raw_dir 依赖 LLM 输出的 page_type（鸡生蛋），prompt 不传真实路径；`step_write_wiki` 解析 page_type 算出 raw_dir 后回填 frontmatter sources（消除占位路径），`validate_wiki` 拒绝 `memory://`。
- **域配置**（`DOMAIN_CONFIG`）：每个域有 `page_types`/`type_to_subdir`/`kw_predicates`/`nav_predicates`/`extra_frontmatter`/`subject_pronoun`/`domain_name`。
  - admin: 代词「本文件」，kw 谓词 涉及/讨论/形成决策/推动/申请事项/适用对象
  - teaching: 代词「本文档」，kw 谓词 涉及/讨论/涵盖/考核
  - business: 代词「本文件」，kw 谓词 涉及/讨论/分析/规划
- **语义槽**：统一三元组格式，关键词由代码从三元组提取（kw 谓词的 object）。hub 涌现机制与会议纪要一致（精确→embedding→catch-all）。
- **不要**：让 agent 手动拆步或全程监控；对学术论文 PDF 使用（走 `ingest_paper.py`）。

### 1.1a `.scripts/ingest_inbox.py`（统一摄入入口）

- **何时调用**：inbox 下有多个文件需摄入、或从网上下载论文 PDF 后摄入。纯 Python 分流（零 LLM 类型判断），自动识别文件类型并分发到对应脚本。
- **分类规则**：
  - PDF：pymupdf 读前2页，学术特征评分（Abstract/References/arXiv/DOI/Keywords/Introduction/University/引用/公式）≥3 → `ingest_paper.py`；否则 → `ingest_document.py`
  - `.txt`：会议特征评分（会议/参会/元宝会议助手/时间戳/汇报/讨论）≥2 → `ingest_meeting.py`；否则 → `ingest_document.py`
  - `.docx`/`.doc`/`.pptx`/`.md` → `ingest_document.py`
- **最小调用**：

```bash
python3 .scripts/ingest_inbox.py                              # 扫描+分类（dry run）
python3 .scripts/ingest_inbox.py --run                        # 扫描+分类+逐个摄入
python3 .scripts/ingest_inbox.py --file inbox/x.pdf           # 单文件分类
python3 .scripts/ingest_inbox.py --download URL1 URL2 --run   # 下载论文+摄入
python3 .scripts/ingest_inbox.py --run --subproject admin     # 指定 meeting/document 域
```

- **下载**：`--download URL` 用 curl 下载 PDF 到 `inbox/`（arxiv URL 自动提取 ID 命名），再按 `--run` 决定是否摄入。下载前自动查重：arxiv ID 查图（aliases + nodes）与 raw `source.yaml`，已在知识库的论文跳过下载（2026-08-07）。
- **不调 LLM**：类型判断纯 Python（pymupdf + 关键词评分），各脚本内部已有合并 LLM 调用（agent 模式一次输出 wiki+slots），不重复阅读。
- **type_mismatch 跳过**（2026-08-07）：多文档循环摄入时，若某篇 LLM META 校验发现 `doc_type` 与 Python 分类不一致，该篇返回 `type_mismatch` 状态被跳过，不阻断循环。全部跑完后汇总跳过的文件报 agent 判断类型后单独重跑。
- **DSH seam**（2026-08-20）：`--run` 的实际分发经 `dsh/agent_loop.py:IngestAgentLoop` 执行。`IngestGuard` 在 pre-execute 只放行 inbox 文件 / 合法事务 ID / `academic/raw/` re-ingest 路径；session log 写 `temp/inbox-dsh/<session_id>.jsonl` 供审计。DSH 只通过子进程调用底层 `ingest_*`，不重写摄入状态机。
- **终态解析**：底层 stdout 可同时包含进度文本、图报告嵌套 JSON 和最终结果 JSON。DSH 与统一入口只接受 `completed/duplicate_found/agent_required/failed/type_mismatch/partial/error` 等工作流状态，并取最后一个终态对象；Hub/page 的 `active/current/retired` 不参与摄入汇总。
- **紧凑输出**（2026-08-25）：`--run` 不回显底层完整 stdout、逐节点 Hub candidates 或完整 `graph_report`；stdout 只返回单个紧凑工作流 JSON（计数、逐文件终态、`report_path`）。完整底层输出、图/Hub 诊断和逐文件详情持久化到该报告，DSH log 继续保留结构化终态。
- **全 PDF 批量捷径**（2026-08-24）：`--run` 扫描到多个文件且全部为 paper 时，自动改走 `ingest_paper_inbox` 工具调 `ingest_paper.py --inbox` 两阶段批量入口；quiet prepare 有界并发（上限2），graph commit 与 verbose 模式保持串行。单篇或混合类型仍走逐文件 DSH seam。
- **自动消解**（2026-08-22）：`--run` 完成后自动运行轻量裸缩写消解（`_auto_resolve_abbreviations`）——槽位层查 `abbreviation-todo.jsonl` 中缩写是否已有图 alias 或 raw 定义，命中即消解并从待办中移除；命题层调 `resolve_abbreviations.py --apply` 消解 raw 有定义的缩写。零 LLM 调用，结果记入摄入报告 `abbreviation_resolution` 字段。
- **People page 候选检测**（2026-08-23）：ingest/query 后自动运行 `detect_people_page_candidates()`（`ingest_common.py`），检测达标人物写入 `cross-domain/people-pending.jsonl`。入选标准（满足任一）：≥6 篇论文作者、≥4 篇通讯作者、≥3 次参会、≥2 种关系类别（paper/meeting/advisory，排除所属/任职）。已有 people page 或 `wiki/authors/` 路径的实体跳过；占位符名（括号开头）排除。纯代码零 LLM；达标即由 `build_people_pages.py` 自动建极简 people page（仿 an-chun-ji 模板）并迁移 graph node path（裸名→`wiki/authors/<slug>`），不再积压 pending 队列；slug 冲突（同名不同人）跳过留待人工。
- **多文档摄入计划**（2026-08-22）：多文件时 `_plan_ingest_order()` 将含版本关键词（盖章/扫描/补充/v2/版本/修订/签字/正式版）的文件排到主文档之后，确保先摄入原文再摄入版本/补充件。重排事件写入 DSH session log（`ingest/plan`），dry-run 时仍输出关联提示。
- **摄入报告**（2026-08-22）：`--run` 结束后写 `cross-domain/ingest-reports/YYYYMMDD-HHMMSS.json`，记录 timestamp、session_id、DSH log 路径、摄入计划、完整底层 tool output、逐文件状态（含 engine/graph_report/transaction_id/proposition_status/quality_status）、成功/降级/失败/跳过汇总。缩写 remaining 属全局 backlog，报告显式标注 scope。供复盘用，不作为事实源。
- **不要**：手动判断文件类型或逐个调用 `ingest_paper/meeting/document.py`——让 `ingest_inbox.py` 自动分流。

### 1.1b `.scripts/playbook_dispatch.py`

- **何时调用**：收到指令后、调 `route.py` 之前，先检索 playbook 是否有命中条目。
- **写入**：否（纯只读）。
- **最小调用**：

```bash
python3 .scripts/playbook_dispatch.py "摄入 inbox"
python3 .scripts/playbook_dispatch.py "更新工程文档"
python3 .scripts/playbook_dispatch.py --list
```

- **行为**：传入指令关键词，程序匹配 playbook 触发词，只输出命中的条目文本（`##` 到 `---`），不读全文。未命中输出"正常推进"并 exit 0。
- **不要**：把 playbook 条目内容复制到别处；playbook 是单一事实源，本脚本只做局部派发。

### 1.1c `.scripts/experience_recall.py`

- **何时调用**：query/ingest/write/build 在 playbook 未命中或出现策略歧义、失败重试、固定路由未覆盖时；playbook 命中时不得调用，除非条目明确允许经验补充。
- **写入**：否（只读 `memory/experiences/<capability>.md`，不读 raw、不调用 LLM）。
- **最小调用**：

```bash
python3 .scripts/experience_recall.py recall --capability query --event start --context "中文人名 英文别名 主题词"
```

- **输出**：最多 3 条泛化 pattern，含 advice/boundaries/source_trace 与 `recall_cost`。把实际使用的 pattern id 记入当次任务 trace/log 的 `experience_used` 字段。
- **不要**：把经验当事实、规范或回溯来源；不要递归检索经验或用 embedding/网络扩展经验召回。

### 1.1a `.scripts/engineering_graph.py`

- **何时调用**：建设任务开始、修改关键脚本或规范后；它是目标工程图查询与漂移检查，不是业务知识图。
- **写入**：`validate`/`impact`/`contract` 否；`forget` 会写回 `graph.yaml`。
- **最小调用**：

```bash
python3 .scripts/engineering_graph.py validate          # 漂移+孤儿检测（WARN 到 stderr，不阻断）
python3 .scripts/engineering_graph.py impact graph_ingest --verify
python3 .scripts/engineering_graph.py contract graph_ingest
python3 .scripts/engineering_graph.py forget triples_legacy --dry-run  # 预览删节点+边+引用清理
```

- **结果处理**：`validate` 机械检查节点路径、task/capability 覆盖、关键入口、声明契约和验证映射；**孤儿脚本检测**——`.scripts/` 下未登记脚本输出 WARN（提示登记或入 `untracked` 白名单），已登记但磁盘消失输出 ERROR；**公开副本漂移检测**——`operations/engineering/` 下的 `public_assets` 镜像副本（code-guidance.md、engineering-handbook.md）若与正本不一致输出 WARN（根目录 `.gitignore` 等有意分叉不纳入）；`impact` 输出每个命中节点时附带 code-guidance.md 段落锚点（如 `→ code-guidance §2.5`），由 `guidance_anchors()` 运行时解析 code-guidance.md 标题自动匹配，无需手动维护映射；`impact --verify` 给出影响面及去重后的最小验证命令；`contract` 只给单脚本前置、可写、禁止与验证；`forget <node> [--dry-run]` 删节点并清理所有入射引用（edges/capabilities/verification/script_contracts/contracts.nodes），写回后须重跑 validate 复核。
- **结构完整性**：`load()` 启动时校验 graph.yaml 含非空 nodes/edges/capabilities 段；文件截断或格式损坏立即报错而非静默通过。graph.yaml 顶部有结构自述注释——节点是命名字典（非 `- id:` 列表），勿用列表式 grep 误判为空。
- **不要**：把元图输出视为完整调用链或语义正确性的证明；未覆盖变更仍显式补查，金样/真实局部校验仍须执行。

### 1.1e `.scripts/engineering_locator.py`

- **何时调用**：建设或维护 Agent 已通过工程图定位目标后，默认必须使用，而不是可选优化。`engineering_graph.py impact <target> --verify` 先输出可机械确定的 graph.yaml node/contract/capability 与 code-guidance 精确 locator，Agent 直接 `read`；只有推荐不足时才调用 filtered `list --prefix`。`route.py --task build` 同时输出这条读取纪律。
- **建设 seam**：`dsh/build_tools.py` 提供 `BuildLocatorCockpit`，将 impact/read/list 包装为 DSH 工具面，`dsh/guards/build_locator_guard.py` 在 impact 完成前拒绝 read/list，list 无 prefix 不放行；仅建设任务使用，不进入查询型 DSH。
- **写入**：否；不建立持久索引、companion 或 locator 缺失审计。
- **能力边界**：这是建设 Agent 的定向读取能力，不注册到通用 `query_actions` 或查询型 DSH。功能性任务调用封装函数；Raw/Wiki locator 继续作为事实读取函数的证据参数。
- **locator**：Markdown 用 `path#md:<heading-path>`；重名层级末尾显式加 `@1`、`@2`。YAML 用 `path#yaml:<JSON Pointer>`；Python/测试用 `path#py:<qualified-symbol>`；普通 UTF-8 文本用 `path#Lx-Ly`。
- **最小调用**：

```bash
python3 .scripts/engineering_locator.py list operations/engineering/graph.yaml --prefix 'yaml:/nodes/route'
python3 .scripts/engineering_locator.py list .scripts/route.py --prefix 'py:load'
python3 .scripts/engineering_locator.py read 'operations/INGEST.md#md:ingest摄入操作规范/创建模式-执行步骤/阶段一编码encoding-忠实提取不加工'
python3 .scripts/engineering_locator.py read 'operations/engineering/graph.yaml#yaml:/script_contracts/engineering_locator'
python3 .scripts/engineering_locator.py read '.scripts/route.py#py:load_sections'
python3 .scripts/engineering_locator.py read '.scripts/route.py#L219-L250'
```

- **成功输出**：只返回一个精确片段，并带 canonical locator、起止行、字符数与 SHA-256；`list` 仅按需枚举逻辑 locator，不返回正文。
- **两级发现**：impact 不猜具体 Python symbol；先读它能确定的规范/契约 locator，再按施工意图用 `rg` 找符号关键词并进一步收紧 `--prefix py:<symbol>`。无 prefix 的完整 list 只作最后发现手段，尤其不得对大型 YAML 默认使用。
- **失败边界**：裸路径、根 YAML pointer、歧义/失效 locator、非 UTF-8/二进制/数据库、越界和超预算都返回错误；不会静默整读，也不会把过大片段截半后交给 LLM。`raw/`、`wiki/` 分别使用 `read_raw`、`read_section`，本工具拒绝绕过。
- **定向扩大例外**：`rg` 只用于定位候选文件或符号。仅 locator 明确不支持、返回错误，或任务所需上下文本身横跨多个逻辑块时，Agent 才可定向扩大读取，并在工作更新中说明原因；不得因为命令更熟悉而默认回到 `cat`/`sed` 全文读取。

### 1.2 `.scripts/read_section.sh`

- **何时调用**：候选 wiki 页已定位，需要先低成本阅读 `Navigation`，或只需某个精确 section。
- **写入**：否。
- **最小调用**：

```bash
.scripts/read_section.sh academic/wiki/papers/example.md Navigation
.scripts/read_section.sh admin/wiki/policies/example.md Content
```

- **成功含义**：输出精确 `## Section` 内容。
- **失败处理**：找不到 section 会非零退出并列出可用标题。旧页未迁移时，显式向用户说明后才可整读；不能静默退化。

### 1.3 `.scripts/read_paper.py`

- **何时调用**：论文 `paper.md` 很长，只需 Abstract/Method/Results/Conclusion 等段落。
- **写入**：否。
- **最小调用**：

```bash
python3 .scripts/read_paper.py academic/raw/works/papers/<paper-id>/paper.md Abstract Results
```

- **方法段展开**（2026-08-20）：`method` 命中父标题后，会继续吸收同号相关子节（如 `3.1 Problem Formulation`、`3.2 Training`、`3.3 Inference`），不要再用人工 grep 逐个子节读取。
- **后续**：仍以实际输出为准；未命中的 section 不能臆补。

## 2. 摄入工作流：原料 → wiki → 图

> 先路由 `ingest`，读本域 `SCHEMA.md`。本节仅给脚本调用次序，不决定页面语义和关系内容。

### 2.1 论文 PDF：`.scripts/extractor.py`

- **何时调用**：学术论文 PDF 已实体归档到内部 `raw` 的论文目录，需要生成可读 `paper.md`。
- **写入**：在论文目录中创建/更新派生 `paper.md`；不改 PDF。
- **前置**：论文归属已判定。自己论文默认在 `academic/raw/works/papers/<id>/paper.pdf`；他人参考论文用 `academic/raw/references/`。
- **最小调用**：

```bash
python3 .scripts/extractor.py --paper <paper-id>
python3 .scripts/extractor.py --paper <paper-id> --papers-dir academic/raw/references/
```

- **后续**：读取提取产物并进入 wiki 编码；不要另写临时 PDF 解析兜底。inbox 新流程用 `extractor --external-pdf <路径> --paper <tmp-id> --papers-dir temp/inbox-extract` 在 `temp/inbox-extract/<tmp-id>/` 临时项中提取（实体复制 PDF 后由 MinerU 提取为 `paper.md`），单遍阅读后写 `manifest.json`，再由 `inbox_finalize.py` 落位到最终 raw/wiki（不再"先归档再提取"）。
- **确定性加固**：若目录内没有 `paper.pdf` 且只有一个 PDF，脚本自动实体复制为 `paper.pdf` 并告警；存在多个非标准 PDF 时拒绝猜选。最终引擎不是 MinerU 时输出 WARNING，提示人工复核。
- **MinerU 重试与不回落（2026-08-07）**：MinerU 失败自动重试 3 次（退避 0/5/15s），认证错误不重试（token 问题重试无意义）。重试耗尽后返回 None，**不静默回落** docling/pymupdf（论文质量要求；静默回落会掩盖质量问题）。需用低优先级引擎须显式 `--engine docling` 或 `--engine pymupdf`。
- **启动提示**：`INGEST_BACKEND=api` 时脚本启动即打印后端和模型；这是状态提示，不替代 API 证据卡校验。
- **不要**：对无关文件使用 `--batch` 或 `--force`。

### 2.1b 落位：`.scripts/inbox_finalize.py`

- **何时调用**：inbox 临时区提取并单遍阅读撰写 wiki 后，把 raw 产物与 wiki 草稿从临时区落位到最终目录（配合 §2.1 的 `--external-pdf` 流程）。
- **写入**：只实体复制当前临时项 `manifest.json` 明确列出的 raw 文件到新目标 raw 目录，并提交 wiki 草稿；目标已存在即失败。脚本在 staging 中完成实体和 SHA-256 校验、提交后生成回执；`--cleanup` 仅在当前 wiki 页通过 `ingest_check.py` 后删除当前临时项目录。
- **最小调用**：

```bash
python3 .scripts/inbox_finalize.py --paper-id <id> --raw-dir <最终raw目录> --wiki-path <最终wiki路径> --extract-dir temp/inbox-extract/<tmp-id>
python3 .scripts/inbox_finalize.py --paper-id <id> --raw-dir <最终raw目录> --wiki-path <最终wiki路径> --extract-dir temp/inbox-extract/<tmp-id> --cleanup
```

- **后续**：检查 `temp/inbox-receipts/` 的回执；落位后 `sources` 已指向最终 raw 路径，再按正常 stage 2/3 巩固与校验。不得在落位前把临时路径写入正式 `sources` 或建图边；`ingest_check` 失败时保留临时区和提交产物，先修复再清理 inbox 原件。

### 2.1c 代码驱动论文摄入：`.scripts/ingest_paper.py`

- **何时调用**：inbox 下的学术论文 PDF（`.pdf`）。这是 inbox 学术论文的**首选入口**——代码端到端托管全流程，agent 不手动拆步、不全程监控。非 inbox 来源（已在 raw 归档）或非学术 PDF 不用此脚本。
- **写入**：`temp/inbox-extract/<txn>/`（paper.md/manifest/语义槽/wiki 草稿）；最终 `raw/wiki` 经 `inbox_finalize.py` 原子落位；`graph.db` 经 `graph_ingest.py`；`wiki/log.md`/`index.md`/派生目录。
- **最小调用**：

```bash
python3 .scripts/ingest_paper.py --pdf inbox/<file>.pdf
python3 .scripts/ingest_paper.py --pdf inbox/<file>.pdf --verbose   # 调试/审计时看全程进度
python3 .scripts/ingest_paper.py --resume <txn-id>                   # 恢复中断事务
```

- **内部流程**（agent 不干预，仅了解）：3.1 PDF 确定性书目预提取 + 去重/raw关系检测（PyMuPDF 一次读取 metadata 与第一页页脚；published year 优先；APS DOI 确定性补 venue；证据写入 `bibliographic_meta`；标题 difflib 比对，arxiv ID/DOI 匹配判版本）→ 3.2 提取（MinerU）→ 3.2b 书目预审门（**调 LLM**，只裁决程序候选；每个字段附 `paper.md` 行号证据，机构片段进 `authors.rejected`；`manual_required` 阻断后续写入；通过后锁定 `bibliographic_meta` 并写 `source.yaml.bibliographic`；agent 模式草稿写入事务内 `bibliographic-review.json`，`--resume` 复核后续跑）→ 3.3a 撰写 wiki（**调 LLM**，程序在调用前回填 skeleton、调用后再次覆盖确定性 authors/date/venue；缺 `<<<WIKI>>>` 但输出自身通过完整结构门时可容错复用；API 模式经 `ingest_common.build_source_context("paper", force_reduced=True)` 定向输入，agent 模式用文件路径替代全文）→ 3.4 wiki 结构与书目一致性校验 → 3.3b 抽取短语义槽（**调 LLM**，只输出 `主体|谓词|客体`，不生成 locator/作者/期刊/日期）→ 3.5 语义槽格式化 → 3.6 语义槽校验 → [3.6b 局部修复 warning，两级 LLM 降级] → [3.6c 稀疏命题登记：零 LLM、不改 semantic] → 落位 → 3.7 确定性补书目边、Raw locator、稀疏命题概念链接与 lineage 后写图 → 3.8 Raw–Wiki–Graph 跨层校验 → 3.9 收尾。
- **输出**：默认 quiet，进度写 `temp/inbox-state/<txn>.log`，stdout 只输出最终 JSON（含 paper-id/raw/wiki 路径/边数）。失败时自动把进度日志展开到 stdout，agent 无需额外读日志文件。
- **首页 venue**：`Proceedings of ...` 和 `Published as a conference paper at <venue> <year>` 均视为近端确定性证据；例如 `Published as a conference paper at ICLR 2025` 直接回填 `venue: "ICLR 2025"`，不交给 LLM 猜测。
- **书目候选等价门**：LLM 原则上只裁决程序候选；title 比较前仅消除 PDF/Markdown 的无语义排版差异（`<sup>`、连字符两侧空白、Unicode 兼容形）。当作者/title/venue 的候选生成因缩写、小写姓氏粒子、中文或版式失败时，候选外值只有在其声明的 `paper.md#Lx`（最多 4 行）中机械可检出才可晋升；错误行号和无证据值仍硬阻断。作者机械提取同时允许 `McCulloch` 等词内大写姓氏，避免把原文完整姓名截成伪候选。
- **书目响应兼容与 evidence 重定位**：`conflicts` 可为结构化对象或描述性字符串审计项。title/venue 的值正确但 locator 错误时，只在 `paper.md` 前 160 行存在唯一精确命中时机械改写 evidence；允许 MinerU 把标题拆到最多 4 个连续行（含空行/Markdown 标题符号），重定位写入 normalization。零命中或多处命中仍硬阻断，不做模糊猜测。
- **锁定书目回填**：通过书目门后，程序强制覆盖 Wiki frontmatter/H1 的 title 以及 authors/date/venue，避免 Markdown 转义（如标题尾 `\\*`）进入 YAML；E1 的 semantics wrapper 内容不可解析时在同一 checkpoint 内最多重试 3 次，每次调用均留档。
- **语义 wrapper 容错**：`<<<SLOTS>>>` 被换行拆开或首尾包装畸形时，仅当去除 wrapper 后每个非空行都是完整三元组才恢复；恢复内容仍运行 normalize、semantic parser 和 validator，不能因容错绕过谓词或结构校验。
- **paper-id**：代码生成（`surname-year-slug`），`ensure_unique_paper_id` 冲突自动消歧（-2/-3）。书目预审通过后，`generate_paper_id` 直接消费 locked 的 title/authors/year，不再从 `paper.md` 机械重解析；`--raw`/re-ingest 复用 locked `source.yaml.bibliographic`，旧 raw 则只读相邻 `paper.pdf`，不回写 raw。
- **修复策略**：wiki 硬错误 → 回 3.3a 重写 wiki（清空 wiki_content）；语义槽结构错误（谓词格式、解析错误）→ 早停为 `agent_required`，保留已通过的 wiki/语义槽，修正后以 `--resume` 恢复且提交前必复验；warning（描述性对象、裸缩写）→ 3.6b 局部修补，不回 3.3。3.6b 两级降级链：DeepSeek-V3.2（`ingest_semantic_fill`）→ GLM-5.2（`ingest_semantic_repair`，`INGEST_REPAIR2_*` 配置）→ agent 兜底。仅修补有问题的行，保留已通过内容。
- **驱动器**：`run_commit`（Phase 2 写图→校验→收尾）委托 `ingest_pipeline.py`（`run_pipeline(state, PAPER_COMMIT_SPEC, progress)`）；`run_prepare`（Phase 1 含 propositions/reingest/from_raw 三分支落位）因 paper 专属逻辑保留在本脚本。`is_blocking_warning`/`validate_before_commit`/`handoff_to_agent`/`stop_for_semantic_errors` 不再有本地版，统一调 `ingest_common.py`（脚本名经 `_resume_cmd`/`_validate_cmd`/`NON_BLOCKING_ISSUES` 参数传入）。
- **候选谓词治理**：格式合格的未登记短谓词在成功落位后写入候选队列；`predicate_governance.py` 按 `.scripts/predicate-governance.yaml` 的别名、页面/来源数量与主体一致性自动归一、观察期或正式注册。正式注册只扩展提示词和校验允许集；不自动创建反向关系或研究方向 tier。
- **弱模型质量护栏**：wiki 中的定理、等式、性能结论须保留原文的对象、条件和比较基准；不以常识补全缺失证据。“局限性”仅限作者明示的限制或近似代价。语义槽仅可从 wiki 的明确陈述抽取，不得把“关联/构造/表示”自行改写为“基于”等方向边；原文未明示方向时不建核心词关系边。
- **agent 接管**：仅当 `INGEST_BACKEND=agent`（非 api）时，3.2 书目预审或 3.3 wiki 撰写输出 `agent_required` 交接包（含 `pipeline_plan` + `prompt`）。agent 模式下 wiki prompt 用文件路径替代论文全文（~17K→~533 token），合并 wiki+语义槽为单次任务（省一轮程序往返）。`agent_required` 响应含 `pipeline_plan`（3 步：判断重复+提取全文/撰写 wiki 与语义槽/更新 Graph+校验+收尾）。api 模式全程代码+API LLM 一条命令跑完，agent 零介入。
- **不要**：对非 inbox 来源或非学术 PDF 使用；agent 全程监控正常摄入；把 warning 当失败回 3.3 全量重写。

### 2.2 页面骨架：`.scripts/wiki_skeleton.py`

- **何时调用**：新 wiki 页开始编码前，尤其论文页；用程序填确定性字段和固定 sections。
- **写入**：脚本输出/生成页面骨架，具体行为以当前 help 为准。
- **最小调用**：

```bash
python3 .scripts/wiki_skeleton.py --page academic/wiki/papers/<paper-id>
```

- **确定性边界**：论文页机械读取标题、来源和作者，兼容 MinerU 的 `<sup>` 上标、姓名断词，以及逐行“姓名 + 机构 + 邮箱”的作者块；扫描持续到摘要标题，含邮箱行只取行首姓名，避免把 `UC San`/`Google` 等机构片段当作者。输出仍须对照 `paper.md` 核验，不能把骨架结果当作作者完整性的唯一证据。
- **会议分支**：`academic/wiki/conferences/<id>` 优先定位对应会议 raw 原文，自动填该 raw 来源、`speech-recognition`、`medium`、`current` 与标准 `Navigation`/`Content` 骨架；仅历史页在没有原文匹配时回退到既有 `corrected.md`。会议助手转写的解释性结论不可直接写入页面。
- **后续**：LLM 基于 raw 直接填写 wiki 语义槽并在 wiki 中完成必要纠错/去口语化；`sources` 必须指向 raw。人物解析复用 `entity-resolution.json` 的 resolved 结果给参与者行和后续建边，避免重复提取；检查 `sources`、type、日期、作者等是否符合域 Schema。
- **不要**：手写一套替代 frontmatter 或把骨架当作已完成页面。

> **摄入后端选择**：`.env` 的 `INGEST_BACKEND` 决定语义编码入口（`agent` 或 `api`，`hybrid` 已删除）。`agent`（默认）由当前 Agent 全程处理语义，`ingest_paper.py` 在 3.3 输出 `agent_required` 交接包；`api` 时 `ingest_paper.py` 内部通过 `call_text()` 调用 API LLM，agent 零介入。下方 2.2a 的 `api_ingest.py` 仅用于证据卡受限场景。

### 2.2a 纯 API 语义草稿：`.scripts/api_ingest.py`

- **何时调用**：仅论文已由 extractor 生成 `paper.md`，且 `.env` 的 `INGEST_BACKEND=api`。这是弱模型的唯一摄入语义入口；不能以自由 prompt 代替。
- **程序职责**：从摘要与讨论生成编号证据卡；模型只能输出 `field + claim + evidence_id`，不能填写 raw locator 或引文。程序自动回填可回溯证据，验证短声明、单卡编号、关键词候选和 Schema；超长或多卡声明仅局部修复一次。
- **写入**：默认只写指定 JSON 草稿；不写 wiki 或图。只有 `complete=true`，且目标为 `wiki_skeleton.py` 新建的占位页时，`--apply-page` 才会编译页面和受控 semantic 槽。失败/不完整项追加 `cross-domain/api-ingest-pending.jsonl`。
- **最小调用**：

```bash
python3 .scripts/api_ingest.py \
  --raw academic/raw/references/<paper-id>/paper.md \
  --candidate '候选关键词一' --candidate '候选关键词二' \
  --output /tmp/<paper-id>-draft.json
```

- **完整草稿的受控落盘**：

```bash
python3 .scripts/wiki_skeleton.py \
  --page academic/wiki/papers/<paper-id>.md \
  > academic/wiki/papers/<paper-id>.md
python3 .scripts/api_ingest.py \
  --raw academic/raw/references/<paper-id>/paper.md \
  --candidate '候选关键词一' \
  --output /tmp/<paper-id>-draft.json \
  --apply-page academic/wiki/papers/<paper-id>.md \
  --semantic-output /tmp/<paper-id>-semantic.txt
python3 .scripts/graph_ingest.py ingest \
  --page academic/wiki/papers/<paper-id>.md \
  --semantic /tmp/<paper-id>-semantic.txt
python3 .scripts/ingest_check.py --graph academic/wiki/papers/<paper-id>.md
```

- **不要**：在 `complete=false` 时调用 `graph_ingest.py`；此时 pending 同时携带 `agent_fallback_required` 的最小证据交接包，当前 Agent 应基于该包兜底，而不是重读全文 raw。不要让模型新造关键词、Hub、谓词、日期、作者、期刊或通讯作者。

#### API 模型分工与回退

- **主模型**：`LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL` 用于论文证据卡 claims，默认仍为全部受限任务的稳定回退。
- **可选专用模型**：仅当三项同名配置均完整时，`INGEST_KEYWORD_API_BASE`、`INGEST_KEYWORD_API_KEY`、`INGEST_KEYWORD_MODEL` 优先用于候选关键词选择；`INGEST_REPAIR_API_BASE`、`INGEST_REPAIR_API_KEY`、`INGEST_REPAIR_MODEL` 优先用于格式性定向修复。例如可将 `MiniMax-M3` 配为专用模型，而保留 `DeepSeek-V3.2` 负责 claims 和回退。
- **命题编译不选模型**：论文 3.6c 只登记完整 proposition，概念链接由图写入代码完成；不再调用 `ingest_proposition`，也不因无匹配概念产生 degraded。
- **模型目录**：`operations/config/llm-models.yaml` 保存 provider 当前目录与推荐用途，`enforced: false`；用于人工选择和审计，不做运行时白名单。
- **Embedding API**：`EMBED_API_BASE`、`EMBED_API_KEY`、`EMBED_MODEL` 用于 `embed_helper.py` 的 GLM-Embedding-3 调用（keyword/seed/hub 向量匹配）。默认复用 LLM 同源 endpoint，在 `.env` 中写 `EMBED_API_BASE=${LLM_API_BASE}`、`EMBED_API_KEY=${LLM_API_KEY}`、`EMBED_MODEL=GLM-Embedding-3`；`embed_helper.py` 读取 `EMBED_*` 并回退 `LLM_*`，支持 `${...}` 展开。向量缓存在 `cross-domain/embeddings.db`（文本→向量去重，独立于 `graph.db`）。
- **推理档位**：内部档位为 `fast` / `standard` / `deep` / `xdeep`；关键词选择和格式修复默认 `fast`，claims 默认 `standard`，论文 wiki 撰写默认 `xdeep`。`LLM_REASONING_DEFAULT` 或 `LLM_REASONING_<OPERATION>` 可显式覆盖；档位只作为调用方预算/重试的上限，不主动提高请求。fast 上限允许短结构化任务显式申请一次恢复；只有服务端明确支持时才同时配置 `LLM_REASONING_FIELD` 与相应 `LLM_REASONING_EFFORT_*`，否则不发送 provider 专属 reasoning 参数。
- **文本输出**：需要长文本（如 wiki 页面 + 语义槽）而非 JSON 时用 `call_text`，它镜像 `call_json` 的调用与重试逻辑但跳过 JSON 解析；`agent_handoff` 行为一致，内容合法性由调用方（如 `ingest_paper.py` 的修复循环）负责。语义槽局部修补另有专用操作 `ingest_semantic_fill`（DeepSeek-V3.2，`standard`）与 `ingest_semantic_repair`（GLM-5.2，`INGEST_REPAIR2_*` 配置，`standard`）。
- **配置示例**：若服务端使用 `reasoning_effort`，可设置 `LLM_REASONING_FIELD=reasoning_effort`、`LLM_REASONING_EFFORT_FAST=low`、`LLM_REASONING_EFFORT_STANDARD=medium`、`LLM_REASONING_EFFORT_DEEP=high`；未验证支持时保持这些变量为空。单项覆盖键把 operation 转大写并以非字母数字替换为下划线，例如 `LLM_REASONING_INGEST_API_CLAIMS=deep`。每次响应的 `reasoning_profile` 与实际 `max_tokens` 会保存在 API 调用 history 中供审计。
- **稳定性边界**：专用模型的 API/Schema 失败先回退主 API；主 API 仍失败时写入 pending，并输出 `agent_fallback_required` 交接包，由当前 Agent 仅凭证据卡、候选词和失败草稿兜底。Agent 结果仍须通过相同的证据绑定、`complete`、页面编译和 `ingest_check --graph` 门槛，不能直接提交。
- **成本记录**：每份 API 草稿的 `metrics.cost` 记录 API 阶段的 `agent_calls: 0`、`api_calls`、重试、专用模型调用、回退次数、模型序列和 provider 返回 token；Agent 兜底另行记录，避免混淆 API 与 Agent 成本。
- **Agent 回交**：读取 pending 中的 `agent_fallback`，只以其中的证据卡和候选词生成 `{claims, uncertain, selected, keyword_uncertain}`；写成仓库内 JSON 后运行 `api_ingest.py --agent-draft <draft.json>`。该命令重新执行证据卡编号、字段和候选词校验；只有通过后才能配合 `--apply-page` 与 `--semantic-output` 编译。

### 2.2b 长文颗粒度规划：`.scripts/long_document_plan.py`

- **何时调用**：综述、书籍、长政策等可能需要总览页与章节页的 Markdown/TXT raw；先评估再决定是否拆分。
- **写入**：否。输出长度、标题树、候选章节、关键词预算与 `semantic_review_required`；不会自动拆页、写关键词或建边。
- **最小调用**：`python3 .scripts/long_document_plan.py <已归档 raw 路径>`。
- **后续**：仅当 `semantic_review_required=true`，让 LLM 一次确认候选章节是否可独立回答；确认后创建总览页和最多一层章节页。关键词密度只作候选信号，入图仍需导航价值与证据确认。
- **不要**：把 `candidate_trigger` 当作自动拆分指令，或让频率高的套话直接成为关键词。


### 2.3 会议语音文本：`.scripts/speech_entity_resolver.py`

- **何时调用**：`conferences/` 或 `discussions/` 下的 ASR `.txt` 在摄入前需要人物/别名候选与安全纠正。
- **写入**：默认只输出 `entity-resolution.json`；`--apply` 仅为历史页写派生 corrected 文本。原始 `.txt` 不变。
- **最小调用**：

```bash
python3 .scripts/speech_entity_resolver.py <raw.txt> --output <resolution.json>
python3 .scripts/speech_entity_resolver.py <raw.txt> --output <resolution.json> --apply <corrected.md>
```

- **后续**：只让程序应用唯一精确 alias；模糊 `review` 候选须结合局部上下文由人/LLM 审查。新流程直接由 raw 编码 wiki，并在 wiki 上完成必要纠错/去口语化；`entity-resolution.json` 的 resolved 结果同时供参与者和建边复用。已有 `corrected.md` 保留供历史回溯，不作为新流程输入；事实底线始终是 ASR raw。
- **不要**：把模糊匹配自动当事实，或覆盖 raw。

### 2.3a 网页资料摄入（web-reference）

- **何时调用**：抓取的网页内容需摄入为可回溯的 wiki 页（非论文 PDF、非会议纪要）。
- **raw 存放**：`*/raw/web-references/YYYY/YYYYMMDD-<标题简写>.md`（纯文本 md，保留原文关键内容不加工；头部用固定字段块记录来源、原文链接、发布/抓取时间与机构）。格式见 `operations/shared-conventions.md`。
- **wiki**：type 为 `web-reference`，frontmatter `source_type: web`，`url`/`source_name`/`confidence` 与 raw 头部对齐。
- **confidence 分层**：按来源权威性分层——官方/权威站点→`high`，论坛/自媒体→`low`，默认 `medium`（档位规则见 `academic/SCHEMA.md`）。
- **后续**：`ingest_check.py` 已在所有域枚举 `web-reference` 类型；编码后照常运行 stage 2/3 巩固与校验，事实仍回溯 raw。

### 2.4 引文：`.scripts/extract_citations.py`

- **何时调用**：论文巩固阶段，需从 `paper.md` 机械提取引用以生成 citation-only 实体和引用边。
- **写入**：机械提取本身通常输出 JSON；随后的 `graph_ingest.py` 才写图。
- **模式**：脚本是位置参数模式，不要用裸 `--help` 试探。常用模式由 `INGEST.md` 定义：

```bash
python3 .scripts/extract_citations.py mechanical <paper.md> > <citations.json>
python3 .scripts/extract_citations.py prefill <paper.md>
```

- **后续**：将 JSON 交给 `graph_ingest.py ingest --page ... --citations ...`；复杂格式才走 prefill 后由 LLM 填语义槽。

### 2.5 图写入：`.scripts/graph_ingest.py`

- **何时调用**：巩固阶段，页面语义内容、关系证据和关键词已审核，要增量 upsert 页面节点、aliases、实体、边、引文及研究方向归属。
- **两阶段建图（2026-08-25）**：`graph_delta.py` 先在内存构造 `GraphDelta`（Wiki anchor + Raw 文档包 + 来源骨架 + 受限 triples），再由确定性 attach plan 与主图连接。程序明确标记的 canonical endpoint 经 `resolve_node_id` 只按 path 复用；Raw/LLM surface mention 经 `resolve_node` 检查 path/title/alias 全部同名候选，即使字符串恰等于某个 path 也不享受直通。唯一候选才复用，多候选可用上下文+description 消歧；仍歧义则 soft abstain 并由 writer 跳过整条边，不创建碰撞节点。未命中新建本地节点。overlay query probes 检查 anchor、Raw 一跳、边界两跳和候选负担，但永不因软探针未满分阻断。实际 writer 在 SQLite SAVEPOINT 内执行，融合后缺 Wiki/Raw 来源骨架才回滚。`add_knowledge_edges` 只按 attach plan 做 proposition/keyword 节点编译和写边。详见 INGEST.md「文档子图与主图融合」。
- **稀疏 proposition 编译（2026-08-25）**：完整命题保留为 proposition 节点。`proposition → 包含 → concept` 只由代码匹配本页已确认概念（含确定性中英/缩写拆解）和主图唯一精确 title/alias；歧义或未命中静默跳过，不告警，不从句内片段创建 `研究`、`输出分数` 等新 concept。保守 proposition embedding 对齐继续独立运行。
- **节点身份与双视图 embedding**：`nodes.path` 是稳定 node ID，`title` 是显示名，`aliases` 为多对多名称入口，`description` 是可空消歧说明。GraphDelta 先走 path/title/alias；普通知识 surface mention 限定在 entity 范围，纯引用标题保留 page/entity 双类型。完整双语名再由 `decompose_name_to_aliases` 拆成中文、英文、缩写。中文/英文完整名称的精确 title/alias 命中并集只有一个 node ID 时确定性复用，完整名称冲突则 ambiguous；缩写只在完整名称均未命中时兜底，不能推翻唯一完整名称。其余轻微名称变体只有存在代码化 lexical identity signal，并同时通过 `node_semantics.resolve_node` 的 label/semantic 双门、唯一候选与 margin 才复用。单一相似度绝不触发 merge。`node_semantics.semantic_search` 只做相关性召回，返回 `identity_claim=false`。候选语料只读既有缓存，普通 Agent 调用不会批量生成全图向量；embedding 失败机械降级。
- **图健康前置检查（2026-08-07）**：`cmd_ingest` 在 `cleanup_ghost_hubs` 后调用 `cleanup_orphan_references`，清理孤儿 alias（`node_path` 不在 nodes 表）+ 孤儿边（FK 违规）。防止上一篇摄入/回滚遗留的脏数据阻塞下一篇的外键约束。幂等，每次摄入前清扫。
- **多形态 alias 即时拆解（`graph_lib.decompose_name_to_aliases`，2026-08-10）**：`_ensure_entity_node` 建实体节点时即时把拼接概念名（如「矩阵乘积态matrix product state(MPS)」）拆为缩写/中文/英文全称，调用 `insert_aliases` 注册多形态 alias。使 `resolve_bare_name` 输入任一形态都能命中同一节点，从源头减少碎片节点。与 `sync_keyword_aliases`（摄入前全库扫描补建）互补——即时覆盖 + 定期扫漏。
- **proposition alias 不进 resolve 索引（2026-08-13）**：proposition 是描述性命题、非概念端点，`decompose_name_to_aliases` 对其拆出的句内片段（如「实验局限于MPS架构」→`MPS`）不应参与概念 resolve。双重排除：①`build_name_index` 的 `alias_idx` JOIN nodes 排除 `entity_subtype='proposition'`（同 `suffix_idx` 已有排除，防御跨论文重新构建）；②`_ensure_entity_node` 对 proposition 不 in-place 更新 `alias_idx`（防御单次摄入内污染）。否则命题句内片段与概念 alias 撞成多 path → resolve 歧义 → `bare_abbreviation` warning 误保留。
- **证据 locator 与确定性元数据**：LLM 只提供短三元组；`attach_evidence_locators` 在不可变 Raw 中按实体词机械匹配，命中写 `path#L<n>` 与短 `evidence_quote`，否则显式退到 `path#全篇`。作者、venue 等边从 wiki frontmatter/source metadata 确定性生成；占位符不入图。结构性 `包含/相似` 边标为 `推断`。
- **类型与低噪声审计**：`第一作者` 也是 person 关系；已有 keyword/concept 节点经确定性人物关系复用后会升级为 `entity_subtype=person`，无 People page 时不参与 Hub。`发表于` 的 ICLR/NeurIPS 等规范 venue 简称不触发 `bare_abbreviation` warning。
- **稀疏导航与 lineage**：Graph 只保存检索导航关键关联。命题内部概念不得因“保证论文直达”自动提升成论文级 `研究关键词`；未直连项仅进入 `navigation_connectivity_candidates` 报告。所有页面贡献的语义边、程序派生包含边与相似边写 `edge_origins`；该表只记录 re-ingest lineage，不充当事实证据。
- **写入**：`cross-domain/graph.db`；仅 `arxiv-directions.yaml` 中的根方向可按配置创建 Hub，其他 Hub 仅由关键词聚类/分裂融合涌现；语义槽方向对象必须解析到唯一既有正式 Hub path，同名 Hub 冲突或未命中时不建方向边。程序从 wiki 页 `sources` 机械建立同词干 Raw 文档包节点和 `Wiki → 来源 → Raw` 边（`ensure_raw_support_edge`，零 token），再原子融合 GraphDelta；摄入末尾自动调用 `dynamic_split`（失败不阻断）。
  - 对 admin `policy`/`procedure`/`decision` 与 teaching `course` 页，`sync_page_temporal_fact` 另写一条 canonical `temporal_facts`（subject/object=页路径、predicate=`生效`），时间来自 frontmatter `effective_from`/`effective_to`；重复摄入 idempotent 替换，`--clean` 会同时清旧时态事实。
- **主要子命令**：

| 子命令 | 使用时机 | 风险 |
|---|---|---|
| `ingest` | 对一个已完成/待巩固 wiki 页建边 | 正常写图 |
| `prefill` | 先生成机械边与语义槽模板，再让 LLM 填 | 主要只输出模板 |
| `merge` | 明确确认两个节点是同一实体 | 迁移边、删除源节点，需谨慎 |
| `init` | 首次建库或补缺失表 | 幂等(仅 CREATE IF NOT EXISTS),不删已有数据;重置需删 graph.db 后重 init |

- **最小调用**：

```bash
python3 .scripts/graph_ingest.py ingest --page academic/wiki/papers/<paper-id>
python3 .scripts/graph_ingest.py ingest --page academic/wiki/papers/<paper-id> --citations <citations.json>
```

- **后续**：运行该页 `ingest_check.py`，必要时用 `query_graph.py node <path>` 或 `graph_dump.py --node <path>` 审核落库结果；记录 wiki log。
- **不要**：把裸文本关系随意塞入图；边需要规范名、谓词、置信和来源定位。不要未经实体消歧执行 `merge`。

**会议关键词分支**：会议页不用论文的研究方向/核心方法槽。使用下列最小语义文件，并在写图后运行 `ingest_check --graph`：

```text
会议关键词:
讨论 | 模型压缩
汇报 | 知识蒸馏
规划 | CoT评测
```

它只生成会议→概念边；不直接建立会议→Hub 边。相关普通节点可在图融合后经统一 Hub 动力学局部归类；未归类是正常状态。

### 2.6 Hub membership 与 Scope 路由：`.scripts/hub_semantics.py`

- **路由契约**：论文只用可 locate 的 `## 研究方向定位` 一句与 active Hub `## Scope` 比较；top-1 通过 floor（`ROUTE_FLOOR=0.5`）即写 `论文 Wiki → 主要研究 → Hub`；不再要求 margin，避免近邻 tie 导致论文悬空无 hub 归属。
- **动态 membership**：keyword 用 `title+description`，proposition 用 canonical statement，People page 只用可 locate `## 人物画像`。程序结合 Scope、同类成员原型和图邻接 affinity，允许一个节点归入多个 Hub，并以进入/保留双阈值防抖。摄入后只刷新本页一跳内节点，写可重建 `聚类于` 边。
- **People 边界**：研究人员画像写对象/问题/方法，行政人员写职责/服务范围，学生写阶段/关注方向；其他角色按实际导航语义写。无 page 的 person entity、无人物画像页面均静默不参与。
- **身份与职责**：Hub 由稳定 path、简短 title 和必填 Scope 定义。代码生成 membership、new/split/merge candidates、probes 和超限检查；主 Agent 确认 title、Scope、parent 及 create/split/merge。三代 `子方向` 血亲 Hub 对禁止合并（`has_blood_relation`，见 §3.2 血缘迁移说明），防合并-分裂死循环；`姻亲` 边不恢复。API LLM 不得决定聚类或激活 canonical Scope。
- **兼容边界**：`direction_matcher.py`、`hub_split.py`、seeds、Hub 关键词和 catch-all 只保留旧数据兼容，普通 ingest 不再调用或写入。未归类、歧义、无 Scope、embedding 不可用和生命周期候选均为 soft state，不报摄入 ERROR/WARN；embedding 失败时不删除旧 membership。
- **摄入末期自动建 Hub**：`ingest_inbox.py` 摄入后置步骤调 `hub_semantics auto-create --check`，对全图 unassigned 普通节点跑 `dynamics_plan(apply_membership=True)` + `analyze_new_hubs`；候选簇 cohesion≥0.6 且 members≥4 为达标，写入 `temp/hub-auto-create/<session>.json` 并在摄入报告设 `hub_auto_create.status=agent_required`，触发 agent（强模型）为每簇生成 title/Scope/parent（不经 API LLM），写定义文件后调 `hub_semantics auto-create --apply <file>` 由 `create_hub(agent_confirmed=True)` 落盘并 apply membership；不达标候选静默进 backlog，全流程不向用户报告。
- **去重门槛（3.1 dedup_check）**：标题相似度 > 0.95（`TITLE_DEDUP_GATE`）才作为候选进入 metadata 判断；图扫描和 raw 目录扫描（预筛后读 paper.md 真实标题）均用此门槛。候选过门槛后通过 DOI/arXiv ID 确认：ID 匹配 → 确定重复；无 ID 但标题 > 0.95 → 不确定重复（agent 确认）；标题 ≤ 0.95 → 不判重。
- **语义槽四类客体校验（3.6 validate_semantics + graph_ingest add_knowledge_edges）**：`descriptive_phrase`（谓语结构/标点超长，**非阻断型**——2026-08-05 proposition 改革：命题谓词 object 作为 proposition 节点入图，不进 3.6b 修复；validator 对 subject 与 object 均查）、`bare_abbreviation`（含英文缩写但无括号释义；keyword 谓词查 object，自由边查 subject+object（跳过结构性谓词包含/相似——端点来自命题/概念,已在命题/keyword 谓词审计过,重复检查只对同一 proposition 多条包含边重复告警）,warning 携带 value(被标记文本)+field(subject/object),object 存真实客体(2026-08-24 修正:旧实现 field=subject 时把 subj_raw 误写入 object 致报告呈现伪自环);两层校验 2026-08-02/03 扩展;2026-08-06 语义改革：融合期 `_revisit_bare_abbreviations` 复查，resolve 到 keyword 的移除 warning，resolve miss 保留交 `resolve_abbreviations.py` 后置双层校验；2026-08-21 三段式消解：`ingest_paper._load_raw_abbr_map` 从 raw paper.md 正则提取 `Full Name (ABBR)` 配对，`_autofix_bare_abbreviations` 在 `step_validate_semantics` 开头自动补全精确匹配的裸缩写为 `Full(ABBR)` 写回语义槽，原文亦无定义才保留 warning）。2026-08-21 会议/文档摄入对齐论文：`non_blocking_issues` 从空元组改为 `("bare_abbreviation", "descriptive_phrase")`——这两类 warning 仍产生但不阻断写图（后置 alias 兜底 + raw 完整保真），agent 模式下不再因格式 warning 卡 agent_required、`citation_fragment`（MinerU 误建的参考文献残片，`is_citation_fragment` 检测「年份;:页码」格式跳过建节点，2026-08-02 新增）、`duplicate_line`（完全相同三角组行，`normalize_slots` 机械去重 + `step_validate_semantics` 兜底检出，2026-08-02 新增）。均产生 `descriptive_warnings` 不阻断摄入。warning 带 `field`（subject/object）标记哪列出问题。
- **局部修复匹配（`patch_semantic_lines`，3.6b，2026-08-03 重构）**：按 `(谓词, 原客体)` 定位行（不按 subject 匹配——parse 后 warning subject 是全路径，sem 行是「本论文」）。修复行解析为有序列表带 consumed 标记，按 subj_keys（warning 主体→当前主体→None 兜底）线性查找消费。**写回时按 field 只替换对应列**：改 object 保留原 subject+谓词，改 subject 保留原谓词+object。**拆解式修复**：descriptive_phrase 客体含主谓宾时，LLM 输出多行（替换行 + 新三元组），未消费的新行自动追加到三元组段——长客体是被折叠的关系，还原结构而非丢信息。
- **抽取端主谓宾拆分（`build_slots_prompt`，3.3b，2026-08-03）**：客体含主谓宾结构时输出 4 组三元组——原行保留 + 2 条拆分边（`原节点 | 拆分 | 主概念` + `原节点 | 拆分 | 工具概念`）+ 1 条语义边（`工具概念 | 原句动词 | 宾概念`，谓词从原句提取零臆造）。原行忠实记录表述，拆分边下钻导航，语义边还原结构关系。
- **修复端补拆分（`_build_repair_prompt`，3.6b，2026-08-03）**：descriptive_phrase 客体含主谓宾时，修复指令要求补出 4 组三元组（原行+2拆分边+1语义边）；仅过长带标点不含主谓宾的截短为规范名词。
- **ghost hub 兜底（`cleanup_ghost_hubs`）**：每次 `graph_ingest ingest` 前自动清扫 `.md` 已删但 graph.db 残留的 `type='hub'` 节点 + 关联边（2026-08-02 新增）；独立入口 `graph_ingest.py cleanup-ghosts` 手动全库清扫。
- **写入**：使用/更新 embedding 缓存；不直接写页面关系。
- **最小测试**：

```bash
python3 .scripts/hub_semantics.py route academic/wiki/papers/<paper-id>
python3 .scripts/hub_semantics.py profile <node-id>
python3 .scripts/hub_semantics.py dynamics-plan [--node <node-id>] [--apply-membership]
python3 .scripts/hub_semantics.py inspect academic/wiki/hubs/<hub>
python3 .scripts/test_hub_semantics.py
```

- **节点对齐**：新概念的同一性统一交给 `GraphDelta` + `node_semantics` 的名称门/语义门；不再扫 Hub `## 关键词` 作去重库。

- **proposition 对齐**（`graph_ingest._align_propositions`）：proposition 节点的跨文档对齐采用双阈值梯度：cosine > 0.98 → 合并节点（新 proposition 用已有名替换，不建重复节点）；0.9 ≤ cosine ≤ 0.98 → 建关联边（`新 | 关联 | 已有`，各自保留独立）；< 0.9 → 不动。基础设施 `embed_helper.match_by_embedding` 是统一对齐框架。keyword 身份消解不经独立去重函数，由 `GraphDelta.plan_attachment` → `node_semantics.resolve_node` 双视图 identity gate（label ≥ 0.92 + semantic ≥ 0.90 + combined ≥ 0.91 + margin ≥ 0.04，且需词汇身份信号）完成；旧 `_dedup_semantic_keywords`（0.92 阈值，扫 Hub `## 关键词` 段）已删除（2026-08-26，dead code）。

- **颗粒度查询**（`query_graph.py search --granularity`）：按 `entity_subtype` 过滤搜索结果。`--granularity keyword` 只返回概念节点（导航聚合类查询："哪些文献涉及某主题"）；`--granularity proposition` 只返回论断节点（精确推理类查询："谁验证/反对某 claim"）；不传则返回所有颗粒度。颗粒度由查询意图驱动——查询层提供能力（按颗粒度过滤），意图判断由调用方（agent）做。

### 2.7 文档版本关联：`--related-to`（2026-08-22）

当 inbox 中有同一材料的不同版本/补充件（如草稿+盖章扫描件），用 `--related-to` 关联摄入，关联文档独立建 wiki 页并关联到主文档：
- **主文档**：正常走 `ingest_inbox.py --run --file <主文档>`
- **关联文档**：`ingest_document.py --file <关联文档> --related-to <主文档wiki路径> --relation-type version|supplementary|translation`
- **效果**：关联文档正常建 wiki 页 + 图页面节点 + 语义/机械边（与普通文档一致），额外在 raw 间建 `后一版本`/`补充材料`/`译自` 关系边，并将关联文档的 raw source 追加到主文档页 `sources` 字段
- **共享函数**：`ingest_common.link_raw_relation(state, REPO, target_page, relation_type)` + `ingest_common.append_source_to_page(REPO, page_path, new_source)`
- **inbox 关联提示**：dry run 时 `_relation_hints()` 分析文件名相似度和版本关键词（盖章/扫描/补充/v2），输出关联提示供 agent 制定摄入计划

### 2.8 摄入即时校验：`.scripts/ingest_check.py`

- **何时调用**：每个新建或修改 wiki 文件收尾前；更新、批量摄入也不省略。
- **写入**：否。
- **最小调用**：

```bash
python3 .scripts/ingest_check.py academic/wiki/papers/<paper-id>.md
python3 .scripts/ingest_check.py --graph admin/wiki/policies/<page>.md
```

- **结果处理**：ERROR 阻断，按明确报错定向修复再复验；WARN 不阻断，但向用户提示。结构校验覆盖有效日期（`YYYY`/`YYYY-MM`/`YYYY-MM-DD`）及 `updated >= created`；论文页会以与骨架相同的解析器比较 raw/wiki 作者数。`--graph` 是显式选项，检查页面节点、该页带 `source` 的出边和已废弃的 `contains` 边；历史页尚无来源边时仅 WARN，不阻断迁移。
- **不要**：用 `--help` 当作帮助入口（它被当路径处理）；直接读脚本 docstring/规范获取扩展参数。
  - **方向锚点检查**：`--graph` 校验方向边（`主要研究`/`涉及` 等）时，锚点 seeds 必须与写入侧（`direction_matcher._load_direction_defs()`）同源——根方向取 `arxiv-directions.yaml` seeds（hub frontmatter 按规范为 `[]`），子方向取 hub frontmatter seeds。若两边 seeds 源不一致，会出现写入通过、校验失败的假阻断。修复方向：改 `ingest_check.py` 锚点源，不手动改 wiki Navigation 补方向名。

### 2.8 派生构建：`.scripts/ingest_build.py`

- **何时调用**：`ingest_check` 通过后，按 INGEST 规范同步本次页面受影响的派生目录、索引或审计产物。
- **写入**：派生物，可影响目录/索引；不替代图写入和即时检查。
- **调用原则**：此脚本有自己的位置/子命令协议，未知参数会报错；从 `INGEST.md` 的当前收尾步骤复制命令，不要对全库猜测调用。
- **后续**：检查脚本报告，再完成自检、日志和用户可见 WARN 提示。

### 2.9 增量计划：`.scripts/ingest_plan.py`

- **何时调用**：新增、更新或批量摄入前，先确定本次 raw 变化和最小候选范围。
- **写入**：默认不写；`--write-state` 仅写 `cross-domain/ingest-state.json` 的 raw 指纹快照。
- **最小调用**：

```bash
python3 .scripts/ingest_plan.py \
  --raw-root academic/raw \
  --wiki-root academic/wiki
```

- **结果处理**：只把 `changed_raw`、`removed_raw`、`affected_wiki_pages` 当作计划输入；仍须按正常 stage 1→2→3 摄入。确认处理成功后，才可追加 `--write-state`。
- **不要**：把计划当作摄入执行器；不要用它修改 raw、wiki 或 `graph.db`，不要因 `requires_llm` 就扩大到全库调用模型。

### 2.10 派生指纹：`.scripts/derivation_state.py`

- **何时调用**：API 摄入记录草稿 provenance，或判断已有语义派生物是否因来源/规则/prompt/model 变化而过期。
- **写入**：无独立 CLI 写入；由 `api_ingest.py` 等编排器把 provenance 写入 JSON 草稿。
- **关键接口**：`sha256_file()`、`provenance()`、`is_stale()`。
- **边界**：它只提供状态判断，不自动重跑 LLM、不自动应用差异、不覆盖人工内容。过期结果必须回到受限 API/正常编译和检查流程。

### 2.11 重新摄入已入库论文：`.scripts/re_ingest.py`

- **何时调用**：管道版本升级后，需对齐已入库论文的 wiki 与图边。raw 不可变（红线），只重生 wiki + 清旧图边 + 重建。
- **写入**：覆盖 `academic/wiki/papers/<id>.md`（旧版备份至 `temp/inbox-state/<txn>-wiki-old.md`）；`cross-domain/graph.db` 经 `graph_ingest.py --clean` 清旧边后重建；`page-catalog.md` 重建；`academic/wiki/log.md` 追加。
- **管道版本戳**：`graph_lib.py` 的 `CURRENT_PIPELINE_VERSION` 控制版本号；影响输出的建设变更才 bump（纯改名不 bump）。`graph_ingest.py` 的 `upsert_page_node` 写入 `ingest_version` 到 page 节点；`--outdated` 据此查落后论文。
- **最小调用**：

```bash
python3 .scripts/re_ingest.py --raw academic/raw/references/<paper-id>/paper.md
python3 .scripts/re_ingest.py --manifest          # 全量
python3 .scripts/re_ingest.py --outdated          # 仅管道版本落后的论文
python3 .scripts/re_ingest.py --outdated --dry-run # 预览落后清单
```

- **边界**：raw 全程不动；`--clean` 先删 page 直连边，再按 `edge_origins` 撤销本页对概念间/命题间边及 evidence 的贡献。共享边仍有其他 origin 时保留并切换主 source；无剩余 origin/evidence 才删除。lineage 上线前的历史边按本页 raw source 保守回收；不重跑 MinerU（raw 已有 paper.md）。
- **不要**：在纯改名类变更后跑 re-ingest（不 bump 版本，`--outdated` 不会选中）。

## 3. Hub 工作流：Scope 路由与 Agent 受控写入

### 3.1 `.scripts/cluster_keywords.py`（legacy 只读兼容）

> 以下命令与机制仅用于理解历史数据，不是当前 Hub 建设入口。新摄入不写 catch-all，不得运行 `--apply`。

- **何时调用**：catch-all 队列（`未归类关键词.md`）的关键词累计达到规范阈值，或用户要求审视未归类词能否涌现新主题。
- **写入**：`--analyze`/`--status` 只读；`--apply` 会新建 Hub、迁移关键词，可能改图。
- **安全调用**：

```bash
python3 .scripts/cluster_keywords.py --status
python3 .scripts/cluster_keywords.py --analyze --hub academic/wiki/hubs/未归类关键词.md
```

- **执行写入**：仅在用户已经确认候选主题名称、边界和执行意图后：

```bash
python3 .scripts/cluster_keywords.py --apply
```

- **后续**：核对新 Hub 的名称、关键词、重复度、成员页面和图连通性；运行相关检查并记录变更。
- **不要**：把“紧密连通簇”直接喂给 `--apply`，或未经确认自动创建一批主题页。

### 3.2 `.scripts/hub_split.py`（legacy 只读兼容）

> 以下关键词数量分裂、seeds 和质心合并机制已退休，仅保留历史实现说明；不得从普通 ingest 或弱 LLM 调用写命令。当前 create/split/merge 以 `operations/HUB.md` 和 `hub_semantics.py --agent-confirmed` 为准。

- **何时调用**：研究方向 Hub 的关键词数 ≥ `SPLIT_THRESHOLD`(80) 时触发分裂；`graph_ingest.py` 的 `cmd_ingest` 在摄入末尾自动调用 `dynamic_split`（失败不阻断摄入）。`check_all_hubs` 扫描 `gl.SUBPROJECTS` 全部四域的 `*/wiki/hubs/`（不只 academic），`cmd_ingest` 全量重扫所有超阈 hub（3 轮 + `attempted` 防重复），消除「本次未涉及则长期超限」的滞留。
- **动态分裂+融合机制**（`dynamic_split`）：分裂 → 子 Hub 与已有 Hub 做 embedding 质心对比 → 相似度 ≥ `MERGE_THRESHOLD`(0.85) 且无血缘关系则融合 → 融合后若超阈值再分裂 → 循环至收敛或达 `MAX_SPLIT_ITERATIONS`(5)。路径归一化（入口去 `.md`，与 `nodes.path`/`create_subhub` 返回一致）。防卡死：`next_pending` 收集本轮分裂/融合后仍超阈的 hub 进下轮（每 hub 经 `hub_keyword_count` 读实际写入数判定）→ 收敛检测（无变化则退出）→ 迭代上限。防乒乓：`processed` 集合保证每 hub 至多分裂一次（A 分裂→子融 B→B 超阈再分裂→子融 A→A 在 `processed` 内不再分裂），`processed` 内 hub 融合后即便超阈也不本轮重分裂，留 `unresolved` 标记由下次 ingest 全量重扫兜底。
- **血缘关系检查**（已迁移）：`get_ancestors` / `has_blood_relation` / `get_child_hubs` 现已迁移到 `hub_semantics.py`（`:1000`/`:1018`/`:1025`），新实现沿 `子方向` 边三代追溯，**不含 `姻亲` 边**（`姻亲` 退役，retired Hub 不参与 merge 候选，`graph.yaml` 契约显式禁止恢复）。`hub_consanguinity_audit.py` 已删除（dead code，无外部调用点）。以下为旧 `hub_split.py` 历史实现说明。
- **边方向约定**：`子方向` 边为 `parent → child`（subject=父, object=子）。`add_child_edge` 创建 `(parent_path, "子方向", sub_path)`。查子节点用 `SELECT object FROM edges WHERE subject=? AND predicate="子方向"`；查父节点用 `SELECT subject FROM edges WHERE object=? AND predicate="子方向"`。
- **合并机制**（`merge_hubs`）：source 的子节点先 reparent 到 target（`_reparent_children`：保持 `子方向` 关系），再迁移 source 的剩余边到 target（`子方向`→`姻亲`，含自环守卫：跳过 `obj==target` 的边防自引用）。source 的父边迁移后变为 target 的 `姻亲` 边。
- **合并-分裂死循环防护**：① 三代血缘+姻亲禁止合并（`has_blood_relation`）；② `combined_kw > SPLIT_THRESHOLD` 时合并降级为重分配（`too_large` 检查）；③ `processed` 集合防乒乓（每 hub 至多分裂一次，融后超阈留待下次 ingest 兜底，替原 `merge_targets`）；④ `skip_pairs` 跳过零移动对；⑤ 迭代上限（`MAX_SPLIT_ITERATIONS`=5, `max_iterations`=15）。
- **Hub 整合检测**（`detect_hub_consolidation`）：两阶段判定——Stage 1 全维度一致（name_sim + seed_jaccard + centroid_sim）→ MERGE；Stage 2 质心近 + keyword 混配 → 可分性测试（`sep_margin` 分布统计量）→ 不可分则 MERGE，可分则 REALLOC。`--consolidation` dry-run，`--run-consolidation` 执行。
- **Seeds 维护**：`--dedup-seeds` 全局互斥（同一 seed 只保留在一个方向）；`--name-seed` hub 名与 seed 互斥（hub 名优先）；`sync_hub_seeds_if_drift` 在 keywords 变动超 1/3 时重选 seeds。
- **种子冲突父子直系豁免**（`direction_matcher._load_direction_defs`，2026-08-13）：加载时校验同一 seed 不跨方向出现；直系父子（parent 链可达）共享种子豁免——子方向继承父种子属合理语义不报 WARN；非直系共享仍报 WARN 促人工核验。`_seed_owners` 改为列表以正确处理多归属。
- **种子冲突缓存化与降级**（`direction_matcher`，2026-08-21）：`_load_direction_defs` 加进程级缓存（`_CACHED_DEFS` + `_CACHED_DEFS_SIG`，签名基于 CONFIG_PATH 与所有 hub 文件 mtime），同一摄入内多次调用只读一次配置、只校验一次冲突；`force=True` 时缓存失效。种子冲突从 stderr `print` 降级为 `_SEED_CONFLICTS` 结构化列表，经 `get_seed_conflicts()` 供 `ingest_check.py` lint 汇总报告（汇总段末尾输出种子冲突告警），摄入日志不再刷屏。
- **assign_keyword_hubs 兜底追踪**（`graph_ingest`，2026-08-21）：返回值从 `(synced, unrecognized)` 扩展为 `(synced, unrecognized, hub_fallback_kws)`，`hub_fallback_kws` 记录 direction 未命中但被主方向 hub 兜底接管的 keyword；`derived_directions` 报告新增 `hub_fallback_keywords` 与 `truly_orphan_keywords`，原 `unmatched_keywords` 重命名为 `direction_unmatched`（标明是 direction_matcher 层未命中，非最终悬空）。
- **同题子方向解析**（`graph_ingest.assign_keyword_hubs`，2026-08-24）：embedding 可能返回方向语义名而非 hub 路径；归并 keyword 前先按 `nodes.title` 精确解析既有 hub，避免为已存在的 `子方向-*.md` 再建同名根 hub。
- **fallback 导航候选触发 hub 兜底**（`graph_ingest`，2026-08-25）：无文本锚点时 fallback 分支除写报告外，将最佳弱匹配候选经 `resolve_semantic_direction_hub` 解析为既有 hub 后 append 进 `dir_preds`（谓词 `涉及`，不产生 sem_triples 事实边），使 `assign_keyword_hubs` 的 `main_hub` 兜底分支能执行，keyword 不再全落 `truly_orphan`；符合合同「fallback 只能进入报告/Hub 兜底，不写事实边」。
- **近亲互斥**（`direction_matcher.classify_keywords`）：两层——直系父子 + 祖孙互斥（隔一代仍直接互斥，后代优先）；曾孙及以下不互斥（允许多归属）。
- **切分点选择**（`cluster_hub_keywords`）：对层次聚类遍历切分点 t，`evaluate_split` 算覆盖度−重合度作为综合得分；v8.2(2026-08-13)：得分乘均衡因子 `balance=min(簇大小)/max(簇大小)`，避免选极不均衡切分（如 83:6 导致子 hub 仍超阈需递归）；选最优 t 时打印覆盖/重合/均衡/簇大小便于调试。
- **重分配规则**（`redistribute_keywords`）：keyword 与子 seeds 最高 cosine ≥ 0.65 → 归子；全 < 0.65 且与父质心 < 0.65 → 推到最高分子（不污染父）；其余 → 留父兜底。
- **seeds 自动补充**（`ensure_hub_seeds`）：seeds 不足（< `MIN_SEEDS`=3）时，从 hub `## 关键词` 段取 embedding 质心最近的 top-`SUBHUB_SEED_COUNT`(10) 个 keyword 补充并写回 frontmatter。条件：hub 有 ≥ 3 个关键词才补充。消费点：`direction_matcher.load_directions`（子方向 seeds 不足时）+ `merge_hubs`（融合前确保双方 seeds 充足）。 **域守卫**(2026-08-25)：补 seed 时质心锚点用 stored seeds（`kept`）而非 available candidates，防止跨域论文误入后从其 keyword 补出跨域 seed（污染自放大循环）；`sync_hub_seeds_if_drift` 漂移重选同理，锚点用仍有效的 old_seeds（非 stale）。
- **CLI**（手动分析/诊断用）：

```bash
python3 .scripts/hub_split.py --check
python3 .scripts/hub_split.py --analyze academic/wiki/hubs/<hub>.md
python3 .scripts/hub_split.py --split academic/wiki/hubs/<hub>.md
python3 .scripts/hub_split.py --consolidation
python3 .scripts/hub_split.py --run-consolidation
python3 .scripts/hub_split.py --dedup-seeds
python3 .scripts/hub_split.py --name-seed
python3 .scripts/hub_split.py --merge-duplicates
```

- **命名约定**：子 Hub 使用独立短名，**不要在文件名或标题中拼入父 Hub 名字**；父子关系由 `子方向` 边承载。`--split` 默认生成 `子方向-<uuid>` 占位，需在分裂后立即改为语义短名。
- **后续**：检查父子关键词互斥、子 seeds、`子方向`/`姻亲` 边和查询可达性；建议用户完成语义重命名，并把旧名在 wiki/log.md 留痕。

### 3.3 `graph_metrics.py` 不是 Hub 创建器

`graph_metrics.py tight_clusters` 可报告无 Hub 覆盖的连通分量，帮助发现结构缺口：

```bash
python3 .scripts/graph_metrics.py tight_clusters
```

它不判断主题含义，也不应直接触发新 Hub。当前候选信号是可 locate 的文档定位句及其查询效用；代码聚类后由主 Agent 写清晰 Scope。

## 4. 查询工作流：会话约束 → 图定位 → section → raw

### 4.1 `.scripts/query_orchestrate.py`

- **何时调用**：使用受控的 Query 编排流程，需要创建会话、将有限意图转换为动作白名单、执行动作并最终写查询记录。
- **写入**：会话状态和查询日志；不修改 raw。
- **子命令**：`init`、`make-plan`、`exec`、`finalize`。它的输入 JSON 契约和顺序由 `QUERY.md` 定义，先读规范，避免自行发明 action payload。
- **安全原则**：程序限制预算、重复和回环，但不替 LLM 判断证据充分性；最终答案仍需 read raw。
- **注册派生**（v2）：`_ACTION_SIGS` 从 `query_actions` 函数签名自动派生 action→参数清单，驱动 `_build_step`（输入组装）、`ALLOWED`（合法性白名单）、`allowed_next_actions`（下一步候选）、去重 set、`--action choices`。新增工具只需在 `query_actions.py` 加函数 + `DISPATCH` 注册，`_ACTION_SIGS` 自动适配，不再需要同步改 5 处硬编码。
- **不要**：直接自由编造长动作列表绕过 `intent_to_plan` 的白名单。

### 4.2 `.scripts/query_actions.py`

- **何时调用**：通常由 orchestrator 调用以执行已批准的检索/读取动作；不是面向手工随意调用的一级入口。
- **写入**：可能更新会话计量；具体取决于 orchestrator 上下文。
- **后续**：读取返回的 Evidence Profile、预算状态和允许的下一动作；不能把动作输出直接当最终答案。
- **Hub 路由工具**：`hub_route` 返回论文定位句→Scope 候选及 floor 决策（margin 已移除）；`hub_inspect` 返回 Scope、keyword/proposition/People 类型化成员和 split candidate。两者只读，不向弱 LLM 暴露 membership 写入、merge 或生命周期能力。`wiki_recall` 搜 title + Navigation + 论文方向定位，不读旧 Hub 关键词段。

### 4.3 `.scripts/query_graph.py`

- **何时调用**：在图中找入口、关系、邻居、Hub 或路径连通性。
- **写入**：否。
- **常用调用**：

```bash
python3 .scripts/query_graph.py search '<term>' --top-k 10
python3 .scripts/query_graph.py node academic/wiki/papers/<paper-id>
python3 .scripts/query_graph.py neighbors academic/wiki/papers/<paper-id> --depth 2
python3 .scripts/query_graph.py relations academic/wiki/papers/<paper-id> --predicate '引用'
python3 .scripts/query_graph.py hub_of academic/wiki/papers/<paper-id>
python3 .scripts/query_graph.py temporal --at 2026-08-23 --json
python3 .scripts/query_graph.py temporal --at 2026-08-23 --subject academic/wiki/authors/<name>
```

- **时态事实查询**：`temporal` 只读 `temporal_facts` 表，按 `valid_from`/`valid_until` 和 `superseded_by` 判定指定时点 effective；与 `edges` 分表保存，默认不进入 `neighbors`/`search` 的普通图导航。`--at` 必填 `YYYY-MM-DD`，`--subject`/`--object`/`--predicate` 可选过滤。
- **后续**：读取候选的 `Navigation`，沿边 `source` 和页面 `sources` 下钻 raw。
- **不要**：将搜索标题命中、邻接关系、Hub 成员关系或时态表快照直接写成事实结论；时态事实同样回 `source` 的 raw 核验。

### 4.4 查询结束后的反哺

若 query 通过联想发现图漏边，或图路径反复绕路而确知需要 shortcut，先在查询日志记录 index enrichment；再按 `QUERY.md` 将 `[推断]` 边增量写入图。未明确知道缺什么的完全失败，不应为了“完善图”随意补边。

查询会话 finalize 时自动运行轻量裸缩写消解（`ingest_common.lightweight_abbr_resolve`，2026-08-22）：只查图 alias 是否能消解 `abbreviation-todo.jsonl` 中的条目，不做 raw 扫描（与 ingest 后的全量消解区分）。消解结果附在 finalize JSON 的 `abbreviation_normalization` 字段。图归一化只改 alias 层，不修改 raw/wiki。

### 4.5 `dsh/` — DSH cockpit 适配层

- **何时调用**：需要受守卫的 agent loop（turn/step 循环、hook 瀑布、session log）时，以 `dsh/agent_loop.py:AgentLoop` 替代 `query_orchestrate._api_query_loop`。
- **架构**：`ToolRegistry`（`harness.py`）注册 `ToolDefinition`，hook 瀑布（pre-execute → execute → post-execute）执行工具，guard 包拦截行为。借鉴 DeepSeek Harness 的「Everything is a Plugin」理念，但用纯 Python 实现，不依赖 Cordis 运行时。
- **工具注册**：`dsh/tools.py:build_tools()` 返回 14 个功能工具。查询类工具除图、Wiki/Raw 读取外，包含只读 `node_resolve`、`semantic_search`、`hub_route` 与 `hub_inspect`；它们直接调用 `query_actions`，不向功能 Agent 暴露裸向量、merge、Hub 写操作或工程文件读取。CLI 类 `recall/remember` 仍经 `wg.py`。工具命名与 `query_actions.DISPATCH` 对齐。
- **guard 包**：`RepeatToolReminder`（重复调用提醒）、`TimeoutPolicy`（超时替换）、`CitationGuard`（引用核验，跟踪 `read_raw` 的 locator）。
- **摄入 seam**：`dsh/ingest_tools.py:build_ingest_tools()` 把 `ingest_inbox`/`ingest_paper`/`ingest_meeting`/`ingest_document`/`re_ingest` 包装为受守卫工具；实际 inbox 分发走 `IngestAgentLoop`，pre-execute 挂 `IngestGuard`。DSH 不重写底层状态机，只负责 guard + session log + subprocess 执行。
- **视觉 QA seam**：`dsh/visual_tools.py:build_visual_tools()` 只注册只读 `visual_check`，由独立 `VisualAgentLoop` 消费；显式“视觉检查/图片质检/PDF 页面排版/PPT 质检”意图在 `dsh/dispatch.py` 优先路由至该 loop。它不进入 `query_actions`，也不挂事实查询 `CitationGuard`；普通论文 PDF 摄入与作者/事实查询仍分别走 ingest/query loop。
- **视觉重建 seam**：`dsh/visual_reconstruction_tools.py:build_visual_reconstruction_tools()` 只注册写新产物的 `visual_to_editable_ppt`，由独立 `VisualReconstructionAgentLoop` 消费；仅“图片/PDF 转可编辑 PPT、复刻/对象化为 PPT”等明确转换意图路由。它不进入只读 `VisualAgentLoop`、`query_actions` 或摄入 loop，源文件与知识库保持只读。
- **结构化错误分类**（2026-08-22）：`_ingest_call` 失败时按 stderr/stdout 模式分类为 `api_timeout`/`extraction_failed`/`semantic_failed`/`graph_failed`/`unknown`，嵌入 `[ERROR category=<cat> script=<s> code=<n>]` 前缀。`IngestAgentLoop` 解析 category 决策后续动作。
- **瞬时失败重试**（2026-08-22）：`IngestAgentLoop._execute` 检测到 `category=api_timeout` 的错误自动重试一次（5 秒间隔），重试事件写 `ingest/retry` 到 session log。非 api_timeout 错误不重试，直接 handoff。
- **不要**：绕过各 capability seam 手工注册工具。事实查询工具在 `query_actions.py` + `dsh/tools.py:build_tools()` 对齐；摄入、视觉 QA、视觉重建分别以 `build_ingest_tools()`、`build_visual_tools()`、`build_visual_reconstruction_tools()` 为单一定义点，不得为复用 AgentLoop 而塞入 `query_actions`。

## 5. 健康、同步与审计

### 5.1 `.scripts/graph_metrics.py`

- **何时调用**：Lint、Sync 或建设后检查图连通性、孤岛、紧密簇和谓词分布。
- **写入**：默认只读；`--apply-state` 更新紧密簇比较状态。
- **最小调用**：

```bash
python3 .scripts/graph_metrics.py connectivity
python3 .scripts/graph_metrics.py predicates
python3 .scripts/graph_metrics.py tight_clusters
```

- **后续**：把孤岛视为需诊断的缺口；把簇视为建议，不自动修复。

### 5.2 `.scripts/graph_dump.py`

- **何时调用**：人工/LLM 审计图节点、别名、边和来源。
- **写入**：默认只读；`--jsonl <file>` 生成审计快照。
- **最小调用**：

```bash
python3 .scripts/graph_dump.py --node academic/wiki/papers/<paper-id>
python3 .scripts/graph_dump.py
```

- **后续**：发现的错误按根因修复页面/摄入流程，不要手工编辑 SQLite 文件。

### 5.3 `.scripts/check_wikilinks.py` 与来源审计

- **何时调用**：排查悬空 wikilink、来源定位器或迁移影响时。
- **写入**：通常否。
- **注意**：部分脚本将第一个位置参数解释为文件，不能用 `--help`。从对应 `LINT.md`/`SYNC.md` 取当前调用方式。

### 5.4 `.scripts/rebuild_triples.py` 是兼容/派生工具

- **何时调用**：仅当前规范明确需要 Markdown triples 派生快照、迁移或历史兼容时。
- **当前边界**：它不是当前向 `graph.db` 写语义边的主入口；日常关系写入用 `graph_ingest.py`。
- **不要**：把它当作摄入后“必须重建图”的固定步骤，或手工编辑 `triples*.md` 代替图数据。

### 5.5 `.scripts/graph_validate.py` — 只读图约束校验

- **何时调用**：建设任务落地后、Lint/Sync 前置检查，或需要审计 `graph.db` 的节点类型、实体子类型、置信度、重复边与证据完整性时。
- **写入**：纯只读，不修改 `graph.db`、页面或 raw；错误只做审计报告和退出码，不做自动修复。
- **配置**：约束来自 `operations/config/graph-schema.yaml`；默认与 `graph_validate.py` 内置默认一致。`ingest_check.py` 仍负责页面 frontmatter 与 sections，本工具只负责图侧结构。
- **最小调用**：

```bash
python3 .scripts/graph_validate.py
python3 .scripts/graph_validate.py --details
python3 .scripts/graph_validate.py --db private/graph.db --json
```

- **退出码**：`ERROR` 返回 1，只有 `WARN` 返回 0；若出现 legacy 置信度、重复边或可追溯边缺 `edge_evidence`，用 `graph_repair.py --dry-run` 查看确定性修复计划，获授权后 `--apply`，不手工编辑 SQLite。

### 5.6 `.scripts/visual_qa.py` — 视觉产物逐页 QA

- **何时调用**：用户显式要求视觉检查时调用；用户要求修改图片、论文/文档 PDF 页面或 PPT/PPTX 静态页面，且 Agent 需要先理解布局、位置、颜色、字号、间距、遮挡、裁切、比例、图例或页面流等可见状态时也调用。第二类调用用于建立修改前视觉上下文，不代表每轮修改后都做全量 QA。常规文字修改、编译和每轮交付不自动调用；不用于判断科学数据和结论真伪。
- **适配器**：图片直接规范化；PDF 用 PyMuPDF 逐页渲染；PPT/PPTX 用 `soffice` 转 PDF 后逐页渲染。动画、视频、备注和切换效果不在范围内。
- **检查层**：始终运行分辨率、空白页、极端纵横比、疑似裁切等确定性检查；非 `--deterministic-only` 时调用 `GLM-4.6V`，失败回退 `GLM-4.5V`。API 未配置、非法 JSON、超时或隐私阻断必须返回 `partial/not_checked`，不得当作 pass。
- **配置**：自动读取仓库根 `.env` 并展开 `${LLM_API_BASE}`/`${LLM_API_KEY}` 引用；进程环境优先。推荐 `VISUAL_QA_API_BASE=${LLM_API_BASE}`、`VISUAL_QA_API_KEY=${LLM_API_KEY}`。密钥和图片 data URL 不进入 receipt 或 DSH log。
- **隐私**：`raw/`、`inbox/`、`private/`、`sources/`、`source-local/` 与 `profile=paper` 的论文/稿件 PDF 默认禁止远程上传；确认授权后才传 `--allow-remote`。路径保护优先于 profile。
- **断点续做**：输出到 `temp/visual-qa/<artifact-sha>/<profile>/<check-key>/`；逐页 `complete` receipt 在输入、模型、prompt/schema、context 与渲染配置哈希一致时跳过，partial 页重试。仓库内自定义 `--receipt-root` 只能位于 `temp/`。
- **规范**：完整参数、结果语义与 DSH 自动调用见 `operations/VISUAL_QA.md`。
- **最小验证**：

```bash
python3 .scripts/test_visual_qa.py
python3 dsh/test_visual_tools.py
python3 .scripts/engineering_graph.py validate
```

### 5.7 `.scripts/visual_to_editable_ppt.py` — 图片/PDF 原生对象化

- **何时调用**：用户明确要求把图片、扫描页或 PDF 转成/复刻为可编辑 PPT；普通视觉检查、PDF 摄入、论文阅读和事实查询不得触发。
- **分流**：矢量 PDF 直接提取 text/path/image；图片与 image-only PDF 使用 Tesseract OCR、线段和色块/轮廓检测。无法可靠对象化的像素只进入透明 residual fallback，并使结果为 `partial`，不得报告完全可编辑。
- **对象**：文字→文本框，直线/箭头→connector，矩形/椭圆→原生 auto shape，Bezier→PowerPoint custom geometry；嵌入位图和 residual 都在 object manifest 中记录原因。
- **模式**：`faithful` 保守、`balanced` 默认、`editable` 激进且不保留 residual。源文件只读，输出已存在默认失败；`--overwrite` 只原子替换显式目标，输出不得落入 raw/wiki/inbox/private/graph 区域。
- **断点续做**：`temp/visual-to-ppt/<source-sha>/<run-key>/` 保存逐页 render、objects、receipt 与 final；run-key 绑定源、脚本、模式、DPI、OCR、模型和隐私参数。
- **模型/隐私**：普通非敏感页面可在 API 已配置时用 `GLM-4.6V`/`GLM-4.5V` 补高置信度缺失对象；敏感路径与 paper PDF 仍须显式 `--allow-remote`。模型输出经类型、坐标、置信度、重叠和数量校验，不能提供命令、路径或 XML。
- **注册**：`visual_to_editable_ppt` 只进入 `VisualReconstructionAgentLoop`；`dsh.dispatch_loop()` 仅对“图片/PDF 转可编辑 PPT、复刻/对象化为 PPT”等显式意图路由。详细契约见 `operations/VISUAL_TO_EDITABLE_PPT.md`。
- **最小验证**：

```bash
python3 .scripts/test_visual_to_editable_ppt.py
python3 dsh/test_visual_reconstruction_tools.py
python3 .scripts/test_visual_qa.py
python3 dsh/test_visual_tools.py
python3 .scripts/engineering_graph.py validate
```

## 6. 建设与迁移脚本

以下工具多为建设、审计、实验或一次性修复用途，不是一般知识操作入口：

- `migrate_section.py`、`migrate_source_locators.py`、`normalize_core_triples_source.py`、`fix_source_type.py`：只在迁移方案明确、范围已限定时调用；先备份/检查 diff，再分批处理。
- `graph_repair.py`：graph.db 确定性存量修复器。负责把旧 `[可追溯]`/`medium` 归一为 `可追溯`/`推断`，从端点页面 `sources` 确定性回填缺失 `edge_evidence`，无来源时降级为 `推断`，并合并完全重复的语义边。默认 `--dry-run`；真实 `graph.db` 修改必须显式 `--apply`，且不改 raw/wiki。
- `person_entity_audit.py`、`scan_authors.py`、`audit_source_locators.py`：诊断和报告优先；`scan_authors.py` 与页面骨架共用作者解析，适合摄入论文后复核作者截断；问题修复仍依证据与规范。
- `embed_init.py`、`embed_helper.py`：embedding 缓存基础设施（配置见 §2.2「Embedding API」）；不要在普通任务中随意强制重算。
- `ingest_pipeline.py`：摄入流程共享驱动器（`run_pipeline`）。三编排器（`ingest_paper/meeting/document.py`）的 9 步调度循环、状态机、修复循环、resume 安全网由此统一托管；各编排器只声明 spec（`script_name`/`preprocess_label`/`repair_fail_strategy`/`cleanup_after`/`steps` 等）+ provider step 函数，差异点全部参数化。新增文档类型只需写 spec，不再复制 950 行编排器骨架。
+- **P0 摄入遥测与完成判定**（`ingest_common.py`/`ingest_pipeline.py`/`inbox_state.py`，2026-08-24）：`inbox_state.save` 记录 `telemetry.source_hash`（跨重试关联同一来源）与状态/重试时间线 `events`；`ingest_common.run_tracked` 记录子进程 `returncode`/`duration_ms`，`record_llm_call` 按 stage 累计 LLM 调用次数；`step_update_graph` 在 graph_ingest 输出非 JSON 或缺 `edges_added` 时返回失败（不再静默继续）；`validate_completion` 在标记 `completed` 前检查历史错误已清空、语义槽非空、graph_report 已解析且非空跑。
- `wg.py`：横向能力面统一入口（跨功能、回合中可调）。薄包 `query_graph.py`/`read_section.sh`/`research_memory.py`/`frontier.py`/`query_actions.py`/`source_locator.py`，输出统一 JSON envelope（`{ok,action,result,sources,status,error}`）。能力：`lookup`/`neighbors`/`relations`/`hub-of`/`read-section`/`read-raw`/`recall`/`remember`/`abbr`/`frontier`。供写作/研究中 agent 回合内组合调用，不必"切换任务"；消费侧规范（`physics-manuscript-editing.md`、`RESEARCH.md`）已列「可用能力调用」节。
- `frontier.py`：研究前沿 overlay 管理器。Markdown 主数据位于 `academic/frontier/{questions,trajectories}/`，一个开放问题由同一 Question Page 贯穿 captured→triaged→active→resolved；旧 intake/thread 只读兼容并由 `migrate-questions` 收敛。`frontier.db` 仅为可重建 FTS/稀疏导航索引。`capture-paper` 限量、幂等捕获作者明示问题，精确复用后用紧凑 Graph→Wiki→Raw 证据包非阻断尝试库内回答；支持结论须引用包内 Raw locator，失败只留 pending。`review active` 仍强制人审；embedding 不自动合并问题。不得修改 Raw/Wiki/事实 graph.db。
- `ingest_paper.py`：inbox 学术论文 PDF 的**代码驱动端到端摄入编排器**（见 §2.1c）。LLM 前用 PyMuPDF metadata+第一页页脚确定性提取书目（published 优先、APS DOI 补 venue）并双重回填；3.3a/3.3b 长文本用 `call_text`，3.6c 仅登记完整命题（零 LLM），稀疏概念链接交 graph writer。全论文 quiet batch 的 prepare 上限2并发，graph commit 串行。结构性语义错误早停为 `agent_required`；事实摄入完成后非阻断调用 Frontier 明示问题捕获（失败只 warning）；默认 quiet，stdout 只输出最终 JSON。仅 inbox 学术 PDF 适用。
- `predicate_governance.py`：候选谓词自动治理器；读取 `.scripts/predicate-governance.yaml`，将 `cross-domain/predicate-candidates.jsonl` 归一、聚合并输出 `cross-domain/predicate-governance.json` 与 `.scripts/predicate-registry.json`。只有达到正式阈值的谓词才扩展摄入允许集；不自动创建反向关系或方向 tier。
- `ingest_document.py`：inbox 通用文档（行政/教学/商业）的**代码驱动端到端摄入编排器**（见 §1.1d）。`--subproject` 指定域，`DOMAIN_CONFIG` 驱动域差异（页面类型/谓词/代词）。流程同 `ingest_paper.py` 但用 textutil/pandoc 提取文本。`ingest_admin.py` 是其薄包装。
- `re_ingest.py`：管道版本升级后重新摄入已入库论文（见 §2.11）。raw 不动，重生 wiki + `graph_ingest.py --clean` 清旧边重建。`--outdated` 按节点 `ingest_version` 筛选落后论文；`--manifest` 全量；`--raw` 单篇。旧 wiki 备份至 `temp/inbox-state/`，不重跑 MinerU。
- `research_memory.py`：研究项目级结构化记忆工具。绑定 `projects/<name>/.research-memory/`，独立于 ingest（研究内容不入库）。`recall` 恢复上下文（profile + 近期条目 + status.md）；`add` 新增记忆（intent: decision/insight/literature_judgment/research_direction）；`profile --refresh` LLM 提取研究画像。研究工作流规范见 `operations/RESEARCH.md`。
- `inbox_ingest.py`：PDF inbox 的可恢复事务入口（`ingest_paper.py` 未覆盖的批量/非学术场景）。按 `plan → 每项 prepare → complete` 调用；同类批量已准备项用 `complete-batch --transaction-id <id> ...`，逐项完成落位、API 受控语义、写图、`ingest_check --graph` 和源文件清理。不要手工删除 inbox 原件或跨项复用临时目录。
- `inbox_state.py`、`inbox_finalize.py`：前者保存单项事务状态，后者以 manifest 原子复制 raw/wiki 并返回回执。状态中的 `receipt` 指向具体 `temp/inbox-receipts/` JSON；回执用于恢复、哈希审计和落位证明，不是知识来源。
- `api_ingest.py`：API 关键词输出必须是白名单 `term` 与单一 `evidence_id` 的配对；只有程序验证后的术语才进入 `--semantic-output` 并可供 `graph_ingest.py ingest --semantic` 使用。
- `e1_experiment.py`：ForBetterScience E1 的隔离实验 harness。Phase L 的 `run-lock.json` 永久绑定 frozen manifest、fresh source-local bundles 与 LLM 产物；Phase G 必须另用 `freeze-fusion-lock` 绑定 Phase-L lock/audits/全部 bundle 哈希、graph/Hub code、embedding、阈值、隔离路径与 Hub Agent gate。正式 `init-graph`/`fuse-graph` 默认使用 `runs/<phase-l-run-lock-prefix>/` 下的 graph DB、fusion ledger、embedding cache、derived pages、gate inputs 和 snapshots；`--shakedown` 保留旧实验根。Hub 候选缺完整 hash-bound gate 时返回 `agent_required`，不得自动创建；显式 reject 仅在 step-independent candidate signature（完整成员与候选结构，score 三位小数归一）相同的后续步骤确定性复用，结构一变重新 gate。Phase-G lock 变化从 G0 重启但不重跑 Phase L；`promote-main` 仍未实现，实验运行不得写生产 Wiki/graph。
- `projects/ForBetterScience/experiments/e1-chronological/analyze.py`：E1 的只读派生分析器。`--experiment-root` 可选择任一冻结 E1 工作区；入口先复验 fusion-lock、连续 ledger、逐步回执与 `G0..GN` 快照，再生成 trajectory、Hub lineage、密度异常样本直接贡献敏感性和图件。敏感性论文以稳定 `entry_id` 定位，`DNNN` 标签按当前 run 的 step 动态生成，派生文件使用描述性名称，避免语料去重或年份校正后沿用失效编号。分析代码独立于 Phase-G 锁，不改 raw、生产 Wiki/graph、bundle、ledger 或 snapshot。
- `baseline_*.py`、`query_ablation.py`、`answer_judge.py`、`metrics.py`：论文实验工具，只按 `projects/kr-wiki-paper` 的实验设计和冻结配置运行。
- `test_ingest_paper.py`、`test_predicate_governance.py`、`test_ingest_document.py`、`test_ingest_admin.py`、`test_inbox_ingest.py`、`test_inbox_finalize.py`、`test_api_ingest.py`、`test_ingest_check.py`、`test_ingest_pipeline.py`、`test_wg.py`、`test_prompt_audit.py`：改动 inbox 事务、候选谓词治理、API 证据、摄入检查、作者提取、会议骨架/关键词、语义槽/方向配置或路由规则时，由 `engineering_graph.py impact <节点> --verify` 选择最小集合。`test_ingest_pipeline.py` 在临时仓库真实重放论文与会议的写图和图校验；不替代真实内容的局部检查。

建设任务落地后，要检查 `engineering-handbook.md` 与本调用指南是否受影响；如需调整，在交付时提示用户更新，未经明确授权不得自动修改。`projects/知识结构涌现/idea-deltas.md` 仅按用户明确指令更新。

## 7. 最小任务配方

### 从 inbox 摄入一篇学术论文 PDF

```text
python3 .scripts/ingest_paper.py --pdf inbox/<file>.pdf
→ 程序端到端完成去重/提取/撰写/校验/落位/写图/收尾
→ 读 stdout 最终 JSON（paper-id/raw/wiki 路径/边数）
→ 仅失败时展开进度日志排查；INGEST_BACKEND=agent 时 3.3 会输出 agent_required 交接包
```

非 inbox 来源（已在 raw 归档）的论文走下方 stage 流程。

### 新增一篇论文

```text
route ingest(stage 1)
→ 归档 PDF + extractor
→ wiki_skeleton + (api 模式时 api_ingest 证据卡草稿；否则按当前后端编码)
→ route ingest(stage 2)
→ extract_citations (如适用) + graph_ingest ingest
→ 方向匹配/Hub 由正常图摄入链处理
→ route ingest(stage 3)
→ ingest_check (ERROR 清零) + ingest_build（按规范）+ log
```

增量版本在 stage 1 前增加：

```text
ingest_plan（只读变化计划）
→ 仅处理 affected 范围
→ 正常 stage 1→2→3
→ 成功后才 write-state
```

### 回答一个事实问题

```text
route query
→ query_orchestrate（如走编排）/ query_graph search
→ read_section Navigation
→ graph neighbors/hub_of（需要关系时）
→ read_section Content
→ 沿 sources/edge source 读 raw
→ Evidence Profile 审核、回答、日志；必要时受控反哺
```

### 在研究项目下做研究

```text
route research
→ python3 .scripts/research_memory.py recall <project>
→ 读材料 / 写笔记 / 写论文文稿 / 做判断
→ 遇决策/发现/文献判断/方向调整时自动 research_memory.py add
→ 产出限于 projects/<name>/notes/ 或 manuscript/，不自动入库
```

### 重新摄入已入库论文（管道版本升级后）

```text
python3 .scripts/re_ingest.py --outdated --dry-run  # 预览落后清单
python3 .scripts/re_ingest.py --outdated            # 仅 re-ingest 落后版本
→ raw 不动，重生 wiki + 清旧图边重建 + catalog + log
```

### 评估定位句候选是否应建 Hub

```text
route hub
→ 收集可 locate 的文档定位句与现有 Scope 路由失配候选
→ 代码聚类并返回代表成员/locator/route probes
→ 主 Agent 确认 title、Scope、parent 与新旧 Hub 关系
→ 只通过 hub_semantics.py --agent-confirmed 写入
→ 检查路由成功率、margin 和代表查询
```

### 检查视觉产物

```text
python3 .scripts/visual_qa.py <figure.png> --deterministic-only
python3 .scripts/visual_qa.py <paper.pdf> --pages 1,3-5 --profile paper --deterministic-only
python3 .scripts/visual_qa.py <slides.pptx> --profile slides --deterministic-only
→ 查看 summary.json 与逐页 receipt
→ 需要视觉模型时去掉 --deterministic-only
→ raw/inbox/private 或 paper-profile PDF 远程检查前，确认授权并显式加 --allow-remote
→ fail 必须修复；warn/not_checked/partial 进入人工复核，不自动修改原产物
```

### 转换为可编辑 PowerPoint

```text
python3 .scripts/visual_to_editable_ppt.py <figure.png|document.pdf> \
  --output projects/<project>/figures/<name>-editable.pptx \
  --mode balanced --deterministic-only
→ 查看 summary.json 的 fully_editable、fallback_objects、editable_foreground_coverage_mean
→ 矢量 PDF 应优先得到零图片原生对象；位图 residual 使结果为 partial
→ 需要视觉模型辅助时去掉 --deterministic-only；敏感/论文页面另加 --allow-remote
```

## 6. 开源发布：`.scripts/open_source_release.py`

- **定位**：从个人工作库白名单构建无个人数据的公开模板，并验证发布树不含未批准文件；公开 `graph.yaml` 按目标树实际存在的节点确定性投影，不携带私有项目节点。
- **先读**：`operations/engineering/open-source-release.md` 与 `operations/engineering/open-source-manifest.yaml`。
- **调用**：`python3 .scripts/open_source_release.py build <目标目录> --clean --force`；随后 `python3 .scripts/open_source_release.py verify <目标目录>`。
- **边界**：只复制 manifest 批准文件和公开资产；不读取或复制业务知识内容。DSH 作为公开执行层随 `dsh/**` 发布；active `projects/` 与 `.project/` 节点从公开工程图移除。目标非空时必须同时显式给出 `--clean --force`；脚本保留目标的 `.git` 元数据。
- **验证**：`python3 .scripts/test_open_source_release.py`；发布前再运行 `verify`，并在生成树内运行 `python3 .scripts/engineering_graph.py validate` 与相关工程回归。

## 7. 论文数据产物：`.scripts/paper_artifact.py`

- **定位**：把冻结的隔离实验导出为与论文/代码版本绑定的可审计 Wiki、Graph 和数值数据包；Raw、数据库、向量与 API 记录不进入产物。
- **构建**：在私有工作库运行 `python3 .scripts/paper_artifact.py build`；构建只读冻结实验输入，重建 `paper-artifacts/v0.2.0/` 中的生成子目录和校验和，不改实验原件。
- **清理**：Wiki 的本地 source locator 和 Graph provenance 路径统一改写为 `raw-not-distributed/...`；Graph 中的 Raw node ID 仅作为拓扑标识保留。
- **版本**：paper artifact `1.0.0` 独立绑定 Ran-ASKS `v0.2.0`。首次公开 tag/Release 后原目录不可原地更新；数据修正必须建立新 artifact version。`config/code-compatibility.json` 对照冻结运行与发布代码哈希，任何 post-run drift 都必须在 `CODE_PROVENANCE.md` 中明确披露。
- **验证**：`python3 .scripts/paper_artifact.py verify paper-artifacts/v0.2.0` 与 `python3 .scripts/test_paper_artifact.py`；随后仍须走完整开源发布 build/verify。
