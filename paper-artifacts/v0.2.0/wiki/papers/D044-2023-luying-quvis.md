---
title: "Quantum compiling with a variational instruction set for accurate and fast quantum computing"
type: paper-summary
sources:
  - "raw-not-distributed/D044-2023-luying-quvis/paper.md"
source_type: official-doc
date: 2023
venue: ""
authors: ["Ying Lu", "Peng-Fei Zhou", "Shao-Ming Fei", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Quantum compiling with a variational instruction set for accurate and fast quantum computing

> **作者**：Ying Lu、Peng-Fei Zhou、Shao-Ming Fei、Shi-Ju Ran | **发表**：2023
> **核心贡献**：提出量子变分指令集（QuVIS），通过细粒度时间优化（FGTO）算法变分地实现多比特门，在与 QuMIS 相同的量子硬件条件下，将线路实现的时间开销降至不足一半，并随线路深度减少代数地抑制误差累积。

## Navigation

QuVIS 把"指令集"重新定义为可灵活设计的多比特门集合，并通过 FGTO 算法变分求解驱动该门的磁脉冲序列，从而直接实现目标酉变换，跳过 QuMIS 类标准指令集中一比特旋转与受控非（CNOT）的逐门分解。[^r1] 作者在 N 比特量子傅里叶变换（QFT）与多比特 SWAP 两种基准上演示：时间开销随比特数 N 近似线性增长，且在同等硬件要求下降低到 QuMIS 的一半以下，同时误差随编译深度的减少呈代数式抑制。[^r1][^r2] 该方案对量子硬件的相互作用形式（伊辛、海森堡等）、连通性与强度保持自适应能力。[^r1]

## 研究方向定位

研究对象为通过受控相互作用自旋系统执行的量子线路编译，核心问题是如何在给定量子硬件上同时降低时间成本与误差积累，方法为基于细粒度时间优化的多比特变分指令集（QuVIS）。[^r1]

## Content

### QuVIS 的定义与实现方式

QuVIS 由"可灵活设计的多比特量子门"组成，这些门由施加于相互作用自旋上的磁脉冲序列在硬件上直接实现；脉冲序列通过细粒度时间优化（FGTO）算法变分确定，FGTO 已证明能高效实现给定的多比特酉变换。[^r1] 与以一比特旋转和 CNOT 为基本单元的 QuMIS 不同，QuVIS 不再约束于固定的 1/2 比特门集合，而是允许针对目标线路整体地定义与优化基本门。[^r1]

### 量子硬件与控制设定

基准测试采用最近邻伊辛链作为时间演化的哈密顿量，耦合常数满足 $J_{nn'} = 2\pi$（$n' = n+1$），其它位置为 0。[^r5] 沿自旋 z 方向的磁场固定为零，x 与 y 方向磁场可独立调节，对应射频脉冲控制场景。[^r6]

### 三比特 QuVIS 用于 QFT 的结构

针对 $N \leq 9$ 的 QFT 编译，作者定义了九个基本门 $\{U_m\}$（$m = 0,\ldots,8$），包含 Hadamard（H）、aSWAP（两交叉加竖线连接的图形记号）以及相位为 $\theta = \pi/2^p$ 的受控相位门 $R_p^*$。[^r3] 这些基本门可通过图 1(a) 与图 2(a)(b) 的递归规则由奇偶 $m$ 区分地构造，从而支撑图 2(c)(d) 中给出的奇偶 N 比特 QFT 编译结构，并配合必要的 SWAP 门得到 $N = 3,\ldots,9$ 的完整编译线路（图 1(b)）。[^r3]

### 时间成本与误差的基准结果

表 I 列出三比特 QuVIS 中九个基本门 $\{U_m\}$ 在误差约 $O(10^{-2})$ 时的时间成本 $T$（单位与原文一致）：第一行为直接以 $\{U_m\}$ 为目标由 FGTO 实现的结果，第二行为把同一 $\{U_m\}$ 分解为 QuMIS 基本门后实现的结果；QuMIS 路径在所有门上都明显更长，例如 $\hat U_1$ 的 $T$ 由 8.4 降到 2.1，$\hat U_2$ 由 6.0 降到 2.1。[^r7] 在 N 比特 QFT 上，$T$ 与 $N$ 近似满足 $T = \gamma_T N + \beta_T$，且误差 $\varepsilon$ 随 $N$ 指数变化；二、三比特 QuVIS 的 $T$ 都低于 QuMIS，并在 $N \geq 5$ 的线性拟合中保持显著优势。[^r4] 论文指出：直接控制（direct control）方案虽然误差最低，但其计算开销随 N 指数增长，因而难以扩展到大规模线路。[^r4] 在仿真中，FGTO 的损失函数最终收敛到约 $O(10^{-6})$（见补充材料的图 S2），而 $O(10^{-2})$ 这一比较基准仅为方便横向对比 QuMIS 与 QuVIS 的时间开销而选取。[^r4]

### 灵活性与适配性

由于 FGTO 在实现酉变换上的通用性与稳定性，QuVIS 可针对不同的量子硬件自适应地定义——无论相互作用类型（伊辛、海森堡等）、连通性与耦合强度如何，只要相互作用是已知的即可应用。[^r1] 对相互作用未知的情形，作者明确提出可将 QuVIS 与通过机器学习局部可观测量与约化密度矩阵来估计相互作用的方法相结合（引用文献 [72–74]）。[^r8]

### 与多比特 SWAP 线路的对比

作者同时在多比特 SWAP 线路上演示了 QuVIS 的优势：与 QuMIS 编译相比，时间开销同样被压缩，且误差随编译深度（即线路所含基本门数）的减少呈代数式抑制。[^r1]

### 适用范围与作者明确的局限

- 适用条件：硬件中自旋间的相互作用是已知的；QuVIS 可针对该相互作用优化门实现。[^r1]
- 作者明确给出的扩展方向：当相互作用未知时，可结合机器学习估计局部可观测量与约化密度矩阵的方法（见文献 [72–74]）。[^r8]

## Sources

[^r1]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L13
[^r2]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L25
[^r3]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L28
[^r4]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L100
[^r5]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L76
[^r6]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L82
[^r7]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L102
[^r8]: raw-not-distributed/D044-2023-luying-quvis/paper.md#L183
