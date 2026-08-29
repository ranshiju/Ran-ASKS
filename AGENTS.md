# KR Wiki — 项目宪法

> 文件型综合知识库:raw 事实层 → wiki(LLM 编译,桥接图与 raw)→ graph.db(导航关键关联边)。事实答案必回溯 raw。

## 红线 (REDLINES)

- 绝不修改 raw/ 中任何文件(例外:同源重提需 log 记录)
- 先读规则再操作,不凭记忆执行
- DSH session log 不得替代 `raw/` 或 `graph.db` 作为事实源;DSH 不得绕过 raw 红线、graph schema 或摄入校验。

## 环境约束 (Sandbox)

- **删除命令**:沙箱拒绝 `rm` / `rm -rf` / `rm -f` 等形态;删除文件/目录必须用 Python(`shutil.rmtree` / `os.remove`)或 `trash`(若安装)。批量清空用 `python3 -c "import shutil; shutil.rmtree('path')"` 一行式。**不要**重试 `rm`,直接换工具。
- **读取限制**:沙箱对 `cat <大文件>` 会截断(超过 `max_output_tokens` 截断显示),导致 LLM 看到不完整内容后再触发脚本二次扫描(浪费 token + 重复 IO)。会议纪要/论文 PDF 等长文件读取时,**不预设截断**,直接传完整路径给读取工具(脚本内部完整读)。
- **图数据库**:边唯一源 `cross-domain/graph.db`(private 物理隔离 `private/graph.db`);路径常量与按域解析见 `.scripts/graph_lib.py` 的 `GRAPH_DB` / `graph_db_for()`,勿裸 `find`/裸 `sqlite3` 直查(裸 `sqlite3 <不存在的路径>` 会自动建空文件),经脚本(`graph_metrics.py`/`query_graph.py`)操作。
- **DSH 运行时**:`dsh/` 是可选 agent cockpit;运行时状态只存内存(SessionLog/ToolRegistry/guard chain),不写 `raw/`、`wiki/` 或 `graph.db`。DSH 工具执行仍遵守本文件沙箱约束与摄入事务边界。

## 指令识别

**先查 playbook**:`python3 .scripts/playbook_dispatch.py '指令关键词'`。命中则按返回的条目执行（不再走下述流程）；未命中则正常推进。

**DSH 外壳优先**:若指令命中 DSH 已封装能力（查询工具、摄入工具、研究记忆工具），优先经 `dsh/` agent loop 执行；DSH 只做编排与 guard，不替代底层 `.scripts/` 工具和 raw/wiki/graph 写入边界。

**再判断类型**:
- **使用**:按既有规范操作知识库内容(不改规范/工具本身)。判断 task:
  - 摄入/新建文件/加内容 → `ingest`;查/问/找 → `query`;检查健康 → `lint`;同步对账 → `sync`
  - 写公文/讲稿 → 调用 general `write` capability（兼容 `task=write`）；扫描待巩固 → `scan`;处理收件箱 → `inbox`;建聚合页 → `hub`
  - 在 projects 下做研究/记笔记/写论文文稿 → `research`
  - 管理学术 Frontier 问题/轨迹/思路/验证记录 → `frontier`
- **建设**:改变项目本身(改 `operations/` 规范、`.scripts/` 工具、写论文、建工具、研究课题) → `build`
- **其他**:不涉及以上 → 直接回答
- **模糊时判据**:指令会不会修改 `operations/` 或 `.scripts/`?会→建设;只按规范操作内容→使用

**再调脚本分发规范**:`.scripts/route.py --task <task>`
  - `query` 默认派发 `--query-stage start`；候选已定位后才调 `evidence`，有具体缺口才调 `continue`，交付前调 `answer`。
  - `ingest` 必显式传入 `--subproject academic|admin|teaching|business`、`--mode create|update|batch`、`--content paper|other`、`--source-kind ordinary|meeting`；`create` 另传单个 `--stage 1|2|3`。参数由本次任务语义判断，脚本不读 log 推断；`source-kind` 默认 `ordinary`，仅 `.txt` 会议纪要传 `meeting`。inbox 论文 PDF 走 `ingest_paper.py` playbook，不走 `route.py`；`academic` 仅旧手动流程非 inbox 来源用。
  - 状态、能力、工具分层调用：`research` 等 task 提供持续工作状态；实际落笔时以 `--capability write --capability-profile academic` 加载写作能力且不切换状态；`wg.py`/DSH ToolRegistry 中的函数属于带参数和结构化返回的执行工具。顶层 `--task write` 保留为通用 write 能力的兼容入口。
  - 任务内部执行纪律、阶段推进与校验要求，以 `route.py` 对当前 task/stage 的派发内容为准。

## 使用任务启动约束

- 接续任务先按既有规则读取 `projects/*/notes/status.md`；其余使用任务完成 task 判断后，立即按上节调用 `route.py`。
- 路由前仅允许为补齐路由参数或定位目标进行最小读取；不得预读规范全文、SCHEMA、`.scripts/` 源码，或进行探索性检查。
- 路由后以任务卡和派发规范为当前步骤的操作边界；仅在派发内容明确要求、参数仍无法判定或命令报错时，定向补读。
- 本节只约束使用任务；建设任务按工程元图的 impact → contract → 实现 → 回归流程执行。
- 新增或改动 `dsh/` 组件，先更新 `operations/engineering/graph.yaml` 的节点/边/约束，再实现，最后补回归；未进入工程图的 DSH 组件不得接入使用任务。
- 建设任务由 Agent 执行：先运行 `python3 .scripts/engineering_graph.py impact <target> --verify` 建立影响卡；有建设域 DSH seam 时优先用 `dsh.build_tools.BuildLocatorCockpit`（guard 强制 impact 完成后才可 `read`，`list` 必须带 `--prefix`）。impact 给出的 graph.yaml node/contract/capability 与 code-guidance 推荐精确 locator 必须优先直接 `read`；推荐不足时再用 filtered discovery，并用返回的 Markdown/YAML/Python/行段 locator 精确读取。不得先枚举大型 YAML 全表。`rg` 只定位候选文件/符号；仅 locator 明确不支持、报错或所需上下文本身跨多个块时才定向扩大读取并说明原因。功能性使用任务只调用已封装函数，`engineering_locator` 不注册到通用 `query_actions` 或查询型 DSH；Raw/Wiki locator 仍作为事实读取函数的证据地址。

## 工作原则

- 接续任务先读 `projects/*/notes/status.md`,不靠 git 历史重推
- 「执行」=授权落地
- 不过度复杂化,遵循奥卡姆剃刀原理;重大改动与用户讨论完备或计划好了再动手
- 讨论物理论文时，把 `operations/research/physics-manuscript-editing.md` 作为注意力清单内化使用；实际写作或修改时先调用 academic write 能力，将共享落笔约定与该清单组合使用，均不按固定流程输出。

## 非默认读取文件

- `.project/README.md`、`.project/META.md`、`.project/config.yaml` 均**不是** Agent 启动或使用任务的默认上下文；不得因其位于项目内而预读。
- 仅在以下情形定向读取：人类项目说明/发布任务（`README.md`）、工程架构状态或论文写作（`META.md`）、PDF 提取或 `synology://` 路径解析故障（`config.yaml`）。

## 连续执行与交付

- 附带发现的可修问题指令末提示,不静默压 LINT,不扩面修复
- **工程文档按任务类型区分更新策略**:CRG(`operations/engineering/graph.yaml`)、代码指南(`code-guidance.md`)、`HUB.md`、`INGEST.md` 等工程文档——使用任务不自动更新(节约 token);建设任务中受影响时由 agent 自主判断并同步更新(先改代码,验证通过后更新文档),保持文档与实现一致
