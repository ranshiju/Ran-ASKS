---
title: "Machine learning by unitary tensor network of hierarchical tree structure"
type: paper-summary
sources:
  - "raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md"
source_type: official-doc
date: 2019
venue: "New J. Phys."
authors: ["Ding Liu", "Shi-Ju Ran", "Peter Wittek", "Cheng Peng", "Raul Blázquez García", "Gang Su", "Maciej Lewenstein"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Machine learning by unitary tensor network of hierarchical tree structure

> **作者**：Ding Liu、Shi-Ju Ran、Peter Wittek、Cheng Peng、Raul Blázquez García、Gang Su、Maciej Lewenstein | **发表**：New J. Phys.
> **核心贡献**：提出一种基于多尺度纠缠重整化拟设（multi-scale entanglement renormalization ansatz, MERA）启发的训练算法，将二维层级树张量网络（tree tensor network, TTN）用于图像分类，并在训练中保持张量酉性，由此将图像类别编码为量子多体态。

## Navigation

该工作将二维层级 TTN 与 MERA 训练算法结合用于 MNIST 与 CIFAR 图像识别，并把每个图像类别关联到一个量子多体态 $|\psi_p\rangle$ 上。关键做法是在训练过程中保持 TTN 的酉性（$\hat\Psi^\dagger\hat\Psi = I$），从而把经典图像与量子态、纠缠、保真度等量子量建立起直接联系。文末还讨论了在量子模拟/计算硬件上制备这种酉 TTN 进行图像识别的可能性。

## 研究方向定位

研究对象为图像识别任务中的二维层级树张量网络分类器，核心问题是如何在保持网络酉性的前提下训练这种 TTN 并把类别编码为量子多体态，所提方法为一种受 MERA 启发的逐张量酉约束训练算法。[^r1]

## Content

### 动机与背景

论文从量子多体物理与机器学习的相似性出发：TNs 已用于降维与手写识别 [^r2]，深度学习与重整化群之间也存在映射，因而可以由量子纠缠的视角设计网络 [^r2]。先前基于一维 TN（如 matrix product state, MPS）的方法在图像识别上的可扩展性与灵活性受限，作者因此选择二维层级 TTN 来更贴合图像的二维结构 [^r3]。

### 图像到量子态的映射与网络结构

每个像素 $x$ 通过非线性特征映射到一个 $d$ 维归一向量 $\nu_s(x)$（一般形式见公式 (1)，文中取 $d=2$），整幅图像因此被向量化为一个 $d^L$ 维 Hilbert 空间中的乘积态 $|\nu^{[n]}\rangle$ [^r4]。分类器是一个 $K$ 层、五指标的 TTN $\hat\Psi$（公式 (2)）：输入键维度 $d$、虚拟键维度 $\chi$，最右侧顶部键 $p$ 为输出，对应类别数 $D$；预测通过 $|\tilde p^{[n]}\rangle = \hat\Psi^\dagger|\nu^{[n]}\rangle$ 得到（公式 (3)）[^r5]。

### MERA 启发的酉训练算法

代价函数取为预测与标签内积之和的负值（公式 (4)），可由均方误差（公式 (5)–(6)）导出 [^r6]。为压低主要代价项，借鉴 MERA 在训练中要求 TTN 满足 $\hat\Psi^\dagger\hat\Psi = I$，并在训练样本所在子空间上近似满足 $\hat\Psi\hat\Psi^\dagger \simeq I$；进一步采用 MERA 的更强约束——TTN 中每个张量都需为等距（isometry）[^r7]。作者指出，与已有把 TTN 用于逼近高阶张量或与卷积算术电路做结构对偶的工作不同，本文通过最小化代价函数训练 TTN，目的是分类而非张量逼近，且不限制张量为 delta 张量等特殊形式 [^r7]。

### 量子多体态、纠缠与保真度

训练完成后可定义关联到第 $p$ 类的量子多体态 $|\psi_p\rangle$，形如 TTN，对近似解有 $|\psi_p\rangle \simeq \tilde\psi_p\rangle = \overline{\sum_{n\in p\text{th class}}|\nu^{[n]}\rangle}$ [^r8]。结合 t-SNE 低维嵌入观察发现，层级 TTN 在不同层呈现出与深度卷积网络、深度信念网络类似的抽象层级递增，最高层可清晰分离类别 [^r9]。在两类分类任务上比较保真度与精度，发现保真度可反映两类之间的分类难度；纠缠熵处于 $S \sim O(1)$ 量级，表明分类器可被高效表示为弱纠缠态 [^r10]。

### 实验与基准

算法在 MNIST（手写识别）与 CIFAR（图像识别）数据集上测试；附录 C 还给出与 SVM、KNN、CNN 等经典方法的对比 [^r11]。作者同时提到，TN（包括 MPS）的量子态层析已有相关工作，可用于验证用于机器学习的 TTN 态 [^r12]。

### 结论与未来工作

- TTN 的表征能力上限由输入键维度决定，虚拟键维度决定逼近该上限的程度 [^r13]。
- 层级 TTN 在抽象层级化上与深度卷积网络、深度信念网络表现相似 [^r13]。
- 训练得到的酉 TTN 对应可在量子硬件上运行的 tensor network 电路，并可被制备为量子态用于图像识别等机器学习任务 [^r14]。

## Sources

[^r1]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L86
[^r2]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L81
[^r3]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L86
[^r4]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L94
[^r5]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L104
[^r6]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L118
[^r7]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L130
[^r8]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L130
[^r9]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L88
[^r10]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L88
[^r11]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L255
[^r12]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L205
[^r13]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L201
[^r14]: raw-not-distributed/D021-2019-machine-learning-by-unitary-tensor-network-of-hierarchical-tree-structure/paper.md#L203
