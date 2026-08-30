---
title: "Machine learning of chaotic characteristics in classical nonlinear dynamics using variational quantum circuit"
type: paper-summary
sources:
  - "raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md"
source_type: official-doc
date: 2026
venue: "Chin. Phys. B"
authors: ["Sheng-Chen Bai", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Machine learning of chaotic characteristics in classical nonlinear dynamics using variational quantum circuit

> **作者**：Sheng-Chen Bai、Shi-Ju Ran | **发表**：Chin. Phys. B
> **核心贡献**：提出用变分量子电路（VQC）以"通用学习"方式复现一维/二维 logistic map 的长期混沌统计特征，并证明其相较 LSTM 与回声状态网络（ESN）能更好抑制"短期预测精度上升 vs 长期特征复现精度下降"之间的发散趋势。

## Navigation

本文聚焦混沌非线性系统中机器学习的两大任务——短期状态预测与长期统计/遍历动力学复现——之间常被忽视的张力。作者采用通用学习范式（单一模型覆盖一定范围的动力学参数 µ），并以 logistic map 为基准，引入可由 µ 调谐的量子特征映射预处理，构建变分量子电路（VQC）做一步前向预测[^r1]。在收敛、分岔、四分岔与混沌区分别比较 VQC、LSTM 与 ESN 的分岔图与 Lyapunov 指数拟合效果，提出以"短期损失 L 与长期损失 L_LE 的发散"作为缺乏泛化能力的指纹[^r1]。结论倾向于紧致、张量网络式的 VQC 更有利于学习长期混沌特征，并对未来高维混沌（Lorenz、流体、时空混沌）以及量子度量刻画 VQC 泛化能力做了展望[^r1]。

## 研究方向定位

研究对象为经典混沌非线性系统的轨迹数据；核心问题是单一 ML 模型能否在一段动力学参数范围内同时兼顾短期状态预测与长期混沌统计复现，并以 VQC 相对 LSTM/ESN 验证其优势；具体场景为一维/二维 logistic map 上的一步前向通用学习[^r1]。

## Content

### 学习范式：通用学习 vs 特定学习

传统方法针对每个动力学参数 µ 训练独立模型（特定学习），作者采用通用学习，由单一模型覆盖一段非平凡的 µ 区间；其假设是这能让模型学到底层动力学而非某个 µ 处的特定属性[^r1]。在该框架下，VQC 的实现关键是一个由 µ 调谐的预处理量子特征映射（quantum feature map），将 µ 的变化显式注入输入编码，使模型能够在一次训练中横跨不同动力学区域[^r1]。

### 评价指标的解耦：L、L_LE 与 Lyapunov 时间

作者明确区分两种精度：

- 短期状态预测精度，用一步前向预测的相对/绝对误差衡量，由 Lyapunov 时间 $T_{\mathrm{LE}} \equiv \lambda^{-1}$ 刻画其有效预测窗口[^r1]。
- 长期动力学特征复现精度 L_LE，用模型迭代轨迹的 Lyapunov 指数与真值 Lyapunov 指数 $\lambda = \lim_{T\to\infty} \frac{1}{T}\sum_{t=1}^{T} \ln| \mathrm{d}f(x;\mu)/\mathrm{d}x |_{x=x_t}$ 的拟合误差衡量[^r1]。

数值上，在混沌区（µ=3.92，正 Lyapunov 指数）相对误差呈指数增长 $\varepsilon_{\mathrm{R}} \sim \mathrm{e}^{\eta t}$，并在 $3.57 < \mu < 4$ 范围内拟合得到指数 $\eta \approx 0.4 + O(10^{-1})$（具体如 µ=3.92 时 $\eta=0.44$）[^r1]。

### 分岔图复现：图像级一致性与 PSNR

为衡量"长期特征复现"而非"逐点预测"，作者把 ML 模型迭代 200 步以上得到的分岔图作为图像，用峰值信噪比（PSNR）$r_P$ 与真值分岔图对比：VQC 与真值的 $r_P = 43.40$，LSTM 为 $r_P = 43.58$，两者在图像层面均高度一致，说明二者都具备一定的长期分布复现能力[^r1]。这种一致性的关键不在于"准确预测"，而在于"长程迭代后状态分布的正确性"[^r1]。

### 核心观察：L 与 L_LE 的发散作为泛化指纹

作者提出一个核心现象——也是本工作的主要诊断信号——随着模型复杂度（VQC 的层数 $N_L$、LSTM 的隐藏维 $D_h$、ESN 的储层规模 $D_r$）增大，短期损失 L 通常下降，但长期损失 L_LE 反向上升，即两条曲线出现发散[^r1]。作者将此解读为即使在最简单 logistic map 上，长期混沌统计的复现仍高度依赖模型的泛化能力而非容量；该发散本身就是缺乏泛化的指纹[^r1]。

### 模型对比与 VQC 的优势来源

在三类模型对比中，作者观察到：

- LSTM 与 ESN 随容量增大均出现明显的 L↓、L_LE↑ 发散[^r1]。
- VQC 在所考察的层数范围内对 L_LE 的抑制更优，能较好地维持 L 与 L_LE 的同步性[^r1]。

作者推测该优势来自 VQC 的块状/张量状结构（张量网络，TN）所带来的紧致数学形式；该结构在量子多体物理与 ML（如量子神经网络、矩阵积态）中已展示出高效性，但在非线性动力学 ML 中的应用此前仍较稀疏[^r1]。

### 总结与未来工作

作者总结：对一步前向训练的模型而言，长期混沌特征的复现能力被过拟合显著抑制；增加模型复杂度通常有损长期复现精度。VQC 凭借紧致的张量网络结构对过拟合有更好的抑制能力，本文因此倡导发展更紧致、高效的模型，而非更大、更复杂的模型[^r1]。

未来工作方向包括：把 VQC 推广至高维混沌（Lorenz 方程、流体动力学模型、更高维时空混沌模型）；用量子度量（quantum measures）理论分析 VQC 的泛化能力；以及引入混合量子-经典优化策略以提高高维设置下的训练效率[^r1]。

## Sources

[^r1]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L382
[^r2]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L437
[^r3]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L439
[^r4]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L441
[^r5]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L443
[^r6]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L451
[^r7]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L457
[^r8]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L459
[^r9]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L496
[^r10]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L498
[^r11]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L500
[^r12]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L502
[^r13]: raw-not-distributed/D052-2026-cpb-chaos-ml/paper.md#L19
