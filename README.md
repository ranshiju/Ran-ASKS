# Ran-ASKS
> Current release: v0.2.0

**ASKS (Agent-Driven Scientific Knowledge System)** is a persistent,
source-traceable knowledge system for sustained scientific work. Ran-ASKS is
its source-available engineering implementation.

**LLMs interpret. Embeddings organize. Graphs emerge.** Each scientific source
remains preserved while ASKS compiles it into readable knowledge and an
evolving graph that researchers and agents can inspect, navigate, revise, and
inherit across later work. The navigation map may be approximate; the evidence
path remains traceable.

![Scientific knowledge compilation in ASKS](docs/assets/scientific-knowledge-compilation.png)

*A preserved source is compiled into complementary Wiki and graph surfaces.
These surfaces guide later work while factual use returns to source-addressed
evidence.*

## Why knowledge compilation?

Retrieval determines what an agent can read for the present task. Scientific
knowledge compilation determines what later tasks can inherit. ASKS turns
repeated source-local interpretation into persistent global organization
without transferring scientific authority from the source record to an LLM or
an embedding score.

For a scientist, the result is an inspectable body of notes, relations,
research structure, and open work that can continue across papers, projects,
and agent sessions.

## What ASKS builds

| Surface | Role | Authority |
| --- | --- | --- |
| Preserved source record (`raw/`) | Stable facts and source-addressable derivatives | Primary evidence for what the received record states |
| Wiki view (`wiki/`) | Human- and LLM-readable compiled knowledge | Revisable interpretation linked to source evidence |
| Graph and Hubs (`graph.db`, `cross-domain/`) | Relations, aliases, research regions, and navigation | Approximate knowledge structure for deciding where to look |
| Research and Frontier state (`projects/`, `frontier/`) | Open questions, trajectories, and provisional work | Working state that remains separate from established evidence |

Wiki and graph are sibling compiled surfaces. The Wiki bridges source material
and structured navigation; graph edges and Hubs express the evolving knowledge
structure and make it easier to move through that structure. Factual answers
resolve back to the preserved source record.

## Paper and code versions

Development continues on `main`, so the repository may advance more quickly
than the paper. Every paper-associated implementation will be preserved as an
immutable Git tag and GitHub Release.

| Manuscript | Ran-ASKS version | Status |
| --- | --- | --- |
| Initial arXiv submission | `v0.2.0` | The arXiv identifier and immutable release link will be added after posting |

The manuscript formulates *scientific knowledge compilation* and presents a
worked chronological demonstration on 56 formally published papers from one
research program. The compiled graph yields a source-traceable author research
portrait organized around a persistent tensor-network methodological trunk.
The personal paper corpus and private compiled knowledge base are outside this
engineering template.

## How it works

1. Preserve each received source and its stable addressing metadata.
2. Use an LLM to produce a readable Wiki view and machine-facing semantic slots.
3. Validate a document-local `GraphDelta` before persistent graph writes.
4. Use embedding geometry together with explicit identity, routing, membership,
   and lifecycle rules to integrate the delta into accumulated graph state.
5. Let researchers and agents navigate the compiled structure, then return to
   source-addressed evidence for factual use.

In compact form:

```text
source record -> local interpretation -> validated GraphDelta
              -> persistent Wiki + evolving graph -> source-traceable use
```

Ingestion is resumable and graph fusion is transactional. Construction
decisions, validation results, and checkpoints make the state transition
inspectable and recoverable.

## Selected capabilities

- Resumable paper and document ingestion with provenance-preserving graph fusion.
- Graph-first navigation that returns to Raw evidence for factual answers.
- Persistent Hubs for research structure, lineage, and cross-source navigation.
- Project-scoped research memory and a Frontier overlay for open questions and
  evolving trajectories.
- On-demand academic writing capability that combines shared writing conventions
  with project and disciplinary context at the moment of composition.
- Read-only visual QA for images, PDF pages, and static PPT/PPTX pages.
- Image/PDF-to-editable-PPT reconstruction that favors native PowerPoint objects
  and records any raster fallback.
- An optional DSH agent cockpit with guarded tools and in-memory session state.

Visual QA is opt-in. It runs when the user requests it or when a layout-dependent
edit requires visible page context; ordinary text editing and compilation do
not trigger it automatically. See `operations/VISUAL_QA.md` and
`operations/VISUAL_TO_EDITABLE_PPT.md`.

## Quick start

```bash
git clone https://github.com/ranshiju/Ran-ASKS.git
cd Ran-ASKS
cp .env.example .env
python3 .scripts/engineering_graph.py validate
```

Configure model backends in `.env` only for workflows that need them. Then read
`AGENTS.md`: it is the operating contract that classifies a request, protects
the Raw layer, and dispatches the relevant workflow. For example, the query
dispatcher can be inspected with:

```bash
python3 .scripts/route.py --task query --query-stage start
```

Place only material you are authorized to process in `inbox/` and let the
registered ingestion workflow create or update domain content. Do not edit an
ingested Raw record in place. The main task specifications live under
`operations/`.

## Repository guide

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Project constitution, task routing, and non-negotiable boundaries |
| `operations/` | Ingestion, query, research, writing, synchronization, and engineering contracts |
| `.scripts/` | Validated command-line tools and regression checks |
| `dsh/` | Optional guarded agent loop and tool registry |
| `academic/`, `admin/`, `teaching/`, `business/` | Independent domain templates |
| `cross-domain/` | Cross-domain graph, Hubs, and navigation surfaces |
| `inbox/` | Local intake boundary for authorized source material |
| `slide-library/` | Reusable slide reconstruction and composition workspace |

ASKS is the complete scientific knowledge system described in the paper.
`WikiGraph` remains an engineering name in some internal paths and documents,
reflecting the central role of the scientific knowledge graph within that
system.

## Validation

Run focused checks after a change:

```bash
python3 .scripts/test_prompt_audit.py
python3 .scripts/engineering_graph.py validate
```

For release construction and privacy audit, see
`operations/engineering/open-source-release.md`.

## Privacy and publication

This repository is an engineering template, not a published personal knowledge
base. Its data directories contain placeholders only. The included `.gitignore`
excludes knowledge content, graph databases, runtime caches, inboxes, local
memory, outputs, and `.env` files by default. Review staged changes before every
commit, especially when a file under `raw/` or `wiki/` has been force-added.

See [DATA_POLICY.md](DATA_POLICY.md) for the publication boundary.

## Citation

The paper citation and BibTeX entry will be added when the arXiv record is
available. Cite the immutable Ran-ASKS release associated with the paper version
rather than the moving `main` branch.

## License

Ran-ASKS is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, including
use by educational institutions and public research organizations, is
permitted under its terms. Commercial use is not granted by that license and
requires a separate written commercial license from Shi-Ju Ran. Commercial
licensing inquiries should be sent through the repository owner's GitHub
contact channel.

PolyForm Noncommercial is not an OSI-approved open-source license because it
restricts commercial use. The term "public release" in this repository refers
to source visibility, not OSI open-source status.
