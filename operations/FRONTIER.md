# Frontier — 研究前沿层规范

> Frontier 管理“尚待理解和推进的研究状态”，包括研究线程、历史演进轨迹、部分答案、残余缺口、候选思路与验证记录。它引用事实层，但不是事实层。

## 1. 层级边界

```text
Raw 文档包 ←来源— Wiki → 其他导航节点   （均登记在 cross-domain/graph.db）
             ↓ 单向引用
academic/frontier/                     研究前沿 overlay
```

Frontier 主数据目录为 `questions/`（一问题一页）和 `trajectories/`；旧 `intake/threads/` 仅作迁移读取兼容。

- `Raw/Wiki/graph.db` 回答“知识库已经知道什么”；事实答案仍回 Raw。
- Frontier 回答“问题如何演进、目前缺什么、有哪些候选思路与尝试”。
- Frontier 只保存指向 Raw locator、Wiki path、Graph node path 的 `fact_links`；不得向事实 `graph.db` 写 Frontier 节点或反向边。
- `academic/frontier/frontier.db` 仅是 Markdown 主数据的可重建 FTS/导航索引，不是事实源或第二知识库。

## 2. 主对象

### 2.1 Question Page

每个被捕获的开放问题立即拥有一个独立 Question Page；页面存在只表示本库记录了该问题，不表示其已被认定为活跃或科学上未解决。页面贯穿 `captured → triaged → active → resolved/parked/rejected`，至少包含：规范问题、来源表述、`kb_state`、`scientific_state`、本库当前回答、回答依据、残余缺口、价值理由、事实锚点和审查状态。

### 2.2 Trajectory

演进轨迹是历史纵剖面，允许事件分叉与汇合。时间、论文结果等直接证据标 `sourced`；“开启路线”“导致转折”等历史解释标 `synthesized`/`derived` 并保留依据。时间先后不得自动提升为因果。

### 2.3 Entry

Question/Trajectory 内的原子条目类型包括 `partial_answer`、`candidate_answer`、`residual_gap`、`hypothesis`、`approach`、`prediction`、`test_plan`、`test_result`、`critique`、`status_change` 及历史事件。条目保存 `origin_kind`、`epistemic_status`、证据和审查状态。

## 3. 用户问题准入

用户提出学术问题是强触发。入口必须先：

1. 保存原始表述、时间和用户归因；
2. 用事实 Graph 导航、Wiki 综合、Raw locator 核验构造知识库证据包；
3. 检查 Frontier 重复或上下位问题；
4. 区分 `kb_state` 与 `scientific_state`；
5. 形成规范问题、已有知识、残余缺口、价值理由和事实锚点；
6. 写入或复用 Question Page，并在本库内做一次有界回答尝试；只有通过准入门才成为 `triaged`，`active` 必须由用户审查确认。

`kb_state` 枚举：`unassessed/no_evidence/no_answer_found/partial/conflicting/answered`。`scientific_state` 枚举：`unverified/likely_open/partially_resolved/contested/likely_resolved/resolved`。知识库没有命中只能说明本库覆盖不足，不能证明科学问题仍开放。

库内回答按 Graph → Wiki → Raw locator 构造紧凑证据包；`supported_claim` 必须引用包内 Raw locator，推导只能标为 `derived`。Question 的事实锚点只固化问题来源和实际被支持结论引用的 Raw，低相关召回候选不转成 `fact_links`。回答失败或模型不可用时保持 `answer_status: pending`，不阻断事实摄入，也不产生摄入告警。

自动回答/刷新已有问题时，先把 `source_locator` 与 `source_mentions` 中去重后的最多 3 个问题来源放入证据地址队列，再并入通常的 Wiki/Graph 召回；不依赖来源论文重新进入文字搜索前列，也不把旧答案累计引用自动当成本轮证据。保留最多 8 个候选地址、5 个有效摘录；问题自身来源摘录最多 4000 字符，一般召回摘录最多 1000 字符，达到预算时在 `evidence_issues` 披露可能截断。API prompt 保留已选摘录，不再二次截断为头部 420 字符；引用白名单与实际送入模型的摘录对应。显式提供证据包时只使用该包，不额外加入来源。来源优先读取不代表它更正确，作者提出的问题本身也不是问题的答案。

精确行号范围与章节片段通过共享 `source_locator` 读取，不能在同一精确地址下返回论文开头或摘要。公共域路径与 companion 绑定沿现有能力检查；失效来源地址不降级冒充已读证据。`evidence_issues` 在证据包中披露未取得片段的地址，自动构造的支持结论白名单只含实际取得摘录的地址。传统 `#全篇` 导航来源仍只提供有限摘要/开头预览，不表示已读全文；需要方法、结果或条件时仍按具体缺口下钻。Agent 读取任务包，API adapter 在原回答调用中收到压缩证据与读取问题，不新增调用。

### 3.1 阶段性回答与证据变化

答案只代表当前已摄入、且本次实际检索核验到的证据下的判断。用户选定的摄入范围、本次检索范围和学科知识状态是三件事；`answered` 表示当前问题在本次证据范围内得到回答，不表示穷尽文献或科学上最终解决。`coverage_note` 说明适用对象、实际依据及未覆盖范围，不要求知识本身消除所有模糊性。

共享 `apply_answer` 在同一 Question Page 的 `answer_history` 追加版本，保存答案、支持结论及 Raw 引用、推导、缺口、范围说明、完成时间与刷新原因。`evidence_scope` 区分候选路径、证据包提供的 Raw 地址和片段哈希、答案声明实际引用的地址；提供片段不等于 Agent 已核验全文，API 还会压缩输入。`packet_built_at` 只表示该包构造时间，缺失则为空，不冒充完整语料快照日期。版本、索引和历史回答均不是事实源。

- 回答正文、引用或提供的证据范围变化时留新版本；相同答案在相同范围内重复提交不重复追加条目。显式变化说明或待复核原因也可留下新版本。文本指纹只表示文本变化，不裁决科学结论是否改变。
- 刷新沿用原有一次回答调用：Agent 读取完整任务包，API adapter 接收压缩包；两者都得到 `previous_answer` 作为比较对象。旧答案不能替代本轮 Raw 证据，也不会自动进入当前引用白名单。
- 可在原输出附 `change: {kind, reason}`：`initial/unchanged/supplemented/strengthened/qualified/contradicted/uncertain/not_assessed`。语义执行主体比较新旧证据及条件后解释理由；不确定可以保留，未提供则记录 `not_assessed`，不增加摄入校验门槛或额外模型调用。
- 旧版无完整历史的页面，在首次刷新时保存可恢复的旧投影并标 `legacy_unversioned`；不从累计条目猜测逐版证据。刷新失败保留旧答案、旧成功核验时间和待复核标记。问题准入评估不覆盖已完成的证据约束回答。
- 新准备的 Agent 刷新包绑定当时的旧回答；提交时若旧回答已被其他刷新替换，重新准备比较，避免用过时比较对象覆盖当前版本。相同已提交任务可无写入重放，不清除随后到达的待复核提醒。

## 4. 触发规则

- **论文正文边界与未来语境**：shared 抽取跳过有标题（References/Bibliography/参考文献）或作者式编号条目引出的书目区及其续行；遇到后续正文标题、End Matter 或附录恢复读取，并保持原始 Raw 行号。普通编号问题不作书目。作者 `we expect/anticipate` 只有同段明确未来上下文，或同句进一步工程/优化等改进语境，才作为未来候选；当前工作、过去预期、否定及普通数值预期不因这些表达自动捕获。此边界与候选识别均为确定性处理，不新增模型调用或自动科学状态判断。
- **强触发**：用户提出学术问题；用户要求保存思路/假设；用户确认有价值；实验或编程产生支持、否定或不确定结果。
- **论文触发**：论文 ingest 成功后，程序仅从 Raw 提取作者明示的 open question、future work、limitation 或未解决问题，单篇最多 3 条；显式 `we conjecture`、仍未证明的 conjecture 与具有 future/looking ahead 等未来上下文的研究意向也进入同一确定性抽取。仅转述过去猜想、否定猜想、同句已证明/反驳或本论文当前研究目标不因这些新表达自动捕获。按句子和枚举项拆分，遇到 this/that/such 或“该/这种/上述”等依赖指代时保留同一有界原文段落，不猜测指代、不新增模型改写。每条自动建或复用 Question Page，仍绑定原文单元和 Raw locator 并按既有 source_mentions 幂等去重。摄入收尾继承事务后端：Agent 仅确定性捕获，回答保持 pending，由宿主按需调用现有 `answer`；API 可非阻断尝试一次库内回答，事件携带原摄入 `transaction_id`，不受独立 `QUERY_BACKEND` 配置改道。
- **更新触发**：论文摄入后的 `capture-paper` 把该页、Raw 来源文件及图相邻目标交给现有锚点匹配；Raw 行号/章节引用按同一来源文件匹配，不以前缀匹配其他文件。命中标记 `possibly_stale` 并保留原因；它仅表示需要复核，不表示结论过时或被反驳。新建、精确复用或显式 `answer/refresh` 才重答，不能自动修改 `scientific_state`。
- **相关旧问题候选**：同一次 `capture-paper` 复用 Wiki locator 的英文词/中文双字片段，对新页 title + Navigation 与已登记问题、当前回答和残余缺口作词汇匹配；两侧各截取最多 4000 字符，至少两个重叠词，按归一化重叠分数排序。默认返回最多 3 个候选，函数上限 5 个；排除 parked/rejected，以及本次新建、精确复用或已直接标记待复核的问题。不读累计历史条目作匹配，不增加模型调用。
- 候选随既有摄入回执返回，包含命中词、`lexical_score`、总匹配数、返回数和截断标记。分数是导航排序信号，不是相关性置信度；可能误召或漏召。候选本身不改答案、`possibly_stale`、`fact_links` 或科学状态，不自动建边或重答。Agent 按具体问题读取相关 section、核验 Raw 后，才在已有更新授权内决定是否通过原回答入口刷新；旧答案只用于比较。复核结果写工作报告，保留原摄入回执。
- **禁止自动状态或事实写入的触发**：普通 query 无命中或单纯关键词相似不能据此判定问题有科学价值、旧答案失效、事实关联成立或答案需要改写。词汇相似仅可用于上述只读导航候选。每篇论文的无约束 AI 发散及行政/教学内容不进入 Frontier 论文触发。

这仍是已登记 Question 的有界机制：未与既有锚点相连的新文献可能返回词汇候选，但不保证召回相关问题，也不会因此自动产生待复核标记；普通历史查询不自动转为追踪对象。需要时显式刷新并重新召回；不声称已有全库历史答案自动影响识别，也不因每篇新论文到达重答所有问题。

## 5. 控噪与生命周期

状态：`captured → triaged → active`，旁路为 `parked/resolved/rejected`。

- 每个捕获问题都有 Question Page；不是每个 Question 都成为 active。
- 规范化文本唯一命中时自动复用；embedding 只给重复候选，不自动合并。已充分回答且无残余缺口的问题保留页面并转为 resolved。
- 已绑定 `source_mentions` 的同页、同 Raw locator 原文单元参与捕获幂等检查；补齐来源上下文或规范化标题后不重复捕获，历史原始表述与回答版本保留。
- 默认导航只显示 `triaged/active`；candidate 和 parked 通过显式参数查看。
- AI 输出默认 candidate；embedding 只给重复/关系候选，不自动合并或建边。
- `sourced` 条目必须有 Raw locator；`derived/speculative/untested` 必须明确标记，不得伪装为文献事实。
- 定期检查重复、孤立锚点、长期未复核、无残余缺口的 active Question 和无可操作内容的条目。

## 6. 使用入口

```bash
python3 .scripts/frontier.py init
python3 .scripts/frontier.py ask --question "..."
python3 .scripts/frontier.py list
python3 .scripts/frontier.py show <ID>
python3 .scripts/frontier.py answer <ID>
python3 .scripts/frontier.py review <ID> --status active
python3 .scripts/frontier.py capture-paper academic/wiki/papers/<paper-id>
python3 .scripts/frontier.py migrate-questions
python3 .scripts/frontier.py rebuild

python3 .scripts/wg.py frontier ask "..."
python3 .scripts/wg.py frontier list
python3 .scripts/wg.py frontier show <ID>
python3 .scripts/wg.py frontier answer <ID>
```

真实验证结果或论文答案若要进入知识底座，必须另走正常 ingest；Frontier 只追加 `supported_by/answered_by` 链接并保留原始推导历史。
