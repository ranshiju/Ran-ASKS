#!/usr/bin/env python3
"""query_actions.py — A+ 编排层检索动作执行 + token 计量(纯程序,不涉 LLM)

复用:wiki_locator/source_locator 确定性截取 + tiktoken 实计
职责:执行单个检索动作,返回真实文本+token。不碰语义判断,不碰状态。
"""
from __future__ import annotations
import subprocess
import json
import re
import sys
import inspect
import sqlite3
import yaml
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / ".scripts"
sys.path.insert(0, str(_SCRIPTS))
import source_locator as sl
import wiki_locator as wl
import graph_lib as gl
import node_semantics as ns
import hub_semantics as hs
import query_graph as qg

PUBLIC_DOMAINS = {"academic", "admin", "teaching", "business"}
_QUERY_SCOPE = ContextVar("query_storage_scope", default=None)


def scope_for(domain: str = "", *paths: str) -> str:
    """Select storage scope; an explicit scope always wins over path inference."""
    if domain:
        if domain not in PUBLIC_DOMAINS | {"private", "public", "cross-domain"}:
            raise ValueError("unsupported query domain")
        return "private" if domain == "private" else "public"
    active = _QUERY_SCOPE.get()
    if active:
        return active
    for value in paths:
        part = str(value or "").split("#", 1)[0]
        path = Path(part)
        if path.is_absolute():
            try:
                path = path.relative_to(_REPO)
            except ValueError:
                continue
        if path.parts and path.parts[0] == "private":
            return "private"
    return "public"


def check_path_scope(value: str | Path) -> Path:
    """Reject traversal/symlink escapes before reading either source representation."""
    path = Path(str(value).split("#", 1)[0])
    target = path if path.is_absolute() else _REPO / path
    try:
        logical = target.relative_to(_REPO)
        physical = target.resolve().relative_to(_REPO.resolve())
    except ValueError:
        raise ValueError("query path escapes repository") from None
    if not logical.parts or not physical.parts or ".." in logical.parts:
        raise ValueError("query path traversal is forbidden")
    expected_private = scope_for("", str(logical)) == "private"
    if ((logical.parts[0] == "private") != expected_private
            or (physical.parts[0] == "private") != expected_private):
        raise ValueError("private/public query scope mismatch")
    return target


def query_db_path() -> Path:
    return check_path_scope("private/graph.db" if _QUERY_SCOPE.get() == "private"
                            else "cross-domain/graph.db")


@contextmanager
def query_scope(scope: str):
    selected = scope_for(scope)
    current = _QUERY_SCOPE.get()
    if current and current != selected:
        raise ValueError("nested query cannot change private/public scope")
    token = _QUERY_SCOPE.set(selected)
    try:
        from embed_helper import offline
        with gl.graph_scope(query_db_path()), (offline() if selected == "private" else nullcontext()):
            yield
    finally:
        _QUERY_SCOPE.reset(token)


def scoped_query(fn):
    """Keep direct Python, wg and API callers on the same deterministic boundary."""
    signature = inspect.signature(fn)
    @wraps(fn)
    def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        identifiers = [values.get(key, "") for key in ("page", "node", "hub", "locator", "name")]
        with query_scope(scope_for(values.get("domain", ""), *identifiers)):
            for key, value in ((key, values.get(key, "")) for key in ("page", "node", "hub", "locator", "name")):
                managed = str(value).split("/", 1)[0] in PUBLIC_DOMAINS | {"private", "cross-domain"}
                if value and (key in {"page", "locator"} or managed or Path(str(value)).is_absolute()):
                    check_path_scope(value)
            return fn(*args, **kwargs)
    return wrapped


def _connect():
    return gl.connect(query_db_path(), read_only=True)


def check_wiki_result(result: dict) -> dict:
    check_path_scope(result["page"])
    for citation in result.get("raw_citations", []):
        check_path_scope(citation)
    return result


def read_wiki_data(page: str, section: str = "") -> dict:
    check_path_scope(page)
    target = wl.resolve_wiki_path(page)
    if target is not None:
        check_path_scope(target)
    return check_wiki_result(wl.read_wiki_locator(page, section))

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
except ImportError:
    _ENC = None

def _tok(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    return len(text) // 2

@scoped_query
def read_section(page: str, section: str = "") -> tuple[str, int]:
    """按 Wiki heading slug 截取一节，并只带回该节使用的 Raw locators。"""
    try:
        result = read_wiki_data(page, section)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return f"[ERROR {exc}]", _tok(str(exc))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)

def graph_query(command: str, args: list[str]) -> tuple[str, int]:
    """执行只读图查询；图结果纳入统一编排轨迹和 token 计量。"""
    db_script = _SCRIPTS / "query_graph.py"
    r = subprocess.run(["python3", str(db_script), command, *args, "--json", "--db", str(query_db_path())],
                       capture_output=True, text=True, cwd=_REPO)
    text = r.stdout.strip() or r.stderr.strip()
    return text, _tok(text)


@scoped_query
def graph_search(term: str = "", include_deprecated: bool = False) -> tuple[str, int]:
    include_deprecated = _history_enabled(include_deprecated)
    args = [term, "--include-deprecated"] if include_deprecated else [term]
    return graph_query("search", args) if term else ("[ERROR 缺 term]", 0)


@scoped_query
def graph_neighbors(node: str = "", depth: str = "2", similar_topk: str = "",
                    profile: str = "", families: str = "", max_nodes: str = "",
                    max_edges: str = "", max_scanned_rows: str = "") -> tuple[str, int]:
    # similar_topk: ""=用CLI默认(5), "0"=排除相似边, "-1"=全部, "N"=动态K上限
    # 编排层按 loop_count 注入(第一轮0=纯知识边,续轮渐进放开)
    args = [node, "--depth", str(depth)]
    if similar_topk:
        args += ["--similar-topk", str(similar_topk)]
    if profile:
        args += ["--profile", profile]
    if families:
        args += ["--families", families]
    for name, value in (("max-nodes", max_nodes), ("max-edges", max_edges), ("max-scanned-rows", max_scanned_rows)):
        if value != "":
            args += ["--" + name, str(value)]
    return graph_query("neighbors", args) if node else ("[ERROR 缺 node]", 0)


@scoped_query
def graph_relations(node: str = "", predicate: str = "", profile: str = "",
                    families: str = "") -> tuple[str, int]:
    args = [node]
    if predicate:
        args += ["--predicate", predicate]
    if profile:
        args += ["--profile", profile]
    if families:
        args += ["--families", families]
    return graph_query("relations", args) if node else ("[ERROR 缺 node]", 0)


@scoped_query
def graph_hub_of(page: str = "") -> tuple[str, int]:
    return graph_query("hub_of", [page]) if page else ("[ERROR 缺 page]", 0)


@scoped_query
def node_resolve(
    name: str = "", context: str = "", node_types: str = "", topk: str = "5"
) -> tuple[str, int]:
    """名称→canonical node_id；只返回身份计划，不修改图。"""
    if not name.strip():
        return "[ERROR 缺 name]", 0
    types = [item.strip() for item in str(node_types).split(",") if item.strip()]
    conn = _connect()
    try:
        result = ns.resolve_node(
            conn, name, context, node_types=types or None,
            top_k=max(1, min(int(topk), ns.MAX_TOP_K)),
        )
        paths = [item["node_id"] for item in result.get("candidates", []) if item.get("node_id")]
        if result.get("node_id"):
            paths.append(result["node_id"])
        result["version_context"] = qg.query_version_context(conn, paths)
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


@scoped_query
def semantic_search(query: str = "", scope: str = "node", topk: str = "8",
                    include_deprecated: bool = False) -> tuple[str, int]:
    """按含义召回节点/Hub；相关候选不构成节点同一性。"""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    include_deprecated = _history_enabled(include_deprecated)
    conn = _connect()
    try:
        result = ns.semantic_search(
            conn, query, scope=scope,
            top_k=max(1, min(int(topk), ns.MAX_TOP_K)),
            include_deprecated=include_deprecated,
        )
        result["version_context"] = qg.query_version_context(conn, [item["node_id"] for item in result.get("candidates", []) if item.get("node_id")])
    finally:
        conn.close()
    if result.get("decision") == "invalid":
        return f"[ERROR {result.get('reason', 'invalid semantic search')}]", 0
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


@scoped_query
def hub_route(page: str = "", topk: str = "5") -> tuple[str, int]:
    """读取论文的可定位方向句并与 canonical Hub Scope 路由；只读。"""
    if not page.strip():
        return "[ERROR 缺 page]", 0
    conn = _connect()
    try:
        result = hs.route_paper(conn, page, top_k=max(1, min(int(topk), 20)))
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


@scoped_query
def hub_inspect(hub: str = "") -> tuple[str, int]:
    """返回 Hub Scope、类型化普通成员及动力学候选；只读。"""
    if not hub.strip():
        return "[ERROR 缺 hub]", 0
    conn = _connect()
    try:
        result = hs.inspect_hub(conn, hub)
        result["writes"] = False
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)



DOMAIN_DIRS = PUBLIC_DOMAINS


def _wiki_query_terms(query: str) -> list[str]:
    terms = []
    for chunk in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9-]{2,}", query.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            terms.extend(chunk[offset:offset + 2] for offset in range(len(chunk) - 1))
            terms.extend(chunk[offset:offset + 3] for offset in range(len(chunk) - 2))
        else:
            terms.append(chunk)
    return list(dict.fromkeys(terms))


def _wiki_match(item: dict, query: str, terms: list[str]):
    needle = " ".join(query.casefold().split())
    labels = [" ".join(label.casefold().split()) for label in
              (item["title"], item["filename"], *item["aliases"]) if label]
    body = " ".join((item["navigation"] + " " + item["profile"]).casefold().split())
    label_hits = {term for term in terms if any(term in label for label in labels)}
    body_hits = {term for term in terms if term in body}
    matched = label_hits | body_hits
    if needle in labels:
        tier, kind = 0, "exact_label"
    elif any(needle in label for label in labels):
        tier, kind = 1, "label_phrase"
    elif needle in body:
        tier, kind = 2, "navigation_phrase"
    elif matched:
        tier, kind = 3, "lexical"
    else:
        return None
    candidate = {
        "path": item["path"], "title": item["title"], **_wiki_version(item),
        "score": max(1, len(label_hits) * 2 + len(body_hits)), "navigation": item["navigation"],
        "match_kind": kind, "matched_terms": sorted(matched),
        "term_coverage": len(matched) / len(terms) if terms else 0,
    }
    return (tier, -len(matched), -len(label_hits), item["path"]), candidate


def _history_enabled(value: bool | str) -> bool:
    return qg.history_enabled(value)


def _wiki_version(item: dict) -> dict:
    return {"status": item.get("status", "unknown"),
            "page_status": item.get("page_status", "unknown"),
            "superseded_by": item.get("superseded_by", "")}


def _wiki_admitted(item: dict, include_deprecated: bool) -> bool:
    return include_deprecated or "deprecated" not in (item.get("status"), item.get("page_status"))


def _wiki_read_entry(page: str, text: str, *terms: str) -> dict:
    """Select a real section from this scan, without publishing the scanned body."""
    section = wl.best_cited_section(page, *terms, text=text)
    if section is not None and section.level < 2:
        section = None
    if section is None:
        sections, _definitions = wl.parse_wiki_text(text)
        candidates = [s for s in sections if s.level >= 2 and s.slug not in {"sources", "来源"}]
        section = next((s for s in candidates if s.slug == "navigation"), None)
        if section is None:
            section = next(iter(candidates), None)
    if section is None:
        return {}
    return {"semantic_address": f"{page}#{section.slug}", "read_action": "read_section",
            "read_input": {"page": page, "section": section.slug}}


@scoped_query
def wiki_recall(query: str = "", domain: str = "", topk: str = "8",
                include_deprecated: bool = False) -> tuple[str, int]:
    """Single-read Wiki navigation recall with literal labels and stable coverage ranking."""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    domain = domain.strip().lower()
    if _QUERY_SCOPE.get() == "private":
        domain = "private"
    if domain and domain not in DOMAIN_DIRS | {"private"}:
        return f"[ERROR 不支持 domain: {domain}]", 0
    limit = max(1, min(int(topk), 20))
    include_deprecated = _history_enabled(include_deprecated)
    conn = _connect()
    try:
        result, _pages = _wiki_recall_data(conn, query, domain, limit, include_deprecated)
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def _interleave_candidates(lanes, limit, key, prefer=None):
    """Bounded union: each explicit discovery lane gets a turn before cutoff."""
    if prefer is not None:
        preferred = {}
        for lane in lanes:
            for item in lane:
                identity, priority = key(item), prefer(item)
                if identity not in preferred or priority < preferred[identity][0]:
                    preferred[identity] = (priority, item)
        # Different queries may expose different previews of one bound object.
        lanes = [[preferred[key(item)][1] for item in lane] for lane in lanes]
    out, seen = [], set()
    for rank in range(max((len(lane) for lane in lanes), default=0)):
        for lane in lanes:
            if rank < len(lane) and key(lane[rank]) not in seen:
                item = lane[rank]
                seen.add(key(item))
                out.append(item)
                if len(out) >= limit:
                    return out
    return out


def _subject_features(subjects, item):
    import lexical_relevance as lr
    labels = [item.get("title", ""), item.get("filename", ""), *item.get("aliases", [])]
    body = "\n".join(item.get(k, "") for k in ("excerpt", "navigation", "capsule"))
    return lr.subject_match(subjects, labels, body)


def _subject_key(features):
    return -len(features["matched"]), -len(features["labels"])


def _question_match(query, item):
    import lexical_relevance as lr
    body = "\n".join(item.get(k, "") for k in ("excerpt", "navigation", "capsule"))
    return lr.match(query, item.get("title", ""), lr.excerpt(query, body)["excerpt"])


def _wiki_recall_data(conn, query: str, domain: str, limit: int,
                      include_deprecated: bool, subjects=()) -> tuple[dict, dict]:
    terms = _wiki_query_terms(query)
    roots = [domain] if domain else sorted(DOMAIN_DIRS)
    pages = {}
    prefixes = [f"{root}/wiki/%" for root in roots]
    condition = " OR ".join("nodes.path LIKE ?" for _ in prefixes)
    rows = conn.execute(
        "SELECT nodes.path,nodes.title,nodes.status,aliases.alias FROM nodes "
        "LEFT JOIN aliases ON aliases.node_path=nodes.path "
        f"WHERE {condition} ORDER BY nodes.path,aliases.alias", prefixes,
    ) if conn is not None else ()
    for row in rows:
        item = pages.setdefault(row["path"], {
            "path": row["path"], "title": row["title"] or Path(row["path"]).name,
            "status": row["status"], "filename": Path(row["path"]).name,
            "aliases": [], "navigation": "", "profile": "",
        })
        if row["alias"]:
            item["aliases"].append(row["alias"])
    for root in roots:
        for path in sorted((_REPO / root / "wiki").rglob("*.md")):
            if path.name in {"index.md", "log.md"}:
                continue
            check_path_scope(path)
            text = path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"^## Navigation\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
            rel = str(path.relative_to(_REPO)).removesuffix(".md")
            graph_registered = rel in pages
            item = pages.setdefault(rel, {
                "path": rel, "title": path.stem, "filename": path.stem,
                "status": "unknown", "aliases": [], "navigation": "", "profile": "",
            })
            item["_scan_text"] = text
            frontmatter = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
            metadata = {}
            if frontmatter:
                try:
                    loaded = yaml.load(frontmatter.group(1), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
                    if isinstance(loaded, dict):
                        metadata = loaded
                except yaml.YAMLError:
                    pass
            item["page_status"] = metadata.get("status") if isinstance(metadata.get("status"), str) else "unknown"
            item["superseded_by"] = metadata.get("superseded_by") if isinstance(metadata.get("superseded_by"), str) else ""
            sources = metadata.get("sources", [])
            item["source_links"] = []
            for source in sources if isinstance(sources, list) else []:
                if isinstance(source, str) and re.match(r"^(?:academic|admin|teaching|business|private)/raw/", source):
                    check_path_scope(source)
                    item["source_links"].append(source)
            if not graph_registered:
                item["status"] = item["page_status"]
            item["navigation"] = match.group(1).strip() if match else ""
            profile = hs.read_paper_profile(rel, text=text)
            if profile is not None:
                item["profile"] = profile.text
    direction_profile_hits = sum(
        bool(item["profile"]) and any(term in item["profile"].casefold() for term in terms)
        for item in pages.values() if _wiki_admitted(item, include_deprecated)
    )
    ranked = [matched for item in pages.values() if _wiki_admitted(item, include_deprecated)
              and (matched := _wiki_match(item, query, terms)) is not None]
    ranked.sort(key=lambda matched: matched[0])
    mode = "direct"
    out = [matched[1] for matched in ranked[:limit]]
    if subjects:
        lanes = []
        for subject in subjects:
            subject_terms = _wiki_query_terms(subject)
            matches = [matched for item in pages.values() if _wiki_admitted(item, include_deprecated)
                       and (matched := _wiki_match(item, subject, subject_terms)) is not None]
            matches.sort(key=lambda matched: matched[0])
            lanes.append([matched[1] for matched in matches[:limit]])
        out = _interleave_candidates([*lanes, out], limit, lambda item: item["path"])
    if not out and conn is not None:
        mode = "graph_fallback"
        graph_paths = set()
        for term in terms:
            for row in conn.execute(
                "SELECT DISTINCT nodes.path FROM nodes "
                "LEFT JOIN aliases ON aliases.node_path=nodes.path "
                "WHERE nodes.title LIKE ? OR aliases.alias LIKE ? ORDER BY nodes.path LIMIT 30",
                (f"%{term}%", f"%{term}%"),
            ):
                node = row["path"]
                for edge in conn.execute("SELECT subject,object FROM edges WHERE subject=? OR object=?", (node, node)):
                    other = edge["object"] if edge["subject"] == node else edge["subject"]
                    if other.startswith(tuple(f"{root}/wiki/" for root in roots)):
                        graph_paths.add(other)
        eligible_paths = [path for path in sorted(graph_paths)
                          if _wiki_admitted(pages.get(path, {}), include_deprecated)]
        for path in eligible_paths[:limit]:
            item = pages.get(path, {})
            out.append({
                "path": path, "title": item.get("title", Path(path).name),
                **_wiki_version(item), "score": 0,
                "navigation": item.get("navigation", ""), "match_kind": "graph_fallback",
                "matched_terms": [], "term_coverage": 0,
            })
    for item in out:
        check_path_scope(item["path"])
        text = pages.get(item["path"], {}).get("_scan_text")
        if text is not None:
            item.update(_wiki_read_entry(item["path"], text, query, item["title"]))
    result = {"query": query, "domain": domain or "all", "mode": mode, "count": len(out), "candidates": out,
          "include_deprecated": include_deprecated,
          "direction_profile_hits": direction_profile_hits,
          "sensitive_review": domain == "business",
          "next": "read returned section entry, then Raw; candidates without an entry need a valid Wiki section" if out else "no_candidate"}
    return result, pages


def admin_recall(query: str = "", topk: str = "8", include_deprecated: bool = False) -> tuple[str, int]:
    """兼容旧入口；新调用统一使用 wiki_recall(domain=admin)。"""
    return wiki_recall(query, "admin", topk, include_deprecated)


_INTENT_WEIGHTS = {
    "fact": (1.0, 0.35),
    "relation": (0.55, 1.0),
    "explanation": (1.0, 0.55),
    "exploration": (0.8, 0.8),
    "lineage": (0.45, 1.0),
}


def _adjacent_wiki_pages(conn, node: str, domain: str = "", limit: int = 30,
                         wiki_pages: Optional[dict] = None, include_deprecated: bool = False) -> list[str]:
    prefix = f"{domain}/wiki/" if domain else ""
    pages = []
    excluded = [path for path, item in (wiki_pages or {}).items()
                if not _wiki_admitted(item, include_deprecated)]
    condition = ""
    parameters = [node, node]
    if excluded:
        condition = " AND (CASE WHEN subject=? THEN object ELSE subject END) NOT IN (" + ",".join("?" for _ in excluded) + ")"
        parameters.extend([node, *excluded])
    parameters.append(limit * 3)
    for row in conn.execute(
        "SELECT subject,object FROM edges WHERE (subject=? OR object=?)" + condition + " LIMIT ?",
        parameters,
    ):
        other = row["object"] if row["subject"] == node else row["subject"]
        if "/wiki/" not in other or (prefix and not other.startswith(prefix)):
            continue
        if wiki_pages is not None and not _wiki_admitted(wiki_pages.get(other, {}), include_deprecated):
            continue
        if other not in pages:
            pages.append(other)
        if len(pages) >= limit:
            break
    return pages


def _section_capsule(page: str, *terms: str) -> dict:
    target = check_path_scope(f"{page.removesuffix('.md')}.md")
    if not target.is_file():
        return {}
    section = wl.best_cited_section(target, *terms)
    if section is None:
        return {}
    for citation in section.raw_citations:
        check_path_scope(citation)
    body = re.sub(r"\s+", " ", section.text).strip()
    return {
        "semantic_address": f"{page.removesuffix('.md')}#{section.slug}",
        "heading": section.title,
        "capsule": body[:320],
        "raw_citations": list(section.raw_citations),
    }


def _query_entity_terms(query: str) -> list[str]:
    terms = []
    math_patterns = (
        r"\|\s*[A-Za-z][A-Za-z0-9_]*\s*\|",
        r"\b(?:Arg|Re|Im)\s+[A-Za-z][A-Za-z0-9_]*(?:\*|\^\*|†)?",
        r"\b[A-Za-z][A-Za-z0-9_]*(?:\*|\^\*|†)",
    )
    for pattern in math_patterns:
        for match in re.finditer(pattern, query):
            candidate = re.sub(r"\s+", " ", match.group(0)).strip()
            if candidate:
                terms.append(candidate)
    chinese_stops = {"是什么", "怎么算", "如何算", "为什么", "如何", "怎样"}
    for chunk in re.findall(r"[\u3400-\u9fff]+|[A-Za-z][A-Za-z0-9-]{1,}", query):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            parts = re.split(
                r"(?:有什么关系|什么关系|是什么|怎么算|如何算|为什么|如何|怎样|与|和|及|的)",
                chunk,
            )
            terms.extend(
                part for part in parts if len(part) >= 2 and part not in chinese_stops
            )
        else:
            terms.append(chunk)
    return list(dict.fromkeys(terms))[:8]


def _navigation_recall_data(query, intent, domain, limit, include_deprecated, subjects=()):
    """Existing Wiki/Graph ranking; usable Wiki files survive a missing graph projection."""
    wiki_weight, graph_weight = _INTENT_WEIGHTS[intent]
    scores: dict[str, float] = {}
    signals: dict[str, list[dict]] = {}
    seen_signals = set()
    channel_scores: dict[tuple[str, str], float] = {}

    def add(path: str, channel: str, rank: int, weight: float, via: str = ""):
        if not path or "/wiki/" not in path:
            return
        if domain and not path.startswith(f"{domain}/wiki/"):
            return
        if not _wiki_admitted(wiki_pages.get(path, {}), include_deprecated):
            return
        signal_key = (path, channel, via)
        if signal_key in seen_signals:
            return
        seen_signals.add(signal_key)
        # Different graph routes are explanations of one channel's hit, not votes.
        key = (path, channel)
        contribution = weight / (60.0 + rank)
        previous = channel_scores.get(key, 0.0)
        if contribution > previous:
            scores[path] = scores.get(path, 0.0) + contribution - previous
            channel_scores[key] = contribution
        signals.setdefault(path, []).append({"channel": channel, "rank": rank, "via": via})

    issues = []
    try:
        conn = _connect()
    except (FileNotFoundError, sqlite3.Error) as exc:
        conn = None
        issues.append({"channel": "graph", "error": str(exc), "next": "restore graph via managed maintenance"})
    try:
        try:
            wiki_result, wiki_pages = _wiki_recall_data(
                conn, query, domain, min(max(limit * 2, 8), 20), include_deprecated,
                **({"subjects": subjects} if subjects else {}))
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            conn = None
            issues.append({"channel": "graph", "error": str(exc), "next": "restore graph via managed maintenance"})
            wiki_result, wiki_pages = _wiki_recall_data(None, query, domain, limit, include_deprecated,
                                                      **({"subjects": subjects} if subjects else {}))
        wiki_items = wiki_result["candidates"]
        for rank, item in enumerate(wiki_items, start=1):
            add(item.get("path", ""), "wiki", rank, wiki_weight)
        exact_rank = 0
        entity_terms = list(dict.fromkeys([*subjects, *_query_entity_terms(query)]))[:8]
        for term in entity_terms if conn is not None else []:
            for item in qg.search_nodes(conn, term, include_deprecated=True).get("nodes", [])[:8]:
                exact_rank += 1
                node = item.get("path", "")
                if "/wiki/" in node:
                    add(node, "graph_exact", exact_rank, graph_weight * 1.2, term)
                for page_path in _adjacent_wiki_pages(conn, node, domain, limit, wiki_pages, include_deprecated):
                    add(page_path, "graph_exact", exact_rank, graph_weight * 1.2, term)
        semantic = ns.semantic_search(conn, query, scope="node", top_k=max(limit * 2, 8), include_deprecated=True) if conn is not None else {}
        graph_nodes = semantic.get("candidates", [])
        if not graph_nodes and conn is not None:
            graph_nodes = qg.search_nodes(conn, query, include_deprecated=True).get("nodes", [])
        for rank, item in enumerate(graph_nodes, start=1):
            node = item.get("node_id") or item.get("path") or ""
            if "/wiki/" in node:
                add(node, "graph", rank, graph_weight, node)
            for page_path in _adjacent_wiki_pages(conn, node, domain, limit, wiki_pages, include_deprecated):
                add(page_path, "graph", rank, graph_weight, node)
    finally:
        if conn is not None:
            conn.close()

    wiki_by_path = {item.get("path"): item for item in wiki_items}
    capsules = {
        path: _section_capsule(path, query, *(s.get("via", "") for s in signals[path]))
        for path in scores
    }
    for path, capsule in capsules.items():
        if capsule.get("raw_citations"):
            scores[path] += 0.15 / 60.0
    subject_features = {path: _subject_features(subjects, {**wiki_pages.get(path, {}), **capsules[path]})
                        for path in scores} if subjects else {}
    ranked = sorted(scores, key=lambda path: (
        *(_subject_key(subject_features[path]) if subjects else ()), -scores[path], path))[:limit]
    candidates = []
    for path in ranked:
        capsule = capsules[path]
        item = wiki_by_path.get(path) or wiki_pages.get(path, {})
        if not capsule.get("semantic_address"):
            text = wiki_pages.get(path, {}).get("_scan_text")
            if text is not None:
                capsule = {**capsule, **_wiki_read_entry(path, text, query, item.get("title", ""))}
        candidates.append({
            "path": path,
            "title": item.get("title") or path.rsplit("/", 1)[-1],
            **_wiki_version(wiki_pages.get(path, {})),
            "score": round(scores[path], 6),
            "signals": signals[path],
            "navigation": item.get("navigation", ""),
            "source_links": wiki_pages.get(path, {}).get("source_links", []),
            **({"subject_match": subject_features[path]} if subjects else {}),
            **capsule,
        })
    candidates.sort(key=lambda item: (*(_subject_key(item["subject_match"]) if subjects else ()),
                                      -item["score"], item["path"]))
    result = {
        "query": query,
        "intent": intent,
        "domain": domain or "all",
        "fusion": "weighted_rrf",
        "weights": {"wiki": wiki_weight, "graph": graph_weight},
        "count": len(candidates),
        "include_deprecated": include_deprecated,
        "candidates": candidates,
        "identity_claim": False,
        "issues": issues,
        "next": "read semantic_address, then raw_citations",
    }
    return result


@scoped_query
def hybrid_recall(query: str = "", intent: str = "exploration", domain: str = "",
                  topk: str = "8", include_deprecated: bool = False,
                  subjects: Optional[list[str]] = None) -> tuple[str, int]:
    """Discover typed Source/KU/Question/Wiki candidates without promoting them to evidence."""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    if subjects is not None and (not isinstance(subjects, list) or len(subjects) > 3 or
            any(not isinstance(s, str) or not s.strip() or len(s) > 160 for s in subjects)):
        return "[ERROR subjects 需为至多3个非空名称，每个不超过160字符]", 0
    subjects = list(dict.fromkeys(" ".join(s.split()) for s in (subjects or [])))
    intent = intent.strip().lower() or "exploration"
    if intent not in _INTENT_WEIGHTS:
        return f"[ERROR 不支持 intent: {intent}]", 0
    domain = domain.strip().lower()
    if _QUERY_SCOPE.get() == "private":
        domain = "private"
    if domain and domain not in DOMAIN_DIRS | {"private"}:
        return f"[ERROR 不支持 domain: {domain}]", 0
    limit = max(1, min(int(topk), 20))
    pool_limit = min(max(limit * 2, 8), 20)
    include_deprecated = _history_enabled(include_deprecated)
    try:
        navigation = _navigation_recall_data(query, intent, domain, pool_limit, include_deprecated,
                                            **({"subjects": subjects} if subjects else {}))
    except (OSError, sqlite3.Error) as exc:
        navigation = {"candidates": [], "issues": [{"channel": "navigation", "error": str(exc)}],
                      "fusion": "weighted_rrf", "weights": dict(zip(("wiki", "graph"), _INTENT_WEIGHTS[intent]))}
    channels = {"navigation": {"status": "partial" if navigation["issues"] else "ok",
                               "count": len(navigation["candidates"]), "issues": navigation["issues"]}}
    groups = {"navigation": [], "knowledge": [], "source": []}
    for candidate in navigation["candidates"]:
        item = {**candidate, "kind": "wiki", "id": candidate["path"], "navigation_score": candidate["score"]}
        address = item.get("semantic_address", "")
        page, _, section = address.partition("#")
        if page and section:
            item.update(read_action="read_section", read_input={"page": page, "section": section})
        groups["navigation"].append(item)

    if domain in {"", "academic"}:
        import knowledge_store as ks
        import source_ingest as si
        import lexical_relevance as lr
        for name, search in (("knowledge", ks.search), ("source", si.search)):
            queries = list(dict.fromkeys([*subjects, query]))
            lanes, issues, last_found = [], [], {}
            succeeded = 0
            for discovery_query in queries:
                try:
                    found = search(discovery_query, repo=_REPO, topk=20)
                except (OSError, sqlite3.Error, ValueError, KeyError, yaml.YAMLError) as exc:
                    issues.append({"query": discovery_query, "error": str(exc)} if subjects else str(exc))
                    continue
                succeeded += 1
                last_found = found
                issues.extend(found.get("issues", []))
                lanes.append(found["candidates"])
            if not succeeded:
                channels[name] = {"status": "unavailable", "count": 0, "issues": issues}
                if subjects:
                    channels[name].update(queries=queries, search_count=len(queries))
                continue
            found = {**last_found, "candidates": _interleave_candidates(lanes, 20,
                (lambda item: (item["source_id"], item["parse_id"], item["locator"])) if name == "source"
                else (lambda item: item["id"]),
                prefer=lambda item: (*_subject_key(_subject_features(subjects, item)),
                                     *lr.rank_key(_question_match(query, item))))} if subjects else last_found
            channels[name] = {"status": "partial" if issues else "ok",
                              "count": len(found["candidates"]), "issues": issues,
                              "candidate_window": 20, "window_full": len(found["candidates"]) >= 20}
            if subjects:
                channels[name].update(queries=queries, search_count=len(queries))
            if name == "knowledge":
                for candidate in found["candidates"]:
                    groups[name].append({**candidate, "read_action": "knowledge_read",
                                         "read_input": {"unit_id": candidate["id"]}})
            else:
                if not found.get("index_available", True):
                    channels[name].update(status="unavailable", issues=["source index absent; use source resume to rebuild registered sources"])
                # Several passages of one immutable source are alternative reading locations, not votes.
                sources = {}
                if subjects:
                    # Sort whole passage objects, so the selected read address, excerpt
                    # and source metadata can never come from different fragments.
                    found["candidates"].sort(key=lambda item: (
                        item.get("section_role") == "references" if found.get("passage_preference") != "literal" else False,
                        *_subject_key(_subject_features(subjects, item)),
                        *lr.rank_key(_question_match(query, item))))
                for candidate in found["candidates"]:
                    identity = (candidate["source_id"], candidate["parse_id"])
                    if identity not in sources:
                        sources[identity] = {**candidate, "id": ":".join(identity), "path": candidate["source_path"],
                            "read_action": "read_raw", "read_input": {"locator": candidate["locator"]},
                            "matched_locators": []}
                    locators = sources[identity]["matched_locators"]
                    if candidate["locator"] not in locators and len(locators) < 3:
                        locators.append(candidate["locator"])
                groups[name] = list(sources.values())
                channels[name]["source_count"] = len(sources)
    else:
        for name in ("knowledge", "source"):
            channels[name] = {"status": "not_applicable", "count": 0, "issues": []}

    # Compare shared literal signals before channel rank; keep weak matches as
    # navigation fallbacks and never promote relevance into scientific confidence.
    import lexical_relevance as lr
    candidates = []
    for name, items in groups.items():
        for rank, item in enumerate(items, 1):
            features = item.get("lexical_match") or lr.match(query, item.get("title", ""),
                item.get("excerpt", "") + "\n" + item.get("navigation", "") + "\n" + item.get("capsule", ""))
            if subjects:
                features = _question_match(query, item)
                item = {**item, "subject_match": item.get("subject_match") or _subject_features(subjects, item)}
            candidates.append({**item, "score": round(1.0/(60+rank), 8), "lexical_match": features,
                "retrieval_channel": name, "channel_rank": rank, "evidence_read": False})
    candidates.sort(key=lambda item: (*(_subject_key(item["subject_match"]) if subjects else ()),
        *lr.rank_key(item["lexical_match"]), -item["score"], item["retrieval_channel"], item["id"]))
    result = {"schema": "unified-recall-v1", "query": query, "intent": intent, "domain": domain or "all",
        "fusion": "literal_coverage_then_rank", "navigation_fusion": navigation["fusion"],
        "navigation_weights": navigation["weights"], "channels": channels,
        "include_deprecated": include_deprecated, "count": min(limit, len(candidates)),
        "candidates": candidates[:limit], "truncated": len(candidates)>limit,
        "identity_claim": False, "fact_claim": False,
        "next": "follow each candidate's read_action/read_input; Wiki source_links are document navigation, not section evidence; verify facts with read_raw"}
    if subjects:
        result.update(subjects=subjects, fusion="subject_then_local_coverage_then_rank",
                      ranking_text="title_and_at_most_700_chars_of_existing_candidate_preview")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


@scoped_query
def wiki_context_data(page: str, section: str, profile: str = "explanation",
                      topk: int = 12) -> dict:
    """Read one semantic address and attach a bounded, derived Graph envelope."""
    located = read_wiki_data(page, section)
    node = located["page"].removesuffix(".md")
    conn = _connect()
    try:
        relation_result = qg.relations(
            conn, node, top_k=max(1, min(int(topk), 30)), profile=profile
        )
        context = []
        for edge in relation_result.get("edges", []):
            other = edge["object"] if edge["subject"] == node else edge["subject"]
            info = qg.node_info(conn, other) or {"path": other, "title": other}
            context.append({
                "relation": edge["predicate"],
                "direction": "out" if edge["subject"] == node else "in",
                "family": edge.get("family", ""),
                "locator": edge.get("locator", ""),
                "node": info,
            })
    finally:
        conn.close()
    return {
        "semantic_address": f"{node}#{located['section']}",
        "wiki": located,
        "graph_context": {"profile": profile, "relations": context,
                          "version_context": relation_result.get("version_context", {})},
    }


def wiki_context(page: str = "", section: str = "", profile: str = "explanation",
                 topk: str = "12") -> tuple[str, int]:
    if not page:
        return "[ERROR 缺 page]", 0
    try:
        result = wiki_context_data(page, section, profile, int(topk))
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return f"[ERROR {exc}]", _tok(str(exc))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)

RAW_PREVIEW_CHARS = 12000


@scoped_query
def read_raw(locator: str = "") -> tuple[str, int]:
    """按精确 locator 读 raw 片段；拒绝裸路径和 #全篇。
    复用 source_locator 解析路径+定位器；companion 可承载读取，引用保留原件。"""
    if not locator.strip():
        return "[ERROR 缺 locator]", 0
    path_part, loc = sl.split_locator(locator)
    if not path_part:
        path_part = locator
    target = sl.resolve_path(path_part)
    if target is None:
        return f"[ERROR raw 路径未解析: {path_part}]", 0
    check_path_scope(target)
    if not loc or loc == "全篇":
        return "[ERROR read_raw 需要精确 locator（标题、Lx-Ly 或 page-x-y）；不向 LLM 返回全文]", 0
    companion_binding = sl.companion_binding_for_target(target)
    if companion_binding and companion_binding["status"] == "invalid":
        return "[ERROR companion 与原件绑定失效，须重新生成后再查询]", 0
    source_target, read_target = sl.evidence_targets(target, loc)
    check_path_scope(source_target)
    check_path_scope(read_target)
    source_rel = str(source_target.resolve().relative_to(_REPO))
    read_rel = str(read_target.resolve().relative_to(_REPO))
    status = sl.locator_status(loc, read_target)
    if status == "missing":
        return f"[ERROR locator '{loc}' 在 {read_rel} 中未找到]", 0
    seg = sl.read_locator_text(read_target, loc)
    if seg is None:
        return f"[ERROR locator '{loc}' 已验证但无法精确截取；未返回全文]", 0
    if len(seg) > RAW_PREVIEW_CHARS:
        guidance = sl.read_size_guidance(f"{read_rel}#{loc}", seg, RAW_PREVIEW_CHARS)
        return (f"[ERROR locator '{loc}' 命中 {len(seg)} 字符，上限 {RAW_PREVIEW_CHARS}；"
                "范围过大，请细化 locator。可选择read_size.suggested_reads再读取；"
                "候选未读，未返回半截内容]\n" +
                json.dumps({"read_size": guidance}, ensure_ascii=False)), 0
    header = f"[raw source={source_rel}; evidence={read_rel}#{loc}]\n"
    return header + seg, _tok(header + seg)


def raw_citation_sources(locator: str) -> list[str]:
    """Return verified citation aliases after a successful ``read_raw`` call."""
    path_part, loc = sl.split_locator(locator)
    target = sl.resolve_path(path_part or locator)
    if target is None or not loc:
        return [locator] if locator else []
    source_target, _ = sl.evidence_targets(target, loc)
    source_rel = str(source_target.resolve().relative_to(_REPO))
    return list(dict.fromkeys([locator, source_rel]))


@scoped_query
def source_search(query: str, domain: str = "", topk: str = "5") -> tuple[str, int]:
    """Find source-only Raw evidence without requiring Wiki or Graph entries."""
    if scope_for() == "private":
        raise ValueError("Source-first search is academic-only; private sources remain isolated")
    from source_ingest import search
    payload = search(query, repo=_REPO, domain=domain, topk=int(topk))
    text = json.dumps(payload, ensure_ascii=False)
    return text, _tok(text)


@scoped_query
def source_outline(source_id: str, query: str = "", start_line: str = "1", limit: str = "40") -> tuple[str, int]:
    """Locate explicit headings/captions within one known public source."""
    if scope_for() == "private":
        raise ValueError("Source-first outline is academic-only; private sources remain isolated")
    from source_ingest import outline
    text = json.dumps(outline(source_id, repo=_REPO, query=query, start_line=int(start_line), limit=int(limit)), ensure_ascii=False)
    return text, _tok(text)


@scoped_query
def knowledge_search(query: str, domain: str = "", topk: str = "5") -> tuple[str, int]:
    if scope_for() == "private" or domain not in {"", "academic", "public"}:
        raise ValueError("KU search currently supports academic public sources only")
    import knowledge_store as ks
    text = json.dumps(ks.search(query, repo=_REPO, topk=int(topk)), ensure_ascii=False)
    return text, _tok(text)


@scoped_query
def knowledge_read(unit_id: str) -> tuple[str, int]:
    if scope_for() == "private":
        raise ValueError("KU read currently supports academic public sources only")
    import knowledge_store as ks
    record = ks.read(unit_id, repo=_REPO)
    result = {k:record.get(k) for k in ("id", "kind", "title", "statement", "revision", "status", "origin_kind",
              "claim_mode", "content_role", "source_question_status", "evidence", "unresolved", "kb_state", "scientific_state", "answer_status", "current_answer", "anchors", "source_locator")}
    result["evidence_issues"] = ks.evidence_status(record, repo=_REPO)
    result["next"] = "read_raw on relevant evidence locator; KU is a revisable interpretation, not a fact source"
    text = json.dumps(result, ensure_ascii=False)
    return text, _tok(text)


DISPATCH = {
    "source_outline": lambda inp: source_outline(inp.get("source_id", ""), inp.get("query", ""), inp.get("start_line", "1"), inp.get("limit", "40")),
    "knowledge_search": lambda inp: knowledge_search(inp.get("query", ""), inp.get("domain", ""), inp.get("topk", "5")),
    "knowledge_read": lambda inp: knowledge_read(inp.get("unit_id", "")),
    "source_search": lambda inp: source_search(inp.get("query", ""), inp.get("domain", ""), inp.get("topk", "5")),
    "read_section": lambda inp: read_section(inp.get("page", ""), inp.get("section", "")),
    "read_raw": lambda inp: read_raw(inp.get("locator", "")),
    "graph_search": lambda inp: graph_search(inp.get("term", ""), inp.get("include_deprecated", False)),
    "graph_neighbors": lambda inp: graph_neighbors(
        inp.get("node", ""), inp.get("depth", "2"), inp.get("similar_topk", ""),
        inp.get("profile", ""), inp.get("families", "")
    ),
    "graph_relations": lambda inp: graph_relations(
        inp.get("node", ""), inp.get("predicate", ""),
        inp.get("profile", ""), inp.get("families", "")
    ),
    "graph_hub_of": lambda inp: graph_hub_of(inp.get("page", "")),
    "node_resolve": lambda inp: node_resolve(
        inp.get("name", ""), inp.get("context", ""), inp.get("node_types", ""), inp.get("topk", "5")
    ),
    "semantic_search": lambda inp: semantic_search(
        inp.get("query", ""), inp.get("scope", "node"), inp.get("topk", "8"), inp.get("include_deprecated", False)
    ),
    "hub_route": lambda inp: hub_route(inp.get("page", ""), inp.get("topk", "5")),
    "hub_inspect": lambda inp: hub_inspect(inp.get("hub", "")),
    "admin_recall": lambda inp: admin_recall(inp.get("query", ""), inp.get("topk", "8"), inp.get("include_deprecated", False)),
    "wiki_recall": lambda inp: wiki_recall(inp.get("query", ""), inp.get("domain", ""), inp.get("topk", "8"), inp.get("include_deprecated", False)),
    "hybrid_recall": lambda inp: hybrid_recall(
        inp.get("query", ""), inp.get("intent", "exploration"),
        inp.get("domain", ""), inp.get("topk", "8"), inp.get("include_deprecated", False), inp.get("subjects")
    ),
    "wiki_context": lambda inp: wiki_context(
        inp.get("page", ""), inp.get("section", ""),
        inp.get("profile", "explanation"), inp.get("topk", "12")
    ),
}

def execute(action: str, input_: dict, *, subproject: str = "") -> dict:
    fn = DISPATCH.get(action)
    if fn is None:
        return {"ok": False, "text": "", "tokens": 0,
                "error": f"未知动作 {action}(允许: {list(DISPATCH)})"}
    try:
        inputs = dict(input_ or {})
        requested = inputs.pop("subproject", "")
        scope = scope_for(subproject or requested or inputs.get("domain", ""),
                          *(inputs.get(k, "") for k in ("page", "locator", "node", "hub", "name")))
        with query_scope(scope):
            if requested and scope_for(requested) != scope:
                raise ValueError("query subproject conflicts with caller scope")
            text, tokens = fn(inputs)
        ok = not text.startswith("[ERROR")
        import artifact_usage
        result = {"ok": ok, "text": text, "tokens": tokens, "error": "" if ok else text[:300]}
        usage = artifact_usage.for_action(action, ok=ok)
        if usage:
            result["usage"] = usage
        return result
    except Exception as e:
        return {"ok": False, "text": "", "tokens": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
