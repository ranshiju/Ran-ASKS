---
title: "Eigenstate thermalization and its breakdown in quantum spin chains with inhomogeneous interactions"
type: paper-summary
sources:
  - "raw-not-distributed/D047-2024-wang-eth-inhomogeneous/paper.md"
source_type: official-doc
date: 2024
venue: "Phys. Rev. B 109, 045139 (2024)"
authors: ["Ding-Zu Wang", "Hao Zhu", "Jian Cui", "Javier Argüello-Luengo", "Maciej Lewenstein", "Guo-Feng Zhang", "Piotr Sierant", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Eigenstate thermalization and its breakdown in quantum spin chains with inhomogeneous interactions

> **作者**：Ding-Zu Wang、Hao Zhu、Jian Cui、Javier Argüello-Luengo、Maciej Lewenstein、Guo-Feng Zhang、Piotr Sierant、Shi-Ju Ran | **发表**：Phys. Rev. B 109, 045139 (2024)
> **核心贡献**：证明在自旋-1/2 XXZ 链中引入沿链线性变化的 zz 相互作用可以从可积点驱动系统进入量子混沌与本征态热化（ETH）成立区间，但当线性梯度足够大时又会重新抑制热化、恢复非遍历行为。

## Navigation

本文针对自旋-1/2 XXZ 链中沿空间位置线性变化的 zz 相互作用，研究 ETH 的成立与破缺。通过精确对角化（ED）分析能级统计、本征态中局域算符矩阵元的统计性质，并追踪纠缠熵与存活概率的演化，文章刻画了从可积、到量子混沌、到强梯度下非遍历三个区间的特征差异，并提出在超冷原子（特别是 Rydberg 原子阵列）中的实验实现路径。

## 研究方向定位

研究对象为 z-z 相互作用沿链线性变化的自旋-1/2 XXZ 链，核心问题是中等梯度诱导 ETH、强梯度是否再次破坏遍历性，采用精确对角化分析能级统计、ETH 矩阵元与动力学可观测量。[^r1]

## Content

### 模型设定

考虑的哈密顿量在均匀跃迁项之外引入位置依赖的 z-z 耦合 $\Delta_i$，其在链的两端分别取值 $\Delta - \theta$ 与 $\Delta + \theta$，即线性梯度由参数 $\theta$ 控制。[^r1]

### 从可积到量子混沌再到非遍历的转变

作者表明：随着线性非均匀性的引入，原本可积的 XXZ 链被驱动进入量子混沌区域；然而当 $\theta$ 足够大、热化反而被抑制。这一图像通过对能级统计的考察给出。[^r1]

### ETH 矩阵元的诊断

在量子混沌区，对角矩阵元的中心值及其本征态间涨落随系统尺寸呈指数衰减；非对角矩阵元呈高斯分布，其方差为频率 $\omega$ 的光滑函数 $|f_O(\bar{E}\simeq 0, \omega)|^2$，与 ETH 预言一致。而在强梯度非遍历区，非对角矩阵元方差不再随能量光滑变化，且行为与均匀可积 XXZ 链不同。[^r2]

### 动力学特征

动力学层面，混沌区出现纠缠熵的弹性格性化扩展与存活概率的快速衰减，指示遍历性；强梯度非遍历区中纠缠熵呈对数增长、存活概率在长时间仍保持显著数值，暗示系统存在记忆——与文献中强无序 MBL 相及“希尔伯特空间碎裂”（Hilbert space shattering）的特征相似。[^r2]

### 与无序情形的联系

作者指出，虽然结果针对干净系统，但类似现象也出现在无序相互作用体系中。[^r2]

### 超冷原子实验实现

文章给出在超冷原子平台实现该模型的方案：自旋自由度映射到原子的两个亚稳态，跃迁由偶极相互作用或波导介导的光子交换提供；针对 Rydberg 原子，因偶极相互作用随主量子数 $n$ 以 $n^4$ 增强，可在典型间距 $r\sim 1\,\mu\mathrm{m}$ 下提供足够强的自旋交换。天然出现的 XX 型相互作用可通过周期性旋转自旋轴的 Floquet 方式工程化为 XXZ 型，而单原子寻址可调制 $\sigma_i^z\sigma_{i+1}^z$ 项上的线性梯度 $\Delta_i$，工作区间为 $0 \leqslant \Delta \pm \theta \leqslant 2J$，覆盖混沌与非遍历两个区。作者亦提及偶极相互作用按 $\bar{r}^{-3}$ 衰减所引入的次近邻项（约为近邻项一个数量级），其影响留待后续工作。[^r3]

## Sources

[^r1]: raw-not-distributed/D047-2024-wang-eth-inhomogeneous/paper.md#L31
[^r2]: raw-not-distributed/D047-2024-wang-eth-inhomogeneous/paper.md#L263
[^r3]: raw-not-distributed/D047-2024-wang-eth-inhomogeneous/paper.md#L257
