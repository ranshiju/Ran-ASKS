---
title: "Kosterlitz-Thouless phase transition and re-entrance in an anisotropic three-state Potts model on the generalized kagome lattice"
type: paper-summary
sources:
  - "raw-not-distributed/D007-2013-kt-phase/paper.md"
source_type: official-doc
date: 2013
venue: "Phys. Rev. E 87, 032151 (2013)"
authors: ["Yang Zhao", "Wei Li", "Bin Xi", "Zhe Zhang", "Xin Yan", "Shi-Ju Ran", "Tao Liu", "Gang Su"]
confidence: high
status: current
created: 2026-08-27
updated: 2026-08-27
related: []
---

# Kosterlitz-Thouless phase transition and re-entrance in an anisotropic three-state Potts model on the generalized kagome lattice

> **作者**：Yang Zhao、Wei Li、Bin Xi、Zhe Zhang、Xin Yan、Shi-Ju Ran、Tao Liu、Gang Su | **发表**：Phys. Rev. E 87, 032151 (2013)
> **核心贡献**：利用线性化张量重正化群（LTRG）方法研究广义 kagome 格子上各向异性三态 Potts 模型，发现再入现象不仅可出现在部分有序相之下，也可出现在属于 Kosterlitz-Thouless（KT）普适类的无局域序参量相之下。

## Navigation

本文研究广义 kagome 格子上各向异性三态 Potts 模型的相图，重点考察再入（re-entrant）现象出现的温度区间及其与次近邻耦合比 $\alpha = J_2/|J_1|$、$\beta = J_3/|J_1|$ 的关系。作者利用线性化张量重正化群（LTRG）方法计算比热、磁化率、关联长度与准纠缠熵（quasientanglement entropy），据此确定相界并区分铁磁、顺磁与浮游相（floating phase）。文中明确指出：在 $\alpha<0$ 与 $0<\alpha<1$ 两种参数区间内各存在一小块再入区域；再入相下方出现的相属于 KT 普适类而非传统的有序相；并观察到再入现象在 $q>3$ 时消失。

## 研究方向定位

研究对象为广义 kagome 格子上各向异性三态 Potts 模型的相结构与再入现象，核心问题是判断再入相是否可出现在无局域序参量的相之下以及该相是否属于 Kosterlitz-Thouless 普适类，方法是利用线性化张量重正化群计算热力学量与准纠缠熵[^r1]。

## Content

### 模型与耦合设定

模型定义在广义 kagome 格子上，含有三种耦合：对角的 $J_1$ 取为铁磁（即 $J_1=-1$），垂直方向的 $J_2$ 与水平方向的 $J_3$ 可取铁磁或反铁磁[^r2]。Ising 情形 ($q=2$) 下基态中存在两种部分有序构型 A 与 B：A 对应 $J_3<0$（F）与 $J_2>0$（AF），B 对应 $J_2,J_3>0$，两构型中心格点自旋均处于自由态，因而整体呈现部分有序或部分无序[^r2]。本文研究的是 $J_2<0$（反铁磁）、$J_3>0$（铁磁）的混合情形，三态 Potts 模型无精确解，需借助数值方法。

### 数值方法与可观测量

作者采用线性化张量重正化群（LTRG）方法处理无精确解的混合各向异性三态 Potts 模型[^r1]。具体观测量包括比热、磁化率、关联长度与准纠缠熵；通过这些量的奇异性来标定相变温度，并据此在 $\beta$–$T$ 平面上绘制相图[^r1]。

### 再入现象与 KT 相变

文中报告了反常的再入现象：在温度轴上，部分有序相之下可出现再入相；并且再入现象不仅出现在常见的部分有序相之下，也可出现于一个不具局域序参量的相之下[^r1]。对该无序相的关联长度行为进行分析后，作者判定其相变属于 Kosterlitz-Thouless（KT）普适类，且 $\beta>1$ 与 $\beta<1$ 时关联长度的行为存在明显差异[^r3]。再入区域的分布对耦合比 $\alpha=J_2/|J_1|$ 与 $\beta=J_3/|J_1|$ 依赖强烈[^r1]。

### 相图与浮游相

文中给出了不同 $\alpha$ 取值下温度–$\beta$ 平面内的相图，分别覆盖 $\alpha<0$、$0<\alpha<1$ 与 $\alpha>1$ 三种情况[^r1]。在 $\alpha<0$ 与 $0<\alpha<1$ 两种情形中各观察到一小块再入区域[^r3]。对 $\alpha>1$ 的相图，作者识别出顺磁相 P 与两个浮游相 X、Y：浮游相不具有任何局域序参量[^r4]。这些浮游相的出现与广义 kagome Ising 模型中存在的部分有序相形成对比，被归因于三态情形下的强热涨落[^r3]。

### $q$ 自由度与再入的关系

文中明确指出，再入现象在 $q>3$ 时消失[^r2]。结合已有研究——例如堆积 domino 模型中 $1\leq q\leq 4$（$q=2$ 除外）范围内均可出现再入[^r2]——作者强调再入现象与格点自旋自由度 $q$ 的取值密切相关。

### 关于再入机理的评注

作者总结指出，2D 经典格子上再入现象的出现主要由阻挫与格子的几何结构所决定，并与局域自旋自由度 $q$ 紧密相关；同时，部分有序态在基态中的存在未必是再入现象出现的必要条件[^r3]。

## Sources

[^r1]: raw-not-distributed/D007-2013-kt-phase/paper.md#L11
[^r2]: raw-not-distributed/D007-2013-kt-phase/paper.md#L25
[^r3]: raw-not-distributed/D007-2013-kt-phase/paper.md#L139
[^r4]: raw-not-distributed/D007-2013-kt-phase/paper.md#L141
