---
name: manuscript-diagnosis
description: Diagnose confidential manuscripts stored under a WikiGraph projects/稿件诊断 subfolder. Use when the user requests 稿件诊断, manuscript assessment, identification of manuscript problems, or evidence-backed improvement recommendations for a manuscript whose target folder names the journal. Discover prior report samples, verify indispensable core literature in WikiGraph, pause for ingestion when required evidence is missing, and draft a diagnosis in the user's established style.
---

# 稿件诊断

Use “稿件诊断” as the feature name in menus, task descriptions, and reports.
Retain evidence checks, confidentiality, journal-specific assessment, and the
user-confirmed writing style; the name does not weaken the diagnosis criteria.

Keep manuscript content and diagnosis artifacts local. Do not upload an unpublished
manuscript, extracted text, diagnosis notes, or historical reports to external
services. Public journal policies may be consulted without transmitting manuscript
content. Never write diagnosis material to `raw/`, `wiki/`, or any graph database.

## Start

1. Follow the repository playbook and task router before reading project content.
2. Resolve the requested folder under `projects/稿件诊断/` and run:

   ```bash
   python3 .codex/skills/manuscript-diagnosis/scripts/diagnosis_preflight.py "<target-folder>"
   ```

3. Treat `journal_hint` from the target folder name as the journal information.
   Ask only if it is absent or genuinely ambiguous; do not infer a different
   journal from manuscript branding.
4. If the project uses research memory, recall the target project before diagnosis.
5. Fail closed when the target is outside `projects/稿件诊断/`, no manuscript is found,
   or multiple candidate manuscripts cannot be distinguished safely.

## Prepare Evidence

- Read the manuscript locally and completely once before detailed analysis. Pass
  the full PDF path to the local reader; do not form conclusions from truncated
  extraction. Revisit exact pages only for focused checks.
- Extract the problem, central claims, assumptions, methods, and the figure or
  derivation supporting each claim. Build a page-addressed claim-evidence matrix.
- Evaluate scientific correctness separately from the journal threshold. Apply
  the journal named in the folder: originality, significance, audience, format,
  and any journal-specific editorial criteria.
- Test decisive equations, limits, dimensions, numerical convergence, uncertainty,
  figure consistency, and plausible alternative explanations in proportion to
  their effect on the verdict.

## Core Literature Gate

Check only papers that are indispensable to the diagnosis. A paper is indispensable
when at least one of these is true:

- a central correctness claim depends on its theorem, data, or method;
- the novelty verdict depends on comparison with the nearest prior result;
- a disputed interpretation cannot be judged without its primary evidence.

Do not gate on general background, broad context, or merely useful reading.

For each indispensable paper, query WikiGraph first with an exact title or a
distinctive title/author fragment:

```bash
python3 .scripts/wg.py lookup "<title-or-distinctive-fragment>"
```

Use `neighbors` or `relations` only to locate evidence. Verify factual conclusions
through `read-raw` using a Raw locator. Do not treat graph edges, Wiki prose, or a
search hit as the final evidence.

If any indispensable paper is not present, stop before the final verdict and
diagnosis report. Return a compact `待摄入核心论文` list containing the citation or
best available identifier, why it is mandatory, and which diagnostic judgment it
blocks. Ask the user to ingest those papers. Do not broaden the list and do not
silently replace ingestion with an external download.

## Learn The User's Style

Use `base_style_guide` and `style_profile` returned by preflight to locate optional
local style files under `projects/稿件诊断/`. Read existing files only when drafting
the final report, applying the baseline guide first and the user-confirmed profile
second. These private files and historical samples are not bundled with the skill.
If either file is absent, continue without it; if no usable style evidence exists,
state that limitation and use a restrained professional diagnostic style. Do not
fetch personal style files externally or invent user preferences.
Keep sample reports in their original local folders so their provenance and version
history remain clear; do not copy them into the skill directory.

Use `style_samples` from preflight as evidence supporting or extending the
incremental profile. Prefer author-final markers such as `_ran`, `final`, or `最终`,
then revised versions, then older drafts. When several versions of one report
exist, compare them to learn the user's edits rather than averaging all versions.

Infer and preserve:

- opening-summary length and degree of first-person voice;
- directness, politeness, and strength of criticism;
- numbering and major/minor organization;
- typical explanation depth and recommendation phrasing.

Copy style, not manuscript-specific sentences. If no usable sample exists, say so
and use a restrained professional diagnostic style.

Update `projects/稿件诊断/STYLE.md` only after the user approves a final report or edits
a draft in a way that establishes a reusable preference. Record the supporting
sample path and keep tentative one-off choices out of the stable profile.

## Produce The Diagnosis

Work in four passes: orientation, claim-evidence verification, journal-threshold
assessment, and adversarial reread. Classify findings as decisive, major, or minor.
For every substantive comment state the location, the problem, why it matters, and
what response or evidence would resolve it.

Before drafting, ensure the core-literature gate is clear. Draft the author-facing
report separately from confidential comments to the editor. Default to a new
`Manuscript_Diagnosis_draft.md` in the target folder only when the user asked to write
the report; never overwrite an existing report. Keep internal WikiGraph paths and
private notes out of the author-facing text.

End with a recommendation supported by the preceding findings and state any
remaining uncertainty. Do not let the desired recommendation determine the issue
list retroactively.
