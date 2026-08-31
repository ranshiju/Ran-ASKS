# Release-Safe Judge Prompt Template

The complete prompts used during evaluation contain source abstracts and
remain outside the public artifact. This file preserves the exact instruction
structure without source text.

## System instruction

```text
You are an independent evaluator of scientific knowledge navigation.
Use only the supplied paper titles and abstracts. Do not use external knowledge or search.
A useful Hub may be interdisciplinary or emerging. Do not reward identical taxonomy alone.
Recognize scientifically interpretable method-to-problem, system-to-technique,
theory-to-application, property, and cross-field relationships. The A/B order is random.
Do not guess which option came from ASKS. Return only the requested JSON and never quote
source sentences. Keep each reason under 60 words.
```

## User payload structure

```json
{
  "task": {
    "hub_id": "<frozen Hub identifier>",
    "hub_label": "<frozen Hub label>",
    "membership_trials": [
      {
        "trial_id": "M-Hxx-yy",
        "A": {"title": "<title>", "abstract": "<private at evaluation>"},
        "B": {"title": "<title>", "abstract": "<private at evaluation>"}
      }
    ],
    "set_coherence_trial": "<optional three-paper A/B sets>"
  },
  "instructions": {
    "membership": {
      "question": "Which paper better supports this Hub as a useful scientific navigation entry?",
      "choice": "A, B, or tie",
      "fit_scores": "integer 1..5 for A and B",
      "confidence": "integer 1..5"
    },
    "set_coherence": {
      "question": "Which three-paper set better supports a coherent and useful entry under this Hub label?",
      "choice": "A, B, or tie"
    }
  }
}
```

The complete output schema and allowed relation bases are preserved in
`frozen-source/run.py`. `inputs/blinded-trials.json` records the released option IDs and
`outputs/trial-key.csv` records the answer/control identities.
