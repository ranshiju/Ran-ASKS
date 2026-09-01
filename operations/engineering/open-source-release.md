# Open-source Release Policy

> The generated repository is source-available under PolyForm Noncommercial
> 1.0.0. In this document, "public release" describes source visibility; it
> does not claim OSI open-source status.

## Boundary

The public release is an engineering template, never a copy of the personal knowledge base. The authoritative allowlist is `open-source-manifest.yaml`; a file not named there is private by default.

- **Public**: generic specifications, scripts, tests, schemas, agents, DSH loops and guards, templates, architecture documentation, and explicitly approved frozen paper artifacts.
- **Template only**: content directories represented by `.gitkeep` files.
- **Private**: all raw sources, personal or production compiled Wiki pages, graph databases, caches, outputs, inbox items, local memory, active project materials, and personal state. A sanitized paper-specific Wiki/Graph export is public only when it is versioned under `paper-artifacts/`, explicitly allowlisted, independently licensed, and verified.
- **Review required**: a document that mixes public engineering rules with personal data must be split before publication. Put stable rules in public engineering documents and private context in `*.private.md` or a private project/status file.

## Release version

- The canonical public release version is the root `VERSION` file, formatted as `MAJOR.MINOR.PATCH`; documentation must not duplicate a current-version constant.
- `open_source_release.py build` reads `VERSION`, writes it to the destination tree, and stamps `> Current release: v<version>` into the public `README.md`.
- `open_source_release.py verify` rejects a version mismatch between source `VERSION`, destination `VERSION`, and the README release badge.
- The root `CHANGELOG.md` records public project updates; its newest release heading must match `VERSION` before publication.
- Keep the changelog human-readable: prioritize important user-facing capabilities, behavior or contract changes, compatibility, quality, and security; summarize minor fixes and internal adjustments instead of listing them individually.
- This number is separate from pipeline versioning: `CURRENT_PIPELINE_VERSION` in `.scripts/graph_lib.py` tracks content-pipeline upgrades and is never the public release number.
- Increment PATCH for compatible fixes or documentation-only releases, MINOR for backward-compatible public capabilities, and MAJOR for breaking public contracts.

## Release workflow

1. Update the manifest when a public engineering file is added or moved; update `VERSION` and the newest `CHANGELOG.md` entry when the public release changes.
2. Build a clean tree; the command only copies manifest-approved files.
3. Verify the tree before staging or publishing.
4. Verify every included paper artifact with its declared verifier. The base
   `v0.2.0` artifact uses `.scripts/paper_artifact.py verify`; additive audit
   extensions such as `v0.2.1` use their versioned `verify.py`.
5. In a Git worktree, verify that no manifest-approved release file is excluded
   by the destination `.gitignore`.
6. Review the staged diff and run the normal engineering regressions.

```bash
python3 .scripts/open_source_release.py build /path/to/WikiGraph_clean --clean --force
python3 .scripts/open_source_release.py verify /path/to/WikiGraph_clean
```

`--clean` deletes only the existing release worktree contents (never the source repository) and requires `--force` when the destination is non-empty. The build writes `.wikigraph-public-release` as a destination marker.

The generated `operations/engineering/graph.yaml` is a deterministic public projection. Nodes whose concrete paths are absent from the allowlisted release, including active `projects/` and `.project/` material, are removed together with dependent edges, verification entries, and script contracts. Placeholder paths remain available for reusable templates. A public capability may not lose a required node: the build fails instead of publishing an incomplete capability. Run `engineering_graph.py validate` inside the generated tree as part of release review.

Project-specific experiment drivers that import active `projects/` paths are
excluded together with those projects. Reusable or frozen experiment code must
be self-contained under a public script or versioned `paper-artifacts/` path.

## Maintenance rule

Do not hand-edit the generated public worktree except for Git metadata. Make engineering changes in the source repository, then rebuild the release. If a private detail appears in a public document, split it at the source and add a regression assertion when the pattern is mechanically detectable.

The generic Raw/Wiki ignore rules must not hide a manifest-approved frozen
artifact. `paper-artifacts/**` is an explicit tracking exception, and release
verification rejects ignored files whenever the destination is a Git worktree.

A paper artifact becomes immutable when its first public Git tag and Release
are created. Later ASKS development continues on `main`; corrections to the
paper data use a new artifact version and preserve the earlier checksums.
When release code differs from hashes recorded by the frozen run, publish the
per-file comparison and state the reproducibility boundary explicitly.
Citation identifiers learned after the freeze belong in repository-level
metadata, not in the immutable artifact directory.
