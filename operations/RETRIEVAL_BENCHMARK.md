# 只读检索基准与邻居预算

## 边界

这是 shared 确定性工程工具，宿主解释结果；不持有语义控制循环、不调用模型、embedding、DSH 或网络，不访问 private，不建立索引，也不恢复暂停的论文复现。临时报告不是事实源。

基准只测 Wiki 字面召回、Graph 字面搜索、Graph 邻居导航；不是完整 Hybrid、Agent 问答或科学答案质量评估。测量含追踪开销、不含模块导入。第一轮不等于冷缓存，同进程重复也不等于独立生产负载。

## 运行

```bash
python3 .scripts/retrieval_benchmark.py --repeats 2
python3 .scripts/retrieval_benchmark.py --suite real --repeats 2
```

重复次数限 1–5；只允许 `curated` 或 `real` 两种 suite，不接受任意 manifest 路径，每组最多40条探针。默认 curated 固定读取 `operations/config/retrieval-benchmark.json` 的20条人工功能探针；real 固定读取 `operations/config/retrieval-benchmark-real.json` 的15条公共历史原问句。expected 为经页面身份和导航关系检查后固定的公共 locator，不从当前检索输出生成。已有页面路径中的年份不作为书目信息核验。

报告与输入副本写入唯一新建的 `temp/retrieval-benchmark/<run-id>/`。运行前后校验 manifest、源码、实际图实现、图文件（包括存在的 WAL）和公共 Wiki 内容指纹；检测到变化就拒绝可比较报告，不改 Raw/Wiki/Graph。比较须确认 manifest 和语料指纹相同，并查看源码差异；不得拿语料更新前后的时间差当优化收益。

## 指标定义

- `elapsed_ms`、median、p95：每个动作在当前机器上的计时；样本少，p95只是经验分位。
- `sql_selects`：追踪的 SELECT/WITH 语句数，不代表实际 SQLite 扫描页数。
- `wiki_reads` / `unique_wiki_files`：`Path.read_text` 的逻辑读取次数/不同 Wiki 文件数，不代表磁盘 I/O；预先计算指纹不计入动作计时。
- `output_bytes`：完整序列化响应的 UTF-8 字节数；`estimated_tokens` 为字节数除4向上取整，不是 tokenizer 计数或实际费用。
- `target_hit`：至少到达一个固定导航目标；`target_recall`：已到达固定目标数/不同目标数。Wiki/search 看返回候选；neighbors 只看返回边的非起点端点，不能用没有对应返回边的旧版节点列表充数。`node_list_count` 另报告节点列表大小。
- `truncation_reported` 与 `truncated` 分开：未提供截断元数据记为未知，不解释为完整。截断率不能充当召回率。
- `stable_responses`：同一运行的重复响应哈希一致，只证明确定性，不证明科学正确性；Raw locator 到达也不等于已经阅读或核验 Raw。

## 邻居遍历预算

`query_graph.py neighbors` 的默认预算为非起点节点100、去重边300、读取边行1000。可显式传 `--max-nodes`、`--max-edges`、`--max-scanned-rows`；硬上限为1000、3000、10000，`--depth` 限0–5。非法参数返回错误，不静默扩大或钳制。`--top-k` 仍只裁剪最终边，不充当遍历预算；节点详情只返回最终边端点。

```bash
python3 .scripts/wg.py neighbors academic/wiki/papers/<id> --depth 2 --max-nodes 150 --max-edges 500 --max-scanned-rows 2000 --top-k 30
```

按知识边/相似边、confidence、object status 排序；同档优先较近 hop，再按路径稳定排序。profile/family、Hub 排除与相似边动态阈值沿用原规则。端点类型/status 随边查询读取，节点详情分批读取，避免逐边/逐节点冗余 SELECT。资源预算下的相似边阈值只基于已取样边，结果必须视为部分导航。

JSON 返回 `budget`、`work`、`truncated`、`traversal_truncated`、`truncation_reasons`、`remaining_frontier`。`output_top_k` 是输出裁剪，其他预算原因表示遍历未完成。`remaining_frontier` 只是定位未完成展开的提示，不是续跑游标；重复原命令不保证继续推进。需要更多关联时，明确定位具体节点或在硬上限内提高预算；来源边也可通过 `relations --predicate 来源` 精确定位。文本输出把截断提示放在边列表之前，超过20条另提示文本展示限制。

`work.scanned_rows` 为预算内取出的边行（重复 incident row 也计数）；一次最终 lookahead 可使 `fetched_rows` 多1。该预算限制返回行数和 Python 展开量，不是数据库内部排序、CPU时间或所有输出字节的硬上限；完整节点 gloss 等字段仍沿用旧契约。Agent CLI、WG 与 API 共享默认限制；`query_actions.graph_neighbors` 可显式传预算，DSH 工具面沿用默认预算。

## 验证与后续

```bash
python3 .scripts/test_query_graph_temporal.py
python3 .scripts/test_retrieval_benchmark.py
python3 .scripts/test_wg.py
python3 .scripts/engineering_graph.py validate
```

先按失败探针分析候选排序和逻辑重复读取；只有数据表明有必要时再决定 Wiki 索引方案。短中文词、别名、跨语言标题和固定目标召回必须先有回归，不把速度提升和召回改善混为一谈。

## 首次测量：2026-10-11

同一 manifest、Wiki 与图文件，使用同一指标实现比较 hash 核对过的旧图代码快照与新实现。当前20条用例集中于 academic，不代表全域用户问题分布。以下为同进程第二轮结果，不是冷缓存或生产端到端性能：

| 指标（4条邻居探针） | 修改前 | 修改后 |
| --- | ---: | ---: |
| 平均 SELECT 次数 | 575 | 12.5 |
| 平均响应 UTF-8 字节数 | 56514.25 | 18687.5 |
| 动作中位耗时（ms） | 7.799 | 6.357 |
| 动作经验 p95（ms） | 27.53 | 11.344 |
| 固定目标到达率 | 2/4 | 2/4 |

20条探针的逐条固定目标召回指标未变化；Wiki 目标进入前8的比例为5/8，Graph search为6/8。这不是回答正确率，也不说明其他目标不受预算影响。旧 neighbors 没有截断披露，新版4条中2条明确报告截断；体积降低部分来自主动限制展开与清除没有对应返回边的冗余节点，不能全部归因于计算优化。

Wiki 每个探针逻辑读取1214次、涉及607个不同文件，第二轮中位约393ms。这支持下一批优先分析重复读取与候选排序，而不是立即引入索引。这里仅记录问题，不在本批扩面修复。

完整对照：`temp/retrieval-benchmark/20261011T120133Z-5fa9f191/comparison.json`。其中分别指向旧实现与新实现的报告、实际执行图源码指纹，并说明早期探索报告曾把旧版无对应返回边的节点算作到达，不能用于质量对照。基准默认不运行；它不会增加正常 query 的扫描或模型调用。

## 第二批：Wiki 单次读取与标签优先排序

2026-10-11，先用独立构造用例复现三个责任边界内的问题：每个 Wiki 文件读取两次；图的完整标题/alias 命中被后续词频候选覆盖；重复出现一个普通词压过多个不同查询词项。修复位于 shared `query_actions` 与 Hub profile 解析入口，没有用论文名称或 gold 路径特判。

每个文件的本次文本同时供 Navigation 和 `read_paper_profile(page,text=loaded_text)` 使用，后者沿用 Wiki section/citation 解析器。图中 title、alias、status 保留；完整标签、标签短语、导航短语、词项召回依次排序，同档看不同词项覆盖、标签命中数和稳定 path，不按重复词频累计。`match_kind`、`matched_terms`、`term_coverage` 解释词汇匹配，不能作为身份、语义 confidence 或事实核验。

同一 manifest、Wiki/Graph 与指标代码的第二轮对照（8条 academic Wiki 探针）：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 每次 Wiki 逻辑读取次数 | 1214 | 607 |
| 每次不同 Wiki 文件数 | 607 | 607 |
| 固定导航目标进入前8 | 5/8 | 8/8 |
| 动作中位耗时（ms） | 396.267 | 310.6325 |
| 动作经验 p95（ms） | 430.282 | 315.442 |
| 平均响应 UTF-8 字节数 | 5044.625 | 5680.375 |

改善的探针是 `wiki-mps`、`wiki-tpu`、`wiki-cjk-short`。其余 Graph search/neighbors 探针的响应哈希不变，同进程重复结果稳定。响应略增是保留规范标题并增加匹配解释字段的代价；计时仍含追踪开销，不代表生产端到端耗时，8/8也不代表全域或答案正确率。Content 不参与此召回评分；实际机械读取仍是全文，每次仍扫描当前范围的 Wiki 文件。

完整对照与前后源码指纹、快照：`temp/retrieval-benchmark/20261011T122438Z-ec942c6a/comparison.json`。新增16项单次读取、标题/alias、覆盖率、中文短词、字面通配字符、图回退、新鲜性、只读与范围隔离回归；Hub、摄入、private、API/WG、提示词审计与工程元图校验通过。没有新增索引、持久缓存、模型调用或论文复现任务。

后续先扩大真实问题覆盖，再判断索引必要性；不因这8条探针全部到达就宣称系统已具有完善语义理解。

## 第三批：真实问句导航验收集

2026-10-11，检查公共 `academic/wiki/outputs/query-log.jsonl` 的59行与 `admin/wiki/outputs/query-log.jsonl` 的5行；teaching/business 没有该入口日志，未读取 private。64条记录中，去掉7条完全重复问句，57条不同问句中冻结15条、排除42条。这是人工目的抽样，不代表生产流量分布。13条出处在 academic、2条出处在 admin；按实际导航范围分为 academic 12条、admin 3条，不把日志文件所在域直接当作证据域。

原问句不删工程用语、不修 `CCCF` 拼写、不提前替换成关键词。每条保存公共日志地址、1-based 行号、原时间及原 JSONL 行的 SHA-256（UTF-8、不含换行）。这些字段只说明历史问句出处，不是事实依据，运行也不会回读日志。审核时日志中 `pages_read` 只用于定位，再独立检查下表的实际 Wiki 身份、主题及 sources；先冻结目标，再执行被测检索。不把历史回答或本次命中项当 gold。

抽样时日志文件 SHA-256：academic `0949ea72ae0e126b1b30853d44ed733d5fed531320caee5ea57c8e13504c3446`；admin `4e472756a205122de2db7573c6456b55ec24ae0cf71c132fea0791556fd8cc17`。后续日志追加不自动改验收集；报告固定所选 manifest 指纹及原样副本。

### 独立目标审核

以下只审核“值得初步到达的页面”，不是完整答案支持集。完整 expected 在冻结 manifest 中；Wiki 来源在页面 frontmatter 的 `sources`，本批不作 Raw 科学结论核验。

| 用例 | 日志行 | 独立审核位置与边界 |
| --- | --- | --- |
| `real-tnr` | academic:1 | `academic/wiki/papers/evenbly-2015-tnr.md#navigation`；标题、作者/年份字段及 Raw 来源指针。 |
| `real-yu-yao` | academic:2 | `academic/wiki/authors/cnu-yu-yao.md#研究进展`；姓名、导师与研究主题；该页没有 Navigation，仍是合理身份入口。 |
| `real-wu-xi-topic` | academic:3 | `academic/wiki/authors/cnu-wu-xi.md#navigation`；研究方向对应，历史日志没有读该 Wiki 也不影响独立选定目标。 |
| `real-wu-xi-date` | academic:4 | 同一吴习页面及其会议 sources；仅评价人物入口，不认为到达该页就能证明合作开始时间。 |
| `real-interpretability` | academic:8 | `academic/wiki/discussions/经典AI可解释性与QIC讨论.md#navigation` 与 `academic/wiki/papers/2021-entanglement-feature-extraction.md#navigation`；主题关联，不是§8.1的穷尽文献表。 |
| `real-conferences` | academic:32 | 第二届、第三届会议页面 `#navigation`；第三届包括暑期学校。第一届不在本次固定目标中，不能用本指标判定“全三届已摄入”。 |
| `real-autumn-plan` | academic:35 | `admin/wiki/meetings/2026-07-07-党政联席会-秋季学期工作要点.md` 的科研管理板块；工作计划的公共背景入口，不涵盖项目工作区的全部计划。 |
| `real-prl` | academic:37 | `academic/wiki/papers/2026-mpe-prl.md#navigation`；论文身份与 state-preparation 主题；不评价报告生成。 |
| `real-qml-bottleneck` | academic:43 | `2024-bai-nll-scaling`、`2025-bai-orthogonality-catastrophe`、`2026-icomputing-qml-orthogonality` 的论文页面 `#navigation`；NLL 标度、正交性及任务/泛化边界。 |
| `real-contraction` | academic:44 | `gray-2021-hyper-optimized-contraction`、`markov-2008-simulating-quantum-computation`、`zhang-arbitrary-tn-contraction` 的论文页面 `#navigation`；路径、树宽与低秩近似三个不同入口，不混成单一复杂度断言。 |
| `real-fermions` | academic:50 | `kraus-2010-fpeps`、`pizorn-2010-fermionic-implementation-projected-entangled` 的论文页面 `#navigation`；局域性、符号处理、病态条件及优化问题入口。 |
| `real-circuit-depth` | academic:53 | `academic/wiki/papers/2021-adqc.md#navigation` 与 `academic/wiki/papers/2026-mpe-prl.md#navigation`；线路层定义和制备任务入口，不宣称一般最小深度定理。 |
| `real-tensorizing` | academic:57 | `academic/wiki/papers/novikov-2012-tensorizing-neural-networks.md#navigation` 与 frontmatter；路径中的2012不作为发表年份依据。只做只读存在性导航，不恢复暂停的复现。 |
| `real-cccf` | admin:1 | `admin/wiki/profile/因私证件与社会兼职登记-20260716.md#兼职信息`；正文有 CCF 对应兼职，保留原问句 CCCF，不把登记页标题中的“因私”误当 private 域。 |
| `real-budget` | admin:4 | `admin/wiki/activities/青梧工作室2026年度项目预算明细.md#项目预算明细`；标题、预算主题与2026来源；历史“今年”保持原意，不随运行日期改写。 |

### 去重与排除

重复问句保留最早行：academic:46→43，45/47/49→44，51→50，54→53，56→23，共7条。下面42条不同问句不进入本次计分，不把它们当作负例、知识不存在或检索失败：

| 日志行 | 数量 | 排除原因 |
| --- | ---: | --- |
| academic:5/6/7 | 3 | 通信邮箱/Raw PDF 细节，或项目关系的广泛论文汇总；本批不构建对应证据支持集。 |
| academic:9–26 | 18 | 摄入、维护、worker、格式、身份融合或节点类型的工程诊断；不少依赖“这次/两者/第一条”等上下文，不是本次 Wiki 初步导航验收。 |
| academic:27–31 | 5 | 项目预算测算/草稿评价，或日志治理材料查找；需要工作区上下文或工程记录。 |
| academic:33/34/36 | 3 | 摄入复盘、成果归档检查，或跨论文/专利/软著的年度写作导出。 |
| academic:38–42 | 5 | 报告修改、工程诊断/回放或使用反馈复核；不把工程回放混充自然用户问题。 |
| academic:48/52/55 | 3 | 指代“这篇论文”的建议、无独立问句的引用片段，或摄入成本复盘。 |
| academic:58/59 | 2 | 论文复现规划或科研代码执行证据验收；仍暂停，不扩大本批范围。 |
| admin:2/3/5 | 3 | 主持人介绍写作/多源改写，或项目申请书编辑；不由一次 Wiki 候选到达评价。 |

`real` 的所有用例只测单次 `wiki_recall`，默认 top8；宿主实际 query 会先理解并分解问题、选择词项/alias、再走图或 Raw，因此原问句单步失配不能等同系统回答失败。多目标的 `target_hit` 是“至少一个到达”，`target_recall` 是该条固定目标的覆盖比例；需要同时读两项，不能把任意命中当完整召回。无 negative/无 gold 测试，也不测 hybrid、图关系到达、Agent 分解、Raw 核验与最终答案。

本批仅建立可复用离线验收，不改变正常 query 入口、默认提示词、摄入流程或运行开销。先区分漏召回与扫描负担，再决策；给同一词汇排序加索引不自动解决跨语言、缩写、问句分解或知识缺口。

### 实测与下一步判断

最终报告：`temp/retrieval-benchmark/20261011T124918Z-3bb5d06a/report.json`。两轮30次动作响应稳定，所选 manifest、公共 Wiki/Graph 在运行前后指纹不变；15条原问句、时间及源记录哈希也独立核对一致。同进程第二轮如下，不能与上一批计时直接相减宣称加速：

| 范围 | 用例数 | 至少一个固定目标进入前8 | 全部固定目标进入前8 | 动作中位 ms | 经验 p95 ms | 每次 Wiki 逻辑读取 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| academic | 12 | 8/12 | 6/12 | 161.8075 | 168.096 | 607 |
| admin | 3 | 3/3 | 3/3 | 28.533 | 29.119 | 105 |
| 合计 | 15 | 11/15 | 9/15 | 161.029 | 168.096 | 平均506.6 |

逐问句固定目标覆盖比例的宏平均为64.44%；23个逐用例目标槽位到达13个，这两个口径不能混用。每次1条 SELECT，平均响应6911.8 UTF-8字节。Wiki channel 没有披露截断字段，15条均记 unknown，不将其解释为完整结果。这里仍不是答案准确率。

4条没有固定目标到达：余遥课题、英文可解释性问句、2026 PRL 定位、费米子困难；另外 QML 瓶颈与收缩复杂度各只到达3个固定目标中的1个。返回项中可能仍有其他相关论文，特别是费米子问句返回 Corboz、Krumnow、Ferris 等已有页面；固定 gold 并非全部有效答案集合，不能据 miss 宣称所有候选无关。

冻结目标后另做 top20 诊断，不改 top8 主指标或 manifest：余遥位于第18；Gray 收缩论文第16、Zhang 第11、Markov 第1；NLL 标度第19、编码扩展版第7，其余缺失目标仍未进入前20。因此全局扩大候选只补回部分固定目标，并不能代替查询理解。

独立查看目标的实际匹配解释：余遥页无 Navigation/论文 profile，整问句只匹配姓名；可解释性讨论页只匹配 `ai`，特征提取论文对原英文问句没有词项匹配；PRL 目标只匹配 `20/2026/prl`，作者与 venue 不在本次 Navigation/标签评分入口；费米子目标有词项命中但被覆盖更多词项的其他页面排在后面。这支持进一步检查实体优先、字段覆盖与跨语言入口，尚不足以决定统一的新排序或新索引方案。

原20条 curated 对照：`temp/retrieval-benchmark/20261011T124921Z-9eab1355/report.json`，与第二批最终报告逐条响应 SHA-256 完全一致，Wiki 仍8/8到达。新增 suite 没有改变生产召回逻辑。13项基准回归、Wiki 召回、Graph、private、API/WG、工具注册、提示词审计及工程元图验证通过。

**决策：暂不建立索引。** 当前小样本显示学术动作约162ms、行政约29ms，仍有每次扫描负担，但没有端到端高并发或生产瓶颈证据。下一步优先用这15条固定问题验收宿主正常的问句分解、实体/作者定位、跨语言 alias 与图导航，记录具体检索动作与固定目标覆盖；控制循环仍由 Agent 持有，不能由脚本模拟语义推理，也不新增模型 judge。只有确认漏召回延续到实际流程后，再对责任组件加通用回归与修复；不要为15条题目写答案或别名特判。

## 第四批：宿主检索与局部取证路径验收

2026-10-11，宿主对同一15条问题实际执行已有 shared 查询动作，首轮策略先记录，再看返回结果决定后续动作。只使用 graph search/relations、Wiki 字面召回、section 与 Raw 读取；没有修改检索排序、增加 alias、建立索引或新查询编排器。路由按 `start → evidence → 按缺口 continue → answer` 加载；本次进程显式 `QUERY_BACKEND=agent`，没有改全局 backend 配置，也没有使用 API/DSH、embedding 或模型 judge。既有查询动作仍负责只读图连接、范围与来源定位校验；临时记录工具只捕获实际调用及返回，不作语义决策。

报告：`temp/retrieval-benchmark/agent-20261011T131253135Z-df285ecb/report.json`。同目录保留冻结 manifest、先验策略、72个逐动作 JSON、输入前后指纹、宿主操作问题及 Raw reader 检查。72个记录包括61次共享查询动作和11次已知 Raw 文档包内的定位辅助；每题记录3–8个动作。另有3次无后缀来源定位失败及1次二进制文本辅助探测失败，不混入已完成动作数量，限制与诊断单独披露。没有把工程验收题作为已完成事实问答追加到生产 query-log。

**非盲测边界**：同一宿主此前已经看过目标审核与上一批结果。这是有目标暴露的开发性路径验证，不是独立测试集、统计性的 Agent 胜率或泛化能力评估。程序只机械计算实际返回路径与冻结目标交集；宿主未输出15份完整事实答案，也未验收答案综合、完整槽位覆盖或语义正确性。累计多动作到达不能与单次 Wiki top8 的命中率或动作时延作同口径对比。

### 实际补回的导航路径

| 上一批单步不足 | 本轮实际宿主动作 | 固定入口到达 |
| --- | --- | --- |
| 余遥课题 | 从问句提取姓名，用 graph search 定位 People；没有 Navigation 时改读工具列出的“研究进展”。 | 1/1 |
| 英文可解释性问题 | 将主题译成“量子/人工智能/可解释性”，读讨论的白箱结构；再查可解释性与特征提取主题，读返回论文导航。 | 2/2 |
| 2026 PRL 定位 | search 冉仕举人物，沿 `作者` 关系找到2026量子态制备论文，再读 Navigation 与 Raw 方法片段。 | 1/1 |
| 概率性量子机器学习瓶颈 | 用题中主题的英文 `quantum-probabilistic` 查图，而不是把整句功能词混在标题匹配中。 | 3/3 |
| 收缩复杂度 | 保留“张量网络/收缩/复杂度”技术词，移除问句功能词。 | 3/3 |
| 费米子困难 | 用英文词干 `fermionic` 查图，在返回候选中阅读 fPEPS 导航与 Raw。 | 2/2 |

累计发现15/15题的全部固定导航入口、23/23个逐题目标槽位。这里并未声称全部目标都逐一读过 Raw，也不是15题全部可完整回答。其他题保留其简单身份、会议、工作计划或预算入口，没有为了追求指标补写专用答案/别名。

### 真正需要关注的是证据桥

每题尝试一个有具体位置的 Raw 片段：14题成功读取，1题失败。成功只说明该片段可达，不自动保证转录准确、科学结论成立、年份/刊物已核实或多源问题答全。部分旧 Wiki 章节无 Raw 脚注时，现有 `relations --predicate 来源` 可以定位来源包；图节点常省略后缀，回到 Wiki `sources` 的完整 `.txt/.xls` 文件名可避免猜错载体，不需要扩大到全库或 private。

唯一最终 Raw 读取失败是 `real-cccf`：`admin/raw/profile/处级领导干部社会组织兼职情况统计表-20260716.xls` **原件存在，但没有同 stem Markdown companion**。已有 reader 不支持直接对该二进制表按 Wiki 中的“兼职信息”标题取证，返回失败；不将 Wiki 表格冒充已核实的 XLS 原文，不声称 Raw 丢失，也不绕过来源门或读取 private。原件/companion 存在性及失败动作71记录在 `raw-reader-check.json`。

另外保留三个明确的回答边界：吴习在0609会议出现不等于科研合作从该日开始；第二/第三届固定入口到达不等于全三届已摄入，本轮 Raw 片段只读第三届；行政背景工作要点不覆盖项目工作区全部计划。PRL 个案读到方法片段也不等于已核实期刊发表信息或完成20分钟报告。

本轮也保留宿主自身的低效动作：预览函数只展示 nodes/candidates、没有计 edges，宿主一度误以为作者谓词无命中而额外枚举所有关系。实际动作30已经返回目标论文，动作31是多余探索，**不是 query_graph 的谓词缺陷**。没有基于误读修改生产代码。无后缀的0701来源还曾落到同名已知资料包目录，其局部匹配只作为定位线索；最终证据读取使用 Wiki sources 明示的原始 `.txt`。

输入前后公共 Wiki/Graph 与基准源码指纹不变，语料与第三批单步报告一致。临时轨迹和报告仍只服务工程诊断，不替代 Raw、Wiki 或 graph.db，也不增加日常查询的扫描、缓存、模型调用或后台任务。暂停的论文复现保持暂停。

**下一步判断**：继续不建索引、不因这组暴露目标的开发题修改全局排序。优先对已确认的旧 XLS 文本 companion 缺口使用现有受管摄入事务补齐文本及来源绑定，按需为对应 Wiki 节补精确脚注，再回放原 CCCF 问句；本批只记录候选，不直接写 Raw/Wiki。完整答案质量与未见问题的泛化验收仍是后续工作，不能用本次累计导航23/23替代。

## 第五批：补齐旧 Excel 证据桥（2026-10-11）

针对第四批唯一最终 Raw 读取失败，复用 `ingest_document` 原生 Excel 提取和 v2 manifest/finalizer，增加面向已有公共域 `.xls/.xlsx` 的窄修复入口。只有原件已在同域 Wiki `sources` 声明、companion 与 sidecar 均缺失且正文无需事实修正时才允许进入；不新建重复 Wiki，不覆盖既有 Raw。宿主收到 `agent-task-v1` 后只补脚注，程序重新提取、核对原件/旧 Wiki/暂存产物和双哈希绑定，经原 `ingest_check` 提交；校验、日志或回执失败补偿回滚本次新增 Raw 与 Wiki/日志。没有模型调用、全库回填、持久检索索引或后台任务。

事务 `companion-a2e680c72fa14e4cab3c0f2002ec9db4` 已完成，回执为 `temp/inbox-receipts/companion-a2e680c72fa14e4cab3c0f2002ec9db4-46e0af1d37d9471c9e9cf5bcf729e443.json`。原件 `admin/raw/profile/处级领导干部社会组织兼职情况统计表-20260716.xls` SHA-256 保持 `56073e503fc0accfee5d0ec0e05194db2bc7338fd5e18edfe7cb6f6aff3bf856`；新增同 stem Markdown 和 `.xls.source.json`，绑定状态 `valid/hash_bound`。Markdown 保留工作表、原始 R 行号、空值、可见性和合并范围，XLS 只读取保存值，不重算公式。对应 Wiki 仅增加 `[^ccf-xls]` 与 `updated`，证件部分及原事实均保留；公共 `graph.db` 字节哈希不变。完成后的重复 resume 返回同一回执，不重复写日志。已运行局部 `ingest_check --graph` 并同步 page-catalog。

原字串 **“查一下我在CCCF的学术兼职”** 回放记录在 `temp/retrieval-benchmark/cccf-companion-repair-20261011/report.json`：3 个既有 query 动作，原问句 `wiki_recall` 第 2 个候选即目标页，`read_section 兼职信息` 返回新脚注，`read_raw` 实际读 `admin/raw/profile/处级领导干部社会组织兼职情况统计表-20260716.md#L17-L20`，同时保留 `.xls` 为原始事实来源。L17–L20 对应原表多级表头 R5–R7 与本人数据 R8，可支持组织、职务、起止与取酬情况。未修改检索词、排序或查询日志；没有因修复一个已知样本而重测、改写第四批历史结果或宣称整体答案准确率提高。

**事实边界**：原表写“中国计算机学会”，不写 CCCF；答复应说明“若 CCCF 指的是本页所记的中国计算机学会（CCF）兼职”，不要把用户缩写固化成新组织事实。原表记录量子计算专业委员会执行委员、`2025.07 至今`，其中“至今”只属于 2026-07-16 来源时点，不能外推为 2026-10-11 仍在任。未核验签章或当前任职变更。

**验收与保留项**：回归覆盖原件/旧 Wiki 漂移、companion/sidecar 篡改、语义改写、失效 locator、校验及回执失败回滚、已有产物拒绝覆盖、completed 幂等；通过 `test_ingest_document`、`test_ingest_admin`、`test_ingest_pipeline`、`test_inbox_finalize` 与工程元图校验。局部自检 ERROR=0，仍有 3 项旧页警告：缺 Navigation、缺 Content、悬空 `[[个人简历]]`。本轮未为消除警告改写旧页或扩面修复，后续应独立处理；暂停的论文复现保持暂停。

## 第六批：新增问句的答案支持验收（2026-10-12）

从公共历史日志选择6个未进入既有15题导航集的原问句：academic 第5、7、27、48行，admin 第2、5行。只使用问句、原时间与JSONL行哈希，不使用历史回答、pages_read、覆盖清单或notes作答案/定位依据。首次动作前冻结问句与答案槽位，执行当前公共库查询，不模拟历史时点语料；“去年”依2026年原日志映射为2025年。报告在 `temp/retrieval-benchmark/answer-acceptance-20261012/report.json`，人类说明在同目录 `README.md`，每题有独立 `answer.md` 和 `audit.json`。

这是一小批目的抽样与宿主自审，**不是独立盲测或统计准确率**。其中两题保留原问句的指代缺失，检验是否会凭搜索命中猜对象；“未进入既有集”不等于宿主或系统从未见过题目，也不等于新问题生产分布。

| 问句 | 本轮结果 | 证据与限制 |
| --- | --- | --- |
| 研究量子纠缠的张士欣的单位与邮箱 | 限定性答复 | 核验 Shi-Xin Zhang 论文的2026-07-07机构署名；中文身份对应、本人邮箱与查询日的工作单位未确认，不拿另一作者邮箱替代。 |
| 本人哪些论文与指定非平衡动力学项目密切相关 | 限定性答复 | 核验2021 ADQC原文署名、MPS态制备方法及纠缠结构讨论；项目原文未定位，仅给条件性方法候选，不将状态制备自动等同于非平衡演化，不声称完整发表清单。 |
| 参照去年例子评价2026理论物理专款讲习班预算 | 无可行动候选 | 未定位2026草稿与2025例子，拒绝以其他活动预算或日志作证。没有候选不等于资料不存在；需要具体文件。 |
| 新生开学典礼主持人介绍 | 来源支持的完整草稿 | 从行政原始申请表PDF第20页核验职称、单位与研究方向；不加入不需要的排名、荣誉或论文数量。 |
| 两位合作者参与申请，哪些位置需改 | 请求澄清 | 缺项目与版本，不猜申请书；用户所说“同意”不冒充库内Raw已确认事实。 |
| 这篇论文哪些设计值得ASKS借鉴 | 请求澄清 | 缺论文指代，不凭检索命中替用户选论文。 |

合计1题完整、2题限定性答复、1题没有可行动候选、2题需澄清。录制34个共享查询动作（2次失败），另有源内定位与元数据定向读取；澄清题均未检索。6次成功Raw局部读取已复读核对，7项输出事实全部映射到实际读取片段并由宿主核对语义，另1项研究关联明确标为条件推断。**这不是7项独立科学/身份真值验证，也不能称6/6答对。** 行政PDF无companion时复用已有非论文原生页码回退；学术论文仍只从MinerU `paper.md`取证，没有绕读PDF文本层。

### 已确认的契约缺口

`new-project-relevance/step-006.json` 的默认admin Wiki召回返回 `admin/wiki/policies/量子信息科学专业培养方案-20260606`，状态明确为 `deprecated`。本题不问历史版本，宿主未采用这一候选。它与 `QUERY.md` 默认过滤废弃页的规则冲突。运行query_actions影响卡后，定向读取 `py:wiki_recall` 确认直接排名和graph_fallback两条候选路径均没有统一deprecated准入过滤；责任在共享召回组件，不是给本题加别名即可解决。本轮不在验收中修改被测排序、语料或代码；下一步优先补默认准入过滤，覆盖两个分支，同时保留显式历史版本查询能力，不因本样本建索引。

另外，作者边的 `paper.md#authors` 命中44010字符，reader正确拒绝超长片段；细化为L1-L11后读取成功。它是旧locator粒度问题，不是原件丢失或应该放宽全文门。当前提取文本无邮箱命中，也不能据此断言原PDF没有邮箱。预算及项目材料未定位的具体缺口保留开放，不把“找不到”统计为已完整回答。

### 宿主偏差与只读边界

独立策略文件晚于首轮4个定位动作，虽然manifest已先冻结问句与槽位，也不能声称完整策略轨迹预先冻结。宿主还曾直接以显示姓名请求作者关系，canonical node解析后才返回4条作者边；这应改进“先定位主体再查关系”的使用纪律，不能误报关系工具故障。主持人题读取过大段Content，后来才缩到人物subsection，这同样属于宿主低效。

公共academic/admin Wiki、图、测试manifest及生产查询日志共720个文件前后哈希一致，Wiki文件集合未变。Raw片段复读一致；未写Raw、Wiki、图或生产完成答案日志。临时轨迹只是工程诊断，不是事实源；未新增模型调用、检索索引或后台任务，暂停的论文复现继续暂停。

## 第七批：废弃页默认准入修复（2026-10-12）

第六批确认的缺口在共享 `query_actions` 中修复，不改样本别名、排名规则或知识数据。`wiki_recall/admin_recall/hybrid_recall` 默认在排名和 top-k 前排除图或磁盘 frontmatter 为 deprecated 的页面；直接召回、graph_fallback、联合召回直接图命中及邻接页共用准入。邻接页先排除废弃页再应用既有行预算，不放宽其他遍历预算。每个 Wiki 文件仍单次扫描，版本元数据复用已加载文本；联合召回复用同次扫描结果，不新增全库重扫。

仅显式历史请求传 `include_deprecated=true`；`false` 字符串不会误开启，其他非布尔/true/false 值被拒绝。Agent execute、API schema/意图计划与 `wg.py hybrid-recall --include-deprecated` 贯通该参数；lineage 不隐式启用。结果保留图规范 status，并报告 page_status 和 superseded_by；这些是导航状态，不是证据。

回放第六批原问句、原 domain 和 top-k：默认结果不再包含20260606废弃培养方案，显式历史模式仍返回它及 superseded_by。回放前后公共academic/admin Wiki、图、冻结manifest及生产查询日志721个文件哈希一致。产物为 `temp/retrieval-benchmark/deprecated-filter-20261012/replay.json`、`comparison.json` 与 `README.md`。

冻结的 curated/real 导航集分别重新跑两轮，前后 Wiki、图和manifest一致；预期目标没有修改。两集候选路径及固定目标命中/召回均未退步：curated Wiki 8/8、real Wiki 11/15 命中（平均目标召回约0.644）。每题逻辑Wiki读取次数和SQL条数不增，但解析版本元数据有可测CPU开销，具体前后耗时以 comparison 为准；首次运行不是受控冷缓存，不能宣称零开销或整体性能提升。该基准不是盲测答案验收，也未包含所有历史/独立图工具；本轮仅修上述Wiki召回入口，不声称所有图检索层都已过滤废弃页。

回归覆盖默认/历史、图与磁盘状态冲突、磁盘独有/图独有页、top-k前过滤、邻接预算、全部废弃时无候选、非法参数、private隔离、单次读取及Agent/API/CLI参数透传。废弃页可作为图定位中间节点到达现行邻页，但不进入默认候选，避免误删有效导航路径。Wiki召回30项通过；private、API编排、API循环、wg、function registry、retrieval benchmark、query graph回归及工程图校验通过。未写Raw/Wiki/图或生产日志，未建索引、后台任务、缓存或新增模型调用；暂停的论文复现保持暂停。

## 第八批：独立图版本边界收尾（2026-10-12）

核验确认两类通用缺口：独立 `search_nodes` 与 `semantic_search` 的页面发现没有默认生命周期准入；`graph_lib.ensure_node` 对已有 deprecated 节点省略 status 时会用默认 current 覆盖旧值。前者造成默认材料候选混入废弃页，后者可造成图生命周期漂移。新增回归分别锁定排名/top-k之前过滤和省略状态保留；不能据此断言下面两处历史漂移都由该写入行为造成。

独立图/语义搜索默认按图 status 排除 deprecated 页面，显式历史模式保留旧版，Agent execute、API schema/意图计划、query_graph/node_semantics CLI及wg lookup/abbr开关贯通。node、neighbors、relations、hub_of保留历史节点及边，以material_role/default_page_candidate/version_context区分历史导航与默认下钻候选；不删旧节点截断关联链。Agent/API node_resolve仍判身份并附版本上下文，resolved不等于材料现行。temporal按显式日期有效性、path_exists只判连通，未被改为默认材料发现。

一次公共扫描比较838个Wiki导航项，发现两处Wiki为deprecated但图为active/current：`academic/wiki/conferences/undated-研究生院座谈会`、`admin/wiki/policies/物理系十五五规划纲要`。核对对应frontmatter和文件SHA后，经graph_writer_lock、BEGIN IMMEDIATE及原graph_lib.ensure_node定向同步两个status。前后图备份及逐表比较确认：只改变这两个nodes.status，节点集合、所有非nodes表及其他节点字段均不变，没有删除边、alias或来源。再次扫描同一公共范围，已检测的deprecated状态冲突为0；未审计真实private语料，不能把此结果推广为全库所有生命周期均一致。

真实原问句回放验证：默认独立搜索不再返回20260606废弃培养方案，显式历史搜索仍返回；关系边与邻居路径保留，旧页标为历史导航；离线语义搜索与身份解析也验证版本边界。本轮语义回放强制词汇降级，不调用真实embedding或模型。回归通过13套，其中图查询21项、节点语义29项、Wiki召回33项；覆盖省略状态、历史模式、预算、private隔离与Agent/API/CLI透传，另通过摄入管道、GraphDelta及时态写入回归和工程图校验。

curated/real两套冻结导航集各跑两轮，前后Wiki和manifest一致，候选路径及目标命中/召回不退步。图有上述两项经审计状态变化，**不是同一图的纯代码性能对照**。搜索/BFS逻辑Wiki读取和SQL条数不增，观测耗时无明显恶化，但版本角色增加返回文本：curated搜索平均约1.1→1.9KB，邻居约18.7→20.6KB；token预算继续按实际输出计量，不宣称零成本、性能提升或答案准确率提升。关系/身份版本上下文采用批量属性查询，不做Wiki全库重扫。

验收在 `temp/retrieval-benchmark/version-boundary-20261012/README.md`，包含replay、status-audit前后、status-sync、comparison、输入哈希及图前后备份。721个受保护文件中仅公共graph.db改变；Wiki、冻结manifest和生产查询日志保持原哈希。未写Raw/Wiki/生产日志，未建索引、缓存、后台任务或新增模型调用。此轮底层收尾结束，后续进入实际使用，以可复现失败样本决定是否继续改造；暂停的论文复现保持暂停。
