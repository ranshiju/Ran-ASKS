# Code Provenance for Paper Artifact 1.0.0

This frozen data artifact is bound to Ran-ASKS `v0.2.0` and source run
`bd7dc4b6e166`. The run recorded SHA-256 hashes for 16 implementation and
configuration files. The release candidate exactly matches 15 of those files.

The remaining difference is disclosed below. It arose after the frozen run; the released
data, metrics, graph, and Wiki pages were not regenerated with the later file. The exact
frozen-run file content is not distributed, so the release supports inspection and partial
re-execution rather than a claim of byte-identical end-to-end regeneration.

| File | Frozen-run SHA-256 | Release SHA-256 |
| --- | --- | --- |
| `.scripts/graph_ingest.py` | `658fce0d6f7bceef6b583dea38f9f797ebd16e21d85d17bee1e74efb927350d7` | `ea5d930ef39b109056108bdb3d52f34781a20ef41cd80a9a585360132f5989e6` |

The complete per-file comparison is available in
`config/code-compatibility.json`; the original frozen hashes remain in
`config/run-metadata.json`.
