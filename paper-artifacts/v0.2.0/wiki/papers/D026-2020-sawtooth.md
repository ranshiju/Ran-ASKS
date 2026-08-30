---
title: "Reentrance of the topological phase in a spin-1 frustrated Heisenberg chain"
type: paper-summary
sources:
  - "raw-not-distributed/D026-2020-sawtooth/paper.md"
source_type: official-doc
date: 2020
venue: "Phys. Rev. B 101, 045133 (2020)"
authors: ["Yuan Yang", "Shi-Ju Ran", "Xi Chen", "Zheng-Zhi Sun", "Shou-Shu Gong", "Zhengchuan Wang", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Reentrance of the topological phase in a spin-1 frustrated Heisenberg chain

> **作者**：Yuan Yang、Shi-Ju Ran、Xi Chen、Zheng-Zhi Sun、Shou-Shu Gong、Zhengchuan Wang、Gang Su | **发表**：Phys. Rev. B 101, 045133 (2020)
> **核心贡献**：在自旋-1 锯齿链（sawtooth chain）反铁磁海森堡模型中通过磁场诱导出"部分 Haldane 相"，首次把零温再入现象从常规有序相扩展到拓扑相。

## Navigation

本文研究自旋-1 反铁磁海森堡模型在锯齿链上的零温基态相图，通过密度矩阵重整化群（DMRG）方法在磁场下系统扫描参数 $\theta$，发现磁场能够再次把已经进入拓扑平庸相的系统驱回一种"部分 Haldane 相"。该结果把统计物理中由熵驱动的再入现象推广到具有拓扑序的量子体系，并以锯齿链上的键心反演对称性作为该部分拓扑相的保护机制。

## 研究方向定位

研究对象为锯齿链上的自旋-1 反铁磁 Heisenberg 模型，核心问题为磁场能否重新诱导出拓扑 Haldane 相，使用密度矩阵重整化群在 $\theta$–$h_z$ 参数空间内扫描基态相图[^r1]。

## Content

### 模型与锯齿链结构

锯齿链是一维结构，可由两套子格 A 和 B 描述，自然界中存在于 CuCl(OH) 等材料[^r2]。模型哈密顿量定义为

$$
H = J_1 \sum_i \mathbf{S}_i \cdot \mathbf{S}_{i+1} + J_2 \sum_{\text{odd } i} \mathbf{S}_i \cdot \mathbf{S}_{i+2} - h_z \sum_i S_i^z,
$$

其中 $J_1 = \cos(\frac{\pi}{3}\theta)$、$J_2 = \sin(\frac{\pi}{3}\theta)$，$0 \le \theta \le 1$，磁场沿 $z$ 方向[^r2]。两条交换键 $J_1$、$J_2$ 分别对应锯齿上不同方向的连接。

### 零温再入：Haldane → 平庸 → 部分 Haldane

对于 $\theta \simeq 0.62$ 的参数附近，作者在 $\theta$–$h_z$ 相图中观察到随磁场 $h_z$ 增大出现的相序列：Haldane 相 → 拓扑平庸磁相 → 部分 Haldane 相[^r2]。即磁场超过 Haldane 能隙后并未直接破坏拓扑序，反而重新诱导出一片拓扑非平庸的区域，这是一种零温下的再入行为。

### 部分 Haldane 相的对称性保护

部分 Haldane 相中，子格 A 处于 Haldane 态，子格 B 处于铁磁有序的拓扑平庸态[^r1]。在 $h_z = 0$ 时，整个系统的 Haldane 相可由时间反演、$D_2$ 或键心反演对称性之一保护[^r2]。在磁场下，虽然全系统的时间反演对称性被破坏，但子格 A 上仍保留键心反演对称性，使得该子格能够再次进入由该对称性保护的 Haldane 相[^r2]。

### 机制：量子涨落与几何阻挫驱动

与经典体系中由热涨落与热熵驱动的再入不同，本文中的零温再入是几何阻挫与量子（零点）涨落共同作用的结果[^r1]。论文明确把这一机制总结为：再入要求系统中存在"部分无序的相与有序相并存"这种必要条件，而在量子情形下，无序子系统是具有短程铁磁关联的态，而非经典顺磁态[^r1]。

### 与更广泛框架的衔接

论文把这一结果解释为把"再入现象"从常规有序-无序体系扩展到拓扑体系的一次尝试：将破坏拓扑相的项（这里是磁场）反向加入，反而在子格尺度上恢复一种拓扑"自举 (bootstrap)"[^r1]。作者指出，键心反演属于空间晶格对称性，因此部分 Haldane 相可被视作一种晶体的对称性保护拓扑相 (crystalline SPT)[^r1]，并预期在二维及更高维系统中可能发现更多类似的拓扑再入现象[^r1]。

## Sources

[^r1]: raw-not-distributed/D026-2020-sawtooth/paper.md#L104
[^r2]: raw-not-distributed/D026-2020-sawtooth/paper.md#L21
