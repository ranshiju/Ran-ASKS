# Changelog

This log highlights important user-facing capabilities and behavior changes.
Minor fixes and internal adjustments are summarized together to keep it
readable. Frozen paper artifacts remain governed by their own versioned
manifests and checksums.

## [Unreleased]

### Highlights

- Batched Hub membership embeddings across profiles, added canonical route
  review handoffs, and blocked redistribution until child Hub scopes are ready.
- Added entity node-origin lineage and conservative re-ingest cleanup without
  deleting historical or shared nodes.
- Added one-shot sparse semantic recovery and deterministic metadata subtype
  repair so venues and people do not enter ordinary Hub membership.
- Made public regressions independent of private embedding caches, experience
  files, and playbooks; synchronized Hub route locators with the current spec.

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

[Unreleased]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.4...HEAD
[0.2.4]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ranshiju/Ran-ASKS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ranshiju/Ran-ASKS/releases/tag/v0.2.0
