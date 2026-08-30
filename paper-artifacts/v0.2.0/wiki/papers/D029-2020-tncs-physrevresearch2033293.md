---
title: "Tensor network compressed sensing with unsupervised machine learning"
type: paper-summary
sources:
  - "raw-not-distributed/D029-2020-tncs-physrevresearch2033293/paper.md"
source_type: official-doc
date: 2020
venue: "Phys. Rev. Research 2, 033293 (2020)"
authors: ["Shi-Ju Ran", "Zheng-Zhi Sun", "Shao-Ming Fei", "Gang Su", "Maciej Lewenstein"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Tensor network compressed sensing with unsupervised machine learning

> **作者**：Shi-Ju Ran、Zheng-Zhi Sun、Shao-Ming Fei、Gang Su、Maciej Lewenstein | **发表**：2020
> **核心贡献**：提出张量网络压缩感知 (TNCS)，结合压缩感知、张量网络与机器学习，将真实信息通过生成型 Born 机与设计性投影进行压缩与传输。

## Navigation

TNCS 的核心是把压缩感知、张量网络与无监督机器学习三者的思想融合在一套量子通信协议中 [^r6]。发送方先用无监督 TN 学习训练一个 Born 机 $|\Psi\rangle$ 表示待传数据的概率分布，再通过投影把要传输的具体信息编码进 $|\Psi\rangle$，接收方则反演投影并采样生成恢复图像 [^r6]。论文还引入 "q 稀疏度" (q sparsity) 来刻画量子态的稀疏性，并以此定性估计 TNCS 的效率 [^r6]。在 MNIST 手写数字与 Fashion-MNIST 时装图像上的测试表明，该方法在压缩比与重建准确率上具有与传统压缩感知方案可比的竞争性表现 [^r6]。

## 研究方向定位

研究对象为面向真实数据（如手写数字、时装图像）的压缩感知协议，核心问题是如何用由无监督张量网络学习得到的量子多体态作为生成模型、并通过部分投影将待传信息的高维经典数据压缩为少量特征与一个投影态；论文以矩阵乘积态 (MPS) 作为 Born 机并在经典模拟上演示该协议 [^r23]。

## Content

### 协议动机与场景设定

论文将发送方 Alice 与接收方 Bob 的经典通信场景与量子态测量结合起来 [^r6]。Alice 想发送一张约 $O(10^2)$ 像素的图像 $\{x\}$ 给 Bob，她只需经典传输少量像素 $\{x^{\mathrm{sent}}\}$，其余 $\{x^{\mathrm{rest}}\}$ 通过双方预先共享的 Born 机 $|\Psi\rangle$ 配合依 $\{x^{\mathrm{sent}}\}$ 决定的投影被 Bob 恢复 [^r6]。由于量子态的制备与每次测量都会扰动态、把高维真实数据塞进多比特量子通信本身面临希尔伯特空间指数增长的难度，论文把 TN 视为"量子启发"的经典可模拟工具来应对这一难题 [^r6]。

### 主要步骤（训练 → 投影 → 解码）

TNCS 的主要流程分为三步（参见原文 Fig. 1）：(1) 通过无监督 TN 学习训练一个表示待传数据概率分布的 Born 机 $|\Psi\rangle$；(2) 用待传的特定信息对 $|\Psi\rangle$ 进行投影得到 $|\widetilde{\Psi}\rangle$；(3) 在接收端通过投影后的 Born 机以生成式方式解码得到恢复信息 [^r6]。

### 监督学习前的无监督 TN 学习

每张图像第 $n$ 个灰度像素 $x_{i,n}\in[0,1]$ 被映射为单比特态 $|s(x_{i,n})\rangle=\cos(x_{i,n}\pi/2)|0\rangle+\sin(x_{i,n}\pi/2)|1\rangle$，整张图成为 $N$ 比特的乘积态 $|\phi_i\rangle=\prod_n|s(x_{i,n})\rangle$ [^r23]。TN 取为矩阵乘积态 (MPS)，其参数通过最小化负对数似然 (NLL) $f=\ln|\langle\Psi|\Psi\rangle|^2-\sum_i\ln|\langle\Psi|\phi_i\rangle|^2/N$ 训练得到 [^r23]。MPS 的总参数量随 $N$ 仅线性增长为 $\sim 2N\chi^2$（$\chi$ 为虚键维度），相比之下希尔伯特空间维数随 $N$ 指数增长至 $\sim 2^N$ [^r23]。这样得到的 $|\Psi\rangle$ 给出像素的联合概率 $P(\{x\})=|\prod_n\langle s(x_n)|\Psi\rangle|^2$，亦称 Born 机 [^r23]。

### "q 稀疏度"与效率刻画

为定量刻画 TNCS 的效率，论文类比压缩感知中的信号稀疏度，提出了 q 稀疏度 (q sparsity) 来刻画量子态的稀疏性 [^r6]。q 稀疏度之所以必要，根本原因在于 TN 态遵从纠缠熵的面积律 (area law of entanglement entropy) [^r6]。在 MNIST 与 Fashion-MNIST 上的测试显示，TNCS 在压缩比与重建准确率上达到了与传统压缩感知方案 (例如基于深度神经网络自编码器、辅助采样的高效马尔可夫链) 可比的竞争性表现 [^r6]。

### 与已有方法的差异

与近期利用深度神经网络自编码器为高效马尔可夫采样设计压缩比的工作不同，TNCS 采用结构相对简单而浅层的矩阵乘积态 (MPS) 来编码信息、并以纠缠作为采样策略的依据 [^r6]。论文强调，虽然这里以经典模拟来演示 TNCS，但协议原则上可在量子平台上实现，例如通过张量网络设计量子电路作为 Born 机，或直接把多比特态当作 Born 机使用 [^r23]。

### 局限与面向量子平台的开放问题

作者明确指出 TNCS 当前主要受限于两方面在量子平台上尚不可行：(1) 在量子设备上实现 TN 模型或对应的多比特量子态；(2) 实际测量纠缠 [^r30]。作者预期未来量子器件能力提升后这些障碍将被克服，并探讨了将 TNCS 推广到基于量子理论的真实数据安全通信的可能性 [^r30]。

## Sources

[^r6]: raw-not-distributed/D029-2020-tncs-physrevresearch2033293/paper.md#L23
[^r23]: raw-not-distributed/D029-2020-tncs-physrevresearch2033293/paper.md#L229
[^r30]: raw-not-distributed/D029-2020-tncs-physrevresearch2033293/paper.md#L197
