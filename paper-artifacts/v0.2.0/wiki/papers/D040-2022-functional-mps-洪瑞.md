---
title: "Functional tensor network solving many-body Schrödinger equation"
type: paper-summary
sources:
  - "raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md"
source_type: official-doc
date: 2022
venue: "Phys. Rev. B 105, 165116 (2022)"
authors: ["Rui Hong", "Ya-Xuan Xiao", "Jie Hu", "An-Chun Ji", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Functional tensor network solving many-body Schrödinger equation

> **作者**：Rui Hong、Ya-Xuan Xiao、Jie Hu、An-Chun Ji、Shi-Ju Ran | **发表**：Phys. Rev. B 105, 165116 (2022)
> **核心贡献**：提出 functional tensor network (FTN) 方法，将连续空间多体薛定谔方程的波函数系数表示为张量网络，以矩阵乘积态 (matrix product state, MPS) 为例并通过自动微分与梯度下降求基态能量。[^r1]

## Navigation

本文将张量网络 (tensor network, TN) 的应用从量子格点模型扩展到连续空间多体薛定谔方程。[^r2] 给定正交函数基后，波函数系数以张量网络表示，基态求解转化为以能量为 loss 的最小化问题。[^r1] 作者以一维矩阵乘积态 (matrix product state, MPS) 为例展示方法，并对含两体与三体相互作用的耦合谐振子进行基准测试。[^r3]

## 研究方向定位

针对连续空间中强关联多体薛定谔方程难以求解的问题，提出以张量网络表示波函数系数并用自动微分与梯度下降求基态的方法。[^r2]

## Content

### 方法：从函数基到张量网络的构造

给定一组正交函数基，多体波函数在该基下的展开系数被组织成张量网络；observable（如能量）通过对两个张量网络取内积（tensor contractions）得到，基态求解即为以该能量为 loss 的最小化问题。[^r1] 论文以一维矩阵乘积态 (matrix product state, MPS) 作为示例，其复杂度随系统尺寸仅线性增长。[^r1]

### 优化：自动微分与梯度下降

组成 MPS 的张量被实现为可自动微分 (automatically differentiable) 的张量，其梯度在反向传播过程中获得，并被用于以梯度下降算法更新 MPS。[^r4] loss 由两个 MPS 的内积计算：一个 MPS 是试探波函数被各算符作用后的结果之和，另一个 MPS 本身即代表试探波函数（如图 1 所示）。[^r4]

### 数值结果：耦合谐振子基态

测试模型为含两体与三体相互作用的耦合谐振子，固定 $\omega_n = 1$。[^r3] 当仅含两体相互作用（$\tilde\gamma=0$）时，体系可解耦为独立振子，精确基态能为 $E_{\text{exact}} = \tfrac{1}{2}\sum_{n=1}^{N}\sqrt{1+2\gamma\cos\!\left(\tfrac{n\pi}{N+1}\right)}$。[^r5] 取 $\gamma=0.5$，对 $N=4,6,\dots,20$，function MPS（bond dimension $\chi=16$）得到的基态能量与精确值的偏差 $\varepsilon=|E-E_{\text{exact}}|$ 在 $\mathcal{D}>12$ 后近似收敛；$\mathcal{D}$ 为函数基截断维度（the physical bond dimension of the MPS）。[^r6]

### 截断误差、纠缠熵与三体相互作用

纠缠熵 $S=-2\sum_{k=0}^{\chi-1}\lambda_k^2\ln\lambda_k$（$\lambda_k$ 为纠缠谱第 $k$ 个 Schmidt 数）在 MPS 中部测量，刻画前 $N/2$ 个振子与剩余振子之间的"量子版"关联；随 $\mathcal{D}$ 增大 $S$ 收敛到约 $0.36$，说明基态并非高度纠缠，纠缠谱最小值约为 $O(10^{-5})$，与能量误差 $\varepsilon$ 同量级。[^r7] 固定 $\mathcal{D}=8$ 时，$\varepsilon$ 在 $\chi\ge 16$ 时收敛到 $O(10^{-5})$。[^r8] 三体项（强度 $\tilde\gamma$）存在时体系不可解耦：取 $N=16,\mathcal{D}=8,\chi=16,\gamma=-0.2$，$\tilde\gamma<\tilde\gamma_c\simeq 0.168$ 时存在实的基态能解；越过该临界点，loss $\mathcal{L}$ 突然远大于 1，标识物理解不再存在。[^r9]

### 视角：与神经网络的对比与可推广性

与基于神经网络、总体上高非线性的微分方程求解器相比，functional TN 不需要采样或训练数据，优化仅由张量收缩完成，误差由纠缠控制，具有可解释性。[^r4] 作者将 FTN 定位为通用多变量微分方程求解器：函数基可替换为如 Taylor 级数等其他展开，MPS 可推广到投影纠缠对态 (projected entangled pair states)；电子体系可结合费米张量网络 (fermionic tensor networks) 以满足反对易关系，文中将这一方向留作未来工作。[^r10]

## Sources

[^r1]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L9
[^r2]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L23
[^r3]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L276
[^r4]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L28
[^r5]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L285
[^r6]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L288
[^r7]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L300
[^r8]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L302
[^r9]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L324
[^r10]: raw-not-distributed/D040-2022-functional-mps-洪瑞/paper.md#L330
