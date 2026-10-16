# Ubuntu host setup

This guide covers generic local host adapters. The Python package and its
capability extras follow the upstream `pyproject.toml` and committed `uv.lock`.
Agent/API control, ingestion transactions, source provenance, and upload
authorization retain their existing contracts.

## Python environment

Use Python 3.12 or later and `uv`. Ubuntu 22.04 remains usable when a separate
supported Python 3.12 interpreter is provided; keep the operating system's Python
intact. From the repository root, create the frozen core environment and run the
offline first-success checks:

```bash
uv sync --frozen
uv run ran-asks doctor
uv run ran-asks demo
```

Install the extras used by the selected workflow. For local Office handling,
visual tools, and the release regression profile:

```bash
uv sync --frozen --extra office --extra visual --extra release
uv run ran-asks test release
```

The release profile includes the synthetic platform regression. These tests
exercise command selection and failure paths; an available command alone does
not prove that a real document rendered successfully.

## System tools

Document conversion and static slide rendering need LibreOffice. Fonts and
fontconfig provide the local font environment; desktop opening and recoverable
trash use standard desktop utilities when available:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends \
  libreoffice fonts-noto-cjk fontconfig xdg-utils libglib2.0-bin
```

DOCX text extraction reads OOXML directly. Markup-compatibility
`AlternateContent` uses only an explicit `Fallback`; unsupported packages can
recover through the existing `textutil` or controlled LibreOffice path.
LibreOffice converts to a temporary DOCX that the native reader reads once,
retaining supported referenced headers and footers. Legacy DOC uses available
converters; `antiword` or `catdoc` can also be installed when those fallbacks are
desired. Other capabilities keep their existing
optional system requirements, such as Tesseract for raster-to-editable-slide
OCR. Missing tools are reported by the selected workflow.

## Platform behavior

- LibreOffice is discovered through explicit `SOFFICE_BIN`, the command search
  path, and supported platform installation locations. Static slide rendering
  uses headless LibreOffice; absent or failed conversion remains a failure.
- Caller-provided `FONTCONFIG_FILE` and `FONTCONFIG_PATH` take precedence. The
  macOS bundled-font configuration applies only to the corresponding app bundle;
  Ubuntu uses its own font environment. The adapter does not install fonts or
  change global configuration.
- DOCX extraction preserves document order, tables, and supported referenced
  auxiliary text. PPTX ingestion keeps its native slide-order/structure parser;
  local rendering supplies static pages without replacing provenance checks or
  remote-consent boundaries.
- Explicit desktop opening uses the host opener. On a headless server, a file
  can still be generated; failed opening reports its status and leaves the file
  available for manual access.
- Cleanup uses a supported system trash command. When trash is unavailable, a
  unique local recovery directory retains the original name and symbolic-link
  identity. A failed cleanup does not permanently delete the source or overwrite
  an earlier recovery item.

For actual host acceptance, render a disposable document with the workflow in
use and inspect its resulting pages. Font availability, native extraction,
static rendering, and optional desktop opening are separate checks.
