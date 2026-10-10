# Changelog

This log highlights important user-facing capabilities and behavior changes.
Minor fixes and internal adjustments are summarized together to keep it
readable. Frozen paper artifacts remain governed by their own versioned
manifests and checksums.

## [Unreleased]

## [0.15.0] - 2026-10-10

### Version decision / 版本判断

- MINOR: Add backward-compatible preview oral-examination plans, explicit lifecycle, discussion-diagnostic dialogue and complete-transcript assessment
- MINOR（中文）：新增向后兼容的 preview 智能口试方案、显式生命周期、讨论诊断式互动与完整对话评估

### Highlights

- Added a preview intelligent oral-examination state with reusable, evidence-backed
  preparation plans, explicit start/pause/resume/end commands, isolated private
  records, complete transcripts, and separate Agent/API controllers.
- Made discussion-diagnostic dialogue the default for new plans: students may
  ask questions, examiners may correct or guide self-correction, and final
  assessment distinguishes independent understanding from learning after help.
  Confirmed legacy plans retain their original rules.
- Added a standalone `/oral help` guide and strengthened source-bound meeting
  identity corrections, unresolved-role handling, and transaction-footnote binding.
  API sharing remains explicitly authorized per call; model-quality acceptance
  still requires real trials and teacher review.

### 主要更新

- 新增 preview 智能口试状态：支持有知识库依据的可复用准备方案、显式开始／暂停／
  恢复／结束指令、私有隔离记录与完整对话，Agent 与 API 分别持有控制循环。
- 新方案默认采用讨论诊断式，允许学生提问、考官纠正和引导自纠；结束后基于完整
  对话区分独立理解与帮助后的学习变化，已确认的旧方案保留原规则。
- 新增独立 `/oral help` 使用帮助，并强化会议身份纠正的来源依据、未确认角色处理
  与事务脚注绑定。API 外发仍需逐次授权；模型质量仍需真实试用与教师验收。

## [0.14.0] - 2026-10-10

### Version decision / 版本判断

- MINOR: Add backward-compatible opt-in PDF extraction recovery, evidence-scoped Frontier answer history, and post-split Hub maintenance reconciliation
- MINOR（中文）：新增向后兼容的显式授权 PDF 提取恢复、按证据范围记录的 Frontier 回答历史与 Hub 分裂后维护对账

### Highlights

- Added an opt-in official PaddleOCR document-parsing fallback for public PDFs,
  with per-run upload authorization, resumable jobs, bounded network transfers,
  and retained extraction provenance; private material cannot use this backend.
- Added evidence-scoped Frontier answer history and refresh comparison, with
  source-first retrieval and exact Raw excerpts. Reference lists and ordinary
  expectations no longer masquerade as author-proposed future research.
- Closed post-split Hub maintenance handoffs through read-only membership and
  Scope-history reconciliation, preserving unrelated review actions and warnings.
- Hardened ingestion and navigation around source-bound paper evidence,
  incompatible graph-node identities, temporal provenance, and failure recovery.

### 主要更新

- 新增面向公开 PDF、显式启用的 PaddleOCR 官网文档解析回退，要求逐次上传授权，
  支持任务恢复、有界网络传输和提取来源记录；private 材料禁止使用该后端。
- 新增按证据范围记录的 Frontier 回答历史与刷新比较，优先读取问题来源并使用
  精确 Raw 摘录；过滤参考文献和普通预期，减少将它们误判为作者未来研究的情况。
- 通过只读核验成员迁移与 Scope 历史，闭合 Hub 分裂后的维护交接，同时保留其他
  待审动作和独立质量告警。
- 强化摄入与导航中的论文来源证据、不兼容图节点身份、时间来源及失败恢复边界。

## [0.13.0] - 2026-10-07

### Version decision / 版本判断

- MINOR: Add backward-compatible evidence-bound Agent paper workspaces and persistent concept-identity governance
- MINOR（中文）：新增向后兼容的证据绑定 Agent 论文工作区与持久概念身份治理

### Highlights

- Strengthened Agent-mode paper ingestion with a single versioned
  `BIBLIOGRAPHIC`/`WIKI`/`SLOTS` workspace, source-line evidence for every
  non-empty bibliographic field, adaptive paper reading, explicit semantic
  predicate roles, and transaction-bound validation and commit handoff.
- Added fail-closed inbox-classification evidence checks and persistent
  concept-identity distinctions that are applied before embedding-based
  resolution, preventing reviewed names from being merged again.

### 主要更新

- 强化 Agent 模式论文摄入：使用单一版本化的
  `BIBLIOGRAPHIC`／`WIKI`／`SLOTS` 工作区，要求每个非空书目字段绑定来源行证据，
  支持自适应论文阅读、显式语义谓词角色，以及与原事务绑定的校验和提交接力。
- 新增失败关闭的 inbox 分类引文检查，并持久保存概念身份区分，在 embedding
  身份解析前优先应用，避免已人工确认不同的名称再次被合并。

## [0.12.0] - 2026-10-04

### Version decision / 版本判断

- MINOR: Add reproducible installation, offline first-run diagnostics and demo, unified tests, explicit threat modeling, and immutable version tags
- MINOR（中文）：新增可复现安装、离线首次运行诊断与演示、统一测试、显式威胁模型和不可变版本标签

### Highlights

- Added a reproducible `uv`-locked Python package with the `ran-asks` command,
  offline first-run diagnostics, a synthetic evidence-trace demo, and unified
  profiled test suites.
- Added explicit prompt-injection and data-plane threat boundaries, evaluated
  provider profile examples, and frozen CI installation from the committed
  lockfile.
- Added declarative ingest transition enforcement, hash-bound final verification
  receipts with read-only status/next/verify commands, and an offline synthetic
  weak-model protocol governance exam.
- From `v0.12.0`, every published public-tree version receives an immutable
  annotated Git tag; pushing publishes the branch and tag atomically, while a
  GitHub Release remains a separate explicitly authorized action.

### 主要更新

- 新增由 `uv` 锁定的可复现 Python 包与 `ran-asks` 命令，并提供离线首次运行诊断、
  合成证据回溯演示和统一的分层测试入口。
- 新增显式的提示注入与数据平面威胁边界、经评估的服务商配置示例，以及基于已提交
  锁文件的冻结 CI 安装。
- 新增声明式摄入状态转换校验、带哈希绑定的最终复验回执与只读 status/next/verify
  命令，并加入离线合成的弱模型协议治理试卷。
- 自 `v0.12.0` 起，每个已发布的公开树版本都获得不可变 annotated Git 标签；推送时
  原子发布分支与标签，而 GitHub Release 仍是需要单独明确授权的动作。

## [0.11.0] - 2026-10-03

### Version decision / 版本判断

- MINOR: Add backward-compatible knowledge graph visualization, governed slide-library reuse, expanded presentation generation modes, and stronger evidence-bound meeting ingestion and maintenance recovery.
- MINOR（中文）：新增向后兼容的知识图可视化、受管幻灯片模板复用、扩展演示文稿生成模式，并强化证据绑定的会议摄入与维护恢复。

### Highlights

- Added bounded, source-aware knowledge graph visualization through
  `wg.py graph-visualize`, with topic, neighborhood, and relation filters plus
  local provenance output.
- Added governed reusable slide-template management through
  `wg.py slide-library`, distinguishing direct reuse from parameterized
  adaptation while preserving source and version receipts.
- Expanded native presentation production modes and strengthened
  evidence-bound meeting compilation, final-decision handling,
  student-guidance projection, people-page provenance, and recoverable
  maintenance publication.

### 主要更新

- 新增通过 `wg.py graph-visualize` 使用的有界、来源感知知识图可视化，支持主题、
  邻域和关系筛选，并输出本地 provenance。
- 新增通过 `wg.py slide-library` 使用的受管幻灯片模板管理，区分直接复用与参数化
  适配，同时保留来源和版本回执。
- 扩展原生演示文稿生产模式，并强化证据绑定的会议编译、最终决议处理、学生指导
  投影、人物页 provenance 与可恢复的维护发布。

## [0.10.0] - 2026-09-26

### Version decision / 版本判断

- MINOR: Add public governance for independent community downstreams, evaluated ecosystem status, generated-tree contribution round trips, and a downstream registration template.
- MINOR（中文）：新增独立社区下游治理、受评估的生态身份、生成式公开树贡献回灌流程和社区下游登记模板。

### Highlights

- Added public governance for independently maintained Community Downstreams,
  evaluated ASKS-Compatible status, later explicit Reference Distribution
  designation, and the contribution round trip required by the generated
  GitHub release tree.
- Added a non-normative downstream record template for exact upstream baseline,
  maintainer ownership, Core modifications, checks, domain evaluations, data
  boundaries, and upstream candidates without prematurely defining a plugin API.

### 主要更新

- 新增独立 Community Downstream、受评估的 ASKS-Compatible 身份、后续明确授予
  Reference Distribution 的公开治理规则，并固化生成式 GitHub 公开树所需的贡献
  回灌流程。
- 新增非规范性的社区下游登记模板，用于记录精确上游基线、维护责任、Core 修改、
  校验、领域评测、数据边界与上游候选，不提前定义插件 API。

## [0.9.3] - 2026-09-26

### Version decision / 版本判断

- PATCH: Add the pinned python-pptx dependency required by editable-presentation runtime imports during public function-registry validation.
- PATCH（中文）：补充公开功能注册表校验导入可编辑演示文稿工具所需的固定 python-pptx 依赖。

### Highlights

- Added the pinned python-pptx dependency required when the public runtime
  function registry imports editable-presentation tooling on a fresh CI runner.

### 主要更新

- 在 CI 固定依赖中补充 python-pptx，确保公开运行时功能注册表可在全新 runner
  中导入可编辑演示文稿工具。

## [0.9.2] - 2026-09-26

### Version decision / 版本判断

- PATCH: Complete the pinned GitHub Actions verification environment with NumPy and Pillow for runtime function-registry validation.
- PATCH（中文）：在 GitHub Actions 固定校验环境中补充 NumPy 与 Pillow，以支持运行时功能注册表校验。

### Highlights

- Completed the pinned GitHub Actions verification environment with NumPy and
  Pillow, matching the runtime imports exercised by the public function-registry
  validation.

### 主要更新

- 在 GitHub Actions 固定校验环境中补充 NumPy 与 Pillow，使其覆盖公开功能注册表
  运行时校验实际导入的依赖。

## [0.9.1] - 2026-09-26

### Version decision / 版本判断

- PATCH: Expose uncaught public-release regression tracebacks as machine-readable GitHub Actions error annotations for reliable CI diagnosis.
- PATCH（中文）：将公开发布回归中的未捕获异常输出为机器可读的 GitHub Actions 错误注解，便于可靠定位 CI 故障。

### Highlights

- GitHub Actions now emits uncaught public-release regression tracebacks as
  machine-readable error annotations, so CI failures remain diagnosable through
  the public Checks API even when raw logs are unavailable.

### 主要更新

- GitHub Actions 现在会把公开发布回归中的未捕获异常输出为机器可读的错误注解；
  即使原始日志不可用，也能通过公开 Checks API 定位 CI 故障。

## [0.9.0] - 2026-09-26

### Version decision / 版本判断

- MINOR: Add backward-compatible committed-snapshot publication with verifiable provenance, transactional installation, Git/GitHub delivery guards, CI verification, mechanical PDF health checks, and bilingual release notes.
- MINOR（中文）：新增向后兼容的已提交源快照发布流程，包括可验证来源记录、事务安装、Git与GitHub交付门禁、CI校验、PDF机械健康检查和双语更新日志。

### Highlights

- Added committed-source publication orchestration with release provenance,
  transactional clean-tree installation, explicit Git remote/branch guards,
  post-push remote commit confirmation, and a GitHub Actions release gate.
- Added mechanical PDF page, text, missing-glyph, and nonblank-render checks;
  release notes and semantic version rationales are now required in both
  English and Chinese.

### 主要更新

- 新增基于已提交源快照的公开发布编排，记录发布来源，事务性安装 clean 树，
  显式校验 Git 远端与分支，并在推送后确认远端提交；GitHub Actions 同步执行发布门禁。
- 新增 PDF 逐页、文本、缺字标记和非空渲染机械检查；更新日志和语义版本判断
  从本版本起必须同时提供英文与中文。

## [0.8.0] - 2026-09-26

### Version decision / 版本判断

- MINOR: Add backward-compatible persistent Agent task handoff, physically isolated private ingestion/query, reusable native-slide tooling, governed presentation artwork, and explicit provider API paths while preserving existing public and frozen-artifact contracts.
- MINOR（中文）：新增向后兼容的 Agent 持久任务接力、物理隔离的私有摄入与查询、可复用原生幻灯片工具、受管演示图稿和显式服务商 API 路径，同时保持既有公开契约与冻结论文产物边界。

### Highlights

- Added a persistent Agent task handoff that lets a later host inspect and
  advance prepared ingestion transactions without replaying source analysis or
  bypassing the original validators and commit boundary.
- Added physically isolated private ingestion and query workflows whose Raw,
  Wiki, graph, receipts, and indexes remain separate from the public domains;
  the public template includes the reusable implementation, never private data.
- Split native presentation handling into deterministic structure extraction,
  source-bound visual reading, reusable slide/component libraries, and
  governed editable artwork, while retaining explicit remote-call consent and
  review receipts.
- Unified provider API path configuration for chat and embedding endpoints and
  strengthened evidence-bound meeting compilation, duplicate detection,
  source locators, graph planning, and ingestion recovery.

### 主要更新

- 新增 Agent 持久任务接力，使后续宿主可检查并推进已准备的摄入事务，且不重放来源分析或绕过原校验与提交边界。
- 新增物理隔离的私有摄入与查询；私有 Raw、Wiki、图、回执和索引不进入公共域，公开模板只包含通用实现。
- 将原生演示文稿处理拆分为确定性结构提取、绑定来源的视觉识读、可复用幻灯片组件和受管可编辑图稿，并保留远程调用授权与复核回执。
- 统一 Chat 与 Embedding 服务商 API 路径配置，并强化证据绑定的会议编译、重复检测、来源定位、图计划和摄入恢复。

## [0.7.0] - 2026-09-23

### Version decision

- MINOR: Add compatible registered artifact workflows for template-driven CVs and native presentations, strengthen ingestion and engineering validation, and preserve existing evidence and frozen-artifact contracts.

### Highlights

- Added a template-driven, evidence-gated bilingual CV workspace function with
  dated immutable DOCX versions, version manifests, comparisons, and a fully
  anonymized public academic-CV template package.
- Added a canonical runtime function registry that keeps persistent states,
  user-facing functions, route capabilities, unified CLI bindings, and backend
  ownership aligned and mechanically validated.
- Added local native presentation authoring with persistent revisions,
  approval/locking boundaries, constrained layouts, deterministic delivery,
  and source-sensitivity inheritance.
- Hardened PDF extraction, ingestion recovery, graph validation, engineering
  impact analysis, and isolated private-knowledge maintenance while preserving
  the Raw evidence boundary and existing frozen paper artifacts.

## [0.6.0] - 2026-09-12

### Version decision

- MINOR: Add compatible chat file/text intake with original retention and native PPTX ingestion with source-bound slide review; preserve existing managed commits and frozen paper artifacts.

### Highlights

- Added chat-file and UTF-8 text intake through the existing inbox pipeline,
  preserving original files across success, duplicate handling, and recovery.
- Added native PPTX extraction and source-bound, full-slide host review before
  semantic generation, with atomic original/companion/provenance archiving.
- Bound meeting sources to final Raw paths after title changes; derive ordinary
  document dates from evidence and retain unknown dates instead of ingestion dates.
- Separated consent-gated image API transcription/review from the semantic
  backend, improved retry diagnostics and visual configuration, and expanded
  ingestion and source-integrity regression coverage. The editable-PPT model
  default changes independently; no reconstruction-quality improvement is claimed.
- Synchronized the English/Chinese guides and printable introduction; preserved
  the existing frozen paper artifacts and experimental boundaries.

## [0.5.0] - 2026-09-08

### Version decision

- MINOR: Add compatible reusable image OCR, reviewed image ingestion and managed source archiving; preserve existing ingestion contracts and frozen paper artifacts.

### Highlights

- Added reusable, consent-gated image OCR with source-bound receipts, paired
  original/Markdown Raw packages, and persistent extraction and review provenance.
- Added field-level risk review with separate Agent and human identities;
  unresolved critical fields block ingestion, while non-critical limitations
  remain visible without turning completed ingestion into a user task.
- Made image-source confidence conservative, preserved blank form fields, and
  deferred source cleanup until final validation and persisted completion;
  cleanup retries do not replay ingestion.
- Synchronized source identity, source-addressed reading, shared ingestion
  recovery, and engineering guidance with the current Agent/API boundaries.
- Updated both READMEs and the Chinese introduction with a dated PDF addendum;
  frozen paper artifacts retain their existing reproducibility boundaries.
- Added semantic version preparation with a recorded compatibility rationale,
  synchronized root and public changelogs, and a publication gate that rejects
  unchanged or decreasing versions, independently of tag/Release creation.
- Consolidated Hub routing and scope maintenance, unified 30-member capacity
  triggers, batched membership embeddings, and child-scope readiness checks.
- Preserved entity-origin lineage during re-ingestion, hardened proposition
  identity and sparse semantic recovery, and removed private-cache dependencies
  from public regression tests.

## [0.4.0] - 2026-09-04

### Highlights

- Added guarded, project-scoped comic image generation through registered
  remote models, including dry-run validation, explicit remote-call consent,
  output-path protection, atomic image writes, and audit receipts.
- Hardened agent-backed ingestion so typed continuation states survive the DSH
  subprocess boundary, resume reports retain graph and quality state, and
  non-blocking semantic warnings remain visible without blocking valid commits.
- Tightened source fidelity for meeting-like office documents, inferred dates,
  department metadata, and responsibility relations; speaking at a meeting is
  no longer treated as evidence of departmental ownership or responsibility.
- Added child-specific evidence gating for paper-to-Hub routing and durable,
  transaction-linked origins for Agent-confirmed route corrections.
### Documentation and compatibility

- Published the Chinese introduction as a native Markdown reading view with
  inline figures and made it the default README link. The PDF remains available
  for download and printing.
- Kept the public READMEs and Chinese introduction synchronized with the unified
  ingestion architecture, and extended the release gate to require the Markdown
  guide and its version-scope note on every GitHub update. Minor release-policy
  and validation details were updated accordingly.

## [0.3.0] - 2026-09-03

### Highlights

- Paper, meeting, and general-document ingestion now share one validated,
  transactional graph-compilation path. Updates are auditable and cannot leave
  partially written graph state.
- Meeting-minute ingestion now uses one bounded specialist to normalize the
  transcript, compile the Wiki page, and extract semantic relations together,
  with deterministic validation and resumable handoff.
- Semantic failures now use bounded recovery and per-request diagnostics
  instead of repeating entire ingestion jobs blindly.
- Per-source concept descriptions and provenance improve entity matching,
  evidence navigation, re-ingestion cleanup, and Hub maintenance without
  overwriting shared or historical knowledge.

### Other changes

- General reliability, test, and documentation improvements across PDF
  bibliography extraction, Hub lifecycle operations, hybrid retrieval, DSH
  guards, and public-release validation.

## [0.2.4] - 2026-09-02

### Highlights

- Added bounded API Worker ingestion with adaptive reasoning, resumable
  checkpoints, and parallel preparation followed by controlled commit.
- Expanded provenance, graph validation, DSH, visual QA, and editable
  presentation support for agent-driven knowledge maintenance.
- Improved PDF ingestion reliability, bibliographic consistency, author
  validation, batch reporting, and related robustness issues.

## [0.2.3] - 2026-09-01

- Replaced the Chinese introduction PDF with the corrected user-produced
  rendering without changing its documented version scope.

## [0.2.2] - 2026-09-01

- Added the dated Chinese project introduction and its version-scope note.
- Added arXiv and AI-agent citation guidance.

## [0.2.1] - 2026-08-31

- Published external audit artifact `1.1.0` for the post-arXiv manuscript while
  preserving the immutable `v0.2.0` artifact boundary.

## [0.2.0] - 2026-08-30

- Initial public Ran-ASKS release with the source-available engineering
  template and frozen paper artifact `1.0.0`.

[0.3.0]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.0

[0.12.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.12.0

[0.13.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.13.0

[0.14.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.14.0

[Unreleased]: https://github.com/ranshiju/Ran-ASKS/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.15.0
