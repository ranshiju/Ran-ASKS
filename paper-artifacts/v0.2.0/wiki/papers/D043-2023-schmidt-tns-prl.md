---
title: "Tensor Network Efficiently Representing Schmidt Decomposition of Quantum Many-Body States"
type: paper-summary
sources:
  - "raw-not-distributed/D043-2023-schmidt-tns-prl/paper.md"
source_type: official-doc
date: 2023
venue: "Phys. Rev. Lett. 131, 020403 (2023)"
authors: ["Peng-Fei Zhou", "Ying Lu", "Jia-Hao Wang", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Tensor Network Efficiently Representing Schmidt Decomposition of Quantum Many-Body States

> **作者**：Peng-Fei Zhou、Ying Lu、Jia-Hao Wang、Shi-Ju Ran | **发表**：Phys. Rev. Lett. 131, 020403 (2023)
> **核心贡献**：提出 Schmidt tensor network state (Schmidt TNS)，用线性随系统尺寸 N 增长的复杂度对有限乃至无穷大量子态的 Schmidt 分解进行张量网络表示。

## Navigation

Schmidt 分解是刻画量子多体态纠缠结构的基本工具，但直接处理 Schmidt 系数与变换矩阵的代价通常随系统尺寸指数增长。本工作提出的 Schmidt TNS 将变换部分用由局部幺正张量组成的张量网络表示，将 Schmidt 系数（即纠缠谱）编码到正定矩阵乘积态 (MPS) 中，并将平移不变性条件施加于网络与 MPS 以覆盖无穷系统情形。论文以具有几何阻挫的准一维自旋模型基态为基准演示了方法的可行性，并给出在全态采样任务上的指数加速前景。

## 研究方向定位

研究对象为具有非平凡二分边界的有限与无穷量子多体态，核心问题是 Schmidt 分解中 Schmidt 系数与变换的指数复杂度瓶颈，方法是用 Schmidt TNS（局部幺正张量网络 + 正定 MPS 编码系数）将 Schmidt 分解表示为线性复杂度的张量网络[^r1]。

## Content

### 复杂度瓶颈与设计思路

纠缠是量子多体态资源性表征的核心，而完整访问一个多体态的纠缠信息通常需要指数级的计算与存储开销[^r1]。Schmidt 分解把一个二分量子态写成 Schmidt 系数与两侧正交基变换的乘积；该分解天然带有一个二分边界，但直接表示 Schmidt 系数和变换矩阵在 N 增大时迅速变得不可行[^r1]。Schmidt TNS 的核心想法是把这两类对象各自用张量网络 (TN) 表示，使描述代价随 N 仅线性增长[^r1]。

### 变换与系数的张量网络表示

在 Schmidt TNS 中，二分两侧的变换各自被组织为由局部幺正张量构成的张量网络，而 Schmidt 系数（即纠缠谱）则被编码进一条正定矩阵乘积态 (MPS)[^r1]。当目标态具有平移不变性（如无穷系统极限）时，可在网络与 MPS 上施加平移不变约束，使表示天然兼容热力学极限[^r1]。这种“系数 MPS + 变换 TN”的拆分是该方案与直接对完整态做张量网络分解的关键区别——Schmidt 系数虽然刻画强纠缠熵，但其 MPS 表示被发现是弱纠缠的，从而保证整体表示的紧凑[^r1]。

### 基准演示：几何阻挫准一维自旋模型

作者以几何阻挫准一维自旋模型的基态作为基准，对 Schmidt TNS 的有效性进行了模拟[^r1]。该基准态本身具有非平凡的纠缠结构与二分边界，是检验 Schmidt 分解表示方法的典型场景[^r1]。数值结果显示，即便被分解态的纠缠熵较强，编码 Schmidt 系数的 MPS 仍呈现弱纠缠特征，从而支撑用 MPS 表示系数这一步骤的效率[^r1]。

### 全态采样的加速前景

由于 Schmidt TNS 把一个多体态压缩为线性尺度的网络 + 系数 MPS，对全态采样一类任务可在原理上获得相对于直接态向量方法的指数加速[^r1]。该加速成立的前提是 Schmidt 系数 MPS 保持弱纠缠——而这正是基准模拟观察到的现象[^r1]。

## Sources

[^r1]: raw-not-distributed/D043-2023-schmidt-tns-prl/paper.md#L9
