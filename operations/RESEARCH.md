# 研究工作流规范

> 研究项目级持续工作：在 `projects/<name>/` 下开展研究，过程中保存结构化工作上下文与记忆。新项目使用通用 `workspace_state` 的 `research` profile；已有 `.research-memory` 项目由兼容入口继续支持。研究内容**不入库**（独立于 ingest）。

## 触发与任务判定

在 `projects/<name>/` 下进行研究工作、撰写研究笔记或论文文稿时，路由为 `research`：

| 信号 | 行为 |
|------|------|
| 用户指定 `projects/<name>/` 下操作（读材料、写笔记、写论文、做判断） | 进入研究工作流 |
| 用户要求「研究」「分析」「判断」某论文/课题，且在 projects 下 | 进入研究工作流 |
| 仅讨论不在 projects 下的内容 | 不路由 research，直接回答 |

### 与其他 task 的边界

- **不路由 ingest**：研究内容是工作过程，不进 raw/wiki/graph。若用户要求把研究产出摄入知识库，切换到 ingest task。
- **research 是持续工作，write 是按需能力**：研究笔记和论文文稿仍属于本项持续工作；真正开始起草、改写或润色时，在当前工作上下文内调用 academic write profile，不切换研究上下文。正式公文/讲稿可从顶层直接调用通用 write profile。
- **不路由 build**：改 `.scripts/` 或 `operations/` 是建设，走 build。

## 进入研究项目

agent 进入研究项目时的第一步是**恢复上下文**。存在 `workspace.yaml` 时使用通用入口：

```bash
python3 .scripts/workspace_state.py recall <project>
```

旧项目尚未初始化通用工作区时继续使用兼容入口：

```bash
python3 .scripts/research_memory.py recall <project>
```

通用 `recall` 输出本工作区画像、当前事项、有效记忆索引和直接子工作区摘要，不读取子工作区记忆。兼容 `recall` 保留研究画像、近期记忆索引和 status.md 输出。

若无记忆条目（首次进入），recall 返回空，正常开始工作。

## 状态与记忆工具

新工作区使用共享内核：

```bash
python3 .scripts/workspace_state.py item add <project> --title "事项" --state active --next-action "下一步"
python3 .scripts/workspace_state.py memory add <project> --title "标题" --kind decision --content "理由与边界"
python3 .scripts/workspace_state.py doctor <project>
```

既有 `.research-memory` 项目继续使用：

```bash
# 恢复上下文
python3 .scripts/research_memory.py recall <project>

# 新增记忆（agent 自动调用）
python3 .scripts/research_memory.py add <project> --title "标题" --intent <intent> --content "内容"

# 列出/检索
python3 .scripts/research_memory.py list <project> [--intent decision]
python3 .scripts/research_memory.py get <project> MEM-0001

# 研究画像
python3 .scripts/research_memory.py profile <project>        # 显示
python3 .scripts/research_memory.py profile <project> --refresh  # LLM 重新提取
```

## 可用能力与工具调用（研究中随时可调）

三层调用语义保持分离：`research` 是当前工作上下文；`write` 是落笔时加载的提示与规范能力；`wg.py`/DSH 注册函数是执行检索、读取或计算的工具。

**落笔写作能力**：当用户要求实际起草、改写、润色论文的标题、摘要、正文、图注、附录或补充材料时，Agent 在落笔前自动调用一次：

```bash
python3 .scripts/route.py --capability write --capability-profile academic
```

该调用组合 `WRITE.md` 的共享落笔约定与对应学科写作 overlay；它不改变 `research` 工作上下文。同一连续写作回合已经加载后无需重复调用。论文讨论、数据核对、实验、查询和进度汇报阶段不调用。

研究中需要查知识库文献、定位已有结果、回溯 raw 原文核验，或操作研究记忆，调用 `.scripts/wg.py`（统一 JSON 输出，跨功能、回合中可调）：

**状态与记忆**：
- `wg.py workspace recall <project>` — 恢复通用研究工作区上下文（新项目进入时首调）
- `wg.py workspace item ...` / `workspace memory ...` — 管理新工作区事项与记忆
- `wg.py recall <project>` / `remember ...` — 仅用于已有 `.research-memory` 项目的兼容入口

**知识库查证**（研究内容不入库，但可查已有 raw/wiki/graph）：
- `wg.py lookup <term>` — 关键词查节点（导航层，省 token）
- `wg.py neighbors <page>` — 关联召回（图边是定位器非答案）
- `wg.py relations <page> [--predicate P]` — 节点关系边；返回可选 `source`/`locator`，为空时从相邻 Wiki section 的 Raw 脚注下钻
- `wg.py read-raw <locator>` — 按 locator 回 raw 原文核验事实（locator 形如 `path#section`，来自 `relations` 的 `sources`）
- `wg.py hub-of <page>` — 页所属 Hub
- `wg.py abbr <term>` — 缩写解析

**研究前沿 Frontier**（独立于事实层，完整契约见 `operations/FRONTIER.md`）：
- `wg.py frontier ask "<学术问题>"` — 先查 WikiGraph、查重并建立单一 Question Page，同时尝试库内回答；弱模型不可用时保留 captured/pending
- `wg.py frontier answer <ID>` — 用当前 Graph→Wiki→Raw 证据重新尝试回答 Question Page
- `wg.py frontier list` / `show <ID>` / `search <term>` — 检索 Question 与 Trajectory
- `wg.py frontier add-entry <ID> ...` — 追加部分答案、残余缺口、思路或验证记录
- `wg.py frontier refresh <Question-ID>` — 基于当前知识库刷新有证据回答；不自动修改 `scientific_state`
- Frontier 只单向引用 Raw/Wiki/Graph，`frontier.db` 可重建；任何 AI 候选默认不 active

纪律不变：图边和 Navigation 只用于定位；事实主张（数据、结论、引用）必须 `read-raw` 回到 raw 原文确认。研究产出（笔记/文稿）不入库。

## 项目初始化与结构校验

新研究项目从通用模板创建：

```bash
python3 .scripts/research_project.py init <project> --name "项目名" --topic "一句话主题"
```

校验既有项目结构、schema 与产物边界：

```bash
python3 .scripts/research_project.py validate <project>
python3 .scripts/research_project.py validate <project> --strict
```

- 默认模式：缺少必需文件/目录/schema 关键字段为 ERROR；生成物未放入 `outputs/` 仅 WARN。
- `--strict`：生成物未放入 `outputs/` 也视为 ERROR。
- 新项目模板位于 `projects/_templates/research/`。

### 状态与记忆目录结构

新工作区结构见 `operations/WORKSPACE.md`，权威记录位于 `.workspace/items/` 与 `.workspace/memories/`，投影可重建。旧项目保持：

```
projects/<name>/.research-memory/
  profile.json       # 自动提取的研究画像
  index.jsonl        # 条目索引（每条一行）
  entries/
    MEM-0001.md      # 完整记忆条目
```

### 意图分类

| intent | 触发场景 |
|-------|---------|
| `decision` | 研究决策：方向选择、方法取舍、实验设计 |
| `insight` | 关键发现：重要洞察、对比结论、新认知 |
| `literature_judgment` | 文献判断：论文质量评估、威胁度、相关性 |
| `research_direction` | 研究方向：脉络调整、阶段推进、下步计划 |

## 自动保存纪律（核心）

agent 在研究过程中遇到以下情况时，**无需用户要求**，新工作区调用 `workspace_state.py memory add`，旧项目调用 `research_memory.py add`：

1. **研究决策**：方向选择、方法取舍、实验设计定案 → `kind/intent=decision`
2. **关键发现**：重要洞察、对比结论、新认知 → `kind/intent=insight`
3. **文献判断**：论文质量评估、威胁度判定、相关性结论 → `kind/intent=literature_judgment`
4. **方向调整**：研究脉络变化、阶段推进、下步计划明确 → `kind/intent=research_direction`

**不记录**的内容：
- 一次性调试命令、临时指令
- 可从代码/文件直接读出的信息
- 未经验证的猜测

### 写入规范

- `title` 简明扼要（≤30 字），自包含
- `content` 包含决策理由 / 发现依据 / 判断证据，不只记结论
- 每条记忆独立原子，不跨条引用（recall 只拉索引）

## 研究画像

画像（`profile.json`）是 LLM 从项目内容自动提取的结构化快照：

- `topic`：一句话描述研究主题
- `keywords`：5-10 个核心关键词
- `stage`：当前阶段（ideation/writing/experiment/revision/submission）
- `active_questions`：当前探索的 2-5 个核心问题

**刷新时机**：
- 首次进入项目（无 profile.json）
- 用户明确要求刷新
- 研究阶段发生重大转变后（agent 判断需要时主动调 `--refresh`）

不每次进入都刷新（省 token）；recall 读取缓存即可。

## 与 status.md 的关系

| | status.md | .research-memory/ |
|--|-----------|-------------------|
| 定位 | 新工作区的可重建当前状态投影；旧项目仍可人工维护 | 结构化历史记忆 |
| 内容 | 当前状态 + 下步计划 | 决策/发现/判断的历史条目 |
| 维护 | 新工作区由 `workspace_state` 生成；旧项目手动 | agent 经受管命令写入 |
| 读取 | 通用 recall 从权威事项生成；兼容 recall 附带旧 status | recall 返回有效记忆索引 |

两者互补不冲突：status.md 是「现在在哪」，记忆是「走过什么路」。

## 产出物

研究工作流的产出包括：

- **研究笔记**：`projects/<name>/notes/*.md`（手动或 agent 辅助撰写）
- **论文文稿**：`projects/<name>/manuscript/*.tex` 或 `*.md`
- **源码与测试**：项目 `codes/`；实验入口、分析和检查分别归入已有子目录。
- **运行与派生结果**：项目 `outputs/`；记录版本、配置、输入、实际运行结果与验证范围，不能把测试定义当作已运行证据。

跨项目的可复用实现可进入 `computing/code-library/`，独立按问题、模型、方法、算法和实现组织。
宿主 Agent 按需使用 `wg.py code-library search/context/read` 定位已有能力；工作元图不作为事实源。

产出物**不自动摄入知识库**。如需摄入，用户明确要求后切换到 ingest task。

## 可用计算资源

### 默认选择与凭据

用户指定计算服务器优先。数值实验、训练、参数扫描和长时间数据处理优先在已登记 SSH 服务器运行，
不默认占用本机。宿主 Agent 读取本地 `.env` 的 `COMPUTE_PREFERRED_BACKEND=ssh` 与 SSH 配置选择执行位置；
shared 程序不因此自动转发命令，API 路径仍遵守其原有授权与控制边界。

- `.env` 的 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PASS` 保存连接参数；文件不入 Git，权限为 0600。
- 密码不进入 Raw、Wiki、图、项目 schema、源码、任务文件或日志；schema 只记录配置键名。
- 轻量单元测试、lint、构建、解析和只支持 local 的固定诊断可在本地执行，明确说明例外。
- 服务器不可达、主机身份未确认或缺少依赖时报告阻塞，不静默改成本地长任务。
- 首次实际运行前核对主机指纹、工作目录、Python/依赖与资源；不把连接记录当成硬件/环境已核验。
- 只上传任务必要的源码和输入，不上传 `.env`、private、整库或无关文件；下载产物写项目 outputs。

新研究项目模板采用 `policy: server_preferred`、`backend: ssh`，runner 在具体任务中明确选择。
既有项目不批量改写；Tensorizing Neural Networks 当前固定梯度检查保留 local，作为显式例外，
后续分类训练优先服务器。现有 `code-run` 配方不支持 SSH，不因偏好变更宣称已有远程版本绑定回执。

### 共享工具

研究项目涉及数值模拟或 GPU 计算时，可使用 `computing/` 下的共享资源：

- **`computing/GPU_TORCH_GUIDE.md`** — GPU + PyTorch 科学计算经验指南（性能陷阱、优化手段、autodiff 陷阱、基准测试方法）。涉及 GPU 计算前先读。
- **`computing/ssh_run.py`** — 历史 SSH 远程工具（上传/执行/下载/状态查询），读取 `.env` 中的 SSH 配置；当前关闭主机密钥检查，密码传给 expect 命令参数。首次使用新凭据前须改用验证主机身份且不在进程参数传密码的入口；此次登记不调用它连接。

这些资源由各研究项目积累，跨项目复用，不属于任何单个研究项目。
