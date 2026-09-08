# Open-source Release Policy

> The generated repository is source-available under PolyForm Noncommercial
> 1.0.0. In this document, "public release" describes source visibility; it
> does not claim OSI open-source status.

## Boundary

The public release is an engineering template, never a copy of the personal knowledge base. The authoritative allowlist is `open-source-manifest.yaml`; a file not named there is private by default.

- **Public**: generic specifications, scripts, tests, schemas, agents, DSH loops and guards, templates, architecture documentation, and explicitly approved frozen paper artifacts.
- **Template only**: content directories represented by `.gitkeep` files.
- **Private**: all raw sources, personal or production compiled Wiki pages, graph databases, caches, outputs, inbox items, local memory, active project materials, personal state, and personal Codex skills declared private by the manifest. A sanitized paper-specific Wiki/Graph export is public only when it is versioned under `paper-artifacts/`, explicitly allowlisted, independently licensed, and verified.
- **Review required**: a document that mixes public engineering rules with personal data must be split before publication. Put stable rules in public engineering documents and private context in `*.private.md` or a private project/status file.

## Release version

- The canonical public release version is the root `VERSION` file, formatted as `MAJOR.MINOR.PATCH`; documentation must not duplicate a current-version constant.
- `open_source_release.py build` reads `VERSION`, writes it to the destination tree, and stamps `> Current release: v<version>` into the public `README.md`.
- `open_source_release.py verify` rejects a version mismatch between source `VERSION`, destination `VERSION`, and the README release badge.
- Edit release notes only in `operations/engineering/open-source-assets/CHANGELOG.md`. `prepare-version --apply` synchronizes this asset and the source root `CHANGELOG.md` mirror; `build` copies the asset to the public root. The newest release heading must match `VERSION` before publication.
- Keep the changelog human-readable: prioritize important user-facing capabilities, behavior or contract changes, compatibility, quality, and security; summarize minor fixes and internal adjustments instead of listing them individually.
- Every GitHub update requires a content-level documentation review. Synchronize the English README, Chinese README, dated Chinese introduction Markdown, and its version-scope note with any affected capabilities, configuration, workflow, or user guidance. The release verifier rejects a pending or latest committed public diff that does not include all four reader-facing documents.
- The Markdown introduction is the default GitHub reading view. Retain the dated PDF for download and printing, and update it whenever the introduction's reader-facing content changes. Normalize the PDF after Word export for portable rendering; the release asset must contain a Ghostscript producer marker, and `verify` rejects the original Word/Quartz export even when local PDF tools accept it.
- Before every actual public update, the host Agent automatically assesses the highest semantic change level and records the capability/compatibility rationale. Do not infer it from file counts, line counts, or the last Git tag: compare with the published tree and its `VERSION`. The deterministic `prepare-version` command calculates the next number; it does not call an LLM or classify changes itself.
- Every public update advances `VERSION`, including documentation-only updates. `verify` rejects unchanged or decreasing versions against `HEAD` for pending changes, or `HEAD^` for the latest committed change when clean. Initial trees without a commit baseline are exempt. Rebuilding or retrying the same prepared batch does not allocate another version; `--from-version` rejects accidental repeated preparation. With no actual change to publish, do not prepare another version or publication commit.
- Number allocation does not authorize a Git tag or GitHub Release. Create those only when separately requested; frozen paper artifacts keep their own immutable boundaries.
- This number is separate from pipeline versioning: `CURRENT_PIPELINE_VERSION` in `.scripts/graph_lib.py` tracks content-pipeline upgrades and is never the public release number.
- Increment PATCH for compatible fixes or documentation-only releases, MINOR for backward-compatible public capabilities, and MAJOR for breaking public contracts.
- Apply this decision policy explicitly during `0.x` development too: record compatibility changes rather than silently treating all pre-1.0 updates as interchangeable. A large internal refactor can remain PATCH; a small new public capability can warrant MINOR.

## Release workflow

1. Review the diff against the current GitHub state and its `VERSION`, classify PATCH/MINOR/MAJOR, and add reviewed notes under the public asset's `Unreleased` heading. Preview `prepare-version --level <level> --reason '<semantic rationale>' --from-version <current>`; then run the same command with `--apply` once. It updates `VERSION`, archives the notes with the date and decision rationale, and synchronizes the source root changelog. It refuses empty notes, stale expected versions, and duplicate release headings.
2. Update the manifest when a public engineering file is added or moved. Synchronize both READMEs and the Chinese introduction Markdown plus its scope note; if reader-facing introduction content changed, update its PDF edition from the same reviewed source. Build a clean tree; the command only copies manifest-approved files and never allocates another version.
3. Verify the tree before staging or publishing.
4. Verify every included paper artifact with its declared verifier. The base
   `v0.2.0` artifact uses `.scripts/paper_artifact.py verify`; additive audit
   extensions such as `v0.2.1` use their versioned `verify.py`.
5. In a Git worktree, verify that no manifest-approved release file is excluded
   by the destination `.gitignore`.
6. Review the staged diff and confirm that documentation describes the affected behavior rather than merely changing dates or version badges; then run the normal engineering regressions.
7. Commit the reviewed source changes locally and the generated public changes in the public repository. Verify the committed public tree again, then push only the public repository and confirm its remote commit. Never push the personal source repository as a substitute for the allowlisted public tree.

Normalize the Word-exported PDF before placing it in the public-assets directory:

```bash
gs -q -dSAFER -dNOPAUSE -dBATCH -sDEVICE=pdfwrite \
  -dCompatibilityLevel=1.4 -dPDFSETTINGS=/prepress -dAutoRotatePages=/None \
  -sOutputFile=/tmp/ASKS-Chinese-Introduction-normalized.pdf \
  /path/to/ASKS-Chinese-Introduction-word-export.pdf
```

Compare the normalized rendering with the Word export, then move the normalized
file to the dated path declared by `open-source-manifest.yaml`.

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
