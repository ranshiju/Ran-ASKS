# Provider profiles

Ran-ASKS uses OpenAI-compatible endpoint fields while keeping model selection
explicit. Copy the relevant example values into the workspace `.env`; do not
commit `.env` or credentials. A provider profile is a tested configuration
example, not part of the knowledge or provenance model.

- `openai-compatible.env.example` is a neutral starting point and leaves model
  names blank because compatible endpoints expose different catalogs.
- `glm.env.example` records an optional provider-specific reference profile.
  It does not require users to choose GLM models.

For API text ingestion, choose two roles: the ingestion LLM (`LLM_MODEL`, with
optional `INGEST_GENERATION_*` overrides) and the recovery subagent LLM
(`SEMANTIC_RECOVERY_*`). Each needs a model, endpoint and API key; the two roles
may use different providers. The neutral example makes proposition extraction
inherit the ingestion model. A separate proposition model or citation-only Wiki
repair profile is an optional advanced setting. An unconfigured recovery profile
retains existing model inheritance; configured recovery runs only for eligible
failures, within existing budgets and validators.

FlashX and EV describe the maintainer's personal configuration, not system
requirements. The root `.env.example` leaves the two text-model selections blank;
its named models for other workflows are reference examples. Embedding, visual
processing and parsing dependencies are configured separately when needed.

Configuring an endpoint never grants permission to upload a source. Workflows
that cross the remote boundary retain their explicit consent checks.
