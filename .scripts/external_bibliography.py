#!/usr/bin/env python3
"""Free, bounded bibliography lookup; no model calls or Raw writes.

Receipts are data, not instructions. Acceptance is reproducible offline from the
source text and archived HTTP response bodies, independently of model output.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import threading
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

PROTOCOL = "external-bibliography-v1"
MAX_REQUESTS = 4
MAX_BYTES = 512_000
TIMEOUT = 8
CROSSREF = "https://api.crossref.org/works"
ARXIV = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
SEMANTIC_SCHOLAR_FIELDS = "title,authors,year,venue,publicationVenue,externalIds,publicationTypes,publicationDate"
MAX_BULK_RESULTS = 100
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"?#]+", re.I)
ARXIV_RE = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]+/\d{7})(?:v\d+)?", re.I)
_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST = 0.0


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean(value) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).split())


def normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", "".join(
        c for c in text if not unicodedata.combining(c))).split())


def doi(value: str) -> str:
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value).strip(), flags=re.I)
    value = value.rstrip(".,;")
    return value.lower() if len(value) <= 256 and DOI_RE.fullmatch(value) else ""


def arxiv_id(value: str) -> str:
    value = re.sub(r"^https?://arxiv\.org/abs/|^arxiv:", "", str(value).strip(), flags=re.I)
    if not ARXIV_RE.fullmatch(value):
        return ""
    number = value.rsplit("/", 1)[-1]
    if not 1 <= int(number[2:4]) <= 12:
        return ""
    return re.sub(r"v\d+$", "", value.lower())


def allowed_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and not parsed.fragment and (
                (parsed.netloc == "api.crossref.org" and (
                    parsed.path == "/works" or parsed.path.startswith("/works/")))
                or (parsed.netloc == "export.arxiv.org" and parsed.path == "/api/query")
                or (parsed.netloc == "api.semanticscholar.org" and parsed.path == "/graph/v1/paper/search/bulk")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("metadata redirect refused")


def fetch(url: str) -> str:
    global _ARXIV_LAST_REQUEST
    if not allowed_url(url):
        raise ValueError("unapproved metadata endpoint")
    if url.startswith(ARXIV):
        with _ARXIV_LOCK:
            delay = 3.0 - (time.monotonic() - _ARXIV_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
            _ARXIV_LAST_REQUEST = time.monotonic()
    request = urllib.request.Request(url, headers={
        "User-Agent": "WikiGraph-bibliography/1.0 (public metadata lookup)",
        "Accept": "application/json, application/atom+xml",
    })
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("metadata response exceeds limit")
    return body.decode("utf-8")


def frontmatter(md_text: str) -> str:
    lines = md_text.splitlines()[:160]
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:#{1,6}\s*)?(?:(?:\d+(?:\.\d+)*|[IVX]+)[.\s]+)?(?:abstract|introduction|references|bibliography|摘要|参考文献)\b", line, re.I) or re.match(r"^\s*(?:\[1\]|1[.)])\s", line):
            return "\n".join(lines[:index])[:12000]
    return "\n".join(lines)[:12000]


def _identity_matches(title: str, families: list[str], front: str) -> bool:
    title_key = normalized(title)
    if len(title_key) < 15 or " " + title_key + " " not in " " + normalized(front) + " ":
        return False
    author_front = normalized(re.sub(r"<(sup|sub)\b[^>]*>.*?</\1>", " ", front,
                                     flags=re.I | re.S))
    present = lambda name: " " + normalized(name) + " " in " " + author_front + " "
    return bool(families and present(families[0]) and
                sum(present(f) for f in set(families)) >= min(2, len(set(families))))


def _header_arxiv_ids(md_text: str) -> set[str]:
    # An identifier mentioned inside an abstract can still belong to a cited
    # work. Only a standalone arXiv header can establish it without a lookup.
    return {arxiv_id(m.group(1)) for m in re.finditer(
        r"(?im)^\s*(?:#{1,6}\s*)?arxiv\s*:\s*(" + ARXIV_RE.pattern + r")\b",
        frontmatter(md_text))} - {""}


def _formal(record: dict, front: str) -> dict | None:
    if record.get("type") not in {"journal-article", "proceedings-article"}:
        return None
    title = clean((record.get("title") or [""])[0])
    venue = clean((record.get("container-title") or [""])[0])
    record_doi = doi(record.get("DOI", ""))
    if not title or not venue or not record_doi:
        return None
    authors, families = [], []
    for author in record.get("author") or []:
        family = clean(author.get("family") or author.get("name"))
        name = clean(" ".join(filter(None, [author.get("given"), family])))
        if not family or not name:
            return None
        authors.append(name)
        families.append(normalized(family))
    # Exact title plus first author and at least two distinct family names where
    # available. No fuzzy title matching, no affiliation-based author inference.
    if not _identity_matches(title, families, front):
        return None
    dates = {}
    for key in ("published-print", "published", "published-online", "issued"):
        parts = (record.get(key) or {}).get("date-parts", [[]])[0]
        if (parts and all(type(v) is int for v in parts) and 1500 <= parts[0] <= 2100
                and len(parts) <= 3 and (len(parts) < 2 or 1 <= parts[1] <= 12)
                and (len(parts) < 3 or 1 <= parts[2] <= 31)):
            try:
                date(*(parts + [1] * (3 - len(parts))))
            except ValueError:
                continue
            dates[key] = parts
    year_key = next((key for key in ("published-print", "published", "published-online") if key in dates), "")
    if not year_key:
        return None
    return {
        "title": title, "authors": authors, "venue": venue,
        "year": str(dates[year_key][0]), "doi": record_doi,
        "publication_status": "published", "publication_type": record["type"],
        "publication_dates": dates, "year_basis": year_key,
        "volume": clean(record.get("volume")), "issue": clean(record.get("issue")),
        "pages": clean(record.get("page")), "article_number": clean(record.get("article-number")),
        "source_content_version": "not_verified",
    }


def _linked_dois(body: str, requested: str, md_text: str) -> list[str]:
    if "<!" in body:
        return []
    root = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    result = []
    for entry in root.findall("atom:entry", ns):
        identity = entry.findtext("atom:id", default="", namespaces=ns)
        identity = re.sub(r"^https?://arxiv\.org/abs/", "", identity)
        if arxiv_id(identity) != requested:
            continue
        title = entry.findtext("atom:title", default="", namespaces=ns)
        families = [clean(node.text).split()[-1] for node in entry.findall("atom:author/atom:name", ns)
                    if clean(node.text)]
        if not _identity_matches(title, families, frontmatter(md_text)):
            continue
        for node in entry.findall("arxiv:doi", ns):
            value = doi(node.text or "")
            if value and not value.startswith("10.48550/arxiv."):
                result.append(value)
    return sorted(set(result))


def _conference_record(record: dict, front: str) -> dict | None:
    """Conservative DOI-less conference coverage; repository records never qualify."""
    venue = record.get("publicationVenue") or {}
    identifiers = record.get("externalIds") or {}
    paper_id = record.get("paperId") or ""
    dblp_key = identifiers.get("DBLP") or ""
    if (not isinstance(record.get("publicationTypes"), list)
            or "Conference" not in record["publicationTypes"]
            or venue.get("type") != "conference"
            or not re.fullmatch(r"[0-9a-f]{40}", paper_id)
            or not re.fullmatch(r"conf/[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", dblp_key)
            or identifiers.get("DOI")):
        return None
    title = clean(record.get("title"))
    venue_name = clean(venue.get("name"))
    year = record.get("year")
    if (not venue_name or normalized(venue_name) in {"arxiv", "arxiv org", "biorxiv", "medrxiv", "ssrn"}
            or type(year) is not int or not 1500 <= year <= 2100):
        return None
    authors = [clean(author.get("name")) for author in record.get("authors") or []]
    if not authors or any(not name for name in authors):
        return None
    if not _identity_matches(title, [name.split()[-1] for name in authors], front):
        return None
    return {
        "title": title, "authors": authors, "venue": venue_name,
        "year": str(year), "doi": "", "publication_status": "published",
        "publication_type": "proceedings-article", "publication_dates": {"published": [year]},
        "year_basis": "indexed_conference_year", "volume": "", "issue": "", "pages": "", "article_number": "",
        "source_content_version": "not_verified", "metadata_provider": "semantic_scholar",
        "author_name_precision": "as_indexed",
        "bibliographic_identifiers": {"semantic_scholar": paper_id, "dblp": dblp_key},
    }


def replay(receipt: dict, md_text: str, artifact_root: Path | None = None) -> dict | None:
    """Recompute acceptance; never trust a cached accepted flag or field values."""
    if isinstance(receipt, dict) and receipt.get("protocol_version") == "local-bibliography-v1":
        import bibliography_sidecar
        return bibliography_sidecar.replay(receipt, md_text, artifact_root)
    if (not isinstance(receipt, dict) or receipt.get("protocol_version") != PROTOCOL
            or receipt.get("source_sha256") != digest(md_text)):
        return None
    responses = receipt.get("responses") or []
    if not isinstance(responses, list) or len(responses) > MAX_REQUESTS:
        return None
    matched = {}
    for response in responses:
        if not isinstance(response, dict):
            return None
        body, url = response.get("body", ""), response.get("url", "")
        if (not isinstance(body, str) or not isinstance(url, str)
                or len(body.encode("utf-8")) > MAX_BYTES
                or not allowed_url(url) or digest(body) != response.get("sha256")):
            return None
        if url.startswith(SEMANTIC_SCHOLAR):
            try:
                payload = json.loads(body)
                records = payload.get("data")
                total = payload.get("total")
                # Do not silently select among a truncated or paginated result.
                if (not isinstance(records, list) or len(records) > MAX_BULK_RESULTS
                        or type(total) is not int or total != len(records) or payload.get("token")):
                    continue
                provider_matches = {}
                for record in records:
                    formal = _conference_record(record, frontmatter(md_text))
                    if formal:
                        provider_matches["s2:" + formal["bibliographic_identifiers"]["semantic_scholar"]] = formal
                matched.update(provider_matches)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
            continue
        if not url.startswith(CROSSREF):
            continue
        try:
            message = json.loads(body)["message"]
            records = message.get("items", []) if urllib.parse.urlsplit(url).path == "/works" else [message]
            if not isinstance(records, list) or len(records) > 5:
                return None
            provider_matches = {}
            for record in records:
                formal = _formal(record, frontmatter(md_text))
                if formal:
                    if urllib.parse.urlsplit(url).path.startswith("/works/"):
                        requested = doi(urllib.parse.unquote(urllib.parse.urlsplit(url).path[len("/works/"):]))
                        if requested != formal["doi"]:
                            continue
                    provider_matches[formal["doi"]] = formal
            matched.update(provider_matches)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
    if len(matched) != 1:
        return None
    result = next(iter(matched.values()))
    aid = arxiv_id(receipt.get("arxiv_id") or "")
    observed_ids = _header_arxiv_ids(md_text)
    linked_ids = set()
    for response in responses:
        if response["url"].startswith(ARXIV):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(response["url"]).query)
            requested = arxiv_id((query.get("id_list") or [""])[0])
            try:
                if requested and result["doi"] in _linked_dois(response["body"], requested, md_text):
                    linked_ids.add(requested)
            except (ET.ParseError, ValueError):
                pass
    result["arxiv_id"] = aid if aid in observed_ids | linked_ids else ""
    result["external_receipt"] = receipt
    result["review"] = {"status": "clean", "doc_type": "paper", "locked": True,
                        "provider": PROTOCOL, "conflicts": [], "review_notes": "Verified free formal publication record"}
    result["evidence"] = {key: "source.yaml#bibliographic.external_receipt" for key in ("title", "authors", "year", "venue", "doi")}
    return result


def resolve(md_text: str, candidates: dict, cache_path: Path, *, fetcher=None) -> dict:
    """A transaction pins results (including failures); changed input invalidates it."""
    execution_started = time.monotonic()
    fetcher = fetcher or fetch
    source_hash = digest(md_text)
    seeds = {field: sorted(set(str(v) for v in candidates.get(field, []) if v))
             for field in ("title", "doi", "arxiv_id")}
    input_hash = digest(json.dumps(seeds, sort_keys=True, ensure_ascii=False))
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("protocol_version") == PROTOCOL and cached.get("source_sha256") == source_hash
                    and cached.get("input_sha256") == input_hash):
                return {"receipt": cached, "bibliographic": replay(cached, md_text), "cache_hit": True,
                        "execution": {"request_count": 0, "elapsed_seconds": round(time.monotonic() - execution_started, 3)}}
        except (ValueError, OSError):
            pass
    receipt = {"protocol_version": PROTOCOL, "source_sha256": source_hash,
               "input_sha256": input_hash, "fetched_at": datetime.now(timezone.utc).isoformat(),
               "responses": [], "errors": [], "status": "unresolved", "request_count": 0,
               "arxiv_id": ""}
    started = time.monotonic()

    def request(url):
        if receipt["request_count"] >= MAX_REQUESTS:
            return ""
        receipt["request_count"] += 1
        try:
            body = fetcher(url)
            if len(body.encode("utf-8")) > MAX_BYTES:
                raise ValueError("metadata response exceeds limit")
            receipt["responses"].append({"url": url, "body": body, "sha256": digest(body)})
            return body
        except Exception as exc:
            # Exception type only: do not copy arbitrary upstream messages/URLs.
            error = {"url": url, "error_type": type(exc).__name__}
            if isinstance(exc, urllib.error.HTTPError):
                error["http_status"] = exc.code
            receipt["errors"].append(error)
            return ""

    ids = sorted({arxiv_id(v) for v in seeds["arxiv_id"]} - {""})
    dois = sorted({doi(v) for v in seeds["doi"]} - {""})
    for value in dois:
        if value.startswith("10.48550/arxiv."):
            aid = arxiv_id(value.split("arxiv.", 1)[1])
            if aid and aid not in ids:
                ids.append(aid)
    if len(ids) == 1:
        receipt["arxiv_id"] = ids[0]
    formal_dois = [v for v in dois if not v.startswith("10.48550/arxiv.")]
    if len(formal_dois) == 1:
        request(CROSSREF + "/" + urllib.parse.quote(formal_dois[0], safe=""))
    if not replay(receipt, md_text) and len(ids) == 1:
        body = request(ARXIV + "?" + urllib.parse.urlencode({"id_list": ids[0], "max_results": 1}))
        try:
            linked = _linked_dois(body, ids[0], md_text) if body else []
        except (ET.ParseError, ValueError):
            linked = []
        if len(linked) == 1 and linked[0] not in formal_dois:
            request(CROSSREF + "/" + urllib.parse.quote(linked[0], safe=""))
    title = ""
    if not replay(receipt, md_text):
        # At most one title query; title must occur in the current paper's front.
        front = normalized(frontmatter(md_text))
        titles = [v for v in seeds["title"] if len(normalized(v)) >= 15 and normalized(v) in front]
        if titles:
            title = max(titles, key=len)
            request(CROSSREF + "?" + urllib.parse.urlencode({"query.title": title[:300], "rows": 5}))
    if title and not replay(receipt, md_text) and receipt["request_count"] < MAX_REQUESTS:
        query = '"' + title[:300].replace('\\', '\\\\').replace('"', '\\"') + '"'
        request(SEMANTIC_SCHOLAR + "?" + urllib.parse.urlencode({
            "query": query, "fields": SEMANTIC_SCHOLAR_FIELDS,
        }))
    result = replay(receipt, md_text)
    receipt["status"] = "verified_published" if result else "unresolved"
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cache_path)
    return {"receipt": receipt, "bibliographic": replay(receipt, md_text), "cache_hit": False,
            "execution": {"request_count": receipt["request_count"],
                          "elapsed_seconds": round(time.monotonic() - execution_started, 3)}}
