# ADR-003：Embedding 驱动的概念涌现式 resolve

> 状态：已批准（2026-08-07）
> 影响：graph_lib.py / graph_ingest.py / graph_metrics.py / SYNC.md / INGEST.md / code-guidance.md

## 背景

当前 alias/resolve 机制是**事后字符串匹配**：LLM 在零上下文下为每篇论文独立命名概念，
`graph_ingest` 的事后 resolve 试图拼回碎片。实测最近摄入 resolve_miss 26/31（84% 未命中），
图内积累 ~610 个问题节点（bare_abbrev 227 / author_year 碎片 243 / citation_fragment 22 /
proposition 未拆分 56 / empty_title 16），占 keyword 节点 19%。

根因：**无概念回流的反馈环**——涌现需要「已有概念 → 生成时对齐」的闭环，当前系统没有。

## 决策

引入 embedding（GLM-Embedding-3，2048 维双语）驱动的 resolve，与字符串匹配**混合**：
string resolve → embedding resolve → 建节点。核心原则——**子涌现、零 LLM 干预**。

### 三档分流（替换 resolve_bare_name 第 5 步归一化匹配）

| 区间 | 动作 | 理由 |
|------|------|------|
| cosine > auto_threshold（如 0.90） | auto-alias 到唯一已有节点 | 仅当**单候选**；多候选落入重叠区 |
| 重叠区（如 0.72–0.90） | **两节点并存 + 建「相似」边 + score** | 满足契约「候选不唯一禁自动归并」；结构化知识，自携带证据 |
| < proposal_floor（如 0.72） | 新建节点 + 缓存 embedding | 无关概念 |

> 契约对齐：graph_ingest 契约禁止「中英文一侧相同但另一侧冲突或候选不唯一的 keyword
> 自动归并（含 embedding 回退）」。重叠区**不合并只建相似边**正是满足此禁令的方式——
> 相似边是关系边而非归并。auto-alias 严格限定单候选，绝不模糊归并。

### 「相似」边 schema

- `ALTER TABLE edges ADD COLUMN score REAL`（加性迁移，现有行自动 NULL，零破坏）
- `相似` 为对称关系，单向插入（A→B），查询双向 `subject=X OR object=X`
- `STRUCTURAL_PREDICATES` 扩展为 `{"包含", "相似"}`（结构性、非知识边，confidence=推断）

### 阈值标定（一次性纯程序，零 LLM）

1. 清洗 278 条 alias → 真同义对（排除 arXiv:ID / 引文残片 / 错误合并产物）
2. 采集硬负例（同子域语义相近但不同概念，如 量子纠缠↔纠缠熵）
3. embed_cached_batch → 算正负例 cosine 分布 → 找分离点
4. 80% 标定 / 20% 验证：auto_threshold precision≥0.98，floor recall≥0.95
5. 产出写入 `operations/config/embedding-resolve.yaml`

## Tier 2：proposition 程序化拆分

proposition 保留（不同颗粒度知识有价值），但拆分改程序化：
- 复用 `_build_subgraph` 的 concept_map（已命中的概念），对每个命中概念建 `proposition | 包含 | concept` 边
- **`拆分` 谓词统一为 `包含`**：语义一致（命题包含概念），减少谓词种类
- 现有 23 条 `拆分` 边迁移为 `包含`；LLM prompt 的 4 行拆分指令降级为「直接输出原子三元组」

### proposition → keyword resolve（纯名词短语）

| 文本 | 类型 | 处理 |
|------|------|------|
| `张量网络` | 纯名词（无谓词结构） | resolve 成 keyword（命中→alias，未命中→新建） |
| `两步法` | 纯名词 | 新建 keyword |
| `用MPS参数化监督学习模型权重` | 论断（动词「参数化」） | 保留 proposition + 包含 MPS |

判定：复用 `is_descriptive_phrase` 的动词触发词集；≤15 字天然归名词短语。
引文残片由 Tier 5 预过滤拦截。

## Tier 3：warning 自动修复

- `is_descriptive_phrase`：跳过 `entity_subtype='proposition'`；公式内逗号（括号内）不触发
- `is_bare_abbreviation`：能 embedding resolve 到 keyword → auto-alias 而非 WARN（复用 `_revisit_bare_abbreviations` 逻辑，扩展为 embedding 回退）

## Tier 4：SYNC 全图 embedding sweep

- SYNC 第三层触发全图 keyword pairwise cosine → 重叠区建相似边（与 Tier 1 共享 `cosine_sim`）
- 增量：只对无相似边且未直接关联的节点对计算

## Tier 5：预过滤垃圾

- 扩展 `is_citation_fragment` 抓 `^[A-Z]{1,3}-\d{4}$` author-year 模式
- 建节点前拦截（`_build_subgraph` 已调用，扩展判据即可）
- 一次性清理存量 243+22 个垃圾节点及关联边

## 实现顺序

1. score 列迁移 + 相似谓词注册（graph_lib.py）
2. 阈值标定脚本 + config
3. Tier 5 预过滤 + 清存量
4. Tier 2 proposition 拆分 + 名词 resolve + 拆分迁移
5. Tier 1 embedding resolve（graph_lib + graph_ingest 接入）
6. Tier 3 warning 修复
7. Tier 4 SYNC sweep
8. 回归：test_ingest_pipeline.py + engineering_graph impact --verify

## 风险

- 阈值标定依赖 alias 清洗质量（噪声 alias 会污染分布）
- GLM-Embedding-3 对人名跨语言对齐弱（标定分类型看，人名走 is_person_reference 独立路径）
- 拆分→包含迁移改 graph_metrics 的未拆分检查（改为查包含边）

## 2026-09-03 修订：来源局部说明与保留式 abstention

- 论文 semantic Worker 为 keyword 输出一句文档局部说明，但不生成 locator；程序从已校验 Wiki section 脚注机械选择 Raw locator。
- `node_glosses` 保存逐来源的 description + Raw locator；`nodes.description` 是可重建的主导航说明。首条合格 gloss 可初始化空描述，后续来源不得盲目覆盖非空主描述。
- GraphDelta 身份解析优先使用当前文档局部说明作为 context。精确名称多候选仍 soft abstain 并跳过边；没有精确名称碰撞、仅 semantic identity gate 未过时，保留本地 keyword、原始边与 gloss。
- 此修订不降低 label/semantic/combined threshold，也不授权 embedding 单独触发 merge。
