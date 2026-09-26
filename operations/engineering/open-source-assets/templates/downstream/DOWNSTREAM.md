---
schema: asks-community-downstream-record-v1
name: "ASKS-Example"
status: community
upstream_repository: "https://github.com/ranshiju/Ran-ASKS"
upstream_version: "0.0.0"
upstream_commit: "0000000000000000000000000000000000000000"
maintainers:
  - name: "Downstream maintainer"
    contact: "maintainer@example.invalid"
---

# Community Downstream Record

> Community project based on Ran-ASKS. Independently maintained and not an
> official Ran-ASKS distribution.

This record documents a downstream's declared baseline and maintenance
boundary. It is not an extension manifest, compatibility certificate, or
endorsement by the Ran-ASKS maintainer.

## Purpose And Ownership

- **Domain or task:** Describe the downstream's scope.
- **Repository owner:** Name the individual or organization responsible.
- **Issue tracker:** Link to the downstream's own support channel.
- **Release policy:** Describe how downstream versions and upgrades are made.

## Upstream Baseline

Record the exact Ran-ASKS version and commit used to create or last upgrade the
downstream. Do not replace the commit with a moving branch name.

## Domain Capabilities

List domain agents, prompts, ontologies, connectors, validators, workflows, and
quality evaluations maintained only by this downstream.

## Core Files Modified

| Core path | Reason | Retain downstream or propose upstream | Migration note |
| --- | --- | --- | --- |
| `path/to/file` | Explain the required change | Retain | Explain upgrade impact |

Keep this table current. It is the primary input for later upgrade reviews and
for discovering repeated integration points across independent downstreams.

## Declared Protocols And Checks

List the existing public protocol identifiers used by the downstream and the
exact commands that exercise them. A declaration is not proof of compatibility;
attach reproducible results when requesting ASKS-Compatible status.

```text
agent task protocol: <identifier>
semantic IR protocol: <identifier>
graph plan protocol: <identifier>
conformance commands:
  - <command>
```

## Domain Evaluations

Describe versioned quality evaluations separately from deterministic contract
checks. Record dataset ownership, redistribution permission, metrics, accepted
thresholds, model/provider configuration, and known limitations. Do not publish
private or unlicensed evaluation data.

## Data, Privacy, Security, And License

Document domain data authority, private storage, remote-processing permissions,
credential handling, incident contact, the downstream license, and its
compatibility with the Ran-ASKS license. A downstream must not weaken the Raw,
provenance, graph-write, or publication boundaries inherited from Ran-ASKS
without documenting the deviation prominently.

## Upstream Candidates

Record Core defects and domain-independent mechanisms that may be proposed to
Ran-ASKS. Remove domain assumptions before proposing generalization, add focused
tests, and follow `CONTRIBUTING.md`. Domain-specific behavior remains here.
