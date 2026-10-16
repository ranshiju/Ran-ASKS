#!/usr/bin/env python3
"""Platform-neutral text extraction for Word documents."""
from __future__ import annotations

import posixpath
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import platform_compat


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_WORD_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
STRICT_OFFICE_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
W = f"{{{WORD_NS}}}"
R = f"{{{OFFICE_REL_NS}}}"
PR = f"{{{PACKAGE_REL_NS}}}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


class UnsupportedMarkupError(ValueError):
    """The native reader cannot select a visible OOXML representation."""


def _visible_children(element: ElementTree.Element):
    """Use the compatibility fallback without guessing Choice feature support."""
    if element.tag == f"{MC}AlternateContent":
        fallback = element.find(f"{MC}Fallback")
        if fallback is None:
            raise UnsupportedMarkupError("AlternateContent has no supported fallback")
        yield from _visible_children(fallback)
        return
    for child in element:
        if child.tag == f"{MC}AlternateContent":
            yield from _visible_children(child)
        else:
            yield child


def _visible_elements(element: ElementTree.Element):
    yield element
    for child in _visible_children(element):
        yield from _visible_elements(child)


def _paragraph_text(paragraph: ElementTree.Element, w: str = W) -> str:
    pieces: list[str] = []
    for element in _visible_elements(paragraph):
        if element.tag == f"{w}t":
            pieces.append(element.text or "")
        elif element.tag == f"{w}tab":
            pieces.append("\t")
        elif element.tag in {f"{w}br", f"{w}cr"}:
            pieces.append("\n")
        elif element.tag in {f"{w}noBreakHyphen", f"{w}softHyphen"}:
            pieces.append("-")
    return "".join(pieces)


def _container_lines(container: ElementTree.Element, w: str = W) -> list[str]:
    lines: list[str] = []
    for child in _visible_children(container):
        if child.tag == f"{w}p":
            lines.append(_paragraph_text(child, w))
        elif child.tag == f"{w}tbl":
            for row in (item for item in _visible_children(child) if item.tag == f"{w}tr"):
                cells: list[str] = []
                for cell in (item for item in _visible_children(row) if item.tag == f"{w}tc"):
                    cell_lines = _container_lines(cell, w)
                    cells.append("\n".join(cell_lines).strip())
                lines.append("\t".join(cells))
        else:
            # Content controls and custom XML wrappers may contain ordinary
            # paragraphs or tables. Descend through the wrapper without
            # flattening table cells into unrelated body lines.
            lines.extend(_container_lines(child, w))
    return lines


def _document_relationships(
    archive: zipfile.ZipFile, names: set[str]
) -> dict[str, tuple[str, str]]:
    rels_path = "word/_rels/document.xml.rels"
    if rels_path not in names:
        return {}
    try:
        root = ElementTree.fromstring(archive.read(rels_path))
    except (KeyError, ElementTree.ParseError):
        return {}

    relationships: dict[str, tuple[str, str]] = {}
    for relationship in root.findall(f"{PR}Relationship"):
        if relationship.get("TargetMode", "").casefold() == "external":
            continue
        rel_id = relationship.get("Id", "")
        rel_type = relationship.get("Type", "").rsplit("/", 1)[-1]
        target = relationship.get("Target", "")
        if not rel_id or rel_type not in {"header", "footer", "footnotes", "endnotes"}:
            continue
        if target.startswith("/"):
            continue
        part = posixpath.normpath(posixpath.join("word", target))
        if part == "word" or not part.startswith("word/") or part not in names:
            continue
        relationships[rel_id] = (rel_type, part)
    return relationships


def _referenced_parts(
    body: ElementTree.Element,
    relationships: dict[str, tuple[str, str]],
    *,
    w: str = W,
    r: str = R,
) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for reference in _visible_elements(body):
        if reference.tag not in {f"{w}headerReference", f"{w}footerReference"}:
            continue
        rel_id = reference.get(f"{r}id", "")
        relationship = relationships.get(rel_id)
        if relationship is None or relationship[1] in seen:
            continue
        seen.add(relationship[1])
        parts.append(relationship)
    return parts


def _referenced_note_ids(
    body: ElementTree.Element, kind: str, *, w: str = W
) -> set[str]:
    return {
        reference.get(f"{w}id", "")
        for reference in _visible_elements(body)
        if reference.tag == f"{w}{kind}Reference" and reference.get(f"{w}id") is not None
    }


def extract_docx_text(path: str | Path) -> str:
    """Extract visible document text from OOXML without host tools."""
    source = Path(path)
    sections: list[str] = []
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                return ""
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            if document.tag.startswith("{"):
                word_namespace = document.tag[1:].split("}", 1)[0]
            else:
                word_namespace = ""
            if word_namespace not in {WORD_NS, STRICT_WORD_NS}:
                return ""
            office_rel_namespace = (
                STRICT_OFFICE_REL_NS
                if word_namespace == STRICT_WORD_NS else OFFICE_REL_NS
            )
            w = f"{{{word_namespace}}}"
            r = f"{{{office_rel_namespace}}}"
            body = document.find(f"{w}body")
            if body is None:
                return ""

            relationships = _document_relationships(archive, names)
            parts = [("document", "word/document.xml")]
            parts.extend(_referenced_parts(body, relationships, w=w, r=r))
            for kind in ("footnotes", "endnotes"):
                part = next((
                    target for rel_type, target in relationships.values()
                    if rel_type == kind
                ), None)
                if part:
                    parts.append((kind, part))

            note_ids = {
                "footnotes": _referenced_note_ids(body, "footnote", w=w),
                "endnotes": _referenced_note_ids(body, "endnote", w=w),
            }
            for kind, part in parts:
                root = document if kind == "document" else ElementTree.fromstring(
                    archive.read(part)
                )
                if kind == "document":
                    containers = [body]
                elif kind in note_ids:
                    ids = note_ids[kind]
                    containers = [
                        note for note in _visible_children(root)
                        if note.tag == f"{w}{kind[:-1]}"
                        and note.get(f"{w}id", "") in ids
                    ]
                else:
                    containers = [root]
                lines: list[str] = []
                for container in containers:
                    lines.extend(_container_lines(container, w))
                section = "\n".join(lines).strip()
                if section:
                    sections.append(section)
    except (OSError, KeyError, ElementTree.ParseError, zipfile.BadZipFile, RuntimeError, NotImplementedError, UnsupportedMarkupError):
        return ""
    text = "\n\n".join(sections)
    return f"{text}\n" if text else ""


def _run_text_command(command: list[str], timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 and result.stdout.strip() else ""


def _extract_doc_with_soffice(path: Path, soffice: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="asks-doc-text-") as temp_dir:
        workspace = Path(temp_dir)
        output_dir = workspace / "output"
        profile_dir = workspace / "profile"
        output_dir.mkdir()
        profile_dir.mkdir()
        command = [
            str(soffice),
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--headless",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(output_dir),
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
                env=platform_compat.soffice_environment(soffice),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        converted = output_dir / f"{path.stem}.docx"
        if result.returncode != 0 or not converted.is_file():
            return ""
        # Read the converted package once, without re-entering host fallback.
        # Plain TXT export omits visible headers and footers.
        return extract_docx_text(converted)


def extract_legacy_doc_text(path: str | Path) -> str:
    """Extract legacy DOC text through a recoverable converter chain."""
    source = Path(path).absolute()
    textutil = platform_compat.find_executable("textutil")
    if textutil:
        text = _run_text_command([
            str(textutil), "-convert", "txt", "-stdout", str(source),
        ])
        if text:
            return text

    soffice = platform_compat.find_soffice()
    if soffice:
        text = _extract_doc_with_soffice(source, soffice)
        if text:
            return text

    for name in ("antiword", "catdoc"):
        converter = platform_compat.find_executable(name)
        if converter:
            text = _run_text_command([str(converter), str(source)])
            if text:
                return text
    return ""


def extract_word_text(path: str | Path, max_chars: int | None = None) -> str:
    """Extract DOCX/DOC text and optionally return a bounded preview."""
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix == ".docx":
        text = extract_docx_text(source)
        if not text.strip():
            # Preserve the existing macOS converter when a package is unreadable
            # or uses an OOXML structure the native reader does not support.
            textutil = platform_compat.find_executable("textutil")
            if textutil:
                text = _run_text_command([
                    str(textutil), "-convert", "txt", "-stdout",
                    str(source.absolute()),
                ])
            if not text.strip():
                soffice = platform_compat.find_soffice()
                if soffice:
                    text = _extract_doc_with_soffice(source.absolute(), soffice)
    elif suffix == ".doc":
        text = extract_legacy_doc_text(source)
    else:
        return ""
    if max_chars is None:
        return text
    return text[:max(0, max_chars)]
