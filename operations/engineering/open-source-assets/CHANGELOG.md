# Changelog

This log highlights important user-facing capabilities and behavior changes.
Minor fixes and internal adjustments are summarized together to keep it
readable. Frozen paper artifacts remain governed by their own versioned
manifests and checksums.

## [Unreleased]

## [0.8.0] - 2026-09-26

### Version decision

- MINOR: Add backward-compatible persistent Agent task handoff, physically isolated private ingestion/query, reusable native-slide tooling, governed presentation artwork, and explicit provider API paths while preserving existing public and frozen-artifact contracts.

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

[Unreleased]: https://github.com/ranshiju/Ran-ASKS/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.0
