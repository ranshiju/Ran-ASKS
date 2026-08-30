---
title: "Thermodynamics of spin-1/2 Kagomé Heisenberg antiferromagnet: algebraic paramagnetic liquid and finite-temperature phase diagram"
type: paper-summary
sources:
  - "raw-not-distributed/D016-2018-kagome-chenxi/paper.md"
source_type: official-doc
date: 2018
venue: ""
authors: ["Xi Chen", "Shi-Ju Ran", "Tao Liu", "Cheng Peng", "Yi-Zhen Huang", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Thermodynamics of spin-1/2 Kagomé Heisenberg antiferromagnet: algebraic paramagnetic liquid and finite-temperature phase diagram

> **作者**：Xi Chen、Shi-Ju Ran、Tao Liu、Cheng Peng、Yi-Zhen Huang、Gang Su | **发表**：2018
> **核心贡献**：利用张量网络方法（iPEPS 与 ODTNS）系统计算自旋-1/2 笼目（kagomé）海森堡反铁磁体（KHA）的基态与有限温热力学性质，发现了一种介于零温无能隙量子自旋液体（QSL）与高温平庸顺磁相之间的"代数顺磁液体"（APL）中间热相，并在 h–T 平面上给出了首个相图。

## Navigation

本文针对自旋-1/2 笼目海森堡反铁磁体这一量子自旋液体（QSL）的经典候选体系，采用 iPEPS 与 ODTNS 两套张量网络方法，结合 cluster update 与 full update 两类环境近似，对基态与有限温热力学进行了高精度模拟。作者在低温下观测到磁化率与比热的代数行为，从而支持无能隙 QSL 图像；进一步通过温场依赖研究，提出 APL 这一新相并在 h–T 平面给出有限温相图。工作还讨论了结果与 Herbertsmithite 实验相图的关联。

## 研究方向定位

研究对象为外磁场下自旋-1/2 笼目海森堡反铁磁体的有限温热力学与基态性质，核心问题在于 QSL 的能隙有无及热涨落引入后的相结构，方法上采用基于张量网络的 iPEPS 与 ODTNS 数值算法并辅以两种环境更新方案 [^r1]。

## Content

### 基态与无能隙量子自旋液体的证据

对于自旋-1/2 KHA 的零温基态，作者采用张量网络方法进行模拟，得到倾向于无能隙 QSL 的证据：未观察到零磁化平台，且在低温区磁化率与比热均呈现代数行为 [^r1]。这一结果与变分蒙特卡洛、部分张量网络态方法以及近期大规模 DMRG 模拟（给出无能隙 Dirac 自旋液体迹象）一致，但与 DMRG、symmetric TNS 等倾向 $Z_2$ 有能隙 QSL 的结论仍存在分歧 [^r1]。

### 代数顺磁液体（APL）的提出

作者发现在某一温区，体系在零场下虽然仍保持零磁化平台（即保持 QSL 的代数特征），但施加微小磁场后即表现为顺磁响应——磁化随磁场呈线性关系。基于此，他们将该相命名为代数顺磁液体（algebraic paramagnetic liquid, APL），并将其理解为连接零温代数 QSL 与高温平庸顺磁相的中间热相。作者进一步推测：只要向无能隙 QSL 引入合适的热涨落，APL 便可一般性地出现 [^r1]。

### h–T 平面有限温相图

通过系统扫描磁场与温度，作者首次给出了自旋-1/2 KHA 在 h–T 平面上的相图，识别出五个相区：QSL、APL、场致有序相（field-induced ordered state）、canted（中间）相，以及高温平庸顺磁相 [^r3]。相图揭示了一个有趣现象：即便零温 QSL 在强磁场下被冻结为"固态"（例如磁化平台相），适当的热涨落仍能将其"熔化"回类液态的 APL 态 [^r1][^r3]。

### 计算方法与设置

研究采用两类张量网络方法：用于基态的 iPEPS 与同时处理基态与配分函数的 ODTNS [^r2]。对网络收缩中的截断误差，分别使用 cluster update（Bethe 近似模拟团簇外环境，团簇由 6 张量构成以保持对称性）与 full update（通过 iTEBD 收缩整个张量网络，可更精确地考虑所有环路）[^r2]。有限温部分则借助张量乘积密度算符（TPDO）实现 Trotter 演化，固定 Trotter 切片为 $10^{-2}$，以高温密度算符为初态逐步演化至目标温度 [^r2]。cluster update 与 full update 之间的精度-代价权衡在原文中亦有讨论 [^r2]。

### 与 Herbertsmithite 实验的联系

论文明确将所提出的 h–T 相图与 Herbertsmithite（ZnCu₃(OH)₆Cl₂）在磁场下的实验相图进行了对照 [^r3]。关于该实验样品究竟是支持无能隙还是有能隙 QSL，现有比热、磁化率、中子散射、拉曼光谱与 NMR 测量之间仍存在分歧，Dzyaloshinskii–Moriya 相互作用被认为在物理图像中起重要作用 [^r1]。作者指出，在自旋-1/2 KHA 上关于磁场中热力学的计算仍很稀少，希望本工作能推动更多实验探索 [^r3]。

## Sources

[^r1]: raw-not-distributed/D016-2018-kagome-chenxi/paper.md#L33
[^r2]: raw-not-distributed/D016-2018-kagome-chenxi/paper.md#L43
[^r3]: raw-not-distributed/D016-2018-kagome-chenxi/paper.md#L107
