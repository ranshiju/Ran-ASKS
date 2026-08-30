# Paper Artifact 1.0.0 for Ran-ASKS v0.2.0

This directory contains the sanitized data artifact associated with the initial
arXiv submission of:

> *LLMs Interpret, Embeddings Organize, Graphs Emerge: Agent-Driven Compilation
> of Scientific Knowledge*

The worked demonstration chronologically compiles 56 independently and
formally published papers from one research program. This artifact exposes the
resulting Wiki and Graph navigation surfaces, the measurements used in the
paper, the frozen publication manifest, and the configuration needed to
interpret those measurements.

## What is included

- `corpus/`: 65 reviewed candidates, including the 56-work frozen trajectory,
  bibliographic identifiers, selection decisions, and canonical PDF hashes.
- `wiki/`: 56 compiled paper pages and 18 final Hub pages from the physically
  isolated run.
- `graph/`: the sanitized final `G056` graph as JSONL and as separate CSV tables
  for nodes, edges, aliases, and edge origins.
- `metrics/`: trajectory, Hub lineage and membership, author-portrait mapping,
  period summary, and the D047 density-outlier sensitivity data.
- `config/`: model identifiers, thresholds, code hashes, source-run hashes, and
  the isolation boundary. It also compares the frozen-run code hashes with the
  code prepared for Ran-ASKS `v0.2.0`.
- `validation/`: frozen mechanical and semantic validation summaries.
- `metadata.json` and `CHECKSUMS.sha256`: machine-readable scope and integrity
  records.

The three compact portrait tables requested for downstream plotting are
`metrics/hub_summary.csv`, `metrics/hub_membership_over_time.csv`, and
`metrics/paper_to_hub.csv`.

## Raw boundary

No source PDF, parsed paper text, embedding database, SQLite graph database,
API request or response, credential, personal knowledge-base page, or
production graph is distributed here. Raw graph node IDs are retained as
structural identifiers so that the released graph remains faithful to the
frozen topology. Wiki locators and graph provenance values that originally
pointed to local source bundles are rendered as `raw-not-distributed/...`.

The released Wiki and graph are revisable navigation structures. They help a
reader inspect how local interpretations accumulated into global organization;
they do not replace the formally published papers as scientific evidence.

## Verify

From the repository root:

```bash
python3 .scripts/paper_artifact.py verify paper-artifacts/v0.2.0
```

The artifact has its own frozen version, `1.0.0`, and is version-bound to
Ran-ASKS `v0.2.0`. The repository `main` branch remains a rolling development
branch; cite the immutable tag and GitHub Release associated with the paper
once they are published.

The source run recorded 16 code/configuration hashes. Fifteen match the
`v0.2.0` release candidate exactly; `.scripts/graph_ingest.py` changed after the
run. The data were not regenerated with that later file. See
[`CODE_PROVENANCE.md`](CODE_PROVENANCE.md) and
`config/code-compatibility.json` for the precise boundary.

After the first public release, this directory is immutable. A correction that
changes data or interpretation receives a new artifact version and directory;
the released `1.0.0` checksums remain available. Any arXiv identifier known
before that release may be finalized here before the freeze. Identifiers that
become available later belong in repository-level citation metadata, without
changing any file or checksum in this directory.

Field definitions and interpretation boundaries are in
[`DATA_DICTIONARY.md`](DATA_DICTIONARY.md). Data-specific licensing is stated
in [`LICENSE-DATA.md`](LICENSE-DATA.md).
