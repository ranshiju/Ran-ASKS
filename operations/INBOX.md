> **使用约束**:先读规则再操作;归类后 raw 不再修改;inbox 处理完应为空。

# Inbox（收件箱）操作规范


---

## 用途

`inbox/` 暂存待分类和摄入的新文件。用户放入后，LLM 自动判断归类并执行摄入。

## 工作流程

> **代码驱动优先**：论文 PDF → `ingest_paper.py`；会议纪要 .txt → `ingest_meeting.py`；行政/教学/商业文档 → `ingest_document.py`（详见 `memory/playbooks/index.md` 对应条目）。以下步骤仅用于 **playbook 未命中** 的文件（如非标准格式、需手动判断归档位置的文件）。

1. 用户触发处理（"处理 inbox""整理 inbox"等）
2. **只读分流（必经）**：先运行 `python3 .scripts/inbox_plan.py --output temp/inbox-plan.json`。仅当 manifest 标记 `batch_eligible: true` 时才可按 batch 路由；否则每个普通文件都按 `create` 路由。`facts-pending.md` 仅在有事实条目时归档。
3. **入文本到临时区**：按文件类型把可读全文落到 `temp/inbox-extract/`：
   - PDF：`extractor.py --external-pdf <inbox文件绝对路径> --paper <tmp-id> --papers-dir temp/inbox-extract` → `paper.pdf` + `paper.md`
   - `.txt` 会议纪要：直接是文本（仍传 `source-kind=meeting` 派发会议预处理与建边规则）
   - `.docx`：`ingest_document.py` 提取文本入临时区
   - `.md`：直接读
4. **单遍阅读 + 判断 + 撰写同时完成**：LLM 只读临时区全文一次，同时产出：
   - **子项目**（academic / admin / teaching / business）、页面类型、最终 ID、最终 raw 路径、最终 wiki 路径
   - `long_document_plan` 评估拆页（仅长文触发）
   - 受目标 Schema 约束的 wiki 内容，`sources` **直接填最终 raw 路径**（前置决策，不留占位、不做后置替换）
   - wiki 暂写到临时区
   - 不为分类单独读全文，也不为 wiki 重读全文；一次阅读完成理解、归类、命名和编码
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
7. 清理：删除临时区 `temp/inbox-extract/`；清空 inbox 原始文件（保留 `facts-pending.md` 和 `.gitkeep`）。清理必须递归处理隐藏目录；成功/失败阶段写入 `temp/inbox-state/<transaction-id>.json`，以便恢复。

**事务入口（批量/非标准场景）**：`.scripts/inbox_ingest.py plan` → 每项 `prepare` → `complete`。仅用于 `ingest_paper.py`/`ingest_document.py` 未覆盖的批量或非标准场景；常规单篇摄入走 playbook 代码驱动脚本。

**全论文批量**：`ingest_paper.py --inbox` 保持 graph_ready 两阶段屏障。quiet 模式的 prepare 最多并发 2 项，以重叠 MinerU/LLM 等待；graph commit 始终按文件顺序串行，verbose 模式也保持串行以免进度输出交错。

**论文语义故障处理**：`ingest_paper.py` 对结构性语义错误早停并返回 `agent_required`，保留已通过的 wiki 与语义槽文件；修正后以 `--resume <transaction-id>` 恢复，程序会在落位/写图前重新校验。弱 API 模型的 wiki 撰写会接收由摘要、定理和结论确定性组成的关键证据包，定理/等式/性能结论必须保留对象、条件和比较基准。完整命题不再交专用 LLM 原子化；程序只链接本页已确认概念或主图唯一精确 title/alias，无匹配/歧义静默保留裸 proposition，不算 degraded。语义边不得从“关联/构造/表示”自行推导“基于”等方向关系；只有作者明示的限制或近似代价可标为“局限性”。描述性对象与裸缩写属于 warning，优先走两级局部修复与复验，不回退整篇生成。未登记但格式合格的新谓词会记录为候选并自动治理，不自动成为正式契约。

执行授权后应连续完成当前文件的“落位 → 巩固 → 校验 → 清理”；只有校验失败、目标冲突、需要用户确认的高风险实体或用户主动插话时才暂停。状态更新只报告已完成阶段与下一项阻塞，不把正常阶段切换当作暂停点。

## 约束

- `inbox/` 仅作中转，不作为长期存储
- **避免重复全文阅读**：全文只读一次（临时区文本），分类、命名、wiki 编码复用同一次理解；不先做只用于分类的全文阅读，也不为 wiki 重读全文
- **前置决策**：`sources` 在撰写时直接填最终 raw 路径，不留占位、不做后置替换
- **清空前必须确认摄入成功**（步骤 5），避免过早清空致原始文件丢失无法追溯
- 不属于四个子项目的文件，告知用户另行处理
- 复制时保留原始扩展名（格式转换由 `extractor.py`（PDF）或 `ingest_document.py`（docx 等）按类型处理）；用 `copy2` 非 `move`，防符号链接 bug
- **禁止符号链接**：raw 归档必须实体复制
- 临时区产物不得建立图边、不得被查询；临时区路径不得写入正式 `sources`
- 目标 raw 目录或 wiki 文件已存在时，脚本必须失败；更新既有条目走标准 `ingest update`，不得以 inbox 落位覆盖事实层

## 用户申明事实

用户可随时输入"添加事实：X"（如"添加事实：我与王晓晨是师生关系"），追加到 `inbox/facts-pending.md`。这是**事实类 raw** 的中转累积，处理方式与普通 inbox 文件一致，**唯一区别在归档**：

- **普通文件**：`copy2` 复制到 `*/raw/`（独立文件）
- **facts-pending.md**：**累积追加**到 `cross-domain/raw/facts/user-assertions.md`（单文件累积，非独立文件），追加后清空 facts-pending.md

- **每条事实带**：时间戳 + 申明原文 + 锚点（`{: #fact-<简写>-<日期>}`），供 wiki 引用精确定位
- **source_type**：`user-assertion`（新增来源类型，属事实源，见 INGEST.md source_type 取值）；confidence 默认 medium（无第二来源，可质疑）
- **摄入流程一致**：归档后走标准 Ingest——读 raw（user-assertions.md）→ 按"添加事实"内容判该写进哪个 wiki 页（关系类→相关页 Core Triples；新概念→建概念页；批量关联→摘要页），不固定建独立页
- **wiki 页类型按内容定**（按通用流程）：人物关系写进 people 页 Core Triples；新概念/项目建对应页；多条关联内容建摘要页。不因"事实"另建碎页

**raw/facts/ 定位**：放 `cross-domain/raw/facts/`（跨域事实，cross-domain 跨域定位一致）。`user-assertions.md` 是累积型 raw，随用户申明持续追加，是事实来源之一。
   - **文件命名**：按目标子目录规范重命名（如会议纪要 → `MMDD.ext`）
   - **读取策略**：与普通 inbox 一致——全文只读一次，归类、命名、wiki 编码复用同一次理解；只有证据定位、格式冲突或不确定项才按需 `grep` 局部片段。
