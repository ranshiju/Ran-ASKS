# Contributing

Ran-ASKS accepts focused proposals for its domain-independent public baseline.
Domain agents, ontologies, connectors, evaluations, and support normally belong
in an independently maintained Community Downstream described by
`ECOSYSTEM.md`.

1. Read `AGENTS.md`, `GOVERNANCE.md`, and the applicable specification in
   `operations/`.
2. For engineering changes, run
   `.scripts/engineering_graph.py impact <node> --verify` before editing and
   inspect the reported contract.
3. Keep Raw knowledge, credentials, caches, generated databases, private
   projects, and restricted evaluation data out of commits.
4. Add or update focused regression tests for behavior changes.
5. Run the relevant validation commands and include their results in the pull
   request.

## Generated Public Tree

The GitHub repository is currently generated from a separately governed source
repository. A pull request is a reviewable contribution proposal, not an
independent source-of-truth change. Do not resolve divergence by merging or
editing the generated public tree directly.

When a proposal is accepted, the maintainer imports it into the governed source
repository, preserves contributor authorship, runs the full publication checks,
regenerates the public tree, and closes the proposal with the resulting public
commit. See `GOVERNANCE.md` for the complete round trip.

## Core Contribution Scope

Suitable upstream changes include Core defects, security and provenance fixes,
domain-independent mechanisms, and generalized behavior with evidence from
more than one use case. A proposal must separate its shared mechanism from any
domain ontology, prompt, dataset, or policy.

Creating a fork or downstream does not require Core approval. Do not describe a
Community Downstream as official, ASKS-Compatible, or a Reference Distribution
unless that status has been explicitly granted under `ECOSYSTEM.md`.

## Contributor licensing

WikiGraph uses a noncommercial public license while reserving separate
commercial licensing rights. An external contribution can be merged only
after its contributor completes the contributor license agreement provided by
the maintainer. That agreement must permit the project owner to use,
distribute, sublicense, and relicense the contribution under both
noncommercial and commercial terms. Opening a pull request does not by itself
mean that the contribution has been accepted or licensed for inclusion.
