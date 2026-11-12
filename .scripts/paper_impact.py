#!/usr/bin/env python3
"""Paper impact: shared deterministic store; semantic decisions belong to the host Agent."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from platform_compat import fcntl
import json
import re
import shlex
import sys
import uuid
from pathlib import Path

import yaml
import agent_task
import citing_papers as discovery
import source_ingest as si
import source_locator as sl
from external_bibliography import doi, normalized
from project_paths import repo_path

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "paper-impact-v1"
KINDS = {"positive", "adoption", "validation", "development", "background", "critical"}
POSITIVE = KINDS - {"background", "critical"}
MAX_JSON = 80_000_000
CREDENTIAL_KINDS = {"award", "academy", "fellowship", "appointment", "affiliation"}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def strings(value, label, nonempty=False):
    need(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value), f"invalid {label}")
    need(not nonempty or bool(value), f"{label} must not be empty")
    return value


def required_text(value, label, limit=20000):
    need(isinstance(value, str) and 0 < len(value.strip()) <= limit, f"invalid {label}")
    return value.strip()


def read_json(path):
    need(path.is_file() and path.stat().st_size <= MAX_JSON, "missing/oversized JSON")
    return json.loads(path.read_text(encoding="utf-8"))


def project_file(repo, project, filename=""):
    need(bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", project)), "invalid paper-impact project ID")
    target = repo_path(repo, f"projects/paper-impact/{project}") / filename
    return si.managed(repo, target.relative_to(Path(repo).absolute()))


def folder(repo, project):
    return project_file(repo, project)


def load(repo, project):
    data = read_json(project_file(repo, project, "state.json"))
    need(data.get("schema") == SCHEMA and data.get("id") == project, "invalid project state")
    return data


@contextmanager
def locked(repo, project):
    root = folder(repo, project)
    root.mkdir(parents=True, exist_ok=True)
    lock = project_file(repo, project, ".lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("paper-impact project is busy; retry the same command") from None
        try:
            yield root
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save(repo, state):
    state["revision"] += 1
    state["updated_at"] = discovery.now()
    need(len(json.dumps(state, ensure_ascii=False, indent=2).encode()) + 1 <= MAX_JSON,
         "project snapshot exceeds size limit; retain existing project and start a separate collection")
    si.atomic_json(project_file(repo, state['id'], "state.json"), state)


def initialize(repo, project, target_doi="", title=""):
    target_doi = doi(target_doi) if target_doi else ""
    need(target_doi or title.strip(), "a valid DOI or exact title is required")
    with locked(repo, project):
        path = project_file(repo, project, "state.json")
        need(not path.exists(), "project already exists")
        state = {"schema": SCHEMA, "id": project, "revision": 0, "created_at": discovery.now(),
                 "target": {"doi": target_doi, "title": title.strip()}, "batches": [], "candidates": {},
                 "config": {"contributions": [], "authors": [], "journals": [], "weights": {"author": 2, "journal": 2, "type": 1}},
                 "reviews": {}, "history": [], "selection": []}
        save(repo, state)
    return {"ok": True, "project": project, "target": state["target"], "next": "discover or import"}


def aliases(record):
    return discovery.aliases(record)


def add_batch(repo, state, batch):
    result = discovery.merge_batch(state, batch)
    save(repo, state)
    return result


def raw_path(repo, value):
    rel = Path(value)
    need(not rel.is_absolute() and rel.parts[:2] == ("academic", "raw"), "evidence must be academic Raw")
    path = si.managed(repo, rel)
    need(path.is_file(), f"Raw missing: {value}")
    return path


def excerpt(repo, item):
    need(isinstance(item, dict), "evidence reference must be an object")
    locator = required_text(item.get("locator"), "locator")
    path_part, loc = sl.split_locator(locator)
    need(loc and loc != "全篇", "a precise Raw locator is required")
    path = raw_path(repo, path_part)
    source_binding = si.binding_for_target(repo, path)
    need(not source_binding or source_binding["status"] == "valid", "invalid Source-first binding")
    binding = sl.companion_binding_for_target(path)
    need(not binding or binding["status"] != "invalid", "invalid companion binding")
    source, carrier = sl.evidence_targets(path, loc)
    source = raw_path(repo, source.relative_to(repo).as_posix())
    carrier = raw_path(repo, carrier.relative_to(repo).as_posix())
    need(sl.locator_status(loc, carrier) == "present", "Raw locator not found")
    body = sl.read_locator_text(carrier, loc)
    need(body is not None and len(body) <= 30000, "unreadable or oversized Raw excerpt")
    quote = required_text(item.get("quote"), "quote")
    need(" ".join(quote.split()) in " ".join(body.split()), "quote is not present verbatim at Raw locator")
    return {"locator": locator, "quote": quote, "context": body,
            "source": source.relative_to(repo).as_posix(), "source_sha256": si.digest(source),
            "carrier": carrier.relative_to(repo).as_posix(), "carrier_sha256": si.digest(carrier)}


def person_profile(repo, profile):
    """Read a shared people page and bind its Agent-verified claims to Raw."""
    relative = required_text(profile.get("profile_page"), "profile_page")
    need(relative.startswith("academic/wiki/authors/") and ".." not in Path(relative).parts and Path(relative).suffix == ".md",
         "profile_page must be an academic people page")
    path = si.managed(repo, relative)
    need(path.resolve().is_relative_to((repo / "academic/wiki/authors").resolve()), "people page escapes authors directory")
    need(path.is_file() and path.stat().st_size <= 1_000_000, "missing/oversized people page")
    text = path.read_text(encoding="utf-8")
    need(text.startswith("---\n") and "\n---" in text[4:], "people page frontmatter required")
    try:
        meta = yaml.safe_load(text.split("---", 2)[1])
    except yaml.YAMLError as exc:
        raise ValueError("invalid people page frontmatter") from exc
    need(isinstance(meta, dict) and meta.get("type") == "people" and meta.get("status", "current") == "current",
         "profile_page must be a current people page")
    data = meta.get("impact_profile")
    need(isinstance(data, dict) and data.get("schema") == "paper-impact-person-v1", "impact_profile schema required")
    identifiers = strings(data.get("identifiers"), "person identifiers", True)
    keys = {v for v in (profile.get("id"), profile.get("orcid")) if v}
    need(keys and keys <= set(identifiers), "people page identity must match stable author identifier")
    identity = excerpt(repo, data.get("identity_evidence"))
    sources = strings(meta.get("sources"), "people sources", True)

    def sourced(item):
        value = excerpt(repo, item)
        need(any(s.split("#", 1)[0] in {value["source"], value["carrier"]} for s in sources),
             "people credential source must be declared by page")
        return value

    identity = sourced(identity)
    credentials = deepcopy(data.get("credentials"))
    need(isinstance(credentials, list) and credentials, "people credentials required")
    for credential in credentials:
        need(isinstance(credential, dict) and credential.get("kind") in CREDENTIAL_KINDS, "invalid credential kind")
        for field in ("name", "organization", "period", "as_of", "basis_reason", "source_url"):
            required_text(credential.get(field), "credential " + field)
        need(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", credential["as_of"])), "credential date must be YYYY-MM-DD")
        date.fromisoformat(credential["as_of"])
        need(credential["source_url"].startswith("https://"), "credential source_url must use HTTPS")
        credential["evidence"] = sourced(credential.get("evidence"))
    return {"page": relative, "page_sha256": si.digest(path), "name": required_text(meta.get("title"), "person title"),
            "identifiers": identifiers, "identity_evidence": identity, "credentials": credentials}


def validate_config(repo, config, target=None, candidates=None):
    required = {"contributions", "authors", "journals", "weights"}
    need(isinstance(config, dict) and required <= set(config) <= required | {"screening"}, "config requires contributions/authors/journals/weights")
    need(isinstance(config["contributions"], list), "contributions must be a list")
    seen = set()
    for contribution in config["contributions"]:
        cid = required_text(contribution.get("id"), "contribution id", 80)
        need(cid not in seen, "duplicate contribution id")
        seen.add(cid)
        required_text(contribution.get("text"), "contribution")
        need(contribution.get("status") in {"proposed", "source_bound"}, "invalid contribution status")
        if contribution["status"] == "source_bound":
            contribution["evidence"] = excerpt(repo, contribution.get("evidence"))
            if target is not None:
                entries, _ = catalog(repo)
                need(any(e["path"] == contribution["evidence"]["carrier"] and
                         same_identity(target, e["title"], e["doi"]) for e in entries),
                     "contribution must bind the target paper's own Raw source")
    for category in ("authors", "journals"):
        need(isinstance(config[category], list), "priority profiles must be lists")
        keys = set()
        for profile in config[category]:
            required_text(profile.get("name"), "profile name", 400)
            need(type(profile.get("level")) is int and 1 <= profile["level"] <= 3, "priority level must be 1..3")
            required_text(profile.get("reason"), "priority reason")
            required_text(profile.get("as_of"), "profile as_of", 40)
            need(profile.get("basis") in {"user_preference", "source_bound"}, "priority basis required")
            if category == "authors" and profile.get("profile_page"):
                profile["profile_snapshot"] = person_profile(repo, profile)
            else:
                profile.pop("profile_snapshot", None)
            if profile["basis"] == "source_bound" and ("evidence" in profile or not profile.get("profile_snapshot")):
                profile["evidence"] = excerpt(repo, profile.get("evidence"))
            if category == "authors":
                key = profile.get("id") or profile.get("orcid")
                need(key or profile["basis"] == "user_preference", "verified author profile requires a stable ID; name alone is ambiguous")
                key = key or "name:" + normalized(profile["name"])
            else:
                key = profile.get("issn") or "name:" + normalized(profile["name"])
                if profile.get("issn"):
                    need(bool(re.fullmatch(r"\d{4}-\d{3}[\dXx]", profile["issn"])), "invalid profile ISSN")
            need(key not in keys, "duplicate priority profile")
            keys.add(key)
    weights = config["weights"]
    need(set(weights) == {"author", "journal", "type"} and all(type(x) is int and 0 <= x <= 5 for x in weights.values()) and sum(weights.values()) > 0, "weights must be integers 0..5, not all zero")
    screening = config.get("screening", [])
    need(isinstance(screening, list), "screening must be a list")
    seen = set()
    for item in screening:
        need(isinstance(item, dict), "invalid screening record")
        cid = item.get("candidate_id")
        need(isinstance(cid, str) and candidates is not None and cid in candidates and cid not in seen,
             "unknown/duplicate screening candidate")
        seen.add(cid)
        need(item.get("title_relevance") in {"direct", "related", "uncertain", "weak"}, "invalid title relevance")
        need(item.get("recommendation") in {"read", "hold", "exclude"}, "invalid screening recommendation")
        for field in ("title_reason", "authority_reason", "decision_reason"):
            required_text(item.get(field), "screening " + field)
        item["record_sha256"] = discovery.fingerprint(candidates[cid]["record"])
    return config


def same_identity(record, title, own_doi):
    # Title never overrides an explicitly conflicting DOI (e.g. different editions).
    if record.get("doi") and own_doi:
        return "doi" if record["doi"] == own_doi else ""
    return "title" if title and normalized(title) == normalized(record["title"]) else ""


def text_identity(path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    first = next((line for line in lines[:80] if re.match(r"^#\s+", line)), "")
    title = re.sub(r"^#\s+", "", first).strip()
    # Own DOI only from explicit front matter/header labels before the first section.
    stop = next((i for i, line in enumerate(lines[:80]) if re.match(r"^##\s+", line)), min(len(lines), 80))
    matches = re.findall(r"(?im)^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s<>]+)\s*$", "\n".join(lines[:stop]))
    return title, doi(matches[0]) if len(set(matches)) == 1 else ""


def catalog(repo):
    """Rebuild a local discovery projection from academic bundles and receipts, never SQLite."""
    entries, issues, seen = [], [], set()

    def add(path, title="", own_doi="", wiki="", trusted_metadata=False):
        rel = path.relative_to(repo).as_posix()
        if rel in seen:
            return
        try:
            path = raw_path(repo, rel)
            if path.suffix.lower() not in {".md", ".txt"}:
                return
            binding = si.binding_for_target(repo, path)
            need(not binding or binding["status"] == "valid", "invalid Source-first binding")
            companion = sl.companion_binding_for_target(path)
            need(not companion or companion["status"] != "invalid", "invalid companion binding")
            actual_title, actual_doi = text_identity(path)
            # Wiki is a locator bridge, not authority for paper identity.
            title = title if trusted_metadata and title else actual_title
            own_doi = own_doi if trusted_metadata and own_doi else actual_doi
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            refs = bool(re.search(r"(?im)^#{1,6}\s*(?:references|bibliography|参考文献)\s*$", "\n".join(lines)))
            entries.append({"path": rel, "title": title, "doi": own_doi, "wiki": wiki,
                            "has_references": refs, "line_count": len(lines)})
            seen.add(rel)
        except (OSError, ValueError) as exc:
            issues.append({"path": rel, "error": str(exc)})

    base = si.managed(repo, "academic/raw/works/papers")
    if base.exists():
        for path in sorted(base.glob("*/paper.md")):
            meta = si.managed(repo, (path.parent / "source.yaml").relative_to(repo))
            try:
                data = yaml.safe_load(meta.read_text()) if meta.is_file() else {}
                bib = (data or {}).get("bibliographic") or {}
                locked_bib = bib.get("review", {}).get("locked") is True
                add(path, bib.get("title", ""), doi(bib.get("doi", "")), trusted_metadata=locked_bib)
            except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
                issues.append({"path": str(meta.relative_to(repo)), "error": str(exc)[:300]})
    receipts = si.managed(repo, "academic/sources")
    if receipts.exists():
        for path in sorted(receipts.glob("S-*.json")):
            try:
                record = si.load_receipt(repo, path.stem)
                parsed = si.selected_parse(record)
                if record.get("source_status") == "source_ready" and parsed:
                    si.validate_binding(repo, record, parsed)
                    add(si.managed(repo, parsed["text_path"]))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append({"path": path.relative_to(repo).as_posix(), "error": str(exc)[:300]})
    wiki = si.managed(repo, "academic/wiki/papers")
    if wiki.exists():
        for page in sorted(wiki.glob("*.md")):
            try:
                page = si.managed(repo, page.relative_to(repo))
                raw = page.read_text(encoding="utf-8")
                if not raw.startswith("---\n"):
                    continue
                fm = yaml.safe_load(raw.split("---", 2)[1]) or {}
                sources = fm.get("sources", [])
                if not isinstance(sources, list):
                    continue
                for source in sources:
                    path_part = str(source).split("#", 1)[0]
                    if path_part.startswith("academic/raw/"):
                        add(si.managed(repo, path_part), wiki=page.relative_to(repo).as_posix())
                hint = {"title": str(fm.get("title") or ""), "doi": doi(fm.get("doi", ""))}
                if hint["title"] and not any(same_identity(hint, e["title"], e["doi"]) for e in entries):
                    entries.append({"path": "", **hint, "wiki": page.relative_to(repo).as_posix(),
                                    "has_references": False, "line_count": 0, "identity_basis": "wiki_metadata_hint"})
            except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
                issues.append({"path": str(page.relative_to(repo)), "error": str(exc)[:300]})
    return entries, issues


def match_kb(record, entries):
    matches = []
    if isinstance(entries, dict):
        pool = entries["doi"].get(record.get("doi"), []) + entries["title"].get(normalized(record["title"]), [])
        pool = list({(e["path"], e["wiki"]): e for e in pool}.values())
    else:
        pool = entries
    for entry in pool:
        method = same_identity(record, entry["title"], entry["doi"])
        if method:
            matches.append({**entry, "match": method})
    # Exact DOI takes precedence; title-only candidates remain possible other versions.
    exact = [x for x in matches if x["match"] == "doi"]
    if exact:
        matches = exact
    textual = [x for x in matches if x["path"]]
    if textual:
        matches = textual
    if matches and not textual:
        status = "metadata_only"
    elif len(matches) > 1:
        status = "ambiguous"
    elif matches:
        status = "fulltext_candidate" if matches[0]["has_references"] else "partial_text"
    else:
        status = "not_found"
    return {"status": status, "matches": matches,
            "next": "prepare evidence and verify completeness" if status == "fulltext_candidate" else
                    "resolve version/identity" if status == "ambiguous" else "suggest managed ingestion or supplementary full text"}


def profile_current(repo, profile):
    try:
        if profile.get("profile_page") and person_profile(repo, profile) != profile.get("profile_snapshot"):
            return False
        if profile.get("basis") == "source_bound" and ("evidence" in profile or not profile.get("profile_page")):
            expected = profile["evidence"]
            return excerpt(repo, expected) == expected
        return True
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return False


def ranking(repo, state, view="combined", entries=None):
    if entries is None:
        entries, issues = catalog(repo)
    else:
        issues = []
    rows = []
    cfg = state["config"]
    kb_index = {"doi": {}, "title": {}}
    for entry in entries:
        if entry["doi"]:
            kb_index["doi"].setdefault(entry["doi"], []).append(entry)
        if entry["title"]:
            kb_index["title"].setdefault(normalized(entry["title"]), []).append(entry)
    current_authors = [(p, profile_current(repo, p)) for p in cfg["authors"]]
    current_journals = [(p, profile_current(repo, p)) for p in cfg["journals"]]
    screening = {x["candidate_id"]: x for x in cfg.get("screening", [])}
    for cid, candidate in state["candidates"].items():
        record = candidate["record"]
        reasons, possible, author_level, journal_level = [], [], 0, 0
        authority_stale = False
        for profile, current in current_authors:
            if not current:
                if any((profile.get("id") and profile["id"] == a["id"]) or
                       (profile.get("orcid") and profile["orcid"] == a["orcid"]) or
                       normalized(profile["name"]) == normalized(a["name"]) for a in record["authors"]):
                    possible.append(f"作者配置来源已变化：{profile['name']}")
                    authority_stale = True
                continue
            for author in record["authors"]:
                exact = ((profile.get("id") and profile["id"] == author["id"]) or
                         (profile.get("orcid") and profile["orcid"] == author["orcid"]))
                named = normalized(profile["name"]) == normalized(author["name"])
                if exact or (named and not (profile.get("id") or profile.get("orcid")) and profile["basis"] == "user_preference"):
                    author_level = max(author_level, profile["level"])
                    reasons.append({"channel": "author", "name": author["name"], "position": author["position"],
                                    "level": profile["level"], "basis": profile["basis"], "identity": "identifier" if exact else "name_preference_unverified",
                                    "reason": profile["reason"]})
                    if exact and profile.get("profile_snapshot"):
                        reasons[-1]["person_profile"] = profile["profile_snapshot"]
                elif named:
                    possible.append(f"同名作者待消歧：{author['name']}")
        for profile, current in current_journals:
            matched = profile["issn"] in record["journal"]["issns"] if profile.get("issn") else normalized(profile["name"]) == normalized(record["journal"]["name"])
            if not current:
                if matched:
                    possible.append(f"期刊配置来源已变化：{profile['name']}")
                    authority_stale = True
                continue
            if matched:
                journal_level = max(journal_level, profile["level"])
                reasons.append({"channel": "journal", "name": profile["name"], "level": profile["level"],
                                "basis": profile["basis"], "reason": profile["reason"]})
        type_level = 1 if record["type"].lower() in {"review", "editorial", "commentary", "perspective"} else 0
        weights = cfg["weights"]
        score = author_level * weights["author"] + journal_level * weights["journal"] + type_level * weights["type"]
        target_authors = state["target"].get("authors", [])
        overlap = sorted({a["name"] for a in record["authors"] for b in target_authors if
                          (a["id"] and a["id"] == b.get("id")) or
                          (a["orcid"] and a["orcid"] == b.get("orcid"))})
        possible_overlap = sorted({a["name"] for a in record["authors"] for b in target_authors if normalized(a["name"]) == normalized(b.get("name", ""))})
        kb = match_kb(record, kb_index)
        screened = deepcopy(screening.get(cid))
        if screened:
            screened["status"] = "current" if screened["record_sha256"] == discovery.fingerprint(record) and not authority_stale else "stale"
            if screened["status"] == "stale":
                possible.append("初筛依据的书目或资历来源已变化，需复核标题、作者与平台")
        prior = state["reviews"].get(cid)
        if prior and prior.get("coverage") == "complete":
            identity = prior["identity"]
            chosen = [x for x in kb["matches"] if x["path"] == identity["carrier"]]
            if chosen and si.digest(raw_path(repo, identity["carrier"])) == identity["carrier_sha256"]:
                kb = {**kb, "status": "fulltext_candidate", "matches": chosen,
                      "alternatives": kb["matches"], "selection_basis": "host_fulltext_review",
                      "next": "read accepted source or explicitly choose another edition"}
        rows.append({"id": cid, "title": record["title"], "doi": record["doi"], "journal": record["journal"]["name"],
                     "authors": record["authors"], "published": record["published"], "score": score,
                     "components": {"author": author_level, "journal": journal_level, "type": type_level},
                     "channels": [name for name, level in (("author", author_level), ("journal", journal_level)) if level],
                     "reasons": reasons, "uncertainties": possible,
                     "screening": screened,
                     "author_overlap": overlap, "possible_name_overlap": possible_overlap,
                     "independence": "overlap" if overlap else "unknown",
                     "kb": kb, "selected": cid in state["selection"],
                     "first_seen_at": candidate["first_seen_at"], "last_seen_at": candidate["last_seen_at"],
                     "citation_context": record["citation_context"]})
    need(view in {"combined", "author", "journal"}, "invalid reading view")
    rows.sort(key=lambda row: (-(row["score"] if view == "combined" else row["components"][view]), -row["score"], row["id"]))
    return {"rows": rows, "catalog_issues": issues, "view": view,
            "notice": "阅读优先级，不是评价质量；名单是偏好，期刊发表不代表编辑部评价；未知不代表低质量。"}


def identity_quote(record, quote):
    quoted_dois = {doi(x.rstrip(".,;)]}")) for x in re.findall(r"10\.\d{4,9}/[^\s<>\"?#]+", quote, re.I)}
    if record.get("doi") and quoted_dois:
        return record["doi"] in quoted_dois
    return bool(record.get("title") and normalized(record["title"]) in normalized(quote))


def marker_matches(reference_marker, citation_marker):
    """Check numbered/ranged markers mechanically; author-year requires literal identity."""
    reference = reference_marker.strip(" []().")
    if reference.isdigit():
        numbers = set()
        for first, last in re.findall(r"(\d+)(?:\s*[-–—]\s*(\d+))?", citation_marker):
            start, end = int(first), int(last or first)
            if end < start or end - start > 1000:
                return False
            numbers.update(range(start, end + 1))
        return int(reference) in numbers
    return normalized(reference_marker) in normalized(citation_marker)


def validate_presentation(repo, review, record, bindings):
    """Bind Agent-authored copy and context facts; do not infer stature or meaning."""
    presentation = review.get("presentation")
    if presentation is None:
        return
    need(isinstance(presentation, dict) and set(presentation) == {
        "grant_text", "ppt_text", "occurrence_indices", "context_facts"}, "invalid presentation fields")
    for field in ("grant_text", "ppt_text"):
        required_text(presentation.get(field), field)
    indices = presentation.get("occurrence_indices")
    need(isinstance(indices, list) and indices and all(type(i) is int for i in indices),
         "presentation requires integer occurrence indices")
    need(len(set(indices)) == len(indices) and all(0 <= i < len(review["occurrences"]) for i in indices),
         "invalid presentation occurrence indices")
    need(review["coverage"] == "complete" and all(
        review["occurrences"][i]["kind"] in POSITIVE and
        review["occurrences"][i]["attribution"] != "uncertain" for i in indices),
        "presentation requires complete, attributable positive evidence")
    facts = presentation.get("context_facts")
    need(isinstance(facts, list), "presentation context_facts must be a list")
    for fact in facts:
        need(isinstance(fact, dict) and fact.get("kind") in CREDENTIAL_KINDS | {"honor", "venue"},
             "invalid presentation context fact")
        for field in ("text", "as_of", "basis_reason"):
            required_text(fact.get(field), "context fact " + field)
        need(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact["as_of"])), "context fact date must be YYYY-MM-DD")
        date.fromisoformat(fact["as_of"])
        if fact["kind"] != "venue":
            author_id = required_text(fact.get("author_id"), "context fact author_id")
            need(any(author_id in (a.get("id"), a.get("orcid")) for a in record["authors"]),
                 "context fact author must match citing author identifier")
        original = excerpt(repo, fact.get("evidence"))
        fact["evidence"] = original
        bindings.append(original)


def validate_reviews(repo, state, reviews, expected_ids, allowed_paths):
    need(isinstance(reviews, list) and len(reviews) == len(expected_ids), "one review required for every selected paper")
    need({r.get("candidate_id") for r in reviews} == set(expected_ids), "review candidate set mismatch")
    contributions = {c["id"]: c for c in state["config"]["contributions"]}
    result = []
    for review in reviews:
        review = deepcopy(review)
        cid = review["candidate_id"]
        need(cid in state["candidates"], "unknown candidate")
        need(review.get("coverage") in {"complete", "partial"}, "full-text coverage declaration required")
        required_text(review.get("coverage_note"), "coverage note")
        need(review.get("independence") in {"unknown", "overlap", "independent"}, "invalid independence")
        required_text(review.get("independence_reason"), "independence reason")
        bindings = []

        def bound(item):
            value = excerpt(repo, item)
            need(value["carrier"] in allowed_paths[cid], "excerpt belongs to another paper or an unselected carrier")
            bindings.append(value)
            return value

        identity = bound(review.get("identity"))
        need(identity_quote(state["candidates"][cid]["record"], identity["quote"]), "identity quote must identify the citing paper")
        reference = bound(review.get("reference"))
        need(identity_quote(state["target"], reference["quote"]), "reference must identify target paper")
        marker = required_text(review.get("reference_marker"), "reference marker", 200)
        if marker.strip(" []().").isdigit():
            number = marker.strip(" []().")
            need(bool(re.search(r"(?:^|\n)\s*\[?" + re.escape(number) + r"(?:\]|[.)]|\s)", reference["quote"])),
                 "target reference marker missing in reference entry")
        else:
            need(normalized(marker) in normalized(reference["quote"]), "target reference marker missing in reference entry")
        if review["independence"] == "independent":
            authors = state["candidates"][cid]["record"]["authors"]
            overlap = any((a.get("id") and a["id"] == b.get("id")) or
                          (a.get("orcid") and a["orcid"] == b.get("orcid"))
                          for a in authors for b in state["target"].get("authors", []))
            need(not overlap, "shared author identifier contradicts independent assessment")
            independent = excerpt(repo, review.get("independence_evidence"))
            bindings.append(independent)
            review["independence_evidence"] = independent
        need(isinstance(review.get("occurrences"), list), "occurrences must be a list")
        if not review["occurrences"]:
            required_text(review.get("no_evidence_reason"), "no-evidence reason")
        for occurrence in review["occurrences"]:
            need(occurrence.get("kind") in KINDS, "unknown evidence kind")
            need(occurrence.get("attribution") in {"target", "group", "uncertain"}, "invalid attribution")
            need(occurrence.get("statement_mode") in {"direct_quote", "synthesis"}, "statement mode required")
            original = bound(occurrence.get("evidence"))
            actual_marker = required_text(occurrence.get("citation_marker"), "citation marker", 300)
            need(actual_marker in original["quote"], "citation marker absent from quoted context")
            need(marker_matches(marker, actual_marker), "citation marker does not include target reference")
            required_text(occurrence.get("reference_mapping_reason"), "reference mapping reason")
            required_text(occurrence.get("translation"), "translation")
            required_text(occurrence.get("analysis"), "interpretation")
            strings(occurrence.get("contribution_ids"), "contribution_ids", occurrence["kind"] in POSITIVE)
            need(set(occurrence["contribution_ids"]) <= set(contributions), "unknown contribution")
            strings(occurrence.get("limitations"), "limitations", occurrence["attribution"] != "target" or review["coverage"] != "complete")
            strings(occurrence.get("unsupported_claims"), "unsupported_claims", True)
            required_text(occurrence.get("supported_statement"), "supported statement")
            occurrence["evidence"] = original
        validate_presentation(repo, review, state["candidates"][cid]["record"], bindings)
        used = {c for occurrence in review["occurrences"] for c in occurrence["contribution_ids"]}
        snapshot = {c: discovery.fingerprint(contributions[c]) for c in sorted(used)}
        need("contribution_snapshot" not in review or review["contribution_snapshot"] == snapshot,
             "contribution definition changed; reassess linked evidence")
        review["contribution_snapshot"] = snapshot
        review["identity"], review["reference"] = identity, reference
        review["bindings"] = bindings
        review["reviewed_at"] = review.get("reviewed_at") or discovery.now()
        result.append(review)
    return result


def task_dir(repo, task_id):
    need(bool(re.fullmatch(r"T-[a-f0-9]{24}", task_id)), "invalid task ID")
    return si.managed(repo, f"temp/paper-impact/{task_id}")


def evidence_template(cid):
    return {"candidate_id": cid, "coverage": "partial", "coverage_note": "",
            "identity": {"locator": "", "quote": ""}, "reference": {"locator": "", "quote": ""},
            "reference_marker": "", "independence": "unknown", "independence_reason": "",
            "occurrences": [], "no_evidence_reason": "", "presentation": None}


def prepare(repo, state, phase, ids=None, sources=None):
    need(phase in {"configure", "evidence"}, "invalid task phase")
    task_id = "T-" + uuid.uuid4().hex[:24]
    prefix = f"temp/paper-impact/{task_id}"
    root = task_dir(repo, task_id)
    root.mkdir(parents=True)
    paths, candidates, source_hashes = {}, [], {}
    if phase == "configure":
        template = deepcopy(state["config"])
        for cid, candidate in list(state["candidates"].items())[:100]:
            record = candidate["record"]
            candidates.append({"id": cid, "title": record["title"], "journal": record["journal"],
                               "authors": record["authors"][:50], "author_count": len(record["authors"])})
    else:
        ids = ids or state["selection"]
        need(ids and len(ids) == len(set(ids)), "select at least one unique candidate")
        rows = {x["id"]: x for x in ranking(repo, state)["rows"]}
        overrides = {}
        for source in sources or []:
            cid, separator, path = source.partition("=")
            need(separator and cid in ids and cid not in overrides, "source override must be selected CID=Raw-path, once per candidate")
            overrides[cid] = path
        for cid in ids:
            need(cid in rows, "unknown candidate ID")
            row = rows[cid]
            if cid in overrides:
                need(overrides[cid] in [x["path"] for x in row["kb"]["matches"]], "source override must match this paper's own bibliographic identity")
                paths[cid] = [overrides[cid]]
            else:
                need(row["kb"]["status"] == "fulltext_candidate", f"{cid}: {row['kb']['status']}; resolve identity with --source CID=Raw-path or suggest managed ingestion first")
                paths[cid] = [x["path"] for x in row["kb"]["matches"]]
            for path in paths[cid]:
                carrier = raw_path(repo, path)
                original, _ = sl.evidence_targets(carrier, "L1-L1")
                for frozen in (carrier, original):
                    relative = frozen.relative_to(repo).as_posix()
                    source_hashes[relative] = si.digest(raw_path(repo, relative))
            candidates.append(row)
        template = {"reviews": [evidence_template(cid) for cid in ids]}
    packet = {"schema": "paper-impact-task-v1", "task_id": task_id, "project": state["id"], "phase": phase,
              "base_revision": state["revision"], "base_sha256": discovery.fingerprint(state),
              "target": state["target"], "config": state["config"], "candidates": candidates,
              "candidate_count": len(state["candidates"]),
              "more_candidates_command": f"python3 .scripts/wg.py paper-impact rank {state['id']} --limit 50 --offset 0",
              "allowed_paths": paths, "source_hashes": source_hashes,
              "output_template": template}
    si.atomic_json(root / "input.json", packet)
    protocol = {"name": "paper-impact-analysis-v1", "spec": "operations/PAPER_IMPACT.md", "phase": phase}
    command = f"python3 .scripts/paper_impact.py {{action}} {shlex.quote(state['id'])} --task {task_id}"
    manifest = agent_task.make_task(kind="paper-impact", transaction_id=task_id,
        inputs=[{"name": "task-input", "path": prefix + "/input.json"}, {"name": "contract", "path": "operations/PAPER_IMPACT.md"}],
        outputs=[{"name": "analysis", "path": prefix + "/analysis.json"}], protocol=protocol,
        commands={"check": command.format(action="check"), "commit": command.format(action="commit")},
        context={"phase": phase, "project": state["id"], "source_boundary": "Raw only; input metadata and citation snippets are discovery hints"})
    si.atomic_json(root / "manifest.json", manifest)
    si.atomic_json(root / "receipt.json", {"input_sha256": discovery.fingerprint(packet), "manifest_sha256": discovery.fingerprint(manifest)})
    return {"ok": True, "status": "prepared", "task_id": task_id, "agent_task": manifest}


def checked(repo, state, task_id):
    root = task_dir(repo, task_id)
    packet = read_json(si.managed(repo, root.relative_to(repo) / "input.json"))
    receipt = read_json(si.managed(repo, root.relative_to(repo) / "receipt.json"))
    need(discovery.fingerprint(packet) == receipt["input_sha256"], "task input changed")
    need(packet["project"] == state["id"] and packet["task_id"] == task_id, "task belongs to another project")
    need(packet["base_revision"] == state["revision"] and packet["base_sha256"] == discovery.fingerprint(state), "stale task; prepare again using current state")
    for path, expected in packet["source_hashes"].items():
        need(si.digest(raw_path(repo, path)) == expected, "source changed after task preparation")
    output = read_json(si.managed(repo, root.relative_to(repo) / "analysis.json"))
    if packet["phase"] == "configure":
        validated = validate_config(repo, deepcopy(output), state["target"], state["candidates"])
    else:
        need(set(output) == {"reviews"}, "analysis requires reviews only")
        validated = validate_reviews(repo, state, output["reviews"], list(packet["allowed_paths"]), packet["allowed_paths"])
    return packet, validated, discovery.fingerprint(output)


def commit(repo, state, task_id):
    previous = next((h for h in state["history"] if h.get("task_id") == task_id), None)
    if previous:
        output = read_json(si.managed(repo, task_dir(repo, task_id).relative_to(repo) / "analysis.json"))
        need(discovery.fingerprint(output) == previous["output_sha256"], "committed task output was changed")
        return {"ok": True, "status": "completed", "idempotent": True, "task_id": task_id}
    packet, validated, output_hash = checked(repo, state, task_id)
    if packet["phase"] == "configure":
        prior = deepcopy(state["config"])
        state["config"] = validated
    else:
        prior = {r["candidate_id"]: state["reviews"].get(r["candidate_id"]) for r in validated}
        for review in validated:
            state["reviews"][review["candidate_id"]] = review
    state["history"].append({"task_id": task_id, "phase": packet["phase"], "at": discovery.now(),
                             "output_sha256": output_hash, "previous": prior})
    save(repo, state)
    return {"ok": True, "status": "completed", "task_id": task_id, "revision": state["revision"]}


def evidence_ranking(repo, state, rows):
    ready, excluded, observations = [], [], []
    for cid, review in state["reviews"].items():
        try:
            paths = {cid: [review["identity"]["carrier"]]}
            validated = validate_reviews(repo, state, [review], [cid], paths)[0]
            need(validated["bindings"] == review["bindings"], "source or quoted context changed")
            review_ready, review_observations = [], []
            for occurrence_index, occ in enumerate(review["occurrences"]):
                for contribution in state["config"]["contributions"]:
                    if contribution["id"] in occ["contribution_ids"] and contribution["status"] == "source_bound":
                        need(excerpt(repo, contribution["evidence"]) == contribution["evidence"], "contribution source changed")
                item = {"candidate_id": cid, "title": state["candidates"][cid]["record"]["title"],
                        "journal": state["candidates"][cid]["record"]["journal"]["name"],
                        "authors": state["candidates"][cid]["record"]["authors"],
                        "identity": review["identity"], "target_reference": review["reference"],
                        "coverage_note": review["coverage_note"],
                        "independence_evidence": review.get("independence_evidence"),
                        "independence": review["independence"], "independence_reason": review["independence_reason"],
                        "coverage": review["coverage"], **occ, "occurrence_index": occurrence_index}
                if occ["kind"] not in POSITIVE or occ["attribution"] == "uncertain" or review["coverage"] != "complete":
                    review_observations.append(item)
                    continue
                row = rows[cid]
                # Transparent ordinal tuple: specific attribution, verified independence, then prominence.
                item["ranking_components"] = {"specificity": 2 if occ["attribution"] == "target" else 1,
                    "independence": 1 if review["independence"] == "independent" else 0,
                    "author": row["components"]["author"], "journal": row["components"]["journal"]}
                review_ready.append(item)
            ready.extend(review_ready)
            observations.extend(review_observations)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            excluded.append({"candidate_id": cid, "reason": str(exc)})
    ready.sort(key=lambda x: tuple(-v for v in x["ranking_components"].values()) + (x["candidate_id"],))
    # Diversity is a visible annotation; never assert that distinct names imply independent teams.
    used = set()
    for item in ready:
        pairs = {(c, item["kind"]) for c in item["contribution_ids"]}
        item["adds_contribution_or_evidence_type"] = bool(pairs - used)
        used.update(pairs)
    return {"usable_evidence": ready, "other_observations": observations, "invalid_reviews": excluded}


def md(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def application_summary(state, evidence):
    """Group only revalidated usable evidence by citing work, with stable indices."""
    groups = {}
    for number, item in enumerate(evidence["usable_evidence"], 1):
        groups.setdefault(item["candidate_id"], []).append((number, item))
    summaries = []
    for cid, entries in groups.items():
        presentation = state["reviews"][cid].get("presentation")
        if presentation:
            entries = [(n, e) for n, e in entries if e["occurrence_index"] in presentation["occurrence_indices"]]
        if not entries:
            continue
        first = entries[0][1]
        summaries.append({"candidate_id": cid, "title": first["title"], "journal": first["journal"],
            "status": "ready" if presentation else "existing_statements",
            "grant_text": presentation["grant_text"] if presentation else " ".join(dict.fromkeys(e["supported_statement"] for _, e in entries)),
            "ppt_text": presentation["ppt_text"] if presentation else "",
            "context_facts": deepcopy(presentation["context_facts"]) if presentation else [],
            "evidence_numbers": [n for n, _ in entries],
            "contribution_ids": sorted({c for _, e in entries for c in e["contribution_ids"]}),
            "limitations": list(dict.fromkeys(s for _, e in entries for s in e["limitations"])),
            "unsupported_claims": list(dict.fromkeys(s for _, e in entries for s in e["unsupported_claims"])),
            "independence": first["independence"]})
    return summaries


def report(repo, state, view="combined"):
    ranking_result = ranking(repo, state, view)
    rows = ranking_result["rows"]
    evidence = evidence_ranking(repo, state, {x["id"]: x for x in rows})
    needs = [r for r in rows if r["selected"] and r["kb"]["status"] != "fulltext_candidate"]
    contribution_map = []
    for c in state["config"]["contributions"]:
        matching = [i + 1 for i, e in enumerate(evidence["usable_evidence"]) if c["id"] in e["contribution_ids"]]
        contribution_map.append({**c, "evidence_numbers": matching})
    result = {"ok": True, "schema": "paper-impact-report-v1", "generated_at": discovery.now(),
              "project": state["id"], "target": state["target"], "revision": state["revision"],
              "batches": [{k: b[k] for k in ("id", "provider", "started_at", "finished_at", "scope", "status", "complete", "errors", "new_candidate_ids")} for b in state["batches"]],
              "contributions": contribution_map, "reading": ranking_result, "ingestion_suggestions": needs,
              "application_summary": application_summary(state, evidence), **evidence}
    lines = ["# 单篇论文第三方评价与影响证据", "", f"目标：{md(state['target'].get('title') or state['target'].get('doi'))}",
             f"生成时间：{result['generated_at']}；版本：{state['revision']}", "",
             "## 申报书与答辩 PPT 可用表述", ""]
    if not result["application_summary"]:
        lines += ["目前没有经核验可用于开篇的第三方正面证据；需补齐全文或完成证据评述。", ""]
    for summary in result["application_summary"]:
        refs = "、".join("E" + str(n) for n in summary["evidence_numbers"])
        lines += [f"### {md(summary['title'])}", "",
                  "**申报书表述（归纳）**" if summary["status"] == "ready" else "**既有审核表述（申报书与 PPT 双版本待补）**", "",
                  md(summary["grant_text"]), ""]
        if summary["ppt_text"]:
            lines += ["**答辩 PPT 表述（归纳）**", "", md(summary["ppt_text"]), ""]
        lines += [f"依据：{refs}（同一篇施引论文）；支持贡献：{', '.join(summary['contribution_ids'])}；独立性：{summary['independence']}。", ""]
        for fact in summary["context_facts"]:
            lines += [f"身份/平台依据：{md(fact['text'])}（事实日期：{fact['as_of']}）；`{fact['evidence']['locator']}`。", ""]
        if summary["limitations"]:
            lines += ["使用限定：" + md("；".join(summary["limitations"])), ""]
        lines += ["不可扩写为：" + md("；".join(summary["unsupported_claims"])), ""]
    lines += ["检索记录用于发现；事实依据为下列 Raw 原文。作者团队评价不等于某位共同作者个人发言，期刊刊载不等于编辑部认可。", "",
              "## 检索范围与批次", ""]
    for batch in result["batches"]:
        lines.append(f"- {md(batch['started_at'])} · {md(batch['provider'])} · {batch['status']} · 新增 {len(batch['new_candidate_ids'])} · {md(batch['scope'])}")
        for error in batch["errors"]:
            lines.append(f"  - 检索问题：{md(error)}")
    lines += ["", "## 贡献与证据覆盖", ""]
    for c in contribution_map:
        lines.append(f"- {md(c['id'])}：{md(c['text'])}（{c['status']}）；证据：{', '.join('E'+str(x) for x in c['evidence_numbers']) or '尚缺'}")
    lines += ["", "## 阅读优先级", "", ranking_result["notice"], "", "|编号|题名|期刊|作者/期刊/类型|入选理由|库内状态|", "|---|---|---|---|---|---|"]
    for row in rows:
        why = "; ".join(f"{r['name']}: {r['reason']} ({r['basis']})" for r in row["reasons"]) or "尚无重点名单匹配；保留候选"
        lines.append(f"|{row['id']}|{md(row['title'])}|{md(row['journal'])}|{row['components']['author']}/{row['components']['journal']}/{row['components']['type']}|{md(why)}|{row['kb']['status']}|")
    lines += ["", "## 人物资历与代表性初筛", "",
              "人物、机构与期刊用于解释评价主体及平台的分量；标题用于判断与目标的关系。初筛建议不自动改变入选名单，不设固定摄入篇数。", ""]
    shown = set()
    labels = {"award": "奖项", "academy": "院士/院成员", "fellowship": "学会 Fellow", "appointment": "学术任职", "affiliation": "机构"}
    for row in rows:
        for reason in row["reasons"]:
            person = reason.get("person_profile")
            if not person or person["page"] in shown:
                continue
            shown.add(person["page"])
            lines += [f"### {md(person['name'])}", "", f"人物页：`{person['page']}`", ""]
            for credential in person["credentials"]:
                lines.append(f"- {labels[credential['kind']]}：{md(credential['name'])}；{md(credential['organization'])}；{md(credential['period'])}（核对日期 {credential['as_of']}）。Raw：`{credential['evidence']['locator']}`")
            lines.append("")
    if not shown:
        lines += ["尚无已绑定且来源有效的人物资历页；阅读偏好不视为获奖、院士或 Fellow 事实。", ""]
    lines += ["|候选|标题相关性及理由|人物/机构/期刊依据|初筛建议及理由|", "|---|---|---|---|"]
    for row in rows:
        screening = row.get("screening")
        if not screening:
            continue
        if screening["status"] != "current":
            lines.append(f"|{row['id']}|待重新核验|来源或书目已变化|旧建议已失效|")
        else:
            recommendation = {"read": "建议阅读全文", "hold": "暂缓/补元信息", "exclude": "本轮排除"}[screening['recommendation']]
            lines.append(f"|{row['id']}|{screening['title_relevance']}：{md(screening['title_reason'])}|{md(screening['authority_reason'])}|{recommendation}：{md(screening['decision_reason'])}|")
    for row in rows:
        if row["uncertainties"]:
            lines.append(f"- {row['id']} 待核验：{md('；'.join(row['uncertainties']))}")
    lines += ["", "## 选中文献的摄入建议", ""]
    for row in needs:
        lines.append(f"- {row['id']} · {md(row['title'])}：{row['kb']['status']}。核对版本后，通过原受管摄入补齐全文。")
    if not needs:
        lines.append("当前无已选文献待补齐，或尚未选择文献。")
    lines += ["", "## 可用第三方证据与申报表述", ""]
    for index, item in enumerate(evidence["usable_evidence"], 1):
        lines += [f"### E{index} · {md(item['title'])}", "", f"期刊：{md(item['journal'])}；类型：{item['kind']}；归属：{item['attribution']}；独立性：{item['independence']}",
                  "作者：" + md("、".join(a["name"] for a in item["authors"])),
                  f"贡献：{', '.join(item['contribution_ids'])}；互补证据：{item['adds_contribution_or_evidence_type']}", "",
                  "原文：", "", "> " + md(item["evidence"]["quote"]), "", "译文：" + md(item["translation"]),
                  "", "分析：" + md(item["analysis"]), "", "可用表述（" + item["statement_mode"] + "）：" + md(item["supported_statement"]),
                  "", "限定：" + md("；".join(item["limitations"]) or "无额外限定；仍以原文为准"),
                  "", "不可支持：" + md("；".join(item["unsupported_claims"])),
                  "", f"原文定位：`{item['evidence']['locator']}`；原件：`{item['evidence']['source']}`",
                  "", "目标参考条目：" + md(item["target_reference"]["quote"]),
                  f"参考条目定位：`{item['target_reference']['locator']}`", ""]
    lines += ["## 其他引用、局限与待核验项", ""]
    for item in evidence["other_observations"]:
        lines.append(f"- {item['candidate_id']} · {item['kind']} / {item['attribution']} / {item['coverage']}：{md(item['analysis'])}；`{item['evidence']['locator']}`")
    for issue in evidence["invalid_reviews"] + ranking_result["catalog_issues"]:
        lines.append("- 未纳入有效证据：" + md(json.dumps(issue, ensure_ascii=False)))
    for cid, review in state["reviews"].items():
        if not review["occurrences"]:
            lines.append(f"- {cid}：未发现可提取评价。{md(review.get('no_evidence_reason', ''))}")
    root = folder(repo, state["id"])
    json_path = si.managed(repo, root.relative_to(repo) / "report.json")
    markdown_path = si.managed(repo, root.relative_to(repo) / "report.md")
    si.atomic_json(json_path, result)
    temporary = si.managed(repo, root.relative_to(repo) / (".report-" + uuid.uuid4().hex + ".md"))
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(markdown_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "report": str(markdown_path), "data": str(json_path), "candidates": len(rows),
            "usable_evidence": len(evidence["usable_evidence"]), "invalid_reviews": evidence["invalid_reviews"],
            "ingestion_suggestions": len(needs), "catalog_issues": ranking_result["catalog_issues"]}


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("project"); p.add_argument("--doi", default=""); p.add_argument("--title", default="")
    for action in ("discover", "import", "rank", "select", "prepare", "check", "commit", "report", "status"):
        p = sub.add_parser(action); p.add_argument("project")
        if action == "discover": p.add_argument("--max-pages", type=int, default=5)
        if action == "import": p.add_argument("--file", required=True)
        if action in {"rank", "report"}: p.add_argument("--view", choices=["combined", "author", "journal"], default="combined")
        if action == "rank":
            p.add_argument("--limit", type=int, default=50)
            p.add_argument("--offset", type=int, default=0)
        if action == "select": p.add_argument("--ids", nargs="+", required=True)
        if action == "prepare":
            p.add_argument("--phase", choices=["configure", "evidence"], required=True)
            p.add_argument("--ids", nargs="*")
            p.add_argument("--source", action="append", default=[], help="explicit CID=Raw-path choice among bibliographic matches")
        if action in {"check", "commit"}: p.add_argument("--task", required=True)
    return ap


def execute(args, repo=REPO):
    repo = Path(repo).resolve()
    if args.command == "init":
        if args.doi:
            need(bool(doi(args.doi)), "invalid DOI")
        return initialize(repo, args.project, args.doi, args.title)
    with locked(repo, args.project):
        state = load(repo, args.project)
        if args.command == "discover":
            return add_batch(repo, state, discovery.discover(state["target"], max_pages=args.max_pages))
        if args.command == "import":
            path = Path(args.file)
            if not path.is_absolute(): path = repo / path
            path = path.resolve()
            need(not path.is_relative_to((repo / "private").resolve()), "private metadata cannot enter public paper-impact")
            return add_batch(repo, state, discovery.imported(read_json(path), state["target"]))
        if args.command == "rank":
            need(1 <= args.limit <= 1000 and args.offset >= 0, "rank limit must be 1..1000 and offset nonnegative")
            ranked = ranking(repo, state, args.view)
            total = len(ranked["rows"])
            ranked["rows"] = ranked["rows"][args.offset:args.offset+args.limit]
            return {"ok": True, **ranked, "total": total, "offset": args.offset,
                    "truncated": args.offset+len(ranked["rows"]) < total}
        if args.command == "select":
            need(set(args.ids) <= set(state["candidates"]), "unknown selection ID")
            state["selection"] = sorted(set(args.ids)); save(repo, state)
            return {"ok": True, "selected": state["selection"]}
        if args.command == "prepare": return prepare(repo, state, args.phase, args.ids, args.source)
        if args.command == "check":
            checked(repo, state, args.task)
            return {"ok": True, "status": "validated", "semantic_judgment": "host_agent"}
        if args.command == "commit": return commit(repo, state, args.task)
        if args.command == "report": return report(repo, state, args.view)
        if args.command == "status":
            return {"ok": True, "project": state["id"], "revision": state["revision"], "target": state["target"],
                    "batches": len(state["batches"]), "candidates": len(state["candidates"]),
                    "selection": state["selection"], "reviews": len(state["reviews"]), "config": state["config"]}
        raise ValueError("unknown command")


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"ok": False, "status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
