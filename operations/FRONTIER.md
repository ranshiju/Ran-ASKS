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

## 4. 触发规则

- **强触发**：用户提出学术问题；用户要求保存思路/假设；用户确认有价值；实验或编程产生支持、否定或不确定结果。
- **论文触发**：论文 ingest 成功后，程序仅从 Raw 提取作者明示的 open question、future work、limitation 或未解决问题，单篇最多 3 条；每条自动建或复用 Question Page，并非阻断地尝试一次库内回答。
- **更新触发**：新入库事实命中现有 fact anchors 时标记 `possibly_stale`；新建、精确复用或显式 `answer/refresh` 才重答，不能自动修改 `scientific_state`。
- **禁止触发**：普通 query 无命中、单纯关键词相似、每篇论文的无约束 AI 发散、行政/教学内容。

## 5. 控噪与生命周期

状态：`captured → triaged → active`，旁路为 `parked/resolved/rejected`。

- 每个捕获问题都有 Question Page；不是每个 Question 都成为 active。
- 规范化文本唯一命中时自动复用；embedding 只给重复候选，不自动合并。已充分回答且无残余缺口的问题保留页面并转为 resolved。
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
