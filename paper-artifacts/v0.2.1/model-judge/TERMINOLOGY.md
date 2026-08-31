# Terminology and Frozen Machine Identifiers

The evaluation uses fixed language models as blinded judges. They receive no
tools, search, or action loop. The manuscript and release documentation
therefore call it a **blinded cross-model navigation audit** or **model-judge
audit**.

The experiment was frozen before this terminology refinement. To preserve
checksum-level provenance, the following machine identifiers remain unchanged
inside the frozen configuration, result schemas, and code:

| Frozen identifier | Manuscript and release term |
| --- | --- |
| `agent audit` | model-judge navigation audit |
| `Q_agent_membership` / `Q_agent` | `Q_nav` |
| `preregistered-before-judge-calls` | specified and frozen before evaluation |

The mapping changes no trial, score, model output, statistical test, or success
gate. `Preregistered` is avoided in human-facing claims because the pre-run
freeze was internal rather than deposited in an external preregistration
service.
