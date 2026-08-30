---
title: "Phase identification in many-body systems by virtual configuration binarization"
type: paper-summary
sources:
  - "raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md"
source_type: official-doc
date: 2021
venue: "Phys. Rev. E 103, 013313 (2021)"
authors: ["Yuan Yang", "Zhengchuan Wang", "Shi-Ju Ran", "Gang Su"]
confidence: high
status: current
created: 2026-08-28
updated: 2026-08-28
related: []
---

# Phase identification in many-body systems by virtual configuration binarization

> **作者**：Yuan Yang、Zhengchuan Wang、Shi-Ju Ran、Gang Su | **发表**：Phys. Rev. E 103, 013313 (2021)
> **核心贡献**：提出名为"虚拟配置二值化（VCB）"的图像分割式可视化方案，将矩阵积态的中心张量编码为彩色图像并二值化，从而无需先验序参量即可揭示多体系统中的量子相与量子相变。

## Navigation

本文将计算机视觉中的图像二值化思想引入量子多体物理：把 DMRG 得到的中心张量视作彩色图像，逐通道以阈值二值化后再对通道图像取绝对差，从而在视觉上区分不同量子相并定位临界点[^r1]。作者在横场伊辛模型（TFIM）、spin-1/2 XXZ 海森堡模型（XXZHM）与自旋-1 海森堡模型（含 Haldane 相）等一维自旋系统中进行了基准测试[^r1][^r17]。该方案的一个特点是，不依赖量子相的序参量先验知识，仅凭二值图像的纹理即可识别相变[^r1]。

## 研究方向定位

本研究针对一维强关联自旋多体系统中的量子相与量子相变识别问题，提出基于矩阵积态中心张量图像二值化的虚拟配置二值化（VCB）方法，将计算机视觉的图像分割技术用于无序参量先验的相变检测[^r1]。

## Content

### 核心思想：把张量当图像来二值化

对于三指标张量 $T=\{T_{m,n}^{d}\}$（$m,n$ 为虚拟指标，$d$ 为物理指标），将其通过映射 $\mathcal G$ 解释为彩色图像，每个 $d$ 通道对应一张 $\chi_1\times\chi_2$ 的灰度图像[^r5]。这一"张量即图像"的解读是 VCB 的起点：彩色 RGB 图像本身也可视为一个三指标张量（颜色空间 $d_c$、像素空间 $dp_1$、$dp_2$），与 MPS 中单格点张量的结构完全同构[^r3]。

### VCB 的两步流程

VCB 的第二步与现有图像二值化方法不同：先按通道二值化，再对通道间二值图像取绝对差[^r7][^r9]。

- **第一步（按通道二值化）**：对每个颜色通道 $d$，以阈值 $\alpha^d$ 将灰度图像 $T^{\mathcal G,d}$ 映射为二值图像 $T^{\mathcal B,d}$，规则为像素值 $\geq\alpha^d$ 取 1，否则取 0[^r7]。对猫图示例，阈值取该通道像素的均值[^r7]。
- **第二步（通道绝对差）**：对任意两通道 $d_1\neq d_2$，计算 $|T^{\mathcal B,d_1}-T^{\mathcal B,d_2}|$，得到 $\sigma(\sigma-1)/2$ 张最终二值图像 $T^{\mathcal AD,\delta_{d_1,d_2}}$[^r9]。

以猫图为例，第一步得到的 $T^{\mathcal B}$ 保留了猫的整体轮廓，而第二步的 $T^{\mathcal AD}$ 则突出眼、耳等局部关键细节，将前景细节与背景分离[^r11]。

### 应用于多体态：阈值取 0.5

对量子态，阈值选取策略与猫图不同：作者对中心张量取 $\alpha^d=0.5$，相当于按张量元素的正负号进行"二值化"[^r9]。映射 $\Phi^{\mathcal G,d}=(1+\Phi^d)/2$（$d=1,2$；$d=3$ 通道填零）将取值范围 $[-1,1]$ 的中心张量元素变换到 $[0,1]$，从而满足彩色图像的像素取值约定[^r13]。

### TFIM 基准测试

在 1D 横场伊辛模型 $\hat H=\sum_i \hat S_i^z \hat S_{i+1}^z - h_x\sum_i \hat S_i^x$ 中，作者取系统尺寸 $L=80$、截断维数 $\chi=30$ 计算基态（混合正则形式），并以中心张量 $\Phi$ 作为态的特征[^r12]。$h_x=0.5$ 处为量子临界点（QCP），分隔反铁磁（AFM）与顺磁（PM）相[^r12]。

- 直接观察 $\Phi^{\mathcal G}[h_x]$：大多数像素呈绿色，只有少数像素因张量元素较大而被红圈标出，肉眼难以区分 AFM、QCP 与 PM 三种情形[^r14]。
- 直方图分析表明，绝大多数张量元素集中在 $[-10^{-4}, 10^{-4}]$，分布随 $h_x$ 变化并不显著，这解释了为何原彩色图像无法直接指示相变[^r14]。
- 第一步得到的二值图像 $\Phi^{\mathcal B}$ 仍呈随机纹理，无法刻画 AFM→PM 的相变[^r15]。
- 第二步得到的 $\Phi^{\mathcal AD,\delta_{1,2}}$ 在 $h_x=0.2$（AFM）与 $h_x=0.8$（PM）处呈现明显差异，PM 相对应的二值图样比 AFM 相更均匀[^r15]。

### 扩展到更广的相变类型

作者将 VCB 推广到 spin-1/2 XXZ 海森堡模型（XXZHM，含 XY 相与反铁磁相）及自旋-1 海森堡模型（含 Haldane 相与二聚化相），覆盖朗道型与非朗道型（含拓扑相）相变[^r16]。

- PM 相、XY 相、TTM 相（二聚化相）下的二值纹理相对均匀；
- Haldane 相下的二值纹理出现菱形孔的"格子窗口"图案[^r16]。
- 在 QCP 处，二值图样表现为相邻两相图样的混合[^r16]。

### 量化指标

为了把"肉眼观察的差异"变成可计算量，作者引入累积 Shannon 熵与虚拟关联（virtual correlation）两种度量[^r17]：

- **累积 Shannon 熵**度量二值像素分布的随机性；二值像素分布越均匀，熵越大。PM 相、XY 相、TTM 相的二值纹理更均匀，因此对应的累积 Shannon 熵更大[^r17]。
- **虚拟关联** $E_{\Phi^w}$、$E_{\Theta^w}$、$E_{\Omega^w}$ 通过对二值图像 $\Phi^{\mathcal AD}$、$\Theta^{\mathcal AD}$、$\Omega^{\mathcal AD}$ 取窗口（如 $\{9,9\}^{1,1}$、$\{40,40\}^{1,1}$）计算得到，同样可以反映量子相变信息[^r17]。

### 局限与未来工作

作者明确指出的局限：阈值选择对方案性能至关重要，错误的阈值会误判背景像素从而降低性能[^r18]。未来工作的方向：可借鉴边缘检测、数字图像处理等更多计算机视觉技术，以获得对重整化量子态更好的图像解读，并激励人们将更多计算机视觉方法引入多体量子态的研究[^r18]。

## Sources

[^r1]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L13
[^r3]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L23
[^r5]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L35
[^r7]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L41
[^r9]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L53
[^r11]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L58
[^r12]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L94
[^r13]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L105
[^r14]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L110
[^r15]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L112
[^r16]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L162
[^r17]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L156
[^r18]: raw-not-distributed/D034-2021-virtual-configuration-binarization/paper.md#L166
