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

上述候选不产生 ERROR/WARN。单一 embedding 分数只能发现候选，不能证明 Hub 应创建、分裂或合并——但摄入末期对达标候选簇（cohesion≥0.6 且 members≥4）自动触发 agent 生成 title/Scope/parent 并 create_hub（见下文「自动建 Hub」），不达标候选静默进 backlog。split/merge 仍只产候选，不自动写生命周期。

## 分裂

代码按类型化成员 profile 做 K=2 二分聚类，生成稳定多簇候选与每簇代表成员（top-5）。节点数量只触发分析，不构成分裂依据。**成员级区分度闸**：两簇质心 cosine < 0.85（`SPLIT_DISTINCTION_THRESHOLD`）才有效；≥ 0.85 判为不可分。

Agent 依据代表成员（揭示簇语义）+ 父 Hub Scope（子 Scope 须是父 Scope 的特化）为每子簇生成 title + Scope，写成 JSON plan 后 `split-apply --agent-confirmed` 运行三道验证闸：① Scope 区分度（两子 Scope cosine < 0.85）；② 路由探针（success ≥ 0.80, margin ≥ 0.03）；③ 通过后创建子 Hub + 迁移 membership。普通 ingest 不自动分裂。

## 血亲机制

三代 `子方向` 血亲 Hub 对禁止合并（`hub_semantics.has_blood_relation`），防合并-分裂死循环。`hub_consanguinity_audit.py` 已删除（dead code）；`姻亲` 边不恢复——retired Hub 不参与 merge 候选，`子方向` 三代追溯足够判定血亲。存量 `姻亲` 边为历史遗留，保留不动。

Hub 成员数 > 20（`HUB_MEMBER_LIMIT`）时 `dynamics_plan` 报告 `overloaded_hubs`：有子 Hub → 重分配；无子 Hub → 触发 split 候选。父 Hub 和子 Hub 同时命中某节点时，子 Hub 获 `+0.05`（`MEMBERSHIP_CHILD_BONUS`）加分，优先吸收成员。

## 合并

代码比较 Hub Scope 与成员原型，只输出候选。Agent 必须区分同义、上下位和相近但不同；仅同义重复适合合并。合并采用非破坏式 redirect：保留 retired Hub 文件和节点，建 `retired → 合并至 → survivor`。

## Agent 与代码职责

代码自动完成 profile 读取、向量计算、结构 affinity、重叠 membership、迟滞、原型、候选聚类、血亲追溯、超限检查和局部刷新。

主 Agent 负责：

- 确认候选是独立 Hub、上下位 Hub、同义 Hub 还是无需动作。
- 写或更新 title、Scope 和 parent。
- 显式确认 split/merge。
- 摄入末期 auto-create 触发时为达标候选簇生成 title/Scope/parent（不经 API LLM）。

API LLM 不决定成员、聚类、分裂、合并或 canonical Scope。普通成员变化不自动改 Scope。

## 操作入口

```bash
## 自动建 Hub

摄入末期 `ingest_inbox.py` 调 `hub_semantics auto-create --check`，对全图 unassigned 普通节点跑 `dynamics_plan(apply_membership=True)` + `analyze_new_hubs`。候选簇 cohesion≥0.6 且 members≥4 为达标，写入 `temp/hub-auto-create/<session>.json` 并在摄入报告设 `hub_auto_create.status=agent_required`，触发 agent（强模型）为每簇生成 title/Scope/parent，写定义文件后调 `hub_semantics auto-create --apply <file>` 由 `create_hub(agent_confirmed=True)` 落盘并 apply membership。不达标候选静默进 backlog，全流程不向用户报告。

```bash
# 检查达标候选（纯代码，不创建 Hub）
python3 .scripts/hub_semantics.py auto-create --check

# Agent 生成定义后创建 Hub
python3 .scripts/hub_semantics.py auto-create --apply <definitions.json>
```

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
```

create/split/merge 保持 `--agent-confirmed` 门：

```bash
python3 .scripts/hub_semantics.py create --path <path> --title '<title>' --scope '<scope>' --parent <parent> --agent-confirmed
python3 .scripts/hub_semantics.py split-plan <hub-path>
python3 .scripts/hub_semantics.py split-apply --parent <hub-path> --plan <agent-plan.json> --agent-confirmed
python3 .scripts/hub_semantics.py merge --survivor <hub> --retired <hub> --scope '<scope>' --agent-confirmed
```

## 兼容与迁移

旧 seeds、Hub 关键词、catch-all 和历史方向边只读兼容，不批量告警。根 Scope 可由 `arxiv-directions.yaml` 初始化，但 Hub 页落位后以页内 Scope 为准。历史清理必须另做显式、可恢复任务，不进入普通 ingest。

## 论文路由

论文 Wiki 仍保留可 locate 的 `## 研究方向定位`。程序只比较该句与 active Hub Scope；top-1 同时达到 threshold 和 margin 才写 `论文 Wiki → 主要研究 → Hub`，边 locator 指向该句。

这条边表达“论文的主要研究方向”，与可重建的 `普通节点 → 聚类于 → Hub` 不混用。
