---
title: "Entanglement scaling and criticality of infinite-size quantum many-body systems in continuous space addressed by a tensor network approach"
type: paper-summary
sources:
  - "raw-not-distributed/D049-2025-ifmps-hong-rui/paper.md"
source_type: official-doc
date: 2025
venue: "Phys. Rev. B 111, 245109 (2025)"
authors: ["Rui Hong", "Hao-Wei Cui", "An-Chun Ji", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Entanglement scaling and criticality of infinite-size quantum many-body systems in continuous space addressed by a tensor network approach

> **作者**：Rui Hong、Hao-Wei Cui、An-Chun Ji、Shi-Ju Ran | **发表**：Phys. Rev. B 111, 245109 (2025)
> **核心贡献**：将 iTEBD 算法与平移不变函数张量网络相结合，在连续空间中求解无穷多耦合量子振子 (iCQOs) 的基态，揭示了物理区与非物理区分界点处纠缠熵与关联长度的临界标度律，并由此识别出 $c=1$ 自由玻色 CFT；进一步证明三体耦合会破坏该 CFT 描述。

## Navigation

本文研究连续空间中无穷多耦合量子振子 (iCQOs) 的基态波函数及其纠缠与临界性质 [^r1]。作者将适用于格点模型的虚时演化算法与平移不变函数矩阵积态 (MPS) 结合，从而直接求解具有无穷多变量的多体薛定谔方程组 [^r1]。数值结果显示，在仅含两体耦合的情形下，物理区与非物理区的分界点处纠缠熵呈对数标度、关联长度呈幂律标度，标度系数给出中心荷 $c=1$，对应自由玻色共形场论 (CFT) [^r1]。同时，三体耦合即使强度很弱，也会使该 CFT 描述失效 [^r1]。

## 研究方向定位

针对连续空间中无穷多耦合量子振子 (iCQOs) 的基态求解与纠缠-临界性刻画，提出将 iTEBD 与平移不变函数张量网络结合的数值方法 [^r1]。

## Content

### 连续空间多体薛定谔方程与函数张量网络方法

在无格点近似下，连续空间多体系统需直接求解无穷多变量偏微分方程形式的薛定谔方程，解析几乎不可行，蒙特卡洛与神经网络方法又面临"指数墙"问题 [^r1]。作者将量子格点模型中成熟的张量网络方法（尤其是虚时演化）拓展到连续空间 [^r1]。具体地，他们构造了两张量 (可自然推广到 $K$ 张量) 的平移不变函数 MPS，用其表示无穷多耦合量子振子波函数在一组正交函数基 $\{ \phi_s(x) \}$ 下的展开系数；借助正则形式的 MPS，可自然定义连续空间波函数的纠缠谱与纠缠熵 (EE) [^r1]。

### 物理区与两体耦合下的临界标度律

仅含两体耦合时，iCQOs 的基态能量存在解析解；该解析解在耦合强度 $\gamma \le \gamma_c \equiv 0.5$ 时为实数，作者据此定义"物理区"与非物理区 [^r1]。在物理区内远离 $\gamma_c$ 处，关联长度 $\xi$ 与纠缠熵 $S$ 都随 MPS 虚键维 $\chi$ 收敛，提示基态具有有限的关联长度与有限的 EE，是典型的非临界、有能隙行为 [^r1]。

当 $\gamma$ 逼近分界点 $\gamma_c=0.5$ 时，$\xi$ 与 $S$ 都发散。在分界点上，数值给出如下标度行为 [^r1]：

- 关联长度满足代数标度：$\xi \sim \chi^{\kappa}$，拟合得 $\kappa \simeq 1.32$ [^r1]；
- 纠缠熵满足对数标度：$S \sim \eta \ln \chi$，拟合得 $\eta \simeq 0.23$ [^r1]。

这两类标度律此前已在临界量子格点模型中被识别为临界性特征，但在连续空间量子多体系统中尚未见报道 [^r1]。

### 中心荷与 $c=1$ 自由玻色 CFT

从上述标度系数可经 CFT 关系 $c = 6\eta/\kappa$ 估计中心荷，得 $c = 1 + O(10^{-2})$，随 $\chi$ 增大收敛至 1 [^r1]。这表明在物理区与非物理区的分界点上，iCQOs 基态可由 $c=1$ 自由玻色共形场论描述 [^r1]。

### 三体耦合破坏 CFT 描述

在加入三体耦合后，作者发现即使三体耦合强度 $\tilde{\gamma}$ 很弱，分界点附近 EE 对 $\chi$ 的对数标度律也不再成立，CFT 描述在分界线上被破坏 [^r1]。

### 对张量网络适用性的意义

作者明确指出，对连续空间波函数而言，无穷大函数 MPS 的能量误差可收敛到 $\varepsilon \sim O(10^{-5})$；即便在分界点处 $\xi$ 与 $S$ 发散，波函数仍能被 MPS 以类似于逼近临界格点系统的方式渐近逼近 [^r1]。由此，他们的工作为张量网络在热力学极限下表示连续空间多体波函数的高效性提供了数值证据，并提示了一条通过 EE 标度律研究连续空间量子多体系统纠缠与临界性的可行路径 [^r1]。

## Sources

[^r1]: raw-not-distributed/D049-2025-ifmps-hong-rui/paper.md#L9
