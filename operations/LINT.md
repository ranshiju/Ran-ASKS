> **使用约束**:先读规则再操作;只检查不擅自修改 raw;发现的问题报告,不自动扩面修复。

# Lint（健康检查）操作规范


---

## 触发方式

用户请求对某个子项目进行健康检查。

> **与摄入即时校验的分工**：摄入时 `.scripts/ingest_check.py` 负责**本次摄入文件的局部事务正确**(frontmatter/section/wikilink/status 一致;边在 graph.db 由 graph_ingest 校验,见 `INGEST.md` 步骤 8);本 LINT 操作负责**全库健康**(孤立页/重复概念/长期未更新/过期政策/Hub 超预算/版本链异常)。后者不每次摄入执行,按需触发;前者每次摄入必跑。两者不重叠：即时 check 查结构,离线 LINT 查演化。执行中附带发现的 WARN 由当次指令末向用户提示(见 INGEST 步骤 11「附带发现上报」)，不静默压到 LINT；LINT 仍负责未被附带发现的全库积压(孤立页/过期政策/Hub 超预算等)。

## 检查清单

| 检查项 | 说明 |
|--------|------|
| Contradictions | 不同页面之间的冲突声明 |
| Orphan pages | 没有任何入链的孤立页面 |
| Missing links | 被引用但尚未创建页面的概念 |
| Stale claims | 已被更新的 raw 来源推翻的声明 |
| Expired items | 行政文件已过期、教学文件已过学期 |
| Research gaps | 需要更多研究才能确定的领域 |
| SR entity mismatch | 语音识别纪要中的人名/术语与 `authors/` 权威实体表不匹配（同音字错误检测） |
| source_type missing | 页面 sources 引用 .txt 但未标注 `source_type: speech-recognition` |
| [SR] triple missing | 来自 SR 来源页面的三元组缺少 `[SR]` 标记 |
| Consolidation pending | 延迟巩固文档:仅有 primary summary 无级联页,需检查 log.md 是否标 `consolidation: pending` 及待巩固原因 |
| Triple-prose duplication | 正文出现与图边（graph.db）重复的关系叙述（含"→ X →"箭头模式或"A 对标 B"纯关系句）；客观关系应只在 graph.db，正文留 wikilink + 论证性映射表 |
| Conflict without resolution | 检测到冲突(Contradictions/Stale claims)但页面未按「冲突处理分支」处理——缺 status:deprecated 标记、缺 superseded_by 指向、或缺对比页 |
| Deprecated without superseded_by | 页面标 status:deprecated 但未填 superseded_by 指向新版,无法追溯替代页 |
| Expired items unmanaged | 行政文件已过期/教学文件已过学期但未标 status:deprecated——应按版本演进机制标记降级,非放任 |
| Frontmatter 必填字段缺失(三格式 key-value 层) | 页面缺 title/type/source_type/date/status 等必填字段;deprecated 页缺 superseded_by。校验各子项目 SCHEMA.md Frontmatter 模板的必填项,缺失即告警 |
| aliases 缺失（v4，缩写已并入 aliases） | 页面（尤其论文/概念页）title 含括注但未进 aliases 表（层1机械抽取应自动补）。运行 `graph_ingest.py` 回补 |
| authors 完整性(academic 论文页,2026-07-23 新增) | 论文页(paper-summary)有 raw sources 但缺 `authors` 字段,或作者数明显少于 raw 作者行(批量摄入串号/截断)。校验方式:frontmatter `authors` 数 vs raw 论文作者行人数,差异≥2 即告警。禁止跨论文复制作者列表。运行 `python3 .scripts/scan_authors.py`(扫描全部论文页对照 raw) |
| 查询 trace 证据状态审计 | 查询 trace 中 Evidence Profile 客观事实(source_presence/source_types/version_status/conflict_markers)是否与页面 frontmatter 一致——抓查询 agent 虚报。运行 `.scripts/verify_dims.py <trace.jsonl> [page1 page2 ...]` 校验。v4 新增:slot_gaps(已声明槽位的覆盖率,程序机械计)与 stop_reason 一致性——sufficient_complete 须无缺口、sufficient_partial 须附缺口说明。trace 来源:`*/outputs/query-log.jsonl`(生产)或 `testbed/traces-v2/`(测试床)。mismatch 即虚报告警 |
| 回环上限审计 | 查询 trace 的回环数(loop_count)是否超 QUERY 步骤 4(三审停止)上限 3 轮。loop_count = LLM 出计划次数 - 1(编排层 session.record_plan 计,非 LLM 自报)。**无需 page 参数**,只传 trace 即报 loop_count + 超限 mismatch(>3)。属事后审计能抓超限,不事前拦截 |
| 动态流程路由审计(v4,2026-07-23 新增) | 查询 trace 的 `route` 字段是否信号驱动(防答案泄漏):route 须引用步骤 2 `probe` 的 hit/miss 事实为分派依据,不得引用"已知答案在 X 文件"类参数知识。`probe` 字段是否真实(声明的 grep 命中可复现)。`routing_cost` 与 `retrieval_cost` 分列。`answer_completeness_audit`(步骤 4 ②)是否对多源/关系类查询执行(单源简单事实可豁免)。程序校验 route 是否引用 probe + probe 可复现;回答完全性(语义)不程序判 |
| capability-experiences 维护(2026-08-23 统一) | LINT 周期从各能力日志的 `experience_used` 字段蒸馏成功且可泛化的 pattern,写入 `memory/experiences/<capability>.md`(每能力 4-6KB,最多返回 3 条)。淘汰判据:已被 playbook/规范/graph 覆盖、低复用、过细难泛化、与现行契约冲突。维护动作记入 LINT log；原始 case 留在各能力日志,不复制进经验库。详见 `operations/EXPERIENCES.md` |
| 孤岛页分类 | 零入链页面分两类:**待激活占位页**(如 faculty 名单批量导入的 people 页,sources 含 `faculty_list`/正文含"待补充",本人未参与任何记录在册活动)——合理孤岛,标 `consolidation: pending` 或备注占位,不告警;**真孤岛**(有 people 页但被裸名提及未 wikilink、级联创建后源页未回链)——告警并定向修复(补 related wikilink)。区分方式:待激活占位页 sources 指向名单类 raw 且正文为占位模板;真孤岛是正文/related 应有连接却缺失 |
| 提取引擎非 mineru(2026-07-30 新增) | 论文 paper-summary 的 raw source 目录 `parse_meta.yaml` 的 `preferred` 字段非 `mineru`(extractor 三级级联 mineru>docling>pymupdf 中质量最高档)。摄入即时校验已 WARN(见 INGEST 步骤 11);LINT 全库复核历史积压,提示用 mineru 重提取。无 `parse_meta.yaml` 的 source(会议纪要/docx/web 等非 PDF 提取场景)不触发 |

## 面向 Query 的图质量

图是 LLM 和代码的导航结构，健康度优先由真实 query 效果衡量，而不是由图形外观衡量：

- 文档子图：Wiki anchor 可定位；Raw 文档包从 Wiki 一跳可达；有导航价值的边界节点在两跳内能回到 Wiki；同名候选数和 `abstain_ambiguous` 数保持可解释。
- 主图：用固定 query 样例观察候选召回、错误合并、候选负担和从命中节点回到 Raw 的跳数；失败样例比抽象总分更重要。
- 摄入时 `graph_delta.query_probes` 是软回归信号。未满分不产生新的 ERROR/WARN，也不回滚；只有空端点、自环、缺 Wiki/Raw 来源骨架等事务结构错误才阻断。
- 不把密度、平均度、全连通、聚类系数或 locator 覆盖率作为子图/主图质量门。它们不能直接说明 LLM 是否能稳定完成 query，还会诱导无价值连边。

## SR 交叉验证执行细则

针对 `source_type: speech-recognition` 的页面，执行人名/术语交叉验证：

1. **构建权威实体表**：扫描 `academic/wiki/authors/` 下所有 people 页面的 title，提取人名集合
2. **扫描 SR 纪要**：读取所有 `source_type: speech-recognition` 页面的正文，提取其中出现的人名
3. **模糊匹配**：对纪要中的人名，与权威实体表做匹配
   - 精确匹配 → 通过
   - 同音/形近（如"作者A" vs "作者B"、"闫" vs "阎"）→ 标记可疑，输出候选纠正
   - 无匹配 → 标记为"未识别实体"（可能是真实新人物，也可能是 ASR 错误）
4. **术语校验**：对纪要中的关键术语（张量网络、纠缠熵、矩阵乘积态等），与 `concepts/` 概念页标题交叉核对
5. **输出**：在 lint 报告中单列"SR 实体校验"小节，列出可疑人名/术语及候选纠正

> 不要求逐字校对全部纪要，只输出与权威实体表不匹配的项，供用户针对性核对原文。

## 冲突分级处理流程

检测到 `Contradictions` / `Stale claims` / `Expired items` 时,按三级分级处理(对应 `INGEST.md` 冲突处理分支):

| 检测项 | 判定 | 处理 |
|--------|------|------|
| Contradictions(数字/数据矛盾) | 事实错误 | 以 raw 证据为准修正 wiki,记 log 标 `conflict-fix` |
| Stale claims(raw 推翻旧声明) | 事实错误 | 修正 wiki,updated 推进 |
| Stale claims(新旧都合法) | 版本演进 | 旧页标 `status: deprecated` + `superseded_by` 指向新版,Hub 时间线串联 |
| Expired items(行政过期/教学过学期) | 版本演进 | 标 `status: deprecated` + `superseded_by`(若有替代);无替代则仅标 deprecated 待补 |
| 结论不一致但无 raw 证据 | 视角补充 | 建对比页保留多视角,两边 `related` 互指,不强制合并 |

**输出**:lint 报告单列"冲突处理"小节,列出冲突页 → 判定类型 → 处理动作 → 是否已处理(待处理项标 ⚠️)。

## 时效性判定规则

遗忘策略极简方案落地——复用已有 `date`+`status` 字段,不新增字段(遵循设计原则:不重复造轮子+奥卡姆剃刀)。

| 域 | 判定规则 | 处理 |
|----|---------|------|
| academic | 不主动遗忘 | 事实性描述永久保留(学术可追溯原则);仅在更新策略版本演进/事实错误场景才被动降级 |
| admin/policies, admin/procedures | `status: active` 且 `date` 距今 > 3年 → 标记"疑似过期待审" | 人工确认:确实过期→`status: deprecated`;有新版→补 `superseded_by`;仍有效→更新 `date` 或加备注 |
| teaching | 按"过学期"判定(教学文件随学期失效) | 过学期且无延续 → `status: deprecated` |
| index 层 | 仅 prune 断链(指向已删页)、超预算分层兜底 | 不主动 prune 低频条目(查询频率非价值好代理,学术场景冷门内容某天可能关键) |

**3年阈值依据**【初始工程参数,后续由评测集校准】:高校政策更新周期通常 3-5 年,3 年提示人工审为保守阈值。判定为"疑似"非"确定",最终由人工确认——避免误降级损失可追溯性。

**学术场景不主动遗忘冷门内容**是差异化贡献(非缺陷):通用 agent(Du/Wu)倾向 learned forgetting,学术知识库主动选择不做,因查询频率不是价值的好代理。这是与通用 agent 记忆系统的本质差异,论文作诚实标注。

## 图相关检查（v4，2026-07-25 主数据化）

graph.db 是边唯一源（不再从 md Core Triples 段派生）。LINT 增图结构检查（运行 `.scripts/graph_metrics.py` + `graph_dump.py`）：

| 检查项 | 说明 | 命令 |
|--------|------|------|
| 图连通性 | 从 Raw 文档包节点 BFS；只有 frontmatter 声明了 `sources` 却不可达的 Wiki 才是结构缺口。无 sources 的 Hub、纯导航页和实体节点可不连 Raw，只作信息统计 | `graph_metrics.py connectivity` |
| 悬空边 | subject/object 指向不存在的 node（删页未清边） | 图查 edges subject/object NOT IN nodes |
| 双向冗余 | 逆谓词对（指导↔师从/受指导于）双向都存；应只留高优先方向（ingest 已自动去重，LINT 复核残留） | 图查逆谓词对 |
| 描述性短语节点/客体 | entity 节点名含括号，或边客体含谓语结构（为/是/导致/采用/基于）/中文标点且较长（脱离论文能否独立指代，见 INGEST.md「描述性短语校验」） | graph_ingest.py 已 WARN；LINT 补查 nodes LIKE '%(%' 及 edges object 匹配谓词/标点正则 |
| 主谓宾结构未拆分（2026-08-03 新增） | entity 节点含主谓宾结构（is_descriptive_phrase 命中）但没有 `predicate='拆分'` 的出边——摄入时 3.3b/3.6b 应已处理，此处是历史存量检查。只报告不自动修复，人工确认后补拆分三元组 | `graph_metrics.py unsplit_phrases` |
| 紧密簇→Hub 触发 | 无 Hub 覆盖的连通分量（>=4 节点），提示建 Hub（开发期即时提示，用户指令修补） | `graph_metrics.py tight_clusters --min-size 4` |
| 谓词报告 | GROUP BY predicate，发现低频/可合并谓词（count=1 待归一化） | `graph_metrics.py predicates` |
| 论文→方向边谓词 tier 登记 | 论文→研究方向 hub 的谓词须在 `predicate_tiers.yaml` 登记（未登记 = 新谓词，ingest 已 WARN + 默认 tier1）；LINT 全量复核未登记谓词清单，提示登记或归并 | 查 edges 中 object 为 research-direction hub 节点的谓词，比对 `predicate_tiers.yaml` keys，未登记告警 |
| `[存疑]` 边待审 | edge confidence=`[存疑]` 的边清单（失败显性化） | 图查 confidence='存疑' |
| aliases 碰撞 | 同 alias 指向不同节点（歧义，PRIMARY KEY 约束已防覆盖，需人工裁决去重） | 图查 aliases alias 出现多次 |
| Hub Scope 契约 | active 研究方向 Hub 的 path/title/Scope/parent 自洽；旧 Hub 缺 Scope 只读兼容，不做全库迁移告警 | `hub_semantics.py inspect <hub>` + 图结构查询 |
| orphan pages | 图入度=0 的页面节点（升级原孤立页检查） | 图查入度=0 |
| Hub 状态过滤 | archived/dormant 的 Hub 默认过滤（复用 deprecated 机制） | 图查 type=hub status |
| 参会边一致性（2026-07-26 新增） | 会议 wiki 参与者行有 people wikilink 但 graph.db 无对应参会边（漏建），或图有参会边但 wiki 无 wikilink（悬空边） | 扫 wiki 参与者行 wikilink vs graph.db 参会边，差异告警 |
| 别名表一致性（2026-07-26 新增; 2026-07-30 统一入 graph.db） | `graph.db` aliases 表中 `status=pending` 项是否长期未确认; confirmed 别名指向的 node_path 是否存在 | 扫 `aliases` 表 status=pending 残留告警; node_path 孤立告警 |
| Raw–Wiki 核心结构 | 进入知识库的 Raw 文件与 Wiki page 都有图中代表；原件与同 stem locator companion 合并为一个 Raw 文档包节点；每个 Wiki frontmatter `sources` 对应的 Raw 节点存在，并有 `Wiki → 来源 → Raw` 直连边。一个 Wiki 可连多个 Raw，一个 Raw 可被多个 Wiki 使用 | `.scripts/ingest_check.py <page> --graph` + 图结构查询 |
| 可选边 locator | `edges.source`/locator 不要求填写；缺 locator、缺 `edge_evidence` 均不报 ERROR/WARN，事实引用由 Wiki section 的 Raw 脚注承担。默认 LINT 不做历史全库 locator 完整率审计；排障时才显式运行 `audit_source_locators.py --strict` | 默认只统计，不阻断 |
| Raw 节点连通性 | 以 `Wiki → 来源 → Raw` 为核心连接检查 Raw 文档包是否有 Wiki 消费者；没有对应 Wiki 的合法孤立 Raw 仅作清单信息，不按证据缺失报错 | 图 BFS/来源边统计 |
| person-entity 数量治理 | `nodes.type=entity AND entity_subtype=person` 的 active 节点数；≥1600 预警，≥2000 触发低优先级候选进入 pending 队列；共同作者/明确高价值人物不阻断 | `.scripts/person_entity_audit.py` |

**开发期即时提示**：ingest 加边后 `graph_metrics.py` 跑紧密簇检测，即时提示新增紧密簇（状态存 graph.db metadata）。LINT 仍做全库周期扫作补充。

## 输出

结果保存到 `*/outputs/lint-YYYY-MM-DD.md`

## 提示词健康审计

`.scripts/lint_specs.py` 审计提示词规范文档（AGENTS.md / operations/*.md / */SCHEMA.md）的健康度：

- **C1** 元说明残留（"本文件定义…"）— ERROR
- **C2** 圆括号日期版本标记（""等过程性注解）— ERROR
- **C3** 原则引用注解（学术引用标签、原则编号注脚）— ERROR
- **C4** 过程性/历史性叙述（版本演进史、重构记录）— WARN
- **C5** section 标题日期后缀（`## xxx `）— ERROR
- **C6** 远期机制密度（section 内远期词≥3）— WARN
- **C7** route.py 映射命中（所有 task/mode/stage 截取成功）— ERROR
- **C8** 跨文件重复原则声明（原则词出现≥3处）— INFO

用法：`.scripts/lint_specs.py`（自动探测）/ `--paths a.md b.md` / `--no-c7`（跳过 route 校验省时）

校验闭环：改规范 → 跑本脚本 → 修全部 ERROR → 复验至 ERROR=0 → 完成。WARN/INFO 供人复核。
