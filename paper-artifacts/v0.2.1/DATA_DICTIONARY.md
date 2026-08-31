# Data Dictionary

This is paper audit artifact `1.1.0`, bound to Ran-ASKS `v0.2.1`. It extends,
without modifying, the chronological E1 artifact `1.0.0` under `../v0.2.0/`.

## Interpretation boundary

Both audits evaluate the frozen `G056` Hub organization after construction.
PhySH provides a stable but coverage-limited external taxonomy reference. The
model judges provide an evidence-bounded test of semantic and navigation
recognizability. Neither reference is a factual authority for the compiled
graph. Factual use returns to the formally published papers identified by the
base artifact manifest.

## PhySH files

- `physh/inputs/physh_gold_matched.jsonl`: the 26 matched APS-assigned label
  records, including paper ID, DOI, title, concepts, and disciplines.
- `physh/inputs/physh_coverage.csv`: released-label coverage for the 56-paper
  manifest.
- `physh/inputs/hub_paper_incidence.csv`: the frozen Hub-paper incidence used by
  the audit.
- `physh/inputs/hub_gold_coverage.csv`: eligible labeled-paper counts per Hub.
- `physh/metrics/permutation_values.csv.gz`: 10,000 time-stratified permutation
  values for the frozen endpoints.
- `physh/metrics/primary.json`: the exact concept-set primary endpoint.
- `physh/metrics/secondary.csv`: discipline, facet, and sensitivity endpoints.
- `physh/metrics/hub_coherence.csv`: Hub-level observed alignment values.

The primary score averages exact-set F1 over labeled paper pairs within each
eligible Hub and then weights the 11 eligible Hubs equally. The null permutes
complete label profiles within the frozen publication periods while retaining
the Hub-paper incidence matrix.

## Model-judge files

- `model-judge/inputs/blinded-trials.json`: option identities and fixed Hub
  order without abstracts or answer keys.
- `model-judge/outputs/trial-key.csv`: true and control paper IDs for each
  released trial.
- `model-judge/outputs/judge-outputs.jsonl`: normalized judge outputs without
  source quotations or complete abstracts.
- `model-judge/metrics/hub_scores.csv`: judge-specific and consensus membership
  scores for all 18 Hubs.
- `model-judge/metrics/judge_summary.csv`: model-level primary and secondary
  scores, position-choice counts, and position-bias checks.
- `model-judge/metrics/primary.json`: the frozen `Q_nav` result, exact sign-flip
  test, agreement measures, and gate checks.
- `model-judge/metrics/disagreements.csv`: trial-level judge disagreements.
- `model-judge/metrics/relation_bases.csv`: normalized scientific-relation bases
  selected by the judges.

A correct membership choice scores 1, a tie scores 0.5, and a control choice
scores 0. Scores are averaged within Hub, across the 18 Hubs, and across the two
model families. The historical frozen field `Q_agent_membership` is displayed
as `Q_nav` in the manuscript. The data and statistic are unchanged.

## Figure files

`figures/figure5-data.csv` contains one PhySH summary row and 18 Hub-level
model-judge rows. `figures/build_figure5.py` reads the complete frozen metrics,
asserts 10,000 permutation values and 18 Hub rows, and generates the PDF and
PNG outputs.

## Integrity

`CHECKSUMS.sha256` covers every distributed file except itself. The
`SOURCE_CHECKSUMS.sha256` files preserve the content digests recorded by the
two private frozen experiment directories. Only their release-safe subsets are
distributed here. `reanalyze.py` recomputes the released primary endpoints and
agreement statistics without private source text or model calls. The exact
original runners are retained under `physh/frozen-source/` and
`model-judge/frozen-source/` for provenance.
