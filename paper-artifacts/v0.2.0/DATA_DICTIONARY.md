# Data Dictionary

This is paper artifact `1.0.0`, generated from frozen run `bd7dc4b6e166` and
bound to Ran-ASKS code release `v0.2.0`. Artifact versioning is independent of
the rolling ASKS development version.

## Interpretation boundary

The Wiki, graph, Hubs, macro-domain labels, and paper-to-Hub assignments are
navigation structures. They are useful for inspecting and traversing the
compiled research structure and may contain approximate or revisable
interpretations. Factual use should return to the formally published paper
identified by the corpus manifest.

The frozen experiment uses uppercase `G` and `D` labels for total graph states
and document-run identifiers. Lowercase variables in the paper denote an
individual index or local quantity.

## Corpus

`corpus/manifest.csv` and `corpus/manifest.json` describe all 65 reviewed
candidates. `decision=include` identifies the 56 independently published works
used in the chronological trajectory. `canonical_pdf_sha256` identifies the
locally verified PDF without distributing it. Formal publication year fixes
chronology, including the paper's documented online-first boundary cases.

## Wiki

`wiki/papers/` contains one compiled Markdown view per included work.
`wiki/hubs/` contains the 18 Hubs active in `G056`. Source locators use the
placeholder prefix `raw-not-distributed/`; the referenced source files are not
part of this artifact.

## Graph

`graph/final-graph.jsonl` is the sanitized complete export of `G056`. Its first
line declares the snapshot schema; subsequent records carry `_table`.

| CSV | One row represents |
| --- | --- |
| `nodes.csv` | A canonical graph node, including paper, Wiki, entity, and Hub nodes |
| `edges.csv` | A directed predicate edge between two canonical nodes |
| `aliases.csv` | An alias-to-canonical-node mapping |
| `edge_origins.csv` | One provenance origin accumulated by an edge |

The source SQLite database is intentionally omitted. JSONL preserves the
complete portable snapshot; CSV files make the same four tables convenient for
analysis.

## Core trajectory

`metrics/trajectory.csv` contains one row for each document step `D001` through
`D056`. Important fields are:

- `reuse`, `create`, `abstain`: local identity decisions for eligible semantic
  units at that step.
- `reuse_fraction_R`: per-step reuse fraction `R(t)`.
- `multi_source_consolidation_M`: fraction of eligible canonical nodes supported
  by at least two works after that step.
- `old_node_membership_churn_C`: fraction of pre-existing eligible nodes whose
  active Hub membership changed at that step.
- `hub_births`, `hub_splits`, `hub_merges`, `hub_retirements`: committed Hub
  lifecycle events.

`metrics/final-summary.json` gives aggregate measurements and validation
counts. `metrics/density-outlier-sensitivity.*` gives the deterministic direct-
contribution analysis for D047 under fixed downstream outcomes.

## Hubs and portrait

`metrics/hub_summary.csv` contains one row per final Hub:

- `birth_step`, `birth_year`: first committed birth event.
- `parent_hub_id`: optional lineage parent; multiple parents use semicolons.
- `supporting_papers_at_birth/final`: distinct works supporting at least one
  active member node at the specified graph state.
- `member_count_at_birth/final`: distinct active nodes connected to the Hub by
  the formal membership predicate.
- `macro_domain`: a post-hoc display label used for portrait grouping and
  coloring, not a frozen compiler output.

`metrics/hub_membership_over_time.csv` contains one row for each Hub from its
birth through `G056`. It reports member and supporting-paper counts using the
same definitions as `hub_summary.csv`.

`metrics/paper_to_hub.csv` maps each paper to a final primary and optional
secondary Hub. The deterministic mapping maximizes overlap between the paper's
supported canonical nodes and final Hub members. Ties prefer the more specific
Hub, then the later-born Hub, then title order. A secondary assignment excludes
the primary Hub's ancestors and descendants. This is a navigation mapping, not
a manually labeled scientific ground truth.

`metrics/hub-lineage.json` and `metrics/hub-membership-trajectory.csv` are the
lower-level frozen lineage exports. `metrics/period_summary.csv` contains the
three periods reported in the manuscript.

## Configuration and integrity

`config/run-metadata.json` records public model identifiers, thresholds,
source-run hashes, code hashes, and isolation properties without endpoints,
credentials, caches, or local paths. `validation/` records the frozen audit
summaries. `CHECKSUMS.sha256` covers every file in this directory except the
checksum file itself.
