# Changelog

This log highlights important user-facing capabilities and behavior changes.
Minor fixes and internal adjustments are summarized together to keep it
readable. Frozen paper artifacts remain governed by their own versioned
manifests and checksums.

## [Unreleased]

- Kept the public READMEs and Chinese introduction synchronized with the unified
  ingestion architecture, and added a release gate requiring those reader-facing
  documents to accompany every GitHub update.

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
