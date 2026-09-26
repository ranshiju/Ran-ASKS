# ASKS Ecosystem

ASKS supports independently maintained systems built for different tasks,
agents, and domains. Ran-ASKS maintains the general-purpose public baseline; it
does not currently designate or maintain an official domain distribution.

The following layers are conceptual boundaries, not a promise that separate
packages or plugin APIs already exist:

- **Shared contracts** describe validated task handoffs, semantic intermediate
  representations, graph plans, provenance, and transactional boundaries.
- **Core mechanisms** implement domain-independent parsing, validation,
  transactions, graph writes, and publication safeguards.
- **Domain capabilities** contain domain agents, prompts, ontologies,
  connectors, validators, and quality evaluations.
- **Distributions** combine the mechanisms and selected domain capabilities
  into systems maintained for end users.

Core mechanisms may provide domain-independent defaults and reference profiles.
Knowledge or policy from one domain must not become mandatory for every
distribution.

## Ecosystem Status

Status is explicit and applies to a named downstream version against a named
Ran-ASKS version and commit.

### Community Downstream

A Community Downstream is independently maintained and may state that it is
based on Ran-ASKS. This status is not certification, endorsement, compatibility
assurance, or a support commitment from the Ran-ASKS maintainer. Community
projects own their domain behavior, data, evaluations, releases, upgrades, and
support.

Projects should use a distinct name such as `ASKS-<Domain>` and display this
notice prominently:

> Community project based on Ran-ASKS. Independently maintained and not an
> official Ran-ASKS distribution.

Names such as `Ran-ASKS-<Domain>`, or descriptions containing "official" or
"reference", require prior maintainer approval because they imply governance
and support relationships that a fork alone does not create.

### ASKS-Compatible

`ASKS-Compatible` is an evaluated status, not a self-declared synonym for
"fork". A claim must identify its exact Ran-ASKS baseline, supported public
protocol versions, conformance commands and results, known deviations, and the
scope and date of evaluation. The Ran-ASKS maintainer must explicitly accept
the claim before it is listed by this repository.

Compatibility does not establish equivalence of Agent quality. Deterministic
contract checks and domain quality evaluations are separate evidence.

### Reference Distribution

A Reference Distribution is an explicit later designation. It requires an
independent maintainer, sustained real use, documented upgrades across Ran-ASKS
releases, reproducible conformance results, domain evaluations, clear data and
license boundaries, and a demonstrated maintenance commitment. No community
project is a Reference Distribution by default, and Ran-ASKS currently lists
none.

## Starting A Community Downstream

1. Create a repository owned by the downstream maintainers and select an exact
   Ran-ASKS release commit as its baseline.
2. Keep Ran-ASKS as an `upstream` remote. Use short-lived branches and reviewed
   pull requests for downstream development and upgrades.
3. Copy `templates/downstream/DOWNSTREAM.md` and record maintainers, baseline,
   Core files changed, tests, domain data boundaries, and upstream candidates.
4. Keep domain code and support in the downstream. Submit only generalized
   mechanisms or Core fixes upstream.
5. Treat public pull requests to Ran-ASKS according to the source-authority
   round trip in `GOVERNANCE.md` and the licensing rules in `CONTRIBUTING.md`.

The downstream template is a collaboration record, not an extension manifest
or compatibility certificate. A machine-readable extension schema should be
standardized only after multiple independent downstreams reveal recurring,
tested integration points.

## Current Listings

Ran-ASKS currently designates no official domain distribution, no
ASKS-Compatible downstream, and no Reference Distribution. Independent teams
may develop Community Downstreams without transferring maintenance obligations
to this project.
