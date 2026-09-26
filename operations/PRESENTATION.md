# Presentation：证据感知的人机协作 PPT 制作

## 入口与成熟度

`presentation` 是当前 task/state 内按需组合的能力，不是新的持续工作状态。研究项目内制作汇报继续保持 `research`；接续已有项目时先读取对应 `notes/status.md`，再定位该 PPT 的创作记录。

```bash
python3 .scripts/route.py --capability presentation --capability-profile create
```

**当前阶段：1B 工程实现（真实用户三页验收与 PowerPoint 人工检查待完成）。** 阶段1A持久状态与单页闭环继续可用；现在支持从已锁定快照组装整套 PPTX、PDF 与预览，并提供独立的 PPT形式检查、PPT证据核查、PPT引用脱敏入口。整套 PPTX 最终组装不自动触发这三项辅助动作。只支持三个受限版式，任意模板尚不支持；只能经 validator/commit 写正式状态，不能手工改 JSON 伪造批准。工程测试通过不等于真实用户验收通过。

## 当前可执行动作

```bash
python3 .scripts/presentation_runtime.py doctor
python3 .scripts/presentation_runtime.py smoke
```

- `doctor`：检查当前 Python 的本地依赖与 LibreOffice 路径，输出 JSON，不创建创作文件。依赖可导入不等于渲染已验证。
- `smoke`：只生成一页固定合成样例（中英文字、矩形、绑定端点的连接线），调用现有本地 PPTX→PDF→PNG 链路，回读原生对象和 PDF 文字，保存独立运行回执。它不是用户 PPT renderer，不接受材料、提示词或任意输出路径。
- Python 使用当前解释器。优先加载已安装的 `python-pptx`；缺失时从本机 bundled runtime 查找，或由受信任的本地环境变量 `PRESENTATION_PPTX_SITE` 显式指定 site-packages。其依赖加载后不长期改写 `sys.path`；不安装包、不修改 `.env`、不复制第三方字体。
- PyMuPDF、Pillow 和本地渲染仍使用现有 `visual_qa` 内核；LibreOffice 定位与 fontconfig 继承保持原契约。缺依赖、生成失败或回读不匹配均返回非零，不能自动调用远程 API。
- 每次 smoke 新建 `temp/presentation-runtime/<run-id>/`，不覆写历史；该缓存可删除，不是事实源。成功回执仅表示机械链路通过，`visual_review=not_checked`、`user_approval=not_requested`；字体报告区分请求字体与 PDF 实际字体，不宣称 PowerPoint 跨平台完全一致。

阶段1B 整套导出入口为 `.scripts/presentation_delivery.py export --store <store> --expected-revision <revision>`；先用 `presentation_state.py status` 读取当前 revision，所有页必须已由用户接受预览并锁定。`presentation_delivery.py status --store <store>` 列出持久成品/报告及未提交目录。三个辅助入口分别为 capability profile `form-check`、`evidence-check`、`citation-redact`，仅用户触发时按需加载；导出不自动加载它们。完整命令与结果 schema 按需读取“阶段1B 导出与按需操作协议”。

阶段1A 入口为 `.scripts/presentation_state.py`：`example-plan` 给出协议样例，`init` 在已有 `projects/<workspace>` 建包，`prepare --kind content|design` 返回 `agent-task-v1`，`check/commit` 共用 validator，`status` 返回 revision、逐页快照和实际预览路径。具体命令/JSON 按需读取本文件“阶段1A 命令与数据协议”，不要猜测请求字段。

只支持 `title`、`image_text`、`flow` 三个固定版式；图片是独立栅格素材，正文和普通框图使用原生对象。每次批准绑定 `status` 的快照哈希和当前 revision；独立 API 检查实际预览后记录 `review`（新流程用 `review-api`），失败视觉审阅不能锁定；用户明确接受预览及原始警告后才能 `lock`。`build` 只接受空参数并调用本地 renderer，不接受手写“通过”回执。普通文字提纲仍按 `write`，旧图片/PDF重建保持独立。

## 创作边界

- **用户主导最终检查**：PPT形式检查、PPT引用脱敏仅用户明确指令触发；可以提醒，不自动执行。PPT证据核查仅用户对具体内容有疑问时按范围执行。三项独立、默认不执行，均不作为默认交付门禁；详细约定见“按需辅助检查”。这不取消制作阶段已有逐页确认、机械校验、来源记录和隐私边界。
- 宿主 Agent 理解用途、检索证据、组织叙事、提出设计并与用户交互；视觉工作交给独立 `pptx_visual` API，宿主只消费文字结果。shared 程序负责 schema、状态、几何、生成、机械检查和事务，不转交 DSH 控制循环。
- 需要语义暂存产物时，沿用 `agent-task-v1` 的 inputs/outputs/protocol/issues/check/commit/resume，不增加 DSH、`llm_structured` 或模型型 planner/critic。内容与设计只经原 validator/commit 进入创作状态。
- Raw 是科学与机构事实的权威。Wiki 与 graph 只用于发现和到达；主张、数字、图分别保存来源 locator 与来源版本，不把“路径存在”当作“主张得到支持”。用户提供但未核验的材料标记 `user_provided`，不伪造 Raw 引用。阶段1A仅接受 user_provided/unresolved，不把文件存在或 hash 匹配自动认证为 raw_verified；保留来源记录不等于自动逐项核查。PPT证据核查仅在用户对具体内容有疑问时执行，不默认生成全套证据映射。
- 私有来源的敏感性沿素材、IR、预览、成品和回执继承。输出路径变化不解除限制；未来远程上传须独立、明确授权，发布版引用不得泄漏本地私有路径。创作包仍为 local_only；`--allow-remote` 仅授权当前选定视觉输入上传，不是公开发布或其他来源的许可，private 来源不经本入口上传；smoke 无用户来源。
- 正文和标题优先 native text；普通框图使用 native shapes/connectors；表格和有明确源数据的受支持图表逐步原生化。科研图片可以独立嵌入；不从图像估读并重造数据，不默认整页图片化。
- 现有 `slide-library` 及图片/PDF 重建保持独立、不迁移、不重构。旧页整页拷贝与 IR 重建不是同一种能力；任意模板、现有 deck 无损编辑、动画、SmartArt、复杂公式和第二 renderer 不在首版承诺内。

## 逐页制作与确认纪律

**默认一步一页：宿主一次完成当前页，用户只确认实际整页。** 全局提纲、页序、故事线及素材定位可以提前准备；制作按 `slide_order` 推进，用户接受当前页实际预览并锁定后，才进入下一页。内容、设计、生成和制作阶段API视觉审阅合并在同一轮工作内，不逐项询问。用户要求调整时直接修改当前页并再次展示。

默认交互流程：**完成当前页 → 展示实际页面与必要问题 → 用户确认整页 → 锁定并制作下一页**。仅用户明确要求分阶段参与时，才分别提出内容或设计问题；真正缺少影响本页的信息时只询问该缺口。

内部仍沿用现有状态事务：内容提交 → 内容实施授权记录 → 设计提交 → 设计实施授权记录 → build → review-api → 用户实际预览接受 → lock。用户要求“一步一页”构成宿主完成当前页内容和设计的授权；`approve-content`、`approve-design` 的 `confirmation_source` 如实记录这项授权和当前页范围，不能写成用户分别审阅并确认了未展示的内容或设计。已有明确内容确认按实际来源保留。shared 校验字段、快照和页内前置条件，不识别人类确认语义，也不提供跨页调度门禁。

“开始制作”“继续”等流程指令可推进当前页制作，但不能记作接受尚未展示的实际预览。全局方案确认不产生各页预览接受。宿主不得循环代签后续页或批量锁定，也不得用 API 通过代替用户确认。用户另行明确授权批量制作时，准确记录页码、版本与范围；尚未展示的实际预览仍不能记作已被用户接受。

发现授权记录失实或超出范围时立即停止后续生成，按受管提交与失效机制纠正，保留历史并说明结果。预生成页只作为草稿，不能因文件存在而跳过用户整页确认。恢复任务先读 `status` 的页序与批准快照，从当前未完成页接续。

此交互规则依据用户2026-09-25“每页的制作不要分多步，一步一页”的明确要求，替代此前对每页内容、设计分别提问的安排；逐页推进与实际预览由用户确认保持。

## 学术论文报告的叙事与风格

适用于介绍研究论文、突出工作亮点的学术报告；由宿主 Agent 在内容规划时使用。`presentation/create` 加载本节，其他用途按需取舍。它是可复用的创作经验，不增加程序校验、批准节点或自动辅助检查。用户当前要求优先。

### 已明确的创作偏好

- **用故事支撑意义**：从论文引言及关键引文出发，先说明问题为何重要、已有研究走到哪里、挑战是什么，再引出本工作的思路与结果。专业同行也需要这一研究动机；背景深度随受众调整。
- **把创新与意义讲成因果链**：明确哪些概念与方法属于已有基础，本工作推进哪一步，这一步如何帮助理解物理或完成任务。以“问题 → 挑战 → 新思路 → 关键结果 → 意义”组织报告，结尾回应开场问题。
- **让重要文献进入页面论证**：对应页面显示可读的作者、期刊与年份等短引文，并在讲解中说明其作用。支撑核心动机或创新对照的引用进入屏幕内容，不能仅留在讲者备注或末尾清单。
- **突出理论工作的操作意义**：提出新量、新框架或新判据时，解释其定义为何这样选择、关键证明建立了什么联系、能用于判断或解决什么问题。严格证明、数值支持与推广展望分别标清适用范围。

### 可调整的制作技巧

| 环节 | 做法 | 使用范围 |
|---|---|---|
| 开场故事 | 短篇论文报告可用2–3页依次回答“为何重要／为何困难／本文如何切入”，每页推动一个问题 | 是起点建议；按报告长度、受众和工作性质压缩或展开 |
| 文献选择 | 优先选与动机、概念来源、方法基础和创新对照直接相关的引文，每页通常1–2条 | 以论证需要和可读性决定数量；期刊声望或引用数量不能代替创新论证 |
| 亮点层级 | 给关键定理、核心图及其意义更多讲解时间；技术推导、优化细节和扩展例子按需移入备注或备用材料 | 不按论文各节长度平均分配时间 |
| 页面语言 | 标题表达本页问题或结论；正文保留必要定义、条件和关键信息；推导过程、过渡句放讲解提示 | 公式首次出现时解释符号，读图先交代坐标、比较对象与结论 |
| 配图 | 优先使用最能支撑本页论点的论文原图或原生示意；同一图重复出现时明确新的讲解焦点 | 保留图例与适用条件，不从图片估读并重造数据；视觉交给既有独立API流程 |
| 时长 | 为每页设预算并求和，短报告可预留约10%的余量；扩充背景时同步合并重复页或压缩次要内容 | 预算须经试讲校准；先区分总时段是否包含问答，固定页数不代表固定时长 |
| 模板 | 先确定本页的表达任务，再选直接使用型或套用填充型模板 | 模板应服务内容；沿用模板时仍按其用途、替换槽与来源约束执行 |

### 文献与创新的写作尺度

背景引用说明已有基础；创新性通过“已有基础 → 本文推进 → 结果支持的意义”具体展开。论文引言及参考文献表可用于定位候选；书目核对与独立原文阅读是不同状态。涉及关键对比、具体定理、资源界或“首次”等主张时，定向阅读相关原文；尚未读到时收窄表述并在创作备注中保留阅读状态。不要把作者自己的概括当成已完成的独立优先权核验。

准备内容所需的文献阅读与来源记录仍按现有写作契约执行，不自动触发“PPT证据核查”或生成全套证据映射。“PPT形式检查”“PPT引用脱敏”仍仅用户明确指令触发；“PPT证据核查”仍仅针对用户的具体疑问。

### 应用与经验来源

形成内容提案时，给出故事线、逐页表达任务、可见引文、拟用图件、讲解提示及时间分配，明确文献如何支撑动机与创新。修改提案时同步受管内容与页序，保留版本，不把讨论建议记成用户批准。

本节来自2026-09-24至25日PRL报告内容讨论及用户要求的经验沉淀。已明确的偏好是背景故事、创新意义和页面可见引文；技巧属于本次讨论提炼的建议。中文页面配英文术语、专业同行受众，以及“12页／18分钟”属于当次设置，其他报告重新确定。经验目前仅经过内容规划讨论，尚未经过实际演讲或成品视觉验收，不作为科学事实源或效果已验证的模板。

## 第一页（标题页）制作流程

标题页用于建立第一印象，让听众认出报告主题并记住一个最有吸引力的贡献。论文报告按以下流程制作；其他类型报告将“论文贡献”替换为本次报告最值得记住的信息。以下是宿主 Agent 的页内工作顺序，**合并为一步完成当前页，只向用户交付实际整页确认**，不增加内容、选图或设计的逐项批准。

1. **先总结贡献，再选择视觉焦点。** 从已定位的原始材料准确提炼“提出了什么、推进了什么、为什么重要”，区分严格结果、数值诊断与展望。选择一个最能吸引目标听众、又能代表工作意义的亮点；主视觉不求面面俱到，也不默认以最容易画的具体定理代表整篇工作。
2. **把亮点转换为可见关系。** 用对比、连接、变化或关键结果回答一个直观问题，突出一处视觉中心，辅助对象退到次级。优先选择有来源的关键原图或可编辑的原创图形；配图承载贡献，不只是装饰。概念图保留正确关系，不虚构数据、数值阈值或普适最优结论，科学上不可省略的条件仍清楚表达。
3. **压缩为标题、主视觉、署名三组信息。** 保留必要的主副标题、报告人、单位及作者/成果归属；期刊年份、会议信息按用途取舍。长英文论文名可简化为主题短名，完整书目信息放记录或后续页。背景故事、研究摘要、推导、重复解释和次要页脚移到正文或备注。页面默认不出现“示意”“概念示意”等制作说明词，图的性质、来源和适用范围保留在制作记录中；确实影响科学理解的限定不能随之丢失。
4. **以美观和辨识度组织版面。** 建立清楚的字号、亮度和留白层级，用少量主题色突出主视觉；背景、图形与文字共同服务主题。根据内容选择版式和模板，避免把全部信息塞进固定标题框与正文框。左文右图、深色背景、高亮中心与弱化两侧是可选手法，颜色、布局、图形形式均随报告调整。
5. **生成实际文件并完成制作期反馈。** 主 Agent 负责科学内容、设计目标及整页整合；图片可由下述 Agent/API 选项创作，独立视觉 API 检查真实预览，除截断、重叠、字体和符号外，还评价构图、层级、视觉焦点与投影可读性。使用 HTML 等中间格式时，同时核对实际导出的 PPTX；回读确认标题、正文及普通图形的原生可编辑性。API 意见结合原始材料判断，不因模型不熟悉 DOI 或专业画法就改动正确事实；“无溢出”或 API pass 不等于美观合格或用户接受。
6. **展示当前整页并按用户反馈收敛。** 交付实际 PPTX 预览及可编辑文件，保留可回退版本；用户的小改动落实到当前页后再展示。“整体认可但要求调整”记录为认可设计方向及当前修改授权，不代签更新后的预览。实际整页接受与锁定仍按“逐页制作与确认纪律”执行，然后才进入第二页。

### 图片创作方式（Agent / API LLM 主导）

制作时可选择“主 Agent 主导图片创作”（默认）或“API LLM 主导图片创作”。用户选择在当前会话后续迭代持续有效，不逐页重复询问。两种方式都先由宿主提炼科学贡献、来源、适用范围和设计目标。

- **Agent 模式**：宿主完成图形创作，程序只提供协议、校验和渲染，不隐式调用 API。
- **API 模式**：宿主提交来源约束与设计简报，由 API 自主决定视觉隐喻、构图、文字和图元几何，输出 `presentation-artwork-v1`。宿主整合页面；不得由宿主预先画好再把 API 点评称为 API 创作。修改构图时重新请求 API 并保留版本；机械渲染不改动模型图元。
- **模型与产物**：`PRESENTATION_ARTWORK_MODEL=GLM-5.3-FlashX`（2026-09-26 用户指定）。经 chat/completions 生成有限矢量图元 JSON，再导出 SVG 和可转换为原生 PPT 对象的 HTML。该选项目前适合学术关系图、框图、贡献图，不把 GLM 宣称为原生位图生成端点。实际 PPT 原生对象需导出后回读验证。
- **检查独立**：`VISUAL_QA_MODEL=GLM-5.3-FlashX`；即使创作与检查同一模型，也使用独立调用审阅实际预览。主 Agent 读取文字意见并核对科学依据，不承担视觉检查。API 检查不代替用户验收，不自动触发最终 PPT形式检查、引用脱敏或证据核查。

入口：`python3 .scripts/presentation_artwork.py generate --brief <brief.json> --creator api --allow-remote`。不指定 `--creator` 时返回 `agent-task-v1`。简报字段为 `schema=presentation-artwork-brief-v1`、`canvas=[宽,高]`、`sensitivity=public|local_only|private`、`brief` 和 `sources`。API 模式只接受明确允许远程的公开输入；不自动读取或上传完整来源。创作推理档位由 `PRESENTATION_ARTWORK_REASONING_EFFORT=high` 控制（可设 low）。独立模型、API base/key 可用 `PRESENTATION_ARTWORK_*` 配置，base/key 缺省复用 LLM 配置。

所有候选、请求、导出及哈希回执留在 `temp/presentation-artwork/<id>/`；Agent 写候选后运行任务中的 `render --run` 完成机械校验与导出。此处 commit 仅提交图形衍生产物，不是正式 PPT revision/用户批准。协议禁止任意代码、外链、越界对象和未知字段；失败不自动改由宿主代画。按页完成实际 PPTX 与独立 API 反馈后，一次交付整页。

### 本次迭代的经验与适用范围

2026年PRL报告的标题页经历了“固定文本框 → 背景与MPS/线路等价图 → 信息精简 → 深度匹配这一主要贡献的可视化 → 去掉‘示意’字样”。用户认可的是有主题图形、信息克制、主要贡献醒目，并明确要求图中不出现制作说明词。最终以“线路多深，才合适？”和“不足／匹配／冗余”的对比突出MPE标度诊断用途；这些科学内容只属于本报告，不作为其他报告的固定图案或结论。

PPTAgent 的 HTML→可编辑PPTX 路径改善了本次单页效果，但底层库不能替代贡献提炼和版面设计。它目前是项目内隔离试验，不能据此宣称正式 renderer 已支持任意版式或绕过受管批准事务。本节固化的是宿主创作流程，不新增 shared validator、全套检查门禁或强制工具；PPT形式检查、PPT引用脱敏和PPT证据核查仍按各自触发条件执行。

## 持久状态与权威（阶段1A已实现）

选定已有的 `projects/<workspace>` 一级工作区后，在其 `presentations/<presentation-id>/` 保存创作包。ID 随机且稳定；slide ID 与页序分离，不使用中心顺序号。

```text
presentations/<presentation-id>/
  current.json                 # 唯一已提交 revision 指针
  revisions/<revision-id>/
    session.json               # 整体设置、页序、批准/锁定快照及父 revision
    deck_plan.json             # brief、叙事线、各页任务与整体风格
    slides/<slide-id>/
      content.json             # 本页主张、文字、素材引用、证据、备注
      design.json              # 版式、强调方式及受约束参数，不复制事实正文
      ir.json                  # 编译后的对象、坐标和素材版本
      build.json               # 本地构建检查与实际依赖/字体指纹
    receipt.json               # 本次提交的操作者声明、变更及验证
  assets/<sha256>               # 原始素材字节快照，非 Raw 替代事实源
  assets/<sha256>.pptx|pdf|png  # 单页构建及批准预览字节快照
  .writer.lock                 # 本机进程锁文件，不删除/换 inode
  outputs/<build-id>/           # 不可变成品/衍生版本/报告，receipt + seal 绑定文件哈希
  requests/<request-id>/        # 持久用户指令、范围、问题及版本快照，候选结果仍暂存在 temp
```

当前通过受管 init/commit 创建 revision 和 assets；delivery 在同一个本机进程锁内向 outputs/requests 写完整临时目录，fsync 后原子发布。`.pending-*` 只报告，不自动恢复或按 mtime 提升；输出失败不改变 current.json。临时编辑与可重建渲染放 `temp/presentation/`，用户批准的预览及依赖随创作包保留；清除缓存不会丢失批准依据。assets 中的 PPTX 是单页快照，不要用 PowerPoint 原地覆盖；另存为新材料再处理。

- `current.json` 指向的不可变 revision 是页面制作状态的唯一权威；状态不能靠可编辑布尔字段、聊天记录或 temp 文件恢复。时间戳用于审计，不用于判定内容一致性。
- 工作区事项只记整体进度及创作包入口，不再复制逐页批准。`notes/status.md` 仍是 workspace 工具管理的投影，不手写第二套真理源。
- 写入先检查 schema、路径、素材哈希和 expected revision；单写者锁内校验并构建新 revision，完成后原子切换指针。失败时旧指针仍有效；未提交 revision 不作为成功。恢复遇到冲突、损坏或外部改写必须报告，不按文件 mtime 猜测最新状态。
- 多文件提交不靠“依次原子写每个 JSON”冒充事务。进程中断、并发旧 revision 提交、损坏 receipt 等都必须有回归测试。
- 成品是派生产物，不自动写入 Raw/Wiki/公共图；用户要求收录时再走受管摄入。外部 PowerPoint 修改后的文件视为新版本输入，不反向覆盖已批准 IR。

## 最小协议与批准（阶段1A核心已实现）

第一版使用创作协议族 `presentation-v1`；内部编译产物为 `presentation-ir-v1`，本地构建回执为 `presentation-build-v1`，不独立部署 IR 服务。下表描述责任，当前精确字段以本文件下一节和共享 validator 为准：

| 对象 | 最少字段与责任 |
|---|---|
| Session | schema、presentation_id、revision_id、parent_revision、approval_mode=strict、slide_order、source_policy=local_only、approval_records；持有唯一批准记录 |
| DeckPlan | 用途、受众、语言、时长、核心信息、叙事顺序、主题版本；每页稳定 ID、表达任务及时间预算 |
| SlideContent | slide_id、标题、正文、素材/证据地址和备注；阶段1A仅接受 user_provided/unresolved |
| SlideDesign | slide_id、layout_id/layout_version、image_source；theme 由 DeckPlan 持有并参与设计快照，不提供任意代码、XML 或命令 |
| SlideIR | slide_id、画布、稳定 object ID、受支持类型、bbox、文本/素材哈希、样式、连接端点、editability/fallback 原因 |
| BuildReceipt | 输入快照、依赖指纹、实际对象统计、渲染产物哈希、分层检查与最终批准引用；不持有第二份可变批准 |

全局方案可一次确认。默认交互为一步一页：宿主合并完成当前页内容与设计、编译/生成/机械检查、独立API审阅真实页图（宿主只消费文字结果），再由用户确认实际整页并锁定，随后进入下一页。内部内容/设计实施授权与用户预览接受分别记录，具体范围见“逐页制作与确认纪律”。

批准事件记录 `kind`、`actor`、用户确认来源说明、`expected_revision`、`approved_at` 与 `snapshot_sha256`。内容批准绑定内容与证据版本；设计批准再绑定布局、主题、素材及内容；成品批准再绑定 IR、renderer、实际字体/配置、预览和检查结果。使用规范化 JSON 与原始素材字节计算 SHA-256；状态检查不得只看 `approved: true`。

**信任边界**：hash 只能证明批准针对哪个快照，不能证明用户真的说过“同意”。宿主 Agent 负责如实转录显式确认，不能替用户签批；程序拒绝缺失确认来源或快照不符，但不声称有独立的人类身份认证。

| 变更 | 必须失效/拒绝的范围 |
|---|---|
| 内容或证据版本变化 | 内容批准及全部下游批准、IR、渲染、QA |
| 仅设计变化 | 保留内容批准；设计批准及下游失效 |
| 素材、theme、layout、renderer、字体/渲染配置变化 | 依赖这些版本的批准/构建失效；先给影响清单，不静默处理锁定页 |
| 修改其他独立页面 | 已锁定页面内容、设计、IR、素材和批准快照不变 |
| 页序/页码/总页数变化 | 全局输出批准失效；检查会改变的页码对象并显式重新确认受影响页 |
| locked 页普通修改、重建或整体美化 | 拒绝；仅用户显式解锁后进入新 revision，历史快照保留 |

状态使用少量事实与派生阶段，不维护互相矛盾的 `state`、`locked`、多个可改 approval 布尔值。validator 由上述快照关系推导 `content_review / design_review / build_ready / preview_review / locked`；损坏或过期则明确返回阻断原因。schema 拒绝未知版本/字段、重复 ID、悬空连接、非法几何、外部写路径；阶段1A没有整页栅格回退入口。

## 阶段1A 命令与数据协议

所有命令在仓库根执行。先选定**已有**一级项目，真实接续项目仍先读其 notes/status.md；不为测试向活跃项目写入伪批准。

```bash
python3 .scripts/presentation_state.py example-plan
# 将输出作为 plan.json 草稿，按真实用途调整；init 不代表用户确认内容
python3 .scripts/presentation_state.py init --project projects/<workspace> --plan <plan.json>
python3 .scripts/presentation_state.py status --store projects/<workspace>/presentations/<id>
python3 .scripts/presentation_state.py prepare --store projects/<workspace>/presentations/<id> --slide s1 --kind content
# 当前宿主填写 task 指定的 request.json；原 validator 先检查，再提交
python3 .scripts/presentation_state.py check --store <store> --request temp/presentation/<task>/request.json
python3 .scripts/presentation_state.py commit --store <store> --request temp/presentation/<task>/request.json
```

- JSON 拒绝未知字段、重复键和 NaN/Infinity。请求严格含 `schema: presentation-v1`、`expected_revision`、`op`、`slide_id`、`payload`；全局操作 slide_id=null。每次成功提交都产生新 revision，后续请求必须重新读取，不复用过期 expected_revision。
- `init` 的 plan：schema、purpose、audience、language、duration_minutes、key_message、theme、slides。theme 含 id/version/font/background/foreground/accent（颜色为6位RGB）；slides 是稳定 ID → {task,time_budget_seconds} 的对象。初始页序由输入顺序确定，随后由 session.slide_order 权威保存。当前不支持新增/删除页，修改目的/叙事请 set-plan；主题改动使设计下游失效，其他 brief 变动使相关内容下游失效。
- `set-content` 的 payload：schema、slide_id、title、body（1–6个字符串）、notes、sources。source 含 id/path/sha256/locator/evidence_status/sensitivity。path 是本仓库 Raw 或 projects 下显式材料的相对路径；拒绝符号链接、隐藏/配置文件、创作包自身和不支持类型；单文件不超过50 MiB。来源变更必须更新 hash 并重新批准；旧素材字节不覆盖。所有派生物继续 local_only，即使 sensitivity=public 也无自动上传能力。
- `set-design` 只在内容已批准后接受 payload：schema、slide_id、layout_id=title|image_text|flow、layout_version="1"、image_source。前两项为一般字段；image_source 在 image_text 中指向本页 PNG/JPEG/WebP source ID，其他版式必须 null。flow 需要2–4条 body，每条一个原生矩形，使用绑定两端的原生连接线。title 是标题与文本区，并非任意封面设计器。
- `approve-content`、`approve-design`、`lock`、`unlock` 的 payload：actor、confirmation_source、snapshot_sha256、accepted_warnings。确认来源必须如实说明用户当前哪条明确确认；快照从 status 对应 content/design/preview 取得。除 lock 外 accepted_warnings 通常是 []；lock 必须原样接受本次 build 的警告列表。unlock 也须明确用户确认，不能因想修改就自动解锁。
- `build` 的 payload 必须是 `{}`，只有内容和设计均获准且未锁定才运行。生成/转换失败时 current 不变。程序记录请求字体、PDF实际字体，以及 renderer/依赖/字体文件清单 hash；需要本机 fc-list，缺失/指纹改变时不得继续确认旧图。单页输出与实际预览放 assets，status 给出路径，不通过外部回执导入成功标记。
- `review` 保留旧宿主记录兼容；新视觉流程调用 `review-api --store <store> --slide <id> --expected-revision <rev> --allow-remote`，程序写入真实 API 回执：除 actor/confirmation_source/snapshot_sha256/accepted_warnings 外，含 verdict=passed|failed、findings（字符串数组）。failed 至少一条问题并阻断 lock；需要修复时回到设计/内容提交、重建和重审。API 回执与实际 PPTX/PNG、结构快照绑定，不能把机械 pass 转录为“已视觉审阅”；API 通过不产生用户确认。
- `reorder` 的 payload 是 {slide_order:[...]}；当前保守拒绝涉及任何 locked 页的全局主题/页序操作，先列出 affected locked slides，再由用户明确解锁。重排清除单页构建/视觉批准，内容/设计不自动重写。尚无页码对象与整套输出批准。
- `check` 与 `commit` 走相同 schema/批准前置校验；check 不渲染、不提交 revision、不摄入素材，不能预告实际构建一定成功。commit 返回 committed 和 issues，依赖复验以 status 为准；status=blocked 的 CLI 退出码非零。只影响单页的操作不隐式重建其他页面。
- 恢复依据 current.json → receipt hash → 全部 revision 文件 hash 与资产 hash，不按 mtime。进程中断可能留下 `.pending-*` 或未引用 revision，status 只报告 uncommitted_revisions，不把它们自动变成成功；current 已切换则读完整新 revision。坏 receipt/素材/外部改写应报告，保留现场并从可信备份恢复或作为新材料处理，不覆盖旧批准。
- 本实现使用本机 POSIX flock、fsync、原子 rename；**不是多台电脑/Synology同步副本之间的分布式锁**。同一创作包只在一个设备写入；同步冲突或存储介质不支持相同文件系统语义时先停写。发布/交付前仍需真实 PowerPoint 打开与编辑验收。

## 分层检查与最终交付（工程已实现，用户验收待完成）

1. 结构 lint：当前支持对象/引用有效性、越界、连接端点和字体范围，图片用 contain 保持比例。长文字仍可能溢出；PDF文字回读只是机械门禁，不代替查看图像。警告与确定性错误分开，不按 bbox 武断否定合理叠放。
2. 本地实际渲染：复用 PPTX→PDF→PNG。仅本地确定性检查通过不等于视觉审阅通过；未检查、失败、警告分别保留。
3. 独立 API 预览审阅：`review-api` 看实际图像后记录 verdict=passed|failed、findings 与 api_receipt；failed 必须说明问题且阻断 lock。审美建议与可见缺陷分开，不能静默修复锁定页。
4. 用户接受真实预览。严重结构错误和渲染失败不能通过用户批准绕过；可接受警告需明确记录，不改写原 QA verdict。

**阶段1B 整套导出已实现：** 最终 deck 从已批准快照生成，不拼接不受控单页二进制。默认保留生成所需的结构、文件完整性与页数/页序等机械校验，不自动逐页对比最终可见画面、全文核查证据或改写引用。最终检查以用户为主；交付可简短提醒用户自行查看，并说明可单独请求下列辅助功能。锁定绑定批准快照，不承诺 PPTX ZIP 或带元数据 PDF 的逐字节相等，也不把未经复核的整套成品称为已复核通过。

默认交付 PPTX、构建 receipt 与已生成的预览，明确三项按需检查未执行；证据映射不是必交附件。object counts 只能说明组成，不能用可编辑对象比例掩盖关键科学图不可编辑。跨平台 PowerPoint 打开、编辑与字体检查由用户进行，LibreOffice 检查不能代替它。发现的确定性错误仍须报告，不因检查可选而隐瞒。

## 按需辅助检查（独立入口已实现）

三项使用独立名称，可单独指定范围，不互相连带触发；中文名称是用户对话入口；CLI 使用独立 profile/kind：`form-check`、`evidence-check`、`citation-redact`。

| 独立名称 | 触发方式 | 执行范围与结果 |
|---|---|---|
| **PPT形式检查** | 仅用户明确要求时执行；交付时可提醒 | 对指定页或明确要求的整套成品，与批准预览核对可见文字、布局、图片、字体及页序，检查遮挡与溢出，报告形式问题及预览差异；不检查事实依据，不自动修改或重新批准页面。例如：“做一次PPT形式检查，只查第3–5页。” |
| **PPT证据核查** | 仅用户对具体内容有疑问时执行；用户直接问出处或依据即构成触发，无需再说固定口令 | 追溯所疑问的主张、数字或图片，给出来源版本、具体位置及是否支持该表述；不扩大为全套逐项审计。例如：“第4页这个数字哪里来的，是否有依据？” |
| **PPT引用脱敏** | 仅用户明确要求时执行；准备对外分享时可提醒 | 按用户指定范围处理原生标题、正文和备注中引用的内部路径、私有标识等，生成独立衍生版本并保留原件；当前不改图片像素、不保证完整隐私审计；不连带触发形式检查或证据核查。例如：“只做PPT引用脱敏，其他不检查。” |

未请求的项目记为“未执行”，不写成 passed，也不以此阻止默认本地交付。用户说“制作/导出PPT”不等于授权三项辅助检查。制作阶段仍保留必要来源 locator、版本及未核验标记，不编造来源或认证；不执行引用脱敏不表示允许泄密或发布，私有来源仍保持 local_only，脱敏也不构成内容公开授权。检查中发现问题应报告；需要修改已锁页面时仍按显式解锁/重批流程处理。

## PPT形式检查入口

仅用户明确要求时加载 `presentation/form-check`；可以提醒，不自动做。将用户页码转换为指定 output 的稳定 slide ID，准备独立 `agent-task-v1`：

```bash
python3 .scripts/presentation_delivery.py prepare --store <store> --expected-revision <revision> --kind form-check --output-id <output-id> --slides s1 s2 --instruction '<用户的真实检查指令>'
```

当前 Agent 调用任务 refresh 命令（`form-api --result <result.json> --allow-remote`），由独立 API 同时接收实际成品 PNG 和对应批准 PNG；宿主只读文字结果。仅检查请求页的排版、字体、图片、遮挡/溢出、页序与预览差异；不检查事实依据，不顺带脱敏，不替用户确认。按任务的 check/commit 提交逐页发现；详细结果 schema 按需读取“阶段1B 导出与按需操作协议”。API 未完成时返回 partial，不伪造通过；页面未批准的新衍生版本不能靠形式检查继承原预览批准。

## PPT证据核查入口

仅用户对具体内容提出疑问时加载 `presentation/evidence-check`；用户直接问出处即构成触发，不要求重复口令，不默认全量审计。无需先导出或锁定，页面已存在内容和来源即可：

```bash
python3 .scripts/presentation_delivery.py prepare --store <store> --expected-revision <revision> --kind evidence-check --slides s1 --instruction '<用户的真实提问>' --question '<具体疑问>'
```

当前 Agent 仅核查疑问范围，沿声明的来源回到原始材料，区分有出处和支持结论。程序只核对来源快照哈希、明确页/行位置及逐字引文，不判断逻辑支持，不把项目材料认证为 Raw。文本来源用 lines:N-M，PDF 用 page:N；扫描页/图片不能用文本引文证明时报告 insufficient 并说明需视觉核对或补充材料，不能编造引文。需要新来源时先走原内容更新/批准流程；不直接修改 Raw。结果按任务 check/commit 保存，详细 schema 见“阶段1B 导出与按需操作协议”。

## PPT引用脱敏入口

仅用户明确要求时加载 `presentation/citation-redact`；对外分享前可提醒，不自动做：

```bash
python3 .scripts/presentation_delivery.py prepare --store <store> --expected-revision <revision> --kind citation-redact --output-id <output-id> --slides s1 --instruction '<用户的真实脱敏指令>'
```

当前 Agent 在指定页原生文本引用槽位提出完整字段 before/after；程序验证目标范围与旧值，重新生成独立 PPTX/PDF/预览，不改变锁页或原件，不触发形式检查/证据核查，不继承用户批准。当前不处理图片像素、任意外部 PPTX、隐藏的第三方对象，也不保证清除了全部敏感信息。成品及内部回执仍为 local_only；回执/快照保留源上下文，不能将整个输出目录当公开包。脱敏不等于公开授权。详细 schema 按需读取“阶段1B 导出与按需操作协议”。

## 阶段1B 导出与按需操作协议

`.scripts/presentation_delivery.py` 的提交和导出是 shared 确定性入口；`form-api` 显式调用独立视觉 adapter。当前宿主负责理解用户指令、消费文字检查报告、判断证据与提出引用替换，不能把 `--instruction` 存在当作已验证真实人类授权。

```bash
python3 .scripts/presentation_state.py status --store <store>
python3 .scripts/presentation_delivery.py export --store <store> --expected-revision <revision>
python3 .scripts/presentation_delivery.py status --store <store>
# 仅用户指令/疑问触发后，用上面的独立 prepare；按 task 给定路径填写 result.json
python3 .scripts/presentation_delivery.py check --store <store> --result <result.json>
python3 .scripts/presentation_delivery.py commit --store <store> --result <result.json>
```

- **导出**：所有页已锁定、来源版本及渲染依赖有效；从批准的 content/design/IR 按 slide_order 原生组装，不读取单页二进制拼接。保存 PPTX、PDF、逐页 PNG、对象数/页数机械回执；不自动评判画面或逐项查证。`assistance` 的三个 kind 均为 not_executed，`user_approval=not_requested`。生成预览不是执行形式检查。
- **持久事务**：同一 Store 本机 flock；outputs/<id> 和 requests/<id> 均有 receipt.json 文件清单与 seal.json 回执哈希，完整写入后原子 rename，不覆盖已发布目录。读取核对文件集合与哈希，外部修改视为新输入而不是同步回写。check 不提交、不渲染；commit 再跑同一 validator。请求只能提交一次；状态/源版本变化必须重新 prepare。中断后看 status：完整输出保留，pending 不自动提升。哈希用于完整性，不是对抗拥有目录写权限者的签名认证。
- **prepare**：显式 kind、expected revision、非空且无重复 slides 和真实 instruction；form-check/citation-redact 必须指定 output-id；evidence-check 必须有 question，可不指定 output。请求范围与 base output 回执哈希持久保存；用户只要求导出不构成三项授权。候选结果位于 temp；删 temp 后可重新 prepare，但不可伪造旧结果已提交。
- **公共结果字段**：严格 `schema=presentation-assistance-v1`、request_id、intent_sha256、actor、summary、items。前三项由 prepare 给出，不改写；actor 为实际执行者声明（宿主或 API），summary 如实说明检查范围与限制。所有检查都不产生用户批准，执行状态 executed 不等于结论 passed。
- **形式 items**：每个请求 slide ID 恰好一项，字段 `slide_id, verdict, findings, inspected_preview_sha256, approved_preview_sha256`。verdict 为 passed/issues_found/unable_to_check；后两种必须有 findings 字符串列表。两个哈希分别取实际成品 PNG 与批准 PNG，新 API 结果另含 `api_receipt`，程序核验 API 身份、源/页图/结构/批准图绑定和结果一致性；旧宿主格式保留历史兼容，不自动伪装成 API。只保存报告，不修改页面。
- **证据 items**：每个请求页至少一项，字段 `slide_id, claim, verdict, reason, evidence`。verdict 为 supports/contradicts/insufficient；前两种必须有 evidence。每条 evidence 严格含 `source_id, locator, quote`，source_id 必须在该页 sources 中；UTF-8 txt/md/csv 的 locator 为 lines:N-M，PDF 为 page:N，引文必须逐字出现在该范围。source hash/locator/逐字引文验证不是语义支持认证；报告保留宿主判断及未核验状态，不写 raw_verified。图片/扫描等当前不可做逐字校验时用 insufficient 明示限制。
- **脱敏 items**：零个或多个替换，每个严格含 `slide_id, field, before, after`；field 仅 title/notes/body:0…body:5，每页每字段最多一项。before 必须等于完整旧字段，after 为完整新字段并通过原 content validator。替换范围仅为指定页的文字引用，不接受任意代码、XML、路径写入或变更来源策略。items 为空表示未找到可处理文字引用，summary 必须如实说明；生成的独立版本也不冒充全面脱敏认证。
- **报告与隐私**：输出均保留真实请求、范围、版本和结果，不连带触发其他动作。脱敏衍生版本保留 origin snapshot 与原始引用上下文供本地追溯；内部 receipt、snapshot、context、result 不是公开附件。单设备写入边界不变，不声称 Synology 分布式锁。

## 实施与验收入口

- 阶段0：本规范、工程图与路由；运行时 doctor、合成页 smoke 及回归。
- 阶段1A（已实现）：状态内核、严格批准/锁定、并发与中断恢复、三个固定版式单页生成；合成三页实际渲染回归不伪造用户验收。
- 阶段1B（工程已实现，人工验收待完成）：整套快照组装、基本机械校验、持久导出及三个独立按需扩展已实现；不串入默认制作/导出流程。真实三页人机协作及实际 PowerPoint 打开编辑仍需用户参与，不以合成批准代替。
- 阶段2：一套真实 8–12 页学术汇报，按案例补齐少量稳定版式、表格和明确源数据的基础图表。
- 阶段3：选取现有素材库的少量模板做受限适配；高级图表、公式、批量批准、API 与第二 renderer 后续按需决策。

逐项测试与故障注入要求见 `operations/engineering/presentation-test-plan.md`。只有对应实现及测试通过后才能更新本节成熟度，不以文档存在代替功能实现。


## 学术报告摄入与模板结构复用

三个视觉模式共用 `.scripts/pptx_visual.py`：`content` 是摄入内容识读，`layout` 是设计与组件用途分析，`form-check` 是用户按需形式检查。主 Agent 不打开页图；纯文字语义 backend 保持不变。API 失败返回文字状态，不能回落宿主看图。缓存按原件、页图、结构、模式、模型与协议绑定；回执记录真实执行身份，不能认证人类批准或研究结论。

```bash
# 只读结构提取，不联网；保存逐页字体/字号/布局/利用率/表格/图表/框图/示意图参数。
python3 .scripts/pptx_structure.py inbox/example.pptx --output temp/pptx-example/structure.json
# 学术报告复用 academic-reference；伴生 Markdown 与原件由受管事务归档。
INGEST_BACKEND=agent python3 .scripts/wg.py ingest inbox/example.pptx --subproject academic --document-type academic-reference --allow-remote-ppt
# 按已授权的选定页解析版式。目录由程序创建，输出仍在 temp。
python3 .scripts/pptx_visual.py inbox/example.pptx --mode layout --pages 1,3 --directory temp/pptx-example/analysis --allow-remote
# 查询模板不调用视觉 API；兼容旧库说明与新受管组件。
python3 .scripts/slide_library.py search '方法 对比'
# 提取候选，不等于收藏；用户选定后 collect，仅接受已归档公共域 Raw 来源。
python3 .scripts/slide_library.py extract --source academic/raw/reference-documents/example.pptx --pages 1,3 --name 方法对比 --output temp/pptx-example/candidate --layout-report temp/pptx-example/analysis/layout-analysis.json
python3 .scripts/slide_library.py collect --prepared temp/pptx-example/candidate --name 方法对比 --instruction '用户实际选页指令'
```

结构参数和视觉分析一起用于设计：精确值读取原生对象；视觉解释负责阅读顺序、层级与可读性。覆盖率是裁到画布后的对象包围盒并集估算，单独列出疑似背景排除项及含背景上界，不表示越高越好。声明/主题解析字体与实际渲染字体分开；图表缓存数据不重算，图像不可冒充可编辑表格或精确数据。

受管组件保存 component.pptx、component.md、structure.json、原版式分析回执和缩略图。复制保留所选原生页及其关系依赖；涉及未选页的跨页依赖拒绝提取，防止悄悄带入其他页。原始内容不自动通用化/脱敏；模板库复制与现有三个 IR 版式是独立能力，不承诺任意旧页自动套用。`reindex` 可恢复受管索引；正式收藏失败不撤销报告摄入。

### 直接使用型与套用填充型

两类按复用方式区分，同一来源页可以分别收藏：direct 保留原样内容，说明适用主题、来源引用、时效与使用限制；adaptable 保存替换位与容量规则，把指定文字和图片改为占位内容，用于填充新材料。旧库保留为 unclassified，不自动宣称可填充。

`.scripts/slide_library.py prepare-reuse` 消费已收藏组件及 Agent 明确编写的 `slide-reuse-v1` profile，生成 v2 候选；`collect` 仍按用户授权发布，去重加入类型与 profile 哈希。`search --kind direct|adaptable` 按类型检索。`fill --template <组件目录> --values <JSON> --output temp/<新目录>` 验证所有槽位后生成独立 PPTX、预览与来源回执，原件/模板不变。制作方可手动拷贝实例页进入新 PPT；这不等于任意旧模板自动转成现有三个 IR 版式。

首版槽位限原生文字框和图片；文字保留对象几何、字体及段落样式，检查总字符/显式行数/单行字符预算；PNG/JPEG 等比居中留边。没有表格、原生图表或任意 SmartArt 自动换数据功能；其他对象必须明确声明固定。派生演讲备注改为模板使用提示，包内历史媒体/隐藏元数据不承诺全面清除。容量检查不等同于视觉通过，制作中的视觉判断调用独立 API，最终三项辅助检查仍遵循用户触发条件。

profile 和完整调用说明见 `slide-library/_rules.md` 第九节；具体模板的 `component.md` / `values.example.json` 提供替换指南，`structure.json` 保留精确参数。形式检验与 PowerPoint 人工编辑验收各自记录，不因程序成功而代填用户批准。
