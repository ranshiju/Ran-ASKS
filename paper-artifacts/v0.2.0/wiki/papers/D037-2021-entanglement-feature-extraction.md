---
title: "Entanglement-Based Feature Extraction by Tensor Network Machine Learning"
type: paper-summary
sources:
  - "raw-not-distributed/D037-2021-entanglement-feature-extraction/paper.md"
source_type: official-doc
date: 2021
venue: "Frontiers in Applied Mathematics and Statistics"
authors: ["Yuhan Liu", "Wen-Jun Li", "Xiao Zhang", "Maciej Lewenstein", "Gang Su", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Entanglement-Based Feature Extraction by Tensor Network Machine Learning

> **作者**：Yuhan Liu、Wen-Jun Li、Xiao Zhang、Maciej Lewenstein、Gang Su、Shi-Ju Ran | **发表**：Frontiers in Applied Mathematics and Statistics
> **核心贡献**：提出一种基于矩阵乘积态（MPS）纠缠结构的特征提取算法，通过丢弃对应低纠缠量子位的特征，在 MNIST 十分类任务中将特征数量降至原来的 1/10 以下而精度仅下降约 10⁻³ 量级。

## Navigation

本文将矩阵乘积态（MPS）作为图像分类器，研究其纠缠性质与特征重要性之间的联系。作者利用单点纠缠熵（SEE）和双点纠缠熵（BEE）刻画 MPS 分类器各位置的纠缠强度，并据此提出一种可解释的特征提取方案。实验在 MNIST 手写数字数据集上进行，结合离散余弦变换（DCT）和 zig-zag 路径优化，仅保留少量高纠缠对应的特征即可维持分类精度。

## 研究方向定位

研究对象为基于矩阵乘积态（MPS）的图像分类器；核心问题是如何利用 MPS 量子态的纠缠性质来刻画特征重要性并实现高效的特征提取；方法/场景为在 MNIST 十类手写数字分类任务中通过丢弃低纠缠位置的特征来压缩输入[^r1]。

## Content

### 背景：量子信息与机器学习的交叉

将经典信息处理（如图像、声音、金融数据等模式识别与分类）推广到量子版本是过去三十年的活跃方向，已有量子感知机、量子神经网络、量子金融与量子博弈论等尝试；近年来量子门、量子电路与量子计算机也被引入以增强学习过程[^r1]。同时，把量子信息方法应用于经典信息处理也形成了一条独立路线：早在 2000 年 Hao 等人就将长 DNA 序列映射为与多体波函数相似的数学对象；2005 年 Latorre 独立提出位图图像到多体波函数的映射并用于图像压缩（虽压缩率不及 JPEG，但随后被反过来用于从多体波函数恢复位图图像）[^r1]。张量网络（TN）在量子多体物理与量子机器学习的融合中扮演关键角色，可高效表示满足纠缠熵面积律的多体态（如一维有隙局域哈密顿量的基态、最近邻 RVB 态等），典型例子包括 MPS、投影纠缠对态（PEPS）、树 TN 和多尺度纠缠重整化拟设（MERA）[^r1]。

### 张量网络机器学习与可解释性挑战

MPS 已用于有监督图像识别与生成式建模（学习联合概率分布），并已有研究表明长程关联对图像分类并非必需，从而支撑了 MPS 的可行性；树 TN 也被用于自然语言建模与图像识别[^r1]。然而这些方法仍面临挑战：经典机器学习模型常被视为“黑箱”，预测虽准确却难以解释其内部逻辑；对 TN 而言，如何利用量子态性质（如纠缠）来改进算法、提升可解释性是一个关键问题[^r1]。

### MPS 分类器与训练算法

文中采用 MPS 表示的线性映射 Ψ̂：将一张 L 像素图像的特征向量 v^[n]（由特征映射生成）映射到 D 维向量 u^[n]，u 的第 b 个分量给出该图像被分到第 b 类的概率[^r2]。对十类手写数字任务，由于图像像素间的短程关联（卷积神经网络使用小卷积核即可获得良好性能是证据之一），MPS 表示是可行的；若图像具有长程关联（如分形图案），MPS 表示可能效率较低[^r2]。

为避免 Ψ̂ 的参数随 L 指数增长，文中采用 MPS 拟设：每个位置 l 对应一个张量 A^[l]，相邻张量之间通过维度为 χ 的虚拟键（virtual bond）相连，总参数量为 O(d χ² L)；数值实验中取 χ = 32[^r2]。训练时通过沿 MPS 来回扫描、逐一优化张量以最小化负对数似然（NLL）代价函数，并在实际模拟中利用 MPS 的正则形式保持 Z = 1[^r2]。更新某一张量时先对其做奇异值分解（SVD），将标签键 b 传到下一张量，再对该张量执行梯度下降，然后继续前进更新下一张量，完成一轮后所有张量均被更新一次[^r2]。

### 纠缠驱动的特征提取

核心方法是利用 MPS 的纠缠性质来评估特征重要性：对 MPS 分类器计算各位置的纠缠（文中使用 SEE 与 BEE），对应低纠缠位置的特征被认为对分类贡献较小，可以丢弃，从而实现特征提取[^r3]。在 MNIST 十类分类器上，结合离散余弦变换（DCT）与路径优化，特征数量可安全地降至原始数量的不到 1/10，而分类精度仅下降 O(10⁻³)[^r1][^r3]。

### 与现有方法的比较及意义

现有图像特征提取通常依赖图像分割与各种矩阵变换滤波器，直接以空间或变换后的特征本身作为重要性判据；本文提出的算法不依赖分割，而是聚焦特征之间的关联[^r3]。作者将本工作视为张量网络版的灵敏度分析（sensitivity analysis），为可解释机器学习提供了影响函数、核方法等之外的一种替代路径[^r3]。该方法原则上也可推广到更复杂的 TN 结构（如 PEPS）以服务高效机器学习[^r3]。

## Sources

[^r1]: raw-not-distributed/D037-2021-entanglement-feature-extraction/paper.md#L33
[^r2]: raw-not-distributed/D037-2021-entanglement-feature-extraction/paper.md#L68
[^r3]: raw-not-distributed/D037-2021-entanglement-feature-extraction/paper.md#L175
