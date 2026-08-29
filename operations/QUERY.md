> **使用约束**:同一 action key 不得重复执行;连续两轮未产生新证据时停止或换路径;答案必下钻 raw(图边是定位器非答案)。

# Query（查询）操作规范


---

## 会话级总计划（条件触发）

总计划仅协调一次查询会话的检索范围，不替代 query 的 `start → evidence → continue → answer` 派发、Evidence Profile、槽位审计或停止判据。问题包含多个子问题、跨源综合、比较/关系解释，或预期需要多轮检索时，在 `query-stage start` 路由后、首轮定位前建立 3–5 项抽象阶段及停止条件；单一事实查询直接执行 `start`。

- 允许的抽象阶段：拆解范围与答案槽位 → 定位候选 → 按缺口取证 → 审计、综合与交付。不得写脚本、检索词、页名、证据卡、预算细节或各阶段任务卡已有规则。
- 计划不预判证据充分性、不得绕过三审停止，也不把 `continue` 变成必经环节；每次是否续查仍由 Evidence Profile、槽位缺口和停止协议决定。
- Agent 可维护计划和阶段状态；API 中 API 仅处理当前派发阶段的最小受控上下文。总计划由编排层确定性提供，不作为 API 的独立推理或持久化输出。

---

## 轻量检索策略（start 前置）

在 `query-stage start` 后、首轮定位前，先判断查询是否已有明确固定策略。若没有，必须先制定 3–5 项轻量检索策略；若属于直达题或已命中高频场景前置路由，可跳过，但须在查询 trace 中记录跳过理由。

- 触发信号：实体或名称可能有多写法/别名/译名、题目含多个可交叉线索、目标来源不明确、预计需要跨域或多轮检索、固定谓词路由未覆盖。
- 策略内容：答案槽位 → 线索矩阵（实体/别名/主题/时间/来源类型）→ 候选定位顺序（库内结构化召回优先，再按索引与图扩展，最后才外部联想）→ 同名消歧与停止条件。
- 执行约束：策略只约束检索范围和顺序，不预判证据充分性、不替代 `start → evidence → continue → answer`、Evidence Profile、槽位审计或 raw 回溯；发现新缺口时按具体缺口修正策略。
- API 首轮选择 `discover/read` 时必须返回 `strategy`；`status=drafted` 时 `slots/clues/search_order/stop_conditions` 均须非空，`status=skipped` 时须给出 `reason`。

---

## 触发方式

用户对知识库提出问题。

## 检索策略

**结构优先（v9）**：query 不让 LLM 从全库自由遍历。先由 `.scripts/query_graph.py`、索引和 `query_orchestrate.py` 完成意图允许的候选定位、去重和预算控制，再由 LLM 阅读最小候选上下文、做语义综合和证据充分性判断；事实答案仍须下钻 raw。ingest 的“先结构、后语义”是生成顺序，query 的对应形式是“先结构化检索、后语义阅读”。

**行政文档自动回退（v6）**：行政问题可先执行编排动作 `admin_recall(query, topk)`。程序先用标题、别名和页面 `Navigation` 做低成本直接召回；直接无候选时自动沿图中主题/事项/版本/人物/部门边扩展。该动作只返回候选页面的 `Navigation`，不读取 `Content`，由 LLM 筛选后再定向读取正文和 raw。直接召回不足（只有弱相关候选）时，LLM 应继续用 `graph_search`/`graph_neighbors` 或 `admin_recall`，并在续检索中说明缺口与预期收益。当前页面规模下的 Navigation 扫描是轻量兜底；规模扩大后应将其改为持久化倒排索引，避免每次遍历页面。

**跨域统一召回（v7）**：新动作 `wiki_recall(query, domain, topk)` 复用上述流程；`domain` 可为 `academic/admin/teaching/business`，为空表示跨域。`admin_recall` 保留为兼容入口。business 结果标记 `sensitive_review=true`，回答前必须按权限/敏感性复核，不能因召回命中直接扩散合同、财务或个人信息。

**Hub Scope 召回**：`wiki_recall` 匹配 page 标题、Navigation 和论文 `## 研究方向定位`，并与图回退结果合并。需要检查论文→Hub 路由时调用只读 `hub_route`；需要审视 Hub Scope、parent 和成员时调用 `hub_inspect`。查询不再读旧 Hub `## 关键词`。

**弱模型保护（v8）**：LLM 只提交有限意图 JSON（`need_recall/need_graph/query/domain/topk`），不得自由编写动作列表；由 `query_orchestrate.py intent_to_plan` 生成白名单动作。意图缺字段或非法值时不执行；召回候选仍须先读 Navigation，再由程序/LLM决定是否读 Content/raw。

查询按以下优先级依次检索，命中即止：

> **图导航 v5**:检索主路径是 SQLite 图查询(`.scripts/query_graph.py`)。图中每个知识库文件都有代表节点：原件与同 stem locator companion 共用一个 Raw 文档包节点，Wiki page 是独立节点并通过 `来源` 边直连 Raw；人物、概念、Hub 等其他节点主要与 Wiki 相连。关键词/别名/标题用于入口定位，作者、引用、发表、方法、研究方向、参会等关系边用于纵向导航。原 keyword/triples/page-catalog 索引的功能已并入图，不再作为独立主数据源。各层映射:
> - 第一层(图搜索入口):图 `search <term>` 子命令(匹配 nodes 的 title/aliases/abbreviations 等定位字段)
> - 第二层(关系):图 `neighbors <node> --depth 2` / `relations <node> --predicate P` 子命令(关联召回 BFS,沿 edges,按 confidence 排序)
> - Hub 召回:图 `hub_of <page>` 子命令(沿语义谓词边反向,结构性关联,不靠关键词匹配)
> - 第三层(子项目 index):`rg --files` 列目录(局部兜底,图盲区)
>
> **private 物理隔离（v1，2026-08-04）**：私人知识库（健康/玄学）用独立 `private/graph.db`，不在主库聚合清单。查私人数据须显式指定：`query_graph.py search <term> --db private/graph.db`；主库查询（默认 `cross-domain/graph.db`）不覆盖 private，反之亦然。resolve/neighbors 均在各自库内，不跨库。
>
> **硬约束(图只导航，Wiki 桥接 Raw)**:Graph 先定位相关 Wiki page；LLM 读取相关 Wiki section，再沿该节 `raw_citations`/脚注精确读取 Raw。若边填写了 locator，可直接用它缩短路径；边没有 locator 是合法状态，不触发错误或警告。`来源` 边用于取得对应 Raw 文档包。图只回答“有关联什么”，事实答案仍须回溯 Raw。
>
> **第四层联想触发条件改"图查询无命中"**(图盲区突破层),边界不变(只定位不回答,网络≠raw)。
>
> **反哺**:query 只识别并记录知识缺口，不直接写 wiki 或 graph.db；经用户授权后，另起 `ingest update` 由既有校验流程编译入库。详见下方「反哺（缺口记录 → ingest update）」段。

## 全局规则

**deprecated 过滤**:所有层检索结果默认过滤 `status: deprecated` 页面(遗忘策略落地后的对接口)。例外:用户明确询问历史版本/演进过程时,可读取 deprecated 页面,并配合 `superseded_by` 跳转现行版。

**token 预算提示(程序预估+提示,LLM 自主决定)**:编排层 `query_orchestrate.py` 实计每次读取 token 并累计。当 `token_used > token_budget × 0.7`(budget_warned)时,程序在返回结果里附 `budget_hint`:
- 读 `Content` 时提示:后续候选页建议先读 `Navigation`(~100 tok)判相关再决定是否读 Content,或用锚点 `[[page#slug]]` 读子段
- 程序**提示不强制**——"某 section 是否必须全文读"是语义判断,归 LLM;程序只供给预算状态
- 预算耗尽(`token_used > token_budget`)则硬停(stop_reason=budget_exhausted),`allowed_next_actions` 收窄为 `[answer]`

原则:降级为摘要/锚点不是放弃层——准确性优先于省 token,仅在预算压力下用摘要替代全文。

**上下文复用(v6,2026-07-27)**:同一 query 内已读 raw/wiki 缓存在会话上下文,后续命中同一文件**不重读**(省 token + 减注意力干扰)。预算翻倍场景的已读页不重读原则推广到日常多跳。复用仅限 in-context;若会话过长早期文件被淘汰,重读合法(非 bug)。

## section 读取(省 token,2026-07-19 新增)

> 候选页确定后,用 `.scripts/read_section.sh <page> <section>` 按 section 截取,而非整文件读取——只把截取段计入 LLM 上下文 token。物理共置(导航与正文同文件)+ 逻辑分离(按段截取),见各 SCHEMA「标准 section 结构」。

**标准两段**(v4,2026-07-25):`## Navigation`(导航概述 80-200 tokens)→ `## Content`(正文)。关系路由走 graph.db 图查询(不再有 md Core Triples 段)。

**读取顺序**:
1. 候选页先读 `Navigation`(导航段,~100 tokens),判断是否相关;涉及关系时查 graph.db neighbors
2. 相关 → 读 `Content`(正文)或按锚点 `[[page#slug]]` 读子段
3. 需核验 → 沿 frontmatter `sources` 下钻 raw
4. 核验证据状态时,需 `[[page#slug]]` 锚点的 → 读对应 section 或 raw

**Raw Locator（2026-08-25）**：统一使用 `python3 .scripts/wg.py read-raw '<path>#<locator>'` 做局部读取。Markdown/TXT 支持标题、`#L12`、`#L12-L18`；非论文且有文本层的 PDF 支持 `#page-3`、`#page-3-5`。工具可以机械扫描文件，但只把 locator 命中的片段送入 LLM；裸路径与 `#全篇` 均拒绝执行，Graph 中的 `#全篇` 仅是文件级 provenance，读取前必须细化 locator。命中片段超过单次上限时也拒绝返回半截内容，调用方须缩小章节、行范围或页码。原文件没有稳定可读 locator 时，沿 wiki `sources` 读取同目录 Markdown companion；不得因此把整份二进制文档送入模型。学术论文始终读取 MinerU `paper.md`，不得因 PDF 有文本层而绕过它。

**Wiki Locator（2026-08-25）**：Wiki 是可重建的阅读导航层，使用稳定 heading slug，不使用 Wiki 行号。调用 `python3 .scripts/wg.py read-section '<wiki-page>.md#<heading-slug>'` 时，程序只返回目标 heading 到下一个同级/更高标题之间的正文，并解析该节实际使用的 `[^rN]` 脚注，返回 `raw_citations`。回答事实时再把这些 Raw locators 交给 `read-raw`；不得因读取 Wiki section 而自动读取整页或整份 Raw。

**防退化**(必守,见 SCHEMA):
- section 名精确匹配(`Navigation`/`Content`),无附加文字
- `read_section.sh` 找不到 section → 非零退出 + 列可用,不静默降级全文
- 过渡期旧页无标准 section 时,脚本报错;agent 显式选择整文件读并知会用户"该页未迁移"(非静默 fallback)

**token 账**(体量大时):候选 50 页 × 正文 3k = 150k;先读导航 50×0.1k=5k + 命中 5 页×3k=15k = 20k,省 ~85%。体量小+候选明确时收益小,可整文件读。

**图数据与检索分工**(v5,2026-08-25):`graph.db` 是节点与关系边的主数据源。`search` 只负责图搜索入口，`neighbors/relations/hub_of` 负责关系导航；关键词是召回信号，不等于图的全部内容。最终事实从相关 Wiki section 的 Raw 脚注回溯；边 locator 若存在可作为捷径，但不要求存在。

摄入报告里的 `graph_delta.query_probes` 只验证新文档 overlay 是否容易被 query 使用，不是事实证据，也不应在回答时引用。回答仍须沿 Wiki section 的 Raw locator 下钻到 Raw 片段。

**节点语义工具。** 明确名称、缩写或别名先调用 `node_resolve(name,context)`，其内部按 path/alias→label embedding→semantic identity gate 返回 `resolved/ambiguous/unmatched`，不会写图。用户描述概念但不知道名称时调用 `semantic_search(query,scope)`；其结果只能作为相关候选，不能据此认定节点相同。定位 canonical node ID 后再走 `neighbors/relations/hub_of`，最终事实仍下钻 Raw。Agent 不调用裸 embedding 或自行解释阈值；embedding 不可用时工具降级到确定性名称和 lexical search。

## 第零层：高频场景前置路由

对以下高频问题类型，跳过索引直接读取对应页面：

| 问题类型 | 识别信号 | 直接读取 |
|----------|---------|----------|
| 实体物件位置 | "放在哪""存放位置""签字表在哪""原件在哪""公章在哪""文件在哪" | `admin/wiki/procedures/重要物品存放位置.md` |
| 办事流程/制度 | "怎么办""流程""审批""三重一大""议事规则" | `admin/wiki/procedures/` 下对应页面 |

判断方法：用户问的是**实体物品的物理位置**还是**电子文档内容**？
- 提到"签字表""原件""公章""证照""合同原件""放在""存放"等 → 实体物件，走前置路由
- 提到"文件内容""方案""报告写了什么" → 电子文档，走正常索引检索

## 第一层：图搜索入口（search）

> 图 search 是入口定位，不是完整答案。它匹配 nodes 的 title、aliases、abbreviations 等字段；主题关键词和实体名称都可作为搜索词。

用图查询定位节点或页面（`query_graph.py search <term>`）：

- 关键词命中 → 直接读取匹配到的 wiki 页面
- 如果涉及高频主题（青梧/班子会/量子信息/十五五等）→ 读取对应 Hub 页面获取聚合视图（Hub 召回走图 `hub_of <page>`，结构性关联）
- **缩写回查**：缩写已并入 aliases 表（v4），图 search 覆盖；若 search 无命中且查询词像缩写，联想层兜底
- 如果未命中 → 进入第二层

## 第二层：关系检索（图 neighbors/relations）

> graph.db 是关系边唯一源(不再从 md Core Triples 段派生),ingest 增量加边。关系边是导航定位器，不是最终事实答案。

当问题涉及实体间关系时（如"哪些论文用了某方法""某作者与谁合作""某方法属于哪个领域"），用图查询（`query_graph.py`）：

- 关联召回 BFS：`neighbors <node> --depth 2`（沿 edges 遍历,按 confidence 排序,可 `--top-k` 限预算）
- 按谓词过滤：`relations <node> --predicate "提出方法"`（查特定关系类型,predicate 为开放集）
- **命中 hub 节点**：沿 hub↔page 边拿成员清单（不读 hub 正文；见 idea-deltas 4.5/4.7）。成员数 > cutoff=50【初始工程参数】 时走概率采样（见下）

**三层筛（v3,2026-07-25,候选管理；2026-07-27 修订：确定性优先）**：BFS 后候选分层削减,避免对全量采样浪费

> **确定性优先原则**：首轮走确定性 top-k（按 confidence + 度数排序），结果可复现；证据不足时才启动概率采样（embedding softmax 温度）。概率采样记录每次命中/漏掉原因（采样日志），便于调试漏召回。真实查询数据积累后,再调温度和采样策略。
- **层1 · 谓词硬过滤（程序,机械,近零成本）**：按 query 类型从预定义谓词集表选相关谓词,程序 `relations --predicate` 过滤
  - 谓词集表（预定义在 QUERY.md,程序读）：合作类=[合作者/作者/通讯作者/指导/受指导于/成员]；方法类=[提出方法/对比方法/采用方法/提出度量]；领域类=[属于领域/应用于/基于]；机构类=[任职于/负责]
  - **前缀匹配**（非精确匹配）：predicate 是自由字符串,描述进 predicate 后变成 `指导(论文术语...)`,精确匹配 `指导` 会漏。过滤用 `predicate LIKE '指导%'` 前缀匹配,兜住所有 `指导` 开头的谓词变体
  - 命中预定义表 → 程序硬筛；命中不了（新谓词/混合 query）→ LLM 现判谓词集（B 兜底）
- **分支判断（层1 后候选数 vs cutoff=50【初始工程参数】）**：
  - **候选 ≤ cutoff**：直接进步骤 3c 下钻（不走层2 embedding、不走层3 LLM 确认——谓词过滤已够准,省 token 省 LLM 调用）
  - **候选 > cutoff**：走层2 + 层3（如下）
- **层2 · embedding 采样（程序,仅候选 > cutoff 时触发）**：
  - 节点名 embedding 余弦相似度 → top-50 截断 → softmax(温度 T) → 不重复采样 top-k
  - top-k 动态：按 evidence state 6 分量（C_slot/C_evidence/C_consistency 低→k 大广采,高→k 小精筛）+ 预算 联合定
  - 多轮可回采：visited 集合去重,没采中的下轮 CONTINUE 时从剩余里采
  - embedding 模型：GLM-Embedding-3（复用项目 .env）；节点名向量预算缓存（graph.db `embeddings` 表），query 向量缓存（LRU 500 + TTL 7 天【初始工程参数】）
- **层3 · LLM 确认 + 下钻指令合一（仅层2 采样后触发,小集合不跑）**：拿到采样候选（节点名+谓词）,LLM 一次性完成两件事,不分两步：
  1. **语义确认**：判断"这真是合作关系吗",筛掉误采
  2. **下钻指令**：对确认的候选直接给下钻指令（读哪个 raw、重点读哪段）
  - 输出结构化: `[{"node":"...", "read_raw":true, "focus":"作者行/方法段/..."}]`,程序解析后执行读 raw
  - 好处:LLM 状态连续（确认时已理解候选语义,同轮给下钻指令,不切换上下文）+ 省一次 LLM 调用（确认+下钻指令合一）
  - **与小集合路径的分工**：小集合（≤cutoff）下钻是程序机械读 Navigation（无 LLM 介入,省）；大集合（>cutoff）下钻是 LLM 指挥读（有 LLM 介入,准）——两条路径各自最优,不强求对称

问题类型路由：论文方法对比 → relations --predicate 提出方法/对比方法；"谁是谁的学生""谁和谁合作" → relations --predicate 合作者/指导；agent 记忆/五策略相关 → search 定位实体后 neighbors；RAG/检索/自适应探索相关 → 同上。

- 图返回的边可能带 `source`/`locator`，也可能为空；locator 是可选的局部读取捷径。
- **硬约束**：图边是导航关系而非事实答案。优先读取相邻 Wiki 的目标 section，再沿该节 Raw 脚注下钻；边 locator 存在时可直接精确读取。
- **Raw 下钻**：论文只读 MinerU `paper.md`；通用文档优先读可原生 locate 的来源，否则读同 stem Markdown companion；非论文且有文本层 PDF 可读取页码 locator。
- 如果命中 → 按 Wiki section → Raw locator 路径核验并综合回答
- 如果未命中 → 进入第三层

## 第三层：子项目索引

读取对应子项目的 `wiki/index.md` 定位相关页面。

- academic 的会议纪要可用"按主题快查"表格加速
- 如果仍未命中 → 告知用户知识库中暂无相关内容

## 混合查询规则

当问题既含实体定位又含关系查询时(如"白生辰的论文用了什么方法""青梧工作室与哪些人合作"),按两步串联,非并行:

1. **先图 search 定位主体实体**(白生辰 → people 页;青梧工作室 → Hub 页)
2. **再图 neighbors/relations 发现关系**(relations --predicate 合作者/指导 查白生辰;relations --predicate 提出方法 查方法关系)
3. 综合两层结果读取目标页面 + 锚点

不并行是因为关系查询依赖实体先定位(三元组主体需先确定),串联符合依赖顺序。

## 第四层：联想层（LLM 参数化知识 + 网络，2026-07-25 v3 合并）

> **触发条件**：图搜索入口与关系导航均无可行动命中，且子项目页面目录也无法提供候选。典型场景：用户问的细节术语未进入图的定位字段或关系网络，如"AKLT 态与 FCS 的关系"这类细节词。
> **定位**：当前规模（≤500 页）的轻量语义层，用 LLM 参数化知识替代 embedding 做语义匹配。与「embedding 语义检索」(远期第五层) 的关系：联想层是当前可用的可解释语义层，embedding 是规模化后的黑盒兜底。
> **最高硬规则**：联想只负责"去哪找"(定位 raw)，不负责"是什么"(事实)。事实依据始终是 raw 回溯，联想完全不影响此原则——raw 没有的，诚实说找不到，不用联想/网络结果编答案。

### 执行流程

联想来源合一：capability experience（最多 2-3 条泛化 pattern）+ LLM 参数化知识（训练习得，非本库）+ 网络（可用时补强）。先运行 `.scripts/experience_recall.py recall --capability query --event deadend --context "<查询线索摘要>"`，再由 LLM 参数化知识联想 3-5 词;若对术语陌生(参数化盲区)且网络可用,先网络检索获取术语上下文,再基于网络结果生成联想词。**显式列出召回的经验 ID、联想词及理由**(可审计)。
   - 例：问"AKLT 态与 FCS 的关系" → 联想 [MPS（AKLT 是知名 MPS）、finitely correlated states、parent Hamiltonian、注入性]，理由"AKLT/FCS 是 MPS 理论历史源头"
   - **硬约束**：网络结果 ≠ raw，不可追溯，**绝对不可作为事实来源或引用**；网络只用于"帮 LLM 想出更好的联想词"，离事实隔两层（网络→联想词→raw grep→raw 行号→事实），不可短路

1. **grep raw 定位**：用联想词对候选论文 raw 做**分级 grep**——候选集优先由联想词中的上位概念在 index 定位（子项目/主题目录），逐级扩大子集范围；**全库逐篇 grep 计数排序仅作最后兜底,非默认**（大规模时效率代价高）
2. **定向截取**：命中论文按「section 读取」grep 行号+段截取（禁止头部切片），拿 raw 行号

### 边界与约束

- **联想只定位不回答**：联想词仅用于 grep raw 定位，答案必须有 raw 行号出处
- **联想依赖参数化知识，有盲区**：LLM 未学过的术语（如 2026 年新概念）联想不出，网络检索不可用时直接诚实说找不到；非结构化保证
- **联想词限量**：3-5 个最相关，过度联想增 grep 噪声
- **可审计**：联想词及理由记入 query-log 的 association 字段，供 LINT 审计联想→命中路径是否合理

### 反哺（缺口记录 → ingest update）

> **时机与边界**：query 完成回答后只记录候选缺口，绝不直接写 wiki、graph.db 或 raw。用户明确要求“反哺/更新知识库”后，另起 `ingest update`；由该流程更新 Wiki，并把 Wiki、Raw 文档包节点及 `Wiki → 来源 → Raw` 与导航关系增量写入 graph.db。
>
> **触发判据（满足任一时记录候选）**：
> 1. **raw 重要事实缺口**：回答所需的重要事实已在可定位 raw 中确认，但对应 wiki 的 Navigation/Content 未覆盖，且 graph 无可用导航关系。
> 2. **稳定导航缺口**：raw 支持可复用的实体—关系，补入后能明显缩短未来同类 query 的定位路径。
> - **不记录**：低复用或仅服务本次问题的细节；无法稳定归属页面/实体的信息；尚未完成 raw 回溯验证的推测；完全失败（不知道具体遗漏什么）。
>
> **候选内容**：记录 raw 路径与 locator、目标 wiki 页面（已有或待建）、拟补的事实/关系及触发原因；不把未经 `ingest update` 编译和校验的候选视为知识库内容。
> - **仅事实补全**：`ingest update` 更新 wiki；除非其形成稳定导航关系，否则不建 graph 边。
> - **稳定导航关系**：先更新 Wiki，再由既有 graph 流程从已校验产物更新 graph；边 locator 可选，事实引用保存在 Wiki 的 Raw 脚注中。
> - **可审计**：候选记录到 query-log `knowledge_gap` 字段（不写入 `index_enrichment`），供 LINT 审计价值、授权与最终落盘结果。

## embedding 应用(v3,2026-07-25 局部引入)

**已引入(局部)**:embedding 用于 hub 成员/节点候选的**概率采样**(步骤 3a),非全局语义检索第五层。
- 模型:GLM-Embedding-3(复用项目 `.env`,dim=2048,中英混排)
- 节点名 embedding 预算缓存(graph.db `embeddings` 表,ingest 时算)+ query 向量缓存(LRU 500 + TTL 7 天【初始工程参数】)
- 采样:节点名 + query 向量余弦 → 两阶段(top-k cutoff 截断 + softmax 温度 T)→ 不重复采样

**远期第五层(全局语义检索,未引入)**:全文 embedding 语义检索,大工程,留远期。当前局部 embedding 只算节点名相似度,不是全文检索。
- 触发判据(满足任一):漏召率上升(月度 >10% 进第三层未命中)/ 规模临界(页面 >500 且图检索召回率下降)
- 引入形态:作第五层兜底,不替代图路由,召回仍需锚点定位 + raw 回溯

**联想层与局部 embedding 的关系**:联想层(第四层,LLM 参数化知识 + 网络)是"图全 miss"时的语义兜底;局部 embedding 是"图有候选但多"时的采样优化。两者不冲突,各管各场景。


## 证据状态与动作协议(v3,2026-07-20,GPT 修正重构)

> **程序不裁决开放语义,但核验客观事实、约束执行过程、要求 LLM 对证据不足作显式说明**。
> 边界:程序管"能不能这样查、证据实际什么状态",不管"你最终怎样理解证据";LLM 可自由理解,但不能虚构证据、无限绕圈、把未知说成已核验。

## 最小证据与披露

| 层 | 归属 | 内容 |
|----|------|------|
| **硬执行约束** | 程序 | token/预算、回环上限、已访问动作、重复拦截、无效路径拦截、工具失败显式返回 |
| **证据状态画像 Evidence Profile** | 程序供给(信息,非门控) | 原始证据事实(非语义结论) |
| **语义决策** | LLM(不被质疑) | 够不够、是否继续、如何回答、来源间关系 |

## Evidence Profile(程序只报告可机械确认的事实,不替 LLM 判)

程序返回原始证据事实和规则提示,**不返回"权威充分/时间匹配/证据足够"等语义结论**(防换个名字又变回语义门控):

| 项 | 程序返回(原始事实) | 不替 LLM 判 |
|----|------------------|------------|
| source_presence | sources 字段是否非空、source 数量、source 指针 | 不判"是否足以支持结论" |
| source_types | source_type 值(official-doc/paper/纪要/转述) | 不判"是否对当前问题最权威" |
| version_status | status 值(current/deprecated)+ date + superseded_by | 不判"时间是否匹配用户意图" |
| conflict_markers | related 指向、版本链、未闭合标记 | 不判"是否构成需说明的冲突" |

LLM 据此判:来源是否足够、权威是否适合、时间是否匹配、标记间是否需说明。

## 三种表述姿态(非互斥,可组合)

一个回答常混合多种(如 Q10:路径已核验+主体缺失+机制探索性)。LLM 据 Evidence Profile 为**不同结论**选不同表述姿态:

- **已核验**:证据/权威/时间/冲突状态均满足该结论的任务要求
- **限定性**:部分满足,答案须明确范围和缺口(如"路径目标如此,决策主体本库未记载")
- **探索性**:证据弱,只给线索/假设/"库中未找到"

**诚实边界**:程序为最终回答提供 `required_disclosures`(不可忽略的证据状态和风险提示);LLM 须依查询规范将证据不足显式反映在答案中。**当前 A+ 对检索动作可硬约束,对最终语义表达主要靠规范约束+事后审计**(程序不检查最终措辞,不谎称硬控制)。

## 回环规则

证据缺失**不自动**等于继续检索。区分两种缺:
- 缺,且有明确可能存在的材料 → 可继续查(须提交 gap+candidate+action+expected_gain)
- 缺,且知识库无可行动线索 → 停止并诚实说明(限定性回答)

**每轮追加检索须提交**(程序检查,缺则否决):
- `gap`:具体缺口(如"不知道最终终止决定",非"信息可能还不够")
- `candidate`:明确候选来源(如"2024第一次班子会纪要")
- `candidate_basis`:为何此候选可行(如"Topic Hub 时间线含该页")
- `action`:具体动作(read_section/read_triples 等)
- `section`:具体 section
- `expected_gain`:预期信息增益

**程序只做可机械检查**(不判"是否可能获新信息"——那是语义):
- 目标 action key 是否已访问(重复拦截)
- 是否还有预算
- 是否超回环上限(3 轮)

边界:**LLM 决定为什么继续,程序检查这次继续是否具体、合法、未超预算。**

## 回环计数(程序计,非 LLM 自报)

回环数 = LLM 出检索计划次数 - 1(初始计划不计),由编排层程序计。loop_count >= 3 强制停(hard_stopped)。能验证则验证。

## 高层摘要定位约束(保留)

高层 review/concept 摘要主要视为导航信息,非具体事实最终证据。涉及制度/时间版本/正式决定/因果/争议时,须沿 sources 下钻到底层页面或 raw。全局态势问题可从最高可用导航视图起,已知论文直接进 L1 summary(见 HUB.md)。

## 槽位清单与缺口回检(v5,2026-07-23 重构)

> 解决 pilot 暴露的漏停问题(M1/M3/M4):系统搜到部分证据就判 `sufficient` 停止,遗漏未覆盖槽位。
> v4 问题:槽位一次性声明后不修正,LLM 开局理解最浅时漏声明→少声明→易判 sufficient→误停。
> v5 改进:动态增量槽位(种子模板开局+证据触发修正+停止前元数据锚定审计)+ 三层停止定位(槽位审计=规划检查/答案审计=交付检查/元数据硬约束=程序最终否决)。不增 LLM 调用次数,轻度增 token。

**触发**:决策追溯/制度版本/争议/关系/比较类题(多槽题)。简单事实题(单槽)可不声明,跳过本机制(向后兼容)。

### 第一步:类型种子槽位开局(LLM 做,语义判断 + 模板种子)

- 第一轮检索前,LLM 按题类型从种子模板生成初始槽位清单(暂定,非最终固定标准)
- **种子模板**(按题类型预置,LLM 在种子基础上增删):
  - 制度版本: `[旧版内容, 新版内容, 修改时间, 提出主体, 批准人, 修改原因]`
  - 决策追溯: `[决策内容, 提出时间, 提出主体, 批准人, 执行状态, 终止原因]`
  - 跨文档比较: `[对象A方法, 对象B方法, 对象A解决的问题, 对象B解决的问题, 本质区别]`
  - 争议/冲突: `[观点A, 观点B, 冲突类型, 各自依据, 是否已闭合]`
  - 关系: `[实体A, 实体B, 关系类型, 关系依据]`
- 编排层 `init --slots "槽1,槽2,槽3"` 提交;程序存入 session

### 第二步:每轮增量检查(LLM 做,搭便车不增调用)

- 每次本来就要调 LLM 分析检索结果时,顺带回答:**新证据是否要求新增、合并、修正或取消某个槽位?**
- 无变化返回 `NO_CHANGE`;有变化返回 `SLOT_UPDATE: +新槽 / -取消槽 / =修正槽(附理由)`
- **修改必须由新证据触发**(不能凭空扩充),触发场景:发现新版本 / 发现适用对象不同 / 出现新决策主体 / 出现来源冲突 / 发现新因果环节
- 程序更新 session 中的 slot_checklist,重算缺口

### 第三步:程序回检覆盖(程序做,机械匹配)

- 每轮检索后,LLM 报告已覆盖槽位(`exec --covered "槽1,槽2"`)
- 程序算缺口 = 清单 - 已覆盖,返回给 LLM
- LLM 想判 `sufficient` 时,程序回一句:"声明槽位 N 项,覆盖 M 项,缺失:XXX。剩余预算:XXXX tok"

### 第四步:停止前元数据锚定审计(LLM 做,锚定程序供给的元数据)

- 槽位全覆盖时**不能立即停止**。利用最后一次已有 LLM 调用,增加一句审计:
  > 程序报告的 version_status / source_types / conflict_markers 中,是否有客观事实指向我尚未声明的槽位?(如:发现 deprecated 版本→是否该有「新旧差异」槽;发现多来源→是否该有「来源权威差异」槽;发现冲突标记→是否该有「冲突原因」槽)
- **审计锚定元数据**(非纯自省):LLM 的盲区在声明时和审计时往往是同一个;锚定程序硬供给的元数据,减少「同一个盲区贯穿始终」的风险
- 无遗漏 → 进入答案审计;发现遗漏 → 新增槽位继续检索(走现有回环)

### 第五步:答案审计(LLM 做,交付检查,停止后交付前)

- 槽位审计通过(停止判据满足)后,形成候选答案,再做一次交付检查:
  > 根据已获证据,当前答案是否完整、准确地回应了用户问题?
- 检查项:
  - 所有关键槽位是否体现在答案中(**程序可锚定**:机械检查 covered_slots 关键词是否在答案文本出现)
  - 是否有证据但没写进答案的内容
  - 是否答非所问、逻辑跳跃或前后矛盾(纯语义,LLM 自检)
  - 是否把不确定结论写得过于肯定(**姿态一致性**:程序可锚定——evidence_profile 有 deprecated/conflict 标记但答案未提及→flag)
  - 引用的版本、权威来源和冲突是否处理正确
- **答案审计不是停止判据,是交付检查**。停止判据是证据状态(槽位+元数据)。答案审计有循环性(同构 AutoSearch 的答案自评估),但元数据硬约束在其后兜底

### 第六步:元数据硬约束(程序做,最终否决)

- 即使槽位审计和答案审计均通过,程序仍检查(答案无关,读 frontmatter):
  - 是否有真实来源(source_presence)
  - 是否引用过期版本(version_status=deprecated 但答案未说明)
  - 是否存在未处理冲突(conflict_markers 未闭合)
  - 是否达到预算或回环上限
- 任一未通过 → 程序否决交付,返回具体问题给 LLM 处理

### 三层停止定位(诚实标注,防过度声称)

| 层 | 角色 | 谁主导 | 答案相关性 | 循环性 | 抓什么失败 |
|---|------|--------|-----------|--------|------------|
| 槽位审计(步骤 4①) | 规划检查(该查什么) | LLM + 程序盯缺口 | 部分 | 有,缓解不消除 | 声明了但没覆盖 |
| 答案审计(步骤 4②) | 交付检查(答好了吗) | LLM + 部分程序锚定 | 是 | 有(同构 AutoSearch) | 有证据但没写进答案 |
| 元数据硬约束(步骤 4 机械层) | 最终否决(客观事实) | 程序 | 否 | **无** | 该查但没声明(程序读 frontmatter 兜底) |

**与 Evidence Profile 的关系**:槽位清单是 Evidence Profile 的补充维度——Profile 供给客观证据事实(sources/status/source_type/conflict),槽位清单供给"本题预期要答几个点"。两者合供 LLM 判充分性,程序都不判语义结论。元数据硬约束是程序最终否决层,独立于 LLM 判断。

## stop_reason(诚实边界可追溯, v5 三返回值对齐)

会话结束附 stop_reason(对齐三返回值):
- `FINAL_ANSWER`(`sufficient_complete`):槽位审计+答案审计+元数据硬约束均通过,输出最终答案
- `BOUNDED_ANSWER`(`sufficient_partial`/`no_actionable_candidate`):证据不足且无可行路径,输出限定性答案(须附缺口说明)
- `CONTINUE`:发现新槽位或证据缺口,继续检索(走现有回环,须提交 gap+candidate+expected_gain)
- `budget_exhausted`:预算耗尽
- `loop_limit`:回环上限到
- `tool_failure`:工具失败

**问题类型分流**(步骤 1 判断后,仅决定 Evidence Profile 关注重点,不强制门控档):
- 简单事实:关注 source_presence + source_types
- 关系:关注 source_presence + source_types + 多来源一致性(LLM 自检,非程序)
- 决策追溯/制度版本/争议:关注全画像 + 槽完整性(LLM 自检清单,非程序门控)

## 首轮定位步骤

0. **预算感知**(程序供给):编排层实计 token 并累计,budget_warned(>70%)时返回降级提示,budget_exhausted(>100%)硬停(见全局规则「token 预算提示」)

1. **意图判别**(用类型信号表)→ 输出**证据需求**(决定槽位)+ **单源/多源标志**(驱动步骤 3 读策略:简单事实=单源集1、关系/对比=多源集2+、汇总=开放集N)。按需 read_section 读对应细则

   **类型信号表**:

   | 类型 | 信号关键词 | Evidence Profile 关注点 | 单/多源 | 按需读细则(用 read_section.sh operations/QUERY.md "<section名>") |
   |------|-----------|--------|--------|--------------------------------------------------------------|
   | 简单事实 | "是什么/几年/多少/在哪/谁/列出" | source_presence + source_types | 单源 | 仅本执行步骤够 |
   | 关系 | "什么关系/差异/对比/哪些...相关" | source_presence + source_types + 多来源一致性(LLM 自检) | 多源 | +混合查询规则(若混合) |
   | 决策追溯 | "怎么决定/为什么/过程/依据/谁决定" | 全画像 + 槽完整性(LLM 自检清单) | 多源 | +证据状态与动作协议(含种子模板) |
   | 制度版本 | "vs/相比/版本/演进/新旧/替代" | 全画像 + 槽完整性(LLM 自检清单) | 多源 | +证据状态与动作协议(含种子模板) |
   | 争议 | "是否一致/冲突/矛盾/分歧" | 全画像 + 槽完整性(LLM 自检清单) | 多源 | +证据状态与动作协议(含种子模板) |
   | 权威判断 | "什么类型/什么来源/哪个层级/谁发布/什么状态" | source_types(source_type 识别,程序供给客观可校验) | 单源 | +证据状态与动作协议 |

   **局部读取规则**:判完类型后按"按需读细则"列用 read_section.sh 读对应 section,不必读全文。简单事实只读本执行步骤即可回答

2. **廉价经验探测**(机械,近零 token):对问题术语跑 sub-second grep,返回 hit/miss **事实**(非参数猜测,消除答案泄漏)。结果入 query-log `probe` 字段

   | 探测 | 命令 | 双功能 |
   |---|---|---|
   | 图搜索入口 | `query_graph.py search <term>` | 定位实体/页(查 nodes 的 title/aliases 等字段) |
   | 关系枚举 | `query_graph.py neighbors/relations <node>` | 枚举关联节点/关系集(BFS,见第二层) |
   | 实验回退 | `grep <term> triples-<域>.md`(仅实验脚本/历史材料) | 在图盲区时辅助定位，不作为生产主路径 |

   **探测边界**:生产探测以图(nodes/edges)为主,triples markdown 仅作实验回退。均非遍历型 O(N)。**禁止全库 raw 内容 grep**(内容层定位属步骤 3b/3c 分级 grep)与 **禁止 `rg --files` 全库遍历**(节点定位用图 search)。遍历型 O(N) 用图消灭,非拆分

   **已编译反哺读取**:经 `ingest update` 校验并写入 graph.db 的稳定关系，会在本步 `search`/`neighbors` 中被读取；仅记录的 `knowledge_gap` 候选不参与检索。**默认过滤 status:deprecated**(用户问历史版本时例外)

## 证据下钻步骤

3. **渐进分派**(双重审计控深度,见步骤 4):据步骤 2 探测结果渐进检索,每级证据够就停。**目标是组装相关文件集(非找单个文件)**,集大小由步骤 1 单/多源标志决定

   - **3a 三层筛**(见第二层「三层筛」):层1 谓词过滤 → 分支判断(≤cutoff 直接下钻 / >cutoff 采样+LLM 确认下钻指令)。route 字段记谓词过滤/采样/确认路径(审计防答案泄漏)
   - **3b 下钻执行**(据 3a 路径分支):
     - **小集合路径(候选 ≤ cutoff)**:程序机械读——按候选节点下钻,读 wiki 页 `Navigation` 判相关,相关读 `Content`,raw 源 grep 定位行号截取(禁头部切片);**只读 md 不读 pdf**(paper 页 sources 含 paper.md 和 pdf 时只读 md)
     - **大集合路径(候选 > cutoff)**:程序按 LLM 在层3 给的下钻指令(`[{node, read_raw, focus}]`)执行读 raw——LLM 已确认相关并指定读哪段,程序直接读指定 raw 的指定段
     - **降级 Raw 分级 grep**(Wiki 摘要不充分 / 无 Wiki 页时,事实必 Raw 回溯):优先从 Wiki `sources`/`raw_citations` 或 `来源` 边取得 Raw 文档包；可选边 locator 存在时直接使用。来源不足时才分级 grep 逐级扩大，禁默认全库（先已知子集 → 联想限定子集：子项目 raw 目录 / 时间范围 → 逐级扩大，每级命中即停）
   - **3c 联想层**(步骤 2 全 miss 时升级,capability experience + LLM + 网络合一):先运行 `experience_recall.py recall --capability query --event deadend` 获取最多 2-3 条泛化 pattern→参数化联想 3-5 词(+理由);参数化盲区且网络可用时,网络检索补强联想词→grep raw 定位→定向截取。**联想只定位不回答,事实必 raw 回溯**。**硬约束**:网络只帮想联想词,离事实隔两层,绝不可作事实来源。详见「第四层:联想层」

## 续查与停止步骤

4. **三审停止**(控升级 + 终止,见「证据状态与动作协议」含槽位清单与缺口回检 v5):

   | 方向 | 审计 | 性质 | 作用 |
   |---|---|---|---|
   | 向上停 ① | 证据充分性(槽位满) | 机械 | 槽位填满→可停升级 |
   | 向上停 ② | 回答完全性(答全) | 语义 | 答案覆盖问题全 scope→可停;①过②不过→重读补综合/重写 |
   | 向下地板 | grep/raw 兜底穷尽 | 机械 | 未穷尽不得判证据不足→强制升级 |

   ①② 都过→`FINAL_ANSWER` 进步骤 5;①过②不过→`CONTINUE` 重读/重写;地板到+仍不足→`BOUNDED_ANSWER` 限定性回答。程序只做机械检查(重复/预算/回环上限/槽位缺口回检/元数据硬约束);回答完全性(②)归 LLM。缺口无常规候选时读 `operations/RECOVERY.md` 找替代路径;仍无则直接限定性回答

   **回答完全性审什么**:问题覆盖(多部分都答)、综合完成(关系/对比/影响做了综合非只罗列)、无遗漏(相关集各源视角被采纳)

   **开放汇总特例**(列所有 X,槽位开放不知该几个):主停靠步骤 2 枚举图中相关节点与关系集耗尽;校验靠机械交叉引用——读过页 frontmatter `related:`/`[[wikilink]]` 是否 ⊆ 已召回集合,集外被引页→缺口→补读;LLM 反思"是否遗漏"仅低置信 flag,不硬阻停(防幻觉+不可审计)

**失败扩预算续查(v3,2026-07-25 新增)**:若步骤 4 判 `BOUNDED_ANSWER`(证据不足且无可行路径)且预算未耗尽:
   - 返回用户 tokens 透明信息:已用 tokens / 原预算 / 消耗分布(探测/采样/读页/LLM 各占)
   - 询问用户:是否翻倍 tokens 预算继续查?(立刻问,内存暂留中间状态,方案 A)
   - 用户确认 → 预算 ×2,从断点续查(visited 不重置、已读页不重读、evidence state 快照恢复);stop_reason: `BOUNDED_ANSWER → 用户扩预算 → CONTINUE(预算翻倍)`
   - 用户拒绝或预算已耗尽 → 维持 BOUNDED_ANSWER,诚实返回限定性回答
   - **续查命中不反哺**(没走联想层,命中直接答);续查仍失败 = 真查不到,诚实返回

## 交付步骤

5. **综合回答**,使用 `[[wikilinks]]` 引用来源
   - 对关键声明,用锚点链接精确定位:`[[papers/论文X#mpe-definition]]`
   - **诚实标注未确认部分**:若证据状态不完整,回答须区分已确认结论与未确认部分,明确缺失的是信息/证据/权威来源/有效版本/冲突解释中的哪类,禁止将尚未验证内容表述为事实
   - **多源综合**:跨源一致性/冲突检测,显式标注冲突,禁静默合并

6. **回答后反哺判断**：按「反哺（缺口记录 → ingest update）」判定并记录 `knowledge_gap` 候选；query 到此结束，不直接写 wiki 或 graph.db。仅在用户明确授权后，另起 `ingest update` 落盘与校验。

7. **查询日志**(v4,2026-07-23 动态检索流程落地):查询完成后追加一行 JSONL 到 `academic/wiki/outputs/query-log.jsonl`(或对应子项目 `*/outputs/`),字段对齐 testbed `trace-format.md`:
   - 必填:`ts`(时间戳)、`query`(查询)、`pages_read`、`evidence_profile`(程序供给的原始证据事实)、`stop_reason`(sufficient/no_actionable_candidate/budget_exhausted/loop_limit/tool_failure)、`decision`(回答/停止)、`tokens_est`/`tokens_actual`、`loop_count`
   - **v4 新增(动态流程审计)**:`probe`(步骤 2 探测结果,hit/miss 事实)、`route`(步骤 3a 排序信号+分派路径,审计路由是否信号驱动防答案泄漏)、`routing_cost`(步骤 2+3a 开销,近零 token,与 retrieval_cost 分列)、`answer_completeness_audit`(步骤 4 ②回答完全性审计结果,LLM 判)
   - **保留程序侧**:`evidence_profile`(source_presence/source_types/version_status/conflict_markers,客观事实)、`required_disclosures`(证据不足提示)、`loop_count`(程序计,非 LLM 自报)、`slot_checklist`/`covered_slots`/`slot_gaps`(v5:含种子模板开局+增量修正轨迹)、`slot_updates`(v5 新增,每轮 SLOT_UPDATE/NO_CHANGE 记录)、`answer_audit_flags`(v5 新增,程序锚定的答案审计 flag:槽位关键词缺失/姿态不一致)、`knowledge_gap`(反哺候选记录；实际落盘由后续 ingest update 另行审计)
   - **用途**:供 LINT 审计证据状态是否属实(程序读 frontmatter 客观,非判语义)、回环计数是否超限、stop_reason 与回答是否一致、`route` 是否信号驱动(防答案泄漏)、`probe` 是否真实

## 关系查询示例

| 用户问题 | 查询策略 |
|----------|----------|
| "矩阵乘积纠缠是什么" | 图搜索入口 → 读取概念页 |
| "哪些论文用了矩阵乘积态" | 图关系导航（提出方法 + 对比方法）→ 读取论文摘要页 |
| "张量网络和量子机器学习有什么关系" | 图关系导航（属于领域 + 应用于）→ 读取概念页 |
| "王XX和哪些人有合作" | 图关系导航（作者/合作者关系）→ 读取 people 页 |
| "十五五规划中关于量子信息的内容" | 图搜索入口 → Hub 关系导航 → 读取政策 + 学术页面 |
| "专家签字表放在哪了" | **前置路由** → 直接读取 `procedures/重要物品存放位置.md` |
| "公章在哪" | **前置路由** → 直接读取 `procedures/重要物品存放位置.md` |
