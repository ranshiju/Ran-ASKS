# Sync（跨域一致性对账）操作规范

> **使用约束**：先读规则再操作；同步失败显式报告；Raw 保持不可变。

## 定位

Sync 负责对账 Wiki、Raw 文档包、graph.db 和可重建索引。graph.db 是边唯一源，Wiki 保存可读节点属性，Raw 保存事实证据；Sync 发现缺口并通过既有受管工具修复派生状态，不建立另一套索引主数据。

private 使用独立 `private/graph.db`，不进入主库 Sync 范围。

## 触发方式

- 用户要求“同步跨域索引”“补全 cross-domain”“刷新知识图谱”或检查多层状态一致性。
- 批量 Scan 或摄入完成后需要确认增量状态。

## 核心思路：增量、补漏、一致性

| 层 | 范围 | 目标 |
|----|------|------|
| 增量层 | 上次同步后的 log 条目 | 核对本轮变更是否完成节点、来源边和摄入版本更新 |
| 补漏层 | Wiki/Raw 文件清单与图节点差集 | 找出未入图页面、Raw 文档包或缺失来源边 |
| 一致性层 | 当前全图与派生目录 | 检查悬空关系、来源可达性、重复结构和页面索引漂移 |

三层依次执行；前层已确认的对象在后层只做一致性验证。

## 执行步骤

### 第一层：log 驱动增量核对

1. 读取 `cross-domain/_sync-state.md` 和各子项目 `wiki/log.md`，确定上次同步后的变更页面。
2. 对每个变更页面核对 graph 节点、frontmatter `sources` 对应的 Raw 文档包，以及 `Wiki → 来源 → Raw` 直连边。
3. 缺口通过标准 ingest update 或 `graph_repair.py` 的受管计划修复；图写入继续由既有 writer 和校验事务完成。
4. 本轮对象全部通过后推进 `_sync-state.md`；失败对象保留原指针并进入报告。

### 第二层：文件节点补漏

1. 用 `rg --files academic/wiki academic/raw admin/wiki admin/raw teaching/wiki teaching/raw business/wiki business/raw` 获取文件路径，不读取正文。
2. 与 graph nodes/aliases 对账：每个 Wiki page 有独立节点；同目录同 stem 的 Raw 原件与 locator companion 合并为一个 Raw 文档包节点，各文件路径作为 alias。
3. Wiki 缺节点时交给标准 ingest；Raw 缺文档包、alias 或来源边时交给标准 graph repair。
4. Raw 文档包可以暂时没有 Wiki 消费者，报告为信息项，不按事实缺失处理。

`graph_ingest_status.py` 的清单和统计使用同一组去重后的现存 Wiki 路径：已进图数取与合法页面类型节点的交集，待补数取差集。图中已无对应文件的节点不抵扣待补数量；误记为普通 entity 的 Wiki 仍列为待补。

### 第三层：结构与派生物一致性

1. 运行 `graph_validate.py` 检查 schema、节点引用和 semantic address。
2. 运行 `graph_metrics.py all` 检查连通性、Wiki–Raw 来源直连、悬空边与双向冗余。
3. 运行 `ingest_build.py --index-drift`，比较各 `wiki/index.md` 与 page-catalog：悬空链接进入修复，漏页进入策展候选。
4. 汇总 graph、索引和 Hub 引用缺口；语义分组与描述仍由 Agent 审核，不从文件存在性机械推导。

## 状态文件

`cross-domain/_sync-state.md` 只保存每个子项目的增量 log 指针和最近成功时间。它是可重建运行状态，不是事实源；首次初始化从现有 log 最新条目开始，避免把初始化误作全量重摄入。

## 输出

报告保存到 `cross-domain/outputs/sync-YYYY-MM-DD.md`，至少包含：

- 各域检查到的增量条目与指针推进结果；
- 缺失 Wiki/Raw 文档包节点及来源边；
- graph validation/metrics 结果；
- index drift 与需要 Agent 审核的策展候选；
- 失败项、原因和下一步受管动作。

## 重要约束

- 同一输入重复执行得到相同对账结果，修复入口保持幂等。
- Raw 全程不可修改。
- graph.db 只通过标准 writer 或 repair 事务更新，不手工编辑数据库。
- page-catalog 等可重建索引由程序生成；`wiki/index.md` 的分组和描述属于人工策展语义。
- Hub 的创建、Scope、分裂和合并遵循 `HUB.md`；Sync 只检查引用、状态与 page/graph 一致性。
- 不一致必须进入报告；信息不足时保留缺口，不猜测关系或事实。

### 用户指定的失效 Raw 链接修复

普通 Sync 不修改 Raw。用户明确要求修复已定位的历史失效链接时，可单独调用 shared `relativize_internal_symlinks.py --materialize-link <链接> --source-file <同目录原件> --expected-sha256 <已核验哈希>` 预检，再加 `--apply` 执行受管单文件修复。宿主先核对原件与 companion 的文档身份；程序仅接受公共域 Raw 内同目录、同扩展名的真实原件，拒绝有效链接、越界和符号链接祖先，不读取旧链接目标。

修复将实体副本原子替换到原链接位置，不改原件或 companion；`temp/raw-link-repair/` 回执保留旧目标、原件哈希及 prepared/completed/rolled_back 状态。提交后验证失败恢复旧链接；中断留下 prepared 回执时先核对现场，不把它视为成功。重复执行只在已是相同哈希实体文件时返回 already_materialized。完成后检查受影响 Wiki 并追加日志；旧 source.yaml 是历史接入记录，不覆写其来源。
