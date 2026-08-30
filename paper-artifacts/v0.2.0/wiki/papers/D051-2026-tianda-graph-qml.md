---
title: "Unveiling the nature of graphs through quantum graphon learning"
type: paper-summary
sources:
  - "raw-not-distributed/D051-2026-tianda-graph-qml/paper.md"
source_type: official-doc
date: 2026
venue: ""
authors: ["Wenbo Qiao", "Peng Zhang", "Jiaming Zhao", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Unveiling the nature of graphs through quantum graphon learning

> **作者**：Wenbo Qiao、Peng Zhang、Jiaming Zhao、Shi-Ju Ran | **发表**：2026
> **核心贡献**：提出 quantum graphon neural representation (QGNR)，借助数据重上传量子电路的傅里叶级数表达能力实现任意分辨率、含高频成分的 graphon 学习，并在 graphon 学习与图分类任务上以更少的变分参数超越经典最优方法。

## Navigation

本论文把 graphon（非参数图生成函数）学习引入量子机器学习（quantum machine learning, QML）框架。针对经典方法在分辨率受限或拟合高频成分时的不足，作者利用数据重上传（data re-uploading）量子电路的截断傅里叶级数表达力提出 QGNR，并以 Gromov-Wasserstein 距离（GWD）作为训练目标实现端到端优化。论文进一步将 QGNR 扩展为条件 graphon 学习，在图分类（如分子属性预测、社交网络分析）任务上展示了量子优势。实验显示 QGNR 在性能上超越当前最优经典方法的同时，变分参数最多减少 81.65%。[^r1]

## 研究方向定位

本论文面向 graphon（非参数化图生成函数）的连续函数建模问题，针对经典方法受分辨率限制或难以拟合高频成分的瓶颈，提出基于数据重上传量子电路的 QGNR 方法在 graphon 学习及图分类任务上的端到端量子机器学习方案。[^r1]

## Content

### Graphon：大规模图背后的连续函数

Graphon 是一类对称、二维 Lebesgue 可测函数，在生成机制下被视为图生成器，同时可作为图序列的极限对象提供超大规模图的统一刻画框架。[^r1] 相比参数化的随机块模型、混合成员模型与潜空间模型，graphon 作为非参数模型在跨任务泛化上更具优势。[^r1]

### 经典 graphon 学习方法的两类瓶颈

传统机器学习方法常以分片常数函数在预设分辨率下学习 graphon，其表达能力受到分辨率约束，限制了任意尺寸图的生成。[^r1] 神经网络方法虽可建模任意分辨率下的连续 graphon，但参数量大、训练与应用成本较高，并且在对含高频成分的复杂函数拟合上效率不足。[^r1]

### QGNR：用数据重上传量子电路拟合高频 graphon

QGNR 利用数据重上传量子电路在拟合截断傅里叶级数上的能力，使模型能高效刻画自然连续信号的高频成分。[^r1] 在此基础上，作者以 Gromov-Wasserstein 距离（GWD）作为训练目标实现端到端优化：GWD 在度量两个度量空间相似性的同时保持图节点的置换不变性，与 graphon 学习天然契合。[^r1]

### 性能：更少参数下的领先表现

数值结果显示 QGNR 不仅优于基于神经网络的最优方法，还将变分参数最多减少 81.65%。[^r1] 论文指出，graphon 中固有高频成分正是经典方法学习困难的原因；QGNR 对高频成分的表征能力是其取得优势的关键。[^r1]

### 频域视角：四个代表性 graphon 上的对比

为从频域解释 QGNR 与经典方法的差异，作者选取四个示例：光滑且频谱能量集中于零附近的 $\mathcal{W}_1 := \tfrac{1}{2}(x+y)$；沿对角线急剧变化的 $\mathcal{W}_2 := 1-|x-y|$；刻画两社区间突变的 $\mathcal{W}_3 := 0.8 \times I_2 \otimes \mathbb{1}_{[0,1/7]^2}$；以及含快速振荡与细尺度变化的 $\mathcal{W}_4 := \tfrac{1}{2}\bigl(1+e^{-(x^2+y^2)}\sin(50\pi|xy|)\bigr)$。[^r1]

依据衰减律 $|a_{m,n}| \sim (m^2+n^2)^{-k/2}$，$\mathcal{W}_1$ 具有较高的光滑度 $k$，(b–d) 的光滑度较低，其中低光滑度 graphon 会带来困难的节点对齐问题，在文献中被认为难以学习。[^r1] 根据频率原则，经典神经网络难以有效建模高频特征；而 QGNR 能高效表示截断傅里叶级数，从而学习这些低光滑、难以对齐的 graphon（命题 2）。[^r1]

频率域拟合的对比实验中，QGNR、IGNR 与 FNO 三种方法在不同频率上的幅度与误差比较显示，IGNR 优于 FNO，而 QGNR 进一步超越两者：QGNR 在低频与高频 graphon 上均能较准确地捕捉各频率成分，作者将其归因于 QGNR 中量子部分的指数傅里叶级数拟合能力（命题 2）。[^r1]

### 条件 graphon 学习与图分类应用

作者把 QGNR 扩展到条件 graphon 学习：通过与图神经网络（graph neural network, GNN）集成，QGNR 可从更具变化性的图数据中挖掘带隐含条件的 graphon，从而在分子属性预测、社交网络分析等图分类任务上取得优势。[^r1]

### 局限与未来工作

作者明确指出的局限主要在运行时间：在最大可容忍时间 $T$ 内，可处理的最大图规模约为 $n_{\max} \approx \lfloor (T/t_q)^{1/2} \rfloor$，其中每节点对的模拟时间 $t_q$ 直接决定整体运行时间。[^r1] 论文在补充材料 Note 5 给出在真实量子硬件上的初步实验，并将近期方向定位为发展量子启发技术以降低 $t_q$、远期方向定为利用更多量子比特学习更复杂 graphon，以及面向药物发现、蛋白质设计、生物分子工程等真实场景扩展。[^r1]

## Sources

[^r1]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L9
[^r2]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L13
[^r3]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L15
[^r4]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L17
[^r5]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L199
[^r6]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L201
[^r7]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L203
[^r8]: raw-not-distributed/D051-2026-tianda-graph-qml/paper.md#L205
