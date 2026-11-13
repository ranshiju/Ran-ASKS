# N3：按用途联合检索

2026-10-29，N3 首批已交付。用户明确本轮以机制改革为主，历史“耗时≤70%”不再作为本轮主要目标或放行门槛。

## 本轮目标与验收

让普通 `hybrid_recall` 同时发现 Source、Statement、Question 与 Wiki；图继续负责导航关联。
调用方按候选的类型和读取动作下钻，不再默认所有知识必须先变成 Wiki 或图节点。
KU 保留条件、草稿/猜想状态与未决项，真正事实证据仍来自 Raw。

1. shared 联合入口：复用已有 Wiki/Graph 导航排序及 Source/KU 索引，先按共享字面相关性排序，再按各路名次融合；
   每个来源版本合并重复片段，KU 保持独立身份，不把多路命中当成证据增强。
2. shared 读取协议：候选标明 kind、read_action/read_input，兼容旧 Wiki 字段；
   Source直接读Raw；KU/Question可沿候选依据预览直接读Raw，需要完整知识条件或其他依据时读取当前KU。
3. 可靠性与维护：没有图也能使用来源/KU/Wiki；单路索引不可用时显式披露，其他可用路继续；
   查询只读，重建/修订仍走现有受管入口，更新后重新查询须看到当前版本。
4. Agent/API 使用同一确定性召回；API 只更新现有查询规划提示，不新增翻译/审核模型调用。
   private 与其他领域保持边界，不能为了联合召回读取 public academic 数据。
5. 验证：少量合成机制回归，以及 N2 的两份存量论文/3 个真实问题的隔离重放。
   检查多种入口、条件/猜想、Raw 可读、旧版不覆盖新版本；记录用时，不设速度阈值。

影响：`query_actions`、`knowledge_store`、`source_ingest` 的索引可用标记、现有 API 查询提示、DSH 工具及使用/工程文档。
采用已有 `hybrid_recall` 动作，不新增 Agent 控制循环、知识主存储或事实图。
风险是弱词汇命中、不同表示重复占位和部分通道不可用；用类型化读取、来源聚合、
通道状态及原文下钻约束处理，不引入固定审核链。

本轮先完成“联合发现→分类型读取→Raw 核验→既有局部更新可见”的纵向功能。
自动维护调度、全库投影迁移和更广的跨语言语义召回按实际使用缺口另行推进。

## 普通查询入口与读取协议

工具回执的usage由程序按成功执行的动作确定：搜索/Wiki/Graph输出是navigation，Raw读取是
source_material，完整KU读取是knowledge；API计算为result。详见[用途契约](artifact-use-contracts.md)。
角色不由候选正文的自称决定，失败读取不标记为来源。用途说明随wg/API/DSH交付，
不改既有kind、身份、内容、地址或引用白名单。已知相关Raw地址可直接核对当前问题，
无须为了消除导航噪声或补全候选元数据而遍历全部KU/Wiki。

```bash
python3 .scripts/wg.py hybrid-recall 'QCNN 无贫瘠高原的适用条件' --domain academic --intent explanation
python3 .scripts/wg.py ku-read KU-<24hex>
python3 .scripts/wg.py read-raw '<依据 locator>'
```

宿主、API query orchestrator 与 DSH 使用同一个 shared `hybrid_recall`，返回 `unified-recall-v1`。
DSH 新注册该工具，API 默认发现计划继续复用原动作；没有新增独立模型规划/翻译/审核调用。
`query-stage start/evidence/continue/answer` 和已读 Raw citation 门保持不变。

| kind | 发现内容 | read_action / read_input | 后续 |
| --- | --- | --- | --- |
| `source` | 来源正文片段，含 S-ID/P-ID 和解析限制 | `read_raw` / `{locator}` | 按实际内容核对；需要其他条件时补读相关位置 |
| `statement` | KU 摘录、当前 revision、draft、claim_mode、unresolved及依据预览 | `knowledge_read` / `{unit_id}` | 可沿预览地址直接读Raw；完整条件或其余依据按需读KU |
| `question` | 原生问题、本库/科学状态及已有依据预览 | `knowledge_read` / `{unit_id}` | 可直接核对相关Raw；当前回答、猜想和未决项按需读KU |
| `wiki` | 既有 Wiki/图导航结果 | `read_section` / `{page, section}` | 读取章节与 Raw 脚注；可沿文档来源进一步定位 |

每个候选保留 path/id、kind、read_action/read_input、retrieval_channel/channel_rank，
并标 `evidence_read=false`。旧 Wiki 的 semantic_address、capsule、signals 等字段继续保留。
Source分组保留所选片段的结构阅读建议 `read_input`；原索引 `locator`、摘录和
`matched_locators` 含义不变，不把建议重新缩成索引范围。建议仅补齐有界的段落、
显示公式及明确相邻定义，不认证语义充分；预算和 `reading_context=limited` 说明见
[Source阅读契约](source-first-ingestion.md#查询与实际范围)。没有建议时仍沿原locator读取。
KU/Question 候选和读取结果透传 content_role；Question 另含 source_question_status，
其 addressed/left_open 仅描述捕获来源的处境，与本库答案和科学状态分开。角色不改变排名或认证真实性。
只有实际 `read_raw` 成功才进入已读证据集合；KU/Question/Wiki 不因命中或读取升格为事实依据。

KU/Question搜索附`evidence_preview`：沿当前记录原顺序至多3条locator/relation/assessed_revision，
同时附`evidence_count`与`evidence_truncated`。这些字段来自已加载的记录，不读Raw、不校验来源绑定、
不按supports过滤contradicts或未知关系，也不改变既有字段、排序和主read_action。
预览是可选下钻入口，关系标记不认证主张；实际read_raw仍校验公共域范围与源绑定。
无依据的历史Question返回空预览，不从标题或摘要编造地址；需要完整条件、冲突或其余依据时knowledge_read。

API发现指导按任务选择初始Source/KU/hybrid入口，观察结果后按缺口补查其他通道；
独立子问题可批量执行。这是规划指导，不硬删召回通道、不判断语义充分性，不增加模型调用或轮次预算。

旧 Wiki 若没有章节脚注，返回的 `source_links` 从同次读取的 frontmatter sources 获取，
经过本域路径检查。这只是文档来源导航，不补造 raw_citations，不声称支持当前章节。
新读取协议的调用方必须按 kind/动作处理混合结果，不能假设每个结果都是 Wiki 页面。

## 显式主体与长问题

调用方知道关注对象时，将完整问题保留在 `query`，另传 `subjects` 名称数组；最多3项，每项非空且不超过160字符，内部压缩空白并去重。比较题可分别列出两个主体。名称由当前宿主或API规划主体判断，程序不猜测主体、翻译名称或认证实体身份；未知时省略，空数组与未提供均保持旧行为。

```bash
python3 .scripts/wg.py hybrid-recall 'TDVP TEBD time evolution truncation energy conservation' --subject TDVP --subject TEBD --domain academic --intent explanation
```

Agent、API意图计划、API工具及DSH共用此可选参数。Wiki在同一次范围扫描上分别计算主体与完整问题的排名，轮流合并后截断；图精确词项优先纳入主体，总词项/邻接预算不扩张，语义检索仍只查询一次完整问题。Source/KU按各主体和完整问题分路检索，再轮流去重合并，每通道仍最多20候选。相同对象/地址的不同摘录按下述优先级选择整份候选，不把先到的主体预览固定为最终预览；摘录、偏移、版本和读取地址始终一起保留。

显式模式先按字面命中的主体数量、标签命中数量排序，再比较标题加既有预览中至多700字符窗口的完整问题覆盖，最后使用通道名次；`subject_match`解释名称命中，`lexical_match`反映局部问题覆盖。回执公开`subjects`、`fusion=subject_then_local_coverage_then_rank`及`ranking_text`。Source同版本仍只占一个位置，普通问题优先其非参考文献片段，备用地址继续可读。

成本：同次Wiki扫描复用，新增内存排名；每个Source/KU通道最多执行4次已有有界搜索，而不是保持单次索引/绑定读取成本。`channels.queries/search_count`披露所尝试分路；分路失败保留其他可用结果并报告问题。没有新增语义API或embedding请求、持久索引和控制循环。是否减少实际取证成本需完整任务验证。

主体分路保留发现机会，不保证最终小topk包含每个主体；比较题若缺一侧，由调用方定向补查。标题包含主体不保证问题相关，完整题名可能也是另一标题的字面子串；局部预览可能遗漏跨片段条件，未命中不证明资料缺失。仍需读取Raw核验身份、条件与支持范围。

## 排名、预算与失败边界

先复用原有按 intent 加权的 Wiki/Graph RRF 排名形成 navigation 路。
其中每页在 `wiki`、`graph_exact`、`graph` 各通道只贡献一次最佳名次；同通道经不同词项或节点
重复到达不累计加分，全部不同 `via` 仍保留在 `signals` 供路径解释。它们不是独立通道或独立证据。
原 intent 权重、候选/遍历预算及有 Raw 引用章节的微量加分保持，不按连接数奖励页面。
未给主体时，共享 `lexical_relevance.py` 对标题和候选文字计算：完整中文查询短语/英文整词的覆盖、
不同查询词项覆盖、标题词项覆盖，依此优先排序，再以通道名次分 `1/(60+rank)` 打破平局。
此兼容模式下，Source/KU 在已校验的完整片段/表述上计算排序特征，不因展示截断漏掉后半段匹配。
随后用共享函数选择至多700字符的连续命中窗口，以不同短语/词项覆盖及前后上下文决定展示；
没有正文命中则沿用前缀。`excerpt_span` 是相对完整片段/表述的零起点、右端不包含的字符范围，
摘录必须是原文精确切片，`excerpt_truncated` 标明任一侧省略；不修改排序、身份或read_input。
摘录仅供选择阅读入口，不保证条件完整；Source及KU候选依据可直接read_raw，完整KU按需knowledge_read。
Wiki 使用当前标题、导航和 capsule。经图关系找到但未进入 Wiki 前置候选窗口的页，复用同次范围校验扫描中的规范标题与 Navigation，不退化为文件名/空导航；不新增扫描或返回完整页面正文。重复提及不加分，英文词不按子串匹配。
结果公开 `lexical_match` 和 `fusion=literal_coverage_then_rank`；score 仍是名次分，
不能单独用 score 重新排序。弱匹配保留作后备；不混加 FTS、embedding 与图的原始分数。
这是字面优先级，不处理自动翻译、同义词或科学支持判断，也不保证每个长名称提及都相关。
多路命中不增加科学确定性，不把来源数量或命中数当作独立证据数。

最终 topk 限 1–20，每路候选窗口最多 20。Source 同一 S-ID/P-ID 的命中合并，最多携带
3 个 matched_locators；重复片段只占一个 Source 位置。候选窗口先截断再分组，
`window_full` 提醒存在进一步候选，不能认为已经遍历全库。
Source 搜索对普通查询先在同源命中内优先非参考文献片段；联合入口沿用此阅读顺序，
其 `read_input`、`excerpt` 和字面特征属于实际所选片段，不沿用被替换参考片段的分数。
`section_role` 只标记可机械确认的参考文献章节；明确查文献时保持原排序，
其他片段及参考文献仍可通过 `matched_locators` 或来源搜索读取。
每次Source搜索先在原有最多 `5*topk` 个 FTS 候选中校验并重排，每次KU搜索使用原有最多 `3*topk`
个 Statement FTS 候选及原生 Question 候选；不为排序读取候选之外的 Raw 或调用模型。
Statement/Question 各自保持 ID；同一论文的不同 KU、Wiki 与原件不自动做语义合并。

`channels` 公开每路 count、issues 和状态。无图时仍扫描范围内已有 Wiki，并独立搜索 Source/KU。
缺失或损坏某一路派生索引时，已可用的其他路继续返回；不静默创建数据库、重建索引或发起摄入。
数据索引修复分别走 source resume / knowledge rebuild-index / 既有图维护入口。
跨通道结果中的低相关命中仍需语义主体筛选，未命中不表示没有知识。

只有 domain=academic 或公共跨域查询才加入 academic Source/KU；
private/admin/teaching/business 均不调用这两路。private 继续使用已有本地 Wiki/Graph 及物理隔离规则。
本联合入口的 KU/Source 默认通道仍是词汇召回；独立的`source_search`与`knowledge_search`可显式选择预建向量混合发现。已有中文 KU 可作为中文问题到英文依据的桥梁，不额外生成语义翻译。
必要时由当前查询规划主体补充英文术语，不加固定翻译请求。

## 当前版本、局部维护与验证

查询读取当前 Markdown/索引，保留候选 revision；真正下钻时 `knowledge_read` 再读取当前版本，
不会把旧候选的摘录当作当前全文。依据绑定/旧解析提示仍由 N2 read 返回。
已经存在的 KU 新建/attach/revise 和索引更新机制继续使用；查询本身不提交修订，也不启动后台维护。
具体缺口由实际查询暴露后，宿主可按既有授权准备有界任务、校验并局部更新，再重查原问题。

公开合成回归覆盖多路发现、分类型读取、版本更新、API 取证和 private 隔离。
少量存量来源的本地重放只验证具体问题和地址可达；真实材料与运行记录不公开。
尚未完成大型语料、最终答案盲测或自动维护吞吐评估；词汇匹配不认证语义相关性。

### 独立 KU 语义入口

`knowledge_search` 可显式 `retrieval=hybrid` 使用当前 Statement 的预建向量，Question仍为词汇，覆盖/失效/失败显式披露。此能力与 Source 向量共用文本窗口及 embedding 请求/缓存，保留不同产物身份和阅读路径；不改变本联合检索默认候选来源，不在查询时整理或认证知识。构建、版本绑定及读取契约见[KU规范](knowledge-units.md#可选-statement-语义发现)。
