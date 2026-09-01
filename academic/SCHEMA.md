# academic/ — 页面类型与摄入规范

> 摄入前先读 `operations/INGEST.md`。模板和惯例见 `operations/` 下对应文件。

---
---

## raw 目录结构(own / others 分离 + md 与 PDF 同位)

| 目录 | 存什么 | 说明 |
|------|--------|------|
| `raw/works/papers/` | 自己论文(md + PDF 同名同位) | 扁平,不分年 |
| `raw/works/books/` | 专著、教材(md + PDF) | |
| `raw/works/patents/` | 专利(md + 证书 PDF) | 证书类 PDF 原样归档,不提取 md |
| `raw/works/software/` | 软著(md + 证书 PDF) | 同上 |
| `raw/works/editorials/` | 自有专题导言、editorial、观点文章 | 非论文通用文档类型为 `editorial` |
| `raw/works/proceedings/` | 会议书、会议卷 | |
| `raw/references/` | **他人参考论文**(md + PDF 同位) | 与自己成果隔离;命名同 `author-year-slug` |
| `raw/reference-documents/` | 学术参考文档（非论文） | 报告、说明、资料汇编等；类型为 `academic-reference`，不得与他人论文混放 |

**命名规则**:PDF 与 md 同名(`<stem>.pdf` ↔ `<stem>.md`),共处同一目录。PDF 命名以 md 名为准(中文 PDF→拼音),保证可追溯配对。历史 `raw/papers/`(自己论文)与 `raw/achievements/<year>/`(PDF 按年散存)已于 2026-07-18 统一迁入 `raw/works/`。

---

## raw 目录结构(own / others 分离 + md 与 PDF 同位)

| 目录 | 存什么 | 说明 |
|------|--------|------|
| `raw/works/papers/` | 自己论文(md + PDF 同名同位) | 扁平,不分年 |
| `raw/works/books/` | 专著、教材(md + PDF) | |
| `raw/works/patents/` | 专利(md + 证书 PDF) | 证书类 PDF 原样归档,不提取 md |
| `raw/works/software/` | 软著(md + 证书 PDF) | 同上 |
| `raw/works/editorials/` | 综述、editorial、观点论文 | |
| `raw/works/proceedings/` | 会议书、会议卷 | |
| `raw/references/` | **他人参考论文**(md + PDF 同位) | 与自己成果隔离;命名同 `author-year-slug` |

**命名规则**:PDF 与 md 同名(`<stem>.pdf` ↔ `<stem>.md`),共处同一目录。PDF 命名以 md 名为准(中文 PDF→拼音),保证可追溯配对。历史 `raw/papers/`(自己论文)与 `raw/achievements/<year>/`(PDF 按年散存)已于 2026-07-18 统一迁入 `raw/works/`。


## 页面类型

| 页面类型 | type 值 | 存储位置 | 说明 |
|----------|---------|----------|------|
| 概念页 | `concept` | `wiki/concepts/` | 方法论、理论框架、核心概念 |
| 论文摘要 | `paper-summary` | `wiki/papers/` | 每篇摄入论文一页（模板见 `operations/templates/paper-summary.md`） |
| 人物 | `people` | `wiki/authors/` | 研究者、团队成员、参会者、被提及者（目录名 historical 保留） |
| 对比分析 | `comparison` | `wiki/comparisons/` | 方法对比、模型对比 |
| 文献综述 | `review` | `wiki/reviews/` | 按主题组织的综述 |
| 审稿写作指南 | `review-guide` | `wiki/review-guides/` | 审稿意见范文、风格指南、审稿模板 |
| 网页资料 | `web-reference` | `wiki/web-references/` | 网页来源的学术参考资料 |
| 专题导言 | `editorial` | `wiki/editorials/` | 对应新摄入 `raw/works/editorials/`；仅强信号可自动分类 |
| 学术参考文档 | `academic-reference` | `wiki/references/` | 对应新摄入 `raw/reference-documents/`，不表示论文摘要 |
| 会议纪要 | `conference-summary` | `wiki/conferences/` | 学术会议、研讨会记录（命名见 `operations/shared-conventions.md`） |
| 研究讨论 | `discussion` | `wiki/discussions/` | 与 AI 协作的学术讨论（规范见 `operations/DISCUSSION.md`） |
| 科研项目 | `research-project` | `wiki/<项目目录>/` | 科研项目协议书、任务书、工作计划、项目总览（按项目建子目录，如 `wiki/中科大科研项目/`） |

`academic` 非论文摄入不得回退到 `admin`。自动分类只接受专题导言、特邀编辑、本期专题等强 editorial 信号；其余文档返回 `classification_required`，由调用方显式选择 `editorial` 或 `academic-reference` 后再进入事务。存量误存 Raw 保持原位，只能通过单独迁移计划调整 Wiki/图归属。


### people 页定位

people 页是**人物节点**，page 即节点（不分两个）。核心功能是「人→论文/关系」入口。**不是人物档案**。

**建 page 条件**：与知识库用户（张明远）共同署名过至少一篇论文的人物，必须建立极简 people page；这是用户科研网络的一级人物，不受论文数量限制。其他人物只有在有个人属性信息需写入（师生关系、研究方向、单位、昵称反查等）时才建 people page，否则仅建 entity 节点（无页，靠知识边连网络）。

**跨域统一（2026-08-25）**：人物在 graph 中是跨域实体，**people 页统一归 `academic/wiki/authors/`，不按来源域分**。来源为 admin（人事新闻、政策提及）的人物也建在 academic 下；对应 Wiki/profile 页通过 `来源` 边直连 Raw 文档包，人物、引用等导航关系主要连接 Wiki page，不在 admin 域另建独立 people 节点（避免同一人裂为两个节点需事后 merge）。admin 的 `profile` 类型仅用于纯行政档案摘要（简历、聘书），作为资料页而非人物图节点。

**人≠作者角色**：people 可为论文作者、参会者、被提及者。无 people page 时仅 entity 节点，等积累属性信息后再升级建 page。


**无 page 人物治理**：无 page 人物在 graph.db 中保留为 `type: entity, entity_subtype: person`，只允许来自作者行、参与者行或明确人物关系；偶然提及不建人物节点。`person-entity` 活跃软上限为 2000，1600 触发治理预警，达到 2000 后低优先级候选进入 `cross-domain/outputs/people-pending.yaml`；共同作者、用户明确要求或明确高价值关系不阻断。`citation-only` 不计入人物数量。

**两种深度共存**（深度由来源决定，不强求统一）：
- **论文 people 页**（references 论文涉及的高频人物）：极简指针。信息从论文作者行直接提取，不深挖师生/职称/同名核实。每页含：frontmatter(name/institution/papers/related) + Navigation(一句话定位+论文簇) + Content(基本信息表+收录论文)；关系边在 graph.db(作者/就读/所属)。末尾标注「检索辅助定位，未做独立核实」。
- **纪要 people 页**（会议纪要涉及的团队成员）：含师生关系/研究进展，有纪要 raw 支撑，可较深。

**人物画像（Hub 导航信号）**：People page 可有一节 `## 人物画像`，只写一句可 locate 的语义定位。研究人员写“研究对象 + 问题 + 方法”，行政人员写“职责 + 服务范围”，学生写“学习/研究阶段 + 关注方向”，其他人员按其实际角色写与导航有关的活动范围。人物画像不以“从事研究”为硬门槛；但只有存在该节的 People page 才直接参与 Hub 动力学。没有页面的 `entity_subtype: person` 和没有人物画像的页面均静默不参与，不产生 ERROR/WARN，也不得用姓名 embedding 代替。

**同名核实原则**：people 页服务于检索路标，同名不深究——路标写错大不了多读两篇 raw 验证，不伤事实层。区分不了的合并并标 `confidence: medium`，不纠结。事实层始终靠 raw 回溯，people 页本身不承担事实声明。

**建设时机**：共同作者（与张明远共同署名至少一篇）全部建页；非共同作者仍按个人属性、研究网络重要性或用户明确要求决定。批量建时用极简指针，单个页面只从论文作者行提取，不补充未经 raw 支撑的履历。

> **目录名 historical**：物理目录仍为 `wiki/authors/`（历史命名），type 值已改为 `people`。后续可按需重命名目录。

**编码/巩固产物归属**:写入分两阶段——
- **编码阶段**产出来源摘要页(paper-summary/政策摘要/会议纪要等),忠实于 raw,不跨文档抽象;
- **巩固阶段**产出概念页/作者页/对比页 + index/graph.db 边/Topic Hub,提炼抽象形成知识网络。详见 `operations/INGEST.md`「创建模式 — 执行步骤」。

## Frontmatter 模板

```yaml
---
title: "页面标题"
type: concept | paper-summary | people | comparison | review | review-guide | conference-summary | discussion | research-project
sources:
  - raw/papers/filename.md
source_type: official-doc | speech-recognition | ocr | web | discussion
date: YYYY-MM-DD          # 会议纪要自动从路径提取
authors: ["作者A", "作者B"]   # (2026-07-23 新增) 完整作者列表,从 raw 作者行忠实提取(见 INGEST),review/group/专利类页面可省略
confidence: high | medium | low
# (v4,2026-07-25 主数据化) tags/keywords/aliases/abbreviations 字段已删:
#   tags/keywords → 功能被 graph.db 邻接节点覆盖
#   aliases/abbreviations → 并入 graph.db aliases 表(ingest 时 graph_ingest 层1机械抽取自动补)
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: current | deprecated | active | completed | draft   # 默认 current;版本演进时旧版标 deprecated(配合 superseded_by);项目/会议沿用 active|completed
superseded_by: "[[新版页面]]"   # 仅 status: deprecated 时填,指向替代页面
related:
  - "[[concepts/相关概念]]"
  - "[[authors/相关作者]]"
  - "[[conferences/相关会议]]"
---
```

## status 与 version 演进取值规则

`status` 标记页面版本状态,支持版本演进管理(更新策略三级分级落地):

| status | 含义 | 适用场景 |
|--------|------|---------|
| `current` | 现行版本(默认) | 所有页面默认值 |
| `active` | 进行中 | 科研项目/会议纪要(项目状态) |
| `completed` | 已完成 | 科研项目结题(项目状态) |
| `draft` | 草稿 | 未定稿页面 |
| `deprecated` | 已废止 | 版本演进时旧版标记,配合 `superseded_by` 指向新版 |

**版本演进处理**(详见 `operations/INGEST.md` 冲突处理分支):新旧内容都合法时(如政策更新、论文修订),旧页标 `deprecated` + 填 `superseded_by`,新建 current 页,Hub 时间线串联。`deprecated` ≠ 删除(与遗忘策略一致,降级非删除,保留可追溯)。

## source_type 与 confidence 取值规则

`source_type` 标记 raw 来源类型，`confidence` 反映**来源可靠性**（非 LLM 主观自信度），两者协同：

| source_type | 来源示例 | 默认 confidence |
|-------------|---------|----------------|
| `official-doc` | .pdf/.docx/.doc 正式文档、论文、政策文件 | high |
| `speech-recognition` | .txt 语音识别纪要（会议、讨论录音转写） | medium |
| `ocr` | 图片 OCR 提取文本 | medium |
| `web` | 网页资料 | medium（默认）；官方/权威站点→high，论坛/自媒体→low |
| `discussion` | 与 AI 协作讨论的整理稿 | medium |
| `user-assertion` | 用户申明事实（"添加事实：X" 累积到 `cross-domain/raw/facts/user-assertions.md`，见 `operations/INBOX.md`） | medium |

**speech-recognition 来源的特殊处理：**

- 默认 `confidence: medium`，关键实体（人名/术语/数字）需交叉验证才升 high
- 语音识别常见错误：人名同音字（张岩→张延）、术语错（纠缠熵→纠缠商）、断句错
- Lint 时与 `wiki/authors/` 交叉核对纪要提到的人名，不匹配的标记可疑
- graph.db 中来自 SR 来源的边 `is_sr=1`，Query 时向用户说明"此关系来自语音识别纪要，专有名词可能有误"

**判定规则：** sources 字段含 `.txt` 且文件位于 `conferences/` 或 `discussions/` 目录 → `source_type: speech-recognition`。

**web 来源 confidence 分层（按来源权威性，非内容准确性）：**

- `high`：官方机构站点（政府 .gov、学校 .edu）、期刊/出版社官网、权威学术数据库（arXiv、DOI 解析页）、官方文档与 API 文档
- `medium`：默认；知名媒体、行业站点、二次转述但原始来源可追溯
- `low`：论坛、自媒体（公众号、知乎专栏、博客）、匿名或不可追溯来源

判定的是**来源可靠性**：公众号解读即使内容准确，来源仍 medium/low；真正 high 的是原始官方页面。confidence 影响查询时的信息姿态（high 可直接陈述，medium 须注明来源，low 须提示"据自媒体，待核实"）。raw md 头部字段规范见 `operations/shared-conventions.md`「网页资料 raw 格式」。

---

## PDF 论文预处理规范

将 PDF 转为 Markdown 全文。**自己论文**存 `raw/works/papers/`,**他人参考论文**存 `raw/references/`,PDF 与 md 同名同位。命名 `author-year-slug.md`(kebab-case)。

### 内置 extractor 规范

PDF 原文与产出的全文 md 由本项目 `.scripts/extractor.py` 提取(多引擎 pipeline:MinerU > Docling > PyMuPDF)。遵循"不跨项目"原则:extractor 已内化进本项目,不再依赖外部项目。

```bash
python3 .scripts/extractor.py --paper <paper-id>   # 默认 works/papers/,自动按优先级提取
python3 .scripts/extractor.py --paper <paper-id> --external-pdf academic/raw/works/papers/<paper-id>.pdf   # 软链现有 PDF,不复制
python3 .scripts/extractor.py --paper <paper-id> --papers-dir academic/raw/references/   # 他人论文
python3 .scripts/extractor.py --batch                # 批量处理
python3 .scripts/extractor.py --paper <paper-id> --force   # 强制重提取
```

提取后产出的 `paper.md` 存于对应论文目录:自己论文默认 `academic/raw/works/papers/<paper-id>/paper.md`,他人论文 `academic/raw/references/<paper-id>/paper.md`(传 `--papers-dir academic/raw/references/`)。wiki 页面 `sources` 字段用相对路径引用。MinerU token 从 `.env`(键 `MINERU_API_TOKEN`)读取,Docling 需独立 venv `.venv-docling`(缺失则自动跳过该引擎,降级到 PyMuPDF)。

### 来源档位与覆盖规则

引擎按提取质量分档:

| 档位 | 引擎 | 优先级 |
|------|------|--------|
| high | mineru | 3 |
| medium | docling | 2 |
| low | pymupdf | 1 |
| low(等同 pymupdf) | 无来源标记 | 1 |

**low 定义**:已有 md 的 frontmatter 若无来源/引擎标记(无 `parse_meta.yaml` 或无 `preferred` 字段),一律视为最低档 **low,等同 pymupdf(优先级 1)**。

**覆盖规则**:
- 高优先级引擎可覆盖低档(mineru 覆盖 docling/pymupdf/low;docling 覆盖 pymupdf/low)
- **同档不可覆盖**(pymupdf 不可覆盖 low,需 `--force`)
- 更低档不可覆盖
- 覆盖时**不改位置/文件名**,仅替换内容(旧文件备份为 `.md.bak`)

### 来源路径协议

extractor 内化后,论文 sources 用**相对路径**:自己论文 `academic/raw/works/papers/<paper-id>/paper.md`(默认),他人论文 `academic/raw/references/<paper-id>/paper.md`。指向 SynologyDrive **跨项目**文件时才用 `synology://` 协议前缀,由本项目 `.project/config.yaml`(见 `operations/INGEST.md`)的 `synology_roots` 解析为绝对路径。禁止硬编码绝对路径。

- 论文(extractor 产出):自己论文 `academic/raw/works/papers/<paper-id>/paper.md`;他人论文 `academic/raw/references/<paper-id>/paper.md`
- 本项目内部 md(已有历史文件):相对路径如 `raw/works/papers/<author-year-slug>.md`
- 跨项目共用产物:如 `synology://其他项目/...`(仅在确实跨项目引用时用)

### 过渡方案(已有文件)

- 2026-07-18 已有的 75 PDF + 68 md(works/)+ 5 references/ 为历史遗留,保持现状,sources 维持相对路径
- 新摄入论文走新规则:调用内置 `.scripts/extractor.py`,自己论文 `--paper <id>`(默认 works/papers/),他人论文加 `--papers-dir academic/raw/references/`;sources 用相对路径
- 未来 LINT 可加检查项:历史 sources 路径与新规则不一致的,按需逐步迁移(不强制)

### 提取职责边界

**始终调用 extractor,不在 WikiRan 侧另写兜底**。extractor 内部已实现 `mineru > docling > pymupdf` 三级级联——新论文(无 md)自动尝试全部引擎,MinerU 失败自动落 Docling,再落 PyMuPDF(pymupdf 兜底是 extractor 的职责,非 WikiRan 的)。WikiRan 侧不重复造兜底轮子,避免双轨制(外部兜底与 extractor 内部级联脱节)。

> 历史:2026-07-19 曾误写 WikiRan 侧 pymupdf4llm 兜底条款,导致摄入时跳过 extractor 直接兜底(low 档副本堆积)。已删除——extractor 内部级联已覆盖此场景。

---

## 标准 section 结构

> wiki 内容页正文统一 section 结构,支持 **section-level retrieval**(只把指定段送入 LLM 上下文以省 token,而非整文件读取)。工具:`.scripts/read_section.sh <page> <section>`;查询规则见 `operations/QUERY.md`「section 读取」。设计原则:**物理共置 + 逻辑分离**——导航与正文同文件(单一事实源,防漂移),LLM 用脚本按段截取(只把截取段计入上下文 token)。
>
> **术语说明(命名理由)**:导航段命名 `## Navigation` 而非 `## Abstract`,是为消除与页面类型 `paper-summary`/`meeting-summary`(整页忠实摘要)的摘要歧义——Navigation 是页面内导航段(供省 token 的 section retrieval 读取),paper-summary 是整页类型,两者是段 vs 页关系。中文描述用导航概述不用导航摘要,同理。

### 三个标准 section(顺序固定,所有内容页必备)

| section | 用途 | 预算 |
|---------|------|------|
| `## Navigation` | 导航概述:页面解决什么问题 / 核心结论(2-4 句),自足但不承担证明责任 | 80-200 tokens |
| (v4 删) 原 `## Core Triples` 段 | 关系路由改走 graph.db 图查询(边在 graph.db,不再有 md 段) | - |
| `## Content` | 正文,原有内容(子标题降为 `###`,如 `### 一、问题与动机`) | - |

> `sources` 不另设段,留 frontmatter(已有字段,单一事实源,防双份漂移)。

### 防退化规则(必守)

1. section 名精确匹配(`Navigation` / `Content`,大小写敏感,标题无附加文字)
2. 同级 `##` section 不重名
3. 代码块内禁止写 `##` 标题(避免 `read_section.sh` 误解析)
4. `read_section.sh` 找不到 section → 非零退出 + 列出可用 section,不静默降级全文
5. 截取失败绝不 fallback 整文件读取(否则 section retrieval 失效,退回"全文豪华套餐")

## 关键声明锚点

正文关键声明可加锚点标记 `{:#<slug>}`,支持段落级精确引用(`[[page#slug]]`),是证据保持与可回溯原则的落地手段。

- **语法**:声明行末尾加 `{:#kebab-case-slug}`,如 `## 一、TT 分解研究 {:#wang-qianran}`、`### 1.1 代码优化 {:#code-optimization}`
- **slug 规则**:kebab-case 英文,语义化(人名/术语/方法名),不与页面文件名重复(页面本身已可引用)
- **何时加**:关键结论/核心方法/重要数字/需被其他页面精确回溯的声明。非每段必加,按回溯价值选加
- **查询使用**:`[[papers/foo#self-attention]]` 锚点引用只取锚点所在段,支持 section retrieval 级省 token
- **现状**:全库已用 370 个锚点(academic/wiki/conferences、admin/wiki/meetings 等),惯例稳定

## graph.db 边写作约束（v4，2026-07-25 主数据化）

- 只保留导航级关系(提出 / 解决 / 对比于 / 属于 / 引用 / 采用方法 等谓词,见 graph.db edges)
- 不塞整页所有事实(实验数字 / 细节结论 / 限制条件留 `## Content`)
- 边只在 graph.db(主数据化,不再有 md Core Triples 段);ingest 时 graph_ingest 增量加边,graph_dump 人读

**学术建边核心导航判据**：graph 只提取核心导航，不存内容。导航关系（进图）回答谁/什么/在哪/属于什么/涉及什么；事实内容（留 `## Content`）回答怎么样/为什么/多少/什么逻辑。关系进图、数值不进图（对比关系有导航价值时只建导航边，如 `论文→对比方法→VMC`，数值留 wiki）。

**进图（核心导航）**：作者、期刊、单位、研究方向 Hub，以及有明确语义关系的概念/方法节点。

**不进图（留 wiki Content）**：实验数值、方法技术细节、证明逻辑、对比数值、背景叙述

**数量提示**：概念边数量只用于人工检查是否堆砌，不触发 Hub 归属、分裂或摄入阻断。

## 研究方向定位与 Hub Scope

论文 Wiki 必须有 `## 研究方向定位`，只写一句「研究对象 + 核心问题 + 方法或场景」，并引用 MinerU `paper.md` 的精确 locator。该句是程序路由信号，不写 Hub 名或分类结论。

active 研究方向 Hub 以稳定 path、简短 title 和必填 `## Scope` 定义身份。程序对 direction profile 与 Scope 做 embedding 候选召回；top-1 同时通过 threshold 和 margin 才建 `论文 Wiki → 主要研究 → Hub`，边 locator 指向研究方向定位句。歧义或 embedding 不可用时只返回候选，不写边、不报摄入 ERROR/WARN。

Hub 同时是 keyword、proposition、People page 等普通节点形成的可重叠动态群落。keyword 使用 `title + description`，proposition 使用 canonical statement，People 使用可 locate 的人物画像。程序结合 Scope 相似性、同类成员原型与图邻接 affinity，幂等维护 `普通节点 → 聚类于 → Hub` 派生边；一个节点可属于多个 Hub。新加入和保留使用不同阈值以防抖动，embedding 不可用时保留旧边。

概念/方法仍用 `研究基础`/`核心方法`/`对比方法` 等明确谓词连论文；套不上时不建事实边，不用「研究关键词」兜底。arXiv 方向只初始化根 Scope，不永久定义成员。新摄入不写 Hub seeds、`## 关键词` 或 catch-all 队列。未归类、歧义和动力学候选都是正常状态，不产生摄入 ERROR/WARN。Hub create/split/merge 只由主 Agent 确认，详见 `operations/HUB.md`。


## 关系级元数据

每条三元组可带**行内方括号 edge confidence** + 花括号来源元数据。两层正交:

```
- 主体 → 谓词 → 客体 [可追溯|推断|存疑] [SR] {authority: ...; temporal: ...}（来源：raw 路径#段落）
```

- **edge confidence(方括号,关系性质,2026-07-25 v3 重构)**:关系如何产生——`[可追溯]`(单篇 raw 直述,来源精确到段落,默认)/`[推断]`(跨篇推断或 LLM 拼接,含反哺产物)/`[存疑]`(低置信待审,失败显性化)。**独立判断,不继承页面级 confidence**(页面级 confidence 是来源可靠性 high/medium/low,边级是关系性质,正交两维)。摄入提取默认 `[可追溯]`,推断时显式标 `[推断]`。
- **[SR] 标记**:来自 `source_type: speech-recognition` 来源的三元组,与 edge confidence 共存(格式:`[可追溯] [SR]`)。从 authority 自动派生(替代手工标),不丢。
- **authority**(花括号):关系来源权威性(同 `source_type` 取值)
- **temporal**(花括号):关系时间有效性(current / deprecated / version:年份)

**默认继承**(减少标注量):花括号不标时从页面 frontmatter 继承:
- authority = 页面 `source_type`(用于 [SR] 派生)
- temporal = 页面 `status`
- **edge confidence 不继承**(独立判断,默认 `[可追溯]`)

**显式标注时机**:edge confidence 在关系为推断/存疑时显式标(默认可追溯可省);花括号仅在关系与页面级不一致时标。

**主数据化**(v4,2026-07-25):graph.db 是边唯一源(不再从 md Core Triples 段派生);edge confidence 存图 edges 表 `confidence` 字段;`[SR]` 从页面 source_type 派生。ingest 时 `graph_ingest.py` 增量加边,`graph_dump.py` 人读,`graph_build.py` 已废弃删除。

**与 Evidence Profile 的协同**:source_types/version_status/conflict_markers 可从关系级元数据判定(精度高于页面级推断);多来源一致性由 LLM 自检(非程序判定)。
edge confidence 为答案姿态提供信号:`[可追溯]` 可说"据论文X第Y段",`[推断]` 须说"综合X、Z推断"。

### 正文迁移(渐进)

现有页面的 `## 一、问题与动机` 等**二级中文编号 section 降为三级**(`### 一、...`),统一包裹在 `## Content` 内。渐进迁移:新摄入页启用;旧页按需(测试床涉及页优先)。过渡期 `read_section.sh` 遇旧页无标准 section 时报错退出(不静默 fallback);agent 看到报错后可显式选择整文件读取并知会用户"该页未迁移"。**脚本层防退化与 agent 层显式决策分离**。
