# 完整摄入性能验收

`.scripts/benchmark_ingest_acceptance.py` 是确定性、离线的证据聚合器。它读取受信任工程 manifest 所指向的已保存记录，不执行摄入、网络请求、资料内命令或科学推理，不改 Raw/Wiki/graph。机械实现归属 shared；当前完整验收配置针对用户指定的 API 论文摄入。它与 `benchmark_ingest_performance.py` 的固定延迟回放互补；回放不能作为真实端到端时长。

## 调用

```bash
python3 .scripts/benchmark_ingest_acceptance.py \
  --root /path/to/WikiGraph \
  --manifest temp/experiment/manifest.json \
  --output temp/experiment/acceptance.json
python3 .scripts/test_benchmark_ingest_acceptance.py
```

省略 output 时输出 JSON。输出只能是 root/temp 内的新文件，不覆盖已有证据或旧报告。输入路径必须相对 root，拒绝绝对路径、`..` 和 symlink。资料内容和审阅内 locator 仅作数据，不用于追加文件访问。

## 输入与合格判定

manifest 的 schema 为 `ingest-acceptance-manifest-v1`，必须预先声明非空、唯一的 `expected_cases`。runs 每项有唯一 `id`、`case`、`variant`（baseline/candidate）、`scope`、`event_coverage`、`context`、`files`。`scope=full_ingestion` 与 `event_coverage=complete` 都是受信任实验 harness 的声明；不能把阶段实验或截断日志标成完整。

files 按角色记录 `{path, sha256}`。完整记录需要：

- input：输入 PDF；source：本次解析 Raw 文本；wiki/semantics：最终落位产物；checklist：调用前冻结的质量检查表。
- pipeline：最终 state，必须 completed、quality_status=complete，无未解决错误、质量告警或 handoff；现有 verification_receipt 必须 PASS/committed，事务与 Wiki 哈希匹配，cleanup_pending=false。
- maintenance：统一维护对象或含 maintenance 的完整报告。顶层完成且无 action/error，publication 完成；abbreviations、people、hubs 分别 completed/no_action，trigger_files 绑定同一事务。
- audit：外部来源审阅。scientific_pass=true、issues 为空；checks 中 bibliography/source_support/conditions/uncertainty/relations/downstream_answers 全部为 pass；artifact_sha256 绑定 source/wiki/semantics/checklist，transaction_id 绑定原事务。旧 audit 的 source_sha256 仅可补充 source 绑定，不能替代其他绑定。审阅由宿主或已验证的外部审阅流程完成；该工具只检查记录齐全且一致，不证明科学蕴含。
- timing：符合下文的连续计时。历史 start 可另外提供 `start` 角色；读取旧计时只恢复其可观测时长，不升级旧边界。
- events：完整 canonical `llm_api_call` 事件数组，保留所有失败、重试和缺失用量。每个事件须绑定事务；其他事件类型忽略。旧 call_text response 可由受信任导入器按原 history 逐次转换，须保留原响应及转换说明。

Frontier 必须记录 backend/answer_policy=api，没有错误，answer_attempts 对本次 count 覆盖完整，所有回答完成。零问题必须有明确 count=0 与空列表。此项检查流程完成，不判断回答事实质量；后者由来源与下游问答审阅负责。

## 时间边界

新 timing schema 为 `ingest-qualified-elapsed-v1`，boundary 为 `accepted-input-through-runtime-completion`。harness 从受理已定位单篇输入、进入摄入入口起同时记录 wall_start 与单调时钟；直至所有运行步骤与流程内质量检查结束再记录 wall_end、elapsed_sec。需要修复时，计时必须覆盖返修与等待；不能把几个已完成的短阶段拼成一次完整成功。

必须记录 transaction_id、complete=true、deducted_sec=0；includes 精确为以下顺序列表：

```json
["entry", "extraction", "generation", "repairs", "graph", "frontier", "maintenance", "publication", "runtime_quality_checks", "waiting"]
```

`audit.review_mode=runtime` 时 review_end_epoch 必须落在计时区间。实验验收可使用 external_read_only，必须声明 artifacts_modified=false、绑定计时结束时冻结的同一批产物，并单独记录审阅结束时刻；其纯评测等待不计入算法延迟。任何实际修复都不能用 external_read_only 隐藏。单调耗时与墙钟差不超过 1 秒；异机、进程恢复或重启后不能验证连续时钟的记录应保持未合格。流程所需审阅等待和返修计入连续时长；入口内部路由/分类不可扣除。自然语言任务澄清发生在受理输入之前，不作为模型服务时延。

工具输出 observed_elapsed_sec 和 qualified_elapsed_sec。后者仅在全部门通过时存在；父子 span、并发请求耗时不求和替代墙钟。多次实验的 known_observed_work_sec 只是已观测工作量之和，不是单篇时延或完整项目累计成本。

## 配对与费用

context 必须有实际 SHA-256：input_sha256、initial_graph_sha256、initial_cache_sha256、model_config_sha256、parser_config_sha256、checklist_sha256；measurement_boundary 必须匹配上述边界。模型/解析配置使用不含密钥的实际生效配置快照哈希；图与缓存来自运行前冻结快照。input/checklist 哈希同时与文件核验。其余起点/配置哈希由受信任 harness 冻结，评估器不重新连接数据库或服务核实。

每个预先声明 case 必须恰有一份合格 baseline 和 candidate，且以上所有对照条件相同，才计算 candidate/baseline。全部 case 比值 ≤0.70 才报告 `target_accepted_for_declared_cases=true`；缺样本、重复挑选、多次运行选最快、失败快速返回和配对配置不同均不通过。单组配对不支持总体稳定性或 p95 声明。代码版本可因优化不同，但应在实验 manifest 另行冻结，不能偷偷更换模型/解析器或起始缓存。

事件按 event_id，或事务/call_id/profile/attempt 去重。同身份内容冲突使记录不合格；缺身份的事件逐条保留并标记。跨 run 重复使用同一 provider attempt 会使关联 run 不合格，aggregate_cost_verified=false，此时汇总值不得当作已核实账单。费用只回报 provider 已知 token 数、失败/重试数与 missing_usage_attempts；缺失用量不等于免费，currency_cost=null。PDF、embedding、宿主审阅费用不在 token 汇总中。

## 历史数据和退出条件

旧记录的 completed/quality_status=complete 仅表示当时程序状态，不能覆盖来源审阅失败、维护待办或新契约缺项。对旧资料缺少新格式字段的诊断表示“证据不足”，不宣称对应实际步骤必然失败。既有待办可记录归因，但不得为了通过验收静默排除必要维护或事后扣除耗时。

如果现有产物已知存在科学错误，先修复责任组件并离线验证；不为了取得新的计时重复发送相同失败配置。新候选仍须真实运行并接受相同审阅；评估器、合成测试或历史记录导入本身不建立合格基线。
