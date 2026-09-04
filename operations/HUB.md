# Hub 创建与动力学

Hub 是一组语义相似或图上紧密联系的普通节点所形成的可重叠动态群落。它是导航结构，不是事实层；事实答案仍须经 Wiki locator 回溯 Raw。

arXiv 研究方向只是学术根 Hub 的初始化模板。Hub 后续的成员、交叉、新生、分裂和合并由实际知识结构驱动，不受 arXiv 分类永久限制。

## 最小模型

canonical Hub 只保留：

- 稳定 `path` 和简短 `title`。
- 必填的一句 `## Scope`，说明这组知识围绕的对象、问题或功能。
- 可选 `parent`。
- `active` 或 `retired` 状态。

`nodes.description` 只同步 Scope 供程序查询。Hub 不再以 seeds、关键词列表、catch-all 队列或单一质心定义身份。旧字段只读兼容，不产生迁移 ERROR/WARN。

```yaml
---
title: 张量网络方法
type: topic-hub
hub_subtype: research-direction
parent: null
status: active
---

# 张量网络方法

## Scope

研究利用张量网络表示和计算量子多体态及其动力学的问题。
```

Scope 保持单段 20–300 字，不写成员清单、论文结论或临时分类结果。

Scope 的语义来源是 Hub 内代表性成员的 profile：keyword 使用 `title + description`，proposition 使用 canonical statement，People 使用 `## 人物画像`。主 Agent 综合代表成员、必要的图结构信号及父 Hub Scope 写成稳定定义；不得把全体成员文本直接拼接或让 API LLM 摘要生成 canonical Scope。每次 create、define、split、merge 的 Scope 写入都在 `hub_scope_history` 保存当时的成员 profile 快照、相关 Hub 及前后 Scope，供生命周期审计；该记录是导航定义来源，不是事实证据。legacy Hub 尚无 `聚类于` membership 时，`legacy-scope-plan` 给出的代表论文必须由 `define-scope --evidence-node <page>` 显式传入，不能以空 evidence history 代替。

## 参与节点与 profile

当前程序直接支持三类普通节点：

- `keyword`：`title + description`。description 可选，只用于消歧和语义计算。
- `proposition`：canonical statement（当前存为 `title + description`）。它是比名词性节点更粗的知识单元。
- `people`：只有实际 People page 且存在可定位 `## 人物画像` 时参与。无 page 的 `entity_subtype=person` 不参与，姓名不得代替人物语义。

人物画像是一句导航性描述，不把人物强制定义为研究者：

- 研究人员：研究对象 + 问题 + 主要方法。
- 行政人员：职责 + 服务范围。
- 学生：学习/研究阶段 + 关注方向。
- 其他人物：写与知识库导航直接相关的角色和活动范围。

People page 没有画像时只是暂不参与 Hub，不得报错或告警。

## 成员归属

成员边为：

```text
普通节点 → 聚类于 → Hub
```

该边是可重建导航状态：`confidence=推断`，`score` 记录本次 affinity，`source` 留空。一个节点可同时属于最多三个 Hub。

程序综合：

1. 节点 profile 与 Hub Scope 的语义相似度。
2. 节点 profile 与该 Hub 同类成员原型的语义相似度。
3. 节点与现有成员的图邻接 affinity。People 在有结构信号时更依赖此项，keyword/proposition 更依赖语义项。

新加入阈值高于保留阈值，避免边界节点来回抖动。embedding 不可用时保留现有成员边，不用 lexical 候选写图。未归类和多义归类都是正常状态。

普通摄入只刷新被摄入 Wiki 一跳范围内的有效 profile，不在每次摄入全库计算。

## Hub 动力学

节点数量变化只是“运行检查”的便宜触发器，不是 create/split/merge 依据。程序生成：

- `new_hubs`：尚未归类且内部稳定的候选簇。
- `splits`：成员已呈稳定多簇的 Hub 候选。
- `merges`：Scope + 成员原型过近的 Hub 对。
- 各候选的代表节点、类型和分数，供 Agent 做语义判断。

上述候选不产生 ERROR/WARN。单一 embedding 分数只能发现候选，不能证明 Hub 应创建、分裂或合并。统一 inbox 收尾对达标新 Hub 候选，以及成员超限且通过稳定二分闸的既存 Hub，自动触发主 Agent 定义 canonical 语义并调用受控写入口；不达标候选静默进 backlog。merge 仍只产候选，不自动写生命周期。

## 分裂

代码按类型化成员 profile 做 K=2 二分聚类，生成稳定多簇候选与每簇代表成员（top-5）。节点数量只触发分析，不构成分裂依据。**成员级区分度闸**：两簇质心 cosine < 0.85（`SPLIT_DISTINCTION_THRESHOLD`）才有效；≥ 0.85 判为不可分。

Agent 依据代表成员（揭示簇语义）+ 父 Hub Scope（子 Scope 须是父 Scope 的特化）为每子簇生成 title + Scope，写成 JSON plan 后 `split-apply --agent-confirmed` 运行三道验证闸：① Scope 区分度（两子 Scope cosine < 0.85）；② 路由探针（success ≥ 0.80, margin ≥ 0.03）；③ 通过后创建子 Hub，把 plan 中成员的直接 membership 从父 Hub 迁至对应子 Hub。family 外的重叠 membership 保留。单文件底层 ingest 只产候选；统一 `ingest_inbox.py` 收尾把达标分裂候选交给主 Agent，并在同一摄入任务中执行该受控入口。

## 血亲机制

三代 `子方向` 血亲 Hub 对禁止合并（`hub_semantics.has_blood_relation`），防合并-分裂死循环。`hub_consanguinity_audit.py` 已删除（dead code）；`姻亲` 边不恢复——retired Hub 不参与 merge 候选，`子方向` 三代追溯足够判定血亲。存量 `姻亲` 边为历史遗留，保留不动。

根 Hub 与子 Hub 均在成员数 > 30（`HUB_MEMBER_LIMIT`）时，`dynamics_plan` 才报告 `overloaded_hubs`：有子 Hub → 重分配；无子 Hub → 触发 split 候选。父 Hub 和子 Hub 同时命中某节点时，子 Hub 获 `+0.05`（`MEMBERSHIP_CHILD_BONUS`）加分，优先吸收成员。

## 合并

代码比较 Hub Scope 与成员原型，只输出候选。Agent 必须区分同义、上下位和相近但不同；仅同义重复适合合并。合并采用非破坏式 redirect：保留 retired Hub 文件、节点与原 Scope，建 `retired → 合并至 → survivor`；retired Hub 的直接 membership 迁至 survivor，family 外重叠 membership 保留，survivor 使用 Agent 确认的统一 Scope。

## Agent 与代码职责

代码自动完成 profile 读取、向量计算、结构 affinity、重叠 membership、迟滞、原型、候选聚类、血亲追溯、超限检查和局部刷新。

主 Agent 负责：

- 确认候选是独立 Hub、上下位 Hub、同义 Hub 还是无需动作。
- 写或更新 title、Scope 和 parent。
- 显式确认 split/merge。
- 摄入末期 auto-create 触发时为达标候选簇生成 title/Scope/parent（不经 API LLM）。
- 摄入末期 auto-split 触发时按候选簇代表成员生成子 Hub title/Scope，原样保留程序给出的 members，并调用 `split-apply --agent-confirmed`。
- 对低 margin 或子方向特异性不足的路由选择当前 canonical 候选，并调用 `route-apply --agent-confirmed --transaction-id <txn>`；对既有子 Hub 补 Scope 后调用 `redistribute --agent-confirmed`。

API LLM 不决定成员、聚类、分裂、合并或 canonical Scope。普通成员变化不自动改 Scope。

create、define-scope、split、merge 与 batch-create 的 apply 共享同一生命周期边界：先快照目标 Hub Markdown，再在 SQLite `SAVEPOINT` 内修改文件、节点、Scope history、membership 与关系，并校验 Hub 页存在、图节点类型以及页内 Scope 与节点 description 一致；任一步失败都会回滚 SQLite 并恢复或移除本次写入的文件。成功结果携带 `hub-lifecycle-v1` 回执，内容寻址绑定 operation、参数、目标 Hub 与前后文件 hash。`agent-confirmed` 仍只是显式授权门，回执中的 `identity_attested=false` 不冒充不可伪造身份认证；`outer_commit_required=true` 表示调用方仍负责外层 commit。

## 操作入口

## inbox 收尾自动维护

摄入末期 `ingest_inbox.py` 汇总各成功文件的 `hub_dynamics.affected_nodes`，再调 `hub_semantics auto-create --check --node ...` 做增量规划：

- unassigned 候选簇 cohesion≥0.6 且 members≥4 时写入 `temp/hub-auto-create/<session>.json`，主 Agent 生成 title/Scope/parent 后调用 `hub_semantics auto-create --apply <file>`。
- canonical Hub 的 `聚类于` 成员数超过上限、尚无子 Hub，且 `analyze_split` 通过成员数量、小簇稳定性和质心区分度闸时，写入 `temp/hub-auto-split/<session>.json`。主 Agent 为每簇生成 title/Scope、原样使用候选 members，随后调用 `hub_semantics split-apply --parent <hub> --plan <file> --agent-confirmed`。
- 已有子 Hub 的超限父 Hub 写入 `temp/hub-auto-redistribute/<session>.json`。handoff 列出 canonical Scope readiness、blockers 及受控命令；任一子 Hub 缺正式 Scope 时先用 `define-scope --agent-confirmed` 定义，随后才允许 `redistribute --agent-confirmed`。
- 论文 canonical Scope 路由达到 floor 但 margin 不足时写入 `temp/hub-route-review/<session>.json`，只保留 canonical 候选和 `route-apply` 命令模板交主 Agent判断，不自动写方向边。
- membership 先汇总并去重 profile、Scope 与 prototype 文本，再单次进入 embedding cache；provider 可按 batch 上限分块，但禁止逐节点串行请求 API。维护超过 120 秒返回可重试 `deferred`，不得改变文件摄入终态。
- `redistribute` 在一次 Agent 确认下执行有界单调迭代；新子 Hub 成员形成 prototype 后可继续吸收残留父成员，直到无新增迁移或父 Hub降到上限。任一轮 embedding 不可用则整次回滚。
- 紧凑摄入结果保留文件摄入终态，并另带有界的 `hub_maintenance` 交接摘要；维护候选不得把已完成文件改报为失败或 `agent_required`。

不达标候选只计入 backlog，不向用户报告。canonical 定义仍只由主 Agent 确认，API LLM 不参与 Hub 生命周期决策。

```bash
# 检查达标候选（纯代码，不创建 Hub）
python3 .scripts/hub_semantics.py auto-create --check

# Agent 生成定义后创建 Hub
python3 .scripts/hub_semantics.py auto-create --apply <definitions.json>
```

```bash
# 查看一个节点的类型化 profile
python3 .scripts/hub_semantics.py profile <node-id>

# 全库 dry-run；不写图
python3 .scripts/hub_semantics.py dynamics-plan

# 只幂等写入可重建 membership，不应用生命周期候选
python3 .scripts/hub_semantics.py dynamics-plan --apply-membership

# 限定受影响节点
python3 .scripts/hub_semantics.py dynamics-plan --node <node-id> --node <node-id>

# 查看 Hub Scope、类型化成员和 split candidate
python3 .scripts/hub_semantics.py inspect <hub-path>

# 列出 legacy Hub 的可用成员 profile；证据不足项保持 blocked，不猜 Scope
python3 .scripts/hub_semantics.py legacy-scope-plan
```

create/split/merge 保持 `--agent-confirmed` 门：

```bash
python3 .scripts/hub_semantics.py create --path <path> --title '<title>' --scope '<scope>' --parent <parent> --agent-confirmed
python3 .scripts/hub_semantics.py split-plan <hub-path>
python3 .scripts/hub_semantics.py split-apply --parent <hub-path> --plan <agent-plan.json> --agent-confirmed
python3 .scripts/hub_semantics.py merge --survivor <hub> --retired <hub> --scope '<scope>' --agent-confirmed
python3 .scripts/hub_semantics.py define-scope --hub <hub> --title '<title>' --scope '<scope>' --evidence-node <representative-node> --agent-confirmed
python3 .scripts/hub_semantics.py redistribute --parent <hub> --agent-confirmed
python3 .scripts/hub_semantics.py route-apply --page <paper> --hub <canonical-hub> --agent-confirmed
```

## 兼容与迁移

旧 seeds、Hub 关键词、catch-all 和历史方向边只读兼容，不批量告警。根 Scope 可由 `arxiv-directions.yaml` 初始化，但 Hub 页落位后以页内 Scope 为准。历史清理必须另做显式、可恢复任务，不进入普通 ingest。

## 论文路由

论文 Wiki 仍保留可 locate 的 `## 研究方向定位`。程序只比较该句与 active、具有正式 `## Scope` 的 canonical Hub；legacy title 只用于候选诊断。canonical top-1 同时达到 floor 与 margin 后，若它是子 Hub，还必须在论文 profile 中命中扣除父 Scope 后的子方向特异性词项；否则以 `child_specificity_unsupported` abstain。达到 floor 但 margin 不足时同样 abstain，二者都生成 route-review handoff。

主 Agent 复核后，`route-apply` 仅接受当前候选榜中的 active canonical research-direction Hub，并替换该论文既有同谓词 Hub 路由边；新边同时记录 `研究方向定位` evidence、普通 origin 与 `agent-confirmed:<locator>` origin，重摄入清理时保留。传入 `--transaction-id` 时，原事务同时写入 `route_corrections`、当前路由快照和派生 `quality_status`；后续 route 查询返回 `agent_confirmed_override`，但仍保留自动门禁结果供审计。它不降低自动 floor/margin。

这条边表达“论文的主要研究方向”，与可重建的 `普通节点 → 聚类于 → Hub` 不混用。
