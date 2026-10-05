# Provider profiles

Ran-ASKS uses OpenAI-compatible endpoint fields while keeping model selection
explicit. Copy the relevant example values into the workspace `.env`; do not
commit `.env` or credentials. A provider profile is a tested configuration
example, not part of the knowledge or provenance model.

- `openai-compatible.env.example` is a neutral starting point and leaves model
  names blank because compatible endpoints expose different catalogs.
- `glm.env.example` records the currently evaluated reference defaults used by
  the root `.env.example`.

Configuring an endpoint never grants permission to upload a source. Workflows
that cross the remote boundary retain their explicit consent checks.
