# ADR-005：借鉴 Semantica 的图约束、可追溯与时间有效机制，不引入 RDF 或重型推理

> 状态：已批准（2026-08-23）
> 影响：graph_lib.py / graph_ingest.py / ingest_check.py / predicate_governance.py / query_graph.py /
> graph.db schema / operations/engineering/graph.yaml（在后续实现阶段落地）

## 背景

评估 [semantica-agi/semantica](https://github.com/semantica-agi/semantica) 后确认，其思想与本仓库的
`raw → wiki → graph.db` 模型同频，但实现目标偏企业 RAG 与多图存储，不能整体照搬。

可借鉴的高价值部分是三类确定性治理能力：

1. **图约束声明化**：用机器可读规则表达节点类型、边端点类型、置信度和证据要求，类似 Semantica 的
   SHACL 作为 CI gate，但不需要 RDF/OWL。
2. **可追溯记录**：每次摄入/更新都知道“何时、由哪条证据支撑、是否被后续记录取代”，而不是只保存当前状态。
3. **时间有效性**：政策、决策、课程关系等事实可能过期或被取代，需要 `valid_from/valid_until/superseded_by`
   的轻量时态语义。

## 决策

把前面筛出的借用点合成一个“工程治理小集群”，按三个依赖阶段落地；每阶段只做当前边界内的事，避免一次性改
写摄入主链路。

### D1:新增只读图约束配置

- 新增 `.scripts/graph_validate.py`（纯只读，不写 `graph.db`），读取
  `operations/config/graph-schema.yaml`。
- `graph-schema.yaml` 只表达 `graph.db` 当前可机械验证的约束：节点类型、`entity_subtype`、
  `confidence` 枚举、谓词别名、允许的边端点类型、raw 证据要求。
- 它是 `ingest_check.py` 中散落的页面枚举和 `predicate_governance` 的**图侧**补充，不是替代；页面
  结构仍由 `ingest_check.py` 校验。
- 不引入 RDF、OWL、SHACL 工具链，不新增第二事实源；`graph.db` 仍是唯一边主数据。

### D2:轻量 provenance 语义

- 继续遵守 raw 红线：provenance 是派生物，不是事实源。
- 为 `edge_evidence` 增加加性字段：`recorded_at`（默认 `CURRENT_TIMESTAMP`）、可选
  `superseded_by`（指向取代本证据的新证据源）。
- 不引入 SHA-256、W3C PROV-O 序列化和独立 provenance 数据库；当前仓库的
  `source`/`evidence_quote`/`is_sr` 已足够定位，缺的只是时间和取代链。
- `graph_ingest.py` 写边时仍只追加 evidence，不覆盖旧记录。
- 新增 `edge_origins(edge_id, origin_page, source, recorded_at)`，记录某页面对共享语义边或程序派生边的贡献。它用于 re-ingest 撤销 lineage，不是事实证据，不能替代 `edge_evidence`。
- `graph_ingest.py ingest --clean` 先撤销本页 origin/evidence；共享边仍有其他 origin 时保留，无剩余 origin/evidence 才删除。lineage 上线前的历史边按本页 raw source 保守清理，raw 始终不改。
- 事实 locator 由程序在不可变 Raw 中机械匹配为 `path#L<n>`，无法可靠匹配时显式退到 `path#全篇`；不要求弱 LLM 生成 locator 或复杂 evidence JSON。

### D3:轻量时态事实

- 引入独立 `temporal_facts` 表，而不是给核心 `edges` 表加诸多可空列，保持现有导航查询语义稳定。
- 表字段最小化：`subject`、`predicate`、`object`、`valid_from`、`valid_until`、`superseded_by`、
  `source`、`is_sr`。
- `query_graph.py` 增加 `temporal --at <date>` 子命令，只读该表；普通 `search`/`neighbors` 默认不
  混入时态事实，避免导航噪声。
- 先用于行政决策、政策、课程等明确会“过期/废止”的页面类型，不把论文关系全部时序化。
- 摄入侧只对 admin `policy`/`procedure`/`decision` 与 teaching `course` 页写入：frontmatter
  `effective_from`/`effective_to` 被 `graph_ingest.sync_page_temporal_fact` 落成 canonical
  `生效` 事实；重复摄入替换旧记录。

### 不采用

- 全套 RDF/OWL/三元组仓储：会引入 `cross-domain/graph.db` 之外的第二个事实源，违背单一图源红线。
- SHACL/RDF validator、Datalog/Rete/SPARQL 推理器：当前规模下维护成本高于收益。
- 向量优先的 GraphRAG 主路径：继续以图/页面/Navigation 定位为主，embedding 只做候选、聚类和消歧。
- Agent Memory/ContextGraph 等运行态持久 memory：会破坏 DSH 运行时状态不写 raw/wiki/graph.db 的边界。
- 完整 Knowledge Explorer、MCP server、polyglot 多图存储与导出：不在本阶段采纳，后续有对外审计/可视化
  需求时另行评估。

## 实施顺序

1. 新增 `graph-schema.yaml` + `graph_validate.py`，先跑存量图校验，修正声明直到没有新 ERROR。
2. 扩展 `edge_evidence` provenance 字段与加性迁移，补 `graph_validate`/`ingest_check` 回归。
3. 新增 `temporal_facts` 与 `query_graph.py temporal`，只接入明确需要时态语义的页面类型。
4. 每阶段完成后同步建立回归；如 `graph.yaml` 影响面扩大才补登记。

## 风险

- 图约束若写得太细，会提高入图摩擦；先从现有硬编码规则开始声明，禁止新增专家规则。
- 时态字段若不加过滤，可能被查询误当普通事实；因此采用独立表，默认不参与普通查询。
- 加性迁移必须兼容 private 域；任何阶段都不允许 DROP/重写真实 `graph.db`。

## 验证

- `python3 .scripts/engineering_graph.py validate`
- `python3 .scripts/test_ingest_pipeline.py`
- `python3 .scripts/test_ingest_check.py`
- 后续每阶段加入对应测试：`test_graph_validate.py`、`test_query_graph_temporal.py`
