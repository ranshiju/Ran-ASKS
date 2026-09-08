# 持续工作区规范

> `workspace_state` 为 `projects/` 下不同类型的持续工作提供共享状态、记忆和接续机制。具体项目的内容、产物、状态与记忆物理隔离；profile 只提供语义配置，不共享项目数据。

## 结构与权威

```text
projects/<path>/
├── workspace.yaml
├── .workspace/
│   ├── items/*.md
│   ├── memories/*.md
│   ├── events.jsonl
│   ├── profile.json
│   └── index.sqlite
├── notes/status.md
└── 项目自己的材料与产物
```

- `workspace.yaml`：工作区身份、profile、domain 与父工作区，是配置事实源。
- `.workspace/items/*.md`：事项当前状态的事实源。
- `.workspace/memories/*.md`：经确认的决策、教训、判断和上下文的长期事实源。
- `.workspace/events.jsonl`：追加式审计历史；解析失败必须报告，不得静默跳过。
- `notes/status.md`、`.workspace/profile.json`、`.workspace/index.sqlite`：可由权威文件重建的投影，不得反向成为唯一事实源。
- Raw/Wiki 仍是机构与外部事实的权威。工作区记录事实依据时只保存可回溯 locator，不复制后取代事实层。

## Profile 与隔离

profile 定义事项类型、记忆类型和画像字段，不定义某个具体职位或项目的内容。内置 profile 位于 `operations/config/workspace-profiles.yaml`：

- `generic`：通用持续工作。
- `research`：科研项目。
- `role_work`：行政、管理、协调与治理职责；具体 domain 可用 `academic_administration` 等值细分。
- `writing`：有持续迭代周期的写作项目。

父工作区只聚合直接子工作区的名称与事项计数。`recall` 不读取子工作区的事项正文、记忆正文或画像；进入子项目后再独立 recall。

## 状态机

事项状态使用 `planned | active | waiting | blocked | done | cancelled`，并执行以下写入门：

- `active` 必须给出 `next_action`。
- `waiting` 必须给出 `waiting_on` 与 `review_at`。
- `blocked` 必须给出 `blocked_by`。
- `done` 必须给出 `outcome`。

事项和记忆 ID 使用时间戳加随机后缀的抗碰撞 ID，不依赖中心顺序号。每次写入先校验 Markdown schema，再原子替换单个权威文件，追加审计事件并重建状态页与 SQLite 索引。

### 简洁提案记录

`role_work` 可使用 `proposal` 类型记录班子会或党政联席会提案。提案仍是普通事项，不创建会议、议程或议事历史实体，只增加一个专用字段：

- `pending_submission`：待提，自动映射为 `state: active`。
- `pending_discussion`：待讨论，自动映射为 `state: active`。
- `discussed`：已讨论，自动映射为 `state: done`。

`proposal` 薄命令自动维护通用 `state`、`next_action` 和必需的 `outcome`；用户只维护提案状态。已讨论提案可选填 `discussed_at` 和实际 `outcome`，未填结果时明确记作“已讨论（未记录结论）”。状态页与 recall 按三类展示提案，不自动生成记忆或后续事项。

## 记忆生命周期

记忆状态使用 `active | superseded | expired`，支持：

- `valid_from` / `valid_to`：事实或决策的适用区间。
- `supersedes`：新记忆替代旧记忆；旧记忆同步标记 `superseded` 与 `superseded_by`。
- `applies_to`：适用对象或范围。
- `evidence`：Raw/Wiki locator 或项目内材料路径。

记忆只保存会影响未来判断的稳定信息：决策及理由、重要教训、已核验判断、稳定偏好与必要上下文。临时命令、可从文件直接重建的信息和未经验证的猜测不进入长期记忆。

## 命令

```bash
# 初始化，可使用 projects/ 下的嵌套路径
python3 .scripts/workspace_state.py init '<path>' --profile role_work --domain academic_administration

# 接续、检查与重建
python3 .scripts/workspace_state.py recall '<path>'
python3 .scripts/workspace_state.py doctor '<path>'
python3 .scripts/workspace_state.py rebuild '<path>'

# 事项
python3 .scripts/workspace_state.py item add '<path>' --title '事项' --state active --next-action '下一步'
python3 .scripts/workspace_state.py item update '<path>' <ITEM-ID> --state waiting --waiting-on '对方回复' --review-at 2026-09-15
python3 .scripts/workspace_state.py item list '<path>'

# 提案（role_work）
python3 .scripts/workspace_state.py proposal add '<path>' --title '提案标题'
python3 .scripts/workspace_state.py proposal update '<path>' <ITEM-ID> --status pending_discussion
python3 .scripts/workspace_state.py proposal update '<path>' <ITEM-ID> --status discussed --discussed-at 2026-09-15 --outcome '原则同意'
python3 .scripts/workspace_state.py proposal list '<path>' --status pending_submission

# 记忆
python3 .scripts/workspace_state.py memory add '<path>' --title '已确认决策' --kind decision --content '理由与边界'
python3 .scripts/workspace_state.py memory add '<path>' --title '新决策' --kind decision --supersedes <MEM-ID> --content '替代原因'

# 可选画像提取；Agent 只生成暂存任务，API 才调用模型
python3 .scripts/workspace_state.py profile '<path>' --refresh
```

同一能力也由 `python3 .scripts/wg.py workspace ...` 提供统一 JSON envelope。旧 `research_memory.py`、`wg.py recall` 和 `wg.py remember` 保留为 `.research-memory` 兼容入口；不自动迁移或重写既有研究记忆。

## 维护节奏

- 开始一段持续工作：`recall`，确认 active/waiting/blocked 和到期复核项。
- 形成可执行承诺：新增或更新 item，不能只把计划写进自由文本。
- 形成稳定决策或教训：写 memory；改变旧判断时使用 `supersedes`，不要覆盖历史。
- 阶段结束：将事项转为 done 并写 outcome，运行 `doctor`。
- 投影损坏或 schema 升级：从 Markdown 执行 `rebuild`；不得手工修补 SQLite 作为最终修复。
