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

1. 用 `rg --files */wiki/ */raw/` 获取文件路径，不读取正文。
2. 与 graph nodes/aliases 对账：每个 Wiki page 有独立节点；同目录同 stem 的 Raw 原件与 locator companion 合并为一个 Raw 文档包节点，各文件路径作为 alias。
3. Wiki 缺节点时交给标准 ingest；Raw 缺文档包、alias 或来源边时交给标准 graph repair。
4. Raw 文档包可以暂时没有 Wiki 消费者，报告为信息项，不按事实缺失处理。

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
