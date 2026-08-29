> **使用约束**:先读规则再操作;不修改 raw;同步失败显式报告不静默降级。

# Sync（跨域索引同步）操作规范


## 定位

cross-domain 的语义价值是**跨域聚合（全面性）+ 关系结构化（准确性）**，而非搜索加速——当前规模下 `rg` 全文搜索已足够快。Sync 的职责是让 graph.db 与 wiki 内容保持一致(v4,2026-07-25 主数据化):graph.db 是边唯一源(不再从 md Core Triples 段派生),ingest 时 graph_ingest 增量加边。原 keyword-index.md/triples*.md 已删,page-catalog.md 仍派生(节点列表视图)。

> **v4**:graph.db 是边唯一源(不再从 md 派生重建)。ingest 时 `graph_ingest.py` 增量加边(resolve+去重+INSERT),不再有全量重建。三层对账调整:
> - 第一层(log 驱动):页面有变更 → ingest 时已增量加边(graph_ingest);无变更不操作
> - 第二层(漏检补全):图 nodes vs Wiki/Raw 文件清单差异(图缺节点=文件未入图；Raw 原件与同 stem companion 按文档包归一)
> - 第三层(一致性校验):图连通性 + `Wiki → 来源 → Raw` 直连 + 悬空边 + 双向冗余校验(`graph_metrics.py`),不再有"页面 Core Triples 漂移"(边在 graph.db 不在 md)
> keyword-index/triples 已删(融进 graph.db);page-catalog 仍派生(节点列表)。`_sync-state.md` 仍记 log 游标。

## 触发方式

- 用户说"同步跨域索引""补全 cross-domain""刷新知识图谱"等
- 建议在批量 Scan 后手动触发一次

## 核心思路：增量对账，三层递进

| 层 | 作用 | 工作量 |
|----|------|--------|
| 第一层 log 驱动增量同步 | 处理上次同步后的新增/更新页面 | 极小 |
| 第二层 漏检补全 | 补齐历史遗漏（路径差异比对） | 小 |
| 第三层 一致性校验 | 检查索引与页面一致性，顺带 keyword-index 超预算评估 | 小 |

三层每次运行都执行，但各自只处理自己负责的子集，互不重复。

## 执行步骤

### 第一层 — log 驱动增量同步

1. 读取 `cross-domain/_sync-state.md`，获取每个子项目上次同步到的 log 条目（`日期 | base_name`）
   > **private 不在同步范围**：private 物理隔离，不在 `graph_lib.SUBPROJECTS`，SYNC 不扫描 `private/`，不记入 `_sync-state.md`（见 INGEST.md「private 领域」）。private 的图一致性由其独立 ingest 流程维护。
2. 读各子项目 `wiki/log.md`，定位 `last_synced` 之后的新条目
3. 对这些条目涉及的页面（log 中列出的创建/更新页面）：
   - 提取 2-5 个关键词,写入页面 frontmatter `keywords`(主数据化,先查重再追加);图重建时自动派生
   - 提取 3-8 条关系,graph_ingest 增量加边到 graph.db（不再写 md Core Triples 段,不再有全局 triples）
4. 推进 `_sync-state.md` 指针到本次处理到的最新条目

> 注：admin 的 update 类条目、business 的 archive 类条目同样要同步——archive 意味着页面位置变更，keyword-index 中的路径需相应更新。

### 第二层 — 漏检补全（文件节点差异）

1. 用 `rg --files */wiki/ */raw/` 列出知识库文件路径（秒级，不读正文）。
2. 解析图 nodes 与 aliases：每个 Wiki page 对应独立节点；Raw 原件与同目录同 stem locator companion 共同对应一个 Raw 文档包节点，各文件路径作为 alias。
3. 差集 = 没有图中代表的文件。所有 Wiki 页面都应入图，不因“纯单子项目内部页面”而跳过；Raw 文档包允许暂时没有 Wiki 消费者。
4. 对缺失 Wiki 节点提示 ingest；对其 frontmatter `sources` 进一步检查对应 Raw 文档包节点与 `Wiki → 来源 → Raw` 直连边。缺失 Raw alias 或来源边时用标准 graph ingest/repair 补齐，不手工编辑数据库。
5. 检查未索引页面是否缺导航边（提出方法/作者/任职于等关系未进 graph.db）→ 提示 ingest 补边（不再有页面 Core Triples 段）。知识边 locator 可选，不做完整率审计。

### 第三层 — 一致性校验（图连通性 + 悬空边 + 冗余）

0. **相似边（历史遗留，只读）**：`sync_embedding_sweep` / `prune_similar_edges` 已删除（2026-08-26，无活跃调用点，dead code）。现存 `相似` 边为历史手动运行产物，保留在 graph.db 不动，导航层由 `--similar-topk` 控制扩散（见 QUERY.md/code-guidance.md）。不再有增量补建。
1. 检查图 edges 的 subject/object 节点是否存在，并核对 Wiki `sources` 对应的 Raw 文档包和 `来源` 边；只按标准 repair 计划处理悬空链接。
2. 跑 `graph_metrics.py all` 校验图连通性 + Wiki–Raw 来源直连 + 悬空边 + 双向冗余（边在 graph.db,无"派生漂移"概念,直接查图结构）
3. 去重合并重复条目（如同一关键词出现在多个分段、同一三元组重复）
4. 评估各子索引大小（**按消费方分两类阈值,2026-07-23 规模不变性三分法落地**）：
   - **keyword-index\*（LLM 整读型）**：`keyword-index-{research,ai,admin,people}.md` 各目标 < 4KB（LLM 读取 ~1k token）；若 > 4KB 输出**子域内拆分建议**
   - **图(v3)**:SQLite 图查询按节点/谓词索引,无 markdown 文件体量问题;全量重建是 O(全库)但 342 页几秒可接受,且不每次做(有反哺才做)
   - (v3 已融图,原 markdown 体量约束作废)
   - 注：已于 2026-07-18 完成首次按域拆分（原单文件 11KB → 4 子文件均 <4.5KB）
   - **与局部读阈值的关系**:4KB 是"拆文件"阈值(LLM 整读型);12KB 是"文件内分段"阈值(LLM 局部读,见 `cross-domain/SCHEMA.md`「索引文件局部读」)。triples 系列已按谓词分段,无需此评估
   - **遍历型 O(N) 不靠拆分**:`rg --files` 全库遍历/扫 N 个 frontmatter 等遍历型 O(N) 拆成子遍历仍 O(N),须用派生索引替代;当前文件名探测已并入 page-catalog,缩写派生索引待触发(abbreviations 页面 >200)
   - `keyword-index-{research,ai,admin,people}.md` 各目标 < 4KB
   - 若某子索引 > 4KB，输出**子域内拆分建议**（如 admin 再拆为 admin-policy / admin-activity）
   - (v3 已融图,原 markdown 体量约束作废)
   - 注：已于 2026-07-18 完成首次按域拆分（原单文件 11KB → 4 子文件均 <4.5KB）
   - **与局部读阈值的关系**:4KB 是"拆文件"阈值(拆成多个子索引文件);12KB 是"文件内分段"阈值(单文件内按 `##` 主题分段以支持 `read_section` 局部读,见 `cross-domain/SCHEMA.md`「索引文件局部读」)。二者层面不同,先拆文件,拆后单文件仍 >12KB 再文件内分段。triples 系列已按谓词分段,无需此评估
5. **index.md vs page-catalog 一致性校验**:运行 `.scripts/ingest_build.py --index-drift`,比对各 `*/wiki/index.md` 的 `[[wikilink]]` 与 page-catalog(派生 manifest),报告:
   - **悬空**:index 引用但页面不存在(已删/未建/路径错)→ 修正 index 引用
   - **漏页**:catalog 有但 index 未收录的内容页(papers/concepts/reviews 等)→ 补入 index 对应分组
   - people 页单列标注(常以作者索引聚合,非逐个列入 index,非真漏)
   - **只校验页面存在性**(纯结构,壳);index 的分组与语义描述仍是手写策展(语义,LLM 维护),不查——与单一事实源原则一致:index 的"页面存在性"可从 catalog 校验,"策展描述"是独立语义层

## 状态文件

`cross-domain/_sync-state.md`：

```markdown
# cross-domain 同步状态

> Sync 操作的增量指针。每次运行后推进。
> 首次初始化时设为各子项目 log 最新条目，避免首次运行变成全量重建。

last_synced:
  academic: 2026-07-16 | 0716-知识图谱构建
  admin: 2026-07-17 | 物理系十五五发展规划
  business: 2026-07-09 | 路演 PPT 归档
  teaching: 2026-06-21 | 知识库初始化
last_run: (未运行)
```

## 输出

报告保存到 `cross-domain/outputs/sync-YYYY-MM-DD.md`：

```markdown
## 同步报告 (YYYY-MM-DD)

### 第一层 增量同步
- 处理 log 条目: N
- 新增关键词: X 条
- 新增三元组: Y 条
- 指针推进: academic ... → ..., admin ... → ...

### 第二层 漏检补全
| 页面 | 跨域? | 操作 |
|------|------|------|
| ... | 是 | 补全关键词+三元组 |
| ... | 否 | 跳过（纯单子项目） |

### 第三层 一致性校验
- 相似边: 存量 N 条（历史遗留，无增量）
- 悬空链接删除: X
- 重复条目合并: Y
- keyword-index 大小: 9.3KB（超 4KB 目标，建议分层：[建议方案]）
```

## 重要约束

- **幂等**：可重复运行不产生重复条目；每条追加前先查重
- **增量优先**：第一层处理过的内容，第二/三层不重做
- **文件节点全覆盖**：所有 Wiki page 都有节点；所有 Raw 文件都有图中代表，同 stem 原件与 locator companion 合并为一个 Raw 文档包节点。跨域语义边仍保持稀疏，不因节点全覆盖而强制补大量关系。
- **不修改 raw/**
- **Topic Hub 不在 Sync 重建范围**：Hub 创建走 `HUB.md`，维护走 Ingest 步骤 9；Sync 仅校验 Hub 的 `page_count` 与实际关联页面数是否一致，不一致时告警。更外层导航视图(L3)同理不自动重建(是派生视图,见 HUB.md)，但 Sync 校验其 sources 指向的下层节点是否有效(悬空则告警)
- **keyword-index 拆分不自动执行**：只输出建议，避免破坏 QUERY.md 检索路径
