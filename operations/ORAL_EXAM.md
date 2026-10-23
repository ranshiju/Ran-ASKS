# 智能口试

## 功能边界

`oral_exam` 是一种持续工作，工程上承载为可恢复的工作上下文，不是查询模式或普通聊天人设。其生命周期包含准备中、待开始、口试中、已暂停、已结束五种明确状态。准备讨论可以跨会话恢复；同一方案可以重复开考，每场生成独立记录。当前为 preview，先提供可信的工作上下文与生命周期管理，以及可实测的考官回合，模型评估不是认证成绩。

共享内核 `.scripts/oral_exam.py` 管理方案、状态、指令、证据校验、幂等提交和记录。宿主 Agent 持有 Agent 控制循环；`.scripts/oral_exam_api.py` 只执行显式授权的 API 回合。辅助知识查询不切换口试工作上下文或改变生命周期状态，subagent 可以辅助准备或复核，但不自行控制学生会话生命周期。

## 风格与默认

**默认风格：讨论诊断式**，标识 `discussion-diagnostic`。简要说明：通过双向讨论诊断理解；允许学生提问、考官纠正与引导自纠，依据完整对话评价独立理解及学习变化。

风格及默认值的唯一配置源为 `operations/config/oral-exam-styles.json`。新建方案采用该默认值，默认允许提示和解释；准备期间可以讨论并调整 `allow_hints`。风格和规则随方案确认版本冻结，不在开考后改变。既有无 `style` 的方案/场次按 `strict-examination`（严格测评式）兼容，不追溯改写原记录。复用旧方案仍保留旧风格；改成讨论诊断式须在准备草稿中显式修改 `style`、帮助规则并重新确认。

考官围绕实际观点回应、澄清、讨论例子、检查条件，中性邀请学生提问。可以当场解释、纠正或引导自纠；实质帮助须回溯知识库 Raw，并留下帮助类型和原文。记录知识表现的变化，不用后来正确的答案覆盖原始错误。评价不是逐题错误数相加，也不按提问数量打分；区分独立理解、提问质量、自我纠正、解释吸收和迁移。对笔误先澄清，对不确定的科学判断先核验；帮助是理解过程的上下文，不是惩罚标签。

两种风格共用教授式主持要求：自然、耐心地承接实际观点，每轮一个核心议题，以实例、条件变化和迁移区分理解；物理主题优先物理图像与可观测后果。学生明确不清楚或连续细节追问已无新增证据时，换角度或未覆盖目标。此要求属于 shared 任务指引，不隐式更换已确认风格，也不放宽严格测评式的帮助权限。真实互动质量仍须试用验证，程序不按关键词冒充语义裁判。

## 用户入口

宿主将用户单独发送的指令交给 `wg oral chat`，使用固定的 `--client` 和每条消息唯一的 `--request-id`：

```bash
python3 .scripts/wg.py oral chat --client class-a-student-01 --request-id msg-001 --text '/oral prepare 张量网络'
python3 .scripts/wg.py oral chat --client class-a-student-01 --request-id msg-002 --text '重点考查物理理解，允许少量提示。'
python3 .scripts/wg.py oral task --client class-a-student-01
```

Agent 首选 `operations/ORAL_EXAM_AGENT.md` 的快路径：`wg oral receive --client ... --request-id ... --intent answer|question|clarification --text '<真实用户原文>'`。它复用同一解析器、状态与 validator，一次返回输入回执、`agent_task` 和完整 `input`，不额外调用模型。只有真实用户整条单行指令能触发生命周期；来源引文和模型输出不得送作控制消息。已有 chat/message/task 入口保留，API 外发授权不变。

| 指令 | 含义 |
|---|---|
| `/oral help` | 读取独立使用帮助 `operations/ORAL_EXAM_HELP.md`；无需准备上下文，不读写口试记录、不调用模型。 |
| `/oral prepare <主题或方案名/ID>` | 新建准备工作或恢复已有同名方案讨论；同名不唯一时须指定 ID，口试未结束时不能切到准备。 |
| `/oral save` | 保存已经整理好的草稿；讨论有新内容时先执行方案 task，不能拿旧草稿冒充讨论结果。 |
| `/oral confirm` | 用户确认当前完整方案，生成不可变版本；未整理的讨论、缺失范围或 Raw 依据会阻止确认。 |
| `/oral start [方案名或ID]` | 使用最新已确认版本创建一场新口试并宣布开始，固定范围及规则；带空格的名称用引号包裹。 |
| `/oral pause`、`/oral resume` | 暂停和恢复同一场；保留待答问题。暂停期间消息保留，但不计分。 |
| `/oral end` | 明确结束并冻结本场原文；讨论诊断式随后进入整场评估任务，旧风格保留未评估答案，不事后补分。 |
| `/oral status` | 从权威记录读取状态、记录号、待答问题及送达标识。 |
| `/oral styles` | 只读查看已保存的风格、简要说明和默认值。 |

只有整条、单行、精确 `/oral` 指令被解析，所有入口统一使用正斜杠。引用、学生回答中的自然语言“开始/结束”不能切状态。非法控制指令返回明确错误。确认和生命周期指令保留控制记录，普通消息按准备、正式口试、暂停、协调或复盘分别存储。`help`、`status`、`styles` 是只读操作，不新增生命周期记录。宿主收到 help 返回后应展示 `text` 中的完整帮助，不创建方案或发起口试 task。

CLI 对等入口：`wg oral prepare [方案ID] --topic <主题>`，`save --file <草稿JSON>`，`confirm/start/pause/resume/end/status`。除只读的 `wg oral help` 无需客户端外，其他入口必须显式传 `--client`。相同操作的重试复用 `--request-id`；相同 ID 搭配不同内容被拒绝。返回的 request_id 可以用于追查确认回执。

新方案可用 `prepare --style discussion-diagnostic` 或 `--style strict-examination` 显式选风格。自然语言正文不负责解析风格开关或控制指令。

## 方案协议

方案字段固定为 `topic`、`style`、`scope`、`excluded`、`depth`、`expected_minutes`、`allow_hints`、`max_answers`、`allow_early_finish`、`objectives`。历史缺少 `style` 的输入按严格测评式兼容。准备期间范围、深度和考查点可以暂空；确认时必须完整。考查点数量 1–20，每点有 1–4 条 Raw 证据。确认版本保存完整风格快照；默认配置或说明的后续修改不改变已确认场次。

每个 objective 必需 `id`、`goal`、`criteria`、`misconceptions`、`evidence`，可选 `exploration_axes` 和 `navigation`。exploration_axes 最多8个探索维度，例如物理图像、条件变化、可观测后果或迁移，不是固定问句或必须问完的清单；正式题目按当轮实际回应生成，不生成题库或预定题序。历史 `probes` 可选兼容保留在原方案中，不进入正式回合/评估的 cards 或 assessment plan，不改写已确认版本。程序校验字段和界限，不用关键词匹配假装裁定某个维度是否具有教学价值。

核心证据 evidence 包括 `locator`、`excerpt`，程序生成 `sha256`；locator 必须精确到 Raw 段落、行号、页码等，摘录必须属于所定位的原文。既有证据 hash 不符时不能静默覆盖；应重新定位、整理并确认新方案。资料只通过现有查询能力读取，口试内核不摄入或修改 Raw。

每个 navigation 最多4条，只含 `locator`、`purpose`：仓库内 Wiki/Raw 的精确入口及其可解决的知识缺口，不携带正文、问句或答案脚本。准备保存、确认与开始时校验路径归属、片段存在、列表上限和重复项；拒绝外部 URL、路径越界、日志、暂存、整篇定位与无效片段。正式回合只传地址/用途，不反复读取或预装载所有备选正文；未使用的导航失效不阻断已有核心证据。使用时由宿主按现有查询工具核验实际内容，缺失则报告该入口不可用，不能猜测正文。

`criteria` 描述推理及适用条件，接受正确的替代解释，不要求词面一致。学生可读的方案视图只显示规则与学习目标，不返回判断要点、误解、追问或准备讨论原文；教师本地使用 `show --examiner` 查看完整内容。方案修订不改变已开始的 session，新的确认形成新版本。

`expected_minutes` 是预期时长，不是后台硬超时。自动结束条件是达到 `max_answers`（1–60）并保存该轮观察；`allow_early_finish=true` 才允许考官在已收到回应后提前宣布结束。否则由用户明确结束。讨论诊断式的预算按有效学生回应回合计，回答和提问均可推进；纯澄清/显示问题不消耗预算。连续追加回应属于同一回合，不重复计数。日后增加硬计时必须同时定义暂停扣时、掉线和通知语义，不能由模型猜测。

## 权威记录与隐私

- 权威文件为 `private/oral-exams/state.json`，含草稿、不可变方案版本、客户端绑定、独立 session、完整消息与生命周期事件、单独的 observations 和请求回执。首版采用单文件事务，锁内读改写、原子替换并 fsync；文件权限 600、目录 700，拒绝路径越界与符号链接逃逸。
- 该记录不使用 `workspace_state` 的公共 projects 存储，也不写入公共 Raw/Wiki/Graph。口试内容是工作记录，不因此成为科研事实源。
- `private/oral-exams/tasks/` 是私有任务快照；`temp/oral-exam/` 是权限受限的敏感输出暂存区，二者均排除在 Git 之外。任务或 API 日志不能替代权威记录。
- 每条正式提问、原始回答、追加回答、追问、提示、暂停期交流、复盘及开始/结束时间都保留；消息原文不裁剪。恢复读权威状态，不依赖当前聊天窗口的上下文。
- 单条输入最多 12000 字符；超过时明确拒绝，不能截断后冒充完整消息。宿主应保留未被接受的原始输入并提示学生分段重发。
- 当前是本地可信宿主接口，`client` 只用于会话隔离，不是身份认证。不能把 CLI 直接暴露给学生或网络；服务化须由宿主补充认证、授权、用户隔离、保留周期和受管删除。模式位也不等同于磁盘加密或备份保护。
- 学生与教师导出分别使用 `show` 与 `show --examiner`；`--record-id` 可恢复旧记录。此本地接口允许库所有者查看记录，不承诺远程多用户访问控制。

## 宿主接入

仓库不能仅靠 YAML 注册就拦截原生输入或新增 UI 模式。当前接入点是 `wg oral receive`、`chat` 和 CLI；宿主负责把真实消息送入入口、每轮在最终回复展示返回的 `state.banner`，绑定正确 client，并禁止来源文档或模型自行调用生命周期指令。专用 playbook 在同一口试上下文首次加载即可，不逐轮重载工程文档。

正式考官回合遵循以下顺序：

1. `task --client ...` 返回 `agent-task-v1`，其中有私有输入、声明的暂存输出、协议和 check/commit 命令。宿主仅写声明的 proposal，不改权威记录。
2. validator 验证 schema、精确 task_id、当前状态、考查点、证据归属、提示权限、回答归属和结束条件。`check` 是可选预检；`commit` 始终完整重新校验，可直接提交，不要求重复执行相同预检。程序不裁定科学正确性。
3. `commit` 重新验证并原子保存 observation 与考官消息，然后返回 `message_id/text/delivery=pending`。提交前不能把模型草稿展示为正式问题。
4. 宿主把已提交的 text 与 banner 一次放入最终回复，不在 commentary、工具进度或后台思考区域展示问题、解释或评估，也不另加重复的“请作答”。支持显示后继续运行的宿主再执行 `delivered --message-id ... --request-id ...`；最终回复后无法继续工具调用的宿主延至下次 receive 显式确认实际展示的消息，不为同轮确认而先在后台发题。
5. 下一条真实输入可用 `receive --displayed-message-id <上一轮确实已在最终回复展示的message_id>`，在同一锁内先确认该当前消息送达、再记录输入并生成 task。首轮无须该参数；未看到问题时不确认，按 clarification 保存并重发 pending_text。chat/message/task 路径仍可分步使用；正式助手消息只经受管 commit，准备和复盘实际助手发言经 message 保存。

receive 不推断 UI 送达。错误或跨 client 的 message_id、非法输入使整次输入事务回滚；同 request_id 幂等重放，不重复计答。help/status/styles 保持只读，即使附带 displayed-message-id 也不确认或发起 task。输入已接受但后续任务因 Raw 变化或预算失败时，回执含 task_error 而不含 agent_task，原文已保留；宿主明确报告并定向修复，不能重复记录答案或自行回落 API。任务 snapshot 是可重建派生物，不是新的事实源；receive 将完整 input 内联到回执，但权威请求记录只保留 manifest 引用，不重复存放整个任务包。

学生原文使用 `--intent answer|question|clarification`，由可信宿主按实际语义标记；默认 `answer`。学生主动提问或回答中的核心提问用 `question`，程序性澄清及未看到问题用 `clarification`。混合回应保留整条原文，由考官综合理解，不删去其中回答或问题。API 宿主也须传入该标签；内核不靠标点猜测语义，不增加自动分类模型调用。暂停和没有已送达发言时的交流仍不用于能力判断。

宿主实际发送的状态通知另用 `message --role assistant --purpose notice` 保存；这类通知不计分，也不会把“方案已确认”的回执误当成新一轮范围讨论而使确认失效。只能由可信宿主标记通知，不能用来绕过正式考官问题的提交门。宿主显示问题时按 message_id 去重；提交到显示再到送达回执之间的中断不能仅靠文件存储承诺 UI 恰好显示一次。

没有问题可答时的学生交流作为协调消息保留，不计入考查。生成下一轮前收到追加回答会使旧 task 失效，须重新获取，不丢失补充内容。暂停、结束或方案讨论改变也会使旧 task 失效。重试返回原提交回执；宿主仍需读取当前状态，不能把历史回执误当成当前状态。

准备时用精确 Raw locator 组织子区域，确认版本保存可重建的 reference_regions，开始时重新核验 Raw 并冻结到场次；不改 Raw 或已确认旧版本。回合与评估任务仍读取当前 Raw 验证 hash，不把缓存当事实；同一包内按 locator 去重，内联完整选定子区域，避免宿主反复开文件。

retrieval_policy 固定顺序为核心 reference_regions → 按用途读取 cards.navigation → 携带具体缺口在已确认主题内定向补查；触发缺口为核心证据不足、来源冲突、学生合理替代表述未覆盖，不因每轮换题自动检索。这是 shared 任务契约，由宿主 Agent 使用现有查询/读取工具执行，不增加独立检索状态机、子图数据库或模型调用。API 单回合不自动检索，不声称已读未提供的正文，外发仍逐次授权。

备选导航不是事实证据，也不自动升级为本场正式依据。validator 继续只接受 cards.evidence 中的 Raw locator 用于 observation/help/assessment，不能借导航绕过冻结边界。补查若需要改变正式考查范围或证据，保持不确定判断，再回准备阶段确认新版本/新场次；不静默扩面或补分。补查是否必要、探索维度是否有教学价值由宿主判断，机械程序只验证可验证的地址、协议与归属。

模型回合包提供当前目标及候选目标、最多三张完整卡片，优先未覆盖目标，其余只给目标导航；不是强制按卡片顺序提问。input.progress 给出已用/剩余回应数与已访问/未访问目标；已访问不等于掌握。当前题目和所有本轮回答保持完整，近期历史可明确节选。包预算仍为 60000 字符，超限报错而非偷偷截断学生本轮答案或 Raw。范围较大时应拆方案，不增加多模型必需依赖。

回合 proposal 固定为 `task_id/action/question/observation`。严格测评式保持 `oral-exam-v1`：action 为 ask、probe、hint、finish，observation 包含 `answer_id/objective_id/judgement/reason/evidence`，judgement 为 demonstrated、partial、incorrect、uncertain、out_of_scope。

讨论诊断式使用 `oral-exam-dialogue-v1`，action 为 `ask/probe/invite/respond/guide/correct/hint/finish`。`question` 是兼容保留的字段名，也承载回应、解释、纠正或邀请，其字段为 `objective_id/text/help/evidence`；help 为 `none/hint/explanation/correction`，非 none 必须提供当前卡片精确 Raw locator。`correct` 固定为 correction，`guide/hint` 固定为 hint，`invite` 固定为 none；普通 respond 若含知识解释也须标为 explanation，不借状态通知绕过帮助门。是否真正含实质帮助由宿主/考官负责，机械门不冒充语义裁判。`allow_hints=false` 阻止所有实质帮助。

讨论诊断式 observation 只有 `answer_id/objective_id/reason/evidence`，不含 judgement，程序标为 provisional。首次 observation=null，结束 question=null。程序记录实际回应归属、intent、帮助类型、依据、前后时序及 assisted；对帮助后的表现不直接宣称原本独立掌握。修改风格不改已确认版本或原场次规则。

**结束后的综合评估**：讨论诊断式结束时保存 `assessment_source`，冻结结束前的完整消息和事件，`assessment_status=pending`，不直接把回合观察冒充最终水平。随后继续 `task → check → commit`，获得 `oral-exam-assessment-v1` 的独立评估任务；口试状态仍为已结束，不重新开考。任务包含完整原文、所有目标卡片和暂定观察，不使用近期历史节选替代原文。评估包最多 240000 字符，超限明确报错、保留原文，不静默截断或退回摘要评估。

评估 proposal 为 `task_id/assessment`。assessment 包含 `transcript_hash/summary/coverage/objectives/interaction`。coverage 列出冻结原文全部消息 ID 且不重复；每个目标含 `objective_id/independent_understanding/reason/message_ids/raw_evidence`，独立理解判断为 demonstrated、partial、incorrect、uncertain、not_assessed。interaction 覆盖 questioning、self_correction、learning_transfer，每项含 judgement、reason、message_ids。理由说明帮助后的变化；程序补入已送达的帮助消息 ID，校验全量覆盖、原文 hash、目标和 Raw 归属、学生证据及考官消息送达凭据。仅有帮助后证据时，原本独立理解只能记为 uncertain 或 not_assessed，不据此判为 demonstrated、partial 或 incorrect；帮助后的学习变化另行描述。未观察到提问机会用 not_assessed，不因此判错。

评估只读取冻结原文，不采纳结束后的复盘回答；完成后 `assessment_status=completed`，只允许同 request_id 幂等重放，不用新请求重新打分。暂停交流、状态通知和协调消息虽完整保留，但不单独用作能力证据。coverage 和 hash 只证明机械覆盖契约，不证明模型真正理解了全文；教师验收仍必要。

## Agent 与 API

Agent 路径只返回任务和调用确定性校验/提交，不导入模型客户端、不模拟 Agent 推理，不隐式回落 API。知识查询与可选 subagent 由当前宿主安排。

API 路径使用同一已确认方案和回合协议，程序持有单回合模型调用、校验和提交循环：

```bash
ORAL_EXAM_BACKEND=api python3 .scripts/wg.py oral api-turn \
  --client class-a-student-01 --request-id api-round-01 --allow-api-sharing
```

必须同时显式选择 `ORAL_EXAM_BACKEND=api` 和授权发送资料；不能靠 QUERY_BACKEND 的值偷换口试 backend。互动发送本轮完整回应、必要历史、考查卡片和风格；整场评估发送结束前冻结的完整对话及所有卡片，须逐次显式外发授权。含敏感学生内容及可能的私有参考资料。API 不自动搜索材料，使用事先讨论、整理和确认的方案。

复用 `llm_structured.call_json` 及既有 LLM_API_BASE/API_PATH/API_KEY/MODEL 配置。互动输出上限 3200 token，完整评估上限 12000 token；超时 90 秒、自动重试 0。结束回合不会自动发起另一模型请求；须再显式调用一次 `api-turn --allow-api-sharing` 执行最终评估。失败不提交结果，仍可手动重试原 ID；评估失败时场次保持已结束/待评估，不隐式回落 Agent。成功的同 ID 重试直接返回回执，不再调用模型。既有 API 执行日志只记 hash、长度与运行元数据，不保存全文。

## 报告与验收

严格测评式报告继续从 observations 派生。讨论诊断式报告区分暂定观察和综合 assessment：结束时明确待评估，完成后按完整对话提供各目标独立理解、互动证据及帮助后学习变化，不简单累计错误或按帮助次数扣分。两者均不输出伪精确总分或永久能力标签；复盘消息不更新正式评估。

自动回归覆盖准备恢复、确认门、版本固定、重复使用方案、跨 client 隔离、显式指令、完整原文、追加回答、送达、中断、暂停/结束、陈旧输出、幂等、证据和隐私门、Agent/API 共用协议。执行：

```bash
python3 .scripts/test_oral_exam.py
python3 .scripts/test_wg.py
python3 .scripts/test_function_registry.py
python3 .scripts/test_agent_task.py
python3 .scripts/test_playbook_dispatch.py
python3 .scripts/engineering_graph.py validate
```

快路径回归还覆盖延后显式送达、送达与输入原子性、只读命令不确认、内联任务、Raw 子区域去重/变更、候选覆盖及两种风格共用教授指引。常规 Agent 回合从 receive 到 commit 只需两次串行工具往返（写 proposal 可与 commit 合并），下一轮输入同时确认上一轮送达；这是调用结构的减少，不承诺实际模型耗时或 UI 网络延迟。

准备检索范围回归覆盖无预设题目的新目标卡、旧 probes 保留但不进入正式任务、导航安全边界/不预装载、核心优先与缺口补查契约、导航不能变成评分或帮助依据，以及 Agent/API 共用但 API 不隐式检索。正式口试的题目质量与检索收益仍需实际试用验证。

模型质量验收另行进行：教师审核主题卡片，再用正确但表达不同、部分正确、错误前提、超范围、提示后正确等真实样例测试弱模型，核对追问与判断。程序/模拟 API 测试通过不等于真实模型或专家验收通过；完成真实试点前保持 preview。首版不做长期画像、排行榜、后台计时或复杂多考官。
