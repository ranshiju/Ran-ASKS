---
title: "Deep Learning Quantum States for Hamiltonian Estimation"
type: paper-summary
sources:
  - "raw-not-distributed/D030-2021-qubismnet/paper.md"
source_type: official-doc
date: 2021
venue: ""
authors: ["Xinran Ma", "Z. C. Tu", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Deep Learning Quantum States for Hamiltonian Estimation

> **作者**：Xinran Ma、Z. C. Tu、Shi-Ju Ran | **发表**：2021
> **核心贡献**：提出 QubismNet，将 Qubism 映射把量子多体态或约化密度矩阵可视化为图像，再用卷积神经网络从图像中估计相互作用哈密顿量的物理参数（如耦合强度、磁场）。

## Navigation

QubismNet 把"Qubism 图像化"与 CNN 回归结合，从基态系数或约化密度矩阵反推哈密顿参数[^r1]。论文在一维与二维自旋模型上展示了"训练范围之外仍可推广"的特性——例如从远离临界点的样本学习后，可较准确地估计临界点附近的磁场[^r1]。文中以二维 XY 与 XXZ 模型为例，给出基于 RDM（reduced density matrix）的图像构建流程[^r3]。

## 研究方向定位

研究对象为量子多体态的基态或约化密度矩阵，核心问题是如何由这些态反推其所属哈密顿量的物理参数，方法与场景为使用 Qubism 映射将其表示为图像后以卷积神经网络做参数回归[^r1]。

## Content

### 从量子态到哈密顿参数的反演

作者指出，人类专家难以直接"读取"多体态的系数来获得物理信息，而通常依赖序参量或量子测量等先验知识；QubismNet 提供一种数据驱动路径——CNN 可从多体态系数或约化密度矩阵中学习耦合强度、磁场等物理参数，前提是输入为对应哈密顿量的基态[^r1]。

### Qubism 图像化 + CNN 的两阶段结构

QubismNet 由两部分构成[^r1]：

- **Qubism 映射**：将基态（净化后的约化密度矩阵）映射为 2D 图像；
- **CNN**：把图像映射到目标物理参数。

### 训练样本受限下的推广能力

通过在训练集上施加平衡性约束，QubismNet 在若干量子自旋模型上表现出学习与推广能力：尽管训练样本只覆盖参数的有限区间，模型仍可对训练区域之外的基态给出准确的参数估计[^r1]。作者特别展示了 CNN 可"从远离临界邻域的态出发，估计临界点附近的磁场"——即从非临界样本学习，泛化到临界点的磁场估计[^r1]。

### 二维格点上的 RDM 图像化流程

二维模型采用 DMRG（density matrix renormalization group）的标准做法[^r3]：

- 4×16 格点、二维 XY 与 XXZ 模型；
- 二维最近邻相互作用格点被拉伸为带长程相互作用的链；
- 取格点中央 4×2 子系统计算 RDM；
- 对 $\rho^2$ 的净化态，bra 空间自由度作为图像的一个维度，ket 空间自由度作为另一维度[^r3]。

论文以该流程得到不同 $h$ 或 $J_z$ 下二维 XY 与 XXZ 基态的若干 RDM 图像作为可视化示例[^r3]。

### 应用前景

作者指出，该工作为按设计基态反推哈密顿量提供数据驱动方法，可服务于基于哈密顿量的量子模拟与态层析等量子技术[^r1]。

## Sources

[^r1]: raw-not-distributed/D030-2021-qubismnet/paper.md#L9
[^r3]: raw-not-distributed/D030-2021-qubismnet/paper.md#L263
