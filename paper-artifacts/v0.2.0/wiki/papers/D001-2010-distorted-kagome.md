---
title: "Phase transitions and thermodynamics of the two-dimensional Ising model on a distorted kagome lattice"
type: paper-summary
sources:
  - "raw-not-distributed/D001-2010-distorted-kagome/paper.md"
source_type: official-doc
date: 2010
venue: "Phys. Rev. B 82, 134434 (2010)"
authors: ["Wei Li", "Shou-Shu Gong", "Yang Zhao", "Shi-Ju Ran", "Song Gao", "Gang Su"]
confidence: high
status: current
created: 2026-08-27
updated: 2026-08-27
related: []
---

# Phase transitions and thermodynamics of the two-dimensional Ising model on a distorted kagome lattice

> **作者**：Wei Li、Shou-Shu Gong、Yang Zhao、Shi-Ju Ran、Song Gao、Gang Su | **发表**：Phys. Rev. B 82, 134434 (2010)
> **核心贡献**：利用精确解与张量重正化群（TRG）方法，系统研究二维 Ising 模型在扭曲 kagome（DK）格点上的相图与磁热力学性质，并拟合 Co(N₃)₂(bpg)·DMF_{4/3} 的磁化率实验数据，提取出耦合常数 J=22 K、J'=33 K。

## Navigation

本文针对具有两条不同键耦合 J 与 J' 的扭曲 kagome 格点，建立二维 Ising 模型的精确解（零场）与 TRG 数值方法（有场）相结合的研究框架。在零场下识别出铁磁、铁磁亚铁磁与顺磁三相及相应的二级相变；在外磁场下发现 m=1/3 磁化平台，并预测 Co(N₃)₂(bpg)·DMF_{4/3} 在约 T=20 K 处可能发生相变。

## 研究方向定位

研究二维 Ising 模型在扭曲 kagome 格点上的相变与热力学行为，结合精确解与 TRG 数值方法，并应用于分子磁体 Co(N₃)₂(bpg)·DMF_{4/3} 的磁化率拟合[^r3]。

## Content

### 模型与格点设置

扭曲 kagome（DK）格点由结构畸变产生两条不同的交换键 J 与 J'，可视为一种空间键各向异性的自旋格点；当自旋-1/2 的 Co²⁺ 离子在低温下通过各向异性 g 因子（g∥≠0，g⊥≈0）形成有效 Ising 型耦合时，DK 格点上的二维 Ising 模型成为合适的描述工具[^r1]。文章对 h=0 情形给出精确解，对 h≠0 情形采用 TRG 数值方法。

### TRG 算法实现

每个三角形被替换为一个三阶张量 T^{A/B}_{s₁,s₂,s₃}=exp[-ε_Δ(s₁,s₂,s₃)/T]，其中三角形能量含 J' s₁s₂ + J' s₁s₃ + J s₂s₃ - (1/2)h(s₁+s₂+s₃)；这些张量构成一个蜂窝张量网络，配分函数即表示为张量迹 tTr(T^A T^B…) [^r4]。计算中粗粒化迭代次数取 20（对应 ~3²² 个格点，近似热力学极限），初始键维度 D=2，截断维度 D_c 取至 18 并检验收敛；物理量如磁化强度通过引入杂质张量 T^{Im}_{s₁,s₂,s₃}=((s₁+s₂+s₃)/3)·exp[-ε_Δ/T] 替换网络中的一个张量得到[^r5]。

### 零场相图与精确解对比

零场下系统呈现三个相：铁磁相、铁亚铁磁相与顺磁相；相变均为二级相变，且由磁化强度的临界指数 β=1/8 落在二维 Ising 普适类中；TRG 得到的零场比热与精确解高度一致，证明 TRG 在处理二维 Ising 模型时的精度与可靠性[^r6]。在强阻挫区域，顺磁相可在 T=0 稳定存在。

### 磁场下的磁化与比热

在 TRG 计算的磁化曲线中，于若干耦合参数下观察到 m=1/3 平台，且平台宽度随 J、J' 变化；零温 J'-h 相图揭示了不同基态相之间的边界。当外磁场打开后，比热不再呈现发散峰，意味着 h≠0 时不存在相变；场致比热峰劈裂出现在接近临界场处[^r7]。

### 与 Co(N₃)₂(bpg)·DMF_{4/3} 实验的拟合

在 Co(N₃)₂(bpg)·DMF_{4/3} 中，Co²⁺ 离子形成自旋-1/2 的扭曲 kagome 层；其实验磁化率在 T→0 时趋近有限值而非零，与各向同性 Heisenberg 反铁磁系统不一致，提示 Ising 型耦合可能主导[^r8]。以 TRG 结果在低温区拟合磁化率数据，得到交换耦合常数 J=22 K 与 J'=33 K，对应 J>0、J'/J=1.5 下的铁亚铁磁相，从而解释低温磁化率趋于有限值的行为；高温区用 Curie-Weiss 律 χ=C'/(T+θ) 拟合给出 θ≈161.3 K，与实验估计 θ≈165.8 K 接近[^r9]。在高温极限下，实验磁化率与 Ising 模型结果之比约为常数 R≈5.4，可能反映 T>20 K 时 Co²⁺ 有效自旋偏离 1/2 或 XY 耦合介入；以拟合得到的耦合常数计算比热，预测在 T≈20 K 处存在发散峰，提示该化合物可能在低温下经历相变[^r9]。

## Sources

[^r1]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L23
[^r3]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L21
[^r4]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L87
[^r5]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L101
[^r6]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L15
[^r7]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L158
[^r8]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L142
[^r9]: raw-not-distributed/D001-2010-distorted-kagome/paper.md#L142
