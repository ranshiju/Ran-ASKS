# Template-Driven CV Workspace

This directory contains an anonymized full academic CV template package:

- `academic-full.template.yaml` defines module order, bilingual labels,
  category mappings, required categories, and record sorting.
- `academic-full.docx` defines page setup, Word styles, placeholders, and
  ordered `cv_module_<id>` bookmarks. It contains no personal CV content.
- `cv-records.example.yaml` is a non-authoritative workspace scaffold. Replace
  every example value and Raw locator before selecting a record for rendering.

Create a local workspace without editing the public template in place:

```bash
mkdir -p projects/cv-demo/templates
cp templates/cv/academic-full.* projects/cv-demo/templates/
cp templates/cv/cv-records.example.yaml projects/cv-demo/cv-records.yaml
python3 .scripts/wg.py cv status --workspace cv-demo
```

Facts must first exist in a managed Raw source. The CV working record controls
wording and edition selection but is not factual authority. Formal rendering
accepts only verified records with usable Raw locators and never overwrites an
existing dated version. See `operations/CV.md` for the full contract.
