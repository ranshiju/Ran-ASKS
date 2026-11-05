#!/usr/bin/env python3
"""Bounded local RIS/BibTeX data reader. Never executes TeX or follows record URLs."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath

PROTOCOL = "local-bibliography-v1"
SUFFIXES = {".ris", ".bib"}
MAX_FILES = 16
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 5000
MAX_DEPTH = 32


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover(pdf: Path, *, same_name_only: bool = False) -> list[Path]:
    """One directory only. Files named for a different PDF are not shared catalogs."""
    if not pdf.parent.is_dir():
        return []
    entries = list(pdf.parent.iterdir())
    pdf_stems = {p.stem.casefold() for p in entries if p.suffix.lower() == ".pdf"}
    candidates = [p for p in entries if p.suffix.lower() in SUFFIXES and not p.name.startswith(".")
                  and (p.stem.casefold() == pdf.stem.casefold()
                       or (not same_name_only and p.stem.casefold() not in pdf_stems))]
    return sorted(candidates, key=lambda p: (p.stem.casefold() != pdf.stem.casefold(), p.name.casefold()))


def associated_paths(directory: Path) -> set[Path]:
    """Metadata stays out of an inbox's independent content queue."""
    return {sidecar for pdf in directory.glob("*") if pdf.suffix.lower() == ".pdf" and pdf.is_file()
            for sidecar in discover(pdf)} if directory.is_dir() else set()


def parse_ris(text: str) -> list[dict]:
    records, fields, last = [], None, ""
    for line in text.lstrip("\ufeff").splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([A-Z0-9]{2})  -\s?(.*)$", line)
        if match:
            tag, value = match.groups()
            if tag == "TY":
                if fields is not None:
                    raise ValueError("RIS record missing ER")
                fields = {"TY": [value.strip()]}
            elif tag == "ER":
                if fields is None:
                    raise ValueError("RIS ER without TY")
                records.append({"entry_type": fields["TY"][0].upper(), "fields": fields,
                                "entry_id": str(len(records) + 1)})
                fields = None
                if len(records) > MAX_RECORDS:
                    raise ValueError("too many bibliography records")
            elif fields is None:
                raise ValueError("RIS field outside record")
            else:
                fields.setdefault(tag, []).append(value.strip())
            last = tag
        elif fields is not None and last in fields and line[:1].isspace():
            fields[last][-1] += " " + line.strip()
        else:
            raise ValueError("unsupported RIS line")
    if fields is not None:
        raise ValueError("RIS record missing ER")
    return records


class _BibReader:
    def __init__(self, text):
        self.text, self.pos = text.lstrip("\ufeff"), 0
        self.expanded_chars = 0
        self.macros = {m: m for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")}

    def skip(self):
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text[self.pos] == "%":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            else:
                break

    def char(self, expected):
        self.skip()
        if self.pos >= len(self.text) or self.text[self.pos] != expected:
            raise ValueError("invalid BibTeX delimiter")
        self.pos += 1

    def word(self):
        self.skip()
        match = re.match(r"[A-Za-z0-9_:./+\-]+", self.text[self.pos:])
        if not match:
            raise ValueError("invalid BibTeX identifier")
        self.pos += len(match.group())
        return match.group()

    def group(self, opener):
        self.char(opener)
        closer = "}" if opener == "{" else ")" if opener == "(" else '"'
        start, depth = self.pos, 0
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if c == "\\":
                self.pos += 2
                continue
            if c == closer and depth == 0:
                value = self.text[start:self.pos]
                self.pos += 1
                return value
            if c == "{":
                depth += 1
                if depth > MAX_DEPTH:
                    raise ValueError("BibTeX nesting exceeds limit")
            elif c == "}":
                depth -= 1
                if depth < 0:
                    raise ValueError("unbalanced BibTeX value")
            self.pos += 1
        raise ValueError("unterminated BibTeX value")

    def value(self):
        parts = []
        size = 0
        while True:
            self.skip()
            if self.pos >= len(self.text):
                raise ValueError("missing BibTeX value")
            if self.text[self.pos] in '{"':
                parts.append(self.group(self.text[self.pos]))
            else:
                name = self.word()
                if name.isdigit():
                    parts.append(name)
                elif name.casefold() in self.macros:
                    parts.append(self.macros[name.casefold()])
                else:
                    raise ValueError("undefined BibTeX string macro")
            size += len(parts[-1])
            if size > MAX_FILE_BYTES:
                raise ValueError("expanded BibTeX value exceeds limit")
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == "#":
                self.pos += 1
            else:
                self.expanded_chars += size
                if self.expanded_chars > MAX_TOTAL_BYTES:
                    raise ValueError("total BibTeX expansion exceeds limit")
                return "".join(parts)

    def parse(self):
        records = []
        while True:
            self.skip()
            if self.pos >= len(self.text):
                return records
            self.char("@")
            kind = self.word().casefold()
            self.skip()
            opener = self.text[self.pos:self.pos + 1]
            if opener not in ("{", "("):
                raise ValueError("invalid BibTeX entry")
            if kind in {"comment", "preamble"}:
                self.group(opener)  # preserved data, never evaluated
                continue
            self.char(opener)
            closer = "}" if opener == "{" else ")"
            if kind == "string":
                name = self.word().casefold()
                self.char("=")
                value = self.value()
                if name in self.macros or len(self.macros) >= 128:
                    raise ValueError("duplicate or excessive BibTeX macros")
                self.macros[name] = value
                self.skip()
                if self.text[self.pos:self.pos + 1] == ",":
                    self.pos += 1
                self.char(closer)
                continue
            key = self.word()
            self.char(",")
            fields = {}
            while True:
                self.skip()
                if self.text[self.pos:self.pos + 1] == closer:
                    self.pos += 1
                    break
                name = self.word().casefold()
                if name in fields:
                    raise ValueError("duplicate BibTeX field")
                self.char("=")
                fields[name] = self.value()
                self.skip()
                if self.text[self.pos:self.pos + 1] == ",":
                    self.pos += 1
                elif self.text[self.pos:self.pos + 1] != closer:
                    raise ValueError("invalid BibTeX field separator")
            records.append({"entry_type": kind, "entry_id": key, "fields": fields})
            if len(records) > MAX_RECORDS:
                raise ValueError("too many bibliography records")


def parse_bib(text: str) -> list[dict]:
    records = _BibReader(text).parse()
    by_key = {record["entry_id"]: record for record in records}
    if len(by_key) != len(records):
        raise ValueError("duplicate BibTeX entry key")

    def inherit(record, seen=()):
        if record["entry_id"] in seen or len(seen) >= 8:
            raise ValueError("cyclic or deep BibTeX crossref")
        fields = dict(record["fields"])
        parent_key = fields.get("crossref")
        if parent_key:
            if parent_key not in by_key:
                raise ValueError("BibTeX crossref must exist in same file")
            parent = inherit(by_key[parent_key], (*seen, record["entry_id"]))
            for key in ("year", "date", "booktitle", "journal", "journaltitle", "publisher", "volume", "number"):
                if parent.get(key) and not fields.get(key):
                    fields[key] = parent[key]
            if not fields.get("booktitle") and by_key[parent_key]["entry_type"] == "proceedings":
                fields["booktitle"] = parent.get("title", "")
        return fields

    return [{**record, "fields": inherit(record)} for record in records]


def latex_text(value: str) -> str:
    accents = {"'": "\u0301", '"': "\u0308", "`": "\u0300", "^": "\u0302", "~": "\u0303",
               "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b",
               "c": "\u0327", "k": "\u0328", "r": "\u030a", "b": "\u0331", "d": "\u0323"}
    value = re.sub(r"\\(['\"`^~=.]|[uvHckrbd](?=[\s{]))\s*(?:\{([A-Za-z])\}|([A-Za-z]))",
                   lambda m: unicodedata.normalize("NFC", (m[2] or m[3]) + accents[m[1]]), value)
    letters = {"ss": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "o": "ø", "O": "Ø", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ"}
    value = re.sub(r"\\(ss|ae|AE|oe|OE|o|O|l|L|i|j)(?![A-Za-z])", lambda m: letters[m[1]], value)
    value = re.sub(r"\\(?:textit|textbf|emph|textrm|mathrm|textsc|texttt|protect|relax)(?![A-Za-z])", "", value)
    value = re.sub(r"\\([&%_#$ {}])", lambda m: m[1], value)
    value = value.replace("\\,", " ").replace("\\\\", " ")
    if "\\" in value:
        raise ValueError("unsupported LaTeX text command")
    return " ".join(value.replace("{", "").replace("}", "").split())


def _bib_authors(value: str) -> list[str]:
    parts, start, depth = [], 0, 0
    for match in re.finditer(r"[{}]|\s+and\s+", value, re.I):
        token = match.group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            parts.append(value[start:match.start()])
            start = match.end()
    parts.append(value[start:])
    return parts


def _person(value: str, bib: bool) -> tuple[str, str]:
    corporate = bib and value.strip().startswith("{") and value.strip().endswith("}")
    text = latex_text(value) if bib else " ".join(value.split())
    if corporate:
        return text, text
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 2:
        return " ".join([parts[1], parts[0]]), parts[0]
    if len(parts) == 3:
        return " ".join([parts[2], parts[0], parts[1]]), parts[0]
    if len(parts) != 1 or not text:
        raise ValueError("unsupported author name")
    return text, text.split()[-1]


def canonical(record: dict, fmt: str, *, normalized_front: str | None = None) -> dict | None:
    import external_bibliography as eb
    fields, kind = record["fields"], record["entry_type"]
    bib = fmt == "bib"
    if bib:
        if kind not in {"article", "inproceedings", "conference"}:
            return None
        get = lambda *keys: next((latex_text(fields[k]) for k in keys if fields.get(k)), "")
        title = get("title")
        if normalized_front is not None and " " + eb.normalized(title) + " " not in normalized_front:
            return None
        venue = get("journaltitle", "journal") if kind == "article" else get("booktitle")
        year_value, raw_doi = get("year", "date"), get("doi")
        raw_authors = _bib_authors(fields.get("author", ""))
        volume, issue, pages = get("volume"), get("number", "issue"), get("pages")
    else:
        if kind not in {"JOUR", "CONF", "CPAPER"}:
            return None
        def get(*keys):
            for key in keys:
                values = fields.get(key) or []
                if len(set(values)) > 1:
                    raise ValueError("conflicting repeated RIS scalar")
                if values and values[0]:
                    return values[0]
            return ""
        title = get("TI", "T1")
        if normalized_front is not None and " " + eb.normalized(title) + " " not in normalized_front:
            return None
        venue = get("JF", "JO", "T2", "JA")
        year_value, raw_doi = get("PY", "Y1", "DA"), get("DO")
        raw_authors = fields.get("AU") or fields.get("A1") or []
        volume, issue = get("VL"), get("IS")
        pages = "-".join(filter(None, (get("SP"), get("EP"))))
    doi = eb.doi(raw_doi)
    year_match = re.match(r"^((?:18|19|20)\d{2})(?:$|[-/\s])", year_value)
    if (not title or not venue or not year_match or not raw_authors
            or (raw_doi and not doi) or doi.startswith("10.48550/arxiv.")
            or re.search(r"arxiv|biorxiv|medrxiv|preprint|unpublished|submitted", venue, re.I)):
        return None
    people = [_person(value, bib) for value in raw_authors]
    if any(not name or name.casefold() in {"others", "et al", "et al."} for name, _ in people):
        return None
    return {"title": title, "authors": [name for name, _ in people], "_families": [family for _, family in people],
            "year": year_match[1], "venue": venue, "doi": doi,
            "publication_status": "published", "publication_status_basis": "sidecar_declared",
            "publication_type": "journal-article" if kind in {"article", "JOUR"} else "proceedings-article",
            "publication_dates": {"published": [int(year_match[1])]}, "year_basis": "sidecar_year",
            "volume": volume, "issue": issue, "pages": pages, "article_number": "",
            "source_content_version": "not_verified", "metadata_provider": "local_bibliography",
            "origin_verified": False}


def _optional_key(field: str, value: str) -> str:
    """Identifiers keep punctuation; page-range typography alone is interchangeable."""
    if field == "doi":
        import external_bibliography as eb
        return eb.doi(value)
    value = " ".join(value.split()).casefold()
    return re.sub(r"\s*[-–—]+\s*", "-", value) if field == "pages" else value


def evaluate(files: list[dict], md_text: str) -> tuple[dict | None, list, str]:
    import external_bibliography as eb
    matched, diagnostics = {}, []
    front = eb.frontmatter(md_text)
    normalized_front = " " + eb.normalized(front) + " "
    core_fields = ("title", "authors", "year", "venue", "publication_type")
    optional_fields = ("doi", "volume", "issue", "pages")
    conflicting_fields = set()
    source_dois = {eb.doi(m.group(1)) for m in re.finditer(
        r"(?im)^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi.org/)(10\.\d{4,9}/[^\s<>]+)", front)}
    source_dois = {value for value in source_dois if value and not value.startswith("10.48550/arxiv.")}
    doi_conflict = len(source_dois) > 1
    parse_failed = False
    for file in files:
        try:
            records = parse_bib(file["body"]) if file["format"] == "bib" else parse_ris(file["body"])
        except ValueError as exc:
            diagnostics.append({"path": file["path"], "issue": str(exc)})
            parse_failed = True
            continue
        for record in records:
            try:
                value = canonical(record, file["format"], normalized_front=normalized_front)
            except ValueError as exc:
                diagnostics.append({"path": file["path"], "entry_id": record["entry_id"], "issue": str(exc)})
                parse_failed = True
                continue
            if not value or not eb._identity_matches(value["title"], value.pop("_families"), front):
                continue
            if len(source_dois) == 1:
                source_doi = next(iter(source_dois))
                if value["doi"] and value["doi"] != source_doi:
                    doi_conflict = True
                elif not value["doi"]:
                    value["doi"] = source_doi
                    value["doi_source"] = "paper.md"
            signature = json.dumps({key: ([eb.normalized(x) for x in value[key]] if key == "authors"
                                        else eb.normalized(value[key])) for key in
                                    core_fields}, sort_keys=True)
            item = matched.setdefault(signature, {**value, "record_evidence": [], "field_record_evidence": {}})
            evidence = {"path": file["path"], "entry_id": record["entry_id"]}
            item["record_evidence"].append(evidence)
            for field in core_fields + optional_fields:
                if value.get(field):
                    # A DOI completed from the PDF is not attributed to an empty export field.
                    origin = {"path": "paper.md"} if field == "doi" and value.get("doi_source") else evidence
                    origins = item["field_record_evidence"].setdefault(field, [])
                    if origin not in origins:
                        origins.append(origin)
                if field not in optional_fields or not value.get(field):
                    continue
                if item.get(field) and _optional_key(field, item[field]) != _optional_key(field, value[field]):
                    conflicting_fields.add(field)
                elif not item.get(field):
                    item[field] = value[field]
    if doi_conflict or conflicting_fields or len(matched) > 1:
        diagnostics.append({"issue": "conflicting_formal_records",
                            "matching_records": sum(len(item["record_evidence"]) for item in matched.values()),
                            "source_doi_conflict": doi_conflict, "conflicting_fields": sorted(conflicting_fields)})
        return None, diagnostics, "conflict"
    if parse_failed:
        return None, diagnostics, "parse_error"
    if not matched:
        return None, diagnostics, "no_match"
    return next(iter(matched.values())), diagnostics, "verified"


def replay(receipt: dict, md_text: str, artifact_root: Path | None = None) -> dict | None:
    try:
        return _replay(receipt, md_text, artifact_root)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def _valid_receipt_files(receipt: dict, md_text: str, artifact_root: Path | None = None) -> bool:
    import external_bibliography as eb
    if (not isinstance(receipt, dict) or receipt.get("protocol_version") != PROTOCOL
            or receipt.get("source_sha256") != eb.digest(md_text)):
        return False
    if receipt.get("discovery_scope", "directory") not in {"directory", "same-name"}:
        return False
    files = receipt.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        return False
    total, seen = 0, set()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("body"), str):
            return False
        path = PurePosixPath(str(file.get("path") or ""))
        if (len(path.parts) != 2 or path.parts[0] != "bibliography" or "\\" in str(path)
                or path.suffix not in SUFFIXES or file.get("format") != path.suffix[1:] or str(path) in seen):
            return False
        seen.add(str(path))
        data = file["body"].encode("utf-8")
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or sha(data) != file.get("sha256"):
            return False
        if artifact_root is not None:
            source = artifact_root.joinpath(*path.parts)
            if (source.parent.is_symlink() or source.is_symlink() or not source.is_file()
                    or source.stat().st_size > MAX_FILE_BYTES or sha(source.read_bytes()) != file["sha256"]):
                return False
    return True


def _replay(receipt: dict, md_text: str, artifact_root: Path | None = None) -> dict | None:
    if not _valid_receipt_files(receipt, md_text, artifact_root):
        return None
    value, _, status = evaluate(receipt['files'], md_text)
    return _bind_receipt(value, receipt) if status == 'verified' and value else None


def _bind_receipt(value: dict, receipt: dict) -> dict:
    value["arxiv_id"] = ""  # Local exports never promote a cited/preprint identifier.
    value["external_receipt"] = receipt
    value["review"] = {"status": "clean", "doc_type": "paper", "locked": True,
                       "provider": PROTOCOL, "conflicts": [],
                       "review_notes": "Matched source-provided formal bibliography; publisher origin not independently verified"}
    value["evidence"] = {key: "source.yaml#bibliographic.external_receipt" for key in ("title", "authors", "year", "venue", "doi")}
    return value


def _resolve_candidates(candidates: list[Path], md_text: str, scope: str) -> dict:
    import external_bibliography as eb
    receipt = {"protocol_version": PROTOCOL, "source_sha256": eb.digest(md_text),
               "discovery_scope": scope, "files": [], "diagnostics": []}
    if len(candidates) > MAX_FILES:
        receipt.update(status="parse_error", diagnostics=[{"issue": "too_many_sidecar_files"}])
        return {"status": "parse_error", "receipt": receipt, "bibliographic": None}
    total = 0
    for index, path in enumerate(candidates, 1):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("sidecar is not a bounded regular file")
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("bibliography byte limit exceeded")
            body = data.decode("utf-8")
            fingerprint = sha(data)
            name = f"{index:02d}-{fingerprint[:16]}{path.suffix.lower()}"
            receipt["files"].append({"path": "bibliography/" + name, "original_name": path.name,
                                     "format": path.suffix.lower()[1:], "sha256": fingerprint, "body": body})
        except (ValueError, OSError) as exc:
            receipt["diagnostics"].append({"original_name": path.name, "issue": str(exc)})
    value = None
    if receipt["diagnostics"]:
        status = "parse_error"
    elif not candidates:
        status = "absent"
    else:
        value, diagnostics, status = evaluate(receipt["files"], md_text)
        receipt["diagnostics"] = diagnostics
    receipt["status"] = status
    return {"status": status, "receipt": receipt,
            "bibliographic": _bind_receipt(value, receipt) if status == "verified" and value else None}


def resolve_local(pdf: Path, md_text: str) -> dict:
    """Read and evaluate one selected scope; named success never opens shared files."""
    candidates = discover(pdf)
    named = [path for path in candidates if path.stem.casefold() == pdf.stem.casefold()]
    if named:
        local = _resolve_candidates(named, md_text, "same-name")
        if local['status'] != 'no_match' or len(named) == len(candidates):
            return local
    return _resolve_candidates(candidates, md_text, "directory")


def stage_resolved(local: dict, extract_dir: Path, md_text: str) -> dict:
    """Stage freshly resolved bytes; later replay remains independent and semantic."""
    receipt = local['receipt']
    if not receipt['files']:
        return local
    if not _valid_receipt_files(receipt, md_text):
        raise ValueError('resolved bibliography snapshot failed integrity check')
    root = extract_dir / "bibliography"
    if root.is_symlink():
        raise ValueError("bibliography staging directory must not be a symlink")
    root.mkdir(exist_ok=True)
    for file in receipt['files']:
        target = extract_dir / file['path']
        data = file['body'].encode('utf-8')
        if target.is_symlink() or (target.exists() and target.read_bytes() != data):
            raise ValueError("staged bibliography snapshot changed")
        target.write_bytes(data)
    if not _valid_receipt_files(receipt, md_text, extract_dir):
        raise ValueError('staged bibliography snapshot failed integrity check')
    return local


def stage_and_resolve(pdf: Path, extract_dir: Path, md_text: str) -> dict:
    return stage_resolved(resolve_local(pdf, md_text), extract_dir, md_text)


def discovery_scope(pdf: Path, receipt: dict) -> str:
    """Legacy receipts cover the directory; named receipts can follow a renamed PDF."""
    if receipt.get('discovery_scope') == 'same-name' and discover(pdf, same_name_only=True):
        return 'same-name'
    return 'directory'


def copy_archived(receipt: dict, source_root: Path, target_root: Path, md_text: str) -> None:
    """Reuse only hash-verified archived files; no discovery or network during re-ingest."""
    if receipt.get("protocol_version") != PROTOCOL:
        return
    if not replay(receipt, md_text, source_root):
        raise ValueError("archived bibliography failed verification")
    folder = target_root / "bibliography"
    if folder.is_symlink():
        raise ValueError("bibliography staging directory must not be a symlink")
    folder.mkdir(exist_ok=True)
    for file in receipt["files"]:
        target = target_root / file["path"]
        data = file["body"].encode("utf-8")
        if target.is_symlink() or (target.exists() and target.read_bytes() != data):
            raise ValueError("staged bibliography snapshot changed")
        target.write_bytes(data)
    if not replay(receipt, md_text, target_root):
        raise ValueError("staged bibliography failed verification")


def duplicate_companion_status(pdf: Path, raw_pdf: Path) -> dict:
    """Read-only cleanup guard: PDF equality alone does not cover new bibliography."""
    import yaml
    if pdf.suffix.lower() != ".pdf":
        return {"status": "absent", "files": []}
    candidates = discover(pdf)
    if not candidates:
        return {"status": "absent", "files": []}
    result = {"status": "unarchived", "files": [], "reason": "bibliography_not_verified_in_archive"}
    try:
        metadata = raw_pdf.with_name("source.yaml")
        markdown = raw_pdf.with_name("paper.md")
        # Fixed managed neighbors only; never follow a path supplied by metadata.
        if (metadata.is_symlink() or markdown.is_symlink() or not metadata.is_file()
                or not markdown.is_file() or metadata.stat().st_size > 4 * MAX_TOTAL_BYTES):
            return result
        source = yaml.safe_load(metadata.read_text(encoding="utf-8"))
        receipt = source.get("bibliographic", {}).get("external_receipt", {})
        scope = discovery_scope(pdf, receipt)
        candidates = discover(pdf, same_name_only=scope == 'same-name')
        result['discovery_scope'] = scope
        if len(candidates) > MAX_FILES:
            return {**result, "reason": "too_many_sidecar_files"}
        total = 0
        for path in candidates:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("sidecar is not a bounded regular file")
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("bibliography byte limit exceeded")
            result["files"].append({"name": path.name, "sha256": sha(data)})
        archived = {file["sha256"] for file in receipt.get("files", [])}
        if not all(file["sha256"] in archived for file in result["files"]):
            return result  # New input does not need the old record to authorize an update.
        if not replay(receipt, markdown.read_text(encoding="utf-8"), raw_pdf.parent):
            return result
        return {**result, "status": "archived", "reason": "all_sidecar_bytes_verified_in_archive"}
    except (OSError, ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError) as exc:
        result["reason"] = str(exc)
    return result


def import_companions(source: Path, target: Path) -> list[dict]:
    """Copy same-name PDF companions only; preflight all destinations and roll back our copies."""
    if source.suffix.lower() != ".pdf":
        return []
    candidates = discover(source, same_name_only=True)
    if candidates and target.suffix.lower() != ".pdf":
        raise ValueError("PDF with bibliography companions must retain a PDF extension")
    if len(candidates) > MAX_FILES:
        raise ValueError("too many bibliography companions")
    planned, destinations, total = [], set(), 0
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("bibliography companion must be a bounded regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("bibliography byte limit exceeded")
        destination = target.with_suffix(path.suffix.lower())
        if destination in destinations or destination.is_symlink():
            raise ValueError("bibliography companion destination conflict")
        destinations.add(destination)
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != data):
            raise ValueError("bibliography companion destination conflict")
        planned.append((path, destination, data))
    created, receipts = [], []
    try:
        for path, destination, data in planned:
            reused = destination.exists()
            if not reused:
                with destination.open("xb") as handle:
                    created.append(destination)
                    handle.write(data)
            receipts.append({"source_path": str(path.absolute()), "staged_path": str(destination.absolute()),
                             "sha256": sha(data), "action": "reused_exact" if reused else "copied"})
    except (OSError, ValueError):
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return receipts
