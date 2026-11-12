# Source-first 摄入 N1

2026-10-28 实现 N1；2026-10-29 接入可选 N2 消费器。显式试用能力，默认摄入仍为 `legacy`。

## 责任与完成契约

`.scripts/source_ingest.py` 是 Agent/API 共用的确定性内核，负责受管原件提交、
版本化解析、来源回执与全文索引。没有语义模型循环、Wiki/Graph 生成或全库维护。
`ingest_inbox --profile source-first` 在选择旧语义 backend 之前分流；宿主 Agent 不隐式进入 API 语义编译。
PDF 仍使用现有远端解析服务，零语义 LLM 调用不等于零解析费用或零远程传输。

| 状态 | 含义 |
| --- | --- |
| `receiving` | 已登记接收意图，原件可能尚未完成复制 |
| `received` | 原件已保存；解析或索引尚未成功发布，具体错误有 phase |
| `source_ready` | 已有哈希绑定的文字载体，片段索引提交和读回成功 |

`legacy_completed=false`，来源发布时默认 `knowledge_status=pending`；显式仅来源模式为 `not_required`。
`knowledge_policy=optional|source_only` 持久记录整理意图。N1 登记持久的知识发现待办；
显式 N2 消费器可独立处理，不把来源可检索当作整篇知识编译完成。没有后台整理保证。
回执是流程和恢复记录，不是事实源、科学质量认证或可替代 Raw 的证据。

显式`--knowledge agent|api --strategy selective`可选择精简首包及有界局部补读，直接生成或零新增
仍走原知识提交；`survey`保持默认。该选项由统一入口在来源发布后传给共享知识任务，
legacy/none/skip组合在写入前拒绝。实现与限制见[按需供料](knowledge-units.md#可选按需供料与局部补读2026-11-11)，
不改变来源事务、解析或全文索引，不自动创建Wiki/Graph，也不新增后台消费者。

## 使用入口

首批仅支持 **academic 的 PDF、Markdown、UTF-8 TXT**。传入文件保留，
不搬走 inbox 原件；必须显式给文件，source-first 不自动扫描整箱。
首次公共域摄入仍按 INGEST 执行上下文预热。

```bash
python3 .scripts/wg.py ingest /absolute/input.pdf --profile source-first
python3 .scripts/wg.py ingest /absolute/input.pdf --profile source-first --knowledge api
python3 .scripts/wg.py ingest /absolute/input.pdf --profile source-first --knowledge agent --focus '本次需要理解或复用的问题；相关章节名'
python3 .scripts/wg.py ingest /absolute/notice.md --profile source-first --knowledge none
python3 .scripts/wg.py ingest --profile source-first --resume S-<24hex>
python3 .scripts/source_ingest.py show S-<24hex>
python3 .scripts/wg.py source-search 'tensor contraction' --topk 5
python3 .scripts/wg.py read-raw 'academic/raw/sources/S-.../parses/P-.../paper.md#L1-L20'
```

`--knowledge skip` 为默认，表示本次暂不执行并保留原整理意图；`none` 显式选择仅来源，
新来源为 `source_only/not_required`，不调用分类器或知识模型，也不建立待编译负担。
由用户/宿主根据实际内容选择纯事务模式，不能把所有非论文或会议记录一律判为无知识。
通知、登记事项等可仅来源；混合材料仍可选择编译，只提炼可复用知识，允许零 KU。
`agent` 在来源发布后返回宿主任务，`api` 同步运行有界语义整理，
要求显式 API backend。知识任务状态及 `knowledge_run` 单列；知识失败不会改变已发布来源。
也可稍后调用 `wg.py knowledge prepare|run S-ID`，详见 [N2 契约](knowledge-units.md)。
统一入口的 `--focus` 原样传入既有知识任务，适用于 `agent|api` 和 S-ID 恢复；
legacy、none、skip 带非空 focus 在写入前拒绝。相同来源版本、用途、执行主体与协议沿用
原 KJ 身份及已有结果，不因重提而强制重新生成；不要用改写用途来隐式重试失败 API。
focus 是用途和选片线索，不保证所需章节完整入包；检查 coverage，已知 Raw 可直接读取，
保存 KU 仍只能引用该任务包内已绑定的依据，不能把包外补读冒作包内证据。

来源回执的 `status/source_status` 仍只描述来源，文件包装的顶层同时透出
`knowledge_status/knowledge_run`。本次知识调用失败或有失败操作时增加 `knowledge_error`，
ingest_inbox 返回非零、wg 返回 `ok=false`，保留已发布来源及已提交知识；
准备阶段尚无新任务就失败时，本次 `knowledge_run=null`，不借用来源的旧任务 ID。
prepared 表示等待 Agent；partial 表示尚有未决范围或对象，详见 `knowledge.unresolved/results`；
只有未读/延期的 partial 不作为执行失败。completed 仅指该用途的知识任务，不能替代用户任务完成或全文质量核验。
API 失败继续用原 knowledge show/run 与显式 retry；Agent 用 KJ task inspect/advance，均不重做来源保存。
显式 `agent|api` 或独立 prepare/run 可重新开启整理。`none` 不删除已有 KU，
也不抹掉已经执行的知识任务及状态；只将未执行待办标为 not_required。
直接 `source_ingest.py ingest|resume --knowledge none` 使用相同意图机制。

已有受管解析包可以显式复用（要求包含同字节 `paper.pdf`、UTF-8 `paper.md`，
已有本地 `images/` 资源一并保存，缺图逐项登记并限制图表使用；不会执行附件里的命令）：

```bash
python3 .scripts/wg.py ingest /absolute/bundle/paper.pdf --profile source-first --reuse-parse /absolute/bundle
python3 .scripts/source_ingest.py ingest /absolute/a.md /absolute/b.pdf
```

后一个命令提供独立来源批处理，单项失败保留成功项并继续，汇总返回 `partial` 和非零退出码。
`ingest_inbox.py --run --profile source-first --file ...` 或 `--import-file ...` 使用同一内核。
不支持旧 stdin、分类、改名、图片 OCR、其他存储域参数；这些组合明确拒绝，不静默忽略。

## 身份、存储与恢复

```text
academic/raw/sources/S-<SHA256前24位>/
  original.pdf|md|txt
  parses/P-<解析请求指纹>/paper.md
  parses/P-<解析请求指纹>/images/...
  parses/P-<解析请求指纹>/parse_meta.yaml  （原解析包提供时）
academic/sources/S-<24hex>.json
academic/sources/index.sqlite
temp/source-ingest/S-<24hex>.lock
temp/source-ingest/S-<24hex>/P-.../parse/ （解析断点）
```

S-ID 表示不可变的**文件字节版本**，不是跨版本论文身份。完整 SHA-256 再校验，
不同字节不自动合并，同字节重提复用来源并追加 `origins` 日志。
来源回执和索引标题来自文件名，明确 `filename_unverified`；作者、年份、期刊不是成功前置条件。查询展示时，若已校验文字载体的首个非空行是单层 H1，则用该行至多200字符作为候选标题，标为 `opening_heading_unverified`，并保留 `filename_title`。它只帮助区分来源，不认证书目或语义身份；其余情况保留原标题。复用搜索已经读取并校验的文字，排名信号、回执、索引与 Raw 不变，旧来源无需迁移。
TXT/MD 直接使用原件文字，不额外制造一份同内容载体。

同源操作在文件锁内串行；原件临时复制并校验，再原子发布。原件已存在时只验哈希，不覆盖。
解析目录先暂存并整体发布；不同解析请求/复用产物使用不同 P-ID，旧目录及旧行号不改写。
文件与 SQLite 分步提交，不宣称跨存储原子性；每个可恢复边界更新持久回执。

- 原件复制中断：`receiving` 保留接收意图；重新提交原文件恢复同一 S-ID。
  `resume` 不自动跟随回执里的任意来源路径，缺原件时明确要求重新提交。
- 解析失败：原件和回执保留；修复服务后按 S-ID 恢复，沿用受管 provider checkpoint。
  显式选择复用包后，该选择持久保留；复用失败的普通重提/resume 不会自动改用远端解析。
  只有显式 `--refresh-parse` 才重新选择当前受管 parser。
- 解析成功但索引失败：解析版本先写入回执；恢复只补索引，不重复解析。
- 索引删除或部分来源失效：对对应 S-ID 执行 resume 重建。搜索只读且不会偷偷创建数据库。
- 索引提交后回执未成功：该来源不作为已发布候选返回，resume 幂等补齐。
- 已完成的知识待办不会因同一解析重建索引被重置；新解析登记新的幂等待办。
  source_only 的新解析保持 not_required；重建索引保留该策略。新解析不沿用旧解析的 knowledge_run。

回执状态是最后提交状态。`show` 会只读核对绑定及索引登记，缺失时返回 `unavailable` 和恢复提示。
外部删除/损坏索引后，搜索不会据旧回执虚构命中，候选摘录/行地址还须与 Raw 对应；
直接读来源也验证原件/文字载体绑定。原件损坏不允许用重新摄入静默覆盖，须诊断恢复。
N1 不提供跨机器同时修改 Synology 同步目录的分布式锁保证，应由一个宿主负责写入。

## 解析复用边界

PDF 使用显式 MinerU；只有配置启用且传 `--allow-paddleocr` 时允许备用 PaddleOCR。
没有新增本地 PDF 兜底。配置、三个 parser adapter 代码哈希、协议及原件共同决定解析请求键；
provider 的远端模型实际更新未必可检测，保留配置指纹和生成时间，不宣称永久解析等价。
解析元数据可用时原样保留。

普通重提/恢复固定复用选定解析。需要应用当前配置时传 `--refresh-parse`；
相同请求仍复用，变更请求产生新版本。它不是强制清空同请求缓存的开关。
`--reuse-parse` 的 P-ID 包含实际载体/资源哈希，载体变化时新增解析版本。
N2 KU 依据绑定具体解析版本与片段哈希；新解析不会悄悄替换旧 KU 的行号。

复用包校验原件匹配、路径及已有产物哈希，未随包保存的独立图片资源在 `missing_assets` 和限制中披露；
该数量是 Markdown 图片资源引用数，不等于论文编号图数量，也不表示原 PDF 缺图。
新回执和旧回执的实时展示都明确资源可按需提取；旧回执仅转换原固定提示语，不回写历史或改变解析身份。
独立图资源不作为正文可检索或本次知识整理完成的必备产物；已有资源继续保存复用并校验完整性，路径越界/软链接仍拒绝。这不能证明文字是对 PDF 的准确转写。
公式、图表、页覆盖和阅读顺序没有全面语义核验，限制随回执和搜索结果返回。
远程图片链接仅保留在 Markdown 中，摄入/查询不会因文本链接主动下载。

### PDF 图号引用与按需读取

默认以保留的原 PDF 为来源，引用 Fig./图号；需要时补子图号和已确认页码。引用文字结论使用正文/图注的
已验证 Markdown locator，同时可注明 PDF 图号。图号是面向读者的文献定位，不把它拼成当前 reader 未支持的
`#fig-N` 或猜测页码；现有 Raw 绑定和引用校验继续执行。PDF文件页序与印刷页码不同时明确区分。

当任务依赖曲线、数值、颜色、几何/空间关系，或需要在产物中展示原图时，才查看原 PDF 对应页或提取局部图像。
使用本地只读 PDF 工具输出到任务 temp/ 或项目派生目录，随产物记录原 PDF 路径/哈希、实际页序及图号/裁剪范围；
不向既有 Raw 解析目录直接补写文件，也不把裁剪图当作新来源或完整图像核验凭证。
不需要全篇重 OCR；只有现有文本解析本身有影响当前任务的缺陷时，才另走原受管重解析/新版本流程。
正文和图注足以回答时止于文字证据，不自动请求视觉模型。既有图像资源可复用，远程传输仍按原授权边界。

## 查询与实际范围

`query_actions.source_search(query, domain, topk)`、API query orchestrator 与 DSH query tools
均已注册，Agent 可经 `wg source-search` 直接调用。首次 N1 查询无需 Wiki/Graph。
返回 `source_id/parse_id/source_path/locator/excerpt/limitations`，随后 `read_raw` 核对。
来源搜索在原有有界 FTS 候选中，按已校验完整片段的查询短语/整词和不同词项覆盖重排，
然后在完整片段内选择至多700字符的连续摘录：优先覆盖完整查询短语/英文整词及不同词项，
同覆盖时尽量保留命中前后上下文；重复提及不加分。短文本或正文无命中时保留原前缀。
`excerpt_span.start/end` 为相对完整片段正文的零起点、右端不包含的字符偏移，
`excerpt == body[start:end]`，不拼接或生成原文；`excerpt_truncated` 表示省略了任一侧文字。
`locator` 仍指向已校验的完整片段，摘录范围不是新的引用地址；不改变排名、索引或Raw绑定。
`lexical_match` 供联合检索复用，仅表示完整片段的字面匹配，不是证据核验或置信度。
窗口可能遗漏限定语，仍需read_raw；该共享摘录函数也用于KU/Question发现，不改变其完整读取。
普通查询在同一来源已召回的片段内，优先非参考文献章节的命中；保留各来源在候选池中的
原有位置，参考文献命中随后保留，只有参考文献命中时仍可返回。明确含 references、
bibliography、citation、DOI 或“参考文献/引用文献”等线索时保持原字面排序。
章节识别只针对已校验载体中的明确 Markdown 参考文献标题，遵守层级结束边界，
代码围栏内的标题不算章节；正文与参考文献混合的片段不标为参考文献。
返回 `section_role=references|unspecified` 和搜索级 `passage_preference`，不改索引或原文，
不额外调用模型。无标题的参考文献不推测分类，优先摘要也不等于已经定位完整答案。
`source_locator` 为新来源保持“引用原件、读取版本化文字载体”的映射；历史解析仍可读。
API 提示接收工具的实际有界输出，200 字符 preview 仅用于日志摘要，不能替代证据。

已知来源但缺少章节或表图位置时，使用 `wg.py source-outline <source_id> --query 'Table 10'`
或共享 `source_outline(source_id, query="", start_line=1, limit=40)`。
它只扫描当前 `source_ready` 的哈希绑定文字载体，不依赖KU、Wiki、图或全文索引，
按原文顺序列出明确的Markdown标题和带编号的Table/Figure/Fig./表/图题注，忽略代码围栏。
query是忽略大小写并合并空白后的原文标签子串；空串列目录，不翻译或推断缺失题注。
每次最多80项，`next_start_line`非空时可继续；超过文件末尾返回空候选。
每项保留标签所在行、至多200字符的标题及截断标记，locator指向现有分片算法中包含该行的片段；
超长单行无分片时只给该行。地址不保证完整表体、图例或章节，跨片段所需内容仍按任务补读。
目录是navigation，不登记已读证据；事实仍经原read_raw绑定、大小和引用检查。
Agent、API、DSH共用内核，API可选择目录后沿原阅读编号执行，不增加固定轮或自动补读。
目录不执行来源指令、不写Raw/回执/索引、不调用模型或联网，private与无效来源绑定明确拒绝。

SQLite FTS5 存储片段及词汇索引，不存图关系；公共关系唯一权威仍为 `cross-domain/graph.db`。
英文词及中文二元词组支持字面检索；不会自动翻译、embedding 召回或语义扩展。
中文问英文资料时可显式查询对应英文术语，或用 N3 联合入口经已有中文 KU 回到英文依据；
这不代表原文索引具备自动跨语言语义召回能力。
索引按来源增量替换，每片不超过 32 行/6000 字符。
超过 6000 字符的单行暂不进入索引，回执明确列出行号和部分覆盖限制，原件保留；
空/无可搜索片段不会成为 `source_ready`。`excerpt` 是候选摘录，不是已核验知识。

仅 academic/public 的 source-first 来源可搜索。private 查询显式拒绝此动作，
不会用公共来源替代；private 导入、源路径/托管路径软链接、越界和未登记载体拒绝。

## 验证与下一阶段

执行 `.scripts/test_source_ingest.py`，覆盖故障恢复、重复/并发、绑定、旧引用、
独立批处理、private 隔离、CLI/API 实际取证通道；配合受影响查询/入口回归及工程图验证。
回归验证不代替 PDF 解析质量或完整知识编译质量评估，也不证明端到端耗时收益。
真实来源、运行记录与少量存量材料的使用评估保持本地，不构成公开可复现的质量基准。

N2 已提供显式 pending 消费、有界 Delta、Statement 版本更新与原生 Question 适配。
N3 首批已接通 [按用途联合检索](unified-retrieval.md)，无需 Wiki/Graph 也能发现来源；
自动维护调度尚未实施，N4 再考虑解析优化和默认切换。
