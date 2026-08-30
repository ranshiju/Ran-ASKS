---
title: "Encoding of matrix product states into quantum circuits of one- and two-qubit gates"
type: paper-summary
sources:
  - "raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md"
source_type: official-doc
date: 2020
venue: "Phys. Rev. A 101, 032310 (2020)"
authors: ["Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Encoding of matrix product states into quantum circuits of one- and two-qubit gates

> **作者**：Shi-Ju Ran | **发表**：Phys. Rev. A 101, 032310 (2020)
> **核心贡献**：提出一种高效且准确的算法，将给定的 N 量子比特、虚维度 χ ≫ 物理维度 d=2 的矩阵乘积态 (MPS) 编码为由仅含单比特与双比特门构成的量子电路。 [^r1]

## Navigation

该工作面向如何在近期量子平台上实现具有较大虚拟维度 χ 与较大纠缠的 N 量子比特 MPS 这一难题，提出"矩阵乘积解纠缠器 (matrix product disentangler, MPD)" 思路：用一层一层的酉矩阵乘积算符最优地把目标 MPS 解纠缠为乘积态，从而反推出由单、双比特门构成的深度电路。[^r2] 作者将该方法与 qubit-efficient scheme 结合，可仅用少于 10 个量子比特实现 N = 24–150 个自旋的强关联一维模型的基态 MPS，并保持较高保真度。[^r3]

## 研究方向定位

研究对象为 N 量子比特、虚维度 χ ≫ 物理维度 d=2 的矩阵乘积态 (MPS) 在量子电路上的高效编码；核心问题是大 χ MPS 在量子硬件上难以直接实现；方法为构造酉矩阵乘积解纠缠器 (MPD) 把目标 MPS 解纠缠为乘积态，进而生成仅含单比特与双比特门的深度电路。[^r4]

## Content

### 背景与动机：MPS 在量子硬件上的实现瓶颈

MPS 是当代物理中最重要的数学工具之一：它是密度矩阵重整化群 (DMRG) 及其变体所采用的状态拟设，可有效描述一维有隙系统的基态与（纯化）热态，并被广泛用于统计物理、非平衡量子物理、场论以及机器学习等领域。[^r5] MPS 也是量子信息与计算中的重要模型，可表示 GHZ 态与 AKLT 态等非平凡量子态，用于实现具有实质意义的量子计算任务。[^r6]

在量子硬件上实现 MPS 受到两方面限制：一方面是相干时间短、可用的计算量子比特数有限；另一方面，MPS 含有两类自由度——表征物理模型 Hilbert 空间的物理自由度（维度 d）与承载纠缠的虚拟自由度（维度 χ），通常 χ ≫ d，在硬件上需以 χ 级 qudit 实现，而 χ 一般为 O(10²) 或更大，这几乎不可行。[^r7] 一种变通方案是以多比特门等效替代 χ 级 qudit，再编译到单、双比特门，但当 χ 较大时电路深度一般随 χ 多项式增长，效率极低，因此迫切需要更高效的 MPS 编码算法。[^r8]

### 方法：矩阵乘积解纠缠器 (MPD) 与深度电路

作者提出一种新算法，可高效、准确地将 d = 2、χ ≫ d 的给定 MPS 编码为仅由单比特与双比特门构成的量子电路。核心思想是构造一层层的酉矩阵乘积算符，即 "矩阵乘积解纠缠器" (matrix product disentanglers, MPD)，把目标 MPS 解纠缠为乘积态。[^r9] 这些 MPD 堆叠形成多层（深度）量子电路，由一个乘积态出发演化到目标 MPS，并以高保真度逼近之。[^r10] 与既有编译方法的本质区别在于，该方法不是将多比特门逐步分解，而是直接优化 MPD 使其最优地将 MPS 解纠缠。[^r11]

### 数值基准：强关联自旋模型的基态

作者将该编码算法应用于近似一维强关联自旋系统基态的 MPS。这些 MPS 纠缠较大，用既有方法实现显然困难。[^r12] 数据表明：仅用 O(10) 层 MPD 构成的深度量子电路，即可在 MPS 与电路演化态之间达到高保真度。[^r13] 结合 qubit-efficient scheme 后，整个 MPS 可被编码到少于 10 个量子比特的量子电路上，远少于 MPS 自身的尺寸 (N = 24–150)。[^r14]

### 意义与适用范围

该方法为大虚拟维度和大系统尺寸的 MPS 在近期量子平台上作为量子电路实现，提供了一条可行且高效的路径，特别适用于实现"有用且/或具有奇异特性"的量子态与基于 MPS 的模型。[^r15] 作者在第 V 节总结中指出，深度量子电路可准确且高效地把一个乘积态演化为目标 MPS，即使该 MPS 具有较大的虚拟维度和/或系统尺寸。[^r16]

## Sources

[^r1]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L9
[^r2]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L23
[^r3]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L25
[^r4]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L23
[^r5]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L15
[^r6]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L17
[^r7]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L19
[^r8]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L21
[^r9]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L23
[^r10]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L9
[^r11]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L9
[^r12]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L25
[^r13]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L25
[^r14]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L25
[^r15]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L9
[^r16]: raw-not-distributed/D025-2020-mps_deep_encoding_circuit/paper.md#L159
