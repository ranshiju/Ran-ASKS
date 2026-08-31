# Paper Audit Artifact 1.1.0 for Ran-ASKS v0.2.1

This directory is the frozen, release-safe audit extension associated with the
post-arXiv submission manuscript of:

> *LLMs Interpret, Embeddings Organize, Graphs Emerge: Agent-Driven Compilation
> of Scientific Knowledge*

The initial arXiv submission remains bound to Ran-ASKS `v0.2.0` and paper
artifact `1.0.0` in `../v0.2.0/`. This additive artifact does not alter that
record. It publishes the later external semantic and navigation audits used by
the submission manuscript.

## Included audits

### PhySH semantic alignment

The first audit compares the frozen ASKS Hub-paper incidence with official
APS-assigned Physics Subject Headings (PhySH). It covers 26 papers with released
labels and 11 Hubs with at least two labeled supporting papers. The primary
exact concept-set Hub macro-average is `0.18701`, compared with a time-stratified
permutation-null mean of `0.14728`. The one-sided permutation value is
`p=0.14759` over 10,000 permutations. This is a directional, not confirmatory,
concept-level result.

### Blinded cross-model navigation audit

The second audit asks two frozen model families to distinguish a Hub-supporting
paper from a matched control using only a Hub label and source title-abstract
evidence. The judges do not construct or tune the Hubs and use no search or
tools. Across 53 membership trials per judge and all 18 Hubs, the two-judge
Hub macro-average is `Q_nav=0.77431` against the `0.5` chance reference, with
exact one-sided Hub-level sign-flip `p=0.0006485`.

The primary gate was specified and frozen before judge calls. The local frozen
configuration used the historical machine identifiers `agent audit` and
`Q_agent_membership`. These identifiers are preserved for checksum-level
provenance and map to `model-judge audit` and `Q_nav` in the manuscript and
human-facing release documentation. See `model-judge/TERMINOLOGY.md`.

## Contents

- `physh/`: frozen configuration, released label matches, Hub incidence,
  permutation values, metrics, validation records, frozen source code, and
  upstream notices.
- `model-judge/`: frozen configuration, blinded trial identities, control key,
  normalized judge outputs without source quotations, metrics, validation
  records, frozen source code, and a release-safe prompt template.
- `figures/`: the combined manuscript Figure 5, its plotting script, and the
  compact data table used by the plot.
- `metadata.json`, `DATA_DICTIONARY.md`, and `CHECKSUMS.sha256`: scope, field
  interpretation, and integrity records.

## Publication boundary

This artifact excludes source PDFs, parsed Raw text, complete abstracts,
private evidence packets, API requests containing abstracts, credentials,
runtime checkpoints, and production knowledge-base state. Paper IDs and DOI
records provide traceable identifiers without redistributing source text.
Model explanations are normalized, capped summaries and contain no source
quotations.

## Verify

From this directory, run:

```bash
python3 verify.py
python3 reanalyze.py
```

The verifier checks every recorded SHA-256 digest, required files, metadata,
the release boundary, and the absence of abstract fields in released model
outputs. The reanalysis script independently recomputes the reported primary
endpoints and agreement statistics from the release-safe inputs and outputs.
The exact frozen runners under each `frozen-source/` directory retain their
original repository bindings. A full experimental replay additionally requires
the private source boundary and configured model services.

## Licenses

ASKS-owned audit data, model-judge outputs, and derived figures use CC BY-NC
4.0 as stated in `LICENSE-DATA.md`. APS-assigned PhySH labels and their direct
derivatives retain CC BY 4.0, while the taxonomy resources retain their
upstream MIT terms. See `LICENSE-PHYSH.md`, `THIRD_PARTY_NOTICES.md`, and the
files under `physh/third_party/physh/`.
