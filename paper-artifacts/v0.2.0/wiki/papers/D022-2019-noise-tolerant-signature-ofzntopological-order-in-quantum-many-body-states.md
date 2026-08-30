---
title: "Noise-tolerant signature of Z<sub>N</sub> topological order in quantum many-body states"
type: paper-summary
sources:
  - "raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md"
source_type: official-doc
date: 2019
venue: "Phys. Rev. B 99, 195101 (2019)"
authors: ["Xi Chen", "Shi-Ju Ran", "Shuo Yang", "Maciej Lewenstein", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Noise-tolerant signature of Z<sub>N</sub> topological order in quantum many-body states

> **作者**：Xi Chen、Shi-Ju Ran、Shuo Yang、Maciej Lewenstein、Gang Su | **发表**：Phys. Rev. B 99, 195101 (2019)
> **核心贡献**：提出"ring degeneracy (RD)"——张量网络自洽方程固定点解的简并度，用以在纯噪声下鲁棒地探测平凡与内禀拓扑序。

## Navigation

本文针对量子多体态中的 Z<sub>N</sub> 拓扑序检测难题，提出基于张量网络自洽方程固定点简并度的"环简并度 (ring degeneracy)"指标。[^r1] 该指标在二维统计 Ising 模型、自旋-1 Haldane 链、Z<sub>N</sub> 拓扑态以及 kagomé 格点共振价键态上得到验证，并给出 Z<sub>N</sub> 拓扑序的统一公式 $\mathcal{D} = (N+1)/2 + d$（$N$ 为奇时 $d=0$，$N$ 为偶时 $d=1/2$）。[^r1] 与拓扑纠缠熵、拓扑 Rényi 相比，RD 的关键优势在于其对不破坏张量对称性的纯噪声具有容忍能力，可稳定到与张量网络边界理论能隙同量级的噪声强度。[^r2]

## 研究方向定位

研究对象为二维量子多体态与张量网络中的拓扑序检测问题，核心问题是寻找一种在纯噪声下仍能区分平凡与内禀 Z<sub>N</sub> 拓扑序的鲁棒签名，方法是在投影纠缠对态 (PEPS) 张量网络上构造自洽本征方程并考察其固定点（环张量）解的简并度（[^r1]）。

## Content

### 背景：拓扑序检测的计算瓶颈与对噪声的脆弱性

拓扑态无法用传统 Landau-Ginzburg 序参量描述，其简并基态由大能隙保护，局域扰动在低于能隙时无法诱导错误，因此被视为容错量子器件的有力候选。[^r3] 已有的检测方法包括拓扑纠缠熵 (TEE)、拓扑 Rényi 、拓扑基态简并度、ribbon operators，以及对称性保护拓扑 (SPT) 态中由张量纠缠过滤重正化给出的不动点张量等。[^r3]

然而这些方法面临两重困难。其一，二维及以上系统纠缠计算复杂度极高，使得 TEE、拓扑 Rényi 熵在高维中应用稀疏。[^r3] 其二，Chen 等人已指出拓扑 Rényi 熵仅在保持 Z<sub>2</sub> 对称性的张量变动下稳定，无法用于检测数值模拟得到的拓扑态（其中必然存在数值噪声/误差），也难以描述对称性破缺邻域内仍可能继承部分拓扑性的态。[^r3]

### Ring degeneracy 的定义与构造

作者定义环张量 $R$ 为 PEPS 内积张量 $T$ 所满足的自洽本征方程的固定点解（固定点 contractor），RD 即满足这些递归方程的稳定固定点的个数。[^r4] 物理上，RD 对应张量网络边界理论中可被观察到的简并基态数目。[^r5] 由于定义本身是"满足自洽方程的稳定固定点解的简并度"，即使噪声破坏了底层张量的对称性，这些被略微抬升的态仍然是给定递归过程的稳定不动点，从而赋予 RD 对纯噪声的内禀鲁棒性。[^r2]

### 典型模型的 RD 数值结果

- **二维统计 Ising 模型（配分函数型张量网络）**：低温对称破缺相 $\mathcal{D}=2$，高温无序相 $\mathcal{D}=1$，刻画了 Z<sub>2</sub> 自发对称破缺。[^r1]
- **自旋-1 Heisenberg 链加磁场**：Haldane 相（$h<0.41$）与极化相（$h>0.41$）两相均给出 $\mathcal{D}=2$。[^r1]
- **Z<sub>N</sub> 内禀拓扑态**（含 kagomé 格点 Z<sub>2</sub> 共振价键态、Z<sub>N</sub> string-net 态）：统一公式
  $$\mathcal{D} = (N+1)/2 + d, \quad d=\begin{cases}0, & N \text{ 奇}\\ 1/2, & N \text{ 偶}\end{cases}$$ [^r1]
- **平移不变的二维量子态**（如 toric-code 型态）：仿真验证 RD 在噪声下保持稳定。[^r2]

### 噪声容忍度与边界理论能隙

与 TEE、拓扑 Rényi 熵不同，RD 计数的是自洽本征方程的稳定 contractor 个数，本身即设计为可在不破坏张量对称性的纯噪声下存活。[^r2] 在自旋-1 Haldane 链模型中，临界噪声强度与 Haldane 能隙一致，表明 RD 的鲁棒性强弱可与张量网络边界理论的能隙相联系。[^r2]

### 总结与开放视角

作者将 RD 定位为对 Z<sub>N</sub> 拓扑序的"简单且鲁棒"的检测工具，并揭示了从递归动力学视角看待多体拓扑稳定性这一新视角。[^r2] 文中特别指出一种有趣的对应：当张量网络表示的是二维量子态的内积时，RD 检测的是量子态的拓扑序；当张量网络是二维经典系统的配分函数时，RD 检测的是对称破缺——这暗示了二维量子态与二维经典配分函数之间更深的联系。[^r2] 论文未给出明确的局限性陈述或未来工作清单，仅以"该方法可用于探测对称性轻微破缺邻域内仍具有拓扑性质的态"作为应用展望。[^r2]

## Sources

[^r1]: raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md#L29
[^r2]: raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md#L154
[^r3]: raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md#L23
[^r4]: raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md#L31
[^r5]: raw-not-distributed/D022-2019-noise-tolerant-signature-ofzntopological-order-in-quantum-many-body-states/paper.md#L156
