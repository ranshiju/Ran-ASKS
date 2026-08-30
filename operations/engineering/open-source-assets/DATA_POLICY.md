# Data and Privacy Policy

This repository is an engineering template with explicitly approved, frozen
paper artifacts. It is not a published personal knowledge base.

- Do not commit source documents, personal notes, meeting records, research files, credentials, or generated graph databases.
- Keep `raw/` immutable after ingestion; retain provenance links from `wiki/` to local raw sources.
- The repository ignores content directories and local artifacts by default. Treat force-added files as a release-review exception.
- Remove document metadata and review every staged file before publishing.
- Use only material you are authorized to store, process, and distribute.

## Frozen paper-artifact exception

`paper-artifacts/` may contain a versioned, manifest-approved artifact prepared
to support a specific paper. Such an artifact must:

- exclude source PDFs, parsed source text, embeddings, databases, API traces,
  credentials, personal knowledge-base content, and production state;
- sanitize local paths while preserving stable source identifiers and hashes;
- include a machine-readable scope record, checksums, field definitions,
  version binding, frozen-run/release-code compatibility record, and a
  data-specific license;
- pass `.scripts/paper_artifact.py verify` and the public-release privacy audit;
- become immutable at its first public release. Corrections receive a new
  artifact version instead of silently replacing released data.

Citation identifiers known after the freeze are recorded outside the artifact
directory so its original files and checksums remain unchanged.

The current approved exception is paper artifact `1.0.0` under
`paper-artifacts/v0.2.0/`, bound to Ran-ASKS `v0.2.0`. Its Wiki and Graph are
released as navigation structures; formally published papers remain the source
of scientific evidence.
