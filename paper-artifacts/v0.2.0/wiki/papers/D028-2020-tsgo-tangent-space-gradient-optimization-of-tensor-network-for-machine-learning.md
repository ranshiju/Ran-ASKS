---
title: "Tangent-space gradient optimization of tensor network for machine learning"
type: paper-summary
sources:
  - "raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md"
source_type: official-doc
date: 2020
venue: "Phys. Rev. E 102, 012152 (2020)"
authors: ["Zheng-Zhi Sun", "Shi-Ju Ran", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Tangent-space gradient optimization of tensor network for machine learning

> **作者**：Zheng-Zhi Sun、Shi-Ju Ran、Gang Su | **发表**：Phys. Rev. E 102, 012152 (2020)
> **核心贡献**：针对概率模型提出切空间梯度优化 (TSGO)，通过将梯度限制在参数向量正交的切平面上，以旋转角 θ 自然决定学习率 η = tan θ，避免梯度消失与爆炸问题。

## Navigation

本文针对深度计算图中常见的梯度消失与爆炸问题，提出了一种适用于概率模型的切空间梯度优化方法 (TSGO)。其核心思想是强制梯度方向与参数向量正交，通过旋转参数向量而非调整标量学习率来执行更新。作者将 TSGO 应用到张量网络 (TN) 生成模型——即把联合概率分布表示为 Hilbert 空间中的归一化态 |ψ⟩——并在 MNIST 上的无监督生成矩阵乘积态 (MPS) 实验中，与 BP + Adam 进行对比，验证 TSGO 在不同初始旋转角和不同 MPS 长度下均更稳定。论文同时讨论了 TSGO 与权重归一化、批量归一化等归一化技术的关系及其向玻尔兹曼机、贝叶斯网络等其他概率模型推广的可能性[^r1][^r2]。

## 研究方向定位

研究对象为以归一化态 |ψ⟩ 描述联合概率分布的张量网络概率模型，核心问题是深度模型中 BP + Adam 仍受梯度消失/爆炸及手动调学习率之苦，方法是在中心正交形式下于单位球 ⟨ψ|ψ⟩=1 的切空间中以旋转角 θ 驱动参数更新[^r3]。

## Content

### 动机：BP + 自适应方法的局限

基于梯度的优化（以 BP 为代表）是训练前馈网络的基础，但当计算图变深时会出现梯度消失与爆炸，使优化低效或不稳定。为此研究者提出了随机梯度下降、均方根传播、AdaGrad、Adam 等方法来调节学习率；然而这些方法（包括 Adam）的有效性仍依赖于学习率的人工选取[^r4]。

### TSGO 的核心机制

TSGO 将参数向量朝梯度方向旋转，并保证梯度落在参数空间的切超平面上。归一化后，学习率 η 由旋转角 θ 通过 η = tan θ 自然确定，因而原则上避免了梯度消失或爆炸，并提供了一种稳健的学习率决定方式。对于以归一化态 |ψ⟩（满足 ⟨ψ|ψ⟩=1）描述概率分布的 TN 生成模型，借助张量网络的中心正交形式 (central-orthogonal form) 可便捷地实施归一化，并证明梯度落在 ⟨ψ|ψ⟩=1 球面的切超平面上[^r5]。

### 在生成 MPS 上的数值结果

在 MNIST 上的无监督生成 MPS 模型中，作者将 TSGO 与自动求导 BP 配合 Adam 进行了对比：BP 直接计算张量梯度而不强制 MPS 处于中心正交形式，学习率由 Adam 决定[^r6]。

TSGO 在测试中表现出最佳收敛性：

- 即使初始旋转角接近 π/2（等价于一个很大的初始学习率），TSGO 仍能稳定收敛到同一位置；取 π/36 或 π/18 等合理初始旋转角时，TSGO 在十个 epoch 内即可收敛[^r7]。
- Adam 在初始学习率 10⁻⁵ 至 10⁻⁴ 范围内损失函数收敛到较高值，呈现梯度消失；初始学习率高于 10⁻³ 时优化变得不稳定，呈现梯度爆炸[^r8]。

为进一步验证 TSGO 确实绕开了梯度消失，作者先用 Adam 训练若干 epoch 再切换到 TSGO：切换后损失函数立即由约 80 降至 40，测试集最终收敛到 44.5，明显低于纯 Adam 的 79.5；说明 Adam 优化得到的态仍可被 TSGO 进一步纠正[^r9]。

### 深度（MPS 长度）对收敛的影响

深度神经网络梯度涉及一连串矩阵的连乘；若同一矩阵 M 被重复乘 N 次，则 M = UΛU⁻¹ 会导致梯度形如 UΛᴺU⁻¹，特征值被 Λᴺ 缩放，因而大于 1 的特征值会爆炸，小于 1 的会消失。MPS 的长度 N 对应 NN 的深度；当 MPS 已（或近似）归一化时，特征值通常小于 1，因此主要遭遇梯度消失[^r10]。

数值上，作者通过改变图像尺寸控制 MNIST 上 MPS 长度 N：

- 当 N < 约 100 时，TSGO 与 Adam 仅有微小差异；
- 当 N 较大时，BP + Adam 陷入更差的收敛，TSGO 给出明显更低的损失[^r11]。
- 图 4 还显示 Adam 在 η=10⁻³、MPS 长度大于 14² 时已不稳定[^r12]。

### 结论与讨论

论文总结指出，TSGO 通过把梯度限制在单位球上、与参数向量正交，以旋转方式在 Hilbert 空间内更新参数，实现了与初始学习率和模型深度无关的稳健收敛；与之相比，BP + Adam 在不同学习率和较大深度下都会受困于梯度消失与爆炸[^r13]。

作者同时讨论了与归一化技术的关系：权重归一化、批量归一化、层归一化等方法通过再中心化和再缩放使预测对参数向量保持不变，从而具有隐式的"早停"效应并稳定学习；TSGO 则更进一步，在概率模型中显式给出了参数向量与梯度之间的几何关系[^r14]。

### 适用范围与局限

作者明确指出，TSGO 一般不能直接用于更新一般神经网络——因为在强非线性下无法保证 Eq. (2) 成立；但原则上可推广到其他概率模型，例如玻尔兹曼机和贝叶斯网络。作者预期 TSGO 将在发展新机器学习算法方面具有重要应用[^r15]。

## Sources

[^r1]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L13
[^r2]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L188
[^r3]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L23
[^r4]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L19
[^r5]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L23
[^r6]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L175
[^r7]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L177
[^r8]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L177
[^r9]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L182
[^r10]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L184
[^r11]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L186
[^r12]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L193
[^r13]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L190
[^r14]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L195
[^r15]: raw-not-distributed/D028-2020-tsgo-tangent-space-gradient-optimization-of-tensor-network-for-machine-learning/paper.md#L197
