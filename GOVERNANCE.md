# Governance

Ran-ASKS is the maintainer-led general-purpose distribution and public
engineering baseline for ASKS. It is not the owner or default maintainer of
systems that other teams build for a particular scientific or operational
domain.

## Responsibilities

The Ran-ASKS maintainer is responsible for the domain-independent mechanisms
and public boundaries maintained in this repository: source traceability,
ingestion transactions, validation, deterministic graph writes, provenance,
release integrity, compatibility decisions, and review of changes proposed for
the shared baseline.

An independent downstream maintainer is responsible for that downstream's
domain agents, prompts, ontologies, connectors, data permissions, evaluations,
releases, upgrades, documentation, security response, and user support. A
downstream does not transfer those responsibilities to the Ran-ASKS maintainer
by using the ASKS name or by forking this repository.

Contributors are responsible for keeping private knowledge, credentials,
restricted data, generated databases, and unlicensed evaluation material out
of public proposals.

## Source Authority And Public Contributions

The GitHub repository is currently a generated public release tree. Approved
public files are selected from a separate governed source repository and
published through the manifest, validation, provenance, and transactional
release process. The generated tree is not edited or merged as an independent
source of truth.

An external GitHub pull request is therefore a contribution proposal:

1. A contributor opens a focused pull request against the public repository.
2. The maintainer reviews its scope, licensing, tests, and public boundaries.
3. An accepted patch is imported into the governed source repository with the
   contributor's authorship preserved in commit metadata or attribution.
4. The complete public release checks run against the source commit.
5. The public tree is regenerated and pushed by the managed publisher.
6. The proposal is closed with a link to the resulting public commit.

Directly merging a pull request into the generated tree would bypass the
authority and privacy boundary and could be overwritten by the next managed
publication. This round trip is transitional. If public code later becomes the
authoritative source, governance and publication rules must be revised together
before ordinary GitHub merges are enabled.

## Decisions

The Ran-ASKS maintainer has final responsibility for shared contracts, release
boundaries, compatibility classification, licensing acceptance, and changes
that affect source authority or protected invariants. A change to a public
contract or a breaking migration requires a written proposal that states the
problem, compatibility effect, alternatives, migration, and verification.

Independent downstream teams make their own domain decisions without Core
approval. They involve Ran-ASKS only when proposing a shared mechanism,
reporting a Core defect, or requesting an ecosystem status described in
`ECOSYSTEM.md`.

Domain-first experimentation is expected. Generalization belongs upstream only
after the domain-specific assumptions have been removed and the proposed Core
behavior has focused tests. A second independent use case is strong evidence
for generality, but Core correctness, security, privacy, provenance, and data
integrity fixes do not need to wait for another domain.

## Releases And Protection

Downstreams should pin an exact published version and commit, perform upgrades
through reviewable branches, and publish their own releases. Tracking the
moving Ran-ASKS `main` branch is suitable for testing, not a compatibility
claim.

The public release gate is required for Ran-ASKS publication. Repository rules
must continue to permit the managed publisher to push the verified generated
commit. A pull-request-only rule must not be enabled until a reviewed publisher
bypass or a new public-source authority model is in place.

## Licensing

Ran-ASKS is source-available under PolyForm Noncommercial 1.0.0 and reserves
separate commercial licensing rights. Downstream use and redistribution remain
subject to the applicable license. Contributing code upstream is a separate
act and requires the contributor licensing terms stated in `CONTRIBUTING.md`.
