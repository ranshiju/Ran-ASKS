---
title: "Fermionic algebraic quantum spin liquid in an octa-kagome frustrated antiferromagnet"
type: paper-summary
sources:
  - "raw-not-distributed/D012-2017-octa-kagome/paper.md"
source_type: official-doc
date: 2017
venue: "Phys. Rev. B 95, 075140 (2017)"
authors: ["Cheng Peng", "Shi-Ju Ran", "Tao Liu", "Xi Chen", "Gang Su"]
confidence: high
status: current
created: 2026-08-27
updated: 2026-08-27
related: []
---

# Fermionic algebraic quantum spin liquid in an octa-kagome frustrated antiferromagnet

> **作者**：Cheng Peng、Shi-Ju Ran、Tao Liu、Xi Chen、Gang Su | **发表**：Phys. Rev. B 95, 075140 (2017)
> **核心贡献**：通过张量网络数值模拟识别出无限八角—笼目晶格上自旋 \(1/2\) 海森堡反铁磁体的无隙费米型代数量子自旋液体，并确定其与价键固体相之间的量子相变。

## Navigation

本文研究无限八角—笼目晶格上的自旋 \(1/2\) 海森堡反铁磁体，重点考察交换耦合比 \(J_d/J_t\) 变化时的基态关联、磁化平台和有限温度响应。作者结合无限投影纠缠对态的簇更新、完全更新和网络收缩动力学方法，并通过多种算法交叉验证结果。计算表明，系统在临界耦合比 \(0.6\) 以下处于有自旋能隙的价键固体态，在该值以上进入无隙量子自旋液体；各向同性点 \(J_d/J_t=1\) 具有费米型代数量子自旋液体的特征。

## 研究方向定位

本文研究八角—笼目晶格上自旋 \(1/2\) 海森堡反铁磁体在几何阻挫下形成的量子自旋液体及量子相变，并采用张量网络方法分析其基态关联、磁化平台和有限温度性质。[^r1]

## Content

### 晶格、模型与数值方法

八角—笼目晶格可视为沿一个方向拉伸角共享三角形的笼目晶格，其结构也由角共享和边共享的八边形组成。模型包含三角形内部最近邻交换耦合 \(J_t\) 和相邻三角形之间二聚体内的最近邻交换耦合 \(J_d\)，并以 \(J_t=1\) 作为能量尺度。[^r2]

研究采用三种张量网络算法：无限投影纠缠对态的簇更新、完全更新，以及网络收缩动力学。前两者属于收缩—截断方案，后者属于编码方案；三种方法得到的结果相互一致。[^r3]

### 基态磁性与相变

系统的基态局域磁化强度为零，表明磁有序被角共享三角形产生的强几何阻挫和量子涨落破坏。计算还发现一个临界耦合比 \(J_d/J_t=0.6\)，将两种不同性质的区域分开。[^r4]

当 \(0<J_d/J_t<0.6\) 时，系统处于价键固体态，并出现明显的零磁化平台，表明自旋激发具有能隙。当 \(J_d/J_t>0.6\) 时，系统表现为无隙激发。[^r5]

### 各向同性点的关联与热力学性质

在 \(J_d/J_t=1\) 处，二聚体—二聚体关联随距离呈幂律衰减，而自旋—自旋和手性—手性关联函数呈指数衰减。该组合表明，无序二聚体涨落具有长程代数关联，而自旋和手性通道保持有隙。[^r6]

该点的基态能量由无限键维数外推得到为每格点 \(-0.4524\)，低于文献所报道的笼目晶格值 \(-0.4386(5)\)。这一比较针对笼目晶格基态能量，并非一般意义上的能量排序结论。[^r7]

有限温度计算显示，低温比热随温度线性变化，即 \(C\propto T\)；当 \(T\to0\) 时，磁化率趋于有限常数。得到的 Wilson 比为 \(0.72\)，接近 \(1\)。这些结果与无隙激发以及费米型代数量子自旋液体相符。[^r8]

### 磁化平台

在磁场作用下，基态具有一个 \(1/2\) 磁化平台，其自旋构型为上—下—上—上。零磁化平台与 \(1/2\) 磁化平台分别对应相变前有能隙价键固体态的磁化响应和相变后无隙量子自旋液体区域的基态磁化特征。[^r9]

## Sources

[^r1]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L25
[^r2]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L29
[^r3]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L48
[^r4]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L15
[^r5]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L15
[^r6]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L15
[^r7]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L213
[^r8]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L15
[^r9]: raw-not-distributed/D012-2017-octa-kagome/paper.md#L15
