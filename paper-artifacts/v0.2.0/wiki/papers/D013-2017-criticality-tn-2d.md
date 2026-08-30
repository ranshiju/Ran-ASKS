---
title: "Criticality in two-dimensional quantum systems: Tensor network approach"
type: paper-summary
sources:
  - "raw-not-distributed/D013-2017-criticality-tn-2d/paper.md"
source_type: official-doc
date: 2017
venue: "Phys. Rev. B 95, 155114 (2017)"
authors: ["Shi-Ju Ran", "Cheng Peng", "Wei Li", "Maciej Lewenstein", "Gang Su"]
confidence: high
status: current
created: 2026-08-27
updated: 2026-08-27
related: []
---

# Criticality in two-dimensional quantum systems: Tensor network approach

> **作者**：Shi-Ju Ran、Cheng Peng、Wei Li、Maciej Lewenstein、Gang Su | **发表**：Phys. Rev. B 95, 155114 (2017)
> **核心贡献**：提出一种基于无限投影纠缠对态（iPEPS）边界理论的通用方案，将二维量子态的临界性归结为边界态的纠缠谱行为，从而用一维共形场论的中心荷刻画二维临界性。

## Navigation

本文针对二维量子多体系统中缺乏高效无限系统算法的难题，基于一维标度理论与张量网络，提出一种通过 iPEPS 边界态来判断与表征二维量子态临界性的方案。核心判据是：当 iPEPS 有能隙时，边界态纠缠谱 $\{ \lambda_i \}$ 随边界键维 $D$ 增加保持不变；而临界 iPEPS 的 $\{ \lambda_i \}$ 随 $D$ 增大被挤压，导致纠缠熵呈对数发散。方案在 kagome 与 honeycomb 格子上的 NN-RVB 态以及 honeycomb XXZ 模型基态上得到了验证。

## 研究方向定位

研究对象为二维量子多体系统（特别是量子自旋液体及其临界性），核心问题是如何在无限晶格上识别并刻画二维量子态的临界性，方法是利用 iPEPS 的边界理论将二维临界性约化为边界态的有效一维共形场论描述[^r1]。

## Content

### 背景与动机

二维量子多体系统因丰富几何结构与量子涨落和磁有序之间的竞争，成为拓扑序、分数化激发和量子自旋液体等新奇现象的沃土[^r1]。然而缺乏高效的无限系统算法：量子蒙特卡洛与密度矩阵重整化群等成熟方法仅对有限尺寸系统高效，人为引入的有限尺寸能隙又使临界性判别更加困难[^r1]。张量网络（MPS 与其高维推广 PEPS）天然满足纠缠面积律，并能作为非临界基态与热力学模拟的可信变分拟设，但在二维下的优化与物理量提取仍十分困难[^r1]。

### 方案：边界态作为临界性指示器

作者利用 iPEPS 的边界理论，证明二维量子态的临界性可由其边界态（即有效一维哈密顿量 $\mathcal{H}$ 的基态）稳健地复现[^r1]。判据如下：以 MPS 形式表示边界态并改变键维 $D$：
- 对有能隙 iPEPS：边界态的 Schmidt 数 $\{ \lambda_i \}$ 不随 $D$ 增大而改变，纠缠熵收敛到有限值；
- 对临界 iPEPS：$\{ \lambda_i \}$ 随 $D$ 增大被挤压，纠缠熵呈对数发散[^r1]。

由此，二维 iPEPS 的临界性可由定义在边界上的中心荷刻画，与一维系统的既有临界性理论一致[^r1]。

### 验证：kagome 与 honeycomb NN-RVB

方案在 kagome 与 honeycomb 格子上的 NN-RVB 态上进行了检验：二者中一个为有能隙，另一个为临界[^r1]。结果显示 honeycomb NN-RVB 的边界态由 $c = 1$ 共形场论描述，从而为该态的临界性提供了明确识别[^r1]。

### 应用：honeycomb XXZ 基态与变分困难

进一步将方案应用于 honeycomb 格子上的自旋 $1/2$ XXZ 模型基态（由 simple update 算法得到），用以揭示标准变分张量网络方法在刻画二维临界基态时面临的困难[^r1]。

### 结论与适用范围

论文总结指出，方案以纠缠谱 $\{ \lambda_i \}$ 在不同 $D$ 下截然不同的演化模式作为判据，可推广至其他 iPEPS（如 string-net 与手性 iPEPS）以及任意张量网络变分算法得到的拟设，并可与张量乘积密度算符算法结合用于有限温度相变研究[^r2]。

## Sources

[^r1]: raw-not-distributed/D013-2017-criticality-tn-2d/paper.md#L15
[^r2]: raw-not-distributed/D013-2017-criticality-tn-2d/paper.md#L180
