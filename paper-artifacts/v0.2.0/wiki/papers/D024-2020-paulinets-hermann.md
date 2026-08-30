---
title: "Deep-neural-network solution of the electronic Schrödinger equation"
type: paper-summary
sources:
  - "raw-not-distributed/D024-2020-paulinets-hermann/paper.md"
source_type: official-doc
date: 2020
venue: ""
authors: ["Jan Hermann", "Zeno Schätzle", "Frank Noé"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Deep-neural-network solution of the electronic Schrödinger equation

> **作者**：Jan Hermann、Zeno Schätzle、Frank Noé | **发表**：2020
> **核心贡献**：提出 PauliNet，一种结合物理先验的深度神经网络波函数 ansatz，在变分量子蒙特卡洛框架下对最多 30 个电子的分子实现接近精确求解电子薛定谔方程。

## Navigation

电子薛定谔方程除氢原子外只能数值求解，而精确的全配置相互作用方法随电子数指数级增长。本文将多行列式 Slater–Jastrow–backflow 形式与深度神经网络结合，构建 PauliNet 波函数 ansatz，并通过变分量子蒙特卡洛进行无监督训练。论文在原子、双原子分子、强关联 H₁₀ 以及环丁二烯过渡态能量上展示了精度与计算效率，并在架构选择上与同期 Pfau 等人的 FermiNet 进行了简要比较[^r1][^r2][^r25]。

## 研究方向定位

本文研究对象为多电子分子的基态电子薛定谔方程求解，核心问题是如何用比传统组态/耦合簇方法更少行列式地达到高精度的相关能，方法是构建 PauliNet 这一将物理先验与深度神经网络 Jastrow 因子和 backflow 相结合的实空间变分波函数 ansatz[^r1]。

## Content

### PauliNet 波函数形式

PauliNet 是一种多行列式 Slater–Jastrow–backflow 型试探波函数 $\psi_\theta(\mathbf{r})$：求和项由上自旋与下自旋的 Slater 行列式组成，分子轨道 $\varphi_\mu$ 经 backflow 函数 $\tilde{\varphi}_{\theta,\mu i}(\mathbf{r}) = \varphi_\mu(\mathbf{r}_i)\,f_{\theta,\mu i}(\mathbf{r})$ 修正，整体乘以指数化的 $\exp(\gamma(\mathbf{r})+J_\theta(\mathbf{r}))$，其中 $J_\theta$ 为 Jastrow 因子、$\gamma$ 为编码电子–电子 cusp 条件的固定函数[^r7]。

波函数的反对称性由矩阵行列式承担：行列式在交换任意两行两列时变号，从而自动满足同自旋电子交换下的反对称要求[^r12]。Jastrow 因子在同自旋电子交换下不变、backflow 在同自旋电子交换下等变，以保证 Slater 行列式所继承的反对称性不被破坏[^r14]。

为给变分优化提供良好起点，PauliNet 内置多参考 Hartree–Fock 基线：从一个小完整活性空间的多参考 HF 计算中挑选线性系数最大的若干行列式及其轨道作为输入[^r13]。核 cusp 条件通过对分子轨道施加 Ma 等人的修改实现[^r16]；电子 cusp 条件由固定的 $\gamma(\mathbf{r})=\sum_{i<j}-c_{ij}/(1+|\mathbf{r}_i-\mathbf{r}_j|)$ 编码，其中 $c_{ij}$ 在自旋平行/反平行时分别取 $1/4$ 与 $1/2$[^r18]。为保留这些 cusp 条件，Jastrow 与 backflow 网络被构造成 cuspless 的[^r19]。

### 用 SchNet 表示电子环境

PauliNet 用图卷积神经网络 SchNet 表示分子中的电子，通过对电子–电子距离的迭代消息传递更新电子特征向量 $\mathbf{h}^{(n)}_\theta$，并区分自旋平行与反平行的贡献（上标 $\pm$）[^r20]。距离特征 $e_k(r)=r^2\exp(-r-(r-\mu_k)^2/\sigma_k^2)$ 模仿 PhysNet 但采用使高斯特征及其导数在零距离处为零的包络，从而保证 cuspless 性质[^r21]。

### 变分优化与训练

参数通过变分原理 $E_0=\min_\psi E[\psi]\le\min_\theta E[\psi_\theta]$ 进行无监督优化，能量积分以局部能量 $E_\mathrm{loc}$ 在概率分布 $|\psi|^2$ 上的期望形式采样[^r2]。训练数据通过 Langevin 蒙特卡洛在线生成：每个采样构型仅使用一次；为避免越过原子核，径向步长被裁剪为不超过到最近核的距离，初始电子位置按 HF 的 Mulliken 电荷围绕各核作高斯采样[^r4]。

优化器为加权 Adam，总能量直接作为损失；随机梯度利用 Hermitian 性得到只用波函数二阶导数（而非三阶导数）的形式[^r5]。每个样本的局部能量按 5 倍批内中位数偏差的窗口被对数增长函数平滑截断；学习率采用循环调度[^r6]。

### 在原子与双原子分子上的精度

在一到六个行列式下，PauliNet 恢复 97%–99.9% 的相关能，比标准变分 ansatz 少一到两个数量级的行列式即可达到或超过其精度；图中比较了单行列式/多行列式以及有/无 backflow 的四种 PauliNet 配置，黑箭头标记的点在 y 轴范围之外[^r8]。参考基准取自 Brown 等、Casalegno 等、Morales 等、Lopez Ríos 等、Seth 等及 Toulouse & Umrigar 的工作，每个组态态函数（CSF）对应数个到数十个行列式，具体数目随体系与方法而异[^r8]。

### 强关联 H₁₀ 与环丁二烯

在强关联的线性 H₁₀ 上，PauliNet 在仅六个行列式下即可达到或优于当前最优变分 QMC 的能量[^r25]。对于 28 电子的环丁二烯过渡态能量估计，为稳定对随机性敏感的神经网络训练，论文同步优化十个独立副本并定期丢弃能量最高的五个、复制其余五个，从而获得平滑的能量收敛；使用同步周期 250 与 375 两次独立优化，分别得到 $9.9\pm0.6\,\mathrm{kcal\,mol^{-1}}$ 与 $7.7\pm0.6\,\mathrm{kcal\,mol^{-1}}$ 的能垒估计，二者均落在 MR-CC 方法给出的范围内[^r24]。在单个 GTX 1080 Ti GPU 上，每次优化迭代的计算开销约为 50 秒[^r24]。

### 与 Pfau 等人 FermiNet 的关系

Pfau 等人的并行工作采用与本文相同的基本思路，但除反对称性外不显式编码物理知识，因此需要多得多的优化参数、训练时间也更长，但代价是单次迭代计算量更高，并在部分体系上达到更高精度[^r23]。论文将 FermiNet 的架构设计与优化方法与 PauliNet 内置物理约束相结合视为一个有前景的方向[^r25]。

### 计算开销与实现

所有方法用 PyTorch 实现；HF 轨道以及多行列式展开中的行列式线性系数由 PySCF 用 6-311G 基组计算；代表可训练函数的全连接 DNN 总参数量约为 $7\times 10^4$[^r22]。

### 讨论中的预期可扩展性

论文预期 PauliNet 的计算开销渐近地按 $N^4$ 标度（行列式评估 $N^3$，外加动能项 $N$），具体细节视实现而定；DNN 的灵活性还可能绕开赝势与不完备基组在传统 QMC 中的限制，使该方法成为面向更大体系的高精度黑箱量子化学方法的候选[^r15]。

## Sources

[^r1]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L7
[^r2]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L225
[^r4]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L237
[^r5]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L239
[^r6]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L245
[^r7]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L36
[^r8]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L40
[^r12]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L50
[^r13]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L52
[^r14]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L59
[^r15]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L119
[^r16]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L247
[^r18]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L74
[^r19]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L80
[^r20]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L261
[^r21]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L279
[^r22]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L287
[^r23]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L26
[^r24]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L115
[^r25]: raw-not-distributed/D024-2020-paulinets-hermann/paper.md#L125
