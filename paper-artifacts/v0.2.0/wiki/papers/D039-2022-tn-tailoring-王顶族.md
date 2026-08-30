---
title: "Efficient simulation of quantum many-body thermodynamics by tailoring a zero-temperature tensor network"
type: paper-summary
sources:
  - "raw-not-distributed/D039-2022-tn-tailoring-王顶族/paper.md"
source_type: official-doc
date: 2022
venue: "Phys. Rev. B 105, 155155 (2022)"
authors: ["Ding-Zu Wang", "Guo-Feng Zhang", "Maciej Lewenstein", "Shi-Ju Ran"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Efficient simulation of quantum many-body thermodynamics by tailoring a zero-temperature tensor network

> **作者**：Ding-Zu Wang、Guo-Feng Zhang、Maciej Lewenstein、Shi-Ju Ran | **发表**：Phys. Rev. B 105, 155155 (2022)
> **核心贡献**：提出 TN tailoring 方法——从零温无限张量网络（TN）"剪裁"有限层并"缝合"形成虚时方向的周期边界，以接近与目标温度无关的耗时获得高精度有限温性质。

## Navigation

TN tailoring 是面向强关联量子多体系统有限温模拟的张量网络方法。与常见的退火或重正化群思路相反，它直接从代表零温密度矩阵的无限张量网络出发，沿虚时方向剪裁 K 层并缝合周期边界，从而表示目标温度 T=1/(K·τ) 的有限温约化密度算符 [^r1]。论文在临界横向场 Ising 链与 XY 链等基准模型上与 LTRG、∂TRG、cMPO 等方法对比，展示了精度与温度无关的近常数耗时等优势 [^r2]。该思路可推广至高维玻色/费米系统以及连续空间量子场 [^r1]。

## 研究方向定位

针对强关联量子多体系统有限温模拟在极低温下精度与效率显著退化的问题，论文在零温无限张量网络基础上提出"剪裁+缝合+边界矩阵乘积态（MPS）微调"的方法，于临界 Ising、XY、XXZ 链上进行基准验证 [^r2]。

## Content

### 方法：从零温 TN 到有限温 TN

作者将"零温 TN"定义为在热力学极限下、其收缩给出零温密度矩阵的无限张量网络（即 e^(−Ĥ/T)，T→0）。具体步骤为 [^r1]：

1. 用基态算法（如 DMRG 或 TEBD）从无限 TN 中提取沿虚时方向的边界 MPS，记作 ⟨L| 与 |R⟩ [^r1]。
2. 沿虚时方向从无限 TN 中"剪裁"出 K 层；该 TN 的高度直接对应要模拟的温度 T=1/(K·τ) [^r1]。
3. 将剪出的有限部分"缝合"，使其沿虚时方向具备周期边界条件，用于计算观测量 [^r1]。
4. 对有限高度的边界 MPS 进行精细调节（fine-tuning），以补偿"从零温网络截取"引入的偏差 [^r1]。

该思想与"从无限系统借助纠缠平均场投影获取有限系统信息"的做法相似 [^r1]。

### 基准模型与对比方法

论文在无限量子 Ising 链 Ĥ=∑Ŝ_n^x Ŝ_{n+1}^x − h∑Ŝ_n^z 上于临界场 h=0.5 测试，将自由能逐点相对误差 δf=|f−f_exact|/|f_exact| 与解析解比较 [^r2]。被对比的现有方法包括 [^r2]：

- 线性张量重正化群（LTRG）[^r2]；
- 可微张量重正化群（∂TRG，取优化深度 n_d=4、总扫描迭代 n_s=3）[^r2]；
- 连续矩阵乘积算符（cMPO）[^r2]。

此外，还在 XY 链与各向异性参数 Δ=0、1、2 的 XXZ 链上对比了 TN tailoring、LTRG、cMPO 的比热 C_v 与内能 U [^r3]。

### 精度：随温度与键维度

- 无 fine-tuning 时，TN tailoring 在低温下即已获得低于 LTRG 与 ∂TRG 的 δf；在高温下 δf 偏大，但仍处于约 O(10^−4) 或更低 [^r2]。
- 启用 fine-tuning 后，从 T~O(1) 到 O(10^−3) 的全温度区间 δf 都显著降低，较 LTRG 与 ∂TRG 低数个数量级；δf 随 T 降低（即随 β 增大）而升高，反映关联与纠缠随降温增长 [^r2]。
- 在 T=1/32 下，按可比键维度对比（cMPO 与 TN tailoring 对 χ、∂TRG 对 χ²，因为后者复杂度按 χ⁴ 缩放、前两者约按 χ⁶ 缩放），TN tailoring 取得最高精度：χ=20 时 δf≈4.0×10^−10（TN tailoring）与 4.×10^−9（cMPO），而 χ=400 时 ∂TRG 给出 δf≈4.3×10^−9 [^r2]。
- 在 XXZ 链上，TN tailoring、LTRG 与 cMPO 结果彼此吻合，差异约 O(10^−4)；其中 LTRG 取 χ=200，cMPO 取 χ=20 [^r3]。

### 效率：与目标温度近似无关的耗时

固定 β≥100、键维度 χ=20、Trotter 切片 τ=10^−4、fine-tuning 学习率 η=10^−9，并在 ∂TRG 中从 n_d=1、n_s=3 逐步加深至精度收敛后 [^r2]：

- TN tailoring 的耗时为"获取零温 TN 的时间 + fine-tuning 时间"之和，在临界 Ising 与 XY 链上对不同 β 几乎保持不变 [^r2]；
- 在临界 Ising 链的 δf–CPU 时间对比中，cMPO 给出明显更高的 δf，TN tailoring 与 ∂TRG 居前 [^r2]。

论文明确指出，TN tailoring 高精度的关键在于"从零温 TN 剪裁并缝合"这一 tailoring 步骤本身，把边界 MPS 改写为连续形式（以契合温度的连续性）并不会显著影响精度 [^r2]。

### 适用与可推广范围

作者明确指出 TN tailoring 可推广至高维相互作用的玻色、费米系统以及连续空间中的量子场 [^r1]。

## Sources

[^r1]: raw-not-distributed/D039-2022-tn-tailoring-王顶族/paper.md#L23
[^r2]: raw-not-distributed/D039-2022-tn-tailoring-王顶族/paper.md#L110
[^r3]: raw-not-distributed/D039-2022-tn-tailoring-王顶族/paper.md#L140
