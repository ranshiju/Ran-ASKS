---
title: "Compressing Neural Networks Using Tensor Networks with Exponentially Fewer Variational Parameters"
type: paper-summary
sources:
  - "raw-not-distributed/D050-2025-adtn-nn-compression/paper.md"
source_type: official-doc
date: 2025
venue: "Intell. Comput."
authors: ["Yong Qing", "Ke Li", "Peng-Fei Zhou", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Compressing Neural Networks Using Tensor Networks with Exponentially Fewer Variational Parameters

> **作者**：Yong Qing、Ke Li、Peng-Fei Zhou、Shi-Ju Ran | **发表**：Intell. Comput.
> **核心贡献**：提出自动可微张量网络（ADTN），将神经网络层的变分参数编码为深层张量网络的收缩结果，把参数量从指数级 O(2^Q) 降至线性级 O(MQ)，并在多个经典网络上验证了显著的压缩比与精度提升。

## Navigation

ADTN（Automatically Differentiable Tensor Network）是一类用于神经网络压缩的张量网络编码方案。它把 NN 层的变分参数 T 表示为一个"砖墙"结构深层张量网络的收缩结果，利用自动微分直接优化网络中的张量。该方法与 TT/MPS/MPO 等浅层张量分解及 LoRA 等显式低秩插入方法有本质区别。作者在 FC-2、LeNet-5、AlexNet、ZFnet、VGG-16 等网络上对 MNIST、CIFAR-10、CIFAR-100 进行了验证，获得了极高压缩比与精度改善。

## 研究方向定位

针对大规模神经网络参数过多导致的过拟合与硬件开销问题，该研究将 NN 层的变分参数编码为深层张量网络的收缩结果，并通过自动微分进行优化 [^r1]。

## Content

### 核心思路：将变分参数编码为深层张量网络的收缩

NN 可写作 y = f(x; T, T', …) 的映射；ADTN 并不限定层的类型（线性、卷积等），而是把待压缩的变分参数张量 T 表示为一个深层张量网络的收缩输出[^r2]。网络采用常见的"砖墙"结构：每个块是一个张量，共享的键（bond）表示需要求和的哑指标，垂直的黑线表示激活函数（如 ReLU）[^r3]。把 2^Q 个变分参数编码进一个 ADTN 时，得到的仍是一个 Q 阶张量，每个指标维度取 d=2；左边界放置若干 2 维常数向量（如 v = [1, 0]）以方便表述与分析[^r4]。

### 砖墙结构与计算复杂度

砖墙 ADTN 由若干 (2×2×2×2) 张量 {A^[k]}（k = 1, …, K）构成；每 (Q−1) 个相邻列的张量构成一个 TN 层，相邻层之间放置一个激活函数 σ[^r5]。整个映射可写为

$$
\boldsymbol{\mathcal{T}} = \boldsymbol{\mathcal{L}}^{(M)} \sigma\!\left(\dots \boldsymbol{\mathcal{L}}^{(3)} \sigma\!\left(\boldsymbol{\mathcal{L}}^{(2)} \sigma\!\left(\boldsymbol{\mathcal{L}}^{(1)} \prod_{\otimes q=1}^{Q} v\right)\right)\right),
$$

即对 Π_{⊗q=1}^Q v 依次施加映射 {σ(𝓛^(m)(·))}（m = 1, …, M）[^r6]。实际计算无需显式构造矩阵 {𝓛^(m)}，只需从左到右顺序收缩 4 阶张量即可，单个张量的收缩等价于一个 (2^(Q−2)×4) 矩阵与 (4×4) 矩阵相乘[^r7]。在编码 2^Q 个参数时，砖墙 ADTN 的变分参数总量为

$$
\#(\mathrm{ADTN}) \equiv \sum_k \#(A^{[k]}) \sim O(MQ),
$$

其中 M ∼ O(1)，从而将原本 O(2^Q) 的指数复杂度降为相对 Q 的线性复杂度 O(MQ)[^r8]。

### 与既有压缩方法的区别

ADTN 直接把 NN 参数编码进深层 TN，并通过自动微分更新其中的张量，这与依赖显式分解/分解步骤或插入矩阵的 TT、LoRA、Tucker/CPD 混合分解、MUSCO 等方法存在本质区别[^r9]。作者指出，浅层 TN（MPS/MPO 等）虽已成功用于 NN 压缩，但深层 TN 在量子物理与机器学习研究中已被证明具有显著不同的、更强的表示能力，ADTN 正利用了这一深层结构所提供的表征能力[^r10]。

### 实验结果概览

作者在 FC-2、LeNet-5、AlexNet、ZFnet、VGG-16 上，针对 MNIST、CIFAR-10、CIFAR-100 评估了 ADTN 的压缩性能，结构与超参数详见补充材料 S1 节[^r11]。例如：在对 VGG-16 的两个全连接层进行压缩的实验中，约 10^? 个参数被压缩为仅 424 个参数的 2 个 ADTN，且 CIFAR-10 的测试准确率从 90.17% 提升至 91.74%[^r12]。

### 灵活性与未来方向

ADTN 方案具有较高灵活性：可调深度、键维度等超参数，可采用非砖墙结构，激活函数也可替换为 leaky ReLU 等；还可以将多个 NN 层的参数拼接成单一张量后用单个 ADTN 压缩，例如在存在冗余 NN 层时可能有帮助，且 NN 层类型对方案并无影响[^r13]。作者也明确指出当前局限：解码（收缩）过程会为推理带来额外计算开销，因为 ADTN 收缩与 NN 推理目前仍是两个独立步骤；将二者合并以提高计算效率的工作留待未来研究[^r14]。

## Sources

[^r1]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L29
[^r2]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L39
[^r3]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L50
[^r4]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L52
[^r5]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L54
[^r6]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L56
[^r7]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L64
[^r8]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L66
[^r9]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L31
[^r10]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L27
[^r11]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L33
[^r12]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L33
[^r13]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L190
[^r14]: raw-not-distributed/D050-2025-adtn-nn-compression/paper.md#L192
