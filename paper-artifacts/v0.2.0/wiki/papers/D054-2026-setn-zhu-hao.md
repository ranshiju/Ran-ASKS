---
title: "Statistics-encoded tensor network approach in disordered quantum many-body spin chains"
type: paper-summary
sources:
  - "raw-not-distributed/D054-2026-setn-zhu-hao/paper.md"
source_type: official-doc
date: 2026
venue: ""
authors: ["Hao Zhu", "Ding-Zu Wang", "Shi-Ju Ran", "Guo-Feng Zhang"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Statistics-encoded tensor network approach in disordered quantum many-body spin chains

> **作者**：Hao Zhu、Ding-Zu Wang、Shi-Ju Ran、Guo-Feng Zhang | **发表**：2026
> **核心贡献**：提出 statistics-encoded tensor network（SeTN）方法，通过将无序编码进辅助层并对无序独立做平均，恢复平移不变性，使含连续无序分布的一般时间无关哈密顿量也能在热力学极限下用紧致 MPO 转移矩阵数值求解，并导出无序编码效率的普适判据 n ≫ α²t²。

## Navigation

本文提出 SeTN 用于模拟含无序的量子多体动力学[^r15]。核心思路是把每个站点独立同分布的无序变量编码进辅助层，对无序单独做平均，从而恢复空间平移不变性，使无序平均后的双层张量网络可写成紧致 MPO 转移矩阵[^r21]。作者从奇异值衰减得到判据 n ≫ α²t²，刻画 Trotter 步数、无序强度、演化时长三者之间的标度关系[^r21]。作为示例，他们用 SeTN 在无序横向场 Ising 模型上计算谱形因子，发现在可数值触及的时间窗口内其标度由转移矩阵的单根主特征值主导[^r27]。

## 研究方向定位

针对含连续局域无序的时间无关哈密顿量多体自旋链，SeTN 把无序编码为辅助层并单独做平均，恢复平移不变性以建立可数值处理的无序平均 MPO 转移矩阵[^r21]。

## Content

### 背景与动机

量子多体系统中的局域无序出现在量子混沌、多体局域化、量子自旋玻璃与临界现象等多个问题里；近年来人们对随机矩阵理论所预测的无序系统混沌动力学兴趣日增[^r23]。在解析路线上，随机量子线路与对偶幺正模型因 Haar 平均或自对偶性可获得精确的转移矩阵处理，但这些方法无法直接推广到自对偶性缺失的一般时间无关哈密顿量，且转移矩阵维度指数增长[^r25]。数值上，严格对角化受限于小系统；常规张量网络与强无序重整化各自处理每个无序实现，更适合深 MBL 区[^r25]。量子并行方法把取离散值的无序嵌入辅助系统并行演化，但在连续无序分布下需要无穷维辅助空间[^r25]。

### SeTN 方法

SeTN 考虑哈密顿量 ℋ[h] = H + Σᵢ hᵢHᵢ，其中 hᵢ 独立同分布；时间演化经一阶 Trotter 分解给出 U(τ) = e^(−iτH) e^(−iτΣᵢ hᵢHᵢ) + O(τ²)[^r31]。对最近邻相互作用 H = Σᵢ H_{i,i+1} 和 Hᵢ = σᵢᶻ 的情形，先用 SVD 或谱分解把两体幺正门拆为左/右两张局部张量，再用三阶 Kronecker 张量与承载无序的向量 v_{l}=e^(−iτhᵢl) 把对角无序项"剥出"，得到局部张量 W[^r31]。

对无序平均后，诸如 ⟨⟨𝒪(t)⟩⟩ = ⟨Tr[𝒪 U(t)ρ(0)U†(t)]⟩ 与谱形因子 K(t) = ⟨Tr[U(t)⊗U†(t)]⟩ 等量自然形成前后演化收缩的双层张量网络；无序效应被编码到"统计层"（辅助层），平均后所有空间站点等价，从而可定义平移不变的 MPO 转移矩阵 T[^r33]。

### 无序编码的普适判据

在把整个无序分布连续积分时，作者对每个 Se-Layer 的 MPO 做 SVD 并追踪奇异值的衰减速度，由此得到

n ≫ α²t²

这一普适判据，其中 n 为 Trotter 时间步数、α 为无序强度、t 为演化时长[^r35]。该条件是 SeTN 在弱无序（通常对应混沌区）效率最高的关键所在，也是该方法在弱无序区——传统张量网络难以处理——可行的原因[^r35]。

### 计算实现

由于对分布的精确积分不可行，作者采用 M 个无序实现的有限样本近似，并按两步流程压缩 MPO[^r35]：

1. 每个 Se-Layer 实现都是键维 1 的 MPO；通过 MPO 加法对 M 个实现求和得到键维 M 的 MPO[^r35]。
2. 再以 SVD 逐站点迭代压缩：在第 n=1 步截断低于阈值的奇异值 S^(1)，把截断后的张量与 A^(2) 收缩后再次分解，逐层递推至最后一层[^r35]。

由于每步只涉及局部操作且 A^(n) 由各实现 MPO 加法相同地构造，整体内存需求降为 O(M)，相对朴素的 O(nM²) 有显著降低，从而可在实践中取 M≫1[^r35]。

### 对无序 TFIM 的应用与发现

作为示例，SeTN 被用于研究无序横向场 Ising 模型在其混沌区的谱形因子[^r27]。在可数值触及的时间窗口内，谱形因子由转移矩阵的单根、非简并主特征值主导；作者将此解读为"前 RMT 暂态"——即谱形因子最终趋向 RMT 行为之前的过渡阶段[^r27]。这与踢 Ising 模型等 Floquet 模型中混沌谱统计与特征值简并从一开始就出现的情形形成对比[^r27]。作者据此猜想：从单主特征值主导向 RMT 行为的过渡与转移矩阵主特征值之间逐渐出现的近简并相关，而阐明这种近简并如何发展、并如何控制 Thouless 时间的出现与系统尺度依赖，是有待进一步研究的重要问题[^r27]。

### 适用范围与展望

SeTN 适用于一切具有多层张量网络表示的观测量，包括 Rényi 熵、时序外关联函数（OTOC）、自旋玻璃序参量等，为研究无序对扩散、流体力学等动力学现象的影响提供了通用平台[^r27]。SeTN 也可作为局域平均方案与 Feynman 轨道、影响泛函、纠缠势垒等概念之间的桥梁[^r27]。总体上，SeTN 为探索静态多体系统中局域性、幺正性、无序与混沌之间的相互作用提供了一个统一框架，补充了无序平均动力学中的涌现对称性及热系综的平移不变 MPO 描述等近期进展[^r27]。

## Sources

[^r15]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L15
[^r21]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L21
[^r23]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L23
[^r25]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L25
[^r27]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L27
[^r31]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L31
[^r33]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L33
[^r35]: raw-not-distributed/D054-2026-setn-zhu-hao/paper.md#L35
