# RECOVERY — 候补检索策略

> **触发**:常规检索(1-3 层:keyword-index / triples / section retrieval)走完,**槽位缺口仍无候选**时,读本文件找替代路径。
> **定位**:经验化的定向检索提示(比 LLM 临时联想省 token、比全库向量检索聚焦)。插在槽位缺口提示之后、联想层(第四层)之前。
> **顺序**:常规检索 → 槽位缺口提示(程序回检) → **读 RECOVERY**(本文档) → 联想层(第四层) → 向量检索(第五层,远期)
> **维护**:静态人工总结,硬上限 50 条 / 单条 ≤200 字。到上限须合并同类或淘汰低频。后续更新由 LLM 把控(详见文末维护规约)。
> **诚实边界**:策略只定位不回答。命中后事实仍须 raw/wiki sources 回溯,不得凭策略直接作答。

---

## 策略清单

### R1 多版本对比遗漏
- **失败模式**:题目含"演变/对比/vs/新旧/版本/替代"但只找到一个版本就停(M1 型)
- **策略**:用 triples 查同实体的 superseded_by 版本链;或 grep page-catalog 找同标题不同日期页。各版本逐个声明为槽位
- **示例**:"培养方案演变" → 查到 0606(已 deprecated)+ 0623(草稿)两个版本,均须读

### R2 多实体比较遗漏
- **失败模式**:题目含"N者/分别/比较"但只定位到部分实体就停(M3 型)
- **策略**:逐个实体查图 search;未命中的可能用缩写(图 search 已覆盖 abbreviations)。图 relations --predicate 方法关系常含同类方法清单
- **示例**:"比较 RAPTOR/GraphRAG/GraphReader" → GraphReader 未命中 → 查图 relations --predicate 对比方法 定位

### R3 缩写/别名未命中
- **失败模式**:术语无命中(TNR/HOTRG 等学术缩写;E2/E5 型)
- **策略**(按顺序):
  1. 查图 `search <term>`——SQL LIKE 三路匹配 nodes 的 title + aliases 表(含缩写/全称/中文别名),缩写已主数据化(v4,2026-07-25),无独立索引
  2. grep raw 全称(如 "Higher-Order Tensor Renormalization")
  3. paper-summary 页 title 字段含全称
- **适用条件**:图 search 对术语无命中,且术语像缩写(全大写或含连字符)
- **中文文章补充**:中文查询词(如"矩阵乘积态")无法直接匹配英文缩写时,走图 `search` 的 aliases 表全称匹配(aliases 含中文则直接命中;为英文则用中文→英文映射:先查概念页中文全称,再查图 search 英文全称)。中文-英文-缩写三层跳转:中文全称→(概念页/摘要页)→英文全称→(graph search aliases)→缩写→论文页

### R4 时间线/沿革截断
- **失败模式**:题目含"沿革/历史/演变/时间线"但只读到部分事件就停(M4 型,910 tok)
- **策略**:该 section 可能按时间分段。①读全该页 Content(非仅 Navigation 摘要) ②沿 sources 下钻 raw ③triples 查时间谓词(发生于/导致/修订为)
- **适用条件**:题目问完整时间线,但检索只返回该页一句

### R5 跨域关联断裂
- **失败模式**:实体跨域(论文→项目→人员)但单域索引无连接
- **策略**:查图 neighbors 人物节点(人物↔论文↔项目);图 search 跨域并行查
- **适用条件**:题目涉及多域实体关联(如"课程负责人科研方向")

### R6 deprecated 页面误选
- **失败模式**:命中 deprecated 页但未跟 superseded_by,答了旧版
- **策略**:读 frontmatter status;deprecated 且有 superseded_by → 跟到 current 版重读。无 superseded_by → 标版本链未闭合(required_disclosure)
- **适用条件**:Evidence Profile version_status 显示 deprecated

### R7 槽位缺口常规检索穷尽
- **失败模式**:R1-R6 均未命中,缺口确无可行候选
- **策略**:触发联想层(第四层,QUERY.md 第四层)——LLM 联想 3-5 词 + grep raw。仍无 → 标 no_actionable_candidate 限定性回答
- **适用条件**:所有定向策略穷尽;这是停止前最后兜底,非首选

---

## 维护规约

- **当前阶段(静态)**:上述 7 条从 pilot 50 题失败案例提炼,人工维护,不自动更新
- **后续更新(验证有价值后)**:LLM 回答后自报告"用了哪条策略/需新策略"→ 记入 query-log 的 recovery_used 字段 → 定期审核高频新策略入库
- **不自动入库**:完全自动 LLM 入库风险引入无效/冲突策略,审核成本反而更高。罕见失败模式让 LLM 临时推理即可,不值得建条目
- **淘汰规则**:到 50 条上限时,合并同类或淘汰低频(按 query-log 命中次数)
