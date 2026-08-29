# WikiGraph

> A provenance-preserving, file-based knowledge graph system: `raw` facts → LLM-compiled `wiki` nodes → `graph.db` navigation edges.

## What is included

This public template contains the engineering system: task specifications, schemas, agents, scripts, tests, and engineering documentation. It deliberately contains **no personal knowledge-base content**, credentials, caches, database files, or Git history from the private working repository.

The data directories contain only `.gitkeep` placeholders. Add content locally, then follow `AGENTS.md` and the relevant workflow under `operations/`.

## Core model

- `raw/`: immutable source facts; answers must be traceable back here.
- `wiki/`: LLM-authored, human-readable nodes with source links.
- `graph.db`: navigation edges and aliases; derived indexes are not primary data.
- `academic/`, `admin/`, `teaching/`, `business/`: independent knowledge domains.
- `cross-domain/`: hubs and cross-domain navigation.

Read `AGENTS.md` before operating the system. For engineering changes, first use `.scripts/engineering_graph.py impact <node> --verify` and `.scripts/engineering_graph.py contract <node>`.

## Selected capabilities

- Resumable ingestion with source-addressed Wiki pages and transactional graph fusion.
- Graph-first navigation that returns to immutable Raw evidence for factual answers.
- Project-scoped research memory and a separate Frontier overlay for open questions and trajectories.
- Read-only visual QA for images, PDF pages, and static PPT/PPTX pages, with deterministic checks and optional vision-model review.
- Explicit image/PDF-to-editable-PPT reconstruction that favors native objects and records any raster fallback.

Visual tools are opt-in. Visual QA runs when the user requests it or when a layout-dependent edit requires visible page context; ordinary text edits and compilation do not trigger it automatically. See `operations/VISUAL_QA.md` and `operations/VISUAL_TO_EDITABLE_PPT.md`.

## Quick start

1. Copy `.env.example` to `.env` and configure an API backend only if required.
2. Optionally add local source materials under a domain's `raw/` directory; do not commit private materials.
3. Use the dispatcher to retrieve the relevant procedure:

   ```bash
   python3 .scripts/route.py --task query --query-stage start
   ```

4. Run a focused validation after changes:

   ```bash
   python3 .scripts/test_prompt_audit.py
   python3 .scripts/engineering_graph.py validate
   ```

## Privacy and publication

The included `.gitignore` excludes knowledge content, graph databases, runtime caches, inboxes, local memory, outputs, and `.env` files by default. Review staged changes before every commit, particularly if you intentionally force-add a file under `raw/` or `wiki/`.

For release construction and audit, see `operations/engineering/open-source-release.md`.

## License

WikiGraph is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, including
use by educational institutions and public research organizations, is
permitted under its terms. Commercial use is not granted by that license and
requires a separate written commercial license from Shi-Ju Ran. Commercial
licensing inquiries should be sent through the repository owner's GitHub
contact channel.

PolyForm Noncommercial is not an OSI-approved open-source license because it
restricts commercial use. The term "public release" in this repository refers
to source visibility, not OSI open-source status.
