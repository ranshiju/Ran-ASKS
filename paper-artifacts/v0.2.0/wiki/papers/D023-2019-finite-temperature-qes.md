---
title: "Efficient quantum simulation for thermodynamics of infinite-size many-body systems in arbitrary dimensions"
type: paper-summary
sources:
  - "raw-not-distributed/D023-2019-finite-temperature-qes/paper.md"
source_type: official-doc
date: 2019
venue: "Phys. Rev. B 99, 205132 (2019)"
authors: ["Shi-Ju Ran", "Bin Xi", "Cheng Peng", "Gang Su", "Maciej Lewenstein"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Efficient quantum simulation for thermodynamics of infinite-size many-body systems in arbitrary dimensions

> **作者**：Shi-Ju Ran、Bin Xi、Cheng Peng、Gang Su、Maciej Lewenstein | **发表**：Phys. Rev. B 99, 205132 (2019)
> **核心贡献**：提出"量子纠缠模拟器"（QES），仅用 O(10) 格点的少体模型即可模拟一维、二维与三维无限大量子晶格模型在有限温度下的热力学[^r1]。

## Navigation

本文针对无限大量子多体晶格模型在有限温度下的热力学难以可靠模拟这一难题，提出通过在少体模型边界引入"纠缠浴"（entanglement bath）格点并优化其与体内（bulk）格点的相互作用，使该少体模型在温度无关的有效哈密顿量下，其体内约化密度矩阵重现无限大正则系综的结果[^r1]。论文在具有精确解的一维 XY 链、二维蜂窝晶格海森堡模型（含 Néel–顺磁相变与低温能隙激发）、三维立方晶格海森堡模型以及二维拓扑系统上验证了 QES 的精度[^r1][^r2]。作者同时区分了 QES 自身带来的关联误差、浴关联误差与结构误差（仅二维/三维），以及来自线性张量重正化群（LTRG）算法的 Trotter 截断误差，并讨论了在冷原子等平台上以 O(10) 格点实现 QES 的实验前景[^r2][^r3]。

## 研究方向定位

本工作面向一维、二维与三维无限大量子晶格模型在有限温度下的热力学模拟，核心问题是能否仅用 O(10) 格点的温度无关少体哈密顿量（其边界相互作用由张量网络方法优化）来再现无限大正则系综的约化密度矩阵，从而以小规模可控系统模拟强关联多体现象[^r1]。

## Content

### 量子纠缠模拟器（QES）的构造思想

QES 的核心是在一个仅含体内物理格点的小型系统中，于其边界附加若干浴格点；体内—浴格点之间的相互作用 $\hat{\mathcal{H}}^{[i,n]}$ 由张量网络方法（如无穷 DMRG、Bethe 晶格上的变种 iDMRG）针对基态进行优化，从而模拟无限大系统中体内与环境之间的纠缠[^r1][^r4]。由此得到一个温度无关的少体有效哈密顿量
$$
\hat{H}_{\mathrm{QES}}=\sum_{\langle i,j\rangle\in\text{bulk}}\hat{H}^{[i,j]}+\sum_{\langle i\in\text{bulk},\,n\in\text{bath}\rangle}\hat{\mathcal{H}}^{[i,n]},
$$
其中 $\hat{H}^{[i,j]}$ 保留原模型的体内相互作用，$\hat{\mathcal{H}}^{[i,n]}$ 只取两体最近邻形式[^r1]。对 QES 整体求密度矩阵 $\hat{\rho}$（基态模拟取 $\hat{\rho}=|\Psi\rangle\langle\Psi|$，有限温度取 $\hat{\rho}=e^{-\beta\hat{\mathcal{H}}}$），再对浴自由度求迹得到的体内约化密度矩阵 $\hat{\rho}_R$，被设计为模仿无限大正则系综下体内子系统的约化密度矩阵[^r1]。

### 一维 XY 链基准测试

以一维无穷大自旋-1/2 XY 链（哈密顿量为相邻 $S_x S_x+S_y S_y$ 之和）作为基准，其热力学存在精确解[^r5]。取浴维度 $D=2$（即浴格点有效为自旋-1/2），QES 的能量误差 $|E-E_{\mathrm{exact}}|$ 在同一尺寸下比无浴的常规有限链高约一个量级；六个自旋构成的 QES 表现甚至略优于 20 个自旋的常规链[^r5]。当固定体内为四个格点、浴为两个格点时，把 $D$ 增大到 $D\ge 8$ 后误差收敛到 $\sim O(10^{-4})$，剩余部分主要来自 LTRG 的 Trotter 误差（量级 $O(\tau^2)$，随 $1/\tau$ 线性增加计算量）[^r5]。

### 多维强关联模型的有限温度结果

在二维无穷大蜂窝晶格海森堡模型上，作者构造了 (14+12) 与 (18+12) 等 QES（即体内 14 或 18 个格点配 12 个浴格点的方案），与连续时间世界线量子蒙特卡罗（worm update，无 Trotter 误差）的有限尺寸外推结果对照，QES 能重现 Néel–顺磁相变与低温下的能隙激发[^r2][^r6]。在三维立方晶格海森堡模型以及二维拓扑模型上也观察到由 QES 准确再现的相变与温度驱动的渡越（crossover）行为[^r1]。

### 误差来源与计算代价

QES 涉及多类误差：浴关联误差源于有限浴维度 $D$ 截断长程关联，温度关联误差源自 $\hat{\mathcal{H}}^{[i,n]}$ 的温度无关假设（在 $\beta$ 与动力学关联长度可比时显现，表现为渡越点附近的误差峰），仅在二维与三维出现的结构误差则因以 Bethe 晶格近似代替原晶格环境而在临界点附近达到最大[^r5][^r2]。LTRG 数值求解中的截断误差与 Trotter 误差属于算法而非 QES 本身：1D 取 $\chi=400\sim600$ 时截断误差可忽略；2D 所需 $\chi$ 随 $D$ 与 QES 尺寸增大，截断误差在低温成为主要误差[^r2]。总体而言，张量网络优化的计算复杂度随晶格配位数多项式增长：对一维链、二维蜂窝晶格、三维立方晶格分别约为 $O(D^4)$、$O(D^6)$、$O(D^{12})$，可通过选择合适的缩并顺序进一步降低[^r4]。

### 意义、关联方法与未来方向

作者把 QES 看作"纠缠可模拟"（entanglement-simulatable）的一类系统：只要关联与结构误差可控，QES 可在平衡态下准确模拟目标无穷大系统；非平衡情形在演化时间远小于 QES 所捕获的动力学关联长度时亦可信赖[^r3]。在算法视角下，QES 借鉴了数值重正化群（特别是 DMRG）中构造有效哈密顿量的思想，但将适用维度从一维拓展到二维与三维，并把温度从零温推广到有限温度，且可推广到玻色子与费米子晶格模型[^r3]。作者还指出 QES 与动力学平均场（DMFT）/密度矩阵嵌入理论（DMET）共享"把复杂系统约化为简化子系统"的理念，但 QES 通过张量网络方法能更好地刻画强纠缠效应，未来或可将 QES 与 DMFT/DMET 进一步结合以提升相互作用电子体系的模拟效率[^r3]。提高精度的一条可能路径是让 $\hat{\mathcal{H}}^{[i,n]}$ 随温度变化（例如使用虚时扫描算法），代价是 QES 不再具有式 (1) 形式的统一温度无关哈密顿量；另一条路径是无 Trotter 误差的级数展开算法，或在二维上把有限尺寸投影纠缠对态（PEPS）推广到热态以更贴合二维几何[^r3]。

## Sources

[^r1]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L19
[^r2]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L62
[^r3]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L156
[^r4]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L136
[^r5]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L65
[^r6]: raw-not-distributed/D023-2019-finite-temperature-qes/paper.md#L69
