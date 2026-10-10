# 智能口试

## 功能边界

`oral_exam` 是新的持续工作状态，不是查询模式或普通聊天人设。内含准备中、待开始、口试中、已暂停、已结束五种明确状态。准备讨论可以跨会话恢复；同一方案可以重复开考，每场生成独立记录。当前为 preview，先提供可信的状态壳和可实测的考官回合，模型评估不是认证成绩。

共享内核 `.scripts/oral_exam.py` 管理方案、状态、指令、证据校验、幂等提交和记录。宿主 Agent 持有 Agent 控制循环；`.scripts/oral_exam_api.py` 只执行显式授权的 API 回合。辅助知识查询不切换口试状态，subagent 可以辅助准备或复核，但不自行控制学生会话生命周期。

## 风格与默认

**默认风格：讨论诊断式**，标识 `discussion-diagnostic`。简要说明：通过双向讨论诊断理解；允许学生提问、考官纠正与引导自纠，依据完整对话评价独立理解及学习变化。

风格及默认值的唯一配置源为 `operations/config/oral-exam-styles.json`。新建方案采用该默认值，默认允许提示和解释；准备期间可以讨论并调整 `allow_hints`。风格和规则随方案确认版本冻结，不在开考后改变。既有无 `style` 的方案/场次按 `strict-examination`（严格测评式）兼容，不追溯改写原记录。复用旧方案仍保留旧风格；改成讨论诊断式须在准备草稿中显式修改 `style`、帮助规则并重新确认。

考官围绕实际观点回应、澄清、讨论例子、检查条件，中性邀请学生提问。可以当场解释、纠正或引导自纠；实质帮助须回溯知识库 Raw，并留下帮助类型和原文。记录知识表现的变化，不用后来正确的答案覆盖原始错误。评价不是逐题错误数相加，也不按提问数量打分；区分独立理解、提问质量、自我纠正、解释吸收和迁移。对笔误先澄清，对不确定的科学判断先核验；帮助是理解过程的上下文，不是惩罚标签。

## 用户入口

宿主将用户单独发送的指令交给 `wg oral chat`，使用固定的 `--client` 和每条消息唯一的 `--request-id`：

```bash
python3 .scripts/wg.py oral chat --client class-a-student-01 --request-id msg-001 --text '/oral prepare 张量网络'
python3 .scripts/wg.py oral chat --client class-a-student-01 --request-id msg-002 --text '重点考查物理理解，允许少量提示。'
python3 .scripts/wg.py oral task --client class-a-student-01
```

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

每个 objective 有 `id`、`goal`、`criteria`、`misconceptions`、`probes`、`evidence`。证据包括 `locator`、`excerpt`，程序生成 `sha256`；locator 必须精确到 Raw 段落、行号、页码等，摘录必须属于所定位的原文。既有证据 hash 不符时不能静默覆盖；应重新定位、整理并确认新方案。资料只通过现有查询能力读取，口试内核不摄入或修改 Raw。

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

仓库不能仅靠 YAML 注册就拦截 Codex 原生输入或新增 UI 模式。当前接入点是 `wg oral chat` 和 CLI；宿主负责把真实消息送入入口、每轮展示返回的 `state.banner`，绑定正确 client，并禁止来源文档或模型自行调用生命周期指令。

正式考官回合遵循以下顺序：

1. `task --client ...` 返回 `agent-task-v1`，其中有私有输入、声明的暂存输出、协议和 check/commit 命令。宿主仅写声明的 proposal，不改权威记录。
2. `check` 验证 schema、精确 task_id、当前状态、考查点、证据归属、提示权限、回答归属和结束条件。程序不裁定科学正确性。
3. `commit` 重新验证并原子保存 observation 与考官消息，然后返回 `message_id/text/delivery=pending`。提交前不能把模型草稿展示为正式问题。
4. 宿主把已提交消息实际显示给学生，再执行 `delivered --message-id ... --request-id ...`。失败或中断时依据 status 中的 pending_text 与送达状态恢复；未送达的问题不能计入学生回答。
5. 学生消息通过 `chat` 或 `message --role user` 进入权威记录，再获取下一轮 task。正式助手消息只经受管 commit；准备和复盘的实际助手发言经 `message --role assistant` 保存。

学生原文使用 `--intent answer|question|clarification`，由可信宿主按实际语义标记；默认 `answer`。学生主动提问或回答中的核心提问用 `question`，程序性澄清及未看到问题用 `clarification`。混合回应保留整条原文，由考官综合理解，不删去其中回答或问题。API 宿主也须传入该标签；内核不靠标点猜测语义，不增加自动分类模型调用。暂停和没有已送达发言时的交流仍不用于能力判断。

宿主实际发送的状态通知另用 `message --role assistant --purpose notice` 保存；这类通知不计分，也不会把“方案已确认”的回执误当成新一轮范围讨论而使确认失效。只能由可信宿主标记通知，不能用来绕过正式考官问题的提交门。宿主显示问题时按 message_id 去重；提交到显示再到送达回执之间的中断不能仅靠文件存储承诺 UI 恰好显示一次。

没有问题可答时的学生交流作为协调消息保留，不计入考查。生成下一轮前收到追加回答会使旧 task 失效，须重新获取，不丢失补充内容。暂停、结束或方案讨论改变也会使旧 task 失效。重试返回原提交回执；宿主仍需读取当前状态，不能把历史回执误当成当前状态。

模型任务包只提供当前考查点和一个待覆盖考查点的完整卡片，其余只给目标导航；当前题目和所有本轮回答保持完整，近期历史可明确节选。首版包预算为 60000 字符，超限报错而非偷偷截断学生本轮答案。范围较大时应拆方案，不增加多模型必需依赖。

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
python3 .scripts/engineering_graph.py validate
```

模型质量验收另行进行：教师审核主题卡片，再用正确但表达不同、部分正确、错误前提、超范围、提示后正确等真实样例测试弱模型，核对追问与判断。程序/模拟 API 测试通过不等于真实模型或专家验收通过；完成真实试点前保持 preview。首版不做长期画像、排行榜、后台计时或复杂多考官。
