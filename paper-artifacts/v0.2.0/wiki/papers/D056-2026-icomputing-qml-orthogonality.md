---
title: "Orthogonality in Quantum-Probabilistic Machine Learning: An Investigation on Multiqubit Encoding"
type: paper-summary
sources:
  - "raw-not-distributed/D056-2026-icomputing-qml-orthogonality/paper.md"
source_type: official-doc
date: 2026
venue: "Intell. Comput."
authors: ["Sheng-Chen Bai", "Kun Zhang", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Orthogonality in Quantum-Probabilistic Machine Learning: An Investigation on Multiqubit Encoding

> **作者**：Sheng-Chen Bai、Kun Zhang、Shi-Ju Ran | **发表**：Intell. Comput.
> **核心贡献**：在张量网络 (TN) 与量子概率解释框架下，提出多比特编码方案 (msQFM-copy 与 msQFM-binary)，系统揭示"正交性灾变"(COO) 与表征/泛化能力之间的耦合关系，并发现"过度正交化"现象。

## Navigation

本文是会议论文 "Quantum-Probabilistic Machine Learning: From Catastrophe of Orthogonality to Multi-qubit Encoding" 的扩展版，重点从量子概率与张量网络视角研究 ML 的可解释性问题。作者通过 Fashion-MNIST 数据集上对生成与分类任务的实验，量化了编码比特数、特征维度 M、样本量与映射角度等超参数对正交性—性能耦合关系的影响，并在多数据集与多种 TN 结构 (MPS、tree TN) 下验证结论的稳健性。[^r1][^r2]

## 研究方向定位

研究对象为基于张量网络的量子概率机器学习中的表征与泛化能力，核心问题是如何通过多比特编码方案控制"正交性灾变"(COO) 进而调控模型性能，方法场景为 Fashion-MNIST 等数据集上的生成与分类任务。[^r1]

## Content

### 背景与动机：可解释性困境与张量网络的优势

深度 ML 模型的"不可解释性"问题构成理解其内部机理的主要障碍，表征与泛化能力的系统化理解仍是关键开放问题，与模型输出的稳定性与可靠性密切相关。[^r1]

张量网络 (TN) 作为量子概率 ML 的基础工具，可将量子多体系统的模拟复杂度从指数级降至多项式级；其基于 Born 统计解释的量子概率视角，使量子理论被用于理解 TN 方法的底层数学，并已被成功应用于生成、特征选择、异常检测与统计推断等需要可解释性的场景。[^r1]

### 核心概念：正交性灾变 (COO) 与多比特编码

本文在量子概率解释与"正交性灾变"(COO) 原则下，结合损失函数与准确率指标，分析 TN 的表征与泛化能力。作者在 Fashion-MNIST 数据集上提出多比特编码方案，将数据映射为用于 TN ML 的量子态，并指出编码比特数、特征数量 M、样本量与特征映射角度共同决定 COO 的成立程度，进而调控表征与泛化能力。[^r1]

### 统一框架与"过度正交化"现象

相比会议版本，本文将多自旋量子特征映射 (msQFM) 形式化为统一理论框架，并新增 msQFM-binary 方案；通过对比分析揭示出"过度正交化"现象——binary 方案因过度正交而损害表征能力，而 copy 方案则提供更细致的控制。[^r1]

### 复杂度—表征能力的权衡与最优超参数 H

msQFM 方法的复杂度随拷贝数 H 线性增长；可视化与聚类指标验证 H 越大聚类可分性越好，并与分类准确率提升高度相关。[^r1]

跨 3 个数据集的实验表明，最优超参数 H 随特征维度 M 增大而减小，这一一致模式为模型设计提供了实用指导。[^r1]

### 跨数据集与跨 TN 结构的稳健性

作者通过两方面互补验证 msQFM 的泛化性：(1) 在多个数据集上使用矩阵乘积态 (MPS) 结构进行测试 (Fig. S5)；(2) 在树形 TN 态 (tree TN states) 上测试 (Fig. S4)。结果显示结论对数据与 TN 结构的变化均具有稳健性。[^r1]

### 结论与展望

本文通过量子概率解释与正交性视角系统阐明 TN 在 ML 任务中的表征与泛化机制。两种编码方案 (msQFM-copy 与 msQFM-binary) 揭示了经典与量子计算机中数字编码方式的本质差异；超参数 H 以线性计算代价提升正交性与表征能力，并关联到泛化性能改善。[^r2]

## Sources

[^r1]: raw-not-distributed/D056-2026-icomputing-qml-orthogonality/paper.md#L19
[^r2]: raw-not-distributed/D056-2026-icomputing-qml-orthogonality/paper.md#L248
