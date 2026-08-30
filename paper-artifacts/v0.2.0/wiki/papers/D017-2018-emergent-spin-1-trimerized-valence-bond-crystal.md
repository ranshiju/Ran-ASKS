---
title: "Emergent spin-1 trimerized valence bond crystal in the spin- <sup>1</sup> Heisenberg model on the star lattice"
type: paper-summary
sources:
  - "raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md"
source_type: official-doc
date: 2018
venue: "Phys. Rev. B 97, 075146 (2018)"
authors: ["Shi-Ju Ran", "Wei Li", "Shou-Shu Gong", "Andreas Weichselbaum", "Jan von Delft", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Emergent spin-1 trimerized valence bond crystal in the spin- <sup>1</sup> Heisenberg model on the star lattice

> **作者**：Shi-Ju Ran、Wei Li、Shou-Shu Gong、Andreas Weichselbaum、Jan von Delft、Gang Su | **发表**：Phys. Rev. B 97, 075146 (2018)
> **核心贡献**：在三角形内为反铁磁、跨三角形为铁磁耦合的自旋-1/2 海森堡星型格子模型中，发现由键上三重态涌现而成的自旋-1 三聚化价键晶体（TVBC），并系统刻画其零温与有限温性质。

## Navigation

本文研究星型格子（star lattice）上的自旋-1/2 海森堡模型，其中三角形内为反铁磁耦合、跨三角形为铁磁耦合。通过四套互补的张量网络与 DMRG 算法，作者在零温和有限温两个层面系统刻画了一种具有反演对称性破缺的三聚化价键晶体（TVBC），并发现键上自旋-1 自由度的涌现与多种标度行为。该工作把几何阻挫体系中的铁磁相互作用引入作为调控序参量、临界场与激发能隙的有效手段，为相关有机铁醋酸盐实验提供了可比较的理论预测。

## 研究方向定位

研究对象为自旋-1/2 海森堡星型格子模型在铁磁跨三角形耦合区的零温与有限温性质，核心问题是探究铁磁耦合是否会破坏三角形内反铁磁阻挫导致的无序态并催生何种量子序，方法为 SU(2) DMRG、含与不含 SU(2) 对称性的 simple update PEPS 以及有限温 NCD 四种数值算法的交叉验证。[^r2]

## Content

### 模型与背景：星型格子的几何阻挫

星型格子是一种阿基米德型格子（又称 (3-12) 格子、Fisher 格子、装饰六角格子、扩展 kagome 格子或三角-蜂窝格子），其所有格点等价。该格子兼具较高的几何阻挫和较低配位数（强涨落），并天然包含两种不等价键，这两点被视为其相比 kagome 海森堡反铁磁体可能蕴含更丰富物理的关键原因[^r1]。此前研究已在反铁磁跨三角形耦合 $J_e > 0$ 区找到 $J_e$-二聚体 VBC 和 $\sqrt{3}\times\sqrt{3}$ VBC 等候选相，但对铁磁 $J_e<0$ 区几乎未探索[^r1]。

本文关注如下哈密顿量：

$$H = J_e \sum_{\langle ij\rangle\in J_e} \mathbf{S}_i\cdot\mathbf{S}_j + J_t \sum_{\langle lm\rangle\in J_t} \mathbf{S}_l\cdot\mathbf{S}_m - h\sum_n \hat{S}^z_n,$$

其中 $J_e$（文中亦记 $J_e$，注意正负号约定）为跨三角形（键间）耦合、$J_t$ 为三角形内耦合、$h$ 为外磁场[^r3]。研究聚焦于 $J_t>0$（三角形内反铁磁）而 $J_e<0$（跨三角形铁磁）的情形。

### 数值方法：四套互补算法的交叉验证

为保证结论无偏，作者同时使用四套最先进算法：

1. **SU(2) DMRG**，作用于圆柱几何上的有限尺寸系统；
2. **带 SU(2) 非阿贝尔对称性的 simple update**，作用于热力学极限的无限格子；
3. **不带 SU(2) 对称性的 plain simple update PEPS**；
4. **网络收缩动力学 NCD**，用于有限温模拟[^r4]。

零态以投影纠缠对态（PEPS）表示：每个三角形上放置一个 $(d^3 \times D^3)$ 张量 $T(j)$，其物理指标对应三角形内三自旋、辅助指标承载纠缠[^r5]。在 $J_e=-1, J_t=1$ 下，plain PEPS 与 SU(2) PEPS 的基态能量 $E_0$ 均随保留态数 $D$（或对应的多重组数 $D^*$）收敛，且 SU(2) 版本对相近 $D$ 显著更高效，最终在大 $D$ 下两者趋于一致，验证了对称性的合法使用[^r6]。SU(2) PEPS 与 DMRG 在 $-10.0 \leq J_e \leq 3.0$ 全区间内 $E_0$ 高度吻合，且在 $J_e=0$ 处出现能量尖点，提示一阶相变[^r7]。

### 涌现的自旋-1 TVBC 与反演对称性破缺

主要发现是：当 $J_e<0$（跨三角形铁磁）时，铁磁耦合并未摧毁由三角形内反铁磁阻挫所产生的磁无序，反而触发了一个**完全带隙的、反演对称性破缺的三聚化价键晶体（TVBC）**：每个 $J_e$ 键上出现一个三重态，使得原本自旋-1/2 的体系在低能下涌现出有效自旋-1 自由度；与此同时，上三角（蓝）与下三角（黄）的等价性被打破[^r1][^r8]。这意味着几何阻挫自旋-1/2 系统中的小铁磁耦合足以稳定自旋-1 VBC 相。

### 磁场下的基态相图与磁化曲线

作者确定了 $J_e$–$h$ 基态相图，在所考察的 $J_e$ 范围内存在**六个由五个临界磁场分隔的相**。在反演对称性破缺相与反演对称性保持相的边界上，发现了**磁化强度在 $M_z \simeq 1/30$ 处的磁化尖点**（magnetization cusp），成为 TVBC 相的特征印记之一[^r9]。

### 普适的指数标度行为

一个突出的结构性结果是：当 $|J_e|$ 增大时，多个物理量表现出**对 $J_e$ 的普适指数依赖**——包括 TVBC 的"序参量"、分隔六相的五个临界场、以及由低温比热提取的激发能隙，均以 $\sim e^{-\alpha |J_e|}$ 的形式随 $J_e$ 标度[^r8][^r10]。这种标度无论是否施加磁场 $h$ 均成立，是该体系作为强关联阻挫模型的一个独特特征。

### 有限温比热与非磁能隙

为便于与实验对比，作者利用 NCD 计算了**比热的温度依赖**，并通过低温行为提取出**非磁能隙**。该能隙同样参与上述关于 $|J_e|$ 的指数标度。文中明确指出 NCD 与 simple update 在数学上源于对相应张量网络的秩-1 分解（最优 Bethe 近似），因此在带隙相内收敛快、精度高；但在临界点处如何优化张量以保持临界性仍属未解决问题，故临界信息的精确提取受限[^r4]。需要注意的是，相关结论建立在带隙 TVBC 相区的精度之上，而非临界点本身。

### 与 spin-1 kagome 模型的联系

作者将铁磁 $J_e<0$ 下 TVBC 的涌现，置于一个更宏观的提问之下：铁磁耦合是否能将自旋-1/2 星型模型绝热连续地接到自旋-1 kagome 模型。本文通过 TVBC 中键上三重态形成的自旋-1 自由度，为该方向提供了一个具体的微观实现。

## Sources

[^r1]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L31
[^r2]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L33
[^r3]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L44
[^r4]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L48
[^r5]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L54
[^r6]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L58
[^r7]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L67
[^r8]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L27
[^r9]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L143
[^r10]: raw-not-distributed/D017-2018-emergent-spin-1-trimerized-valence-bond-crystal/paper.md#L37
