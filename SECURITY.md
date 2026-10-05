# Security Policy

## Supported versions

Security fixes target the current `main` public-tree version. Immutable paper
tags remain reproducibility records and may not receive backports. Downstreams
must identify the exact Ran-ASKS version and commit they use.

## Reporting a vulnerability

Do not include credentials, personal data, source documents, or other sensitive
material in a public issue. Use
[GitHub Private Vulnerability Reporting](https://github.com/ranshiju/Ran-ASKS/security/advisories/new)
or email [sjran@cnu.edu.cn](mailto:sjran@cnu.edu.cn) with a minimal reproduction,
affected version, and impact description. Encrypt or redact sensitive evidence
before sending it. The maintainer targets acknowledgement within seven days and
an initial assessment within fourteen days; complex reports may require longer.

## Security model

The [threat model](docs/security/threat-model.md) documents trust boundaries for
source documents, parsers, agents, remote services, filesystem writes, graph
mutation, and public/private separation. Source text is untrusted data: commands
or permission requests embedded in a document never become control instructions.

## Publication checklist

Before opening a pull request or release, review staged files for:

- API keys, tokens, and local `.env` files;
- private source materials and generated wiki pages;
- graph databases, caches, logs, and document metadata;
- personal identifiers, local paths, and unauthorized third-party content.
