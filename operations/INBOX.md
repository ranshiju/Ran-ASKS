> **使用约束**:先读规则再操作;归类后 raw 不再修改;inbox 处理完应为空。

# Inbox（收件箱）操作规范


---

## 用途

`inbox/` 暂存待分类和摄入的新文件。统一入口先由程序评分归类；不确定样本在 Agent backend 合并为一个 `agent-task-v1` 供当前宿主裁决，在 API backend 由受限分类器裁决。仍不确定时才由用户兜底。

## 工作流程

单张图片（PNG/JPEG/WebP/BMP/TIFF）走通用文档入口，所属域由内容判定。
默认 Agent 模式先交接图片转写任务；`--file <图片> --ocr-result <JSON>` 复用独立 OCR 回执，
或用 `--file <图片> --allow-remote-ocr` 显式授权 API OCR，不改变 Wiki/语义 backend。
远程图片 OCR 不因 API 配置存在而自动启用。原图和同名 Markdown companion 均经受管事务落位；
两者作为同一 Raw 来源，配对与 OCR 溯源写入同事务的 `<原图文件名>.source.json`，不污染转写正文。
具体复用与校验契约见 `operations/IMAGE_OCR.md`。
图片在进入 Wiki/图前须有风险评估和源绑定复核记录，关键字段未核对即阻断。
摄入 completed 与 OCR partial/review_required 可以并存；非关键未决项和原件空白是材料状态，不自动生成用户待办。交付先说明是否入库完成，再区分限制与真正阻断项，详见 `operations/IMAGE_OCR.md`「状态解释与交付表述」。
来源与暂存清理仅在 validate_completion 通过、completed 已持久化之后执行；清理失败保留 cleanup_pending，resume 只重试清理。

执行后端遵循进程环境 → 项目 `.env` → 默认 `agent`；入口在实际摄入前向 stderr 输出 backend，紧凑回执也保留该值。API 模式由程序驱动 worker/sub-agent，恢复耗尽后交当前宿主 Agent 兜底；这不是 Agent 模式回落 API，也不因宿主是 Agent 就覆盖显式 API 配置。

紧凑回执保留质量告警总数和最多 5 条 issue/detail 摘要（每字段最多 240 字符）；先据此定位降级原因，只有摘要不足时再按报告字段定向读取完整诊断。

首次摄入与直接 resume 使用共享报告发布入口，把维护回执及报告地址关联到本批所有已完成事务。历史报告缺少关联时运行 `python3 .scripts/ingest_inbox.py --reconcile-maintenance-report cross-domain/ingest-reports/<report>.json`：仅重建指定报告的关联、重放已有方向裁决并同步回执，不重新摄入、不调用模型、不写 Raw 或图。独立质量告警和其他待审动作继续保留。

维护语义以共享回执为准，事务仅持有同步快照。发布先在报告写 `maintenance.publication.status=pending` 检查点，再同步本批事务，最后标为 `completed`；方向闭环同步全部关联事务而非仅当前论文。I/O 失败单独返回 `maintenance.status=error`、`publication`、`retryable` 与 `next_action`，文件 `completed` 和失败计数不变。已落检查点可用上述 reconcile 命令继续，直接 resume 遇到 publication pending/error 也只重试发布，不重跑全局维护；检查点尚未落盘时明确返回 `report_persisted=false`，须先恢复报告持久化条件，不假称存在可恢复报告。

> **代码驱动优先**：论文 PDF → `ingest_paper.py`；会议纪要 → `ingest_meeting.py`；学术非论文及行政/教学/商业文档 → `ingest_document.py`（详见 `memory/playbooks/index.md` 对应条目）。文件扩展名只选择提取器：`.docx/.doc/.pptx/.md` 若命中文件名或正文中的强会议速记标记，仍显式传 `source_kind=meeting`，不得退化为普通 official-doc。PDF/TXT 不确定分数只在 `--run` 时进入对应 backend 的一次合并裁决；中/高置信度具体类型可纠正程序初判，`ambiguous`、低置信或裁决失败时才在事务前停止。`academic` 非论文必须分类为 `editorial`、`academic-reference` 或 `conference-summary`；文件名或正文首个 H1 同时含会议实体词与事务记录词时可强判为 `conference-summary`，其余不确定样本停在 `classification_required`，不得回退到 `admin`。单文件可用 `--document-type` 显式定类；该参数直接选择通用文档分流，同时保留程序初判用于审计。

1. 用户触发处理（"处理 inbox""整理 inbox"等）
2. **只读分流（必经）**：先运行 `python3 .scripts/inbox_plan.py --output temp/inbox-plan.json`。仅当 manifest 标记 `batch_eligible: true` 时才可按 batch 路由；否则每个普通文件都按 `create` 路由。`facts-pending.md` 仅在有事实条目时归档。
3. **入文本到临时区**：按文件类型把可读全文落到 `temp/inbox-extract/`：
   - PDF：`extractor.py --external-pdf <inbox文件绝对路径> --paper <tmp-id> --papers-dir temp/inbox-extract` → `paper.pdf` + `paper.md`
   - `.txt` 会议纪要：直接是文本（仍传 `source-kind=meeting` 派发会议预处理与建边规则）
   - `.docx/.doc/.pptx`：`ingest_document.py` 提取文本入临时区；强会议速记标记同时记录 `source_kind=meeting`
   - `.md`：直接读
4. **有界分类 + 单遍语义生成**：边界分类器最多读取 8,000 字符，只返回类型复核；放行后语义 Worker 读取所需上下文并产出：
   - **子项目**（academic / admin / teaching / business）、页面类型、最终 ID、最终 raw 路径、最终 wiki 路径
   - `long_document_plan` 评估拆页（仅长文触发）
   - 受目标 Schema 约束的 wiki 内容，`sources` **直接填最终 raw 路径**（前置决策，不留占位、不做后置替换）
   - wiki 暂写到临时区
   - 分类器不读全文、不生成知识产物；高置信度文件跳过分类器。Wiki/子图生成复用同一语义上下文，不因分类再次读取全文
5. **清单化落位**：每个 inbox 文件使用独立临时目录 `temp/inbox-extract/<tmp-id>/`，并在该目录写入 `manifest.json`：
   - 固定结构：`{"raw_files":["paper.pdf","paper.md","source.yaml"],"wiki_file":"wiki.md"}`；`raw_files` 只能列出需归档的原始实体和允许的同源提取产物，禁止列入 `corrected.*`、实体解析 JSON、提示词、草稿或其他派生文件
   - 调用：`python3 .scripts/inbox_finalize.py --paper-id <id> --raw-dir <最终raw目录> --wiki-path <最终wiki路径> --extract-dir temp/inbox-extract/<tmp-id>`；`--manifest` 仅在文件不叫 `manifest.json` 时传入。目标是已有共享容器目录（如 `raw/conferences/YYYY/`）时，显式加 `--allow-existing-raw-dir`；脚本仍拒绝 manifest 中任一同名 raw 文件或 wiki 文件冲突。
   - 脚本只复制 manifest 指定的顶层实体文件；预检目标路径，默认拒绝覆盖；在 staging 中完成 SHA-256 校验后再提交 raw 目录和 wiki 文件，并写入 `temp/inbox-receipts/` 回执
   - **禁止符号链接**——实体复制（`copy2`，非移动）；保留原始扩展名。`--cleanup` 会在提交后运行当前 wiki 页的 `ingest_check.py`，仅 PASS 时删除当前 `<tmp-id>` 临时目录；失败时保留临时区和已提交产物供修复
6. **摄入成功确认**：对每个 inbox 文件，验证摄入产物三件齐全：
   - **raw 落位**：目标 `*/raw/` 下有对应文件（实体，非符号链接）
   - **wiki 落位**：对应 `wiki/` 页面已创建
   - **ingest_check PASS**：wiki 页 `ERROR=0`
   - 三件任一缺失 → **不清空 inbox**，定位问题修复后重验；不得传 `--cleanup` 或删除 inbox 原始文件
   - 全部齐全 → 记录回执路径后进入步骤 6
   - 成功后由统一收尾维护依次处理缩写、人物页和 Hub；直接 `--resume` 成功也必须走同一入口，并把包含完整 `graph_report`、事务号和质量状态的报告写入 `cross-domain/ingest-reports/resume-<txn>.json`。若 inbox 仍有普通待摄入文件或非空 `facts-pending.md`，入口返回 `maintenance.status=deferred`，由最后一个完成项执行一次全局维护，禁止每篇重复扫描。文件终态见 `status`/`file_status`，维护终态见 `maintenance.status`，完整回执写 `temp/inbox-maintenance/`。类型化缩写与 Hub canonical 候选由主 Agent 批量处理，不能用维护 handoff 覆盖文件成功终态；缩写摘要须区分唯一 token 与 occurrence，Agent 决策应用后同步闭环对应 maintenance receipt。Hub membership 的 profile/Scope/prototype embedding 必须汇总去重后批量请求；路由 margin 不足或子方向特异性不足时写带 `route-apply --transaction-id` 模板的 `hub-route-review`，已有子 Hub 的超限父 Hub写带 Scope readiness/blockers 及 `define-scope`/`redistribute` 动作的 `hub-auto-redistribute`。120 秒维护超时只返回可重试 `deferred`。
7. 清理：普通成功项在校验通过后删除临时区并清空对应 inbox 原件（保留 `facts-pending.md` 和 `.gitkeep`）。源指纹精确重复项须在既有路径仍位于受管 `raw/` 且 inbox/Raw 双方 SHA-256 复核一致后，写 `temp/inbox-duplicate-receipts/` 回执并移入可恢复废纸篓；复核失败不得删除。清理必须递归处理隐藏目录；成功/失败阶段写入 `temp/inbox-state/<transaction-id>.json`，以便恢复。

**事务入口（批量/非标准场景）**：`.scripts/inbox_ingest.py plan` → 每项 `prepare` → `complete`。仅用于 `ingest_paper.py`/`ingest_document.py` 未覆盖的批量或非标准场景；常规单篇摄入走 playbook 代码驱动脚本。

**全论文批量**：`ingest_paper.py --inbox` 保持 graph_ready 两阶段屏障。quiet 模式的 prepare 最多并发 2 项，以重叠 MinerU/LLM 等待；所有 live graph preflight/commit 由 `graph_ingest` 的数据库级跨进程 writer lock 串行，逐篇保持独立原子事务。Agent resume 会按 transaction ID 回填原 batch 报告，最后一个 item 完成后用全批完整 `graph_report` 统一闭合维护；同源重复的旧未提交事务只能经 `inbox_state.py --supersede ... --by ...` 留痕关闭。

**论文书目预审**：DOI/arXiv 候选只从 PDF 元数据与 `paper.md` 前部身份区提取，并在 Abstract/Introduction 或 References/Bibliography/参考文献标题前截止，不得把正文引文当成本篇标识。`metadata.subject` 中可识别的完整期刊引文或期刊名，以及近端 `ACM Reference format:` 引用中的标题与 ACM/Proceedings venue，属于带精确证据的候选；关键词式 subject 不猜期刊。所有确定性提取结果均为 `candidate_only`，可靠 venue 候选存在时不得留空。Agent 后端把 candidate-id-v2 书目裁决、Wiki 与语义槽合并进一次 `paper-agent-workspace-v1` 接管，并把暂存 `paper.pdf` 的 `pages:1-2` 作为首个有界输入，要求 Agent 直接核对完整作者；带页码且绑定哈希的 `first-two-pages.txt` 仅作 PDF 工具不可用时的备用。两者都不替代 Raw，最终作者仍由候选 ID 或 `paper.md` locator validator 锁定。常规走 `paper_workspace_read → paper_workspace_check → paper_workspace_commit`；候选提供器版本落后且已有未提交输出时，须显式调用 `paper_workspace_refresh`，先归档旧输出到 `workspace-history/` 再刷新同一事务，禁止静默覆盖。check 同轮汇总可独立发现的 Wiki/semantic 问题，并在最终 Raw/Wiki 落位前对 live graph 执行回滚式 Graph plan 预检；公开状态仅为 `awaiting_agent|ready_to_commit|completed|failed`。只有作者候选确实不完整时，Agent 才可按受限 locator 提交完整逐人列表。

**论文语义故障处理**：Agent backend 的结构性语义错误写回原 `agent-task-v1.issues`，当前宿主修正同一暂存产物后继续原 validator/commit；API backend 在有界修订耗尽后保留兼容 `agent_required`，再沿原事务恢复。弱 API 模型的 Wiki 撰写接收由摘要、定理和结论确定性组成的关键证据包，定理/等式/性能结论必须保留对象、条件和比较基准。语义槽少于 4 条时只定向重抽一次 slots，不重写已通过校验的 Wiki，并保留两次中覆盖更高的一版；仍稀疏才以 `graph_semantic_coverage_sparse` 降级提交。完整命题不再交专用 LLM 原子化；程序只链接本页已确认概念或主图唯一精确 title/alias，无匹配/歧义静默保留裸 proposition，不算 degraded。语义边不得从“关联/构造/表示”自行推导“基于”等方向关系；只有作者明示的限制或近似代价可标为“局限性”。合法 proposition object 不触发描述性短语误报；真正的描述性对象与裸缩写 warning 不阻断提交，但正常校验和 resume 都必须把它们持久化为质量告警。未登记但格式合格的新谓词会记录为候选并自动治理，不自动成为正式契约。

执行授权后应连续完成当前文件的“落位 → 巩固 → 校验 → 清理”；只有校验失败、目标冲突、需要用户确认的高风险实体或用户主动插话时才暂停。状态更新只报告已完成阶段与下一项阻塞，不把正常阶段切换当作暂停点。

## 约束

- `inbox/` 仅作中转，不作为长期存储
- **避免重复全文阅读**：边界分类只读有限摘录；全文/定向上下文只进入语义生成，不为类型复核单独读取全文，也不为 Wiki/子图反复重读全文
- **前置决策**：`sources` 在撰写时直接填最终 raw 路径，不留占位、不做后置替换
- **清空前必须确认摄入成功**（步骤 5），避免过早清空致原始文件丢失无法追溯
- 不属于四个子项目的文件，告知用户另行处理
- 复制时保留原始扩展名（格式转换由 `extractor.py`（PDF）或 `ingest_document.py`（docx 等）按类型处理）；用 `copy2` 非 `move`，防符号链接 bug
- **禁止符号链接**：raw 归档必须实体复制
- 临时区产物不得建立图边、不得被查询；临时区路径不得写入正式 `sources`
- 目标 raw 目录或 wiki 文件已存在时，脚本必须失败；更新既有条目走标准 `ingest update`，不得以 inbox 落位覆盖事实层
- 行政文档中的 `department` 只接受 Raw 逐字出现的完整部门名称；负责人关系还须同一证据片段明确出现“负责人/负责”等职责措辞，校长讲话、主讲或发言身份不得自行推断部门或负责人
- 统一入口按顶层结构化状态解释摄入结果；Agent 的 `prepared` 与 API 兼容的 `agent_required`，以及 `awaiting_agent`、`ready_to_commit`、`partial`、`graph_ready`、`classification_required` 等可继续状态，不得包装成普通进程错误。统一报告将 `duplicates`、`awaiting_agent`、`pending` 与 `failed` 分开统计；DSH 只在 API backend 解释这些状态
- 分类 `evidence_quotes` 仍须能回溯输入，但 validator 先做 Unicode NFKC 和连续空白折叠，允许 PDF 标题断行等表示差异；增删实词后的概述不算原文证据

## 用户申明事实

用户可随时输入"添加事实：X"（如"添加事实：我与王晓晨是师生关系"），追加到 `inbox/facts-pending.md`。这是**事实类 raw** 的中转累积。统一入口只在存在标准事实条目时将其识别为 `user-assertions`，`--run` 先调用 `.scripts/ingest_user_assertions.py --prepare`，返回绑定 pending/Raw 哈希与固定 proposal schema 的 `prepared + agent-task-v1`。当前宿主 Agent 只填写 `write_to` 指向的提案，再执行任务中的 commit 命令；不得直接依次修改 Raw、Wiki、Graph 或 pending。

- **普通文件**：`copy2` 复制到 `*/raw/`（独立文件）
- **facts-pending.md**：由受管 apply **累积追加**到 `cross-domain/raw/facts/user-assertions.md`（单文件累积，非独立文件）；Raw、目标 Wiki、临时 Graph、`ingest_check --graph` 全部成功后才移除已提交条目，失败恢复事务前字节与图快照

- **每条事实带**：时间戳 + 申明原文 + 锚点（`{: #fact-<简写>-<日期>}`），供 wiki 引用精确定位
- **source_type**：`user-assertion`（新增来源类型，属事实源，见 INGEST.md source_type 取值）；confidence 默认 medium（无第二来源，可质疑）
- **摄入流程一致**：Agent 根据申明内容选择受管 Wiki 页面，在 proposal 的 `wiki_updates` 中提交带精确 Raw locator 的完整页面内容，并在独立 `relations` 中提交关系；程序分别校验页面与关系，再经 staged `graph_ingest` 写入 graph.db，不把关系嵌入 Wiki 正文
- **受管事务**：proposal 必须逐条覆盖 manifest 的 fact ID，并为每条关系指定受管 Wiki 页；Wiki 正文须含 `cross-domain/raw/facts/user-assertions.md#fact-*` 精确引用。程序在副本图上执行 `graph_ingest --clean`，提交后逐页运行 `ingest_check --graph`，任何失败均补偿回滚并写 `temp/user-assertions/<transaction-id>/receipt.json`
- **wiki 页类型按内容定**（按通用流程）：人物关系更新对应 people 页正文；新概念/项目建对应页；多条关联内容可建摘要页。关系仍单独进入 proposal `relations`，不因"事实"另建碎页

**raw/facts/ 定位**：放 `cross-domain/raw/facts/`（跨域事实，cross-domain 跨域定位一致）。`user-assertions.md` 是累积型 raw，随用户申明持续追加，是事实来源之一。
   - **文件命名**：按目标子目录规范重命名（如会议纪要 → `MMDD.ext`）
   - **读取策略**：与普通 inbox 一致——全文只读一次，归类、命名、wiki 编码复用同一次理解；只有证据定位、格式冲突或不确定项才按需 `grep` 局部片段。
