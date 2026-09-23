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

只支持 `title`、`image_text`、`flow` 三个固定版式；图片是独立栅格素材，正文和普通框图使用原生对象。每次批准绑定 `status` 的快照哈希和当前 revision；Agent 看真实图后才能 `review`，失败视觉审阅不能锁定；用户明确接受预览及原始警告后才能 `lock`。`build` 只接受空参数并调用本地 renderer，不接受手写“通过”回执。普通文字提纲仍按 `write`，旧图片/PDF重建保持独立。

## 创作边界

- **用户主导最终检查**：PPT形式检查、PPT引用脱敏仅用户明确指令触发；可以提醒，不自动执行。PPT证据核查仅用户对具体内容有疑问时按范围执行。三项独立、默认不执行，均不作为默认交付门禁；详细约定见“按需辅助检查”。这不取消制作阶段已有逐页确认、机械校验、来源记录和隐私边界。
- 宿主 Agent 理解用途、检索证据、组织叙事、提出设计、查看实际预览并与用户交互；shared 程序负责 schema、状态、几何、生成、机械检查和事务。当前不提供 API adapter。
- 需要语义暂存产物时，沿用 `agent-task-v1` 的 inputs/outputs/protocol/issues/check/commit/resume，不增加 DSH、`llm_structured` 或模型型 planner/critic。内容与设计只经原 validator/commit 进入创作状态。
- Raw 是科学与机构事实的权威。Wiki 与 graph 只用于发现和到达；主张、数字、图分别保存来源 locator 与来源版本，不把“路径存在”当作“主张得到支持”。用户提供但未核验的材料标记 `user_provided`，不伪造 Raw 引用。阶段1A仅接受 user_provided/unresolved，不把文件存在或 hash 匹配自动认证为 raw_verified；保留来源记录不等于自动逐项核查。PPT证据核查仅在用户对具体内容有疑问时执行，不默认生成全套证据映射。
- 私有来源的敏感性沿素材、IR、预览、成品和回执继承。输出路径变化不解除限制；未来远程上传须独立、明确授权，发布版引用不得泄漏本地私有路径。当前所有创作包固定 local_only，无远程 adapter；smoke 无用户来源。
- 正文和标题优先 native text；普通框图使用 native shapes/connectors；表格和有明确源数据的受支持图表逐步原生化。科研图片可以独立嵌入；不从图像估读并重造数据，不默认整页图片化。
- 现有 `slide-library` 及图片/PDF 重建保持独立、不迁移、不重构。旧页整页拷贝与 IR 重建不是同一种能力；任意模板、现有 deck 无损编辑、动画、SmartArt、复杂公式和第二 renderer 不在首版承诺内。

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

全局方案可一次确认。严格逐页流程：内容提案 → 用户确认内容 → 设计提案 → 用户确认设计 → 编译/生成/机械检查 → Agent 查看真实页图 → 用户接受预览并锁定。

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
- `review` 是宿主实际查看预览后的记录：除 actor/confirmation_source/snapshot_sha256/accepted_warnings 外，含 verdict=passed|failed、findings（字符串数组）。failed 至少一条问题并阻断 lock；需要修复时回到设计/内容提交、重建和重审。命令不会自己查看图片，宿主不能把机械 pass 转录为“已视觉审阅”。
- `reorder` 的 payload 是 {slide_order:[...]}；当前保守拒绝涉及任何 locked 页的全局主题/页序操作，先列出 affected locked slides，再由用户明确解锁。重排清除单页构建/视觉批准，内容/设计不自动重写。尚无页码对象与整套输出批准。
- `check` 与 `commit` 走相同 schema/批准前置校验；check 不渲染、不提交 revision、不摄入素材，不能预告实际构建一定成功。commit 返回 committed 和 issues，依赖复验以 status 为准；status=blocked 的 CLI 退出码非零。只影响单页的操作不隐式重建其他页面。
- 恢复依据 current.json → receipt hash → 全部 revision 文件 hash 与资产 hash，不按 mtime。进程中断可能留下 `.pending-*` 或未引用 revision，status 只报告 uncommitted_revisions，不把它们自动变成成功；current 已切换则读完整新 revision。坏 receipt/素材/外部改写应报告，保留现场并从可信备份恢复或作为新材料处理，不覆盖旧批准。
- 本实现使用本机 POSIX flock、fsync、原子 rename；**不是多台电脑/Synology同步副本之间的分布式锁**。同一创作包只在一个设备写入；同步冲突或存储介质不支持相同文件系统语义时先停写。发布/交付前仍需真实 PowerPoint 打开与编辑验收。

## 分层检查与最终交付（工程已实现，用户验收待完成）

1. 结构 lint：当前支持对象/引用有效性、越界、连接端点和字体范围，图片用 contain 保持比例。长文字仍可能溢出；PDF文字回读只是机械门禁，不代替查看图像。警告与确定性错误分开，不按 bbox 武断否定合理叠放。
2. 本地实际渲染：复用 PPTX→PDF→PNG。仅本地确定性检查通过不等于视觉审阅通过；未检查、失败、警告分别保留。
3. 宿主 Agent 视觉审阅：必须查看实际图像后记录 verdict=passed|failed 与 findings；failed 必须说明问题且阻断 lock。审美建议与可见缺陷分开，不能静默修复锁定页。
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

当前 Agent 查看任务中的实际成品 PNG 和对应批准 PNG，仅检查请求页的排版、字体、图片、遮挡/溢出、页序与预览差异；不检查事实依据，不顺带脱敏，不替用户确认。按任务的 check/commit 提交逐页发现；详细结果 schema 按需读取“阶段1B 导出与按需操作协议”。无法查看图像应如实记 unable_to_check；页面未批准的新衍生版本不能靠形式检查继承原预览批准。

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

`.scripts/presentation_delivery.py` 是 shared 确定性入口，无模型 backend。当前宿主负责理解用户指令、审阅图像、判断证据与提出引用替换，不能把 `--instruction` 存在当作已验证真实人类授权。

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
- **公共结果字段**：严格 `schema=presentation-assistance-v1`、request_id、intent_sha256、actor、summary、items。前三项由 prepare 给出，不改写；actor 为实际宿主声明，summary 如实说明检查范围与限制。所有检查都不产生用户批准，执行状态 executed 不等于结论 passed。
- **形式 items**：每个请求 slide ID 恰好一项，字段 `slide_id, verdict, findings, inspected_preview_sha256, approved_preview_sha256`。verdict 为 passed/issues_found/unable_to_check；后两种必须有 findings 字符串列表。两个哈希分别取实际成品 PNG 与批准 PNG，脚本校验绑定但不能证明 Agent 真的看图。只保存报告，不修改页面。
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
