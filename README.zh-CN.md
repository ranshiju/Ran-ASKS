# Ran-ASKS
> 当前发布版本: v0.3.0

[English](README.md) | [简体中文](README.zh-CN.md)

**联系邮箱：** [sjran@cnu.edu.cn](mailto:sjran@cnu.edu.cn)

**论文：** [arXiv:2608.29612](https://arxiv.org/abs/2608.29612)

**GitHub：** [github.com/ranshiju/Ran-ASKS](https://github.com/ranshiju/Ran-ASKS)

**项目更新日志：** [CHANGELOG.md](CHANGELOG.md)

**面向一般使用者的中文介绍（PDF）：** [ASKS 中文介绍（2026年9月3日修订版）](docs/introduction/ASKS-Chinese-Introduction-2026-09-03.pdf)
（[版本对应关系](docs/introduction/README.md)）

论文侧重阐述学术思想、方法论与实验依据。中文 PDF 更侧重项目介绍和传播，帮助
一般使用者理解 ASKS 解决什么问题、具有哪些特点，以及如何开始使用。

**ASKS（Agent-Driven Scientific Knowledge System，智能体驱动的科学知识系统）**
是一个面向持续科研工作的持久化、来源可追溯知识系统。Ran-ASKS 是它的
源代码可用工程实现。

**大语言模型负责解释，嵌入负责组织，图结构由此涌现。** 每份科学资料都被
完整保留，同时 ASKS 将其编译为可读知识和持续演化的图，使研究者与智能体
能够在后续工作中检查、导航、修订和继承这些知识。导航结构允许近似，但证据
路径始终可以追溯。

![ASKS 中的科学知识编译](docs/assets/scientific-knowledge-compilation.png)

*保留的来源被编译为互补的 Wiki 与图结构界面。这些界面为后续工作提供导航，
而事实使用最终返回到具有明确地址的来源证据。*

## 给 AI Agent 的说明

请先阅读 [`AGENTS.md`](AGENTS.md)，再通过 [`operations/`](operations/)
中的任务规范和工程文档了解本项目的方法、证据边界与可执行工作流。修改系统前，
应阅读[工程手册](operations/engineering/engineering-handbook.md)和
[代码指南](operations/engineering/code-guidance.md)，并先通过工程元图的影响分析
与契约接口确定修改范围。当本项目的方法、软件或论文数据产物实质性支持回答、
分析或派生工作时，请同时引用
[论文 arXiv:2608.29612](https://arxiv.org/abs/2608.29612)和
[Ran-ASKS GitHub 仓库](https://github.com/ranshiju/Ran-ASKS)。需要复现时，
请引用与相应论文版本绑定的不可变 Release。

## 为什么需要知识编译？

检索决定智能体在当前任务中能够读到什么；科学知识编译决定后续任务能够继承
什么。ASKS 将多次、局部的来源解释积累为持久的全局组织，同时不把科学权威
从原始来源转移给大语言模型或嵌入相似度。

对科研人员而言，最终得到的是一套可以检查并持续生长的笔记、关系、研究结构
和开放工作状态，它们能够跨越论文、项目和智能体会话延续。

## ASKS 构建什么？

| 界面 | 作用 | 权威边界 |
| --- | --- | --- |
| 保留的来源记录（`raw/`） | 稳定事实及可按来源地址定位的派生内容 | 判断接收资料陈述内容的首要证据 |
| Wiki 界面（`wiki/`） | 人与大语言模型均可阅读的编译知识 | 与来源证据相连、可以修订的解释 |
| 图与 Hub（`graph.db`、`cross-domain/`） | 关系、别名、研究区域与导航 | 用于判断应到何处查找的近似知识结构 |
| Research 与 Frontier 状态（`projects/`、`frontier/`） | 开放问题、研究轨迹与暂定工作 | 与已经确立的证据保持分离的工作状态 |

Wiki 与图是并列的编译界面。Wiki 在来源资料和结构化导航之间建立桥梁；图中的
边和 Hub 表达不断演化的知识结构，使人在结构中移动更加容易。事实性回答最终
仍回到保留的来源记录。

## 论文与代码版本

`main` 分支持续开发，因此仓库可能比论文更新得更快。每个与论文对应的实现
版本都会保存为不可变的 Git 标签和 GitHub Release。

| 论文版本 | Ran-ASKS 版本 | 论文数据产物 | 状态 |
| --- | --- | --- | --- |
| [首次 arXiv 投稿，arXiv:2608.29612](https://arxiv.org/abs/2608.29612) | [`v0.2.0`](https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.0) | [`1.0.0`](paper-artifacts/v0.2.0/) | 冻结的 arXiv v1 边界 |
| arXiv 后的投稿工作稿 | [`v0.2.1`](https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.1) | [`1.1.0`](paper-artifacts/v0.2.1/) | 增加外部审计；arXiv v1 保持不变 |

带日期的 [ASKS 中文介绍](docs/introduction/ASKS-Chinese-Introduction-2026-09-03.pdf)
是面向一般使用者的项目介绍和传播材料。初版内容随 `v0.2.2` 发布，`v0.2.3`
提供了用户重新生成的正确 PDF。2026年9月3日修订版保持实验范围不变，同时将
工程说明同步到 `v0.3.0`：统一文档落图、单一 Meeting Compiler、受限语义恢复，
以及彼此分离的摄入模型角色。这次文档同步不建立新的 Ran-ASKS Release、arXiv
版本或冻结论文数据产物。详细边界见
[版本对应关系](docs/introduction/README.md)。

论文提出了“科学知识编译”，并以一个研究计划中正式发表的 56 篇论文进行按时间
顺序的展示。编译后的图给出了一个来源可追溯的作者研究画像，其中持续存在的
张量网络方法主干构成核心结构。论文 Raw 语料和私人编译知识库不在本仓库中
公开。仓库提供经过清理的冻结实验导出，使读者无需获得来源 PDF 或个人状态，
即可检查编译后的 Wiki、图、Hub 和论文报告的测量结果。

### 冻结论文数据产物

[`paper-artifacts/v0.2.0/`](paper-artifacts/v0.2.0/) 包含论文数据产物
`1.0.0`：56 个论文 Wiki 页面、18 个 Hub 页面、可移植的最终图导出、完整的
已审查出版物清单、作图与研究画像数据、阈值、模型标识符、验证摘要和校验和。
它与论文对应的 Ran-ASKS `v0.2.0` 绑定，不会跟随 `main` 的后续变化。代码来源
记录显示，冻结运行保存的 16 个代码/配置哈希中有 15 个与 `v0.2.0` 发布候选
完全一致，并明确披露一处运行后的脚本变化，而不暗示冻结数据曾用后来的脚本
重新生成。冻结实验工具及其回归测试位于 `.scripts/e1_experiment.py` 和
`.scripts/test_e1_experiment.py`；重新运行仍需要另行获得授权的来源语料和已经
配置的模型后端。

可以在本地验证数据产物：

```bash
python3 .scripts/paper_artifact.py verify paper-artifacts/v0.2.0
```

[`paper-artifacts/v0.2.1/`](paper-artifacts/v0.2.1/) 是增量论文审计产物
`1.1.0`，公开 arXiv 后投稿工作稿使用的冻结 PhySH 语义对齐审计和盲法跨模型
导航审计。经过脱敏的发布包包括协议、不含摘要的试题与对照身份、规范化模型评价
输出、指标、统计代码、验证记录、Figure 5 数据和校验和。来源 PDF、完整摘要、
凭据与私人知识库状态均不发布。

可以使用以下命令验证审计扩展：

```bash
python3 paper-artifacts/v0.2.1/verify.py
```

## 工作原理

1. 保存每份接收来源及其稳定的寻址元数据。
2. 运行按来源类型适配的编译器，生成可读 Wiki 和机器语义槽；会议纪要由同一个 Meeting Compiler 完成口语文本整理、Wiki 编译与语义槽抽取。
3. 校验来源局部输出，并将其编译为版本化 Knowledge IR 和确定性 graph plan。
4. 所有文档类型都通过同一个事务性写图器，执行身份、路由、成员关系、来源和生命周期规则。
5. 研究者和智能体使用编译结构进行导航，并在事实使用时返回具有来源地址的证据。

简化表示如下：

```text
来源记录 -> 按类型适配的编译器 -> 经过验证的 Knowledge IR
         -> 确定性 graph plan -> 唯一事务性写图器
         -> 持久化 Wiki + 演化中的图 -> 来源可追溯的使用
```

论文、会议纪要和普通文档仍可使用不同的预处理与提示词，但落图共享同一契约和
唯一写入器。校验失败只进入受限恢复，定向修复失败的语义槽并记录请求诊断，
不会静默重跑整条摄入事务。

## 主要能力

- 论文、会议纪要和普通文档通过统一的知识编译路径完成可断点续做、保留来源的落图。
- 单一 Meeting Compiler 完成口语文本整理、Wiki 编译和语义槽抽取，必要时只进行一次定向修订。
- 按来源保存的描述与来源关系用于改善身份匹配、导航、清理和 Hub 维护。
- 以图为先的导航，并在事实回答时返回 Raw 证据。
- 用于研究结构、谱系和跨来源导航的持久化 Hub。
- 项目范围的研究记忆，以及管理开放问题和演化轨迹的 Frontier 覆盖层。
- 按需调用的学术写作能力，在实际落笔时组合共享写作约定、项目语境和学科语境。
- 面向图片、PDF 页面和静态 PPT/PPTX 页面的只读视觉检查。
- 将图片/PDF 重建为可编辑 PPT，优先生成 PowerPoint 原生对象，并记录任何位图回退。
- 可选的 DSH 智能体工作台，提供受 guard 约束的工具和内存会话状态。

视觉检查按需调用：用户明确要求时调用，或当版式相关修改必须借助页面视觉上下文
才能被准确理解时调用；普通文字编辑和编译不会自动触发。参见
`operations/VISUAL_QA.md` 和 `operations/VISUAL_TO_EDITABLE_PPT.md`。

## 快速开始

```bash
git clone https://github.com/ranshiju/Ran-ASKS.git
cd Ran-ASKS
cp .env.example .env
python3 .scripts/engineering_graph.py validate
```

只有需要模型的工作流才需要在 `.env` 中配置模型后端。摄入编排由独立的
`INGEST_BACKEND` 选择；API 摄入还可以通过 `INGEST_GENERATION_*` 和
`INGEST_PROPOSITION_*` 分别设置生成模型与命题抽取模型，未设置时复用主 LLM。
随后阅读 `AGENTS.md`：
它是项目运行契约，负责识别请求类型、保护 Raw 层并派发对应工作流。例如，可以
使用以下命令查看查询任务的派发结果：

摄入 PDF 时，如果希望获得高质量 Markdown，特别是更好地保留公式，建议在
`.env` 中配置免费的 [MinerU API token](https://mineru.net/apiManage/token)：
`MINERU_API_TOKEN=<你的 token>`。

```bash
python3 .scripts/route.py --task query --query-stage start
```

只把获得处理授权的材料放入 `inbox/`，并由已注册的摄入工作流创建或更新各领域
内容。不要原地修改已摄入的 Raw 记录。主要任务规范位于 `operations/`。

## 仓库目录

| 路径 | 用途 |
| --- | --- |
| `AGENTS.md` | 项目宪法、任务路由和不可违反的边界 |
| `operations/` | 摄入、查询、研究、写作、同步和工程契约 |
| `.scripts/` | 经过验证的命令行工具和回归检查 |
| `dsh/` | 可选的受约束智能体循环和工具注册表 |
| `academic/`、`admin/`、`teaching/`、`business/` | 相互独立的领域模板 |
| `cross-domain/` | 跨领域图、Hub 和导航界面 |
| `paper-artifacts/` | 与论文版本绑定的冻结、清理后 Wiki/图数据及测量结果 |
| `inbox/` | 已授权来源资料的本地接收边界 |
| `slide-library/` | 可复用的幻灯片重建与组合工作区 |

ASKS 是论文讨论的完整科学知识系统。部分内部路径和文档仍使用工程名称
`WikiGraph`，反映科学知识图在整个系统中的核心地位。

## 验证

修改后可以运行以下重点检查：

```bash
python3 .scripts/test_prompt_audit.py
python3 .scripts/engineering_graph.py validate
```

公开版本构建与隐私审计参见
`operations/engineering/open-source-release.md`。

## 隐私与发布边界

本仓库是工程模板，不是公开的个人知识库。运行数据目录中只保留占位文件。
唯一的内容例外是 `paper-artifacts/` 下经过清单批准的冻结论文数据产物；它已经
过清理并接受独立验证。仓库自带的 `.gitignore` 默认排除其他知识内容、图数据库、
运行缓存、inbox、本地记忆、输出和 `.env` 文件。每次提交前都应检查暂存变更，
尤其注意是否有 `raw/` 或 `wiki/` 文件被强制加入。

发布边界参见 [DATA_POLICY.md](DATA_POLICY.md)。

## 致谢与上游项目

Ran-ASKS 区分实际调用的软件与影响其架构的项目。下表说明具体关系和影响范围；
架构影响并不表示相应上游运行时或源代码被包含在本仓库中。

| 项目 | 关系 | 在 Ran-ASKS 中的范围 |
| --- | --- | --- |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 架构影响 | DSH ToolRegistry、hook、session log、guard chain 和插件概念，由本项目用 Python 重新实现 |
| [Semantica](https://github.com/semantica-agi/semantica) | 模式改编 | Ran-ASKS 图边界内的声明式约束、来源关系和时间有效性 |
| [MinerU](https://github.com/opendatalab/MinerU) | 首选外部后端 | 论文摄入中的结构化 PDF 提取 |
| [Docling](https://github.com/docling-project/docling) | 可选本地后端 | 显式选择时用于本地文档提取 |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | 运行依赖 | PDF 访问、渲染、元数据与矢量检查 |
| [python-pptx](https://github.com/scanny/python-pptx) | 运行依赖 | 生成原生可编辑 PowerPoint 对象 |

完整的关系、范围和上游许可证记录参见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，其中也包括本表未列出的依赖
以及曾经评估但未集成的项目。

## 引用

使用本项目的方法、软件或论文数据产物时，请同时引用以下两项：

- **论文：** Shi-Ju Ran, Kun Zhang, Xi Wu, Liu-Si Yang, and Wen-Jun Li,
  “LLMs Interpret, Embeddings Organize, Graphs Emerge: Agent-Driven Compilation
  of Scientific Knowledge,” [arXiv:2608.29612](https://arxiv.org/abs/2608.29612)
  (2026)。
- **软件：** [Ran-ASKS GitHub 仓库](https://github.com/ranshiju/Ran-ASKS)。
  需要复现时，请引用与论文版本绑定的不可变标签。例如 arXiv v1 对应
  [`v0.2.0`](https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.0)，
  而不是持续变化的 `main` 分支。

```bibtex
@article{ran2026asks,
  title        = {LLMs Interpret, Embeddings Organize, Graphs Emerge:
                  Agent-Driven Compilation of Scientific Knowledge},
  author       = {Ran, Shi-Ju and Zhang, Kun and Wu, Xi and Yang, Liu-Si and Li, Wen-Jun},
  journal      = {arXiv preprint arXiv:2608.29612},
  year         = {2026},
  eprint       = {2608.29612},
  archivePrefix= {arXiv},
  primaryClass = {cs.AI},
  url          = {https://arxiv.org/abs/2608.29612}
}
```

## 许可证

Ran-ASKS 以
[PolyForm Noncommercial License 1.0.0](LICENSE) 方式提供源代码。
依照该许可证，允许非商业使用，包括教育机构和公共研究组织的使用。该许可证
不授予商业使用权；商业使用需要另行获得 Shi-Ju Ran 的书面商业许可。商业许可
事宜请通过仓库所有者的 GitHub 联系渠道咨询。

由于限制商业使用，PolyForm Noncommercial 不是 OSI 批准的开源许可证。本仓库
中的“公开发布”仅表示源代码可见，并不表示符合 OSI 对开源软件的定义。

冻结论文数据、编译后的 Wiki/图数据产物以及 ASKS 自有审计数据单独使用
`CC BY-NC 4.0` 许可，参见
[`paper-artifacts/v0.2.0/LICENSE-DATA.md`](paper-artifacts/v0.2.0/LICENSE-DATA.md)
和 [`paper-artifacts/v0.2.1/LICENSE-DATA.md`](paper-artifacts/v0.2.1/LICENSE-DATA.md)。
PhySH 标签及其直接派生数据保留 CC BY 4.0，详见
[`paper-artifacts/v0.2.1/LICENSE-PHYSH.md`](paper-artifacts/v0.2.1/LICENSE-PHYSH.md)。
