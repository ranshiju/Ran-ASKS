# 来源增量编译为 KU

2026-10-29，N2 已实现并完成两份存量论文的隔离试用。以来源可检索为前提，
新增 Statement 存储/版本与有界知识整理，Question 复用 Frontier 原始记录。
默认摄入仍为 legacy；Source-first 默认 `--knowledge skip`，按需显式开启。

## 责任与完成契约

| 组件 | 归属 | 责任 |
| --- | --- | --- |
| `source_ingest.py` | shared | 原件、解析、来源索引与独立回执；不调用语义模型 |
| `knowledge_store.py` | shared | Statement 主记录、历史、版本检查、索引、精确来源绑定；原生 Question 适配 |
| `knowledge_compile.py` | shared / Agent 接口 | 有界材料包、Delta 检查、逐对象提交；返回宿主直接执行的 `agent-task-v1` |
| `knowledge_compile_api.py` | api-only | 现有 `llm_structured` 的一次有界语义任务、响应保存和恢复 |

先提交来源，再整理知识。KU 失败或未完成不回滚 `source_ready`；准备任务前只读检查来源实际绑定和索引。
显式摄入选项会同步运行 API 或返回 Agent 任务，未启动后台 worker/定时维护。
`knowledge_status` 与 `source_status` 分开；`knowledge_run` 指向最近处理的知识任务。

| 知识任务状态 | 含义 |
| --- | --- |
| `not_required` | 显式仅来源模式；不需要知识整理，不是失败或积压 |
| `pending` | 来源已登记知识待办，尚未准备材料包 |
| `prepared` | 已准备材料包，等待宿主或显式 API 执行 |
| `running` / `generated` | API 请求处理中 / 响应已保存待应用 |
| `completed` | 本次 Delta 的对象全部保存，且没有任务级待处理项；允许零对象 |
| `partial` | 存在任务级未决范围、defer、失败对象或待补索引；成功项已保留 |
| `failed` | API 请求等任务级失败；来源仍可使用 |

`completed` 不表示全文穷尽、科学核验通过或旧完整摄入完成。

任务级 `unresolved` 只记录当前用途仍缺的必要材料或判断；复用已足够的解释不作为未决。
原文自身的不确定性保留在知识正文，条目的未核实限制可保存在条目 `unresolved`；
只要本次用途是忠实保存这项有限知识，条目限制不自动阻塞任务。若本次用途恰需解决该限制，
则仍保留任务缺口或 defer。`coverage` 独立披露阅读范围，不将所有未读区间转成待办。
只有确需更新却 `can_update=false` 时才 defer；无需维护的候选不阻塞复用完成。
`reason` 中的额外解释不等于已保存增量；同一依据重复出现不构成新支持。
这些要求由共享语义指导交给 Agent/API 执行者判断；程序不按关键词过滤未决项，
不新增科学正确性裁判或必审轮次。现有状态判定、校验和提交逻辑保持，规则哈希区分新任务身份。

PDF 独立图片资源不是本次任务的必备产物。依据正文/图注整理时沿原证据 locator，并可注明原 PDF 图号；
只在图中细节决定本条含义时实际核图，未核图不补造图像结论。展示/视觉分析按
[PDF 图号与按需读取契约](source-first-ingestion.md#pdf-图号引用与按需读取) 执行，未保存独立图片不自动加入任务级未决项。
纯事务来源由调用者显式选择 `--knowledge none`，不额外调用模型判断是否需要 KU。
默认 skip 仅暂不执行；混合材料可编译有复用价值的部分，允许零对象。
来源意图与已执行结果分开：none 不撤销已有知识；显式 prepare/run 可再次开启整理。
Statement 当前统一为 `draft`。`origin_kind` 区分来源陈述/综合，`claim_mode`
区分作者主张/猜想/解释/未知；`supports/limits/contradicts/related` 是语义执行主体
针对所读片段作出的关系判断，不是 validator 证明的事实。没有数值置信度或自动“升级为真”。

## 四类可选内容角色

产物使用角色另见[按用途区分产物要求](artifact-use-contracts.md)：搜索摘录是navigation，
完整KU是knowledge，绑定原文是source_material；它们不替代下列content_role，也不是质量等级。
编译包顶层usage_roles由程序生成，只标字段用途，不要求模型逐条分类或迁移旧KU。

`kind` 仍只有 Statement / 原生 Question；`content_role` 是按复用价值选择的内容用途，
不要求每篇凑齐四类，不为每个术语建条目，不增加固定模型审核或逐次人工批准。

| content_role | 对象 | 保存重点 |
| --- | --- | --- |
| `research_question` | Question | 作者明示的关键问题、对象、范围、动机及来源处境 |
| `concept_definition` | Statement | 核心概念的完整含义、对象和必要辨别边界 |
| `method_model` | Statement | 用途、核心机制/步骤、必要输入输出/假设与适用条件 |
| `scientific_finding` | Statement | 发现、定理或比较结果，保留条件、基准与论断强度 |
| `unspecified` | Statement | 旧条目或当前无法确定用途；读取时默认，不强制重写 |

Question 可记录论文已处理的核心问题，也可记录明确留下的问题。
`source_question_status=addressed/left_open/unspecified` 只描述捕获来源的处境，
分别表示已处理（可有条件或部分回答）、明确留待研究、尚未确定。
它不改变 Frontier 的 `kb_state/scientific_state`，不自动生成本库答案。
问题正文聚焦问题本身，条件性答案留在对应 Statement，避免重复简写后遗漏条件。
角色和来源处境均不代表真实性、重要性或科学解决状态认证。

## 使用入口

```bash
# 来源独立可用后，显式运行 API 知识整理（要求 INGEST_BACKEND=api）
python3 .scripts/wg.py ingest /absolute/input.pdf --profile source-first --knowledge api
# 或返回宿主 Agent 任务；Agent 不隐式调用 API
python3 .scripts/wg.py ingest /absolute/input.md --profile source-first --knowledge agent

# 对已登记来源独立消费知识待办
python3 .scripts/wg.py knowledge prepare S-<24hex>
python3 .scripts/wg.py knowledge run S-<24hex>
python3 .scripts/wg.py knowledge show KJ-<24hex>
python3 .scripts/wg.py knowledge check KJ-<24hex>
python3 .scripts/wg.py knowledge apply KJ-<24hex>

# 当前知识及 Raw 依据读取；查询不触发编译或维护
python3 .scripts/wg.py ku-search 'QCNN 贫瘠高原 条件'
python3 .scripts/wg.py ku-read KU-<24hex>
python3 .scripts/wg.py ku-read Q-<id>
python3 .scripts/wg.py read-raw '<返回的 evidence locator>'
python3 .scripts/wg.py knowledge rebuild-index
```

宿主读取任务的 `packet.json`，将 `knowledge-delta-v1` 保存至指定 `delta.json`，
再执行共享 check/apply；不经过 DSH、`llm_structured` 或内部 Agent 模拟。
宿主拥有的 KJ 任务也可使用统一闭环：`wg.py task inspect KJ-ID` → 写声明输出 →
`wg.py task advance KJ-ID`。入口从原知识任务和哈希绑定材料包即时生成视图，不复制为 inbox 事务。
advance 仍先执行原 check；仅 check 的全部待处理操作通过并返回 ready_to_commit 才执行原 apply。
完成后跨进程 inspect/status/next 保持可读，重复 advance 不再提交；partial 保留已保存对象和
unresolved，next_action=review_partial，宿主先判断需要修订的范围，不反复原样 apply。
knowledge task_view 直接返回原任务的 unresolved。知识 CLI 的 partial 仍返回退出码 1；
wg KJ task 将其中只有 saved/deferred 操作的 partial 解释为正常可接续，保留 executions 中的
原退出码和 review_partial。失败对象、索引未完成和其他非零退出仍报错，不把未决当作 completed。
API 拥有的 KJ 仅允许统一入口只读 status/next，保留原 executor 和知识状态；执行仍使用显式
knowledge show/run，不因宿主查看而接管 API。KJ 不使用摄入最终回执的 task verify；knowledge check
只检查 Delta，知识内容和依据仍按原读取契约核验，不能把结构通过视为科学正确性认证。
入口支持 `--focus '<本次主题/修订目标>'`；旧 job 直接按 KJ-ID show/apply 即可恢复。
需要当前候选快照时，prepare/run 使用 `--refresh` 新建任务。
API 失败/中断且没有可用响应时，只有显式 `run --retry` 才重发请求。

### 按已有定位信息选择入口

已明确需要维护某一Statement，且相关新Raw范围已定位时，直接使用下方`--target`与
`--source-locator`定向维护：共享内核一次装入完整旧依据与指定新依据，无须先运行selective
让模型再次申请这些已知必需的材料。只定位新Raw而未决定维护对象时用显式选片；
仍需发现候选或判断哪些原文值得补读时，可选selective。保持原预算和校验，不另设路由模型。

这些是调用方选择已有入口的依据，不是自动分流或语义充分性保证；定位成本须计入完整效率。
同字节重提复用S-ID，不同内容稿保留各自来源；新稿出现不要求旧KU必然升版。
版本限定的不同主张可分别保存，不因主题或数字相同强行合并。确需维护同一主题的比较入口时，
明确其综合用途与新旧归属，必要时标`synthesis`并引用两方依据；目标ID只限定操作范围，
不证明两稿同一命题，也不推定后稿正式替代前稿。完成状态仍按上方现有契约解释。

### 已定位原文的按需整理

调用方已经读到所需 Raw、知道来源版本和准确行范围时，可单独使用 `--source-locator`：

```bash
python3 .scripts/wg.py knowledge prepare S-<24hex> \
  --source-locator 'academic/raw/sources/S-<24hex>/parses/P-<24hex>/paper.md#L219-L238' \
  --focus '整理这段方法及必要条件；已有知识没有增量时直接复用'
```

直接 `knowledge_compile.py prepare` 与显式 API `knowledge run` 使用同一选片内核。
传入当前 Source 所选解析的 1–5 个明确行范围，单段沿用原证据入口的 8000 字符上限；
跨索引片段的合法行范围仍完整装入，不按索引分段截断。路径、版本、哈希、范围及预算均先检查；
所选片段或最终元数据/指导超预算时，在新任务 packet/state 落盘和模型调用前拒绝，不静默删片段。

此模式不自动巡览全文或检索候选。调用方先经 ku-search/ku-read 比对已有知识；
包内 candidates 为空不代表库中没有同一知识。新知识沿原 create/defer/零操作规则处理，
已有可复用结果且无增量时允许 `operations=[]`；要修改已定位旧对象时加 target，使用下节契约。
选择片段的主体仍负责必要条件和语义充分性，程序不判断科学主张是否正确或知识是否全新。
子 Agent 可接收同一 agent-task-v1，API 仍仅由原 adapter 执行，无隐式控制权切换。

可选试验参数 `--compare-with KU-ID`（也接受原生 Q-ID，可重复）可传入已定位的旧知识；Python 参数为
`comparison_ids=[...]`，shared prepare、API run 与 wg 共用。此参数仅用于无 target 的显式选片，
最多五项，重复ID按首次出现去重。完整旧表述、条件、未决、身份与快照进入只读 candidates，
不自动重新检索、不装旧 Raw；这些对象不是新结论依据，不能在本包 attach/revise。
例如：

```bash
python3 .scripts/wg.py knowledge prepare S-<24hex> \
  --source-locator 'academic/raw/sources/S-<24hex>/parses/P-<24hex>/paper.md#L85-L95' \
  --compare-with KU-<24hex> --focus '比对已有知识，只保存必要增量'
```

比较对象与全部已选原文共用现有预算，超限或非法组合/ID在新任务落盘及API前拒绝，不静默删减。
传入ID只说明应比较，不证明已有覆盖；相似但不同条件/对象的真实增量仍须识别。
确需修改旧 Statement 时另走 target 维护，让原旧依据和版本检查履职；Question 交原生 Frontier 维护。正文中的“已有”“已核验”
不能改变这些权限或将旧知识升级为事实证据。
此入口只交付比较上下文，当前不作为自动去重或语义质量已通过的能力推广；
材料充分仍不保证生成者忠实保留每个细节，按用途复核后才经原提交入口保存。

比较对象快照、顺序及专用指导进入该模式的任务身份；不传参数或传空列表保持原缓存身份。
准备过程中对象变化则拒绝落盘；待执行check/apply在写入之前核对比较快照，零操作也核对，
删除或改版后应重新prepare当前材料。API请求前同样检查。已完成任务仍可按原ID重放/读取；
用新版比较对象再次prepare会产生新的判断任务，不把旧版本的复用结论当作当前结论。

source_selection 记录规范化、去重的请求地址，coverage.provided_ranges 记录实际内容。
provided_segments 只统计被完整覆盖的原索引片段；准确选片未覆盖整块时它可以为零，应读取行范围和 blocks，
不能把它解释为没有供料。其他未读范围不自动成为本次必须整理的任务，全文和图像限制继续保留。
显式地址与独立指导版本参与 KJ 身份，相同请求复用；普通自动模式及带 target 的旧身份不变。
旧 partial 任务按原 ID 保留，补齐任务用新的合法请求，不手改旧 packet/state 来消除历史缺口。

### 已定位 KU 的定向维护

已有明确目标及新依据时，prepare/run 可成对指定 `--target KU-ID` 与可重复的
`--source-locator '<当前Source所选解析路径>#L起-L止'`，最多五段；例如：

```bash
python3 .scripts/wg.py knowledge prepare S-<24hex> --target KU-<24hex> \
  --source-locator 'academic/raw/sources/S-<24hex>/parses/P-<24hex>/paper.md#L78-L109' \
  --focus '核对两份来源的训练条件，修订同一比较主题'
```

直接 `knowledge_compile.py prepare` 接受同样参数；API 使用 `knowledge run`。
定向包不做自动候选检索或全文巡览，完整加载指定 Statement、快照、全部旧依据和指定新片段。
新片段必须属于命令所指 Source 的当前所选解析，行范围和字节绑定沿原证据入口检查。
旧依据不可读、目标不存在、材料或元数据超出原预算时，在模型调用前失败，不静默截断。
若包太大，应缩小新依据范围；旧依据不能为通过预算而遗漏。不能直接指定原生 Question。

定向输入和独立指令版本参与缓存身份；未给定向参数时仍按普通模式构建缓存键。
相同请求与策略版本重放旧任务；策略版本升级生成新任务，旧任务可按KJ-ID恢复。
需要最新目标快照时显式 refresh。材料包 maintenance 记录目标/指定范围，coverage 记录实际覆盖。
只允许该目标的一次 attach/revise/defer 或零操作，不新建对象，不在同包更新其他目标。
显式定位不证明知识相同或互相支持，语义主体仍须判断主题、必要条件、归属与缺口；综合须标 synthesis。
原证据校验、CAS、历史保存、幂等与中断恢复继续生效。无固定审核调用，未读范围不等于本次须补全全文。

## 可选按需供料与局部补读（2026-11-11）

`--strategy selective` 在同一Source/KU任务内选择性供料；默认`survey`及旧任务身份保持。
它不生成额外语义粗稿或必经审核，不自动生成Wiki/Graph/Hub。来源必须先可检索，
原有`none`仍为明确仅来源、零语义调用；此策略只用于已要求知识整理的Agent/API任务。

```bash
python3 .scripts/wg.py ingest --resume S-ID --profile source-first --knowledge api --strategy selective --focus '本次用途'
python3 .scripts/wg.py knowledge run S-ID --strategy selective --focus '本次用途'
python3 .scripts/wg.py knowledge prepare S-ID --strategy selective --focus '本次用途'
```

shared首包复用来源章节、限制和已有定位，以较小材料预算优先装入focus命中及邻域；
无命中时从开头两片进入，不填满全文巡览预算。仍最多5个已有候选，完整正文只读，
旧Raw暂不加载、`can_update=false`；`source_relation=same_source`明确同源，不冒充独立旧库佐证。
标题/行号是导航，真正依据只来自blocks；词面选片不保证语义充分或跨语言召回。
持久知识正文保留当前用途所需且已读原文支持的内容。focus中的“不推导/不展开”等处理范围
不作为来源事实；缺少的公式、证明或实验只有在当前用途确需时才补读，否则省去无关缺失断言。
必要核验限制限定于本次已读范围；来源明确的负面结论保留作者归属与成立条件。由语义执行者
判断，程序不以关键词裁决科学含义，也不为此新增必经审核。

语义执行者可直接返回原`knowledge-delta-v1`（含零操作），或在确有必要缺口时返回：

```json
{"schema":"knowledge-read-v1","source_locators":["当前reading.source_path#L100-L110"],"candidate_ids":[],"reason":"当前用途缺少的条件"}
```

每次1–5个当前来源精确行范围，单段沿原8000字符上限；也可仅以`candidate_ids`请求
已列出Statement的全部旧依据，两个列表至少一个非空、各最多5项。候选身份/快照、来源
版本、实际文本及原总材料预算继续校验。读请求不能携带operations，也不接受任意路径、网络
指令或本包外知识ID；Question仍只读。已有片段保留，完全重复请求拒绝，超预算/旧依据
不完整时整个补读不生效；不以截断证据授权维护。首版不在补读后重新全库召回候选。

每任务最多两次补读，API最多三轮语义响应，现有基础设施恢复另外计入请求账本；没有固定第二轮。
达到补读上限仍请求材料时返回partial及具体缺口，不伪造完成。完成任务重放零模型调用；
失败调用须显式retry，失败响应与费用保留。各轮响应先保存并绑定当轮packet hash，再补读或提交。
`api_attempts/api_usage`累计各轮和恢复调用；包外来源不因模型声称已读而获准引用。

Agent在任务protocol指定的`optional_read_request`写入读请求，执行task的`refresh`命令
（`knowledge expand KJ-ID`），读取新的输入包后继续原check/apply。Agent控制循环不进入API，
API任务不能经宿主expand隐式接管。原始packet.json及每次packet-N.json保留，state以原子
指针绑定当前包及hash；API响应和补读轨迹可恢复，不重复创建知识任务或重解析来源。
若维护已写入KU而任务回执中断，按该操作的摘要及紧邻修订识别本次已提交内容，允许恢复回执而不再升版；操作内容被改动或其后又发生并发修订时，不豁免旧候选快照检查。
存在delta或已提交操作时拒绝扩大材料；新的用途/额外范围另建任务。

此策略不能与`target/source-locator/compare-with`混用；已定位完整证据的任务沿原显式入口直接编译。
selective策略及指导参与本策略任务身份，不影响survey/定向身份；下节独立的自动候选排序版本另计。策略已可选运行，质量和总成本需按
真实用途验证，初稿结构通过与零新增均不自动证明语义正确；默认摄入未切换。

### 用途相关的旧知识优先进入材料预算

自动构包带focus时，在原已确认名称和同源优先层内，用现有字面匹配能力按当前用途重排
已召回的有限候选池，再用原文偶然出现的名称数量排序。survey与selective共用这一责任，
不增加模型请求或候选额度；来源主题准入、完整旧依据及修改权限保持。
这一排序只决定先读哪些对象，不判定相同、重复或科学支持，也不把任务文字当作Raw证据。
2026-11-12补充一条独立的非空focus字面查询，使调用方的中文/其他表达可以召回缺少来源语言词面的旧KU。
原来源主题、来源名称查询保持，查询文本去重，最多三次本地搜索；结果仍经原同源/主题/确认名称准入。
`candidate_retrieval.purpose_query`和`searches`记录实际发现词。focus只提供导航线索，不认证来源、重复或支持。
`purpose-recalled-candidates-v2`只加入受影响的自动、有focus新任务身份；无focus及显式选片/
定向维护的身份不变，既有任务仍按原ID恢复。它分别处理未召回和已召回后的用途排序；
不是翻译器，任务语言与旧KU也无词面交集时仍可能漏召，模型还可能忽略已提供候选。

## 有界材料与 Delta（默认survey）

包包含标题、章节结构、选定来源片段、未读范围及最多 5 个相关 Statement/Question 的完整表述。
Statement 候选加载可用旧依据；Question 仅保留完整问题和状态，不加载本次不能修订的旧依据。
正文优先包含开头、正式定理/推论/定义及邻近上下文、方法/模型/问题/结果/讨论等章节，随后分散取样。
v9 正式条目识别支持裸文本、Markdown 标题及加粗，以及常见数字编号。
有正式条目时先放首个开头片段；主定理/推论/定义的命中片段先于它们的后继和前邻片段，
随后处理命题/引理及其邻居，再考虑第二个开头片段。不再只取前两处正式条目，
避免先行背景挤掉后面的主要结果；仍受原预算限制，不保证每个结果或其全部条件入包。
没有正式条目时保留原开头选择。未对公式正确性、证明或语义依赖作程序判断。
v11 在旧候选及其完整依据装载后，利用未使用的预留预算补回此前放不下的正式条目及其相邻片段。
先前原文和旧依据不被替换，保留元数据余量及原最终预算检查；不重新提词或召回候选。
context_fill 记录实际新增行范围，coverage 仍为实际阅读范围。这可能增加实际输入 token，
并不扩大配置预算，也不保证全部前提齐全；无正式条目的来源不执行该补片过程。
这是确定性选择策略，未做全文话语角色分类。focus 同时约束语义任务并优先选片：
v14先匹配focus中明确写出的来源Markdown章节标题，去掉常见编号并规范化词项。
标题匹配保留词序和重复词，不复用FTS去重词项；多个章节共享词语时，后一个完整标题仍可识别。
分号（中英文）和换行分隔的请求分别匹配，不跨分隔符拼出标题；中英文混排保留原顺序。
至少含两个不同词项的完整标题匹配优先，重复同一个词不使单词标题获得额外优先级。
其余仍用原去重词项及本篇词项稀有程度排序，不改变普通词汇检索或预算。
这可区分明确指定的主文章节与词汇重合更高的附录变体，不进行翻译、语义角色判定或自动支持认证。
v17让所有明确点名的标题命中片段在原预算内获得优先机会；不足两片时才由普通词汇命中补到两片。
命中间仍按本篇词项稀有程度排序，全部主要命中先尝试入包，再依次加入相邻片段，
避免第三个明确章节被前两个章节的附带上下文挤掉。重复请求或同片段重复标题不增加槽位；
不同片段的同名标题仍可能各自命中，不推断它们是否属于目录、正文或重复内容。
标题不同写法可能只进入普通词汇匹配。预算未增加；较多明确章节会与邻域竞争，
可能遗漏部分章节或相邻条件，不能把获得优先机会理解为章节和条件完整覆盖。
v12在保留开头后先装这些明确任务片段及邻域，再按原顺序巡览正式条目与一般章节；沿用同一预算。
这避免无关定理在显式维护任务中挤掉已定位的方法或定义。无focus或无字面命中时，原选片顺序保持。
正式条目的剩余预算补片仍仅处理原定理邻域；不扩大窗口、不重新召回，也不保证焦点覆盖完整。
focus_selection 报告优先范围，coverage 报告实际送入范围；优先片段也可能因预算未进入。
无命中沿用默认选片，不声称支持跨语言翻译或任意片段定位。
保留来源标题/章节主题查询，无可用结构时取有界开头，过滤通用词；非空focus另作独立的候选发现查询。
另从实际入包的 focus 命中片段提取混合大小写名称/缩写，包括RAGTruth式大写前缀、Stop-RAG式连字符缩写段；
无已入包 focus 命中时使用实际所选来源片段。
排除公式、图片路径与 URL，按片段交错取最多 12 项；不从指令或旧依据提词。这是字面检索信号，非实体识别。
无focus至多两次、有focus至多三次去重后的本地查询，每路最多20个返回；排序后最多20项进入筛选，最后至多5个入包。
若实际入包的来源名称也被 focus 明确提及，旧标题命中这些名称的候选先于其他同源背景；
priority_names记录这个交集，任务指令中单独出现的名称不会获得该优先权或改变来源名称准入；focus的独立发现查询仍可使用任务词。
v16按完整名称边界与focus做不区分大小写的匹配，不再把Stop-RAG拆出的RAG当作明确请求。
有该交集时，仅名称路径的跨来源准入使用这些名称；没有时回退到全部已提取来源名称。
admission_names记录实际准入名称集合，同源、主题短语和显式目标路径独立保持。
这是字面约束，不解释否定或任务意图；focus含否定句中的名称也可能进入交集，宽泛focus仍可带入噪声。
未点名的背景知识可能不再凭名称入包；需要时可按需检索或显式定向维护，不承诺完整召回。
各优先层内仍先同源，其次来源名称在旧标题/正文中的精确匹配；大小写敏感且使用英文词边界，支持中文紧邻名称。
例如 ComplEx 不等同普通 complex。仅由名称召回的跨来源候选须在标题命中，
正文偶然出现 DOI 等缩写、又不符合原主题准入的候选不带入。
v15名称匹配与提取时的ASCII连字符复合名称边界保持一致：LLM不能仅因出现于EM-LLM中而命中，
RAG也不能仅匹配S2G-RAG的尾部；该边界同时用于候选优先级与准入。
独立LLM、完整EM-LLM、中文相邻的完整方法名仍可匹配；连接ASCII字母/数字才视为连字符名称，
例如`LLM - 方法`或`LLM- 方法`中的标点分隔仍合法。大小写要求保持，没有缩写黑名单。
基础名不单独匹配带前后缀的派生名称；同来源、主题短语和显式目标路径仍可独立准入。
普通FTS匹配算法与检索次数保持，不宣称语义身份判定；Unicode连字符归一化未纳入。
v10 主题准入保留中文三字短语及英文多词主题短语；单个普通英文标题词不再单独放行，
例如 quantum、discussion 等宽泛词只能帮助召回。admission_names中的来源名称精确命中旧标题仍可准入，
同源准入保持。该策略可能漏掉单词主题或跨语言相关知识，不代表语义相关性判断或完整召回。
candidate_retrieval 记录来源名称、priority_names、admission_names、各路查询/命中数/问题、合并池大小和弱匹配排除数量。
Statement 旧依据须完整可读才能更新；超预算或绑定失败时，回收只为该候选新装入的块，保留原本已有的来源/共享块，
将其 can_update=false 并注明 update_blocked_reason，供语义主体 defer；不以部分旧证据放行修订。
该回收让后续较小的完整依据包继续使用原预算。仍不保证窗口外、名称大小写变体或跨语言条目召回。
这只控制导航上下文，不裁决知识身份或支持关系；跨语言、改名概念仍可能漏召。
通用查询排序未改变；budgeted-named-sections-v17参与普通及定向新任务缓存键，旧KJ任务仍可按ID恢复。
定向材料包语义不变；策略升级本身不触发生产任务重跑。
输入约 14k 估算 tokens，加指令/覆盖元数据的有界余量；API 实际 token 数另外记录。
单次输出最多 4096 tokens、5 项操作。通常一次成功模型请求；基础设施故障允许一次受限恢复，
输出错误不自动启动多轮语义修复。实际调用数取 request history，不把重试算成免费缓存。

API 请求的任务编号由程序持有。`knowledge_compile_api.py` 以可信请求上下文绑定 `job_id`，
模型可省略该字段；即使模型复述了编号，也不以该值选择写入任务。其余语义字段不改写，
程序组装的完整 Delta 仍通过原 schema、片段/来源绑定、对象版本和提交校验；Agent 输出契约保持。
原始响应正文与 parsed 原样保存；新响应在模型输出之外附请求编号、packet 哈希和策略绑定，
从保存响应恢复时先匹配该绑定。旧无绑定响应须严格匹配原任务编号，不据文件位置猜补。
`request-bound-job-v1` 参与新 API 任务身份，已有任务仍可按 ID 查看和按原 Delta 接续。
这只消除机械编号复述的失败，不修补畸形 JSON 或认证语义质量；输入仍保留完整原材料包。

- `create`：保存可独立复用的概念定义、方法模型、科学结论或作者明示的问题，不强求数量。
- `attach`：同一知识补依据，也可补齐 unspecified 角色；保留已指定角色、旧正文、来源类型、论断强度和未决项。
  完全重复且无内容变化时复用当前版本，回执标记 `reused: true, unchanged: true`，不增加历史快照。
- `revise`：在完整候选/旧依据可读的前提下，修订同一命题；保留 ID，增加版本。
- `defer`：保留当前问题和原因，不强行生成或合并知识。

每条 Statement 聚焦本次用途需要的一个可复用定义、方法或结论，篇幅由含义与关键条件决定。
可保留帮助理解的少量背景，不要求精确排净无关信息或补齐作者/年份/期刊。
默认新Statement输出示例仅列id/action/title/statement/evidence/reason；kind默认statement，
content_role/source_question_status默认unspecified，origin_kind默认source_statement，claim_mode默认unknown。
分类未知可省略，实际重要缺口仍记录unresolved，正文保留作者归属、必要条件及原始不确定性。
综合知识需标origin_kind=synthesis，Question需kind=question，attach/revise仍需target和完整旧依据。
这些均沿用原validator缺省与事务规则；没有撤除来源检查或新增自动语义验收。
共享语义指导按用途选择增量，并要求每条在独立读取时交代对象、必要前提、核心关系与适用范围，
不以“同一/上述设置”替代本条条件；涉及标度时区分变化的量与固定的前提。
有 focus 时围绕该用途，无 focus 时选主要贡献；候选供比对维护，不是输出清单。
证明步骤、实验验证、软件链接与旁支解释默认留在 Raw，只有本身是所需独立知识时才单列。
四类角色保持可选，不设条数配额；这仍是语义指导，不是硬性长度、数量或相关性门槛。
必要条件和不确定性保留在正文。正式条件优先于摘要概括；单独列 unresolved 不能修复正文过强断言。
数学/观测结果与作者对实用性、因果、泛化的解释分开表述；解释在正文中标明作者归属与强度。
统计量的尺度结论不自动支持训练效率或优化收敛判断。该指导不等于硬语义验证或科学正确性认证。
旁支实验、完整证明和非必要公式留在 Raw。未读范围如实披露，候选相似度不裁决知识身份。

程序只接受包内 block ID、允许的候选 ID 和当前版本，不接受任意输出路径或自报来源哈希。
每条新建/更新必须有当前 Source/parse 的依据；旧候选证据仅用于维护上下文，
不能单凭旧来源重复生成“本次新增”。这一既有校验要求也在语义指令中明确给出。
检查身份、原文绑定、内容结构和可恢复性；科学支持程度由宿主/API 判断，没有固定审核模型。
Question 只允许 create，沿用 Frontier `captured/unassessed/unverified`，不自动回答或判定科学状态。
已有 Question 作为只读候选帮助识别复用；遇到同一问题应 defer 并指明 Q-ID，交原生 Frontier 维护。
这不是程序保证的语义去重，候选漏召或模型误判仍可能重复；Delta 不直接修订/合并已有 Question。
支持关系必须匹配本条的具体条件；不同假设的特例不能算作同一命题的支持。

## 存储、版本与恢复

```text
academic/knowledge/units/KU-<24hex>.md       Statement 主记录
academic/knowledge/history/KU-<24hex>/<revision>.md
academic/knowledge/index.sqlite            可重建词汇索引
academic/frontier/questions/Q-<id>.md       原生 Question，无第二主存储
temp/knowledge-compile/KJ-<24hex>/           packet/state/delta/model-response
temp/knowledge-locks/                       单宿主文件锁
```

Markdown 主记录使用 JSON-compatible YAML frontmatter 加完整正文。依据绑定 S-ID、
原件哈希、不可变 P-ID、文字哈希、Raw 行地址与片段哈希；Statement 额外记录判断主体和被判断版本。
新表述重新选择本次全部必要依据，旧版支持判断不自动继承。Question 使用原生 `answer_history`，不虚构 KU 修订号。
修订未提供或显式 null 的 content_role 保留旧值；null 的 source_question_status 视为 unspecified。
Statement 不接受 addressed/left_open；Question 的用途固定为 research_question。
KU、流程回执和索引均不替代 Raw；这些 SQLite 文件不保存公共图关系。

更新持有对象锁，以旧 revision 和快照哈希做 compare-and-swap。先保存旧版，再原子替换当前文件，
最后更新索引；索引失败返回 `index_pending`，主记录保留。apply 重放补索引，不再增加版本。
任务中每个成功对象即时保存；其他对象失败无需整篇重做。
已经提交的操作通过操作 ID/内容摘要冻结；修复只改尚未提交/暂缓对象，已提交对象需新修订任务。
提交主记录后回执中断时，从主记录恢复该操作；同一操作换内容会被拒绝。

只有 attach 启用无增量检查：在对象锁内通过 CAS 和全部依据绑定校验后，比较完整正文、标题、
来源类型、论断强度、角色、未决项与依据绑定/关系。依据顺序、重复项和执行者记录不构成知识增量；
旧依据须已对应当前 KU 版本。新依据、新解析版本、关系变化、新增未决项、补齐未知角色仍正常升版。
此机制只识别机械相等，不认证语义等价，不放松来源校验；create/revise 保持原行为。

无增量时 KU 主记录、历史、判断主体及时间保持，复用索引发布机制和 saved/index_pending 状态。
操作摘要只保存在现有 job 回执，不为记一次重复尝试而改写 KU operations；回执保存后仍禁止换内容。
若无写入操作的回执中断，重试重新检查快照和来源；期间发生有效修订则拒绝旧包，需刷新任务。
这与实际写入后凭 KU operations 恢复不同，不新增持久化状态或模型调用，也不撤销历史空版本。

任务缓存键绑定来源/解析、协议、选片策略、指令版本、focus、执行主体及预算。
相同任务重放不重新读新候选、不调用模型；`--refresh` 才刷新候选快照。
API 响应先保存再发布 Delta；有成功响应时直接恢复，避免重发请求。
这些机制提供单宿主的独立对象恢复，不宣称跨文件/数据库事务或跨机器分布式锁。

## 查询、验证与边界

`knowledge_search/knowledge_read` 已接入 `wg`、API query orchestrator 与 DSH 白名单。
Statement 使用增量 FTS，Question 直接只读原生 Markdown；当前仅 academic/public，private 明确拒绝。
返回当前正文、content_role、问题来源处境、状态、未决项及依据问题。读取 KU 不计入 Raw citation 证据；实际答案须按相关 locator 回读。
历史解析仍可定位，解析更新会披露 `historical_parse`；失效绑定不能静默替换为最新行号。
当前 KU 是词汇召回，已接入 [N3 按用途联合检索](unified-retrieval.md) 的混合结果，
在原有候选窗内以完整表述的短语/整词与不同词项覆盖排序，再返回有界摘录；匹配不代表事实支持。
搜索候选另带至多3条`evidence_preview`，原样投影locator/relation/assessed_revision及总数/截断标记；
沿记录顺序，不按支持关系删去反驳或未知关系。此处不读Raw、不认证绑定，地址可供直接read_raw；
完整条件、冲突或其余依据按需knowledge_read，所有实际来源读取仍校验绑定。无依据则空预览。
可与来源、Wiki/Graph 一起用于普通查询。KU embedding、自动维护调度和全库迁移尚未实现。

执行 `test_knowledge_units.py`，并按工程影响卡运行来源、Frontier、查询、入口回归及工程图校验。
自动产物在少量真实使用中曾出现实质语义错误，不能以结构通过证明质量无损。
真实来源与使用评估保持本地；公开回归验证确定性机制，不构成大语料科学质量基准。
