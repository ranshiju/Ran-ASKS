---
title: "Visualizing quantum phases and identifying quantum phase transitions by nonlinear dimensional reduction"
type: paper-summary
sources:
  - "raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md"
source_type: official-doc
date: 2021
venue: "Phys. Rev. B 103, 075106 (2021)"
authors: ["Yuan Yang", "Zheng-Zhi Sun", "Shi-Ju Ran", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Visualizing quantum phases and identifying quantum phase transitions by nonlinear dimensional reduction

> **作者**：Yuan Yang、Zheng-Zhi Sun、Shi-Ju Ran、Gang Su | **发表**：Phys. Rev. B 103, 075106 (2021)
> **核心贡献**：提出一种基于非线性降维 t-SNE 与负对数保真度距离的方案，将基态从希尔伯特空间映射到二维特征空间，以可视化方式识别量子相与相变点。

## Navigation

本文针对多体系统量子相与相变点的识别难题，提出用无监督非线性降维方法 t-SNE 把高维希尔伯特空间中的基态分布映射到二维特征空间，并以负对数保真度（NLF）作为量子态之间的距离度量[^r1]。在多个一维强关联自旋模型上的测试表明，gapped、critical、topological 等不同相在二维空间中呈现出可区分的聚类或流形结构，从而以"视觉感知"方式定位相变点[^r2]。该方法不需要先验的序参量知识，属于"通过学习来感知相与相变"的非传统路线[^r3]。

## 研究方向定位

研究对象为多体量子系统中的基态相与相变，核心问题是如何在不依赖序参量与先验标签的情况下识别包括 gapped、critical 与 topological 相在内的量子相，方法是无监督非线性降维（t-SNE）结合负对数保真度距离对基态分布做可视化[^r2]。

## Content

### 方法概览：从希尔伯特空间到二维特征空间

作者提出把基态分布 $\mathcal{H}$ 通过 t-SNE（一种非线性降维算法）映射到二维特征空间 $\mathcal{R}^2$，映射原则是随机地最大化 $\mathcal{H}$ 与 $\mathcal{R}^2$ 中基态分布的相似度[^r4]。量子态之间的距离采用 NLF（负对数保真度）来度量；通过肉眼观察或经典聚类（如 k-means）即可在 $\mathcal{R}^2$ 中直接读出相的分类与相变点的位置[^r4]。该方法不要求事先知道相的个数与训练标签，因此相对于有监督分类器与依赖蒙特卡罗采样的无监督方案，被定位为"novel, efficient, and simple"[^r5]。

### 与既有方法的关系

在传统朗道范式下，量子相的刻画往往依赖事先给定的序参量；对超出朗道范式的相（如拓扑相），寻找合适的"序参量"本身即为难题，且序参量可能是非局域的或无法用可观测量表示[^r6]。有监督机器学习方案（神经网络、Boltzmann 机等）虽可用作分类器，但需要带标签的训练数据[^r6]。在无监督方向上，PCA 等线性降维已被用于经典 Ising 模型的相变识别，但对量子相而言，希尔伯特空间的指数增长使无监督学习尤其具有挑战[^r5]。本文方案属于这一脉络的非传统延伸[^r4]。

### 在一维强关联自旋模型上的基准测试

方案在一维量子格点模型上做了基准检验[^r4]：
- **横场伊辛模型（TFIM）**：极化相与顺磁相分别呈现为不同的二维簇，临界点处结构发生显著变化[^r7]。
- **XXZ 模型**：含能隙的铁磁/反铁磁相与非临界相呈椭圆簇，gapless XY 相呈现为一维流形[^r7]。
- **自旋-1 海森堡链**：在 Haldane 相（gapped 拓扑相）与 Luttinger liquid（LL）临界相之间，相边界可被识别[^r7]。

论文因此宣称方案能同时处理朗道范式下的常规相、非局域拓扑相以及由 CFT 描述的临界相[^r8]。

### 分布形态与物理的直观对应

作者观察到一条经验规律：在 $\mathcal{R}^2$ 中，**非临界相**（FM/AFM 与 gapped 拓扑相）呈椭圆状聚簇，**临界相**（XX 与 LL）则呈现一维流形结构[^r9]。其直观解释为：同一相内的态共享相近的物理量，因而彼此距离很小，自然聚成一小片区域；在临界点附近能隙关闭，物理量随参数剧烈变化，不同参数下态之间的距离显著增大[^r10]。当两个非临界相之间夹着一段临界相时，临界相内部的态间距离大于非临界相内部，从而形成被拉伸的一维流形[^r11]。

### 对经典数据的延伸验证

除量子态外，作者也将同一可视化流程（t-SNE + NLF 或欧氏距离）应用到 MNIST 与 fashion-MNIST 图像数据集，每类取 100 张、共 1000 张图像，参数取 $\mathcal{P}=16$，迭代次数 $n_{\mathrm{it}}=5000$，以说明该"通过学习来感知相"的可视化策略对经典数据同样适用[^r12]。

### 局限与未来工作

作者明确指出，t-SNE 作为一种非线性流形学习方法，其在 $\mathcal{R}^2$ 中生成的两个特征 $y_1, y_2$ 各自的物理含义无法被解读，方法在变分意义上保证 KL 散度的最小化，但具体几何形状与临界性、拓扑等物理属性之间的严格对应关系目前仍只是"speculation"，尚有待建立[^r13]。作者将更严格、稳健的关系以及更高可解释性的降维方法留作未来工作[^r13]。

## Sources

[^r1]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L15
[^r2]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L15
[^r3]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L15
[^r4]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L25
[^r5]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L23
[^r6]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L21
[^r7]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L25
[^r8]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L303
[^r9]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L290
[^r10]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L295
[^r11]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L297
[^r12]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L293
[^r13]: raw-not-distributed/D033-2021-quantum-phase-visualization/paper.md#L305
