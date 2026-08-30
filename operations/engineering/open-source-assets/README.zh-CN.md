# Ran-ASKS

[English](README.md) | [简体中文](README.zh-CN.md)

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
| 首次 arXiv 投稿 | `v0.2.0` | [`1.0.0`](paper-artifacts/v0.2.0/) | arXiv 标识符和不可变 Release 链接将在发布后补入仓库级引用信息 |

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

## 工作原理

1. 保存每份接收来源及其稳定的寻址元数据。
2. 使用大语言模型生成可读 Wiki 界面和面向机器的语义槽位。
3. 在持久化写图前验证文档局部的 `GraphDelta`。
4. 将嵌入几何与显式的身份、路由、成员关系和生命周期规则结合，把增量融合到累计图状态中。
5. 研究者和智能体使用编译结构进行导航，并在事实使用时返回具有来源地址的证据。

简化表示如下：

```text
来源记录 -> 局部解释 -> 经过验证的 GraphDelta
         -> 持久化 Wiki + 演化中的图 -> 来源可追溯的使用
```

摄入支持断点续做，图融合采用事务机制。构建决策、验证结果和检查点使状态转换
能够被检查和恢复。

## 主要能力

- 支持断点续做的论文和文档摄入，以及保留来源关系的图融合。
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

只有需要模型的工作流才需要在 `.env` 中配置模型后端。随后阅读 `AGENTS.md`：
它是项目运行契约，负责识别请求类型、保护 Raw 层并派发对应工作流。例如，可以
使用以下命令查看查询任务的派发结果：

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

arXiv 记录建立后将补充论文引用和 BibTeX。请引用与论文版本对应的不可变
Ran-ASKS Release，而不是持续变化的 `main` 分支。

## 许可证

Ran-ASKS 以
[PolyForm Noncommercial License 1.0.0](LICENSE) 方式提供源代码。
依照该许可证，允许非商业使用，包括教育机构和公共研究组织的使用。该许可证
不授予商业使用权；商业使用需要另行获得 Shi-Ju Ran 的书面商业许可。商业许可
事宜请通过仓库所有者的 GitHub 联系渠道咨询。

由于限制商业使用，PolyForm Noncommercial 不是 OSI 批准的开源许可证。本仓库
中的“公开发布”仅表示源代码可见，并不表示符合 OSI 对开源软件的定义。

冻结论文数据以及编译后的 Wiki/图数据产物单独使用 `CC BY-NC 4.0` 许可，参见
[`paper-artifacts/v0.2.0/LICENSE-DATA.md`](paper-artifacts/v0.2.0/LICENSE-DATA.md)。
