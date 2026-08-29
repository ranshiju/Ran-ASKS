# cross-domain/ — 跨域关联规范

> Topic Hub、知识三元组索引和跨域引用的规则文件。
> Hub 创建/维护的详细规范见 `operations/HUB.md`。

---

## Topic Hub 文件结构

> Topic Hub 是**通用导航层**：无损路由指针（非综述正文）、无天花板、可多级生长。横向主题聚合与纵向更外层导航视图（L3 领域地图）都是其实例，触发规则见 `operations/HUB.md`。

| 文件 | 用途 |
|------|------|
| `cross-domain/topics/_index.md` | 主题注册表（轻量索引，Query 先读此文件） |
| `cross-domain/topics/<主题名>.md` | 单个主题的 Hub 页面（横向聚合或纵向 L3 导航视图） |

## Hub 页面 Frontmatter

```yaml
---
title: "主题名称"
type: topic-hub
status: active | dormant | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
page_count: N                      # 关联页面数
---
```

---

## 跨域知识引用格式

当概念或文件涉及多个子项目时：

1. 在各子项目的 wiki 页面中正常引用
2. 在 `cross-domain/README.md` 中记录跨域关联
3. 跨域关联格式：

```markdown
## 跨域主题名称
- academic: [[../academic/wiki/concepts/xxx]]
- admin: [[../admin/wiki/policies/xxx]]
- teaching: [[../teaching/wiki/topics/xxx]]
- business: [[../business/wiki/research/xxx]]
```

## 知识图谱（graph.db，v4 主数据化，2026-07-25）

graph.db 是边唯一源（不再有 `triples*.md`/`keyword-index*.md` 派生文件，已删）。md 存节点属性，graph.db 存边。

无 page 人物使用 `nodes.entity_subtype=person` 标记，便于与 citation-only、venue、institution 等 entity 区分。活跃 person-entity 软上限为 2000，1600 预警；治理队列为 `cross-domain/outputs/people-pending.yaml`。

| 组件 | 用途 |
|------|------|
| `cross-domain/graph.db` | 边的唯一源（nodes + edges + aliases 表，进 git） |
| `.scripts/graph_ingest.py` | ingest 增量加边（resolve 裸名 + 去双向冗余 + alias 自动识别 + INSERT） |
| `.scripts/graph_dump.py` | 图文本快照（人读，替代原 md Core Triples 段） |
| `.scripts/graph_lib.py` | 共享库（resolve/去重/schema） |
| `.scripts/query_graph.py` | 查询（search/neighbors/relations/hub_of/path_exists） |

关系类型见 `operations/INGEST.md`，查询策略见 `operations/QUERY.md`，LINT 检查见 `operations/LINT.md`。

### 节点与 Hub 分类

节点 type 按 `operations/INGEST.md`「节点类型」表。Hub 类型分四类，聚合机制不同：

| Hub 类型 | type 值 | 聚合机制 | 典型实例 |
|----------|---------|---------|---------|
| 主题聚合 | `topic-hub` | 语义谓词边（人工策展） | 横向主题、L3 导航视图 |
| 时间线聚合 | `timeline-summary` | 语义谓词边 | 行政时间线 |
| 期刊/会议场所 | `entity` | `发表于` 语义边逆遍历 | PRB、PRL |
| 单位/机构 | `entity` | `就读`/`所属` 语义边逆遍历 | 首都师范大学 |

venue / institution 的「内容」是它的边本身（不另写正文）；具体期刊/单位各为一个 entity 节点，ingest 时首次遇到按需自动建。期刊与单位维度独立。有属性需记时建极简 md（frontmatter only），无属性时仅 entity 节点。

### raw 节点与事实支撑（2026-07-26）

- **raw 节点**：所有 raw 文件（论文 paper.md / 会议 corrected.md / 简历 md 等）建 `raw` 类型节点进图，是结构节点（非知识节点）
- **事实支撑边**：`raw节点 → 事实支撑 → 主消费节点`（1 条边/raw），保证事实连通性在图结构里可 BFS 遍历
- **source 字段**：事实边的 `source` 存 `raw节点path#section`（段落级精确，不是裸文件路径）
- **source locator**：统一格式为 `path#locator`；文本源 locator 需能对应 heading/锚点，PDF/DOCX locator 使用页码但允许标记为不可机械核验；外部 `synology://` 源在当前环境不可访问时单独记录为外部缺口。
- **简历特殊**：简历不建 wiki 摘要页，raw 直接充当 wiki；people 节点是主消费节点
- **查询过滤**：`raw` 类型节点和 `事实支撑` 谓词在语义查询时默认过滤，LINT 连通性检查时启用
- 详见 `operations/INGEST.md`「raw 节点与证据完整性」

### 动态 hub 生长机制（2026-07-26）

对于无预定义结构的文件类型（如会议纪要，区别于论文有 arXiv 方向可对齐），采用**涌现式 hub 生长**：

- **catch-all hub**：普通 topic-hub（无 hub_subtype），命名"<文件类型>关键词"（如"会议纪要关键词"），存 `*/wiki/hubs/<名称>.md`。未归类 keyword 的临时池
- **keyword 归属**：提取的 keyword 先查现有 hub 正文 `## 关键词` 段——命中则已在 hub 内；未命中 → 进 catch-all hub
- **聚类触发**：catch-all hub `## 关键词` 段达 100 个 keyword → 触发聚类（`.scripts/cluster_keywords.py`）：GLM-Embedding-3 批量 embedding → 层次聚类 → 最大簇（≤30）拆出建新 hub → LLM 语义命名
- **查重合并**：新 hub 与已有 hub 的 keyword 集合 embedding 质心距离 < 阈值 → 提议合并；合并后 ≤100 才合并，超限不合并仅区别命名
- **持续接收**：聚类拆分后 catch-all hub 继续接收新 keyword
- **推广性**：各文件类型可各建一个 catch-all hub（如"行政文档关键词""教学文档关键词"）
- 详见 `operations/INGEST.md`「会议纪要 keyword + 动态 hub 生长」段


---

## 语义原型与 Schema 包（v5，2026-07-27）

> 统一处理流程，不统一信息结构。通用底座（证据/导航/主题）所有文档共享；语义提取层按文档信息行为分流。

### 六种语义原型

| 原型 | 信息行为 | 适用文档 | 实现状态 |
|------|----------|----------|----------|
| 实体型 | "描述谁/什么" | 简历/人物/机构/产品 | ✓ |
| 事件型 | "发生了什么" | 会议/活动/实验日志 | ✓ |
| 规范型 | "必须/可以/不得做什么" | 制度/政策/通知/合同 | ⏳ 行政入图时实现 |
| 过程型 | "怎么做" | SOP/操作手册/流程 | 远期预留 |
| 项目型 | "要达成什么" | 项目方案/工单/需求 | 远期预留 |
| 数据型 | "测得多少" | 报表/表格/实验数据 | 远期预留 |

一篇文档可同时命中多个原型（会议纪要=事件+规范+任务）。

### Schema 包（记录级提取目标）

每个 Schema 包定义该类文档的必填槽位。ingest 时 LLM 先判断原型，选 Schema 包，按槽位提取信息记录：

| Schema 包 | 必填槽位 | 选填 |
|-----------|----------|------|
| 通用 | 标题/时间/来源/版本 | 实体/主题 |
| 实体型 | 主锚点/属性/稳定关系 | 别名 |
| 事件型 | 参与者/时间/议题 | 决定/行动项/结果 |
| 规范型 | 发布者/适用对象/生效期 | 义务/禁止/例外/失效期/版本 |
| 过程型 | 步骤/输入/输出 | 前置条件/负责人/异常 |
| 项目型 | 目标/负责人/状态 | 里程碑/截止/依赖/交付物 |
| 数据型 | 指标/数值 | 单位/时间区间/计算口径 |

### 拒绝判断

LLM 判断原型时可输出 `type: unknown`（无法归类）。此时：
- **ingest 端**：建 raw 节点 + 事实支撑边（保证连通），文档进待归一队列，不强行提取关系/keyword
- **query 端**：命中 unknown 文档时，仍返回图里已有的最相关连通信息，标注"该文档结构尚未完全识别"——**拒绝判断 ≠ 拒绝提供已有信息**

### 证据先于关系

ingest 时 LLM 先定位原文证据段，再从证据段推导关系。source 字段自然非空（关系从证据段推导，不是先想出边再找来源）。

### qualifier 字段（edges 表，可选）

规范型/数值型/任务型边可附加限定信息（普通边不用）：
- `valid_from`/`valid_to`（政策生效期）
- `condition`（条件限定）
- `unit`/`value`（数值型）
- `status`（任务状态）

### 文档内共指

LLM 读全文后统一指代（"张教授"和"冉仕举"是否同人），再 resolve_bare_name 归一到全库。不逐段独立处理。

### 兜底机制

无法归入任何原型时走"通用文档"兜底：建轻量 document 节点（只存标题/类型/日期/状态/raw 指针）+ raw 事实支撑 + keyword 进 catch-all（待归一队列）。

### 开源扩展

其他用户可写自定义 Schema 包扩展语义提取层，通用底座不变。6 种原型是参考集非封闭集。

## 标准 section 结构

> cross-domain 的索引文件非知识内容页,**不适用**内容页三段规范。Topic Hub 页面是导航聚合页,不强制三段。跨域引用的各子项目内容页适用各自 SCHEMA 的标准 section 规范。

## 图查询局部读（v4，2026-07-25 主数据化）

> 原 `triples*.md`/`keyword-index*.md` 已删，融进 graph.db。局部读改为图查询：

| 查询类型 | 命令 | 说明 |
|---------|------|------|
| 关键词定位 | `query_graph.py search <term>` | 查 nodes.title + aliases 表 |
| 关系召回 | `query_graph.py relations <node> [--predicate P]` | 某节点的边（可按谓词过滤） |
| BFS 邻接 | `query_graph.py neighbors <node> --depth N` | 多跳关联（按 confidence 排序） |
| Hub 归属 | `query_graph.py hub_of <page>` | 沿语义谓词边反向查 `type=hub` 节点 |
| 连通性 | `query_graph.py path_exists <from> <to>` | 两节点是否连通 |

**方法选择**(由 agent 判断,不强禁):
- `read_section` 按 `##` 整段:首选,天然连贯(整段含表头+同主题行)
- `grep '^## '` 定位段名:不知有哪些段时先定位再 read_section
- `grep` 取内容行:仅当结果能保持连贯块时可用(如带 `-A/-B` 上下文且命中行构成完整表);纯裸行匹配会丢表头/丢同主题/漏同义别名,**应避免**
- 整读:体量小(≤ ~3k token)时收益大于局部读

**体量阈值**:单文件 > 12KB(~3k token)时,局部读收益超过整读成本,触发按 `##` 分段。
