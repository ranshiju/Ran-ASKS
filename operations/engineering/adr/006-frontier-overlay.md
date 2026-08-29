# ADR-006：以独立稀疏图建设 Frontier 研究前沿层

> 状态：已批准（2026-08-24）

## 背景

Raw/Wiki/graph.db 已承担事实、编译知识与导航，但开放问题、部分答案、候选思路、失败尝试和历史解释具有不同认识论状态。把它们混入 Wiki 或事实图会让检索结果难以区分事实与推测；只存静态问题卡又无法表达问题被部分解决、重开及多路线分叉汇合。

## 决策

1. 在 `academic/frontier/` 建立独立 Frontier overlay；Markdown 是主数据，`frontier.db` 是可重建的 FTS/稀疏导航索引。
2. Frontier 的主对象为 Question Page（一个问题贯穿全部生命周期）与 Trajectory（历史演进），条目保存有证据回答、缺口、思路、测试和状态变化；旧 intake/thread 双页模型停止新建。
3. Frontier 通过 `fact_links` 单向引用 Raw locator、Wiki path 和 Graph node path；不修改或复制事实图。
4. 用户问题采用“先查库、写/复用 Question Page、尝试库内回答、再评估”的准入；模型不可用时保留 captured/pending，active 必须人审。
5. 论文 ingest 后只机械捕获作者明示的开放问题/future work，限量、幂等、非阻断；每条建立或精确复用 Question Page，并用紧凑 Graph→Wiki→Raw 证据包尝试回答，不自动发散问题。
6. Frontier Graph 保持稀疏：显式结构关系入图，embedding/AI 关系仅为候选；检索以 FTS、状态过滤和 fact link 为主。
7. `kb_state` 与 `scientific_state` 分离；支持结论必须引用证据包内 Raw locator，禁止从本库无命中推出全球科学开放性，也禁止库内回答自动修改 `scientific_state`。

## 不采用

- 在 `cross-domain/graph.db` 增加 question/idea 节点；这会污染事实导航。
- 以 LLM 生成的复杂知识图作为 Frontier 主数据；弱模型格式失败和伪关系风险过高。
- 对每次 query 或每篇论文无条件生成开放问题；这会迅速形成低价值候选堆积。
- 把演进轨迹简化为线性论文年表；研究路线需要分叉、汇合、挑战与重开。

## 验证

- `python3 .scripts/test_frontier.py`
- `python3 .scripts/test_wg.py`
- `python3 .scripts/engineering_graph.py validate`
